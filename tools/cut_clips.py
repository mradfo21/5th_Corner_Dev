#!/usr/bin/env python3
"""Cut the /get page's clips out of real recordings of the game.

    python tools/cut_clips.py                 # cut everything in tools/get_clips.json
    python tools/cut_clips.py fight hero      # just these
    python tools/cut_clips.py --probe         # the source recordings' lengths
    python tools/cut_clips.py --still <src> --from 120 --to 300
                                              # where a recording sits still (the waits)
    python tools/cut_clips.py --spec S --out D --og F
                                              # another spec / folder (tools/refresh_get.py
                                              # cuts candidates this way before publishing)

The page shows only footage of the real app being played. The recordings are
tools/film_run.py masters (Chrome's own painted frames at a steady 30 fps).
tools/get_clips.json says which seconds of which recording make each clip.

Waiting is cut, not sped up. A segment marked "squeeze" is measured for motion
(10 samples a second, the mean change between samples); wherever the picture
holds still for longer than `hold` seconds — a turn developing, a plate
drawing, a veil breathing — only `hold` of it is kept, and the cut lands on
the next thing that moves. Everything that moves plays at its real speed, so
the game's own animation (the flipbook panels, the fight's hits, the tags
popping) is exactly as fluid as it is on screen. "speed" is still available
for a stretch that is slow but not still.

Writes into static/video/get/:
    <name>.mp4       1920x1080 30 fps H.264 (crf 21, slow), silent, faststart
    <name>-720.mp4   the same at 1280x720 for phones
    <name>.jpg       the poster (the clip's own `poster` second)
    clips.json       what the page reads
and static/img/get/og.jpg (the link preview) from the hero's poster.
Needs ffmpeg; the one imageio-ffmpeg ships (requirements.txt) is found.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "tools" / "get_clips.json"
OUT = ROOT / "static" / "video" / "get"
FPS = 30
FADE = 0.3
SIZES = {"": (1920, 1080, 21), "-720": (1280, 720, 23)}

# Motion analysis: a small grey picture, 10 samples a second.
AW, AH, ARATE = 160, 90, 10
STILL = 0.55          # mean change (0-255) below which a sample is "still"
GRAIN = 4             # per-pixel change at or under this is the game's film grain, not motion
HOLD = 0.5            # seconds of any still stretch that are kept
DARK = 16             # mean luma (0-255) under which a sample is a black hold
DARK_HOLD = 0.1       # seconds of a black hold that are kept


def ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        sys.exit("ffmpeg not found (pip install imageio-ffmpeg)")


def duration(ff: str, src: Path) -> float:
    r = subprocess.run([ff, "-i", str(src)], capture_output=True, text=True)
    for line in r.stderr.splitlines():
        if "Duration:" in line:
            h, m, s = line.split("Duration:")[1].split(",")[0].strip().split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    return 0.0


def motion(ff: str, src: Path, a: float, b: float, crop=None) -> list[float]:
    """How much the picture changes between consecutive samples, one per
    1/ARATE s from a. Measured on the middle of the frame: the game's HUD
    keeps a REC timecode ticking in a corner, and a second hand is not
    motion. The game lays an animated film grain over the picture, so the
    frame is area-averaged down and any pixel that moved by GRAIN or less
    counts as unchanged; otherwise a 1080p master never holds still. A
    near-black sample (a plate still developing) is returned as -1: still,
    and a hold nobody needs to see at all.

    `crop` measures inside that part of the frame instead ("motion_in_crop"
    on a segment). Not the default for a cropped segment: on the ACCOUNT
    sheet a key going in is a row of dots too small to survive the
    downscale, and measured in the crop the whole 40 s flow squeezed to
    two seconds."""
    if crop:
        cx, cy, cw, ch = (int(v) for v in crop)
        area = f"crop={cw}:{ch}:{cx}:{cy},"
    else:
        area = "crop=iw*0.8:ih*0.74:iw*0.1:ih*0.13,"
    proc = subprocess.Popen([ff, "-v", "error", "-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", str(src),
                             "-vf", f"fps={ARATE},{area}scale={AW}:{AH}:flags=area,format=gray", "-f", "rawvideo", "-"],
                            stdout=subprocess.PIPE)
    n = AW * AH
    prev, out = None, []
    while True:
        buf = proc.stdout.read(n)
        if len(buf) < n:
            break
        if prev is not None:
            sample = buf[::7]
            dark = sum(sample) / len(sample) < DARK
            out.append(-1.0 if dark else sum(max(abs(x - y) - GRAIN, 0) for x, y in zip(sample, prev[::7])) / len(sample))
        prev = buf
    proc.wait()
    return out


def still_runs(ff: str, src: Path, a: float, b: float, hold: float, crop=None) -> list[tuple[float, float, bool]]:
    """The spans inside [a, b] that hold still for longer than they need to:
    (start, end, dark). A dark span is cut to DARK_HOLD, a lit one to `hold`."""
    m = motion(ff, src, a, b, crop)
    runs, start, dark_n, n = [], None, 0, 0
    for i, d in enumerate(m + [999.0]):
        t = a + (i + 1) / ARATE
        if d < STILL:
            if start is None:
                start, dark_n, n = t - 1 / ARATE, 0, 0
            n += 1
            dark_n += d < 0
        else:
            if start is not None:
                dark = dark_n > n / 2
                if t - start > (DARK_HOLD if dark else hold) + 0.3:
                    runs.append((round(start, 2), round(t - 1 / ARATE, 2), dark))
            start = None
    return runs


def keep_ranges(ff: str, src: Path, a: float, b: float, hold: float, crop=None) -> list[tuple[float, float]]:
    ranges, cur = [], a
    for s, e, dark in still_runs(ff, src, a, b, hold, crop):
        keep = DARK_HOLD if dark else hold
        if s + keep > cur:
            ranges.append((cur, s + keep))
        cur = max(cur, e)
    if b - cur > 0.05:
        ranges.append((cur, b))
    return [(x, y) for x, y in ranges if y - x > 0.04]


def plan(ff: str, clip: dict, sources: dict) -> list[dict]:
    """Every segment turned into the exact pieces that play."""
    pieces = []
    for seg in clip["segments"]:
        src = ROOT / sources[seg["src"]]
        if not src.is_file():
            sys.exit(f"recording {seg['src']} not found at {src}")
        a, b, speed = float(seg["from"]), float(seg["to"]), float(seg.get("speed", 1))
        # "crop": [x, y, w, h] in the recording's pixels — a part of the screen
        # (the ACCOUNT sheet) blown up to fill the clip.
        crop = seg.get("crop")
        # "caption": the words the player typed, laid over the top of the
        # picture (the game's own input is a sliver at the bottom that
        # scrolls; nobody reads it in a clip). "type_on": this piece types it.
        cap = ({"caption": seg["caption"], "type_on": bool(seg.get("type_on")), "caption_box": seg.get("caption_box")}
               if seg.get("caption") else {})
        if seg.get("squeeze"):
            # "max": at most this many seconds of what survives the squeeze
            # (one world's montage in a clip that shows several). "keep":
            # "end" keeps the LAST seconds instead — where the payoff is (a
            # typed action's result, a fitting's new pose).
            left = float(seg["max"]) if seg.get("max") else None
            ranges = keep_ranges(ff, src, a, b, float(seg.get("hold", HOLD)),
                                 crop if seg.get("motion_in_crop") else None)
            from_end = seg.get("keep") == "end"
            kept = []
            for x, y in (reversed(ranges) if from_end else ranges):
                if left is not None:
                    if left <= 0.05:
                        break
                    if from_end:
                        x = max(x, y - left)
                    else:
                        y = min(y, x + left)
                    left -= y - x
                kept.append((x, y))
            for x, y in (reversed(kept) if from_end else kept):
                pieces.append({"src": src, "from": x, "to": y, "speed": speed, "crop": crop, **cap})
                cap = dict(cap, type_on=False) if cap else cap
        else:
            pieces.append({"src": src, "from": a, "to": b, "speed": speed, "crop": crop, **cap})
    return pieces


CAPTION_FONTS = ("consola.ttf", "C:/Windows/Fonts/consola.ttf", "DejaVuSansMono.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")


def caption_frames(text: str, w: int, h: int, folder: Path, type_on: bool, box=None) -> list[Path]:
    """The caption as transparent w x h PNGs: one (the whole line), or the
    line typed out a character at a time at FPS then held. `box` is
    [left, top, width] as fractions of the frame (a screen with its own
    heading where the default, centred at the top, would sit on it);
    the box then takes as many lines as the words need."""
    from PIL import Image, ImageDraw, ImageFont
    size = max(12, int(h * (0.022 if box else 0.03)))
    font = None
    for name in CAPTION_FONTS:
        try:
            font = ImageFont.truetype(name, size)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    pad_x, pad_y = int(size * 0.9), int(size * 0.55)
    prompt = "> "
    full = prompt + text
    probe = ImageDraw.Draw(Image.new("RGBA", (w, h)))
    max_w = int(w * (box[2] if box else 0.84)) - 2 * pad_x
    # Wrap to two lines at most, on words.
    lines, cur = [], ""
    for word in full.split(" "):
        trial = (cur + " " + word) if cur else word
        if probe.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = "  " + word
    lines.append(cur)
    lines = lines[:8 if box else 2]
    box_w = int(max(probe.textlength(line, font=font) for line in lines)) + 2 * pad_x
    line_h = int(size * 1.35)
    box_h = line_h * len(lines) + 2 * pad_y
    x0, y0 = ((int(w * box[0]), int(h * box[1])) if box else ((w - box_w) // 2, int(h * 0.075)))

    def frame(shown: int, cursor: bool) -> "Image.Image":
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([x0, y0, x0 + box_w, y0 + box_h], radius=int(size * 0.4),
                            fill=(4, 8, 6, 178), outline=(142, 255, 193, 70), width=max(1, size // 18))
        left, at = shown, None
        for i, line in enumerate(lines):
            part = line[:max(0, left)]
            y = y0 + pad_y + i * line_h
            x = x0 + pad_x
            if part.startswith(prompt):
                d.text((x, y), prompt, font=font, fill=(142, 255, 193, 255))
                x += probe.textlength(prompt, font=font)
                part = part[len(prompt):]
            d.text((x, y), part, font=font, fill=(236, 244, 239, 255))
            if at is None and (left <= len(line) or i == len(lines) - 1):
                at = (x + probe.textlength(part, font=font), y)
            left -= len(line) + 1
        if cursor and at:
            cx, y = at
            d.rectangle([cx + 2, y + int(size * 0.12), cx + 2 + int(size * 0.5), y + int(size * 1.08)],
                        fill=(142, 255, 193, 220))
        return im

    folder.mkdir(parents=True, exist_ok=True)
    total = sum(len(line) for line in lines) + len(lines) - 1
    if not type_on:
        p = folder / "still.png"
        frame(total, False).save(p)
        return [p]
    # Typed out in at most 1.4 s whatever the length (a long line gets
    # several characters a frame), then the cursor blinks and it holds.
    out = []
    n_frames = max(1, int(FPS * min(1.4, 0.04 * total)))
    n = 0
    for f in range(n_frames + 1):
        shown = len(prompt) + round((total - len(prompt)) * f / n_frames)
        p = folder / f"t{n:04d}.png"
        frame(shown, True).save(p)
        out.append(p)
        n += 1
    for k in range(int(FPS * 0.6)):
        p = folder / f"t{n:04d}.png"
        frame(total, (k // 8) % 2 == 0).save(p)
        out.append(p)
        n += 1
    frame(total, False).save(folder / f"t{n:04d}.png")
    out.append(folder / f"t{n:04d}.png")
    return out


def encode(ff: str, pieces: list[dict], out: Path, w: int, h: int, crf: int) -> float:
    # One input per source file; the pieces are trims of it, so fifty cuts in
    # one recording do not open it fifty times.
    files = []
    for p in pieces:
        if p["src"] not in files:
            files.append(p["src"])
    inputs = []
    for f in files:
        inputs += ["-i", str(f)]
    # The captions: one input per piece that shows one (an image sequence
    # typing it, or the still line), made at this size.
    import hashlib
    import tempfile
    cap_dir = Path(tempfile.mkdtemp(prefix="getcap_"))
    cap_input = {}
    n_inputs = len(files)
    for i, p in enumerate(pieces):
        if not p.get("caption"):
            continue
        key = hashlib.sha1(f"{p['caption']}|{w}|{p.get('type_on')}|{p.get('caption_box')}".encode()).hexdigest()[:10]
        folder = cap_dir / key
        frames = (sorted(folder.glob("*.png")) if folder.is_dir()
                  else caption_frames(p["caption"], w, h, folder, bool(p.get("type_on")), p.get("caption_box")))
        if len(frames) == 1:
            inputs += ["-i", str(frames[0])]
        else:
            inputs += ["-framerate", str(FPS), "-i", str(folder / "t%04d.png")]
        cap_input[i] = n_inputs
        n_inputs += 1
    uses = {f: sum(1 for p in pieces if p["src"] == f) for f in files}
    chains, labels, split_idx = [], [], {f: 0 for f in files}
    for fi, f in enumerate(files):
        if uses[f] > 1:
            chains.append(f"[{fi}:v]split={uses[f]}" + "".join(f"[s{fi}_{k}]" for k in range(uses[f])))
    total = 0.0
    for i, p in enumerate(pieces):
        fi = files.index(p["src"])
        src_label = f"[s{fi}_{split_idx[p['src']]}]" if uses[p["src"]] > 1 else f"[{fi}:v]"
        split_idx[p["src"]] += 1
        crop = ""
        if p.get("crop"):
            cx, cy, cw, ch = (int(v) for v in p["crop"])
            crop = f"crop={cw}:{ch}:{cx}:{cy},"
        chains.append(f"{src_label}trim=start={p['from']:.3f}:end={p['to']:.3f},"
                      f"setpts=(PTS-STARTPTS)/{p['speed']},fps={FPS},{crop}"
                      f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},setsar=1"
                      + (f"[b{i}]" if i in cap_input else f"[v{i}]"))
        if i in cap_input:
            # Trimmed back to the piece: a caption that types for longer than
            # the piece lasts would otherwise hold the picture's last frame.
            dur = (p["to"] - p["from"]) / p["speed"]
            chains.append(f"[b{i}][{cap_input[i]}:v]overlay=0:0:eof_action=repeat:format=auto,"
                          f"trim=duration={dur:.3f},setpts=PTS-STARTPTS,setsar=1[v{i}]")
        labels.append(f"[v{i}]")
        total += (p["to"] - p["from"]) / p["speed"]
    chains.append("".join(labels) + f"concat=n={len(pieces)}:v=1:a=0,"
                  f"fade=t=in:st=0:d={FADE}:color=black,"
                  f"fade=t=out:st={max(0.0, total - FADE):.3f}:d={FADE}:color=black,format=yuv420p[out]")
    script = out.with_suffix(".filter.txt")
    script.write_text(";\n".join(chains), encoding="utf-8")
    cmd = [ff, "-v", "error", "-y", *inputs, "-filter_complex_script", str(script), "-map", "[out]", "-an",
           "-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-profile:v", "high",
           "-g", str(FPS * 2), "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)
    script.unlink(missing_ok=True)
    shutil.rmtree(cap_dir, ignore_errors=True)
    return total


def cut(ff: str, name: str, clip: dict, sources: dict) -> dict:
    pieces = plan(ff, clip, sources)
    OUT.mkdir(parents=True, exist_ok=True)
    total = 0.0
    sizes = {}
    for suffix, (w, h, crf) in SIZES.items():
        mp4 = OUT / f"{name}{suffix}.mp4"
        total = encode(ff, pieces, mp4, w, h, int(clip.get("crf", crf)) + (crf - SIZES[""][2]))
        sizes[suffix or "full"] = mp4.stat().st_size
    poster = OUT / f"{name}.jpg"
    # "poster" is a second of the finished clip; "poster_at" a fraction of it
    # (a recipe written before the clip exists cannot know its length).
    at = float(clip["poster_at"]) * total if "poster_at" in clip else float(clip.get("poster", total / 2))
    subprocess.run([ff, "-v", "error", "-y", "-ss", f"{max(0.0, min(at, total - 0.1)):.3f}",
                    "-i", str(OUT / f"{name}.mp4"), "-frames:v", "1", "-vf", "scale=1600:-2", "-q:v", "3", str(poster)],
                   check=True)
    raw = sum(float(s["to"]) - float(s["from"]) for s in clip["segments"])
    print(f"  {name:<8} {total:5.1f}s (from {raw:5.1f}s of recording, {len(pieces)} pieces)  "
          f"{sizes['full'] / 1e6:4.1f} MB / {sizes['-720'] / 1e6:4.1f} MB")
    return {"name": name, "src": f"/static/video/get/{name}.mp4", "src720": f"/static/video/get/{name}-720.mp4",
            "poster": f"/static/video/get/{name}.jpg", "label": clip.get("label", ""),
            "sub": clip.get("sub", ""),
            "seconds": round(total, 1), "bytes": sizes["full"], "bytes720": sizes["-720"]}


def write_og(poster: Path, og: Path) -> None:
    """The link preview (iMessage, Discord) is the hero clip's poster, cropped
    to the 1200x630 card every service expects."""
    try:
        from PIL import Image
    except ImportError:
        print("  (Pillow missing: og.jpg not updated)")
        return
    og.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(poster) as im:
        im = im.convert("RGB")
        w, h = im.size
        th = int(w * 630 / 1200)
        top = max(0, (h - th) // 2)
        im.crop((0, top, w, top + min(th, h))).resize((1200, 630), Image.LANCZOS).save(og, quality=84, optimize=True)
    print(f"  og.jpg from {poster.name}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("only", nargs="*", help="clip names to cut (default: all)")
    ap.add_argument("--probe", action="store_true", help="print each recording's length and stop")
    ap.add_argument("--still", help="a source key: print where it holds still between --from and --to")
    ap.add_argument("--from", dest="a", type=float, default=0)
    ap.add_argument("--to", dest="b", type=float, default=0)
    ap.add_argument("--spec", default="", help="clip spec (default tools/get_clips.json)")
    ap.add_argument("--out", default="", help="folder to cut into (default static/video/get)")
    ap.add_argument("--og", default="", help="where the link preview goes (default static/img/get/og.jpg)")
    args = ap.parse_args(argv)
    global SPEC, OUT
    if args.spec:
        SPEC = Path(args.spec).resolve()
    if args.out:
        OUT = Path(args.out).resolve()
    og_path = Path(args.og).resolve() if args.og else ROOT / "static" / "img" / "get" / "og.jpg"

    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    ff = ffmpeg()
    if args.probe:
        for key, rel in spec["sources"].items():
            p = ROOT / rel
            print(f"{key:<8} {duration(ff, p) if p.is_file() else 'MISSING':>8}  {rel}")
        return 0
    if args.still:
        src = ROOT / spec["sources"][args.still]
        b = args.b or duration(ff, src)
        for s, e, dark in still_runs(ff, src, args.a, b, HOLD):
            print(f"  {'black' if dark else 'still'} {s:8.2f} -> {e:8.2f}  ({e - s:5.1f}s)")
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT / "clips.json"
    old = {}
    if manifest_path.exists():
        old = {c["name"]: c for c in json.loads(manifest_path.read_text(encoding="utf-8")).get("clips", [])}
    print(f"cutting into {OUT}")
    done = {}
    for name, clip in spec["clips"].items():
        if args.only and name not in args.only:
            if name in old:
                done[name] = old[name]
            continue
        done[name] = cut(ff, name, clip, spec["sources"])
    # Clips the spec no longer names are gone from the page and the folder.
    for stale in set(old) - set(spec["clips"]):
        for f in OUT.glob(f"{stale}*"):
            f.unlink()
    order = [n for n in spec["clips"] if n in done]
    manifest = {"note": "Written by tools/cut_clips.py from tools/get_clips.json. Real recordings of the game.",
                "clips": [done[n] for n in order]}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if "hero" in done and (not args.only or "hero" in args.only):
        write_og(OUT / "hero.jpg", og_path)
    total = sum(c["bytes"] for c in manifest["clips"])
    print(f"total {total / 1e6:.1f} MB at 1080p, {sum(c.get('bytes720', 0) for c in manifest['clips']) / 1e6:.1f} MB at 720p")
    return 0


if __name__ == "__main__":
    sys.exit(main())
