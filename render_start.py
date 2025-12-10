import threading

def start_flask():
    from engine import _run_flask
    _run_flask()

if __name__ == "__main__":
    threading.Thread(target=start_flask, daemon=True).start()
    # Start the Discord bot as in bot.py's __main__
    import bot as bot_module
    import json
    from pathlib import Path
    ROOT = Path(__file__).parent.resolve()
    conf = json.load((ROOT / "config.json").open(encoding="utf-8"))
    TOKEN = conf["DISCORD_TOKEN"]
    # Use the bot object from the imported module
    bot_module.bot.run(TOKEN) 