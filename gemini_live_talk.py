"""
gemini_live_talk.py — experimental Gemini Live-API path for the TALK
mechanic (voice + optional vision), as an alternative to the default
ElevenLabs Conversational-AI path.

The default TALK stack (see ``engine.api_talk_session`` /
``engine.api_talk_message``) has two paths:
  * VOICE — the browser opens an ElevenLabs Conversational-AI session with a
    persona override; the agent speaks and listens directly.
  * TEXT  — the browser POSTs each turn to ``/api/talk/message``, which asks
    Gemini for one line and returns it.

Both paths ultimately build their persona from ``engine.build_talk_context``.
PR #(this one) already vision-grounds that context via
``_talk_vision_snapshot`` so the character can reference what's actually on
screen even in the ElevenLabs path.

This module goes one step further: it lets the browser bypass ElevenLabs
entirely and talk **directly** to a Gemini Live session with
``response_modalities=["AUDIO"]`` and the persona as the system
instruction. Vision frames (JPEG ≤ 1 FPS) can ride the same session so the
character literally sees the scene the player is looking at.

Opt-in, always. Nothing here executes unless ``TALK_LIVE_API=1``.

# Wire

The browser opens a WebSocket at ``/ws/talk/live`` (registered by
``api.py`` only when this module is available). First message is a JSON
handshake:

    {"type": "start",
     "subject": {"label": "figure", "kind": "person", "speaks": true},
     "session_id": "default"}

Then the client streams:
  * Binary frames — raw 16-bit PCM 16 kHz mono audio (mic capture)
  * JSON ``{"type": "frame", "data": "data:image/jpeg;base64,..."}`` — an
    optional live vision frame, rate-limited server-side to ≤ 1 FPS

The server relays audio+frames to Gemini Live and streams back:
  * Binary — 16-bit PCM 24 kHz mono audio (the character speaking)
  * JSON ``{"type": "transcript", "role": "user"|"assistant", "text": "..."}``
    — either side's speech-to-text
  * JSON ``{"type": "end"}`` on session close / rotation

# What this module ISN'T

It's a **prototype**. It has been syntax-checked, unit-tested for the
message-parsing helpers, and had its route-registration verified. It has
NOT been round-tripped against real audio hardware inside this cloud env
(no mic, no speaker). Ship it disabled, enable it in a browser + real key
+ mic to validate.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import threading
import time
from typing import Optional

# Match the ambient vision path so the Live-audio TALK model has the same
# perception vocabulary the ElevenLabs / text paths get through
# _talk_vision_snapshot. Kept independent so this module is safe to import
# in isolation.
DEFAULT_LIVE_AUDIO_MODEL = os.getenv(
    "GEMINI_LIVE_TALK_MODEL",
    "gemini-2.5-flash-native-audio-preview-09-2025",
)

# Live API caps video input at ≤ 1 FPS. We enforce it server-side so a chatty
# client can't burn Gemini rate-limit budget.
MIN_VIDEO_INTERVAL_SECONDS = float(os.getenv("GEMINI_LIVE_TALK_MIN_VIDEO_INTERVAL", "1.1"))

# TALK sessions typically last a while (a minute or two of back-and-forth).
# Audio+video Live sessions cap at ~2 min without compression — rotate a hair
# under that so we can seam a fresh session instead of getting GoAway'd
# mid-sentence.
MAX_SESSION_SECONDS = float(os.getenv("GEMINI_LIVE_TALK_MAX_SECONDS", "100"))

# Idle-close in case the client half-crashes without sending a proper close.
IDLE_CLOSE_SECONDS = float(os.getenv("GEMINI_LIVE_TALK_IDLE_CLOSE", "30"))


# ─────────────────────────────────────────────────────────────────────────────
# Feature flag / dependency probe
# ─────────────────────────────────────────────────────────────────────────────

def _flag_on() -> bool:
    return os.getenv("TALK_LIVE_API", "").strip() in ("1", "true", "yes", "on")


def _api_key() -> str:
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
    """True when the Live TALK path is opted-in and can plausibly connect."""
    return _flag_on() and bool(_api_key()) and _sdk_importable()


# ─────────────────────────────────────────────────────────────────────────────
# Frame parsing helpers (module-level so they're unit-testable without WS)
# ─────────────────────────────────────────────────────────────────────────────

_DATA_URL_RE = re.compile(r"^data:(image/[^;]+);base64,(.*)$", re.DOTALL)


def _decode_video_frame(payload: dict) -> Optional[tuple]:
    """Turn a ``{"type":"frame","data":"data:image/jpeg;base64,..."}`` payload
    into ``(bytes, mime_type)`` or ``None`` if the payload isn't usable.

    Kept pure so it can be unit-tested without a live socket.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "frame":
        return None
    raw = payload.get("data")
    if not isinstance(raw, str) or not raw:
        return None
    match = _DATA_URL_RE.match(raw)
    if match:
        mime = match.group(1)
        body = match.group(2)
    else:
        mime = "image/jpeg"
        body = raw
    try:
        image_bytes = base64.b64decode(body, validate=False)
    except Exception:
        return None
    if len(image_bytes) < 512:
        return None
    return image_bytes, mime


def _extract_transcripts(server_content) -> list:
    """Walk a Gemini Live ``server_content`` and yield transcript events.

    Returns a list of ``(role, text)`` tuples where role is ``"user"`` or
    ``"assistant"``. Newer SDK preview builds expose these under different
    attribute names; check them all defensively.
    """
    out = []
    if server_content is None:
        return out
    input_t = getattr(server_content, "input_transcription", None)
    if input_t is not None:
        text = getattr(input_t, "text", None) or ""
        if text.strip():
            out.append(("user", text.strip()))
    output_t = getattr(server_content, "output_transcription", None)
    if output_t is not None:
        text = getattr(output_t, "text", None) or ""
        if text.strip():
            out.append(("assistant", text.strip()))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# The bridge: shuttle bytes between one browser WS and one Gemini Live session
# ─────────────────────────────────────────────────────────────────────────────

class LiveTalkBridge:
    """Owns one Gemini Live session on behalf of one browser WebSocket.

    Lifecycle: ``run(ws)`` is called from the Flask/flask-sock handler on the
    request thread; it spins up an asyncio loop in the same thread (the WS
    already owns the thread for its duration), builds the persona system
    instruction from ``engine.build_talk_context``, opens the Live session,
    and pumps messages both ways until the browser or Gemini closes.

    Any error terminates the session gracefully — the browser can fall back
    to ``/api/talk/session`` (ElevenLabs) or ``/api/talk/message`` (text).
    """

    def __init__(self, ws, subject: dict, session_id: str = "default"):
        self.ws = ws
        self.subject = subject or {}
        self.session_id = session_id
        self._last_video_send_ts = 0.0
        self._last_client_activity = time.time()

    # ── entry point (sync) ─────────────────────────────────────────────────
    def run(self) -> None:
        try:
            asyncio.run(self._run_async())
        except Exception as exc:  # noqa: BLE001
            self._safe_send_json({
                "type": "error",
                "message": f"live-talk failed: {type(exc).__name__}: {exc}",
            })

    async def _run_async(self) -> None:
        from google import genai
        from google.genai import types

        # Persona (vision-grounded via _talk_vision_snapshot) — this is the
        # same context the ElevenLabs voice agent and the /api/talk/message
        # text path build from, so switching transports doesn't change the
        # character.
        try:
            import engine
            ctx = engine.build_talk_context(self.subject, self.session_id)
        except Exception as exc:
            self._safe_send_json({"type": "error",
                                  "message": f"context build failed: {exc}"})
            return

        persona = ctx["persona_prompt"]
        opening = ctx.get("opening_line") or ""

        # Tell the model up front who to be, and (critically) that it must
        # SPEAK its lines (native audio out). The opening line rides along so
        # the character greets the player without a client "start" prompt.
        system_text = (
            persona
            + "\n\nHOW YOU SPEAK NOW: You are speaking OUT LOUD to the "
            "investigator. Deliver one to two short sentences at a time. Keep "
            "silences short but real; do not narrate them.\n"
            + (f"\nSAY THIS FIRST, unprompted: {opening}\n" if opening else "")
        )

        client = genai.Client(api_key=_api_key())
        config = {
            "response_modalities": ["AUDIO"],
            "system_instruction": {"parts": [{"text": system_text}]},
        }

        # Announce mode + opening line up front so the browser can render the
        # bubble instantly without waiting for the model's transcript event.
        self._safe_send_json({
            "type": "start",
            "subject": ctx["subject"],
            "opening_line": opening,
            "voice_source": "gemini-live",
            "vision_available": bool(ctx.get("vision", {}).get("visible")),
        })

        try:
            async with client.aio.live.connect(model=DEFAULT_LIVE_AUDIO_MODEL,
                                               config=config) as session:
                producer = asyncio.create_task(self._client_to_gemini(session, types))
                consumer = asyncio.create_task(self._gemini_to_client(session))
                deadline = asyncio.create_task(asyncio.sleep(MAX_SESSION_SECONDS))
                done, pending = await asyncio.wait(
                    {producer, consumer, deadline},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                for t in pending:
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
        finally:
            self._safe_send_json({"type": "end"})

    # ── producer: browser → Gemini ─────────────────────────────────────────
    async def _client_to_gemini(self, session, types) -> None:
        """Read the next thing from the browser WS and hand it to Gemini.

        flask-sock's ``ws.receive`` is blocking — we run each receive in a
        thread executor so it doesn't stall the asyncio loop that
        ``_gemini_to_client`` needs to keep servicing.
        """
        loop = asyncio.get_running_loop()
        while True:
            # Idle-timeout: if the client has been silent for too long the
            # session ends. Prevents zombie sessions holding billed WSS open.
            if time.time() - self._last_client_activity > IDLE_CLOSE_SECONDS:
                return
            try:
                message = await loop.run_in_executor(
                    None, lambda: self.ws.receive(timeout=IDLE_CLOSE_SECONDS)
                )
            except Exception:
                return
            if message is None:
                return
            self._last_client_activity = time.time()

            if isinstance(message, (bytes, bytearray)):
                # Raw mic audio — 16-bit PCM 16 kHz mono.
                try:
                    await session.send_realtime_input(
                        audio=types.Blob(data=bytes(message),
                                         mime_type="audio/pcm;rate=16000")
                    )
                except Exception:
                    return
                continue

            # Text/control messages are JSON.
            try:
                payload = json.loads(message)
            except Exception:
                continue
            mtype = payload.get("type") if isinstance(payload, dict) else None
            if mtype == "frame":
                # Video frame; throttled server-side so a chatty client can't
                # exceed the Live API's 1 FPS cap.
                now = time.time()
                if now - self._last_video_send_ts < MIN_VIDEO_INTERVAL_SECONDS:
                    continue
                decoded = _decode_video_frame(payload)
                if decoded is None:
                    continue
                img_bytes, mime = decoded
                try:
                    await session.send_realtime_input(
                        video=types.Blob(data=img_bytes, mime_type=mime)
                    )
                    self._last_video_send_ts = now
                except Exception:
                    return
            elif mtype == "text":
                text = str(payload.get("text") or "").strip()
                if not text:
                    continue
                try:
                    await session.send_realtime_input(text=text)
                except Exception:
                    return
            elif mtype == "end":
                return
            # Silently ignore unknown message types — the browser and server
            # are versioned independently.

    # ── consumer: Gemini → browser ─────────────────────────────────────────
    async def _gemini_to_client(self, session) -> None:
        async for response in session.receive():
            # Model audio output — 16-bit PCM 24 kHz mono per the Live spec.
            data = getattr(response, "data", None)
            if data:
                self._safe_send_bytes(bytes(data))

            # Also relay speech-to-text transcripts (both sides) so the UI can
            # render bubbles alongside the audio.
            server_content = getattr(response, "server_content", None)
            for role, text in _extract_transcripts(server_content):
                self._safe_send_json(
                    {"type": "transcript", "role": role, "text": text}
                )
            if server_content is not None and getattr(server_content, "turn_complete", False):
                self._safe_send_json({"type": "turn_complete"})

    # ── safe send helpers ──────────────────────────────────────────────────
    def _safe_send_json(self, obj: dict) -> None:
        try:
            self.ws.send(json.dumps(obj))
        except Exception:
            pass

    def _safe_send_bytes(self, data: bytes) -> None:
        try:
            self.ws.send(data)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Session registry (mostly for future stop-all-on-shutdown; the bridge itself
# is short-lived — one per WS)
# ─────────────────────────────────────────────────────────────────────────────

_SESSIONS_LOCK = threading.Lock()
_SESSIONS: set = set()


def handle_websocket(ws, subject: dict, session_id: str = "default") -> None:
    """Blocking entry point called by the Flask WebSocket route.

    Returns when the browser disconnects, when Gemini closes the session, or
    when ``MAX_SESSION_SECONDS`` fires. Safe to call directly from a request
    thread; runs its own asyncio loop internally.
    """
    if not is_available():
        try:
            ws.send(json.dumps({"type": "error",
                                "message": "live-talk disabled"}))
        except Exception:
            pass
        return
    bridge = LiveTalkBridge(ws, subject, session_id)
    with _SESSIONS_LOCK:
        _SESSIONS.add(bridge)
    try:
        bridge.run()
    finally:
        with _SESSIONS_LOCK:
            _SESSIONS.discard(bridge)
