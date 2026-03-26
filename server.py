from flask import Flask, jsonify, send_from_directory
import threading
import asyncio
import bot
import os
import signal
import sys

app = Flask(__name__)
bot_running = False
bot_thread = None

def signal_handler(sig, frame):
    print("🛑 Shutdown signal received")
    bot.running = False
    sys.exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

@app.route("/")
def home():
    return "Crypto Auto Bot Running ✅"

@app.route("/ping")
def ping():
    return "OK", 200

@app.route("/keepalive")
def keepalive():
    return "Keep-alive OK", 200

@app.route("/dashboard")
def dashboard():
    try:
        return send_from_directory(os.getcwd(), "dashboard.html")
    except Exception as e:
        return f"Dashboard file not found: {str(e)}", 404

@app.route("/start", methods=["GET", "POST"])
def start_bot():
    global bot_running, bot_thread
    if bot_running:
        return jsonify({"status": "already running", "running": True})

    bot.running = True
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    bot_running = True
    return jsonify({"status": "bot started", "running": True})

@app.route("/stop", methods=["GET", "POST"])
def stop_bot():
    global bot_running
    bot.running = False
    bot_running = False
    return jsonify({"status": "stop signal sent", "running": False})

@app.route("/status")
def status():
    return jsonify({"running": bot_running})

def run_bot():
    try:
        asyncio.run(bot.run())
    except Exception as e:
        print(f"❌ Bot crashed: {e}")
        global bot_running
        bot_running = False

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)