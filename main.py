#!/usr/bin/env python3
"""
Roulette Docena Signal Bot
Conecta vía WebSocket a Pragmatic Play, detecta señales de docenas
con EMA 4/8/20 (modo Tendencia y Moderado) y envía alertas a Telegram.
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
from typing import Optional, List

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
logger = logging.getLogger("DocenaBot")

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

# Level change per dozen result (D1=+1, D3=-1, D2 depends on number, 0=inherit)
def dozen_change(num: int, last_dozen: Optional[int], last_d2_num: Optional[int]) -> int:
    d = REAL_DOZENS[num]
    if d == 1:   return 1
    if d == 3:   return -1
    if d == 2:   return 1 if num <= 18 else -1
    if d == 0:   # zero — inherit last direction
        if last_dozen == 1: return 1
        if last_dozen == 3: return -1
        if last_dozen == 2: return 1 if (last_d2_num is not None and last_d2_num <= 18) else -1
    return 0

# ─── DOZEN DATA (per roulette) ─────────────────────────────────────────────────
DOZEN_DATA_AUTO = [{"id":0,"docena1":32,"docena2":44,"docena3":24,"probability":76,"senal":"DOCENA 1 y DOCENA 2"},{"id":1,"docena1":36,"docena2":40,"docena3":20,"probability":76,"senal":"DOCENA 1 y DOCENA 2"},{"id":2,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":3,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":4,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":5,"docena1":36,"docena2":32,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":6,"docena1":28,"docena2":32,"docena3":40,"probability":72,"senal":"DOCENA 2 y DOCENA 3"},{"id":7,"docena1":40,"docena2":20,"docena3":36,"probability":76,"senal":"DOCENA 1 y DOCENA 3"},{"id":8,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":9,"docena1":44,"docena2":24,"docena3":28,"probability":76,"senal":"DOCENA 1 y DOCENA 3"},{"id":10,"docena1":24,"docena2":36,"docena3":36,"probability":72,"senal":"DOCENA 2 y DOCENA 3"},{"id":11,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":12,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":13,"docena1":36,"docena2":28,"docena3":36,"probability":72,"senal":"DOCENA 1 y DOCENA 3"},{"id":14,"docena1":36,"docena2":40,"docena3":20,"probability":76,"senal":"DOCENA 1 y DOCENA 2"},{"id":15,"docena1":44,"docena2":32,"docena3":24,"probability":76,"senal":"DOCENA 1 y DOCENA 2"},{"id":16,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":17,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":18,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":19,"docena1":36,"docena2":28,"docena3":36,"probability":72,"senal":"DOCENA 1 y DOCENA 3"},{"id":20,"docena1":32,"docena2":32,"docena3":40,"probability":72,"senal":"DOCENA 1 y DOCENA 3"},{"id":21,"docena1":28,"docena2":32,"docena3":40,"probability":72,"senal":"DOCENA 2 y DOCENA 3"},{"id":22,"docena1":28,"docena2":36,"docena3":28,"probability":72,"senal":"DOCENA 1 y DOCENA 2"},{"id":23,"docena1":24,"docena2":36,"docena3":40,"probability":76,"senal":"DOCENA 2 y DOCENA 3"},{"id":24,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":25,"docena1":24,"docena2":32,"docena3":40,"probability":72,"senal":"DOCENA 2 y DOCENA 3"},{"id":26,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":27,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":28,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":29,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":30,"docena1":28,"docena2":32,"docena3":40,"probability":72,"senal":"DOCENA 2 y DOCENA 3"},{"id":31,"docena1":40,"docena2":24,"docena3":32,"probability":72,"senal":"DOCENA 1 y DOCENA 3"},{"id":32,"docena1":24,"docena2":32,"docena3":40,"probability":72,"senal":"DOCENA 2 y DOCENA 3"},{"id":33,"docena1":28,"docena2":36,"docena3":36,"probability":72,"senal":"DOCENA 2 y DOCENA 3"},{"id":34,"docena1":32,"docena2":24,"docena3":36,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":35,"docena1":32,"docena2":40,"docena3":24,"probability":72,"senal":"DOCENA 1 y DOCENA 2"},{"id":36,"docena1":36,"docena2":36,"docena3":24,"probability":72,"senal":"DOCENA 1 y DOCENA 2"}]

DOZEN_DATA_RUSSIAN = [{"id":0,"docena1":32,"docena2":32,"docena3":32,"probability":32,"senal":"NO APOSTAR"},{"id":1,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":2,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":3,"docena1":24,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":4,"docena1":32,"docena2":40,"docena3":24,"probability":72,"senal":"DOCENA 1 y DOCENA 2"},{"id":5,"docena1":28,"docena2":40,"docena3":28,"probability":72,"senal":"DOCENA 1 y DOCENA 2"},{"id":6,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":7,"docena1":40,"docena2":28,"docena3":28,"probability":72,"senal":"DOCENA 1 y DOCENA 3"},{"id":8,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":9,"docena1":28,"docena2":40,"docena3":28,"probability":72,"senal":"DOCENA 1 y DOCENA 2"},{"id":10,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":11,"docena1":32,"docena2":28,"docena3":36,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":12,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":13,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":14,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":15,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":16,"docena1":32,"docena2":32,"docena3":32,"probability":32,"senal":"NO APOSTAR"},{"id":17,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":18,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":19,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":20,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":21,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":22,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":23,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":24,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":25,"docena1":32,"docena2":32,"docena3":32,"probability":32,"senal":"NO APOSTAR"},{"id":26,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":27,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":28,"docena1":32,"docena2":28,"docena3":36,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":29,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":30,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":31,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":32,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":33,"docena1":32,"docena2":28,"docena3":36,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":34,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":35,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":36,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"}]

DOZEN_DATA_AZURE = [{"id":0,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":1,"docena1":24,"docena2":36,"docena3":40,"probability":76,"senal":"DOCENA 2 y DOCENA 3"},{"id":2,"docena1":36,"docena2":36,"docena3":28,"probability":72,"senal":"DOCENA 1 y DOCENA 2"},{"id":3,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":4,"docena1":36,"docena2":24,"docena3":36,"probability":72,"senal":"DOCENA 1 y DOCENA 3"},{"id":5,"docena1":32,"docena2":40,"docena3":24,"probability":72,"senal":"DOCENA 1 y DOCENA 2"},{"id":6,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":7,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":8,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":9,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":10,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":11,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":12,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":13,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":14,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":15,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":16,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":17,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":18,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":19,"docena1":32,"docena2":28,"docena3":36,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":20,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":21,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":22,"docena1":32,"docena2":28,"docena3":36,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":23,"docena1":24,"docena2":36,"docena3":40,"probability":76,"senal":"DOCENA 2 y DOCENA 3"},{"id":24,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":25,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":26,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":27,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":28,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":29,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":30,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":31,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":32,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":33,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":34,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":35,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":36,"docena1":28,"docena2":36,"docena3":36,"probability":72,"senal":"DOCENA 2 y DOCENA 3"}]

DOZEN_DATA_SPEED1 = [{"id":0,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":1,"docena1":24,"docena2":40,"docena3":32,"probability":72,"senal":"DOCENA 2 y DOCENA 3"},{"id":2,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":3,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":4,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":5,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":6,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":7,"docena1":36,"docena2":24,"docena3":36,"probability":72,"senal":"DOCENA 1 y DOCENA 3"},{"id":8,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":9,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":10,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":11,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":12,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":13,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":14,"docena1":36,"docena2":28,"docena3":32,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":15,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":16,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":17,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":18,"docena1":28,"docena2":40,"docena3":28,"probability":72,"senal":"DOCENA 1 y DOCENA 2"},{"id":19,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":20,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":21,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":22,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":23,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":24,"docena1":28,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":25,"docena1":28,"docena2":28,"docena3":40,"probability":72,"senal":"DOCENA 1 y DOCENA 3"},{"id":26,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":27,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":28,"docena1":32,"docena2":36,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":29,"docena1":36,"docena2":32,"docena3":28,"probability":68,"senal":"DOCENA 1 y DOCENA 2"},{"id":30,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":31,"docena1":32,"docena2":28,"docena3":36,"probability":68,"senal":"DOCENA 1 y DOCENA 3"},{"id":32,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":33,"docena1":36,"docena2":36,"docena3":24,"probability":72,"senal":"DOCENA 1 y DOCENA 2"},{"id":34,"docena1":28,"docena2":36,"docena3":32,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":35,"docena1":32,"docena2":32,"docena3":36,"probability":68,"senal":"DOCENA 2 y DOCENA 3"},{"id":36,"docena1":28,"docena2":32,"docena3":40,"probability":72,"senal":"DOCENA 2 y DOCENA 3"}]

# ─── ROULETTE CONFIGS ─────────────────────────────────────────────────────────
# Signal mode: "tendencia" | "moderado" | "ambos"
SIGNAL_MODE = "tendencia"   # ← cambiar aquí para configurar

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
MAX_ATTEMPTS = 2
BASE_BET   = 0.10   # USD
VISIBLE    = 40

# ─── LABOUCHÈRE 1.50x (for dozens — adapted) ──────────────────────────────────
# Initial sequence [1,1,1,1]; bet = (first+last)*base total, per dozen = half
LABOUCHERE_INIT = [1, 1, 1, 1]

class Labouchere:
    def __init__(self, init_seq: list, base: float):
        self.init_seq  = init_seq[:]
        self.base      = base
        self.seq       = init_seq[:]
        self.bankroll  = 0.0

    def _ensure(self):
        if not self.seq:
            self.seq = self.init_seq[:]

    def total_bet(self) -> float:
        """Total bet (both dozens combined)."""
        self._ensure()
        units = self.seq[0] + (self.seq[-1] if len(self.seq) > 1 else self.seq[0])
        return round(units * self.base, 2)

    def per_dozen_bet(self) -> float:
        return round(self.total_bet() / 2, 2)

    def win(self) -> float:
        bet = self.total_bet()
        # Win pays 2:1 on each dozen → net = bet (total bet recovered + profit equal to bet)
        self.bankroll = round(self.bankroll + bet * 0.5, 2)  # net profit at 1.50x combined
        if len(self.seq) <= 2:
            self.seq = []
        else:
            self.seq = self.seq[1:-1]
        self._ensure()
        return bet

    def loss(self) -> float:
        bet = self.total_bet()
        self.bankroll = round(self.bankroll - bet, 2)
        added = max(1, round(bet / self.base))
        if len(self.seq) < 8:
            self.seq.append(added)
        return bet


# ─── STATISTICS ───────────────────────────────────────────────────────────────
class Stats:
    def __init__(self):
        self.total = 0; self.wins = 0; self.losses = 0
        self.last_stats_at = 0
        self._wins_at_last_batch = 0
        self._h24: deque = deque()

    def record(self, is_win: bool, bankroll: float):
        self.total += 1
        if is_win: self.wins += 1
        else:      self.losses += 1
        self._h24.append((time.time(), is_win, bankroll))
        self._trim24()

    def _trim24(self):
        cutoff = time.time() - 86400
        while self._h24 and self._h24[0][0] < cutoff:
            self._h24.popleft()

    def should_send_stats(self) -> bool:
        return (self.total - self.last_stats_at) >= 20

    def batch_stats(self, bk: float):
        n = self.total - self.last_stats_at
        w = self.wins - self._wins_at_last_batch
        l = n - w
        e = round(w / n * 100, 1) if n else 0.0
        self._wins_at_last_batch = self.wins
        self.last_stats_at = self.total
        return w, l, n, e

    def stats_24h(self):
        self._trim24()
        t = len(self._h24)
        w = sum(1 for _, iw, _ in self._h24 if iw)
        l = t - w
        e = round(w / t * 100, 1) if t else 0.0
        return w, l, t, e


# ─── CHART GENERATION ─────────────────────────────────────────────────────────
D_COLORS = {1: "#5bc8fa", 2: "#f0c040", 3: "#c0392b", 0: "#3fe06d"}
D_LABELS = {1: "D1 (1-12)", 2: "D2 (13-24)", 3: "D3 (25-36)", 0: "0"}

def generate_chart(level_data: list, spin_history: list,
                   signal_dozens: List[int], visible: int = VISIBLE) -> io.BytesIO:
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

    # Signal colour (mix of the two dozens)
    if set(signal_dozens) == {1, 2}: sig_c = "#5bc8fa"
    elif set(signal_dozens) == {2, 3}: sig_c = "#c0392b"
    else: sig_c = "#f39c12"   # D1+D3

    bg = "#0b101f"; ax_bg = "#0f1a2a"; grid_c = "#1e2e48"

    fig, ax = plt.subplots(figsize=(8, 3.6), facecolor=bg)
    ax.set_facecolor(ax_bg)

    y   = arr[sl]
    ax.fill_between(x, y, alpha=0.09, color=sig_c)
    ax.plot(x, y,       color=sig_c,   linewidth=0.8, zorder=3)
    ax.plot(x, e4[sl],  color="#ffd700", linewidth=0.7, linestyle="--", label="EMA 4",  zorder=4)
    ax.plot(x, e8[sl],  color="#ff922b", linewidth=0.7, linestyle="--", label="EMA 8",  zorder=4)
    ax.plot(x, e20[sl], color="#ff4d4d", linewidth=1.0, label="EMA 20", zorder=4)

    # Coloured dots per dozen
    for i, spin in enumerate(hist_sl):
        c = D_COLORS.get(spin["real_dozen"], "#ffffff")
        ax.scatter(i, y[i], color=c, s=22, zorder=5, edgecolors="white", linewidths=0.3)

    # X-axis: numbers
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


# ─── ROULETTE ENGINE ──────────────────────────────────────────────────────────
class DozenEngine:
    def __init__(self, name: str, cfg: dict):
        self.name       = name
        self.ws_key     = cfg["ws_key"]
        self.chat_id    = cfg["chat_id"]
        self.thread_id  = cfg["thread_id"]
        self.dozen_data = cfg["dozen_data"]

        # Level tracking
        self.level_data:   list = []
        self.spin_history: list = []   # {"number", "real_dozen"}
        self.last_dozen:   Optional[int] = None
        self.last_d2_num:  Optional[int] = None
        self.anti_block:   set  = set()

        # Signal state
        self.signal_active:  bool       = False
        self.signal_dozens:  List[int]  = []
        self.signal_prob:    int        = 0
        self.trigger_number: Optional[int] = None
        self.attempts_left:  int        = 0
        self.total_attempts: int        = 0

        # Result cooldown
        self.result_until:  float = 0.0
        self.consec_losses: int   = 0

        # Betting
        self.lab = Labouchere(LABOUCHERE_INIT, BASE_BET)

        # Stats
        self.stats = Stats()
        self.signal_msg_id: Optional[int] = None
        self.ws     = None
        self.running = True

    # ── EMA ──────────────────────────────────────────────────────────────────
    @staticmethod
    def calc_ema(data: list, period: int) -> list:
        if len(data) < period: return [None] * len(data)
        mult = 2 / (period + 1)
        out  = [None] * (period - 1)
        prev = sum(data[:period]) / period
        out.append(prev)
        for i in range(period, len(data)):
            prev = (data[i] - prev) * mult + prev
            out.append(prev)
        return out

    # ── EMA trend ────────────────────────────────────────────────────────────
    def get_ema_trend(self) -> str:
        """
        bullish  = alcista fuerte  (cur > EMA4 > EMA8 > EMA20)
        bearish  = bajista fuerte  (cur < EMA4 < EMA8 < EMA20)
        neutral  = consolidación   (todo lo demás)
        """
        if len(self.level_data) < 20:
            return "neutral"
        ema4  = self.calc_ema(self.level_data, 4)
        ema8  = self.calc_ema(self.level_data, 8)
        ema20 = self.calc_ema(self.level_data, 20)
        cur   = self.level_data[-1]
        e4, e8, e20 = ema4[-1], ema8[-1], ema20[-1]
        if None in (e4, e8, e20):
            return "neutral"
        if cur > e4 > e8 > e20:  return "bullish"
        if cur < e4 < e8 < e20:  return "bearish"
        return "neutral"

    def get_trend_dozens_tendencia(self) -> List[int]:
        t = self.get_ema_trend()
        if t == "bullish": return [1, 2]
        if t == "bearish": return [2, 3]
        return [1, 3]   # neutral → consolidación

    def get_ema_trend_moderado(self) -> str:
        """
        Detecta cruces EMA4/EMA20 o patrón V para moderado.
        Returns: 'cross_up', 'cross_down', 'v_pattern', 'neutral'
        """
        if len(self.level_data) < 20: return "neutral"
        ema4  = self.calc_ema(self.level_data, 4)
        ema20 = self.calc_ema(self.level_data, 20)
        if len(ema4) < 2 or None in (ema4[-1], ema4[-2], ema20[-1], ema20[-2]):
            return "neutral"
        cross_up   = ema4[-2] <= ema20[-2] and ema4[-1] > ema20[-1]
        cross_down = ema4[-2] >= ema20[-2] and ema4[-1] < ema20[-1]
        # Patrón V
        v = False
        if len(self.level_data) >= 3:
            a, b, c = self.level_data[-3], self.level_data[-2], self.level_data[-1]
            v = b < a and b < c and c > a
        if cross_up or v:   return "cross_up"
        if cross_down:      return "cross_down"
        return "neutral"

    def get_trend_dozens_moderado(self) -> List[int]:
        t = self.get_ema_trend_moderado()
        if t == "cross_up":   return [1, 2]
        if t == "cross_down": return [2, 3]
        return [1, 3]   # neutral / consolidación

    # ── Table signal ─────────────────────────────────────────────────────────
    def get_table_signal(self, number: int):
        entry = next((e for e in self.dozen_data if e["id"] == number), None)
        if not entry or entry["senal"] == "NO APOSTAR":
            return [], 0
        senal = entry["senal"]
        if "DOCENA 1" in senal and "DOCENA 2" in senal: dozens = [1, 2]
        elif "DOCENA 1" in senal and "DOCENA 3" in senal: dozens = [1, 3]
        elif "DOCENA 2" in senal and "DOCENA 3" in senal: dozens = [2, 3]
        else: dozens = []
        return dozens, entry["probability"]

    # ── Combined signal ───────────────────────────────────────────────────────
    def get_combined_signal(self, number: int):
        table_dozens, prob = self.get_table_signal(number)
        if not table_dozens:
            return None

        if SIGNAL_MODE == "tendencia":
            trend_dozens = self.get_trend_dozens_tendencia()
            match = set(table_dozens) == set(trend_dozens)
            if not match: return None
            ema_label = self._ema_label_tendencia()

        elif SIGNAL_MODE == "moderado":
            trend_dozens = self.get_trend_dozens_moderado()
            match = set(table_dozens) == set(trend_dozens)
            if not match: return None
            ema_label = self._ema_label_moderado()

        else:  # "ambos" — tabla + cualquiera de los dos modos
            t_td = self.get_trend_dozens_tendencia()
            t_md = self.get_trend_dozens_moderado()
            if set(table_dozens) == set(t_td):
                ema_label = self._ema_label_tendencia()
            elif set(table_dozens) == set(t_md):
                ema_label = self._ema_label_moderado()
            else:
                return None

        return {"dozens": table_dozens, "prob": prob, "ema_label": ema_label}

    def _ema_label_tendencia(self) -> str:
        t = self.get_ema_trend()
        if t == "bullish": return "📈 Alcista Fuerte"
        if t == "bearish": return "📉 Bajista Fuerte"
        return "⟷ Consolidación"

    def _ema_label_moderado(self) -> str:
        t = self.get_ema_trend_moderado()
        if t == "cross_up":   return "🔼 Cruce EMA alcista"
        if t == "cross_down": return "🔽 Cruce EMA bajista"
        return "↔️ Patrón neutro"

    # ── Process one number ────────────────────────────────────────────────────
    def process_number(self, number: int):
        real_dozen = REAL_DOZENS[number]
        chg = dozen_change(number, self.last_dozen, self.last_d2_num)
        level = (self.level_data[-1] if self.level_data else 0) + chg
        self.level_data.append(level)
        if len(self.level_data) > 100:
            self.level_data.pop(0)

        self.spin_history.append({"number": number, "real_dozen": real_dozen})
        if len(self.spin_history) > 200:
            self.spin_history.pop(0)

        if real_dozen != 0:
            self.last_dozen = real_dozen
            if real_dozen == 2:
                self.last_d2_num = number

        # ── Resolve signal ────────────────────────────────────────────────────
        if self.signal_active and time.time() > self.result_until:
            hit = real_dozen in self.signal_dozens
            if hit:
                bet = self.lab.win()
                self.stats.record(True, self.lab.bankroll)
                self.signal_active  = False
                self.consec_losses  = 0
                self._send_result(number, real_dozen, True, bet)
                self._check_stats()
            else:
                self.attempts_left -= 1
                bet = self.lab.loss()
                if self.attempts_left <= 0:
                    self.consec_losses = min(self.consec_losses + 1, 9)
                    self.stats.record(False, self.lab.bankroll)
                    self.signal_active = False
                    self._send_result(number, real_dozen, False, bet)
                    self._check_stats()
                else:
                    new_bet = self.lab.total_bet()
                    self._send_retry_signal(number, new_bet)

        # ── Activate new signal ───────────────────────────────────────────────
        if not self.signal_active and time.time() > self.result_until:
            if len(self.spin_history) < 21:
                return
            sig = self.get_combined_signal(number)
            if sig:
                self.signal_active   = True
                self.signal_dozens   = sig["dozens"]
                self.signal_prob     = sig["prob"]
                self.trigger_number  = number
                self.attempts_left   = MAX_ATTEMPTS
                self.total_attempts  = MAX_ATTEMPTS
                self._ema_label_cache = sig["ema_label"]
                self._send_signal(number, 1, sig["ema_label"])

    # ── Signal message helpers ────────────────────────────────────────────────
    def _dozen_str(self, dozens: List[int]) -> str:
        return " + ".join(f"DOCENA {d} ({'1-12' if d==1 else '13-24' if d==2 else '25-36'})" for d in sorted(dozens))

    def _dozen_emoji(self, dozens: List[int]) -> str:
        if set(dozens) == {1, 2}: return "🔵🟡"
        if set(dozens) == {2, 3}: return "🟡🔴"
        return "🔵🔴"

    def _caption(self, trigger, attempt, bet_per, bet_total, prob, ema_label) -> str:
        dozen_str  = self._dozen_str(self.signal_dozens)
        dozen_icon = self._dozen_emoji(self.signal_dozens)
        return (
            f"✅ <b>SEÑAL CONFIRMADA</b> ✅\n\n"
            f"🎰 <b>Juego: {self.name}</b>\n"
            f"👉 <b>Ingresar después del: {trigger}</b>\n"
            f"🎯 <b>Apostar a: {dozen_str}</b> {dozen_icon}\n\n"
            f"💡 <i>Probabilidad de señal: {prob}%</i>\n"
            f"📊 <i>Tendencia EMA: {ema_label}</i>\n"
            f"📍 <i>Apuesta por docena: {bet_per:.2f} usd | Total: {bet_total:.2f} usd</i>\n\n"
            f"♻️ <i>Intento {attempt}/{MAX_ATTEMPTS}</i>"
        )

    def _send_signal(self, trigger: int, attempt: int, ema_label: str):
        bet_total = self.lab.total_bet()
        bet_per   = self.lab.per_dozen_bet()
        caption   = self._caption(trigger, attempt, bet_per, bet_total,
                                  self.signal_prob, ema_label)
        chart  = generate_chart(self.level_data[:], self.spin_history[:], self.signal_dozens)
        msg_id = tg_send_photo(self.chat_id, self.thread_id, chart, caption)
        self.signal_msg_id = msg_id
        logger.info(f"[{self.name}] Signal → {self.signal_dozens} after {trigger}, bet={bet_total:.2f}")

    def _send_retry_signal(self, trigger: int, new_bet_total: float):
        if self.signal_msg_id:
            tg_delete(self.chat_id, self.signal_msg_id)
            self.signal_msg_id = None
        bet_per = round(new_bet_total / 2, 2)
        ema_label = getattr(self, "_ema_label_cache", "–")
        caption = self._caption(trigger, 2, bet_per, new_bet_total,
                                self.signal_prob, ema_label)
        chart  = generate_chart(self.level_data[:], self.spin_history[:], self.signal_dozens)
        msg_id = tg_send_photo(self.chat_id, self.thread_id, chart, caption)
        self.signal_msg_id = msg_id
        logger.info(f"[{self.name}] Retry signal → {self.signal_dozens} after {trigger}, bet={new_bet_total:.2f}")

    def _send_result(self, number: int, real_dozen: int, won: bool, bet: float):
        bankroll = self.lab.bankroll
        d_icon   = {1: "🔵", 2: "🟡", 3: "🔴", 0: "🟢"}.get(real_dozen, "⬜")
        if won:
            text = (f"✅ <b>RESULTADO: {number}</b> {d_icon} D{real_dozen}\n"
                    f"💰 <i>Bankroll Actual: {bankroll:.2f} usd</i>")
        else:
            text = (f"❌ <b>RESULTADO: {number}</b> {d_icon} D{real_dozen}\n"
                    f"💰 <i>Bankroll Actual: {bankroll:.2f} usd</i>")
        self.result_until = time.time() + 7.0
        tg_send_text(self.chat_id, self.thread_id, text)
        logger.info(f"[{self.name}] Result {'WIN' if won else 'LOSS'} #{number} D{real_dozen}, bk={bankroll:.2f}")

    def _check_stats(self):
        if not self.stats.should_send_stats():
            return
        bk = self.lab.bankroll
        w20, l20, t20, e20 = self.stats.batch_stats(bk)
        w24, l24, t24, e24 = self.stats.stats_24h()
        text = (
            f"👉🏼 <b>ESTADÍSTICAS {t20} SEÑALES</b>\n"
            f"🈯️ <b>W: {w20}</b> 🈲 <b>L: {l20}</b> 🈺 <b>T: {t20}</b> 📈 <b>E: {e20}%</b>\n"
            f"💰 <i>Bankroll acumulado: {bk:.2f} usd</i>\n\n"
            f"👉🏼 <b>ESTADÍSTICAS 24 HORAS</b>\n"
            f"🈯️ <b>W: {w24}</b> 🈲 <b>L: {l24}</b> 🈺 <b>T: {t24}</b> 📈 <b>E: {e24}%</b>\n"
            f"💰 <i>Bankroll acumulado: {bk:.2f} usd</i>"
        )
        tg_send_text(self.chat_id, self.thread_id, text)
        logger.info(f"[{self.name}] Stats sent")

    # ── WebSocket ─────────────────────────────────────────────────────────────
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
    return jsonify({"status": "ok", "bot": "Docena Signal Bot", "ts": time.time()})

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


def run_flask():
    port = int(os.environ.get("PORT", 10001))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


async def main():
    engines = [DozenEngine(name, cfg) for name, cfg in ROULETTE_CONFIGS.items()]
    tasks   = [asyncio.create_task(e.run_ws()) for e in engines]
    tasks.append(asyncio.create_task(self_ping_loop()))
    logger.info(f"Docena bot started — {len(engines)} roulettes, mode={SIGNAL_MODE}")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("Flask started")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
