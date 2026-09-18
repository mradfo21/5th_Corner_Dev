"""Put the real Level sheet back where a reset will actually read it.

Restoring `prompts/simulation_prompts.json` is not enough and it took a live
playtest to see why: `engine.apply_experience_start` reinstalls the START
WORLD's snapshot (`worlds/<slug>.json` → `prompts`) over the live prompt file on
EVERY reset. So a Level sheet restored only into the live file is wiped before
the first render, and the opening montage announces itself as
`'SOMEWHERE' toward '(no goal authored)'` — which is exactly what the log said.

This writes the sheet into BOTH, so the two agree and a reset is idempotent.

    python tools/restore_level_to_world.py --show
    python tools/restore_level_to_world.py --apply
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LIVE = ROOT / "prompts" / "simulation_prompts.json"


def from_head(key: str):
    out = subprocess.run(
        ["git", "show", "HEAD:prompts/simulation_prompts.json"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        sys.exit(f"git show failed: {out.stderr.strip()}")
    return json.loads(out.stdout).get(key)


def start_slug() -> str:
    import experience_store as xs
    import world_frames as wf
    return wf.start_world_slug(xs.get_experience())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--slug", default=None, help="override the start world slug")
    args = ap.parse_args()

    import worlds_store as ws

    slug = args.slug or start_slug()
    if not slug:
        sys.exit("could not resolve the start world slug")
    level = from_head("setting_reference")
    if not level or not (level.get("name") or "").strip():
        sys.exit("HEAD has no authored setting_reference to restore")

    snap_path = Path(ws.WORLDS_DIR) / f"{slug}.json"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    live = json.loads(LIVE.read_text(encoding="utf-8"))

    print(f"start world: {slug}  ({snap_path})")
    print(f"  snapshot level now : "
          f"{json.dumps((snap.get('prompts') or {}).get('setting_reference'))[:120]}")
    print(f"  live level now     : "
          f"{json.dumps(live.get('setting_reference'))[:120]}")
    print(f"  restoring to       : {json.dumps(level)[:160]}")
    if not args.apply:
        print("\n(dry run — pass --apply to write)")
        return 0

    snap.setdefault("prompts", {})["setting_reference"] = level
    snap_path.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    live["setting_reference"] = level
    # Keep the hero the same in both, or the snapshot wins at reset and the two
    # disagree about who the run is about. The snapshot's copy is the one a
    # reset installs, so it is the source of truth here.
    hero = (snap.get("prompts") or {}).get("player_character")
    if hero:
        live["player_character"] = hero
    LIVE.write_text(json.dumps(live, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print(f"\nwrote {snap_path}\nwrote {LIVE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
