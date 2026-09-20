"""Replace flipbook prompts that hard-code a grid we no longer draw.

Every copy of ``gemini_flipbook_4panel_prefix`` written before the frame count
became a setting opens with "THE RENDER MUST BE A 4x4 GRID" and walks through
sixteen numbered frames. The engine already refuses to send one of those
alongside a request for a different shape — ``flipbook.prefix_is_stale`` spots
it and falls back to the built-in rules for that turn — so nothing is broken by
leaving them. What IS lost is the authored half of the prompt: on a 4-frame run
every turn logs "authored prefix describes another grid" and the world's own art
direction never reaches the model at all. Found on a flipbook playtest where it
fired on all seven turns.

The shipped replacement (``simulation_prompts.defaults.json``) says the same
thing about continuity and motion without naming a shape, so it survives any
frame count.

    python tools/retire_stale_flipbook_prompts.py           # what would change
    python tools/retire_stale_flipbook_prompts.py --apply

Checked at EVERY supported frame count, not just the one currently selected: a
prompt that is honest about 2x2 and stale at 4x4 is still a prompt that dies the
moment somebody moves the slider.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import flipbook  # noqa: E402

KEY = "gemini_flipbook_4panel_prefix"
DEFAULTS = ROOT / "prompts" / "simulation_prompts.defaults.json"


def shipped_prefix() -> str:
    text = json.loads(DEFAULTS.read_text(encoding="utf-8")).get(KEY) or ""
    if not text.strip():
        sys.exit(f"{DEFAULTS.name} has no {KEY} to copy from")
    stale_at = [n for n in flipbook.FRAME_COUNTS
                if flipbook.prefix_is_stale(text, n)]
    if stale_at:
        sys.exit(f"the shipped prefix is itself stale at {stale_at} — fix it "
                 f"before copying it over eleven worlds")
    return text


def targets() -> list[Path]:
    return [ROOT / "prompts" / "simulation_prompts.json"] + \
        sorted((ROOT / "worlds").glob("*.json"))


def find(doc, path=""):
    """Every (json-path, text) pair under a flipbook prompt key."""
    out = []
    if isinstance(doc, dict):
        for k, v in doc.items():
            if k == KEY and isinstance(v, str):
                out.append((f"{path}/{k}", v))
            out.extend(find(v, f"{path}/{k}"))
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            out.extend(find(v, f"{path}[{i}]"))
    return out


def replace(doc, text: str) -> int:
    n = 0
    if isinstance(doc, dict):
        for k, v in list(doc.items()):
            if k == KEY and isinstance(v, str):
                doc[k] = text
                n += 1
            else:
                n += replace(v, text)
    elif isinstance(doc, list):
        for v in doc:
            n += replace(v, text)
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the changes")
    args = ap.parse_args()

    text = shipped_prefix()
    changed = 0
    for path in targets():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception as err:
            print(f"!! {path.name}: unreadable ({err})")
            continue
        stale = [(where, body) for where, body in find(doc)
                 if any(flipbook.prefix_is_stale(body, n)
                        for n in flipbook.FRAME_COUNTS)]
        if not stale:
            continue
        for where, body in stale:
            # A Windows console is cp1252 and these prompts open with an emoji.
            head = body.strip().splitlines()[0][:70]
            head = head.encode("ascii", "replace").decode("ascii")
            print(f"{path.name:32} {where:46} {len(body):>5} chars  {head}")
        changed += len(stale)
        if args.apply:
            replace(doc, text)
            path.write_text(json.dumps(doc, indent=2, ensure_ascii=False),
                            encoding="utf-8")

    if not changed:
        print("every flipbook prompt is shape-agnostic")
    elif args.apply:
        print(f"\nreplaced {changed} stale prompt(s) with the shipped prefix")
    else:
        print(f"\n{changed} stale prompt(s) — re-run with --apply")


if __name__ == "__main__":
    main()
