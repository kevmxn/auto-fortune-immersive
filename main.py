#!/usr/bin/env python3
"""
Roulette Docena Signal Bot - Sistema AMX V20 (Tendencia + Moderado)
Estrategia de apuesta: Secuencia fija 2,6,18,54,162,486
Restricciones endurecidas tras pérdida.
"""

import asyncio
import io
import json
import logging
import os
import threading
import time
import urllib.request
from collections import deque
from typing import Optional, List, Literal

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import numpy as np
import requests
import telebot
import websockets
from flask import Flask, jsonify
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("DocenaBotAMX")

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
TOKEN = "8608757433:AAE9dGWN7wvFQQbQN_HocdQ5p8UmhcgzWIA"

_session = requests.Session()
_retry = Retry(
    total=5, backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
    raise_on_status=False,
)
_adapter = HTTPAdapter(max_retries=_retry, pool_connections=10, pool_maxsize=20)
_session.mount("https://", _adapter)
_session.mount("http://",  _adapter)

bot = telebot.TeleBot(TOKEN, threaded=False)
bot.session = _session

# ─── DOZEN MAPS ───────────────────────────────────────────────────────────────
REAL_DOZENS = {i: (0 if i == 0 else 1 if i <= 12 else 2 if i <= 24 else 3) for i in range(37)}

def dozen_change(num: int, last_dozen: Optional[int], last_d2_num: Optional[int]) -> int:
    d = REAL_DOZENS[num]
    if d == 1:   return 1
    if d == 3:   return -1
    if d == 2:   return 1 if num <= 18 else -1
    if d == 0:
        if last_dozen == 1: return 1
        if last_dozen == 3: return -1
        if last_dozen == 2: return 1 if (last_d2_num is not None and last_d2_num <= 18) else -1
    return 0

# ─── DOZEN DATA COMPLETAS ─────────────────────────────────────────────────────
DOZEN_DATA_AUTO = [{"id":0,"docena1":32,"docena2":44,"docena3":24,"probability":76,"senal":"DOCENA 1 y DOCENA 2"},{"id":1,"docena1":36,"docena2":40,"docena3":20,"probability":76,"senal":"DOCENA 1 y DOCENA 2"},{"id":2,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":3,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":4,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":5,"docena1":36,"docena2":32,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":6,"docena1":28,"docena2":32,"docena3":40,"probability":72,"senal":"DOCENA 2 y DOCENA 3"},{"id":7,"docena1":40,"docena2":20,"docena3":36,"probability":76,"senal":"DOCENA 1 y DOCENA 3"},{"id":8,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":9,"docena1":44,"docena2":24,"docena3":28,"probability":76,"senal":"DOCENA 1 y DOCENA 3"},{"id":10,"docena1":24,"docena2":36,"docena3":36,"probability":72,"senal":"DOCENA 2 y DOCENA 3"},{"id":11,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":12,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":13,"docena1":36,"docena2":28,"docena3":36,"probability":72,"senal":"DOCENA 1 y DOCENA 3"},{"id":14,"docena1":36,"docena2":40,"docena3":20,"probability":76,"senal":"DOCENA 1 y DOCENA 2"},{"id":15,"docena1":44,"docena2":32,"docena3":24,"probability":76,"senal":"DOCENA 1 y DOCENA 2"},{"id":16,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":17,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":18,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":19,"docena1":36,"docena2":28,"docena3":36,"probability":72,"senal":"DOCENA 1 y DOCENA 3"},{"id":20,"docena1":32,"docena2":32,"docena3":40,"probability":72,"senal":"DOCENA 1 y DOCENA 3"},{"id":21,"docena1":28,"docena2":32,"docena3":40,"probability":72,"senal":"DOCENA 2 y DOCENA 3"},{"id":22,"docena1":28,"docena2":36,"docena3":28,"probability":72,"senal":"DOCENA 1 y DOCENA 2"},{"id":23,"docena1":24,"docena2":36,"docena3":40,"probability":76,"senal":"DOCENA 2 y DOCENA 3"},{"id":24,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":25,"docena1":24,"docena2":32,"docena3":40,"probability":72,"senal":"DOCENA 2 y DOCENA 3"},{"id":26,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":27,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":28,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":29,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":30,"docena1":28,"docena2":32,"docena3":40,"probability":72,"senal":"DOCENA 2 y DOCENA 3"},{"id":31,"docena1":40,"docena2":24,"docena3":32,"probability":72,"senal":"DOCENA 1 y DOCENA 3"},{"id":32,"docena1":24,"docena2":32,"docena3":40,"probability":72,"senal":"DOCENA 2 y DOCENA 3"},{"id":33,"docena1":28,"docena2":36,"docena3":36,"probability":72,"senal":"DOCENA 2 y DOCENA 3"},{"id":34,"docena1":32,"docena2":24,"docena3":36,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":35,"docena1":32,"docena2":40,"docena3":24,"probability":72,"senal":"DOCENA 1 y DOCENA 2"},{"id":36,"docena1":36,"docena2":36,"docena3":24,"probability":72,"senal":"DOCENA 1 y DOCENA 2"}]

DOZEN_DATA_RUSSIAN = [{"id":0,"docena1":32,"docena2":32,"docena3":32,"probability":32,"senal":"NO APOSTAR"},{"id":1,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":2,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":3,"docena1":24,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":4,"docena1":32,"docena2":40,"docena3":24,"probability":72,"senal":"DOCENA 1 y DOCENA 2"},{"id":5,"docena1":28,"docena2":40,"docena3":28,"probability":72,"senal":"DOCENA 1 y DOCENA 2"},{"id":6,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":7,"docena1":40,"docena2":28,"docena3":28,"probability":72,"senal":"DOCENA 1 y DOCENA 3"},{"id":8,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":9,"docena1":28,"docena2":40,"docena3":28,"probability":72,"senal":"DOCENA 1 y DOCENA 2"},{"id":10,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":11,"docena1":32,"docena2":28,"docena3":36,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":12,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":13,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":14,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":15,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":16,"docena1":32,"docena2":32,"docena3":32,"probability":32,"senal":"NO APOSTAR"},{"id":17,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":18,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":19,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":20,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":21,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":22,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":23,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":24,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":25,"docena1":32,"docena2":32,"docena3":32,"probability":32,"senal":"NO APOSTAR"},{"id":26,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":27,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":28,"docena1":32,"docena2":28,"docena3":36,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":29,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":30,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":31,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":32,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":33,"docena1":32,"docena2":28,"docena3":36,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":34,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":35,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":36,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"}]

DOZEN_DATA_AZURE = [{"id":0,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":1,"docena1":24,"docena2":36,"docena3":40,"probability":76,"senal":"DOCENA 2 y DOCENA 3"},{"id":2,"docena1":36,"docena2":36,"docena3":28,"probability":72,"senal":"DOCENA 1 y DOCENA 2"},{"id":3,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":4,"docena1":36,"docena2":24,"docena3":36,"probability":72,"senal":"DOCENA 1 y DOCENA 3"},{"id":5,"docena1":32,"docena2":40,"docena3":24,"probability":72,"senal":"DOCENA 1 y DOCENA 2"},{"id":6,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":7,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":8,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":9,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":10,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":11,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":12,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":13,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":14,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":15,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":16,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":17,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":18,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":19,"docena1":32,"docena2":28,"docena3":36,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":20,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":21,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":22,"docena1":32,"docena2":28,"docena3":36,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":23,"docena1":24,"docena2":36,"docena3":40,"probability":76,"senal":"DOCENA 2 y DOCENA 3"},{"id":24,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":25,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":26,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":27,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":28,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":29,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":30,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":31,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":32,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":33,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":34,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":35,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":36,"docena1":28,"docena2":36,"docena3":36,"probability":72,"senal":"DOCENA 2 y DOCENA 3"}]

DOZEN_DATA_SPEED1 = [{"id":0,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":1,"docena1":24,"docena2":40,"docena3":32,"probability":72,"senal":"DOCENA 2 y DOCENA 3"},{"id":2,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":3,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":4,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":5,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":6,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":7,"docena1":36,"docena2":24,"docena3":36,"probability":72,"senal":"DOCENA 1 y DOCENA 3"},{"id":8,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":9,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":10,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":11,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":12,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":13,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":14,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":15,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":16,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":17,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":18,"docena1":28,"docena2":40,"docena3":28,"probability":72,"senal":"DOCENA 1 y DOCENA 2"},{"id":19,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":20,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":21,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":22,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":23,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":24,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":25,"docena1":28,"docena2":28,"docena3":40,"probability":72,"senal":"DOCENA 1 y DOCENA 3"},{"id":26,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":27,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":28,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":29,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":30,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":31,"docena1":32,"docena2":28,"docena3":36,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":32,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":33,"docena1":36,"docena2":36,"docena3":24,"probability":72,"senal":"DOCENA 1 y DOCENA 2"},{"id":34,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":35,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":36,"docena1":28,"docena2":32,"docena3":40,"probability":72,"senal":"DOCENA 2 y DOCENA 3"}]

# ─── ROULETTE CONFIGS ─────────────────────────────────────────────────────────
ROULETTE_CONFIGS = {
    "Auto Roulette": {
        "ws_key":    225,
        "chat_id":  -1003835197023,
        "thread_id": 8,
        "dozen_data": DOZEN_DATA_AUTO,
    },
    "Russian Roulette": {
        "ws_key":    221,
        "chat_id":  -1003835197023,
        "thread_id": 11,
        "dozen_data": DOZEN_DATA_RUSSIAN,
    },
    "Azure Roulette 1": {
        "ws_key":    227,
        "chat_id":  -1003835197023,
        "thread_id": 10,
        "dozen_data": DOZEN_DATA_AZURE,
    },
    "Speed Roulette 1": {
        "ws_key":    203,
        "chat_id":  -1003835197023,
        "thread_id": 9,
        "dozen_data": DOZEN_DATA_SPEED1,
    },
}

WS_URL     = "wss://dga.pragmaticplaylive.net/ws"
CASINO_ID  = "ppcjd00000007254"
MAX_ATTEMPTS = 2          # Intentos por señal
BASE_BET   = 0.10         # Unidad base (para el nivel 2 de la secuencia, realmente la secuencia está en múltiplos de esta unidad)
VISIBLE    = 40

# ─── ESTRATEGIA DE APUESTA: SECUENCIA FIJA 2,6,18,54,162,486 ──────────────────
BET_SEQUENCE = [2, 6, 18, 54, 162, 486]  # en unidades de BASE_BET

class FixedSequenceBetting:
    def __init__(self, base: float, sequence: List[int]):
        self.base = base
        self.sequence = sequence
        self.level = 0          # índice en la secuencia (0 = primer nivel)
        self.bankroll = 0.0

    def current_bet_total(self) -> float:
        """Devuelve la apuesta total para los dos docenas combinados."""
        if self.level >= len(self.sequence):
            self.level = 0
        return round(self.sequence[self.level] * self.base, 2)

    def per_dozen_bet(self) -> float:
        return round(self.current_bet_total() / 2, 2)

    def win(self) -> float:
        """Ganancia: se recupera la apuesta total más un beneficio igual a la mitad (pago 2:1 en dos docenas -> neto 1:2 del total)."""
        bet = self.current_bet_total()
        # Beneficio neto = bet * 0.5 (porque se apuesta a dos docenas, cada una paga 2:1)
        self.bankroll = round(self.bankroll + bet * 0.5, 2)
        self.level = 0   # reiniciar secuencia tras ganar
        return bet

    def loss(self) -> float:
        """Pérdida: se resta la apuesta total y se avanza al siguiente nivel."""
        bet = self.current_bet_total()
        self.bankroll = round(self.bankroll - bet, 2)
        self.level += 1
        if self.level >= len(self.sequence):
            self.level = 0   # reiniciar si se supera el máximo (opcional)
        return bet

    def is_recovery_mode(self) -> bool:
        """Indica si estamos en modo recuperación (nivel > 0)."""
        return self.level > 0


# ─── SISTEMA AMX V20 PARA DOCENAS (CON BAJISTAS) ──────────────────────────────
class DozenAMXSignalSystem:
    def __init__(self, mode: Literal["tendencia", "moderado"] = "moderado"):
        self.mode = mode
        self.last_signal_time: float = 0
        self.cooldown_seconds: int = 8
        self.so_cooldown: Optional[float] = None

    @staticmethod
    def calculate_ema(data: list, period: int) -> list:
        if len(data) < period:
            return [None] * len(data)
        mult = 2 / (period + 1)
        ema = [None] * (period - 1)
        prev = sum(data[:period]) / period
        ema.append(prev)
        for i in range(period, len(data)):
            prev = (data[i] * mult) + (prev * (1 - mult))
            ema.append(prev)
        return ema

    # ─── ALCISTAS ─────────────────────────────────────────────────────────
    def check_signal_tendencia(self, level_data: list, dozen_data: list,
                               current_number: int, prob_threshold: float,
                               require_strong: bool = False) -> Optional[dict]:
        if len(level_data) < 20:
            return None

        ahora = time.time()
        if ahora - self.last_signal_time < self.cooldown_seconds:
            return None
        if self.so_cooldown and ahora - self.so_cooldown < 8:
            return None

        ema4  = self.calculate_ema(level_data, 4)
        ema8  = self.calculate_ema(level_data, 8)
        ema20 = self.calculate_ema(level_data, 20)

        if None in (ema4[-1], ema8[-1], ema20[-1], ema4[-2], ema20[-2]):
            return None

        current_pos = level_data[-1]
        cruce_alcista = ema4[-2] <= ema20[-2] and ema4[-1] > ema20[-1]
        sobre_tres_emas = current_pos > ema4[-1] and current_pos > ema8[-1] and current_pos > ema20[-1]

        # Si se requiere señal fuerte (recuperación), exigir ambas condiciones
        if require_strong:
            if not (cruce_alcista and sobre_tres_emas):
                return None
        else:
            if not (cruce_alcista or sobre_tres_emas):
                return None

        entry = next((e for e in dozen_data if e["id"] == current_number), None)
        if not entry or entry["senal"] == "NO APOSTAR":
            return None

        prob = entry["probability"]
        if prob < prob_threshold:
            return None

        dozens = self._parse_dozens(entry["senal"])
        return {
            "type": "SKRILL_2.0",
            "mode": "tendencia",
            "dozens": dozens,
            "probability": prob,
            "trigger_number": current_number,
            "strength": "strong" if cruce_alcista else "moderate",
            "direction": "alcista"
        }

    def check_signal_moderado(self, level_data: list, dozen_data: list,
                              current_number: int, prob_threshold: float,
                              require_strong: bool = False) -> Optional[dict]:
        if len(level_data) < 20:
            return None

        ahora = time.time()
        if ahora - self.last_signal_time < self.cooldown_seconds:
            return None
        if self.so_cooldown and ahora - self.so_cooldown < 8:
            return None

        ema4  = self.calculate_ema(level_data, 4)
        ema8  = self.calculate_ema(level_data, 8)
        ema20 = self.calculate_ema(level_data, 20)

        if None in (ema4[-1], ema8[-1], ema20[-1], ema8[-2], ema20[-2]):
            return None

        cruce_ema8 = ema8[-2] <= ema20[-2] and ema8[-1] > ema20[-1]
        sobre_emas = level_data[-1] > ema4[-1] and level_data[-1] > ema8[-1]

        patron_v = False
        if len(level_data) >= 3:
            a, b, c = level_data[-3], level_data[-2], level_data[-1]
            patron_v = b < a and b < c and abs(a - c) <= 1 and c > a

        if require_strong:
            # En recuperación, requerir cruce + sobre_emas, o patrón V + sobre_emas
            if not ((cruce_ema8 or patron_v) and sobre_emas):
                return None
        else:
            if not ((cruce_ema8 or patron_v) and sobre_emas):
                return None  # el moderado normal ya exige sobre_emas

        entry = next((e for e in dozen_data if e["id"] == current_number), None)
        if not entry or entry["senal"] == "NO APOSTAR":
            return None

        prob = entry["probability"]
        if prob < prob_threshold:
            return None

        dozens = self._parse_dozens(entry["senal"])
        return {
            "type": "ALERTA_2.0",
            "mode": "moderado",
            "dozens": dozens,
            "probability": prob,
            "trigger_number": current_number,
            "pattern": "V" if patron_v else "EMA_CROSS",
            "direction": "alcista"
        }

    # ─── BAJISTAS ─────────────────────────────────────────────────────────
    def check_signal_tendencia_bajista(self, level_data: list, dozen_data: list,
                                       current_number: int, prob_threshold: float,
                                       require_strong: bool = False) -> Optional[dict]:
        if len(level_data) < 20:
            return None

        ahora = time.time()
        if ahora - self.last_signal_time < self.cooldown_seconds:
            return None
        if self.so_cooldown and ahora - self.so_cooldown < 8:
            return None

        ema4  = self.calculate_ema(level_data, 4)
        ema8  = self.calculate_ema(level_data, 8)
        ema20 = self.calculate_ema(level_data, 20)

        if None in (ema4[-1], ema8[-1], ema20[-1], ema4[-2], ema20[-2]):
            return None

        current_pos = level_data[-1]
        cruce_bajista = ema4[-2] >= ema20[-2] and ema4[-1] < ema20[-1]
        bajo_tres_emas = current_pos < ema4[-1] and current_pos < ema8[-1] and current_pos < ema20[-1]

        if require_strong:
            if not (cruce_bajista and bajo_tres_emas):
                return None
        else:
            if not (cruce_bajista or bajo_tres_emas):
                return None

        entry = next((e for e in dozen_data if e["id"] == current_number), None)
        if not entry or entry["senal"] == "NO APOSTAR":
            return None

        prob = entry["probability"]
        if prob < prob_threshold:
            return None

        dozens = self._parse_dozens(entry["senal"])
        return {
            "type": "SKRILL_2.0_SHORT",
            "mode": "tendencia",
            "dozens": dozens,
            "probability": prob,
            "trigger_number": current_number,
            "strength": "strong" if cruce_bajista else "moderate",
            "direction": "bajista"
        }

    def check_signal_moderado_bajista(self, level_data: list, dozen_data: list,
                                      current_number: int, prob_threshold: float,
                                      require_strong: bool = False) -> Optional[dict]:
        if len(level_data) < 20:
            return None

        ahora = time.time()
        if ahora - self.last_signal_time < self.cooldown_seconds:
            return None
        if self.so_cooldown and ahora - self.so_cooldown < 8:
            return None

        ema4  = self.calculate_ema(level_data, 4)
        ema8  = self.calculate_ema(level_data, 8)
        ema20 = self.calculate_ema(level_data, 20)

        if None in (ema4[-1], ema8[-1], ema20[-1], ema8[-2], ema20[-2]):
            return None

        cruce_ema8_bajista = ema8[-2] >= ema20[-2] and ema8[-1] < ema20[-1]
        bajo_emas = level_data[-1] < ema4[-1] and level_data[-1] < ema8[-1]

        patron_v_inv = False
        if len(level_data) >= 3:
            a, b, c = level_data[-3], level_data[-2], level_data[-1]
            patron_v_inv = b > a and b > c and abs(a - c) <= 1 and c < a

        if require_strong:
            if not ((cruce_ema8_bajista or patron_v_inv) and bajo_emas):
                return None
        else:
            if not ((cruce_ema8_bajista or patron_v_inv) and bajo_emas):
                return None

        entry = next((e for e in dozen_data if e["id"] == current_number), None)
        if not entry or entry["senal"] == "NO APOSTAR":
            return None

        prob = entry["probability"]
        if prob < prob_threshold:
            return None

        dozens = self._parse_dozens(entry["senal"])
        return {
            "type": "ALERTA_2.0_SHORT",
            "mode": "moderado",
            "dozens": dozens,
            "probability": prob,
            "trigger_number": current_number,
            "pattern": "V_INV" if patron_v_inv else "EMA_CROSS",
            "direction": "bajista"
        }

    def _parse_dozens(self, senal: str) -> List[int]:
        dozens = []
        if "DOCENA 1" in senal: dozens.append(1)
        if "DOCENA 2" in senal: dozens.append(2)
        if "DOCENA 3" in senal: dozens.append(3)
        return dozens

    def register_signal_sent(self):
        self.last_signal_time = time.time()

    def register_so_failed(self):
        self.so_cooldown = time.time()


# ─── STATISTICS ───────────────────────────────────────────────────────────────
class Stats:
    def __init__(self):
        self.total = 0
        self.wins = 0
        self.losses = 0
        self.last_stats_at = 0
        self._h24: deque = deque()
        self.batch_start_bankroll = None
        self._wins_at_last_batch = 0

    def record(self, is_win: bool, bankroll: float):
        self.total += 1
        if is_win:
            self.wins += 1
        else:
            self.losses += 1
        now = time.time()
        self._h24.append((now, is_win, bankroll))
        self._trim24()

    def _trim24(self):
        cutoff = time.time() - 86400
        while self._h24 and self._h24[0][0] < cutoff:
            self._h24.popleft()

    def should_send_stats(self) -> bool:
        return (self.total - self.last_stats_at) >= 20

    def mark_stats_sent(self, bankroll: float):
        self.last_stats_at = self.total
        self.batch_start_bankroll = bankroll
        self._wins_at_last_batch = self.wins

    def batch_stats(self, current_bankroll: float):
        n = self.total - self.last_stats_at
        w = self.wins - self._wins_at_last_batch
        l = n - w
        e = round(w / n * 100, 1) if n else 0.0
        if self.batch_start_bankroll is not None:
            batch_bankroll = round(current_bankroll - self.batch_start_bankroll, 2)
        else:
            batch_bankroll = 0.0
        return w, l, n, e, batch_bankroll

    def stats_24h(self, current_bankroll: float):
        self._trim24()
        t = len(self._h24)
        w = sum(1 for _, iw, _ in self._h24 if iw)
        l = t - w
        e = round(w / t * 100, 1) if t else 0.0
        if t >= 2:
            first_bankroll = self._h24[0][2]
            last_bankroll  = self._h24[-1][2]
            bk24 = round(last_bankroll - first_bankroll, 2)
        else:
            bk24 = 0.0
        return w, l, t, e, bk24


# ─── CHART GENERATION ─────────────────────────────────────────────────────────
D_COLORS = {1: "#5bc8fa", 2: "#f0c040", 3: "#c0392b", 0: "#3fe06d"}
D_LABELS = {1: "D1 (1-12)", 2: "D2 (13-24)", 3: "D3 (25-36)", 0: "0"}

def generate_chart(level_data: list, spin_history: list,
                   signal_dozens: List[int], visible: int = VISIBLE) -> io.BytesIO:
    min_len = min(len(level_data), len(spin_history))
    level_data = level_data[-min_len:]
    spin_history = spin_history[-min_len:]

    arr  = np.array(level_data, dtype=float)
    n    = len(arr)

    def calc_ema(data, period):
        if len(data) < period: return np.full(len(data), np.nan)
        out  = np.full(len(data), np.nan)
        mult = 2 / (period + 1)
        out[period - 1] = np.mean(data[:period])
        for i in range(period, len(data)):
            out[i] = (data[i] - out[i - 1]) * mult + out[i - 1]
        return out

    e4  = calc_ema(arr, 4)
    e8  = calc_ema(arr, 8)
    e20 = calc_ema(arr, 20)

    start   = max(0, n - visible)
    sl      = slice(start, n)
    x       = np.arange(len(arr[sl]))
    hist_sl = spin_history[start:]

    if set(signal_dozens) == {1, 2}: sig_c = "#5bc8fa"
    elif set(signal_dozens) == {2, 3}: sig_c = "#c0392b"
    else: sig_c = "#f39c12"

    bg = "#0b101f"; ax_bg = "#0f1a2a"; grid_c = "#1e2e48"

    fig, ax = plt.subplots(figsize=(8, 3.6), facecolor=bg)
    ax.set_facecolor(ax_bg)

    y   = arr[sl]
    ax.fill_between(x, y, alpha=0.09, color=sig_c)
    ax.plot(x, y,       color=sig_c,   linewidth=0.8, zorder=3)
    ax.plot(x, e4[sl],  color="#ffd700", linewidth=0.7, linestyle="--", label="EMA 4",  zorder=4)
    ax.plot(x, e8[sl],  color="#ff922b", linewidth=0.7, linestyle="--", label="EMA 8",  zorder=4)
    ax.plot(x, e20[sl], color="#ff4d4d", linewidth=1.0, label="EMA 20", zorder=4)

    for i, spin in enumerate(hist_sl):
        c = D_COLORS.get(spin["real_dozen"], "#ffffff")
        ax.scatter(i, y[i], color=c, s=22, zorder=5, edgecolors="white", linewidths=0.3)

    tick_step = max(1, len(x) // 8)
    tick_x    = list(range(0, len(x), tick_step))
    tick_lbs  = [str(hist_sl[i]["number"]) if i < len(hist_sl) else "" for i in tick_x]
    ax.set_xticks(tick_x); ax.set_xticklabels(tick_lbs, color="#8899bb", fontsize=7)
    ax.tick_params(axis="y", colors="#8899bb", labelsize=7)
    ax.tick_params(axis="x", colors="#8899bb", labelsize=7)
    for sp in ["top", "right"]: ax.spines[sp].set_visible(False)
    ax.spines["bottom"].set_color(grid_c); ax.spines["left"].set_color(grid_c)
    ax.grid(axis="y", color=grid_c, linewidth=0.4, alpha=0.5)

    dozen_str = " + ".join(f"D{d}" for d in sorted(signal_dozens))
    ax.set_title(f"🎯 Señal {dozen_str} — últimos {visible} giros · EMA 4/8/20",
                 color=sig_c, fontsize=9, pad=6)

    legend_els = [
        Line2D([0],[0], color=sig_c,    linewidth=0.8, label="Nivel"),
        Line2D([0],[0], color="#ffd700", linewidth=0.7, linestyle="--", label="EMA 4"),
        Line2D([0],[0], color="#ff922b", linewidth=0.7, linestyle="--", label="EMA 8"),
        Line2D([0],[0], color="#ff4d4d", linewidth=1.0,  label="EMA 20"),
        *[Line2D([0],[0], marker="o", color="w", markerfacecolor=D_COLORS[d],
                 markersize=5, label=D_LABELS[d]) for d in [0, 1, 2, 3]],
    ]
    ax.legend(handles=legend_els, loc="upper left", fontsize=6.5,
              facecolor="#0b101f", edgecolor=grid_c, labelcolor="white",
              framealpha=0.8, ncol=2)

    plt.tight_layout(pad=0.8)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, facecolor=bg)
    plt.close(fig)
    buf.seek(0)
    return buf


# ─── TELEGRAM HELPERS ─────────────────────────────────────────────────────────
_TG_MAX_RETRIES = 5

def _tg_call(fn, *args, **kwargs):
    delay = 2.0
    for attempt in range(1, _TG_MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            err = str(e)
            if "retry after" in err.lower():
                try:    wait = int("".join(filter(str.isdigit, err))) + 1
                except: wait = 30
                logger.warning(f"Flood-wait {wait}s")
                time.sleep(wait); continue
            logger.warning(f"TG error attempt {attempt}: {e}")
            if attempt < _TG_MAX_RETRIES:
                time.sleep(delay); delay = min(delay * 2, 60)
            else:
                logger.error(f"TG failed after {_TG_MAX_RETRIES} attempts: {e}")
                return None

def tg_send_photo(chat_id, thread_id, buf, caption) -> Optional[int]:
    buf.seek(0)
    msg = _tg_call(bot.send_photo, chat_id=chat_id, photo=buf, caption=caption,
                   parse_mode="HTML", message_thread_id=thread_id)
    return msg.message_id if msg else None

def tg_send_text(chat_id, thread_id, text) -> Optional[int]:
    msg = _tg_call(bot.send_message, chat_id=chat_id, text=text,
                   parse_mode="HTML", message_thread_id=thread_id)
    return msg.message_id if msg else None

def tg_delete(chat_id, msg_id):
    _tg_call(bot.delete_message, chat_id=chat_id, message_id=msg_id)


# ─── ROULETTE ENGINE (CON NUEVA ESTRATEGIA Y RESTRICCIONES) ───────────────────
class DozenEngine:
    def __init__(self, name: str, cfg: dict):
        self.name       = name
        self.ws_key     = cfg["ws_key"]
        self.chat_id    = cfg["chat_id"]
        self.thread_id  = cfg["thread_id"]
        self.dozen_data = cfg["dozen_data"]

        self.level_data:   list = []
        self.spin_history: list = []
        self.last_dozen:   Optional[int] = None
        self.last_d2_num:  Optional[int] = None
        self.anti_block:   set  = set()

        self.signal_active:  bool       = False
        self.signal_dozens:  List[int]  = []
        self.signal_prob:    int        = 0
        self.trigger_number: Optional[int] = None
        self.attempts_left:  int        = 0
        self.total_attempts: int        = 0

        self.result_until:  float = 0.0

        # Sistema de apuestas con secuencia fija
        self.bet_sys = FixedSequenceBetting(BASE_BET, BET_SEQUENCE)

        self.stats = Stats()
        self.signal_msg_id: Optional[int] = None
        self.ws     = None
        self.running = True

        self.amx_system = DozenAMXSignalSystem(mode="moderado")
        self.base_prob_threshold = 68
        self._last_amx_signal = None

    def set_mode(self, mode: Literal["tendencia", "moderado"]):
        self.amx_system = DozenAMXSignalSystem(mode=mode)
        logger.info(f"[{self.name}] Modo AMX V20 cambiado a: {mode}")
        return mode

    def _current_prob_threshold(self) -> int:
        """Aumenta el umbral de probabilidad si estamos en recuperación."""
        if self.bet_sys.is_recovery_mode():
            return self.base_prob_threshold + 4  # 72% en lugar de 68%
        return self.base_prob_threshold

    def process_number(self, number: int):
        real_dozen = REAL_DOZENS[number]
        chg = dozen_change(number, self.last_dozen, self.last_d2_num)
        level = (self.level_data[-1] if self.level_data else 0) + chg
        self.level_data.append(level)
        if len(self.level_data) > 300:
            self.level_data.pop(0)

        self.spin_history.append({"number": number, "real_dozen": real_dozen})
        if len(self.spin_history) > 300:
            self.spin_history.pop(0)

        if real_dozen != 0:
            self.last_dozen = real_dozen
            if real_dozen == 2:
                self.last_d2_num = number

        # Resolver señal activa
        if self.signal_active and time.time() > self.result_until:
            hit = real_dozen in self.signal_dozens
            if hit:
                bet = self.bet_sys.win()
                self.stats.record(True, self.bet_sys.bankroll)
                self.signal_active  = False
                self._send_result(number, real_dozen, True, bet)
                self._check_stats()
            else:
                self.attempts_left -= 1
                bet = self.bet_sys.loss()   # Aquí se descuenta y avanza nivel
                if self.attempts_left <= 0:
                    self.stats.record(False, self.bet_sys.bankroll)
                    self.signal_active = False
                    self._send_result(number, real_dozen, False, bet)
                    self._check_stats()
                else:
                    new_bet_total = self.bet_sys.current_bet_total()
                    self._send_retry_signal(number, new_bet_total)

        # Activar nueva señal
        if not self.signal_active and time.time() > self.result_until:
            if len(self.level_data) < 20:
                return
            signal = self._detect_amx_signal()
            if signal:
                self._last_amx_signal = signal
                self.signal_active   = True
                self.signal_dozens   = signal["dozens"]
                self.signal_prob     = signal["probability"]
                self.trigger_number  = signal["trigger_number"]
                self.attempts_left   = MAX_ATTEMPTS
                self.total_attempts  = MAX_ATTEMPTS
                self._send_signal(signal["trigger_number"], 1, amx_signal=signal)

    def _detect_amx_signal(self) -> Optional[dict]:
        if len(self.level_data) < 20:
            return None
        current_number = self.spin_history[-1]["number"]
        ema20 = self.amx_system.calculate_ema(self.level_data, 20)
        if len(ema20) < 1 or ema20[-1] is None:
            return None
        current_pos = self.level_data[-1]
        usar_bajista = current_pos < ema20[-1]

        prob_threshold = self._current_prob_threshold()
        require_strong = self.bet_sys.is_recovery_mode()  # más restrictivo si estamos en nivel >0

        signal = None
        if self.amx_system.mode == "tendencia":
            if usar_bajista:
                signal = self.amx_system.check_signal_tendencia_bajista(
                    self.level_data, self.dozen_data, current_number,
                    prob_threshold, require_strong
                )
            if not signal:
                signal = self.amx_system.check_signal_tendencia(
                    self.level_data, self.dozen_data, current_number,
                    prob_threshold, require_strong
                )
        else:
            if usar_bajista:
                signal = self.amx_system.check_signal_moderado_bajista(
                    self.level_data, self.dozen_data, current_number,
                    prob_threshold, require_strong
                )
            if not signal:
                signal = self.amx_system.check_signal_moderado(
                    self.level_data, self.dozen_data, current_number,
                    prob_threshold, require_strong
                )
        return signal

    def _dozen_str(self, dozens: List[int]) -> str:
        parts = []
        for d in sorted(dozens):
            if d == 1: parts.append("D1 (1-12)")
            elif d == 2: parts.append("D2 (13-24)")
            elif d == 3: parts.append("D3 (25-36)")
        return " + ".join(parts)

    def _dozen_emoji(self, dozens: List[int]) -> str:
        emojis = []
        for d in sorted(dozens):
            if d == 1: emojis.append("🔵")
            elif d == 2: emojis.append("🟡")
            elif d == 3: emojis.append("🔴")
        return " + ".join(emojis)

    def _caption(self, trigger, attempt, bet_per, bet_total, prob, amx_signal: Optional[dict] = None) -> str:
        docenas_text = self._dozen_str(self.signal_dozens)
        emoji_text   = self._dozen_emoji(self.signal_dozens)

        tipo_senal = ""
        if amx_signal:
            dir_text = "📉 Bajista" if amx_signal.get("direction") == "bajista" else "📈 Alcista"
            mode_name = "Tendencia" if amx_signal["mode"] == "tendencia" else "Moderado"
            tipo_senal = f"{dir_text} · {mode_name}"

        recovery_note = ""
        if self.bet_sys.is_recovery_mode():
            recovery_note = f"\n⚠️ <i>Modo recuperación (nivel {self.bet_sys.level+1}/6)</i>"

        return (
            f"✅☑️ <b>SEÑAL CONFIRMADA</b> ☑️✅\n\n"
            f"🎰 <b>Juego: {self.name}</b>\n"
            f"👉 <b>Después de: {trigger}</b>\n"
            f"🎯 <b>Apostar a: {docenas_text} ({emoji_text})</b>\n\n"
            f"💡 <i>Probabilidad de señal: {prob}%</i>\n"
            f"🌀 <i>Tipo de señal: {tipo_senal}</i>\n"
            f"📍 <i>Apuesta: {bet_per:.2f} usd | Total: {bet_total:.2f} usd</i>\n\n"
            f"♻️ <i>Intento {attempt}/{MAX_ATTEMPTS}</i>"
        )

    def _send_signal(self, trigger: int, attempt: int, amx_signal: Optional[dict] = None):
        bet_total = self.bet_sys.current_bet_total()
        bet_per   = self.bet_sys.per_dozen_bet()
        caption   = self._caption(trigger, attempt, bet_per, bet_total, self.signal_prob, amx_signal)
        chart  = generate_chart(self.level_data[:], self.spin_history[:], self.signal_dozens)
        msg_id = tg_send_photo(self.chat_id, self.thread_id, chart, caption)
        self.signal_msg_id = msg_id
        logger.info(f"[{self.name}] Signal → {self.signal_dozens} after {trigger}, bet={bet_total:.2f}, level={self.bet_sys.level+1}")

    def _send_retry_signal(self, trigger: int, new_bet_total: float):
        if self.signal_msg_id:
            tg_delete(self.chat_id, self.signal_msg_id)
            self.signal_msg_id = None
        bet_per = round(new_bet_total / 2, 2)
        caption = self._caption(trigger, 2, bet_per, new_bet_total, self.signal_prob, self._last_amx_signal)
        chart  = generate_chart(self.level_data[:], self.spin_history[:], self.signal_dozens)
        msg_id = tg_send_photo(self.chat_id, self.thread_id, chart, caption)
        self.signal_msg_id = msg_id
        logger.info(f"[{self.name}] Retry signal → {self.signal_dozens} after {trigger}, bet={new_bet_total:.2f}")

    def _send_result(self, number: int, real_dozen: int, won: bool, bet: float):
        bankroll = self.bet_sys.bankroll
        d_icon = {1: "🔵", 2: "🟡", 3: "🔴", 0: "🟢"}.get(real_dozen, "⬜")
        d_name = {1: "Docena 1", 2: "Docena 2", 3: "Docena 3", 0: "Cero"}.get(real_dozen, "")

        if won:
            text = (
                f"💎 <b>RESULTADO: {number} - {d_name} ({d_icon})</b>\n"
                f"💰 <i>Bankroll Actual: {bankroll:.2f} usd</i>"
            )
        else:
            text = (
                f"❌ <b>RESULTADO: {number} - {d_name} ({d_icon})</b>\n"
                f"💰 <i>Bankroll Actual: {bankroll:.2f} usd</i>"
            )
        self.result_until = time.time() + 7.0
        tg_send_text(self.chat_id, self.thread_id, text)
        logger.info(f"[{self.name}] Result {'WIN' if won else 'LOSS'} #{number} D{real_dozen}, bk={bankroll:.2f}")

    def _check_stats(self):
        if not self.stats.should_send_stats():
            return
        bk = self.bet_sys.bankroll
        w20, l20, t20, e20, batch_bk = self.stats.batch_stats(bk)
        self.stats.mark_stats_sent(bk)
        w24, l24, t24, e24, bk24 = self.stats.stats_24h(bk)
        text = (
            f"👉🏼 <b>ESTADÍSTICAS {t20} SEÑALES</b>\n"
            f"🈯️ <b>W: {w20}</b> 🈲 <b>L: {l20}</b> 🈺 <b>T: {t20}</b> 📈 <b>E: {e20}%</b>\n"
            f"💰 <i>Bankroll acumulado: {batch_bk:.2f} usd</i>\n\n"
            f"👉🏼 <b>ESTADÍSTICAS 24 HORAS</b>\n"
            f"🈯️ <b>W: {w24}</b> 🈲 <b>L: {l24}</b> 🈺 <b>T: {t24}</b> 📈 <b>E: {e24}%</b>\n"
            f"💰 <i>Bankroll acumulado: {bk24:.2f} usd</i>"
        )
        tg_send_text(self.chat_id, self.thread_id, text)
        logger.info(f"[{self.name}] Stats sent")

    async def run_ws(self):
        delay = 5
        while self.running:
            try:
                async with websockets.connect(
                    WS_URL, ping_interval=30, ping_timeout=60, close_timeout=10
                ) as ws:
                    self.ws = ws; delay = 5
                    logger.info(f"[{self.name}] WS connected")
                    await ws.send(json.dumps({
                        "type": "subscribe", "casinoId": CASINO_ID,
                        "currency": "USD", "key": [self.ws_key]
                    }))
                    async for msg in ws:
                        if not self.running: break
                        try: data = json.loads(msg)
                        except: continue

                        if "last20Results" in data and isinstance(data["last20Results"], list):
                            tmp = []
                            for r in data["last20Results"]:
                                gid = r.get("gameId"); num = r.get("result")
                                if gid and num is not None:
                                    try: n = int(num)
                                    except: continue
                                    if 0 <= n <= 36 and gid not in self.anti_block:
                                        tmp.append((gid, n))
                                        if len(self.anti_block) > 1000: self.anti_block.clear()
                                        self.anti_block.add(gid)
                            for _, n in reversed(tmp):
                                self.process_number(n)

                        gid = data.get("gameId"); res = data.get("result")
                        if gid and res is not None:
                            try: n = int(res)
                            except: continue
                            if 0 <= n <= 36 and gid not in self.anti_block:
                                if len(self.anti_block) > 1000: self.anti_block.clear()
                                self.anti_block.add(gid)
                                self.process_number(n)

            except Exception as e:
                logger.warning(f"[{self.name}] WS error: {e}. Retry in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)


# ─── FLASK KEEPALIVE ──────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def index():
    return jsonify({"status": "ok", "bot": "Docena Signal Bot AMX V20", "ts": time.time()})

@app.route("/ping")
def ping():
    return jsonify({"pong": True, "ts": time.time()})

@app.route("/health")
def health():
    return jsonify({"healthy": True})


async def self_ping_loop():
    port = int(os.environ.get("PORT", 10001))
    url  = os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{port}")
    while True:
        await asyncio.sleep(300)
        try:
            with urllib.request.urlopen(f"{url}/ping", timeout=10) as r:
                logger.info(f"Self-ping OK: {r.status}")
        except Exception as e:
            logger.warning(f"Self-ping failed: {e}")


# ─── COMANDOS TELEGRAM ───────────────────────────────────────────────────────
engines: dict[str, DozenEngine] = {}

@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    help_text = """
<b>🎰 Docena Bot - Sistema AMX V20</b>

Comandos:
/moderado - Modo MODERADO
/tendencia - Modo TENDENCIA
/status - Estado de ruletas
/reset - Resetear estadísticas
/help - Ayuda

<b>Estrategia:</b> Secuencia 2,6,18,54,162,486.
Tras pérdida, condiciones más estrictas.
    """
    bot.reply_to(message, help_text, parse_mode="HTML")


@bot.message_handler(commands=['moderado'])
def cmd_moderado(message):
    changed = []
    for name, engine in engines.items():
        old_mode = engine.amx_system.mode
        engine.set_mode("moderado")
        if old_mode != "moderado":
            changed.append(name)

    if changed:
        text = f"✅ <b>Modo MODERADO activado</b>\n\nRuletas: {', '.join(changed)}"
    else:
        text = "📊 <b>Todas las ruletas en modo MODERADO</b>"
    bot.reply_to(message, text, parse_mode="HTML")


@bot.message_handler(commands=['tendencia'])
def cmd_tendencia(message):
    changed = []
    for name, engine in engines.items():
        old_mode = engine.amx_system.mode
        engine.set_mode("tendencia")
        if old_mode != "tendencia":
            changed.append(name)

    if changed:
        text = f"📈 <b>Modo TENDENCIA activado</b>\n\nRuletas: {', '.join(changed)}"
    else:
        text = "📈 <b>Todas las ruletas en modo TENDENCIA</b>"
    bot.reply_to(message, text, parse_mode="HTML")


@bot.message_handler(commands=['status'])
def cmd_status(message):
    lines = ["<b>📊 ESTADO</b>\n"]
    for name, engine in engines.items():
        mode_icon = "📈" if engine.amx_system.mode == "tendencia" else "📊"
        signal_status = "🟢" if engine.signal_active else "⚪"
        level_info = f" (nivel {engine.bet_sys.level+1})" if engine.bet_sys.is_recovery_mode() else ""
        lines.append(f"<b>{name}</b>: {mode_icon} {engine.amx_system.mode} {signal_status}{level_info}")
    bot.reply_to(message, "\n".join(lines), parse_mode="HTML")


@bot.message_handler(commands=['reset'])
def cmd_reset(message):
    for engine in engines.values():
        engine.stats = Stats()
        engine.bet_sys = FixedSequenceBetting(BASE_BET, BET_SEQUENCE)
    bot.reply_to(message, "🔄 <b>Estadísticas y nivel de apuesta reseteados</b>", parse_mode="HTML")


def run_flask():
    port = int(os.environ.get("PORT", 10001))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


async def main():
    global engines
    engines = {name: DozenEngine(name, cfg) for name, cfg in ROULETTE_CONFIGS.items()}

    tasks = [asyncio.create_task(e.run_ws()) for e in engines.values()]
    tasks.append(asyncio.create_task(self_ping_loop()))

    def telegram_polling():
        logger.info("Iniciando polling de Telegram...")
        bot.polling(none_stop=True, interval=1, timeout=30)

    tg_thread = threading.Thread(target=telegram_polling, daemon=True)
    tg_thread.start()

    logger.info("🎰 Docena Bot AMX V20 iniciado (Secuencia 2,6,18,54,162,486)")
    logger.info("Comandos: /moderado, /tendencia, /status, /reset, /help")

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask server started.")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
