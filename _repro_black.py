"""Repro: load /play (the normal entry) in mock mode and report any JS error
plus whether a scene image ever renders."""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/workspace")
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright
from test_realtime_e2e import _find_free_port, _wait_for_health

port = _find_free_port()
base = f"http://127.0.0.1:{port}"
env = os.environ.copy()
env.update({"GEMINI_API_KEY": "", "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": ""})
proc = subprocess.Popen([sys.executable, "run_local.py", "--mock", "--no-browser", "--port", str(port)],
                        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
assert _wait_for_health(base), "server did not boot"

try:
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        for path in ("/play", "/realtime"):
            page = b.new_page(viewport={"width": 1200, "height": 850})
            errors, console = [], []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
            page.goto(base + path, wait_until="domcontentloaded")
            page.wait_for_timeout(9000)
            state = page.evaluate("""() => ({
                bootGate: document.body.classList.contains('awaiting-first-scene'),
                veil: !!(document.getElementById('processing-veil') &&
                        !document.getElementById('processing-veil').classList.contains('hidden')),
                sceneA: (document.getElementById('sceneA')||{}).style ?
                        document.getElementById('sceneA').style.backgroundImage.slice(0, 40) : 'n/a',
                proseItems: document.querySelectorAll('#prose-feed > *').length,
                choices: document.querySelectorAll('#choices-container > *').length,
                movementReady: typeof window.__Movement !== 'undefined',
                mouseLookReady: typeof window.__MouseLook !== 'undefined',
                bindings: window.__InputBindings ? window.__InputBindings.current() : 'MISSING',
                pollingStarted: !!window.__pollingStarted,
            })""")
            print(f"===== {path} =====")
            print("  page errors:", errors[:5] if errors else "NONE")
            print("  state:", state)
            bad = [c for c in console if c.startswith("error")]
            print("  console errors:", bad[:5] if bad else "none")
            page.close()
        b.close()
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
