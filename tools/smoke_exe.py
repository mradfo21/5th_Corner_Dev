#!/usr/bin/env python3
"""Prove the packaged app is playable, and that it never writes where it lives.

Boots dist/ABYSS/ABYSS.exe on a scratch port in mock mode, resets a
session, plays a couple of turns through the real HTTP API and checks the save
landed in the DATA root. Mock mode keeps it offline and free.

The install folder is made read-only for this user first (icacls deny), and
every file in it is fingerprinted before and after: an installer update
replaces that folder, so anything the game writes there is something a player
loses (docs/plans/DISTRIBUTION_MVP_PLAN.md, M2). The data root and %APPDATA%
are scratch folders, so the run touches none of this machine's keys, saves or
characters.

    python tools/smoke_exe.py               # read-only install folder
    python tools/smoke_exe.py --writable    # leave the ACL alone
    python tools/smoke_exe.py --app DIR     # a build somewhere else
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app_identity import APP_NAME  # noqa: E402

APP = ROOT / "dist" / APP_NAME
EXE = APP / f"{APP_NAME}.exe"
PORT = 5093
BASE = f"http://127.0.0.1:{PORT}"
# The exe arms local_guard; handing it the token is how this script knocks.
LAUNCH_TOKEN = secrets.token_urlsafe(32)
LAUNCH = {"X-Launch-Token": LAUNCH_TOKEN}


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **LAUNCH}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def get(path: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(BASE + path, headers=LAUNCH),
                                timeout=30) as r:
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


def _fingerprint(folder: Path) -> dict:
    out = {}
    for f in folder.rglob("*"):
        if f.is_file():
            st = f.stat()
            out[f.relative_to(folder).as_posix()] = (st.st_size, st.st_mtime_ns)
    return out


def _deny_writes(folder: Path) -> None:
    user = getpass.getuser()
    # Create/write/append only. Generic "W" carries SYNCHRONIZE, and D/DC
    # (delete) turn out to block CreateProcess too — either way Windows then
    # refuses to start the exe. A deletion still shows in the fingerprint.
    subprocess.run(["icacls", str(folder), "/deny", f"{user}:(OI)(CI)(WD,AD,WEA,WA)"],
                   check=True, capture_output=True)


def _allow_writes(folder: Path) -> None:
    user = getpass.getuser()
    subprocess.run(["icacls", str(folder), "/remove:d", user, "/T", "/C", "/Q"],
                   capture_output=True)


def main(argv=None) -> int:
    global APP, EXE
    ap = argparse.ArgumentParser()
    ap.add_argument("--writable", action="store_true")
    ap.add_argument("--app", default="")
    args = ap.parse_args(argv)
    if args.app:
        APP = Path(args.app).resolve()
        EXE = APP / f"{APP_NAME}.exe"
    if not EXE.exists():
        print(f"no build at {EXE} - run tools/build_exe.py first")
        return 1

    scratch = Path(tempfile.mkdtemp(prefix="abyss-smoke-"))
    data = scratch / "data"
    env = dict(os.environ, SOMEWHERE_LAUNCH_TOKEN=LAUNCH_TOKEN,
               SOMEWHERE_DATA_ROOT=str(data), APPDATA=str(scratch / "appdata"),
               ABYSS_NO_UPDATE_CHECK="1", SOMEWHERE_KEEP_OTHERS="1")
    for k in ("SESSIONS_DIR", "SOMEWHERE_KEYS_PATH", "SOMEWHERE_PROMPTS_PATH",
              "SOMEWHERE_WORLDS_DIR", "SOMEWHERE_EXPERIENCES_DIR", "GEMINI_API_KEY",
              "OPENAI_API_KEY", "ELEVENLABS_API_KEY"):
        env.pop(k, None)
    before = _fingerprint(APP)
    proc = None
    try:
        if not args.writable:
            _deny_writes(APP)
            print(f"install     read-only for {getpass.getuser()}: {APP}")
        print(f"data        {data}")
        proc = subprocess.Popen([str(EXE), "--mock", "--windowed", "--port", str(PORT)],
                                cwd=APP, env=env)
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

        saved = data / "sessions" / sess
        print(f"save        {'yes' if saved.exists() else 'NO'} -> {saved}")
        if not saved.exists():
            print("FAIL: the save did not land in the data root")
            return 1
        log = data / "logs" / "somewhere.log"
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        denied = [ln for ln in text.splitlines()
                  if ("PermissionError" in ln or "Access is denied" in ln) and str(APP) in ln]
        if denied:
            print("FAIL: the game tried to write into its install folder:")
            for ln in denied[:10]:
                print("   " + ln[:200])
            return 1
        print("\nPASS - the packaged app is playable.")
        return 0
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        if not args.writable:
            _allow_writes(APP)
        after = _fingerprint(APP)
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        if changed:
            print(f"\nFAIL: {len(changed)} file(s) in the install folder changed:")
            for k in changed[:30]:
                print("   " + k)
            os._exit(1)
        print(f"install     untouched ({len(before)} files)")
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
