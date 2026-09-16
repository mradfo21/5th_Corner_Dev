"""Put the live prompt file back to a coherent SOMEWHERE.

The shipped world had been split in half and each half was hollow where the other
was full. `somewhere-fp` carries a complete setting -- the 1993 Four Corners
quarantine fence, its era, palette, landmarks and opening shot -- but no
protagonist at all. `somewhere` carries the complete protagonist, Jason Fleece,
but a setting with nothing in it beyond the name.

Either half alone breaks the run in a way that reads as a rendering bug. With no
described setting, every turn's generation invents a new place and a single run
walks from a desert basin to a canyon to a shipbreaking yard without the player
moving. With no described protagonist, each frame re-casts the person on screen.

The protagonist also contradicted itself: named Jason, pronouns he/him, appearance
"adult man", but the demeanor and backstory were written in she/her from an
earlier draft. The prose flips gender mid-scene as a direct result.

Restoring the live prompt file is not enough on its own, and this is the part that
made the problem look unfixable. The active Experience owns the world: its
`start_world` node names a slug, and every reset applies that saved world's
prompts over the live file. The shipped `default` experience points at slug
`world`, and `worlds/world.json` is the hollow "an open place". So any world you
author -- by hand, through the editor, or with this script -- survives until the
next reset and is then silently replaced.

So --apply also writes the restored prompts back into the world the active
experience actually loads. Without that step the fix lasts one run.

    python tools/restore_somewhere.py --check    # report, change nothing
    python tools/restore_somewhere.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GEMINI_API_KEY", "")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SETTING_FROM = "somewhere-fp"   # the half with a real place
CHARACTER_FROM = "somewhere"    # the half with a real person

# The protagonist contradicted itself: name "Jason Fleece", pronouns he/him and
# appearance "adult man, short dark hair, stubble", but a demeanor and backstory
# written entirely in she/her.
#
# The reference plate settles it. `character_54a7f7d76882.png` is the image every
# generation is locked to -- it is literally what the player looks at -- and its
# own metadata labels it "KelseyRowe", a blonde woman. A live run confirmed it:
# the cutscene and the first playable frame both show her. So the she/her prose
# was the correct half and the name, pronouns and appearance were the stale draft.
# Fixing it the other way (which was tried first) left the narrator saying "he"
# over a picture of a woman.
CHARACTER_CORRECTIONS = {
    "name": "Kelsey Rowe",
    "pronouns": "she/her",
    "appearance": "adult woman, long blonde hair, weathered face, sun-lined",
}


def world_prompts(slug: str) -> dict:
    path = ROOT / "worlds" / f"{slug}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("prompts") or {}


def fix_pronouns(character: dict) -> tuple:
    """Make the written character agree with the plate it is drawn from."""
    out = dict(character)
    changed = []
    for field, value in CHARACTER_CORRECTIONS.items():
        if str(out.get(field) or "").strip() != value:
            out[field] = value
            changed.append(field)
    return out, changed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)

    setting = world_prompts(SETTING_FROM).get("setting_reference") or {}
    character = world_prompts(CHARACTER_FROM).get("player_character") or {}
    character, fixed = fix_pronouns(character)
    character["enabled"] = True
    setting = dict(setting)
    setting["enabled"] = True

    print(f"setting  <- worlds/{SETTING_FROM}.json : {setting.get('name')!r}")
    print(f"           era={setting.get('era')!r} palette={setting.get('palette')!r}")
    print(f"           landmarks={setting.get('landmarks')!r}")
    print(f"character<- worlds/{CHARACTER_FROM}.json: {character.get('name')!r} "
          f"({character.get('pronouns')})")
    if fixed:
        print(f"           pronouns corrected in: {', '.join(fixed)}")
    else:
        print("           no pronoun contradictions found")

    if not args.apply:
        print("\n--check only; rerun with --apply to write prompts/simulation_prompts.json")
        return 0

    import prompts_store
    import worlds_store
    # The narrative prompts (world_initial_state, the instruction blocks) come
    # from the SOMEWHERE half, which is where the 1993 analog-horror brief lives.
    payload = {k: v for k, v in world_prompts(CHARACTER_FROM).items()
               if k in set(prompts_store.editable_keys())}
    payload["setting_reference"] = setting
    payload["player_character"] = character
    prompts_store.save_prompts_bulk(payload)
    print(f"\napplied {len(payload)} field(s) to prompts/simulation_prompts.json")

    slug = active_start_slug()
    if not slug:
        print("could not read the active experience's start world; live prompts will "
              "be replaced on the next reset")
        return 1
    saved = worlds_store.save_world(name="World", note="restored coherent SOMEWHERE",
                                    slug=slug)
    print(f"snapshotted the same prompts into worlds/{slug}.json "
          f"(the world the active experience loads on reset)")
    print(f"  -> {saved.get('name')} / {saved.get('slug')}")
    return 0


def active_start_slug() -> str:
    """Which saved world the active Experience applies on every reset."""
    try:
        active = (ROOT / "experiences" / ".active").read_text(encoding="utf-8").strip()
    except Exception:
        active = "default"
    try:
        graph = json.loads((ROOT / "experiences" / f"{active}.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    start = graph.get("start_world")
    for world in graph.get("worlds") or []:
        if isinstance(world, dict) and world.get("id") == start:
            return str(world.get("slug") or "")
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
