"""Put a World's authoring blocks back the way git has them.

The sibling of restore_polluted_blocks.py, for the other file the same accident
lands in. A World snapshot carries its OWN copy of the sheets and the prompt
layers under ``prompts``, so when an e2e suite overwrites the live authoring
data (it launches the real app in a subprocess, which writes to the repo — see
that tool's docstring) anything that then saves the World writes the factory
copy into the snapshot as well. Restoring only the live prompt file leaves the
World able to put the damage straight back the next time it is bound.

Restores NAMED BLOCKS ONLY, from HEAD, so prompt work that is genuinely newer
than the last commit survives beside them.

    python tools/restore_polluted_world.py worlds/world.json --show
    python tools/restore_polluted_world.py worlds/world.json \
        player_character setting_reference --apply

With no block names it restores every block whose live value is byte-identical
to the SHIPPED DEFAULTS while HEAD's is not — which is precisely the signature
of this accident, and does not touch a block you simply have not committed yet.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = ROOT / "prompts" / "simulation_prompts.defaults.json"


def head_copy(rel: str) -> dict:
    out = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=str(ROOT),
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        sys.exit(f"git show HEAD:{rel} failed: {out.stderr.strip()}")
    return json.loads(out.stdout)


def clip(value) -> str:
    return json.dumps(value, ensure_ascii=False)[:100]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("world", help="path to the world json, e.g. worlds/world.json")
    ap.add_argument("blocks", nargs="*", help="block ids (default: every polluted one)")
    ap.add_argument("--apply", action="store_true", help="write the change")
    ap.add_argument("--show", action="store_true", help="print both copies")
    args = ap.parse_args()

    path = (ROOT / args.world).resolve()
    rel = path.relative_to(ROOT).as_posix()
    live = json.loads(path.read_text(encoding="utf-8"))
    head = head_copy(rel)
    shipped = json.loads(DEFAULTS.read_text(encoding="utf-8"))

    live_p = live.get("prompts") or {}
    head_p = head.get("prompts") or {}

    blocks = args.blocks
    if not blocks:
        blocks = [k for k, v in live_p.items()
                  if k in head_p and v != head_p[k]
                  and k in shipped and v == shipped[k]]
        if not blocks:
            print("nothing in this World matches the shipped defaults over HEAD")
            return
        print(f"polluted blocks: {', '.join(blocks)}\n")

    changed = 0
    for key in blocks:
        if key not in head_p:
            print(f"!! {key} is not in HEAD's copy of this World; skipping")
            continue
        if live_p.get(key) == head_p[key]:
            print(f"== {key} already matches HEAD")
            continue
        print(f"-> {key}")
        if args.show:
            print(f"     LIVE: {clip(live_p.get(key))}")
            print(f"     HEAD: {clip(head_p[key])}")
        live_p[key] = head_p[key]
        changed += 1

    if not args.apply:
        print(f"\n{changed} block(s) would change - re-run with --apply")
        return
    live["prompts"] = live_p
    path.write_text(json.dumps(live, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
