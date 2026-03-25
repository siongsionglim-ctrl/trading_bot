from flask import Flask, jsonify
import threading
import asyncio
import bot

app = Flask(__name__)
bot_running = False

@app.route("/")
def home():
    return "Bot Server Running ✅"

@app.route("/ping")
def ping():
    return "OK", 200

@app.route("/start", methods=["GET", "POST"])
def start_bot():
    global bot_running
    if bot_running:
        return jsonify({"status": "already running"})
    
    bot.running = True
    thread = threading.Thread(target=run_bot, daemon=True)
    thread.start()
    
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
    asyncio.run(bot.run())

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)