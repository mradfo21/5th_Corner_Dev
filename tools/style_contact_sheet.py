"""Two rows of raw generated frames, before and after, for eyeballing a look change.

Usage: python tools/style_contact_sheet.py <before_session> <after_session> <out.png>
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
TILE_W = 420
LABEL_H = 26


def scene_frames(session: str, limit: int = 5) -> list[Path]:
    """The turn renders, newest last — skipping vision grabs and thumbnails."""
    d = ROOT / "sessions" / session / "images"
    frames = [
        p for p in d.glob("*.png")
        if "_small" not in p.name
        and not p.name.startswith("observed_")
        and "styleswatch" not in p.name
    ]
    frames.sort(key=lambda p: p.stat().st_mtime)
    return frames[:limit]


def row(paths: list[Path], label: str, width: int) -> Image.Image:
    tiles = []
    for p in paths:
        img = Image.open(p).convert("RGB")
        h = round(img.height * TILE_W / img.width)
        tiles.append(img.resize((TILE_W, h), Image.LANCZOS))
    tall = max((t.height for t in tiles), default=1)
    strip = Image.new("RGB", (width, tall + LABEL_H), (18, 18, 18))
    ImageDraw.Draw(strip).text((8, 7), label, fill=(235, 235, 235))
    x = 0
    for t in tiles:
        strip.paste(t, (x, LABEL_H))
        x += t.width + 4
    return strip


def main() -> int:
    before, after, out = sys.argv[1], sys.argv[2], sys.argv[3]
    a, b = scene_frames(before), scene_frames(after)
    if not a or not b:
        print(f"no frames: before={len(a)} after={len(b)}")
        return 1
    width = max(len(a), len(b)) * (TILE_W + 4)
    top = row(a, f"BEFORE  {before}", width)
    bottom = row(b, f"AFTER  {after}", width)
    sheet = Image.new("RGB", (width, top.height + bottom.height + 6), (18, 18, 18))
    sheet.paste(top, (0, 0))
    sheet.paste(bottom, (0, top.height + 6))
    sheet.save(out)
    print(f"wrote {out}  ({sheet.width}x{sheet.height})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
