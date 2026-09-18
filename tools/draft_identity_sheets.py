"""Draft the blank fields on the Character / Level / Camera sheets from the bible.

The same capability the editor's fill button has, on the command line, and
deliberately NOT on the boot path — see engine._ensure_level_sheet_is_filled for
what happened the afternoon it was. A model's guess replacing something a person
typed has to be something a person asked for and saw first.

    python tools/draft_identity_sheets.py            # show the gaps, write nothing
    python tools/draft_identity_sheets.py --apply     # draft and persist

Only ever fills fields that are EMPTY. Nothing authored is overwritten.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    import game_identity as gi

    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the drafts into the live prompt file")
    ap.add_argument("--block", action="append", default=None,
                    help="limit to one sheet (repeatable)")
    args = ap.parse_args()

    blocks = args.block or gi.text_fillable_blocks()
    gaps = {b: gi.empty_fill_fields(b) for b in blocks}
    gaps = {b: f for b, f in gaps.items() if f}
    if not gaps:
        print("every fillable field on every sheet is already filled.")
        return 0

    for block, fields in gaps.items():
        print(f"{block}: blank -> {fields}")
    if not args.apply:
        print("\n(dry run — pass --apply to draft and persist)")
        return 0

    # The Level sheet leads: it is what anchors the place.
    order = sorted(gaps, key=lambda b: b != getattr(gi, "SETTING_KEY", ""))
    wrote = 0
    for block in order:
        res = gi.apply_text_fill(block)
        filled = (res or {}).get("filled") or {}
        if not filled:
            print(f"\n{block}: nothing drafted ({(res or {}).get('reason')})")
            continue
        print(f"\n{block}:")
        for key, val in filled.items():
            print(f"  {key} = {val}")
        wrote += len(filled)
    print(f"\nwrote {wrote} field(s) to the live prompt file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
