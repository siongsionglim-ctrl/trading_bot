from flask import Flask, request, jsonify
import threading
import bot

app = Flask(__name__)

bot_running = False

def run_bot():
    import asyncio
    asyncio.run(bot.run())

@app.route("/")
def home():
    return "Bot Server Running"

@app.route("/start", methods=["POST"])
def start_bot():
    global bot_running

    if bot_running:
        return jsonify({"status": "already running"})

    thread = threading.Thread(target=run_bot)
    thread.start()

    bot_running = True
    return jsonify({"status": "bot started"})

@app.route("/stop", methods=["POST"])
def stop_bot():
    global bot_running
    bot_running = False
    return jsonify({"status": "stop signal sent"})

@app.route("/status")
def status():
    return jsonify({"running": bot_running})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)