#!/usr/bin/env python3
"""Two Starts in a row: does the cache heal itself and then get used?

The first run should refuse the frame on disk (drawn by somebody else, or by
nobody we can name) and render its own, stamping it with the live prompts.
The second should open on that frame instantly.

    python -B tools/opening_frame_run.py --url http://127.0.0.1:5001
"""

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def post(base, path, body, timeout=180):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def opening(items):
    for it in items or []:
        if isinstance(it, dict) and it.get("type") == "scene_image":
            return it
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:5001")
    args = ap.parse_args()

    import world_frames as wf
    import worlds_store as ws

    slug = wf.start_world_slug()
    print(f"start world: {slug}")
    live = wf.live_fingerprint()
    print(f"live prompts: {live[:12]}\n")

    for attempt in (1, 2):
        before = wf._read_meta(slug)
        t0 = time.time()
        items = post(args.url, "/api/reset", {})
        took = time.time() - t0
        scene = opening(items)
        cached = bool((scene.get("metadata") or {}).get("cached_opening"))
        print(f"run {attempt}: {took:5.1f}s  "
              f"{'opened on the cache' if cached else 'rendered its own opening'}")
        print(f"  drawn before : {(str(before.get('drawn') or ''))[:12] or '(unrecorded)'}")
        # The intro render lands in the background; give it a moment to cache.
        for _ in range(40):
            after = wf._read_meta(slug)
            if str(after.get("drawn") or "") == live:
                break
            time.sleep(1)
        after = wf._read_meta(slug)
        print(f"  drawn after  : {(str(after.get('drawn') or ''))[:12] or '(unrecorded)'}"
              f"  {'== live' if str(after.get('drawn') or '') == live else '!= live'}")
        print(f"  source       : {after.get('source')}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
