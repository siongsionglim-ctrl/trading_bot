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
MIN_SCORE = 8
DF_WINDOW = 100

last_trade_time = 0
initial_balance = 0
running = True
top_signals = []

higher_tf_bias = {}
last_higher_tf_time = {}

exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"}
})

MAJOR_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "SUI/USDT",
    "DOT/USDT", "TON/USDT", "NEAR/USDT", "WIF/USDT", "PEPE/USDT",
    "1000PEPE/USDT", "ARB/USDT", "OP/USDT", "FIL/USDT", "ETC/USDT",
    "ATOM/USDT", "INJ/USDT", "FET/USDT", "TAO/USDT", "HYPE/USDT",
    "ENA/USDT", "WLD/USDT", "ONDO/USDT", "RENDER/USDT", "KAS/USDT"
]

print(f"🚀 Scanning {len(MAJOR_SYMBOLS)} major liquid pairs on 5m timeframe")

streams = [s.replace("/", "").replace(":", "").lower() + "@kline_5m" for s in MAJOR_SYMBOLS]
ws_url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"

dataframes = {symbol: pd.DataFrame(columns=["open", "high", "low", "close", "volume"]) for symbol in MAJOR_SYMBOLS}

def set_leverage_for_all():
    print("🔧 Setting 10x leverage for all symbols...")
    for symbol in MAJOR_SYMBOLS:
        try:
            exchange.set_leverage(LEVERAGE, symbol)
            print(f"✅ Leverage {LEVERAGE}x set for {symbol}")
        except Exception as e:
            print(f"⚠️ Leverage set failed for {symbol}: {e}")

def apply_indicators(df):
    if len(df) < 20:
        return df
    df = df.copy()
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

def get_higher_tf_bias(symbol):
    global higher_tf_bias, last_higher_tf_time
    now = time.time()
    if symbol in last_higher_tf_time and now - last_higher_tf_time.get(symbol, 0) < 600:
        return higher_tf_bias.get(symbol)

    try:
        ohlcv = exchange.fetch_ohlcv(symbol, "15m", limit=50)
        df5 = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
        df5 = df5[["o", "h", "l", "c", "v"]].astype(float)
        df5 = apply_indicators(df5)
        last = df5.iloc[-1]

        bias = "LONG" if (last["ema20"] > last["ema50"] and last["close"] > last["ema20"]) else \
               "SHORT" if (last["ema20"] < last["ema50"] and last["close"] < last["ema20"]) else None

        higher_tf_bias[symbol] = bias
        last_higher_tf_time[symbol] = now
        return bias
    except Exception as e:
        print(f"⚠️ Higher TF bias error for {symbol}: {e}")
        return higher_tf_bias.get(symbol, None)

def calculate_score(df, symbol):
    if len(df) < 60:
        return 0, None, {}

    last = df.iloc[-1]
    prev = df.iloc[-8:-1] if len(df) > 8 else df.iloc[:-1]

    score = 0
    trend = None
    breakdown = {}

    # EMA Trend
    if last["ema20"] > last["ema50"] and last["close"] > last["ema20"]:
        score += 3
        trend = "LONG"
        breakdown["EMA"] = 3
    elif last["ema20"] < last["ema50"] and last["close"] < last["ema20"]:
        score += 3
        trend = "SHORT"
        breakdown["EMA"] = 3

    # Higher TF Bias (relaxed a bit for visibility)
    bias = get_higher_tf_bias(symbol)
    if bias == trend and bias is not None:
        score += 4
        breakdown["HigherTF"] = 4
    elif bias is not None:
        breakdown["HigherTF"] = 0   # still record it

    # MACD
    if (trend == "LONG" and last["macd_hist"] > 0) or (trend == "SHORT" and last["macd_hist"] < 0):
        score += 2
        breakdown["MACD"] = 2

    # RSI
    if (trend == "LONG" and 40 < last["rsi"] < 80) or (trend == "SHORT" and 20 < last["rsi"] < 60):
        score += 1
        breakdown["RSI"] = 1

    # Liquidity Sweep
    if (last["high"] > prev["high"].max() and last["close"] < last["open"]) or \
       (last["low"] < prev["low"].min() and last["close"] > last["open"]):
        score += 3
        breakdown["Liquidity"] = 3

    # Volume
    if last["volume"] > last["vol_avg"] * 1.5:
        score += 2
        breakdown["Volume"] = 2

    # Candle
    if trend and ((trend == "LONG" and last["close"] > last["open"]) or (trend == "SHORT" and last["close"] < last["open"])):
        score += 1
        breakdown["Candle"] = 1

    return score, trend, breakdown

# ... (get_balance, has_position, can_trade, place_trade functions remain the same as previous version)

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

def can_trade(symbol, required_margin):
    if time.time() - last_trade_time < COOLDOWN or has_position(symbol):
        print(f"❌ can_trade blocked for {symbol} (cooldown or position)")
        return False
    free, total = get_balance()
    if free < 5:
        print(f"⛔ Free balance too low ({free:.2f} USDT)")
        return False
    if free < required_margin:
        print(f"⛔ Not enough margin for {symbol}")
        return False
    if total < initial_balance * 0.70:
        print("🚨 Max drawdown reached")
        return False
    print(f"✅ can_trade PASSED for {symbol}")
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
    global initial_balance, running, top_signals
    running = True

    set_leverage_for_all()

    free, total = get_balance()
    initial_balance = total or 100
    print(f"💰 Bot Started | Total Balance: {initial_balance:.2f} USDT | 5m Candle Mode")

    print("🔌 Connecting to Binance WebSocket (5m candles)...")

    async with websockets.connect(ws_url) as ws:
        print("✅ WebSocket connected successfully - Receiving live 5m candles")

        while running:
            signals_list = []
            try:
                message = await ws.recv()
                data = json.loads(message)

                if 'data' in data and 'k' in data['data']:
                    k = data['data']['k']
                    if k['x']:
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

                        score, trend, breakdown = calculate_score(df, symbol)

                        # Always print score for visibility
                        print(f"Score for {symbol}: {score} ({trend or 'NEUTRAL'}) | Breakdown: {breakdown}")

                        if score >= MIN_SCORE and trend:
                            price = float(df.iloc[-1]["close"])
                            print(f"🟢 STRONG SIGNAL: {trend} {symbol} | Score: {score} | Price: {price:.2f}")

                            free_balance, _ = get_balance()
                            margin_needed = (free_balance * 0.02 * LEVERAGE) / price
                            amount = (free_balance * 0.02) / (price * 0.008)

                            if can_trade(symbol, margin_needed):
                                place_trade(symbol, trend, amount, price)
                            else:
                                print(f"❌ Order blocked by can_trade() for {symbol}")

                        # Collect Top 5 even with lower scores for dashboard visibility
                        if score >= 3 and trend:      # Lowered threshold for dashboard only
                            signals_list.append({
                                "symbol": symbol,
                                "score": score,
                                "trend": trend,
                                "breakdown": breakdown,
                                "price": float(df.iloc[-1]["close"])
                            })

            except Exception as e:
                print(f"WebSocket error: {e}")

            # Always update Top 5 with current best signals
            if signals_list:
                top_signals = sorted(signals_list, key=lambda x: x["score"], reverse=True)[:5]
            else:
                # Even if no strong signals, keep previous or clear gracefully
                pass

            await asyncio.sleep(0.05)

if __name__ == "__main__":
    asyncio.run(run())