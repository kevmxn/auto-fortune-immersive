#!/usr/bin/env python3
"""
Speed Roulette Stats Server — Recopilador Multi-Ruleta y Servidor WebSocket
===========================================================================
  - Usa aiohttp: HTTP + WebSocket en el MISMO puerto (elimina error HEAD).
  - Se conecta a 7 ruletas Pragmatic Play simultáneamente.
  - Los clientes (bots) se SUSCRIBEN a una ruleta específica.
  - Solo recibe actualizaciones de la ruleta a la que está suscrito.
  - Estadísticas HISTÓRICAS ACUMULADAS (no se borran).
  - Últimos 200 giros por ruleta (rotativos).
  - Self-ping para Render.
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
import urllib.request
from collections import defaultdict
from typing import Optional, Dict, List

import aiohttp
from aiohttp import web, WSMsgType
import websockets

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s [StatsServer] %(levelname)s %(message)s')
logger = logging.getLogger("StatsServer")
for _ln in ['aiohttp.access', 'aiohttp.server', 'urllib3']:
    logging.getLogger(_ln).setLevel(logging.ERROR)

# ─── CONFIGURACIÓN ────────────────────────────────────────────────────────────
WS_URL_PRAGMATIC = "wss://dga.pragmaticplaylive.net/ws"
CASINO_ID = "ppcjd00000007254"
RENDER_EXTERNAL_URL = "https://ruletasbot-rjce.onrender.com"

ROULETTES = {
    "SPEED1":    {"key": 203, "name": "Speed Roulette 1"},
    "SPEED2":    {"key": 205, "name": "Speed Roulette 2"},
    "MACAO":     {"key": 206, "name": "Roulette Macao"},
    "RUSSIAN":   {"key": 221, "name": "Roulette Russian"},
    "ALEMANA":   {"key": 222, "name": "Roulette Alemana"},
    "ITALIANA":  {"key": 223, "name": "Roulette Italiana"},
    "TURKISH":   {"key": 224, "name": "Roulette Turkish"},
}

STATS_DB = "roulette_stats.db"
MAX_STORED_SPINS = 200

# ─── BASE DE DATOS ────────────────────────────────────────────────────────────
def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(STATS_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS spins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        roulette TEXT NOT NULL,
        game_id TEXT NOT NULL UNIQUE,
        number INTEGER NOT NULL,
        ts INTEGER NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_spins_roulette ON spins(roulette, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_spins_gameid ON spins(game_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS transitions (
        roulette TEXT NOT NULL,
        from_number INTEGER NOT NULL,
        d0 INTEGER DEFAULT 0, d1 INTEGER DEFAULT 0, d2 INTEGER DEFAULT 0, d3 INTEGER DEFAULT 0,
        c0 INTEGER DEFAULT 0, c1 INTEGER DEFAULT 0, c2 INTEGER DEFAULT 0, c3 INTEGER DEFAULT 0,
        total INTEGER DEFAULT 0,
        PRIMARY KEY(roulette, from_number)
    )""")
    conn.commit()
    return conn

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def get_dozen(n: int) -> int:
    if n == 0: return 0
    return (n - 1) // 12 + 1

def get_column(n: int) -> int:
    if n == 0: return 0
    return ((n - 1) % 3) + 1

# ─── MOTOR DE ESTADÍSTICAS ───────────────────────────────────────────────────
class StatsEngine:
    def __init__(self):
        self.db = _get_db()
        self.last_numbers: Dict[str, Optional[int]] = {r: None for r in ROULETTES}
        self.last_game_ids: Dict[str, str] = {r: "" for r in ROULETTES}
        self.client_subscriptions: Dict[aiohttp.web.WebSocketResponse, str] = {}
        self._load_last_states()

    def _load_last_states(self):
        for roulette in ROULETTES:
            row = self.db.execute(
                "SELECT number, game_id FROM spins WHERE roulette=? ORDER BY id DESC LIMIT 1",
                (roulette,)
            ).fetchone()
            if row:
                self.last_numbers[roulette] = row["number"]
                self.last_game_ids[roulette] = row["game_id"]
                logger.info(f"[{roulette}] Último estado: #{row['number']}")
            else:
                logger.info(f"[{roulette}] Sin datos previos")

    def process_spin(self, roulette: str, number: int, game_id: str):
        """Procesar un nuevo giro, actualizar DB y estadísticas."""
        exists = self.db.execute("SELECT 1 FROM spins WHERE game_id=?", (game_id,)).fetchone()
        if exists:
            return

        ts = int(time.time())

        # 1. Insertar giro
        self.db.execute(
            "INSERT INTO spins(roulette, game_id, number, ts) VALUES(?,?,?,?)",
            (roulette, game_id, number, ts)
        )

        # 2. Actualizar estadísticas de transición
        prev_num = self.last_numbers.get(roulette)
        if prev_num is not None:
            d = get_dozen(number)
            c = get_column(number)

            self.db.execute(
                "INSERT OR IGNORE INTO transitions(roulette, from_number) VALUES(?,?)",
                (roulette, prev_num)
            )

            d_col = f"d{d}"
            c_col = f"c{c}"
            self.db.execute(f"""
                UPDATE transitions 
                SET {d_col} = {d_col} + 1, 
                    {c_col} = {c_col} + 1, 
                    total = total + 1 
                WHERE roulette=? AND from_number=?
            """, (roulette, prev_num))

        self.db.commit()

        # 3. Actualizar último número en memoria
        self.last_numbers[roulette] = number
        self.last_game_ids[roulette] = game_id

        # 4. Limpiar giros antiguos
        self._cleanup_old_spins(roulette)

    def _cleanup_old_spins(self, roulette: str):
        self.db.execute("""
            DELETE FROM spins 
            WHERE roulette=? AND id NOT IN (
                SELECT id FROM spins WHERE roulette=? ORDER BY id DESC LIMIT ?
            )
        """, (roulette, roulette, MAX_STORED_SPINS))

    def get_last_n_spins(self, roulette: str, n: int = 20) -> List[int]:
        rows = self.db.execute(
            "SELECT number FROM spins WHERE roulette=? ORDER BY id DESC LIMIT ?",
            (roulette, n)
        ).fetchall()
        return [r["number"] for r in rows]

    def get_total_spins(self, roulette: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) as cnt FROM spins WHERE roulette=?", (roulette,)
        ).fetchone()
        return row["cnt"] if row else 0

    def get_stats_table(self, roulette: str, cat_type: str) -> Dict[str, Dict]:
        """Obtener tabla de estadísticas completa (0-36)."""
        rows = self.db.execute(
            "SELECT * FROM transitions WHERE roulette=?", (roulette,)
        ).fetchall()
        
        db_data = {}
        for row in rows:
            db_data[row["from_number"]] = dict(row)

        result = {}
        for num in range(0, 37):
            data = db_data.get(num)
            if not data or data["total"] == 0:
                result[str(num)] = {"1": 0.0, "2": 0.0, "3": 0.0, "zero": 0.0, "total": 0}
                continue

            total = data["total"]
            if cat_type == "DOCENA":
                d0, d1, d2, d3 = data["d0"], data["d1"], data["d2"], data["d3"]
                result[str(num)] = {
                    "1": round(d1 / total * 100, 1),
                    "2": round(d2 / total * 100, 1),
                    "3": round(d3 / total * 100, 1),
                    "zero": round(d0 / total * 100, 1),
                    "total": total
                }
            else:
                c0, c1, c2, c3 = data["c0"], data["c1"], data["c2"], data["c3"]
                result[str(num)] = {
                    "1": round(c1 / total * 100, 1),
                    "2": round(c2 / total * 100, 1),
                    "3": round(c3 / total * 100, 1),
                    "zero": round(c0 / total * 100, 1),
                    "total": total
                }

        return result

    def get_full_state(self, roulette: str) -> dict:
        """Obtener el estado completo para enviar al bot."""
        return {
            "roulette": roulette,
            "roulette_name": ROULETTES.get(roulette, {}).get("name", roulette),
            "total_spins": self.get_total_spins(roulette),
            "last_20": self.get_last_n_spins(roulette, 20),
            "last_50": self.get_last_n_spins(roulette, 50),
            "stats_dozen": self.get_stats_table(roulette, "DOCENA"),
            "stats_column": self.get_stats_table(roulette, "COLUMNA")
        }

    def subscribe_client(self, ws: aiohttp.web.WebSocketResponse, roulette: str) -> bool:
        if roulette not in ROULETTES:
            return False
        self.client_subscriptions[ws] = roulette
        logger.info(f"🔗 Cliente suscrito a {roulette}")
        return True

    def unsubscribe_client(self, ws: aiohttp.web.WebSocketResponse):
        if ws in self.client_subscriptions:
            sub = self.client_subscriptions.pop(ws)
            logger.info(f"🔌 Cliente desuscrito de {sub}")

    async def broadcast_update(self, roulette: str, number: int):
        """Enviar actualización SOLO a clientes suscritos a esta ruleta."""
        if not self.client_subscriptions:
            return

        message = json.dumps({
            "type": "new_spin",
            "data": {
                "roulette": roulette,
                "number": number,
                "last_20": self.get_last_n_spins(roulette, 20),
                "stats_dozen": self.get_stats_table(roulette, "DOCENA"),
                "stats_column": self.get_stats_table(roulette, "COLUMNA")
            }
        })

        disconnected = []
        for client, sub_roulette in list(self.client_subscriptions.items()):
            if sub_roulette != roulette:
                continue
            try:
                await client.send_str(message)
            except Exception:
                disconnected.append(client)
        
        for client in disconnected:
            self.client_subscriptions.pop(client, None)


# ─── INSTANCIA GLOBAL ─────────────────────────────────────────────────────────
stats_engine = StatsEngine()

# ─── HANDLERS HTTP (aiohttp) ──────────────────────────────────────────────────
async def handle_home(request):
    """GET / — Estado general del servidor."""
    roulettes_status = {}
    for r_key, r_conf in ROULETTES.items():
        roulettes_status[r_key] = {
            "name": r_conf["name"],
            "total_spins": stats_engine.get_total_spins(r_key),
            "last_number": stats_engine.last_numbers.get(r_key)
        }
    return web.json_response({
        "status": "ok",
        "service": "Roulette Stats Server",
        "connected_bots": len(stats_engine.client_subscriptions),
        "roulettes": roulettes_status
    })

async def handle_ping(request):
    """GET /ping — Keep-alive para Render."""
    return web.json_response({"status": "pong", "ts": time.time()})

async def handle_health(request):
    """GET /health — Estado de todas las ruletas."""
    return web.json_response({
        "status": "ok",
        "roulettes": {r: stats_engine.get_total_spins(r) for r in ROULETTES}
    })

async def handle_stats_dozen(request):
    """GET /stats/{roulette}/dozen — Tabla de docenas."""
    roulette = request.match_info.get("roulette", "").upper()
    if roulette not in ROULETTES:
        return web.json_response({"error": f"Ruleta no encontrada. Disponibles: {list(ROULETTES.keys())}"}, status=404)
    return web.json_response(stats_engine.get_stats_table(roulette, "DOCENA"))

async def handle_stats_column(request):
    """GET /stats/{roulette}/column — Tabla de columnas."""
    roulette = request.match_info.get("roulette", "").upper()
    if roulette not in ROULETTES:
        return web.json_response({"error": f"Ruleta no encontrada. Disponibles: {list(ROULETTES.keys())}"}, status=404)
    return web.json_response(stats_engine.get_stats_table(roulette, "COLUMNA"))

async def handle_spins(request):
    """GET /spins/{roulette}/{n} — Últimos N giros."""
    roulette = request.match_info.get("roulette", "").upper()
    if roulette not in ROULETTES:
        return web.json_response({"error": f"Ruleta no encontrada. Disponibles: {list(ROULETTES.keys())}"}, status=404)
    try:
        n = min(int(request.match_info.get("n", "20")), 200)
    except ValueError:
        n = 20
    return web.json_response(stats_engine.get_last_n_spins(roulette, n))


# ─── HANDLER WEBSOCKET (aiohttp) ─────────────────────────────────────────────
async def handle_websocket(request):
    """WebSocket endpoint — Los bots se conectan aquí."""
    ws = aiohttp.web.WebSocketResponse()
    await ws.prepare(request)
    
    logger.info(f"🔗 Nuevo cliente WebSocket conectado")
    
    # Enviar lista de ruletas disponibles
    available = {k: v["name"] for k, v in ROULETTES.items()}
    await ws.send_str(json.dumps({
        "type": "welcome",
        "available_roulettes": available,
        "message": "Send {\"type\":\"subscribe\",\"roulette\":\"SPEED2\"} to start receiving data"
    }))
    
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    req_type = data.get("type")
                    
                    if req_type == "subscribe":
                        roulette = data.get("roulette", "").upper()
                        if stats_engine.subscribe_client(ws, roulette):
                            state = stats_engine.get_full_state(roulette)
                            await ws.send_str(json.dumps({"type": "full_state", "data": state}))
                        else:
                            await ws.send_str(json.dumps({
                                "type": "error",
                                "message": f"Ruleta '{roulette}' no encontrada. Disponibles: {list(ROULETTES.keys())}"
                            }))
                            
                    elif req_type == "get_state":
                        roulette = data.get("roulette", "SPEED2").upper()
                        state = stats_engine.get_full_state(roulette)
                        await ws.send_str(json.dumps({"type": "full_state", "data": state}))
                        
                    elif req_type == "get_last_20":
                        roulette = data.get("roulette", "SPEED2").upper()
                        spins = stats_engine.get_last_n_spins(roulette, 20)
                        await ws.send_str(json.dumps({"type": "last_20", "roulette": roulette, "data": spins}))
                        
                    elif req_type == "get_stats":
                        roulette = data.get("roulette", "SPEED2").upper()
                        cat = data.get("category", "DOCENA")
                        stats = stats_engine.get_stats_table(roulette, cat)
                        await ws.send_str(json.dumps({"type": "stats", "roulette": roulette, "category": cat, "data": stats}))
                        
                except json.JSONDecodeError:
                    await ws.send_str(json.dumps({"type": "error", "message": "Invalid JSON"}))
                    
            elif msg.type == WSMsgType.ERROR:
                logger.error(f"WS error: {ws.exception()}")
                
    except Exception as e:
        logger.warning(f"WebSocket desconectado: {e}")
    finally:
        stats_engine.unsubscribe_client(ws)
    
    return ws


# ─── CLIENTE PRAGMATIC PLAY ──────────────────────────────────────────────────
async def connect_pragmatic(roulette_key: str, roulette_config: dict):
    """Conectar a Pragmatic Play y recibir giros para una ruleta."""
    key = roulette_config["key"]
    name = roulette_config["name"]
    reconnect_delay = 5
    
    while True:
        try:
            async with websockets.connect(
                WS_URL_PRAGMATIC, 
                ping_interval=30, 
                ping_timeout=60, 
                close_timeout=10
            ) as ws:
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "key": key,
                    "casinoId": CASINO_ID
                }))
                logger.info(f"✅ [{roulette_key}] Conectado a {name} (key={key})")
                reconnect_delay = 5
                
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    
                    if not isinstance(data, dict):
                        continue
                    
                    # Procesar lote inicial/actualización
                    results = data.get("last20Results")
                    if results and isinstance(results, list):
                        for result in reversed(results):
                            gid = str(result.get("gameId", ""))
                            if gid == stats_engine.last_game_ids.get(roulette_key, ""):
                                continue
                            try:
                                n = int(result.get("result", ""))
                                if 0 <= n <= 36:
                                    stats_engine.process_spin(roulette_key, n, gid)
                                    await stats_engine.broadcast_update(roulette_key, n)
                            except (ValueError, TypeError):
                                pass
                        continue
                    
                    # Procesar resultado individual
                    for res_key in ("result", "number", "outcome", "winningNumber"):
                        if res_key in data:
                            gid = str(data.get("gameId", f"{roulette_key}_{int(time.time()*1000)}"))
                            try:
                                n = int(data[res_key])
                                if 0 <= n <= 36:
                                    stats_engine.process_spin(roulette_key, n, gid)
                                    await stats_engine.broadcast_update(roulette_key, n)
                            except (ValueError, TypeError):
                                pass
                            break
                            
        except Exception as e:
            logger.warning(f"⚠️ [{roulette_key}] WS desconectado: {e}. Recon en {reconnect_delay}s")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)


# ─── SELF-PING ────────────────────────────────────────────────────────────────
async def self_ping_loop():
    url = RENDER_EXTERNAL_URL.rstrip("/")
    if not url:
        logger.info("⚠️ RENDER_EXTERNAL_URL no configurada. Self-ping desactivado.")
        return
    await asyncio.sleep(30)
    while True:
        try:
            urllib.request.urlopen(f"{url}/ping", timeout=15)
            logger.debug("Self-ping exitoso")
        except Exception as e:
            logger.warning(f"Self-ping falló: {e}")
        await asyncio.sleep(240)


# ─── TAREAS EN SEGUNDO PLANO ──────────────────────────────────────────────────
async def start_background_tasks(app):
    """Iniciar todas las tareas en segundo plano cuando arranca el servidor."""
    # Conectar a todas las ruletas
    for roulette_key, roulette_config in ROULETTES.items():
        app[f"task_{roulette_key}"] = asyncio.create_task(
            connect_pragmatic(roulette_key, roulette_config)
        )
        await asyncio.sleep(0.3)  # Escalonar conexiones
    
    # Self-ping
    app["task_ping"] = asyncio.create_task(self_ping_loop())
    
    logger.info(f"🎰 {len(ROULETTES)} tareas de recopilación iniciadas")

async def cleanup_background_tasks(app):
    """Limpiar tareas al cerrar el servidor."""
    for key in list(app.keys()):
        if key.startswith("task_"):
            app[key].cancel()
            try:
                await app[key]
            except asyncio.CancelledError:
                pass


# ─── APLICACIÓN PRINCIPAL ─────────────────────────────────────────────────────
def create_app():
    """Crear y configurar la aplicación aiohttp."""
    app = web.Application()
    
    # ─── Rutas HTTP ────────────────────────────────────────────────
    app.router.add_get("/", handle_home)
    app.router.add_get("/ping", handle_ping)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/stats/{roulette}/dozen", handle_stats_dozen)
    app.router.add_get("/stats/{roulette}/column", handle_stats_column)
    app.router.add_get("/spins/{roulette}/{n}", handle_spins)
    
    # ─── Ruta WebSocket ────────────────────────────────────────────
    app.router.add_get("/ws", handle_websocket)
    
    # ─── Tareas en segundo plano ──────────────────────────────────
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10004))
    app = create_app()
    logger.info(f"🚀 Stats Server iniciando en puerto {port}")
    web.run_app(app, host="0.0.0.0", port=port, access_log=None)
