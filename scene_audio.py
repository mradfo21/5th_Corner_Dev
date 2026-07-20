"""
scene_audio.py — realtime scene audio from the guide image.

Turns the scene descriptor that already rides along with every guide image
(the `metadata.prompt` the engine emits) into a short, scene-matched
instrumental music clip using Google **Lyria RealTime**
(`models/lyria-realtime-exp`) via the already-installed `google-genai` SDK and
the already-deployed `GEMINI_API_KEY`. No new provider, no new secret.

Increment 1 (this module): open a Lyria RealTime session, buffer a few seconds
of PCM, encode to a WAV, cache it under the session dir, and hand back a web
URL the standalone UI loops as an ambient score. The frontend re-requests audio
on every new scene so the soundtrack re-scores itself as the world changes.

Everything degrades gracefully: if `GEMINI_API_KEY` is unset, the SDK is too old
to expose Lyria, or the stream errors, `get_scene_audio()` returns ``None`` and
the client simply stays silent (mirrors how image-disabled mode is handled).
"""

import asyncio
import hashlib
import json
import os
import threading
import time
import wave
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).parent.resolve()

# Best-effort cost tracking (see ADMIN_COST_ANALYTICS_DASHBOARD_PLAN.md). A
# broken/missing analytics module must never break scene audio.
try:
    import cost_tracker
except Exception:
    class _NoopCostTracker:
        def record_usage(self, *args, **kwargs):
            return None

    cost_tracker = _NoopCostTracker()

# Reuse the same key the rest of the stack already deploys (see
# veo_video_utils.py / engine.py). Env wins, then config.json.
try:
    _CONFIG = json.load((ROOT / "config.json").open(encoding="utf-8"))
except Exception:
    _CONFIG = {}

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", _CONFIG.get("GEMINI_API_KEY", ""))

# Lyria RealTime emits raw 16-bit PCM, 48 kHz, stereo.
LYRIA_MODEL = "models/lyria-realtime-exp"
SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2  # bytes per sample (16-bit)

# How much audio a single scene clip should contain. The client loops it, so a
# dozen seconds is enough to feel like a bed without a long first-scene wait.
DEFAULT_CLIP_SECONDS = 12
# Never block a request forever if the stream stalls.
_STREAM_TIMEOUT_SECONDS = 45


# ────────────────────────────────────────────────────────────────────────────
# Prompt mapping: scene descriptor -> Lyria weighted prompts + generation config
# ────────────────────────────────────────────────────────────────────────────

# Coarse mood cues we can read straight out of the scene descriptor to nudge
# tempo/brightness without an extra model call. Ordered by priority.
_MOOD_CUES = [
    # (keywords, weighted-prompt phrase, bpm, brightness 0..1)
    (("battle", "fight", "chase", "run", "escape", "explosion", "alarm", "attack"),
     "urgent, driving, percussive tension", 128, 0.7),
    (("horror", "terror", "monster", "blood", "corpse", "nightmare", "dread", "haunt"),
     "dark ambient horror, dissonant drones, unsettling", 70, 0.25),
    (("ruin", "abandoned", "decay", "derelict", "empty", "desolate", "wasteland"),
     "bleak, sparse, haunting ambient", 68, 0.3),
    (("forest", "jungle", "garden", "trees", "nature", "meadow", "river", "ocean", "sea"),
     "organic, lush, natural ambience with soft pads", 84, 0.6),
    (("city", "street", "neon", "market", "crowd", "traffic", "station"),
     "cinematic urban underscore, low synth pulse", 96, 0.55),
    (("temple", "shrine", "cathedral", "sacred", "ritual", "ancient"),
     "solemn, reverent, cavernous reverb, choral pads", 66, 0.4),
    (("space", "stars", "void", "cosmic", "nebula", "orbit", "station"),
     "vast cosmic ambient, weightless synth textures", 72, 0.5),
    (("snow", "ice", "frozen", "cold", "winter", "tundra"),
     "cold, crystalline, sparse ambient", 74, 0.45),
    (("cave", "tunnel", "underground", "basement", "sewer", "mine", "dark"),
     "claustrophobic low drones, dripping reverb", 64, 0.2),
    (("dream", "surreal", "strange", "shimmer", "glow", "ethereal"),
     "dreamy, ethereal, shimmering ambient", 80, 0.65),
]

# Always-on style anchors so the score stays a tasteful, vocal-free underscore.
_STYLE_ANCHORS = "cinematic instrumental score, atmospheric, no vocals, no drums lead"


def _clean_scene_text(scene_prompt: str) -> str:
    """Compress a (possibly long, comma-stuffed image) prompt into a short
    descriptor suitable as a music style cue."""
    if not scene_prompt:
        return ""
    text = " ".join(str(scene_prompt).split())
    # Image prompts can be enormous; Lyria only needs the gist.
    return text[:240]


def _scene_to_music_prompt(scene_prompt: str):
    """Map a scene descriptor to (weighted_prompts, generation_config_kwargs).

    Returns plain data (list of {text, weight} dicts + a kwargs dict) so this is
    unit-testable without importing the SDK. The caller converts them to SDK
    types just before the network call.
    """
    scene = _clean_scene_text(scene_prompt)
    low = scene.lower()

    mood_phrase = "calm, mysterious, exploratory ambient"
    bpm = 78
    brightness = 0.5
    for keywords, phrase, cue_bpm, cue_bright in _MOOD_CUES:
        if any(k in low for k in keywords):
            mood_phrase = phrase
            bpm = cue_bpm
            brightness = cue_bright
            break

    prompts = [
        # The scene itself, weighted highest, so the bed tracks the image.
        {"text": (scene or "an unknown place"), "weight": 1.0},
        {"text": mood_phrase, "weight": 0.9},
        {"text": _STYLE_ANCHORS, "weight": 0.6},
    ]
    config = {"bpm": bpm, "brightness": brightness, "temperature": 1.1}
    return prompts, config


# ────────────────────────────────────────────────────────────────────────────
# Lyria RealTime session -> PCM buffer -> WAV
# ────────────────────────────────────────────────────────────────────────────

def _pcm_to_wav_bytes(pcm: bytes) -> bytes:
    """Wrap raw 16-bit/48k/stereo PCM in a WAV container."""
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()


async def _stream_pcm(scene_prompt: str, seconds: int) -> bytes:
    """Open a Lyria RealTime session and collect ~`seconds` of PCM."""
    from google import genai
    from google.genai import types

    prompts, cfg = _scene_to_music_prompt(scene_prompt)

    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options={"api_version": "v1alpha"},
    )

    bytes_needed = int(seconds * SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH)
    collected = bytearray()

    async with client.aio.live.music.connect(model=LYRIA_MODEL) as session:
        await session.set_weighted_prompts(
            prompts=[types.WeightedPrompt(text=p["text"], weight=p["weight"]) for p in prompts]
        )
        await session.set_music_generation_config(
            config=types.LiveMusicGenerationConfig(
                bpm=cfg["bpm"],
                brightness=cfg["brightness"],
                temperature=cfg["temperature"],
            )
        )
        await session.play()

        async for message in session.receive():
            server_content = getattr(message, "server_content", None)
            chunks = getattr(server_content, "audio_chunks", None) if server_content else None
            if chunks:
                data = getattr(chunks[0], "data", None)
                if data:
                    collected.extend(data)
                    if len(collected) >= bytes_needed:
                        break
            # Yield to the loop so back-pressure / cancellation behaves.
            await asyncio.sleep(0)

    return bytes(collected[:bytes_needed])


def _run_stream_blocking(scene_prompt: str, seconds: int) -> bytes:
    """Run the async Lyria stream to completion from a sync (gunicorn) worker.

    Uses a dedicated thread + event loop so it is safe regardless of whether the
    calling thread already has a running loop, and bounds the whole thing with a
    hard timeout so a stalled stream can never hang the request thread forever.
    """
    result = {"pcm": b"", "error": None}

    def _worker():
        try:
            result["pcm"] = asyncio.run(
                asyncio.wait_for(_stream_pcm(scene_prompt, seconds), timeout=_STREAM_TIMEOUT_SECONDS)
            )
        except Exception as e:  # noqa: BLE001 — degrade gracefully, never crash the request
            result["error"] = e

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=_STREAM_TIMEOUT_SECONDS + 5)

    if result["error"] is not None:
        raise result["error"]
    return result["pcm"]


# ────────────────────────────────────────────────────────────────────────────
# Public entry point: cached WAV on disk -> web URL
# ────────────────────────────────────────────────────────────────────────────

def is_available() -> bool:
    """True when we can plausibly generate audio (key present + SDK importable)."""
    if not GEMINI_API_KEY:
        return False
    try:
        import google.genai  # noqa: F401
    except Exception:
        return False
    return True


def _get_audio_dir(session_id: str = "default") -> Path:
    """Per-session scratch dir for generated audio (mirrors the image dir)."""
    try:
        import engine
        audio_dir = Path(engine._get_session_root(session_id)) / "audio"
    except Exception:
        audio_dir = ROOT / "sessions" / session_id / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    return audio_dir


def _cache_name(scene_prompt: str, seconds: int) -> str:
    """Stable filename keyed on the derived music prompt so identical scenes
    reuse the same clip instead of re-billing Lyria."""
    prompts, cfg = _scene_to_music_prompt(scene_prompt)
    key = json.dumps({"p": prompts, "c": cfg, "s": seconds}, sort_keys=True)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"scene_{digest}.wav"


# Coalesce concurrent identical requests so two turns landing at once don't both
# open a (billed) Lyria session for the same scene.
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT = {}


def get_scene_audio(scene_prompt: str, session_id: str = "default",
                    seconds: int = DEFAULT_CLIP_SECONDS) -> dict | None:
    """Return {"audio_url": "/audio/<file>.wav", "cached": bool} for a scene, or
    ``None`` when audio can't be produced (no key / SDK / stream failure)."""
    if not is_available():
        return None

    scene_prompt = _clean_scene_text(scene_prompt)
    if not scene_prompt:
        return None

    audio_dir = _get_audio_dir(session_id)
    fname = _cache_name(scene_prompt, seconds)
    fpath = audio_dir / fname
    web_url = f"/audio/{fname}"

    if fpath.exists() and fpath.stat().st_size > 44:  # >WAV header
        return {"audio_url": web_url, "cached": True}

    # Only one generation per (session, file) in flight at a time.
    ikey = (session_id, fname)
    with _INFLIGHT_LOCK:
        lock = _INFLIGHT.get(ikey)
        if lock is None:
            lock = threading.Lock()
            _INFLIGHT[ikey] = lock

    with lock:
        # Another waiter may have finished while we blocked on the lock.
        if fpath.exists() and fpath.stat().st_size > 44:
            return {"audio_url": web_url, "cached": True}
        gen_t0 = time.time()
        try:
            pcm = _run_stream_blocking(scene_prompt, seconds)
            if not pcm:
                cost_tracker.record_usage(
                    session_id, "voice", "gemini", LYRIA_MODEL, operation="lyria_music",
                    success=False, error_message="empty_pcm",
                    latency_ms=int((time.time() - gen_t0) * 1000),
                )
                return None
            fpath.write_bytes(_pcm_to_wav_bytes(pcm))
            cost_tracker.record_usage(
                session_id, "voice", "gemini", LYRIA_MODEL, operation="lyria_music",
                output_units=seconds, unit_type="seconds", success=True,
                latency_ms=int((time.time() - gen_t0) * 1000),
            )
        except Exception as e:  # noqa: BLE001
            print(f"[SCENE AUDIO] generation failed: {e}", flush=True)
            cost_tracker.record_usage(
                session_id, "voice", "gemini", LYRIA_MODEL, operation="lyria_music",
                success=False, error_message=str(e),
                latency_ms=int((time.time() - gen_t0) * 1000),
            )
            return None
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.pop(ikey, None)

    return {"audio_url": web_url, "cached": False}


def resolve_audio_path(filename: str, session_id: str = "default") -> Path | None:
    """Resolve a served '/audio/<filename>' back to disk (path-traversal safe)."""
    safe = Path(filename).name
    candidate = _get_audio_dir(session_id) / safe
    return candidate if candidate.exists() else None


# ────────────────────────────────────────────────────────────────────────────
# Increment 2 — true realtime streaming (continuous, re-steerable score)
#
# A persistent Lyria RealTime session bridged to a browser WebSocket: raw PCM
# flows out as binary frames; JSON steer messages ({"prompt": ...}) flow in and
# re-weight the prompts live, so the score morphs between scenes instead of
# looping a clip. Opt-in from the client (?music=stream); the clip-loop MVP
# above stays the default.
# ────────────────────────────────────────────────────────────────────────────

def stream_music_over_ws(ws, initial_prompt: str = "") -> None:
    """Blocking bridge between a flask-sock WebSocket (`ws`) and a Lyria session.

    Runs its own asyncio loop for the lifetime of the socket. `ws.send` (bytes)
    ships PCM to the browser; `ws.receive` yields steer JSON. Any error tears the
    session down quietly — the client falls back to the clip loop / silence.
    """
    if not is_available():
        return

    async def _run():
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=GEMINI_API_KEY,
            http_options={"api_version": "v1alpha"},
        )

        async with client.aio.live.music.connect(model=LYRIA_MODEL) as session:
            async def _apply(prompt_text: str, set_config: bool):
                prompts, cfg = _scene_to_music_prompt(prompt_text or "an unknown place")
                await session.set_weighted_prompts(
                    prompts=[types.WeightedPrompt(text=p["text"], weight=p["weight"]) for p in prompts]
                )
                if set_config:
                    await session.set_music_generation_config(
                        config=types.LiveMusicGenerationConfig(
                            bpm=cfg["bpm"], brightness=cfg["brightness"], temperature=cfg["temperature"]
                        )
                    )

            await _apply(initial_prompt, set_config=True)
            await session.play()

            loop = asyncio.get_event_loop()
            stop = asyncio.Event()

            async def pump_audio():
                try:
                    async for message in session.receive():
                        sc = getattr(message, "server_content", None)
                        chunks = getattr(sc, "audio_chunks", None) if sc else None
                        if chunks:
                            data = getattr(chunks[0], "data", None)
                            if data:
                                await loop.run_in_executor(None, ws.send, data)
                        await asyncio.sleep(0)
                        if stop.is_set():
                            break
                except Exception:
                    stop.set()

            async def pump_control():
                try:
                    while not stop.is_set():
                        raw = await loop.run_in_executor(None, lambda: ws.receive(timeout=1))
                        if raw is None:
                            continue  # idle keep-alive tick
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        prompt = (msg.get("prompt") or "").strip()
                        if prompt:
                            # Re-weight prompts only (no BPM change) so the score
                            # morphs smoothly without a reset_context() glitch.
                            await _apply(prompt, set_config=False)
                except Exception:
                    stop.set()

            await asyncio.gather(pump_audio(), pump_control())

    try:
        asyncio.run(_run())
    except Exception as e:  # noqa: BLE001
        print(f"[SCENE AUDIO] stream ended: {e}", flush=True)
