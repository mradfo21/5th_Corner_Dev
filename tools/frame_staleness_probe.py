#!/usr/bin/env python3
"""Is the cached opening frame the same hero the run is about to play?

The first frame is cached per World and stamped with a hash of the World
snapshot's prompts. The run itself reads the LIVE prompt file. Those are two
different sources, so this prints both and says whether Play would open on a
picture of somebody else.

    python -B tools/frame_staleness_probe.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import world_frames as wf  # noqa: E402
import worlds_store as ws  # noqa: E402
from prompts_store import PROMPTS  # noqa: E402


def short(value, n=90):
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1] + "\u2026"


def main():
    slugs = [w["slug"] for w in ws.list_worlds()]
    if not slugs:
        print("no worlds on disk")
        return 0
    live = wf.fingerprint(PROMPTS)
    print(f"LIVE prompt fingerprint: {live[:12]}")
    print(f"  player_character: {short(PROMPTS.get('player_character'))}")
    print()
    for slug in slugs:
        snap = (ws.get_world(slug).get("prompts") or {})
        snap_fp = wf.fingerprint(snap)
        meta = wf._read_meta(slug)
        stamped = str(meta.get("fingerprint") or "")
        rec = wf.record(slug)
        print(f"[{slug}]")
        print(f"  snapshot fingerprint : {snap_fp[:12]}"
              f"  {'== live' if snap_fp == live else '!= LIVE'}")
        print(f"  frame stamped with   : {stamped[:12] or '(none)'}")
        print(f"  status/source        : {rec.get('status')} / {rec.get('source')}")
        print(f"  drawn from           : {(str(meta.get('drawn') or ''))[:12] or '(unrecorded)'}")
        print(f"  snapshot hero        : {short(snap.get('player_character'))}")
        would_open = bool(rec.get("url") and rec.get("path")
                          and wf.is_real_still(rec)
                          and wf.drawn_from_live(rec))
        verdict = "OPENS ON CACHE" if would_open else "renders fresh"
        if would_open and snap_fp != live:
            verdict += "  <-- STALE HERO"
        print(f"  play would           : {verdict}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
