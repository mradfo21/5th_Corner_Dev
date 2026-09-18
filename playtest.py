#!/usr/bin/env python3
"""playtest.py — the one PLAYtest.

Plays a real session against a running server through the same /api/* the
browser uses: ordinary choices, a SCAN→MOVE object turn, a free-text custom
action, and an encounter forced open (not waited on) and played through to a
resolution. Every frame is checked by code, not by asking a model whether it
looks right — see docs/plans/PLAYTEST_CONSOLIDATION_PLAN.md §2.1 for why that
split is deliberate: solid-color / placeholder detection, an exact-content
diff against the previous frame (a perceptual hash was tried and calibrated
out — see the docstring on frames_byte_identical), and a plain text-similarity
check for a narrative that's stalled. None of that needs a model call and
none of it needs to be trusted on faith — it's either right or wrong every
time.

What still needs a real look (does this actually read as a coherent scene,
does the encounter make sense) is deliberately NOT automated here. Every run
writes one small, reviewable bundle instead: SUMMARY.md, a single contact
sheet image of the turns that matter, and the narrative transcript — sized to
be read by a person, or an agent session, in one pass. That's §2.2 of the plan.

Usage:
    python playtest.py --url http://127.0.0.1:5001 --turns 10
    python playtest.py --url http://127.0.0.1:5001 --turns 10 --server-log logs/somewhere.log

Exit code is 0 only if every mechanical check passed. Findings (one per
distinct problem, mechanical or otherwise) are appended to
playtest_results/findings.jsonl across every run, so the ledger accumulates
instead of resetting each time.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import io
import json
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent.resolve()
FINDINGS_LEDGER = ROOT / "playtest_results" / "findings.jsonl"

CUSTOM_ACTIONS = [
    "Raise the camera and take it all in, hands shaking.",
    "Call out, voice cracking, and wait to see who answers.",
    "Crouch low and press your back to whatever is nearest.",
]


# ──────────────────────────────────────────────────────────────────────
# HTTP client — same shape as playtest_interactive.Client, kept independent
# on purpose: this script is meant to stand alone as THE playtest, not to
# import its predecessor.
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
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode()), resp.status
        except urllib.error.HTTPError as e:
            try:
                body_j = json.loads(e.read().decode())
            except Exception:
                body_j = {"error": f"HTTP {e.code}"}
            return body_j, e.code

    def fetch_bytes(self, path: str, timeout: int = 30) -> Optional[bytes]:
        url = path if path.startswith("http") else self.base + path
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.read()
        except Exception:
            return None


# ──────────────────────────────────────────────────────────────────────
# Feed helpers
# ──────────────────────────────────────────────────────────────────────

def latest_prompt(items: list) -> Optional[dict]:
    for it in reversed(items or []):
        if it.get("type") == "player_choice_prompt":
            return it
    return None


def latest_image_url(items: list) -> Optional[str]:
    for it in reversed(items or []):
        if it.get("image_url"):
            return it["image_url"]
    return None


def wait_for_turn(client: Client, since_id: int, timeout_s: int = 180):
    start = time.time()
    while time.time() - start < timeout_s:
        items = client.get(f"/api/feed?since_id={since_id}")
        types = {i.get("type") for i in items}
        # game_over ships in the SAME batch as its own player_choice_prompt
        # ("Restart Simulation"), so death has to be checked first or every
        # death reads as an ordinary resolved turn.
        if "game_over" in types:
            return items, time.time() - start, "death"
        if "player_choice_prompt" in types:
            return items, time.time() - start, "resolved"
        if "error_event" in types and "narrative_event" not in types:
            return items, time.time() - start, "error"
        time.sleep(0.5)
    return client.get(f"/api/feed?since_id={since_id}"), time.time() - start, "timeout"


def wait_for_scene_image(client: Client, since_id: int, items: list,
                         grace_s: float = 25.0, prev_url: Optional[str] = None):
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
# SCAN frame capture (mirrors static/js/standalone.js captureScanFrame)
# ──────────────────────────────────────────────────────────────────────

def to_scan_frame(png_bytes: bytes, width: int = 640, quality: int = 72) -> str:
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    if img.width > width:
        img = img.resize((width, max(1, round(img.height * width / img.width))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def detect(client: Client, frame_data_url: str, timeout: int = 60) -> tuple[list, Optional[str]]:
    try:
        resp, status = client.post("/api/detect", {"frame": frame_data_url, "purpose": "scan"},
                                   timeout=timeout)
        if status >= 300:
            return [], str(resp.get("error") or f"HTTP {status}")
        return list(resp.get("objects") or []), None
    except Exception as e:
        return [], str(e)


def pick_object(objects: list, turn_idx: int) -> Optional[dict]:
    if not objects:
        return None
    return objects[turn_idx % len(objects)]


def move_phrase(label: str) -> str:
    return f"Move to the {label}."


# ──────────────────────────────────────────────────────────────────────
# Code-only QA checks — no model call, deterministic, run every time.
# See docs/plans/PLAYTEST_CONSOLIDATION_PLAN.md §2.1.
# ──────────────────────────────────────────────────────────────────────

def dominant_color_fraction(png_bytes: bytes) -> tuple[float, tuple]:
    """The largest single color's share of the pixels, on a downsized copy.

    Catches a black screen, a solid gray/green error tile, or any frame that
    rendered as one flat color — the exact "is there a picture here" question
    the mint-placeholder bug (world_frames._placeholder_png, fixed for the
    World's opening frame in commit 96425e9) shows nothing upstream of the
    editor asks about a mid-session frame.
    """
    try:
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    except Exception:
        return 1.0, (0, 0, 0)
    small = img.resize((64, 64), Image.BILINEAR)
    colors = small.getcolors(64 * 64) or []
    if not colors:
        return 1.0, (0, 0, 0)
    colors.sort(key=lambda c: c[0], reverse=True)
    count, rgb = colors[0]
    return count / (64 * 64), rgb


def is_blank_frame(png_bytes: bytes, threshold: float = 0.92) -> tuple[bool, float, tuple]:
    frac, rgb = dominant_color_fraction(png_bytes)
    return frac >= threshold, frac, rgb


def content_hash(png_bytes: Optional[bytes]) -> Optional[str]:
    if not png_bytes:
        return None
    return hashlib.sha256(png_bytes).hexdigest()


def frames_byte_identical(png_a: Optional[bytes], png_b: Optional[bytes]) -> Optional[bool]:
    """Exact-content check, not a perceptual one — on purpose.

    A perceptual hash (average-hash, tried first) turned out to be the wrong
    tool for this game's frames: calibrated against a real playtest run, two
    frames the model actually re-rendered (the character shifted, a monitor
    lit up) scored a 0% hash distance at 16x16 — indistinguishable from an
    actual duplicate — while a real scene change scored ~20%. Single-character,
    single-light-source compositions are close enough turn to turn that a
    coarse hash cannot tell "barely moved" from "identical," and reporting
    that as a game bug would have been exactly the kind of automated
    over-trust this harness exists to avoid (see
    docs/plans/PLAYTEST_CONSOLIDATION_PLAN.md §2.1). An exact byte hash has
    the opposite property: it only ever fires when the server returned the
    literal same file under a new URL, so there is nothing here to
    second-guess if it does.
    """
    if not png_a or not png_b:
        return None
    return content_hash(png_a) == content_hash(png_b)


def text_similarity(a: str, b: str) -> float:
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def find_near_duplicate(narratives: list[str], idx: int, threshold: float = 0.85) -> Optional[int]:
    """Is turn `idx`'s narrative close to any earlier turn's? Returns the
    earliest matching turn index, or None. A plain string-similarity ratio —
    not "is this good writing," just "is this the same beat again."
    """
    cur = narratives[idx]
    if not (cur or "").strip():
        return None
    for j in range(idx):
        if text_similarity(cur, narratives[j]) >= threshold:
            return j
    return None


# ──────────────────────────────────────────────────────────────────────
# Server-log tailing — a traceback in the log fails the run even if the
# HTTP response looked fine.
# ──────────────────────────────────────────────────────────────────────

def tail_new_tracebacks(log_path: Optional[Path], start_offset: int) -> list[str]:
    if not log_path or not log_path.is_file():
        return []
    try:
        with log_path.open("rb") as fh:
            fh.seek(start_offset)
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    blocks = []
    for m in re.finditer(r"Traceback \(most recent call last\):.*?(?=\n\S|\Z)", chunk, re.S):
        blocks.append(m.group(0)[:1200])
    return blocks


def log_size(log_path: Optional[Path]) -> int:
    if not log_path or not log_path.is_file():
        return 0
    try:
        return log_path.stat().st_size
    except OSError:
        return 0


# ──────────────────────────────────────────────────────────────────────
# Rendering: choice overlay (for turns with no bounding boxes) + boxes
# (for scan turns) — reused from playtest_interactive's visual language.
# ──────────────────────────────────────────────────────────────────────

def _font(size: int):
    for name in ("arialbd.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _glow_text(layer: Image.Image, xy, text: str, font, fill, glow_radius: int = 3) -> None:
    from PIL import ImageFilter
    glow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).text(xy, text, font=font, fill=(0, 0, 0, 235), anchor="mm")
    layer.alpha_composite(glow.filter(ImageFilter.GaussianBlur(glow_radius)))
    ImageDraw.Draw(layer).text(xy, text, font=font, fill=fill, anchor="mm")


def label_frame(img: Image.Image, label: str) -> Image.Image:
    """Small caption baked onto a contact-sheet cell so a turn is identifiable
    without opening the transcript."""
    out = img.convert("RGBA").copy()
    overlay = Image.new("RGBA", out.size, (0, 0, 0, 0))
    font = _font(max(11, out.width // 24))
    _glow_text(overlay, (out.width / 2, out.height - font.size), label, font,
              (234, 255, 242, 255), glow_radius=3)
    return Image.alpha_composite(out, overlay).convert("RGB")


def build_contact_sheet(entries: list[tuple[Image.Image, str]], out_path: Path,
                        cols: int = 4, cell_w: int = 300) -> Optional[Path]:
    """One grid image, not a folder of forty PNGs — sized to be looked at in
    one pass, not scrolled through."""
    if not entries:
        return None
    cell_h = 0
    scaled = []
    for img, label in entries:
        w, h = img.size
        ch = max(1, round(h * cell_w / w))
        cell_h = max(cell_h, ch)
        scaled.append((img.resize((cell_w, ch), Image.LANCZOS), label))
    rows = (len(scaled) + cols - 1) // cols
    pad = 6
    sheet = Image.new("RGB", (cols * (cell_w + pad) + pad, rows * (cell_h + pad) + pad),
                      (10, 12, 14))
    for i, (img, label) in enumerate(scaled):
        r, c = divmod(i, cols)
        x, y = pad + c * (cell_w + pad), pad + r * (cell_h + pad)
        cell = Image.new("RGB", (cell_w, cell_h), (10, 12, 14))
        cell.paste(img, (0, (cell_h - img.height) // 2))
        cell = label_frame(cell, label)
        sheet.paste(cell, (x, y))
    sheet.save(out_path)
    return out_path


def _save_atomic(data: bytes, path: Path) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


# ──────────────────────────────────────────────────────────────────────
# The encounter probe — forced open, not waited on.
# ──────────────────────────────────────────────────────────────────────

def run_encounter_probe(client: Client, view_png: Optional[bytes], max_exchanges: int = 4) -> dict:
    """POST /api/encounter/begin with force=True, then resolve it out.

    Loops /api/encounter/resolve while the fight stays open (released=False,
    alive=True), capped at max_exchanges so a fight that never settles is
    its own finding instead of an infinite loop.
    """
    rec: dict[str, Any] = {
        "began": False, "error": None, "exchanges": [], "outcome": None,
        "released": None, "alive": None, "plate_url": None, "final_url": None,
        "never_resolved": False,
    }
    frame = to_scan_frame(view_png) if view_png else None
    begin_body = {"force": True}
    if frame:
        begin_body["frame"] = frame
    resp, status = client.post("/api/encounter/begin", begin_body, timeout=90)
    if status >= 300:
        rec["error"] = f"begin failed: HTTP {status} {resp.get('error')}"
        return rec
    rec["began"] = True
    rec["plate_url"] = resp.get("plate_url")
    choices = resp.get("choices") or []

    for _ in range(max_exchanges):
        if not choices:
            rec["error"] = "encounter offered no choices to resolve with"
            break
        choice = choices[0]
        text = choice.get("text") if isinstance(choice, dict) else str(choice)
        lane = choice.get("lane") if isinstance(choice, dict) else None
        body = {"choice": text}
        if lane:
            body["lane"] = lane
        resp, status = client.post("/api/encounter/resolve", body, timeout=120)
        if status >= 300:
            rec["error"] = f"resolve failed: HTTP {status} {resp.get('error')}"
            break
        exchange = {
            "verb": resp.get("verb") or text,
            "lane": resp.get("lane") or lane,
            "outcome": resp.get("outcome"),
            "alive": resp.get("alive"),
            "released": resp.get("released"),
            "enemy_state": resp.get("enemy_state"),
            "resolve_url": resp.get("resolve_url"),
        }
        rec["exchanges"].append(exchange)
        rec["outcome"] = exchange["outcome"]
        rec["alive"] = exchange["alive"]
        rec["released"] = exchange["released"]
        rec["final_url"] = exchange["resolve_url"] or rec["final_url"]
        choices = resp.get("choices") or []
        if exchange["released"] or exchange["alive"] is False:
            break
    else:
        rec["never_resolved"] = True

    return rec


def run_edge_checks(client: Client, turn_timeout: int) -> list[dict]:
    """Fast, cheap request-level checks that the turn-by-turn narrative loop
    never exercises: malformed input and a concurrent double-submit. Found
    by hand once (a double-click / client retry on /api/choose used to run
    the SAME click as two full turns — no re-entrancy guard existed), now
    pinned here so it can never silently regress.
    """
    findings: list[dict] = []

    def finding(kind: str, detail: str) -> None:
        findings.append({"turn": 0, "kind": kind, "detail": detail})

    # 1. Malformed /api/choose (no 'choice') should be a clean 400, not a 500.
    resp, status = client.post("/api/choose", {}, timeout=30)
    if status != 400:
        finding("bad_request_not_handled",
               f"POST /api/choose with no 'choice' returned HTTP {status} "
               f"(expected 400): {resp}")

    # 2. /api/encounter/resolve with no open encounter should be 409, not a crash.
    resp, status = client.post("/api/encounter/resolve", {"choice": "test"}, timeout=30)
    if status != 409:
        finding("bad_request_not_handled",
               f"POST /api/encounter/resolve with no open encounter returned "
               f"HTTP {status} (expected 409): {resp}")

    # 3. Double-submit race: fire two /api/choose for the same click at once.
    #    Exactly one should be accepted (200) and the other refused (409
    #    turn_in_progress) — both succeeding means the same click ran the
    #    turn twice (doubled narrative, doubled choice prompt, doubled spend).
    reset_items, _ = client.post("/api/reset", {})
    prompt = latest_prompt(reset_items)
    choices = [c.get("text") for c in ((prompt or {}).get("choices") or [])]
    last_id = max((i.get("id", 0) for i in reset_items), default=0)
    text = choices[0] if choices else "Look around"

    results: list[Optional[tuple]] = [None, None]

    def fire(idx: int) -> None:
        results[idx] = client.post("/api/choose", {"choice": text, "context_item_id": last_id},
                                   timeout=turn_timeout)

    t1 = threading.Thread(target=fire, args=(0,))
    t2 = threading.Thread(target=fire, args=(1,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    statuses = sorted(r[1] for r in results if r)
    if statuses != [200, 409]:
        finding("double_submit_not_guarded",
               f"two concurrent /api/choose for the same click returned "
               f"statuses {statuses} (expected exactly one 200 and one 409 "
               f"turn_in_progress) — a double-click or client retry can run "
               f"one click's turn twice")

    # Let the accepted turn actually finish, then confirm the guard released
    # and ordinary play resumed — a guard that never clears is worse than no
    # guard, since it would brick every session after its first turn.
    items, elapsed, turn_status = wait_for_turn(client, last_id, turn_timeout)
    if turn_status not in ("resolved", "death"):
        finding("double_submit_recovery_failed",
               f"after the double-submit race, the turn never resolved "
               f"(status={turn_status}) — the in-progress guard may be stuck")
    else:
        new_prompt = latest_prompt(items)
        new_choices = [c.get("text") for c in ((new_prompt or {}).get("choices") or [])]
        follow_last_id = max((i.get("id", 0) for i in items), default=last_id)
        resp, status = client.post("/api/choose",
                                   {"choice": (new_choices or ["Look around"])[0],
                                    "context_item_id": follow_last_id},
                                   timeout=30)
        if status != 200:
            finding("double_submit_recovery_failed",
                   f"an ordinary /api/choose right after the race returned "
                   f"HTTP {status} instead of 200 — the turn_in_progress guard "
                   f"did not release after the turn finished: {resp}")

    return findings


def _choices_from_cutscene_complete(resp) -> list:
    """Pull the opening slate out of an /api/cutscene/complete response.

    Its shape is {"choices": {"choices": [{"text": ...}, ...]}} — a structured
    player_choice_prompt nested one level deeper than a feed item — so dig
    through both layers defensively."""
    if not isinstance(resp, dict):
        return []
    inner = resp.get("choices")
    if isinstance(inner, dict):
        inner = inner.get("choices")
    if isinstance(inner, list):
        return [c.get("text") for c in inner if isinstance(c, dict) and c.get("text")]
    return []


def _prompt_choices(seq) -> list:
    p = latest_prompt(seq if isinstance(seq, list) else [])
    return [c.get("text") for c in (p.get("choices") or [])] if p else []


def drive_opening(client: Client, reset_items, *, play_timeout: int = 240,
                  complete_timeout: int = 300) -> dict:
    """Play the opening the way the BROWSER plays it, and report what happened.

    A fresh run opens on the intro montage (tunables `intro_cutscene`), and that
    beat is CLIENT-DRIVEN: the server stages a `cutscene` feed item, the browser
    calls /api/cutscene/play to render it, watches the shots, then signals
    /api/cutscene/complete — and only THEN is the opening slate minted and the
    first playable frame installed. A harness that posts /api/reset and reads
    the feed is looking at a run that has not started yet.

    This is where the whole opening lives, so this is what has to be exercised:
    the montage renders as the run's first image, the idle beat renders behind
    it and becomes turn one's anchor, and the parked slate comes back. Returns
    everything the caller needs to check all three, plus ``ok`` for "the run is
    playable now".
    """
    out: dict[str, Any] = {"ok": False, "via": "", "choices": [],
                           "shots": [], "image_url": "", "sequence": None,
                           "errors": []}

    # Already playable: no intro montage configured.
    choices = _prompt_choices(reset_items)
    if choices:
        return {**out, "ok": True, "via": "no_cutscene", "choices": choices}

    has_cutscene = isinstance(reset_items, list) and any(
        isinstance(it, dict) and it.get("type") == "cutscene"
        for it in reset_items)
    if not has_cutscene:
        out["errors"].append("reset returned neither a choice prompt nor a "
                             "cutscene — the run has nothing to show")
        return out

    meta = next((it.get("metadata") or {} for it in reset_items
                 if isinstance(it, dict) and it.get("type") == "cutscene"), {})
    out["opening_flag"] = bool(meta.get("opening"))
    out["name"] = meta.get("name") or ""
    out["goal"] = meta.get("goal") or ""

    # The montage is the run's FIRST render now (no plate in front of it), so
    # this call is a full image generation and wants a real timeout.
    t0 = time.time()
    try:
        play_resp, play_status = client.post(
            "/api/cutscene/play",
            {"cutscene_id": meta.get("cutscene_id") or "",
             "mood": meta.get("mood") or "approach",
             "name": meta.get("name") or "",
             "source_url": ""},
            timeout=play_timeout)
    except Exception as e:
        out["errors"].append(f"/api/cutscene/play raised: {e}")
        return out
    out["play_ms"] = int((time.time() - t0) * 1000)
    out["play_status"] = play_status
    if isinstance(play_resp, dict):
        out["shots"] = [s.get("url") for s in (play_resp.get("shots") or [])
                        if isinstance(s, dict)]
        out["montage_source"] = play_resp.get("source") or ""
    if play_status != 200:
        out["errors"].append(f"/api/cutscene/play returned {play_status}: "
                             f"{str(play_resp)[:300]}")

    t0 = time.time()
    try:
        done, done_status = client.post("/api/cutscene/complete", {},
                                        timeout=complete_timeout)
    except Exception as e:
        out["errors"].append(f"/api/cutscene/complete raised: {e}")
        return out
    out["complete_ms"] = int((time.time() - t0) * 1000)
    out["complete_status"] = done_status
    if isinstance(done, dict):
        out["image_url"] = done.get("image_url") or ""
        out["sequence"] = done.get("sequence")
        out["kind"] = done.get("kind") or ""
    out["choices"] = _choices_from_cutscene_complete(done)
    if not out["choices"]:
        # Fall back to whatever the feed shows once the montage resolved.
        try:
            out["choices"] = _prompt_choices(client.get("/api/feed"))
            if out["choices"]:
                out["slate_from_feed"] = True
        except Exception:
            pass
    out["via"] = "cutscene"
    out["ok"] = bool(out["choices"]) and not out["errors"]
    return out


def verify_restart(client: Client) -> dict:
    """After a death, confirm /api/reset actually starts a fresh run rather
    than leaving a dead session that a harness (or a player) keeps 'playing'.
    """
    try:
        items, _status = client.post("/api/reset", {}, timeout=60)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    res = drive_opening(client, items)
    if res["errors"]:
        return {"ok": False, "choices": res["choices"],
                "error": "; ".join(res["errors"]), "via": res["via"]}
    return {"ok": res["ok"], "choices": res["choices"], "via": res["via"]}


# ──────────────────────────────────────────────────────────────────────
# The run
# ──────────────────────────────────────────────────────────────────────

def play(args) -> dict:
    client = Client(args.url, args.session)
    out_dir = Path(args.out).resolve()
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    server_log = Path(args.server_log).resolve() if args.server_log else None
    log_start_offset = log_size(server_log)

    findings: list[dict] = []

    def finding(turn: int, kind: str, detail: str, **extra) -> None:
        f = {"turn": turn, "kind": kind, "detail": detail, **extra}
        findings.append(f)

    plan = [p.strip() for p in args.plan.split(",") if p.strip()]

    run: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "url": client.base,
        "session_id": args.session,
        "seed": args.seed,
        "plan": plan,
        "turns_requested": args.turns,
        "turns": [],
    }

    status = client.get("/api/status")
    images_on = bool(status.get("image_enabled"))
    run["images_enabled"] = images_on
    run["backend"] = status.get("backend")

    if not args.skip_edge_checks:
        findings.extend(run_edge_checks(client, args.turn_timeout))

    contact_entries: list[tuple[Image.Image, str]] = []
    narratives: list[str] = []

    reset_items, _ = client.post("/api/reset", {})

    # ---- THE OPENING ----------------------------------------------------
    # Driven the way the browser drives it, because that is the only way it
    # runs at all: the montage renders on /api/cutscene/play and the first
    # playable frame is installed by /api/cutscene/complete. This used to be
    # skipped here (only the death-restart path drove it), so every run of this
    # harness began by reading a feed belonging to a run that had not started.
    opening = drive_opening(client, reset_items)
    run["opening"] = opening
    for err in opening["errors"]:
        finding(0, "opening_failed", err)
    if opening["via"] == "cutscene":
        shots = opening["shots"]
        if len(shots) != 4:
            finding(0, "opening_montage_shot_count",
                    f"the montage came back with {len(shots)} shot(s), expected "
                    f"4 — the opening is four cold-open photographs and the "
                    f"beat after them is the idle, not a fifth still")
        if opening.get("montage_source") == "optical":
            finding(0, "opening_montage_fell_back",
                    "the montage came back from the optical fallback (four "
                    "crops of one plate), not a render")
        if not opening["image_url"]:
            finding(0, "opening_no_first_frame",
                    "/api/cutscene/complete handed back no image_url — the run "
                    "has nothing to play from")
        if opening.get("slate_from_feed"):
            finding(0, "opening_slate_not_on_the_wire",
                    "the opening slate had to be scraped off /api/feed; "
                    "/api/cutscene/complete should return it directly")
        if not opening["choices"]:
            finding(0, "opening_no_slate",
                    "the montage finished and left no choices — the run is "
                    "unplayable without reloading")
        elif [c.lower() for c in opening["choices"]] == ["look around"]:
            finding(0, "opening_slate_is_the_fallback",
                    "the opening slate is the bare 'Look around' fallback, so "
                    "the three choices written for this level were lost")
        print(f"[opening] montage {len(shots)} shots in "
              f"{opening.get('play_ms', 0)}ms, handoff in "
              f"{opening.get('complete_ms', 0)}ms, "
              f"sequence={'yes' if opening.get('sequence') else 'no'}, "
              f"slate={opening['choices']}", flush=True)

    if images_on:
        reset_items = wait_for_scene_image(client, 0, reset_items, args.image_grace)
    feed_now = client.get("/api/feed")
    if isinstance(feed_now, list) and feed_now:
        reset_items = feed_now
    prompt = latest_prompt(reset_items)
    choices = opening["choices"] or [
        c.get("text") for c in ((prompt or {}).get("choices") or [])]
    last_id = max((i.get("id", 0) for i in reset_items), default=0)
    scene_url = opening["image_url"] or latest_image_url(reset_items)
    prev_view_bytes: Optional[bytes] = None

    n = 0
    turn_no = 0
    dead = False
    while turn_no < args.turns:
        turn_no += 1
        kind = plan[(turn_no - 1) % len(plan)]

        view_bytes = client.fetch_bytes(scene_url) if scene_url else None
        if view_bytes:
            view_path = frames_dir / f"turn_{turn_no:02d}_view.png"
            _save_atomic(view_bytes, view_path)

        turn: dict[str, Any] = {"turn": turn_no, "kind": kind, "checks": {}}

        # ---- blank-frame / placeholder check (code-only, every frame) ----
        if view_bytes:
            blank, frac, rgb = is_blank_frame(view_bytes)
            turn["checks"]["blank_frame"] = blank
            turn["dominant_color_fraction"] = round(frac, 3)
            if blank:
                finding(turn_no, "blank_frame",
                       f"frame is ~{frac:.0%} one color (rgb={rgb}) — looks like a "
                       f"black screen or a placeholder, not a rendered scene")
        else:
            turn["checks"]["blank_frame"] = None

        # ---- pixel-identical-but-claims-new check happens after the turn resolves ----

        if kind == "encounter":
            enc = run_encounter_probe(client, view_bytes)
            turn["encounter"] = enc
            turn["checks"]["encounter_began"] = enc["began"]
            turn["checks"]["encounter_resolved"] = bool(enc["released"] or enc["alive"] is False)
            if not enc["began"]:
                finding(turn_no, "encounter_begin_failed", enc.get("error") or "unknown error")
            elif enc.get("never_resolved"):
                finding(turn_no, "encounter_never_resolved",
                       f"encounter stayed open after {len(enc['exchanges'])} exchanges "
                       f"without releasing or ending the run")
            if enc.get("final_url"):
                fb = client.fetch_bytes(enc["final_url"])
                if fb:
                    sel_path = frames_dir / f"turn_{turn_no:02d}_selected.png"
                    _save_atomic(fb, sel_path)
                    blank2, frac2, rgb2 = is_blank_frame(fb)
                    if blank2:
                        finding(turn_no, "blank_frame",
                               f"encounter resolution frame is ~{frac2:.0%} one color "
                               f"(rgb={rgb2})")
                    try:
                        contact_entries.append((Image.open(io.BytesIO(fb)),
                                                f"T{turn_no} encounter:{enc.get('outcome')}"))
                    except Exception:
                        pass
            if enc.get("alive") is False:
                dead = True
                restart = verify_restart(client)
                turn["restart_after_death"] = restart
                turn["checks"]["restart_after_death_works"] = restart["ok"]
                if not restart["ok"]:
                    finding(turn_no, "restart_failed",
                           "died in the encounter, but /api/reset did not return a "
                           "fresh choice prompt afterward")
                # Re-seed the run state from the restart so the loop can keep going.
                prompt = latest_prompt([])
                fresh = client.get("/api/feed")
                prompt = latest_prompt(fresh)
                choices = [c.get("text") for c in ((prompt or {}).get("choices") or [])]
                last_id = max((i.get("id", 0) for i in fresh), default=last_id)
                scene_url = latest_image_url(fresh) or scene_url
                narratives.append("")
                run["turns"].append(turn)
                continue
            # Encounter released back into ordinary play — pick up the feed.
            items, elapsed, turn_status = wait_for_turn(client, last_id, args.turn_timeout)
            if images_on and turn_status == "resolved":
                items = wait_for_scene_image(client, last_id, items, args.image_grace, scene_url)
            narr = " ".join(i.get("content", "") for i in items if i.get("type") == "narrative_event")
            new_prompt = latest_prompt(items)
            new_choices = [c.get("text") for c in ((new_prompt or {}).get("choices") or [])]
            new_scene = latest_image_url(items)
            turn["status"] = turn_status
            turn["narrative"] = narr
            turn["new_choices"] = new_choices
            narratives.append(narr)
            last_id = max((i.get("id", last_id) for i in items), default=last_id)
            choices = new_choices or choices
            scene_url = new_scene or scene_url
            run["turns"].append(turn)
            continue

        if kind == "custom":
            text = CUSTOM_ACTIONS[rng.randrange(len(CUSTOM_ACTIONS))]
            action_kind, source, subject = "custom", None, None
        elif kind == "scan_move":
            objects, detect_err = ([], "no frame to scan")
            if view_bytes:
                objects, detect_err = detect(client, to_scan_frame(view_bytes))
            turn["detections"] = [o.get("label") for o in objects]
            turn["detect_error"] = detect_err
            obj = pick_object(objects, turn_no)
            if obj:
                text = move_phrase(obj.get("label") or "it")
                action_kind, source, subject = "scan_move", "scan_move", obj.get("label")
            else:
                idx = rng.randrange(len(choices)) if choices else 0
                text = choices[idx] if choices else "Look around"
                action_kind, source, subject = "choice", None, None
                turn["scan_degraded"] = True
        else:  # "choice"
            idx = rng.randrange(len(choices)) if choices else 0
            text = choices[idx] if choices else "Look around"
            action_kind, source, subject = "choice", None, None

        turn["action_kind"] = action_kind
        turn["choice_text"] = text
        turn["subject"] = subject

        body = {"choice": text, "context_item_id": last_id}
        if source:
            body["source"] = source
        if subject:
            body["subject"] = subject
        since = last_id
        _, status_code = client.post("/api/choose", body, timeout=args.turn_timeout)
        items, elapsed, turn_status = wait_for_turn(client, since, args.turn_timeout)
        if images_on and turn_status == "resolved":
            items = wait_for_scene_image(client, since, items, args.image_grace, scene_url)

        types_seen = {i.get("type") for i in items}
        if "error_event" in types_seen:
            for i in items:
                if i.get("type") == "error_event":
                    finding(turn_no, "error_event", str(i.get("content") or "")[:300])

        narr = " ".join(i.get("content", "") for i in items if i.get("type") == "narrative_event")
        for i in items:
            if i.get("type") == "narrative_event" and (i.get("metadata") or {}).get("degraded"):
                finding(turn_no, "degraded_narrative",
                       "narrative fell back to a degraded/diegetic-glitch line")
        blob = json.dumps(items)
        if "[object Object]" in blob:
            finding(turn_no, "object_leak",
                   "a raw object leaked into the feed as '[object Object]'")

        new_prompt = latest_prompt(items)
        new_choices = [c.get("text") for c in ((new_prompt or {}).get("choices") or [])]
        new_scene = latest_image_url(items)

        turn["status"] = turn_status
        turn["narrative"] = narr
        turn["new_choices"] = new_choices
        turn["scene_changed_by_url"] = bool(new_scene) and new_scene != scene_url
        turn["choices_changed"] = bool(new_choices) and new_choices != choices
        turn["checks"]["turn_resolved"] = turn_status in ("resolved", "death")
        turn["checks"]["full_choice_slate"] = turn_status != "resolved" or len(new_choices) >= 3

        if turn_status == "timeout":
            finding(turn_no, "timeout", f"turn did not resolve within {args.turn_timeout}s")
        if turn_status == "resolved" and len(new_choices) < 3:
            finding(turn_no, "short_choice_slate", f"only {len(new_choices)} choices offered")

        # ---- exact-content diff: does a "new" URL actually carry new bytes? ----
        if new_scene and new_scene != scene_url and images_on:
            new_bytes = client.fetch_bytes(new_scene)
            if new_bytes and view_bytes:
                identical = frames_byte_identical(view_bytes, new_bytes)
                turn["checks"]["frame_actually_changed"] = (identical is False) if identical is not None else None
                if identical:
                    finding(turn_no, "stale_frame_new_url",
                           "scene_image_url changed but the server returned the exact "
                           "same image bytes as the previous turn's frame")
                blank2, frac2, rgb2 = is_blank_frame(new_bytes)
                if blank2:
                    finding(turn_no, "blank_frame",
                           f"the frame this turn rendered is ~{frac2:.0%} one color (rgb={rgb2})")
            prev_view_bytes = new_bytes or prev_view_bytes

        # ---- text-repetition check ----
        narratives.append(narr)
        dup_idx = find_near_duplicate(narratives, len(narratives) - 1, args.repeat_threshold)
        turn["checks"]["not_a_repeat_beat"] = dup_idx is None
        if dup_idx is not None:
            finding(turn_no, "repeated_beat",
                   f"this turn's narrative is a near-duplicate of turn {dup_idx + 1}'s "
                   f"(similarity >= {args.repeat_threshold})")

        if turn_status == "death":
            dead = True
            restart = verify_restart(client)
            turn["restart_after_death"] = restart
            turn["checks"]["restart_after_death_works"] = restart["ok"]
            if not restart["ok"]:
                finding(turn_no, "restart_failed",
                       "died during ordinary play, but /api/reset did not return a "
                       "fresh choice prompt afterward")
            fresh = client.get("/api/feed")
            new_prompt = latest_prompt(fresh)
            new_choices = [c.get("text") for c in ((new_prompt or {}).get("choices") or [])]
            new_scene = latest_image_url(fresh)
            last_id = max((i.get("id", 0) for i in fresh), default=last_id)

        if view_bytes:
            try:
                contact_entries.append((Image.open(io.BytesIO(view_bytes)), f"T{turn_no} {action_kind}"))
            except Exception:
                pass

        choices = new_choices or choices
        last_id = max((i.get("id", last_id) for i in items), default=last_id)
        scene_url = new_scene or scene_url
        run["turns"].append(turn)

    # Server-log tracebacks observed during the whole run.
    tracebacks = tail_new_tracebacks(server_log, log_start_offset)
    for tb in tracebacks:
        finding(0, "server_traceback", tb.splitlines()[-1] if tb else "traceback", excerpt=tb)
    run["server_tracebacks"] = len(tracebacks)

    sheet_path = build_contact_sheet(contact_entries, out_dir / "contact_sheet.png")
    run["contact_sheet"] = str(sheet_path) if sheet_path else None
    run["findings"] = findings
    run["finished_at"] = datetime.now(timezone.utc).isoformat()
    run["died_at_least_once"] = dead
    run["checks"] = aggregate_checks(run)
    run["passed"] = all(run["checks"].values())

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "session.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
    (out_dir / "SUMMARY.md").write_text(render_summary(run), encoding="utf-8")
    append_findings_ledger(run, findings)
    return run


def aggregate_checks(run: dict) -> dict:
    turns = run["turns"]
    ordinary = [t for t in turns if t["kind"] in ("choice", "scan_move", "custom")]
    resolved = [t for t in ordinary if t.get("checks", {}).get("turn_resolved")]
    encounters = [t for t in turns if t["kind"] == "encounter"]
    scans = [t for t in turns if t["kind"] == "scan_move"]
    return {
        "every_ordinary_turn_resolved": (not ordinary) or len(resolved) == len(ordinary),
        "no_blank_frames": not any(f["kind"] == "blank_frame" for f in run["findings"]),
        "no_stale_frames_reported_as_new": not any(
            f["kind"] == "stale_frame_new_url" for f in run["findings"]),
        "no_repeated_beats": not any(f["kind"] == "repeated_beat" for f in run["findings"]),
        "no_error_events": not any(f["kind"] == "error_event" for f in run["findings"]),
        "no_degraded_narrative": not any(f["kind"] == "degraded_narrative" for f in run["findings"]),
        "malformed_requests_handled": not any(
            f["kind"] == "bad_request_not_handled" for f in run["findings"]),
        "double_submit_guarded": not any(
            f["kind"] in ("double_submit_not_guarded", "double_submit_recovery_failed")
            for f in run["findings"]),
        "no_object_leaks": not any(f["kind"] == "object_leak" for f in run["findings"]),
        "no_server_tracebacks": run.get("server_tracebacks", 0) == 0,
        "encounter_forced_and_resolved": (not encounters) or all(
            t.get("checks", {}).get("encounter_resolved") for t in encounters),
        "restart_after_death_works": not any(f["kind"] == "restart_failed" for f in run["findings"]),
        "scan_found_something_when_tried": (not scans) or any(t.get("detections") for t in scans),
        # The opening is the only beat every player sees and the one this
        # harness used to skip entirely (see drive_opening).
        "opening_played_through": not any(
            f["kind"].startswith("opening_") for f in run["findings"]),
    }


def append_findings_ledger(run: dict, findings: list[dict]) -> None:
    if not findings:
        return
    FINDINGS_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with FINDINGS_LEDGER.open("a", encoding="utf-8") as fh:
        for f in findings:
            row = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "session_id": run["session_id"],
                **f,
            }
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def render_summary(run: dict) -> str:
    lines = [
        "# PLAYTEST run",
        "",
        f"- Server: `{run['url']}` (backend `{run.get('backend')}`, images "
        f"`{'on' if run.get('images_enabled') else 'off'}`)",
        f"- Session: `{run['session_id']}`  seed: `{run['seed']}`",
        f"- Plan: `{' -> '.join(run['plan'])}`",
        f"- Turns played: {len(run['turns'])}/{run['turns_requested']}",
        f"- Died at least once: {run['died_at_least_once']}",
        f"- Server tracebacks observed: {run.get('server_tracebacks', 0)}",
        "",
        "## Checks",
        "",
    ]
    for name, ok in run["checks"].items():
        lines.append(f"- [{'PASS' if ok else 'FAIL'}] {name}")
    lines += ["", f"## Findings ({len(run['findings'])})", ""]
    if not run["findings"]:
        lines.append("None.")
    for f in run["findings"]:
        lines.append(f"- **turn {f['turn']}** `{f['kind']}` — {f['detail']}")
    lines += ["", "## Turns", ""]
    for t in run["turns"]:
        lines.append(f"### Turn {t['turn']} — {t['kind']}")
        if t.get("choice_text"):
            lines.append(f"- action: {t['choice_text']}")
        if t.get("encounter"):
            e = t["encounter"]
            lines.append(f"- encounter outcome: {e.get('outcome')} "
                         f"(released={e.get('released')}, alive={e.get('alive')}, "
                         f"exchanges={len(e.get('exchanges', []))})")
        if t.get("narrative"):
            lines.append(f"- {t['narrative'][:300]}")
        lines.append("")
    if run.get("contact_sheet"):
        lines += ["## Contact sheet", "", "![contact sheet](contact_sheet.png)", ""]
    return "\n".join(lines)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://127.0.0.1:5001")
    p.add_argument("--turns", type=int, default=10)
    p.add_argument("--session", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--plan", default="choice,scan_move,choice,custom,encounter,choice,scan_move,choice,choice,choice",
                   help="comma-separated rotation of turn kinds: choice, scan_move, custom, encounter")
    p.add_argument("--seed", type=int, default=1234)
    p.add_argument("--turn-timeout", type=int, default=180)
    p.add_argument("--image-grace", type=float, default=25.0)
    p.add_argument("--repeat-threshold", type=float, default=0.85)
    p.add_argument("--server-log", default=None,
                   help="path to the server's stdout/stderr log; tailed for tracebacks")
    p.add_argument("--skip-edge-checks", action="store_true",
                   help="skip the malformed-request / double-submit-race checks that "
                        "run once before the narrative turns")
    args = p.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if not args.session:
        args.session = f"playtest{stamp}"
    if not args.out:
        args.out = str(ROOT / "playtest_results" / f"playtest_{stamp}")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    print(f"PLAYTEST -> {args.out}")
    run = play(args)
    print("\n" + "=" * 64)
    for name, ok in run["checks"].items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print("-" * 64)
    print(f"  turns={len(run['turns'])}  findings={len(run['findings'])}  "
         f"died={run['died_at_least_once']}  server_tracebacks={run.get('server_tracebacks', 0)}")
    print(f"  folder: {args.out}")
    print("=" * 64)
    return 0 if run["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
