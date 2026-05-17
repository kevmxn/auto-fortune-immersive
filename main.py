#!/usr/bin/env python3
"""
Speed Roulette Stats Server — Recopilador Multi-Ruleta y Servidor WebSocket
===========================================================================
  - Se conecta a 7 ruletas Pragmatic Play simultáneamente.
  - Recopila datos en segundo plano para TODAS las ruletas siempre.
  - Los clientes (bots) se SUSCRIBEN a una ruleta específica.
  - Solo recibe actualizaciones de la ruleta a la que está suscrito.
  - Mantiente estadísticas HISTÓRICAS ACUMULADAS (no se borran).
  - Últimos 200 giros por ruleta (rotativos).
  - Tabla de transiciones: Tras número N → D1%, D2%, D3%, Zero% (y Columnas).
  - Self-ping para Render + Flask + WebSocket server.
"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.request
from collections import defaultdict
from typing import Optional, Dict, List, Set

import websockets
from flask import Flask, jsonify

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s [StatsServer] %(levelname)s %(message)s')
logger = logging.getLogger("StatsServer")
for _ln in ['werkzeug', 'flask.app', 'flask', 'urllib3']:
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
WS_SERVER_PORT = 8765

# ─── BASE DE DATOS ────────────────────────────────────────────────────────────
_db_lock = threading.Lock()

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
        
        # Clientes suscritos: {websocket: "SPEED2"}
        self.client_subscriptions: Dict = {}
        
        self._load_last_states()

    def _load_last_states(self):
        """Cargar los últimos números desde DB al reiniciar."""
        for roulette in ROULETTES:
            row = self.db.execute(
                "SELECT number, game_id FROM spins WHERE roulette=? ORDER BY id DESC LIMIT 1",
                (roulette,)
            ).fetchone()
            if row:
                self.last_numbers[roulette] = row["number"]
                self.last_game_ids[roulette] = row["game_id"]
                logger.info(f"[{roulette}] Último estado: #{row['number']} (gameId: {row['game_id'][:12]}...)")
            else:
                logger.info(f"[{roulette}] Sin datos previos")

    def process_spin(self, roulette: str, number: int, game_id: str):
        """Procesar un nuevo giro, actualizar DB y estadísticas."""
        with _db_lock:
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

            # 4. Limpiar giros antiguos (mantener solo 200)
            self._cleanup_old_spins(roulette)

        # 5. Notificar a clientes suscritos a ESTA ruleta
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._broadcast_update(roulette, number), loop
                )
        except RuntimeError:
            pass

    def _cleanup_old_spins(self, roulette: str):
        """Eliminar giros más allá de los últimos 200."""
        self.db.execute("""
            DELETE FROM spins 
            WHERE roulette=? AND id NOT IN (
                SELECT id FROM spins WHERE roulette=? ORDER BY id DESC LIMIT ?
            )
        """, (roulette, roulette, MAX_STORED_SPINS))

    def get_last_n_spins(self, roulette: str, n: int = 20) -> List[int]:
        with _db_lock:
            rows = self.db.execute(
                "SELECT number FROM spins WHERE roulette=? ORDER BY id DESC LIMIT ?",
                (roulette, n)
            ).fetchall()
            return [r["number"] for r in rows]

    def get_total_spins(self, roulette: str) -> int:
        with _db_lock:
            row = self.db.execute(
                "SELECT COUNT(*) as cnt FROM spins WHERE roulette=?", (roulette,)
            ).fetchone()
            return row["cnt"] if row else 0

    def get_stats_table(self, roulette: str, cat_type: str) -> Dict[int, Dict]:
        """Obtener tabla de estadísticas completa (0-36)."""
        with _db_lock:
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

    def subscribe_client(self, websocket, roulette: str) -> bool:
        """Suscribir un cliente a una ruleta específica."""
        if roulette not in ROULETTES:
            return False
        self.client_subscriptions[websocket] = roulette
        logger.info(f"🔗 Cliente {websocket.remote_address} suscrito a {roulette}")
        return True

    def unsubscribe_client(self, websocket):
        """Eliminar suscripción de un cliente."""
        if websocket in self.client_subscriptions:
            sub = self.client_subscriptions.pop(websocket)
            logger.info(f"🔌 Cliente {websocket.remote_address} desuscrito de {sub}")

    async def _broadcast_update(self, roulette: str, number: int):
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

        disconnected = set()
        for client, sub_roulette in list(self.client_subscriptions.items()):
            if sub_roulette != roulette:
                continue  # Solo enviar a suscritos a ESTA ruleta
            try:
                await client.send(message)
            except websockets.exceptions.ConnectionClosed:
                disconnected.add(client)
        
        for client in disconnected:
            self.client_subscriptions.pop(client, None)


# ─── SERVIDOR WEBSOCKET ──────────────────────────────────────────────────────
stats_engine = StatsEngine()

async def ws_server_handler(websocket, path):
    """Manejar conexiones de clientes (bots)."""
    logger.info(f"🔗 Nuevo cliente conectado: {websocket.remote_address}")
    
    # Enviar lista de ruletas disponibles al conectar
    available = {k: v["name"] for k, v in ROULETTES.items()}
    await websocket.send(json.dumps({
        "type": "welcome",
        "available_roulettes": available,
        "message": "Send {\"type\":\"subscribe\",\"roulette\":\"SPEED2\"} to start receiving data"
    }))
    
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                req_type = data.get("type")
                
                if req_type == "subscribe":
                    roulette = data.get("roulette", "").upper()
                    if stats_engine.subscribe_client(websocket, roulette):
                        # Enviar estado completo de la ruleta suscrita
                        state = stats_engine.get_full_state(roulette)
                        await websocket.send(json.dumps({"type": "full_state", "data": state}))
                    else:
                        await websocket.send(json.dumps({
                            "type": "error",
                            "message": f"Roulette '{roulette}' not found. Available: {list(ROULETTES.keys())}"
                        }))
                    
                elif req_type == "get_state":
                    roulette = data.get("roulette", "SPEED2").upper()
                    state = stats_engine.get_full_state(roulette)
                    await websocket.send(json.dumps({"type": "full_state", "data": state}))
                    
                elif req_type == "get_last_20":
                    roulette = data.get("roulette", "SPEED2").upper()
                    spins = stats_engine.get_last_n_spins(roulette, 20)
                    await websocket.send(json.dumps({"type": "last_20", "roulette": roulette, "data": spins}))
                    
                elif req_type == "get_stats":
                    roulette = data.get("roulette", "SPEED2").upper()
                    cat = data.get("category", "DOCENA")
                    stats = stats_engine.get_stats_table(roulette, cat)
                    await websocket.send(json.dumps({"type": "stats", "roulette": roulette, "category": cat, "data": stats}))
                    
            except json.JSONDecodeError:
                await websocket.send(json.dumps({"type": "error", "message": "Invalid JSON"}))
                
    except websockets.exceptions.ConnectionClosed:
        logger.info(f"🔌 Cliente desconectado: {websocket.remote_address}")
    finally:
        stats_engine.unsubscribe_client(websocket)


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
                            except (ValueError, TypeError):
                                pass
                            break
                            
        except Exception as e:
            logger.warning(f"⚠️ [{roulette_key}] WS desconectado: {e}. Recon en {reconnect_delay}s")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)


# ─── FLASK (KEEP-ALIVE + API EXTERNA) ────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def home():
    roulettes_status = {}
    for r_key, r_conf in ROULETTES.items():
        roulettes_status[r_key] = {
            "name": r_conf["name"],
            "total_spins": stats_engine.get_total_spins(r_key),
            "last_number": stats_engine.last_numbers.get(r_key)
        }
    return jsonify({
        "status": "ok",
        "service": "Roulette Stats Server",
        "connected_bots": len(stats_engine.client_subscriptions),
        "roulettes": roulettes_status
    })

@app.route("/ping")
def ping():
    return jsonify({"status": "pong", "ts": time.time()})

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "roulettes": {r: stats_engine.get_total_spins(r) for r in ROULETTES}
    })

@app.route("/stats/<roulette>/dozen")
def stats_dozen_api(roulette):
    roulette = roulette.upper()
    if roulette not in ROULETTES:
        return jsonify({"error": f"Rouetta not found. Available: {list(ROULETTES.keys())}"}), 404
    return jsonify(stats_engine.get_stats_table(roulette, "DOCENA"))

@app.route("/stats/<roulette>/column")
def stats_column_api(roulette):
    roulette = roulette.upper()
    if roulette not in ROULETTES:
        return jsonify({"error": f"Rouetta not found. Available: {list(ROULETTES.keys())}"}), 404
    return jsonify(stats_engine.get_stats_table(roulette, "COLUMNA"))

@app.route("/spins/<roulette>/<int:n>")
def last_spins_api(roulette, n):
    roulette = roulette.upper()
    if roulette not in ROULETTES:
        return jsonify({"error": f"Rouetta not found. Available: {list(ROULETTES.keys())}"}), 404
    n = min(n, 200)
    return jsonify(stats_engine.get_last_n_spins(roulette, n))

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10004)), debug=False, use_reloader=False)


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


# ─── INICIO ───────────────────────────────────────────────────────────────────
async def main():
    # Iniciar Flask en hilo separado
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("🚀 Flask iniciado (Keep-Alive + API)")
    
    # Iniciar servidor WebSocket para los bots
    ws_server = await websockets.serve(ws_server_handler, "0.0.0.0", WS_SERVER_PORT)
    logger.info(f"🌐 Servidor WebSocket iniciado en puerto {WS_SERVER_PORT}")
    
    # Iniciar clientes Pragmatic Play para TODAS las ruletas en segundo plano
    tasks = []
    for roulette_key, roulette_config in ROULETTES.items():
        tasks.append(asyncio.create_task(connect_pragmatic(roulette_key, roulette_config)))
        await asyncio.sleep(0.5)  # Escalonar conexiones iniciales
    
    # Self-ping
    tasks.append(asyncio.create_task(self_ping_loop()))
    
    logger.info(f"🎰 Stats Server operativo — Monitoreando {len(ROULETTES)} ruletas")
    
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Servidor detenido.")
