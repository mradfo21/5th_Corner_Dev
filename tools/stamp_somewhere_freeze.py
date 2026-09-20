"""Stamp the shipped SOMEWHERE freeze: experiences/somewhere.json + a complete
worlds/somewhere.json.

Both halves of Play's front door were missing, and nothing said so at runtime.

`experiences/somewhere.json` is the shipped Play door. `.gitignore` allowlists
it by name (`!experiences/somewhere.json`), `tools/ship_layout.py` lists it in
FACTORY_FILES, `experience_store.factory_slug()` returns "somewhere" only when
it exists, and eight tests across test_somewhere_snapshot and
test_experience_graph assert its contents. It had never been created. So
`factory_slug()` fell through to `default` — this machine's author graph —
`stamp_factory()` copied no Play door at all, and a clean install opened
whatever leftover draft happened to be on disk.

`worlds/somewhere.json` exists but is hollow where it matters: six prompt
blocks are empty strings that the live authored world has filled. That is not
cosmetic, because `ship_layout.write_factory_prompts()` builds a build's live
prompt file as `defaults | somewhere.prompts` — and an empty string OVERRIDES
the default it is merged over. A build therefore shipped with no director
instructions and no encounter prompts at all.

This fills the hollow blocks from the world the active Experience actually
plays, and writes the Play door from the Experience that is already named
"somewhere". It deliberately touches NOTHING ELSE:

  * not `experiences/.active` — which Experience you have selected is yours,
  * not `prompts/simulation_prompts.json` — that is live authoring state, and
    a reset rewrites it from the bound world anyway.

    python tools/stamp_somewhere_freeze.py            # report, change nothing
    python tools/stamp_somewhere_freeze.py --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FREEZE_WORLD = ROOT / "worlds" / "somewhere.json"
FREEZE_EXP = ROOT / "experiences" / "somewhere.json"
#: The Experience already named "somewhere"; the source for lore and sound.
SOURCE_EXP = ROOT / "experiences" / "default.json"

#: World facts test_experience_graph requires on the shipped Lore node. The
#: protagonist is NOT one of them — see that test for why a shipped bible must
#: not name someone a recast cannot rename.
LORE_MARKERS = ("Horizon", "The Gate", "Four Corners", "1993")


def _blank(value) -> bool:
    """A prompt block that would override its default with nothing."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (dict, list)):
        return not value
    return False


#: Where the freeze's missing blocks come from. Deliberately a fixed slug and
#: NOT `start_world_slug(get_experience())`: the active pointer is the one thing
#: that cannot be trusted here — it is what bound Play to a half-finished draft
#: in the first place — and resolving the donor through it aimed this tool at
#: that draft's world on the first run. Pass --donor to override.
DEFAULT_DONOR = "world"


def plan(donor_slug: str) -> dict:
    donor_path = ROOT / "worlds" / f"{donor_slug}.json"
    if not donor_path.is_file():
        sys.exit(f"donor world not found: {donor_path}")
    donor = json.loads(donor_path.read_text(encoding="utf-8"))
    freeze = json.loads(FREEZE_WORLD.read_text(encoding="utf-8")) if FREEZE_WORLD.is_file() else {}

    dp = donor.get("prompts") or {}
    fp = freeze.get("prompts") or {}
    # Only what the freeze is MISSING, at either level. A value the freeze
    # already holds is a deliberate difference between the shipped game and
    # this machine's working copy, so overwriting it would quietly ship the
    # author's draft. That rule is also what protects the one field that must
    # never come across — the level's `name`, which is "SOMEWHERE" in the
    # freeze and whatever the author called theirs in the donor.
    fills = sorted(k for k, v in dp.items() if not _blank(v) and _blank(fp.get(k)))

    # Sheets are dicts, and a HALF-filled sheet is the shape that actually
    # broke runs: `setting_reference` carried the name "SOMEWHERE" and nothing
    # else, so the block read as present while describing no place at all, and
    # every turn's generation invented one — a single run walking from desert
    # basin to canyon to shipyard without the player moving (see
    # tools/restore_somewhere.py). Fill field by field for those.
    field_fills: dict = {}
    for key, dval in dp.items():
        fval = fp.get(key)
        if not isinstance(dval, dict) or not isinstance(fval, dict):
            continue
        missing = sorted(k for k, v in dval.items()
                         if not _blank(v) and _blank(fval.get(k)))
        if missing:
            field_fills[key] = missing
    return {
        "donor_slug": donor_slug,
        "donor_path": donor_path,
        "donor": donor,
        "freeze": freeze,
        "fills": fills,
        "field_fills": field_fills,
    }


def build_experience() -> dict:
    """The Play door, derived from the Experience already named "somewhere"."""
    import experience_store as xs

    src = json.loads(SOURCE_EXP.read_text(encoding="utf-8")) if SOURCE_EXP.is_file() else {}
    wid = "w-somewhere"
    exp = {
        "id": xs.SHIPPED_SLUG,
        "name": "SOMEWHERE",
        "start_world": wid,
        # One world, bound by SLUG to the freeze — not to the author's `world`
        # snapshot, which is the whole reason the shipped door has to exist
        # separately from `default`.
        "worlds": [{"id": wid, "name": "SOMEWHERE", "slug": xs.SHIPPED_SLUG,
                    "x": 0, "y": 0}],
        "cutscenes": list(src.get("cutscenes") or []),
        "transitions": [],
        "sound": src.get("sound") or xs.default_sound(),
        "lore": src.get("lore") or xs.default_lore(),
        # Points, not turns: a choice adds 1 and a MOVE TO / INTERACT adds 2, so
        # 4 / 9 is the "escalate around turn 2, peak around turn 5" arc
        # SOMEWHERE is written for. Entering the turn numbers themselves is how
        # live data ended up at 2 / 4, which reached critical on turn 2 and
        # stayed there for the rest of the run.
        "threat": {
            "escalate_at": xs.PRODUCT_ESCALATE_AT,
            "critical_at": xs.PRODUCT_CRITICAL_AT,
            "beat_normal": xs.DEFAULT_BEAT_NORMAL,
            "beat_escalating": xs.DEFAULT_BEAT_ESCALATING,
            "beat_critical": xs.DEFAULT_BEAT_CRITICAL,
        },
    }
    return exp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--donor", default=None,
                    help="world slug to fill the freeze's empty blocks from")
    args = ap.parse_args()

    donor_slug = args.donor or DEFAULT_DONOR
    p = plan(donor_slug)

    print(f"worlds/somewhere.json  <- filling from worlds/{donor_slug}.json")
    if not p["fills"] and not p["field_fills"]:
        print("   nothing hollow; the freeze already carries every block")
    for k in p["fills"]:
        val = (p["donor"].get("prompts") or {}).get(k)
        size = len(json.dumps(val))
        print(f"   + {k:<40s} {size:>6d} chars")
    for block, keys in sorted(p["field_fills"].items()):
        print(f"   ~ {block}")
        for k in keys:
            val = str((p["donor"]["prompts"][block]).get(k) or "")
            print(f"       . {k:<20s} {val[:56]!r}")

    exp = build_experience()
    lore_blob = str((exp.get("lore") or {}).get("notes") or "")
    for doc in (exp.get("lore") or {}).get("documents") or []:
        lore_blob += "\n" + str(doc.get("text") or "")
    missing = [m for m in LORE_MARKERS if m not in lore_blob]
    print(f"\nexperiences/somewhere.json <- id={exp['id']!r} "
          f"world={exp['worlds'][0]['slug']!r} "
          f"clock={exp['threat']['escalate_at']}/{exp['threat']['critical_at']} "
          f"lore={len(lore_blob)} chars")
    if missing:
        print(f"   WARNING: shipped lore is missing {missing} — "
              f"test_experience_graph asserts these")

    if not args.apply:
        print("\n(dry run — pass --apply to write)")
        return 0

    freeze = p["freeze"]
    freeze.setdefault("prompts", {})
    for k in p["fills"]:
        freeze["prompts"][k] = (p["donor"].get("prompts") or {})[k]
    for block, keys in p["field_fills"].items():
        for k in keys:
            freeze["prompts"][block][k] = p["donor"]["prompts"][block][k]
    FREEZE_WORLD.write_text(json.dumps(freeze, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
    FREEZE_EXP.parent.mkdir(parents=True, exist_ok=True)
    FREEZE_EXP.write_text(json.dumps(exp, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")
    print(f"\nwrote {FREEZE_WORLD}\nwrote {FREEZE_EXP}")
    print("left alone: experiences/.active, prompts/simulation_prompts.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
