import asyncio
import json
import time
import os
import ccxt
import pandas as pd
import numpy as np
import websockets
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

SYMBOL = "BTC/USDT"
WS_SYMBOL = "btcusdt"

TIMEFRAME = "1m"
LEVERAGE = 10

COOLDOWN = 300  # 5 min
MIN_SCORE = 5

last_trade_time = 0
initial_balance = 0

# =========================
# EXCHANGE SETUP
# =========================
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"}
})

# =========================
# DATA STORAGE
# =========================
df = pd.DataFrame(columns=["open","high","low","close","volume"])

# =========================
# WEBSOCKET
# =========================
async def price_stream():
    url = f"wss://stream.binance.com:9443/ws/{WS_SYMBOL}@kline_1m"
    async with websockets.connect(url) as ws:
        while True:
            data = await ws.recv()
            yield json.loads(data)

# =========================
# INDICATORS
# =========================
def apply_indicators(df):
    df["ema20"] = df["close"].ewm(span=20).mean()
    df["ema50"] = df["close"].ewm(span=50).mean()
    df["vol_avg"] = df["volume"].rolling(20).mean()
    return df

# =========================
# STRATEGY ENGINE
# =========================
def calculate_score(df):
    score = 0
    last = df.iloc[-1]
    prev = df.iloc[-5:]

    # 1. EMA trend
    if last["ema20"] > last["ema50"]:
        score += 1
        trend = "LONG"
    elif last["ema20"] < last["ema50"]:
        score += 1
        trend = "SHORT"
    else:
        return 0, None

    # 2. BOS (simple)
    if last["high"] > prev["high"].max():
        score += 2
    if last["low"] < prev["low"].min():
        score += 2

    # 3. Liquidity sweep
    if last["high"] > prev["high"].max() and last["close"] < last["open"]:
        score += 2
    if last["low"] < prev["low"].min() and last["close"] > last["open"]:
        score += 2

    # 4. Volume spike
    if last["volume"] > last["vol_avg"]:
        score += 1

    # 5. Order block (basic)
    if abs(last["close"] - last["open"]) > 0.5 * (last["high"] - last["low"]):
        score += 1

    return score, trend

# =========================
# BALANCE + RISK
# =========================
def get_balance():
    bal = exchange.fetch_balance()
    return bal["free"]["USDT"], bal["total"]["USDT"]

def has_position():
    pos = exchange.fetch_positions([SYMBOL])
    for p in pos:
        if float(p["contracts"]) > 0:
            return True
    return False

def calc_position_size(balance, score):
    if score >= 7:
        risk = 0.03
    elif score >= 5:
        risk = 0.02
    else:
        risk = 0.01

    return (balance * risk) * LEVERAGE

# =========================
# PROTECTION SYSTEM
# =========================
def in_cooldown():
    return time.time() - last_trade_time < COOLDOWN

def can_trade(required_margin):
    free, total = get_balance()

    if in_cooldown():
        print("⏳ Cooldown")
        return False

    if has_position():
        print("⚠️ Existing position")
        return False

    if free < required_margin:
        print("💰 Low balance")
        return False

    if total < initial_balance * 0.7:
        print("🚨 Max drawdown reached")
        exit()

    return True

# =========================
# EXECUTION
# =========================
def place_trade(side, amount, price):
    global last_trade_time

    try:
        exchange.set_leverage(LEVERAGE, SYMBOL)

        order = exchange.create_market_order(
            SYMBOL,
            "buy" if side == "LONG" else "sell",
            amount
        )

        # TP / SL
        if side == "LONG":
            sl = price * 0.995
            tp = price * 1.01
        else:
            sl = price * 1.005
            tp = price * 0.99

        print(f"🚀 {side} | TP: {tp:.2f} | SL: {sl:.2f}")

        last_trade_time = time.time()

    except Exception as e:
        print("Trade error:", e)

# =========================
# MAIN LOOP
# =========================
async def run():
    global df, initial_balance

    free, total = get_balance()
    initial_balance = total

    print(f"💰 Starting Balance: {total}")

    async for msg in price_stream():
        k = msg["k"]

        new_row = {
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"])
        }

        df = pd.concat([df, pd.DataFrame([new_row])]).tail(100)

        if len(df) < 50:
            continue

        df = apply_indicators(df)

        score, trend = calculate_score(df)

        print(f"Score: {score} | Trend: {trend}")

        if score < MIN_SCORE:
            continue

        price = df.iloc[-1]["close"]
        free_balance, _ = get_balance()

        amount = calc_position_size(free_balance, score) / price

        if can_trade(amount * price / LEVERAGE):
            place_trade(trend, amount, price)

# =========================
# START
# =========================
# asyncio.run(run())