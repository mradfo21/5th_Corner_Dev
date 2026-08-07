import time
from playwright.sync_api import sync_playwright
URL = "https://fiveth-corner-dev-1a00.onrender.com"
sess = f"diag-{int(time.time())}"
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    page = b.new_page(viewport={"width": 1200, "height": 850})
    errors, console = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
    failed = []
    page.on("requestfailed", lambda r: failed.append(f"{r.method} {r.url[:90]} {r.failure}"))
    page.goto(f"{URL}/play?session={sess}", wait_until="domcontentloaded", timeout=60000)
    for t in (5, 15, 30, 45):
        page.wait_for_timeout((t - (0 if t == 5 else 0)) * 1000 if t == 5 else 10000)
        st = page.evaluate("""() => ({
            bootGate: document.body.classList.contains('awaiting-first-scene'),
            veilUp: !!(document.getElementById('processing-veil') &&
                      !document.getElementById('processing-veil').classList.contains('hidden')),
            sceneImg: (document.getElementById('sceneA')||{style:{}}).style.backgroundImage ? 'YES' : 'no',
            prose: document.querySelectorAll('#prose-feed > *').length,
            choices: document.querySelectorAll('#choices-container > *').length,
            mode: window.__InputBindings ? window.__InputBindings.current() : 'MISSING',
            renderer: window.__Renderer ? window.__Renderer.mode : 'MISSING',
        })""")
        print(f"  t+{t}s:", st)
    print("PAGE ERRORS:", errors[:6] if errors else "NONE")
    print("CONSOLE ERR:", [c for c in console if c.startswith('error')][:6] or "none")
    print("FAILED REQ:", failed[:6] or "none")
    print("LAST CONSOLE:")
    for c in console[-14:]:
        print("   ", c[:160])
    b.close()
