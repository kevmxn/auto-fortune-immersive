#!/usr/bin/env python3
"""
Mega Roulette Telegram Signal Bot - Sistema AMX
  - Mega Roulette (ws_key=204)
  - Sin tabla de color predefinida: solo Markov + ML
  - Todas las funciones de unified_bot.py adaptadas
  - CategoryPredictor: 1024 patrones por categoría (COLOR, PARIDAD, RANGO)
  - AMXSignalSystem: EMA pura sin tabla
  - Probabilidad unificada ≥ 60% para emitir señal
  - Labouchère [1,2,1] — 5 intentos
  - Re-evalúa categoría en cada intento
  - Persistencia SQLite 24/7 (sin pre-entrenamiento)
  - Calentamiento WS: 21 giros silenciosos
  - Reporte diario 12:00 hora Argentina
  - Modo MODERADO / TENDENCIA
  - Sistema de recuperación tras pérdidas
"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.request
from collections import deque, defaultdict
from typing import Optional, Literal

import numpy as np
import telebot
import websockets
from flask import Flask, jsonify

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s %(message)s'
)
logger = logging.getLogger("MegaRouletteAMX")

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
TOKEN   = "8308452662:AAGZFIZyYsmVR39SvIOSlKD3OY_YNMOsEQU"
CHAT_ID = -1002753250188
THREAD_ID: Optional[int] = None

_session = requests.Session()
_retry = Retry(
    total=5, backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"], raise_on_status=False,
)
_adapter = HTTPAdapter(max_retries=_retry, pool_connections=10, pool_maxsize=20)
_session.mount("https://", _adapter)
_session.mount("http://",  _adapter)

bot = telebot.TeleBot(TOKEN, threaded=False)
bot.session = _session

# ─── CONSTANTES ───────────────────────────────────────────────────────────────
WS_URL             = "wss://dga.pragmaticplaylive.net/ws"
CASINO_ID          = "ppcjd00000007254"
WS_KEY             = 204
DB_TABLE           = "mega_roulette"
LIVE_DB_PATH       = "mega_live_spins.db"
BASE_BET           = 0.10
MAX_ATTEMPTS       = 5
WARMUP_SPINS       = 21
MIN_PROB_THRESHOLD = 0.60
LABOUCHERE_SEQUENCE: list[int] = [1, 2, 1]

# ─── COLORES POR NÚMERO ───────────────────────────────────────────────────────
REAL_COLOR_MAP: dict[int, str] = {
    0:"VERDE",
    1:"ROJO",2:"NEGRO",3:"ROJO",4:"NEGRO",5:"ROJO",6:"NEGRO",
    7:"ROJO",8:"NEGRO",9:"ROJO",10:"NEGRO",11:"NEGRO",12:"ROJO",
    13:"NEGRO",14:"ROJO",15:"NEGRO",16:"ROJO",17:"NEGRO",18:"ROJO",
    19:"ROJO",20:"NEGRO",21:"ROJO",22:"NEGRO",23:"ROJO",24:"NEGRO",
    25:"ROJO",26:"NEGRO",27:"ROJO",28:"NEGRO",29:"NEGRO",30:"ROJO",
    31:"NEGRO",32:"ROJO",33:"NEGRO",34:"ROJO",35:"NEGRO",36:"ROJO",
}

CATEGORY_ICONS: dict[str, str] = {
    "ROJO":"🔴","NEGRO":"⚫️","VERDE":"🟢",
    "PAR":"🟣","IMPAR":"🟡",
    "ALTO":"🔵","BAJO":"🟤",
}

def get_paridad(n: int) -> Optional[str]:
    if n == 0: return None
    return "PAR" if n % 2 == 0 else "IMPAR"

def get_rango(n: int) -> Optional[str]:
    if n == 0: return None
    return "ALTO" if n >= 19 else "BAJO"

def _opposite(color: str) -> str:
    return "NEGRO" if color == "ROJO" else "ROJO"

# ─── SQLITE ───────────────────────────────────────────────────────────────────
def _get_live_db() -> sqlite3.Connection:
    conn = sqlite3.connect(LIVE_DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS live_spins (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            number     INTEGER NOT NULL,
            ts         INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_table ON live_spins(table_name, id)")
    conn.commit()
    return conn

# ─── SOPORTE Y RESISTENCIA ────────────────────────────────────────────────────
def find_support_resistance(levels: list, lookback: int = 30) -> dict:
    if len(levels) < lookback:
        return {"support": None, "resistance": None}
    recent = levels[-lookback:]
    return {"support": min(recent), "resistance": max(recent)}

# ─── TELEGRAM HELPERS ─────────────────────────────────────────────────────────
_TG_MAX_RETRIES = 12

def _tg_call(fn, *args, **kwargs):
    delay = 2.0
    for attempt in range(1, _TG_MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            err = str(e)
            if "retry after" in err.lower():
                try:    wait = int(''.join(filter(str.isdigit, err))) + 1
                except: wait = 30
                logger.warning(f"Telegram flood-wait {wait}s")
                time.sleep(wait)
                continue
            if attempt == _TG_MAX_RETRIES:
                logger.error(f"Telegram call falló tras {_TG_MAX_RETRIES} intentos: {e}")
                return None
            logger.warning(f"Telegram error (intento {attempt}/{_TG_MAX_RETRIES}): {e}")
            time.sleep(delay)
            delay = min(delay * 2, 60)
    return None

def tg_send(text: str) -> Optional[int]:
    kwargs = dict(chat_id=CHAT_ID, text=text, parse_mode="HTML")
    if THREAD_ID:
        kwargs["message_thread_id"] = THREAD_ID
    msg = _tg_call(bot.send_message, **kwargs)
    return msg.message_id if msg else None

def tg_delete(msg_id: int):
    _tg_call(bot.delete_message, chat_id=CHAT_ID, message_id=msg_id)

# ─── MARKOV ───────────────────────────────────────────────────────────────────
class MarkovChainPredictor:
    ORDER = 2
    def __init__(self, window: int = 60, order: int = 2):
        self.window = window
        self.order  = order
        self.counts: dict = defaultdict(lambda: defaultdict(int))

    def update(self, spin_history: list):
        colors = [s["real"] for s in spin_history if s["real"] != "VERDE"]
        colors = colors[-self.window:]
        for i in range(len(colors) - self.order):
            pattern = tuple(colors[i:i+self.order])
            self.counts[pattern][colors[i+self.order]] += 1

    def predict(self, spin_history: list) -> Optional[dict]:
        colors = [s["real"] for s in spin_history if s["real"] != "VERDE"]
        if len(colors) < self.order:
            return None
        pattern = tuple(colors[-self.order:])
        c = dict(self.counts.get(pattern, {}))
        total = sum(c.values())
        if total < 3:
            return None
        return {k: v/total for k, v in c.items()}

# ─── ML PATTERN PREDICTOR ─────────────────────────────────────────────────────
class MLPatternPredictor:
    def __init__(self, pattern_length: int = 3):
        self.pattern_length = pattern_length
        self.pattern_counts: dict = defaultdict(lambda: defaultdict(int))
        self._known_len: int = 0

    def add_spin(self, spin_history: list):
        non_verde = [s["real"] for s in spin_history if s["real"] != "VERDE"]
        cur = len(non_verde)
        if cur <= self._known_len: return
        self._known_len = cur
        if cur < self.pattern_length + 1: return
        i = cur - self.pattern_length - 1
        pattern = tuple(non_verde[i:i+self.pattern_length])
        nxt = non_verde[i+self.pattern_length]
        if nxt in ("ROJO", "NEGRO"):
            self.pattern_counts[pattern][nxt] += 1

    def predict(self, spin_history: list) -> Optional[dict]:
        non_verde = [s["real"] for s in spin_history if s["real"] != "VERDE"]
        if len(non_verde) < self.pattern_length: return None
        pattern = tuple(non_verde[-self.pattern_length:])
        counts  = dict(self.pattern_counts.get(pattern, {}))
        total   = sum(counts.values())
        if total < 2: return None
        return {
            "ROJO":  counts.get("ROJO",  0) / total,
            "NEGRO": counts.get("NEGRO", 0) / total,
            "total": total,
        }

# ─── CATEGORY PREDICTOR — 1024 patrones ──────────────────────────────────────
class CategoryPredictor:
    PATTERN_LEN = 10  # 2¹⁰ = 1024 patrones por categoría

    def __init__(self):
        self._hist: dict[str, list[str]] = {
            "COLOR":[], "PARIDAD":[], "RANGO":[],
        }
        self._counts: dict[str, dict] = {
            "COLOR":   defaultdict(lambda: defaultdict(int)),
            "PARIDAD": defaultdict(lambda: defaultdict(int)),
            "RANGO":   defaultdict(lambda: defaultdict(int)),
        }

    def add_spin(self, number: int, real_color: str):
        if number == 0 or real_color == "VERDE": return
        par  = get_paridad(number)
        rang = get_rango(number)
        if not par or not rang: return
        new_vals = {"COLOR": real_color, "PARIDAD": par, "RANGO": rang}
        for cat, val in new_vals.items():
            hist = self._hist[cat]
            if len(hist) >= self.PATTERN_LEN:
                self._counts[cat][tuple(hist[-self.PATTERN_LEN:])][val] += 1
            hist.append(val)

    def predict_category(self, category: str) -> Optional[dict]:
        hist   = self._hist[category]
        counts = self._counts[category]
        if len(hist) < self.PATTERN_LEN: return None
        c = dict(counts.get(tuple(hist[-self.PATTERN_LEN:]), {}))
        total = sum(c.values())
        if total < 5: return None
        result = {k: v/total for k, v in c.items()}
        result["total"] = total
        return result

    def best_category(self, threshold: float = MIN_PROB_THRESHOLD) -> Optional[dict]:
        best = None
        for cat in ("COLOR", "PARIDAD", "RANGO"):
            pred = self.predict_category(cat)
            if pred is None: continue
            clean = {k: v for k, v in pred.items() if k != "total"}
            if not clean: continue
            top_val  = max(clean, key=clean.get)
            top_prob = clean[top_val]
            if top_prob >= threshold:
                if best is None or top_prob > best["probability"]:
                    best = {"category":cat,"bet_value":top_val,"probability":top_prob}
        return best

# ─── AMX SIGNAL SYSTEM — EMA pura, sin tabla de color ────────────────────────
class AMXSignalSystem:
    """
    Sistema AMX adaptado para Mega Roulette.
    Usa niveles acumulados (EMA 4/8/20) sin tabla de color predefinida.
    El nivel se construye en tiempo real: +1 ROJO, -1 NEGRO.
    """
    def __init__(self, mode: Literal["tendencia","moderado"] = "moderado"):
        self.mode             = mode
        self.last_signal_time = 0.0
        self.ultimos_puntos:  list  = []   # nivel acumulado COLOR
        self.last_two_colors: deque = deque(maxlen=2)
        self.last_two_same:   deque = deque(maxlen=2)  # True si el color repitió

    def update(self, color: str, expected: str):
        """Actualiza el nivel acumulado y la racha."""
        delta = 1 if color == "ROJO" else (-1 if color == "NEGRO" else 0)
        acc   = (self.ultimos_puntos[-1] + delta) if self.ultimos_puntos else delta
        self.ultimos_puntos.append(acc)
        self.last_two_colors.append(color)
        self.last_two_same.append(color == expected)

    @staticmethod
    def _ema(data: list, period: int) -> list:
        if len(data) < period:
            return [None] * len(data)
        mult = 2 / (period + 1)
        out  = [None] * (period - 1)
        prev = sum(data[:period]) / period
        out.append(prev)
        for i in range(period, len(data)):
            prev = data[i] * mult + prev * (1 - mult)
            out.append(prev)
        return out

    def check_signal(self, expected_color: str) -> bool:
        """
        Evalúa condiciones EMA para emitir señal.
        Sin tabla: solo usa el nivel acumulado.
        """
        pts = self.ultimos_puntos
        if len(pts) < 20:
            return False
        # Usar nivel normal para ROJO, invertido para NEGRO
        levels = pts if expected_color == "ROJO" else [-p for p in pts]
        e4  = self._ema(levels, 4)
        e8  = self._ema(levels, 8)
        e20 = self._ema(levels, 20)
        li  = len(levels) - 1
        if any(v is None for v in [e4[li], e8[li], e20[li]]):
            return False
        cur = levels[li]
        if self.mode == "tendencia":
            cruce = li > 0 and e4[li-1] is not None and e20[li-1] is not None and \
                    e4[li-1] <= e20[li-1] and e4[li] > e20[li]
            sobre = cur > e4[li] and cur > e8[li] and cur > e20[li]
            return cruce or sobre
        else:  # moderado
            cruce8 = li > 0 and e8[li-1] is not None and e20[li-1] is not None and \
                     e8[li-1] <= e20[li-1] and e8[li] > e20[li]
            sobre  = cur > e4[li] and cur > e8[li]
            patron_v = False
            if len(levels) >= 3:
                a, b, c = levels[-3], levels[-2], levels[-1]
                patron_v = b < a and b < c and c > a
            return cruce8 or sobre or patron_v

    def register_signal_sent(self):
        self.last_signal_time = time.time()

# ─── UNIFIED PROBABILITY SYSTEM — sin tabla de color ─────────────────────────
class UnifiedProbabilitySystem:
    """
    Markov + ML con pesos adaptativos.
    table_prob = 0.5 siempre (neutral, sin tabla predefinida).
    """
    def __init__(self):
        self.weights = {"markov": 0.40, "ml": 0.60}
        self.markov_correct = self.markov_total = 0
        self.ml_correct     = self.ml_total     = 0
        self.spins_since_update = 0
        self.UPDATE_INTERVAL    = 50
        self.volatility      = 1.0
        self.ema_trend_factor = 1.0
        self.sr_factor        = 1.0
        self.confidence_factor = 0.5
        self.current_streak    = 0
        self.streak_direction: Optional[str] = None

    def _calc_ema(self, data: list, period: int) -> Optional[float]:
        if len(data) < period: return None
        mult = 2 / (period + 1)
        prev = sum(data[:period]) / period
        for v in data[period:]:
            prev = v * mult + prev * (1 - mult)
        return prev

    def calculate_volatility(self, levels: list) -> float:
        if len(levels) < 20: return 1.0
        std = float(np.std(levels[-20:]))
        self.volatility = min(max(std / 5.0, 0.5), 1.5)
        return self.volatility

    def update_trend_factors(self, levels: list):
        if len(levels) < 20:
            self.ema_trend_factor = 1.0
            self.sr_factor        = 1.0
            return
        ema20 = self._calc_ema(levels, 20)
        if ema20 is not None and levels:
            cur  = levels[-1]
            diff = (cur - ema20) / (abs(ema20) + 1) * 0.2
            self.ema_trend_factor = max(0.8, min(1.2,
                1.0 + diff if cur > ema20 else 1.0 - abs(diff)))
        sr = find_support_resistance(levels, lookback=30)
        if sr["support"] is not None and sr["resistance"] is not None:
            rng = sr["resistance"] - sr["support"]
            if rng > 0:
                pos = (levels[-1] - sr["support"]) / rng
                self.sr_factor = max(0.9, min(1.1, 1.0 + (pos - 0.5) * 0.1))
        else:
            self.sr_factor = 1.0

    def update_streak(self, color: str):
        if self.streak_direction == color:
            self.current_streak += 1
        else:
            self.streak_direction = color
            self.current_streak   = 1

    def update_weights(self):
        self.spins_since_update += 1
        if self.spins_since_update < self.UPDATE_INTERVAL: return
        self.spins_since_update = 0
        m_acc = self.markov_correct / max(self.markov_total, 1)
        l_acc = self.ml_correct     / max(self.ml_total,     1)
        total = m_acc + l_acc
        if total > 0:
            self.weights["markov"] = max(0.2, min(0.6, m_acc / total))
            self.weights["ml"]     = max(0.4, min(0.8, l_acc / total))
            s = self.weights["markov"] + self.weights["ml"]
            self.weights["markov"] /= s
            self.weights["ml"]     /= s
        self.markov_correct = self.markov_total = self.ml_correct = self.ml_total = 0
        logger.info(f"[Mega] Pesos: M={self.weights['markov']:.2f} ML={self.weights['ml']:.2f}")

    def record_prediction(self, color: str, markov_pred, ml_pred, actual: str):
        if markov_pred:
            self.markov_total += 1
            if (markov_pred.get(color, 0) > 0.5) == (actual == color):
                self.markov_correct += 1
        if ml_pred:
            self.ml_total += 1
            if (ml_pred.get(color, 0) > 0.5) == (actual == color):
                self.ml_correct += 1

    def get_joint_probability(self, category: str, bet_value: str,
                              markov_pred, ml_pred, cat_prob: Optional[float]) -> dict:
        # Para COLOR usar Markov/ML directamente; para PARIDAD/RANGO usar cat_prob
        if category == "COLOR":
            m_p  = markov_pred.get(bet_value, 0.5) if markov_pred else 0.5
            ml_p = ml_pred.get(bet_value, 0.5)     if ml_pred     else 0.5
        else:
            m_p  = cat_prob if cat_prob is not None else 0.5
            ml_p = cat_prob if cat_prob is not None else 0.5

        model_prob    = self.weights["markov"] * m_p + self.weights["ml"] * ml_p
        # Sin tabla: no mezclamos table_prob; solo ajustes EMA/SR
        combined_prob = model_prob * self.ema_trend_factor * self.sr_factor
        combined_prob = max(0.30, min(0.95, combined_prob))

        strength = ("strong"   if combined_prob >= MIN_PROB_THRESHOLD + 0.10 else
                    "moderate" if combined_prob >= MIN_PROB_THRESHOLD         else "weak")
        return {
            "combined_prob":    combined_prob,
            "markov_prob":      m_p,
            "ml_prob":          ml_p,
            "signal_strength":  strength,
            "weights":          self.weights.copy(),
            "ema_trend_factor": self.ema_trend_factor,
            "sr_factor":        self.sr_factor,
            "volatility":       self.volatility,
        }

# ─── LABOUCHÈRE ───────────────────────────────────────────────────────────────
class Labouchere:
    def __init__(self, sequence: list[int], base: float):
        self.base         = base
        self.original_seq = list(sequence)
        self.sequence     = list(sequence)
        self.bankroll     = 0.0

    @property
    def step(self) -> int:
        return max(0, len(self.sequence) - len(self.original_seq))

    def is_fresh(self) -> bool:
        return self.sequence == self.original_seq

    def reset(self):
        self.sequence = list(self.original_seq)

    def set_sequence(self, new_seq: list[int]):
        self.original_seq = list(new_seq)
        self.sequence     = list(new_seq)

    def current_bet(self) -> float:
        if not self.sequence: self.reset()
        val = self.sequence[0] + self.sequence[-1] if len(self.sequence) >= 2 else self.sequence[0]
        return round(self.base * val, 2)

    def win(self) -> float:
        bet = self.current_bet()
        self.bankroll = round(self.bankroll + bet, 2)
        if len(self.sequence) >= 2:
            self.sequence.pop(0); self.sequence.pop(-1)
        elif self.sequence:
            self.sequence.pop(0)
        if not self.sequence: self.reset()
        return bet

    def loss(self) -> float:
        bet = self.current_bet()
        self.bankroll = round(self.bankroll - bet, 2)
        units = round(bet / self.base)
        self.sequence.append(units if units > 0 else 1)
        return bet

    def sequence_display(self) -> str:
        return " - ".join(str(v) for v in self.sequence)

# ─── DETAILED STATS ───────────────────────────────────────────────────────────
class DetailedStats:
    def __init__(self):
        self.signal_history: deque = deque(maxlen=50)
        self.wins_attempt_1 = self.wins_attempt_2 = self.wins_attempt_3 = 0
        self.wins_attempt_4 = self.wins_attempt_5 = 0
        self.losses = self.total_signals = self.last_stats_at = 0
        self.batch_start_bankroll: Optional[float] = None
        self.batch_start_wins = self.batch_start_losses = 0
        self.batch_start_w1 = self.batch_start_w2 = self.batch_start_w3 = 0
        self.batch_start_w4 = self.batch_start_w5 = 0
        self.last_daily_date      = ""
        self.daily_start_bankroll: Optional[float] = None
        self.daily_signals  = 0
        self.daily_wins     = 0
        self.daily_losses   = 0
        self.daily_w1 = self.daily_w2 = self.daily_w3 = 0
        self.daily_w4 = self.daily_w5 = 0

    def record_signal_result(self, attempt_won: int, final_result: bool,
                             bet_amount: float, bankroll: float):
        entry = {"attempt_won":attempt_won,"won":final_result,
                 "bet":bet_amount,"bankroll":bankroll,"timestamp":time.time()}
        self.signal_history.append(entry)
        self.total_signals += 1
        if final_result:
            if   attempt_won == 1: self.wins_attempt_1 += 1
            elif attempt_won == 2: self.wins_attempt_2 += 1
            elif attempt_won == 3: self.wins_attempt_3 += 1
            elif attempt_won == 4: self.wins_attempt_4 += 1
            elif attempt_won == 5: self.wins_attempt_5 += 1
        else:
            self.losses += 1
        self.daily_signals += 1
        if final_result:
            self.daily_wins += 1
            if   attempt_won == 1: self.daily_w1 += 1
            elif attempt_won == 2: self.daily_w2 += 1
            elif attempt_won == 3: self.daily_w3 += 1
            elif attempt_won == 4: self.daily_w4 += 1
            elif attempt_won == 5: self.daily_w5 += 1
        else:
            self.daily_losses += 1
        if self.daily_start_bankroll is None:
            self.daily_start_bankroll = bankroll

    def should_send_stats(self) -> bool:
        return (self.total_signals - self.last_stats_at) >= 20

    def mark_stats_sent(self, bankroll: float):
        self.last_stats_at        = self.total_signals
        self.batch_start_bankroll = bankroll
        self.batch_start_wins     = sum([self.wins_attempt_1, self.wins_attempt_2,
                                         self.wins_attempt_3, self.wins_attempt_4,
                                         self.wins_attempt_5])
        self.batch_start_losses   = self.losses
        self.batch_start_w1 = self.wins_attempt_1
        self.batch_start_w2 = self.wins_attempt_2
        self.batch_start_w3 = self.wins_attempt_3
        self.batch_start_w4 = self.wins_attempt_4
        self.batch_start_w5 = self.wins_attempt_5

    def get_batch_stats(self, bk: float) -> dict:
        n = self.total_signals - self.last_stats_at
        if n == 0: return {}
        w1=self.wins_attempt_1-self.batch_start_w1; w2=self.wins_attempt_2-self.batch_start_w2
        w3=self.wins_attempt_3-self.batch_start_w3; w4=self.wins_attempt_4-self.batch_start_w4
        w5=self.wins_attempt_5-self.batch_start_w5; l=self.losses-self.batch_start_losses
        w=w1+w2+w3+w4+w5
        bd=round(bk-self.batch_start_bankroll,2) if self.batch_start_bankroll is not None else 0.0
        return {"total":n,"wins":w,"losses":l,"w1":w1,"w2":w2,"w3":w3,"w4":w4,"w5":w5,
                "efficiency":round(w/n*100,1) if n else 0.0,
                "e_w1":round(w1/n*100,2) if n else 0.0,"e_w2":round(w2/n*100,2) if n else 0.0,
                "e_w3":round(w3/n*100,2) if n else 0.0,"e_w4":round(w4/n*100,2) if n else 0.0,
                "e_w5":round(w5/n*100,2) if n else 0.0,"e_loss":round(l/n*100,2) if n else 0.0,
                "bankroll_delta":bd}

    def get_daily_stats(self, bk: float) -> dict:
        t=self.daily_signals; w=self.daily_wins; l=self.daily_losses
        bd=round(bk-self.daily_start_bankroll,2) if self.daily_start_bankroll is not None else 0.0
        return {"total":t,"wins":w,"losses":l,
                "w1":self.daily_w1,"w2":self.daily_w2,"w3":self.daily_w3,
                "w4":self.daily_w4,"w5":self.daily_w5,
                "efficiency":round(w/t*100,1) if t else 0.0,
                "e_w1":round(self.daily_w1/t*100,2) if t else 0.0,
                "e_w2":round(self.daily_w2/t*100,2) if t else 0.0,
                "e_w3":round(self.daily_w3/t*100,2) if t else 0.0,
                "e_w4":round(self.daily_w4/t*100,2) if t else 0.0,
                "e_w5":round(self.daily_w5/t*100,2) if t else 0.0,
                "e_loss":round(l/t*100,2) if t else 0.0,"bankroll_delta":bd}

    def reset_daily(self, date_str: str, bankroll: float):
        self.last_daily_date      = date_str
        self.daily_start_bankroll = bankroll
        self.daily_signals=self.daily_wins=self.daily_losses=0
        self.daily_w1=self.daily_w2=self.daily_w3=self.daily_w4=self.daily_w5=0

    def reset(self):
        self.signal_history.clear()
        self.wins_attempt_1=self.wins_attempt_2=self.wins_attempt_3=0
        self.wins_attempt_4=self.wins_attempt_5=0
        self.losses=self.total_signals=self.last_stats_at=0
        self.batch_start_bankroll=None
        self.reset_daily("", 0.0)

# ─── ROULETTE ENGINE ──────────────────────────────────────────────────────────
class RouletteEngine:

    def __init__(self):
        # Predictores
        self.markov        = MarkovChainPredictor(window=60, order=2)
        self.ml_predictor  = MLPatternPredictor(pattern_length=3)
        self.category_ml   = CategoryPredictor()
        self.prob_system   = UnifiedProbabilitySystem()
        self.amx_system    = AMXSignalSystem(mode="moderado")

        # Niveles acumulados (sin tabla de color — construidos en tiempo real)
        self.color_levels:    list = []   # +1 ROJO, -1 NEGRO
        self.inv_color_levels: list = []  # inverso: +1 NEGRO, -1 ROJO

        # Apuestas
        self.bet_sys = Labouchere(LABOUCHERE_SEQUENCE, BASE_BET)

        # Estado señal
        self.signal_active:          bool = False
        self.waiting_for_attempt:    bool = False
        self.waiting_attempt_number: int  = 0
        self.skip_one_after_zero:    bool = False
        self.active_category: Optional[str] = None
        self.bet_value:       Optional[str] = None
        self.attempts_left:   int = MAX_ATTEMPTS
        self.total_attempts:  int = MAX_ATTEMPTS
        self.trigger_number:  int = 0
        self.signal_msg_ids:  list = []

        # Recuperación
        self.consec_losses:    int   = 0
        self.recovery_active:  bool  = False
        self.recovery_target:  float = 0.0
        self.level1_bankroll:  float = 0.0
        self.signal_is_level1: bool  = False

        # Historial
        self.spin_history: list = []

        # Stats
        self.stats = DetailedStats()

        # Persistencia
        self._live_conn = _get_live_db()
        live_loaded     = self._load_live_history()
        self.ws_spins_count: int  = live_loaded
        self.warmup_done:    bool = live_loaded >= WARMUP_SPINS

        if self.warmup_done:
            logger.info(f"[Mega] ✅ {live_loaded} giros cargados → señales activas")
        else:
            logger.info(f"[Mega] Calentamiento: {live_loaded}/{WARMUP_SPINS} giros")

    # ── Persistencia ──────────────────────────────────────────────────────────
    def _load_live_history(self) -> int:
        try:
            cutoff = int(time.time()) - 7 * 86400
            rows   = self._live_conn.execute(
                "SELECT number FROM live_spins WHERE table_name=? AND ts>=? ORDER BY id ASC",
                (DB_TABLE, cutoff)
            ).fetchall()
        except Exception as e:
            logger.warning(f"[Mega] Error cargando live history: {e}")
            return 0
        if not rows: return 0
        for (n,) in rows:
            real = REAL_COLOR_MAP.get(n, "VERDE")
            entry = {"number": n, "real": real}
            self.spin_history.append(entry)
            if len(self.spin_history) > 300:
                self.spin_history.pop(0)
            self.markov.update(self.spin_history)
            self.ml_predictor.add_spin(self.spin_history)
            self.category_ml.add_spin(n, real)
            self._update_levels(real)
        logger.info(f"[Mega] ✅ Historial ML cargado: {len(rows)} giros")
        return len(rows)

    def _persist_spin(self, number: int):
        try:
            self._live_conn.execute(
                "INSERT INTO live_spins (table_name, number, ts) VALUES (?,?,?)",
                (DB_TABLE, number, int(time.time()))
            )
            self._live_conn.commit()
        except Exception as e:
            logger.warning(f"[Mega] SQLite error, reconectando: {e}")
            try:
                self._live_conn = _get_live_db()
                self._live_conn.execute(
                    "INSERT INTO live_spins (table_name, number, ts) VALUES (?,?,?)",
                    (DB_TABLE, number, int(time.time()))
                )
                self._live_conn.commit()
            except Exception as e2:
                logger.error(f"[Mega] SQLite irrecuperable: {e2}")

    def _cleanup_old_spins(self):
        try:
            cutoff = int(time.time()) - 7 * 86400
            self._live_conn.execute(
                "DELETE FROM live_spins WHERE table_name=? AND ts<?", (DB_TABLE, cutoff)
            )
            self._live_conn.commit()
        except Exception as e:
            logger.debug(f"[Mega] Error limpiando live_db: {e}")

    # ── Niveles acumulados ────────────────────────────────────────────────────
    def _update_levels(self, real: str):
        """Actualiza los niveles de COLOR sin tabla de color predefinida."""
        if real == "VERDE": return
        delta = 1 if real == "ROJO" else -1
        acc  = (self.color_levels[-1] + delta)    if self.color_levels    else delta
        iacc = (self.inv_color_levels[-1] - delta) if self.inv_color_levels else -delta
        self.color_levels.append(acc)
        self.inv_color_levels.append(iacc)
        self.amx_system.update(real, "ROJO")  # referencia neutra

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _cat_icon(self, val: str) -> str:
        return CATEGORY_ICONS.get(val, "❓")

    def _cat_val(self, number: int, real: str) -> tuple[str, str]:
        cat = self.active_category or "COLOR"
        if cat == "COLOR":      val = real
        elif cat == "PARIDAD":  val = get_paridad(number) or "VERDE"
        else:                   val = get_rango(number)   or "VERDE"
        return val, self._cat_icon(val)

    def _trigger_display(self, number: int, category: str) -> str:
        if number == 0: return "0 VERDE 🟢"
        if category == "COLOR":     val = REAL_COLOR_MAP.get(number, "VERDE")
        elif category == "PARIDAD": val = get_paridad(number) or "VERDE"
        else:                       val = get_rango(number)   or "VERDE"
        return f"{number} {val} {self._cat_icon(val)}"

    def set_mode(self, mode: str):
        self.amx_system.mode = mode
        logger.info(f"[Mega] Modo → {mode}")

    # ── Probabilidad ──────────────────────────────────────────────────────────
    def _get_category_probability(self, category: str, bet_value: str,
                                   trigger_number: int) -> dict:
        markov_pred = self.markov.predict(self.spin_history)
        ml_pred     = self.ml_predictor.predict(self.spin_history)
        cat_pred    = self.category_ml.predict_category(category)
        cat_prob: Optional[float] = None
        if cat_pred:
            clean    = {k:v for k,v in cat_pred.items() if k != "total"}
            cat_prob = clean.get(bet_value)
        return self.prob_system.get_joint_probability(
            category, bet_value, markov_pred, ml_pred, cat_prob)

    def _passes_markov_ml_filter(self, color: str) -> bool:
        """Verifica que Markov y ML no rechacen el color apostado."""
        mp = self.markov.predict(self.spin_history)
        ml = self.ml_predictor.predict(self.spin_history)
        if mp is not None and mp.get(color, 0) < 0.50:
            return False
        if ml is not None and ml.get(color, 0) < 0.50:
            return False
        return True

    # ── AMX signal check — sin tabla ──────────────────────────────────────────
    def _detect_amx_signal(self, expected_color: str) -> bool:
        """Verifica condiciones EMA para el color esperado."""
        if len(self.color_levels) < 20:
            return False
        return self.amx_system.check_signal(expected_color)

    # ── Detección de mejor categoría ──────────────────────────────────────────
    def _detect_best_category_signal(self) -> Optional[dict]:
        """
        Evalúa COLOR, PARIDAD y RANGO.
        Para COLOR: requiere además que las condiciones EMA y Markov/ML sean favorables.
        Retorna la categoría con mayor probabilidad ≥ 60%.
        """
        candidates = []
        trigger    = self.spin_history[-1]["number"] if self.spin_history else 0

        for cat in ("COLOR", "PARIDAD", "RANGO"):
            pred = self.category_ml.predict_category(cat)
            if pred is None: continue
            clean = {k:v for k,v in pred.items() if k != "total"}
            if not clean: continue
            top_val  = max(clean, key=clean.get)
            top_prob = clean[top_val]
            if top_prob < MIN_PROB_THRESHOLD: continue

            # Para COLOR: filtro adicional AMX + Markov/ML
            if cat == "COLOR":
                if not self._detect_amx_signal(top_val): continue
                if not self._passes_markov_ml_filter(top_val): continue

            candidates.append({"category":cat,"bet_value":top_val,
                                "probability":top_prob,"trigger_number":trigger})

        if not candidates: return None
        return max(candidates, key=lambda x: x["probability"])

    # ── Recuperación ──────────────────────────────────────────────────────────
    def _check_recovery(self):
        if self.recovery_active:
            if self.bet_sys.bankroll >= self.recovery_target:
                self.recovery_active = False
                self.consec_losses   = 0
                logger.info("[Mega] ✅ Recuperación completada")
        elif not self.signal_is_level1:
            pass  # no fue un nivel 1, no afecta recovery

    def _handle_full_loss(self, number: int, real: str, bet: Optional[float] = None):
        if bet is None:
            bet = self.bet_sys.loss()
        bk = self.bet_sys.bankroll
        self.consec_losses += 1
        if self.signal_is_level1:
            self.recovery_active = True
            self.recovery_target = self.level1_bankroll
        self.stats.record_signal_result(0, False, bet, bk)
        self._send_result(number, real, False, bet, 0)
        self._reset_signal()
        self._check_daily_report()
        self._check_stats()

    def _reset_signal(self):
        self.signal_active       = False
        self.waiting_for_attempt = False
        self.active_category     = None
        self.bet_value           = None
        self.attempts_left       = MAX_ATTEMPTS
        self.signal_msg_ids      = []

    def _is_win(self, number: int, real: str) -> Optional[bool]:
        """None = VERDE (neutral), True = win, False = loss."""
        if real == "VERDE": return None
        if self.active_category == "COLOR":    return real == self.bet_value
        elif self.active_category == "PARIDAD": return get_paridad(number) == self.bet_value
        else:                                   return get_rango(number) == self.bet_value

    # ── Mensajes ──────────────────────────────────────────────────────────────
    def _build_signal_text(self, attempt: int, unified_prob: dict) -> str:
        bet      = self.bet_sys.current_bet()
        prob_pct = int(unified_prob["combined_prob"] * 100)
        val_icon = self._cat_icon(self.bet_value or "")
        trig     = self._trigger_display(self.trigger_number, self.active_category or "COLOR")
        return (
            f"☑️☑️ <b>SEÑAL CONFIRMADA</b> ☑️☑️\n\n"
            f"🎰 Mega Roulette\n\n"
            f"👉 Después de: {trig}\n"
            f"🎯 Apostar a: <b>{self.bet_value}</b> {val_icon}\n"
            f"🤖 Probabilidad Unificada: {prob_pct}%\n"
            f"🎲 Labouchère: [{self.bet_sys.sequence_display()}]\n"
            f"📍 Apuesta: {bet:.2f} usd\n\n"
            f"♻️ Intento {attempt}/{MAX_ATTEMPTS}"
        )

    def _send_signal(self, attempt: int, unified_prob: dict):
        self.signal_is_level1 = self.bet_sys.is_fresh() and not self.recovery_active
        if self.signal_is_level1:
            self.level1_bankroll = self.bet_sys.bankroll
        for mid in self.signal_msg_ids:
            tg_delete(mid)
        self.signal_msg_ids = []
        msg_id = tg_send(self._build_signal_text(attempt, unified_prob))
        if msg_id:
            self.signal_msg_ids.append(msg_id)
        self.amx_system.register_signal_sent()
        logger.info(
            f"[Mega] 🎯 [{self.active_category}] {self.bet_value} "
            f"intento={attempt} prob={int(unified_prob['combined_prob']*100)}%"
        )

    def _send_waiting_message(self, attempt_number: int):
        for mid in self.signal_msg_ids:
            tg_delete(mid)
        self.signal_msg_ids = []
        logger.info(f"[Mega] ⏳ Esperando condiciones intento {attempt_number}")

    def _send_result(self, number: int, real: str, won: bool, bet: float, attempt_won: int):
        for mid in self.signal_msg_ids:
            tg_delete(mid)
        self.signal_msg_ids = []
        bk                = self.bet_sys.bankroll
        cat_val, cat_icon = self._cat_val(number, real)
        bet_icon          = self._cat_icon(self.bet_value or "")
        status            = "✅ Acierto" if won else "❌ Fallo"
        tg_send(
            f"{status}\n\n"
            f"🎯 Aposté a: <b>{self.bet_value}</b> {bet_icon}\n"
            f"🔢 Salió: {number} <b>{cat_val}</b> {cat_icon}\n"
            f"💰 Bankroll: {bk:.2f} usd"
        )
        logger.info(f"[Mega] {'WIN' if won else 'LOSS'} #{number} "
                    f"cat_val={cat_val} intento={attempt_won} bk={bk:.2f}")

    def _send_no_confirm(self, attempt_number: int):
        ords    = {2:"2°",3:"3°",4:"4°",5:"5°"}
        ord_str = ords.get(attempt_number, f"{attempt_number}°")
        tg_send(f"🔔 Sin confirmación para enviar señal para el intento {ord_str}")

    # ── Stats ─────────────────────────────────────────────────────────────────
    def _check_stats(self):
        if not self.stats.should_send_stats(): return
        bk  = self.bet_sys.bankroll
        s20 = self.stats.get_batch_stats(bk)
        s24 = self.stats.get_daily_stats(bk)
        self.stats.mark_stats_sent(bk)
        text = ""
        if s20:
            text += (
                f"👉🏼 <b>ESTADISTICAS {s20['total']} SENALES — Mega Roulette</b>\n"
                f"🈯️ <b>T:</b> {s20['total']} 📈 <b>E:</b> {s20['efficiency']}%\n"
                f"1️⃣ <b>W:</b> {s20['w1']} --> <b>E:</b> {s20['e_w1']}%\n"
                f"2️⃣ <b>W:</b> {s20['w2']} --> <b>E:</b> {s20['e_w2']}%\n"
                f"3️⃣ <b>W:</b> {s20['w3']} --> <b>E:</b> {s20['e_w3']}%\n"
                f"4️⃣ <b>W:</b> {s20['w4']} --> <b>E:</b> {s20['e_w4']}%\n"
                f"5️⃣ <b>W:</b> {s20['w5']} --> <b>E:</b> {s20['e_w5']}%\n"
                f"🈲 <b>L:</b> {s20['losses']} --> <b>E:</b> {s20['e_loss']}%\n"
                f"💰 <i>Bankroll: {s20['bankroll_delta']:.2f} usd</i>\n\n"
            )
        if s24 and s24.get("total", 0) > 0:
            text += (
                f"👉🏼 <b>ESTADISTICAS 24 HORAS</b>\n"
                f"🈯️ <b>T:</b> {s24['total']} 📈 <b>E:</b> {s24['efficiency']}%\n"
                f"1️⃣ <b>W:</b> {s24['w1']} --> <b>E:</b> {s24['e_w1']}%\n"
                f"2️⃣ <b>W:</b> {s24['w2']} --> <b>E:</b> {s24['e_w2']}%\n"
                f"3️⃣ <b>W:</b> {s24['w3']} --> <b>E:</b> {s24['e_w3']}%\n"
                f"4️⃣ <b>W:</b> {s24['w4']} --> <b>E:</b> {s24['e_w4']}%\n"
                f"5️⃣ <b>W:</b> {s24['w5']} --> <b>E:</b> {s24['e_w5']}%\n"
                f"🈲 <b>L:</b> {s24['losses']} --> <b>E:</b> {s24['e_loss']}%\n"
                f"💰 <i>Bankroll 24h: {s24['bankroll_delta']:.2f} usd</i>"
            )
        if text: tg_send(text)

    def _check_daily_report(self):
        import datetime
        tz_ar  = datetime.timezone(datetime.timedelta(hours=-3))
        now_ar = datetime.datetime.now(tz=tz_ar)
        if now_ar.hour < 12: return
        today  = now_ar.strftime("%Y-%m-%d")
        if self.stats.last_daily_date == today: return
        bk = self.bet_sys.bankroll
        sd = self.stats.get_daily_stats(bk)
        if sd["total"] == 0:
            self.stats.reset_daily(today, bk)
            return
        tg_send(
            f"📅 <b>REPORTE DIARIO — {now_ar.strftime('%d/%m/%Y')}</b>\n"
            f"🕛 Actualizado a las 12:00 hs (AR)\n\n"
            f"🎰 Juego: <b>Mega Roulette</b>\n"
            f"🈯️ <b>Total señales:</b> {sd['total']}\n"
            f"📈 <b>Eficiencia:</b> {sd['efficiency']}%\n\n"
            f"1️⃣ <b>W:</b> {sd['w1']} ({sd['e_w1']}%)\n"
            f"2️⃣ <b>W:</b> {sd['w2']} ({sd['e_w2']}%)\n"
            f"3️⃣ <b>W:</b> {sd['w3']} ({sd['e_w3']}%)\n"
            f"4️⃣ <b>W:</b> {sd['w4']} ({sd['e_w4']}%)\n"
            f"5️⃣ <b>W:</b> {sd['w5']} ({sd['e_w5']}%)\n"
            f"🈲 <b>L:</b> {sd['losses']} ({sd['e_loss']}%)\n\n"
            f"💰 <b>Balance del día: {sd['bankroll_delta']:+.2f} usd</b>"
        )
        logger.info(f"[Mega] Reporte diario enviado ({today})")
        self.stats.reset_daily(today, bk)

    # ── Proceso principal ─────────────────────────────────────────────────────
    def process_number(self, number: int):
        try:
            self._process_inner(number)
        except Exception as e:
            logger.error(f"[Mega] ❌ Error en process_number({number}): {e}", exc_info=True)
            if self.signal_active:
                self._reset_signal()

    def _process_inner(self, number: int):
        real = REAL_COLOR_MAP.get(number, "VERDE")

        # Persistir
        self._persist_spin(number)
        if len(self.spin_history) > 0 and len(self.spin_history) % 5000 == 0:
            self._cleanup_old_spins()

        # Actualizar historial
        self.spin_history.append({"number": number, "real": real})
        if len(self.spin_history) > 300:
            self.spin_history.pop(0)

        # Actualizar predictores y niveles
        self.markov.update(self.spin_history)
        self.ml_predictor.add_spin(self.spin_history)
        self.category_ml.add_spin(number, real)
        self._update_levels(real)

        # Actualizar sistema de probabilidad
        self.prob_system.update_streak(real)
        self.prob_system.calculate_volatility(self.color_levels)
        self.prob_system.update_trend_factors(self.color_levels)
        self.prob_system.update_weights()

        # Calentamiento
        if not self.warmup_done:
            self.ws_spins_count += 1
            if self.ws_spins_count < WARMUP_SPINS:
                logger.info(f"[Mega] Calentamiento: {self.ws_spins_count}/{WARMUP_SPINS}")
                return
            self.warmup_done = True
            logger.info("[Mega] ✅ Calentamiento completado. Iniciando señales.")
            tg_send("🟢 <b>Mega Roulette</b> — Sistema listo. Emitiendo señales.")

        # ══════════════════════════════════════════════════════════════════════
        #  MÁQUINA DE ESTADOS
        # ══════════════════════════════════════════════════════════════════════

        # ── ESTADO 1: Señal activa ─────────────────────────────────────────
        if self.signal_active:
            result = self._is_win(number, real)

            if result is None:  # VERDE
                self.attempts_left -= 1
                if self.attempts_left <= 0:
                    self._handle_full_loss(number, real)
                    return
                attempt_number = MAX_ATTEMPTS - self.attempts_left + 1
                self.signal_active      = False
                self.waiting_for_attempt = True
                self.waiting_attempt_number = attempt_number
                self.skip_one_after_zero = True
                self._send_waiting_message(attempt_number)
                return

            current_attempt = MAX_ATTEMPTS - self.attempts_left + 1

            if result:
                bet = self.bet_sys.win()
                self.stats.record_signal_result(current_attempt, True, bet, self.bet_sys.bankroll)
                if self.active_category == "COLOR":
                    self.prob_system.record_prediction(
                        self.bet_value or "", self.markov.predict(self.spin_history),
                        self.ml_predictor.predict(self.spin_history), real)
                self.signal_active   = False
                self.active_category = None
                self._check_recovery()
                self._send_result(number, real, True, bet, current_attempt)
                self._check_daily_report()
                self._check_stats()
                self.signal_msg_ids = []
            else:
                self.attempts_left -= 1
                bet = self.bet_sys.loss()
                if self.attempts_left <= 0:
                    self._handle_full_loss(number, real, bet)
                else:
                    attempt_number = MAX_ATTEMPTS - self.attempts_left + 1
                    self.signal_active      = False
                    self.waiting_for_attempt = True
                    self.waiting_attempt_number = attempt_number
                    self._send_waiting_message(attempt_number)

        # ── ESTADO 2: Esperando condiciones para reintento ─────────────────
        elif self.waiting_for_attempt:
            if real == "VERDE":
                self.skip_one_after_zero = True
                return
            if self.skip_one_after_zero:
                self.skip_one_after_zero = False
                return
            attempt_number = self.waiting_attempt_number
            best = self._detect_best_category_signal()
            if not best or best["probability"] < MIN_PROB_THRESHOLD:
                self._send_no_confirm(attempt_number)
            else:
                unified_prob = self._get_category_probability(
                    best["category"], best["bet_value"], number)
                if unified_prob["combined_prob"] < MIN_PROB_THRESHOLD:
                    logger.debug(
                        f"[Mega] Reintento descartado [{best['category']}] "
                        f"{best['bet_value']} prob={unified_prob['combined_prob']*100:.0f}% < 60%")
                    self._send_no_confirm(attempt_number)
                else:
                    self.active_category    = best["category"]
                    self.bet_value          = best["bet_value"]
                    self.trigger_number     = number
                    self.signal_active      = True
                    self.waiting_for_attempt = False
                    self._send_signal(attempt_number, unified_prob)

        # ── ESTADO 3: Idle — buscar señal ─────────────────────────────────
        else:
            self.signal_msg_ids = []
            if len(self.spin_history) < 22 + self.consec_losses * 2:
                return
            best = self._detect_best_category_signal()
            if best:
                unified_prob = self._get_category_probability(
                    best["category"], best["bet_value"], best["trigger_number"])
                if unified_prob["combined_prob"] < MIN_PROB_THRESHOLD:
                    logger.debug(
                        f"[Mega] Señal descartada [{best['category']}] "
                        f"{best['bet_value']} prob={unified_prob['combined_prob']*100:.0f}% < 60%")
                    return
                self.signal_active   = True
                self.active_category = best["category"]
                self.bet_value       = best["bet_value"]
                self.attempts_left   = MAX_ATTEMPTS
                self.total_attempts  = MAX_ATTEMPTS
                self.trigger_number  = best["trigger_number"]
                self._send_signal(1, unified_prob)

    # ── WebSocket ─────────────────────────────────────────────────────────────
    async def run_ws(self):
        """
        WS de Pragmatic — formato:
        {
          "totalSeatedPlayers": N,
          "last20Results": [
            {"result": "22", "color": "black", "gameId": "...", ...},
            ...
          ]
        }
        El primer elemento de last20Results es el giro más reciente.
        Se usa gameId para evitar procesar el mismo giro dos veces.
        """
        reconnect_delay  = 5
        last_game_id: Optional[str] = None

        while True:
            try:
                async with websockets.connect(
                    WS_URL, ping_interval=30, ping_timeout=60, close_timeout=10
                ) as ws:
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "key":  WS_KEY,
                        "casinoId": CASINO_ID,
                    }))
                    logger.info(f"[Mega] ✅ WS conectado — key={WS_KEY}")
                    reconnect_delay = 5

                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                        except Exception:
                            continue
                        if not isinstance(data, dict):
                            continue

                        # Extraer el giro más reciente de last20Results
                        results = data.get("last20Results")
                        if not results or not isinstance(results, list):
                            continue

                        latest    = results[0]
                        game_id   = str(latest.get("gameId", ""))
                        result_str = latest.get("result", "")

                        # Evitar duplicados — mismo gameId = mismo giro
                        if game_id == last_game_id:
                            continue
                        last_game_id = game_id

                        try:
                            number = int(result_str)
                        except (TypeError, ValueError):
                            logger.warning(f"[Mega] No se pudo parsear result='{result_str}'")
                            continue

                        if not (0 <= number <= 36):
                            continue

                        real = REAL_COLOR_MAP.get(number, "VERDE")
                        logger.info(
                            f"[Mega] 🎰 Giro #{len(self.spin_history)+1}: "
                            f"{number} {real} (gameId={game_id})"
                        )
                        self.process_number(number)

            except Exception as e:
                logger.warning(f"[Mega] WS desconectado: {e}. Recon en {reconnect_delay}s")
                try:
                    tg_send(f"⚠️ <b>Mega Roulette</b> — Conexión perdida. "
                            f"Reconectando en {reconnect_delay}s...")
                except Exception:
                    pass
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

# ─── FLASK ────────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def home(): return jsonify({"status":"ok","bot":"Mega Roulette AMX"})

@app.route("/ping")
def ping(): return jsonify({"status":"pong"})

async def self_ping_loop():
    url = os.environ.get("RENDER_EXTERNAL_URL","")
    if not url: return
    while True:
        await asyncio.sleep(300)
        try:
            with urllib.request.urlopen(f"{url}/ping", timeout=10) as r:
                logger.info(f"Self-ping OK: {r.status}")
        except Exception as e:
            logger.warning(f"Self-ping failed: {e}")

# ─── TELEGRAM HANDLERS ────────────────────────────────────────────────────────
engine: Optional[RouletteEngine] = None

@bot.message_handler(commands=['start','help'])
def cmd_start(message):
    seq = " - ".join(str(v) for v in LABOUCHERE_SEQUENCE)
    bot.reply_to(message,
        f"<b>🎰 Mega Roulette Bot AMX</b>\n\n"
        f"<b>Características:</b>\n"
        f"• Markov + ML sin tabla de color predefinida\n"
        f"• AMXSignalSystem con EMA 4/8/20\n"
        f"• CategoryPredictor: 1024 patrones/categoría\n"
        f"• Probabilidad unificada ≥ 60%\n"
        f"• 5 intentos · Labouchère [{seq}]\n"
        f"• Recuperación automática tras pérdidas\n"
        f"• Persistencia SQLite 24/7\n\n"
        f"Comandos:\n"
        f"/moderado - Modo MODERADO\n"
        f"/tendencia - Modo TENDENCIA\n"
        f"/status - Estado actual\n"
        f"/secuencia 1 2 1 - Cambiar secuencia\n"
        f"/reset - Resetear estadísticas\n"
        f"/help - Esta ayuda",
        parse_mode="HTML")

@bot.message_handler(commands=['moderado'])
def cmd_moderado(message):
    if engine: engine.set_mode("moderado")
    bot.reply_to(message,"✅ <b>Modo MODERADO activado</b>",parse_mode="HTML")

@bot.message_handler(commands=['tendencia'])
def cmd_tendencia(message):
    if engine: engine.set_mode("tendencia")
    bot.reply_to(message,"📈 <b>Modo TENDENCIA activado</b>",parse_mode="HTML")

@bot.message_handler(commands=['status'])
def cmd_status(message):
    if engine is None:
        bot.reply_to(message,"⏳ Bot iniciando..."); return
    if engine.signal_active:
        cat=engine.active_category or "?"; val=engine.bet_value or "?"
        icon=CATEGORY_ICONS.get(val,"")
        st=f"🟢 [{cat}] {val}{icon} intento {MAX_ATTEMPTS-engine.attempts_left+1}/{MAX_ATTEMPTS}"
    elif engine.waiting_for_attempt:
        st=f"⏳ Esperando intento {engine.waiting_attempt_number}/{MAX_ATTEMPTS}"
    else:
        st="⚪ Idle"
    warmup="✅" if engine.warmup_done else f"⏳ {engine.ws_spins_count}/{WARMUP_SPINS}"
    w=engine.prob_system.weights
    bot.reply_to(message,
        f"<b>📊 ESTADO — Mega Roulette</b>\n\n"
        f"Modo: {engine.amx_system.mode.upper()}\n"
        f"Estado: {st}\n"
        f"Calentamiento: {warmup}\n"
        f"Giros cargados: {len(engine.spin_history)}\n"
        f"Bankroll: {engine.bet_sys.bankroll:.2f} usd\n"
        f"Secuencia: [{engine.bet_sys.sequence_display()}]\n"
        f"Pesos M:{w['markov']:.2f} ML:{w['ml']:.2f}",
        parse_mode="HTML")

@bot.message_handler(commands=['secuencia'])
def cmd_secuencia(message):
    global LABOUCHERE_SEQUENCE
    parts = message.text.strip().split()[1:]
    if not parts:
        seq_str=" - ".join(str(v) for v in LABOUCHERE_SEQUENCE)
        bot.reply_to(message,f"🎲 Secuencia actual: <code>{seq_str}</code>\nUso: /secuencia 1 2 1",
                     parse_mode="HTML"); return
    try:
        new_seq=[int(x) for x in parts if int(x)>0]
        if not new_seq: raise ValueError
    except ValueError:
        bot.reply_to(message,"⚠️ Formato inválido. Ejemplo: <code>/secuencia 1 2 3 2 1</code>",
                     parse_mode="HTML"); return
    LABOUCHERE_SEQUENCE=new_seq
    if engine: engine.bet_sys.set_sequence(new_seq)
    seq_str=" - ".join(str(v) for v in new_seq)
    bot.reply_to(message,
        f"✅ <b>Secuencia actualizada</b>\n"
        f"🎲 Nueva: <code>{seq_str}</code>\n"
        f"📍 Apuesta inicial: {(new_seq[0]+new_seq[-1])*BASE_BET:.2f} usd",
        parse_mode="HTML")

@bot.message_handler(commands=['reset'])
def cmd_reset(message):
    if engine: engine.stats=DetailedStats()
    bot.reply_to(message,"🔄 <b>Estadísticas reseteadas</b>",parse_mode="HTML")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def run_flask():
    port=int(os.environ.get("PORT",10000))
    app.run(host="0.0.0.0",port=port,debug=False,use_reloader=False)

async def main():
    global engine
    engine=RouletteEngine()
    tasks=[asyncio.create_task(engine.run_ws()),
           asyncio.create_task(self_ping_loop())]
    def _poll():
        logger.info("Iniciando polling Telegram — Mega Roulette")
        bot.polling(none_stop=True,interval=1,timeout=30)
    threading.Thread(target=_poll,daemon=True).start()
    logger.info("🎰 Mega Roulette Bot AMX iniciado")
    await asyncio.gather(*tasks)

if __name__=="__main__":
    threading.Thread(target=run_flask,daemon=True).start()
    logger.info("Flask server started.")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot detenido.")
