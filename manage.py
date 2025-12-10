#!/usr/bin/env python
"""
manage.py – process helper for SOMEWHERE StoryGen
2025‑05‑12 patch‑3
• Uses text mode for subprocess pipes → no RuntimeWarning
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).parent
PID_FILE = ROOT / ".story_pids.json"


# ───────── util ─────────────────────────────────────────────────────────────
def _read_pids() -> list[int]:
    if not PID_FILE.exists():
        return []
    raw = PID_FILE.read_text().strip()
    try:
        data = json.loads(raw)
        if isinstance(data, int):
            return [data]
        if isinstance(data, list):
            return [int(p) for p in data]
        if isinstance(data, dict):
            return [int(p) for p in data.values()]
    except json.JSONDecodeError:
        pass
    try:
        return [int(raw)]
    except ValueError:
        print("⚠️ PID file corrupted:", raw)
        return []


def _write_pid(pid: int):
    PID_FILE.write_text(json.dumps({"bot": pid}))


def _stream(tag: str, pipe):
    for line in pipe:
        print(f"{tag} | {line.rstrip()}", flush=True)


def _spawn(cmd: str, tag: str, env=None) -> int:
    proc = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / cmd)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,         # line‑buffering now works because text=True
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    import threading

    threading.Thread(target=_stream, args=(tag, proc.stdout), daemon=True).start()
    return proc.pid


def _kill(pids: Iterable[int]):
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def ensure_state_files(resume=False):
    # Initial content for world_state.json using base prompts
    world_state_path = Path("world_state.json")
    if not resume and (not world_state_path.exists() or world_state_path.stat().st_size == 0):
        # Load the default world prompt from the base prompts file
        prompts_path = ROOT / "prompts" / "simulation_prompts.json"
        try:
            base_prompts = json.load(prompts_path.open("r", encoding="utf-8"))
            initial_prompt = base_prompts.get("world_prompt", "")
        except Exception:
            initial_prompt = ""
        world_state_path.write_text(json.dumps({
            "world_prompt": initial_prompt,
            "current_phase": "normal",
            "chaos_level": 0,
            "last_choice": "",
            "last_saved": "",
            "seen_elements": []
        }, indent=2))
    # Initial content for history.json
    history_path = Path("history.json")
    if not resume and (not history_path.exists() or history_path.stat().st_size == 0):
        history_path.write_text("[]")


# ───────── commands ─────────────────────────────────────────────────────────
def start(args):
    if PID_FILE.exists() and _read_pids():
        print("Already running – use restart or stop first.")
        return
    env = os.environ.copy()
    if args.fast:
        env["VOTE_SECONDS"] = str(args.vote)
    pid = _spawn("bot.py", "BOT", env)
    _write_pid(pid)
    print(f"Started bot (PID {pid})")


def stop(_):
    pids = _read_pids()
    if not pids:
        print("Nothing running.")
        PID_FILE.unlink(missing_ok=True)
        return
    _kill(pids)
    PID_FILE.unlink(missing_ok=True)
    print("Stopped:", ", ".join(str(p) for p in pids))


def status(_):
    pids = _read_pids()
    if not pids:
        print("Not running.")
        return
    live = []
    for p in pids:
        try:
            os.kill(p, 0)
            live.append(p)
        except OSError:
            pass
    if live:
        print("Running (pids", ", ".join(map(str, live)), ")")
    else:
        print("PID file present but processes dead.")
        PID_FILE.unlink(missing_ok=True)


# ───────── CLI ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true", help="Resume from previous session (do not reset state files)")
    parser.add_argument("--fast", action="store_true", help="Enable fast mode (shorter vote time)")
    parser.add_argument("--vote", type=int, default=6, help="Vote seconds if fast mode enabled")
    args = parser.parse_args()
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    if args.fast:
        env["VOTE_SECONDS"] = str(args.vote)
    if args.resume:
        env["RESUME_MODE"] = "1"
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "bot.py"],
            stdout=sys.stdout,
            stderr=sys.stderr,
            env=env
        )
        proc.wait()
    except KeyboardInterrupt:
        print("\nStopping bot...")
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true", help="Resume from previous session (do not reset state files)")
    parser.add_argument("--fast", action="store_true", help="Enable fast mode (shorter vote time)")
    parser.add_argument("--vote", type=int, default=6, help="Vote seconds if fast mode enabled")
    args = parser.parse_args()
    ensure_state_files(resume=args.resume)
    # Pass args to main for further use
    main()
