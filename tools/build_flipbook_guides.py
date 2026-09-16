#!/usr/bin/env python3
"""(Re)build the flipbook layout guides in prompts/.

The engine attaches one of these to a flipbook generation as the LAST reference
image: it carries no scene, only the shape the output is supposed to be. The old
code referenced two of these by filename and neither was ever in the repo, so
every flipbook generation ran with no layout reference and the grid came back
however the model felt.

They are generated rather than committed as binaries so adding a frame count to
flipbook.SHAPES gives you its guide by re-running this:

    python tools/build_flipbook_guides.py

    --numbered   also write *_numbered.png variants. For reading the order
                 yourself only — never attach them. Text in a reference image
                 comes back burned into the generated panels.
    --cell W H   cell size (default 384x216, i.e. 16:9 panels)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import flipbook  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--numbered", action="store_true",
                    help="also write numbered variants (for humans, not the model)")
    ap.add_argument("--cell", nargs=2, type=int, default=[384, 216],
                    metavar=("W", "H"), help="cell size in pixels")
    args = ap.parse_args()

    out_dir = ROOT / "prompts"
    cell = (args.cell[0], args.cell[1])

    for frames in flipbook.FRAME_COUNTS:
        path = flipbook.build_guide(frames, cell=cell, root=out_dir)
        rows, cols = flipbook.shape_for(frames)
        print(f"  {path.relative_to(ROOT)}  {rows}x{cols}, {frames} panels")
        if args.numbered:
            numbered = out_dir / f"{path.stem}_numbered.png"
            flipbook.build_guide(frames, out_path=numbered, cell=cell,
                                 numbered=True)
            print(f"  {numbered.relative_to(ROOT)}  (debug only — do not attach)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
