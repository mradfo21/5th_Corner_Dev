"""Walk a session through a fixed slate and report, per turn, whether the
engine composed a fresh frame or edited the previous one.

    python -B tools/cut_probe.py --session cutprobe

Prints the history flags and copies each rendered frame out so the sequence
can be eyeballed for generation loss.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:5001"
OUT = Path("logs/cut_probe")

TRAVEL = [
    "Follow the canyon trail",
    "Sprint toward the central wreckage",
    "Scale the lightning struck mesa",
    "Scramble over the debris pile",
    "Sprint toward the scorched basin",
]


def post(path: str, body: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:
        return {"_error": str(e)}


def history(session: str) -> list:
    f = Path("sessions", session, "history.json")
    if not f.is_file():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="cutprobe")
    args = ap.parse_args()
    sid = args.session

    import engine

    print("=" * 78)
    print("CUT PROBE â€” does a travel choice compose a new frame or edit the old one?")
    print("=" * 78)
    print()
    for c in TRAVEL:
        kind, why = engine._transition_reason(c.lower())
        print(f"  classifier: {(kind or 'soft edit'):<9} {c:<36} {why}")
    print()

    for i, choice in enumerate(TRAVEL, 1):
        before = len(history(sid))
        res = post("/api/choose", {"session_id": sid, "choice": choice})
        if isinstance(res, dict) and (res.get("_http") or res.get("_error")):
            print(f"  turn {i}: request failed {json.dumps(res)[:200]}")
            break
        # /api/choose returns as soon as the turn is queued; the frame lands
        # later. Firing the whole slate at once just races the renderer.
        for _ in range(90):
            if len(history(sid)) > before:
                break
            time.sleep(2)
        else:
            print(f"  turn {i}: timed out waiting for the frame")
            break
        print(f"  turn {i}: {choice}")

    print()
    print("-- what the engine actually recorded --")
    OUT.mkdir(parents=True, exist_ok=True)
    hist = history(sid)
    soft_run = 0
    worst = 0
    for i, e in enumerate(hist):
        if e.get("hard_transition"):
            soft_run = 0
        else:
            soft_run += 1
            worst = max(worst, soft_run)
        img = Path(str(e.get("image") or ""))
        tag = "FRESH COMPOSITION" if e.get("hard_transition") else f"edit of previous (chain {soft_run})"
        print(f"  {i:>2}. {tag:<34} {str(e.get('choice'))[:40]}")
        if img.is_file():
            shutil.copy2(img, OUT / f"{i:02d}_{'cut' if e.get('hard_transition') else 'soft'}_{img.name}")
    print()
    print(f"deepest chain of consecutive edits in this run: {worst}")
    print(f"frames copied to {OUT.resolve()}")


if __name__ == "__main__":
    main()

