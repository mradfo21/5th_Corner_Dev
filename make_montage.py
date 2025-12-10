# montage_from_images.py – standalone montage builder using Python + FFmpeg via imageio

import os
import random
from pathlib import Path
import imageio_ffmpeg

IMAGE_DIR = Path("images")
TEMP_LIST = "input.txt"
OUTPUT = "montage.mp4"
MIN_DURATION = 2
MAX_DURATION = 3

# ───────── Collect and Sort Images ─────────
images = sorted(
    [f for f in IMAGE_DIR.glob("*.png") if f.is_file()],
    key=lambda f: f.stat().st_mtime
)

if not images:
    print("❌ No images found in images/")
    exit(1)

# ───────── Write FFmpeg Input File ─────────
with open(TEMP_LIST, "w", encoding="utf-8") as f:
    for img in images:
        dur = round(random.uniform(MIN_DURATION, MAX_DURATION), 3)
        f.write(f"file '{img.as_posix()}'\n")
        f.write(f"duration {dur}\n")
    f.write(f"file '{images[-1].as_posix()}'\n")  # FFmpeg quirk: last image again

# ───────── Run FFmpeg from Python ─────────
ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
cmd = (
    f'"{ffmpeg_path}" -y -f concat -safe 0 -i {TEMP_LIST} '
    f'-vsync vfr -pix_fmt yuv420p -vf scale=1024:-2 {OUTPUT}'
)

print("▶️ Running FFmpeg command...")
os.system(cmd)
print(f"✅ Montage saved to {OUTPUT}")
