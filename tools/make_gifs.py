"""Phone copies of the /get clips: one animated GIF per clip, under a size cap.

    python tools/make_gifs.py                 # static/video/get/*.mp4 -> *.gif
    python tools/make_gifs.py --src DIR --cap-mb 7

Why GIFs: on a phone the page's <video> often never plays. iPhone Low Power
Mode refuses every autoplay, data-saver skips it, and some in-app browsers
never start a muted inline video at all. Matt, on 2026-09-25: "the videos
still dont play… make them into small gif files around 7mb or less." An
<img> of a GIF always animates, so the page shows these at phone width.

Each clip is tried at a ladder of sizes, largest first, and the first that
fits under the cap wins; a clip too long for even the smallest rung is
trimmed from the end. The grain in the footage is what makes GIFs big, so
every rung takes a light denoise first. The chosen settings are written into
clips.json ("gif": true) so a server with no local copy knows to ask the
media release for it.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIPS = ROOT / "static" / "video" / "get"

# (width, fps) rungs, best-looking first.
LADDER = [(540, 12), (480, 12), (480, 10), (420, 10), (360, 10), (360, 8), (320, 8)]
MAX_SECONDS = (24.0, 18.0, 14.0, 11.0)   # trims tried on the last rung


def ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def encode(src: Path, out: Path, width: int, fps: int, seconds: float | None = None) -> int:
    trim = ["-t", f"{seconds:.2f}"] if seconds else []
    chain = (f"fps={fps},scale={width}:-2:flags=lanczos,hqdn3d=3:3:4:4,"
             "split[a][b];[a]palettegen=max_colors=160:stats_mode=diff[p];"
             "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")
    subprocess.run([ffmpeg(), "-loglevel", "error", "-y", *trim, "-i", str(src),
                    "-filter_complex", chain, "-loop", "0", str(out)], check=True)
    return out.stat().st_size


def make(src: Path, dest: Path, cap: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        trial = Path(tmp) / "t.gif"
        tries = [(w, f, None) for w, f in LADDER] + [(LADDER[-1][0], LADDER[-1][1], s) for s in MAX_SECONDS]
        for w, f, s in tries:
            size = encode(src, trial, w, f, s)
            if size <= cap:
                dest.write_bytes(trial.read_bytes())
                return {"width": w, "fps": f, "seconds": s, "bytes": size}
    raise SystemExit(f"{src.name}: no rung fits under {cap / 1e6:.1f} MB")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=str(CLIPS), help="folder holding <clip>.mp4 and clips.json")
    ap.add_argument("--cap-mb", type=float, default=7.0)
    args = ap.parse_args(argv)
    src = Path(args.src)
    manifest = src / "clips.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    cap = int(args.cap_mb * 1_000_000)
    for c in data.get("clips") or []:
        name = c.get("name") or ""
        mp4 = src / f"{name}.mp4"
        if not mp4.is_file():
            print(f"  {name:8s} no mp4, skipped")
            continue
        got = make(mp4, src / f"{name}.gif", cap)
        c["gif"] = True
        c["gif_bytes"] = got["bytes"]
        trim = f", first {got['seconds']:.0f}s" if got["seconds"] else ""
        print(f"  {name:8s} {got['bytes'] / 1e6:4.1f} MB  {got['width']}px {got['fps']}fps{trim}")
    manifest.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
