"""Push the three-answer encounter slate into every saved prompt snapshot.

The code defaults in encounter.py are only the fallback. prompts/*.json and
the per-world / per-experience snapshots each carry their own authored copy,
and the snapshot wins on load — so changing the default alone leaves the
running game asking for the old confront/evade/use fan and the "use" key the
schema no longer accepts.

Run once: python -B tools/retune_encounter_lanes.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import encounter  # noqa: E402

KEYS = {
    "encounter_choice_overlay": encounter.DEFAULT_CHOICE_OVERLAY,
    "encounter_choice_instructions": encounter.DEFAULT_CHOICE_INSTRUCTIONS,
}

STALE = ("use:", "confront THEM", "turn the moment against THEM",
         "keys confront, evade, use")


def retune(obj) -> int:
    """Replace the authored slate wherever it appears, at any depth."""
    hits = 0
    if isinstance(obj, dict):
        for key, value in list(obj.items()):
            if key in KEYS and isinstance(value, str):
                if value != KEYS[key]:
                    obj[key] = KEYS[key]
                    hits += 1
            else:
                hits += retune(value)
    elif isinstance(obj, list):
        for item in obj:
            hits += retune(item)
    return hits


def main() -> None:
    targets = sorted(
        list(ROOT.glob("prompts/*.json"))
        + list(ROOT.glob("worlds/*.json"))
        + list(ROOT.glob("experiences/*.json"))
    )
    touched = 0
    for path in targets:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  skip {path.name}: {e}")
            continue
        hits = retune(data)
        if not hits:
            continue
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  retuned {hits:>2} in {path.relative_to(ROOT)}")
        touched += 1
    print(f"\n{touched} file(s) updated.")

    leftover = []
    for path in targets:
        try:
            blob = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for phrase in STALE:
            if phrase in blob:
                leftover.append(f"{path.relative_to(ROOT)}: {phrase!r}")
    if leftover:
        print("\nSTILL CARRYING THE OLD SLATE:")
        for line in leftover:
            print("  " + line)
    else:
        print("No snapshot still asks for the old confront/evade/use fan.")


if __name__ == "__main__":
    main()
