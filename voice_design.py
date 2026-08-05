"""
voice_design.py — dynamic per-character ElevenLabs voices.

Turns a SCAN subject (label + kind) plus the current story context into a
custom ElevenLabs voice designed to sound like *that specific character*,
instead of routing every subject through the static ``by_kind`` roster in
``voices.json``. Designed voices are cached per-session on disk, tagged in
the ElevenLabs workspace with ``source=somewhere-dyn``, and DELETED at
session end so the workspace's voice-slot quota stays bounded.

Design goals (see DYNAMIC_VOICES_PLAN.md):

* Non-blocking hot path — ``get_or_design_voice(..., wait=0)`` never spends
  more than a JSON-encode of latency on the caller's thread; the actual
  Voice Design + save call runs in a background worker. The caller gets a
  fallback voice immediately and can poll ``/api/talk/voice/status`` (or
  pass ``wait>0`` to catch a fast path).
* Byte-identical fallback — every entry point degrades to ``None`` or an
  empty result when the API key is missing / the feature is disabled / a
  request fails, so callers that "OR" a fallback in behave exactly as they
  did before this module existed.
* Slot-safe — a per-session **budget** caps design calls per session, an
  in-memory **refcount** blocks deletion of voices attached to an open
  Convai call, an **LRU eviction** frees the oldest voice when we approach
  the workspace slot ceiling, and a periodic **sweep** reconciles the cache
  with ``GET /v1/voices`` to reap orphans left by crashes.

Only depends on ``requests`` + stdlib — matches the style of ``scene_audio.py``
and the existing ElevenLabs calls in ``engine.py``. No new dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).parent.resolve()

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


ENABLED = _cfg_bool("ELEVENLABS_DYNAMIC_VOICES", True)
API_KEY = _cfg("ELEVENLABS_API_KEY")
TTV_MODEL = _cfg("ELEVENLABS_TTV_MODEL", "eleven_ttv_v3")
DESIGN_BUDGET_PER_SESSION = _cfg_int("ELEVENLABS_DESIGN_BUDGET_PER_SESSION", 8)
DESIGN_CONCURRENCY = _cfg_int("ELEVENLABS_DESIGN_CONCURRENCY", 3)
VOICE_SOFT_CAP_OVERRIDE = _cfg_int("ELEVENLABS_VOICE_SOFT_CAP", 0)  # 0 = auto
SWEEP_HOURS = _cfg_int("ELEVENLABS_VOICE_SWEEP_HOURS", 6)
MAX_AGE_HOURS = _cfg_int("ELEVENLABS_VOICE_MAX_AGE_HOURS", 24)
LABEL_TAG = _cfg("ELEVENLABS_VOICE_LABEL_TAG", "somewhere-dyn")
FAIL_TTL_SECONDS = _cfg_int("ELEVENLABS_VOICE_FAIL_TTL_SECONDS", 900)
DESIGN_TIMEOUT_SECONDS = _cfg_int("ELEVENLABS_DESIGN_TIMEOUT_SECONDS", 45)

# Cache file lives at repo root so it survives `delete_session` (which wipes
# per-session dirs) and stays authoritative across workers/restarts. Each
# entry embeds its own session_id so cross-session sweeps/LRU can inspect it.
CACHE_PATH = ROOT / "voice_design_cache.json"

# ElevenLabs endpoints. Voice Design lives under /v1/text-to-voice.
_API_BASE = "https://api.elevenlabs.io"
_URL_DESIGN = _API_BASE + "/v1/text-to-voice/design"
_URL_SAVE_TPL = _API_BASE + "/v1/text-to-voice/{gvid}"
_URL_DELETE_TPL = _API_BASE + "/v1/voices/{voice_id}"
_URL_LIST_VOICES = _API_BASE + "/v1/voices"
_URL_SUBSCRIPTION = _API_BASE + "/v1/user/subscription"


# ────────────────────────────────────────────────────────────────────────────
# Public availability probe
# ────────────────────────────────────────────────────────────────────────────

def is_available() -> bool:
    """True when we can plausibly design + save voices.

    Cheap: only checks flag + key presence. Actual tier / quota errors surface
    at design time and degrade to the fallback voice.
    """
    return bool(ENABLED and API_KEY)


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


def _age_bucket(label: str, kind: str) -> str:
    if kind == "machine":
        return "n/a"
    if _first_hit(label, _ELDER_HINTS):
        return "elder"
    if _first_hit(label, _YOUNG_HINTS):
        return "young"
    return "adult"


def _environment(label: str, kind: str) -> str:
    if kind == "machine" or _first_hit(label, _MACHINE_HINTS):
        return "filtered through a corroded 1990s PA / intercom, faint tape hiss, band-limited"
    if kind in ("creature", "animal") or _first_hit(label, _CREATURE_HINTS):
        return "close, wet room tone, faint reverb, uncomfortably intimate"
    return "close-mic'd, natural room, minimal processing"


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
    """Pick the line ElevenLabs will actually synthesize into the preview.

    Prefer the character's own opening line (it's already in-voice), fall back
    to a neutral analog-horror snippet. Trimmed to Voice Design's practical
    upper bound (~1000 chars) but usually much shorter is better.
    """
    text = (opening or "").strip()
    if 20 <= len(text) <= 300:
        return text
    # Sneak the label into the fallback so the sample sounds slightly
    # tailored ("the warden's coming back") without being brittle.
    if label and re.match(r"^[a-z][a-z0-9 '\-]{1,30}$", label):
        return f"The {label} is close. Keep still. Don't let them hear you breathe."
    return _DEFAULT_SAMPLE


def brief_for_subject(
    subject: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Turn a SCAN subject + optional talk-context into a structured Voice
    Design brief.

    ``context`` mirrors the dict returned by ``engine.build_talk_context``:
    ``{"situation": {"phase", "chaos", ...}, "recent": [...],
       "opening_line": "..."}``. When missing, sensible defaults are used —
    the function never touches state on its own so it's cheap and
    deterministic (a property the unit tests lean on).

    Returns::

        {
          "description": <str>,      # the natural-language brief for design
          "sample_text": <str>,      # the line ElevenLabs synthesizes
          "labels":      <dict>,     # metadata for the saved voice tag
          "voice_name":  <str>,      # human-readable name for the library
        }
    """
    subject = subject or {}
    context = context or {}
    situation = context.get("situation") or {}

    label = _norm(subject.get("label")) or "figure"
    kind = _norm(subject.get("kind")) or "person"
    world_premise = str(context.get("premise") or "")[:400]
    world_scene = str(situation.get("scene") or "")[:280]
    chaos = int(situation.get("chaos") or 0)
    phase = str(situation.get("phase") or "normal")
    time_of_day = str(situation.get("time_of_day") or "")[:80]
    recent = [str(r) for r in (context.get("recent") or [])[-3:]]

    gender = _gender_hint(label, kind)
    age = _age_bucket(label, kind)
    timbre = _timbre(label, kind, age)
    delivery = _delivery(chaos, kind)
    register = _register(kind, chaos)
    env = _environment(label, kind)
    emotion = _emotion(chaos, phase, recent)

    notes_bits = []
    if world_scene:
        notes_bits.append(f"Currently in: {world_scene}")
    if time_of_day:
        notes_bits.append(f"Time: {time_of_day}")
    if recent:
        notes_bits.append("Just witnessed: " + " | ".join(r[:120] for r in recent))
    notes = " ".join(notes_bits)[:400]

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
        f"{_voice_phrase} for a {kind} known as \"{label}\" "
        f"in a 1993 analog-horror world. "
        f"Timbre: {timbre}. "
        f"Delivery: {delivery}. "
        f"Emotion: {emotion}. "
        f"Register: {register}. "
        f"Environment: {env}. "
        + (f"Character notes: {notes}. " if notes else "")
        + (f"World premise: {world_premise}. " if world_premise else "")
        + "Speak the sample line as this character would speak it, "
          "once, cleanly. Do NOT include music, background sound effects, "
          "singing, or non-speech noises."
    )
    # ElevenLabs Voice Design requires 20 <= len(voice_description) <= 1000.
    description = description[:990]

    sample = _sample_text(str(context.get("opening_line") or ""), label)

    return {
        "description": description,
        "sample_text": sample,
        "voice_name": _voice_name(label, kind),
        "labels": {
            "source": LABEL_TAG,
            "subject_label": label[:60],
            "subject_kind": kind[:20],
            "created_at": _now_iso(),
        },
    }


def _voice_name(label: str, kind: str) -> str:
    """A human-readable name for the ElevenLabs library entry.

    Kept short and greppable (the sweeper filters library ids by our
    ``source`` label, but a friendly name helps humans in the dashboard)."""
    clean = re.sub(r"[^a-zA-Z0-9 \-_]", "", label or "").strip() or "figure"
    return f"[dyn] {clean} ({kind or 'person'})"[:100]


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
# convai call still needs. Voices with refcount > 0 are skipped by
# release_session_voices and reaped on the next sweep.
_REFCOUNT: Dict[str, int] = {}
_REFCOUNT_LOCK = threading.Lock()

# Cached workspace slot ceiling (from GET /v1/user/subscription). Refreshed
# lazily; 0 means "unknown" so callers treat it as unbounded.
_SLOT_INFO = {"limit": 0, "used": 0, "checked_at": 0.0}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_ts() -> float:
    return time.time()


def _atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_cache() -> Dict[str, Any]:
    if not CACHE_PATH.exists():
        return {"version": 1, "voices": {}}
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"version": 1, "voices": {}}
        data.setdefault("version", 1)
        data.setdefault("voices", {})
        if not isinstance(data["voices"], dict):
            data["voices"] = {}
        return data
    except Exception:
        # Corrupt file — start fresh, don't crash the caller.
        return {"version": 1, "voices": {}}


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


# ────────────────────────────────────────────────────────────────────────────
# ElevenLabs HTTP wrappers (all quiet-fail)
# ────────────────────────────────────────────────────────────────────────────

def _post_design(brief: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """POST /v1/text-to-voice/design -> {previews: [...]} or None on failure."""
    if not API_KEY:
        return None
    try:
        import requests
        resp = requests.post(
            _URL_DESIGN,
            headers={"xi-api-key": API_KEY, "Content-Type": "application/json"},
            json={
                "voice_description": brief["description"],
                "model_id": TTV_MODEL,
                "text": brief["sample_text"],
                "auto_generate_text": False,
                "guidance_scale": 25,
                "loudness": 0.5,
                "quality": 0.9,
            },
            timeout=DESIGN_TIMEOUT_SECONDS,
        )
        if resp.status_code == 200:
            data = resp.json() or {}
            if isinstance(data, dict) and data.get("previews"):
                return data
            print(f"[VOICE DESIGN] design returned no previews: {str(data)[:180]}", flush=True)
            return None
        print(
            f"[VOICE DESIGN] design http {resp.status_code}: {resp.text[:180]}",
            flush=True,
        )
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[VOICE DESIGN] design exception: {e}", flush=True)
        return None


def _post_save(generated_voice_id: str, brief: Dict[str, Any]) -> Optional[str]:
    """POST /v1/text-to-voice/{gvid} to save the preview -> voice_id or None."""
    if not API_KEY or not generated_voice_id:
        return None
    try:
        import requests
        resp = requests.post(
            _URL_SAVE_TPL.format(gvid=generated_voice_id),
            headers={"xi-api-key": API_KEY, "Content-Type": "application/json"},
            json={
                "voice_name": brief["voice_name"],
                "voice_description": brief["description"],
                "labels": brief["labels"],
            },
            timeout=30,
        )
        if resp.status_code in (200, 201):
            data = resp.json() or {}
            return data.get("voice_id") or None
        print(
            f"[VOICE DESIGN] save http {resp.status_code}: {resp.text[:180]}",
            flush=True,
        )
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[VOICE DESIGN] save exception: {e}", flush=True)
        return None


def _delete_voice(voice_id: str) -> bool:
    """DELETE /v1/voices/{voice_id}. True on success or 404. Never raises."""
    if not API_KEY or not voice_id:
        return False
    try:
        import requests
        resp = requests.delete(
            _URL_DELETE_TPL.format(voice_id=voice_id),
            headers={"xi-api-key": API_KEY},
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


def _list_workspace_voices() -> List[Dict[str, Any]]:
    """GET /v1/voices — return the raw voice list (may be empty on failure)."""
    if not API_KEY:
        return []
    try:
        import requests
        resp = requests.get(
            _URL_LIST_VOICES,
            headers={"xi-api-key": API_KEY},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json() or {}
            return list(data.get("voices") or [])
        print(f"[VOICE DESIGN] list http {resp.status_code}", flush=True)
        return []
    except Exception as e:  # noqa: BLE001
        print(f"[VOICE DESIGN] list exception: {e}", flush=True)
        return []


def _get_subscription_slots() -> Tuple[int, int]:
    """Return (used, limit) from GET /v1/user/subscription. Zeros on failure."""
    if not API_KEY:
        return (0, 0)
    now = _now_ts()
    if _SLOT_INFO["checked_at"] and now - _SLOT_INFO["checked_at"] < 24 * 3600:
        return (_SLOT_INFO["used"], _SLOT_INFO["limit"])
    try:
        import requests
        resp = requests.get(
            _URL_SUBSCRIPTION,
            headers={"xi-api-key": API_KEY},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json() or {}
            used = int(data.get("voice_slots_used") or 0)
            limit = int(data.get("voice_limit") or 0)
            _SLOT_INFO.update({"used": used, "limit": limit, "checked_at": now})
            return (used, limit)
    except Exception as e:  # noqa: BLE001
        print(f"[VOICE DESIGN] subscription exception: {e}", flush=True)
    return (0, 0)


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

def _design_and_save(key: str, brief: Dict[str, Any],
                     session_id: str, subject: Dict[str, Any]) -> Optional[str]:
    """Blocking: design 3 previews, save preview[0], return the new voice_id.

    Called only from the background worker via ``_start_design_worker``. Caller
    holds the semaphore + the inflight event. Marks the cache entry ready /
    failed as it goes.
    """
    label = _norm(subject.get("label")) or "figure"
    kind = _norm(subject.get("kind")) or "person"

    # LRU pressure check BEFORE spending credits: if we're at the soft cap,
    # evict the oldest ready voice (any session) whose refcount is zero.
    _evict_if_over_soft_cap(need=1)

    result = _post_design(brief)
    # ElevenLabs bills the /design call itself (a fixed credit cost) whether
    # or not we go on to /save a preview, so record it here regardless of
    # what happens next.
    cost_tracker.record_usage(
        session_id, "voice", "elevenlabs", "voice_design", operation="design",
        output_units=1, unit_type="calls", success=bool(result),
        error_message=None if result else "design_call_failed",
    )
    if not result:
        _put_entry(key, {
            **(_get_entry(key) or {}),
            "status": "failed",
            "error": "design_call_failed",
            "failed_at": _now_iso(),
            "expires_at": _now_ts() + FAIL_TTL_SECONDS,
        })
        return None

    previews = result.get("previews") or []
    if not previews:
        _put_entry(key, {
            **(_get_entry(key) or {}),
            "status": "failed",
            "error": "no_previews",
            "failed_at": _now_iso(),
            "expires_at": _now_ts() + FAIL_TTL_SECONDS,
        })
        return None

    # Pick the first preview; keep the others for a future re-cast affordance.
    picked = previews[0]
    generated_voice_id = picked.get("generated_voice_id") or picked.get("id")
    if not generated_voice_id:
        _put_entry(key, {
            **(_get_entry(key) or {}),
            "status": "failed",
            "error": "no_gvid",
            "failed_at": _now_iso(),
            "expires_at": _now_ts() + FAIL_TTL_SECONDS,
        })
        return None

    voice_id = _post_save(generated_voice_id, brief)
    if not voice_id:
        _put_entry(key, {
            **(_get_entry(key) or {}),
            "status": "failed",
            "error": "save_call_failed",
            "generated_voice_id": generated_voice_id,
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
        "sample_text": brief["sample_text"],
        "generated_voice_id": generated_voice_id,
        "extra_preview_gvids": [
            p.get("generated_voice_id") or p.get("id")
            for p in previews[1:] if p.get("generated_voice_id") or p.get("id")
        ],
        "created_at": now,
        "last_used_at": now,
        "status": "ready",
    })
    # New voice consumes a slot — bump our cached count so subsequent LRU
    # checks are accurate without another /subscription call.
    if _SLOT_INFO["limit"]:
        _SLOT_INFO["used"] = _SLOT_INFO["used"] + 1
    return voice_id


def _start_design_worker(key: str, brief: Dict[str, Any],
                         session_id: str, subject: Dict[str, Any]) -> threading.Event:
    """Start (or return the existing) background design job for ``key``.

    The returned Event is set when the job finishes (success OR failure).
    Coalesces per-key so N concurrent SCAN taps on the same subject only spend
    ONE Voice Design credit.
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
        "sample_text": brief["sample_text"],
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
    """Force a NEW Voice Design from a stored companion description.

    Companions persist the Voice Design brief so a later beat can recreate
    the same character's voice after slot eviction / session cleanup. This
    path does **not** rebuild the brief from story context — it reuses the
    exact ``description`` seed — then evicts the prior cache entry (and
    best-effort deletes ``old_voice_id`` when its refcount is zero) before
    kicking off a fresh design job under the same cache key.

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
    desc = (description or "").strip()
    # ElevenLabs Voice Design requires 20 <= len(voice_description) <= 1000.
    if len(desc) < 20:
        return None
    desc = desc[:990]

    key = cache_key(subject, session_id, world_prompt)

    # Drop any ready/generating/failed entry for this key so we actually
    # spend a new design credit instead of returning the cached voice_id.
    existing = _get_entry(key)
    if existing:
        _drop_entries([key])
    # Best-effort: free the previous ElevenLabs slot when nothing is holding
    # a ref (an open Convai call keeps refcount > 0 and must not be yanked).
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
    brief = {
        "description": desc,
        "sample_text": _sample_text(sample_text or "", label),
        "voice_name": _voice_name(label, kind),
        "labels": {
            "source": LABEL_TAG,
            "subject_label": label[:60],
            "subject_kind": kind[:20],
            "created_at": _now_iso(),
            "regen": "1",
        },
    }
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
    Voice Design brief instead of rebuilding one from story context — the
    recovery path when a companion's ``voice_id`` was evicted but the regen
    seed survived on the roster.

    Returns a dict::

        {
          "voice_id":   "<real elevenlabs id>" | None,
          "cache_key":  "<16-hex>",
          "source":     "cache" | "designed" | "generating" | "failed" | "budget",
          "status":     "ready" | "generating" | "failed",
          "description": "<the voice-design brief>",
        }

    Never raises. Returns ``None`` when the feature is disabled / API key
    missing / the subject is missing a label. The caller should then fall back
    to whatever it used before this module existed (typically the
    ``by_kind`` map in ``voices.json``).
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

    override = (description_override or "").strip()
    if len(override) >= 20:
        kind = _norm(subject.get("kind")) or "person"
        brief = {
            "description": override[:990],
            "sample_text": _sample_text(
                str((context or {}).get("opening_line") or ""), label
            ),
            "voice_name": _voice_name(label, kind),
            "labels": {
                "source": LABEL_TAG,
                "subject_label": label[:60],
                "subject_kind": kind[:20],
                "created_at": _now_iso(),
                "from_companion": "1",
            },
        }
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
    """Cheap allowlist check: is this voice_id present in the cache with
    status='ready'? Used by ``engine._valid_voice_id`` to admit designed
    voices through the same validation as preset ones without falling back
    to the heavier ``cache_snapshot`` call on every TALK request."""
    vid = (voice_id or "").strip()
    if not vid:
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

    Voices with refcount > 0 (a live convai call still holds them) are
    skipped and left for the next sweep. Idempotent + never raises.
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
                if _SLOT_INFO["limit"] and _SLOT_INFO["used"] > 0:
                    _SLOT_INFO["used"] -= 1
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
    """The cache LRU-evicts once total ready+generating voices reach this."""
    if VOICE_SOFT_CAP_OVERRIDE > 0:
        return VOICE_SOFT_CAP_OVERRIDE
    _used, limit = _get_subscription_slots()
    if limit <= 0:
        return 20  # unknown quota — sensible upper bound
    # Leave 2 slots of headroom for concurrent designs in flight.
    return max(2, limit - 2)


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
            if _SLOT_INFO["limit"] and _SLOT_INFO["used"] > 0:
                _SLOT_INFO["used"] -= 1
    _drop_entries(evict_keys)
    if evicted:
        print(f"[VOICE DESIGN] LRU-evicted {evicted} designed voice(s)", flush=True)
    return evicted


def sweep_orphans(max_age_hours: Optional[int] = None,
                  active_session_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Reconcile with ``GET /v1/voices``: delete any voice tagged
    ``source=<LABEL_TAG>`` that is (a) older than ``max_age_hours`` and
    (b) whose ``session_id`` label is not in ``active_session_ids`` (or is
    missing). Also drops cache entries whose voice_id no longer exists on
    the server. Idempotent + never raises.
    """
    if not is_available():
        return {"deleted": 0, "unknown": 0, "kept": 0}
    max_age = int(max_age_hours if max_age_hours is not None else MAX_AGE_HOURS)
    active = set(active_session_ids or [])
    cutoff_ts = _now_ts() - max_age * 3600

    remote = _list_workspace_voices()
    deleted = 0
    kept = 0
    unknown = 0
    remote_ids: set = set()
    for v in remote:
        if not isinstance(v, dict):
            continue
        vid = v.get("voice_id")
        labels = v.get("labels") or {}
        if not vid or labels.get("source") != LABEL_TAG:
            continue
        remote_ids.add(vid)
        created_iso = str(labels.get("created_at") or "")
        session_id = str(labels.get("session_id") or "")
        try:
            created_ts = datetime.fromisoformat(
                created_iso.replace("Z", "+00:00")
            ).timestamp() if created_iso else 0.0
        except Exception:
            created_ts = 0.0
        stale = created_ts and created_ts < cutoff_ts
        orphaned = bool(session_id and session_id not in active)
        if refcount(vid) > 0:
            kept += 1
            continue
        if stale or orphaned:
            if _delete_voice(vid):
                deleted += 1
            else:
                unknown += 1
        else:
            kept += 1

    # Drop cache entries whose voice_id vanished server-side.
    with _CACHE_LOCK:
        cache = _load_cache()
        drop_keys = [
            k for k, e in (cache.get("voices") or {}).items()
            if isinstance(e, dict) and e.get("voice_id") and e["voice_id"] not in remote_ids
            and e.get("status") == "ready"
        ]
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
    """Human-readable snapshot for the admin dashboard. No secrets."""
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
    used, limit = _get_subscription_slots()
    return {
        "enabled": is_available(),
        "config": {
            "budget_per_session": DESIGN_BUDGET_PER_SESSION,
            "concurrency": DESIGN_CONCURRENCY,
            "soft_cap": _soft_cap(),
            "label_tag": LABEL_TAG,
            "ttv_model": TTV_MODEL,
        },
        "workspace_slots": {"used": used, "limit": limit},
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
