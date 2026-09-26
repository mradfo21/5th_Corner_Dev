"""
voice_design.py — a voice designed for each character, on the player's key.

Turns a SCAN subject (label + kind) into a Gemini voice designed to sound like
*that specific character*, instead of routing every subject through the
static ``by_kind`` roster in ``voices.json``. One call —
``POST /v1beta/voices`` with a ``prompted`` description — designs the voice
and stores it in the key's Google project; the id it answers (``voice_…``) is
what ``speech.py`` hands Gemini TTS as ``voiceConfig.voice``. Designed voices
are cached per session on disk, named with the ``[dyn]`` prefix so the sweep
can tell them from anything else in the project, and DELETED at session end.

Why Gemini and not ElevenLabs any more: ElevenLabs was a second account. A
player who had only pasted the one key ACCOUNT asks for heard the stock
roster forever, and the ones who had both kept hitting ElevenLabs' voice-slot
ceiling (the reason half of this module — the subscription lookup, the v1/v2
listing dance, the ``sk_`` check — existed at all). Gemini 3.8 TTS gained
voice design on 2026-09-23 on the same key that already draws every frame, so
the game now speaks through one key. An OpenAI player has no voice design
(OpenAI cannot build a voice from words); ``is_available()`` is False for
them and the engine plays the roster.

What the Google project allows, and what it shapes here: 200 stored voices
per project, each expiring a year after it was made. Nothing reports how many
of the 200 are used, so the soft cap counts our own ready voices and evicts
the least-recently-used at 180, leaving headroom for designs in flight and for
voices another checkout on the same key is holding.

Design goals (see docs/plans/DYNAMIC_VOICES_PLAN.md):

* Non-blocking hot path — ``get_or_design_voice(..., wait=0)`` never spends
  more than a JSON-encode of latency on the caller's thread; the design call
  runs in a background worker. The caller gets a fallback voice immediately
  and can poll ``/api/talk/voice/status`` (or pass ``wait>0`` to catch the
  ~2 s the call usually takes).
* Byte-identical fallback — every entry point degrades to ``None`` or an
  empty result when the key is missing / the feature is disabled / the game
  is in mock mode or on OpenAI / a request fails, so callers that "OR" a
  fallback in behave exactly as they did before this module existed.
* Slot-safe — a per-session **budget** caps design calls per session, an
  in-memory **refcount** blocks deletion of a voice a live TALK still holds,
  an **LRU eviction** frees the oldest voice as we approach the project's
  200, and a periodic **sweep** reconciles the cache with
  ``GET /v1beta/voices`` to reap ``[dyn]`` orphans left by crashes.

Only depends on ``requests`` + stdlib. The HTTP goes through ``requests`` on
purpose: the cost and provider hooks patch it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).parent.resolve()
import paths as _paths  # where the game writes (M2): the repo from source, %APPDATA%/ABYSS built

# Best-effort cost tracking (see ADMIN_COST_ANALYTICS_DASHBOARD_PLAN.md). A
# broken/missing analytics module must never break voice design.
try:
    import cost_tracker
except Exception:
    class _NoopCostTracker:
        def record_usage(self, *args, **kwargs):
            return None

    cost_tracker = _NoopCostTracker()

# ────────────────────────────────────────────────────────────────────────────
# Configuration (all env-gated with safe defaults)
# ────────────────────────────────────────────────────────────────────────────

try:
    _CONFIG = json.load((ROOT / "config.json").open(encoding="utf-8"))
except Exception:
    _CONFIG = {}


def _cfg(name: str, default: str = "") -> str:
    return (os.getenv(name) or _CONFIG.get(name) or default).strip()


def _cfg_int(name: str, default: int) -> int:
    raw = _cfg(name, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _cfg_bool(name: str, default: bool) -> bool:
    raw = _cfg(name, "1" if default else "0").lower()
    return raw not in ("0", "false", "no", "off", "")


ENABLED = _cfg_bool("SOMEWHERE_DYNAMIC_VOICES", True)
DESIGN_BUDGET_PER_SESSION = _cfg_int("SOMEWHERE_DESIGN_BUDGET_PER_SESSION", 8)
DESIGN_CONCURRENCY = _cfg_int("SOMEWHERE_DESIGN_CONCURRENCY", 3)
VOICE_SOFT_CAP_OVERRIDE = _cfg_int("SOMEWHERE_VOICE_SOFT_CAP", 0)  # 0 = auto
SWEEP_HOURS = _cfg_int("SOMEWHERE_VOICE_SWEEP_HOURS", 6)
MAX_AGE_HOURS = _cfg_int("SOMEWHERE_VOICE_MAX_AGE_HOURS", 24)
FAIL_TTL_SECONDS = _cfg_int("SOMEWHERE_VOICE_FAIL_TTL_SECONDS", 900)
DESIGN_TIMEOUT_SECONDS = _cfg_int("SOMEWHERE_DESIGN_TIMEOUT_SECONDS", 45)
# A "[dyn]" voice on the server that this cache does not know is an orphan —
# but it may also be one a design in flight made a moment ago, or one another
# checkout on the same key is using. It is only reaped once it is this old.
ORPHAN_GRACE_MINUTES = _cfg_int("SOMEWHERE_VOICE_ORPHAN_GRACE_MINUTES", 60)

# The model that designs the voice AND speaks with it (speech.py): a designed
# voice belongs to the model it was designed on.
MODEL = "gemini-3.8-flash-tts"
LANGUAGE_CODE = "en-US"
# Our voices are recognised by this display-name prefix: Gemini voices carry
# no labels, and the sweep must never touch a voice it did not make.
NAME_PREFIX = "[dyn]"
# Google's per-project ceiling on stored voices, and how close we let it get.
PROJECT_VOICE_LIMIT = 200
_DEFAULT_SOFT_CAP = 180
# A stored voice expires this long after it was made; the create answer says
# when it expires, not when it was made, so age is read back from that.
_STORED_LIFETIME = timedelta(days=365)
# The brief builder targets well under this; a stored companion description
# (or an ElevenLabs-era one, which ran to ~990) is trimmed to it.
DESCRIPTION_MAX = 600

# Cache file lives in the data root so it survives `delete_session` (which
# wipes per-session dirs) and stays authoritative across workers/restarts.
# Each entry embeds its own session_id so cross-session sweeps/LRU can inspect
# it. Version 2 is the Gemini cache: a version-1 file holds ElevenLabs ids,
# which Gemini TTS cannot speak and this key cannot delete, so it is dropped.
CACHE_PATH = _paths.data_root() / "voice_design_cache.json"
_CACHE_VERSION = 2

_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
_URL_VOICES = _API_BASE + "/voices"
_URL_VOICE_TPL = _API_BASE + "/voices/{voice_id}"


# ────────────────────────────────────────────────────────────────────────────
# Public availability probe
# ────────────────────────────────────────────────────────────────────────────

def _api_key() -> str:
    """The player's Gemini key, read live (ACCOUNT can paste one mid-run)."""
    try:
        import provider_bridge
        return (provider_bridge.gemini_key() or "").strip()
    except Exception:
        return ""


def _gemini_playing() -> bool:
    """True when real Gemini is what answers this game's calls: not mock
    mode, not an OpenAI player, and a key to call it with."""
    try:
        import provider_bridge
        if provider_bridge._mock_forced():
            return False
        return (provider_bridge.effective_provider() == "gemini"
                and bool(provider_bridge.gemini_key()))
    except Exception:
        return False


def is_available() -> bool:
    """True when we can plausibly design voices.

    Cheap: flag, mock mode, provider and key presence. Quota and model errors
    surface at design time and degrade to the fallback voice.
    """
    return bool(ENABLED and _gemini_playing())


def unavailable_reason() -> str:
    """Why ``is_available()`` is False, in words for the startup log; '' when
    it is True."""
    if not ENABLED:
        return "SOMEWHERE_DYNAMIC_VOICES=0"
    try:
        import provider_bridge
        if provider_bridge._mock_forced():
            return "mock mode"
        if provider_bridge.effective_provider() == "openai":
            return "playing on OpenAI, which cannot design a voice"
        if not provider_bridge.gemini_key():
            return "no Gemini key"
    except Exception as e:  # noqa: BLE001
        return f"provider unknown ({e})"
    return ""


# ────────────────────────────────────────────────────────────────────────────
# Voice-design brief — deterministic classifiers over subject + context
# ────────────────────────────────────────────────────────────────────────────

_FEMALE_HINTS = (
    "woman", "girl", "lady", "mother", "sister", "wife", "queen", "priestess",
    "witch", "widow", "matriarch", "she", "her", "female", "nun", "mistress",
    "actress", "waitress", "hostess",
)
_MALE_HINTS = (
    "man", "boy", "father", "brother", "husband", "king", "priest", "warden",
    "sheriff", "guard", "soldier", "he", "him", "male", "monk", "master",
    "operator", "captain", "detective",
)

_ELDER_HINTS = ("elder", "old", "ancient", "grand", "veteran", "wizened", "crone", "hermit")
_YOUNG_HINTS = ("child", "kid", "boy", "girl", "teen", "young", "youngster")

_MACHINE_HINTS = ("intercom", "radio", "speaker", "phone", "telephone", "handset",
                  "walkie", "loudspeaker", "megaphone", "pa system", "terminal",
                  "computer", "robot", "drone", "camera", "recorder")
_CREATURE_HINTS = ("creature", "beast", "thing", "figure", "silhouette", "shape",
                   "form", "entity", "wraith", "spectre", "ghost")

_HIGH_CHAOS = 7  # inclusive threshold for "frayed, breath-short" emotion


def _norm(text: Any) -> str:
    """Lowercase, whitespace-collapse. Never raises."""
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _first_hit(text: str, hints: Tuple[str, ...]) -> str:
    """Return the first hint word that appears as a token in text, else ''."""
    if not text:
        return ""
    for h in hints:
        if re.search(r"\b" + re.escape(h) + r"\b", text):
            return h
    return ""


def _gender_hint(label: str, kind: str) -> str:
    if kind == "machine":
        return "neutral"
    if _first_hit(label, _FEMALE_HINTS):
        return "female"
    if _first_hit(label, _MALE_HINTS):
        return "male"
    return "unspecified"


def _api_gender(label: str, kind: str) -> str:
    """The create call's ``gender``: "male" or "female", else left out.
    ("neutral" was accepted too on 2026-09-25, but a machine's voice is
    better said by its description than by a field.)"""
    g = _gender_hint(label, kind)
    return g if g in ("male", "female") else ""


def _age_bucket(label: str, kind: str) -> str:
    if kind == "machine":
        return "n/a"
    if _first_hit(label, _ELDER_HINTS):
        return "elder"
    if _first_hit(label, _YOUNG_HINTS):
        return "young"
    return "adult"


def _environment(label: str, kind: str) -> str:
    """How the voice reaches you — permanent for a machine or a creature (an
    intercom is always an intercom), nothing worth saying for a person."""
    if kind == "machine" or _first_hit(label, _MACHINE_HINTS):
        return "filtered through a corroded PA / intercom, faint tape hiss, band-limited"
    if kind in ("creature", "animal") or _first_hit(label, _CREATURE_HINTS):
        return "close and uncomfortably intimate"
    return ""


def _emotion(chaos: int, phase: str, recent: List[str]) -> str:
    chaos = int(chaos or 0)
    phase_l = (phase or "").lower()
    tokens = " ".join(recent).lower() if recent else ""
    if chaos >= _HIGH_CHAOS or phase_l == "climax":
        return "frayed, breath-short, urgent"
    if any(w in tokens for w in ("blood", "corpse", "scream", "attack", "chase")):
        return "shaken, quiet, guarded"
    if any(w in tokens for w in ("safe", "calm", "quiet", "rest")):
        return "measured, wary but composed"
    return "wary, alert, low-affect"


def _delivery(chaos: int, kind: str) -> str:
    if kind == "machine":
        return "clipped, unemotive, deliberate cadence"
    if int(chaos or 0) >= _HIGH_CHAOS:
        return "halting, mid-sentence pauses, short breaths"
    return "measured, thinks between phrases"


def _register(kind: str, chaos: int) -> str:
    if kind == "machine":
        return "conversational, deliberate"
    if int(chaos or 0) >= _HIGH_CHAOS:
        return "hushed to raised whisper, avoiding notice"
    return "conversational, occasionally hushed"


def _timbre(label: str, kind: str, age: str) -> str:
    parts = []
    if kind == "machine":
        parts.append("synthetic, band-limited, faint carrier hum")
    elif kind in ("creature", "animal"):
        parts.append("rough, uncanny resonance, subtle non-human overtones")
    else:
        if age == "elder":
            parts.append("gravelly, worn, faint chest resonance")
        elif age == "young":
            parts.append("light, breathy, thin high-end")
        else:
            parts.append("natural human timbre")
    # Nudge from label keywords
    if _first_hit(label, ("wounded", "hurt", "dying", "bleeding")):
        parts.append("weakened, occasional catch in the throat")
    if _first_hit(label, ("cold", "frozen", "icy")):
        parts.append("shivering under the words")
    if _first_hit(label, ("hostile", "warden", "guard", "hunter")):
        parts.append("hard-edged, commanding")
    return ", ".join(parts)


_DEFAULT_SAMPLE = "There's someone else down here. Stay low and don't say my name."


def _sample_text(opening: str, label: str) -> str:
    """A line in this character's mouth, kept on the cache entry for a
    preview. (Gemini designs from the description alone; the line is no
    longer part of the design call.)
    """
    text = (opening or "").strip()
    if 20 <= len(text) <= 300:
        return text
    # Sneak the label into the fallback so the sample sounds slightly
    # tailored ("the warden's coming back") without being brittle.
    if label and re.match(r"^[a-z][a-z0-9 '\-]{1,30}$", label):
        return f"The {label} is close. Keep still. Don't let them hear you breathe."
    return _DEFAULT_SAMPLE


# Sentences an ElevenLabs-era description carried that are situational or
# addressed to ElevenLabs. A companion saved before 2026-09-25 still has one
# stored as its regen seed; redesigned on Gemini, "Emotion: frayed" would be
# baked into the character's voice for good.
_STALE_SENTENCE = re.compile(
    r"^(emotion|character notes|world premise|environment)\s*:|"
    r"^speak the sample line|^do not include music",
    re.IGNORECASE,
)


def _compact_description(desc: str) -> str:
    """A stored description, made fit for Gemini: situational and
    ElevenLabs-addressed sentences dropped, capped at DESCRIPTION_MAX."""
    text = re.sub(r"\s+", " ", str(desc or "")).strip()
    if not text:
        return ""
    sentences = re.split(r"(?<=\.)\s+(?=[A-Z])", text)
    kept = [s for s in sentences if not _STALE_SENTENCE.match(s.strip())]
    out = " ".join(kept).strip() or text
    if len(out) > DESCRIPTION_MAX:
        cut = out[:DESCRIPTION_MAX]
        # End on a sentence if one ends in the last third, else on a word.
        dot = cut.rfind(". ")
        if dot > DESCRIPTION_MAX * 2 // 3:
            cut = cut[:dot + 1]
        else:
            cut = cut.rsplit(" ", 1)[0]
        out = cut.strip()
    return out


def brief_for_subject(
    subject: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Turn a SCAN subject + optional talk-context into a voice-design brief.

    ``context`` mirrors the dict returned by ``engine.build_talk_context``:
    ``{"situation": {"phase", "chaos", ...}, "recent": [...],
       "opening_line": "..."}``. When missing, sensible defaults are used —
    the function never touches state on its own so it's cheap and
    deterministic (a property the unit tests lean on).

    The description is the voice's PERMANENT traits only — age, gender,
    timbre, pace, register, and how it reaches you — because a designed
    voice keeps whatever it was designed with. The ElevenLabs brief also
    carried "Emotion: frayed" and "Just witnessed: …"; designed that way,
    the character is frayed in every line it ever says. The moment's
    feeling is returned separately (``emotion``, ``delivery``) for the
    speaker to pass as that line's style.

    Returns::

        {
          "description": <str>,  # the voice's permanent traits, < ~400 chars
          "gender":      <str>,  # "male" | "female" | "" (the API field)
          "emotion":     <str>,  # situational: say THIS line this way
          "delivery":    <str>,  # situational pace for this line
          "sample_text": <str>,  # a line in-voice, kept for a preview
          "labels":      <dict>, # metadata kept on the cache entry
          "voice_name":  <str>,  # "[dyn] …" display name the sweep keys on
        }
    """
    subject = subject or {}
    context = context or {}
    situation = context.get("situation") or {}

    label = _norm(subject.get("label")) or "figure"
    kind = _norm(subject.get("kind")) or "person"
    chaos = int(situation.get("chaos") or 0)
    phase = str(situation.get("phase") or "normal")
    recent = [str(r) for r in (context.get("recent") or [])[-3:]]

    gender = _gender_hint(label, kind)
    age = _age_bucket(label, kind)
    timbre = _timbre(label, kind, age)
    # Pace and register as the character's own, not as tonight's: chaos 0.
    pace = _delivery(0, kind)
    register = _register(kind, 0)
    env = _environment(label, kind)

    # Grammar / phrasing tweaks so the brief reads cleanly to the model.
    # Machines are age-less ("n/a"), and unspecified gender is best omitted
    # rather than surfaced as the literal word "unspecified".
    _age_part = "" if age in ("n/a", "") else age
    _gender_part = "" if gender in ("unspecified", "neutral") else gender
    if kind == "machine":
        _voice_phrase = "A synthetic voice"
    else:
        _bits = " ".join(b for b in (_age_part, _gender_part) if b) or "human"
        _article = "An" if _bits[:1] in "aeiou" else "A"
        _voice_phrase = f"{_article} {_bits} voice"
    description = (
        f"{_voice_phrase} for a {kind} called \"{label[:60]}\". "
        f"Timbre: {timbre}. "
        f"Pace: {pace}. "
        f"Register: {register}."
        + (f" Heard {env}." if env else "")
    )
    description = _compact_description(description)

    sample = _sample_text(str(context.get("opening_line") or ""), label)

    return {
        "description": description,
        "gender": _api_gender(label, kind),
        "emotion": _emotion(chaos, phase, recent),
        "delivery": _delivery(chaos, kind),
        "sample_text": sample,
        "voice_name": _voice_name(label, kind),
        "labels": {
            "source": NAME_PREFIX,
            "subject_label": label[:60],
            "subject_kind": kind[:20],
            "created_at": _now_iso(),
        },
    }


def _voice_name(label: str, kind: str) -> str:
    """The voice's display name in the Google project.

    Starts with NAME_PREFIX: that prefix is the ONLY thing that marks a voice
    as ours, so the sweep never reaps one a person made by hand."""
    clean = re.sub(r"[^a-zA-Z0-9 \-_]", "", label or "").strip() or "figure"
    return f"{NAME_PREFIX} {clean} ({kind or 'person'})"[:100]


# ────────────────────────────────────────────────────────────────────────────
# Cache key derivation
# ────────────────────────────────────────────────────────────────────────────

def cache_key(subject: Dict[str, Any], session_id: str,
              world_prompt: str = "") -> str:
    """Deterministic 16-hex-char key for the (session, subject, world) tuple.

    Session-scoped by design so the same "warden" in two runs sounds
    different — the whole point of the feature. A world_prompt change also
    re-casts (a new environment often warrants a new voice).
    """
    subject = subject or {}
    world_hash = hashlib.sha1((world_prompt or "").encode("utf-8")).hexdigest()[:12]
    material = "|".join([
        str(session_id or "default"),
        _norm(subject.get("label")),
        _norm(subject.get("kind")),
        world_hash,
    ])
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


# ────────────────────────────────────────────────────────────────────────────
# Cache (JSON on disk, in-memory lock)
# ────────────────────────────────────────────────────────────────────────────

_CACHE_LOCK = threading.RLock()
# Bounded semaphore so we can't run more than DESIGN_CONCURRENCY design calls
# in-flight process-wide. Guards paid API traffic under a burst of SCAN taps.
_DESIGN_SEMAPHORE = threading.BoundedSemaphore(max(1, DESIGN_CONCURRENCY))
# Coalesces concurrent design attempts for the SAME cache_key — the second
# caller waits on this Event rather than kicking off a duplicate paid call.
_INFLIGHT_EVENTS: Dict[str, threading.Event] = {}
_INFLIGHT_LOCK = threading.Lock()
# In-memory refcount so /api/talk/end can gate deletion of a voice a live
# TALK still needs. Voices with refcount > 0 are skipped by
# release_session_voices and reaped on the next sweep.
_REFCOUNT: Dict[str, int] = {}
_REFCOUNT_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_ts() -> float:
    return time.time()


def _parse_ts(value: Any) -> Optional[float]:
    """RFC 3339 -> epoch seconds, or None. Google writes nanoseconds and a
    trailing Z ("2027-09-25T12:00:00.123456789Z"); fromisoformat takes six
    fractional digits at most."""
    s = str(value or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00").replace("z", "+00:00")
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _fresh_cache() -> Dict[str, Any]:
    return {"version": _CACHE_VERSION, "voices": {}}


def _load_cache() -> Dict[str, Any]:
    if not CACHE_PATH.exists():
        return _fresh_cache()
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _fresh_cache()
        # A version-1 (ElevenLabs) cache: its ids mean nothing to Gemini.
        if data.get("version") != _CACHE_VERSION:
            return _fresh_cache()
        data.setdefault("voices", {})
        if not isinstance(data["voices"], dict):
            data["voices"] = {}
        return data
    except Exception:
        # Corrupt file — start fresh, don't crash the caller.
        return _fresh_cache()


def _save_cache(cache: Dict[str, Any]) -> None:
    try:
        _atomic_write_json(CACHE_PATH, cache)
    except Exception as e:  # noqa: BLE001
        print(f"[VOICE DESIGN] cache write failed: {e}", flush=True)


def _get_entry(key: str) -> Optional[Dict[str, Any]]:
    with _CACHE_LOCK:
        return (_load_cache()["voices"] or {}).get(key)


def _put_entry(key: str, entry: Dict[str, Any]) -> None:
    with _CACHE_LOCK:
        cache = _load_cache()
        cache["voices"][key] = entry
        _save_cache(cache)


def _drop_entries(keys: List[str]) -> None:
    if not keys:
        return
    with _CACHE_LOCK:
        cache = _load_cache()
        for k in keys:
            cache["voices"].pop(k, None)
        _save_cache(cache)


def _touch_entry(key: str) -> None:
    with _CACHE_LOCK:
        cache = _load_cache()
        entry = cache["voices"].get(key)
        if entry:
            entry["last_used_at"] = _now_iso()
            _save_cache(cache)


def _count_session_designs(session_id: str) -> int:
    with _CACHE_LOCK:
        voices = _load_cache().get("voices") or {}
        return sum(
            1 for e in voices.values()
            if isinstance(e, dict)
            and e.get("session_id") == session_id
            and e.get("status") in ("ready", "generating")
        )


def _count_ready() -> int:
    with _CACHE_LOCK:
        voices = _load_cache().get("voices") or {}
    return sum(1 for e in voices.values()
               if isinstance(e, dict) and e.get("status") == "ready" and e.get("voice_id"))


# ────────────────────────────────────────────────────────────────────────────
# Gemini voices HTTP wrappers (all quiet-fail)
# ────────────────────────────────────────────────────────────────────────────

def _headers(key: str) -> Dict[str, str]:
    return {"x-goog-api-key": key, "Content-Type": "application/json"}


def _voice_id_of(voice: Dict[str, Any]) -> str:
    """``id`` as the create call answers it, or a resource ``name``
    ("voices/voice_…") should a listing answer that way."""
    if not isinstance(voice, dict):
        return ""
    vid = str(voice.get("id") or "").strip()
    if not vid:
        vid = str(voice.get("name") or "").strip().rsplit("/", 1)[-1]
    return vid


def _display_name_of(voice: Dict[str, Any]) -> str:
    return str(voice.get("display_name") or voice.get("displayName") or "")


def _create_body(brief: Dict[str, Any], gender: str) -> Dict[str, Any]:
    voice: Dict[str, Any] = {
        "model": MODEL,
        "type": "prompted",
        "display_name": brief["voice_name"],
        "language_code": LANGUAGE_CODE,
        "prompted": {"input": brief["description"]},
    }
    if gender:
        voice["gender"] = gender
    return {"store": True, "voice": voice}


def _post_create(brief: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """POST /v1beta/voices -> the stored voice ({id, expire_time, usage, …})
    or None on failure. One call designs AND stores it.

    Only "male"/"female" are known to be accepted as ``gender``; should a
    create with one of them 400, it is asked once more without the field.
    """
    key = _api_key()
    if not key:
        return None
    gender = str(brief.get("gender") or "").strip().lower()
    if gender not in ("male", "female"):
        gender = ""
    attempts = [gender, ""] if gender else [""]
    try:
        import requests
    except Exception as e:  # noqa: BLE001
        print(f"[VOICE DESIGN] requests unavailable: {e}", flush=True)
        return None
    for g in attempts:
        try:
            resp = requests.post(
                _URL_VOICES,
                headers=_headers(key),
                json=_create_body(brief, g),
                timeout=DESIGN_TIMEOUT_SECONDS,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[VOICE DESIGN] create exception: {e}", flush=True)
            return None
        if resp.status_code == 200:
            try:
                data = resp.json() or {}
            except Exception:
                data = {}
            vid = _voice_id_of(data)
            if vid:
                data["id"] = vid
                return data
            print(f"[VOICE DESIGN] create returned no id: {str(data)[:180]}", flush=True)
            return None
        if resp.status_code == 400 and g:
            print(f"[VOICE DESIGN] create 400 with gender={g!r}; retrying without "
                  f"it: {resp.text[:180]}", flush=True)
            continue
        print(f"[VOICE DESIGN] create http {resp.status_code}: {resp.text[:180]}",
              flush=True)
        return None
    return None


def _delete_voice(voice_id: str) -> bool:
    """DELETE /v1beta/voices/{id}. True on success or 404. Never raises."""
    key = _api_key()
    if not key or not voice_id:
        return False
    try:
        import requests
        resp = requests.delete(
            _URL_VOICE_TPL.format(voice_id=voice_id),
            headers={"x-goog-api-key": key},
            timeout=15,
        )
        if resp.status_code in (200, 204, 404):
            return True
        print(
            f"[VOICE DESIGN] delete http {resp.status_code}: {resp.text[:180]}",
            flush=True,
        )
        return False
    except Exception as e:  # noqa: BLE001
        print(f"[VOICE DESIGN] delete exception: {e}", flush=True)
        return False


_LIST_MAX_PAGES = 20  # 200 stored voices at the project cap; 50 to a page


def _list_voices() -> Tuple[List[Dict[str, Any]], str]:
    """Every voice stored in the key's project, as ``(voices, reason)``.

    Asks for ``type=prompted``. Unfiltered, the listing is Google's prebuilt
    catalogue — 2,089 voices over 42 pages on 2026-09-25, the stored ones
    somewhere among them — and the first live run of this function walked
    twenty pages of it and gave up. Prebuilt entries are skipped even so.

    ``reason`` is "ok" / "empty" only when the WHOLE listing was read; the
    sweep acts on nothing else (dropping cache entries off half a listing
    would forget voices that still exist). Pages on ``next_page_token`` (what
    the API answers) or ``nextPageToken``. Normalised to ``{"id",
    "display_name", "expire_time", "create_time"}``.
    """
    key = _api_key()
    if not key:
        return [], "no_api_key"
    try:
        import requests
        out: List[Dict[str, Any]] = []
        seen: set = set()
        token = ""
        for _ in range(_LIST_MAX_PAGES):
            params = {"type": "prompted"}
            if token:
                params["pageToken"] = token
            resp = requests.get(_URL_VOICES, headers={"x-goog-api-key": key},
                                params=params, timeout=15)
            status = resp.status_code
            if status != 200:
                print(f"[VOICE DESIGN] list http {status}: {resp.text[:180]}", flush=True)
                if status in (401, 403):
                    return out, "key_cannot_read_voices"
                if status == 429:
                    return out, "rate_limited"
                return out, f"http_{status}"
            data = resp.json() or {}
            for v in (data.get("voices") or []):
                if not isinstance(v, dict) or v.get("type") == "prebuilt":
                    continue
                vid = _voice_id_of(v)
                if not vid or vid in seen:
                    continue
                seen.add(vid)
                out.append({
                    "id": vid,
                    "display_name": _display_name_of(v),
                    "expire_time": v.get("expire_time") or v.get("expireTime") or "",
                    "create_time": v.get("create_time") or v.get("createTime") or "",
                })
            token = str(data.get("next_page_token") or data.get("nextPageToken") or "")
            if not token:
                return out, ("ok" if out else "empty")
        print("[VOICE DESIGN] list: page limit reached", flush=True)
        return out, "incomplete"
    except Exception as e:  # noqa: BLE001
        print(f"[VOICE DESIGN] list exception: {e}", flush=True)
        return [], "unreachable"


def _remote_age_hours(voice: Dict[str, Any]) -> Optional[float]:
    """How old a listed voice is, or None when it cannot be told. From
    ``create_time`` if the listing gives one, else a year before it expires."""
    created = _parse_ts(voice.get("create_time"))
    if created is None:
        expires = _parse_ts(voice.get("expire_time"))
        if expires is None:
            return None
        created = expires - _STORED_LIFETIME.total_seconds()
    return max(0.0, (_now_ts() - created) / 3600.0)


# ────────────────────────────────────────────────────────────────────────────
# Characters' own voices — kept, not swept
# ────────────────────────────────────────────────────────────────────────────
# The player's character (characters.py) is designed a voice when it is made,
# and that voice narrates every run it plays (the narrator is "you, speaking
# into a tape"). It must outlive sessions, so it is not a [dyn] voice: it is
# named with CHARACTER_PREFIX, which the sweep never touches, and its id lives
# on the character record, not in this cache. What survives a new key, a new
# Google project or the year's expiry is the description on the record.

CHARACTER_PREFIX = "[chr]"


def key_fingerprint() -> str:
    """Which key a voice was made on — a voice id belongs to that key's Google
    project and names nothing on another. Never the key itself."""
    key = _api_key()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12] if key else ""


CHARACTER_TAKES = _cfg_int("SOMEWHERE_CHARACTER_VOICE_TAKES", 2)
JUDGE_MODEL = "gemini-3.1-flash-lite"

_JUDGE = """A voice designer was asked for this voice:
"{description}"
{n} takes follow, in order. Listen to each. Which take IS that person — above all the
age, gender, accent and texture asked for — and sounds natural, with no robotic
artifacts, clipping or odd pacing? Reply with JSON only: {{"best": <take number>, "why": "<one short clause>"}}"""


def _judge_takes(description: str, takes: List[Dict[str, Any]]) -> Tuple[int, str]:
    """Which take matches the description best, by listening to each design's
    own sample (no extra speech). (index, why); index 0 on any failure."""
    parts: List[Dict[str, Any]] = [{"text": _JUDGE.format(description=description[:600], n=len(takes))}]
    for i, t in enumerate(takes, 1):
        sample = t.get("sample_audio") or {}
        data = sample.get("data") if isinstance(sample, dict) else None
        if not data:
            return 0, "a take had no sample"
        parts += [{"text": f"Take {i}:"},
                  {"inlineData": {"mimeType": str(sample.get("mime_type") or "audio/wav"), "data": data}}]
    try:
        import requests
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{JUDGE_MODEL}:generateContent",
            headers=_headers(_api_key()),
            json={"contents": [{"role": "user", "parts": parts}],
                  "generationConfig": {"responseMimeType": "application/json", "temperature": 0}},
            timeout=60)
        if resp.status_code != 200:
            return 0, f"judge http {resp.status_code}"
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        out = json.loads(re.sub(r"^```[a-z]*\s*|\s*```$", "", text.strip()))
        best = int(out.get("best") or 1) - 1
        if 0 <= best < len(takes):
            return best, str(out.get("why") or "")[:160]
    except Exception as e:  # noqa: BLE001
        return 0, f"judge failed: {type(e).__name__}"
    return 0, "judge gave no take"


def design_character_voice(name: str, description: str, gender: str = "",
                           session_id: str = "default",
                           takes: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Design and store the voice for a player character. Blocking.

    There is no seed and no "give me variations" in voice design, and one
    description can come back as quite different people (a "mid-seventies"
    rancher designed on 2026-09-25 was heard as middle-aged). Google's own
    cookbook: create two or three, listen, keep one. So TAKES are designed
    side by side, a listener model hears each design's own sample against
    the description, the closest is kept and the rest are deleted — they
    would otherwise hold slots of the project's 200 for a year.
    Returns {id, expire_time, fingerprint, takes, picked_because} or None."""
    description = (description or "").strip()
    if not is_available() or len(description) < 20:
        return None
    clean = re.sub(r"[^a-zA-Z0-9 \-_']", "", name or "").strip() or "character"
    brief = {"voice_name": f"{CHARACTER_PREFIX} {clean}"[:100],
             "description": description[:1000],
             "gender": gender if gender in ("male", "female") else ""}
    n = max(1, min(3, int(takes if takes is not None else CHARACTER_TAKES)))
    results: List[Optional[Dict[str, Any]]] = [None] * n

    def one(i: int) -> None:
        with _DESIGN_SEMAPHORE:
            results[i] = _post_create(brief)
        _record_design_cost(session_id, results[i])

    threads = [threading.Thread(target=one, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(DESIGN_TIMEOUT_SECONDS * 2 + 10)
    made = [r for r in results if r and r.get("id")]
    if not made:
        return None
    best, why = (0, "one take") if len(made) == 1 else _judge_takes(description, made)
    keep = made[best]
    for i, r in enumerate(made):
        if i != best:
            _delete_voice(r["id"])
    keep["fingerprint"] = key_fingerprint()
    keep["takes"] = len(made)
    keep["picked_because"] = why
    return keep


def delete_character_voice(voice_id: str) -> bool:
    """Free a character's old voice once a new one has replaced it."""
    if not voice_id or not str(voice_id).startswith("voice_"):
        return False
    return _delete_voice(voice_id)


# ────────────────────────────────────────────────────────────────────────────
# The ElevenLabs library, retired
# ────────────────────────────────────────────────────────────────────────────

def voice_library(force: bool = False) -> Dict[str, Any]:
    """There is no account library any more.

    ElevenLabs had "your voices" — clones and designs made in its dashboard —
    and the game listed them in TALK and the editor. A Gemini key has only the
    prebuilt voices (the ``voices.json`` roster) and the ``[dyn]`` voices this
    module makes and deletes. Kept so callers degrade to the roster exactly as
    they did for a key that could not read the library.
    """
    return {"ok": False, "reason": "no_library", "voices": []}


def is_library_voice_id(voice_id: str) -> bool:
    """Always False: see ``voice_library``."""
    return False


# ────────────────────────────────────────────────────────────────────────────
# Refcount API (used by api_talk_session / /api/talk/end)
# ────────────────────────────────────────────────────────────────────────────

def acquire(voice_id: str) -> None:
    if not voice_id:
        return
    with _REFCOUNT_LOCK:
        _REFCOUNT[voice_id] = _REFCOUNT.get(voice_id, 0) + 1


def release(voice_id: str) -> int:
    if not voice_id:
        return 0
    with _REFCOUNT_LOCK:
        v = max(0, _REFCOUNT.get(voice_id, 0) - 1)
        if v:
            _REFCOUNT[voice_id] = v
        else:
            _REFCOUNT.pop(voice_id, None)
        return v


def refcount(voice_id: str) -> int:
    with _REFCOUNT_LOCK:
        return _REFCOUNT.get(voice_id, 0)


# ────────────────────────────────────────────────────────────────────────────
# Design pipeline — async by default, coalescing per cache key
# ────────────────────────────────────────────────────────────────────────────

def _record_design_cost(session_id: str, result: Optional[Dict[str, Any]]) -> None:
    usage = (result or {}).get("usage") or {}

    def n(*names: str) -> int:
        for name in names:
            try:
                if usage.get(name) is not None:
                    return int(usage.get(name) or 0)
            except (TypeError, ValueError):
                pass
        return 0

    tokens_in = n("total_input_tokens", "totalInputTokens")
    tokens_out = (n("total_output_tokens", "totalOutputTokens")
                  + n("total_thought_tokens", "totalThoughtTokens"))
    try:
        cost_tracker.record_usage(
            session_id, "voice", "gemini", f"{MODEL}:voice_design",
            operation="design",
            input_units=tokens_in, output_units=tokens_out, unit_type="tokens",
            success=bool(result),
            error_message=None if result else "design_call_failed",
        )
    except Exception:
        pass


def _design_and_save(key: str, brief: Dict[str, Any],
                     session_id: str, subject: Dict[str, Any]) -> Optional[str]:
    """Blocking: design and store one voice, return its ``voice_…`` id.

    Called only from the background worker via ``_start_design_worker``. Caller
    holds the semaphore + the inflight event. Marks the cache entry ready /
    failed as it goes.
    """
    label = _norm(subject.get("label")) or "figure"
    kind = _norm(subject.get("kind")) or "person"

    # LRU pressure check BEFORE spending tokens: at the soft cap, evict the
    # oldest ready voice (any session) whose refcount is zero.
    _evict_if_over_soft_cap(need=1)

    result = _post_create(brief)
    _record_design_cost(session_id, result)
    voice_id = _voice_id_of(result or {})
    if not voice_id:
        _put_entry(key, {
            **(_get_entry(key) or {}),
            "status": "failed",
            "error": "design_call_failed",
            "failed_at": _now_iso(),
            "expires_at": _now_ts() + FAIL_TTL_SECONDS,
        })
        return None

    now = _now_iso()
    _put_entry(key, {
        "voice_id": voice_id,
        "session_id": session_id,
        "label": label,
        "kind": kind,
        "description": brief["description"],
        "sample_text": brief.get("sample_text", ""),
        "display_name": brief.get("voice_name", ""),
        "labels": brief.get("labels") or {},
        "model": (result or {}).get("model") or MODEL,
        "expire_time": (result or {}).get("expire_time") or "",
        "created_at": now,
        "last_used_at": now,
        "status": "ready",
    })
    return voice_id


def _start_design_worker(key: str, brief: Dict[str, Any],
                         session_id: str, subject: Dict[str, Any]) -> threading.Event:
    """Start (or return the existing) background design job for ``key``.

    The returned Event is set when the job finishes (success OR failure).
    Coalesces per-key so N concurrent SCAN taps on the same subject only spend
    ONE design call.
    """
    with _INFLIGHT_LOCK:
        ev = _INFLIGHT_EVENTS.get(key)
        if ev is not None:
            return ev
        ev = threading.Event()
        _INFLIGHT_EVENTS[key] = ev

    # Mark the entry generating BEFORE the thread starts so a wait=0 caller
    # sees the right status immediately.
    existing = _get_entry(key) or {}
    _put_entry(key, {
        **existing,
        "session_id": session_id,
        "label": _norm(subject.get("label")) or "figure",
        "kind": _norm(subject.get("kind")) or "person",
        "description": brief["description"],
        "sample_text": brief.get("sample_text", ""),
        "status": "generating",
        "generating_since": _now_iso(),
    })

    def _worker():
        acquired = False
        try:
            # Bound total concurrent paid calls process-wide.
            acquired = _DESIGN_SEMAPHORE.acquire(timeout=DESIGN_TIMEOUT_SECONDS + 5)
            if not acquired:
                _put_entry(key, {
                    **(_get_entry(key) or {}),
                    "status": "failed",
                    "error": "semaphore_timeout",
                    "failed_at": _now_iso(),
                    "expires_at": _now_ts() + FAIL_TTL_SECONDS,
                })
                return
            _design_and_save(key, brief, session_id, subject)
        except Exception as e:  # noqa: BLE001
            print(f"[VOICE DESIGN] worker exception: {e}", flush=True)
            _put_entry(key, {
                **(_get_entry(key) or {}),
                "status": "failed",
                "error": "worker_exception",
                "failed_at": _now_iso(),
                "expires_at": _now_ts() + FAIL_TTL_SECONDS,
            })
        finally:
            if acquired:
                try:
                    _DESIGN_SEMAPHORE.release()
                except ValueError:
                    pass
            ev.set()
            with _INFLIGHT_LOCK:
                _INFLIGHT_EVENTS.pop(key, None)

    t = threading.Thread(target=_worker, name=f"voice-design-{key}", daemon=True)
    t.start()
    return ev


def _brief_from_description(label: str, kind: str, description: str,
                            sample_text: str, **extra_labels: str) -> Dict[str, Any]:
    """A brief around a stored description (a companion's regen seed)."""
    return {
        "description": description,
        "gender": _api_gender(label, kind),
        "emotion": "",
        "delivery": "",
        "sample_text": _sample_text(sample_text or "", label),
        "voice_name": _voice_name(label, kind),
        "labels": {
            "source": NAME_PREFIX,
            "subject_label": label[:60],
            "subject_kind": kind[:20],
            "created_at": _now_iso(),
            **extra_labels,
        },
    }


# ────────────────────────────────────────────────────────────────────────────
# Public entry point — resolve / design a voice for a subject
# ────────────────────────────────────────────────────────────────────────────

def regenerate_voice(
    subject: Dict[str, Any],
    session_id: str,
    description: str,
    *,
    world_prompt: str = "",
    sample_text: str = "",
    old_voice_id: Optional[str] = None,
    wait: float = 0.0,
) -> Optional[Dict[str, Any]]:
    """Force a NEW voice design from a stored companion description.

    Companions persist the voice-design brief so a later beat can recreate
    the same character's voice after eviction / session cleanup. This path
    does **not** rebuild the brief from story context — it reuses the
    ``description`` seed (compacted: an ElevenLabs-era seed loses its
    situational and ElevenLabs-addressed sentences) — then evicts the prior
    cache entry (and best-effort deletes ``old_voice_id`` when its refcount
    is zero) before kicking off a fresh design job under the same cache key.

    Returns the same shape as ``get_or_design_voice``, or ``None`` when the
    feature is unavailable / the description is unusable. Never raises.
    """
    if not is_available():
        return None
    if not isinstance(subject, dict):
        return None
    label = _norm(subject.get("label"))
    if not label:
        return None
    desc = _compact_description(description)
    if len(desc) < 20:
        return None

    key = cache_key(subject, session_id, world_prompt)

    # Drop any ready/generating/failed entry for this key so we actually
    # spend a new design call instead of returning the cached voice_id.
    existing = _get_entry(key)
    if existing:
        _drop_entries([key])
    # Best-effort: free the previous voice when nothing is holding a ref (a
    # live TALK keeps refcount > 0 and must not be yanked).
    if old_voice_id and refcount(old_voice_id) <= 0:
        try:
            _delete_voice(old_voice_id)
        except Exception:
            pass

    if _count_session_designs(session_id) >= DESIGN_BUDGET_PER_SESSION:
        return {
            "voice_id": None,
            "cache_key": key,
            "source": "budget",
            "status": "failed",
            "description": desc,
        }

    kind = _norm(subject.get("kind")) or "person"
    brief = _brief_from_description(label, kind, desc, sample_text, regen="1")
    ev = _start_design_worker(key, brief, session_id, subject)

    if wait > 0:
        ev.wait(timeout=wait)
        entry = _get_entry(key)
        if entry and entry.get("status") == "ready" and entry.get("voice_id"):
            _touch_entry(key)
            return {
                "voice_id": entry["voice_id"],
                "cache_key": key,
                "source": "designed",
                "status": "ready",
                "description": desc,
            }
        if entry and entry.get("status") == "failed":
            return {
                "voice_id": None,
                "cache_key": key,
                "source": "failed",
                "status": "failed",
                "description": desc,
            }

    return {
        "voice_id": None,
        "cache_key": key,
        "source": "generating",
        "status": "generating",
        "description": desc,
    }


def get_or_design_voice(
    subject: Dict[str, Any],
    session_id: str = "default",
    context: Optional[Dict[str, Any]] = None,
    world_prompt: str = "",
    wait: float = 0.0,
    description_override: str = "",
) -> Optional[Dict[str, Any]]:
    """Resolve a designed voice for ``subject``, or ``None`` to signal that
    the caller should fall back to the static ``by_kind`` roster.

    ``wait`` seconds > 0 blocks the caller until a currently-in-flight job for
    this cache key completes (bounded by ``DESIGN_TIMEOUT_SECONDS``).

    ``description_override`` (when >= 20 chars) reuses a stored companion
    voice description instead of rebuilding one from story context — the
    recovery path when a companion's ``voice_id`` was evicted but the regen
    seed survived on the roster.

    Returns a dict::

        {
          "voice_id":   "voice_…" | None,
          "cache_key":  "<16-hex>",
          "source":     "cache" | "designed" | "generating" | "failed" | "budget",
          "status":     "ready" | "generating" | "failed",
          "description": "<the voice description>",
        }

    Never raises. Returns ``None`` when the feature is disabled / Gemini is
    not what is playing / the subject is missing a label. The caller should
    then fall back to whatever it used before this module existed (typically
    the ``by_kind`` map in ``voices.json``).
    """
    if not is_available():
        return None
    if not isinstance(subject, dict):
        return None
    label = _norm(subject.get("label"))
    if not label:
        return None

    key = cache_key(subject, session_id, world_prompt)
    now_ts = _now_ts()

    entry = _get_entry(key)
    if entry:
        status = entry.get("status")
        # Stale failure — allow a retry after the TTL elapses.
        if status == "failed":
            if float(entry.get("expires_at") or 0) > now_ts:
                return {
                    "voice_id": None,
                    "cache_key": key,
                    "source": "failed",
                    "status": "failed",
                    "description": entry.get("description", ""),
                }
            # TTL elapsed — clear + fall through to a fresh design.
            _drop_entries([key])
            entry = None

        if entry and status == "ready" and entry.get("voice_id"):
            _touch_entry(key)
            return {
                "voice_id": entry["voice_id"],
                "cache_key": key,
                "source": "cache",
                "status": "ready",
                "description": entry.get("description", ""),
            }

        if entry and status == "generating":
            ev = _INFLIGHT_EVENTS.get(key)
            if ev is not None and wait > 0:
                ev.wait(timeout=wait)
                entry = _get_entry(key)
                if entry and entry.get("status") == "ready" and entry.get("voice_id"):
                    _touch_entry(key)
                    return {
                        "voice_id": entry["voice_id"],
                        "cache_key": key,
                        "source": "designed",
                        "status": "ready",
                        "description": entry.get("description", ""),
                    }
            return {
                "voice_id": None,
                "cache_key": key,
                "source": "generating",
                "status": "generating",
                "description": (entry or {}).get("description", ""),
            }

    # No usable entry — check the per-session design budget, then kick off a
    # background design job.
    if _count_session_designs(session_id) >= DESIGN_BUDGET_PER_SESSION:
        return {
            "voice_id": None,
            "cache_key": key,
            "source": "budget",
            "status": "failed",
            "description": "",
        }

    override = _compact_description(description_override)
    if len(override) >= 20:
        kind = _norm(subject.get("kind")) or "person"
        brief = _brief_from_description(
            label, kind, override,
            str((context or {}).get("opening_line") or ""),
            from_companion="1",
        )
    else:
        brief = brief_for_subject(subject, context)
    ev = _start_design_worker(key, brief, session_id, subject)

    if wait > 0:
        ev.wait(timeout=wait)
        entry = _get_entry(key)
        if entry and entry.get("status") == "ready" and entry.get("voice_id"):
            _touch_entry(key)
            return {
                "voice_id": entry["voice_id"],
                "cache_key": key,
                "source": "designed",
                "status": "ready",
                "description": entry.get("description", ""),
            }
        if entry and entry.get("status") == "failed":
            return {
                "voice_id": None,
                "cache_key": key,
                "source": "failed",
                "status": "failed",
                "description": entry.get("description", ""),
            }

    return {
        "voice_id": None,
        "cache_key": key,
        "source": "generating",
        "status": "generating",
        "description": brief["description"],
    }


def is_ready_voice_id(voice_id: str) -> bool:
    """Cheap allowlist check: is this a designed ``voice_…`` id present in
    the cache with status='ready'? Used by ``engine._valid_voice_id`` to admit
    designed voices through the same validation as roster ones without
    falling back to the heavier ``cache_snapshot`` call on every TALK."""
    vid = (voice_id or "").strip()
    if not vid.startswith("voice_"):
        return False
    try:
        with _CACHE_LOCK:
            voices = _load_cache().get("voices") or {}
        for e in voices.values():
            if isinstance(e, dict) and e.get("voice_id") == vid and e.get("status") == "ready":
                return True
    except Exception:
        pass
    return False


def get_status(cache_key_str: str) -> Optional[Dict[str, Any]]:
    """Cheap poll endpoint for ``/api/talk/voice/status``. Returns::

        {"cache_key": ..., "voice_id": ..., "status": "ready"|"generating"|"failed"|"unknown",
         "description": ..., "source": "cache"|"generating"|"failed"|"unknown"}
    """
    if not cache_key_str:
        return None
    entry = _get_entry(cache_key_str)
    if not entry:
        return {
            "cache_key": cache_key_str,
            "voice_id": None,
            "status": "unknown",
            "source": "unknown",
            "description": "",
        }
    status = entry.get("status") or "unknown"
    if status == "ready" and entry.get("voice_id"):
        _touch_entry(cache_key_str)
    return {
        "cache_key": cache_key_str,
        "voice_id": entry.get("voice_id"),
        "status": status,
        "source": "cache" if status == "ready" else status,
        "description": entry.get("description", ""),
    }


# ────────────────────────────────────────────────────────────────────────────
# Cleanup: per-session release, LRU eviction, orphan sweep
# ────────────────────────────────────────────────────────────────────────────

def release_session_voices(session_id: str,
                           grace_seconds: float = 0.0) -> Dict[str, Any]:
    """DELETE every designed voice tagged to ``session_id``.

    Voices with refcount > 0 (a live TALK still holds them) are skipped and
    left for the next sweep. Idempotent + never raises.
    Returns ``{"deleted": N, "skipped": M, "voice_ids": [...]}``.
    """
    if not session_id:
        return {"deleted": 0, "skipped": 0, "voice_ids": []}
    deleted: List[str] = []
    skipped: List[str] = []
    drop_keys: List[str] = []
    with _CACHE_LOCK:
        voices = dict((_load_cache().get("voices") or {}))
    for key, entry in voices.items():
        if not isinstance(entry, dict) or entry.get("session_id") != session_id:
            continue
        vid = entry.get("voice_id")
        if vid and refcount(vid) > 0:
            skipped.append(vid)
            continue
        if vid:
            if grace_seconds > 0:
                time.sleep(grace_seconds)
            if _delete_voice(vid):
                deleted.append(vid)
        drop_keys.append(key)
    _drop_entries(drop_keys)
    if deleted or skipped:
        print(
            f"[VOICE DESIGN] session={session_id} released "
            f"{len(deleted)} voice(s), skipped {len(skipped)} (refcount>0)",
            flush=True,
        )
    return {"deleted": len(deleted), "skipped": len(skipped), "voice_ids": deleted}


def _soft_cap() -> int:
    """The cache LRU-evicts once its ready voices reach this.

    Google stores 200 voices per project and says nothing about how many are
    used, so the count is our own; 180 leaves room for designs in flight and
    for another checkout's voices on the same key."""
    if VOICE_SOFT_CAP_OVERRIDE > 0:
        return VOICE_SOFT_CAP_OVERRIDE
    return _DEFAULT_SOFT_CAP


def _evict_if_over_soft_cap(need: int = 1) -> int:
    """Evict LRU 'ready' entries (any session, refcount 0) to make room for
    ``need`` more designed voices. Returns the number actually evicted."""
    cap = _soft_cap()
    with _CACHE_LOCK:
        voices = dict((_load_cache().get("voices") or {}))
    ready = [
        (k, e) for k, e in voices.items()
        if isinstance(e, dict) and e.get("status") == "ready" and e.get("voice_id")
    ]
    if len(ready) + need <= cap:
        return 0
    # Sort oldest last_used_at first; skip anything with a live refcount.
    ready.sort(key=lambda kv: str((kv[1] or {}).get("last_used_at") or ""))
    evict_target = (len(ready) + need) - cap
    evicted = 0
    evict_keys: List[str] = []
    for k, e in ready:
        if evicted >= evict_target:
            break
        vid = e.get("voice_id")
        if refcount(vid) > 0:
            continue
        if _delete_voice(vid):
            evict_keys.append(k)
            evicted += 1
    _drop_entries(evict_keys)
    if evicted:
        print(f"[VOICE DESIGN] LRU-evicted {evicted} designed voice(s)", flush=True)
    return evicted


def sweep_orphans(max_age_hours: Optional[int] = None,
                  active_session_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Reconcile the cache with ``GET /v1beta/voices``. Idempotent, never
    raises. Only ever touches voices whose display name starts with
    NAME_PREFIX, and never one with a live refcount.

    * A ``[dyn]`` voice the cache does not know is an orphan (a crash between
      the create and the cache write, a cache file deleted by hand): reaped
      once it is older than ORPHAN_GRACE_MINUTES, or when its age cannot be
      told. The grace is for a design in flight right now, and for a second
      checkout on the same key, whose cache is a different file.
    * A ``[dyn]`` voice the cache does know is reaped once it is older than
      ``max_age_hours`` AND its session is not in ``active_session_ids``.
    * A ready cache entry whose voice is no longer on the server is dropped
      — but only off a complete listing, and only entries that existed
      before the listing was asked for.
    """
    if not is_available():
        return {"deleted": 0, "unknown": 0, "kept": 0}
    max_age = int(max_age_hours if max_age_hours is not None else MAX_AGE_HOURS)
    active = set(active_session_ids or [])
    now_ts = _now_ts()

    # Snapshot BEFORE listing: a voice designed while the listing is in
    # flight is in the cache but not in the listing, and must not be dropped.
    with _CACHE_LOCK:
        snapshot = dict((_load_cache().get("voices") or {}))
    by_vid = {
        e["voice_id"]: (k, e) for k, e in snapshot.items()
        if isinstance(e, dict) and e.get("voice_id")
    }

    remote, list_reason = _list_voices()
    if list_reason not in ("ok", "empty"):
        return {"deleted": 0, "unknown": 0, "kept": 0, "reason": list_reason}
    deleted = 0
    kept = 0
    unknown = 0
    remote_ids: set = set()
    drop_keys: List[str] = []
    for v in remote:
        if not isinstance(v, dict):
            continue
        vid = v.get("id") or ""
        if not vid or not _display_name_of(v).startswith(NAME_PREFIX):
            continue
        remote_ids.add(vid)
        if refcount(vid) > 0:
            kept += 1
            continue
        known = by_vid.get(vid)
        if known is None:
            age_h = _remote_age_hours(v)
            reap = age_h is None or age_h * 60.0 >= ORPHAN_GRACE_MINUTES
        else:
            _k, entry = known
            created = _parse_ts(entry.get("created_at"))
            age_h = ((now_ts - created) / 3600.0) if created else _remote_age_hours(v)
            stale = age_h is not None and age_h >= max_age
            reap = bool(stale and entry.get("session_id") not in active)
        if not reap:
            kept += 1
            continue
        if _delete_voice(vid):
            deleted += 1
            if known is not None:
                drop_keys.append(known[0])
        else:
            unknown += 1

    # Drop ready cache entries whose voice vanished server-side.
    for vid, (k, e) in by_vid.items():
        if e.get("status") == "ready" and vid not in remote_ids and k not in drop_keys:
            drop_keys.append(k)
    if drop_keys:
        _drop_entries(drop_keys)

    if deleted or drop_keys:
        print(
            f"[VOICE DESIGN] sweep: deleted={deleted}, "
            f"kept={kept}, unknown={unknown}, dropped_cache={len(drop_keys)}",
            flush=True,
        )
    return {"deleted": deleted, "unknown": unknown, "kept": kept,
            "dropped_cache": len(drop_keys)}


# ────────────────────────────────────────────────────────────────────────────
# Admin / observability
# ────────────────────────────────────────────────────────────────────────────

def cache_snapshot() -> Dict[str, Any]:
    """Human-readable snapshot for the admin dashboard. No secrets, and no
    network: the slot count is our own cache's."""
    with _CACHE_LOCK:
        cache = _load_cache()
    entries = []
    for k, e in (cache.get("voices") or {}).items():
        if not isinstance(e, dict):
            continue
        vid = e.get("voice_id")
        entries.append({
            "cache_key": k,
            "voice_id": vid,
            "session_id": e.get("session_id"),
            "label": e.get("label"),
            "kind": e.get("kind"),
            "status": e.get("status"),
            "created_at": e.get("created_at"),
            "last_used_at": e.get("last_used_at"),
            "refcount": refcount(vid) if vid else 0,
        })
    return {
        "enabled": is_available(),
        "config": {
            "budget_per_session": DESIGN_BUDGET_PER_SESSION,
            "concurrency": DESIGN_CONCURRENCY,
            "soft_cap": _soft_cap(),
            "name_prefix": NAME_PREFIX,
            "model": MODEL,
        },
        "project_slots": {"used": _count_ready(), "limit": PROJECT_VOICE_LIMIT},
        "cache_size": len(entries),
        "entries": entries,
    }


# ────────────────────────────────────────────────────────────────────────────
# Periodic sweep (best-effort, opt-in via caller thread; api.py may schedule
# a first call at startup). Never runs on import.
# ────────────────────────────────────────────────────────────────────────────

_SWEEP_THREAD: Optional[threading.Thread] = None
_SWEEP_STOP = threading.Event()


def start_periodic_sweep(active_sessions_getter=None) -> None:
    """Start a daemon thread that runs ``sweep_orphans`` every SWEEP_HOURS.

    ``active_sessions_getter`` is a callable returning the current list of
    session ids to preserve (typically ``list(sessions_root.iterdir())``).
    Called on module unavailability is a no-op. Idempotent."""
    global _SWEEP_THREAD
    if not is_available():
        return
    if _SWEEP_THREAD is not None and _SWEEP_THREAD.is_alive():
        return

    def _loop():
        # Kick off an immediate sweep so orphans from a crashed prior process
        # get cleaned up before the first player arrives.
        try:
            active = list(active_sessions_getter() or []) if active_sessions_getter else []
            sweep_orphans(active_session_ids=active)
        except Exception as e:  # noqa: BLE001
            print(f"[VOICE DESIGN] initial sweep failed: {e}", flush=True)
        while not _SWEEP_STOP.wait(max(1, SWEEP_HOURS) * 3600):
            try:
                active = list(active_sessions_getter() or []) if active_sessions_getter else []
                sweep_orphans(active_session_ids=active)
            except Exception as e:  # noqa: BLE001
                print(f"[VOICE DESIGN] periodic sweep failed: {e}", flush=True)

    _SWEEP_THREAD = threading.Thread(target=_loop, name="voice-design-sweep", daemon=True)
    _SWEEP_THREAD.start()
    print(f"[VOICE DESIGN] periodic sweep armed (every {SWEEP_HOURS}h)", flush=True)


def stop_periodic_sweep() -> None:
    """For tests — signals the sweep thread to exit at the next wake."""
    _SWEEP_STOP.set()
