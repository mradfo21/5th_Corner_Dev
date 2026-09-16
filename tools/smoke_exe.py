#!/usr/bin/env python3
"""Prove the packaged app is playable, not just serving pages.

Boots dist/SOMEWHERE/SOMEWHERE.exe on a scratch port in mock mode, resets a
session, plays a couple of turns through the real HTTP API and checks the app
wrote its save to disk. Mock mode keeps it offline and free.

    python tools/smoke_exe.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "dist" / "SOMEWHERE"
EXE = APP / "SOMEWHERE.exe"
PORT = 5093
BASE = f"http://127.0.0.1:{PORT}"


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read())


def _narrative_in(payload) -> str:
    """The prose a turn produced, from either a feed list or a turn object."""
    if isinstance(payload, dict):
        if payload.get("narrative"):
            return payload["narrative"]
        payload = payload.get("feed") or []
    texts = [e.get("content", "") for e in (payload if isinstance(payload, list) else [])
             if isinstance(e, dict) and e.get("type") == "narrative_event"]
    return texts[-1] if texts else ""


def _choices_in(payload) -> list:
    """Dig the choice slate out of a feed list or a turn response."""
    if isinstance(payload, dict):
        if payload.get("choices"):
            return payload["choices"]
        payload = payload.get("feed") or []
    for event in reversed(payload if isinstance(payload, list) else []):
        if isinstance(event, dict) and event.get("choices"):
            return event["choices"]
    return []


def main() -> int:
    if not EXE.exists():
        print(f"no build at {EXE} - run tools/build_exe.py first")
        return 1

    proc = subprocess.Popen([str(EXE), "--mock", "--windowed", "--port", str(PORT)],
                            cwd=APP)
    try:
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                if get("/api/health").get("status") == "healthy":
                    break
            except Exception:
                time.sleep(0.5)
        else:
            print("FAIL: the app never became healthy")
            return 1
        print(f"health      ok ({int(time.time() - (deadline - 120))}s to boot)")

        sess = "exe_smoke"
        # /api/reset answers with the opening feed: a list of events, one of
        # which carries the opening slate of choices.
        feed = post("/api/reset", {"session_id": sess})
        choices = _choices_in(feed)
        opening = next((e.get("content") for e in feed
                        if e.get("type") == "narrative_event"), "")
        print(f"reset       ok, {len(choices)} choices")
        print(f"            {opening[:120]}...")
        if not choices:
            print("FAIL: no opening choices")
            return 1

        last_id = max((e.get("id", 0) for e in feed), default=0)
        for turn in range(1, 3):
            text = choices[0].get("text") if isinstance(choices[0], dict) else str(choices[0])
            post("/api/choose", {"session_id": sess, "choice": text})

            # A turn resolves on a background thread and lands in the feed, so
            # the POST only echoes the action. Wait for the prose to show up.
            narrative, deadline = "", time.time() + 90
            while time.time() < deadline:
                events = get(f"/api/feed?since_id={last_id}&session_id={sess}")
                events = events if isinstance(events, list) else events.get("events", [])
                narrative = _narrative_in(events).strip()
                if narrative:
                    last_id = max((e.get("id", 0) for e in events), default=last_id)
                    choices = _choices_in(events) or choices
                    break
                time.sleep(0.5)

            print(f"turn {turn}      '{text[:38]}' -> {len(narrative)} chars, "
                  f"{len(choices)} next choices")
            if not narrative:
                print("FAIL: the turn never produced a narrative")
                return 1
            print(f"            {narrative[:130]}...")

        status = get(f"/api/status?session_id={sess}")
        print(f"status      turn={status.get('turn')} alive={status.get('alive')}")

        saved = APP / "sessions" / sess
        print(f"save on disk {'yes' if saved.exists() else 'NO'} -> {saved}")
        print("\nPASS - the packaged app is playable.")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
