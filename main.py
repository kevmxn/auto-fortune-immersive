#!/usr/bin/env python3
"""
Speed Roulette Stats Server — Recopilador Multi-Ruleta (FIXED)
===========================================================================
CAMBIOS:
  - Thread-safe DB access con asyncio.Lock
  - Query SQLite corregida (sin alias incompleto)
  - Mejor manejo de errores
  - Logging mejorado
"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from collections import defaultdict
from typing import Optional, Dict, List

import aiohttp
from aiohttp import web, WSMsgType
import websockets

logging.basicConfig(level=logging.INFO, format='%(asctime)s [StatsServer] %(levelname)s %(message)s')
logger = logging.getLogger("StatsServer")
for _ln in ['aiohttp.access', 'aiohttp.server', 'urllib3']:
    logging.getLogger(_ln).setLevel(logging.ERROR)

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

# ─── DB CONNECTION POOL ────────────────────────────────────────────────────────
class DBPool:
    """Thread-safe database access for async context"""
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.lock = asyncio.Lock()
        self._init_db()
    
    def _init_db(self):
        """Initialize database schema"""
        conn = sqlite3.connect(self.db_path)
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
        conn.close()
        logger.info(f"✅ DB initialized: {db_path}")
    
    async def execute(self, query: str, params: tuple = ()):
        """Thread-safe async execute"""
        async with self.lock:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                return conn.execute(query, params).fetchall()
            finally:
                conn.close()
    
    async def execute_single(self, query: str, params: tuple = ()):
        """Thread-safe async execute (single row)"""
        async with self.lock:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                return conn.execute(query, params).fetchone()
            finally:
                conn.close()
    
    async def commit(self, query: str, params: tuple = ()):
        """Thread-safe async write"""
        async with self.lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute(query, params)
                conn.commit()
            finally:
                conn.close()

db_pool = DBPool(STATS_DB)

def get_dozen(n: int) -> int:
    if n == 0: return 0
    return (n - 1) // 12 + 1

def get_column(n: int) -> int:
    if n == 0: return 0
    return ((n - 1) % 3) + 1

class StatsEngine:
    def __init__(self):
        self.last_numbers: Dict[str, Optional[int]] = {r: None for r in ROULETTES}
        self.last_game_ids: Dict[str, str] = {r: "" for r in ROULETTES}
        self.client_subscriptions: Dict = {}
        asyncio.create_task(self._load_last_states())

    async def _load_last_states(self):
        for roulette in ROULETTES:
            row = await db_pool.execute_single("SELECT number, game_id FROM spins WHERE roulette=? ORDER BY id DESC LIMIT 1", (roulette,))
            if row: 
                self.last_numbers[roulette] = row["number"]
                self.last_game_ids[roulette] = row["game_id"]
                logger.info(f"[{roulette}] Último: #{row['number']}")
            else: 
                logger.info(f"[{roulette}] Sin datos")

    async def process_spin(self, roulette: str, number: int, game_id: str) -> bool:
        # Verificar duplicado
        existing = await db_pool.execute_single("SELECT 1 FROM spins WHERE game_id=?", (game_id,))
        if existing:
            return False
        
        # Insertar nuevo spin
        await db_pool.commit("INSERT INTO spins(roulette, game_id, number, ts) VALUES(?,?,?,?)", 
                           (roulette, game_id, number, int(time.time())))
        
        # Procesar transición
        prev_num = self.last_numbers.get(roulette)
        if prev_num is not None and prev_num != 0:
            d = get_dozen(number)
            c = get_column(number)
            await db_pool.commit("INSERT OR IGNORE INTO transitions(roulette, from_number) VALUES(?,?)", 
                               (roulette, prev_num))
            await db_pool.commit(
                f"UPDATE transitions SET d{d} = d{d} + 1, c{c} = c{c} + 1, total = total + 1 WHERE roulette=? AND from_number=?",
                (roulette, prev_num)
            )
        
        self.last_numbers[roulette] = number
        self.last_game_ids[roulette] = game_id
        await self._cleanup_old_spins(roulette)
        return True

    async def _cleanup_old_spins(self, roulette):
        """Limpia spins viejos (mantiene últimos MAX_STORED_SPINS)"""
        # Query CORREGIDA: usar subquery con OFFSET instead of relying on implicit ordering
        await db_pool.commit(
            """DELETE FROM spins WHERE roulette=? AND id NOT IN 
               (SELECT id FROM spins WHERE roulette=? ORDER BY id DESC LIMIT ?)""",
            (roulette, roulette, MAX_STORED_SPINS)
        )

    async def get_last_n_spins(self, roulette, n=20):
        rows = await db_pool.execute(
            "SELECT number, game_id FROM spins WHERE roulette=? ORDER BY id DESC LIMIT ?", 
            (roulette, n)
        )
        return [{"number": r["number"], "game_id": r["game_id"]} for r in rows]

    async def get_total_spins(self, roulette):
        row = await db_pool.execute_single("SELECT COUNT(*) as cnt FROM spins WHERE roulette=?", (roulette,))
        return row["cnt"] if row else 0

    async def get_stats_table(self, roulette, cat_type):
        rows = await db_pool.execute("SELECT * FROM transitions WHERE roulette=?", (roulette,))
        db_data = {row["from_number"]: dict(row) for row in rows}
        result = {}
        for num in range(0, 37):
            data = db_data.get(num)
            if not data or data["total"] == 0: 
                result[str(num)] = {"1":0.0,"2":0.0,"3":0.0,"zero":0.0,"total":0}
                continue
            total = data["total"]
            if cat_type == "DOCENA": 
                result[str(num)] = {
                    "1":round(data["d1"]/total*100,1),
                    "2":round(data["d2"]/total*100,1),
                    "3":round(data["d3"]/total*100,1),
                    "zero":round(data["d0"]/total*100,1),
                    "total":total
                }
            else: 
                result[str(num)] = {
                    "1":round(data["c1"]/total*100,1),
                    "2":round(data["c2"]/total*100,1),
                    "3":round(data["c3"]/total*100,1),
                    "zero":round(data["c0"]/total*100,1),
                    "total":total
                }
        return result

    async def get_latest_data(self, roulette):
        """Todo en una sola consulta para el polling del bot."""
        return {
            "roulette": roulette,
            "roulette_name": ROULETTES.get(roulette, {}).get("name", roulette),
            "total_spins": await self.get_total_spins(roulette),
            "last_20": await self.get_last_n_spins(roulette, 20),
            "stats_dozen": await self.get_stats_table(roulette, "DOCENA"),
            "stats_column": await self.get_stats_table(roulette, "COLUMNA")
        }

    async def broadcast_update(self, roulette, number, game_id):
        if not self.client_subscriptions: return
        data = await self.get_latest_data(roulette)
        message = json.dumps({"type": "new_spin", "data": {**data, "number": number, "game_id": game_id}})
        disconnected = []
        for client, sub in list(self.client_subscriptions.items()):
            if sub != roulette: continue
            try: 
                await client.send_str(message)
            except: 
                disconnected.append(client)
        for c in disconnected: 
            self.client_subscriptions.pop(c, None)

stats_engine = StatsEngine()

# ─── HANDLERS HTTP ────────────────────────────────────────────────────────────
async def handle_home(request):
    rs = {}
    for k, v in ROULETTES.items():
        total = await stats_engine.get_total_spins(k)
        rs[k] = {"name": v["name"], "total": total, "last": stats_engine.last_numbers.get(k)}
    return web.json_response({"status": "ok", "roulettes": rs, "ws_clients": len(stats_engine.client_subscriptions)})

async def handle_ping(request):
    return web.json_response({"status": "pong", "ts": time.time()})

async def handle_health(request):
    health_data = {}
    for r in ROULETTES:
        health_data[r] = await stats_engine.get_total_spins(r)
    return web.json_response(health_data)

async def handle_latest(request):
    """GET /latest/{roulette} — Todo en 1 petición para polling."""
    roulette = request.match_info.get("roulette", "").upper()
    if roulette not in ROULETTES: 
        return web.json_response({"error": f"Disponibles: {list(ROULETTES.keys())}"}, status=404)
    return web.json_response(await stats_engine.get_latest_data(roulette))

async def handle_stats_dozen(request):
    roulette = request.match_info.get("roulette", "").upper()
    if roulette not in ROULETTES: 
        return web.json_response({"error": f"Disponibles: {list(ROULETTES.keys())}"}, status=404)
    return web.json_response(await stats_engine.get_stats_table(roulette, "DOCENA"))

async def handle_stats_column(request):
    roulette = request.match_info.get("roulette", "").upper()
    if roulette not in ROULETTES: 
        return web.json_response({"error": f"Disponibles: {list(ROULETTES.keys())}"}, status=404)
    return web.json_response(await stats_engine.get_stats_table(roulette, "COLUMNA"))

async def handle_spins(request):
    roulette = request.match_info.get("roulette", "").upper()
    if roulette not in ROULETTES: 
        return web.json_response({"error": f"Disponibles: {list(ROULETTES.keys())}"}, status=404)
    try: 
        n = min(int(request.match_info.get("n", "20")), 200)
    except: 
        n = 20
    return web.json_response(await stats_engine.get_last_n_spins(roulette, n))

async def handle_websocket(request):
    ws = aiohttp.web.WebSocketResponse()
    await ws.prepare(request)
    available = {k: v["name"] for k, v in ROULETTES.items()}
    await ws.send_str(json.dumps({"type": "welcome", "available_roulettes": available}))
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if data.get("type") == "subscribe":
                        roulette = data.get("roulette", "").upper()
                        if roulette in ROULETTES:
                            stats_engine.client_subscriptions[ws] = roulette
                            full_data = await stats_engine.get_latest_data(roulette)
                            await ws.send_str(json.dumps({"type": "full_state", "data": full_data}))
                except: pass
            elif msg.type == WSMsgType.ERROR: 
                break
    except: pass
    finally: 
        stats_engine.client_subscriptions.pop(ws, None)
    return ws

# ─── CLIENTE PRAGMATIC PLAY ──────────────────────────────────────────────────
async def connect_pragmatic(roulette_key, roulette_config):
    key = roulette_config["key"]
    name = roulette_config["name"]
    recon = 5
    
    while True:
        try:
            async with websockets.connect(WS_URL_PRAGMATIC, ping_interval=30, ping_timeout=60, close_timeout=10) as ws:
                await ws.send(json.dumps({"type": "subscribe", "key": key, "casinoId": CASINO_ID}))
                logger.info(f"✅ [{roulette_key}] Conectado a {name} (key={key})")
                recon = 5
                
                async for raw in ws:
                    try: 
                        data = json.loads(raw)
                    except: 
                        continue
                    
                    if not isinstance(data, dict): 
                        continue
                    
                    results = data.get("last20Results")
                    if results and isinstance(results, list):
                        for result in reversed(results):
                            gid = str(result.get("gameId", ""))
                            if not gid or gid == stats_engine.last_game_ids.get(roulette_key, ""): 
                                continue
                            try:
                                n = int(result.get("result", ""))
                                if 0 <= n <= 36 and await stats_engine.process_spin(roulette_key, n, gid):
                                    await stats_engine.broadcast_update(roulette_key, n, gid)
                            except: 
                                pass
                        continue
                    
                    for rk in ("result", "number", "outcome", "winningNumber"):
                        if rk in data:
                            gid = str(data.get("gameId", f"{roulette_key}_{int(time.time()*1000)}"))
                            try:
                                n = int(data[rk])
                                if 0 <= n <= 36 and await stats_engine.process_spin(roulette_key, n, gid):
                                    await stats_engine.broadcast_update(roulette_key, n, gid)
                            except: 
                                pass
                            break
        except Exception as e:
            logger.warning(f"⚠️ [{roulette_key}] Desconectado: {e}. Recon en {recon}s")
            await asyncio.sleep(recon)
            recon = min(recon * 2, 60)

async def self_ping_loop():
    """Keep Render instance awake"""
    url = RENDER_EXTERNAL_URL.rstrip("/")
    if not url or "localhost" in url:
        return
    await asyncio.sleep(30)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(f"{url}/ping", timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200: 
                        logger.info("✅ Self-ping")
            except Exception as e:
                logger.debug(f"Ping error: {e}")
            await asyncio.sleep(240)

async def start_background_tasks(app):
    for rk, rc in ROULETTES.items():
        app[f"task_{rk}"] = asyncio.create_task(connect_pragmatic(rk, rc))
        await asyncio.sleep(0.3)
    app["task_ping"] = asyncio.create_task(self_ping_loop())
    logger.info(f"🎰 {len(ROULETTES)} tareas iniciadas")

async def cleanup_background_tasks(app):
    for k in list(app.keys()):
        if k.startswith("task_"):
            app[k].cancel()
            try: 
                await app[k]
            except asyncio.CancelledError: 
                pass

def create_app():
    app = web.Application()
    app.router.add_get("/", handle_home)
    app.router.add_get("/ping", handle_ping)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/latest/{roulette}", handle_latest)
    app.router.add_get("/stats/{roulette}/dozen", handle_stats_dozen)
    app.router.add_get("/stats/{roulette}/column", handle_stats_column)
    app.router.add_get("/spins/{roulette}/{n}", handle_spins)
    app.router.add_get("/ws", handle_websocket)
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    return app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10004))
    web.run_app(create_app(), host="0.0.0.0", port=port, access_log=None)
