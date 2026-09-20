#!/usr/bin/env python3
"""Scan-driven playtester.

The autoplay harness only ever pressed the three choice buttons, so the SCAN
pipeline — the detector, the hotspot tags, and the object-interaction turn —
went untested by every automated run. This driver plays the game the way a
person does: it grabs the current frame, runs it through /api/detect, picks one
of the returned objects, and commits MOVE TO on it.

MOVE TO is the default. INTERACT ships too now — on stills it is a Moment that
dives to a generated close-up of the object while a same-place turn runs
underneath (see interactEnabled / openInteractMoment in standalone.js), and it
is shelved only under the live renderer, where poking the world model reacts
too weakly to read. So `--plan scan_interact` is worth running: it drives a
turn players do produce.

What it cannot reach from here is the close-up handoff. The dive is client
chrome, so nothing in this harness generates a plate, and the server only holds
the scene render for one when the client says a dive opened (`awaiting_closeup`
— see _arm_interact_plate). That is deliberate: a harness INTERACT must not
wait thirty-five seconds for a picture nobody is drawing. Exercising the handoff
needs a browser.

Every turn produces exactly three frames, in the order a reviewer needs to
judge whether the next generation makes sense:

    1. VIEW      — the frame the player is looking at before acting.
    2. CHOICES   — that same frame with every option called out: the
                   detector's boxes when SCAN ran, or the button slate when
                   it didn't.
    3. SELECTED  — the same options again, but with the one that got
                   committed picked out from the rest.

Turn N+1's VIEW frame is the generation that resulted from turn N's SELECTED
choice, so reading a run kind-by-kind or turn-by-turn both answer the same
question: does what happened next follow from what was picked?

Everything is recorded to a run folder — the three frames per turn, a JSON
transcript, and video. Two kinds of video come out: one flipbook per frame
kind (view / choices / selected), each holding a bare source frame for half a
second so a single track can be watched on its own; and one "review" video
that interleaves all three in the order the run produced them, captions baked
onto every frame, meant to be watched start to finish with nothing else open.

    python playtest_interactive.py --url http://127.0.0.1:5002 --turns 8

Notes on fidelity: the composed action phrases, the `source`/`subject` fields,
and the JPEG capture size/quality all mirror static/js/standalone.js exactly
(commitScanAction / moveActionPhrase / captureScanFrame). If those diverge, the
harness stops testing the thing the client actually does.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).parent.resolve()

# Colors for the annotated frames / GIF overlay.
BOX_COLOR = (0, 255, 170)
PICK_COLOR = (255, 90, 60)
CAPTION_BG = (10, 12, 14)  # GIF pad backdrop only — no more opaque caption strips.
CAPTION_FG = (226, 232, 236)

# Same palette as the live UI's .choice-btn (static/css/standalone.css) — pure
# glowing text, no background plate — so the CHOICES/SELECTED frames read like
# the actual game instead of a debug overlay.
CHOICE_TEXT_COLOR = (234, 255, 242)   # #eafff2
CHOICE_PICK_COLOR = (142, 255, 193)   # #8effc1
CHOICE_DIM_ALPHA = 150                 # unpicked lines once something IS picked

MODE_PRESETS = {
    # Cycle the three choice buttons — no SCAN. Shows world + slate evolution.
    "auto": {
        "plan": ["choice"],
        "title": "Auto playtest (choice evolution)",
        "require_scan": False,
    },
    # SCAN every turn, then MOVE TO on a detected object (rotates targets).
    "scan_move": {
        "plan": ["scan_move"],
        "title": "SCAN → MOVE TO playtest",
        "require_scan": True,
    },
}


# ──────────────────────────────────────────────────────────────────────
# HTTP
# ──────────────────────────────────────────────────────────────────────

class Client:
    def __init__(self, base: str, session_id: str):
        self.base = base.rstrip("/")
        self.session_id = session_id

    def _url(self, path: str) -> str:
        url = self.base + path
        if "session_id=" in url or "session=" in url:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}session_id={urllib.parse.quote(self.session_id)}"

    def get(self, path: str, timeout: int = 30):
        req = urllib.request.Request(self._url(path), method="GET",
                                     headers={"X-Session-Id": self.session_id})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    def post(self, path: str, body: dict, timeout: int = 180):
        payload = dict(body or {})
        payload["session_id"] = self.session_id
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self._url(path), data=data, method="POST",
            headers={"Content-Type": "application/json", "X-Session-Id": self.session_id},
        )
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode()), time.time() - t0

    def fetch_bytes(self, path: str, timeout: int = 30) -> bytes | None:
        """Scene image URLs already carry their own ?session= param when the
        frame belongs to a non-default session, so don't append another one."""
        url = path if path.startswith("http") else self.base + path
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.read()
        except Exception:
            return None


# ──────────────────────────────────────────────────────────────────────
# Feed helpers
# ──────────────────────────────────────────────────────────────────────

def latest_prompt(items: list) -> dict | None:
    for it in reversed(items or []):
        if it.get("type") == "player_choice_prompt":
            return it
    return None


def latest_image_url(items: list) -> str | None:
    for it in reversed(items or []):
        if it.get("image_url"):
            return it["image_url"]
    return None


def latest_sequence(items: list) -> dict | None:
    """The newest beat's flipbook frames, if this turn drew a sequence.

    A flipbook turn's `image_url` is the LAST frame of its sequence — the still
    the action ended on — so a harness that only reads image_url captures the
    outcome of every turn and none of the motion. This is the rest of it.
    """
    for it in reversed(items or []):
        seq = (it.get("metadata") or {}).get("sequence")
        if seq and (seq.get("frames") or []):
            return seq
    return None


def wait_for_turn(client: Client, since_id: int, timeout_s: int = 180):
    start = time.time()
    while time.time() - start < timeout_s:
        items = client.get(f"/api/feed?since_id={since_id}")
        types = {i.get("type") for i in items}
        # `game_over` ships in the SAME batch as its own player_choice_prompt
        # (the "Restart Simulation" button, from _structure_choices_for_feed
        # in engine.py) — so both types are present together and death must
        # be checked FIRST, or every death in a run reads as an ordinary
        # resolved turn and the harness keeps "playing" a dead session.
        if "game_over" in types:
            return items, time.time() - start, "death"
        if "player_choice_prompt" in types:
            return items, time.time() - start, "resolved"
        if "error_event" in types and "narrative_event" not in types:
            return items, time.time() - start, "error"
        time.sleep(0.5)
    return client.get(f"/api/feed?since_id={since_id}"), time.time() - start, "timeout"


def world_tick(client: Client, timeout: int = 30) -> dict:
    """Ask the server for one ambient simulation step.

    This is what a real browser does while the player sits at a decision point:
    the world keeps changing between actions instead of holding the prompt from
    the last choice. No harness ever called it, so the whole drift path — and
    the phase-gated lighting rules written into its prompt — went unexercised.

    Every refusal comes back as a named `skipped` reason rather than an error
    (see engine.world_drift_tick), so a run can report exactly why the world
    stayed still: "disabled" means the server was started without WORLD_DRIFT=1.

    An ACCEPTED tick returns {"ok": true, "queued": true} and no beat — the LLM
    call runs off the request thread and the beat lands later as a `world_drift`
    feed item. So the beats have to be counted out of the feed (see
    drift_beats_in), not read off this response.
    """
    try:
        resp, _ = client.post("/api/world_tick", {}, timeout=timeout)
        return resp or {}
    except Exception as e:
        return {"ok": False, "skipped": f"error: {e}"}


def drift_beats_in(items: list) -> list[str]:
    """The ambient beats the server published into this slice of the feed.

    A beat queued at the end of turn N is published a few seconds later, so it
    shows up in turn N+1's slice. Counting them across the whole run is what
    tells us the drift path actually ran rather than merely being accepted.
    """
    return [(i.get("content") or "").strip() for i in (items or [])
            if i.get("type") == "world_drift" and (i.get("content") or "").strip()]


def wait_for_scene_image(client: Client, since_id: int, items: list,
                         grace_s: float = 25.0, prev_url: str | None = None):
    """Scene rendering runs off the turn's critical path — the choice prompt is
    served before the picture lands, so a capture taken at the prompt records a
    frame that is still being painted as absent.

    The prompt carries the CURRENT scene image, which until the new frame lands
    is still the PREVIOUS turn's picture. Waiting on "any image_url" therefore
    returned immediately and this harness scanned last turn's frame — the whole
    run would detect objects in a scene the player had already left."""
    if latest_image_url(items) not in (None, prev_url):
        return items
    deadline = time.time() + max(0.0, grace_s)
    while time.time() < deadline:
        time.sleep(1.0)
        fresh = client.get(f"/api/feed?since_id={since_id}")
        if latest_image_url(fresh) not in (None, prev_url):
            return fresh
        items = fresh or items
    return items


# ──────────────────────────────────────────────────────────────────────
# Frame capture + detection (mirrors captureScanFrame in standalone.js)
# ──────────────────────────────────────────────────────────────────────

def to_scan_frame(png_bytes: bytes, width: int = 640, quality: int = 72) -> str:
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    if img.width > width:
        img = img.resize((width, max(1, round(img.height * width / img.width))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def detect(client: Client, frame_data_url: str, timeout: int = 60) -> tuple[list, str | None, bool]:
    # purpose: "scan" mirrors triggerScan — it asks the server to cache these
    # labels for the turn, which is what grounds the consequence in what is on
    # screen. Without it this harness would exercise a different code path than
    # the SCAN button it exists to test.
    #
    # The third return value distinguishes "the detector found nothing" from
    # "the detector found things but engine.api_detect's anti-loop gate
    # deliberately withheld them" (streak stuck, or the player is
    # DETECT_HUNTED and should be reading the flee option, not tapping
    # scenery). Both look identical on the wire otherwise — an empty
    # `objects` list either way — which used to make this gate's own correct
    # behavior read as a detector regression in every summary that measured
    # hit rate against it.
    try:
        resp, _ = client.post("/api/detect",
                              {"frame": frame_data_url, "purpose": "scan"},
                              timeout=timeout)
        return (list(resp.get("objects") or []), None,
                bool(resp.get("anti_loop_suppressed")))
    except urllib.error.HTTPError as e:
        try:
            return [], (json.loads(e.read().decode()) or {}).get("error", str(e)), False
        except Exception:
            return [], f"HTTP {e.code}", False
    except Exception as e:
        return [], str(e), False


def labels_present(labels: list, text: str) -> list:
    """Which of the detected labels the turn's writing actually mentions.

    The engine asks the consequence call to weave two or more on-screen things
    into every beat (engine.onscreen_directive); this is how we see whether it
    did. Matching is the same loose word test the permanence check uses — a
    label like "blast door" counts when any word over two characters appears,
    because the prose rarely repeats the detector's exact noun phrase. That
    makes this a signal, not a proof: read it as a trend across a run.
    """
    hay = (text or "").lower()
    hits = []
    for label in labels or []:
        words = [w for w in re.findall(r"[a-z]+", str(label).lower()) if len(w) > 2]
        if words and any(w in hay for w in words):
            hits.append(str(label))
    return hits


# ──────────────────────────────────────────────────────────────────────
# Action selection (mirrors commitScanAction / moveActionPhrase)
# ──────────────────────────────────────────────────────────────────────

def interact_phrase(label: str) -> str:
    # Mirrors the client's own INTERACT phrase (SCAN_ACTIONS in standalone.js).
    # The verb is live on stills, so this drives a turn players really produce —
    # minus the close-up dive, which is chrome this harness has no browser for.
    return f"Interact with the {label}."


def move_phrase(label: str) -> str:
    # Mirrors moveActionPhrase in standalone.js: ONE phrase for every object.
    # It used to pick between an "Enter the X" and a "Walk over to the X"
    # phrasing depending on a curated word-list match, because the SERVER
    # decided hard-cut-vs-not by pattern-matching that text. MOVE is now an
    # unconditional hard cut regardless of wording (see is_move in
    # advance_turn_image_fast), so there's nothing left for the phrasing to
    # negotiate.
    return f"Move to the {label}."


def pick_object(objects: list, turn_idx: int) -> dict | None:
    """Choose which hotspot to 'click'. Rotates so a run touches a spread of
    objects rather than hammering whatever the detector happens to rank first."""
    if not objects:
        return None
    return objects[turn_idx % len(objects)]


def plan_action(kind: str, objects: list, choices: list, turn_idx: int) -> dict:
    """Resolve the planned action into a concrete /api/choose payload. Scan
    actions degrade to a plain choice press when the detector found nothing,
    so an empty scan never stalls the run."""
    if kind in ("scan_interact", "scan_move") and objects:
        obj = pick_object(objects, turn_idx)
        label = obj.get("label") or "it"
        phrase = move_phrase(label) if kind == "scan_move" else interact_phrase(label)
        return {
            "kind": kind,
            "choice": phrase,
            "source": kind,
            "subject": label,
            "object": obj,
            "degraded": False,
        }
    fallback = kind in ("scan_interact", "scan_move")
    text = choices[turn_idx % len(choices)] if choices else "Look around"
    return {
        "kind": "choice",
        "choice": text,
        "source": None,
        "subject": None,
        "object": None,
        "degraded": fallback,
    }


# ──────────────────────────────────────────────────────────────────────
# Rendering: annotated frames + GIF
# ──────────────────────────────────────────────────────────────────────

def _font(size: int):
    for name in ("arialbd.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _boxes(img: Image.Image, objects: list, picked_label: str | None) -> Image.Image:
    out = img.convert("RGB").copy()
    draw = ImageDraw.Draw(out)
    font = _font(max(11, out.width // 46))
    for obj in objects:
        try:
            cx, cy = float(obj.get("cx", 0.5)), float(obj.get("cy", 0.5))
            w, h = float(obj.get("w", 0.1)), float(obj.get("h", 0.1))
        except (TypeError, ValueError):
            continue
        label = str(obj.get("label") or "?")
        is_pick = picked_label is not None and label == picked_label
        color = PICK_COLOR if is_pick else BOX_COLOR
        x0 = max(0, (cx - w / 2) * out.width)
        y0 = max(0, (cy - h / 2) * out.height)
        x1 = min(out.width, (cx + w / 2) * out.width)
        y1 = min(out.height, (cy + h / 2) * out.height)
        draw.rectangle([x0, y0, x1, y1], outline=color, width=3 if is_pick else 2)
        tag = label.upper() if is_pick else label
        tw = draw.textlength(tag, font=font)
        th = font.size + 6
        ty = max(0, y0 - th)
        draw.rectangle([x0, ty, x0 + tw + 10, ty + th], fill=color)
        draw.text((x0 + 5, ty + 3), tag, fill=(0, 0, 0), font=font)
    return out


def _scaled_to_width(img: Image.Image, width: int) -> Image.Image:
    """Resize to the GIF/review canvas width, nothing else. What used to be
    the first half of `_captioned` before the caption strip it built got
    cut — every frame in the walkthrough is presentation-ready already
    (raw VIEW, or CHOICES/SELECTED with `_boxes`/`_choice_overlay` baked
    in), so scaling is the only thing left to do here."""
    scaled = img.convert("RGB")
    if scaled.width != width:
        scaled = scaled.resize((width, max(1, round(scaled.height * width / scaled.width))),
                               Image.LANCZOS)
    return scaled


def _glow_text(layer: Image.Image, xy: tuple[float, float], text: str, font,
               fill: tuple[int, int, int, int], glow_radius: int = 3) -> None:
    """Draw text the way the live UI's .choice-btn does: no background plate,
    just a soft dark halo (its CSS text-shadow) so light text stays readable
    over any frame, then the crisp glyphs on top."""
    glow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).text(xy, text, font=font, fill=(0, 0, 0, 235), anchor="mm")
    layer.alpha_composite(glow.filter(ImageFilter.GaussianBlur(glow_radius)))
    ImageDraw.Draw(layer).text(xy, text, font=font, fill=fill, anchor="mm")


def _choice_overlay(img: Image.Image, choices: list, picked: str | None = None) -> Image.Image:
    """Choices/selected artwork for a turn with nothing to box — a plain
    button press. Mirrors what `_boxes` does for a scanned turn (same canvas
    size, the taken option called out from the rest) but for text choices,
    rendered with the SAME "no opaque box, glowing text" language the real
    #choices-container uses (static/css/standalone.css .choice-btn) instead
    of a solid strip pasted over the frame — so a plain-choice turn looks
    like the game, not a debug tool.

    Nothing is picked on the CHOICES frame (plain white glow, matching the
    live UI before a tap); the SELECTED frame re-renders the identical list
    with the taken line in the game's mint accent and the rest dimmed.
    """
    out = img.convert("RGBA").copy()
    overlay = Image.new("RGBA", out.size, (0, 0, 0, 0))
    lines = choices or ["(no choices offered)"]
    font = _font(max(14, out.width // 30))
    line_h = int(font.size * 1.7)
    total_h = line_h * len(lines)
    y = out.height - total_h - max(16, int(out.height * 0.05)) + line_h / 2
    cx = out.width / 2
    for text in lines:
        is_pick = picked is not None and text == picked
        if picked is None:
            fill = (*CHOICE_TEXT_COLOR, 255)
        elif is_pick:
            fill = (*CHOICE_PICK_COLOR, 255)
        else:
            fill = (*CHOICE_TEXT_COLOR, CHOICE_DIM_ALPHA)
        label = ("\u203a " if is_pick else "") + str(text)
        _glow_text(overlay, (cx, y), label, font, fill, glow_radius=4 if is_pick else 3)
        y += line_h
    return Image.alpha_composite(out, overlay).convert("RGB")
    return canvas


FRAME_KINDS = ("view", "choices", "selected")

# Slideshow of stills, watched fullscreen. 30 fps with a keyframe on every
# page so scrubbing lands on a source frame. Cap at 1080p so a 2K plate is
# not thrown away, but we never upscale — a smaller render stays native.
VIDEO_FPS = 30
VIDEO_MAX_WIDTH = 1920


def _h264_encode_args(keyint: int | None = None) -> list[str]:
    """Offline still-slideshow encode. Baseline / veryfast / CRF 20 looked
    like a low-bitrate stream once Watch stretched it to the monitor."""
    args = [
        "-c:v", "libx264",
        "-profile:v", "high",
        "-level", "4.1",
        "-preset", "medium",
        "-tune", "stillimage",
        "-crf", "16",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]
    if keyint:
        args += [
            "-g", str(keyint),
            "-keyint_min", str(keyint),
            "-sc_threshold", "0",
        ]
    return args


def _created_at(path: Path) -> float:
    """Creation time where the platform records one, else the closest stand-in.

    Windows puts real creation time in st_ctime and macOS in st_birthtime; on
    Linux st_ctime is inode-change time, which for write-once run frames is
    still the moment the file appeared.
    """
    st = path.stat()
    return getattr(st, "st_birthtime", None) or st.st_ctime


def collect_frame_tracks(frames_dir: Path, created_at=_created_at) -> dict[str, list[Path]]:
    """Split the run's PNGs into one ordered track per frame kind.

    Ordered by creation time so the flipbook always plays in the order the run
    actually produced the frames; the turn number in the filename only breaks
    ties, which same-second writes on fast turns make common. Sorting by name
    alone would put turn 10 before turn 2.

    ``created_at`` is injectable because no portable API can *set* a file's
    creation time, so it is the only way to test the ordering directly.
    """
    found: dict[str, list[tuple[float, int, Path]]] = {kind: [] for kind in FRAME_KINDS}
    for p in frames_dir.glob("turn_*_*.png"):
        m = re.match(r"turn_(\d+)_([a-z]+)\.png$", p.name)
        if m and m.group(2) in found:
            found[m.group(2)].append((created_at(p), int(m.group(1)), p))
    return {kind: [p for _, _, p in sorted(entries)] for kind, entries in found.items()}


def _ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return shutil.which("ffmpeg")


def _video_canvas(frame_paths: list[Path]) -> tuple[int, int]:
    """A single even-sided canvas every frame in the track fits inside.

    H.264 needs even dimensions, and a track whose frames differ in size (a
    provider that switched aspect ratio mid-run) would otherwise fail the
    encode outright, so the largest box wins and smaller frames get padded.
    """
    width = height = 0
    for p in frame_paths:
        try:
            with Image.open(p) as img:
                width, height = max(width, img.width), max(height, img.height)
        except Exception:
            continue
    if not width or not height:
        return 0, 0
    if width > VIDEO_MAX_WIDTH:
        height = round(height * VIDEO_MAX_WIDTH / width)
        width = VIDEO_MAX_WIDTH
    return width - (width % 2), height - (height % 2)


def build_flipbook(frame_paths: list[Path], out_path: Path, pause_s: float) -> Path | None:
    """One MP4 holding each source PNG for ``pause_s`` seconds.

    Encoded as constant-frame-rate High-profile H.264 / yuv420p with the moov
    atom up front: the concat demuxer's native output is variable frame rate,
    which QuickTime, Windows' Media Player, and most browser <video> tags
    either refuse or scrub incorrectly.
    """
    if not frame_paths or pause_s <= 0:
        return None
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        print("[video] ffmpeg not found — skipping MP4 export", flush=True)
        return None
    width, height = _video_canvas(frame_paths)
    if not width or not height:
        print(f"[video] no readable frames for {out_path.name} — skipping", flush=True)
        return None

    concat_list = out_path.parent / f"_concat_{out_path.stem}.txt"
    lines: list[str] = []
    for p in frame_paths:
        path = str(p.resolve()).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{path}'")
        lines.append(f"duration {pause_s}")
    # The concat demuxer ignores the final entry's duration, so the last frame
    # is repeated to give it the same time on screen as every other page.
    last = str(frame_paths[-1].resolve()).replace("\\", "/").replace("'", "'\\''")
    lines.append(f"file '{last}'")
    concat_list.write_text("\n".join(lines), encoding="utf-8")

    keyint = max(1, round(VIDEO_FPS * pause_s))
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={VIDEO_FPS},format=yuv420p"
    )
    try:
        proc = subprocess.run(
            [
                ffmpeg, "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list),
                "-an",
                "-vf", vf,
                "-r", str(VIDEO_FPS),
                *_h264_encode_args(keyint),
                str(out_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            tail = (proc.stderr or "")[-800:]
            print(f"[video] ffmpeg failed for {out_path.name}: {tail}", flush=True)
            return None
        return out_path
    finally:
        try:
            concat_list.unlink()
        except OSError:
            pass


def build_flipbooks(frames_dir: Path, out_dir: Path, pause_s: float) -> dict[str, dict]:
    """One flipbook per frame kind, so each track can be watched on its own —
    e.g. every CHOICES frame back to back, to see how the detector's hit rate
    trended across a run. `build_review_video` below is for the other kind of
    reading: turn-by-turn causality instead of one aspect across the whole run.
    """
    videos: dict[str, dict] = {}
    for kind, paths in collect_frame_tracks(frames_dir).items():
        if not paths:
            continue
        out_path = build_flipbook(paths, out_dir / f"playtest_{kind}.mp4", pause_s)
        videos[kind] = {
            "path": str(out_path) if out_path else None,
            "file": out_path.name if out_path else None,
            "frames": len(paths),
            "seconds_per_frame": pause_s,
        }
    return videos


def build_review_video(frames: list[tuple[Image.Image, int]], out_path: Path) -> Path | None:
    """The walkthrough: VIEW, CHOICES, SELECTED, VIEW, CHOICES, SELECTED... in
    the order the run actually produced them. Every frame is presentation
    art already — the raw VIEW render, or CHOICES/SELECTED with `_boxes`/
    `_choice_overlay` baked in — so this is just the interleaved, full-size
    sibling of the per-kind flipbooks (`build_flipbooks`, one track each) and
    the GIF (the same sequence, small and lossy)."""
    if not frames:
        return None
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        print("[video] ffmpeg not found — skipping review MP4", flush=True)
        return None
    widest = max(img.width for img, _ in frames)
    tallest = max(img.height for img, _ in frames)
    width, height = widest - (widest % 2), tallest - (tallest % 2)
    if not width or not height:
        return None

    tmp_dir = out_path.parent / f"_review_frames_{out_path.stem}"
    tmp_dir.mkdir(exist_ok=True)
    concat_list = out_path.parent / f"_concat_{out_path.stem}.txt"
    try:
        paths: list[Path] = []
        lines: list[str] = []
        for i, (img, ms) in enumerate(frames):
            p = tmp_dir / f"f{i:04d}.png"
            _pad_to(img.convert("RGB"), width, height).save(p)
            paths.append(p)
            safe = str(p.resolve()).replace("\\", "/").replace("'", "'\\''")
            lines.append(f"file '{safe}'")
            lines.append(f"duration {max(0.1, ms / 1000)}")
        # concat ignores the last entry's duration, same as build_flipbook.
        last = str(paths[-1].resolve()).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{last}'")
        concat_list.write_text("\n".join(lines), encoding="utf-8")

        proc = subprocess.run(
            [
                ffmpeg, "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list),
                "-an",
                "-vf", f"fps={VIDEO_FPS},format=yuv420p",
                "-r", str(VIDEO_FPS),
                *_h264_encode_args(),
                str(out_path),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            tail = (proc.stderr or "")[-800:]
            print(f"[video] ffmpeg failed for {out_path.name}: {tail}", flush=True)
            return None
        return out_path
    finally:
        try:
            concat_list.unlink()
        except OSError:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)


def build_gif(frames: list[tuple[Image.Image, int]], out_path: Path, width: int) -> Path | None:
    """frames: (image, duration_ms). GIF has one global canvas size, so every
    frame is padded to the tallest one — caption strips differ in height."""
    if not frames:
        return None
    tallest = max(img.height for img, _ in frames)
    sized = [(_pad_to(img, width, tallest), ms) for img, ms in frames]
    first, rest = sized[0][0], [f for f, _ in sized[1:]]
    first.save(
        out_path,
        save_all=True,
        append_images=rest,
        duration=[ms for _, ms in sized],
        loop=0,
        optimize=True,
    )
    return out_path


def _pad_to(img: Image.Image, width: int, height: int) -> Image.Image:
    if img.width == width and img.height == height:
        return img
    canvas = Image.new("RGB", (width, height), CAPTION_BG)
    canvas.paste(img, (0, 0))
    return canvas


# ──────────────────────────────────────────────────────────────────────
# The run
# ──────────────────────────────────────────────────────────────────────

def _save_frame_atomic(img: Image.Image, path: Path) -> None:
    """Save a frame the same way live.json is written: to a temp file next
    to the real name, then rename. render_jobs.py polls the frames directory
    from another process while a run is live, and Image.save() writes the
    real filename directly — a poller that lists/fetches in that window sees
    a PNG that only exists in a partially-written state (a torn read: the
    top rows decode, the rest is missing or corrupt). The rename is what
    makes the file appear all at once."""
    tmp = path.with_name(path.name + ".tmp")
    # PIL infers format from the filename extension when none is given, and
    # ".tmp" isn't one it knows — every frame written here is a PNG, so say
    # so explicitly rather than let the temp name pick the wrong format.
    img.save(tmp, format="PNG")
    tmp.replace(path)


def _save_sequence_frames(client, sequence: dict | None, frames_dir: Path,
                          turn_no: int) -> list[Path]:
    """Save a flipbook turn's in-between frames beside its VIEW frame.

    The VIEW frame is the still the turn ended on, which is everything a single
    image can say about it. These are the frames that got there, fetched as the
    lossless PNGs the engine split the grid into — no GIF anywhere in the path,
    because the point of splitting a high-resolution grid is the resolution.

    Named `turn_NN_seq_MM.png` so the per-kind flipbook builders, which match
    `turn_N_<kind>.png`, leave them alone.
    """
    urls = [u for u in ((sequence or {}).get("frames") or []) if u]
    if len(urls) < 2:
        return []
    saved: list[Path] = []
    for i, url in enumerate(urls, 1):
        try:
            png = client.fetch_bytes(url)
        except Exception:
            png = None
        if not png:
            continue
        path = frames_dir / f"turn_{turn_no:02d}_seq_{i:02d}.png"
        _save_bytes_atomic(png, path)
        saved.append(path)
    return saved


def _save_bytes_atomic(data: bytes, path: Path) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _label_of(value):
    """A detection is a dict. The live panel must never see one raw —
    ``String({label: 'barrel'})`` in JS is ``[object Object]``."""
    if value is None or value is False:
        return None
    if isinstance(value, dict):
        for key in ("label", "subject", "text", "name", "choice"):
            inner = _label_of(value.get(key))
            if inner:
                return inner
        return None
    if isinstance(value, (list, tuple)):
        parts = [p for p in (_label_of(v) for v in value) if p]
        return ", ".join(parts) if parts else None
    text = str(value).strip()
    if not text or text == "[object Object]":
        return None
    return text


def _labels_of(values) -> list:
    if not values:
        return []
    if isinstance(values, dict):
        values = [values]
    out = []
    for v in values:
        lab = _label_of(v)
        if lab:
            out.append(lab)
    return out


def _box_of(value) -> dict | None:
    """Normalized detector box for the live overlay. Labels stay on
    ``detections``; this sidecar is the only place geometry is allowed
    so the ticker never stringifies a raw dict."""
    if not isinstance(value, dict):
        return None
    label = _label_of(value)
    if not label or not any(k in value for k in ("cx", "cy", "w", "h")):
        return None
    try:
        cx, cy = float(value.get("cx", 0.5)), float(value.get("cy", 0.5))
        w, h = float(value.get("w", 0.1)), float(value.get("h", 0.1))
    except (TypeError, ValueError):
        return None
    return {
        "label": label,
        "cx": round(cx, 4),
        "cy": round(cy, 4),
        "w": round(max(0.01, w), 4),
        "h": round(max(0.01, h), 4),
    }


def _boxes_of(values) -> list:
    if not values:
        return []
    if isinstance(values, dict):
        values = [values]
    out = []
    for v in values:
        box = _box_of(v)
        if box:
            out.append(box)
    return out


def _beat_from_turn(t: dict) -> dict:
    """The slim record the live panel reads — enough to draw a timeline
    dot, expand it, and paint scan boxes on the stage. Human-facing
    fields stay strings (or lists of strings); ``boxes`` is the
    geometry sidecar so the overlay can draw before the next frame."""
    labels = t.get("detection_labels")
    if labels is None:
        labels = t.get("detections")
    box_src = t.get("boxes")
    if box_src is None:
        box_src = t.get("detections")
    pick = _box_of(t.get("selected_object")) or _box_of(t.get("pick"))
    return {
        "turn": t.get("turn"),
        "kind": t.get("action_kind") or t.get("kind"),
        "phase": t.get("phase") or ("resolved" if t.get("status") and t.get("status") != "pending"
                                   else "pending"),
        "choice": _label_of(t.get("choice_text") or t.get("choice")),
        "subject": _label_of(t.get("selected_object")) or _label_of(t.get("subject")),
        "narrative": t.get("narrative") or "",
        # The scene descriptor that produced this turn's frame. Watch's live
        # film re-steers the world model with it as each still lands (the still
        # itself is the real guide image; this is the text steer alongside it).
        "image_prompt": t.get("image_prompt") or "",
        "choices": _labels_of(t.get("prev_choices") or t.get("choices") or []),
        "next_choices": _labels_of(t.get("new_choices") or t.get("next_choices") or []),
        "detections": _labels_of(labels),
        "boxes": _boxes_of(box_src),
        "pick": pick,
        "status": t.get("status") or "pending",
    }


def _write_live_beats(out_dir: Path, turns: list, pending: dict | None = None) -> None:
    """What's happening right now, including a turn that hasn't resolved yet.

    The panel used to only see a beat after the next frame landed, which
    made the feed sit one turn behind the picture. Writing a pending beat
    as soon as a scan or choice is committed means the timeline can show
    SCAN / TOOK / DRAWING while the image is still coming.
    """
    beats = [_beat_from_turn(t) for t in turns]
    if pending:
        beats.append(_beat_from_turn(pending))
    try:
        tmp = out_dir / ".live.json.tmp"
        tmp.write_text(json.dumps({"beats": beats}), encoding="utf-8")
        tmp.replace(out_dir / "live.json")
    except OSError:
        pass  # the feed is a nicety; never let it break the run itself


def play(args) -> dict:
    client = Client(args.url, args.session)
    out_dir = Path(args.out).resolve()
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    # A cooperative stop: the supervisor (render_jobs.py) touches this instead
    # of killing the process, so a "stop" still falls through to the finalize
    # code below with whatever turns landed — real frames, a real gif and
    # videos — rather than being cut off before any of that gets written.
    stop_path = out_dir / ".stop"
    stop_path.unlink(missing_ok=True)

    plan = [p.strip() for p in args.plan.split(",") if p.strip()]
    gif_frames: list[tuple[Image.Image, int]] = []

    status = client.get("/api/status")
    try:
        health = client.get("/api/health")
    except Exception:
        health = {}
    images_on = bool(status.get("image_enabled"))

    run = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "url": client.base,
        "session_id": args.session,
        "mode": getattr(args, "mode", "custom"),
        "mode_title": getattr(args, "mode_title", "Custom plan"),
        "require_scan": bool(getattr(args, "require_scan", False)),
        "plan": plan,
        "turns_requested": args.turns,
        "images_enabled": images_on,
        "backend": status.get("backend"),
        "image_provider": status.get("image_provider"),
        "detect_backend": ((health.get("detect") or {}).get("backend")),
        "detect_local_available": bool(((health.get("detect") or {}).get("local") or {}).get("available")),
        "world_drift_requested": bool(getattr(args, "world_drift", False)),
        "turns": [],
    }

    # With images off (mock backend) there is no generated frame to scan, so
    # fall back to a bundled still. Without this the SCAN path — the whole
    # point of this harness — would be skipped on every offline run.
    fallback_frame = None
    if args.fallback_frame:
        fb = (ROOT / args.fallback_frame) if not Path(args.fallback_frame).is_absolute() else Path(args.fallback_frame)
        if fb.exists():
            fallback_frame = fb.read_bytes()
            run["fallback_frame"] = str(fb)

    reset_items, reset_elapsed = client.post("/api/reset", {})
    # An authored opening (or the level's approach montage) parks the choice
    # slate and sets `experience_cutscene_id`, and /api/choose answers 409
    # `cutscene_playing` while it is set. The browser plays the shots and then
    # POSTs /api/cutscene/complete; no headless driver in this repo did, so a
    # run against an Experience that starts on a Cutscene opened with no slate
    # at all and degraded straight to a typed action on turn one.
    if any((i or {}).get("type") == "cutscene" for i in (reset_items or [])):
        print("  opening cutscene — completing it as the client would")
        try:
            client.post("/api/cutscene/complete", {})
        except Exception as exc:
            print(f"  cutscene complete failed: {exc}")
        reset_items = client.get("/api/feed?since_id=0")
    if images_on:
        reset_items = wait_for_scene_image(client, 0, reset_items, args.image_grace)
    prompt = latest_prompt(reset_items)
    choices = [c.get("text") for c in ((prompt or {}).get("choices") or [])]
    # The prompt that rendered the opening still lives in server state, not
    # the feed batch /api/reset returns — one extra status call to fetch it.
    intro_image_prompt = ""
    if images_on:
        try:
            intro_image_prompt = client.get("/api/status").get("current_image_prompt", "") or ""
        except Exception:
            intro_image_prompt = ""
    run["reset"] = {
        "elapsed_s": round(reset_elapsed, 2),
        "intro_choices": choices,
        "narratives": [i.get("content") for i in reset_items if i.get("type") == "narrative_event"],
        "scene_image_url": latest_image_url(reset_items),
        "image_prompt": intro_image_prompt,
    }
    last_id = max((i.get("id", 0) for i in reset_items), default=0)
    scene_url = latest_image_url(reset_items)
    # Tracked alongside scene_url, and for the same reason: the frame we capture
    # at the top of a turn is the PREVIOUS turn's render, so its motion has to
    # be carried forward the same way its still is.
    scene_sequence = latest_sequence(reset_items)
    for n in range(args.turns):
        if stop_path.exists():
            print(f"  stop requested \u2014 saving {len(run['turns'])} turn(s) done so far",
                  flush=True)
            break
        kind = plan[n % len(plan)]
        turn_no = n + 1
        print(f"  turn {turn_no}/{args.turns} [{kind}] ...", flush=True)
        # Tell the panel this turn has started *before* the slow bits, so
        # the timeline isn't sitting on last turn's story while we scan.
        _write_live_beats(out_dir, run["turns"], pending={
            "turn": turn_no, "action_kind": kind, "phase": "scanning" if kind.startswith("scan") else "choosing",
            "prev_choices": choices, "status": "pending",
        })

        # 1. VIEW — capture the frame the player is looking at. This is the
        # generation the PREVIOUS turn's SELECTED choice produced (or the
        # opening scene, for turn 1). Shown clean, exactly as rendered — no
        # overlay belongs on this one; it's the "what did the model draw"
        # frame the CHOICES/SELECTED frames right after it get judged against.
        png = client.fetch_bytes(scene_url) if scene_url else None
        frame_source = "scene"
        if png is None and fallback_frame is not None:
            png, frame_source = fallback_frame, "fallback_still"
        view_path = choices_path = selected_path = None
        seq_paths: list[Path] = []
        if png:
            view_path = frames_dir / f"turn_{turn_no:02d}_view.png"
            try:
                _save_frame_atomic(Image.open(io.BytesIO(png)).convert("RGB"), view_path)
            except Exception:
                _save_bytes_atomic(png, view_path)
            seq_paths = _save_sequence_frames(client, scene_sequence,
                                              frames_dir, turn_no)
            if seq_paths:
                print(f"    flipbook: {len(seq_paths)} frames", flush=True)
            if args.gif or args.video:
                gif_frames.append((
                    _scaled_to_width(Image.open(io.BytesIO(png)).convert("RGB"), args.gif_width),
                    args.gif_ms,
                ))

        # 2. SCAN it. A frame we never got is recorded as an explicit error
        # rather than an empty result — "the capture failed" and "the detector
        # saw nothing" have to stay distinguishable, or the run silently
        # reports empty scans it never actually performed.
        objects, detect_error = [], None
        detect_elapsed = 0.0
        anti_loop_suppressed = False
        if kind in ("scan_interact", "scan_move"):
            if not png:
                detect_error = "no frame to scan"
            else:
                t0 = time.time()
                objects, detect_error, anti_loop_suppressed = detect(
                    client, to_scan_frame(png), timeout=args.detect_timeout
                )
                detect_elapsed = round(time.time() - t0, 2)
            # Boxes land the moment SCAN returns — don't wait for CHOICES
            # PNG or the next generation. The panel plays boxes → pick
            # off this beat while the still on stage is still the VIEW.
            if objects:
                _write_live_beats(out_dir, run["turns"], pending={
                    "turn": turn_no, "action_kind": kind,
                    "detections": objects,
                    "detection_labels": [o.get("label") for o in objects],
                    "prev_choices": choices, "phase": "scanned", "status": "pending",
                })

        # 3. Select an object and commit the action.
        action = plan_action(kind, objects, choices, n)
        _write_live_beats(out_dir, run["turns"], pending={
            "turn": turn_no,
            "action_kind": action["kind"],
            "choice_text": action["choice"],
            "subject": action.get("subject"),
            "selected_object": action.get("object"),
            "detections": objects,
            "detection_labels": [o.get("label") for o in objects],
            "prev_choices": choices,
            "phase": "chosen",
            "status": "pending",
        })

        # 4. CHOICES / SELECTED — what was on offer, and which one got taken.
        # Boxed when SCAN ran and found something (the taken object's box
        # goes to PICK_COLOR on the SELECTED frame — that IS its "selected"
        # state, no extra text needed); a floating glow-text choice overlay
        # otherwise, styled like the live game's own choice buttons rather
        # than a debug caption, with the taken line in the mint accent on
        # SELECTED. Either way every turn gets both frames, not just scan ones.
        if png:
            base = Image.open(io.BytesIO(png))
            if objects:
                choices_img = _boxes(base, objects, None)
                selected_img = _boxes(base, objects, action.get("subject"))
            else:
                choices_img = _choice_overlay(base, choices, None)
                selected_img = _choice_overlay(base, choices, action.get("choice"))
            choices_path = frames_dir / f"turn_{turn_no:02d}_choices.png"
            selected_path = frames_dir / f"turn_{turn_no:02d}_selected.png"
            _save_frame_atomic(choices_img, choices_path)
            _save_frame_atomic(selected_img, selected_path)
            if args.gif or args.video:
                gif_frames.append((_scaled_to_width(choices_img, args.gif_width), args.gif_ms))
                gif_frames.append((_scaled_to_width(selected_img, args.gif_width), args.gif_ms))

        since = last_id
        choose_error = None
        # Mirror standalone.js makeChoice: the frame on screen rides as
        # act_frame so ingest + live_capture fire. Without this the harness
        # never hits the path a real MOVE TO uses.
        act_frame = None
        if png:
            try:
                act_frame = to_scan_frame(png)
            except Exception:
                act_frame = None
        try:
            _, choose_elapsed = client.post("/api/choose", {
                "choice": action["choice"],
                "context_item_id": last_id,
                "source": action["source"],
                "subject": action["subject"],
                "act_frame": act_frame,
            }, timeout=args.turn_timeout)
        except Exception as e:
            choose_elapsed, choose_error = 0.0, str(e)

        _write_live_beats(out_dir, run["turns"], pending={
            "turn": turn_no,
            "action_kind": action["kind"],
            "choice_text": action["choice"],
            "subject": action.get("subject"),
            "selected_object": action.get("object"),
            "detections": objects,
            "detection_labels": [o.get("label") for o in objects],
            "prev_choices": choices,
            "phase": "waiting",
            "status": "pending",
        })

        items, wait_elapsed, turn_status = wait_for_turn(client, since, args.turn_timeout)
        if images_on and turn_status == "resolved":
            items = wait_for_scene_image(client, since, items, args.image_grace, scene_url)

        # Sitting at the new decision point is exactly when a real browser ticks
        # the world. Doing it here — after the turn resolved, before the next
        # action — is the only moment the server will accept one, since a tick
        # refuses while a turn holds the lock.
        drift = world_tick(client) if getattr(args, "world_drift", False) else None

        new_prompt = latest_prompt(items)
        new_choices = [c.get("text") for c in ((new_prompt or {}).get("choices") or [])]
        narratives = [i.get("content") for i in items if i.get("type") == "narrative_event"]
        narrative = " ".join(n for n in narratives if n)
        new_scene = latest_image_url(items)
        new_sequence = latest_sequence(items)
        after = client.get("/api/status")
        image_prompt = after.get("current_image_prompt", "") or ""

        # Object permanence: a scanned subject the player acted on is supposed
        # to survive into the next scene (engine's OBJECT PERMANENCE block).
        subject = (action.get("subject") or "").lower()
        haystack = f"{narrative} {after.get('current_image_prompt', '')}".lower()
        subject_kept = None
        if subject:
            words = [w for w in re.findall(r"[a-z]+", subject) if len(w) > 2]
            subject_kept = bool(words) and any(w in haystack for w in words)

        # Did the beat actually use the scene? The engine now names every scanned
        # object in the consequence prompt and asks for two of them to feature, so
        # a turn that mentions only the one thing the player poked means the
        # directive isn't landing. Only measurable when the scan offered a choice
        # of two or more things to weave.
        scanned_labels = [str(o.get("label")) for o in objects if o.get("label")]
        woven = labels_present(scanned_labels, haystack) if len(scanned_labels) >= 2 else None

        run["turns"].append({
            "turn": turn_no,
            "planned": kind,
            "action_kind": action["kind"],
            "degraded_to_choice": action["degraded"],
            "choice_text": action["choice"],
            "source": action["source"],
            "subject": action["subject"],
            "selected_object": action["object"],
            "frame_source": frame_source if png else None,
            "detections": objects,
            "detection_labels": [o.get("label") for o in objects],
            "detect_error": detect_error,
            "detect_elapsed_s": detect_elapsed,
            "anti_loop_suppressed": anti_loop_suppressed,
            "view_frame": str(view_path.relative_to(out_dir)) if view_path else None,
            # The in-betweens behind view_frame, when this was a flipbook turn.
            "sequence_frames": [str(p.relative_to(out_dir)) for p in seq_paths],
            "sequence_frame_ms": (scene_sequence or {}).get("frame_ms") if seq_paths else None,
            "choices_frame": str(choices_path.relative_to(out_dir)) if choices_path else None,
            "selected_frame": str(selected_path.relative_to(out_dir)) if selected_path else None,
            "choose_elapsed_s": round(choose_elapsed, 2),
            "choose_error": choose_error,
            "wait_elapsed_s": round(wait_elapsed, 2),
            "status": turn_status,
            "narrative": narrative,
            "image_prompt": image_prompt,
            "prev_choices": choices,
            "new_choices": new_choices,
            "choices_changed": bool(new_choices) and new_choices != choices,
            "scene_image_url": new_scene,
            "scene_changed": bool(new_scene) and new_scene != scene_url,
            "subject_kept_in_next_scene": subject_kept,
            "onscreen_labels_used": woven,
            "onscreen_weave_count": (len(woven) if woven is not None else None),
            # Beats seen in THIS turn's feed slice were queued by the tick at
            # the end of the previous turn; `skipped` is why this turn's tick
            # was refused, if it was.
            "world_drift_beats": drift_beats_in(items),
            "world_drift_skipped": (drift or {}).get("skipped") if drift else None,
            "game_status": after,
        })
        _write_live_beats(out_dir, run["turns"])

        if turn_status in ("death", "timeout", "error"):
            break

        choices = new_choices or choices
        last_id = max((i.get("id", last_id) for i in items), default=last_id)
        scene_url = new_scene or scene_url
        # Cleared when this turn drew a still, so the next turn cannot save the
        # previous turn's motion against its own frame.
        scene_sequence = new_sequence if new_scene else scene_sequence

    # Final VIEW — the generation the last turn's SELECTED choice produced.
    # Without this the run ends on a SELECTED frame with nothing to show
    # whether the choice actually landed; saved the same way every other VIEW
    # frame is; turn number one past the last so it sorts after it.
    final_view_frame = None
    if scene_url and run["turns"]:
        last = run["turns"][-1]
        png = client.fetch_bytes(scene_url)
        if png:
            tail_no = last["turn"] + 1
            final_path = frames_dir / f"turn_{tail_no:02d}_view.png"
            _save_frame_atomic(Image.open(io.BytesIO(png)).convert("RGB"), final_path)
            final_view_frame = str(final_path.relative_to(out_dir))
            _save_sequence_frames(client, scene_sequence, frames_dir, tail_no)
            if args.gif or args.video:
                gif_frames.append((
                    _scaled_to_width(Image.open(io.BytesIO(png)).convert("RGB"), args.gif_width),
                    int(args.gif_ms * 2),
                ))
    run["final_view_frame"] = final_view_frame

    run["finished_at"] = datetime.now(timezone.utc).isoformat()
    run["verdict"] = summarize(run)

    if args.gif and gif_frames:
        gif_path = build_gif(gif_frames, out_dir / "playtest.gif", args.gif_width)
        run["gif"] = str(gif_path) if gif_path else None
        run["gif_frames"] = len(gif_frames)

    if args.video:
        run["videos"] = build_flipbooks(frames_dir, out_dir, args.video_pause)
        if gif_frames:
            review_path = build_review_video(gif_frames, out_dir / "playtest_review.mp4")
            if review_path:
                run["videos"]["review"] = {
                    "path": str(review_path),
                    "file": review_path.name,
                    "frames": len(gif_frames),
                }
        run["video_pause_s"] = args.video_pause
    run["gif_ms"] = args.gif_ms

    (out_dir / "session.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
    (out_dir / "SUMMARY.md").write_text(render_summary(run, out_dir), encoding="utf-8")
    return run


def summarize_pacing(turns: list) -> dict:
    """Whether time actually moved over the run.

    Every number here was flat in the baseline runs: one time_of_day for all 30
    turns, chaos climbing +1 a turn and never falling, and a MOVE-driven run
    that cut to a freshly composed scene on essentially every turn. They're
    reported per run so two runs can be compared directly.
    """
    status = [t.get("game_status") or {} for t in turns]
    times = [s.get("time_of_day") for s in status if s.get("time_of_day")]
    chaos = [s.get("chaos") for s in status if isinstance(s.get("chaos"), int)]
    threat = [s.get("threat") for s in status if isinstance(s.get("threat"), int)]
    phases = [s.get("phase") for s in status if s.get("phase")]
    drift_beats = [b for t in turns for b in (t.get("world_drift_beats") or [])]
    drift_skips = [t.get("world_drift_skipped") for t in turns if t.get("world_drift_skipped")]
    # Detection is simulated rather than narrated as of the danger pass, and is
    # reported here for the same reason the clock is: a flat column is the
    # symptom that tells you a system is not actually running. (There was a
    # health column beside it until the hit-point pool was removed — see
    # engine's "how a run ends".)
    detect = [s.get("detection") for s in status if s.get("detection")]
    return {
        "time_of_day_values": len(dict.fromkeys(times)),
        "time_of_day_timeline": list(dict.fromkeys(times)),
        "phases_seen": len(dict.fromkeys(phases)),
        "phase_timeline": list(dict.fromkeys(phases)),
        "chaos_range": ([min(chaos), max(chaos)] if chaos else None),
        "chaos_went_down_at_least_once": any(b < a for a, b in zip(chaos, chaos[1:])),
        "chaos_distinct_values": len(set(chaos)),
        "threat_final": (threat[-1] if threat else None),
        "detection_levels_seen": list(dict.fromkeys(detect)),
        "detection_turns_not_hidden": sum(1 for d in detect if d and d != "hidden"),
        "world_drift_beats": len(drift_beats),
        "world_drift_beat_samples": drift_beats[:5],
        "world_drift_skip_reasons": sorted(set(drift_skips)),
    }


def summarize(run: dict) -> dict:
    turns = run["turns"]
    scan_planned = [t for t in turns if t["planned"] in ("scan_interact", "scan_move")]
    scan_committed = [t for t in turns if t["action_kind"] in ("scan_interact", "scan_move")]
    with_dets = [t for t in scan_planned if t["detection_labels"]]
    resolved = [t for t in turns if t["status"] == "resolved"]
    permanence = [t for t in scan_committed if t["subject_kept_in_next_scene"] is not None]
    kept = [t for t in permanence if t["subject_kept_in_next_scene"]]
    require_scan = bool(run.get("require_scan"))

    # engine.api_detect's anti-loop gate deliberately empties the object list
    # when the run is stuck (environment_streak maxed) or the player is
    # DETECT_HUNTED — SCAN going quiet there is the gate working, steering
    # the player onto the choice pills' forced flee/egress option instead of
    # letting them keep tapping scenery while being chased. On the wire that
    # empty result is indistinguishable from "the detector genuinely found
    # nothing", so without this split every hunted stretch of a run reads as
    # a detector regression — which is exactly what a hit_rate of 0.79 with
    # zero detect_error and zero timeouts turned out to be (verified against
    # a live run: replaying the SAME frame straight through the detector,
    # bypassing the gate, found 5 objects every time). Suppressed turns are
    # excluded from the measured checks below and counted separately instead.
    scan_gated = [t for t in scan_planned if t.get("anti_loop_suppressed")]
    scan_ungated = [t for t in scan_planned if not t.get("anti_loop_suppressed")]

    hit_rate = (len(with_dets) / len(scan_ungated)) if scan_ungated else None

    # Turns where the scan gave the writer two or more things it could braid.
    weavable = [t for t in turns if t.get("onscreen_weave_count") is not None]
    wove_two = [t for t in weavable if t["onscreen_weave_count"] >= 2]
    weave_rate = (len(wove_two) / len(weavable)) if weavable else None

    pacing = summarize_pacing(turns)

    checks = {
        "every turn resolved": (len(resolved) == len(turns) and bool(turns)),
        "scan finds objects on nearly every frame": (not require_scan) or (hit_rate is not None and hit_rate >= 0.8),
        "scan actions committed as object interactions":
            (not require_scan) or (len(scan_committed) == len(with_dets)),
        # Only an UNGATED fallback (detector genuinely returned nothing) counts
        # against this — a gated one is the anti-loop backstop correctly
        # steering a stuck-or-hunted run onto the forced flee/egress choice,
        # not a miss.
        "scan never fell back to a plain button press (excluding anti-loop gates)":
            (not require_scan) or (not any(
                t["degraded_to_choice"] and not t.get("anti_loop_suppressed") for t in scan_planned
            )),
        "scan actions changed the scene": (not scan_committed)
            or all(t["scene_changed"] for t in scan_committed),
        "choices refresh after every turn": all(t["choices_changed"] for t in resolved) if resolved else False,
        "scanned subject persists into next scene": (not permanence) or (len(kept) / len(permanence) >= 0.5),
        # The bar sits below 1.0 on purpose: `labels_present` is a loose word
        # match against the prose, so individual turns can read as a miss when
        # the writing renamed the thing ("the growth" for "organic mass"). A
        # majority is the honest signal that the directive is landing.
        "consequence weaves 2+ on-screen objects": (not weavable) or (weave_rate >= 0.6),
        # Lighting is the evening this run started in. A later pass stepped
        # it on every phase tip and injected the new string as a CRITICAL
        # rewrite, so hard cuts relit the movie. More than one clock value
        # in a run is that regression coming back.
        "the lighting stays the evening it started as":
            pacing["time_of_day_values"] <= 1,
        "chaos reads the turn, not the turn number":
            pacing["chaos_went_down_at_least_once"] or len(turns) < 5,
    }
    return {
        **pacing,
        "turns_played": len(turns),
        "scan_turns_planned": len(scan_planned),
        "scan_turns_committed": len(scan_committed),
        "scan_turns_degraded_to_choice": sum(1 for t in scan_planned if t["degraded_to_choice"]),
        "scan_turns_anti_loop_gated": len(scan_gated),
        "turns_with_detections": len(with_dets),
        # Measured against scan_ungated (see scan_gated above), not every
        # planned SCAN turn — a gated turn was never a real attempt to detect.
        "scan_hit_rate": (round(hit_rate, 2) if hit_rate is not None else None),
        "total_objects_detected": sum(len(t["detection_labels"]) for t in turns),
        "unique_objects_selected": sorted({t["subject"] for t in scan_committed if t["subject"]}),
        "permanence_rate": (round(len(kept) / len(permanence), 2) if permanence else None),
        "weave_rate": (round(weave_rate, 2) if weave_rate is not None else None),
        "checks": checks,
        "passed": all(checks.values()),
    }


def render_summary(run: dict, out_dir: Path) -> str:
    v = run["verdict"]
    lines = [
        f"# {run.get('mode_title', 'Playtest')}",
        "",
        f"- Mode: `{run.get('mode', 'custom')}`",
        f"- Server: `{run['url']}` (chat backend `{run.get('backend')}`, "
        f"images `{run.get('image_provider')}`, detector `{run.get('detect_backend')}`)",
        f"- Session: `{run['session_id']}`",
        f"- Plan: `{' -> '.join(run['plan'])}`",
        f"- Turns played: {v['turns_played']}",
        f"- SCAN turns committed as object interactions: "
        f"{v['scan_turns_committed']}/{v['scan_turns_planned']}",
        f"- SCAN hit rate (frames where the detector found something, "
        f"excluding anti-loop gated turns): {v['scan_hit_rate']}",
        f"- SCAN turns the anti-loop gate deliberately went quiet on (stuck "
        f"streak or DETECT_HUNTED): {v['scan_turns_anti_loop_gated']}",
        f"- Objects detected across the run: {v['total_objects_detected']}",
        f"- Objects selected: {', '.join(v['unique_objects_selected']) or '(none)'}",
        f"- Subject survived into the next scene: {v['permanence_rate']}",
        f"- Beats weaving 2+ on-screen objects: {v['weave_rate']}",
        "",
        "## Pacing",
        "",
        f"- Time of day: {' -> '.join(v['time_of_day_timeline']) or '(none)'}",
        f"- Phase: {' -> '.join(v['phase_timeline']) or '(none)'}",
        f"- Chaos range: {v['chaos_range']} across {v['chaos_distinct_values']} distinct "
        f"value(s); fell at least once: {v['chaos_went_down_at_least_once']}",
        f"- Threat at the end: {v['threat_final']}",
        f"- World drift beats consumed: {v['world_drift_beats']}"
        + (f" (skipped: {', '.join(v['world_drift_skip_reasons'])})"
           if v['world_drift_skip_reasons'] else ""),
        "",
        "## Checks",
        "",
    ]
    for name, ok in v["checks"].items():
        lines.append(f"- [{'PASS' if ok else 'FAIL'}] {name}")
    lines += [
        "", "## Turns", "",
        "Each turn shows the causal triplet in reading order: the VIEW the "
        "player acted on, the CHOICES that were on offer, and which one got "
        "SELECTED — then the narrative that follows is *why* the next turn's "
        "VIEW looks the way it does.",
        "",
    ]
    for t in run["turns"]:
        verb = {"scan_interact": "INTERACT", "scan_move": "MOVE TO"}.get(t["action_kind"], "CHOICE")
        target = t["subject"] or t["choice_text"]
        lines.append(f"### Turn {t['turn']} — {verb}: {target}")
        if t["detection_labels"]:
            lines.append(f"- SCAN saw: {', '.join(str(x) for x in t['detection_labels'])}")
        if t.get("onscreen_labels_used"):
            lines.append(f"- Woven into the beat: {', '.join(t['onscreen_labels_used'])}")
        for label, key in (("view", "view_frame"), ("choices", "choices_frame"),
                           ("selected", "selected_frame")):
            frame = t.get(key)
            if frame:
                lines.append(f"- **{label}** ![{label}]({frame.replace(chr(92), '/')})")
        lines.append(f"- You chose: {t['choice_text']}")
        if t.get("new_choices"):
            lines.append(f"- Next slate: {' | '.join(t['new_choices'])}")
        lines.append(f"- {(t['narrative'] or '(no narrative)')[:400]}")
        if t.get("image_prompt"):
            lines.append("- <details><summary>Image prompt (rendered this turn's scene)</summary>\n\n"
                        f"  ```\n  {t['image_prompt']}\n  ```\n  </details>")
        lines.append("")
    if run.get("final_view_frame"):
        lines += [
            f"### Final view — after turn {run['turns'][-1]['turn']}",
            f"![final view]({run['final_view_frame'].replace(chr(92), '/')})",
            "",
        ]
    if run.get("gif"):
        lines += ["## Playback", "", "![playtest](playtest.gif)", ""]
    videos = {k: v for k, v in (run.get("videos") or {}).items() if v.get("file")}
    review = videos.pop("review", None)
    if review:
        lines += [
            "## Walkthrough",
            "",
            f"[{review['file']}]({review['file']}) — every VIEW, CHOICES and SELECTED "
            f"frame in the order the run produced them, captioned, meant to be "
            f"watched start to finish ({review['frames']} frames).",
            "",
        ]
    if videos:
        pause = run.get("video_pause_s", 0.5)
        lines += [
            "## Flipbooks",
            "",
            f"One MP4 per frame kind, {pause}s per source frame, in the order the "
            "run wrote them — for reading one aspect across the whole run rather "
            "than turn by turn.",
            "",
        ]
        blurb = {
            "view": "the frame the player was looking at before acting",
            "choices": "that same frame with every option called out",
            "selected": "the same options, with the one taken picked out from the rest",
        }
        for kind in FRAME_KINDS:
            v = videos.get(kind)
            if v:
                lines.append(f"- [{v['file']}]({v['file']}) — {blurb[kind]} "
                             f"({v['frames']} frames)")
        lines.append("")
    return "\n".join(lines)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://127.0.0.1:5001")
    p.add_argument("--turns", type=int, default=8)
    p.add_argument("--session", default=None,
                   help="session id to play in (default: a fresh scan<timestamp> session)")
    p.add_argument("--out", default=None, help="run folder (default: playtest_results/scan_run_<stamp>)")
    p.add_argument("--mode", choices=list(MODE_PRESETS.keys()), default=None,
                   help="Preset: auto = cycle choice buttons only; "
                        "scan_move = SCAN then MOVE TO on a detected object every turn")
    p.add_argument("--plan", default="scan_move",
                   help="comma-separated rotation of actions per turn (ignored when "
                        "--mode is set). `scan_interact` drives a real player turn "
                        "now that INTERACT ships on stills, minus the close-up dive "
                        "(browser chrome).")
    p.add_argument("--turn-timeout", type=int, default=180)
    p.add_argument("--detect-timeout", type=int, default=60)
    p.add_argument("--image-grace", type=float, default=25.0)
    p.add_argument("--fallback-frame", default="static/img/scene_exterior.png",
                   help="still used for SCAN when the backend generates no images")
    p.add_argument("--gif", dest="gif", action="store_true", default=True)
    p.add_argument("--no-gif", dest="gif", action="store_false")
    p.add_argument("--gif-width", type=int, default=520)
    p.add_argument("--gif-ms", type=int, default=1100)
    p.add_argument("--world-drift", dest="world_drift", action="store_true", default=False,
                   help="POST /api/world_tick at each decision point, the way a browser "
                        "does. Needs the server started with WORLD_DRIFT=1, otherwise "
                        "every tick reports skipped=disabled.")
    p.add_argument("--video", dest="video", action="store_true", default=True,
                   help="build playtest_view/choices/selected.mp4 flipbooks plus the "
                        "interleaved playtest_review.mp4 walkthrough after the run")
    p.add_argument("--no-video", dest="video", action="store_false")
    p.add_argument("--video-pause", type=float, default=0.5,
                   help="seconds each source PNG stays on screen in the flipbooks")
    args = p.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if args.mode:
        preset = MODE_PRESETS[args.mode]
        args.plan = ",".join(preset["plan"])
        args.mode_title = preset["title"]
        args.require_scan = preset["require_scan"]
    else:
        args.mode = "custom"
        args.mode_title = "Custom plan"
        plan_parts = [p.strip() for p in args.plan.split(",") if p.strip()]
        args.require_scan = any(p in ("scan_interact", "scan_move") for p in plan_parts)
    if not args.session:
        args.session = f"{args.mode}{stamp}"
    if not args.out:
        args.out = str(ROOT / "playtest_results" / f"{args.mode}_run_{stamp}")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    print(f"Playtest [{args.mode}] -> {args.out}")
    run = play(args)
    v = run["verdict"]
    print("\n" + "=" * 64)
    for name, ok in v["checks"].items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print("-" * 64)
    print(f"  turns={v['turns_played']} scan_committed={v['scan_turns_committed']}"
          f"/{v['scan_turns_planned']} hit_rate={v['scan_hit_rate']}"
          f" (anti_loop_gated={v['scan_turns_anti_loop_gated']})"
          f" objects={v['total_objects_detected']}"
          f" permanence={v['permanence_rate']}"
          f" weave={v['weave_rate']}")
    print(f"  selected: {', '.join(v['unique_objects_selected']) or '(none)'}")
    if run.get("gif"):
        print(f"  gif: {run['gif']} ({run.get('gif_frames')} frames)")
    for kind in FRAME_KINDS:
        vid = (run.get("videos") or {}).get(kind)
        if vid and vid.get("file"):
            print(f"  video[{kind}]: {vid['file']} ({vid['frames']} frames, "
                  f"{vid['seconds_per_frame']}s each)")
    print(f"  folder: {args.out}")
    print("=" * 64)
    return 0 if v["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
