import asyncio
import json
import logging
import random
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Set

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─── Configuración ───────────────────────────────────────────────────────────

BASE_URL   = "https://api-cs.casino.org/svc-evolution-game-events/api/autoroulette"
POLL_BULK  = 20      # segundos entre carga inicial / re-sync completo
POLL_LIVE  = 2       # segundos entre peticiones /latest
MAX_ITEMS  = 20      # últimos N resultados a mantener
PING_EVERY = 60      # segundos entre self-ping (evita sleep en Render free tier)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# ─── Estado global ───────────────────────────────────────────────────────────

results: deque = deque(maxlen=MAX_ITEMS)   # últimos MAX_ITEMS resultados
known_ids: set = set()                      # IDs ya vistos
clients: Set[WebSocket] = set()            # clientes WS conectados
stats = {"requests": 0, "errors": 0, "blocked": 0, "last_id": None}

# ─── Helpers ─────────────────────────────────────────────────────────────────

def base_headers() -> dict:
    return {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "es-ES,es;q=0.9",
        "Origin": "https://www.casino.org",
        "Referer": "https://www.casino.org/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "User-Agent": random.choice(USER_AGENTS),
    }

def parse_item(raw: dict) -> dict | None:
    """Extrae solo los campos que necesitamos."""
    try:
        outcome = raw["data"]["result"]["outcome"]
        return {
            "id":     raw["id"],
            "number": outcome["number"],
            "color":  outcome.get("color", "Green"),
            "type":   outcome.get("type", ""),
            "ts":     raw["data"].get("settledAt", ""),
        }
    except (KeyError, TypeError):
        return None

async def broadcast(payload: dict):
    """Envía payload a todos los clientes WS conectados."""
    if not clients:
        return
    msg = json.dumps(payload)
    dead = set()
    for ws in clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    clients.difference_update(dead)

async def add_items(raw_list: list, is_live: bool = False):
    """Agrega items nuevos al buffer y notifica clientes."""
    new_items = []
    for raw in raw_list:
        item = parse_item(raw)
        if item and item["id"] not in known_ids:
            known_ids.add(item["id"])
            results.appendleft(item)
            new_items.append(item)
            stats["last_id"] = item["id"]

    if new_items:
        log.info(f"{'[LIVE]' if is_live else '[BULK]'} +{len(new_items)} nuevos → total buffer: {len(results)}")
        await broadcast({
            "event":  "update",
            "new":    new_items,
            "buffer": list(results),
        })

# ─── Fetcher con backoff + anti-bloqueo ──────────────────────────────────────

class Fetcher:
    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._fail_streak = 0
        self._last_request = 0.0

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0),
                follow_redirects=True,
                http2=True,
            )
        return self._client

    async def _throttle(self, min_gap: float = 1.0):
        """Garantiza un mínimo de tiempo entre peticiones."""
        elapsed = time.monotonic() - self._last_request
        if elapsed < min_gap:
            await asyncio.sleep(min_gap - elapsed + random.uniform(0.1, 0.4))

    async def get(self, url: str, params: dict | None = None) -> list | None:
        """GET con reintentos y backoff exponencial."""
        await self._throttle()
        max_retries = 4
        delay = 2.0

        for attempt in range(1, max_retries + 1):
            try:
                c = await self.client()
                r = await c.get(url, params=params, headers=base_headers())
                self._last_request = time.monotonic()
                stats["requests"] += 1

                if r.status_code == 200:
                    self._fail_streak = 0
                    data = r.json()
                    return data if isinstance(data, list) else [data]

                if r.status_code in (429, 403):
                    stats["blocked"] += 1
                    wait = delay * (2 ** attempt) + random.uniform(2, 6)
                    log.warning(f"[BLOQUEADO {r.status_code}] esperando {wait:.1f}s …")
                    await asyncio.sleep(wait)
                    continue

                log.warning(f"[HTTP {r.status_code}] intento {attempt}/{max_retries}")

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                stats["errors"] += 1
                log.warning(f"[RED] {e} — intento {attempt}/{max_retries}")

            except Exception as e:
                stats["errors"] += 1
                log.error(f"[ERROR] {e}")

            if attempt < max_retries:
                jitter = random.uniform(0.5, 2.0)
                await asyncio.sleep(delay * attempt + jitter)

        self._fail_streak += 1
        return None

    async def close(self):
        if self._client:
            await self._client.aclose()


fetcher = Fetcher()

# ─── Tareas en background ────────────────────────────────────────────────────

async def task_bulk():
    """Carga bulk cada POLL_BULK segundos (re-sync completo)."""
    while True:
        log.info("[BULK] Sincronizando datos iniciales …")
        data = await fetcher.get(
            BASE_URL,
            params={"page": 0, "size": 29, "sort": "data.settledAt,desc", "duration": 6},
        )
        if data:
            await add_items(data, is_live=False)
        else:
            log.warning("[BULK] Sin datos, reintentando en el próximo ciclo")
        await asyncio.sleep(POLL_BULK)


async def task_live():
    """Consulta /latest cada POLL_LIVE segundos."""
    # Espera inicial para dejar que bulk cargue primero
    await asyncio.sleep(5)
    while True:
        data = await fetcher.get(f"{BASE_URL}/latest")
        if data:
            await add_items(data, is_live=True)
        await asyncio.sleep(POLL_LIVE + random.uniform(0.1, 0.5))


async def task_ping():
    """Self-ping al endpoint /ping para evitar sleep en Render free tier."""
    import os
    await asyncio.sleep(10)
    host = os.getenv("RENDER_EXTERNAL_URL", "http://localhost:8000")
    url  = f"{host}/ping"
    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(url)
                log.debug(f"[PING] self-ping → {r.status_code}")
        except Exception as e:
            log.debug(f"[PING] error: {e}")
        await asyncio.sleep(PING_EVERY)


# ─── FastAPI + lifespan ───────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Iniciando tareas en background …")
    tasks = [
        asyncio.create_task(task_bulk(), name="bulk"),
        asyncio.create_task(task_live(), name="live"),
        asyncio.create_task(task_ping(), name="ping"),
    ]
    yield
    for t in tasks:
        t.cancel()
    await fetcher.close()
    log.info("Servidor apagado.")


app = FastAPI(title="AutoRoulette Proxy", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ─── Rutas HTTP ───────────────────────────────────────────────────────────────

@app.get("/ping")
async def ping():
    return {"status": "ok", "ts": time.time()}


@app.get("/stats")
async def get_stats():
    return {
        **stats,
        "buffer_size": len(results),
        "clients":     len(clients),
    }


@app.get("/results")
async def get_results():
    return {"data": list(results)}


# ─── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    client_ip = ws.client.host if ws.client else "?"
    log.info(f"[WS] Cliente conectado: {client_ip} | total: {len(clients)}")

    # Envía el buffer actual al conectarse
    try:
        await ws.send_text(json.dumps({
            "event":  "init",
            "buffer": list(results),
        }))

        # Mantiene la conexión viva con pings cada 30s
        while True:
            await asyncio.sleep(30)
            await ws.send_text(json.dumps({"event": "ping", "ts": time.time()}))

    except WebSocketDisconnect:
        log.info(f"[WS] Cliente desconectado: {client_ip}")
    except Exception as e:
        log.warning(f"[WS] Error con {client_ip}: {e}")
    finally:
        clients.discard(ws)
