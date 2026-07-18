"""
gemini_live_vision.py — experimental Gemini Live API path for realtime object
recognition.

The default SCAN pipeline (see ``engine.api_detect`` / ``engine._detect_objects``)
is one-shot HTTP: the client uploads a JPEG frame at a ~2.5 s cadence, the
server round-trips it to ``gemini-3.1-flash-lite:generateContent``, and the
client renders whatever tags come back. This module is an **opt-in prototype**
that instead keeps a *persistent* Live-API WebSocket session open per game
session and streams frames into it as they arrive, so the model has running
context across frames (better label stability) and can start emitting the next
detection while we're still uploading the next frame.

CAVEATS — read before merging into the hot path:

* The Live API caps **video input at ≤ 1 FPS** (as of 2026-07). At our current
  2.5 s cadence we're already sending fewer than 1 FPS worth of frames, so the
  *upstream* wins are modest. The wins come from **cross-frame state** (Gemini
  remembers the last few frames, so tag labels stop flickering between "figure"
  and "person") and from **streaming text output** overlapping with the next
  frame upload.
* An audio+video Live session without context compression **times out after
  ~2 minutes**. We only send video, but we still budget for periodic session
  rotation. See ``LiveVisionSession._maybe_recycle``.
* The Live API is a **billed, always-on WebSocket**, not a per-request LLM
  call. When enabled, a session is opened lazily on the first ``push_frame``
  and stays open until the game session goes idle for
  ``IDLE_CLOSE_SECONDS``. Cost is proportional to wall-clock, not to request
  count — turn it off for demos where the SCAN overlay is idle.
* Client-to-server is possible with an ephemeral token, but this module is
  intentionally the **server-side proxy** path so we never ship an API key
  (matching how ``/api/reactor/token`` shields the Reactor key today).

Public surface (all safe to call even when the feature is disabled — they
degrade to ``False`` / ``None`` / no-op):

* ``is_available()``  — feature flag; True only when ``DETECT_LIVE_API=1`` **and**
  ``GEMINI_API_KEY`` is set **and** the ``google-genai`` SDK is importable.
* ``push_frame(session_id, jpeg_bytes, *, mime_type, scene_prompt)`` — hand a
  freshly-captured frame to the per-session Live worker.
* ``get_latest_detections(session_id)`` — return the most recent parsed
  detection list from the Live worker (or ``None`` if we haven't received one
  yet on this session).
* ``stop(session_id)`` / ``stop_all()`` — close the underlying WSS.

Wire contract with the browser is identical to ``/api/detect`` (same object
schema), so a client can A/B by swapping the endpoint URL and nothing else.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from collections import deque
from typing import Optional

# Match the wire contract of engine._detect_objects / engine._DETECT_RESPONSE_SCHEMA
# exactly so the client can consume live-API detections without changes.
_DETECT_RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "label":   {"type": "STRING"},
            "box_2d":  {"type": "ARRAY", "items": {"type": "NUMBER"}},
            "kind":    {"type": "STRING",
                        "enum": ["person", "character", "creature",
                                 "animal", "machine", "object"]},
            "speaks":  {"type": "BOOLEAN"},
        },
        "required": ["label", "box_2d"],
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# Tunables (env-overridable — the whole module is behind DETECT_LIVE_API=1)
# ─────────────────────────────────────────────────────────────────────────────

# The Live-API-capable Gemini model to bind. gemini-live-2.5-flash-preview is
# the current documented text-out video-in preview; a future gemini-3.x-live
# variant will be a drop-in swap here.
LIVE_MODEL = os.getenv("GEMINI_LIVE_VISION_MODEL", "gemini-live-2.5-flash-preview")

# Hard cap the model's input framerate: Live API rejects > 1 FPS video anyway,
# so throttle here rather than eat WSS errors on every push.
MIN_FRAME_INTERVAL_SECONDS = float(os.getenv("GEMINI_LIVE_VISION_MIN_INTERVAL", "1.05"))

# Close the WSS after this long without a push_frame call (Live sessions are
# billed for wall-clock, so idle-close is important).
IDLE_CLOSE_SECONDS = float(os.getenv("GEMINI_LIVE_VISION_IDLE_CLOSE", "45"))

# Force a session rotation before the documented 2-min audio+video ceiling to
# avoid a mid-stream GoAway. Video-only sessions are usually longer, but we
# don't rely on that in code.
MAX_SESSION_SECONDS = float(os.getenv("GEMINI_LIVE_VISION_MAX_SECONDS", "100"))

MAX_DETECTIONS = int(os.getenv("GEMINI_LIVE_VISION_MAX_ITEMS", "8"))


# ─────────────────────────────────────────────────────────────────────────────
# Feature flag / dependency probe
# ─────────────────────────────────────────────────────────────────────────────

def _flag_on() -> bool:
    return os.getenv("DETECT_LIVE_API", "").strip() in ("1", "true", "yes", "on")


def _api_key() -> str:
    # Match the resolution order engine.py uses (env then config.json). We
    # deliberately import engine lazily so this module is safe to import in
    # tests / tools that don't want the full engine surface.
    key = os.getenv("GEMINI_API_KEY", "")
    if key:
        return key
    try:
        import engine
        return getattr(engine, "GEMINI_API_KEY", "") or ""
    except Exception:
        return ""


def _sdk_importable() -> bool:
    try:
        import google.genai  # noqa: F401
        return True
    except Exception:
        return False


def is_available() -> bool:
    """True when the Live-API path is opted-in and can plausibly connect."""
    return _flag_on() and bool(_api_key()) and _sdk_importable()


# ─────────────────────────────────────────────────────────────────────────────
# Per-session Live worker
# ─────────────────────────────────────────────────────────────────────────────

def _system_instruction(max_items: int) -> str:
    """Fixed system prompt that pins Gemini to our detection JSON schema.

    Kept short — the schema itself does the structural work; the instruction
    just tells the model *what* to detect and how to behave across frames.
    """
    return (
        "You are a realtime object-recognition assistant embedded inside an "
        "adventure game's SCAN overlay. Every video frame you receive is a "
        "still from the player's live view.\n\n"
        "On every frame, respond with a JSON array of AT MOST "
        f"{max_items} of the most prominent, distinct things a player could "
        "look at or interact with: objects, tools, doors, exits, figures, "
        "creatures, vehicles, hazards. Each entry is "
        '{"label": "<1-3 word noun, lowercase>", '
        '"box_2d": [ymin, xmin, ymax, xmax], '
        '"kind": "<person|character|creature|animal|machine|object>", '
        '"speaks": <true|false>}. '
        "Box coordinates are integers normalized to 0-1000 (y top-to-bottom, "
        "x left-to-right). Set \"speaks\" true ONLY for something that could "
        "plausibly hold a conversation right now: a visible person, humanoid "
        "figure, named character, sentient creature, or a talking machine "
        "(radio, phone, intercom, robot, terminal with a voice).\n\n"
        "Prefer specific, concrete labels over vague ones. Skip generic "
        "background like 'sky', 'ground', 'wall' unless notable. When an "
        "object persists across frames, REUSE the same label so tags stay "
        "stable — do not rename 'figure' to 'person' on identical pixels."
    )


class LiveVisionSession:
    """Owns one Gemini Live WSS + a background asyncio task per game session.

    Threading model: game code (Flask request handlers, gunicorn worker
    threads) drives this via the sync ``push_frame`` / ``latest`` helpers.
    Internally we run a dedicated event loop in a background thread — same
    pattern ``scene_audio._run_stream_blocking`` uses for Lyria, but held open
    rather than one-shot.

    Everything degrades quietly on error: connection failures set
    ``self._error`` and drain the frame queue; the caller sees ``latest() is
    None`` and falls back to the HTTP path.
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._frame_queue: Optional[asyncio.Queue] = None
        self._latest = {"objects": None, "ts": 0.0}
        self._latest_lock = threading.Lock()
        self._last_push_ts = 0.0
        self._last_send_ts = 0.0
        self._session_started_ts = 0.0
        self._error: Optional[str] = None
        # Bounded push history so a runaway producer can't OOM us. We only ever
        # send the freshest frame, dropping older ones.
        self._pending = deque(maxlen=1)

    # ── public sync API ────────────────────────────────────────────────────
    def push_frame(self, jpeg_bytes: bytes, mime_type: str = "image/jpeg",
                   scene_prompt: str = "") -> None:
        self._last_push_ts = time.time()
        self._ensure_worker()
        if self._loop is None or self._frame_queue is None:
            return
        item = {"data": jpeg_bytes, "mime": mime_type or "image/jpeg",
                "scene_prompt": scene_prompt or ""}
        # Drop the previous unsent frame if one's still queued — we only care
        # about the freshest available image.
        self._pending.clear()
        self._pending.append(item)
        try:
            self._loop.call_soon_threadsafe(self._enqueue_latest)
        except RuntimeError:
            pass  # loop already closed

    def latest(self) -> Optional[list]:
        with self._latest_lock:
            return list(self._latest["objects"]) if self._latest["objects"] is not None else None

    def close(self) -> None:
        if self._loop and self._stop_event is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._loop = None
        self._thread = None
        self._frame_queue = None
        self._stop_event = None

    # ── internal helpers ───────────────────────────────────────────────────
    def _enqueue_latest(self) -> None:
        # Runs on the worker loop thread. Move the freshest pending frame into
        # the async queue that the worker coroutine consumes.
        if not self._pending or self._frame_queue is None:
            return
        frame = self._pending.popleft()
        # Non-blocking put; if the queue is full (worker fell behind), drop.
        try:
            if self._frame_queue.full():
                _ = self._frame_queue.get_nowait()
            self._frame_queue.put_nowait(frame)
        except asyncio.QueueEmpty:
            pass

    def _ensure_worker(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        started = threading.Event()

        def _run():
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            self._stop_event = asyncio.Event()
            self._frame_queue = asyncio.Queue(maxsize=1)
            started.set()
            try:
                loop.run_until_complete(self._worker())
            except Exception as exc:  # noqa: BLE001 — never propagate
                self._error = f"{type(exc).__name__}: {exc}"
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        self._thread = threading.Thread(
            target=_run, name=f"gemini-live-vision:{self.session_id}", daemon=True
        )
        self._thread.start()
        started.wait(timeout=2)

    async def _worker(self) -> None:
        """Run one or more Live sessions in a row until stop / idle.

        A single ``async with client.aio.live.connect(...)`` block owns one
        WSS; when it approaches ``MAX_SESSION_SECONDS`` (or Gemini closes it
        with GoAway) we exit the block and start a fresh one.

        Circuit breaker: if we fail to open a session ``MAX_CONSECUTIVE_ERRORS``
        times in a row (bad model name, revoked key, sustained upstream outage)
        we stop trying and let the caller fall back to ``/api/detect``. A
        successful connect resets the counter.
        """
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=_api_key())
        config = {
            "response_modalities": ["TEXT"],
            "system_instruction": {
                "parts": [{"text": _system_instruction(MAX_DETECTIONS)}],
            },
        }

        MAX_CONSECUTIVE_ERRORS = 5
        consecutive_errors = 0

        while not self._stop_event.is_set():
            # Idle-close: if no frames have arrived recently, don't hold a
            # billed WSS open just to wait.
            if (self._last_push_ts and
                    time.time() - self._last_push_ts > IDLE_CLOSE_SECONDS):
                self._stop_event.set()
                break

            try:
                async with client.aio.live.connect(model=LIVE_MODEL, config=config) as session:
                    consecutive_errors = 0
                    self._session_started_ts = time.time()
                    # Fan out: producer streams frames from the queue into the
                    # Live session; consumer collects text and updates latest.
                    prod = asyncio.create_task(self._send_frames(session, types))
                    cons = asyncio.create_task(self._recv_detections(session))
                    stop_wait = asyncio.create_task(self._stop_event.wait())
                    done, pending = await asyncio.wait(
                        {prod, cons, stop_wait},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                    for t in pending:
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
            except Exception as exc:  # noqa: BLE001
                # Transient — record the error and try to reconnect with an
                # exponential-with-cap backoff, unless we've clearly hit a
                # permanent failure (see circuit breaker above).
                consecutive_errors += 1
                self._error = f"{type(exc).__name__}: {exc}"
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    self._stop_event.set()
                    break
                await asyncio.sleep(min(1.0 * (2 ** (consecutive_errors - 1)), 10.0))
                continue

    async def _send_frames(self, session, types) -> None:
        """Producer coroutine: dequeue frames and forward them to Gemini."""
        first_frame = True
        while not self._stop_event.is_set():
            # Rotate the session periodically so we don't get killed mid-run.
            if (self._session_started_ts and
                    time.time() - self._session_started_ts > MAX_SESSION_SECONDS):
                return
            try:
                frame = await asyncio.wait_for(self._frame_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            now = time.time()
            wait = MIN_FRAME_INTERVAL_SECONDS - (now - self._last_send_ts)
            if wait > 0:
                await asyncio.sleep(wait)

            # Fold the current scene prompt into the FIRST frame's turn as
            # scaffolding text — same reason the HTTP path does, but here it
            # only needs to be sent once per session (system-instruction-like
            # behavior isn't guaranteed on preview Live models).
            if first_frame and frame.get("scene_prompt"):
                try:
                    await session.send_realtime_input(
                        text=(
                            "Scene prompt for the frames that follow (labels "
                            "should be story-grounded but never invent objects "
                            "not actually visible):\n"
                            f"{frame['scene_prompt'][:500]}"
                        )
                    )
                except Exception:
                    pass
            first_frame = False

            try:
                await session.send_realtime_input(
                    video=types.Blob(data=frame["data"], mime_type=frame["mime"])
                )
                self._last_send_ts = time.time()
            except Exception as exc:  # noqa: BLE001
                self._error = f"send: {type(exc).__name__}: {exc}"
                return

    async def _recv_detections(self, session) -> None:
        """Consumer coroutine: parse each text turn as a detection array."""
        buffer_text = []
        async for response in session.receive():
            if self._stop_event.is_set():
                return
            # Newer SDKs expose response.text; also fall back to walking
            # server_content.model_turn.parts so this works across the
            # 1.x preview model matrix.
            text_chunk = getattr(response, "text", None)
            if text_chunk:
                buffer_text.append(text_chunk)

            server_content = getattr(response, "server_content", None)
            if server_content is not None:
                model_turn = getattr(server_content, "model_turn", None)
                if model_turn is not None:
                    for part in getattr(model_turn, "parts", []) or []:
                        pt = getattr(part, "text", None)
                        if pt:
                            buffer_text.append(pt)
                if getattr(server_content, "turn_complete", False):
                    joined = "".join(buffer_text).strip()
                    buffer_text.clear()
                    objects = _parse_detection_payload(joined)
                    if objects is not None:
                        with self._latest_lock:
                            self._latest = {"objects": objects, "ts": time.time()}


# ─────────────────────────────────────────────────────────────────────────────
# Parsing helpers (shared with the HTTP path in engine.py — kept independent
# so this module is self-contained; if the two ever diverge, prefer engine.py)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_detection_payload(text: str) -> Optional[list]:
    """Turn Gemini's text turn into a normalized detection list, or None.

    Returns ``None`` (not ``[]``) when parsing fails, so callers can tell
    "no detections this frame" (empty array) apart from "we couldn't decode
    anything usable" (still hold the last good result).
    """
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(cleaned)
    except Exception:
        m = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(0))
        except Exception:
            return None
    if not isinstance(parsed, list):
        return None

    # Re-use engine._classify_speaker so the TALK affordance behaves identically
    # to the HTTP path. Imported lazily to avoid a module-level engine import
    # (this module needs to be safe to import in isolation).
    try:
        from engine import _classify_speaker
    except Exception:
        def _classify_speaker(label, kind_raw, speaks_raw):  # type: ignore
            return (str(kind_raw or "object").lower(), bool(speaks_raw))

    objects = []
    seen = set()
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip().lower()
        box = entry.get("box_2d") or entry.get("box") or entry.get("bbox")
        if not label or not isinstance(box, (list, tuple)) or len(box) < 4:
            continue
        try:
            ymin, xmin, ymax, xmax = (float(box[0]), float(box[1]),
                                      float(box[2]), float(box[3]))
        except Exception:
            continue
        ymin, ymax = sorted((ymin / 1000.0, ymax / 1000.0))
        xmin, xmax = sorted((xmin / 1000.0, xmax / 1000.0))
        cx = max(0.0, min(1.0, (xmin + xmax) / 2.0))
        cy = max(0.0, min(1.0, (ymin + ymax) / 2.0))
        w = max(0.0, min(1.0, xmax - xmin))
        h = max(0.0, min(1.0, ymax - ymin))
        if w <= 0.001 or h <= 0.001 or (w >= 0.98 and h >= 0.98):
            continue
        key = label[:24]
        if key in seen:
            continue
        seen.add(key)
        kind, speaks = _classify_speaker(label, entry.get("kind"), entry.get("speaks"))
        objects.append({
            "label": label[:40],
            "cx": round(cx, 4),
            "cy": round(cy, 4),
            "w": round(w, 4),
            "h": round(h, 4),
            "kind": kind,
            "speaks": speaks,
        })
        if len(objects) >= MAX_DETECTIONS:
            break
    return objects


# ─────────────────────────────────────────────────────────────────────────────
# Session registry
# ─────────────────────────────────────────────────────────────────────────────

_SESSIONS_LOCK = threading.Lock()
_SESSIONS: dict = {}


def _get_or_create(session_id: str) -> LiveVisionSession:
    with _SESSIONS_LOCK:
        ses = _SESSIONS.get(session_id)
        if ses is None:
            ses = LiveVisionSession(session_id)
            _SESSIONS[session_id] = ses
        return ses


def push_frame(session_id: str, jpeg_bytes: bytes, *,
               mime_type: str = "image/jpeg", scene_prompt: str = "") -> None:
    if not is_available():
        return
    _get_or_create(session_id).push_frame(jpeg_bytes, mime_type, scene_prompt)


def get_latest_detections(session_id: str) -> Optional[list]:
    if not is_available():
        return None
    with _SESSIONS_LOCK:
        ses = _SESSIONS.get(session_id)
    if ses is None:
        return None
    return ses.latest()


def stop(session_id: str) -> None:
    with _SESSIONS_LOCK:
        ses = _SESSIONS.pop(session_id, None)
    if ses is not None:
        ses.close()


def stop_all() -> None:
    with _SESSIONS_LOCK:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
    for ses in sessions:
        ses.close()
