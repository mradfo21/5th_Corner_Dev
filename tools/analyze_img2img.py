#!/usr/bin/env python3
"""Reconstruct, per frame, WHICH frames img2img was steered from.

The question this answers: when a turn is a location change, does the render
compose off the frame the player is standing in, or off something older? The
"I entered a door and came out where I started" report is what an older
reference looks like from the inside.

Reads a server stdout log and prints one line per rendered frame:
  frame  cut?  refs  the reference filenames, newest first

Run: python tools/analyze_img2img.py logs/server_review.log
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

FRAME = re.compile(r"\[IMG2IMG COLLECT\] Frame (\d+) - Starting reference collection")
ENTRY = re.compile(r"\[IMG2IMG COLLECT\] History\[(\d+)\]: image=(\w+), vision=(\w+), "
                   r"hard_transition=(\w+)")
PATH = re.compile(r"\[IMG2IMG COLLECT\]   Image path: (.+)")
ADDED = re.compile(r"\[IMG2IMG COLLECT\]   -> Added to reference list \(total: (\d+)\)")
BOUNDARY = re.compile(r"\[IMG2IMG COLLECT\] HARD TRANSITION BOUNDARY")
RESULT = re.compile(r"\[IMG2IMG COLLECT\] RESULT: Found (\d+) reference images")
CUT = re.compile(r"  - Hard transition: (\w+)")
CHOICE = re.compile(r"  - Choice: '(.*)'")
THROTTLED = re.compile(r"\[HARD TRANSITION\] throttled")


def short(path: str) -> str:
    """The descriptive slug the renderer bakes into every filename."""
    name = Path(path.strip()).name
    name = re.sub(r"^\d+_", "", name)
    name = re.sub(r"(_small)?\.png$", "", name)
    return name[:44]


def main(log: Path) -> int:
    frames = []
    cur = None
    pending_path = None
    throttled_next = False

    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if THROTTLED.search(line):
            throttled_next = True
            continue
        m = FRAME.search(line)
        if m:
            cur = {"frame": int(m.group(1)), "entries": [], "refs": [],
                   "boundary": False, "found": None, "cut": None,
                   "choice": "", "throttled": throttled_next}
            throttled_next = False
            frames.append(cur)
            continue
        if cur is None:
            continue
        m = ENTRY.search(line)
        if m:
            cur["entries"].append({
                "idx": int(m.group(1)),
                "image": m.group(2) == "True",
                "hard": m.group(4) == "True",
                "path": None,
            })
            pending_path = cur["entries"][-1]
            continue
        m = PATH.search(line)
        if m and pending_path is not None:
            pending_path["path"] = m.group(1)
            continue
        if ADDED.search(line) and cur["entries"]:
            cur["refs"].append(cur["entries"][-1])
            continue
        if BOUNDARY.search(line):
            cur["boundary"] = True
            continue
        m = RESULT.search(line)
        if m:
            cur["found"] = int(m.group(1))
            continue
        m = CUT.search(line)
        if m:
            cur["cut"] = m.group(1) == "True"
            continue
        m = CHOICE.search(line)
        if m:
            cur["choice"] = m.group(1)

    print(f"{len(frames)} rendered frames in {log.name}\n")
    hdr = f"{'frm':>4} {'cut':>5} {'thr':>4} {'refs':>4}  {'choice':<34} references"
    print(hdr)
    print("-" * len(hdr))
    suspicious = []
    for f in frames:
        refs = " | ".join(short(r["path"] or "?") for r in f["refs"]) or "(none)"
        print(f"{f['frame']:>4} {str(f['cut']):>5} "
              f"{'YES' if f['throttled'] else '':>4} {f['found'] or 0:>4}  "
              f"{f['choice'][:34]:<34} {refs}")
        # A location change steered by more than one frame is the shape of the
        # bug: the older reference is a place the player has already left.
        if f["cut"] and (f["found"] or 0) > 1:
            suspicious.append(f)

    print()
    if suspicious:
        print(f"!! {len(suspicious)} location change(s) rendered with >1 reference "
              f"— the extra one is a place already left:")
        for f in suspicious:
            print(f"   frame {f['frame']}: {f['choice'][:60]}")
            for r in f["refs"]:
                print(f"      hard={r['hard']!s:<5} {short(r['path'] or '?')}")
    else:
        print("no location change was rendered with more than one reference")

    throttles = [f for f in frames if f["throttled"]]
    if throttles:
        print(f"\n!! {len(throttles)} turn(s) WANTED a location change and were "
              f"throttled into composing off the previous frame:")
        for f in throttles:
            print(f"   frame {f['frame']}: {f['choice'][:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1
                               else "logs/server_review.log")))
