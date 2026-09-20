"""Give a World the authored protagonist instead of the shipped default.

A World snapshot carries its own copy of the Character sheet, and BINDING one
installs that copy as the live sheet. So a World saved before the protagonist
was authored keeps the factory character forever — and the moment you play it,
it writes that character over yours.

That is the "the custom character doesn't appear, instead it's the default guy"
report. It was never the cutscene: the run was bound to a World whose sheet
still said "adult man, short dark hair, weathered face, stubble" with no
reference plate, so every frame of it drew a stranger, and the live sheet was
replaced on the way in.

Only rewrites a World whose character is the SHIPPED DEFAULT and which has no
plate of its own — never one carrying a character somebody wrote. A World with
a deliberately different protagonist ("the traveler") is left alone.

    python tools/sync_character_into_worlds.py                 # what would change
    python tools/sync_character_into_worlds.py --apply
    python tools/sync_character_into_worlds.py worlds/yard.json --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

KEY = "player_character"
LIVE = ROOT / "prompts" / "simulation_prompts.json"
DEFAULTS = ROOT / "prompts" / "simulation_prompts.defaults.json"
# What identifies a sheet as "never authored": the factory text, in the fields
# the editor actually shows.
MARKS = ("appearance", "role", "wardrobe", "signature_gear")


def sheet(doc) -> dict:
    return dict((doc.get("prompts") or {}).get(KEY) or {})


def is_factory(live: dict, shipped: dict) -> bool:
    """The World's character is the shipped one, unchanged and unreferenced."""
    if live.get("reference_images"):
        return False                      # it has a plate: somebody meant this
    matched = 0
    for field in MARKS:
        a = str(live.get(field) or "").strip().casefold()
        b = str(shipped.get(field) or "").strip().casefold()
        if a and a == b:
            matched += 1
        elif a and b and a != b:
            return False                  # authored away from the default
    return matched >= 2


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("worlds", nargs="*", help="paths (default: every world)")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    authored = (json.loads(LIVE.read_text(encoding="utf-8")).get(KEY)) or {}
    shipped = (json.loads(DEFAULTS.read_text(encoding="utf-8")).get(KEY)) or {}
    if not authored.get("appearance"):
        sys.exit("the live Character sheet has no appearance to copy")
    if authored.get("appearance") == shipped.get("appearance"):
        sys.exit("the live Character sheet IS the shipped default - restore it "
                 "first (tools/restore_polluted_blocks.py player_character)")

    paths = [ROOT / w for w in args.worlds] if args.worlds else \
        sorted(p for p in (ROOT / "worlds").glob("*.json")
               if not p.name.endswith(".frame.json"))

    changed = 0
    for path in paths:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception as err:
            print(f"!! {path.name}: unreadable ({err})")
            continue
        live = sheet(doc)
        if not live:
            continue
        forced = bool(args.worlds)
        if not forced and not is_factory(live, shipped):
            continue
        look = str(live.get("appearance") or "(blank)")[:44]
        print(f"{path.name:28} {look:46} plates={len(live.get('reference_images') or [])}")
        changed += 1
        if args.apply:
            doc.setdefault("prompts", {})[KEY] = json.loads(json.dumps(authored))
            path.write_text(json.dumps(doc, indent=2, ensure_ascii=False),
                            encoding="utf-8")

    if not changed:
        print("every World already carries an authored character")
    elif args.apply:
        print(f"\ngave {changed} World(s) the authored protagonist")
    else:
        print(f"\n{changed} World(s) still carry the shipped default "
              f"- re-run with --apply")


if __name__ == "__main__":
    main()
