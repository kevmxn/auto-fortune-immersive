# ============================================================================
# SISTEMA DE EVENTOS DE RULETA CON SSE - VERSIÓN CON DOCENA Y COLUMNA
# ============================================================================
# Características:
# - 8 mesas de ruleta con IDs 300-307
# - Cada mesa tiene su propio contador de rondas (Round # independiente)
# - Mantiene solo los últimos 20 giros por mesa en SQLite
# - Columnas: número, color (Rojo/Negro/Cero), tipo (Par/Impar/Cero), rango (Bajo/Alto/Cero),
#   docena (1/2/3/Cero), columna (1/2/3/Cero)
# - Cuando un cliente se conecta, recibe el lote inicial de los últimos 20 resultados
# - Cuando se detecta un nuevo giro, se envía el lote actualizado a todos los clientes conectados
# - Polling adaptativo: normal 20s, después de 15s sin giro pasa a 1s
# - Rotación de User-Agent y backoff exponencial contra bloqueos
# - Detección específica de error 403
# - Logs detallados en consola
# - Servidor web con endpoint SSE /events/<table_id> y dashboard /dashboard
# - CORS habilitado
# ============================================================================

import sqlite3
import requests
import time
import threading
import logging
import random
import json
import queue
import sys
import os
from datetime import datetime, timedelta
from flask import Flask, Response, stream_with_context, jsonify
from flask_cors import CORS

# ============================================================================
# CONFIGURACIÓN DE LOGGING
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

# ============================================================================
# POOL DE USER-AGENTS (más de 10, actualizados)
# ============================================================================
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:135.0) Gecko/20100101 Firefox/135.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/142.0.3595.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.3595.0",
]

class RotadorUserAgent:
    def __init__(self, agentes=None):
        self.agentes = agentes if agentes else USER_AGENTS
        if not self.agentes:
            raise ValueError("No hay user-agents disponibles")
    def obtener_agente(self):
        return random.choice(self.agentes)

# ============================================================================
# CONFIGURACIÓN DE MESAS (IDs 300 a 307)
# ============================================================================
class MesaConfig:
    def __init__(self, id_mesa, nombre, url_api):
        self.id = id_mesa
        self.nombre = nombre
        self.url_api = url_api

MESAS = [
    MesaConfig(300, "Auto Roulette", "https://api-cs.casino.org/svc-evolution-game-events/api/autoroulette/latest"),
    MesaConfig(301, "Immersive Roulette", "https://api-cs.casino.org/svc-evolution-game-events/api/immersiveroulette/latest"),
    MesaConfig(302, "Fortune Roulette", "https://api-cs.casino.org/svc-evolution-game-events/api/fortuneroulette/latest"),
    MesaConfig(303, "Lightning Roulette", "https://api-cs.casino.org/svc-evolution-game-events/api/lightningroulette/latest"),
    MesaConfig(304, "XXXtreme Lightning Roulette", "https://api-cs.casino.org/svc-evolution-game-events/api/xxxtremelightningroulette/latest"),
    MesaConfig(305, "Gold Vault Roulette", "https://api-cs.casino.org/svc-evolution-game-events/api/goldvaultroulette/latest"),
    MesaConfig(306, "Fireball Roulette", "https://api-cs.casino.org/svc-evolution-game-events/api/fireballroulette/latest"),
    MesaConfig(307, "Red Door Roulette", "https://api-cs.casino.org/svc-evolution-game-events/api/reddoorroulette/latest")
]

# ============================================================================
# CLIENTE API CON ROTACIÓN Y DETECCIÓN DE 403
# ============================================================================
class ClienteAPI:
    def __init__(self, url, timeout=10, max_reintentos=3):
        self.url = url
        self.timeout = timeout
        self.max_reintentos = max_reintentos
        self.rotador_ua = RotadorUserAgent()
    
    def _obtener_headers(self):
        return {
            "User-Agent": self.rotador_ua.obtener_agente(),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://www.casino.org/",
            "Origin": "https://www.casino.org",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
        }
    
    def obtener_ultimo(self):
        for intento in range(self.max_reintentos):
            try:
                headers = self._obtener_headers()
                respuesta = requests.get(self.url, headers=headers, timeout=self.timeout)
                if respuesta.status_code == 403:
                    logging.error(f"⚠️ BLOQUEO 403 detectado en {self.url}")
                    return None
                respuesta.raise_for_status()
                return respuesta.json()
            except Exception as e:
                if intento == self.max_reintentos - 1:
                    logging.error(f"Error después de {self.max_reintentos} reintentos: {e}")
                    return None
                espera = (2 ** intento) + random.uniform(0, 1)
                logging.warning(f"Intento {intento+1} falló: {e}. Reintentando en {espera:.2f}s")
                time.sleep(espera)
        return None

# ============================================================================
# FORMATEADOR DE DATOS (convierte a español y calcula docena/columna)
# ============================================================================
class Formateador:
    @staticmethod
    def formatear(datos_crudos):
        api_id = datos_crudos.get("id")
        datos_internos = datos_crudos.get("data", {})
        resultado = datos_internos.get("result", {})
        resultado_final = resultado.get("outcome", {})
        numero = resultado_final.get("number")
        if numero is None:
            return None   # No es un giro válido

        # Manejo del número 0
        if numero == 0:
            color = "Cero"
            tipo = "Cero"
            rango = "Cero"
            docena = "Cero"
            columna = "Cero"
        else:
            # Color en español
            color_original = resultado_final.get("color")
            if color_original == "Red":
                color = "Rojo"
            elif color_original == "Black":
                color = "Negro"
            else:
                color = color_original  # por si acaso

            # Tipo (Par/Impar) en español
            tipo_original = resultado_final.get("type")
            if tipo_original == "Even":
                tipo = "Par"
            elif tipo_original == "Odd":
                tipo = "Impar"
            else:
                tipo = tipo_original

            # Rango
            if 1 <= numero <= 18:
                rango = "Bajo"
            elif 19 <= numero <= 36:
                rango = "Alto"
            else:
                rango = ""  # no debería pasar

            # Docena
            if 1 <= numero <= 12:
                docena = "1"
            elif 13 <= numero <= 24:
                docena = "2"
            elif 25 <= numero <= 36:
                docena = "3"
            else:
                docena = ""

            # Columna: ((número-1) % 3) + 1
            columna = str(((numero - 1) % 3) + 1)

        momento = datos_internos.get("settledAt")
        if momento:
            dt = datetime.fromisoformat(momento.replace('Z', '+00:00')).replace(tzinfo=None)
            hora = dt.strftime("%H:%M - %d/%m")
        else:
            hora = ""
            dt = None

        return {
            "api_id": api_id,
            "numero": numero,
            "color": color,
            "tipo": tipo,
            "rango": rango,
            "docena": docena,
            "columna": columna,
            "hora": hora,
            "momento_dt": dt
        }

# ============================================================================
# BASE DE DATOS CON CONTADOR DE RONDAS POR MESA + DOCENA/COLUMNA
# ============================================================================
class BaseDeDatos:
    def __init__(self, db_path="spins.db"):
        self.db_path = db_path
        self._crear_tabla()
        self._agregar_columnas_si_faltan()

    def _crear_tabla(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS giros (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mesa_id INTEGER NOT NULL,
                    api_id TEXT NOT NULL,
                    round_number INTEGER NOT NULL,
                    numero INTEGER,
                    color TEXT,
                    tipo TEXT,
                    rango TEXT,
                    docena TEXT,
                    columna TEXT,
                    hora TEXT,
                    timestamp TEXT,
                    UNIQUE(mesa_id, api_id),
                    UNIQUE(mesa_id, round_number)
                )
            ''')
            conn.commit()
        logging.info("Tabla 'giros' lista (o ya existía).")

    def _agregar_columnas_si_faltan(self):
        """Agrega las columnas docena y columna si no existen (migración)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # Obtener columnas existentes
            cursor.execute("PRAGMA table_info(giros)")
            columnas = [col[1] for col in cursor.fetchall()]
            if "docena" not in columnas:
                cursor.execute("ALTER TABLE giros ADD COLUMN docena TEXT")
                logging.info("Columna 'docena' añadida a la tabla.")
            if "columna" not in columnas:
                cursor.execute("ALTER TABLE giros ADD COLUMN columna TEXT")
                logging.info("Columna 'columna' añadida a la tabla.")
            conn.commit()

    def _siguiente_round(self, mesa_id):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(round_number) FROM giros WHERE mesa_id = ?", (mesa_id,))
            max_round = cursor.fetchone()[0]
            return 1 if max_round is None else max_round + 1

    def insertar_giro(self, mesa_id, api_id, numero, color, tipo, rango, docena, columna, hora):
        try:
            round_num = self._siguiente_round(mesa_id)
            now = datetime.utcnow().isoformat()
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO giros (mesa_id, api_id, round_number, numero, color, tipo, rango, docena, columna, hora, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (mesa_id, api_id, round_num, numero, color, tipo, rango, docena, columna, hora, now))
                conn.commit()
            self._limpiar_mesa(mesa_id)
            return True
        except sqlite3.IntegrityError:
            logging.debug(f"Giro duplicado para mesa {mesa_id}: {api_id}")
            return False

    def _limpiar_mesa(self, mesa_id, keep=20):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM giros
                WHERE mesa_id = ?
                AND id NOT IN (
                    SELECT id FROM giros
                    WHERE mesa_id = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                )
            ''', (mesa_id, mesa_id, keep))
            conn.commit()

    def obtener_ultimos(self, mesa_id, n=20):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT round_number, api_id, numero, color, tipo, rango, docena, columna, hora, timestamp
                FROM giros
                WHERE mesa_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            ''', (mesa_id, n))
            rows = cursor.fetchall()
            resultados = []
            for row in rows:
                resultados.append({
                    "id": row[0],
                    "api_id": row[1],
                    "numero": row[2],
                    "color": row[3],
                    "tipo": row[4],
                    "rango": row[5],
                    "docena": row[6],
                    "columna": row[7],
                    "hora": row[8],
                    "timestamp": row[9]
                })
            return resultados

    def existe_giro(self, mesa_id, api_id):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM giros WHERE mesa_id = ? AND api_id = ?", (mesa_id, api_id))
            return cursor.fetchone() is not None

# ============================================================================
# GESTOR DE EVENTOS SSE
# ============================================================================
class GestorEventos:
    def __init__(self):
        self._clientes = {}
        self._lock = threading.Lock()
    
    def suscribir(self, mesa_id):
        q = queue.Queue()
        with self._lock:
            if mesa_id not in self._clientes:
                self._clientes[mesa_id] = []
            self._clientes[mesa_id].append(q)
        logging.info(f"Cliente suscrito a mesa {mesa_id}. Total: {len(self._clientes[mesa_id])}")
        return q
    
    def desuscribir(self, mesa_id, q):
        with self._lock:
            if mesa_id in self._clientes and q in self._clientes[mesa_id]:
                self._clientes[mesa_id].remove(q)
                if not self._clientes[mesa_id]:
                    del self._clientes[mesa_id]
    
    def difundir(self, mesa_id, datos):
        with self._lock:
            if mesa_id not in self._clientes:
                return
            for q in self._clientes[mesa_id]:
                try:
                    q.put_nowait(datos)
                except queue.Full:
                    pass

# ============================================================================
# POLLER ADAPTATIVO POR MESA
# ============================================================================
class PollerMesa:
    def __init__(self, mesa, db, gestor):
        self.mesa = mesa
        self.db = db
        self.gestor = gestor
        self.api = ClienteAPI(mesa.url_api)
        self.formateador = Formateador()
        self.ultimo_giro_tiempo = None
        self.ejecutando = True
        self.error_count = 0
        self.INTERVALO_NORMAL = 20
        self.INTERVALO_RAPIDO = 1
        self.RETRASO_ANTES_RAPIDO = 15
        self.MAX_ESPERA_ERROR = 20
    
    def _calcular_intervalo(self):
        if self.ultimo_giro_tiempo is None:
            return self.INTERVALO_NORMAL
        segundos = (datetime.utcnow() - self.ultimo_giro_tiempo).total_seconds()
        if segundos >= self.RETRASO_ANTES_RAPIDO:
            return self.INTERVALO_RAPIDO
        return self.INTERVALO_NORMAL
    
    def ejecutar(self):
        logging.info(f"Iniciando poller para mesa {self.mesa.nombre} (ID {self.mesa.id})")
        while self.ejecutando:
            try:
                datos = self.api.obtener_ultimo()
                if datos is None:
                    espera = min(self.MAX_ESPERA_ERROR, (2 ** self.error_count) + random.uniform(0, 1))
                    self.error_count += 1
                    logging.warning(f"Error en API de {self.mesa.nombre}, reintento en {espera:.2f}s")
                    time.sleep(espera)
                    continue
                self.error_count = 0
                giro = self.formateador.formatear(datos)
                if giro is None:
                    logging.debug(f"Formato inválido para {self.mesa.nombre}, no se inserta")
                else:
                    api_id = giro["api_id"]
                    if not self.db.existe_giro(self.mesa.id, api_id):
                        insertado = self.db.insertar_giro(
                            self.mesa.id, api_id,
                            giro["numero"], giro["color"], giro["tipo"],
                            giro["rango"], giro["docena"], giro["columna"], giro["hora"]
                        )
                        if insertado:
                            if giro["momento_dt"]:
                                self.ultimo_giro_tiempo = giro["momento_dt"]
                            else:
                                self.ultimo_giro_tiempo = datetime.utcnow()
                            logging.info(f"Nuevo giro mesa {self.mesa.nombre}: {giro['numero']} ({api_id})")
                            ultimos = self.db.obtener_ultimos(self.mesa.id, 20)
                            self.gestor.difundir(self.mesa.id, ultimos)
                intervalo = self._calcular_intervalo()
                time.sleep(intervalo)
            except Exception as e:
                logging.exception(f"Error inesperado en poller de {self.mesa.nombre}")
                time.sleep(min(5, self.MAX_ESPERA_ERROR))
    
    def detener(self):
        self.ejecutando = False

# ============================================================================
# APLICACIÓN FLASK
# ============================================================================
app = Flask(__name__)
CORS(app)

db = None
gestor = None
pollers = []

try:
    db = BaseDeDatos()
    gestor = GestorEventos()
    for mesa in MESAS:
        poller = PollerMesa(mesa, db, gestor)
        hilo = threading.Thread(target=poller.ejecutar, daemon=True)
        hilo.start()
        pollers.append(poller)
        logging.info(f"Poller lanzado para {mesa.nombre} (ID {mesa.id})")
except Exception as e:
    logging.exception("FATAL: Error durante inicialización")

@app.route('/')
def index():
    return jsonify({"estado": "activo", "mesas": [{"id": m.id, "nombre": m.nombre} for m in MESAS]})

@app.route('/health')
def health():
    return "OK", 200

@app.route('/events/<int:mesa_id>')
def eventos(mesa_id):
    if not any(m.id == mesa_id for m in MESAS):
        return jsonify({"error": "Mesa no encontrada"}), 404
    q = gestor.suscribir(mesa_id)
    def generar():
        try:
            ultimos = db.obtener_ultimos(mesa_id, 20)
            yield f"event: init\ndata: {json.dumps(ultimos)}\n\n"
            while True:
                datos = q.get()
                yield f"event: update\ndata: {json.dumps(datos)}\n\n"
        except GeneratorExit:
            gestor.desuscribir(mesa_id, q)
        except Exception as e:
            logging.error(f"Error SSE mesa {mesa_id}: {e}")
            gestor.desuscribir(mesa_id, q)
    return Response(stream_with_context(generar()), mimetype="text/event-stream")

@app.route('/dashboard')
def dashboard():
    return '''
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ruletas en Vivo</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box;}
        body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background:#0f172a;color:#e2e8f0;padding:2rem 1rem;}
        .container{max-width:1200px;margin:0 auto;}
        h1{font-size:1.8rem;font-weight:600;margin-bottom:0.25rem;color:#fbbf24;}
        .sub{color:#94a3b8;margin-bottom:2rem;}
        .card{background:#1e293b;border-radius:1rem;padding:1.5rem;box-shadow:0 1px 2px rgba(0,0,0,0.1);}
        .controls{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:1rem;margin-bottom:1.5rem;padding-bottom:1rem;border-bottom:1px solid #334155;}
        .select-group{display:flex;align-items:center;gap:0.5rem;background:#0f172a;padding:0.3rem 1rem;border-radius:2rem;}
        select{background:#0f172a;color:#fbbf24;border:1px solid #334155;border-radius:1.5rem;padding:0.4rem 1.8rem 0.4rem 1rem;font-size:0.9rem;cursor:pointer;}
        button{background:#fbbf24;color:#0f172a;border:none;border-radius:2rem;padding:0.5rem 1.2rem;font-weight:600;cursor:pointer;transition:0.2s;}
        button:hover{background:#f59e0b;}
        .status{display:flex;align-items:center;gap:0.5rem;font-size:0.85rem;background:#0f172a;padding:0.4rem 1rem;border-radius:2rem;}
        .connected{color:#10b981;}
        .disconnected{color:#ef4444;}
        .update-time{font-size:0.75rem;color:#94a3b8;}
        .table-wrapper{overflow-x:auto;margin-top:1rem;}
        table{width:100%;border-collapse:collapse;font-size:0.9rem;}
        th,td{padding:0.75rem 0.5rem;text-align:center;border-bottom:1px solid #334155;}
        th{background:#0f172a;color:#fbbf24;}
        tr:hover td{background:#0f172a;}
        .no-data{text-align:center;padding:2rem;color:#64748b;}
        .footer{margin-top:1.5rem;text-align:center;font-size:0.75rem;color:#64748b;border-top:1px solid #334155;padding-top:1rem;}
    </style>
</head>
<body>
<div class="container">
    <h1>🎲 Ruletas en Vivo</h1>
    <div class="sub">Últimos 20 giros · Actualización en tiempo real</div>
    <div class="card">
        <div class="controls">
            <div style="display:flex;gap:1rem;align-items:center;">
                <div class="select-group"><span>📌 Mesa</span><select id="mesaSelect">
                    <option value="300">Auto Roulette (300)</option>
                    <option value="301">Immersive Roulette (301)</option>
                    <option value="302">Fortune Roulette (302)</option>
                    <option value="303">Lightning Roulette (303)</option>
                    <option value="304">XXXtreme Lightning Roulette (304)</option>
                    <option value="305">Gold Vault Roulette (305)</option>
                    <option value="306">Fireball Roulette (306)</option>
                    <option value="307">Red Door Roulette (307)</option>
                </select></div>
                <button id="connectBtn">Conectar</button>
                <div class="status"><span id="statusDot">⚫</span><span id="statusText">Desconectado</span></div>
            </div>
            <div class="update-time" id="lastUpdate">🕒 --</div>
        </div>
        <div class="table-wrapper">
            <table id="spinsTable"><thead><th>Round #</th><th>Número</th><th>Color</th><th>Tipo</th><th>Rango</th><th>Hora</th></thead><tbody id="spinsBody">发展<td colspan="6" class="no-data">⏳ Esperando datos...发展</tbody>发展</table>
        </div>
    </div>
</div>
<script>
    const SERVER_BASE = "";
    let eventSource = null, currentMesa = null;
    const mesaSelect=document.getElementById('mesaSelect'),connectBtn=document.getElementById('connectBtn');
    const statusDot=document.getElementById('statusDot'),statusText=document.getElementById('statusText');
    const lastUpdateSpan=document.getElementById('lastUpdate'),spinsBody=document.getElementById('spinsBody');
    function updateTable(spins){
        if(!spins||spins.length===0){spinsBody.innerHTML='<tr><td colspan="6" class="no-data">📭 No hay datos aún.</td></tr>';return;}
        let html='';
        spins.forEach(s=>{
            let colorStyle='';
            if(s.color==='Rojo') colorStyle='color:#f87171;font-weight:500;';
            else if(s.color==='Negro') colorStyle='color:#9ca3af;font-weight:500;';
            else if(s.color==='Cero') colorStyle='color:#4ade80;font-weight:500;';
            html+=`<tr><td style="font-family:monospace;">${s.id}</td><td style="${colorStyle}">${s.numero??'-'}</td><td>${s.color??'-'}</td><td>${s.tipo??'-'}</td><td>${s.rango??'-'}</td><td style="font-size:0.8rem;">${s.hora??'-'}</td></tr>`;
        });
        spinsBody.innerHTML=html;
        lastUpdateSpan.innerText=`🕒 ${new Date().toLocaleTimeString()}`;
    }
    function setStatus(connected){
        if(connected){statusDot.innerHTML='🟢';statusText.innerHTML='Conectado';statusText.className='connected';}
        else{statusDot.innerHTML='🔴';statusText.innerHTML='Desconectado';statusText.className='disconnected';}
    }
    function connect(mesaId){
        if(eventSource) eventSource.close();
        const url=`${SERVER_BASE}/events/${mesaId}`;
        eventSource=new EventSource(url);
        eventSource.onopen=()=>{setStatus(true);console.log(`Conectado mesa ${mesaId}`);};
        eventSource.onerror=()=>setStatus(false);
        eventSource.addEventListener('init',e=>updateTable(JSON.parse(e.data)));
        eventSource.addEventListener('update',e=>updateTable(JSON.parse(e.data)));
    }
    connectBtn.addEventListener('click',()=>{const mesaId=parseInt(mesaSelect.value);if(mesaId===currentMesa)return;currentMesa=mesaId;connect(currentMesa);});
    window.addEventListener('load',()=>{currentMesa=parseInt(mesaSelect.value);connect(currentMesa);});
</script>
</body>
</html>
    '''

# ============================================================================
# PUNTO DE ENTRADA
# ============================================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
