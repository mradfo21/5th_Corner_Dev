"""Which saved worlds are actually described, and which are hollow?

A world whose `setting_reference` has a name but no summary, era, palette or
landmarks reads to the engine as a place with no properties. Nothing then anchors
the setting, so every turn's image and prose invent somewhere new -- a run walks
from a desert basin to a canyon to a shipbreaking yard without moving. That looks
like a rendering bug and is really an empty field.

The same applies to the protagonist: an under-filled `player_character` lets each
generation re-cast the person on screen.

    python tools/audit_worlds.py            # one line per saved world
    python tools/audit_worlds.py --detail somewhere
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SETTING_FIELDS = ("summary", "era", "palette", "landmarks", "opening_shot")
CHARACTER_FIELDS = ("name", "pronouns", "role", "appearance", "wardrobe",
                    "signature_gear", "demeanor", "backstory")

# A protagonist described with mismatched pronouns comes back as a different
# person from sentence to sentence. Cheap to detect, invisible until you read the
# prose and notice the hero changed sex mid-scene.
PRONOUN_WORDS = {
    "he/him": ("she", "her", "hers", "they", "them"),
    "she/her": ("he", "him", "his", "they", "them"),
    "they/them": (),
}


def _text(value) -> str:
    return str(value or "").strip()


def pronoun_conflicts(character: dict) -> list:
    declared = _text(character.get("pronouns")).lower()
    banned = PRONOUN_WORDS.get(declared)
    if not banned:
        return []
    out = []
    for field in ("demeanor", "backstory", "appearance", "wardrobe"):
        words = _text(character.get(field)).lower().replace(",", " ").replace(".", " ").split()
        hits = sorted({w for w in words if w in banned})
        if hits:
            out.append(f"{field} uses {'/'.join(hits)} but pronouns say {declared}")
    return out


def audit(prompts: dict) -> dict:
    setting = prompts.get("setting_reference")
    character = prompts.get("player_character")
    setting = setting if isinstance(setting, dict) else {}
    character = character if isinstance(character, dict) else {}
    return {
        "setting_name": _text(setting.get("name")),
        "setting_filled": sum(1 for f in SETTING_FIELDS if _text(setting.get(f))),
        "setting_missing": [f for f in SETTING_FIELDS if not _text(setting.get(f))],
        "character_name": _text(character.get("name")),
        "character_filled": sum(1 for f in CHARACTER_FIELDS if _text(character.get(f))),
        "character_missing": [f for f in CHARACTER_FIELDS if not _text(character.get(f))],
        "pronoun_conflicts": pronoun_conflicts(character),
        "setting": setting,
        "character": character,
    }


def load_prompts(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("prompts") if isinstance(data.get("prompts"), dict) else data


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--detail", metavar="SLUG", default="",
                    help="dump the full setting and character for one world (or 'live')")
    args = ap.parse_args(argv)

    live = ROOT / "prompts" / "simulation_prompts.json"
    targets = [("live (prompts/simulation_prompts.json)", live)]
    for f in sorted(glob.glob(str(ROOT / "worlds" / "*.json"))):
        if ".frame." in os.path.basename(f):
            continue
        targets.append((os.path.basename(f)[:-5], Path(f)))

    if args.detail:
        for label, path in targets:
            if args.detail in label:
                report = audit(load_prompts(path))
                print(f"=== {label} ===")
                print(json.dumps({"setting": report["setting"],
                                  "character": report["character"]}, indent=2))
                for problem in report["pronoun_conflicts"]:
                    print(f"  !! {problem}")
                return 0
        print(f"no world matching {args.detail!r}")
        return 1

    print(f"{'world':34s} {'setting':22s} {'set':>5s} {'character':18s} {'chr':>5s}  problems")
    print("-" * 110)
    for label, path in targets:
        try:
            report = audit(load_prompts(path))
        except Exception as exc:
            print(f"{label:34s} unreadable: {exc}")
            continue
        problems = []
        if report["setting_filled"] == 0:
            problems.append("SETTING HOLLOW - nothing anchors the place")
        elif report["setting_missing"]:
            problems.append("missing " + ",".join(report["setting_missing"]))
        problems.extend(report["pronoun_conflicts"])
        print(f"{label:34s} {report['setting_name'][:20]:22s} "
              f"{report['setting_filled']}/5  {report['character_name'][:16]:18s} "
              f"{report['character_filled']}/8  {'; '.join(problems)[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
