# ============================================================================
# SISTEMA DE EVENTOS DE RULETA CON SSE (SERVER-SENT EVENTS) - VERSIÓN ROBUSTA
# ============================================================================
# Características:
# - Soporta múltiples mesas: Auto Roulette (300), Immersive Roulette (301), Fortune Roulette (302)
# - Mantiene solo los últimos 20 giros por mesa en SQLite
# - Cuando un cliente se conecta, recibe el lote inicial de los últimos 20 resultados
# - Cuando se detecta un nuevo giro, se envía el lote actualizado a todos los clientes conectados de esa mesa
# - Polling adaptativo: normal 20s, después de 15s sin giro pasa a 1s (para no perder el siguiente)
# - Rotación de User-Agent y backoff exponencial contra bloqueos
# - Detección específica de error 403 (bloqueo por API)
# - Logs detallados en consola (nivel INFO) para ver giros y errores
# - Servidor web con endpoint SSE /events/<table_id> y dashboard /dashboard
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
from flask import Flask, Response, stream_with_context, request, jsonify

# ============================================================================
# CONFIGURACIÓN DE LOGGING
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]  # Asegura que se vea en Render
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

# ============================================================================
# ROTADOR DE USER-AGENT
# ============================================================================
class RotadorUserAgent:
    def __init__(self, agentes=None):
        self.agentes = agentes if agentes else USER_AGENTS
        if not self.agentes:
            raise ValueError("No hay user-agents disponibles")
    
    def obtener_agente(self):
        return random.choice(self.agentes)

# ============================================================================
# CONFIGURACIÓN DE MESAS
# ============================================================================
class MesaConfig:
    def __init__(self, id_mesa, nombre, url_api):
        self.id = id_mesa          # 300, 301, 302
        self.nombre = nombre
        self.url_api = url_api

MESAS = [
    MesaConfig(300, "Auto Roulette", "https://api-cs.casino.org/svc-evolution-game-events/api/autoroulette/latest"),
    MesaConfig(301, "Immersive Roulette", "https://api-cs.casino.org/svc-evolution-game-events/api/immersiveroulette/latest"),
    MesaConfig(302, "Fortune Roulette", "https://api-cs.casino.org/svc-evolution-game-events/api/fortuneroulette/latest")
]

# ============================================================================
# CLIENTE API CON ROTACIÓN DE USER-AGENT Y DETECCIÓN DE 403
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
                
                # Detectar 403 específicamente
                if respuesta.status_code == 403:
                    logging.error(f"⚠️ BLOQUEO 403 detectado en {self.url}. Posible bloqueo de la API.")
                    return None
                
                respuesta.raise_for_status()
                return respuesta.json()
            except requests.exceptions.RequestException as e:
                if intento == self.max_reintentos - 1:
                    logging.error(f"Error después de {self.max_reintentos} reintentos: {e}")
                    return None
                espera = (2 ** intento) + random.uniform(0, 1)
                logging.warning(f"Intento {intento+1} falló: {e}. Reintentando en {espera:.2f}s")
                time.sleep(espera)
            except Exception as e:
                logging.error(f"Error inesperado en la petición: {e}")
                return None
        return None

# ============================================================================
# FORMATEADOR DE DATOS (genérico)
# ============================================================================
class Formateador:
    @staticmethod
    def formatear(datos_crudos):
        """
        Extrae los campos de la API. Devuelve None si no hay número (giro inválido).
        """
        api_id = datos_crudos.get("id")
        datos_internos = datos_crudos.get("data", {})
        resultado = datos_internos.get("result", {})
        resultado_final = resultado.get("outcome", {})
        numero = resultado_final.get("number")
        
        # Si no hay número, no es un giro válido (ej. Immersive Roulette devuelve count de jugadores)
        if numero is None:
            return None
        
        color = resultado_final.get("color")
        tipo_original = resultado_final.get("type")   # "Even" / "Odd"
        momento = datos_internos.get("settledAt")
        
        # Tipo
        if tipo_original == "Even":
            tipo = "Par"
        elif tipo_original == "Odd":
            tipo = "Impar"
        else:
            tipo = tipo_original
        
        # Rango
        if numero is not None:
            if 1 <= numero <= 18:
                rango = "Bajo"
            elif 19 <= numero <= 36:
                rango = "Alto"
            else:
                rango = ""
        else:
            rango = ""
        
        # Hora
        if momento:
            # Convertir a datetime naive (sin zona horaria)
            dt = datetime.fromisoformat(momento.replace('Z', '+00:00')).replace(tzinfo=None)
            hora = dt.strftime("%H:%M - %d/%m")
        else:
            hora = ""
        
        return {
            "api_id": api_id,
            "numero": numero,
            "color": color,
            "tipo": tipo,
            "rango": rango,
            "hora": hora
        }

# ============================================================================
# BASE DE DATOS (solo últimos 20 por mesa)
# ============================================================================
class BaseDeDatos:
    def __init__(self, db_path="spins.db"):
        self.db_path = db_path
        self._crear_tabla()
    
    def _crear_tabla(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS giros (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mesa_id INTEGER NOT NULL,
                    api_id TEXT NOT NULL,
                    numero INTEGER,
                    color TEXT,
                    tipo TEXT,
                    rango TEXT,
                    hora TEXT,
                    timestamp TEXT,          -- ISO datetime para ordenar
                    UNIQUE(mesa_id, api_id)  -- Evitar duplicados por mesa
                )
            ''')
            conn.commit()
        logging.info("Tabla 'giros' lista.")
    
    def insertar_giro(self, mesa_id, api_id, numero, color, tipo, rango, hora):
        try:
            now = datetime.utcnow().isoformat()
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO giros (mesa_id, api_id, numero, color, tipo, rango, hora, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (mesa_id, api_id, numero, color, tipo, rango, hora, now))
                conn.commit()
            # Limpiar los antiguos para mantener solo los 20 más recientes
            self._limpiar_mesa(mesa_id)
            return True
        except sqlite3.IntegrityError:
            logging.debug(f"Giro duplicado para mesa {mesa_id}: {api_id}")
            return False
    
    def _limpiar_mesa(self, mesa_id, keep=20):
        """Elimina los registros más antiguos de la mesa, dejando solo los 'keep' más recientes."""
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
        """Retorna los últimos n giros de una mesa, ordenados del más reciente al más antiguo.
           Incluye el campo 'id' (Round #)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, api_id, numero, color, tipo, rango, hora, timestamp
                FROM giros
                WHERE mesa_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            ''', (mesa_id, n))
            rows = cursor.fetchall()
            resultados = []
            for row in rows:
                resultados.append({
                    "id": row[0],            # Round #
                    "api_id": row[1],
                    "numero": row[2],
                    "color": row[3],
                    "tipo": row[4],
                    "rango": row[5],
                    "hora": row[6],
                    "timestamp": row[7]
                })
            return resultados
    
    def existe_giro(self, mesa_id, api_id):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM giros WHERE mesa_id = ? AND api_id = ?", (mesa_id, api_id))
            return cursor.fetchone() is not None

# ============================================================================
# GESTOR DE EVENTOS (SSE)
# ============================================================================
class GestorEventos:
    """Maneja los clientes SSE y la difusión de actualizaciones por mesa."""
    def __init__(self):
        self._clientes = {}  # mesa_id -> list of queues
        self._lock = threading.Lock()
    
    def suscribir(self, mesa_id):
        """Crea una nueva cola para un cliente y retorna la cola."""
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
                logging.info(f"Cliente desuscrito de mesa {mesa_id}. Restantes: {len(self._clientes.get(mesa_id, []))}")
    
    def difundir(self, mesa_id, datos):
        """Envía 'datos' a todos los clientes suscritos a esa mesa."""
        with self._lock:
            if mesa_id not in self._clientes:
                return
            for q in self._clientes[mesa_id]:
                try:
                    q.put_nowait(datos)
                except queue.Full:
                    logging.warning(f"Cola llena para mesa {mesa_id}, cliente perdido.")

# ============================================================================
# POLLER ADAPTATIVO POR MESA
# ============================================================================
class PollerMesa:
    def __init__(self, mesa, db, gestor_eventos):
        self.mesa = mesa
        self.db = db
        self.gestor = gestor_eventos
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
                # Si no hay número, no es un giro válido (ej. Immersive Roulette)
                if giro is None:
                    # No hay giro, pero la API puede estar respondiendo (ej. count de jugadores)
                    # Para Immersive, simplemente no insertamos y seguimos con intervalo normal
                    logging.debug(f"Formato inválido para {self.mesa.nombre}, no se inserta")
                else:
                    api_id = giro["api_id"]
                    if not self.db.existe_giro(self.mesa.id, api_id):
                        # Insertar nuevo giro
                        insertado = self.db.insertar_giro(
                            self.mesa.id,
                            api_id,
                            giro["numero"],
                            giro["color"],
                            giro["tipo"],
                            giro["rango"],
                            giro["hora"]
                        )
                        if insertado:
                            # Actualizar timestamp del último giro (en naive UTC)
                            # Intentar obtener el momento desde la API, sino usar ahora
                            momento_str = datos.get("data", {}).get("settledAt")
                            if momento_str:
                                dt = datetime.fromisoformat(momento_str.replace('Z', '+00:00')).replace(tzinfo=None)
                                self.ultimo_giro_tiempo = dt
                            else:
                                self.ultimo_giro_tiempo = datetime.utcnow()
                            logging.info(f"Nuevo giro mesa {self.mesa.nombre}: {giro['numero']} ({api_id})")
                            
                            # Obtener los últimos 20 giros de esta mesa
                            ultimos = self.db.obtener_ultimos(self.mesa.id, 20)
                            # Difundir a todos los clientes
                            self.gestor.difundir(self.mesa.id, ultimos)
                
                # Calcular siguiente intervalo (incluso si no hubo giro)
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

# Instancias globales
db = None
gestor_eventos = None
pollers = []

# Inicializar todo con manejo de errores
try:
    logging.info("Inicializando base de datos...")
    db = BaseDeDatos()
    gestor_eventos = GestorEventos()
    
    logging.info("Iniciando pollers...")
    for mesa in MESAS:
        poller = PollerMesa(mesa, db, gestor_eventos)
        hilo = threading.Thread(target=poller.ejecutar, daemon=True)
        hilo.start()
        pollers.append(poller)
        logging.info(f"Poller lanzado para {mesa.nombre} (ID {mesa.id})")
except Exception as e:
    logging.exception("FATAL: Error durante la inicialización del sistema")
    # No lanzamos excepción, pero el servidor seguirá funcionando sin pollers
    # Esto permite que al menos el endpoint / muestre un estado de error

@app.route('/')
def index():
    """Endpoint de estado."""
    if db is None:
        return jsonify({"estado": "error", "mensaje": "Base de datos no inicializada"}), 500
    try:
        total = db.contar_giros() if hasattr(db, 'contar_giros') else 0
    except:
        total = 0
    return jsonify({
        "estado": "activo" if db else "parcial",
        "mesas": [
            {"id": mesa.id, "nombre": mesa.nombre}
            for mesa in MESAS
        ],
        "total_giros": total
    })

@app.route('/health')
def health():
    """Endpoint simple para health checks."""
    return "OK", 200

@app.route('/events/<int:mesa_id>')
def eventos(mesa_id):
    """
    Endpoint SSE para una mesa específica.
    El cliente recibe:
        - Inicialmente: los últimos 20 giros (como evento "init")
        - Luego: cada nuevo giro actualiza el lote (evento "update")
    """
    # Verificar que la mesa existe
    if not any(m.id == mesa_id for m in MESAS):
        return jsonify({"error": "Mesa no encontrada"}), 404
    
    # Crear cola para este cliente
    q = gestor_eventos.suscribir(mesa_id)
    
    def generar():
        try:
            # Enviar lote inicial
            ultimos = db.obtener_ultimos(mesa_id, 20)
            yield f"event: init\ndata: {json.dumps(ultimos)}\n\n"
            
            # Mantener conexión y esperar nuevos mensajes
            while True:
                datos = q.get()  # bloquea hasta que llegue un nuevo lote
                yield f"event: update\ndata: {json.dumps(datos)}\n\n"
        except GeneratorExit:
            # Cliente se desconectó
            gestor_eventos.desuscribir(mesa_id, q)
        except Exception as e:
            logging.error(f"Error en SSE para mesa {mesa_id}: {e}")
            gestor_eventos.desuscribir(mesa_id, q)
    
    return Response(stream_with_context(generar()), mimetype="text/event-stream")

@app.route('/dashboard')
def dashboard():
    """Página HTML para visualización."""
    return '''
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ruletas en Vivo</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            background: #111827;
            color: #e5e7eb;
            padding: 2rem 1rem;
            line-height: 1.5;
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
        }

        h1 {
            font-size: 1.8rem;
            font-weight: 600;
            margin-bottom: 0.25rem;
            color: #fbbf24;
        }

        .sub {
            color: #9ca3af;
            margin-bottom: 2rem;
            font-size: 0.9rem;
        }

        .card {
            background: #1f2937;
            border-radius: 1rem;
            padding: 1.5rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }

        .controls {
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            gap: 1rem;
            margin-bottom: 1.5rem;
            padding-bottom: 1rem;
            border-bottom: 1px solid #374151;
        }

        .select-group {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            background: #111827;
            padding: 0.3rem 1rem;
            border-radius: 2rem;
        }

        select {
            background: #111827;
            color: #fbbf24;
            border: 1px solid #374151;
            border-radius: 1.5rem;
            padding: 0.4rem 1.8rem 0.4rem 1rem;
            font-size: 0.9rem;
            cursor: pointer;
            outline: none;
        }

        button {
            background: #fbbf24;
            color: #111827;
            border: none;
            border-radius: 2rem;
            padding: 0.5rem 1.2rem;
            font-weight: 600;
            cursor: pointer;
            transition: 0.2s;
        }

        button:hover {
            background: #f59e0b;
        }

        .status {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-size: 0.85rem;
            background: #111827;
            padding: 0.4rem 1rem;
            border-radius: 2rem;
        }

        .connected { color: #10b981; }
        .disconnected { color: #ef4444; }

        .update-time {
            font-size: 0.75rem;
            color: #9ca3af;
        }

        .table-wrapper {
            overflow-x: auto;
            margin-top: 1rem;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.9rem;
        }

        th, td {
            padding: 0.75rem 0.5rem;
            text-align: center;
            border-bottom: 1px solid #374151;
        }

        th {
            background: #111827;
            font-weight: 600;
            color: #fbbf24;
        }

        tr:hover td {
            background: #111827;
        }

        .no-data {
            text-align: center;
            padding: 2rem;
            color: #6b7280;
        }

        .footer {
            margin-top: 1.5rem;
            text-align: center;
            font-size: 0.75rem;
            color: #6b7280;
            border-top: 1px solid #374151;
            padding-top: 1rem;
        }
    </style>
</head>
<body>
<div class="container">
    <h1>🎲 Ruletas en Vivo</h1>
    <div class="sub">Últimos 20 giros · Actualización en tiempo real</div>

    <div class="card">
        <div class="controls">
            <div style="display: flex; gap: 1rem; align-items: center;">
                <div class="select-group">
                    <span>📌 Mesa</span>
                    <select id="mesaSelect">
                        <option value="300">Auto Roulette (300)</option>
                        <option value="301">Immersive Roulette (301)</option>
                        <option value="302">Fortune Roulette (302)</option>
                    </select>
                </div>
                <button id="connectBtn">Conectar</button>
                <div class="status">
                    <span id="statusDot">⚫</span>
                    <span id="statusText">Desconectado</span>
                </div>
            </div>
            <div class="update-time" id="lastUpdate">🕒 --</div>
        </div>

        <div class="table-wrapper">
            <table id="spinsTable">
                <thead>
                    <tr><th>Round #</th><th>Número</th><th>Color</th><th>Tipo</th><th>Rango</th><th>Hora</th></tr>
                </thead>
                <tbody id="spinsBody">
                    <tr><td colspan="6" class="no-data">⏳ Esperando datos...</td></tr>
                </tbody>
            </table>
        </div>
        <div class="footer">
            🔄 Los datos se actualizan automáticamente con cada nuevo giro.<br>
            ℹ️ Immersive Roulette actualmente no publica giros (solo conteo de jugadores). Si deseas ver sus resultados, verifica el endpoint correcto.
        </div>
    </div>
</div>

<script>
    // Si el HTML se sirve desde el mismo servidor, deja SERVER_BASE vacío.
    // Si lo abres localmente, cambia a la URL de tu servidor:
    const SERVER_BASE = "";  // Ejemplo: "https://casinoscores-es.onrender.com"

    let eventSource = null;
    let currentMesa = null;

    const mesaSelect = document.getElementById('mesaSelect');
    const connectBtn = document.getElementById('connectBtn');
    const statusDot = document.getElementById('statusDot');
    const statusText = document.getElementById('statusText');
    const lastUpdateSpan = document.getElementById('lastUpdate');
    const spinsBody = document.getElementById('spinsBody');

    function updateTable(spins) {
        if (!spins || spins.length === 0) {
            spinsBody.innerHTML = '<tr><td colspan="6" class="no-data">📭 No hay datos aún. Esperando primer giro...</td></tr>';
            return;
        }
        let html = '';
        spins.forEach(spin => {
            let numberColor = '';
            if (spin.color === 'Red') numberColor = 'color: #f87171; font-weight:500;';
            else if (spin.color === 'Black') numberColor = 'color: #9ca3af; font-weight:500;';
            else if (spin.color === 'Green') numberColor = 'color: #4ade80; font-weight:500;';

            html += `
                <tr>
                    <td style="font-family: monospace;">${spin.id}</td>
                    <td style="${numberColor}">${spin.numero ?? '-'}</td>
                    <td>${spin.color ?? '-'}</td>
                    <td>${spin.tipo ?? '-'}</td>
                    <td>${spin.rango ?? '-'}</td>
                    <td style="font-size:0.8rem;">${spin.hora ?? '-'}</td>
                </tr>
            `;
        });
        spinsBody.innerHTML = html;
        const now = new Date();
        lastUpdateSpan.innerText = `🕒 ${now.toLocaleTimeString()}`;
    }

    function setStatus(connected) {
        if (connected) {
            statusDot.innerHTML = '🟢';
            statusText.innerHTML = 'Conectado';
            statusText.className = 'connected';
        } else {
            statusDot.innerHTML = '🔴';
            statusText.innerHTML = 'Desconectado';
            statusText.className = 'disconnected';
        }
    }

    function connect(mesaId) {
        if (eventSource) {
            eventSource.close();
            eventSource = null;
        }
        const url = `${SERVER_BASE}/events/${mesaId}`;
        console.log('Conectando a', url);
        eventSource = new EventSource(url);

        eventSource.onopen = () => {
            setStatus(true);
            console.log(`Conectado a mesa ${mesaId}`);
        };

        eventSource.onerror = (err) => {
            console.error('Error SSE', err);
            setStatus(false);
        };

        eventSource.addEventListener('init', (e) => {
            updateTable(JSON.parse(e.data));
        });

        eventSource.addEventListener('update', (e) => {
            updateTable(JSON.parse(e.data));
        });
    }

    connectBtn.addEventListener('click', () => {
        const mesaId = parseInt(mesaSelect.value);
        if (mesaId === currentMesa) return;
        currentMesa = mesaId;
        connect(currentMesa);
    });

    window.addEventListener('load', () => {
        currentMesa = parseInt(mesaSelect.value);
        connect(currentMesa);
    });
</script>
</body>
</html>
    '''

# ============================================================================
# PUNTO DE ENTRADA
# ============================================================================
if __name__ == "__main__":
    # Obtener puerto desde variable de entorno (Render la asigna)
    port = int(os.environ.get("PORT", 5000))
    logging.info(f"Iniciando servidor Flask en puerto {port}")
    # Ejecutar Flask en el hilo principal
    app.run(host="0.0.0.0", port=port)
