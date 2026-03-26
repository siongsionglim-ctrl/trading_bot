import asyncio
import json
import time
import os
import ccxt
import pandas as pd
import websockets
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

LEVERAGE = 10
COOLDOWN = 300
MIN_SCORE = 10
DF_WINDOW = 180

last_trade_time = 0
initial_balance = 0
running = True

# Global caches for higher timeframe bias
higher_tf_bias = {}
last_higher_tf_time = {}

# Initialize exchange
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {
        "defaultType": "future",
        "adjustForTimeDifference": True
    }
})

# === FIXED: Load and select Top 30 by real 24h volume ===
print("📡 Loading Binance futures markets...")
exchange.load_markets()

# Filter active USDT perpetual futures
all_futures = []
for symbol, market in exchange.markets.items():
    try:
        if (symbol.endswith('/USDT') or symbol.endswith('/USDT:USDT')) and \
           market.get('active', False) and \
           (market.get('swap', False) or market.get('future', False)) and \
           market.get('linear', False):
            all_futures.append(symbol)
    except Exception:
        continue

print(f"🚀 Loaded {len(all_futures)} USDT perpetuals")

# ==================== FIXED MARKET LOADING - TOP 30 BY VOLUME ====================
print("📡 Loading Binance futures markets...")
exchange.load_markets()

all_futures = []
for symbol, market in exchange.markets.items():
    try:
        if (symbol.endswith('/USDT:USDT') or symbol.endswith('/USDT')) and \
           market.get('active', False) and \
           market.get('swap', False) and \
           market.get('linear', False):
            all_futures.append(symbol)
    except Exception:
        continue

print(f"🚀 Loaded {len(all_futures)} USDT perpetuals")

# === Sort by real 24h volume (most popular & liquid first) ===
print("🔄 Fetching 24h volume to select Top 30 liquid pairs...")
try:
    tickers = exchange.fetch_tickers()

    volume_dict = {}
    for symbol, ticker in tickers.items():
        if symbol.endswith('/USDT:USDT') or symbol.endswith('/USDT'):
            volume_dict[symbol] = float(ticker.get('quoteVolume') or 0)

    sorted_futures = sorted(
        all_futures,
        key=lambda s: volume_dict.get(s, 0),
        reverse=True
    )

    MAJOR_SYMBOLS = sorted_futures[:30]

    print(f"✅ Selected Top {len(MAJOR_SYMBOLS)} most liquid pairs by 24h volume")
    print(f"Example: {MAJOR_SYMBOLS[:8]}")   # This should show BTC, ETH, SOL, etc.

except Exception as e:
    print(f"⚠️ Volume fetch failed: {e}. Falling back to first 30.")
    all_futures.sort()
    MAJOR_SYMBOLS = all_futures[:30]

# WebSocket streams
streams = [s.replace("/", "").replace(":", "").lower() + "@kline_1m" for s in MAJOR_SYMBOLS]
ws_url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"

# Data storage
dataframes = {symbol: pd.DataFrame(columns=["open", "high", "low", "close", "volume"]) for symbol in MAJOR_SYMBOLS}

def set_leverage_for_all():
    for symbol in MAJOR_SYMBOLS:
        try:
            exchange.set_leverage(LEVERAGE, symbol)
            print(f"✅ Leverage {LEVERAGE}x set for {symbol}")
        except Exception as e:
            print(f"⚠️ Failed to set leverage for {symbol}: {e}")

def apply_indicators(df):
    if len(df) < 20:
        return df
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["vol_avg"] = df["volume"].rolling(20).mean()

    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = -delta.where(delta < 0, 0).rolling(14).mean()
    rs = gain / loss
    df["rsi"] = 100 - (100 / (1 + rs))

    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd_hist"] = (ema12 - ema26) - (ema12 - ema26).ewm(span=9, adjust=False).mean()

    return df

def calculate_score(df, symbol):
    if len(df) < 60:
        return 0, None
    last = df.iloc[-1]
    prev = df.iloc[-8:-1]

    score = 0
    trend = None

    if last["ema20"] > last["ema50"] and last["close"] > last["ema20"]:
        score += 3
        trend = "LONG"
    elif last["ema20"] < last["ema50"] and last["close"] < last["ema20"]:
        score += 3
        trend = "SHORT"
    else:
        return 0, None

    bias = get_higher_tf_bias(symbol)
    if bias == trend:
        score += 4
    elif bias is not None:
        return 0, None

    if (trend == "LONG" and last["macd_hist"] > 0) or (trend == "SHORT" and last["macd_hist"] < 0):
        score += 2
    if (trend == "LONG" and 45 < last["rsi"] < 75) or (trend == "SHORT" and 28 < last["rsi"] < 55):
        score += 2

    if (last["high"] > prev["high"].max() and last["close"] < last["open"]) or \
       (last["low"] < prev["low"].min() and last["close"] > last["open"]):
        score += 3

    if last["volume"] > last["vol_avg"] * 1.7:
        score += 2
    body_ratio = abs(last["close"] - last["open"]) / (last["high"] - last["low"] + 1e-9)
    if body_ratio > 0.65:
        score += 1

    return score, trend

def get_higher_tf_bias(symbol):
    now = time.time()
    if symbol in last_higher_tf_time and now - last_higher_tf_time.get(symbol, 0) < 600:
        return higher_tf_bias.get(symbol)

    try:
        ohlcv = exchange.fetch_ohlcv(symbol, "5m", limit=50)
        df5 = pd.DataFrame(ohlcv, columns=["ts","o","h","l","c","v"])[["o","h","l","c","v"]].astype(float)
        df5 = apply_indicators(df5)
        last = df5.iloc[-1]

        bias = "LONG" if (last["ema20"] > last["ema50"] and last["close"] > last["ema20"]) else \
               "SHORT" if (last["ema20"] < last["ema50"] and last["close"] < last["ema20"]) else None

        higher_tf_bias[symbol] = bias
        last_higher_tf_time[symbol] = now
        return bias
    except Exception as e:
        print(f"⚠️ Higher TF bias error for {symbol}: {e}")
        return higher_tf_bias.get(symbol)

def get_balance():
    try:
        bal = exchange.fetch_balance()
        free = float(bal.get("free", {}).get("USDT", 0))
        total = float(bal.get("total", {}).get("USDT", 0))
        print(f"💰 Balance fetched → Free: {free:.2f} | Total: {total:.2f} USDT")
        return free, total
    except Exception as e:
        print(f"⚠️ Balance fetch error: {e}")
        return 0, 0

def has_position(symbol):
    try:
        positions = exchange.fetch_positions([symbol])
        for p in positions:
            if abs(float(p.get("contracts", 0))) > 0 or float(p.get("info", {}).get("positionAmt", 0)) != 0:
                return True
        return False
    except:
        return False

def in_cooldown():
    return time.time() - last_trade_time < COOLDOWN

def can_trade(symbol, required_margin):
    if in_cooldown() or has_position(symbol):
        return False
    free, total = get_balance()
    if free < 5:
        print(f"⛔ Free balance too low ({free:.2f} USDT)")
        return False
    if free < required_margin:
        return False
    if total < initial_balance * 0.70:
        print("🚨 Max drawdown reached")
        return False
    return True

def place_trade(symbol, side, amount, price):
    global last_trade_time
    try:
        print(f"🔄 Opening {side} order for {symbol}...")
        exchange.set_leverage(LEVERAGE, symbol)
        entry_side = "buy" if side == "LONG" else "sell"
        
        exchange.create_market_order(symbol, entry_side, amount)
        print(f"✅ {side} ENTRY FILLED {symbol} | Amount: {amount:.4f}")

        sl_price = price * 0.994 if side == "LONG" else price * 1.006
        tp_price = price * 1.012 if side == "LONG" else price * 0.988
        sl_side = "sell" if side == "LONG" else "buy"
        tp_side = "sell" if side == "LONG" else "buy"

        exchange.create_order(symbol, "STOP_MARKET", sl_side, amount, None, {"stopPrice": sl_price, "reduceOnly": True})
        exchange.create_order(symbol, "TAKE_PROFIT_MARKET", tp_side, amount, None, {"stopPrice": tp_price, "reduceOnly": True})

        print(f"🚀 {side} {symbol} | TP: {tp_price:.2f} | SL: {sl_price:.2f}")
        last_trade_time = time.time()

    except Exception as e:
        print(f"❌ Trade error on {symbol}: {e}")

async def run():
    global initial_balance, running
    running = True

    set_leverage_for_all()

    free, total = get_balance()
    initial_balance = total or 100
    print(f"💰 Bot Started | Total Balance: {initial_balance:.2f} USDT")
    print(f"🔌 Connecting to Binance WebSocket...")

    async with websockets.connect(ws_url) as ws:
        print("✅ WebSocket connected successfully")

        while running:
            try:
                message = await ws.recv()
                data = json.loads(message)

                if 'data' in data and 'k' in data['data']:
                    k = data['data']['k']
                    if k['x']:  # candle closed
                        raw_symbol = data['data']['s']
                        symbol = next((s for s in MAJOR_SYMBOLS if s.replace("/", "").replace(":", "") == raw_symbol), None)
                        if not symbol:
                            continue

                        new_row = {
                            "open": float(k["o"]),
                            "high": float(k["h"]),
                            "low": float(k["l"]),
                            "close": float(k["c"]),
                            "volume": float(k["v"])
                        }

                        df = dataframes.get(symbol, pd.DataFrame(columns=["open", "high", "low", "close", "volume"]))
                        df = pd.concat([df, pd.DataFrame([new_row])]).tail(DF_WINDOW)
                        dataframes[symbol] = df

                        df = apply_indicators(df)

                        score, trend = calculate_score(df, symbol)
                        if score >= MIN_SCORE and trend:
                            price = df.iloc[-1]["close"]
                            print(f"🟢 STRONG SIGNAL: {trend} {symbol} | Score: {score} | Price: {price:.2f}")

                            free_balance, _ = get_balance()
                            margin_needed = (free_balance * 0.02 * LEVERAGE) / price
                            amount = (free_balance * 0.02) / (price * 0.008)

                            if can_trade(symbol, margin_needed):
                                place_trade(symbol, trend, amount, price)

            except Exception as e:
                print(f"WebSocket error: {e}")

            await asyncio.sleep(0.05)

if __name__ == "__main__":
    asyncio.run(run())