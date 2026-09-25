"""Draw the hero pose again for characters posed before the stylish-pose
prompt (2026-09-24: "why is he in an A pose. the off the sheet style should be
a stylish pose"). Only the idle is redrawn — the turnaround (what the
simulation sees), the words and the face sheet are untouched. Needs a Gemini
key; ~25 s per look.

    python tools/repose_characters.py                     every character
    python tools/repose_characters.py ghost jason-fleece  by id or name
    python tools/repose_characters.py --dir <roster> ...  another roster
                                                          (a copy, to look first)
    python tools/repose_characters.py --starters          the shipped starters
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

args = sys.argv[1:]
if "--dir" in args:
    i = args.index("--dir")
    os.environ["SOMEWHERE_CHARACTERS_DIR"] = str(Path(args[i + 1]).resolve())
    del args[i:i + 2]
starters = "--starters" in args
args = [a for a in args if a != "--starters"]

import play, keys_store  # noqa: E402
play._load_keys(); keys_store.load_into_environ()

import characters  # noqa: E402

if starters:
    characters.CHARACTERS_DIR = characters.STARTERS_DIR
elif os.environ.get("SOMEWHERE_CHARACTERS_DIR"):
    characters.CHARACTERS_DIR = Path(os.environ["SOMEWHERE_CHARACTERS_DIR"])


def main() -> int:
    if not characters.have_key():
        print("no Gemini key — nothing drawn")
        return 1
    rows = [characters.load(d.name) for d in sorted(characters.CHARACTERS_DIR.iterdir())
            if d.is_dir() and characters.valid_id(d.name) and not d.name.startswith("_")]
    rows = [r for r in rows if r and r.get("base_look")]
    if args:
        want = [a.lower() for a in args]
        rows = [r for r in rows if any(w == r["id"] or w in (r.get("name") or "").lower() for w in want)]
    print(f"re-posing in {characters.CHARACTERS_DIR}: {', '.join(r['name'] for r in rows) or 'nobody'}")
    bad = 0
    for r in rows:
        got = characters.repose(r["id"])
        for lid, res in got.items():
            print(f"  {r['name']:<24} {lid:<16} {res}")
            bad += res != "ok"
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
