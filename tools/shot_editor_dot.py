#!/usr/bin/env python3
"""Screenshot the editor at each depth, and measure the nucleus against its ring.

The centre dot is the node you are inside. At the top level it is the only thing
on the page and should read as the subject; once a ring blooms around it, it is
context, and drawn at the same weight as its own children it competed with them
instead of framing them.

Writes logs/editor_dot_<depth>.png and prints the on-screen radii.
Run: python tools/shot_editor_dot.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

OUT = ROOT / "logs"
PHONE = {"width": 430, "height": 932}

RADII = """() => {
  const out = {};
  document.querySelectorAll('#eg-world .eg-node').forEach((g) => {
    if (g.style.display === 'none' || g.classList.contains('is-leaving')) return;
    const r = g.querySelector('.eg-cell').getBoundingClientRect();
    out[g.getAttribute('data-id')] = {
      w: Math.round(r.width * 10) / 10,
      core: g.classList.contains('is-core'),
    };
  });
  return out;
}"""


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> int:
    OUT.mkdir(exist_ok=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ, MOCK_MODE="1")
    proc = subprocess.Popen(
        [sys.executable, "run_local.py", "--mock", "--no-browser", "--port", str(port)],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(base + "/", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("server did not start")
            return 1

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport=PHONE, is_mobile=True, has_touch=True)
            page.goto(base + "/standalone?mode=play")
            page.keyboard.press("r")
            page.wait_for_selector(".choice-btn", state="attached", timeout=20000)
            page.keyboard.press("`")
            page.wait_for_selector("#we-graph", state="visible", timeout=10000)
            page.wait_for_timeout(1200)

            def shot(name: str) -> dict:
                page.wait_for_timeout(900)
                page.screenshot(path=str(OUT / f"editor_dot_{name}.png"))
                return page.evaluate(RADII)

            def dot(node_id: str) -> dict:
                return page.evaluate("(id) => window.EditorGraph.dotAt(id)", node_id)

            report = {"alone": shot("0_alone")}

            pt = dot("game")
            page.mouse.dblclick(pt["x"], pt["y"])
            report["ring"] = shot("1_ring")

            pt = dot("dot:game")
            page.mouse.click(pt["x"], pt["y"])
            page.wait_for_timeout(900)
            pt = dot("dot:mechanics")
            page.mouse.click(pt["x"], pt["y"])
            report["deep"] = shot("2_mechanics")

            browser.close()

        for depth, nodes in report.items():
            core = next((v["w"] for v in nodes.values() if v["core"]), None)
            leaves = [v["w"] for v in nodes.values() if not v["core"]]
            print(f"{depth:>6}: nucleus={core}  ring={sorted(set(leaves))}"
                  + (f"  ratio={round(core / max(leaves), 2)}" if core and leaves else ""))
        print(json.dumps(report, indent=2))
        print(f"\nwrote {OUT}/editor_dot_*.png")
        return 0
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
