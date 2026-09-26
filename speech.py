"""speech.py — every spoken line in the game, on the player's one key.

The narrator and the people you TALK to used to speak through ElevenLabs:
a Convai agent session opened in the browser for every line (the narrator's
"first message" was the line), and a public agent id baked into the engine
so it worked without a key. That was a second account for a player to make,
and without one every keyless install talked on whoever owned that agent.

Now a line is text-to-speech on the key the player already gave us:

  * Gemini (the default): ``gemini-3.8-flash-tts`` through the Interactions
    API, the line as text and the delivery ("low, close to the microphone")
    in its own ``speech_metadata.style`` annotation. A voice is one of
    Gemini's 30 prebuilt voices by name ("Charon") or a voice designed from a
    description (``voice_…``, see voice_design.py).
  * OpenAI: a Gemini ``generateContent`` request (``build_request``) that
    provider_bridge answers with ``/v1/audio/speech`` (``gpt-4o-mini-tts``),
    the delivery split back out into OpenAI's ``instructions``.

WHY THE DELIVERY NEEDS ITS OWN FIELD. The first version put it in the
prompt as ``<style>:\\n<line>`` — Google's one-adverb example ("Say
cheerfully: ...") stretched to a paragraph — and the model READ THE
DIRECTION ALOUD: a live check transcribed "Say this in character. An older
voice for a person called Old Rancher. Timbre, gravelly..." in front of the
answer, 47 s of audio for a 32-word line. Markdown headings (``### DIRECTOR'S
NOTES`` / ``### TRANSCRIPT``, what build_prompt still makes for the bridge)
fixed TALK and still leaked about half the narrator lines (2 of 4, probed);
a system instruction is refused ("Developer instruction is not enabled for
this model"). The Interactions annotation went 6 for 6 with the same long
directions, transcribed, 2026-09-25. And as a last line of defence a line
that comes back far longer than its words (``_sounds_read_aloud``) is said
again plain, because a narrator reciting its stage directions is worse
than a flat one.

Measured 2026-09-25 on the real key: a 24-word narrator line is ~6 s to
synthesise on Flash and ~5.8 s on Flash-Lite, ~$0.005 a line. Lines are
cached on disk by (text, voice, style, model), so a replayed tape or a
repeated stinger line costs nothing the second time.

Never raises; every failure is ``None`` and the caller shows the subtitle.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import threading
import time
import wave
from pathlib import Path
from typing import Optional

import paths as _paths

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
# Flash reads with more colour than Flash-Lite for the same latency (the spike
# measured both within half a second of each other), and a narrator line is
# a handful of seconds of audio, so the price difference is fractions of a
# cent. SOMEWHERE_TTS_MODEL swaps it without a release.
DEFAULT_MODEL = "gemini-3.8-flash-tts"
# The headings around the delivery and the line. provider_bridge reads them.
NOTES_HEAD = "### DIRECTOR'S NOTES\n"
TRANSCRIPT_HEAD = "\n\n### TRANSCRIPT\n"
MAX_CHARS = 2500
TIMEOUT_S = 45

_CACHE_LOCK = threading.Lock()


def model() -> str:
    return (os.getenv("SOMEWHERE_TTS_MODEL") or DEFAULT_MODEL).strip()


def cache_dir() -> Path:
    # .cache/ is wiped by a reset (CLAUDE.md, rule 7) — right for this: a
    # line is cheap to say again and the folder must not grow for ever.
    return _paths.data_root() / ".cache" / "speech"


def _mock() -> bool:
    try:
        import provider_bridge
        return provider_bridge._mock_forced()
    except Exception:
        return False


def can_speak() -> bool:
    """True when a line would actually be voiced: a real key for the chosen
    provider, and not mock mode (which has no audio to give)."""
    if _mock():
        return False
    try:
        import provider_bridge
        if provider_bridge.active():
            return bool(provider_bridge.openai_key())
        return provider_bridge.can_call_gemini_api()
    except Exception:
        return bool((os.getenv("GEMINI_API_KEY") or "").strip())


def voice_config(voice: str) -> dict:
    """Gemini's speechConfig.voiceConfig for a voice name or a designed id."""
    v = (voice or "").strip()
    if v.startswith("voice_"):
        return {"voice": v}
    return {"prebuiltVoiceConfig": {"voiceName": v or "Charon"}}


def build_prompt(text: str, style: str = "") -> str:
    text = (text or "").strip()[:MAX_CHARS]
    style = (style or "").strip()
    return f"{NOTES_HEAD}{style}{TRANSCRIPT_HEAD}{text}" if style else text


def split_prompt(prompt: str):
    """(style, text) back out of build_prompt's form. For the bridge."""
    prompt = prompt or ""
    if prompt.startswith(NOTES_HEAD) and TRANSCRIPT_HEAD in prompt:
        notes, _, text = prompt[len(NOTES_HEAD):].partition(TRANSCRIPT_HEAD)
        return notes.strip(), text.strip()
    return "", prompt.strip()


def build_request(text: str, voice: str, style: str = "") -> dict:
    return {
        "contents": [{"role": "user", "parts": [{"text": build_prompt(text, style)}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": voice_config(voice)},
        },
    }


def _pcm_to_wav(raw: bytes, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(raw)
    return buf.getvalue()


def _as_wav(raw: bytes, mime: str) -> bytes:
    """Gemini answers WAV on the unary path and bare 16-bit PCM when streamed
    (``audio/L16;rate=24000``). The browser plays only the first."""
    if raw[:4] == b"RIFF":
        return raw
    rate = 24000
    for piece in (mime or "").split(";"):
        piece = piece.strip().lower()
        if piece.startswith("rate="):
            try:
                rate = int(piece[5:])
            except ValueError:
                pass
    return _pcm_to_wav(raw, rate)


def _key(text: str, voice: str, style: str) -> str:
    material = "\x1f".join((model(), voice or "", style or "", text or ""))
    return hashlib.sha1(material.encode("utf-8", "ignore")).hexdigest()[:24]


def cached_path(text: str, voice: str, style: str = "") -> Path:
    return cache_dir() / f"{_key(text, voice, style)}.wav"


def build_interaction(text: str, voice: str, style: str = "") -> dict:
    content = {"type": "text", "text": (text or "").strip()[:MAX_CHARS]}
    style = (style or "").strip()
    if style:
        content["annotations"] = [{"type": "speech_metadata", "style": style[:1000]}]
    return {
        "model": model(),
        "input": [{"type": "user_input", "content": [content]}],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": (voice or "Charon").strip()}]},
    }


def _seconds(wav: bytes) -> float:
    return max(0, len(wav) - 44) / 48000.0   # 24 kHz, 16-bit, mono


def _sounds_read_aloud(text: str, wav: bytes) -> bool:
    """Far longer than its words: the direction was spoken with the line.
    A slow, pausing read of this game's lines measured ~0.45 s a word (15 s
    for 35 words); a leaked direction added 12-35 s to lines of 20-35 words."""
    words = max(1, len((text or "").split()))
    return _seconds(wav) > words * 0.8 + 6.0


def _session_id() -> str:
    try:
        import engine
        return engine.get_active_session_id() or "default"
    except Exception:
        return "default"


def _gemini_line(key: str, text: str, voice: str, style: str) -> Optional[bytes]:
    import requests
    t0 = time.time()
    resp = requests.post(
        f"{GEMINI_API}/interactions",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json=build_interaction(text, voice, style),
        timeout=TIMEOUT_S,
    )
    ms = int((time.time() - t0) * 1000)
    body = {}
    try:
        body = resp.json() or {}
    except Exception:
        body = {}
    usage = body.get("usage") or {}
    ok = resp.status_code == 200
    # The wire meter only sees generateContent; this call is logged here.
    try:
        import cost_tracker
        cost_tracker.record_usage(
            _session_id(), "voice", "gemini", model(), operation="tts",
            input_units=usage.get("total_input_tokens") if ok else None,
            output_units=usage.get("total_output_tokens") if ok else None,
            unit_type="tokens", latency_ms=ms, success=ok,
            error_message=None if ok else f"http_{resp.status_code}")
    except Exception:
        pass
    if not ok:
        print(f"[SPEECH] {model()} http {resp.status_code}: {resp.text[:180]}", flush=True)
        try:
            import provider_bridge
            if resp.status_code in (401, 403, 429):
                provider_bridge.note_problem("gemini", resp.status_code, provider_bridge._err_text(resp))
        except Exception:
            pass
        return None
    for step in body.get("steps") or []:
        for part in (step or {}).get("content") or []:
            if isinstance(part, dict) and part.get("type") == "audio" and part.get("data"):
                return _as_wav(base64.b64decode(part["data"]), str(part.get("mime_type") or ""))
    print(f"[SPEECH] no audio in the answer: {str(body)[:180]}", flush=True)
    return None


def _bridged_line(text: str, voice: str, style: str) -> Optional[bytes]:
    """OpenAI chosen: a generateContent request provider_bridge answers."""
    import requests
    resp = requests.post(
        f"{GEMINI_API}/models/{model()}:generateContent",
        headers={"Content-Type": "application/json"},
        json=build_request(text, voice, style),
        timeout=TIMEOUT_S,
    )
    if resp.status_code != 200:
        print(f"[SPEECH] bridged http {resp.status_code}: {resp.text[:180]}", flush=True)
        return None
    body = resp.json() or {}
    for cand in body.get("candidates") or []:
        for part in ((cand or {}).get("content") or {}).get("parts") or []:
            data = (part or {}).get("inlineData") or (part or {}).get("inline_data")
            if isinstance(data, dict) and data.get("data"):
                return _as_wav(base64.b64decode(data["data"]),
                               str(data.get("mimeType") or data.get("mime_type") or ""))
    return None


def synthesize(text: str, voice: str, style: str = "") -> Optional[bytes]:
    """One line, spoken. WAV bytes, or None (no key, mock, refusal, timeout)."""
    text = (text or "").strip()
    if not text or not can_speak():
        return None
    path = cached_path(text, voice, style)
    try:
        if path.is_file() and path.stat().st_size > 44:
            return path.read_bytes()
    except OSError:
        pass
    t0 = time.time()
    try:
        import provider_bridge
        bridged = provider_bridge.active()
        if bridged:
            wav = _bridged_line(text, voice, style)
        else:
            key = provider_bridge.gemini_key()
            wav = _gemini_line(key, text, voice, style)
            if wav and style and _sounds_read_aloud(text, wav):
                print(f"[SPEECH] {_seconds(wav):.0f}s for {len(text.split())} words: the "
                      f"direction was read aloud; saying it again plain", flush=True)
                wav = _gemini_line(key, text, voice, "") or wav
        if not wav:
            return None
        _store(path, wav)
        print(f"[SPEECH] {len(text)} chars -> {_seconds(wav):.0f}s audio in "
              f"{time.time() - t0:.1f}s ({voice or 'default'}"
              f"{', via OpenAI' if bridged else ''})", flush=True)
        return wav
    except Exception as e:  # noqa: BLE001
        print(f"[SPEECH] failed: {type(e).__name__}: {e}", flush=True)
    return None


def _store(path: Path, wav: bytes) -> None:
    try:
        with _CACHE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(wav)
            os.replace(tmp, path)
    except OSError:
        pass
