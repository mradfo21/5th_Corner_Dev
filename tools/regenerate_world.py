"""Fill in whatever this world has not been told, and print what changed.

The editor is supposed to do this on re-draw and reset. This is the same contract
from the command line, so it can be checked and so a world can be repaired without
clicking through the studio.

    python tools/regenerate_world.py                     # report what is missing
    python tools/regenerate_world.py --apply             # draft the blanks
    python tools/regenerate_world.py --apply --overwrite # redraw everything
    python tools/regenerate_world.py --apply --block setting_reference
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _load_env() -> None:
    """play.py loads .env; a bare import does not, and drafting needs a key."""
    import os
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually draft")
    ap.add_argument("--overwrite", action="store_true",
                    help="redraw fields that already have text (a re-draw)")
    ap.add_argument("--block", action="append", default=None, metavar="BLOCK",
                    help="limit to one block (repeatable)")
    args = ap.parse_args(argv)

    _load_env()
    import world_regen

    todo = world_regen.plan()
    print("=== what this world has not been told ===")
    for block, info in (todo.get("blocks") or {}).items():
        empty = info.get("empty") or []
        plate = "plate attached" if info.get("has_plate") else "no plate"
        print(f"  {block:20s} {plate:15s} empty: {empty or '(none)'}")
    music = todo.get("music") or {}
    print(f"  {'music':20s} {'':15s} "
          + ("empty" if music.get("empty") else f"'{music.get('direction')}'"))
    print(f"  MISSING FIELDS: {todo.get('missing', 0)}")

    if not args.apply:
        print("\n--report only; rerun with --apply to draft it")
        return 0

    print("\n=== regenerating ===")
    report = world_regen.regenerate(overwrite=args.overwrite, blocks=args.block)
    for line in world_regen.describe(report):
        print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
