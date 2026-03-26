import asyncio
import time
import os
import ccxt
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

LEVERAGE = 10
COOLDOWN = 300          # 5 minutes between trades
MIN_SCORE = 10          # Strong confidence (pro level)
DF_WINDOW = 180
SCAN_INTERVAL = 50      # seconds between full scans (balanced)

last_trade_time = 0
initial_balance = 0
higher_tf_bias = {}
last_higher_tf_time = {}
last_balance_time = 0

# =========================
# EXCHANGE SETUP
# =========================
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"}
})

# Load markets once
exchange.load_markets()
# Major liquid coins only (to avoid ban + better quality)
MAJOR_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "TON/USDT",
    "SUI/USDT", "NEAR/USDT", "TRX/USDT"
]

print(f"🚀 Pro Bot Started | Monitoring {len(MAJOR_SYMBOLS)} major pairs")

# =========================
# INDICATORS
# =========================
def apply_indicators(df):
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

# =========================
# STRATEGY (Solid Pro Confluence)
# =========================
def calculate_score(df, symbol):
    if len(df) < 60:
        return 0, None

    last = df.iloc[-1]
    prev = df.iloc[-8:-1]

    score = 0
    trend = None

    # 1. EMA + Price Alignment
    if last["ema20"] > last["ema50"] and last["close"] > last["ema20"]:
        score += 3
        trend = "LONG"
    elif last["ema20"] < last["ema50"] and last["close"] < last["ema20"]:
        score += 3
        trend = "SHORT"
    else:
        return 0, None

    # 2. Higher TF (5m) Confluence
    bias = get_higher_tf_bias(symbol)
    if bias == trend:
        score += 4
    elif bias is not None:
        return 0, None

    # 3. Momentum
    if (trend == "LONG" and last["macd_hist"] > 0) or (trend == "SHORT" and last["macd_hist"] < 0):
        score += 2
    if (trend == "LONG" and 45 < last["rsi"] < 75) or (trend == "SHORT" and 28 < last["rsi"] < 55):
        score += 2

    # 4. Liquidity Sweep + Reversal
    if (last["high"] > prev["high"].max() and last["close"] < last["open"]) or \
       (last["low"] < prev["low"].min() and last["close"] > last["open"]):
        score += 3

    # 5. Volume + Strong Candle
    if last["volume"] > last["vol_avg"] * 1.7:
        score += 2
    body_ratio = abs(last["close"] - last["open"]) / (last["high"] - last["low"] + 1e-9)
    if body_ratio > 0.65:
        score += 1

    return score, trend

# Cached Higher TF Bias
def get_higher_tf_bias(symbol):
    global higher_tf_bias, last_higher_tf_time
    now = time.time()
    if symbol in last_higher_tf_time and now - last_higher_tf_time.get(symbol, 0) < 300:
        return higher_tf_bias.get(symbol)

    try:
        ohlcv = exchange.fetch_ohlcv(symbol, "5m", limit=70)
        df5 = pd.DataFrame(ohlcv, columns=["ts","o","h","l","c","v"])[["o","h","l","c","v"]].astype(float)
        df5 = apply_indicators(df5)
        last = df5.iloc[-1]

        bias = "LONG" if (last["ema20"] > last["ema50"] and last["close"] > last["ema20"]) else \
               "SHORT" if (last["ema20"] < last["ema50"] and last["close"] < last["ema20"]) else None

        higher_tf_bias[symbol] = bias
        last_higher_tf_time[symbol] = now
        return bias
    except:
        return higher_tf_bias.get(symbol)

# Safe Balance Check
def get_balance():
    global last_balance_time
    if time.time() - last_balance_time < 20:
        return None, None  # use cached if possible, but for safety we fetch lightly
    last_balance_time = time.time()
    try:
        bal = exchange.fetch_balance()
        free = bal["free"].get("USDT", 0)
        total = bal["total"].get("USDT", 0)
        return free, total
    except:
        return 0, 0

# Position Check
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
    if free < 5:                     # ← Your request: min 5 USDT free
        print("💰 Balance too low (<5 USDT)")
        return False
    if free < required_margin:
        return False
    if total < initial_balance * 0.70:
        print("🚨 Max drawdown reached")
        return False
    return True

# Professional TP/SL Placement
def place_trade(symbol, side, amount, price):
    global last_trade_time
    try:
        exchange.set_leverage(LEVERAGE, symbol)

        # 1. Open Market Order
        entry_side = "buy" if side == "LONG" else "sell"
        exchange.create_market_order(symbol, entry_side, amount)
        print(f"✅ {side} ENTRY {symbol} | Amount: {amount:.4f} @ ~{price:.2f}")

        # 2. Stop Loss (STOP_MARKET)
        sl_price = price * 0.994 if side == "LONG" else price * 1.006
        sl_side = "sell" if side == "LONG" else "buy"
        exchange.create_order(
            symbol, "STOP_MARKET", sl_side, amount, None,
            {"stopPrice": sl_price, "reduceOnly": True}
        )

        # 3. Take Profit (TAKE_PROFIT_MARKET)
        tp_price = price * 1.012 if side == "LONG" else price * 0.988
        tp_side = "sell" if side == "LONG" else "buy"
        exchange.create_order(
            symbol, "TAKE_PROFIT_MARKET", tp_side, amount, None,
            {"stopPrice": tp_price, "reduceOnly": True}
        )

        print(f"🚀 {side} {symbol} | TP: {tp_price:.2f} | SL: {sl_price:.2f}")
        last_trade_time = time.time()

    except Exception as e:
        print(f"❌ Trade error on {symbol}: {e}")

# =========================
# MAIN 24/7 LOOP (with balance protection)
# =========================
async def run():
    global initial_balance
    free, total = get_balance()
    initial_balance = total or 1000
    print(f"💰 Starting Balance: {initial_balance:.2f} USDT")

    while True:                                 # True 24/7 loop
        try:
            # First check balance before heavy scanning
            free, _ = get_balance()
            if free < 5:
                print("⛔ Balance < 5 USDT → Skipping scan to avoid ban")
                await asyncio.sleep(30)
                continue

            candidates = []
            for symbol in MAJOR_SYMBOLS:
                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, "1m", limit=DF_WINDOW)
                    df = pd.DataFrame(ohlcv, columns=["ts","o","h","l","c","v"])[["o","h","l","c","v"]].astype(float)
                    df = apply_indicators(df)

                    score, trend = calculate_score(df, symbol)
                    if score >= MIN_SCORE and trend:
                        price = df.iloc[-1]["close"]
                        candidates.append((score, trend, symbol, price))
                except:
                    continue

            candidates.sort(reverse=True, key=lambda x: x[0])
            top5 = candidates[:3]   # Only top 3 for safety

            print(f"\n🔥 Top Signals @ {time.strftime('%H:%M:%S')}")
            for score, trend, sym, price in top5:
                print(f"   Score: {score} | {trend} | {sym} @ {price:.2f}")

            if top5:
                best_score, best_trend, best_symbol, best_price = top5[0]
                free_balance, _ = get_balance()
                margin_needed = (free_balance * 0.025 * LEVERAGE) / best_price   # ~2.5% risk
                amount = (free_balance * 0.025) / (best_price * 0.006)           # rough risk-based

                if can_trade(best_symbol, margin_needed):
                    place_trade(best_symbol, best_trend, amount, best_price)

            await asyncio.sleep(SCAN_INTERVAL)

        except Exception as e:
            print(f"Scanner error: {e}")
            await asyncio.sleep(15)

# =========================
# START
# =========================
if __name__ == "__main__":
    asyncio.run(run())