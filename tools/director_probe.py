#!/usr/bin/env python3
"""What would Watch actually choose?

Watch has nobody at the controls, so the only way to see its taste is to
hand it slates and read the picks back. Runs against a live server so the
answer comes from the same path the browser uses.

    python -B tools/director_probe.py --url http://127.0.0.1:5001
"""

import argparse
import json
import sys
import time
import urllib.request

SLATES = [
    ["Look through the gap in the fence",
     "Kick the generator over and run",
     "Wait for the lights to come back"],
    ["Follow the man at a distance",
     "Grab the man by the collar",
     "Shout to warn the others"],
    ["Study the markings on the door",
     "Force the door with the crowbar",
     "Back away down the corridor"],
    ["Scan the treeline for movement",
     "Walk toward the parked truck",
     "Set the flare off in the open"],
]


def post(base, path, body, timeout=60):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:5001")
    ap.add_argument("--session", default="default")
    args = ap.parse_args()

    for slate in SLATES:
        t0 = time.time()
        try:
            out = post(args.url, "/api/director/pick",
                       {"choices": slate, "session_id": args.session})
        except Exception as e:
            print(f"FAILED: {e}")
            return 1
        idx = out.get("index")
        chosen = slate[idx] if isinstance(idx, int) and 0 <= idx < len(slate) else repr(idx)
        print(f"[{out.get('source', '?'):8s}] {time.time() - t0:4.1f}s  {chosen}")
        if out.get("why"):
            print(f"           because {out['why']}")
        for i, text in enumerate(slate):
            print(f"             {'>' if i == idx else ' '} {text}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
