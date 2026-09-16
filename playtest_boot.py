"""Start a fresh run and measure, twice a second, when the PICTURE appears vs
when the UI chrome appears. The UI must never be up first.
"""
import io
import os
import sys
import time

from PIL import Image
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.makedirs("_verify2", exist_ok=True)

PROBE = r"""
() => {
  const vis = (sel) => {
    const n = document.querySelector(sel);
    if (!n) return false;
    const cs = getComputedStyle(n);
    if (cs.display === "none" || cs.visibility === "hidden") return false;
    if (parseFloat(cs.opacity || "1") < 0.05) return false;
    const r = n.getBoundingClientRect();
    return r.width > 2 && r.height > 2;
  };
  return {
    cls: document.body.className,
    gated: document.body.classList.contains("awaiting-first-scene"),
    sceneVisible: !!(window.__state && window.__state.sceneVisible),
    chrome: ["#menu-toggle", "#control-rail", "#danger-health", "#action-wheel",
             "#scan-btn", "#objectives-hud", "#move-pad"]
      .filter((s) => vis(s)),
  };
}
"""


def luma(png):
    im = Image.open(io.BytesIO(png)).convert("L")
    w, h = im.size
    box = im.crop((int(w * 0.08), int(h * 0.12), int(w * 0.92), int(h * 0.72)))
    px = list(box.getdata())
    return round(sum(px) / len(px), 1)


with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp("http://127.0.0.1:9333")
    page = [p for p in b.contexts[0].pages if "standalone" in p.url][0]

    errors = []
    page.on("console", lambda m: errors.append(m.text[:200]) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)[:200]))

    print(">>> reloading for current JS", flush=True)
    page.reload(wait_until="load")
    page.wait_for_function("() => !!window.Renderer", timeout=60000)
    time.sleep(3)
    if errors:
        print("!! console errors right after load:", flush=True)
        for e in set(errors):
            print("   ", e, flush=True)

    cls = page.evaluate("() => document.body.className")
    if "xp-open" not in cls:
        print(">>> PLAY", flush=True)
        page.click("#start-play", timeout=10000)
        for _ in range(30):
            time.sleep(0.5)
            if "xp-ready" in page.evaluate("() => document.body.className"):
                break
    print(">>> Enter to start the run; timing from here", flush=True)
    t0 = time.time()
    page.keyboard.press("Enter")

    image_at = None
    chrome_at = None
    violations = []
    while time.time() - t0 < 75:
        time.sleep(0.5)
        el = time.time() - t0
        try:
            s = page.evaluate(PROBE)
            png = page.screenshot()
        except Exception:
            continue
        lv = luma(png)
        has_image = lv > 18
        has_chrome = len(s["chrome"]) > 0

        if has_image and image_at is None:
            image_at = el
            print(f"  [{el:5.1f}s] PICTURE on screen (luma={lv})", flush=True)
            with open("_verify2/a_picture.png", "wb") as f:
                f.write(png)
        if has_chrome and chrome_at is None:
            chrome_at = el
            print(f"  [{el:5.1f}s] UI chrome appeared: {s['chrome']} (luma={lv})",
                  flush=True)
            with open("_verify2/b_chrome.png", "wb") as f:
                f.write(png)
        if has_chrome and not has_image:
            violations.append((round(el, 1), lv, list(s["chrome"])))
        if image_at is not None and chrome_at is not None:
            break

    print("\n=== RESULT ===", flush=True)
    print(f"  picture at : {f'{image_at:.1f}s' if image_at is not None else 'NEVER'}",
          flush=True)
    print(f"  UI at      : {f'{chrome_at:.1f}s' if chrome_at is not None else 'never'}",
          flush=True)
    if violations:
        print(f"  FAIL: UI was up over a black screen in {len(violations)} sample(s):",
              flush=True)
        for el, lv, ch in violations[:8]:
            print(f"      [{el:5.1f}s] luma={lv} chrome={ch}", flush=True)
    elif image_at is None:
        print("  FAIL: no picture ever appeared", flush=True)
    else:
        print("  PASS: the picture was never later than the UI", flush=True)

    uniq = []
    for e in errors:
        if e not in uniq:
            uniq.append(e)
    print("\nconsole errors:" if uniq else "\nno console errors", flush=True)
    for e in uniq[:10]:
        print("  ", e, flush=True)
