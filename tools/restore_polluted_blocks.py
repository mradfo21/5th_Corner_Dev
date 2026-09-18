"""Put an identity block back the way git has it.

A full test run overwrote live authoring data in
``prompts/simulation_prompts.json``: ``setting_reference`` came back as "The
Kettle Yard", which is a fixture in test_cutscene.py, and ``player_character``
came back as the shipped defaults. conftest.py sandboxes the in-process suites,
but the e2e suites launch the real app in a subprocess and that is the hole its
own docstring warns about.

This restores NAMED BLOCKS ONLY, from HEAD, leaving every other field on disk
alone — the same file also carries real prompt work that is not committed yet,
so `git checkout` of the whole file is not the move.

    python tools/restore_polluted_blocks.py --show
    python tools/restore_polluted_blocks.py setting_reference --apply
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "prompts" / "simulation_prompts.json"


def head_copy() -> dict:
    out = subprocess.run(
        ["git", "show", "HEAD:prompts/simulation_prompts.json"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
    )
    if out.returncode != 0:
        sys.exit(f"git show failed: {out.stderr.strip()}")
    return json.loads(out.stdout)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("blocks", nargs="*", help="block ids to restore")
    ap.add_argument("--apply", action="store_true", help="write the change")
    ap.add_argument("--show", action="store_true", help="print both copies")
    args = ap.parse_args()

    head = head_copy()
    live = json.loads(LIVE.read_text(encoding="utf-8"))

    if args.show or not args.blocks:
        for key in ("player_character", "setting_reference", "camera_perspective"):
            print(f"=== {key}")
            print("  HEAD:", json.dumps(head.get(key), ensure_ascii=False)[:300])
            print("  LIVE:", json.dumps(live.get(key), ensure_ascii=False)[:300])
        if not args.blocks:
            return

    for key in args.blocks:
        if key not in head:
            print(f"!! {key} is not in HEAD; skipping")
            continue
        print(f"-> {key}: {json.dumps(live.get(key), ensure_ascii=False)[:90]}")
        print(f"        => {json.dumps(head[key], ensure_ascii=False)[:90]}")
        live[key] = head[key]

    if not args.apply:
        print("\n(dry run — pass --apply to write)")
        return
    LIVE.write_text(json.dumps(live, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print(f"\nwrote {LIVE}")


if __name__ == "__main__":
    main()
