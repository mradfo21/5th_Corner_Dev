"""Apply one edit to a prompt block in the live file AND in every World.

A World snapshot carries its own copy of the prompt layers, and BINDING one
installs that copy as live. So editing the bible in `prompts/simulation_prompts.json`
alone is a change that silently reverts the next time somebody picks a World —
which is exactly how the shipped default character came back and put a stranger
in the opening (see the 2026-09-18 entries).

Removes or replaces a literal string. Matching is exact, because a prompt is
prose and a regex over prose is how you delete half a sentence you meant to
keep. Reports every file it touched, and refuses to write anything if the needle
is not found anywhere (a typo should not read as "already clean").

    python tools/edit_prompt_everywhere.py world_initial_state \
        --remove "Never depict forests, dense woods, or non-desert biomes." \
        --apply

    python tools/edit_prompt_everywhere.py world_initial_state \
        --replace "always high desert" "usually high desert" --apply
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "prompts" / "simulation_prompts.json"
# The factory copy is a target too. It is what a NEW World is built from, so an
# edit that skips it is an edit that comes back the next time somebody creates
# one — the same reverting-change trap as the World snapshots, one level up.
DEFAULTS = ROOT / "prompts" / "simulation_prompts.defaults.json"


def targets() -> list[Path]:
    return [LIVE, DEFAULTS] + [Path(p) for p in
                               sorted(glob.glob(str(ROOT / "worlds" / "*.json")))
                               if not p.endswith(".frame.json")]


def block_of(doc: dict, key: str):
    """(container, value) for the prompt block, wherever this file keeps it."""
    if key in doc and isinstance(doc[key], str):
        return doc, doc[key]
    prompts = doc.get("prompts")
    if isinstance(prompts, dict) and isinstance(prompts.get(key), str):
        return prompts, prompts[key]
    return None, None


def tidy(text: str) -> str:
    """Close the gap a removed sentence leaves behind."""
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +([.,;:])", r"\1", text)
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("block", help="prompt block id, e.g. world_initial_state")
    ap.add_argument("--remove", action="append", default=[], metavar="TEXT")
    ap.add_argument("--replace", nargs=2, action="append", default=[],
                    metavar=("FROM", "TO"))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not args.remove and not args.replace:
        sys.exit("nothing to do: pass --remove or --replace")

    touched, found_any = 0, False
    for path in targets():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception as err:
            print(f"!! {path.name}: unreadable ({err})")
            continue
        container, text = block_of(doc, args.block)
        if container is None:
            continue

        out = text
        hits = []
        for needle in args.remove:
            if needle in out:
                hits.append(f"-{len(needle)}c")
                out = out.replace(needle, "")
        for a, b in args.replace:
            if a in out:
                hits.append(f"{a!r}->{b!r}")
                out = out.replace(a, b)
        if not hits:
            continue
        found_any = True
        out = tidy(out)
        print(f"{path.name:30} {args.block:<28} {' '.join(hits)}  "
              f"{len(text)} -> {len(out)} chars")
        touched += 1
        if args.apply:
            container[args.block] = out
            path.write_text(json.dumps(doc, indent=2, ensure_ascii=False),
                            encoding="utf-8")

    if not found_any:
        sys.exit("\nthat text is not in any copy of this block - check the "
                 "wording (matching is exact)")
    if args.apply:
        print(f"\nedited {touched} file(s). A World bind cannot put it back.")
    else:
        print(f"\n{touched} file(s) would change - re-run with --apply")


if __name__ == "__main__":
    main()
