"""Does vision still describe the old picture after a file is replaced?

World frames live at one fixed path per world (worlds/<slug>.frame.png) and
are regenerated in place. The vision cache is keyed on the absolute path, so
if the answer below is "yes", every consumer of vision is reading a
description of an image nobody is looking at any more.
"""

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

import engine  # noqa: E402

IMAGES = sorted(
    (ROOT / "sessions/default/images").glob("*.png"),
    key=lambda p: p.stat().st_mtime,
)
IMAGES = [p for p in IMAGES if "_small" not in p.name][-6:]
if len(IMAGES) < 2:
    print("need two rendered frames to compare")
    raise SystemExit(1)

A, B = IMAGES[0], IMAGES[-1]
scratch = ROOT / "sessions/default/images/_cache_probe.png"
small = scratch.with_name(scratch.name.replace(".png", "_small.png"))
for p in (scratch, small):
    p.unlink(missing_ok=True)

print(f"image A : {A.name}")
print(f"image B : {B.name}")
print()

shutil.copyfile(A, scratch)
first = engine._vision_analyze_all(str(scratch)) or {}
print("described as A:", (first.get("description") or "")[:150])
print()

# Same path, different picture — exactly what a world-frame regen does.
shutil.copyfile(B, scratch)
second = engine._vision_analyze_all(str(scratch)) or {}
print("described as B:", (second.get("description") or "")[:150])
print()

stale = (first.get("description") or "") == (second.get("description") or "")
print("=" * 66)
print("VERDICT:", "STALE — the new image was never looked at" if stale
      else "fresh — the cache noticed the file changed")

for p in (scratch, small):
    p.unlink(missing_ok=True)
