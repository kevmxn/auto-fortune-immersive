# ============================================================================
# SISTEMA DE EVENTOS DE RULETA CON SSE (SERVER-SENT EVENTS)
# ============================================================================
# Características:
# - Soporta múltiples mesas: Auto Roulette (300), Immersive Roulette (301), Fortune Roulette (302)
# - Mantiene solo los últimos 20 giros por mesa en SQLite
# - Cuando un cliente se conecta, recibe el lote inicial de los últimos 20 resultados
# - Cuando se detecta un nuevo giro, se envía el lote actualizado a todos los clientes conectados de esa mesa
# - Polling adaptativo: normal 20s, después de 15s sin giro pasa a 1s (para no perder el siguiente)
# - Rotación de User-Agent y backoff exponencial contra bloqueos
# - Servidor web con endpoint SSE /events/<table_id>
# ============================================================================

import sqlite3
import requests
import time
import threading
import logging
import random
import json
import queue
from datetime import datetime, timedelta
from flask import Flask, Response, stream_with_context, request, jsonify
import os

# ============================================================================
# CONFIGURACIÓN DE LOGGING
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
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
    MesaConfig(301, "Immersive Roulette", "https://api-cs.casino.org/cg-neptune-notification-center/api/evolobby/playercount/latest"),
    MesaConfig(302, "Fortune Roulette", "https://api-cs.casino.org/svc-evolution-game-events/api/fortuneroulette/latest")
]

# ============================================================================
# CLIENTE API CON ROTACIÓN DE USER-AGENT
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
                respuesta.raise_for_status()
                return respuesta.json()
            except Exception as e:
                if intento == self.max_reintentos - 1:
                    logging.error(f"Error en API {self.url}: {e}")
                    return None
                espera = (2 ** intento) + random.uniform(0, 1)
                logging.warning(f"Intento {intento+1} falló para {self.url}: {e}. Reintentando en {espera:.2f}s")
                time.sleep(espera)
        return None

# ============================================================================
# FORMATEADOR DE DATOS (genérico)
# ============================================================================
class Formateador:
    @staticmethod
    def formatear(datos_crudos):
        """Extrae los campos comunes de la API."""
        api_id = datos_crudos.get("id")
        datos_internos = datos_crudos.get("data", {})
        resultado = datos_internos.get("result", {})
        resultado_final = resultado.get("outcome", {})
        numero = resultado_final.get("number")
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
            dt = datetime.fromisoformat(momento.replace('Z', '+00:00'))
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
        """Retorna los últimos n giros de una mesa, ordenados del más reciente al más antiguo."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT api_id, numero, color, tipo, rango, hora, timestamp
                FROM giros
                WHERE mesa_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            ''', (mesa_id, n))
            rows = cursor.fetchall()
            # Convertir a lista de diccionarios
            resultados = []
            for row in rows:
                resultados.append({
                    "api_id": row[0],
                    "numero": row[1],
                    "color": row[2],
                    "tipo": row[3],
                    "rango": row[4],
                    "hora": row[5],
                    "timestamp": row[6]
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
                        # Actualizar timestamp del último giro
                        momento_str = datos.get("data", {}).get("settledAt")
                        if momento_str:
                            self.ultimo_giro_tiempo = datetime.fromisoformat(momento_str.replace('Z', '+00:00'))
                        else:
                            self.ultimo_giro_tiempo = datetime.utcnow()
                        logging.info(f"Nuevo giro mesa {self.mesa.nombre}: {giro['numero']} ({api_id})")
                        
                        # Obtener los últimos 20 giros de esta mesa
                        ultimos = self.db.obtener_ultimos(self.mesa.id, 20)
                        # Difundir a todos los clientes
                        self.gestor.difundir(self.mesa.id, ultimos)
                
                # Calcular siguiente intervalo
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
db = BaseDeDatos()
gestor_eventos = GestorEventos()
pollers = []

# Iniciar un poller por cada mesa
for mesa in MESAS:
    poller = PollerMesa(mesa, db, gestor_eventos)
    hilo = threading.Thread(target=poller.ejecutar, daemon=True)
    hilo.start()
    pollers.append(poller)
    logging.info(f"Poller lanzado para {mesa.nombre} (ID {mesa.id})")

@app.route('/')
def index():
    """Endpoint de estado."""
    return jsonify({
        "estado": "activo",
        "mesas": [
            {"id": mesa.id, "nombre": mesa.nombre}
            for mesa in MESAS
        ]
    })

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

# ============================================================================
# PUNTO DE ENTRADA
# ============================================================================
if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=puerto)
