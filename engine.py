"""
engine.py – core story simulator
2025‑12‑10 deploy update

• Vision‑enabled continuity with GPT‑4‑Vision
• Fixed OpenAI error import (use openai.error)
• All other functionality unchanged
"""

from __future__ import annotations

# Debug logging (must come AFTER __future__ imports)
import sys
print("[ENGINE] engine.py module loading started...", flush=True)
sys.stdout.flush()
sys.stderr.flush()
import base64
import concurrent.futures
import hashlib
import json
import os
import random
import re
import sys
import threading
import time # Added for sleep
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from typing import Optional, List, Dict, Any
import logging
print("[ENGINE] stdlib imports complete", flush=True); sys.stdout.flush()

import openai
print("[ENGINE] openai imported", flush=True); sys.stdout.flush()
from openai import OpenAIError
print("[ENGINE] OpenAIError imported", flush=True); sys.stdout.flush()
from flask import request, jsonify
print("[ENGINE] flask imported", flush=True); sys.stdout.flush()
from PIL import Image
print("[ENGINE] PIL imported", flush=True); sys.stdout.flush()
import io
import requests  # For OpenAI multipart form-data img2img
print("[ENGINE] flask_cors imported", flush=True); sys.stdout.flush()

# Note: choices module imported locally in functions to avoid circular dependency
print("[ENGINE] About to import evolve_prompt_file...", flush=True)
import sys; sys.stdout.flush(); sys.stderr.flush()
from evolve_prompt_file import evolve_world_state
print("[ENGINE] evolve_prompt_file imported", flush=True)
sys.stdout.flush(); sys.stderr.flush()

print("[ENGINE] About to import ai_provider_manager...", flush=True)
sys.stdout.flush(); sys.stderr.flush()
import ai_provider_manager
print("[ENGINE] ai_provider_manager imported", flush=True)
sys.stdout.flush(); sys.stderr.flush()

# cost_tracker records every paid-provider call for the admin Analytics tab.
# Import is best-effort: a broken/missing analytics module must never take
# down the game engine. See ADMIN_COST_ANALYTICS_DASHBOARD_PLAN.md.
try:
    import cost_tracker
except Exception as _cost_tracker_import_error:
    print(f"[ENGINE] WARNING: cost_tracker unavailable ({_cost_tracker_import_error}); cost tracking disabled.", flush=True)

    class _NoopCostTracker:
        def record_usage(self, *args, **kwargs):
            return None

        def track(self, *args, **kwargs):
            from contextlib import contextmanager

            @contextmanager
            def _noop(*_a, **_k):
                yield {}

            return _noop()

    cost_tracker = _NoopCostTracker()

# lore_cache_manager is imported lazily (only when a lore-backed _ask runs)
# so the disabled-by-default lore cache does not load with the core engine.

# ───────── OpenAI client loader ──────────────────────────────────────────────
def _client(api_key: str, base_url: str):
    # PRODUCTION HARDENING: The OpenAI SDK raises immediately if no api_key is set.
    # In Gemini-only deployments this would crash the entire module at import time
    # and take down the API + bot. Return a stub that fails lazily on first use
    # instead so the server can still boot and serve other routes.
    if not api_key:
        print("[ENGINE INIT] OPENAI_API_KEY not set - OpenAI client disabled (Gemini-only mode).")

        class _MissingKeyClient:
            def __getattr__(self, name):
                raise RuntimeError(
                    "OpenAI client is not configured (OPENAI_API_KEY missing). "
                    "Set OPENAI_API_KEY or switch the active provider to gemini."
                )

        return _MissingKeyClient()

    if hasattr(openai, "OpenAI"):
        try:
            return openai.OpenAI(api_key=api_key, base_url=base_url)
        except Exception as e:
            print(f"[ENGINE INIT] Failed to create OpenAI client: {e}")

            class _BrokenClient:
                def __getattr__(self, name):
                    raise RuntimeError(f"OpenAI client init failed: {e}")

            return _BrokenClient()
    openai.api_key, openai.api_base = api_key, base_url

    class _Chat:
        class completions:
            create = staticmethod(openai.ChatCompletion.create)

    class _Images:
        generate = staticmethod(openai.Image.create)

    return type("LegacyClient", (), {"chat": _Chat, "images": _Images})()

# ───────── config & assets ──────────────────────────────────────────────────
ROOT = Path(__file__).parent.resolve()

# Debug mode (set DEBUG_MODE=1 environment variable to enable verbose logging)
DEBUG_MODE = os.getenv("DEBUG_MODE", "0") == "1"

# Load config from file if it exists, otherwise use empty dict (for Render deployment)
try:
    CONFIG = json.load((ROOT/"config.json").open(encoding="utf-8"))
except FileNotFoundError:
    CONFIG = {}

# Read from environment variables first, fall back to config.json
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", CONFIG.get("OPENAI_API_KEY"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    GEMINI_API_KEY = CONFIG.get("GEMINI_API_KEY", "")
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN", CONFIG.get("REPLICATE_API_TOKEN"))

# ── ElevenLabs Conversational AI (the TALK mechanic's voice layer) ──────────
# The agent id is a PUBLIC identifier (it's sent to the browser to open the
# call), so it's safe to ship a default. The default agent is PUBLIC, so voice
# works out of the box with NO secret at all — the client connects by agent id.
# The API key is an OPTIONAL secret (env or gitignored config.json): when set,
# the server mints a short-lived signed URL so PRIVATE agents also work without
# ever exposing the key to the browser.
ELEVENLABS_API_KEY = (os.getenv("ELEVENLABS_API_KEY") or CONFIG.get("ELEVENLABS_API_KEY") or "").strip()
ELEVENLABS_AGENT_ID = (os.getenv("ELEVENLABS_AGENT_ID") or CONFIG.get("ELEVENLABS_AGENT_ID")
                       or "agent_1601kxh3rz2hej9swfs75dv33q78").strip()
ELEVENLABS_VOICE_ID = (os.getenv("ELEVENLABS_VOICE_ID") or CONFIG.get("ELEVENLABS_VOICE_ID") or "").strip()
# Our default agent has prompt + first_message overrides enabled, so we push the
# full per-subject persona. Set to "0" if you point the agent id at one that
# does NOT allow overrides (the widget errors otherwise; dynamic variables still
# keep it story-aware).
ELEVENLABS_ALLOW_OVERRIDES = (os.getenv("ELEVENLABS_ALLOW_OVERRIDES", "1").strip().lower()
                              not in ("0", "false", "no", "off"))
print(f"[ENGINE INIT] ElevenLabs TALK: key={'YES' if ELEVENLABS_API_KEY else 'no'}, "
      f"agent={'set' if ELEVENLABS_AGENT_ID else 'none'}, overrides={'on' if ELEVENLABS_ALLOW_OVERRIDES else 'off'}")

# ── Voice registry (data-driven) ────────────────────────────────────────────
# Shared by the TALK mechanic (live voice switching per subject) and the
# narrator stream (world-building, possibly voiced as multiple characters).
# Loaded from voices.json so new voices/characters need NO code change. Env
# ELEVENLABS_VOICE_ID / ELEVENLABS_NARRATOR_VOICE_ID override the defaults.
try:
    VOICES_CONFIG = json.load((ROOT / "voices.json").open(encoding="utf-8"))
    if not isinstance(VOICES_CONFIG, dict):
        VOICES_CONFIG = {}
except Exception as _ve:
    print(f"[ENGINE INIT] voices.json not loaded ({_ve}); using empty registry")
    VOICES_CONFIG = {}

ELEVENLABS_NARRATOR_VOICE_ID = (os.getenv("ELEVENLABS_NARRATOR_VOICE_ID")
                                or VOICES_CONFIG.get("narrator_voice") or "").strip()
# The default TTS model: turbo is low-latency and great for realtime narration.
ELEVENLABS_TTS_MODEL = (os.getenv("ELEVENLABS_TTS_MODEL") or "eleven_turbo_v2_5").strip()
# The narrator speaks through a GENERATIVE conversational agent in the browser
# (like TALK) so it works live with NO server key. Defaults to the same public
# agent as TALK; override to give the narrator its own agent.
ELEVENLABS_NARRATOR_AGENT_ID = (os.getenv("ELEVENLABS_NARRATOR_AGENT_ID")
                                or ELEVENLABS_AGENT_ID or "").strip()


def _default_voice_id() -> str:
    return (ELEVENLABS_VOICE_ID or VOICES_CONFIG.get("default_voice") or "cjVigY5qzO86Huf0OWal").strip()


def get_voice_registry() -> dict:
    """The client-facing voice catalog: the selectable voices, the resolved
    defaults, and the per-kind + named-cast mappings. Safe to expose (no keys)."""
    voices = VOICES_CONFIG.get("voices") or []
    # Only surface well-formed entries.
    clean = [
        {"id": v.get("id"), "name": v.get("name") or v.get("id"),
         "tag": v.get("tag", ""), "gender": v.get("gender", "")}
        for v in voices if isinstance(v, dict) and v.get("id")
    ]
    return {
        "voices": clean,
        "default": _default_voice_id(),
        "narrator": ELEVENLABS_NARRATOR_VOICE_ID or _default_voice_id(),
        "by_kind": VOICES_CONFIG.get("by_kind") or {},
        "cast": VOICES_CONFIG.get("cast") or {},
    }


def _valid_voice_id(voice_id) -> str:
    """Return voice_id if it's a known/registered id, else ''. Guards against a
    client sending an arbitrary/unknown voice into ElevenLabs.

    "Known" now includes ids designed dynamically by ``voice_design`` and
    cached in ``voice_design_cache.json`` — so a hot-swapped custom character
    voice flows through the same validation as the static presets. When the
    dynamic-voices module is unavailable/disabled, its cache lookup is a
    quiet no-op and behavior falls back to today's static-only allowlist.
    """
    vid = str(voice_id or "").strip()
    if not vid:
        return ""
    known = {v.get("id") for v in (VOICES_CONFIG.get("voices") or []) if isinstance(v, dict)}
    # Also accept ids referenced by the cast (narrator characters).
    for c in (VOICES_CONFIG.get("cast") or {}).values():
        if isinstance(c, dict) and c.get("voice_id"):
            known.add(c["voice_id"])
    if vid in known:
        return vid
    # Accept designed voices from the dynamic-voices cache. Wrapped in a
    # broad try so a missing module / corrupt cache never blocks a TTS call.
    try:
        import voice_design as _vd
        if _vd.is_ready_voice_id(vid):
            return vid
    except Exception:
        pass
    return ""


def resolve_voice_for_kind(kind: str) -> str:
    """Pick the default voice for a SCAN subject kind (person/creature/…)."""
    by_kind = VOICES_CONFIG.get("by_kind") or {}
    return (by_kind.get((kind or "").strip().lower()) or _default_voice_id()).strip()


# Label-token buckets that steer the fallback voice picker toward gender/age
# hints WITHOUT running an LLM. Kept in sync with the classifiers in
# voice_design.py so a subject's fallback voice at least matches the eventual
# designed voice's basic timbre bracket.
_FALLBACK_FEMALE_HINTS = (
    "woman", "girl", "lady", "mother", "sister", "wife", "queen", "priestess",
    "witch", "widow", "matriarch", "female", "nun", "mistress", "actress",
    "waitress", "hostess",
)
_FALLBACK_MALE_HINTS = (
    "man", "boy", "father", "brother", "husband", "king", "priest", "warden",
    "sheriff", "guard", "soldier", "male", "monk", "master", "operator",
    "captain", "detective", "cowboy", "hunter",
)
_FALLBACK_MACHINE_HINTS = (
    "intercom", "radio", "speaker", "phone", "telephone", "handset", "walkie",
    "loudspeaker", "megaphone", "pa system", "terminal", "computer", "robot",
    "drone", "camera", "recorder", "machine",
)


def _hash_pick(seed: str, options: list) -> str:
    """Deterministically pick one item from ``options`` by hashing ``seed``.
    Returns "" when options is empty. Same seed always picks the same item —
    so a given character keeps the same fallback voice across TALK opens
    within a session (no "voice roulette" between reconnects)."""
    if not options:
        return ""
    import hashlib as _h
    digest = _h.sha1((seed or "").encode("utf-8", "ignore")).hexdigest()
    idx = int(digest[:8], 16) % len(options)
    return options[idx]


def resolve_fallback_voice_for_subject(subject: dict) -> str:
    """Pick a per-subject fallback voice from the static roster.

    Different from ``resolve_voice_for_kind`` (which always returns the same
    voice for every "person" / "creature" / etc.): this hashes the subject
    label into a *pool* of roster voices filtered by inferred gender/kind,
    so distinct characters get distinct-sounding voices IMMEDIATELY, before
    the per-character designed voice is ready.

    Used as the fallback in ``resolve_voice_for_subject`` when the dynamic
    voice is disabled, still generating, or failed. Always returns a
    non-empty voice_id from the registered ``voices.json`` roster.
    """
    if not isinstance(subject, dict):
        subject = {}
    label = str(subject.get("label") or "").strip().lower()
    kind = str(subject.get("kind") or "").strip().lower()

    voices = VOICES_CONFIG.get("voices") or []
    # Exclude the narrator from the character pool — it's marked with a
    # distinctive "the archive voice" tag and would break the fiction.
    def _pool(pred):
        return [
            v.get("id") for v in voices
            if isinstance(v, dict) and v.get("id") and pred(v)
            and (v.get("name") or "").lower() != "narrator"
        ]

    def _has_hint(hints):
        for h in hints:
            if re.search(r"\b" + re.escape(h) + r"\b", label):
                return True
        return False

    # Machines / voice-carriers: gender-neutral synthetic pool.
    if kind == "machine" or _has_hint(_FALLBACK_MACHINE_HINTS):
        pool = _pool(lambda v: (v.get("gender") or "").lower() == "neutral")
        if pool:
            return _hash_pick(label or "machine", pool)

    # Gender hint from the label wins over kind default.
    if _has_hint(_FALLBACK_FEMALE_HINTS):
        pool = _pool(lambda v: (v.get("gender") or "").lower() == "female")
        if pool:
            return _hash_pick(label, pool)
    if _has_hint(_FALLBACK_MALE_HINTS):
        pool = _pool(lambda v: (v.get("gender") or "").lower() == "male")
        if pool:
            return _hash_pick(label, pool)

    # Creatures / animals: prefer the deeper male roster (the husky-trickster
    # feel already picked by by_kind), but vary between them.
    if kind in ("creature", "animal"):
        pool = _pool(lambda v: (v.get("gender") or "").lower() == "male")
        if pool:
            return _hash_pick(label or kind, pool)

    # Everything else: hash across the whole human pool so two "figures" or
    # two "characters" sound different.
    pool = _pool(lambda v: (v.get("gender") or "").lower() in ("male", "female"))
    if pool:
        return _hash_pick(label or kind, pool)

    return resolve_voice_for_kind(kind)


def resolve_voice_for_subject(subject: dict, session_id: str = "default",
                              context: dict = None, world_prompt: str = "",
                              wait: float = 0.0) -> dict:
    """Pick a voice for a SCAN subject, preferring a per-character voice
    designed on the fly via ``voice_design`` and falling back to the static
    ``by_kind`` roster.

    Returns::

        {
          "voice_id":       <str>,          # ALWAYS non-empty (fallback is ok)
          "cache_key":      <str|None>,     # non-null when a dyn voice exists / is generating
          "source":         "cache" | "designed" | "generating" | "fallback" | "budget" | "failed" | "disabled",
          "status":         "ready" | "generating" | "failed" | "disabled",
          "description":    <str>,          # empty on the fallback path
        }

    The caller (``api_talk_session``) surfaces status/cache_key to the client
    so it can poll ``/api/talk/voice/status`` and hot-swap the Convai voice
    once the designed one lands. Guaranteed to always return a usable
    ``voice_id`` so callers can never end up with an empty tts.voice_id.
    """
    # Smart per-subject fallback: hash the label into a gender/kind-filtered
    # pool of roster voices so different characters sound different even
    # before their per-character voice is designed. Falls back to the flat
    # by_kind default if the roster is empty or the subject is malformed.
    fallback = resolve_fallback_voice_for_subject(subject) \
        or resolve_voice_for_kind(subject.get("kind") if isinstance(subject, dict) else "")
    try:
        import voice_design as _vd
    except Exception:
        return {"voice_id": fallback, "cache_key": None, "source": "disabled",
                "status": "disabled", "description": ""}
    if not _vd.is_available():
        return {"voice_id": fallback, "cache_key": None, "source": "disabled",
                "status": "disabled", "description": ""}
    try:
        result = _vd.get_or_design_voice(subject or {}, session_id, context=context,
                                          world_prompt=world_prompt, wait=wait)
    except Exception as _e:
        log_error(f"[VOICE DESIGN] resolver failed: {_e}")
        return {"voice_id": fallback, "cache_key": None, "source": "failed",
                "status": "failed", "description": ""}
    if not result:
        return {"voice_id": fallback, "cache_key": None, "source": "fallback",
                "status": "disabled", "description": ""}
    if result.get("voice_id") and result.get("status") == "ready":
        return {
            "voice_id": result["voice_id"],
            "cache_key": result.get("cache_key"),
            "source": result.get("source") or "cache",
            "status": "ready",
            "description": result.get("description", ""),
        }
    # generating / failed / budget: use the fallback voice but surface the
    # cache_key so the client can poll for the eventual designed voice.
    return {
        "voice_id": fallback,
        "cache_key": result.get("cache_key"),
        "source": result.get("source") or "fallback",
        "status": result.get("status") or "generating",
        "description": result.get("description", ""),
    }


def resolve_cast(character: str) -> dict:
    """Resolve a narrator 'character' name to its voice + TTS settings. Falls
    back to the narrator voice for an unknown name so narration always speaks."""
    cast = VOICES_CONFIG.get("cast") or {}
    key = (character or "narrator").strip().lower()
    entry = cast.get(key)
    if not isinstance(entry, dict) or not entry.get("voice_id"):
        entry = {"voice_id": ELEVENLABS_NARRATOR_VOICE_ID or _default_voice_id()}
    return entry

# DEBUG: Log API keys at module initialization
print(f"[ENGINE INIT] GEMINI_API_KEY loaded: {'YES' if GEMINI_API_KEY else 'NO (EMPTY!)'}")
if GEMINI_API_KEY:
    print(f"[ENGINE INIT] Key: {GEMINI_API_KEY[:20]}...{GEMINI_API_KEY[-8:]} (len={len(GEMINI_API_KEY)})")
print(f"[ENGINE INIT] Source: os.getenv={bool(os.getenv('GEMINI_API_KEY'))}, config={bool(CONFIG.get('GEMINI_API_KEY'))}")

# Load prompts — shared, hot-reloadable singleton (see prompts_store.py).
# Every PROMPTS["key"] / PROMPTS.get("key") access below automatically picks
# up edits made through the World Studio editor on the next request, with no
# process restart required.
from prompts_store import PROMPTS

# Game constants - Structured time/atmosphere tracking
INITIAL_TIME_OF_DAY = "6:30pm | weather: clear, warm light | mood: tense anticipation"  # Start time matching world_initial_state

# NOTE: engine.py no longer owns a Flask app. The feed-based game loop
# functions below (api_reset / api_feed / api_choose / api_regenerate_choices)
# are plain functions; production serves them via api.py (gunicorn api:app),
# which mounts them with add_url_rule and owns the single Flask app + CORS +
# iframe-embed headers.
CONFIG_DATA = CONFIG  # Already loaded above

# Session-based paths (thread-safe by design - no global state)
def _get_session_root(session_id='default'):
    """Get the root directory for a specific session"""
    if session_id == 'legacy':
        return ROOT  # Backward compatibility
    
    # Validate session ID for security (skip validation for 'default')
    if session_id != 'default':
        try:
            _validate_session_id(session_id)
        except ValueError as e:
            print(f"[SECURITY WARNING] Invalid session ID attempted: {session_id}")
            raise
    
    return ROOT / "sessions" / session_id

def _get_state_path(session_id='default'):
    """Get state file path for a session"""
    if session_id == 'legacy':
        return ROOT / "world_state.json"  # Backward compatibility
    root = _get_session_root(session_id)
    root.mkdir(parents=True, exist_ok=True)
    return root / "state.json"

def _get_history_path(session_id='default'):
    """Get history file path for a session"""
    if session_id == 'legacy':
        return ROOT / "history.json"  # Backward compatibility
    root = _get_session_root(session_id)
    root.mkdir(parents=True, exist_ok=True)
    return root / "history.json"

def _get_image_dir(session_id='default'):
    """Get image directory for a session"""
    if session_id == 'legacy':
        img_dir = ROOT / "generated_images"
    else:
        img_dir = _get_session_root(session_id) / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    return img_dir

def _get_video_dir(session_id='default'):
    """Get video directory for a session"""
    if session_id == 'legacy':
        vid_dir = ROOT / "generated_videos"
    else:
        vid_dir = _get_session_root(session_id) / "videos"
    vid_dir.mkdir(parents=True, exist_ok=True)
    return vid_dir

def _get_video_segments_dir(session_id='default'):
    """Get video segments directory for a session (used by Veo)"""
    segments_dir = _get_session_root(session_id) / "films" / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)
    return segments_dir

def _get_video_films_dir(session_id='default'):
    """Get final stitched films directory for a session (used by Veo)"""
    films_dir = _get_session_root(session_id) / "films" / "final"
    films_dir.mkdir(parents=True, exist_ok=True)
    return films_dir

def _get_meta_path(session_id='default'):
    """Get metadata file path for a session"""
    root = _get_session_root(session_id)
    root.mkdir(parents=True, exist_ok=True)
    return root / "meta.json"

def _validate_session_id(session_id):
    """Validate session ID to prevent path traversal and injection attacks"""
    import re
    
    if not session_id or not isinstance(session_id, str):
        raise ValueError("Session ID must be a non-empty string")
    
    # Only allow alphanumeric, hyphens, and underscores (UUID-safe + readable names)
    # No dots, slashes, backslashes, or other special characters
    if not re.match(r'^[a-zA-Z0-9_-]+$', session_id):
        raise ValueError(f"Invalid session ID '{session_id}': Only alphanumeric characters, hyphens, and underscores allowed")
    
    # Reasonable length limits
    if len(session_id) > 100:
        raise ValueError(f"Session ID too long (max 100 characters)")
    
    if len(session_id) < 1:
        raise ValueError("Session ID cannot be empty")
    
    return True

def _create_session_metadata(session_id='default', name=None, description=None):
    """Create metadata for a new session (Minecraft-style world info)"""
    from datetime import datetime, timezone
    
    meta = {
        "session_id": session_id,
        "name": name or f"Game Session {session_id[:8]}",
        "description": description or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_accessed": datetime.now(timezone.utc).isoformat(),
        "turn_count": 0,
        "player_alive": True,
        "version": "1.0"
    }
    
    meta_path = _get_meta_path(session_id)
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    
    print(f"[SESSION META] Created metadata for session '{session_id}': {name}")
    return meta

def _load_session_metadata(session_id='default', create_if_missing=True):
    """Load session metadata (like loading Minecraft world info)"""
    meta_path = _get_meta_path(session_id)
    
    # Check if session directory exists first
    session_root = _get_session_root(session_id)
    if not session_root.exists():
        if create_if_missing:
            # Create new session
            return _create_session_metadata(session_id)
        else:
            # Session doesn't exist and we shouldn't create it
            raise FileNotFoundError(f"Session '{session_id}' does not exist")
    
    if not meta_path.exists():
        # Metadata missing but session exists - recreate it
        if create_if_missing:
            return _create_session_metadata(session_id)
        else:
            raise FileNotFoundError(f"Metadata for session '{session_id}' not found")
    
    try:
        with open(meta_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[SESSION META] Error loading metadata: {e}")
        if create_if_missing:
            return _create_session_metadata(session_id)
        else:
            raise

def _update_session_metadata(session_id='default', **updates):
    """Update session metadata (like updating world stats in Minecraft)"""
    from datetime import datetime, timezone
    
    meta = _load_session_metadata(session_id)
    
    # Auto-update last_accessed
    meta["last_accessed"] = datetime.now(timezone.utc).isoformat()
    
    # Apply updates
    for key, value in updates.items():
        meta[key] = value
    
    # Save
    meta_path = _get_meta_path(session_id)
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    
    return meta

def get_all_sessions():
    """List all available sessions (like Minecraft's world list)"""
    sessions_dir = ROOT / "sessions"
    if not sessions_dir.exists():
        return []
    
    sessions = []
    for session_path in sessions_dir.iterdir():
        if session_path.is_dir() and session_path.name != '__pycache__':
            session_id = session_path.name
            try:
                meta = _load_session_metadata(session_id)
                sessions.append(meta)
            except Exception as e:
                print(f"[SESSION LIST] Error loading session {session_id}: {e}")
                # Include session even if metadata is corrupt
                sessions.append({
                    "session_id": session_id,
                    "name": f"Session {session_id[:8]}",
                    "error": str(e)
                })
    
    # Sort by last accessed (most recent first)
    sessions.sort(key=lambda s: s.get('last_accessed', ''), reverse=True)
    return sessions

def delete_session(session_id, archive_first=True):
    """
    Delete a session and all its data (like deleting a Minecraft world).
    
    Args:
        session_id: Session to delete
        archive_first: Whether to archive before deletion (default: True)
    """
    if session_id == 'default':
        raise ValueError("Cannot delete the default session. Use reset_state() instead.")
    
    session_root = _get_session_root(session_id)
    if not session_root.exists():
        raise ValueError(f"Session '{session_id}' does not exist")
    
    import shutil
    
    # Archive before deletion if requested
    if archive_first:
        archive_session(session_id, reason='manual_deletion')

    # Release any ElevenLabs voices designed for this session so we don't
    # leak workspace slots. Runs before rmtree so a failure here still lets
    # the disk cleanup proceed; the sweep will catch any stragglers later.
    try:
        import voice_design as _vd
        released = _vd.release_session_voices(session_id)
        if released.get("deleted") or released.get("skipped"):
            print(f"[SESSION DELETE] voice_design: {released}")
    except Exception as _e:
        print(f"[SESSION DELETE] voice_design release failed: {_e}")

    # Count files before deletion for logging
    file_count = sum(1 for _ in session_root.rglob('*') if _.is_file())

    # Delete the entire session directory
    shutil.rmtree(session_root)

    print(f"[SESSION DELETE] Deleted session '{session_id}' ({file_count} files)")
    return {"session_id": session_id, "files_deleted": file_count}

# Legacy constants for backward compatibility
STATE_PATH = ROOT/"world_state.json"
IMAGE_DIR = ROOT / "images"
# Use an RLock (reentrant lock), not a plain Lock: many call sites do
# `with WORLD_STATE_LOCK: ... _save_state(state)`, and _save_state() itself
# acquires this same lock. A plain Lock would deadlock the owning thread on
# that second acquisition; RLock allows the same thread to re-enter safely
# while still excluding other threads, which is what was always intended.
WORLD_STATE_LOCK = threading.RLock() # Global lock for world_state.json access

# Multi-user session framework: WORLD_STATE_LOCK above only guards the brief
# read-modify-write critical sections around a state file. It is deliberately
# NOT held across the slow parts of a turn (LLM calls, image generation),
# which instead read/write the module-global `state`/`history` mirrors
# directly as a convenience for same-session polling (see engine.py's
# "MULTI-USER SESSION CONTEXT SWITCHING" section). With multiple sessions now
# live at once, two DIFFERENT sessions' background work running concurrently
# would otherwise race on those shared mirrors — session A's turn could read
# session B's world_prompt (or vice versa) for the few hundred milliseconds
# between critical sections.
#
# TURN_LOCK closes that gap by fully serializing state-mutating engine work
# system-wide: _process_turn_background (the /api/choose turn loop),
# _perform_game_reset (the /api/reset new-game flow), and
# _generate_and_append_scene_image (all scene-image generation) each hold it
# for their ENTIRE body, not just the critical sections. This is the "only
# one turn processed by the engine at a time" concurrency model documented in
# LOBBY_MULTIUSER.md — an intentional throughput/correctness trade-off that's
# reasonable here because per-turn latency is already dominated by multi-
# second LLM + image API calls, not CPU.
TURN_LOCK = threading.RLock()

IMAGE_ENABLED       = True  # ENABLED for production
WORLD_IMAGE_ENABLED = True  # ENABLED for production
QUALITY_MODE        = True  # Quality mode: False=Gemini Flash (fast), True=Gemini Pro (high quality, slower)
VEO_MODE_ENABLED    = False # DISABLED by default - use video generation instead of images

# ── Scene renderer selection ──────────────────────────────────────────────────
# Which renderer paints the scene each turn. This is a *hint* the standalone web
# client reads (via /api/status and /api/reactor/config) to decide whether to show
# the Gemini still image or steer Reactor's realtime world model with the same
# per-turn scene prompt. The server still builds the prompt + still either way;
# defaulting to "image" keeps the classic experience fully intact.
#   "image"   -> Gemini still per turn (classic behavior)
#   "reactor" -> Reactor Happy Oyster realtime navigable world, steered by the scene prompt (default)
#   "hybrid"  -> still generated AND used to seed the realtime video (future)
# Defaults to "reactor" so the realtime world model is the out-of-the-box
# experience; the web client still auto-falls back to stills if Reactor is
# unconfigured/unavailable, and players can flip renderers from the UI.
SCENE_RENDERER = os.getenv("SCENE_RENDERER", "reactor")

# ── Realtime world-model registry ─────────────────────────────────────────────
# Reactor exposes several real-time world models through one SDK, and ships new
# ones over time. We want to be able to use ALL of them — including ones that
# don't exist yet — the moment they come out, with zero code changes. So this
# registry is deliberately data-driven and open:
#
#   1. A built-in list of every Reactor model we currently know about (below).
#   2. An env override REACTOR_MODELS (a JSON array of {id,label,sdk_name,
#      requires_seed_image,protocol}) that REPLACES/extends the built-ins — so a
#      brand-new model can be added purely by config.
#   3. REACTOR_ALLOW_CUSTOM_MODELS (default on): the web client may connect to
#      ANY model name a tester types in, even one not advertised here — so we're
#      free to experiment with a new model the instant Reactor ships it.
#
# Each entry advertises the SDK model name the browser passes to `new Reactor(...)`
# and enough metadata for the client to pick the right per-model driver:
#   • requires_seed_image — must a guide image be present before `start`?
#   • protocol — how a scene is realized on the model. Three families cover every
#     Reactor model today, and a new model defaults to the flexible "blend"
#     family so it works out of the box:
#       - "happy_oyster": prompt-to-world navigable model (Reactor Happy Oyster).
#         A world is BUILT once from a prompt (+ optional first-frame image),
#         then TRAVELLED — the live stream is driven by held movement/look and
#         interaction verbs, not by live prompt edits. A new scene = a new world
#         (rebuild: create_world -> start_travel). This is the DEFAULT model.
#       - "seed_locked": reference image is locked once a run starts, so a new
#         guide image forces a fresh stage (reset + re-establish). (LingBot)
#       - "blend": text/image-to-video; a new guide image blends in-stream with
#         no reset, prompts re-steer live. (Helios, and the default for anything
#         new.)
REACTOR_WORLD_MODEL = os.getenv("REACTOR_WORLD_MODEL", "happy-oyster")
_DEFAULT_HAPPY_OYSTER_SDK = os.getenv("REACTOR_MODEL", "reactor/happy-oyster")
_DEFAULT_LINGBOT_SDK = os.getenv("REACTOR_LINGBOT_MODEL", "reactor/lingbot-world-2")


def _default_sdk_name(model_id: str) -> str:
    """SDK model string for a model id. The repo's Reactor account uses the
    ``reactor/<id>`` namespace (see the REACTOR_MODEL default), so new/unknown
    models follow the same convention unless overridden per-entry or by env."""
    return "reactor/{}".format(model_id)


# Every Reactor world model we currently know of. Happy Oyster is the default
# (prompt-to-world, navigable in real time); the older models remain advertised
# so they're selectable live. Ones beyond these default their protocol to
# "blend" (the flexible, modern family) until proven otherwise.
_BUILTIN_WORLD_MODELS = [
    {
        "id": "happy-oyster",
        "label": "Happy Oyster",
        "sdk_name": _DEFAULT_HAPPY_OYSTER_SDK,
        # first_frame_image_url is OPTIONAL for Happy Oyster (the prompt alone
        # builds a world), but we always anchor with our generated still, so the
        # composition matches. Not "required" in the create_world sense.
        "requires_seed_image": False,
        "protocol": "happy_oyster",
    },
    {
        "id": "lingbot-world-2",
        "label": "LingBot World 2",
        "sdk_name": _DEFAULT_LINGBOT_SDK,
        "requires_seed_image": True,
        "protocol": "seed_locked",
    },
    {
        "id": "helios",
        "label": "Helios",
        "sdk_name": os.getenv("REACTOR_HELIOS_MODEL", "reactor/helios"),
        "requires_seed_image": False,
        "protocol": "blend",
    },
    {
        "id": "lingbot",
        "label": "LingBot",
        "sdk_name": _default_sdk_name("lingbot"),
        "requires_seed_image": True,
        "protocol": "seed_locked",
    },
    {
        "id": "longlive-v2",
        "label": "LongLive V2",
        "sdk_name": _default_sdk_name("longlive-v2"),
        "requires_seed_image": False,
        "protocol": "blend",
    },
    {
        "id": "sana-streaming",
        "label": "Sana Streaming",
        "sdk_name": _default_sdk_name("sana-streaming"),
        "requires_seed_image": False,
        "protocol": "blend",
    },
]


def _normalize_world_model(entry: dict) -> dict:
    """Fill in sane defaults for a (possibly partial) model registry entry."""
    mid = str(entry.get("id") or "").strip()
    if not mid:
        return None
    protocol = entry.get("protocol")
    requires_seed = entry.get("requires_seed_image")
    # Infer the missing half of (protocol, requires_seed_image) from the other.
    if protocol is None:
        protocol = "seed_locked" if requires_seed else "blend"
    if requires_seed is None:
        requires_seed = (protocol == "seed_locked")
    return {
        "id": mid,
        "label": entry.get("label") or mid,
        "sdk_name": entry.get("sdk_name") or _default_sdk_name(mid),
        "requires_seed_image": bool(requires_seed),
        "protocol": protocol,
    }


def _load_world_models() -> list:
    """Build the world-model registry: env override (REACTOR_MODELS) if valid,
    otherwise the built-in list. New models can thus be added with pure config."""
    raw = os.getenv("REACTOR_MODELS")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                models = [_normalize_world_model(e) for e in parsed if isinstance(e, dict)]
                models = [m for m in models if m]
                if models:
                    return models
            print("[reactor] REACTOR_MODELS is not a non-empty JSON array; using built-ins")
        except Exception as e:
            print("[reactor] failed to parse REACTOR_MODELS ({}); using built-ins".format(e))
    return [_normalize_world_model(m) for m in _BUILTIN_WORLD_MODELS]


AVAILABLE_WORLD_MODELS = _load_world_models()

# Let the web client connect to a model name a tester types in even if it isn't
# advertised above — so a newly released Reactor model is usable instantly.
REACTOR_ALLOW_CUSTOM_MODELS = os.getenv("REACTOR_ALLOW_CUSTOM_MODELS", "1").strip().lower() not in (
    "0", "false", "no", "off",
)


def world_model_sdk_name(model_id: str) -> str:
    """SDK model string for a world-model id.

    Falls back to the ``reactor/<id>`` convention for unknown ids so a custom /
    brand-new model still resolves to a usable SDK name."""
    for m in AVAILABLE_WORLD_MODELS:
        if m["id"] == model_id:
            return m["sdk_name"]
    return _default_sdk_name(model_id) if model_id and "/" not in model_id else model_id

# Guard so the (slow, LLM-backed) realtime "vision" re-grounding never stacks up.
_observe_reground_active = False

# ── Experience Mode System ────────────────────────────────────────────────────
# These constants name the three selectable visual modes shown at game start.
# Storing them in engine ensures a single source of truth for bot, tests, and
# any future API endpoint that exposes mode selection.

EXPERIENCE_MODE_NO_IMAGES  = "no_images"   # Text-only; all image generation off
EXPERIENCE_MODE_FLIPBOOK   = "flipbook"    # 4×4 animated GIF sequence (16 frames)
EXPERIENCE_MODE_FULL_FRAME = "full_frame"  # Single photorealistic still image (default)

EXPERIENCE_MODES: dict = {
    EXPERIENCE_MODE_NO_IMAGES: {
        "label":         "📝 Text Only",
        "emoji":         "📝",
        "description":   (
            "Pure narrative — no image generation. "
            "Fastest; every word of the horror stands alone."
        ),
        "image_enabled":  False,
        "flipbook_mode":  False,
    },
    EXPERIENCE_MODE_FLIPBOOK: {
        "label":         "🎬 Flipbook",
        "emoji":         "🎬",
        "description":   (
            "16-frame animated sequence per turn — "
            "cinematic action storytelling rendered as a looping GIF."
        ),
        "image_enabled":  True,
        "flipbook_mode":  True,
    },
    EXPERIENCE_MODE_FULL_FRAME: {
        "label":         "🖼️ Full Frame",
        "emoji":         "🖼️",
        "description":   (
            "Single photorealistic still image per turn — "
            "classic analog-horror atmosphere, no flipbook guide."
        ),
        "image_enabled":  True,
        "flipbook_mode":  False,
    },
}


def apply_experience_mode(mode: str, session_id: str = "default") -> bool:
    """Apply a named experience mode, updating engine globals and session state.

    Sets ``IMAGE_ENABLED`` / ``WORLD_IMAGE_ENABLED`` and writes
    ``flipbook_mode`` + ``experience_mode`` into the session state so that
    every subsequent turn respects the player's choice without needing to
    pass the flag around.

    Returns ``True`` on success, ``False`` if *mode* is not recognised.
    """
    global IMAGE_ENABLED, WORLD_IMAGE_ENABLED

    if mode not in EXPERIENCE_MODES:
        logging.warning(f"[EXPERIENCE] Unknown mode requested: {mode!r}")
        return False

    cfg = EXPERIENCE_MODES[mode]

    IMAGE_ENABLED       = cfg["image_enabled"]
    WORLD_IMAGE_ENABLED = cfg["image_enabled"]

    st = _load_state(session_id)
    st["flipbook_mode"]   = cfg["flipbook_mode"]
    st["experience_mode"] = mode
    _save_state(st, session_id)

    logging.info(
        f"[EXPERIENCE] Mode '{mode}' applied — "
        f"image_enabled={IMAGE_ENABLED}, flipbook={cfg['flipbook_mode']}"
    )
    return True

# OpenAI img2img consistency settings
OPENAI_IMG2IMG_ENABLED = True  # Set to False to always use text-to-image (more variation, less consistency)
OPENAI_IMG2IMG_REFERENCE_COUNT = 2  # Set to 1 if consistency is poor with 2 frames
OPENAI_IMG2IMG_QUALITY = 'medium'  # 'low', 'medium', or 'high' - higher = better consistency but less VHS degradation

DEFAULT_BASE = "https://api.openai.com/v1"
API_BASE     = (os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE).strip() or DEFAULT_BASE

client      = _client(OPENAI_API_KEY, API_BASE)
LLM_ENABLED = True

VISION_ENABLED = True  # ENABLED for production

# LEGACY boot variable only — NOT used for routing. Actual per-frame image
# generation routes on ai_provider_manager.get_image_provider() (see _gen_image),
# which reads ai_config.json at runtime. Kept for backwards compatibility/logging.
IMAGE_PROVIDER = CONFIG.get("IMAGE_PROVIDER", "gemini").lower()
print(f"[ENGINE INIT] IMAGE_PROVIDER (legacy, unused for routing): {IMAGE_PROVIDER}")
try:
    print(f"[ENGINE INIT] Runtime image provider (ai_config.json): "
          f"{ai_provider_manager.get_image_provider()}/{ai_provider_manager.get_image_model()}")
except Exception as _img_prov_err:
    print(f"[ENGINE INIT] Could not read runtime image provider: {_img_prov_err}")

# Track the last dispatch image path for vision continuity
_last_image_path: Optional[str] = None

# Global vision cache to avoid re-analyzing the same image.
# PRODUCTION HARDENING: bounded LRU to prevent unbounded memory growth
# during long-running sessions (a frequent source of bot stalls / OOM kills).
from collections import OrderedDict
import threading as _vc_threading

_VISION_CACHE_MAX = 256
_vision_cache_lock = _vc_threading.Lock()


class _BoundedLRUCache(OrderedDict):
    """OrderedDict-based LRU with thread-safe size cap."""

    def __init__(self, maxsize: int = 256):
        super().__init__()
        self._maxsize = maxsize

    def __setitem__(self, key, value):
        with _vision_cache_lock:
            if key in self:
                super().move_to_end(key)
            super().__setitem__(key, value)
            while len(self) > self._maxsize:
                self.popitem(last=False)

    def __getitem__(self, key):
        with _vision_cache_lock:
            value = super().__getitem__(key)
            super().move_to_end(key)
            return value

    def __contains__(self, key):
        with _vision_cache_lock:
            return super().__contains__(key)

    def clear(self):
        with _vision_cache_lock:
            super().clear()


_vision_cache = _BoundedLRUCache(_VISION_CACHE_MAX)

# Add a global counter for choices since last reset
_choices_since_edit_reset = 0

# Track detected movement type for Discord display
_last_movement_type = None

# Add a global flag for interior/exterior state
_is_inside = False

FORCE_TEST_THREAT = False # Global flag for testing combat trigger

print("[ENGINE INIT] Starting engine initialization...", flush=True)

# Initialize a lock for feed item ID generation if not already present
feed_item_id_lock = threading.Lock()
_next_feed_item_id = 0

def get_next_feed_item_id() -> int:
    global _next_feed_item_id
    with feed_item_id_lock:
        _next_feed_item_id += 1
        return _next_feed_item_id

def create_feed_item(type: str, content: str, image_url: Optional[str] = None, choices: Optional[List[Dict[str, str]]] = None, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    item_id = get_next_feed_item_id()
    timestamp = datetime.now(timezone.utc).isoformat()
    feed_item = {
        "id": item_id,
        "type": type,
        "content": content,
        "timestamp": timestamp,
    }
    if image_url:
        feed_item["image_url"] = image_url
    if choices:
        feed_item["choices"] = choices
    if metadata:
        feed_item["metadata"] = metadata
    return feed_item


def _feed_append(container, item):
    """Append ONE feed item to a feed_log, stamping its id at APPEND time so the
    log is always ordered by id.

    Feed ids used to be assigned at create time (create_feed_item) but appended
    later. Under the realtime concurrency (turn thread + /api/observe + scene
    image + reground all writing), an item created earlier could be appended
    AFTER one created later, leaving feed_log out of id order. The client tracks
    the highest id it has seen and asks /api/feed for `id > since_id`, so a lower
    id appended afterward is filtered out forever — the browser then polls an
    empty feed and the turn appears to freeze. Stamping the id at append time
    (always under WORLD_STATE_LOCK, which serialises appends) guarantees append
    order == id order, so nothing is ever skipped. Returns the item.
    """
    item["id"] = get_next_feed_item_id()
    if isinstance(container, dict):
        container.setdefault("feed_log", []).append(item)
    else:
        container.append(item)
    return item


def _feed_extend(container, items):
    """Append several feed items in order, stamping each id at append time.
    See _feed_append. Returns the items list."""
    for it in items:
        _feed_append(container, it)
    return items

# Dummy log_error if not present, for robustness
def log_error(message: str):
    print(f"ERROR: {message}", file=sys.stderr, flush=True)

class _SkipImage(Exception):
    """Internal sentinel: skip inline image generation (feed path streams it)."""
    pass


def _to_web_image_url(image_path, session_id: str = 'default') -> Optional[str]:
    """Convert an image path from _gen_image into a browser-servable URL for
    the standalone feed.

    _gen_image (Gemini path) returns an absolute filesystem path
    (sessions/<id>/images/<file>.png), which the Discord bot attaches directly
    but a web browser cannot load. The standalone /images/<filename> route
    serves those files by basename, so feed items must reference that URL form.

    Because every session writes into its OWN images/ dir (and different
    sessions can produce identically-named files — the basename is a
    deterministic hash of the caption), the flat basename alone is ambiguous:
    the /images route can only guess a directory and 404s whenever the active
    session isn't 'default' (e.g. a shared '/play?session=<id>' link). We embed
    the session as a '?session=<id>' query param so the route resolves the
    exact per-session directory. Omitted for the 'default'/'legacy' dirs to keep
    URLs stable for the standalone path.

    Returns None for falsy/failed inputs; passes through values already in
    '/images/..' form.
    """
    if not image_path:
        return None
    s = str(image_path)
    if s.startswith("/images/"):
        return s
    url = "/images/" + os.path.basename(s)
    if session_id and session_id not in ('default', 'legacy'):
        url += "?session=" + quote(str(session_id), safe='')
    return url

# ───────── prompt fragments ──────────────────────────────────────────────────
# NOTE: these used to be snapshotted into plain string constants at import
# time (choice_tmpl / dispatch_sys / neg_prompt / narrative_tmpl), which meant
# editing prompts/simulation_prompts.json on disk had no effect until the
# process restarted. They're now looked up live via PROMPTS[...] at every
# call site below so World Studio edits apply on the very next turn.

RISKY_ACTION_KEYWORDS = [
    "risky", "dangerous", "reckless", "chance it", "gamble", "all or nothing", 
    "desperate measure", "long shot", "against the odds", "bold move"
]

# core_modes = list(image_modes)  # Removed - not used in StoryGen version

# ───────── world‑state helpers ───────────────────────────────────────────────
def _load_state(session_id='default') -> dict:
    """Load state for a specific session"""
    state_path = _get_state_path(session_id)
    with WORLD_STATE_LOCK:
        if state_path.exists():
            try:
                # Explicitly open with utf-8, and ensure file is closed with try/finally or with statement
                with state_path.open('r', encoding='utf-8') as f:
                    st = json.load(f)
                # Ensure essential keys exist after loading
                st.setdefault('player_state', {'alive': True})
                st.setdefault('feed_log', [])
                st.setdefault('current_image_url', None)
                st.setdefault('choices', []) # Ensure choices list is present
                # Default to Full Frame (flipbook off) to match the UI default;
                # apply_experience_mode flips this on when Flipbook is chosen.
                st.setdefault('flipbook_mode', False)
                return st
            except json.JSONDecodeError as e_json:
                logging.error(f"JSONDecodeError in _load_state for {state_path}: {e_json}. File might be corrupt or empty.")
                # Fallback to a default state but log this as a critical issue
            except Exception as e_load:
                logging.error(f"Unexpected error loading {state_path} in _load_state: {e_load}")
                # Fallback for other errors too
        
        # Fallback: If file doesn't exist or loading failed, return a clean default state.
        # First-time runs and brand-new sessions are expected to hit this path,
        # so log at INFO level when the file simply doesn't exist (and only WARN
        # if there was an actual load error above — those errors are already
        # logged separately).
        if not state_path.exists():
            logging.info(f"{state_path} does not exist yet; returning default state.")
        else:
            logging.warning(f"{state_path} failed to load, returning default state.")
        return {
            "world_prompt": PROMPTS.get("world_initial_state", "Default world starting point."), # Use .get for safety
            "current_phase": "normal",
            "chaos_level": 0,
            "last_choice": "",
            "last_saved": datetime.now(timezone.utc).isoformat(),
            "seen_elements": [],
            "inventory": [],  # Player inventory
            "player_state": {"alive": True},
            "feed_log": [],
            "current_image_url": None,
            "choices": [],
            "turn_count": 0, # Initialize turn_count
            "interim_index": 0, # Initialize interim_index
            "time_of_day": INITIAL_TIME_OF_DAY,
            "flipbook_mode": False  # Full Frame default; Flipbook opt-in via experience mode
        }

# Module-global state backing the standalone feed UI. It mirrors the
# 'default' session on disk (sessions/default/state.json), the same session
# the feed endpoints and _perform_game_reset read and write, so in-memory
# state and disk state no longer diverge across restarts.
print("[ENGINE INIT] Initializing global state from 'default' session...", flush=True)
try:
    state = _load_state('default')
    print(f"[ENGINE INIT] Default-session state loaded successfully", flush=True)
except Exception as e:
    print(f"[ENGINE INIT ERROR] Failed to load default-session state: {e}", flush=True)
    import traceback
    traceback.print_exc()
    # Create default state if loading fails
    state = {
        "world_prompt": "You crouch behind a rusted Horizon vehicle at the edge of the facility.",
        "current_phase": "normal",
        "chaos_level": 0,
        "last_choice": "",
        "last_saved": datetime.now(timezone.utc).isoformat(),
        "seen_elements": [],
        "inventory": [],  # Player inventory
        "player_state": {"alive": True},
        "feed_log": [],
        "current_image_url": None,
        "choices": [],
        "choices_metadata": {},
        "turn_count": 0,
        "interim_index": 0,
        "in_combat": False,
        "threat_level": 0,
        "time_of_day": INITIAL_TIME_OF_DAY,
        "flipbook_mode": False  # Full Frame default; Flipbook opt-in via experience mode
    }
    print("[ENGINE INIT] Created default fallback state", flush=True)

# Global history mirrors the 'default' session (standalone feed path) so the
# feed no longer writes history to a separate root history.json.
history_path = _get_history_path('default')
if history_path.exists():
    try:
        with history_path.open("r", encoding="utf-8") as f:
            history = json.load(f)
    except Exception:
        history = []
else:
    history = []

# Advance the feed-item id counter past any ids already persisted in the
# resumed 'default' session so new items stay monotonically increasing and
# never collide with existing feed_log entries after a restart.
try:
    _existing_feed = state.get("feed_log", []) if isinstance(state, dict) else []
    _max_feed_id = max((int(i.get("id", 0)) for i in _existing_feed), default=0)
    if _max_feed_id > _next_feed_item_id:
        _next_feed_item_id = _max_feed_id
except Exception:
    pass

# Session-based history functions
def _load_history(session_id='default') -> list:
    """Load history for a specific session"""
    history_path = _get_history_path(session_id)
    if history_path.exists():
        try:
            with history_path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Failed to load history for session {session_id}: {e}")
            return []
    return []

def _save_history(hist: list, session_id='default'):
    """Save history for a specific session"""
    history_path = _get_history_path(session_id)
    try:
        with history_path.open("w", encoding="utf-8") as f:
            json.dump(hist, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Failed to save history for session {session_id}: {e}")

def _save_state(st: dict, session_id='default'):
    """Save state for a specific session"""
    state_path = _get_state_path(session_id)
    # CRITICAL FIX: Always acquire lock to prevent concurrent write race conditions
    with WORLD_STATE_LOCK:
        st["last_saved"] = datetime.now(timezone.utc).isoformat()
        temp_state_file = state_path.with_suffix(".json.tmp")
        max_retries = 3
        retry_delay = 0.1 # seconds

        for attempt in range(max_retries):
            try:
                temp_state_file.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding='utf-8')
                os.replace(temp_state_file, state_path)
                
                # Update session metadata (Minecraft-style world stats)
                try:
                    _update_session_metadata(
                        session_id,
                        turn_count=st.get('turn_count', 0),
                        player_alive=st.get('player_state', {}).get('alive', True)
                    )
                except Exception as meta_error:
                    print(f"[SESSION META] Warning: Failed to update metadata: {meta_error}")
                
                return # Success
            except OSError as e_os:
                logging.warning(f"Attempt {attempt + 1} to save state to {state_path} failed with OSError: {e_os}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    logging.error(f"All {max_retries} attempts to save state to {state_path} failed due to OSError: {e_os}")
            except Exception as e:
                logging.error(f"Failed to save state to {state_path} on attempt {attempt + 1}: {e}")
                if attempt == max_retries - 1 or not isinstance(e, OSError):
                    try:
                        logging.error(f"State content that failed to save: {json.dumps(st, indent=2, default=str, ensure_ascii=False)}")
                    except Exception as e_log_state:
                        logging.error(f"Could not even serialize state for error logging: {e_log_state}")
                if attempt == max_retries - 1:
                    break 
                time.sleep(retry_delay)

        logging.error(f"Persistently failed to save state to {state_path} after {max_retries} attempts.")
        if temp_state_file.exists():
            try:
                os.remove(temp_state_file)
            except Exception as e_remove:
                logging.error(f"Error removing temporary state file {temp_state_file} after failed save: {e_remove}")


# ═══════════════════════════════════════════════════════════════════
# MULTI-USER SESSION CONTEXT SWITCHING
#
# The engine keeps ONE active game state in memory (module globals `state`,
# `history`, `_next_feed_item_id`, `_last_image_path`, `history_path`) so
# the standalone feed handlers (api_reset / api_feed / api_choose) can stay
# simple. To let multiple people play concurrently against the same server
# process, each with their own persistent instance on disk, we treat that
# in-memory slot as a *context* and swap it before each session-scoped
# request handler runs.
#
# Concurrency model: turns serialize on WORLD_STATE_LOCK. Two different
# users on two different sessions can each read/write their own on-disk
# state and each interact with the game — but only one turn is actively
# being processed by the engine at a time. This is acceptable because the
# per-turn work is dominated by LLM + image API latency (multi-second),
# not CPU, and this framework lets us horizontally split later by process
# without changing the client contract.
#
# Public API:
#   engine.set_active_session(session_id) -> str
#     Persist current state under its previous session id, then load the
#     requested session's state into memory. Returns the resolved id.
#   engine.get_active_session_id() -> str
#     Returns the id of the session currently loaded in memory.
#   engine.session_context(session_id)   [context manager]
#     Acquires WORLD_STATE_LOCK, swaps active session, yields, then
#     persists on exit. Use this around anything that mutates state.
# ═══════════════════════════════════════════════════════════════════

# The session id currently loaded into the module-global `state`. The
# engine boots with the 'default' session (see _load_state('default') above)
# so start there and update as we swap.
_active_session_id: str = 'default'


def get_active_session_id() -> str:
    """Session id whose state is currently loaded in memory."""
    return _active_session_id


def _sanitize_session_id(session_id) -> str:
    """
    Normalize + validate an incoming session id. Falls back to 'default' if
    the id is missing, empty, or does not match the allowed character set.
    Never raises — a bad id from a URL simply routes to the default slot so
    the caller still gets a working game instead of a 400.
    """
    if not session_id:
        return 'default'
    try:
        sid = str(session_id).strip()
        if not sid:
            return 'default'
        # _validate_session_id accepts alnum + '_' + '-' up to 100 chars.
        _validate_session_id(sid)
        return sid
    except Exception:
        logging.warning(f"[SESSION CTX] Rejected invalid session id: {session_id!r}; using 'default'")
        return 'default'


def _resolve_request_session_id() -> str:
    """Resolve the session id for the CURRENT request straight from Flask's
    `request` object — NOT from `get_active_session_id()` / the shared
    `_active_session_id` global.

    Why this matters: `_active_session_id` (and the `state`/`history`
    mirrors) are module-level globals shared by every request thread. Flask
    runs concurrent requests on separate threads (see app.run(threaded=True)
    in api.py), so between the moment `_session_scoped` swaps the active
    session in for THIS request and the moment this request's handler body
    actually reads `get_active_session_id()`, a DIFFERENT thread handling a
    DIFFERENT session's request can run its own swap and change that shared
    global out from under us. That raced in practice: two sessions taking
    turns at the same instant could see one player's action land in BOTH
    sessions' saves.

    `flask.request` is a context-local proxy — safe to read from any thread
    handling its own request — so re-deriving the session id here (mirroring
    the exact precedence `_session_scoped` used in api.py: query string,
    then JSON body, then header) gives each request thread its own correct
    answer regardless of what any other concurrent request is doing.
    """
    try:
        sid = (
            request.args.get('session_id')
            or (request.get_json(silent=True) or {}).get('session_id')
            or request.headers.get('X-Session-Id')
        )
    except Exception:
        sid = None
    return _sanitize_session_id(sid)


def set_active_session(session_id: str) -> str:
    """
    Swap the module-global game state to the requested session.

    - Saves the currently active state to its own session file first so we
      never lose in-flight progress when a different user takes a turn.
    - Loads the target session's state (creating a fresh default state on
      disk if it does not yet exist).
    - Rebinds `state`, `history`, `history_path`, and advances
      `_next_feed_item_id` past any ids persisted in the loaded feed_log so
      newly generated items stay monotonically increasing.

    Safe to call with the same id (no-op). Always returns the resolved id.
    """
    global state, history, history_path, _next_feed_item_id, _active_session_id

    target = _sanitize_session_id(session_id)

    with WORLD_STATE_LOCK:
        if target == _active_session_id and isinstance(state, dict):
            # Already loaded; nothing to do. Touch metadata so recent-access
            # timestamps still bump for the lobby list.
            try:
                _update_session_metadata(target)
            except Exception:
                pass
            return target

        # Persist whatever is currently in memory back to its own slot before
        # we overwrite the globals. This preserves in-flight progress even if
        # a request handler forgot to _save_state() before returning.
        try:
            if isinstance(state, dict) and _active_session_id:
                _save_state(state, _active_session_id)
        except Exception as e_save_prev:
            logging.warning(f"[SESSION CTX] Failed to persist previous session '{_active_session_id}' during swap: {e_save_prev}")

        # Ensure the target session directory + metadata exist. _load_session_metadata
        # creates a fresh session on disk when create_if_missing=True (default).
        try:
            _load_session_metadata(target)
        except Exception as e_meta:
            logging.warning(f"[SESSION CTX] Metadata init failed for '{target}': {e_meta}")

        # Swap in the new session's persisted state.
        state = _load_state(target)
        history = _load_history(target)
        history_path = _get_history_path(target)

        # Feed-item ids must stay monotonically increasing per active session,
        # so bump the module-global counter past whatever the loaded feed_log
        # already contains. (This is the same guarantee the boot path makes.)
        try:
            feed = state.get('feed_log', []) if isinstance(state, dict) else []
            max_id = max((int(i.get('id', 0)) for i in feed if isinstance(i, dict)), default=0)
            if max_id > _next_feed_item_id:
                _next_feed_item_id = max_id
        except Exception:
            pass

        _active_session_id = target
        logging.info(f"[SESSION CTX] Active session -> '{target}' (feed_log len={len(state.get('feed_log', []))})")
        return target


from contextlib import contextmanager as _contextmanager

@_contextmanager
def session_context(session_id):
    """
    Context manager that switches the engine's active session for the
    duration of a request. Persists the state on exit so the on-disk copy
    always reflects any mutations made inside the block — but ONLY if
    `resolved` is still the active session when we exit.

    Why the guard: `state` is a module-global mirror shared by every
    request thread. If a DIFFERENT session's request ran (and swapped the
    mirror again) while this request's handler was doing slow work, `state`
    at exit time no longer belongs to `resolved` — blindly saving it here
    would stomp `resolved`'s on-disk file with another session's data. Every
    handler that actually needs to persist mutations now does so itself
    against an explicitly-resolved session id (see _resolve_request_session_id
    and api_choose / _perform_game_reset / api_regenerate_choices), so this
    is a safety net for anything else, not the only path to disk. When the
    mirror has moved on, the swap that moved it already persisted whatever
    was in `resolved`'s slot at that time (see set_active_session), so
    skipping here loses nothing.
    """
    resolved = set_active_session(session_id)
    try:
        yield resolved
    finally:
        try:
            with WORLD_STATE_LOCK:
                if isinstance(state, dict) and get_active_session_id() == resolved:
                    _save_state(state, resolved)
        except Exception as e:
            logging.warning(f"[SESSION CTX] Failed to persist session '{resolved}' after request: {e}")


def _sync_ambient_state(st: dict, session_id: str) -> None:
    """Best-effort refresh of the module-global `state` mirror used by the
    same-session /api/feed fast path (see set_active_session's no-op branch).

    Background threads (turn processing, scene-image generation, world
    evolution, vision reground) run well after the request that spawned them
    returns, and by the time they finish, a DIFFERENT session's request may
    have swapped the ambient mirror via session_context(). Blindly
    reassigning `state` here would leak session_id's data into whichever
    session is now active. Each caller already persists the authoritative
    copy to disk via _save_state(); this only controls the in-memory
    convenience mirror, so skip the write when session_id is no longer the
    active one — the next request for session_id will simply reload from
    disk (set_active_session always does on a real switch).

    The check-and-set runs under WORLD_STATE_LOCK so it is atomic with
    respect to set_active_session() (which also holds that lock while it
    swaps `_active_session_id` and `state` together). Without the lock the
    `get_active_session_id() == session_id` test and the `state = st` write
    straddle a window in which a concurrent set_active_session could change
    the active session out from under us, leaving `state` pointing at one
    session's data while `_active_session_id` names another — which the
    session_context exit-save would then persist to the WRONG file.
    """
    global state
    with WORLD_STATE_LOCK:
        if get_active_session_id() == session_id:
            state = st


def _sync_ambient_history(hist: list, session_id: str) -> None:
    """History counterpart of _sync_ambient_state: refresh the module-global
    `history` mirror only when session_id is still the active session, under
    WORLD_STATE_LOCK for atomicity with set_active_session. The on-disk copy
    (via _save_history) is always the source of truth; this only controls the
    in-memory convenience mirror so a finished background task can't leak its
    frames into whichever session is active by the time it completes."""
    global history
    with WORLD_STATE_LOCK:
        if get_active_session_id() == session_id:
            history = hist


def _publish_ambient(st=None, hist=None) -> None:
    """UNCONDITIONALLY set the module-global state/history mirrors.

    Used ONLY by the legacy single-session Discord bot turn path
    (advance_turn_image_fast / advance_turn_choices_deferred called with
    local_only=False), whose interaction handlers read engine.state /
    engine.history directly after a turn resolves. The bot process runs a
    single game flow and never calls set_active_session, so an unconditional
    write is correct there. The web multi-user path passes local_only=True and
    never calls this — it keeps everything in locals + on-disk per session, so
    no concurrent request can corrupt an in-flight turn via the shared mirror."""
    global state, history
    if st is not None:
        state = st
    if hist is not None:
        history = hist


def summarize_world_state(state: dict) -> str:
    """
    Return a single, actionable, dynamic sentence summarizing the most important, immediate world state or threat.
    Prioritize: player danger, pursuit, injury, chaos, visible threats, or urgent objectives.

    NOTE: This IS live — it feeds situation_summary into the dispatch/choice
    prompts (see call sites in the turn pipeline). An earlier comment wrongly
    labelled it dead code; do not remove without checking those callers.
    """
    chaos = state.get('chaos_level', 0)
    if chaos > 7:
        return "OVERWHELMING CHAOS! Immediate, decisive action is paramount to survive!"
    elif chaos > 5:
        return "CRITICAL CHAOS! Guards are on high alert and actively hunting."
    
    if not state.get('player_state', {}).get('alive', True):
        return "You are gravely wounded and in danger of dying."
    if 'storm' in state.get('world_prompt', '').lower():
        return "A violent storm is gathering overhead."
    if any(word in state.get('world_prompt', '').lower() for word in ['pursued', 'chased', 'hunted', 'spotted']):
        return "You are being pursued by hostile forces."
    if 'red biome' in state.get('world_prompt', '').lower():
        return "The red biome is dangerously close."
    # Add more as needed for your motifs
    return "You are alone, but danger could strike at any moment."

# ───────── safe OpenAI wrapper ──────────────────────────────────────────────
def _call(fn, *a, **kw):
    global LLM_ENABLED
    if not LLM_ENABLED:
        raise RuntimeError("LLM disabled")
    try:
        return fn(*a, **kw)
    except OpenAIError as e:
        if any(t in str(e).lower() for t in ("quota", "credit", "authentication", "insufficient")):
            LLM_ENABLED = False
            print("LLM disabled:", e, file=sys.stderr, flush=True)
        raise

def _ask(prompt: str, model="gemini", temp=1.0, tokens=90, image_path: str = None, use_lore: bool = True) -> str:
    """Flexible text generation supporting multiple AI providers.
    
    Args:
        prompt: Text prompt
        model: Legacy parameter (ignored - uses ai_config.json instead)
        temp: Temperature (0-1)
        tokens: Max output tokens
        image_path: Optional path to image for multimodal input (e.g. "/images/file.png")
        use_lore: Whether to include lore cache (default True). Set False for mechanical/vision tasks.
    """
    if not LLM_ENABLED:
        return random.choice([
            "System communications remain static; awaiting new data.",
            "Narrative paused until resources are replenished.",
            "The world holds its breath for new directives."
        ])
    
    # Get active provider from config
    provider = ai_provider_manager.get_text_provider()
    model_name = ai_provider_manager.get_text_model()
    
    if provider == "gemini":
        return _ask_gemini(prompt, model_name, temp, tokens, image_path, use_lore)
    elif provider == "openai":
        return _ask_openai(prompt, model_name, temp, tokens, image_path)
    elif provider == "anthropic":
        return _ask_claude(prompt, model_name, temp, tokens, image_path)
    else:
        print(f"[ASK ERROR] Unknown provider: {provider}, falling back to Gemini")
        return _ask_gemini(prompt, model_name, temp, tokens, image_path, use_lore)

def _record_text_usage(provider: str, model_name: str, *, success: bool,
                        input_units: Optional[float] = None,
                        output_units: Optional[float] = None,
                        error_message: Optional[str] = None) -> None:
    """
    Record one text-generation call for the admin Analytics tab.

    Attribution uses `get_active_session_id()` rather than a threaded-through
    `session_id` param — `_ask()`/`_ask_gemini()`/`_ask_openai()`/`_ask_claude()`
    have ~10 call sites across engine.py and none of them carry session_id
    today. This is the same known tradeoff already documented for
    `_active_session_id` elsewhere (see `_resolve_request_session_id`'s
    docstring): accurate for the common case (one active session at a time),
    imprecise under truly concurrent multi-session load. Good enough for a
    cost *estimate* dashboard; revisit if/when that becomes the norm.
    """
    try:
        cost_tracker.record_usage(
            get_active_session_id(), "text", provider, model_name,
            operation="ask", input_units=input_units, output_units=output_units,
            unit_type="tokens", success=success, error_message=error_message,
        )
    except Exception as _e:
        print(f"[COST TRACKER] _record_text_usage failed (non-fatal): {_e}", flush=True)


def _ask_gemini(prompt: str, model_name: str, temp: float, tokens: int, image_path: str = None, use_lore: bool = True) -> str:
    """Gemini text generation implementation with optional lore cache."""
    import requests
    import base64
    from pathlib import Path
    gemini_api_key = GEMINI_API_KEY
    
    try:
        # Build parts list (text + optional image)
        parts = [{"text": prompt}]
        
        # Add image if provided
        if image_path:
            # Convert path to actual file path
            if image_path.startswith("/images/"):
                actual_path = Path("images") / image_path.replace("/images/", "")
            else:
                actual_path = Path(image_path)
            
            if actual_path.exists():
                # Use pre-downsampled version if available (saves processing time)
                small_path = actual_path.parent / actual_path.name.replace(".png", "_small.png")
                use_path = small_path if small_path.exists() else actual_path
                
                with open(use_path, "rb") as f:
                    image_data = base64.b64encode(f.read()).decode('utf-8')
                
                parts.insert(0, {
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": image_data
                    }
                })
                size_note = "(480x360, 4:3)" if small_path.exists() else "(full-res)"
                print(f"[GEMINI TEXT+IMG] Including image: {image_path} {size_note}")
        
        # Check for lore cache (only if use_lore=True). Imported lazily so the
        # disabled-by-default lore cache never loads with the core engine.
        cache_id = None
        if use_lore:
            import lore_cache_manager
            cache_id = lore_cache_manager.get_cache_id()
        
        # Build request payload with ALL SAFETY FILTERS DISABLED
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}, "temperature": temp, "maxOutputTokens": tokens},
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
            ]
        }
        
        # Add cached content if available
        if cache_id:
            payload["cachedContent"] = cache_id
            print(f"[GEMINI CACHED] Using lore cache: {cache_id.split('/')[-1][:16]}...")
        elif use_lore:
            print(f"[GEMINI] Lore requested but cache not available")
        
        print(f"[GEMINI TEXT] Calling {model_name} API...", flush=True)
        _gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        _gemini_headers = {"x-goog-api-key": gemini_api_key, "Content-Type": "application/json"}
        response_data = None
        for _attempt in range(2):  # one retry on 429
            try:
                response = requests.post(_gemini_url, headers=_gemini_headers, json=payload, timeout=15)
                print(f"[GEMINI TEXT] API returned status: {response.status_code}", flush=True)
                if response.status_code == 429 and _attempt == 0:
                    print(f"[GEMINI TEXT] Rate limited (429) — retrying in 4s...", flush=True)
                    time.sleep(4)
                    continue
                response.raise_for_status()
                response_data = response.json()
                print(f"[GEMINI TEXT] Response parsed successfully", flush=True)
                break
            except requests.exceptions.Timeout:
                print(f"[GEMINI TEXT ERROR] API timeout after 15 seconds!", flush=True)
                _record_text_usage("gemini", model_name, success=False, error_message="timeout")
                return "Signal interrupted due to timeout..."
            except requests.exceptions.HTTPError as e:
                print(f"[GEMINI TEXT ERROR] HTTP error: {e}", flush=True)
                _record_text_usage("gemini", model_name, success=False, error_message=str(e))
                return "Signal interrupted due to API error..."
            except Exception as e:
                print(f"[GEMINI TEXT ERROR] Unexpected error: {type(e).__name__}: {e}", flush=True)
                import traceback
                traceback.print_exc()
                _record_text_usage("gemini", model_name, success=False, error_message=str(e))
                return "Signal interrupted..."
        if response_data is None:
            _record_text_usage("gemini", model_name, success=False, error_message="rate_limited")
            return "Signal interrupted due to rate limiting..."
        
        # Check for error response from Gemini API
        if "candidates" not in response_data:
            print(f"[ASK GEMINI ERROR] Gemini API error response: {response_data}", flush=True)
            if "error" in response_data:
                error_details = response_data['error']
                print(f"[ASK GEMINI ERROR] Code: {error_details.get('code')}, Message: {error_details.get('message')}", flush=True)
            _record_text_usage("gemini", model_name, success=False, error_message=str(response_data.get("error")))
            return "The transmission wavers... static fills the air as the signal struggles to maintain connection."
        
        result = response_data["candidates"][0]["content"]["parts"][0]["text"].strip()
        # Gemini's raw REST response already includes token counts — just never
        # read them until now. See ADMIN_COST_ANALYTICS_DASHBOARD_PLAN.md §4.
        usage = response_data.get("usageMetadata", {}) or {}
        _record_text_usage(
            "gemini", model_name, success=True,
            input_units=usage.get("promptTokenCount"),
            output_units=usage.get("candidatesTokenCount"),
        )
        return result if result else "..."
    except Exception as e:
        # Catch any unexpected errors not handled above
        log_error(f"[ASK GEMINI CRITICAL] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        _record_text_usage("gemini", model_name, success=False, error_message=str(e))
        return "Signal interrupted..."

def _ask_openai(prompt: str, model_name: str, temp: float, tokens: int, image_path: str = None) -> str:
    """OpenAI text generation implementation."""
    import base64
    from pathlib import Path
    
    # Validate API key
    if not OPENAI_API_KEY:
        print("[OPENAI TEXT] ERROR: OPENAI_API_KEY not set! Cannot generate text.")
        print("[OPENAI TEXT] Set environment variable OPENAI_API_KEY or add to config.json")
        raise ValueError("OPENAI_API_KEY not configured")
    
    try:
        messages = []
        
        # Add image if provided (using GPT-4 Vision)
        if image_path:
            if image_path.startswith("/images/"):
                actual_path = Path("images") / image_path.replace("/images/", "")
            else:
                actual_path = Path(image_path)
            
            if actual_path.exists():
                small_path = actual_path.parent / actual_path.name.replace(".png", "_small.png")
                use_path = small_path if small_path.exists() else actual_path
                
                with open(use_path, "rb") as f:
                    image_data = base64.b64encode(f.read()).decode('utf-8')
                
                messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_data}"
                            }
                        },
                        {
                            "type": "text",
                            "text": prompt
                        }
                    ]
                })
                print(f"[OPENAI TEXT+IMG] Including image: {image_path}")
            else:
                messages.append({"role": "user", "content": prompt})
        else:
            messages.append({"role": "user", "content": prompt})
        
        response = client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=temp,
            max_tokens=tokens
        )
        
        result = response.choices[0].message.content.strip()
        usage = getattr(response, "usage", None)
        _record_text_usage(
            "openai", model_name, success=True,
            input_units=getattr(usage, "prompt_tokens", None) if usage else None,
            output_units=getattr(usage, "completion_tokens", None) if usage else None,
        )
        return result if result else "..."
    except Exception as e:
        log_error(f"[ASK OPENAI] {e}")
        _record_text_usage("openai", model_name, success=False, error_message=str(e))
        return "Signal interrupted..."

def _ask_claude(prompt: str, model_name: str, temp: float, tokens: int, image_path: str = None) -> str:
    """Anthropic Claude text generation implementation."""
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        print("[CLAUDE TEXT] ERROR: ANTHROPIC_API_KEY not set! Cannot generate text.")
        return "Signal interrupted — Anthropic API key not configured."

    try:
        import anthropic as _anthropic
        _claude_client = _anthropic.Anthropic(api_key=anthropic_key)

        content: list = []

        if image_path:
            from pathlib import Path as _Path
            import base64 as _base64
            if image_path.startswith("/images/"):
                actual_path = _Path("images") / image_path.replace("/images/", "")
            else:
                actual_path = _Path(image_path)
            if actual_path.exists():
                small_path = actual_path.parent / actual_path.name.replace(".png", "_small.png")
                use_path = small_path if small_path.exists() else actual_path
                with open(use_path, "rb") as f:
                    image_data = _base64.b64encode(f.read()).decode("utf-8")
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": image_data,
                    },
                })
                print(f"[CLAUDE TEXT+IMG] Including image: {image_path}")

        content.append({"type": "text", "text": prompt})

        print(f"[CLAUDE TEXT] Calling {model_name} API...", flush=True)
        response = _claude_client.messages.create(
            model=model_name,
            max_tokens=tokens,
            temperature=temp,
            messages=[{"role": "user", "content": content}],
        )
        result = response.content[0].text.strip()
        print(f"[CLAUDE TEXT] Response received ({len(result)} chars)", flush=True)
        usage = getattr(response, "usage", None)
        _record_text_usage(
            "anthropic", model_name, success=True,
            input_units=getattr(usage, "input_tokens", None) if usage else None,
            output_units=getattr(usage, "output_tokens", None) if usage else None,
        )
        return result if result else "..."
    except Exception as e:
        log_error(f"[ASK CLAUDE] {e}")
        import traceback
        traceback.print_exc()
        _record_text_usage("anthropic", model_name, success=False, error_message=str(e))
        return "Signal interrupted..."

# ───────── path resolution helper ───────────────────────────────────────────
def _resolve_image_path(image_path: str) -> Path:
    """
    Resolve image path to actual filesystem location.
    Handles both absolute (session-specific) and relative (legacy) paths.
    
    Args:
        image_path: Path like "/opt/.../sessions/default/images/file.png" 
                    or "/images/file.png"
                    or "images/file.png"
    
    Returns:
        Path object pointing to actual file location
    """
    if not image_path:
        return None
    
    path = Path(image_path)
    
    # If already absolute and exists, use it
    if path.is_absolute():
        if path.exists():
            return path
        # Absolute but doesn't exist - maybe it's a different session
        # Try to find it in images directory
        return ROOT / "images" / path.name
    
    # Relative path handling
    if image_path.startswith("/images/"):
        # Legacy format: "/images/filename.png"
        return ROOT / "images" / path.name
    elif image_path.startswith("images/"):
        # Another legacy format: "images/filename.png"
        return ROOT / image_path
    else:
        # Just a filename
        return ROOT / "images" / path.name

# ───────── vision description helper ────────────────────────────────────────
def _downscale_for_vision(image_path: str, size=(640, 426)) -> io.BytesIO:
    full = _resolve_image_path(image_path)
    buf = io.BytesIO()
    if not full or not full.exists():
        return None
    try:
        img = Image.open(full)
        img = img.convert("RGB")
        img = img.resize(size, Image.LANCZOS)
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception as e:
        print("[VISION] Downscale error:", e, file=sys.stderr)
        return None

def _vision_analyze_all(image_path: str) -> dict:
    """
    Unified vision analysis - gets description, time of day, and color in ONE API call.
    Results are cached to avoid redundant API calls.
    
    Returns dict with keys: 'description', 'time_of_day', 'color_palette'
    """
    import base64
    import requests
    import os
    
    if not LLM_ENABLED or not VISION_ENABLED:
        return {"description": "", "time_of_day": "", "color_palette": ""}
    
    # Check cache first
    cache_key = os.path.abspath(image_path)
    if cache_key in _vision_cache:
        print(f"[VISION] Using cached analysis for {os.path.basename(image_path)}")
        return _vision_cache[cache_key]
    
    try:
        
        # Handle path - ensure it's accessible
        full_path = _resolve_image_path(image_path)
        if not full_path or not full_path.exists():
            return {"description": "", "time_of_day": "", "color_palette": ""}
        
        if not os.path.exists(full_path):
            print(f"[VISION ERROR] Image file not found: {image_path}")
            return {"description": "", "time_of_day": "", "color_palette": ""}
        
        # Use pre-downsampled version if available (saves processing time)
        from pathlib import Path
        full_path_obj = Path(full_path)
        small_path = full_path_obj.parent / full_path_obj.name.replace(".png", "_small.png")
        use_path = small_path if small_path.exists() else full_path_obj
        
        with open(use_path, "rb") as f:
            image_bytes = f.read()
        image_b64 = base64.b64encode(image_bytes).decode('utf-8')
        
        if small_path.exists():
            print(f"[VISION] Using pre-downsampled image (480x360, 4:3)")
        
        # Determine MIME type
        mime_type = "image/png"
        if str(full_path).endswith(('.jpg', '.jpeg')):
            mime_type = "image/jpeg"
        
        # Use Gemini vision API - ONE call for everything
        print(f"[VISION] Analyzing {os.path.basename(image_path)} (all-in-one)...")
        
        vision_prompt = """Analyze this image and respond in this EXACT format:

TIME: <time of day - use ONLY: dawn, morning, afternoon, golden hour, dusk, or night>
COLOR: <dominant color palette in 5-10 words>
DESCRIPTION: <detailed description of what is visible, focusing on objects, threats, exits, and anything you could interact with. Be direct and literal. If there are hands, weapons, tools, figures, silhouettes, or creatures visible, mention them explicitly.>
SPATIAL: <spatial compass — describe: (a) what is DIRECTLY AHEAD at what distance, (b) what is visible to the LEFT, (c) what is visible to the RIGHT, (d) what is underfoot/ground type, (e) camera height estimate (standing/crouching/elevated). Keep under 50 words. Example: "Ahead: chain-link fence ~20m with facility gate. Left: red mesa cliff face ~100m. Right: abandoned Horizon truck ~15m. Ground: sandy desert with scrub. Standing height.">
SETTING: <ONE of: outdoor-desert, outdoor-cliff, outdoor-road, indoor-corridor, indoor-lab, indoor-warehouse, indoor-other, transitional>"""
        
        # Model note: gemini-2.0-flash-exp and later gemini-2.0-flash were retired
        # on the current API/key (text calls 404'd while gemini-3.1-flash-lite-image still
        # worked). Use the current gemini-3.1-flash-lite text model.
        api_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"
        
        # DEBUG: Log API key status for Vision
        if not GEMINI_API_KEY:
            print(f"[VISION ERROR] GEMINI_API_KEY is EMPTY or None!")
        else:
            print(f"[VISION DEBUG] Using API key: {GEMINI_API_KEY[:10]}...{GEMINI_API_KEY[-5:]}")

        headers = {
            "x-goog-api-key": GEMINI_API_KEY,
            "Content-Type": "application/json"
        }
        
        payload = {
            "contents": [{
                "parts": [
                    # IMAGE FIRST per Gemini best practices for single-image prompts
                    {
                        "inlineData": {
                            "mimeType": mime_type,
                            "data": image_b64
                        }
                    },
                    {"text": vision_prompt}
                ]
            }],
            "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}, 
                "temperature": 1.0,  # Default for Gemini 2.x/3.x per guidelines
                "maxOutputTokens": 800
            },
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
            ]
        }
        
        response = requests.post(api_url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        
        result = response.json()
        
        # Extract text from Gemini response
        if "candidates" not in result or not result["candidates"]:
            print(f"[VISION ERROR] No candidates in response")
            return {"description": "", "time_of_day": "", "color_palette": ""}
        
        candidate = result["candidates"][0]
        if "content" not in candidate or "parts" not in candidate["content"]:
            print(f"[VISION ERROR] Invalid response structure")
            return {"description": "", "time_of_day": "", "color_palette": ""}
        
        parts = candidate["content"]["parts"]
        full_text = ""
        for part in parts:
            if "text" in part:
                full_text += part["text"]
        
        if not full_text:
            print(f"[VISION ERROR] No text in response")
            return {"description": "", "time_of_day": "", "color_palette": ""}
        
        # Parse the structured response
        time_of_day  = ""
        color_palette = ""
        description  = ""
        spatial      = ""   # NEW: directional compass (ahead/left/right/ground/height)
        setting      = ""   # NEW: environment type (outdoor-desert, indoor-corridor, etc.)

        lines = full_text.strip().split("\n")
        for i, line in enumerate(lines):
            if line.startswith("TIME:"):
                time_of_day = line.replace("TIME:", "").strip()
            elif line.startswith("COLOR:"):
                color_palette = line.replace("COLOR:", "").strip()
            elif line.startswith("DESCRIPTION:"):
                description = line.replace("DESCRIPTION:", "").strip()
                # Capture continuation lines that are NOT other field labels
                j = i + 1
                while j < len(lines) and not any(
                    lines[j].startswith(k) for k in ("SPATIAL:", "SETTING:", "TIME:", "COLOR:")
                ):
                    description += " " + lines[j]
                    j += 1
            elif line.startswith("SPATIAL:"):
                spatial = line.replace("SPATIAL:", "").strip()
            elif line.startswith("SETTING:"):
                setting = line.replace("SETTING:", "").strip()

        # If parsing failed, fall back to raw text
        if not description:
            description = full_text

        result_dict = {
            "description":   description.strip().replace("\n", " "),
            "time_of_day":   time_of_day.strip(),
            "color_palette": color_palette.strip(),
            "spatial":       spatial.strip(),
            "setting":       setting.strip(),
        }

        # Cache the result
        _vision_cache[cache_key] = result_dict

        print(
            f"[VISION] Analysis complete: {len(description)} chars, "
            f"time={time_of_day}, setting={setting}, "
            f"spatial='{spatial[:60]}...'" if len(spatial) > 60 else
            f"[VISION] Analysis complete: {len(description)} chars, "
            f"time={time_of_day}, setting={setting}, spatial='{spatial}'"
        )
        return result_dict
    
    except requests.exceptions.HTTPError as e:
        safe_e = str(e).encode('ascii', 'replace').decode('ascii')
        print(f"[VISION ERROR] Gemini API HTTP error: {safe_e}")
        if e.response is not None:
            print(f"[VISION ERROR] Response: {e.response.text}")
        return {"description": "", "time_of_day": "", "color_palette": ""}
    except Exception as e:
        safe_e = str(e).encode('ascii', 'replace').decode('ascii')
        print(f"[VISION ERROR] Failed to analyze image: {safe_e}")
        import traceback
        traceback.print_exc()
        return {"description": "", "time_of_day": "", "color_palette": ""}

# Legacy wrapper for backward compatibility
def _vision_describe(image_path: str) -> str:
    """Get image description (uses cached unified analysis)."""
    result = _vision_analyze_all(image_path)
    return result["description"]


# Labels that read as something you could hold a conversation with, even when
# the vision model forgets to flag `speaks`. Kept deliberately broad (people,
# figures, sentient creatures, and machines that carry a voice) because a false
# positive just offers a TALK option the story can gracefully play off, while a
# false negative silently hides the whole mechanic.
_SPEAKER_LABEL_RE = re.compile(
    r"\b("
    r"person|people|man|men|woman|women|boy|girl|child|children|kid|guy|lady|"
    r"figure|silhouette|stranger|survivor|soldier|guard|worker|scientist|"
    r"doctor|nurse|officer|cop|ranger|pilot|driver|operator|technician|"
    r"crowd|villager|prisoner|captive|hostage|patient|passenger|civilian|"
    r"face|head|corpse|body|ghost|spirit|apparition|"
    r"creature|beast|monster|alien|humanoid|android|robot|droid|cyborg|"
    r"ai|hologram|avatar|puppet|doll|mannequin|statue|"
    r"radio|intercom|speaker|phone|telephone|handset|walkie|walkie-talkie|"
    r"transceiver|receiver|terminal|console|computer|monitor|screen|"
    r"pa system|loudspeaker|megaphone|"
    r"dog|cat|horse|bird|parrot|crow|raven|snake|wolf|fox|rat|owl"
    r")\b"
)

# Kinds the model may return that we consider conversational.
_SPEAKER_KINDS = {"person", "character", "creature"}


def _classify_speaker(label: str, kind_raw, speaks_raw) -> tuple:
    """Decide a detected thing's ``kind`` and whether it ``speaks``.

    Fuses the vision model's own classification with a label heuristic so the
    TALK affordance is offered whenever a person, character, sentient creature,
    or voice-carrying machine is on screen — and withheld for inert scenery.

    Returns ``(kind: str, speaks: bool)`` where kind is one of
    person/character/creature/animal/machine/object.
    """
    valid_kinds = {"person", "character", "creature", "animal", "machine", "object"}
    kind = str(kind_raw or "").strip().lower()
    if kind not in valid_kinds:
        kind = ""

    # Normalize the model's speaks flag (it may arrive as a real bool, a string,
    # or be absent entirely).
    speaks = None
    if isinstance(speaks_raw, bool):
        speaks = speaks_raw
    elif isinstance(speaks_raw, str):
        speaks = speaks_raw.strip().lower() in ("true", "yes", "1")

    label_hit = bool(_SPEAKER_LABEL_RE.search(label or ""))

    # Infer a kind when the model didn't give a usable one.
    if not kind:
        if label_hit:
            if re.search(r"\b(radio|intercom|speaker|phone|telephone|handset|walkie|"
                         r"walkie-talkie|transceiver|receiver|terminal|console|computer|"
                         r"monitor|screen|robot|droid|android|cyborg|ai|hologram|"
                         r"loudspeaker|megaphone|pa system)\b", label):
                kind = "machine"
            elif re.search(r"\b(creature|beast|monster|alien|humanoid|ghost|spirit|apparition)\b", label):
                kind = "creature"
            elif re.search(r"\b(dog|cat|horse|bird|parrot|crow|raven|snake|wolf|fox|rat|owl)\b", label):
                kind = "animal"
            else:
                kind = "person"
        else:
            kind = "object"

    # Decide speaks: honor an explicit model call, else fall back to the kind +
    # the label heuristic.
    if speaks is None:
        speaks = kind in _SPEAKER_KINDS or label_hit
    else:
        # Even a model "true" needs a plausible subject; and a model "false" is
        # overridden when the label very clearly names a speaker (people/voices).
        speaks = speaks or label_hit
        if kind == "object" and not label_hit:
            speaks = False

    return kind, bool(speaks)


# Shared keep-alive HTTP session for Gemini vision calls. Reopening a TLS
# connection on every /api/detect (which fires at a ~2.1-2.6 s cadence while
# the SCAN overlay is live) burns 100-300 ms per call; a pooled session cuts
# that to near zero and keeps our request/response amortized across the run.
_GEMINI_HTTP_SESSION = requests.Session()

# JSON schema Gemini must adhere to for /api/detect responses. Using a
# responseSchema (not just responseMimeType) guarantees a well-formed array
# with the exact fields we consume — no code-fence stripping, no regex
# fallback, no "wrapped the JSON in prose" edge cases.
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


def _read_detect_image_bytes(image_path: str) -> tuple:
    """Load a path-based detection input into (bytes, mime_type).

    Prefers the pre-downsampled ``*_small.png`` companion when present, matching
    the behavior we've used since ``_vision_analyze_all``. Returns
    ``(None, None)`` if the path can't be resolved.
    """
    full_path = _resolve_image_path(image_path)
    if not full_path or not os.path.exists(full_path):
        return None, None
    full_path_obj = Path(full_path)
    small_path = full_path_obj.parent / full_path_obj.name.replace(".png", "_small.png")
    use_path = small_path if small_path.exists() else full_path_obj
    with open(use_path, "rb") as f:
        image_bytes = f.read()
    mime_type = "image/jpeg" if str(use_path).lower().endswith((".jpg", ".jpeg")) else "image/png"
    return image_bytes, mime_type


def _detect_objects(image_path: str = None,
                    max_items: int = 8,
                    image_bytes: bytes = None,
                    mime_type: str = None,
                    scene_prompt: str = "") -> list:
    """Realtime object recognition for the live scene.

    Ask Gemini for the prominent, interactable things visible in a frame and
    their 2D bounding boxes, so the standalone UI can float "starfield" tags
    over the live video where each object actually sits. This powers the SCAN
    tool: the player drags across the scene and the world names what it sees.

    Callers can pass either ``image_path`` (legacy) or the raw frame directly
    via ``image_bytes`` + ``mime_type`` (preferred from ``api_detect``, which
    already has the frame in memory — this avoids a disk round-trip plus a
    second base64 encode per call). When both are given, bytes win.

    ``scene_prompt`` is the exact text the world model was steered with for
    this frame (``state['current_image_prompt']``). Passing it as extra context
    gives Gemini strong priors ("weathered valve wheel" vs "handle") without
    changing the response schema.

    Returns a list of dicts (at most ``max_items``), each:
        {"label": str, "cx": float, "cy": float, "w": float, "h": float,
         "kind": str, "speaks": bool}
    where cx/cy are the box CENTER and w/h its size, all normalized 0..1
    relative to the frame. ``kind`` is one of person/character/creature/
    animal/machine/object and ``speaks`` marks whether the thing can be TALKED
    to (drives the SCAN "TALK" affordance). Returns [] on any failure (never
    raises).
    """
    import base64
    import json as _json
    import random as _random
    import time as _time

    if not LLM_ENABLED or not VISION_ENABLED or not GEMINI_API_KEY:
        return []

    try:
        if image_bytes is None:
            image_bytes, mime_type = _read_detect_image_bytes(image_path)
            if image_bytes is None:
                return []
        if not mime_type:
            mime_type = "image/jpeg"
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        detect_instructions = (
            "Detect the prominent, distinct things a person could look at or "
            "interact with in this image (objects, tools, doors, exits, "
            "figures, creatures, vehicles, hazards). "
            f"Return AT MOST {max_items} of the most salient. "
            "Each item: label (1-3 word noun, lowercase), "
            "box_2d [ymin, xmin, ymax, xmax] as integers 0-1000 "
            "(y top-to-bottom, x left-to-right), "
            "kind (person/character/creature/animal/machine/object), "
            "speaks (true|false). "
            "Set \"speaks\" true ONLY for something that could plausibly hold a "
            "conversation right now: a visible person, humanoid figure, named "
            "character, sentient creature, or a talking machine (radio, phone, "
            "intercom, robot, terminal with a voice). Set it false for inert "
            "objects, scenery, tools, and plain animals that would not speak. "
            "Prefer specific, concrete labels over vague ones. "
            "Skip generic background like 'sky', 'ground', 'wall' unless notable."
        )

        # Story-grounded prior: the world model was steered by this exact
        # prompt, so folding it in sharpens labels on the ambiguous stuff
        # (a "handle" the prompt calls "weathered valve wheel"). Kept short so
        # it can't dominate the visual signal.
        prompt_prior = ""
        if scene_prompt:
            prior = scene_prompt.strip().replace("\n", " ")
            if len(prior) > 500:
                prior = prior[:500].rstrip() + "..."
            prompt_prior = (
                "This frame was rendered from the following scene prompt. "
                "Use it as a hint for specific, story-grounded labels, but "
                "only tag things you can actually see in the pixels — do NOT "
                "invent objects that are named in the prompt but not visible.\n"
                f"SCENE PROMPT: {prior}\n\n"
            )

        api_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"
        headers = {
            "x-goog-api-key": GEMINI_API_KEY,
            "Content-Type": "application/json",
        }
        payload = {
            "contents": [{
                "parts": [
                    {"inlineData": {"mimeType": mime_type, "data": image_b64}},
                    {"text": prompt_prior + detect_instructions},
                ]
            }],
            "generationConfig": {
                "thinkingConfig": {"thinkingBudget": 0},
                # Detection is a classification task: deterministic runs mean
                # tag labels stay stable frame-to-frame, so the client-side
                # reconciler doesn't get whiplashed by "figure" vs "person"
                # renaming on identical pixels.
                "temperature": 0.0,
                "maxOutputTokens": 700,
                "responseMimeType": "application/json",
                "responseSchema": _DETECT_RESPONSE_SCHEMA,
            },
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ],
        }

        # Realtime cadence: /api/detect fires every ~2.5 s, so a 20 s timeout
        # was long enough to stack multiple in-flight calls and completely
        # blackhole the SCAN overlay on a single slow response. Cap it tight
        # and retry once on the errors that are actually worth retrying
        # (429 rate-limit, 5xx transient) with jittered backoff.
        response = None
        last_err = None
        for attempt in range(2):
            try:
                response = _GEMINI_HTTP_SESSION.post(
                    api_url, headers=headers, json=payload, timeout=8
                )
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    last_err = requests.exceptions.HTTPError(
                        f"{response.status_code} {response.reason}", response=response
                    )
                    if attempt == 0:
                        _time.sleep(0.35 + _random.random() * 0.35)
                        continue
                    raise last_err
                response.raise_for_status()
                last_err = None
                break
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError) as e:
                last_err = e
                if attempt == 0:
                    _time.sleep(0.35 + _random.random() * 0.35)
                    continue
                raise
        if last_err is not None and response is None:
            raise last_err
        result = response.json()

        candidates = result.get("candidates") or []
        if not candidates:
            return []
        parts = (candidates[0].get("content") or {}).get("parts") or []
        full_text = "".join(p.get("text", "") for p in parts).strip()
        if not full_text:
            return []

        # With responseSchema Gemini returns strict JSON — a single json.loads
        # is enough. Still guard: if the model somehow returns nothing valid,
        # log it and return [] rather than raising into the request handler.
        try:
            parsed = _json.loads(full_text)
        except Exception as parse_err:
            log_error(f"[DETECT] malformed structured response: {parse_err} :: {full_text[:200]}")
            return []

        if not isinstance(parsed, list):
            return []

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
                ymin, xmin, ymax, xmax = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
            except Exception:
                continue
            # Normalize the 0-1000 grid to 0..1, guarding against swapped bounds.
            ymin, ymax = sorted((ymin / 1000.0, ymax / 1000.0))
            xmin, xmax = sorted((xmin / 1000.0, xmax / 1000.0))
            cx = max(0.0, min(1.0, (xmin + xmax) / 2.0))
            cy = max(0.0, min(1.0, (ymin + ymax) / 2.0))
            w = max(0.0, min(1.0, xmax - xmin))
            h = max(0.0, min(1.0, ymax - ymin))
            # Drop degenerate or full-frame boxes (not useful as a tag point).
            if w <= 0.001 or h <= 0.001 or (w >= 0.98 and h >= 0.98):
                continue
            key = label[:24]
            if key in seen:
                continue
            seen.add(key)
            # Classify whether this thing can be TALKED to. Trust the model's
            # own call, but backstop it with a label heuristic so the TALK
            # affordance still appears when the model omits/undercalls the field.
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
            if len(objects) >= max_items:
                break

        return objects
    except requests.exceptions.HTTPError as e:
        safe_e = str(e).encode("ascii", "replace").decode("ascii")
        log_error(f"[DETECT] Gemini API HTTP error: {safe_e}")
        return []
    except Exception as e:
        safe_e = str(e).encode("ascii", "replace").decode("ascii")
        log_error(f"[DETECT] Failed to detect objects: {safe_e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Realtime danger perception (for the peripheral-vignette / health system).
#
# One vision call, one answer: "is what's on screen dangerous, right now?"
# Fired ~1x/second by the client against the live world-model frame. Kept
# deliberately narrow — three ordinal levels + a short human-readable reason
# + optional bbox of the primary threat — so the client's state machine has
# a single, predictable signal to drive the red-vignette pulse and health
# drain. Nothing here mutates world state; this is a read-only perception
# probe, like /api/detect.
# ─────────────────────────────────────────────────────────────────────────────

_DANGER_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        # 0 = safe, 1 = threatened (hostile in frame, not committing),
        # 2 = attacking (aimed/lunging at camera, or camera is IN a hazard).
        "level": {"type": "INTEGER", "enum": [0, 1, 2]},
        # A terse, human-readable reason (<=8 words). Shown in the client
        # log for tuning and debugging; never rendered to the player.
        "reason": {"type": "STRING"},
        # Optional bbox of the primary threat, so the client can bias the
        # vignette gradient toward the edge that thing is coming from.
        # Same 0-1000 int format as _detect_objects for consistency.
        "threat_box": {"type": "ARRAY", "items": {"type": "NUMBER"}},
    },
    "required": ["level"],
}


def _perceive_danger(image_bytes: bytes = None,
                     mime_type: str = None,
                     scene_prompt: str = "") -> dict:
    """Grade the live frame's threat level for the peripheral-vignette loop.

    Returns a dict:
        {"level": 0|1|2,
         "reason": str,
         "direction": "left"|"right"|"top"|"bottom"|"center"|None,
         "threat_cx": float,   # 0..1, only when a threat_box is present
         "threat_cy": float}

    ``level``:
      • 0 — safe. Nothing hostile / actively harmful in frame.
      • 1 — threatened. Hostile entity present and oriented toward camera,
            OR player is adjacent to a live environmental hazard.
      • 2 — attacking. Hostile weapon/limb pointed at or striking camera,
            OR camera is physically inside a hazard (in the flames, on the
            collapsing floor, engulfed by the toxic cloud, etc).

    Returns level=0 with reason="" on any failure — the danger loop should
    NEVER be able to hurt the player because a vision call went sideways.
    """
    import base64
    import json as _json
    import random as _random
    import time as _time

    safe = {"level": 0, "reason": "", "direction": None,
            "threat_cx": None, "threat_cy": None}

    if not LLM_ENABLED or not VISION_ENABLED or not GEMINI_API_KEY:
        return safe
    if not image_bytes:
        return safe
    if not mime_type:
        mime_type = "image/jpeg"

    try:
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        # Tuned rubric. Three levels, one paragraph each — the goal is that
        # the SAME frame gets the SAME level on re-runs (client state machine
        # stability), while ALSO being sensitive enough that a typical
        # exploration scene actually surfaces threats. Prior version required
        # a hostile to be aimed AT the camera to escalate, which made the
        # loop effectively inert in a world-model exploration game (most
        # frames are rooms/corridors, not gunfights). We now escalate on
        # anything a person would reasonably flinch at: visible weapons,
        # aggressive creatures, active hazards in-frame, high edges,
        # aggressive approach — even when not committed AT camera yet.
        danger_instructions = (
            "You are a threat detector for a first-person survival exploration "
            "game. Look at this single frame from the player's point-of-view "
            "camera and grade its immediate danger to the person BEHIND the "
            "camera. Return ONE JSON object with fields: level (integer 0, 1, "
            "or 2), reason (<= 8 words, lowercase), and optionally threat_box "
            "[ymin, xmin, ymax, xmax] integers 0-1000 (y top-to-bottom, "
            "x left-to-right) around the single most dangerous thing in view.\n\n"
            "LEVEL 0 — SAFE: nothing that could plausibly injure the player is "
            "in frame. Calm empty rooms, benign scenery, cluttered but inert "
            "spaces, distant crowds of non-hostile figures, safe outdoor areas. "
            "Choose 0 only when the scene is clearly benign.\n\n"
            "LEVEL 1 — THREATENED: something in the frame that a reasonable "
            "person would want to move away from. Examples that qualify:\n"
            "  • A visible weapon carried by anyone in view (gun, blade, "
            "improvised weapon), whether or not it is aimed at camera.\n"
            "  • A hostile-looking creature, monster, or aggressive animal "
            "anywhere in frame, whether or not it faces camera.\n"
            "  • A human figure with visibly hostile posture (raised fist, "
            "advancing on camera, glaring / staring down camera).\n"
            "  • An active environmental hazard visible in frame: open flame, "
            "large fire, exposed live wiring / sparks, deep water, high drop / "
            "cliff edge, unstable / collapsing structure, poisonous / toxic "
            "atmosphere hints (smoke, gas, dense fog), extreme heights, "
            "radiation / warning signs indicating current danger.\n"
            "  • Blood, gore, or fresh signs of violence in the immediate area "
            "(indicates the space is unsafe).\n"
            "  • Camera is in a very unsafe posture: standing on a narrow "
            "ledge, looking down a shaft, near a large predator, at the mouth "
            "of a burning corridor.\n"
            "You have a beat to react — the threat is present but not "
            "yet committed to harming you.\n\n"
            "LEVEL 2 — ATTACKING: damage is imminent or already happening. "
            "Examples that qualify:\n"
            "  • A weapon, limb, or projectile committed toward the camera "
            "(muzzle flash, swinging blade close to lens, incoming projectile, "
            "hands reaching for the lens).\n"
            "  • A creature or hostile figure lunging / charging directly at "
            "the camera, filling a significant portion of the frame.\n"
            "  • Camera is PHYSICALLY INSIDE a hazard: engulfed in flames, "
            "submerged in toxic fluid, buried under debris, falling, being "
            "crushed, surrounded by fire on multiple sides.\n"
            "  • The frame itself is visibly obscured by the attack: blood on "
            "the lens, cracked visor, smoke choking the view.\n\n"
            "RULES:\n"
            "• Grade the FRAME, not the story. Don't infer off-screen dangers.\n"
            "• Any visible weapon or hostile creature is at least LEVEL 1, even "
            "if the camera isn't currently being targeted. In an exploration "
            "game, seeing a threat is itself dangerous.\n"
            "• Dark / low-visibility scenes are NOT automatically dangerous. "
            "Grade what you can actually see; if the scene is dark but the "
            "visible content is benign, choose 0.\n"
            "• Prefer LEVEL 1 over LEVEL 0 whenever there is a plausible "
            "threat in frame — the game's danger vignette should be a live "
            "signal, not a rarely-triggered alarm."
        )

        prompt_prior = ""
        if scene_prompt:
            prior = scene_prompt.strip().replace("\n", " ")
            if len(prior) > 400:
                prior = prior[:400].rstrip() + "..."
            prompt_prior = (
                "This frame was rendered from the following scene prompt. "
                "Use it as a hint for what the world contains, but grade "
                "ONLY what you can actually see in the pixels.\n"
                f"SCENE PROMPT: {prior}\n\n"
            )

        api_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"
        headers = {
            "x-goog-api-key": GEMINI_API_KEY,
            "Content-Type": "application/json",
        }
        payload = {
            "contents": [{
                "parts": [
                    {"inlineData": {"mimeType": mime_type, "data": image_b64}},
                    {"text": prompt_prior + danger_instructions},
                ]
            }],
            "generationConfig": {
                "thinkingConfig": {"thinkingBudget": 0},
                # Deterministic: same frame → same level. Predictability
                # matters more here than variety.
                "temperature": 0.0,
                "maxOutputTokens": 120,
                "responseMimeType": "application/json",
                "responseSchema": _DANGER_RESPONSE_SCHEMA,
            },
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ],
        }

        # Same tight-timeout + one-retry pattern as _detect_objects. The
        # danger loop is running at ~1 Hz, so a stalled call must NOT stack.
        response = None
        last_err = None
        for attempt in range(2):
            try:
                response = _GEMINI_HTTP_SESSION.post(
                    api_url, headers=headers, json=payload, timeout=6
                )
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    last_err = requests.exceptions.HTTPError(
                        f"{response.status_code} {response.reason}", response=response
                    )
                    if attempt == 0:
                        _time.sleep(0.25 + _random.random() * 0.25)
                        continue
                    raise last_err
                response.raise_for_status()
                last_err = None
                break
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError) as e:
                last_err = e
                if attempt == 0:
                    _time.sleep(0.25 + _random.random() * 0.25)
                    continue
                raise
        if last_err is not None and response is None:
            raise last_err
        result = response.json()

        candidates = result.get("candidates") or []
        if not candidates:
            return safe
        parts = (candidates[0].get("content") or {}).get("parts") or []
        full_text = "".join(p.get("text", "") for p in parts).strip()
        if not full_text:
            return safe

        try:
            parsed = _json.loads(full_text)
        except Exception as parse_err:
            log_error(f"[DANGER] malformed response: {parse_err} :: {full_text[:200]}")
            return safe
        if not isinstance(parsed, dict):
            return safe

        try:
            level = int(parsed.get("level", 0))
        except Exception:
            level = 0
        if level not in (0, 1, 2):
            level = max(0, min(2, level))
        reason = str(parsed.get("reason") or "").strip().lower()
        if len(reason) > 80:
            reason = reason[:80]

        # Compute a direction hint from the threat bbox (if present) so the
        # client can bias the vignette gradient toward the edge the danger
        # is coming from. Empty / degenerate boxes → direction None → the
        # vignette stays symmetric.
        direction = None
        threat_cx = None
        threat_cy = None
        box = parsed.get("threat_box")
        if isinstance(box, (list, tuple)) and len(box) >= 4:
            try:
                ymin, xmin, ymax, xmax = (float(box[0]), float(box[1]),
                                          float(box[2]), float(box[3]))
                ymin, ymax = sorted((ymin / 1000.0, ymax / 1000.0))
                xmin, xmax = sorted((xmin / 1000.0, xmax / 1000.0))
                cx = max(0.0, min(1.0, (xmin + xmax) / 2.0))
                cy = max(0.0, min(1.0, (ymin + ymax) / 2.0))
                w = max(0.0, min(1.0, xmax - xmin))
                h = max(0.0, min(1.0, ymax - ymin))
                if w > 0.001 and h > 0.001 and not (w >= 0.98 and h >= 0.98):
                    threat_cx = round(cx, 3)
                    threat_cy = round(cy, 3)
                    # Cardinal direction from the box center, with a dead
                    # zone in the middle so a threat dead-center reads as
                    # "center" (symmetric vignette) rather than randomly
                    # snapping to a side.
                    dx = cx - 0.5
                    dy = cy - 0.5
                    if abs(dx) < 0.15 and abs(dy) < 0.15:
                        direction = "center"
                    elif abs(dx) >= abs(dy):
                        direction = "right" if dx > 0 else "left"
                    else:
                        direction = "bottom" if dy > 0 else "top"
            except Exception:
                direction = None
                threat_cx = None
                threat_cy = None

        return {
            "level": level,
            "reason": reason,
            "direction": direction,
            "threat_cx": threat_cx,
            "threat_cy": threat_cy,
        }
    except requests.exceptions.HTTPError as e:
        safe_e = str(e).encode("ascii", "replace").decode("ascii")
        log_error(f"[DANGER] Gemini API HTTP error: {safe_e}")
        return safe
    except Exception as e:
        safe_e = str(e).encode("ascii", "replace").decode("ascii")
        log_error(f"[DANGER] Failed: {safe_e}")
        return safe


def _appraise_photo(image_path: str, max_items: int = 6) -> dict:
    """Appraise a photograph the player captured — the reward loop's "feedback".

    Where ``_detect_objects`` locates things for live SCAN tags, this reads a
    single captured crop like an evidence analyst: it names the notable things
    in frame, rates how *interesting/telling* each one is, and gives a terse
    reason it matters — plus a one-line caption and an overall mood word. The
    standalone UI turns this into a "receipt" that prints item-by-item, each
    line scoring points toward the run's EVIDENCE total.

    Returns a dict (never raises):
        {
          "items": [{"label": str, "interest": 1..5, "note": "<=6 words"}],
          "caption": str,   # one evocative line about the shot
          "mood": str,      # single lowercase word, may be ""
        }
    On any failure / disabled vision it returns {"items": [], "caption": "",
    "mood": ""} so the client can still render a graceful "undeveloped" receipt.
    """
    import base64
    import json as _json
    import re as _re
    import requests

    empty = {"items": [], "caption": "", "mood": ""}
    if not LLM_ENABLED or not VISION_ENABLED or not GEMINI_API_KEY:
        return empty

    try:
        full_path = _resolve_image_path(image_path)
        if not full_path or not os.path.exists(full_path):
            return empty

        from pathlib import Path
        full_path_obj = Path(full_path)
        small_path = full_path_obj.parent / full_path_obj.name.replace(".png", "_small.png")
        use_path = small_path if small_path.exists() else full_path_obj

        with open(use_path, "rb") as f:
            image_bytes = f.read()
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        mime_type = "image/png"
        if str(use_path).lower().endswith((".jpg", ".jpeg")):
            mime_type = "image/jpeg"

        appraise_prompt = (
            "You are an evidence analyst reviewing a single photograph a field "
            "investigator just captured. Identify the notable, concrete things "
            "in the frame (objects, tools, figures, creatures, hazards, exits, "
            "clues). For each, rate how interesting/telling it is and say WHY in "
            "a few words. "
            f"Return AT MOST {max_items} items, most interesting first. "
            "Respond with ONLY a JSON object, no prose, no code fences:\n"
            '{"items": [{"label": "<1-3 word noun, lowercase>", '
            '"interest": <integer 1-5>, "note": "<<=6 words on why it matters>"}], '
            '"caption": "<one evocative sentence about the shot>", '
            '"mood": "<single lowercase word for the vibe>"}. '
            "interest: 1 = mundane, 5 = striking/rare/story-critical. "
            "Prefer specific labels over vague ones. Skip generic background "
            "(sky, ground, wall) unless genuinely notable."
        )

        api_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"
        headers = {
            "x-goog-api-key": GEMINI_API_KEY,
            "Content-Type": "application/json",
        }
        payload = {
            "contents": [{
                "parts": [
                    {"inlineData": {"mimeType": mime_type, "data": image_b64}},
                    {"text": appraise_prompt},
                ]
            }],
            "generationConfig": {
                "thinkingConfig": {"thinkingBudget": 0},
                "temperature": 0.5,
                "maxOutputTokens": 700,
                "responseMimeType": "application/json",
            },
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ],
        }

        response = requests.post(api_url, headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        result = response.json()

        candidates = result.get("candidates") or []
        if not candidates:
            return empty
        parts = (candidates[0].get("content") or {}).get("parts") or []
        full_text = "".join(p.get("text", "") for p in parts).strip()
        if not full_text:
            return empty

        cleaned = _re.sub(r"^```(?:json)?|```$", "", full_text.strip(), flags=_re.MULTILINE).strip()
        try:
            parsed = _json.loads(cleaned)
        except Exception:
            m = _re.search(r"\{.*\}", cleaned, _re.DOTALL)
            if not m:
                return empty
            try:
                parsed = _json.loads(m.group(0))
            except Exception:
                return empty

        if not isinstance(parsed, dict):
            return empty

        raw_items = parsed.get("items")
        if not isinstance(raw_items, list):
            raw_items = []
        items = []
        seen = set()
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            label = str(entry.get("label") or "").strip().lower()
            if not label:
                continue
            key = label[:24]
            if key in seen:
                continue
            seen.add(key)
            try:
                interest = int(round(float(entry.get("interest", 2))))
            except Exception:
                interest = 2
            interest = max(1, min(5, interest))
            note = str(entry.get("note") or "").strip()[:80]
            items.append({"label": label[:40], "interest": interest, "note": note})
            if len(items) >= max_items:
                break

        caption = str(parsed.get("caption") or "").strip()[:160]
        mood = str(parsed.get("mood") or "").strip().lower()[:24]
        return {"items": items, "caption": caption, "mood": mood}
    except requests.exceptions.HTTPError as e:
        safe_e = str(e).encode("ascii", "replace").decode("ascii")
        print(f"[APPRAISE ERROR] Gemini API HTTP error: {safe_e}")
        return empty
    except Exception as e:
        safe_e = str(e).encode("ascii", "replace").decode("ascii")
        print(f"[APPRAISE ERROR] Failed to appraise photo: {safe_e}")
        return empty

def _fallback_directive(st: dict) -> dict:
    """A deterministic "current lead" derived purely from world state — no LLM.

    Used both when the model is unavailable and as the always-safe default the
    generative path upgrades. Keeps the objectives tracker's LEAD reading well
    even with no network / no key.
    """
    phase = str((st or {}).get("current_phase", "normal") or "normal").lower()
    recent = (st or {}).get("recent_events") or []
    seen = (st or {}).get("seen_elements") or []
    subject = ""
    if seen:
        subject = str(seen[-1]).strip()
    templates = {
        "normal": ("Survey the area",
                   "Read the scene and document your first real subject."),
        "escalating": ("Keep the camera close",
                       "Something is shifting — capture it before it turns."),
        "critical": ("Get what you can and move",
                     "It's turning against you — shoot the evidence and stay alive."),
    }
    lead, detail = templates.get(phase, templates["normal"])
    # If the world has surfaced a named element, make the lead concrete.
    if subject and phase != "critical":
        lead = f"Document the {subject}"[:48]
        detail = "It matters to the case — get it on the record."
    return {"lead": lead, "detail": detail, "generated": False}


def generate_directive(session_id: str = "default") -> dict:
    """Generate the objectives tracker's evolving "current lead" — a short,
    in-world investigative directive grounded in the live world state.

    This is the GENERATIVE spine of the objectives system: it reads the
    evolving world (premise, recent beats, phase, discovered elements) and asks
    the model for the player's most pressing current goal, phrased like a AAA
    objective line. It NEVER raises and always returns a usable directive —
    degrading to :func:`_fallback_directive` when the model is unavailable.

    Returns: {"lead": str, "detail": str, "generated": bool}
    """
    import json as _json
    import re as _re
    import requests

    # Always read the requested session from disk (not the shared `state`
    # global) so a concurrent different-session request can't make this
    # objective describe the wrong player's world.
    try:
        st = _load_state(session_id)
    except Exception:
        st = state or {}

    fb = _fallback_directive(st or {})
    if not LLM_ENABLED or not GEMINI_API_KEY:
        return fb

    try:
        world_prompt = str((st or {}).get("world_prompt", "") or "")[:900]
        recent = (st or {}).get("recent_events") or []
        recent_txt = " | ".join(str(r) for r in recent[-3:])[:500]
        seen = (st or {}).get("seen_elements") or []
        seen_txt = ", ".join(str(s) for s in seen[-8:])[:300]
        phase = str((st or {}).get("current_phase", "normal") or "normal")
        threat = (st or {}).get("threat_level", 0)
        tod = str((st or {}).get("time_of_day", "") or "")[:80]

        prompt = (
            "You are the objective director for a first-person investigative "
            "documentary game. The player explores a strange, evolving world and "
            "photographs subjects to build a case file. Given the CURRENT world "
            "state, write the player's single most pressing CURRENT OBJECTIVE — "
            "the 'lead' they should pursue right now. It must fit the fiction and "
            "point toward action (explore, reach, document, escape, confront).\n\n"
            f"PREMISE / WORLD: {world_prompt}\n"
            f"RECENT BEATS: {recent_txt}\n"
            f"KNOWN ELEMENTS: {seen_txt}\n"
            f"PHASE: {phase} (threat {threat}); TIME: {tod}\n\n"
            "Respond with ONLY a JSON object, no prose, no code fences:\n"
            '{"lead": "<imperative objective, 2-7 words, Title Case, no period>", '
            '"detail": "<one grounded sentence of context, <= 16 words>"}'
        )

        api_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"
        headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "thinkingConfig": {"thinkingBudget": 0},
                "temperature": 0.8,
                "maxOutputTokens": 200,
                "responseMimeType": "application/json",
            },
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ],
        }
        response = requests.post(api_url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        result = response.json()
        candidates = result.get("candidates") or []
        if not candidates:
            return fb
        parts = (candidates[0].get("content") or {}).get("parts") or []
        full_text = "".join(p.get("text", "") for p in parts).strip()
        if not full_text:
            return fb
        cleaned = _re.sub(r"^```(?:json)?|```$", "", full_text.strip(), flags=_re.MULTILINE).strip()
        try:
            parsed = _json.loads(cleaned)
        except Exception:
            m = _re.search(r"\{.*\}", cleaned, _re.DOTALL)
            parsed = _json.loads(m.group(0)) if m else None
        if not isinstance(parsed, dict):
            return fb
        lead = str(parsed.get("lead") or "").strip().strip('".').strip()[:48]
        detail = str(parsed.get("detail") or "").strip()[:160]
        if not lead:
            return fb
        return {"lead": lead, "detail": detail, "generated": True}
    except requests.exceptions.HTTPError as e:
        safe_e = str(e).encode("ascii", "replace").decode("ascii")
        print(f"[DIRECTIVE ERROR] Gemini API HTTP error: {safe_e}")
        return fb
    except Exception as e:
        safe_e = str(e).encode("ascii", "replace").decode("ascii")
        print(f"[DIRECTIVE ERROR] Failed to generate directive: {safe_e}")
        return fb


# ───────── world report (with vision‑desc) ─────────────────────────────────
def _world_report() -> str:
    base = PROMPTS["field_notes_format"].format(
        context=state["world_prompt"],
        last_choice=state["last_choice"]
    )
    # Vision model disabled: do not use _vision_describe or add image description
    # Alternate tone based on phase and beat
    phase = state.get("current_phase", "normal")
    beat = state.get("current_beat", None)
    if phase == "critical":
        tone = "suspenseful"
    elif phase == "escalating":
        tone = "mysterious"
    else:
        tone = "reflective"
    prompt = (
        f"{base}\n\n"
        f"[{tone.upper()} TONE] {PROMPTS['situation_report_prompt']}"
    )
    # Don't use lore - this is just a summary of current state
    return _ask(prompt, tokens=60, use_lore=False)

# ───────── dispatch helpers ────────────────────────────────────────────────
def summarize_world_prompt_for_image(world_prompt: str) -> str:
    """Summarize the world prompt to 1-2 sentences for image generation.
    CRITICAL: Avoid graphic/violent/NSFW terms in the summary to prevent image safety blocks.
    """
    prompt = (
        "Summarize the following world context in 1-2 vivid, scene-specific sentences for an image generation model. "
        "Focus only on details relevant to the current visual environment. Omit backstory and generalities. "
        "CRITICAL: Avoid using graphic or violent words like 'blood', 'gore', 'mutilated', 'viscera', etc. "
        "Use clinical or atmospheric equivalents if needed.\n\nWORLD PROMPT: " + world_prompt
    )
    # Don't use lore - just summarizing existing text
    return _ask(prompt, model="gemini", temp=1.0, tokens=48, use_lore=False)


def _generate_dispatch(choice: str, state: dict, prev_state: dict = None) -> dict:
    """Generate dispatch with death detection. Returns dict with 'dispatch' and 'player_alive' keys."""
    try:
        # Get previous vision analysis AND image for spatial consistency
        prev_vision = ""
        prev_image_path = None
        if history and len(history) > 0:
            last_entry = history[-1]
            if last_entry.get("vision_analysis"):
                prev_vision = last_entry["vision_analysis"][:300]
            if last_entry.get("image"):
                prev_image_path = last_entry["image"]  # e.g. "/images/123_file.png"
        
        spatial_context = ""
        if prev_vision:
            spatial_context = f"\n\nCURRENT VISUAL SCENE (MUST STAY CONSISTENT): {prev_vision}\nDo NOT change locations unless the choice explicitly moves through a door, entrance, or exit. Stay in the same environment."
        
        # System instructions + user prompt combined for Gemini
        prompt = (
            f"{PROMPTS['action_consequence_instructions']}\n\n"
            f"PLAYER CHOICE: '{choice}'\n"
            f"WORLD CONTEXT: {state['world_prompt']}\n"
            f"PREVIOUS: {prev_state['world_prompt'] if prev_state else ''}"
            f"{spatial_context}\n\n"
            "Describe what you do and what immediately happens as a result."
        )
        # Don't use lore - dispatch is immediate action/consequence, not world building
        result = _ask(prompt, model="gemini", temp=1.0, tokens=250, image_path=prev_image_path, use_lore=False)
        
        # Try to parse as JSON first (new format)
        import json
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict) and "dispatch" in parsed:
                dispatch_text = parsed.get("dispatch", "").strip()
                player_alive = parsed.get("player_alive", True)
                
                # Hard cap at 400 characters
                if len(dispatch_text) > 400:
                    dispatch_text = dispatch_text[:385] + "...(truncated)"
                
                return {"dispatch": dispatch_text, "player_alive": player_alive}
        except json.JSONDecodeError:
            pass  # Not JSON, treat as plain text (backward compatibility)
        
        # FALLBACK: Plain text (old format) - assume player alive
        # If result is just '[' or '[]' or empty, fallback immediately
        if result.strip() in {"[", "[]", ""}:
            return {"dispatch": "You make a tense move in the chaos.", "player_alive": True}
        
        # Sanitize: if result looks like a list, extract the text
        if result.startswith("[") or result.startswith("-") or result.startswith("\""):
            try:
                arr = json.loads(result)
                if isinstance(arr, list):
                    for item in arr:
                        if isinstance(item, str) and item.strip():
                            result = item.strip()
                            break
            except Exception:
                lines = [l.strip('-*[] ",') for l in result.splitlines() if l.strip()]
                if not lines or all(l in {"[", "[]", ""} for l in lines):
                    return {"dispatch": "You make a tense move in the chaos.", "player_alive": True}
                result = " ".join(lines)
        
        # Hard cap at 400 characters
        if len(result) > 400:
            result = result[:385] + "...(truncated)"
        
        return {"dispatch": result, "player_alive": True}
        
    except Exception as e:
        log_error(f"[DISPATCH] LLM error: {e}")
        return {"dispatch": "You make a tense move in the chaos.", "player_alive": True}

def _generate_caption(dispatch: str, mode: str, is_first_frame: bool = False) -> str:
    # Simplified caption generation - not used in StoryGen version
    return f"{dispatch} ({mode})"

# ───────── imaging helpers ─────────────────────────────────────────────────
def _slug(s: str) -> str:
    # Ensure get_next_feed_item_id is available if slug is empty.
    # This is a minor case, get_next_feed_item_id has its own lock.
    return "".join(c for c in s.lower().replace(" ","_") if c.isalnum() or c=="_")[:48] or f"auto_slug_{get_next_feed_item_id()}"

def _save_img(b64: str, caption: str, session_id: str = 'default') -> str:
    """Save image to session-specific directory"""
    img_dir = _get_image_dir(session_id)
    path = img_dir / f"{hash(caption) & 0xFFFFFFFF}_{_slug(caption)}.png"
    path.write_bytes(base64.b64decode(b64))
    # Return relative path from session root
    return f"images/{path.name}"

# _vision_is_inside removed - was expensive and never used in StoryGen
# _generate_burn_in removed - was never called and caused timecode overlays

# Enclosed / distinct spaces a player can move INTO or THROUGH. Crossing into
# one of these from a different environment is a genuine scene change (a "hard
# cut") — a fresh composition — rather than a small step within the current view.
_TRANSITION_SPACE_NOUNS = [
    'shaft', 'vent', 'ventilation', 'duct', 'ductwork', 'tunnel', 'crawlspace',
    'crawl space', 'conduit', 'hatch', 'opening', 'corridor', 'hallway',
    'stairwell', 'stairway', 'staircase', 'chamber', 'room', 'building',
    'facility', 'structure', 'doorway', 'gateway', 'gate', 'threshold',
    'archway', 'entrance', 'gap', 'hole', 'cave', 'cavern', 'basement',
    'cellar', 'elevator', 'airlock', 'passage', 'passageway', 'grate', 'grating',
    'manhole', 'sewer', 'pit', 'trench', 'alcove', 'vault', 'den', 'nest',
    'window', 'maw', 'pipe', 'pipeline', 'lab', 'laboratory', 'silo', 'bunker',
    'compound', 'courtyard', 'clearing', 'ravine', 'culvert', 'shed', 'garage',
    'warehouse', 'reactor', 'core', 'atrium', 'lobby', 'vestibule', 'antechamber',
]

# Verbs that denote the player physically relocating their whole body/camera INTO
# or THROUGH a space (as opposed to observational verbs like "peer/look/reach
# into", which must NOT trigger a scene change).
_TRANSITION_MOVE_VERBS = [
    'enter', 'climb', 'clamber', 'crawl', 'scramble', 'squeeze', 'wriggle',
    'duck', 'drop', 'descend', 'ascend', 'wade', 'slip', 'slide', 'dive',
    'vault', 'step', 'push', 'pass', 'move', 'go', 'head', 'venture', 'sneak',
    'creep', 'lower yourself', 'haul yourself', 'pull yourself', 'force your way',
    'navigate', 'plunge', 'burrow', 'thrust yourself', 'hoist yourself',
    'make your way', 'break', 'stumble', 'run', 'sprint', 'walk', 'march',
]

# Prepositions that, paired with a move verb and a space noun, indicate crossing
# into a new area.
_TRANSITION_PREPS = [
    'into', 'through', 'inside', 'in through', 'out through', 'down into',
    'up into', 'in to', 'onto',
]

# Words signalling continuation WITHIN the current space rather than crossing into
# a new one ("navigate DEEPER into the crawlspace" = same crawlspace, keep img2img
# continuity; "scramble into the ventilation shaft" = new space, hard cut).
_TRANSITION_CONTINUATION_WORDS = [
    'deeper', 'further', 'farther', 'onward', 'onwards', 'along', 'ahead within',
]


def is_hard_transition(choice: str, dispatch: str) -> bool:
    """
    Detect if the player's CHOICE moves them into a genuinely new area — a
    location change that warrants a "hard cut" (fresh image composition) rather
    than img2img continuity off the previous frame.

    Only the player's CHOICE is inspected, never the LLM narrative ``dispatch``:
    narrative prose is full of dramatic language ("the floor falls away", "you
    crumple") that would cause false location changes and shatter continuity.

    Two detectors, tuned to fire on real scene changes while staying quiet on
    small in-place moves so the world still feels temporally continuous:

      1. Unambiguous location phrases (enter/exit/leave/through the door, …).
      2. A movement verb + into/through + a distinct/enclosed space noun
         (e.g. "scramble into the ventilation shaft", "crawl through the duct").
         Suppressed when a continuation word ("deeper", "further", …) is present,
         since that means moving WITHIN the current space, not into a new one.
    """
    if not choice:
        return False

    choice_lower = choice.lower()

    # ── 1. Unambiguous location-change phrases ────────────────────────────────
    location_keywords = [
        'enter ', 'step inside', 'go inside', 'walk inside', 'move inside',
        'step outdoors', 'go outdoors', 'walk outdoors', 'move outdoors',
        'step outside', 'head outside', 'get outside',
        'exit ', 'leave ', 'open door', 'open the door', 'through the door',
        'through the doorway', 'through the gate', 'through the hatch',
        'cross into', 'cross over', 'cross through', 'cross the threshold',
        'red biome', 'new location', 'different room', 'different area',
        'next room', 'another room', 'the next area',
        'teleport', 'wake up in', 'dragged to', 'carried to', 'transported to',
        'emerge into', 'emerge from', 'emerge onto',
    ]
    reason = ""
    if any(k in choice_lower for k in location_keywords):
        reason = "explicit location phrase"

    # ── 2. move-verb + preposition + enclosed-space noun ──────────────────────
    if not reason:
        is_continuation = any(w in choice_lower for w in _TRANSITION_CONTINUATION_WORDS)
        if not is_continuation:
            has_verb = any(v in choice_lower for v in _TRANSITION_MOVE_VERBS)
            if has_verb:
                for prep in _TRANSITION_PREPS:
                    marker = f" {prep} "
                    if marker not in choice_lower:
                        continue
                    # Only the text AFTER the preposition should name the space we
                    # are moving into, so "reach into your PACK" style objects
                    # before the noun don't cause false hits.
                    tail = choice_lower.split(marker, 1)[1]
                    if any(re.search(rf"\b{re.escape(n)}\b", tail) for n in _TRANSITION_SPACE_NOUNS):
                        reason = f"move-verb + '{prep}' + new space"
                        break

    if reason:
        safe_choice = choice.encode('ascii', 'replace').decode('ascii')
        print(f"[HARD TRANSITION] Detected in choice ({reason}): '{safe_choice}' - new location (fresh composition, keep lighting/aesthetic)")
        return True

    return False

def get_last_movement_type() -> Optional[str]:
    """Get the last detected movement type for display purposes."""
    return _last_movement_type

def _detect_movement_type(player_choice: str) -> str:
    """
    Use LLM to intelligently classify action type.
    Returns: 'forward_movement', 'stationary', 'exploration'
    """
    # Keyword-based classification for common patterns (faster, more reliable)
    choice_lower = player_choice.lower()
    
    # Stationary actions (observing, no camera movement)
    stationary_keywords = ['photograph', 'examine', 'inspect', 'check', 'observe', 'watch', 'study', 'crouch in place', 'stand still']
    if any(keyword in choice_lower for keyword in stationary_keywords):
        return 'stationary'
    
    # Exploration actions (subtle movement, turning, panning)
    exploration_keywords = ['turn', 'look around', 'scan', 'survey', 'glance', 'peer', 'look back', 'turn around', 'rotate', 'pan', 'back away', 'step back', 'retreat']
    if any(keyword in choice_lower for keyword in exploration_keywords):
        return 'exploration'
    
    # Forward movement actions (significant spatial progression)
    forward_keywords = ['sprint', 'dash', 'run toward', 'charge', 'advance', 'approach', 'move forward', 'walk toward', 'climb', 'enter', 'cross', 'vault', 'scramble', 'rush']
    if any(keyword in choice_lower for keyword in forward_keywords):
        return 'forward_movement'
    
    # Fallback to LLM if no keywords match
    detection_prompt = (
        f"Classify this player action into ONE category:\n\n"
        f"ACTION: '{player_choice}'\n\n"
        f"CATEGORIES:\n"
        f"- FORWARD_MOVEMENT: Camera moves significantly FORWARD/CLOSER to destination (advance, sprint, climb, enter, cross, approach buildings)\n"
        f"- EXPLORATION: Camera moves slightly (look around, scan, pan, turn, back away, retreat) but no major spatial progression\n"
        f"- STATIONARY: Camera stays in exact same position (examine object, photograph, crouch in place)\n\n"
        f"CRITICAL: 'Turn back', 'retreat', 'back away' are EXPLORATION (slight movement), NOT forward_movement.\n\n"
        f"Return ONLY one word: FORWARD_MOVEMENT, EXPLORATION, or STATIONARY"
    )
    
    try:
        result = _ask(detection_prompt, temp=0.2, tokens=5, use_lore=False).strip().upper()
        if 'FORWARD' in result:
            return 'forward_movement'
        elif 'EXPLORATION' in result:
            return 'exploration'
        else:
            return 'stationary'
    except Exception as e:
        safe_e = str(e).encode('ascii', 'replace').decode('ascii')
        print(f"[MOVEMENT DETECTION] Error: {safe_e}, defaulting to exploration")
        return 'exploration'  # Default to some movement to keep things interesting

def build_image_prompt(
    player_choice: str = "",
    dispatch: str = "",
    narrative_dispatch: str = "",
    prev_vision_analysis: str = "",
    hard_transition: bool = False,
    is_timeout_penalty: bool = False,
    prev_spatial: str = "",
    prev_setting: str = "",
) -> str:
    """
    Build an image generation prompt with spatial-anchor continuity.

    ``dispatch`` — visual scene description of what the camera sees now
    (generated as ``visual_scene`` by the LLM, or falls back to narrative).

    ``narrative_dispatch`` — the narrative consequence text the player reads
    (what they feel/experience). Provided as context alongside the visual scene.

    ``prev_spatial`` — the directional compass extracted from the previous
    turn's vision analysis (ahead/left/right/ground/height).  When present it
    is injected as a hard spatial constraint so the model cannot silently move
    the camera to a different environment.

    ``prev_setting`` — indoor/outdoor environment type from previous vision
    analysis; used to enforce environment type consistency.
    """
    # ── TIMEOUT PENALTIES: identical camera, only environment reacts ──────────
    if is_timeout_penalty:
        base = (
            f"Result: {dispatch}\n\n"
            f"CRITICAL: TIMEOUT PENALTY — ZERO camera movement.\n"
            f"Show the EXACT SAME ANGLE and LOCATION as before.\n"
            f"ONLY environmental reactions (dust, debris, creature, attack, danger).\n"
            f"If outdoor, STAY outdoors. If indoor, STAY indoors. NO teleportation."
        )
        if prev_spatial:
            base += f"\n\n🗺️ LOCKED POSITION: {prev_spatial}"
        return base

    # ── INTRO FRAME: pure establishing shot ───────────────────────────────────
    if player_choice.lower().strip() == "intro":
        prompt = f"Opening scene: {dispatch}"
        print(f"\n{'='*60}")
        print(f"[INTRO MODE] Creating establishing shot")
        print(f"{'='*60}\n", flush=True)
        return prompt

    # ── Detect movement type ──────────────────────────────────────────────────
    global _last_movement_type
    movement_type = _detect_movement_type(player_choice)
    _last_movement_type = movement_type

    type_label = {
        'forward_movement': 'FORWARD MOVEMENT',
        'exploration':      'EXPLORATION',
        'stationary':       'STATIONARY',
    }
    print(f"\n{'='*60}")
    safe_choice = player_choice.encode('ascii', 'replace').decode('ascii')
    print(f"[MOVEMENT DETECTION] '{safe_choice}'")
    print(f"  -> {type_label.get(movement_type, movement_type.upper())}")
    print(f"{'='*60}\n", flush=True)

    # ── Base prompt ───────────────────────────────────────────────────────────
    # Three cases:
    #   A. LLM produced a distinct visual_scene → VISUAL-DOMINANT prompt, narrative
    #      compressed to a short context clause so it cannot dominate the image.
    #   B. No visual_scene but we DO have a prior vision_analysis → reuse the prior
    #      camera position as the spatial scaffold, then describe what changed.
    #   C. No visual scene and no prior vision → last-resort original style.
    has_visual_scene = (
        narrative_dispatch and
        narrative_dispatch.strip() and
        narrative_dispatch.strip() != dispatch.strip()
    )
    if has_visual_scene:
        # Compress narrative to a single short clause. The image model is biased
        # toward describing whatever it reads at length, so long narrative text
        # (which is intentionally non-visual / kinesthetic) was the original drift
        # cause. Keep it to ~120 chars of context only.
        narrative_brief = narrative_dispatch.strip().replace("\n", " ")
        if len(narrative_brief) > 120:
            narrative_brief = narrative_brief[:117] + "..."
        prompt = (
            f"FIRST-PERSON CAMERA VIEW — render exactly this scene:\n"
            f"{dispatch}\n\n"
            f"Action just performed: {player_choice}\n"
            f"(Brief narrative context, do not over-illustrate: {narrative_brief})"
        )
    elif prev_vision_analysis:
        # Visual scaffold from the previous frame's actual visual analysis. This is
        # WAY more visually grounded than narrative text and prevents drift even
        # when the LLM forgets to emit `visual_scene`.
        scaffold = prev_vision_analysis.strip().replace("\n", " ")
        if len(scaffold) > 240:
            scaffold = scaffold[:237] + "..."
        prompt = (
            f"FIRST-PERSON CAMERA VIEW — continuing directly from the previous frame.\n"
            f"Previous frame visual state: {scaffold}\n"
            f"Action just performed: {player_choice}\n"
            f"Now render the SAME camera position evolved to show the result of that "
            f"action. Keep ground type, environment type, lighting, and visible "
            f"landmarks identical unless the action itself changes them."
        )
    else:
        prompt = f"Action taken: {player_choice}. Result: {dispatch}"

    # ── Spatial anchor (highest priority constraint) ──────────────────────────
    # Injected BEFORE movement-type guidance so the model reads it first.
    if prev_spatial and not hard_transition:
        setting_note = (
            f"  Environment type: {prev_setting}." if prev_setting else ""
        )
        prompt = (
            f"🗺️ SPATIAL ANCHOR — YOU ARE EXACTLY HERE:\n"
            f"{prev_spatial}{setting_note}\n"
            f"The camera CANNOT leave this spatial position unless the action"
            f" explicitly moves through a door, entrance, or large gap.\n"
            f"Visible landmarks from this position MUST remain consistent.\n\n"
        ) + prompt

    # ── Movement-type guidance ────────────────────────────────────────────────
    if prev_vision_analysis:
        if hard_transition:
            prompt = (
                f"{prompt}\n\nLOCATION CHANGE: Maintain same lighting, time of day, "
                f"and VHS aesthetic as before, but show the new environment."
            )
        elif movement_type == 'forward_movement':
            prompt = (
                f"{prompt}\n\n"
                f"FORWARD MOVEMENT: Camera advances naturally.\n"
                f"- Objects ahead are closer and larger in frame\n"
                f"- Camera moved 5–15 ft forward (walking), 15–30 ft (sprint)\n"
                f"- Smooth perspective shift; horizon line stays stable\n"
                f"- Ground/foreground elements reflect new position\n"
                f"Scene context: {prev_vision_analysis[:150]}"
            )
        elif movement_type == 'exploration':
            prompt = (
                f"{prompt}\n\n"
                f"SUBTLE CAMERA SHIFT: Small pan, tilt, or drift — not a new location.\n"
                f"- Roughly same position, slightly different angle\n"
                f"- Environmental elements shift naturally with camera movement\n"
                f"Scene context: {prev_vision_analysis[:150]}"
            )
        else:
            # STATIONARY
            prompt = (
                f"{prompt} Same camera position. "
                f"Only environmental/lighting changes: {prev_vision_analysis[:200]}"
            )

    return prompt

# Stable "scene bible" anchor for the realtime world model (Reactor Happy Oyster).
# Happy Oyster turns a PARAGRAPH OF TEXT into a navigable place you then travel
# through in first person, so every prompt describes a coherent WORLD (space,
# lighting, mood) in a consistent style. We keep this constant across turns so
# the built world maintains one look while the scene text evolves. Style-only
# (NO location) so location comes from the per-turn scene description. Because
# the player navigates with held movement/look (not per-frame prompts), the
# anchor commits to a first-person, walk-through vantage.
REALTIME_STYLE_ANCHOR = os.getenv(
    "REALTIME_STYLE_ANCHOR",
    "A navigable first-person world you can walk through, shot as 1993 analog VHS "
    "home-video footage from a handheld camcorder, heavy film grain and chromatic "
    "aberration, slightly desaturated, low-light dread and horror atmosphere. "
    "Eye-level walking vantage with a medium-wide field of view",
)

# Conversation Moment portraits use a DIFFERENT lens language than the handheld
# camcorder world view — a shallow-DoF cinematic medium shot — so the cut into
# dialogue reads as a register change (Mass Effect / Persona style), not a
# different game. Era/palette stay continuous with the 1993 analog-horror world.
CONVERSATION_PORTRAIT_STYLE_ANCHOR = os.getenv(
    "CONVERSATION_PORTRAIT_STYLE_ANCHOR",
    "stylish cinematic medium shot, 35mm film still, shallow depth of field, "
    "dramatic rim lighting, soft bokeh background, analog-horror 1993 muted palette, "
    "subtle film grain, intimate character portrait from mid-torso up",
)

# Soft budget: how many conversation portraits a single session may mint.
# Cached hits do not count. Prevents runaway cost if a player re-opens TALK a lot.
CONVERSATION_PORTRAIT_BUDGET = int(os.getenv("CONVERSATION_PORTRAIT_BUDGET", "12"))

# In-memory portrait cache: (session_id, subject_label, scene_hash) -> web url.
# Cleared implicitly when the process restarts; disk files remain in session images/.
_PORTRAIT_CACHE: dict = {}
_PORTRAIT_CACHE_LOCK = threading.Lock()
_PORTRAIT_SPEND: dict = {}  # session_id -> count of generations this session

# Camp establishing-shot cache: (session_id, sorted_labels, jeep_hash) -> web url.
# Revisiting camp with the same roster reuses the shot (no re-generation).
_CAMP_CACHE: dict = {}
_CAMP_CACHE_LOCK = threading.Lock()

# Fixed seat slots around the fire, keyed by attendee count. Approximate
# tap-target positions (x_pct / y_pct of the establishing shot) — no vision.
_CAMP_SEATS = {
    1: [{"x_pct": 50, "y_pct": 62}],
    2: [{"x_pct": 32, "y_pct": 60}, {"x_pct": 68, "y_pct": 60}],
    3: [{"x_pct": 28, "y_pct": 62}, {"x_pct": 50, "y_pct": 58}, {"x_pct": 72, "y_pct": 62}],
    4: [
        {"x_pct": 22, "y_pct": 64}, {"x_pct": 40, "y_pct": 58},
        {"x_pct": 60, "y_pct": 58}, {"x_pct": 78, "y_pct": 64},
    ],
    5: [
        {"x_pct": 18, "y_pct": 66}, {"x_pct": 34, "y_pct": 58},
        {"x_pct": 50, "y_pct": 55}, {"x_pct": 66, "y_pct": 58},
        {"x_pct": 82, "y_pct": 66},
    ],
}

_JEEP_PROP_PROMPT = (
    "Hero plate of a dusty bright-red 1990s Jeep Cherokee / Wrangler, parked alone "
    "in empty high-desert scrub at dusk. Three-quarter view from the front-left, "
    "vehicle fills most of the frame, weathered paint, mud-caked tires, spare tire "
    "on the back, chrome details dulled by dust. No people, no other vehicles, no "
    "text, photoreal cinematic still, VHS analog grain, muted 1993 palette."
)

# Bump when camp plate grammar changes so stale jeep-less caches regenerate.
_CAMP_CACHE_VERSION = "v3-jeep4x3"


def realtime_action_beat(choice: str = "") -> str:
    """The single 'one new element per prompt' motion clause for an action.

    Kept in sync with the client (standalone.js) so an action can be injected
    into the live video instantly — against the current scene — before the full
    turn resolves.
    """
    c = (choice or "").strip().rstrip(".")
    if not c or c.lower() in ("intro", "initialize simulation"):
        return ""
    return f"Motion: the view shifts as you {c[0].lower() + c[1:]}."


def build_realtime_base(visual_scene: str = "", narrative: str = "") -> str:
    """The stable part of a realtime prompt: style/camera anchor + physical scene
    description (no action beat). The client recombines this with the live action
    for instant, seamless steering."""
    scene = (visual_scene or narrative or "").strip().replace("\n", " ")
    scene = _sanitize_for_image_generation(scene)
    # Keep it focused; overly long prompts dilute the signal for the video model.
    if len(scene) > 600:
        scene = scene[:597].rstrip() + "..."
    parts = [REALTIME_STYLE_ANCHOR.rstrip(". ") + "."]
    if scene:
        parts.append(scene)
    return " ".join(parts)


def build_realtime_prompt(visual_scene: str = "", narrative: str = "", choice: str = "") -> str:
    """Compose a clean, natural-language prompt for the realtime world model.

    This is deliberately NOT the still-image diffusion prompt (which is packed
    with model-specific control text — spatial anchors, camera-distance math,
    anti-border/anti-person rules, img2img continuity clauses, world-state dumps).
    Feeding that to a video world model produces incoherent output. Instead we
    follow Happy Oyster's prompt-to-world guidance: a consistent style/vantage
    anchor + a physical description of the PLACE (which already covers
    near/mid/far + lighting) + one action beat. Everything is sanitized the same
    way as the image prompt so we don't regress on content filtering, and the
    whole prompt stays well under Happy Oyster's 2000-character world-prompt cap.
    """
    base = build_realtime_base(visual_scene, narrative)
    beat = realtime_action_beat(choice)
    return base + (" " + beat if beat else "")


def _build_vhs_prompt(base_prompt: str, use_img2img: bool = False) -> str:
    """
    Wrap any image generation prompt with VHS aesthetic instructions.
    This ensures Gemini and OpenAI both generate the same gritty analog look.
    
    Args:
        base_prompt: The scene description (what's happening)
        use_img2img: If True, use img2img instructions; else text-to-image
    
    Returns:
        Full prompt with VHS aesthetic styling
    """
    # Load the appropriate template from simulation_prompts.json
    if use_img2img:
        template_key = "gemini_image_to_image_instructions"
    else:
        template_key = "gemini_text_to_image_instructions"
    
    structured_prompt = PROMPTS[template_key].format(prompt=base_prompt)
    
    # Add CRITICAL anti-border instructions
    anti_border = "\n\nCRITICAL - ABSOLUTELY NO BORDERS OR FRAMES:\nThe image MUST fill the ENTIRE canvas edge-to-edge with ZERO borders, frames, or edges of any kind. NO black bars, NO white borders, NO photo frames, NO matting, NO letterboxing. The content fills 100% of the image area. This is RAW FOOTAGE, not a framed photograph."
    
    # Add CRITICAL anti-person instructions
    anti_person = "\n\nCRITICAL - ABSOLUTELY NO PERSON/PLAYER VISIBLE:\nThis is a FIXED CAMERA VIEW mounted to a wall or tripod. The camera operator does NOT exist in this image. NEVER show ANY part of a human body - no head, no back of head, no shoulders, no arms, no hands, no legs, no feet, no torso, no silhouette. Show ONLY the environment - walls, floor, ceiling, objects, debris, sky, ground. Think: security camera footage, dashboard cam, surveillance view - PURE environmental shot with ZERO human presence in frame."
    
    # Add CRITICAL anti-timecode/text instructions
    anti_timecode = (
        "\n\n CRITICAL - ABSOLUTELY NO TEXT OR TIMECODE OVERLAYS:\n"
        "This is RAW CAMERA FOOTAGE with NO on-screen displays.\n"
        "Do NOT add ANY text, numbers, letters, or symbols to the image.\n"
        "FORBIDDEN:\n"
        "ERROR: NO timecode (NO 'DEC 14 1993', NO '14:32:05', NO date/time stamps)\n"
        "ERROR: NO 'REC' indicator\n"
        "ERROR: NO 'PCC HISS' or any text overlays\n"
        "ERROR: NO battery indicators, recording icons, or UI elements\n"
        "ERROR: NO scanline overlays or grid patterns\n"
        "The image is PURE FOOTAGE with ZERO on-screen text of any kind."
    )
    
    full_prompt = structured_prompt + anti_border + anti_person + anti_timecode
    
    # Add negative prompt for extra safety (used by some providers)
    negative_prompt_text = PROMPTS.get("image_negative_prompt", "")
    if negative_prompt_text:
        full_prompt += f"\n\nNEGATIVE PROMPT (avoid these): {negative_prompt_text}"
    
    return full_prompt


def _gen_image(*args, session_id: str = 'default', **kwargs) -> Optional[tuple[str, str, Optional[str]]]:
    """
    Thin cost-tracking wrapper around `_gen_image_impl` (the real
    900+-line, multi-provider implementation below). Deliberately does NOT
    touch the implementation's internals — it just times the call, reads
    which provider/model was active, and records one usage event based on
    whether an image path came back. This keeps the (fragile, many-return-
    path) implementation untouched while still covering every provider
    branch (gemini/krea/fal/veo/openai) from a single call site.

    Image providers don't return granular token usage the way text APIs do
    (Krea/fal/Veo bill per-image or per-second at a flat published rate, not
    per-request usage metadata), so this records exactly one image (or, for
    Veo, one ~8s video clip) per call — see pricing.json / ADMIN_COST_ANALYTICS_DASHBOARD_PLAN.md §4.
    """
    provider = ai_provider_manager.get_image_provider()
    model_name = ai_provider_manager.get_image_model()
    t0 = time.time()
    result = _gen_image_impl(*args, session_id=session_id, **kwargs)
    latency_ms = int((time.time() - t0) * 1000)

    try:
        image_path, _prompt_used, video_path = result if result else (None, "", None)
        success = bool(image_path)
        if provider == "veo" and video_path:
            cost_tracker.record_usage(
                session_id, "video", provider, model_name, operation="gen_image",
                output_units=8.0, unit_type="seconds", latency_ms=latency_ms, success=success,
                error_message=None if success else "no_image_returned",
            )
        else:
            cost_tracker.record_usage(
                session_id, "image", provider, model_name, operation="gen_image",
                output_units=1.0, unit_type="images", latency_ms=latency_ms, success=success,
                error_message=None if success else "no_image_returned",
            )
    except Exception as _e:
        print(f"[COST TRACKER] _gen_image usage recording failed (non-fatal): {_e}", flush=True)

    return result


def _gen_image_impl(caption: str, mode: str, choice: str, previous_image_url: Optional[str] = None, previous_caption: Optional[str] = None, previous_mode: Optional[str] = None, strength: float = 0.25, image_description: str = "", time_of_day: Optional[str] = None, use_edit_mode: bool = False, frame_idx: int = 0, dispatch: str = "", world_prompt: str = "", hard_transition: bool = False, is_timeout_penalty: bool = False, session_id: str = 'default', history_ref: Optional[list] = None) -> Optional[tuple[str, str, Optional[str]]]:
    """Generate image and return (image_path, prompt_used, video_path).
    
    video_path is None for non-Veo providers or when video generation fails/disabled.
    
    time_of_day: If None, will use state['time_of_day'] for consistency
    session_id: Session ID for storing images in correct directory
    history_ref: The caller's session history to collect img2img reference frames
        from. Pass this (rather than relying on the module-global `history`) from
        any multi-user path — a concurrent different-session request can swap the
        global mirror out mid-render, which would otherwise feed one session's
        reference frames into another session's image. Falls back to the global
        for legacy single-session callers (the Discord bot) that don't pass it.
    """
    global _last_image_path
    # Resolve the history this render reads its img2img reference frames from.
    # Local variable so a concurrent set_active_session()/session swap can't
    # change it mid-function (the whole point of the history_ref parameter).
    _hist = history_ref if history_ref is not None else history
    import random
    if not (IMAGE_ENABLED and LLM_ENABLED):
        print("[IMG] Image or LLM disabled, returning None")
        return (None, "", None)
    
    # SANITIZE caption for image generation (narrative text stays grim!)
    # This prevents content filter blocks while keeping the story dark
    original_caption = caption
    caption = _sanitize_for_image_generation(caption)
    if original_caption != caption:
        print(f"[SANITIZE] Image prompt cleaned to avoid content filters")

    # CRITICAL: The narrative `dispatch` text (player-facing) is now ALSO injected
    # into the image prompt as `narrative_dispatch`. It must be sanitized the same
    # way as `caption`, otherwise raw narrative gore/violence words bypass the
    # content filter shield and the image silently fails or degrades.
    sanitized_narrative_dispatch = ""
    if dispatch:
        sanitized_narrative_dispatch = _sanitize_for_image_generation(dispatch)
        if sanitized_narrative_dispatch != dispatch:
            print(f"[SANITIZE] Narrative dispatch cleaned for image prompt")
    
    try:
        prev_time_of_day, prev_color = "", ""
        prev_img_captions = []
        prev_vision_analysis = ""  # Vision AI description of last frame
        prev_spatial         = ""  # Spatial compass: ahead/left/right/ground/height
        prev_setting         = ""  # Environment type: outdoor-desert, indoor-corridor, etc.
        prev_img_path = None
        prev_img_paths_list = []  # List of recent image paths for multi-img2img
        # High-fidelity guide still for the PRIMARY reference. When the primary
        # reference 'image' has been replaced by a live world-model frame (act-time
        # capture / observe), this holds the original Gemini still so img2img can
        # anchor QUALITY on it while spatial state follows the live frame.
        primary_guide_image_path = None
        
        if frame_idx > 0 and _hist:
            last_imgs = []
            # Use 2 reference images for better stability
            num_images_to_collect = 2
            print(f"\n{'='*70}")
            print(f"[IMG2IMG COLLECT] Frame {frame_idx} - Starting reference collection")
            print(f"[IMG2IMG COLLECT] History has {len(_hist)} entries")
            print(f"[IMG2IMG COLLECT] Collecting up to {num_images_to_collect} reference images")
            print(f"[IMG2IMG COLLECT] Will stop at last hard transition (location change)")
            print(f"{'='*70}\n")
            
            for idx, entry in enumerate(reversed(_hist)):
                has_image = bool(entry.get("image"))
                has_vision = bool(entry.get("vision_dispatch"))
                was_hard_transition = entry.get("hard_transition", False)
                
                print(f"[IMG2IMG COLLECT] History[{idx}]: image={has_image}, vision={has_vision}, hard_transition={was_hard_transition}")
                if has_image:
                    print(f"[IMG2IMG COLLECT]   Image path: {entry.get('image')}")
                
                if entry.get("image") and entry.get("vision_dispatch"):
                    last_imgs.append((
                        entry["image"],
                        entry["vision_dispatch"],
                        entry.get("vision_analysis", ""),  # visual description
                        entry.get("spatial_compass", ""),  # ahead/left/right compass
                        entry.get("setting_type", ""),     # indoor/outdoor type
                        entry.get("guide_image", ""),      # original hi-fi guide still
                    ))
                    print(f"[IMG2IMG COLLECT]   -> Added to reference list (total: {len(last_imgs)})")
                
                # CRITICAL: The reference buffer must RESET at a location change —
                # whether or not that boundary frame produced a usable image.
                #
                # This break used to be nested INSIDE the has-image branch above.
                # That meant a hard-cut frame whose image was missing (async
                # write-back race, content-filter block, or flipbook turn) never
                # stopped the search, so collection kept walking back PAST the cut
                # and grabbed PRE-CUT frames. The next turn then did img2img off the
                # OLD scene instead of the freshly-expanded new area — exactly the
                # "hard cut expands, then the next image snaps back to the old scene"
                # bug. Evaluating the boundary unconditionally fixes that: we include
                # the new-area frame if it has an image, otherwise we fall back to a
                # clean text-to-image for the new area rather than reviving the old one.
                if was_hard_transition:
                    print(f"[IMG2IMG COLLECT] HARD TRANSITION BOUNDARY - stopping collection")
                    print(f"[IMG2IMG COLLECT] Reference buffer reset at location change (collected {len(last_imgs)} pre-boundary refs)")
                    break
                
                if len(last_imgs) == num_images_to_collect:
                    print(f"[IMG2IMG COLLECT] Collected {num_images_to_collect} references, stopping search")
                    break
            
            print(f"\n[IMG2IMG COLLECT] RESULT: Found {len(last_imgs)} reference images in history")
            if len(last_imgs) == 0:
                print(f"[IMG2IMG COLLECT] WARNING: NO REFERENCE IMAGES FOUND!")
                print(f"[IMG2IMG COLLECT] This will generate a text-to-image (no continuity)")
            
            if len(last_imgs) >= 1:
                print(f"[IMG2IMG COLLECT] Processing {len(last_imgs)} references for img2img...")
                # Get most recent entry — unpack all fields
                img, cap, vis_analysis, spatial_compass, setting_type, guide_img = last_imgs[0]
                prev_vision_analysis = vis_analysis
                prev_spatial         = spatial_compass
                prev_setting         = setting_type
                prev_img_path = str(_resolve_image_path(img))
                if not os.path.exists(prev_img_path):
                    prev_img_path = None

                # If the primary reference's 'image' is a live world-model frame,
                # the original hi-fi guide still lives under 'guide_image'. Keep it
                # as a secondary reference so img2img anchors quality on it while
                # the composition follows the realtime frame. (Skip if it's the
                # same file — i.e. no realtime frame ever replaced it.)
                if guide_img:
                    _guide_resolved = str(_resolve_image_path(guide_img))
                    if os.path.exists(_guide_resolved) and _guide_resolved != prev_img_path:
                        primary_guide_image_path = _guide_resolved
                
                # Collect all recent image paths for multi-reference img2img
                for idx, (img, cap, *_rest) in enumerate(last_imgs):
                    img_path = str(_resolve_image_path(img))
                    if not os.path.exists(img_path):
                        continue  # Skip missing images
                    if os.path.exists(img_path):
                        prev_img_paths_list.append(img_path)
                        prev_img_captions.append(cap)
                    else:
                        print(f"[IMG2IMG ERROR] Ref {idx+1}: {img_path} NOT FOUND!")
                
                # EXPERIMENTAL: Always include frame 0 as visual anchor
                # Toggle: Set USE_FRAME_0_ANCHOR = False to disable
                USE_FRAME_0_ANCHOR = False  # Set to False to disable frame 0 anchoring
                
                if USE_FRAME_0_ANCHOR and len(_hist) > 0 and frame_idx > 1:
                    frame_0_image = _hist[0].get("image")
                    if frame_0_image and frame_0_image not in prev_img_paths_list:
                        frame_0_path = str(_resolve_image_path(frame_0_image))
                        if not os.path.exists(frame_0_path):
                            frame_0_path = None
                        if os.path.exists(frame_0_path):
                            # Prepend frame 0 as the "visual constitution"
                            prev_img_paths_list.insert(0, frame_0_path)
                            print(f"[IMG2IMG] Frame 0 anchor ENABLED - Added: {os.path.basename(frame_0_path)}")
                elif not USE_FRAME_0_ANCHOR and frame_idx > 1:
                    print(f"[IMG2IMG] Frame 0 anchor DISABLED")
                
                print(f"\n[IMG2IMG SUMMARY] Frame {frame_idx}: Final reference list:")
                for i, path in enumerate(prev_img_paths_list):
                    print(f"[IMG2IMG SUMMARY]   Ref {i+1}: {os.path.basename(path)}")
                print(f"[IMG2IMG SUMMARY] Total: {len(prev_img_paths_list)} reference image(s)\n")
                
                # Skip time extraction - we maintain it in state already
                prev_time_of_day = ""
                prev_color = ""
        
        # Use provided time_of_day, or fall back to state (persistent across frames)
        # Check for None explicitly (not just falsy) to handle empty string vs None
        if time_of_day is None:
            use_time_of_day = state.get('time_of_day', '') if 'state' in globals() else ''
            if use_time_of_day:
                print(f"[TIME] Using time_of_day from state: {use_time_of_day}")
        else:
            use_time_of_day = time_of_day
            if use_time_of_day:
                print(f"[TIME] Using explicitly provided time_of_day: {use_time_of_day}")
        use_color = prev_color
        
        # --- Inject world summary as background context ---
        current_state = get_state(session_id)
        world_summary = summarize_world_state(current_state)
        # --- Summarize world prompt for image flavor ---
        world_flavor = ""
        if current_state.get("world_prompt", ""):
            world_flavor = summarize_world_prompt_for_image(current_state["world_prompt"])
        prompt_str = build_image_prompt(
            player_choice=choice,
            dispatch=caption,                              # visual scene (sanitized)
            narrative_dispatch=sanitized_narrative_dispatch,  # narrative text (sanitized)
            prev_vision_analysis=prev_vision_analysis,
            hard_transition=hard_transition,
            is_timeout_penalty=is_timeout_penalty,
            prev_spatial=prev_spatial,
            prev_setting=prev_setting,
        )
        
        # Inject world flavor and location for image model only
        if world_flavor:
            prompt_str += f" World flavor: {world_flavor}."
        if world_summary:
            prompt_str += f" Background context: {world_summary}."
        # ALWAYS maintain lighting/aesthetic continuity, even during location changes.
        # NOTE: guard on prev_img_paths_list (the list we actually populate). The old
        # code checked `prev_img_paths`, which is never appended to, so this whole
        # continuity clause was silently dead — hard cuts lost their "same world
        # aesthetic" instruction and same-location frames lost their lighting match.
        if prev_img_paths_list:
            if hard_transition:
                # Location change - use reference for lighting/aesthetic ONLY (not composition)
                prompt_str = (
                    f"{prompt_str}\nMaintain the same lighting, time of day, and color palette as the previous image. "
                    f"New location, but same world aesthetic and atmospheric conditions."
                )
            else:
                # Same location - full img2img continuity
                prompt_str = (
                    f"{prompt_str}\nMatch the lighting, time of day, and color palette to the previous image."
                )
        # --- LOGGING ---
        print("[IMG LOG] --- IMAGE GENERATION PARAMETERS ---")
        print(f"[IMG LOG] frame_idx: {frame_idx}")
        print(f"[IMG LOG] mode: {mode}")
        safe_choice = str(choice).encode('ascii', 'replace').decode('ascii')
        print(f"[IMG LOG] choice: {safe_choice}")
        safe_caption = str(caption).encode('ascii', 'replace').decode('ascii')
        print(f"[IMG LOG] caption (vision_dispatch): {safe_caption}")
        safe_dispatch = str(dispatch).encode('ascii', 'replace').decode('ascii')
        print(f"[IMG LOG] dispatch (narrative): {safe_dispatch}")
        print(f"[IMG LOG] time_of_day: {use_time_of_day}")
        safe_prompt = str(prompt_str).encode('ascii', 'replace').decode('ascii')
        print(f"[IMG LOG] prompt_str (full): {safe_prompt}")
        print(f"[IMG LOG] previous_image_path (actual): {prev_img_path if prev_img_path else 'None'}")
        print(f"[IMG LOG] reference_images_list: {len(prev_img_paths_list)} images")
        print(f"[IMG LOG] use_edit_mode: {use_edit_mode}")
        safe_world = str(world_prompt).encode('ascii', 'replace').decode('ascii')
        print(f"[IMG LOG] world_prompt: {safe_world}")
        print(f"[IMG LOG] session_id: {session_id}")
        print("[IMG LOG] --- END IMAGE GENERATION PARAMETERS ---")
        img_dir = _get_image_dir(session_id)
        filename = f"{hash(caption) & 0xFFFFFFFF}_{_slug(caption)}.png"
        image_path = img_dir / filename
        
        # --- ROUTE TO APPROPRIATE IMAGE PROVIDER ---
        active_image_provider = ai_provider_manager.get_image_provider()
        if active_image_provider == "veo":
            try:
                # Use Veo 3.1 for video-based image generation
                # Generates video from previous frame, extracts last frame as "image"
                print(f"[IMG] Using Veo 3.1 video generation (last frame extraction)", flush=True)
                from veo_video_utils import generate_frame_via_video, _session_costs, MAX_SESSION_COST
                print(f"[IMG] Veo module imported, session_cost=${_session_costs['total_cost']:.2f}/${MAX_SESSION_COST:.2f}", flush=True)
                
                # No frame limit - only budget limit
                print(f"[IMG] Calling generate_frame_via_video with frame_idx={frame_idx}", flush=True)
                
                # Pass reference frames for visual continuity (like img2img)
                # Use the same reference frames we collected for img2img
                reference_frames_for_veo = prev_img_paths_list if prev_img_paths_list else []
                if reference_frames_for_veo:
                    print(f"[IMG] Passing {len(reference_frames_for_veo)} reference frames to Veo for continuity", flush=True)
                
                result_path, veo_prompt, video_path = generate_frame_via_video(
                    prompt=prompt_str,
                    first_frame_path=prev_img_paths_list[0] if prev_img_paths_list else None,
                    caption=caption,
                    frame_idx=frame_idx,
                    world_prompt=world_prompt,
                    action_context=choice,
                    reference_frames=reference_frames_for_veo,  # Pass ALL frames (including starting frame) for style continuity
                    video_segments_dir=_get_video_segments_dir(session_id),  # Session-specific segments directory
                    video_films_dir=_get_video_films_dir(session_id)  # Session-specific films directory
                )
                print(f"[IMG] generate_frame_via_video returned: {result_path}", flush=True)
                if video_path:
                    print(f"[IMG] Video available for playback: {video_path}", flush=True)
                
                if result_path:
                    _last_image_path = result_path
                    return (result_path, veo_prompt, video_path)
                else:
                    print(f"[IMG] Veo returned None - budget limit reached or error, falling back to Gemini", flush=True)
                    # Fall through to Gemini below
            except Exception as veo_error:
                print(f"[IMG] Veo error: {veo_error}", flush=True)
                import traceback
                traceback.print_exc()
        
        elif active_image_provider == "gemini":
            # Use Google Gemini (Nano Banana) - OFFICIAL API
            print(f"[IMG] Using Google Gemini (Nano Banana) provider")
            from gemini_image_utils import generate_with_gemini, generate_gemini_img2img
            
            # Use img2img for ALL frames with history (style continuity + movement instructions)
            if prev_img_paths_list and frame_idx > 0:
                # For hard transitions (location changes), use ONLY 1 reference for lighting/aesthetic
                # For normal transitions, use full reference set for composition continuity
                print(f"\n{'='*70}")
                print(f"[IMG GENERATION] USING IMG2IMG MODE (STYLE CONTINUITY)")
                print(f"[IMG GENERATION] frame_idx={frame_idx}")
                print(f"[IMG GENERATION] movement_type={_last_movement_type}")
                print(f"[IMG GENERATION] hard_transition={hard_transition}")
                print(f"[IMG GENERATION] References will provide STYLE/AESTHETIC only")
                print(f"[IMG GENERATION] Movement instructions will override composition")
                print(f"[IMG GENERATION] Available references: {len(prev_img_paths_list)}")
                
                # SPECIAL CASE: Frame 1 always uses Frame 0 strongly (no hard transition)
                if frame_idx == 1:
                    ref_images_to_use = prev_img_paths_list[:1]  # Use ONLY most recent (Frame 0)
                    print(f"[IMG GENERATION] FRAME 1 SPECIAL CASE - Using most recent reference from intro")
                    print(f"[IMG GENERATION] This ensures color/lighting consistency from Frame 0 to Frame 1")
                elif hard_transition:
                    ref_images_to_use = prev_img_paths_list[:1]  # Only most recent for lighting
                    print(f"[IMG GENERATION] Hard transition - using 1 reference image (lighting/aesthetic only)")
                else:
                    ref_images_to_use = prev_img_paths_list[:1]  # ONLY most recent for strongest continuity
                    print(f"[IMG GENERATION] Normal transition - using 1 reference image (most recent frame)")

                # REALTIME QUALITY GUARD: the most-recent reference may be a LIVE
                # world-model screenshot (act-time / observe capture). Those frames
                # are often melty / low-fidelity, so they must NOT be the MAIN img2img
                # influence — Gemini weights the FIRST reference most heavily, and
                # letting the live frame lead produced ugly, degraded images. When the
                # original high-fidelity guide still is available (primary_guide_image_path),
                # make the GUIDE STILL the PRIMARY influence and demote the live frame
                # to a SECONDARY spatial anchor (or drop it entirely on single-reference
                # hard cuts / Frame 1, where a clean aesthetic anchor matters most).
                if primary_guide_image_path:
                    live_frame = ref_images_to_use[0] if ref_images_to_use else None
                    if hard_transition or frame_idx == 1:
                        ref_images_to_use = [primary_guide_image_path]
                        print(f"[IMG GENERATION] Realtime: guide still as sole reference (quality anchor) [{os.path.basename(primary_guide_image_path)}]")
                    else:
                        ref_images_to_use = [primary_guide_image_path]
                        if live_frame and live_frame != primary_guide_image_path:
                            ref_images_to_use.append(live_frame)
                        print(f"[IMG GENERATION] Realtime dual-ref: guide still PRIMARY (quality/aesthetic) + live frame SECONDARY (spatial) [{os.path.basename(primary_guide_image_path)}]")
                
                print(f"[IMG GENERATION] References being passed to API:")
                for i, ref in enumerate(ref_images_to_use):
                    print(f"[IMG GENERATION]   {i+1}. {os.path.basename(ref)}")
                print(f"{'='*70}\n")
                
                # ALWAYS use HQ for first image, then respect quality toggle
                use_hq_for_this_frame = True if frame_idx == 0 else QUALITY_MODE
                if frame_idx == 0:
                    print(f"[QUALITY MODE] Frame 0 (intro) - FORCING HQ (Gemini Pro) for visual consistency")
                
                # --- PARALLEL FLIPBOOK GENERATION (Direct Comparison Mode) ---
                # Start flipbook generation at the SAME TIME as static image, using SAME parent reference
                current_state = _load_state(session_id)
                flipbook_enabled = current_state.get("flipbook_mode", False)
                if flipbook_enabled:
                    print(f"[FLIPBOOK] Parallel generation starting - using parent reference: {os.path.basename(ref_images_to_use[0])}")
                    
                    # Clear any stale flipbook URL from the previous turn so the bot's
                    # wait loop doesn't immediately pick up an old GIF.  The new URL will
                    # be written by the thread when it completes (or "FAILED" on error).
                    try:
                        _st_clear = _load_state(session_id)
                        _st_clear['current_flipbook_url'] = None
                        _save_state(_st_clear, session_id)
                        print(f"[FLIPBOOK] Cleared stale flipbook URL before starting new generation.")
                    except Exception as _clear_err:
                        print(f"[FLIPBOOK] Warning: could not clear stale flipbook URL: {_clear_err}")
                    print(f"[FLIPBOOK] Starting new flipbook generation (preserving previous frames for style continuity).")

                    import threading
                    
                    def generate_flipbook_parallel():
                        print(f"[FLIPBOOK THREAD] Parallel thread started", flush=True)
                        try:
                            from create_flipbook_gif import grid_to_flipbook_gif
                            from gemini_image_utils import generate_gemini_img2img
                            
                            # Reload state to get temporal anchors (first/last frames of previous flipbook)
                            state_path = _get_state_path(session_id)
                            with open(state_path, 'r', encoding='utf-8') as f:
                                st_temp = json.load(f)
                            
                            prev_grid  = st_temp.get('flipbook_last_grid')   # Full 4x4 grid from previous turn (style ref)
                            prev_last  = st_temp.get('flipbook_last_frame')  # Panel 16 — spatial ground truth (WHERE camera is NOW)
                            prev_first = st_temp.get('flipbook_first_frame') # Panel 01 — shows the broader environment at turn start
                            
                            # FLIPBOOK PREFIX: Spatial-anchor-first philosophy
                            # Panel 16 is the first reference image — Frame 1 of the new grid
                            # must be the immediate continuation of it.
                            flipbook_prefix = (
                                "🚨🚨🚨 ABSOLUTE COMMAND — READ THIS BEFORE ANYTHING ELSE 🚨🚨🚨\n\n"
                                "═══════════════════════════════════════════════════════════════\n"
                                "⚡ SPATIAL CONTINUITY — YOUR CAMERA POSITION IS LOCKED ⚡\n"
                                "═══════════════════════════════════════════════════════════════\n\n"
                                "THE FIRST REFERENCE IMAGE IS PANEL 16 OF THE PREVIOUS SEQUENCE.\n"
                                "It shows EXACTLY where the camera is pointing RIGHT NOW.\n"
                                "YOUR FRAME 1 MUST BE THE VERY NEXT MOMENT AFTER THAT IMAGE.\n\n"
                                "FRAME 1 REQUIREMENTS (non-negotiable):\n"
                                "✓ Camera at IDENTICAL height as the first reference image\n"
                                "✓ Camera pointing in the SAME DIRECTION as the first reference image\n"
                                "✓ Same visible landmarks, terrain, and sky/ground ratio\n"
                                "✓ Scene is clearly 0.25 seconds AFTER the reference — seamless cut\n\n"
                                "A viewer watching the reference then Frame 1 must see UNCUT FOOTAGE.\n\n"
                                "═══════════════════════════════════════════════════════════════\n"
                                "📐 GRID OUTPUT FORMAT\n"
                                "═══════════════════════════════════════════════════════════════\n\n"
                                "Output: 4×4 grid, 16 panels, 1200×896 pixels total\n"
                                "• Each panel: 300×224 pixels\n"
                                "• Grid reads: LEFT→RIGHT, TOP→BOTTOM (panels 1…16)\n"
                                "• NO text, numbers, labels, or timecodes in any panel\n"
                                "• NO borders visible within panels (grid dividers only between panels)\n\n"
                                "═══════════════════════════════════════════════════════════════\n"
                                "🎥 CAMERA: 1993 VHS CAMCORDER / BODY CAM — WIDE ANGLE\n"
                                "═══════════════════════════════════════════════════════════════\n\n"
                                "• First-person POV, camera strapped to player's chest/head\n"
                                "• 28-35mm equivalent field of view (WIDE — show MORE environment)\n"
                                "• Hands may appear at frame bottom when reaching/interacting\n\n"
                                "FORBIDDEN (third-person shots will invalidate the entire grid):\n"
                                "❌ Showing player from behind, side, or above\n"
                                "❌ 'Camera following a character' shots\n"
                                "❌ Any frame showing the player's body as a separate entity\n\n"
                                "═══════════════════════════════════════════════════════════════\n\n"
                            )
                            flipbook_prefix += PROMPTS.get("gemini_flipbook_4panel_prefix", "")
                            
                            # --- REFERENCE STRATEGY: SPATIAL ANCHOR FIRST ---
                            # ORDER MATTERS: Gemini weights the FIRST reference most heavily.
                            # 1. PANEL 16 (last frame) — PRIMARY spatial anchor; Frame 1 must continue from here
                            # 2. PANEL 01 (first frame) — shows what the environment looked like at turn start
                            # 3. FULL grid — style/quality reference only (lower priority)
                            # Grid templates (if they exist) are appended last — layout aid only.
                            flipbook_refs = []

                            # 1. LAST FRAME (panel_16) — THE PRIMARY SPATIAL ANCHOR
                            # This is where the camera IS right now. Frame 1 of the new sequence
                            # must be the very next moment after this image.
                            if prev_last and os.path.exists(prev_last):
                                flipbook_refs.append(prev_last)
                                print(f"[FLIPBOOK ANCHOR] Panel 16 (spatial ground truth) FIRST: {os.path.basename(prev_last)}", flush=True)
                            else:
                                print(f"[FLIPBOOK GEN] No panel_16 available (first turn after intro)", flush=True)

                            # 2. FIRST FRAME (panel_01) — environment reference for world coherence
                            # Shows the broader environment before the previous action began.
                            if prev_first and os.path.exists(prev_first):
                                flipbook_refs.append(prev_first)
                                print(f"[FLIPBOOK ANCHOR] Panel 01 (environment reference) SECOND: {os.path.basename(prev_first)}", flush=True)

                            # 3. FULL previous flipbook grid — style/quality reference (lowest priority)
                            # Only included if we have fewer than 2 spatial refs to pad context.
                            if prev_grid and os.path.exists(prev_grid) and len(flipbook_refs) < 2:
                                flipbook_refs.append(prev_grid)
                                print(f"[FLIPBOOK STYLE] Full grid as style reference: {os.path.basename(prev_grid)}", flush=True)

                            # 4. Grid template (layout hint — lowest weight, append last)
                            numbered_template_path = str(ROOT / "prompts" / "flipbook_numbered_template.png")
                            blank_template_path    = str(ROOT / "prompts" / "flipbook_blank_grid_template.png")
                            if os.path.exists(numbered_template_path):
                                flipbook_refs.append(numbered_template_path)
                                print(f"[FLIPBOOK LAYOUT] Numbered template appended (layout hint)", flush=True)
                            elif os.path.exists(blank_template_path):
                                flipbook_refs.append(blank_template_path)
                                print(f"[FLIPBOOK LAYOUT] Blank grid template appended (layout hint)", flush=True)

                            if not flipbook_refs:
                                print(f"[FLIPBOOK GEN] No reference images available (first turn)", flush=True)
                            
                            print(f"[FLIPBOOK GEN] Using {len(flipbook_refs)} total references (template + grid + last frame)", flush=True)

                            # CRITICAL: Add explicit action enforcement AT THE VERY START
                            # FREE WILL ACTIONS (custom actions not in standard choices) MUST TAKE PRIORITY
                            dispatch_preview = dispatch[:250] if dispatch else caption[:250]
                            
                            # Detect if this is a FREE WILL action (not a standard choice like "Approach X" or "Examine Y")
                            is_free_will = choice and not any([
                                choice.startswith("Approach"),
                                choice.startswith("Examine"),
                                choice.startswith("Use"),
                                choice.startswith("Take"),
                                choice.startswith("Look"),
                                choice.startswith("Search"),
                                choice.startswith("Listen"),
                                choice.startswith("Wait"),
                                choice == "Intro"
                            ])
                            
                            if is_free_will:
                                # FREE WILL: Show the player's EXACT action, ignore AI interpretation
                                try:
                                    safe_choice = choice[:80].encode('ascii', 'replace').decode('ascii')
                                    print(f"[FREE WILL DETECTED] Prioritizing player's direct command: {safe_choice}", flush=True)
                                except:
                                    print(f"[FREE WILL DETECTED] Prioritizing player's direct command (contains special characters)", flush=True)
                                action_enforcement = (
                                    "🚫🚫🚫 FIRST-PERSON ONLY - NO 3RD PERSON ALLOWED 🚫🚫🚫\n\n"
                                    "THIS IS BODY CAM FOOTAGE. The camera IS the player's eyes.\n"
                                    "DO NOT show 'a man walking' or 'a person' from outside.\n"
                                    "Show ONLY what the player's eyes see while performing the action.\n\n"
                                    "ABSOLUTE COMMAND - FREE WILL ACTION\n\n"
                                    "The player used FREE WILL to command this EXACT action:\n"
                                    f">>> \"{choice}\" <<<\n\n"
                                    "YOU MUST OBEY THIS COMMAND AT ALL COSTS.\n\n"
                                    "ABSOLUTE RULES:\n"
                                    "1. Show FIRST-PERSON perspective - you ARE the player, camera = your eyes\n"
                                    "2. DO NOT show 'a man' or 'a person' - that's 3rd person (FORBIDDEN)\n"
                                    "3. Show what YOUR EYES see while performing the action\n"
                                    "4. If 'head towards vehicles' -> show vehicles getting closer in YOUR view\n"
                                    "5. If 'climb fence' -> show YOUR hands grabbing fence from YOUR POV\n"
                                    "6. If 'run to tower' -> show ground/tower from running POV\n"
                                    "7. The flipbook shows 4 seconds of this action from YOUR eyes\n"
                                    "8. NEVER show the player as a separate person/character\n\n"
                                    f"Context (what happens as result): {dispatch_preview}\n\n"
                                    "=" * 70 + "\n\n"
                                )
                            else:
                                # Standard choice: Use consequence text as primary instruction
                                print(f"[STANDARD CHOICE] Using consequence text as primary instruction", flush=True)
                                action_enforcement = (
                                    "🚫🚫🚫 FIRST-PERSON ONLY - NO 3RD PERSON ALLOWED 🚫🚫🚫\n\n"
                                    "THIS IS BODY CAM FOOTAGE. The camera IS the player's eyes.\n"
                                    "DO NOT show 'a man walking' or 'a person' from outside.\n"
                                    "Show ONLY what the player's eyes see while performing the action.\n\n"
                                    "CRITICAL INSTRUCTION - READ THIS FIRST\n\n"
                                    "YOU MUST ANIMATE THIS SPECIFIC ACTION:\n"
                                    f">>> {dispatch_preview} <<<\n\n"
                                    f"Player's choice was: \"{choice}\"\n\n"
                                    "RULES:\n"
                                    "1. FIRST-PERSON PERSPECTIVE - Show what YOUR eyes see (NOT a person from outside)\n"
                                    "2. Show EXACTLY what the text describes from first-person POV\n"
                                    "3. If text says 'approach' -> show target getting closer in YOUR view\n"
                                    "4. If text says 'examine' -> show object filling YOUR view\n"
                                    "5. NEVER show 'a man' or 'a person' - that's 3rd person (FORBIDDEN)\n"
                                    "6. You ARE the player - camera = your eyes - no external views\n\n"
                                    "=" * 70 + "\n\n"
                                )
                            
                            # Use the FULL prompt_str for flipbooks with action FIRST
                            flipbook_prompt = action_enforcement + flipbook_prefix + prompt_str
                            try:
                                safe_prompt = prompt_str[:100].encode('ascii', 'replace').decode('ascii')
                                print(f"[FLIPBOOK] Using full prompt with context: {safe_prompt}...", flush=True)
                            except:
                                print(f"[FLIPBOOK] Using full prompt (contains special characters)", flush=True)
                            
                            # Use layout template + parent references
                            grid_path = generate_gemini_img2img(
                                prompt=flipbook_prompt,
                                caption=f"{caption}_flipbook",
                                reference_image_path=flipbook_refs,
                                world_prompt=world_prompt,
                                time_of_day=use_time_of_day,
                                action_context=choice,
                                hd_mode=True, # Use Pro model for HIGH QUALITY flipbooks
                                output_dir=img_dir,
                                is_flipbook=True
                            )
                            
                            if grid_path:
                                result_dict = grid_to_flipbook_gif(Path(grid_path))
                                gif_path = result_dict.get('gif_path')
                                if gif_path:
                                    # Save to state (SAFE LOCK VERSION)
                                    st = _load_state(session_id)
                                    st['current_flipbook_url'] = str(gif_path)
                                    st['flipbook_last_grid'] = str(grid_path) # Store the entire 4x4 grid PNG
                                    st['flipbook_first_frame'] = str(result_dict.get('first_frame')) if result_dict.get('first_frame') else None
                                    st['flipbook_last_frame'] = str(result_dict.get('last_frame')) if result_dict.get('last_frame') else None
                                    _save_state(st, session_id)
                                    print(f"[FLIPBOOK] Parallel GIF ready and stored in state: {gif_path}", flush=True)
                                else:
                                    # PRODUCTION HARDENING: GIF conversion failure must signal FAILED to
                                    # state, otherwise the bot's wait loop polls for the full 120s timeout
                                    # holding _turn_processing_lock and the whole channel freezes.
                                    st = _load_state(session_id)
                                    st['current_flipbook_url'] = "FAILED"
                                    _save_state(st, session_id)
                                    print(f"[FLIPBOOK ERROR] GIF conversion failed - signaled FAILED to unblock bot", flush=True)
                            else:
                                # Signal failure (SAFE LOCK VERSION)
                                st = _load_state(session_id)
                                st['current_flipbook_url'] = "FAILED"
                                _save_state(st, session_id)
                                print(f"[FLIPBOOK] Parallel generation blocked/failed", flush=True)
                        except Exception as e:
                            try:
                                # Signal failure (SAFE LOCK VERSION)
                                st = _load_state(session_id)
                                st['current_flipbook_url'] = "FAILED"
                                _save_state(st, session_id)
                            except: pass

                            try:
                                safe_e = str(e).encode('ascii', 'replace').decode('ascii')
                                print(f"[FLIPBOOK ERROR] Parallel exception: {safe_e}", flush=True)
                            except:
                                print(f"[FLIPBOOK ERROR] Parallel exception (contains special characters)", flush=True)
                    
                    threading.Thread(target=generate_flipbook_parallel, daemon=True).start()

                # --- STATIC IMAGE GENERATION (Skip if in Flipbook Mode) ---
                if not flipbook_enabled:
                    result_path = generate_gemini_img2img(
                        prompt=prompt_str,
                        caption=caption,
                        reference_image_path=ref_images_to_use,  # Adjusted based on transition type
                        world_prompt=world_prompt,
                        time_of_day=use_time_of_day,
                        action_context=choice,  # Pass action for FPS hands context
                        hd_mode=use_hq_for_this_frame,  # Frame 0 always HQ, others respect quality toggle
                        output_dir=img_dir  # Session-specific directory
                    )
                    # SAFETY NET: img2img can come back empty (API timeout on the
                    # slow lite model, a safety block triggered by the accumulated
                    # live-frame references, or a transient API error — all of which
                    # generate_gemini_img2img swallows into a None). When that
                    # happens the turn used to emit NOTHING, so "interacting" left
                    # the scene frozen on the last frame ("gemini fast can't render a
                    # new image on interact"). Fall back to a plain text-to-image
                    # render (no reference images → smaller payload, far less likely
                    # to time out or trip the reference-driven safety filter) so a
                    # FRESH frame still lands. We lose pixel-perfect img2img
                    # continuity for that one turn, but the world keeps moving —
                    # which mirrors the Krea/fal branches' existing Gemini safety net.
                    if not result_path:
                        print(f"[IMG GENERATION] img2img returned no image - falling back to text-to-image so the scene still advances", flush=True)
                        result_path = generate_with_gemini(
                            prompt=prompt_str,
                            caption=caption,
                            world_prompt=world_prompt,
                            aspect_ratio="4:3",
                            time_of_day=use_time_of_day,
                            is_first_frame=(frame_idx == 0),
                            action_context=choice,
                            hd_mode=use_hq_for_this_frame,
                            output_dir=img_dir,
                        )
                else:
                    print(f"[IMG GENERATION] Skipping static image - Flipbook mode is active.")
                    result_path = None
            else:
                print(f"\n{'='*70}")
                print(f"[IMG GENERATION] USING TEXT-TO-IMAGE MODE (NO STYLE ANCHOR)")
                print(f"[IMG GENERATION] Reasons:")
                print(f"[IMG GENERATION]   - prev_img_paths_list has {len(prev_img_paths_list)} items")
                print(f"[IMG GENERATION]   - frame_idx={frame_idx}")
                if len(prev_img_paths_list) == 0:
                    print(f"[IMG GENERATION] NO REFERENCE IMAGES IN HISTORY")
                    print(f"[IMG GENERATION] This may cause style/aesthetic discontinuity")
                print(f"{'='*70}\n")
                
                # --- PARALLEL FLIPBOOK GENERATION (Direct Comparison Mode for T2I) ---
                current_state = _load_state(session_id)
                flipbook_enabled = current_state.get("flipbook_mode", False)
                if flipbook_enabled:
                    print(f"[FLIPBOOK] Parallel generation starting for TEXT-TO-IMAGE mode (Turn 0 or no references)")
                    
                    # For Turn 0 (intro), clear all flipbook data since there's no previous reference
                    # NOTE: For subsequent turns, we do NOT clear current_flipbook_url (the client clears it after display)
                    try:
                        st_init = _load_state(session_id)
                        st_init['current_flipbook_url'] = None
                        st_init['flipbook_last_frame'] = None
                        st_init['flipbook_first_frame'] = None
                        st_init['flipbook_last_grid'] = None
                        _save_state(st_init, session_id)
                        print(f"[FLIPBOOK] Reset all flipbook data for Turn 0 (intro).")
                    except Exception as e:
                        print(f"[FLIPBOOK ERROR] Failed to manage flipbook data: {e}")

                    import threading
                    
                    def generate_flipbook_parallel_t2i():
                        print(f"[FLIPBOOK THREAD] Parallel T2I thread started", flush=True)
                        try:
                            from create_flipbook_gif import grid_to_flipbook_gif
                            # Note: For T2I, we use generate_with_gemini which produces the grid if the prompt asks for it
                            # OR we can still use img2img with the layout template as the only reference.
                            # Using img2img with the template is safer for layout consistency.
                            from gemini_image_utils import generate_gemini_img2img
                            
                            # Add flipbook prefix - SPECIAL CASE for intro
                            flipbook_prefix = PROMPTS.get("gemini_flipbook_4panel_prefix", "")
                            
                            # Add standard template instruction
                            flipbook_prefix = (
                                "LAYOUT REFERENCE: The attached image is a 4x4 grid template showing STRUCTURAL LAYOUT ONLY.\n\n"
                                "Use the reference for:\n"
                                "- Grid arrangement (4 rows, 4 columns, 16 panels total)\n"
                                "- Panel dimensions and spacing\n\n"
                                "DO NOT use the reference for:\n"
                                "- Content, scenes, subjects, or visual themes\n"
                                "- Any imagery shown in the reference panels\n\n"
                                "The reference is an empty structural template.\n"
                                "Generate completely new visual content based solely on the text prompt below.\n\n" +
                                flipbook_prefix
                            )
                            
                            # CRITICAL: Add action enforcement for FREE WILL (same as img2img path)
                            dispatch_preview = dispatch[:250] if dispatch else caption[:250]
                            
                            # Detect if this is a FREE WILL action
                            is_free_will = choice and not any([
                                choice.startswith("Approach"),
                                choice.startswith("Examine"),
                                choice.startswith("Use"),
                                choice.startswith("Take"),
                                choice.startswith("Look"),
                                choice.startswith("Search"),
                                choice.startswith("Listen"),
                                choice.startswith("Wait"),
                                choice == "Intro"
                            ])
                            
                            if is_free_will:
                                # FREE WILL: Show the player's EXACT action, ignore AI interpretation
                                try:
                                    safe_choice = choice[:80].encode('ascii', 'replace').decode('ascii')
                                    print(f"[FREE WILL DETECTED - T2I] Prioritizing player's direct command: {safe_choice}", flush=True)
                                except:
                                    print(f"[FREE WILL DETECTED - T2I] Prioritizing player's direct command (contains special characters)", flush=True)
                                action_enforcement = (
                                    "🔥🔥🔥 ABSOLUTE COMMAND - FREE WILL ACTION 🔥🔥🔥\n\n"
                                    "The player used FREE WILL to command this EXACT action:\n"
                                    f">>> \"{choice}\" <<<\n\n"
                                    "YOU MUST OBEY THIS COMMAND AT ALL COSTS.\n\n"
                                    "ABSOLUTE RULES:\n"
                                    "1. Show the player ATTEMPTING this exact action in a photorealistic way\n"
                                    "2. The action MUST be physically plausible and shown from first-person perspective\n"
                                    "3. IGNORE the consequence text below - show the ACTION, not the result\n"
                                    "4. If the command says 'kick door' -> show foot moving toward door\n"
                                    "5. If the command says 'climb fence' -> show hands grabbing fence, body moving up\n"
                                    "6. If the command says 'run to tower' -> show rapid movement toward tower\n"
                                    "7. The flipbook shows the first 4 seconds of this action beginning\n"
                                    "8. This is a direct player command - it overrides ALL other instructions\n\n"
                                    "PERSPECTIVE: ONE CONTINUOUS FIRST-PERSON SHOT - camera strapped to player's head.\n"
                                    "NEVER show the player from outside. NEVER show their back, profile, or body.\n"
                                    "You ARE the player looking OUT at the world.\n\n"
                                    f"Context (what happens as result): {dispatch_preview}\n\n"
                                    "=" * 70 + "\n\n"
                                )
                                flipbook_prompt = action_enforcement + flipbook_prefix + prompt_str
                            elif choice == "Intro":
                                # Intro - use standard prompt
                                print(f"[INTRO] Using standard intro prompt", flush=True)
                                flipbook_prompt = flipbook_prefix + prompt_str
                            else:
                                # Standard choice - add action enforcement
                                print(f"[STANDARD CHOICE - T2I] Using consequence text as primary instruction", flush=True)
                                action_enforcement = (
                                    "CRITICAL INSTRUCTION - READ THIS FIRST\n\n"
                                    "YOU MUST ANIMATE THIS SPECIFIC ACTION:\n"
                                    f">>> {dispatch_preview} <<<\n\n"
                                    f"Player's choice was: \"{choice}\"\n\n"
                                    "RULES:\n"
                                    "1. Show EXACTLY what the text above describes\n"
                                    "2. DO NOT show climbing ladders, opening boxes, or indoor scenes unless the text says so\n"
                                    "3. If text says 'outside' -> show outdoor scene\n"
                                    "4. If text says 'approach' -> show walking toward something\n"
                                    "5. If text says 'examine' -> show looking at something\n"
                                    "6. IGNORE any conflicting visual references - follow the TEXT ONLY\n\n"
                                    "PERSPECTIVE: ONE CONTINUOUS FIRST-PERSON SHOT - camera strapped to player's head.\n"
                                    "NEVER show the player from outside. NEVER show their back, profile, or body.\n"
                                    "You ARE the player looking OUT at the world. No cuts, no edits, no perspective changes.\n\n"
                                    "=" * 70 + "\n\n"
                                )
                                flipbook_prompt = action_enforcement + flipbook_prefix + prompt_str
                            try:
                                safe_prompt = prompt_str[:100].encode('ascii', 'replace').decode('ascii')
                                print(f"[FLIPBOOK T2I] Using full prompt with context: {safe_prompt}...", flush=True)
                            except:
                                print(f"[FLIPBOOK T2I] Using full prompt (contains special characters)", flush=True)
                            
                            # For intro (Turn 0), use PURE T2I with NO reference images
                            # ANY reference (even blank template) confuses the AI for intro
                            print(f"[FLIPBOOK T2I] Using PURE T2I with INTRO-SPECIFIC prefix (no reference)", flush=True)
                            from gemini_image_utils import generate_with_gemini
                            
                            # INTRO-SPECIFIC flipbook prefix - ENVIRONMENTAL ONLY, NO HANDS/PERSON
                            intro_flipbook_prefix = (
                                "🚨🚨🚨 ABSOLUTE COMMAND - READ THIS FIRST 🚨🚨🚨\n\n"
                                "YOU MUST GENERATE A 4x4 GRID OF 16 SEPARATE IMAGES.\n"
                                "DO NOT GENERATE ONE CONTINUOUS IMAGE.\n"
                                "GENERATE 4 ROWS × 4 COLUMNS = 16 SEPARATE PANELS.\n\n"
                                "🚫🚫🚫 ABSOLUTELY NO TEXT IN THE OUTPUT 🚫🚫🚫\n"
                                "❌ DO NOT include 'FRAME 1', 'FRAME 2', etc.\n"
                                "❌ DO NOT include timestamps like '0.00s', '0.25s', etc.\n"
                                "❌ DO NOT include ANY text, numbers, labels, or overlays\n"
                                "✅ ONLY generate CLEAN photorealistic imagery (NO TEXT)\n\n"
                                "EACH PANEL IS A DISTINCT FRAME IN A 16-FRAME ANIMATION.\n"
                                "Your output MUST show clear visual separation between all 16 panels.\n\n"
                                "FLIPBOOK MODE - ENVIRONMENTAL ESTABLISHING SHOT\n\n"
                                "**ALL panels must be the same resolution and arranged in a perfect grid.**\n\n"
                                "This is an ESTABLISHING SHOT showing a location BEFORE the player enters.\n"
                                "Think: Opening scene of a documentary or film showing the setting.\n\n"
                                "📐 FIELD OF VIEW: EXTRA WIDE ANGLE (24mm-28mm equivalent)\n"
                                "CRITICAL: Use an EXTREMELY WIDE field of view for this establishing shot.\n"
                                "• Show the ENTIRE facility complex in frame\n"
                                "• Show MAXIMUM landscape - sky, horizon, distant terrain\n"
                                "• Think: Wide documentary establishing shot\n"
                                "• MORE environment visible, NOT close-up details\n"
                                "• Avoid narrow/telephoto compositions\n\n"
                                "CRITICAL RULES FOR INTRO:\n"
                                "• WIDE LANDSCAPE VIEW from an elevated/distant vantage point\n"
                                "• ABSOLUTELY NO people, NO hands, NO body parts, NO character visible\n"
                                "• Show ONLY the environment: buildings, landscape, terrain, sky\n"
                                "• This is a STATIONARY CAMERA on a tripod or mounted position\n"
                                "• Documentary/observational style - showing the location FROM OUTSIDE\n"
                                "• The 16 frames show subtle environmental changes over 4 seconds:\n"
                                "  - Dust blowing, clouds moving, light shifting\n"
                                "  - NO major camera movement, just ambient atmosphere\n"
                                "  - Each frame is slightly different but maintains same viewpoint\n\n"
                                "GRID LAYOUT:\n"
                                "Row 1: Frames 1-4 (0-1 seconds) - Initial view\n"
                                "Row 2: Frames 5-8 (1-2 seconds) - Subtle changes\n"
                                "Row 3: Frames 9-12 (2-3 seconds) - Continued atmosphere\n"
                                "Row 4: Frames 13-16 (3-4 seconds) - Final establishing view\n\n"
                                "VHS AESTHETICS:\n"
                                "• 1993 camcorder footage: grainy, desaturated, analog degradation\n"
                                "• Heavy color bleed, tracking errors, VHS artifacts\n"
                                "• NO text overlays, NO timecodes, NO borders\n\n"
                                "=" * 70 + "\n\n"
                            )
                            
                            flipbook_prompt = intro_flipbook_prefix + prompt_str
                            
                            grid_path = generate_with_gemini(
                                prompt=flipbook_prompt,
                                caption=f"{caption}_flipbook",
                                world_prompt=world_prompt,
                                time_of_day=use_time_of_day,
                                action_context=choice,
                                hd_mode=True, # Use Pro model for HIGH QUALITY flipbooks
                                output_dir=img_dir
                            )
                            
                            if grid_path:
                                result_dict = grid_to_flipbook_gif(Path(grid_path))
                                gif_path = result_dict.get('gif_path')
                                if gif_path:
                                    # Save to state (SAFE LOCK VERSION)
                                    st = _load_state(session_id)
                                    st['current_flipbook_url'] = str(gif_path)
                                    st['flipbook_last_grid'] = str(grid_path) # Store intro grid
                                    st['flipbook_first_frame'] = str(result_dict.get('first_frame')) if result_dict.get('first_frame') else None
                                    st['flipbook_last_frame'] = str(result_dict.get('last_frame')) if result_dict.get('last_frame') else None
                                    _save_state(st, session_id)
                                    print(f"[FLIPBOOK] Parallel T2I GIF ready and stored in state: {gif_path}", flush=True)
                                else:
                                    # Signal failure (SAFE LOCK VERSION)
                                    st = _load_state(session_id)
                                    st['current_flipbook_url'] = "FAILED"
                                    _save_state(st, session_id)
                                    print(f"[FLIPBOOK ERROR] T2I GIF conversion failed", flush=True)
                            else:
                                # Signal failure (SAFE LOCK VERSION)
                                st = _load_state(session_id)
                                st['current_flipbook_url'] = "FAILED"
                                _save_state(st, session_id)
                                print(f"[FLIPBOOK] Parallel T2I generation blocked/failed", flush=True)
                        except Exception as e:
                            try:
                                # Signal failure (SAFE LOCK VERSION)
                                st = _load_state(session_id)
                                st['current_flipbook_url'] = "FAILED"
                                _save_state(st, session_id)
                            except: pass
                            
                            try:
                                safe_e = str(e).encode('ascii', 'replace').decode('ascii')
                                print(f"[FLIPBOOK ERROR] Parallel T2I exception: {safe_e}", flush=True)
                            except:
                                print(f"[FLIPBOOK ERROR] Parallel T2I exception (contains special characters)", flush=True)
                    
                    threading.Thread(target=generate_flipbook_parallel_t2i, daemon=True).start()

                # ALWAYS use HQ for first image, then respect quality toggle
                use_hq_for_this_frame = True if frame_idx == 0 else QUALITY_MODE
                if frame_idx == 0:
                    print(f"[QUALITY MODE] Frame 0 (intro) - FORCING HQ (Gemini Pro) for visual consistency")
                
                # --- STATIC IMAGE GENERATION (Skip if in Flipbook Mode) ---
                if not flipbook_enabled:
                    result_path = generate_with_gemini(
                        prompt=prompt_str,
                        caption=caption,
                        world_prompt=world_prompt,
                        aspect_ratio="4:3",  # Faster generation, smaller files (1184x864)
                        time_of_day=use_time_of_day,
                        is_first_frame=(frame_idx == 0),  # Keep for fallback logic
                        action_context=choice,  # Pass action for FPS hands context
                        hd_mode=use_hq_for_this_frame,  # Frame 0 always HQ, others respect quality toggle
                        output_dir=img_dir  # Session-specific directory
                    )
                else:
                    print(f"[IMG GENERATION] Skipping static T2I image - Flipbook mode is active.")
                    result_path = None
            # Return canonical frame (always single image now)
            _last_image_path = result_path
            return (result_path, prompt_str, None)  # Return canonical frame for story logic

        elif active_image_provider == "krea":
            # Use Krea 2 (foundation image model). img2img continuity is done via
            # Krea's style-transfer system (previous frame(s) uploaded as style
            # references). Mirrors the Gemini branch's t2i/img2img split and the
            # realtime guide-still quality guard.
            print(f"[IMG] Using Krea 2 provider")
            from krea_image_utils import generate_with_krea, generate_krea_img2img

            use_hq_for_this_frame = True if frame_idx == 0 else QUALITY_MODE
            if frame_idx == 0:
                print(f"[QUALITY MODE] Frame 0 (intro) - FORCING Krea Large for visual consistency")

            if prev_img_paths_list and frame_idx > 0:
                # Most-recent frame is the strongest continuity anchor.
                ref_images_to_use = prev_img_paths_list[:1]

                # REALTIME QUALITY GUARD: the most-recent reference may be a live
                # world-model screenshot (melty/low-fidelity). When a clean guide
                # still exists, make it the PRIMARY style reference instead.
                if primary_guide_image_path:
                    live_frame = ref_images_to_use[0] if ref_images_to_use else None
                    if hard_transition or frame_idx == 1:
                        ref_images_to_use = [primary_guide_image_path]
                        print(f"[IMG GENERATION] Krea realtime: guide still as sole style reference")
                    else:
                        ref_images_to_use = [primary_guide_image_path]
                        if live_frame and live_frame != primary_guide_image_path:
                            ref_images_to_use.append(live_frame)
                        print(f"[IMG GENERATION] Krea realtime dual-ref: guide still PRIMARY + live frame SECONDARY")

                print(f"[IMG GENERATION] Krea img2img (style transfer) with {len(ref_images_to_use)} reference(s)")
                result_path = generate_krea_img2img(
                    prompt=prompt_str,
                    caption=caption,
                    reference_image_path=ref_images_to_use,
                    world_prompt=world_prompt,
                    time_of_day=use_time_of_day,
                    action_context=choice,
                    hd_mode=use_hq_for_this_frame,
                    output_dir=img_dir,
                )
            else:
                print(f"[IMG GENERATION] Krea text-to-image (no reference anchor)")
                result_path = generate_with_krea(
                    prompt=prompt_str,
                    caption=caption,
                    world_prompt=world_prompt,
                    aspect_ratio="4:3",
                    time_of_day=use_time_of_day,
                    is_first_frame=(frame_idx == 0),
                    action_context=choice,
                    hd_mode=use_hq_for_this_frame,
                    output_dir=img_dir,
                )

            # SAFETY NET: if Krea failed (job error/timeout or missing key) but
            # Gemini is available, render the frame with Gemini so the world
            # never goes blank on a single bad turn.
            if not result_path and GEMINI_API_KEY:
                print(f"[IMG] Krea returned no image - falling back to Gemini for this frame", flush=True)
                from gemini_image_utils import generate_with_gemini, generate_gemini_img2img
                if prev_img_paths_list and frame_idx > 0:
                    fb_refs = [primary_guide_image_path] if primary_guide_image_path else prev_img_paths_list[:1]
                    result_path = generate_gemini_img2img(
                        prompt=prompt_str, caption=caption, reference_image_path=fb_refs,
                        world_prompt=world_prompt, time_of_day=use_time_of_day,
                        action_context=choice, hd_mode=use_hq_for_this_frame, output_dir=img_dir,
                    )
                else:
                    result_path = generate_with_gemini(
                        prompt=prompt_str, caption=caption, world_prompt=world_prompt,
                        aspect_ratio="4:3", time_of_day=use_time_of_day,
                        is_first_frame=(frame_idx == 0), action_context=choice,
                        hd_mode=use_hq_for_this_frame, output_dir=img_dir,
                    )

            _last_image_path = result_path
            return (result_path, prompt_str, None)

        elif active_image_provider == "fal":
            # fal.ai SDXL Lightning — optional speed preset. Synchronous REST
            # call typically completes in ~1-2s (vs ~12s Krea Medium / ~15-30s
            # Gemini Pro), at the cost of lower fidelity than either. Only a
            # single reference image is supported for continuity.
            print(f"[IMG] Using fal.ai (SDXL Lightning) provider")
            from fal_image_utils import generate_with_fal, generate_fal_img2img

            if prev_img_paths_list and frame_idx > 0:
                ref_image_to_use = primary_guide_image_path or prev_img_paths_list[0]
                print(f"[IMG GENERATION] fal img2img with reference: {Path(ref_image_to_use).name}")
                result_path = generate_fal_img2img(
                    prompt=prompt_str,
                    caption=caption,
                    reference_image_path=ref_image_to_use,
                    world_prompt=world_prompt,
                    time_of_day=use_time_of_day,
                    action_context=choice,
                    output_dir=img_dir,
                )
            else:
                print(f"[IMG GENERATION] fal text-to-image (no reference anchor)")
                result_path = generate_with_fal(
                    prompt=prompt_str,
                    caption=caption,
                    world_prompt=world_prompt,
                    time_of_day=use_time_of_day,
                    is_first_frame=(frame_idx == 0),
                    action_context=choice,
                    output_dir=img_dir,
                )

            # SAFETY NET: if fal failed (bad key, rate limit, etc.) but Gemini
            # is available, render the frame with Gemini so the world never
            # goes blank on a single bad turn.
            if not result_path and GEMINI_API_KEY:
                print(f"[IMG] fal returned no image - falling back to Gemini for this frame", flush=True)
                from gemini_image_utils import generate_with_gemini, generate_gemini_img2img
                if prev_img_paths_list and frame_idx > 0:
                    fb_refs = [primary_guide_image_path] if primary_guide_image_path else prev_img_paths_list[:1]
                    result_path = generate_gemini_img2img(
                        prompt=prompt_str, caption=caption, reference_image_path=fb_refs,
                        world_prompt=world_prompt, time_of_day=use_time_of_day,
                        action_context=choice, hd_mode=False, output_dir=img_dir,
                    )
                else:
                    result_path = generate_with_gemini(
                        prompt=prompt_str, caption=caption, world_prompt=world_prompt,
                        aspect_ratio="4:3", time_of_day=use_time_of_day,
                        is_first_frame=(frame_idx == 0), action_context=choice,
                        hd_mode=False, output_dir=img_dir,
                    )

            _last_image_path = result_path
            return (result_path, prompt_str, None)

        elif active_image_provider == "openai":
            # Use OpenAI gpt-image-1
            # Supports img2img via /images/edits endpoint (up to 16 reference images!)
            
            # Validate API key
            if not OPENAI_API_KEY:
                print("[OPENAI IMG] ERROR: OPENAI_API_KEY not set! Cannot generate image.")
                print("[OPENAI IMG] Set environment variable OPENAI_API_KEY or add to config.json")
                return (None, "", None)
            
            # Try IMG2IMG if we have reference images AND it's enabled
            use_img2img = (OPENAI_IMG2IMG_ENABLED and prev_img_paths_list and len(prev_img_paths_list) > 0 and frame_idx > 0)
            img2img_success = False
            
            if use_img2img:
                # IMG2IMG MODE - Use /images/edits with previous frames as reference
                # Using raw requests because Python SDK doesn't support multiple images properly
                print(f"[OPENAI IMG2IMG] Attempting img2img with {len(prev_img_paths_list)} reference image(s)")
                print(f"[OPENAI IMG2IMG] Wrapping prompt with VHS aesthetic instructions...")
                
                # Wrap with VHS styling (same as Gemini)
                vhs_prompt = _build_vhs_prompt(prompt_str, use_img2img=True)
                
                # Build multipart form-data with multiple images
                files = []
                for idx, img_path in enumerate(prev_img_paths_list[:OPENAI_IMG2IMG_REFERENCE_COUNT]):
                    if os.path.exists(img_path):
                        try:
                            files.append(('image[]', (os.path.basename(img_path), open(img_path, 'rb'), 'image/png')))
                            print(f"[OPENAI IMG2IMG] Added reference {idx+1}: {os.path.basename(img_path)}")
                        except Exception as e:
                            print(f"[OPENAI IMG2IMG] Failed to open {img_path}: {e}")
                
                # If no files could be opened, fall back to text-to-image
                if len(files) == 0:
                    print(f"[OPENAI IMG2IMG] No reference images available, falling back to TEXT-TO-IMAGE")
                else:
                    print(f"[OPENAI IMG2IMG] Total references: {len(files)}")
                    
                if len(files) > 0:
                    try:
                        # Use raw requests library for multipart form-data
                        headers = {
                            "Authorization": f"Bearer {OPENAI_API_KEY}"
                        }
                        data = {
                            'model': 'gpt-image-1',
                            'prompt': vhs_prompt,
                            'n': '1',
                            'size': '1536x1024',
                            'quality': OPENAI_IMG2IMG_QUALITY,  # Configurable: 'low', 'medium', 'high'
                            'input_fidelity': 'high',  # CRITICAL: Stick closer to reference images
                            'moderation': 'low'
                        }
                        
                        response = requests.post(
                            "https://api.openai.com/v1/images/edits",
                            headers=headers,
                            data=data,
                            files=files,
                            timeout=60  # 60 second timeout
                        )
                        
                        if response.status_code != 200:
                            print(f"[OPENAI IMG2IMG] HTTP ERROR {response.status_code}: {response.text}")
                            raise Exception(f"OpenAI API error: {response.status_code}")
                        
                        # Parse JSON response with error handling
                        try:
                            result = response.json()
                        except json.JSONDecodeError as e:
                            print(f"[OPENAI IMG2IMG] Failed to parse JSON response: {e}")
                            raise Exception(f"Invalid JSON response from OpenAI: {e}")
                        
                        # Extract image data with error handling
                        if 'data' not in result or len(result['data']) == 0:
                            print(f"[OPENAI IMG2IMG] No image data in response")
                            raise Exception("OpenAI response missing 'data' field")
                        
                        if 'b64_json' not in result['data'][0]:
                            print(f"[OPENAI IMG2IMG] No b64_json in response data")
                            raise Exception("OpenAI response missing 'b64_json' field")
                        
                        b64_data = result['data'][0]['b64_json']
                        
                        # Decode and save the image with error handling
                        try:
                            img_data = base64.b64decode(b64_data)
                        except Exception as e:
                            print(f"[OPENAI IMG2IMG] Failed to decode base64: {e}")
                            raise Exception(f"Base64 decode error: {e}")
                        
                        try:
                            with open(image_path, "wb") as f:
                                f.write(img_data)
                        except Exception as e:
                            print(f"[OPENAI IMG2IMG] Failed to write image file: {e}")
                            raise Exception(f"File write error: {e}")
                        
                        print(f"[OPENAI IMG2IMG] Edit complete with {len(files)} reference(s)")
                        print(f"[OPENAI IMG2IMG] Image saved to: {image_path}")
                        
                        img2img_success = True
                        _last_image_path = f"/images/{filename}"
                        return (_last_image_path, vhs_prompt, None)  # OpenAI doesn't generate videos
                        
                    except Exception as e:
                        print(f"[OPENAI IMG2IMG] Error during img2img: {e}")
                        print(f"[OPENAI IMG2IMG] Will fall back to TEXT-TO-IMAGE")
                        # Don't re-raise - let it fall through to text-to-image fallback
                        
                    finally:
                        # Close all file handles
                        for field_name, file_tuple in files:
                            try:
                                # file_tuple is (filename, file_obj, mime_type)
                                file_tuple[1].close()
                            except Exception as e:
                                print(f"[OPENAI IMG2IMG] Warning: Failed to close file handle: {e}")
            
            # TEXT-TO-IMAGE MODE - Either img2img failed or no reference images
            if not img2img_success:
                print(f"[OPENAI TEXT2IMG] Generating fresh image")
                print(f"[OPENAI TEXT2IMG] Wrapping prompt with VHS aesthetic instructions...")
                
                # Wrap with VHS styling (same as Gemini)
                vhs_prompt = _build_vhs_prompt(prompt_str, use_img2img=False)
                
                response = client.images.generate(
                    model="gpt-image-1",
                    prompt=vhs_prompt,  # ← Now using VHS-wrapped prompt!
                    n=1,
                    size="1536x1024",  # Landscape (closer to 4:3)
                    quality=OPENAI_IMG2IMG_QUALITY,  # Use same quality as img2img for consistency
                    moderation="low"  # Less restrictive for horror content
                )
                
                # gpt-image-1 always returns b64_json (no URL option)
                b64_data = response.data[0].b64_json
                img_data = base64.b64decode(b64_data)
                
                with open(image_path, "wb") as f:
                    f.write(img_data)
                print(f"[OPENAI TEXT2IMG] Image saved to: {image_path}")
                
            _last_image_path = f"/images/{filename}"
            return (_last_image_path, vhs_prompt, None)  # OpenAI doesn't generate videos
        
        else:
            raise ValueError(f"Unknown IMAGE_PROVIDER: {active_image_provider}. Supported: 'openai', 'gemini', 'veo', 'krea', 'fal'")
        # Skip time extraction - we already set time_of_day in state before generation
        # No need to extract it back from the image we just generated!
        return (f"/images/{filename}", prompt_str, None)
    except Exception as e:
        try:
            safe_error = str(e).encode('ascii', 'replace').decode('ascii')
            print(f"[IMG PROVIDER {ai_provider_manager.get_image_provider()}] Error: {safe_error}")
            import traceback
            traceback.print_exc()
        except UnicodeEncodeError:
            # Windows console can't handle Unicode in traceback - print minimal info
            print(f"[IMG PROVIDER] Error during image generation (traceback contains special characters)")
            print(f"[IMG PROVIDER] Error type: {type(e).__name__}")
        return (None, "", None)

# ───────── image prompt sanitization ─────────────────────────────────────────
def _sanitize_for_image_generation(text: str) -> str:
    """
    Sanitize visual descriptions to avoid content filters while keeping dramatic tension.
    Only affects IMAGE prompts - narrative text stays intact!
    """
    import re
    
    # Explicit gore/violence keywords to soften
    replacements = {
        # Blood and injuries
        r'\b(blood|bleeding|bloody)\b': 'dark stains',
        r'\bgushes?\b': 'flows',
        r'\bsoaked?\b': 'dampened',
        r'\bsoaking\b': 'wetting',
        r'\bbleeding\b': 'injured',
        r'\bhemorrhag(e|ing)\b': 'severe injury',
        
        # Wounds and gore
        r'\bwound(s)?\b': 'injury',
        r'\bgash(es)?\b': 'tear',
        r'\bgaping\b': 'deep',
        r'\bopen wound\b': 'injury',
        r'\brip(s|ped|ping)?\b': 'tear',
        r'\bsever(ed|ing)?\b': 'separated',
        r'\bgore\b': 'injury',
        r'\bgory\b': 'disturbing',
        r'\bviscera\b': 'internal damage',
        r'\bentrails\b': 'remains',
        r'\bguts\b': 'interior',
        
        # Body parts and trauma
        r'\bflesh\b': 'tissue',
        r'\bimpale(d|s|ment)?\b': 'pierce',
        r'\bpiercing through\b': 'penetrating',
        r'\bpuncture(d|s)?\b': 'penetrate',
        r'\bmaul(ed|ing)?\b': 'attacked severely',
        r'\bmutilate(d|s)?\b': 'damaged severely',
        r'\bdismember(ed|ing|ment)?\b': 'torn apart',
        r'\bshredded\b': 'torn',
        r'\bslashed\b': 'cut',
        
        # Explicit damage descriptions
        r'\btearing (through|into)\b': 'damaging',
        r'\bripping through\b': 'tearing into',
        r'\bembedded in\b': 'stuck in',
        r'\blodged in\b': 'caught in',
        r'\bpulled free\b': 'removed',
        r'\byanked out\b': 'extracted',
        
        # Violence and pain
        r'\bagony\b': 'severe pain',
        r'\bscreaming\b': 'crying out',
        r'\bshriek(ing|s)?\b': 'calling out',
        r'\btorture(d)?\b': 'extreme discomfort',
        r'\bthrobbing\b': 'pulsing',
        r'\bburning pain\b': 'intense sensation',
        
        # Body horror
        r'\bbone fragments?\b': 'debris',
        r'\bexposed bone\b': 'structural damage',
        r'\bfractured?\b': 'broken',
        r'\bshattered\b': 'broken badly',
        r'\bcrushed\b': 'compressed',
        r'\bpulp\b': 'mush',
        r'\bmangled\b': 'damaged',
        r'\brotting\b': 'decaying',
        r'\bdecompos(ing|ed)\b': 'deteriorating',
        
        # Death and corpses
        r'\bcorpse(s)?\b': 'remains',
        r'\bdead bod(y|ies)\b': 'remains',
        r'\bcadaver(s)?\b': 'remains',
        r'\bskull(s)?\b': 'cranium',
    }
    
    # Apply replacements (case-insensitive)
    sanitized = text
    for pattern, replacement in replacements.items():
        sanitized = re.sub(pattern, replacement, sanitized, flags=re.IGNORECASE)
    
    # Remove intensifiers that make it more graphic
    sanitized = re.sub(r'\b(violently|brutally|viciously|savagely|horrifically)\s+', '', sanitized, flags=re.IGNORECASE)
    
    # Soften extreme descriptors
    sanitized = re.sub(r'\b(profusely|heavily)\s+(bleeding|damaged)', r'significantly injured', sanitized, flags=re.IGNORECASE)
    
    # Remove redundant graphic details
    sanitized = re.sub(r'\s+stained (with blood|red)', ' stained darkly', sanitized, flags=re.IGNORECASE)
    
    print(f"[SANITIZE] Original length: {len(text)}, Sanitized length: {len(sanitized)}")
    if text != sanitized:
        print(f"[SANITIZE] Changes applied to image prompt (narrative text unchanged)")
    
    return sanitized

# ───────── vision dispatch generator ─────────────────────────────────────────
def _generate_vision_dispatch(narrative_dispatch: str, world_prompt: str = "") -> str:
    prompt = (
        "You are a visual scene writer. Output only the literal, visible scene as you would see it, in first-person present tense.\n\n"
        "Rewrite the following narrative as a first-person, present-tense description of what you see, suitable for a visual scene. "
        "Only describe what is visible. Do not include yourself or any internal thoughts. "
        "Do not show yourself. Do not show the protagonist. Do not show any character from behind. Only show what you see from your own eyes. "
        f"\n\nNARRATIVE DISPATCH: {narrative_dispatch}\n\nWORLD CONTEXT: {world_prompt}"
    )
    # Don't use lore - this is just reformatting narrative to visual description
    result = _ask(prompt, model="gemini", temp=1.0, tokens=100, use_lore=False)
    return result

# ───────── public API (two‑stage) ───────────────────────────────────────────
def _generate_situation_report(current_image: str = None, current_dispatch: str = None, vision_analysis: str = None) -> str:
    """Generate situation report with optional visual context from current frame."""
    if history or current_dispatch:
        # Use provided dispatch or last one from history
        last_dispatch = current_dispatch if current_dispatch else (history[-1]["dispatch"] if history else "")
        
        # Use the updated world state after the simulation tick
        state_data = _load_state()
        world_state = state_data.get("world_prompt", "")
        turn_count = len(history)
        
        major_event_nudge = ""
        if turn_count % 5 == 0 and turn_count > 0:
            major_event_nudge = (
                "\n\nIMPORTANT: This is a turning point. Introduce a major new development, threat, opportunity, or mystery. Shake up the situation in a dramatic way."
            )
        
        prompt = (
            PROMPTS.get("situation_summary_instructions", "Describe what is happening NOW.") +
            f"\n\nWorld State (before current moment):\n{world_state}\n\nNarrative Result of Last Action:\n{last_dispatch}"
        )
        
        if vision_analysis:
            prompt += f"\n\nVisual Reality (what is actually seen):\n{vision_analysis}"
            
        prompt += major_event_nudge + "\n\nDescribe what is happening NOW as a concise 1-2 sentence situation report. This should reconcile the narrative text with the visual reality."
        
        # Don't use lore - this is just a summary with visual grounding
        return _ask(prompt, model="gemini", temp=1.0, tokens=60, image_path=current_image, use_lore=False)
    
    return "You stand on a rocky outcrop overlooking the Horizon facility, situated in the distance, surrounded by the vast red american southwest."

def begin_tick() -> dict:
    from choices import generate_choices  # Local import to avoid circular dependency
    
    state = _load_state()
    # If at intro/prologue, return generate_intro_turn result (with choices)
    if not history or (state.get('last_choice', '') == '' and state.get('world_prompt', '').startswith('You crouch behind a rusted Horizon vehicle')):
        return generate_intro_turn()
    # Generate a world summary (narrative)
    world_summary = state["world_prompt"]
    # Use the last dispatch as the situation report
    situation_report = history[-1]["dispatch"]
    # Use evolution summary if available, otherwise fallback
    interim_messages = state.get("evolution_summary", "World state evolving...")
    loader = interim_messages
    # Generate the narrative update using the narrative prompt
    narrative_update = _world_report()
    # Condense world state for choices
    situation_summary = summarize_world_state(state)
    options = generate_choices(
        client, PROMPTS["player_choice_generation_instructions"],
        situation_report,
        n=3,
        seen_elements=', '.join(state.get('seen_elements', [])[-10:]),  # Last 10 discovered entities
        recent_choices='',
        caption=situation_report,
        image_description='',
        world_prompt=state.get('world_prompt', ''),
        temperature=0.2,
        situation_summary=situation_summary,
        injury_state=', '.join(state.get('injuries', []) or []) or 'none',
    )
    # Remove placeholder/empty choices
    options = [c for c in options if c and c.strip() and c.strip() != '—']
    if not options:
        options = ["Look around", "Move forward", "Wait"]
    while len(options) < 3:
        options.append("")
    return {
        "situation_report": situation_report,  # Return this as a separate field
        "choices": options,
        "interim_loader": loader,
    }

def _extract_time_and_color(image_path: str) -> tuple[str, str]:
    """Extract time of day and color palette (uses cached unified analysis)."""
    result = _vision_analyze_all(image_path)
    return result["time_of_day"], result["color_palette"]

def _generate_random_starting_time() -> str:
    """
    Use LLM to generate a randomized starting time/weather/mood for each game session.
    Format: "6:30pm | weather: clear, warm light | mood: tense anticipation"
    """
    prompt = f"""Generate a starting time/weather/mood for a horror game set in the American Southwest desert at a mysterious facility.

Use EXACTLY this format: "TIME | weather: DESCRIPTION | mood: DESCRIPTION"

Example: "{INITIAL_TIME_OF_DAY}"

Requirements:
- TIME: Evening hours only (6:00pm - 8:00pm range, use exact time like 6:45pm or 7:22pm)
- WEATHER: Desert weather + lighting description (clear/cloudy/dusty/overcast + lighting type)
- MOOD: Horror/suspense mood (2-3 words describing emotional tone)

Generate ONE random variation. Return ONLY the formatted string, no explanation."""
    
    try:
        result = _ask(prompt, model="gemini", temp=1.2, tokens=40, use_lore=False).strip()
        
        # Validate format roughly (has | separators and pm)
        if '|' in result and 'pm' in result.lower() and 'weather:' in result and 'mood:' in result:
            print(f"[INIT] Generated starting time: {result}")
            return result
        else:
            print(f"[INIT] LLM returned invalid format, using default")
            return INITIAL_TIME_OF_DAY
    except Exception as e:
        print(f"[INIT] Error generating time: {e}, using default")
        return INITIAL_TIME_OF_DAY

def extract_scene_elements(*args):
    """Extract key nouns/entities from dispatch, vision, and world state."""
    text = ' '.join([a for a in args if a])
    # Simple noun extraction: words after 'the', 'a', or capitalized words
    words = re.findall(r'\b(?:the|a|an) ([A-Za-z0-9\-]+)|\b([A-Z][a-z0-9\-]+)', text)
    nouns = set()
    for w1, w2 in words:
        if w1: nouns.add(w1.lower())
        if w2: nouns.add(w2.lower())
    # Also add all unique words longer than 3 chars
    for w in re.findall(r'\b\w{4,}\b', text):
        nouns.add(w.lower())
    return nouns

# RENAMED from advance_turn
def _process_turn_background(choice: str, initial_player_action_item_id: int, signal_file_path: Optional[str] = None, source: Optional[str] = None, session_id: str = 'default'):
    """Standalone feed turn — a thin adapter over the canonical two-phase
    session pipeline.

    This runs in the background thread spawned by api_choose, scoped to
    whichever session issued the choice (see the `session_id` argument —
    api_choose passes the requester's session so multiple users' turns never
    cross-write each other's saves). It delegates the actual turn work to
    advance_turn_image_fast + advance_turn_choices_deferred (the same
    pipeline the Discord/session path uses), then translates their return
    dicts into the feed items the standalone UI polls for. It replaces the
    previous ~600-line duplicate orchestration so there is now ONE turn
    implementation.

    Design decisions (previously divergent between the two paths):
      • Death: honor the consequence LLM's player_alive verdict (Phase 1),
        not a second check_player_death call.
      • Image + flipbook: produced inside _gen_image (Phase 1); no separate
        standalone flipbook copy.
      • Risk: every turn now drives the story-escalation backend (threat_level →
        phase → fate roll) via advance_story_dynamics, so the web/SCAN path stops
        running flat at NORMAL. `source` marks how the action was issued; SCAN
        object interactions ("scan_interact"/"scan_move") push risk harder so
        poking the world moves the story forward and raises the stakes.

    NOTE ON CONCURRENCY: this function reads/writes a LOCAL `turn_state` dict
    (always reloaded via _load_state(SID)) rather than the ambient module-
    global `state`, specifically so that a DIFFERENT session's concurrent
    background thread swapping the global mirror can't corrupt this turn's
    own in-flight computation. `_sync_ambient_state` only mirrors our result
    into the global when this session is still the active one (see its
    docstring) — the on-disk save is always the source of truth regardless.
    """
    if signal_file_path:
        try:
            Path(signal_file_path).write_text("THREAD SPAWNED AND WROTE TO FILE")
        except Exception:
            pass

    time.sleep(0.75)  # brief pacing delay so the client renders the action first

    SID = session_id
    # Serialize this session's ENTIRE turn pipeline (dispatch generation,
    # world evolution kickoff, choice generation) against every OTHER
    # session's turn/reset processing. advance_turn_image_fast and
    # advance_turn_choices_deferred still read/write the module-global
    # `state`/`history` mirrors internally (see their docstrings) — TURN_LOCK
    # is what actually prevents two sessions' turns from interleaving on
    # those shared mirrors, matching the documented "only one turn processed
    # by the engine at a time" model. Scene-image generation is spawned as a
    # separate background thread (_spawn_scene_image_async) that acquires
    # TURN_LOCK itself once this function returns, so it isn't held here.
    with TURN_LOCK:
        try:
            # ── STORY ESCALATION + FATE ──
            # Drive the risk backend BEFORE the consequence generates, so the rising
            # phase feeds the prompt (advance_turn_image_fast reads current_phase for
            # its grounding block) and the rolled fate colors THIS turn's outcome.
            # SCAN interactions ("scan_interact"/"scan_move") are deliberate meddling
            # with the world, so they push threat harder and bias fate toward risk —
            # exactly the "interacting moves the story forward + raises stakes" goal.
            is_interaction = source in ("scan_interact", "scan_move")
            risk_boost = 2 if is_interaction else 0
            dyn = advance_story_dynamics(session_id=SID, risk_boost=risk_boost)
            turn_fate = dyn.get("fate", "NORMAL")
            print(f"[TURN DYNAMICS] source={source} phase={dyn.get('phase')} "
                  f"threat={dyn.get('threat_level')} fate={turn_fate} escalated={dyn.get('escalated')}", flush=True)

            # ── PHASE 1: consequence dispatch only (fast text; NO image, async evolve) ──
            # Image and world-evolution run in the background so narrative + choices
            # return fast.
            p1 = advance_turn_image_fast(choice, fate=turn_fate, is_timeout_penalty=False, session_id=SID, skip_image=True, skip_evolve=True, interaction=is_interaction, local_only=True)
            turn_state = _load_state(SID)
            _sync_ambient_state(turn_state, SID)

            dispatch_text = (p1.get("dispatch") or "").strip() or "The situation evolves..."
            consequence_img_url = None  # streamed in asynchronously below
            vision_dispatch_text = p1.get("vision_dispatch", "")
            player_alive = turn_state.get("player_state", {}).get("alive", True)

            turn_items: List[Dict[str, Any]] = [
                create_feed_item(type="narrative_event", content=dispatch_text, metadata={"source": "dispatch"})
            ]

            # Phase escalation sting: when this turn tipped the story into a higher
            # phase, surface an in-world beat (the client renders `threat_escalation`
            # with its own style + escalation sound) so the player FEELS the stakes
            # climb. Skip on the death turn — the game-over beat carries that weight.
            if player_alive and dyn.get("escalated"):
                beat = _phase_escalation_beat(dyn.get("phase", ""))
                if beat:
                    turn_items.append(create_feed_item(
                        type="threat_escalation", content=beat,
                        metadata={"phase": dyn.get("phase"), "threat_level": dyn.get("threat_level")},
                    ))

            # Item pickup detection (feed notification; inventory itself is in state).
            _inventory_update = None
            try:
                from items import detect_item_pickups, add_items_to_inventory, ITEMS
                current_inventory = turn_state.get("inventory", [])
                picked_up = detect_item_pickups(dispatch_text, current_inventory)
                if picked_up:
                    updated_inventory, didnt_fit = add_items_to_inventory(current_inventory, picked_up)
                    _inventory_update = updated_inventory
                    names = [ITEMS[i]["display"] for i in picked_up if i in ITEMS]
                    if names:
                        turn_items.append(create_feed_item(type="inventory_pickup", content=f"\U0001F392 **Picked up:** {', '.join(names)}"))
                    overflow = [ITEMS[i]["display"] for i in (didnt_fit or []) if i in ITEMS]
                    if overflow:
                        turn_items.append(create_feed_item(type="inventory_full", content=f"\u26A0\uFE0F Inventory full! Couldn't pick up: {', '.join(overflow)}"))
            except Exception as e_pick:
                log_error(f"Error detecting item pickups: {e_pick}")

            # Atomic append (reload inside the lock) so concurrent realtime writers
            # (/api/observe, scene-image, reground) can't clobber these feed items —
            # the bug that made SCAN/realtime turns "freeze" (narrative + choices
            # vanished from feed_log and the client polled an empty feed forever).
            with WORLD_STATE_LOCK:
                st = _load_state(SID)
                if _inventory_update is not None:
                    st["inventory"] = _inventory_update
                _feed_extend(st, turn_items)
                _save_state(st, SID)
                turn_state = st
                _sync_ambient_state(st, SID)

            # ── DEATH: single mechanism — the Phase 1 player_alive verdict ──
            if not player_alive:
                # Still render the death moment's scene image — it streams in
                # behind the "YOU DIED" overlay and lands on the tape.
                _spawn_scene_image_async(
                    caption=vision_dispatch_text or dispatch_text,
                    dispatch=dispatch_text,
                    choice=choice,
                    frame_idx=int(p1.get("frame_idx", 1)),
                    world_prompt=turn_state.get("world_prompt", ""),
                    hard_transition=bool(p1.get("hard_transition", False)),
                    session_id=SID,
                )
                game_over_item = create_feed_item(type="game_over", content="You have succumbed to the horrors. The transmission ends.")
                game_over_choices = _structure_choices_for_feed(
                    ["Restart Simulation"], "GAME OVER",
                    image_url=turn_state.get("current_image_url"),
                )
                with WORLD_STATE_LOCK:
                    st = _load_state(SID)
                    _feed_append(st, game_over_item)
                    _feed_append(st, game_over_choices)
                    st["turn_count"] = int(st.get("turn_count", 0)) + 1
                    _save_state(st, SID)
                    turn_state = st
                    _sync_ambient_state(st, SID)
                return

            # Remember the pre-turn image so the async vision reground below can tell
            # when THIS turn's new guide image has actually landed.
            _prev_image_url = turn_state.get("current_image_url")

            # ── Stream the scene image asynchronously (FAST — never block choices on
            # the slow render, or the turn/ceremony stalls on the last step waiting
            # for the prompt). The choices are re-grounded on the rendered image via
            # a NON-BLOCKING vision pass below, so they stop going stale without
            # gating the whole turn on image + vision. ──
            _spawn_scene_image_async(
                caption=vision_dispatch_text or dispatch_text,
                dispatch=dispatch_text,
                choice=choice,
                frame_idx=int(p1.get("frame_idx", 1)),
                world_prompt=turn_state.get("world_prompt", ""),
                hard_transition=bool(p1.get("hard_transition", False)),
                session_id=SID,
            )

            # ── PHASE 2: resolve the turn's choices promptly. ──
            # Prefer the provisional choices produced in the SAME LLM call as the
            # consequence (Phase 1) — when present and usable this skips the
            # situation-report + choice-generation round-trips entirely, so the
            # turn's text pipeline is a single LLM call. The client's vision
            # reground below still refines them against the rendered frame. When
            # the consequence call didn't return usable options, this falls back
            # to full choice generation automatically.
            p2 = advance_turn_choices_deferred(
                None, dispatch_text, vision_dispatch_text, choice,
                "", p1.get("hard_transition", False), SID, local_only=True,
                pregenerated_choices=p1.get("provisional_choices") or [],
            )
            turn_state = _load_state(SID)
            _sync_ambient_state(turn_state, SID)

            next_choices = [c for c in (p2.get("choices") or []) if c and c.strip() and c.strip() != "\u2014"]
            prompt_item = _structure_choices_for_feed(
                next_choices, "What do you do next?",
                turn_state.get("current_image_url"),
            )

            with WORLD_STATE_LOCK:
                st = _load_state(SID)
                _feed_append(st, prompt_item)
                st["turn_count"] = int(st.get("turn_count", 0)) + 1
                MAX_FEED_LOG_ITEMS = 100  # keep feed_log manageable
                if len(st.get("feed_log", [])) > MAX_FEED_LOG_ITEMS:
                    st["feed_log"] = st["feed_log"][-MAX_FEED_LOG_ITEMS:]
                _save_state(st, SID)
                turn_state = st
                _sync_ambient_state(st, SID)

            # ── Vision reground (non-blocking): once THIS turn's guide image has
            # rendered, regenerate the choices from what's ACTUALLY on screen and
            # push them as a choices_revised item the client swaps in place — so the
            # options reflect the real scene instead of the dispatch text. Bounded +
            # guarded; if the image never lands it simply gives up. This is what
            # makes choices image-derived WITHOUT stalling the turn on the render. ──
            try:
                _spawn_scene_choices_reground(prompt_item.get("id"), _prev_image_url, SID)
            except Exception as _e_reground:
                log_error(f"[REGROUND] spawn failed: {_e_reground}")

        except Exception as e_critical:
            log_error(f"Critical unhandled error in _process_turn_background thread: {e_critical}")
            logging.exception("CRITICAL EXCEPTION in _process_turn_background thread top level:")
            try:
                critical_error_item = create_feed_item(type="error_event", content=f"System critical error during turn processing: {e_critical}")
                with WORLD_STATE_LOCK:
                    current_state_for_err = _load_state(SID)
                    _feed_append(current_state_for_err, critical_error_item)
                    _save_state(current_state_for_err, SID)
                    _sync_ambient_state(current_state_for_err, SID)
            except Exception as e_final_log:
                log_error(f"Could not even log critical error to feed_log: {e_final_log}")
    # No return value: runs in a thread; persists its session's state to disk
    # and mirrors it into the ambient global only if still the active session.


def _structure_choices_for_feed(choice_texts: List[str], prompt_text: str = "What do you do next?", image_url: Optional[str] = None) -> Dict[str, Any]:
    global state 
    structured_choices_list = []
    if not choice_texts: 
        choice_texts = ["Observe your surroundings.", "Think about your next move.", "Stay alert."]
        prompt_text = "The path ahead is unclear. Consider your options carefully."

    for i, text in enumerate(choice_texts):
        if text and text.strip() and text.strip() != "—":
            action_id = _slug(text) if _slug(text) else f"auto_choice_{get_next_feed_item_id()}_{i}"
            structured_choices_list.append({"text": text, "action_id": action_id})
    
    if not structured_choices_list: 
        structured_choices_list = [
            {"text": "Look around.", "action_id": "fallback_look_around"},
            {"text": "Wait and see.", "action_id": "fallback_wait_see"}
        ]
    
    return create_feed_item(
        type="player_choice_prompt",
        content=prompt_text,
        choices=structured_choices_list,
        image_url=image_url
    )

def _generate_and_append_scene_image(caption: str, dispatch: str, choice: str, frame_idx: int,
                                     world_prompt: str, hard_transition: bool = False,
                                     session_id: str = 'default', write_history: bool = True):
    """Generate the scene image, append the scene_image feed item, and update
    session state. Returns {'img_path','web_url','image_prompt','render_prompt'}
    or None on failure / when image generation is disabled.

    write_history controls the history img2img-continuity write:
      • True  — write the image into THIS turn's history entry ourselves. Used by
                the async/death/intro paths, where choices were produced in
                PARALLEL, so we must wait for their entry to land and target it.
      • False — the caller sequences the choice phase AFTER us and hands our
                img_path to advance_turn_choices_deferred, which writes the image
                into the entry it appends — so there's no race and nothing to do.
    """
    global state, history
    if not WORLD_IMAGE_ENABLED:
        return None
    # Serialize scene-image generation system-wide with TURN_LOCK so it never
    # runs concurrently with a turn's own state mutations (and, for the bot
    # path, so two channels' renders don't collide on the module globals).
    # We read this session's history from disk into a LOCAL and hand it to
    # _gen_image via history_ref, so a concurrent different-session request
    # swapping the module-global `history` can't feed the wrong session's
    # frames into this render. Single-session latency is unaffected: a turn
    # only ever has one image generation in flight (see _process_turn_background).
    with TURN_LOCK:
        try:
            # Before the (large) still write, make sure the persistent disk has
            # room. If it's low, this sweeps stale/regenerable data across all
            # sessions so the render — and the state save that follows — don't
            # fail on a full disk. No-op when there's healthy headroom.
            _ensure_disk_headroom()
            local_history = _load_history(session_id)
            result = _gen_image(
                caption=caption or dispatch,
                mode="normal",
                choice=choice,
                dispatch=dispatch,
                world_prompt=world_prompt,
                hard_transition=hard_transition,
                frame_idx=frame_idx,
                session_id=session_id,
                history_ref=local_history,
            )
            img_path = result[0] if result else None
            # Two different prompts for two different renderers:
            #   • image_prompt (result[1]) — the diffusion prompt used for the
            #     Gemini still; kept in state for debugging only.
            #   • render_prompt — a clean, video-model-appropriate scene bible
            #     used to STEER Reactor/Helios (see build_realtime_prompt). This
            #     is what we hand the standalone client via metadata.prompt.
            # Built up front (independent of the still) so realtime can keep
            # steering the world off the prompt even when the still was blocked.
            image_prompt = result[1] if (result and len(result) > 1) else ""
            render_base = build_realtime_base(visual_scene=caption, narrative=dispatch)
            render_prompt = build_realtime_prompt(
                visual_scene=caption, narrative=dispatch, choice=choice
            )

            if not img_path:
                # Image was blocked (content filter) or generation failed. Do NOT go
                # silent: emitting nothing leaves the turn's ceremony parked on the
                # guide-image step (a spinner that never resolves) and the scene
                # visually frozen — which reads as "the game broke" (exactly the
                # report: switching back to stills, "selecting an action didn't
                # change scenes"). Emit a scene beat WITHOUT an image so the client
                # still resolves the turn, realtime keeps steering off the prompt,
                # and stills mode can surface a "signal lost" glitch instead of a
                # dead UI. We deliberately keep the LAST good still as
                # current_image_url so a fallback to stills still shows a real frame.
                blocked_item = create_feed_item(
                    type="scene_image",
                    content="",
                    image_url=None,
                    metadata={
                        "prompt": render_prompt,
                        "base": render_base,
                        "hard_transition": bool(hard_transition),
                        "blocked": True,
                    },
                )
                with WORLD_STATE_LOCK:
                    st = _load_state(session_id)
                    st['current_render_prompt'] = render_prompt
                    st['current_render_base'] = render_base
                    _feed_append(st, blocked_item)
                    _save_state(st, session_id)
                    _sync_ambient_state(st, session_id)
                print(f"[SCENE IMG] image blocked/failed for {session_id}; emitted signal-lost beat "
                      f"(turn resolves, realtime keeps steering)", flush=True)
                return None

            web = _to_web_image_url(img_path, session_id)
            item = create_feed_item(
                type="scene_image",
                content="",
                image_url=web,
                metadata={
                    "prompt": render_prompt,
                    # 'base' (style + scene, no action) lets the client re-steer
                    # instantly with the next action before the turn resolves.
                    "base": render_base,
                    "hard_transition": bool(hard_transition),
                },
            )
            with WORLD_STATE_LOCK:
                st = _load_state(session_id)
                st['current_image_url'] = web
                st['current_image_prompt'] = image_prompt
                st['current_render_prompt'] = render_prompt
                st['current_render_base'] = render_base
                _feed_append(st, item)
                _save_state(st, session_id)
                _sync_ambient_state(st, session_id)

            if write_history:
                # Write the absolute image path back into history so the NEXT turn's
                # img2img can use this frame for continuity.
                if int(frame_idx) == 0:
                    # INTRO (frame 0): the opening image never goes through
                    # advance_turn_choices_deferred, so no history entry exists for it
                    # yet. Seed history[0] ourselves — otherwise the FIRST player turn
                    # finds no reference frame and falls back to text-to-image, so the
                    # very first image never passes itself to the second via img2img
                    # (breaking visual continuity right at the start of the game).
                    hist = _load_history(session_id)
                    if not hist:
                        intro_entry = {
                            "choice":            choice,
                            "dispatch":          dispatch,
                            # vision_dispatch is REQUIRED by the img2img reference gate
                            # in _gen_image (it skips entries missing it).
                            "vision_dispatch":   caption or dispatch,
                            "world_prompt":      world_prompt,
                            "image":             img_path,
                            "image_url":         img_path,
                            "analysis_image":    img_path,
                            # Original hi-fi guide still, preserved so a later realtime
                            # frame overwriting 'image' still leaves it as a secondary
                            # img2img quality reference (see _ingest_realtime_frame).
                            "guide_image":       img_path,
                            "image_prompt":      image_prompt,
                            "hard_transition":   bool(hard_transition),
                        }
                        hist.append(intro_entry)
                        _save_history(hist, session_id)
                        _sync_ambient_history(hist, session_id)
                        print(f"[SCENE IMG] intro frame seeded into history[0] for img2img continuity: {img_path}")
                    else:
                        # A player turn already appended before the (async) intro image
                        # landed. Seeding now would reorder history, so skip — the intro
                        # still shows in the feed, we just don't chain off it this game.
                        print(f"[SCENE IMG] intro image arrived after a turn was appended (len={len(hist)}); skipping history seed to avoid reordering")
                else:
                    # Regular turn: frame_idx == len(history)+1 at turn start, so after
                    # the parallel choice append this turn's entry is at index
                    # frame_idx-1; wait briefly for it.
                    hist = _load_history(session_id)
                    target_idx = int(frame_idx) - 1
                    for _ in range(40):  # up to ~10s: append is quick, image is slow
                        if 0 <= target_idx < len(hist):
                            break
                        time.sleep(0.25)
                        hist = _load_history(session_id)
                    if 0 <= target_idx < len(hist):
                        hist[target_idx]["image"] = img_path
                        hist[target_idx]["image_url"] = img_path
                        # Record the original high-fidelity guide still separately so it
                        # survives even after a realtime frame later overwrites 'image'
                        # (see _ingest_realtime_frame) — it stays available as a
                        # secondary img2img quality reference.
                        hist[target_idx]["guide_image"] = img_path
                        _save_history(hist, session_id)
                        _sync_ambient_history(hist, session_id)
                    elif hist:
                        print(f"[SCENE IMG] WARN: turn entry idx {target_idx} absent (len={len(hist)}); writing hist[-1]")
                        hist[-1]["image"] = img_path
                        hist[-1]["image_url"] = img_path
                        hist[-1]["guide_image"] = img_path
                        _save_history(hist, session_id)
                        _sync_ambient_history(hist, session_id)
            print(f"[SCENE IMG] scene appended for {session_id}: {web}", flush=True)
            return {"img_path": img_path, "web_url": web,
                    "image_prompt": image_prompt, "render_prompt": render_prompt}
        except Exception as e:
            log_error(f"[SCENE IMG] failed: {e}")
            # Do NOT swallow the turn's scene entirely. On the realtime (reactor)
            # path the client only re-steers the live world when a scene_image
            # feed item arrives with metadata.prompt; if an exception here emits
            # nothing, the video never receives the new scene and appears to
            # "just stop" (exactly the report: entered the dark hatch and the
            # video never started). Emit a best-effort blocked beat so realtime
            # keeps steering off the prompt and the turn's ceremony still
            # resolves. Built from the always-available turn inputs since
            # render_prompt/render_base may not have been reached before the
            # throw. Guarded so a secondary failure can't mask the original.
            try:
                fb_base = build_realtime_base(visual_scene=caption, narrative=dispatch)
                fb_prompt = build_realtime_prompt(
                    visual_scene=caption, narrative=dispatch, choice=choice
                )
                fallback_item = create_feed_item(
                    type="scene_image",
                    content="",
                    image_url=None,
                    metadata={
                        "prompt": fb_prompt,
                        "base": fb_base,
                        "hard_transition": bool(hard_transition),
                        "blocked": True,
                    },
                )
                with WORLD_STATE_LOCK:
                    st = _load_state(session_id)
                    st['current_render_prompt'] = fb_prompt
                    st['current_render_base'] = fb_base
                    _feed_append(st, fallback_item)
                    _save_state(st, session_id)
                    _sync_ambient_state(st, session_id)
                print(f"[SCENE IMG] exception recovery for {session_id}: emitted signal-lost beat "
                      f"so realtime keeps steering", flush=True)
            except Exception as e2:
                log_error(f"[SCENE IMG] failed to emit recovery beat: {e2}")
            return None


def _spawn_scene_image_async(caption: str, dispatch: str, choice: str, frame_idx: int,
                             world_prompt: str, hard_transition: bool = False,
                             session_id: str = 'default'):
    """Generate a scene image OFF the turn's critical path (death + intro paths,
    where choices are produced in parallel). The browser polls /api/feed and
    streams the scene in. For the main turn loop we instead generate the image
    synchronously and derive the choices from it — see _process_turn_background.
    """
    if not WORLD_IMAGE_ENABLED:
        return
    threading.Thread(
        target=_generate_and_append_scene_image,
        kwargs=dict(caption=caption, dispatch=dispatch, choice=choice, frame_idx=frame_idx,
                    world_prompt=world_prompt, hard_transition=hard_transition,
                    session_id=session_id, write_history=True),
        daemon=True,
    ).start()


def _evolve_world_async(session_id: str, consequence_summary: str, vision_dispatch: str):
    """Run world evolution off the turn's critical path. evolve_world_state is
    read-only; we merge only the world fields under lock so a concurrent feed
    write is never clobbered. Affects the next turn's world_prompt."""
    def _worker():
        global state
        try:
            from evolve_prompt_file import evolve_world_state
            hist = _load_history(session_id)
            evolution_result = evolve_world_state(
                hist, consequence_summary,
                state_file=str(_get_state_path(session_id)),
                vision_description=vision_dispatch,
            )
            if not evolution_result:
                return
            with WORLD_STATE_LOCK:
                st = _load_state(session_id)
                for k in ("world_prompt", "evolution_summary", "recent_events", "seen_elements"):
                    if k in evolution_result:
                        st[k] = evolution_result[k]
                _save_state(st, session_id)
                _sync_ambient_state(st, session_id)
            print(f"[ASYNC EVOLVE] world updated for {session_id}", flush=True)
        except Exception as e:
            log_error(f"[ASYNC EVOLVE] failed: {e}")

    threading.Thread(target=_worker, daemon=True).start()


# Ensure generate_intro_turn_feed_items is defined AFTER _structure_choices_for_feed
def generate_intro_turn_feed_items(session_id: str = 'default', new_state: Optional[dict] = None) -> List[Dict[str, Any]]:
    """Build the intro feed items for a fresh session.

    `new_state` — the LOCAL (not module-global) fresh-state dict the caller
    just built for `session_id`. Reading/writing it directly (instead of the
    ambient `state` global) matters here because this function makes a slow
    synchronous LLM call (generate_choices); if it touched the ambient
    global, a concurrent request resetting a DIFFERENT session could swap
    that global out from under us mid-call, corrupting either session's
    dict. Falls back to the ambient global only for legacy direct callers
    that don't pass one.
    """
    from choices import generate_choices # Local import
    global state
    if new_state is None:
        new_state = state
    intro_items = [] # This list will be returned
    
    initial_narrative_content = (
        "1993. Golden hour bleeds across the Four Corners desert. You are Jason Fleece, "
        "photojournalist, crouched at the perimeter of Horizon Industries' quarantined "
        "facility \u2014 the last place the missing were ever seen. Your camcorder hums against "
        "your palm. Red dust drifts over the chain-link fence ahead. Whatever they buried "
        "out here, you came to film it."
    )
    narrative_item = create_feed_item(type="narrative_event", content=initial_narrative_content)
    intro_items.append(narrative_item)

    # Choices are grounded on text (no image needed), so the intro returns fast.
    initial_choice_texts = []
    try:
        initial_choice_texts = generate_choices(
            client=client,
            prompt_tmpl=PROMPTS["player_choice_generation_instructions"],
            last_dispatch=initial_narrative_content,
            world_prompt=new_state.get("world_prompt", "System Online."),
            image_description="Golden-hour desert at the perimeter fence of the Horizon facility; red mesas, chain-link fence, abandoned vehicles.",
            situation_summary="You are crouched at the fence line of the quarantined Horizon facility as the sun drops. This is your way in.",
            n=3
        )
    except Exception as e_choices:
        log_error(f"Error generating initial choices: {e_choices}")
        initial_choice_texts = ["Vault the perimeter fence", "Crouch low and scan the facility", "Photograph the abandoned vehicles"]

    choice_prompt_text = "The fence line waits. What's your first move?"
    choices_item = _structure_choices_for_feed(initial_choice_texts, choice_prompt_text, None)
    intro_items.append(choices_item)
    new_state["choices"] = choices_item['choices']

    # The intro scene image renders in the background and streams into the feed,
    # so reset returns immediately instead of blocking on image generation.
    _spawn_scene_image_async(
        caption=initial_narrative_content,
        dispatch=initial_narrative_content,
        choice="Initialize Simulation",
        frame_idx=0,
        world_prompt=new_state.get("world_prompt", "Initialization sequence."),
        session_id=session_id,
    )

    return intro_items
    
# --- Internal Reset Logic --- (Moved from api_reset for reusability)
def _perform_game_reset() -> List[Dict[str, Any]]:
    global state, history, _last_image_path, _next_feed_item_id
    # Resolve THIS request's session id straight from Flask's request object
    # (see _resolve_request_session_id's docstring) rather than the shared
    # get_active_session_id() global, which a concurrent different-session
    # request can swap out from under us between _session_scoped's swap-in
    # and this line running. Previously this (and the _load_state()/
    # _save_state(state) calls below) defaulted to 'default' unconditionally,
    # so /api/reset for ANY session silently read + overwrote the 'default'
    # session's save file instead of the caller's own session — the
    # multi-user "New Game" flow never actually created an isolated instance.
    # Serialize the ENTIRE reset (state build, intro-turn LLM call, save)
    # against every OTHER session's turn/reset processing — see TURN_LOCK's
    # definition and _process_turn_background's matching wrapper. Without
    # this, a concurrent /api/choose or /api/reset for a DIFFERENT session
    # could interleave with generate_intro_turn_feed_items()'s calls into
    # the deep turn pipeline (which still touch the module-global `state`/
    # `history` mirrors internally) and corrupt either session's data.
    with TURN_LOCK:
        SID = _resolve_request_session_id()
        logging.info(f"_perform_game_reset: ENTER session='{SID}'. Initial global state object id: {id(state)}")
    
        # Reset state variables by loading a fresh copy and then clearing/setting specifics
        current_state_at_reset_start = _load_state(SID) 
        logging.info(f"_perform_game_reset: After _load_state. Loaded state id: {id(current_state_at_reset_start)}. Its feed_log (len {len(current_state_at_reset_start.get('feed_log',[]))}) id: {id(current_state_at_reset_start.get('feed_log')) if current_state_at_reset_start.get('feed_log') is not None else 'None'}")
    
        # Generate random starting time/weather/mood for this session
        starting_time = _generate_random_starting_time()
    
        # Explicitly create a new dictionary for the state to ensure no shared references for critical parts.
        # This is a LOCAL variable — NOT assigned to the ambient `state` global
        # yet — because generate_intro_turn_feed_items() below makes a slow
        # synchronous LLM call. If we reassigned the global here, a concurrent
        # /api/reset for a DIFFERENT session could swap it out (or itself get
        # clobbered) mid-call; see generate_intro_turn_feed_items's docstring.
        new_state = {
            "world_prompt": PROMPTS.get("world_initial_state", "Default world starting point."),
            "current_phase": "normal",
            "chaos_level": 0,
            "last_choice": "",
            "last_saved": datetime.now(timezone.utc).isoformat(),
            "seen_elements": [],
            "player_state": {"alive": True},
            "feed_log": [],  # Explicitly a new empty list
            "current_image_url": None,
            "choices": [],
            "choices_metadata": {},
            "turn_count": 0,
            "interim_index": 0,
            "in_combat": False,
            "threat_level": 0,
            "time_of_day": starting_time
            # Add any other essential keys that should be present from a fresh state
        }
        logging.info(f"_perform_game_reset: New state object created. New state id: {id(new_state)}. Its feed_log (len {len(new_state['feed_log'])}) id: {id(new_state['feed_log'])}")

        _last_image_path = None

        # NOTE: Do NOT reset _next_feed_item_id here. Feed item ids must stay
        # monotonically increasing across resets — a connected client tracks the
        # last id it has seen (and dedups by id), so restarting the counter made
        # a fresh intro reuse low ids that the client had already rendered, which
        # got deduped away → "Reset does nothing". Keeping the counter monotonic
        # guarantees the new intro's items are always newer than anything seen.

        new_history: List[dict] = []
        hist_path_for_sid = _get_history_path(SID)
        if hist_path_for_sid.exists():
            try:
                hist_path_for_sid.write_text("[]", encoding='utf-8') # Clear history file
                logging.info("_perform_game_reset: history.json cleared.")
            except Exception as e_hist_clear:
                logging.error(f"_perform_game_reset: Error clearing history.json: {e_hist_clear}")
        else:
            logging.info("_perform_game_reset: history.json does not exist, no need to clear.")

        initial_items = generate_intro_turn_feed_items(SID, new_state)
        logging.info(f"_perform_game_reset: initial_items from generate_intro_turn_feed_items (IDs): {[item['id'] for item in initial_items if item]}")
    
        new_state['feed_log'].extend(initial_items) # Add to the new state's new feed_log
        logging.info(f"_perform_game_reset: state['feed_log'] before _save_state (IDs): {[item['id'] for item in new_state['feed_log'] if item]}")
    
        _save_state(new_state, SID) # Save the completely new state to THIS session's file
        # Mirror into the ambient globals only if SID is still the active
        # session (see _sync_ambient_state) — same reasoning applies to `history`.
        if get_active_session_id() == SID:
            state = new_state
            history = new_history
        logging.info(f"_perform_game_reset: Game reset complete. {len(initial_items)} initial items generated and saved.")
        return initial_items

def api_reset():
    # Resolve THIS request's session id straight from Flask's request object
    # (see _resolve_request_session_id's docstring) rather than reading the
    # ambient `state` global below — _perform_game_reset() already does the
    # same resolution internally and is now serialized system-wide via
    # TURN_LOCK, but the fallback/error paths here used to read/write the
    # shared `state` mirror directly (and `_save_state(state)` with no
    # session_id at all, which defaults to 'default'!), so a concurrent
    # different-session reset could make this handler report or persist the
    # WRONG session's data.
    SID = _resolve_request_session_id()
    logging.info(f"api_reset: POST request received for session='{SID}'.")
    try:
        initial_items = _perform_game_reset()
        if not initial_items:
            logging.warning("api_reset: _perform_game_reset returned no items, but this might be okay if feed_log is now populated by it.")
            # Fallback to checking THIS session's on-disk feed_log if initial_items is empty from return
            initial_items = _load_state(SID).get('feed_log', [])

        # Fallback: If still no player_choice_prompt, add a default
        has_choice_prompt = any(item.get('type') == 'player_choice_prompt' for item in initial_items)
        if not has_choice_prompt:
            logging.error("api_reset: No player_choice_prompt found in initial_items. Adding fallback.")
            fallback_item = {
                "id": 999999,
                "type": "player_choice_prompt",
                "content": "The system is online. Your journey begins now. What is your first action?",
                "choices": [
                    {"text": "Look around", "action_id": "look_around"},
                    {"text": "Move forward", "action_id": "move_forward"},
                    {"text": "Wait", "action_id": "wait"}
                ]
            }
            initial_items.append(fallback_item)
        logging.info(f"api_reset: Returning {len(initial_items)} items. First item ID (if any): {initial_items[0]['id'] if initial_items else 'N/A'}")
        return jsonify(initial_items)
    except Exception as e:
        log_error(f"Critical error in api_reset: {e}")
        logging.exception("Exception in api_reset:")
        # In case of an error, ensure a valid JSON response is sent.
        # _perform_game_reset might have partially modified state, or failed before creating items.
        error_feed_item = create_feed_item(type="error_event", content=f"Failed to reset game: {str(e)}")
        # Attempt to log this error to THIS session's on-disk feed_log.
        try:
            with WORLD_STATE_LOCK:
                err_state = _load_state(SID)
                err_state.setdefault('feed_log', []).append(error_feed_item)
                _save_state(err_state, SID)
                _sync_ambient_state(err_state, SID)
        except Exception as e_log:
            log_error(f"Could not save error item to feed_log during api_reset error handling: {e_log}")
        return jsonify([error_feed_item]), 500


def api_revive():
    """Bring a dead player back to life on the CURRENT run.

    Intended to be called by the coin-op layer AFTER a payment has been
    verified server-side (see coinop.verify_and_redeem). This endpoint is
    itself agnostic to payment — it simply flips the death state, restores a
    partial health, and appends a short narrative beat + a fresh choice
    prompt so the player can keep going.

    Idempotent: calling api_revive on an already-alive player is a no-op
    that returns the (empty) list of newly appended items, so a duplicate
    return-URL redemption cannot double-revive or corrupt state.
    """
    SID = _resolve_request_session_id()
    appended: List[Dict[str, Any]] = []
    try:
        with WORLD_STATE_LOCK:
            st = _load_state(SID)
            ps = st.get("player_state") or {}
            # NOTE: no "already alive → no-op" guard here. In realtime
            # sessions the client's peripheral-vignette DangerSystem can
            # kill the player CLIENT-side (see standalone.js DangerSystem.
            # die() and its enterGameOver call) before the server's Phase-1
            # verdict has flipped alive→False. If we no-op'd on
            # alive==True, a legitimately-paid coin-op continue in that
            # window would silently do nothing and the death overlay would
            # never dismiss. Idempotency for double-charges lives one
            # layer up in coinop.verify_and_redeem's redeemed-set — this
            # function trusts its caller.
            ps["alive"] = True
            if isinstance(ps.get("health"), (int, float)):
                # Best-effort partial heal when the run tracks a numeric HP;
                # if it doesn't, the flag flip is all that's needed.
                ps["health"] = max(int(ps["health"]), 25)
            ps["revived"] = True
            ps["revive_count"] = int(ps.get("revive_count", 0)) + 1
            st["player_state"] = ps

            # Bookkeeping so downstream logic (e.g. tape / analytics) can
            # tell a run apart from one that was never dead.
            revives = int(st.get("continues_used", 0)) + 1
            st["continues_used"] = revives

            # Narrative beat: a short, arcade-native line. Kept intentionally
            # separate from the standard 'narrative_event' so the client can
            # style it (CRT flicker, coin-drop cue) if it wants to. Falls
            # back to 'narrative_event' rendering in older clients.
            revive_item = create_feed_item(
                type="continue_used",
                content=(
                    "\U0001FA99 A coin drops. The transmission stutters back to life. "
                    "You are on your feet, breathing hard. Keep moving."
                ),
            )
            _feed_append(st, revive_item)
            appended.append(revive_item)

            # Give the player something to do next. We use a set of
            # deliberately safe, universally applicable choices so the
            # revive never lands the player in a broken decision point; the
            # next real turn will regenerate choices from vision as normal.
            prompt_item = _structure_choices_for_feed(
                [
                    "Look around carefully.",
                    "Move forward, cautiously.",
                    "Check yourself for injuries.",
                    "Wait a moment and listen.",
                ],
                "What do you do next?",
                image_url=st.get("current_image_url"),
            )
            _feed_append(st, prompt_item)
            appended.append(prompt_item)

            _save_state(st, SID)
            _sync_ambient_state(st, SID)

        logging.info(f"api_revive: revived session='{SID}' (revive_count={ps.get('revive_count')})")
        return jsonify(appended)
    except Exception as e:  # noqa: BLE001
        log_error(f"api_revive: error reviving session='{SID}': {e}")
        logging.exception("Exception in api_revive:")
        err = create_feed_item(
            type="error_event",
            content=f"Continue failed: {e}. The run remains ended.",
        )
        try:
            with WORLD_STATE_LOCK:
                st = _load_state(SID)
                _feed_append(st, err)
                _save_state(st, SID)
        except Exception:
            pass
        return jsonify([err]), 500


def api_feed():
    # Resolve straight from Flask's (thread-local) request object rather than
    # reading the ambient `state` global directly. `/api/feed` is polled
    # continuously by every connected browser, so with multiple sessions live
    # at once this handler runs concurrently across many threads — reading
    # the shared `state` mirror here raced with OTHER sessions' concurrent
    # session_context() swaps (a poll for session A could land squarely on
    # top of session B's swap-in and read B's feed_log instead of A's).
    # Loading directly from THIS session's disk file every poll costs one
    # small JSON read and closes that race entirely.
    session_id = _resolve_request_session_id()
    since_id_str = request.args.get('since_id')
    items_to_return = []
    with WORLD_STATE_LOCK: # Ensure thread-safe access to the on-disk feed_log
        st = _load_state(session_id)
        feed_log = st.get('feed_log', [])
        if since_id_str:
            try:
                since_id = int(since_id_str)
                items_to_return = [item for item in feed_log if item.get('id', 0) > since_id]
            except ValueError:
                log_error(f"/api/feed: Invalid since_id '{since_id_str}'. Returning full feed.")
                items_to_return = list(feed_log) # Return a copy
        else:
            items_to_return = list(feed_log) # Return a copy of the full feed log
    return jsonify(items_to_return)

def api_choose():
    global state
    try:
        data = request.get_json()
        if not data or 'choice' not in data:
            return jsonify({"error": "Missing 'choice' in request body"}), 400
        
        player_choice_text = data['choice']
        context_item_id = data.get('context_item_id') # Optional, for context
        # Session id for THIS request, resolved straight from Flask's request
        # object (see _resolve_request_session_id's docstring for why this
        # must NOT be get_active_session_id() — that shared global can be
        # swapped by a concurrent, different-session request between when
        # _session_scoped entered session_context() and this line running).
        # Previously this defaulted to 'default' and was never actually used
        # below — every /api/choose call silently operated on the 'default'
        # session's saved state regardless of which session the caller
        # asked for. Using it here (and threading it into the background
        # thread below) is what makes each session's turns land in ITS OWN
        # save file instead of everyone's turns landing in 'default'.
        session_id = _resolve_request_session_id()
        # How the action was issued. SCAN object interactions ("scan_interact"/
        # "scan_move") drive the story-escalation backend harder (see
        # _process_turn_background) so poking the world moves the plot + raises risk.
        action_source = data.get('source')

        if DEBUG_MODE: print(f"[DEBUG] api_choose received choice: '{player_choice_text}', context_id: {context_item_id}. Current state ID: {id(state)}", flush=True)

        # ACT-TIME FRAME CAPTURE: if the client sent the frame the player was
        # actually looking at in the live world model, ingest it BEFORE the turn
        # spawns. It becomes the latest history entry's img2img reference (the
        # realtime state we're interacting with), while the original high-fidelity
        # guide still is preserved as a secondary quality anchor. Must run before
        # the background thread appends this turn's new history entry, so it lands
        # on the CURRENT scene (history[-1]). Best-effort: a bad frame is ignored.
        act_frame_b64 = data.get('act_frame')
        if act_frame_b64:
            try:
                if _ingest_realtime_frame(act_frame_b64, session_id):
                    if DEBUG_MODE: print(f"[DEBUG] api_choose ingested act-time world-model frame for img2img.", flush=True)
            except Exception as e_frame:
                log_error(f"api_choose: failed to ingest act frame: {e_frame}")

        # 1. Immediately create and log the Player Action
        player_action_item = create_feed_item(
            type="player_action", 
            content=f"{player_choice_text}", # Display the choice text directly
            metadata={"raw_choice": player_choice_text, "context_id": context_item_id}
        )
        # Atomic read-modify-write: reload the freshest state INSIDE the lock
        # before appending, so a concurrent writer (the realtime /api/observe
        # fast-path, scene-image thread, or a reground worker) can't have its
        # save clobbered — and ours can't be clobbered by a stale snapshot.
        with WORLD_STATE_LOCK:
            st = _load_state(session_id)
            _feed_append(st, player_action_item)
            st['last_choice'] = player_choice_text
            _save_state(st, session_id)
            # Guarded mirror update (see _sync_ambient_state's docstring): a
            # blind `state = st` here raced with OTHER sessions' concurrent
            # /api/choose calls — this request thread's `st` could overwrite
            # the ambient mirror right before a DIFFERENT session's request
            # exits session_context() and (seeing its own session still
            # "active") persists that stale mirror over ITS OWN save file.
            # That was a real, reproducible cross-session leak under
            # concurrent load; _sync_ambient_state's active-session check
            # closes it.
            _sync_ambient_state(st, session_id)
        if DEBUG_MODE: print(f"[DEBUG] api_choose - Player action item ID {player_action_item['id']} logged. Starting background thread for _process_turn_background.", flush=True)

        # 2. Start background processing for the rest of the turn
        # Pass the ID of the player_action_item so the background thread can link its logs if needed.
        
        temp_signal_file = ROOT / f"thread_signal_{player_action_item['id']}.tmp" # Use the correct ID here
        
        try:
            thread = threading.Thread(target=_process_turn_background, args=(player_choice_text, player_action_item['id'], str(temp_signal_file)), kwargs={"source": action_source, "session_id": session_id})
            # thread.daemon = True # Allow main program to exit even if threads are running. Temporarily commenting out for testing.
            thread.start()            
            # Check for signal from thread via temp file
            time.sleep(0.2) # Give thread a moment to write
            signal_received = False
            if temp_signal_file.exists():
                try:
                    content = temp_signal_file.read_text().strip()
                    if content == "THREAD SPAWNED AND WROTE TO FILE":
                        signal_received = True
                    temp_signal_file.unlink() # Clean up
                except Exception as e_file_read:
                    if DEBUG_MODE: print(f"[DEBUG] api_choose - Error reading/deleting signal file: {e_file_read}", flush=True)
            
            if DEBUG_MODE: print(f"[DEBUG] api_choose - Signal from thread via file: {'RECEIVED' if signal_received else 'NOT RECEIVED'}", flush=True)
            if not signal_received and thread.is_alive():
                 if DEBUG_MODE: print(f"[DEBUG] api_choose - Thread is alive but signal file not as expected.", flush=True)
            elif not signal_received and not thread.is_alive():
                 if DEBUG_MODE: print(f"[DEBUG] api_choose - Thread is NOT alive and signal file not as expected.", flush=True)

        except Exception as e_thread_start:
            print(f"CRITICAL DEBUG PRINT: api_choose - ERROR STARTING THREAD: {e_thread_start}", flush=True)
            # Optionally re-raise or handle specifically if needed, for now just printing
            raise # Re-raise to see if it gets caught by the broader handler or stops the test

        # 3. Return only the player_action_item immediately to the client
        if DEBUG_MODE: print(f"[DEBUG] api_choose - Returning player_action_item (ID: {player_action_item['id']}) to client immediately.", flush=True)
        return jsonify([player_action_item])

    except Exception as e:
        log_error(f"Error in api_choose: {e}")
        logging.exception("Exception in api_choose:")
        # Create a generic error item to return
        error_item = create_feed_item(type="error_event", content=f"Server error processing choice: {str(e)}")
        # Attempt to log this error to the feed_log if state is available
        try:
            with WORLD_STATE_LOCK:
                err_session_id = _resolve_request_session_id()
                err_state = _load_state(err_session_id)
                err_state.setdefault('feed_log', []).append(error_item)
                _save_state(err_state, err_session_id)
                _sync_ambient_state(err_state, err_session_id)
        except Exception as e_log:
            log_error(f"Could not save error item to feed_log during api_choose error handling: {e_log}")
        return jsonify([error_item]), 500


def _prune_observed_frames(img_dir, keep: int = 12):
    """Cap the number of transient realtime-capture frames on disk.

    Every realtime turn writes one or more `observed_*.png` scratch frames
    (act-time capture in /api/choose + the post-choice perception frame in
    /api/observe). These are ONLY ever used as an img2img reference for the
    immediately-following turn (reference collection in _gen_image reaches back
    at most ~2 history entries), yet nothing pruned them mid-session — they only
    got wiped on /api/reset. On the now-persistent 1GB disk (see
    RENDER_STORAGE_LIMITATION.md) they pile up across a play session until the
    disk fills, at which point new image writes start failing and the scene can
    no longer render a fresh frame — i.e. "I can only play for a short time".

    Keep the most-recent `keep` frames (generous margin over the ~2-turn
    reference window) and delete the rest. Best-effort: any failure is ignored
    so this can never break a turn.
    """
    try:
        from pathlib import Path as _P
        d = _P(img_dir)
        if not d.exists():
            return
        frames = sorted(d.glob("observed_*.png"), key=lambda p: p.stat().st_mtime)
        excess = len(frames) - max(0, int(keep))
        for old in frames[:excess]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception as _e:
        log_error(f"_prune_observed_frames failed: {_e}")


# ── Persistent-disk headroom management ────────────────────────────────────
# The sessions/ tree lives on a bounded persistent disk on Render (1GB by
# default — see RENDER_STORAGE_LIMITATION.md / render.yaml). Per-session pruning
# (_prune_observed_frames) bounds ONE session's scratch frames, but across many
# sessions + generated stills + tapes/films the disk can still fill up, and once
# it's full EVERY image/state write fails (the scene can't render, saves are
# lost). When we detect the disk is low, sweep stale/regenerable data across ALL
# sessions — oldest first — until a healthy margin is restored.
_DISK_MIN_FREE_FRACTION = 0.12          # keep ≥12% of the disk free
_DISK_MIN_FREE_BYTES = 150 * 1024 * 1024  # …and never below a 150MB floor
_DISK_SWEEP_MIN_INTERVAL_S = 20         # throttle the (FS-walking) sweep
_DISK_KEEP_IMAGES_PER_SESSION = 12      # protect the newest N images/session
_last_disk_sweep_ts = 0.0
_disk_sweep_lock = threading.Lock()


def _disk_free_status(path):
    """Return (ok, usage, need_bytes). ok=True when free space is healthy.
    On any error, reports ok=True so disk checks can never break a turn."""
    import shutil as _sh
    try:
        usage = _sh.disk_usage(str(path))
        need = max(_DISK_MIN_FREE_BYTES, int(usage.total * _DISK_MIN_FREE_FRACTION))
        return (usage.free >= need, usage, need)
    except Exception:
        return (True, None, 0)


def _ensure_disk_headroom(force: bool = False):
    """Free space on the persistent sessions disk when it's running low.

    Deletes stale, REGENERABLE data across every session, oldest-first, until a
    healthy free-space margin is recovered. Deliberately conservative about what
    it will remove:

      • NEVER deletes session state — state.json / history.json / meta.json — or
        the analytics ledger (anything under sessions/_analytics, or any *.db).
      • PROTECTS the newest _DISK_KEEP_IMAGES_PER_SESSION images in each
        session so img2img continuity and the on-screen scene survive.
      • Everything else under sessions/ (old stills, observed_*/photo/
        investigation scratch frames, _small downsamples, tapes, films/videos)
        is fair game, removed oldest-first.

    Throttled (won't walk the tree more than once per _DISK_SWEEP_MIN_INTERVAL_S)
    and fully best-effort: any failure is swallowed so it can never break a turn.
    """
    global _last_disk_sweep_ts
    sessions_root = ROOT / "sessions"
    ok, usage, need = _disk_free_status(sessions_root)
    if ok and not force:
        return
    if not _disk_sweep_lock.acquire(blocking=False):
        return  # another thread is already sweeping
    try:
        # Re-check under the lock — the other waiter may have already freed space.
        ok, usage, need = _disk_free_status(sessions_root)
        if usage is None:
            return
        now = time.time()
        if ok and not force:
            return
        if not force and (now - _last_disk_sweep_ts) < _DISK_SWEEP_MIN_INTERVAL_S:
            return
        _last_disk_sweep_ts = now
        if not sessions_root.exists():
            return

        # Protect the newest N images per session (continuity + current scene).
        protected = set()
        try:
            for sess_dir in sessions_root.iterdir():
                if not sess_dir.is_dir() or sess_dir.name == "_analytics":
                    continue
                img_dir = sess_dir / "images"
                if not img_dir.exists():
                    continue
                imgs = sorted(
                    (p for p in img_dir.glob("*") if p.is_file()),
                    key=lambda p: p.stat().st_mtime, reverse=True,
                )
                for p in imgs[:_DISK_KEEP_IMAGES_PER_SESSION]:
                    try:
                        protected.add(p.resolve())
                    except Exception:
                        pass
        except Exception:
            pass

        def _is_protected(p):
            try:
                if p.resolve() in protected:
                    return True
            except Exception:
                pass
            if p.name in ("state.json", "history.json", "meta.json"):
                return True
            if p.suffix == ".db" or "_analytics" in p.parts:
                return True
            # Companion portraits are a persistent roster (see _record_companion)
            # meant to be reused across scenes for a continuing story — never
            # sweep them. Prop images (e.g. prop_jeep.png) are the same idea
            # for durable objects (see _record_prop).
            if p.name.startswith("companion_") or p.name.startswith("prop_"):
                return True
            return False

        # How much to free (with a little extra so we don't immediately re-trip).
        to_free = int(max(0, need - usage.free) * 1.25)

        candidates = []
        for p in sessions_root.rglob("*"):
            try:
                if not p.is_file() or _is_protected(p):
                    continue
                candidates.append((p.stat().st_mtime, p.stat().st_size, p))
            except Exception:
                continue
        candidates.sort(key=lambda t: t[0])  # oldest first

        freed = 0
        deleted = 0
        for _mtime, size, p in candidates:
            if freed >= to_free:
                break
            try:
                p.unlink()
                freed += size
                deleted += 1
            except Exception:
                continue
        if deleted:
            print(f"[DISK] headroom sweep freed ~{freed // (1024 * 1024)}MB "
                  f"({deleted} stale files) to keep the sessions disk under "
                  f"{int((1 - _DISK_MIN_FREE_FRACTION) * 100)}% full", flush=True)
    except Exception as _e:
        log_error(f"_ensure_disk_headroom failed: {_e}")
    finally:
        try:
            _disk_sweep_lock.release()
        except Exception:
            pass


def _ingest_realtime_frame(frame_b64: str, session_id: str = 'default'):
    """Ingest a frame captured from the live world-model video.

    Decodes the base64 frame, saves it, and makes it the current scene image +
    the latest history entry's img2img reference — so the NEXT turn's img2img
    evolves from what the world model actually rendered (the realtime state the
    player is interacting with) instead of a stale guide still.

    Crucially, the ORIGINAL high-fidelity guide still is preserved on the history
    entry under 'guide_image' (set once, never clobbered). That lets _gen_image
    pass BOTH references to img2img: the live frame for spatial/state continuity,
    and the guide still for image quality.

    Shared by /api/choose (act-time capture) and /api/observe (post-choice
    perception loop). Returns (fpath, web_url) or None on a bad/too-small frame.
    """
    import base64 as _b64, re as _re, time as _time
    from pathlib import Path as _Path
    global state, history
    if not frame_b64:
        return None
    m = _re.match(r'^data:image/[^;]+;base64,(.*)$', frame_b64, _re.DOTALL)
    raw = m.group(1) if m else frame_b64
    try:
        img_bytes = _b64.b64decode(raw)
    except Exception:
        return None
    if len(img_bytes) < 512:
        return None

    img_dir = _Path(_get_image_dir(session_id))
    img_dir.mkdir(parents=True, exist_ok=True)
    fname = f"observed_{int(_time.time() * 1000)}.png"
    fpath = img_dir / fname
    fpath.write_bytes(img_bytes)
    web = _to_web_image_url(fname, session_id)

    # Bound the transient-frame footprint so a long session can't fill the disk
    # (which would make every subsequent image write fail — see the helper's
    # docstring). Runs AFTER writing this frame so the newest one is retained.
    _prune_observed_frames(img_dir)
    # And, if the shared persistent disk is genuinely low (many sessions / stale
    # tapes+films), sweep old regenerable data across ALL sessions. Throttled and
    # a no-op while there's healthy headroom, so this is cheap on the hot path.
    _ensure_disk_headroom()

    with WORLD_STATE_LOCK:
        st = _load_state(session_id)
        st['current_image_url'] = web
        hist = _load_history(session_id)
        if hist:
            # Preserve the original high-fidelity guide still ONCE, before we
            # point 'image' at the live frame, so it survives as a secondary
            # img2img quality reference (see _gen_image dual-reference logic).
            if not hist[-1].get('guide_image') and hist[-1].get('image'):
                hist[-1]['guide_image'] = hist[-1]['image']
            hist[-1]['image'] = str(fpath)
            hist[-1]['image_url'] = str(fpath)
            _save_history(hist, session_id)
            if get_active_session_id() == session_id:
                history = hist
        _save_state(st, session_id)
        _sync_ambient_state(st, session_id)

    return (str(fpath), web)


def api_observe():
    """Vision for the realtime renderer — close the perception loop.

    The player watches the Helios video, which drifts from the Gemini still the
    simulation is grounded on, so narrative/choices stop matching the screen.
    This accepts the ACTUAL frame currently on screen (captured client-side from
    the video) and feeds it back into the simulation:

      • saves it as the current scene image and overwrites the latest history
        frame + its vision, so the NEXT turn's consequence, img2img reference,
        and narrative track what the player actually saw (anti-drift);
      • regenerates the current choices from what's literally visible in the
        video and returns them for the client to swap in place.

    Loop: video -> vision -> simulation -> prompt (+seed) -> video.
    """
    global state, history
    try:
        data = request.get_json(silent=True) or {}
        frame_b64 = data.get('frame')
        session_id = data.get('session_id', 'default')
        prompt_id = data.get('prompt_id')
        if not frame_b64:
            return jsonify({"error": "missing frame"}), 400
        # FAST PATH (no LLM on the request): make the actual video frame the
        # current scene image + the latest history reference (preserving the
        # original guide still as a secondary quality ref), so the NEXT turn's
        # img2img composition follows the video. Returns immediately.
        ingested = _ingest_realtime_frame(frame_b64, session_id)
        if not ingested:
            return jsonify({"error": "bad frame"}), 400
        fpath, web = ingested

        # SLOW PATH (off the request thread): analyze what's actually on screen
        # and regenerate the live choices to match. Bounded so it can never hang
        # the app; delivers revised choices via a 'choices_revised' feed item.
        _spawn_observe_reground(str(fpath), web, session_id, prompt_id)

        return jsonify({"ok": True, "image_url": web})
    except Exception as e:
        import traceback as _tb
        log_error(f"[OBSERVE] failed: {e}")
        _tb.print_exc()
        return jsonify({"error": str(e)}), 500


def api_detect():
    """Realtime object recognition for the SCAN tool.

    Accepts the frame currently on screen (a JPEG data URL captured client-side
    from the live world-model video) and returns the prominent, interactable
    things visible in it plus WHERE they are, so the standalone UI can float
    "starfield" tags over the live scene where each object actually sits — and
    let the player poke any of them.

    Request JSON:  {"frame": "data:image/jpeg;base64,..."}
    Response JSON: {"objects": [{"label", "cx", "cy", "w", "h"}, ...]}
    Coordinates are normalized 0..1 (cx/cy = box center, w/h = box size).

    This is a stateless, read-only perception call: unlike /api/observe it does
    NOT mutate world state, history, or choices — it just names what's on screen.
    """
    import base64 as _b64, re as _re
    try:
        data = request.get_json(silent=True) or {}
        frame_b64 = data.get('frame')
        session_id = data.get('session_id', 'default')
        if not frame_b64:
            return jsonify({"error": "missing frame"}), 400
        # Pull the MIME type off the data URL so we can pass it straight to
        # Gemini without any guesswork or filename-based inference.
        mime_match = _re.match(r'^data:(image/[^;]+);base64,(.*)$', frame_b64, _re.DOTALL)
        if mime_match:
            mime_type = mime_match.group(1)
            raw = mime_match.group(2)
        else:
            mime_type = "image/jpeg"
            raw = frame_b64
        try:
            img_bytes = _b64.b64decode(raw)
        except Exception:
            return jsonify({"error": "bad frame encoding"}), 400
        if len(img_bytes) < 512:
            return jsonify({"error": "frame too small"}), 400

        # Ground the detector with the exact prompt the world model was steered
        # with for this frame. Pulled from live state so it tracks the current
        # turn without any client changes; falls back gracefully to no prior
        # if the session has no state yet (opening frame, intro, tests).
        scene_prompt = ""
        try:
            _st = get_state(session_id) or {}
            scene_prompt = str(_st.get('current_image_prompt') or "")
        except Exception:
            scene_prompt = ""

        # Detection runs on the bytes we already have in RAM — no disk write,
        # no re-read, no second base64 encode. The old scratch-file path was
        # both slower and a source of contention when SCAN + PHOTO detects
        # fired against the same session at once.
        objects = _detect_objects(
            image_bytes=img_bytes,
            mime_type=mime_type,
            scene_prompt=scene_prompt,
        )
        return jsonify({"objects": objects or []})
    except Exception as e:
        import traceback as _tb
        log_error(f"[DETECT] failed: {e}")
        _tb.print_exc()
        return jsonify({"error": str(e), "objects": []}), 500


def api_danger():
    """Realtime danger grading for the peripheral-vignette / health system.

    Accepts the frame currently on screen (a JPEG data URL captured client-side
    from the live world-model video) and returns a single ordinal threat level
    for that frame. The client fires this at ~1 Hz while the realtime renderer
    is live; the returned level drives the client's danger state machine (SAFE
    → WARNING → HURTING) which pulses the red peripheral vignette and drains
    health when danger persists.

    Request JSON:  {"frame": "data:image/jpeg;base64,..."}
    Response JSON: {"level": 0|1|2,
                    "reason": "<8 words",
                    "direction": "left"|"right"|"top"|"bottom"|"center"|null,
                    "threat_cx": float|null,
                    "threat_cy": float|null}

    Stateless and read-only, like /api/detect: never mutates world state,
    history, or choices. Degrades to `level: 0` (safe) on ANY failure — the
    danger loop must not be able to hurt the player because vision hiccuped.
    """
    import base64 as _b64, re as _re
    safe = {"level": 0, "reason": "", "direction": None,
            "threat_cx": None, "threat_cy": None}
    try:
        data = request.get_json(silent=True) or {}
        frame_b64 = data.get('frame')
        session_id = data.get('session_id', 'default')
        if not frame_b64:
            return jsonify({"error": "missing frame", **safe}), 400
        mime_match = _re.match(r'^data:(image/[^;]+);base64,(.*)$', frame_b64, _re.DOTALL)
        if mime_match:
            mime_type = mime_match.group(1)
            raw = mime_match.group(2)
        else:
            mime_type = "image/jpeg"
            raw = frame_b64
        try:
            img_bytes = _b64.b64decode(raw)
        except Exception:
            return jsonify({"error": "bad frame encoding", **safe}), 400
        if len(img_bytes) < 512:
            # A tiny/black frame is almost certainly the freeze buffer or a
            # blackout gap — grade it as safe so the vignette doesn't flash
            # on transitions.
            return jsonify(safe)

        # Ground the danger call with the current scene prompt, same as
        # /api/detect. Helps the model disambiguate "guard with rifle
        # aimed at you" from "guard idling behind a desk".
        scene_prompt = ""
        try:
            _st = get_state(session_id) or {}
            scene_prompt = str(_st.get('current_image_prompt') or "")
        except Exception:
            scene_prompt = ""

        result = _perceive_danger(
            image_bytes=img_bytes,
            mime_type=mime_type,
            scene_prompt=scene_prompt,
        )
        return jsonify(result if isinstance(result, dict) else safe)
    except Exception as e:
        import traceback as _tb
        log_error(f"[DANGER] failed: {e}")
        _tb.print_exc()
        # Return 200 with a safe reading — the client's loop should not treat
        # a server error as danger. It'll ease back toward SAFE on its own.
        return jsonify({"error": str(e), **safe})


def api_photo():
    """Appraise a photograph the player just captured (the reward loop).

    Accepts the captured crop (a JPEG data URL made client-side from the live
    frame or the current still) and returns an evidence-style appraisal: the
    notable things in frame, each with an interest rating and a terse reason,
    plus a caption and mood. The standalone UI prints this as a "receipt" that
    reveals item-by-item and scores each line toward the run's EVIDENCE total.

    Request JSON:  {"frame": "data:image/jpeg;base64,..."}
    Response JSON: {"items": [{"label", "interest", "note"}], "caption", "mood"}

    Stateless and read-only, like /api/detect: it never mutates world state,
    history, or choices — it only reads what the photo shows. Degrades to an
    empty appraisal (never an error) when vision is unavailable, so the client
    can always render a graceful receipt.
    """
    import base64 as _b64, re as _re
    from pathlib import Path as _Path
    try:
        data = request.get_json(silent=True) or {}
        frame_b64 = data.get('frame')
        session_id = data.get('session_id', 'default')
        if not frame_b64:
            return jsonify({"error": "missing frame"}), 400
        m = _re.match(r'^data:image/[^;]+;base64,(.*)$', frame_b64, _re.DOTALL)
        raw = m.group(1) if m else frame_b64
        try:
            img_bytes = _b64.b64decode(raw)
        except Exception:
            return jsonify({"error": "bad frame encoding"}), 400
        if len(img_bytes) < 512:
            return jsonify({"error": "frame too small"}), 400

        img_dir = _Path(_get_image_dir(session_id))
        img_dir.mkdir(parents=True, exist_ok=True)
        # Reuse a single scratch file per session, like the SCAN grab: these are
        # disposable perception frames, not canonical stills.
        fpath = img_dir / "photo_frame.jpg"
        fpath.write_bytes(img_bytes)

        appraisal = _appraise_photo(str(fpath))
        if not isinstance(appraisal, dict):
            appraisal = {"items": [], "caption": "", "mood": ""}
        return jsonify(appraisal)
    except Exception as e:
        import traceback as _tb
        log_error(f"[PHOTO] failed: {e}")
        _tb.print_exc()
        return jsonify({"error": str(e), "items": [], "caption": "", "mood": ""}), 500


# ═══════════════════════════════════════════════════════════════════
# TALK — converse with a person / character / thing that speaks
#
# The SCAN tool now classifies whether each detected thing can hold a
# conversation (``speaks``). When the player picks TALK on such a subject we
# open a dialogue that is AWARE of the story: who the player is, the premise,
# the current phase/chaos/location, and the last few beats. This awareness is
# assembled once here and reused two ways:
#   • text mode (always available) — an LLM roleplays the subject in-world.
#   • voice mode (when ElevenLabs is configured) — the same briefing is handed
#     to an ElevenLabs Conversational AI agent as prompt overrides + dynamic
#     variables, so you can literally speak to the character.
# Both are stateless/read-only: talking never mutates world state or history.
# ═══════════════════════════════════════════════════════════════════

def _story_premise() -> str:
    """The high-level premise the whole simulation runs on (first paragraph of
    the world-setup prompt), used to ground a conversation partner."""
    try:
        raw = (PROMPTS.get("world_initial_state") or "").strip()
        # First paragraph is the evocative premise; the rest is model direction.
        premise = raw.split("\n\n")[0].strip()
        return premise[:600]
    except Exception:
        return "An analog-horror investigation. You are a photojournalist documenting something that should not exist."


# Best-effort in-memory rate limiting for the (LLM/TTS-backed, cost-bearing)
# conversation + narrator endpoints. Per-client + per-bucket minimum interval;
# generous enough not to bother real players, tight enough to blunt abuse. This
# is a single-process guard (fine for the current gunicorn setup); a shared
# store would be needed if scaled horizontally.
_RATE_BUCKETS = {}


def _rate_limited(bucket: str, min_interval: float) -> bool:
    """Return True if this client is calling ``bucket`` faster than
    ``min_interval`` seconds apart (and should be turned away)."""
    try:
        ip = (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "?").split(",")[0].strip()
    except Exception:
        ip = "?"
    key = f"{bucket}:{ip}"
    now = time.time()
    last = _RATE_BUCKETS.get(key, 0)
    # Opportunistic prune so the dict can't grow without bound.
    if len(_RATE_BUCKETS) > 5000:
        cutoff = now - 300
        for k in [k for k, t in _RATE_BUCKETS.items() if t < cutoff]:
            _RATE_BUCKETS.pop(k, None)
    if now - last < min_interval:
        return True
    _RATE_BUCKETS[key] = now
    return False


def _clean_subject_text(value: str, fallback: str, limit: int) -> str:
    """Sanitize client-supplied subject fields before they enter an LLM prompt:
    collapse whitespace/newlines and hard-cap the length (prompt-injection and
    token-burn guard)."""
    s = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    return (s[:limit] or fallback)


def _talk_vision_snapshot(session_id: str = "default") -> dict:
    """Snapshot of what the character being talked to can actually SEE.

    Reads the frame the player is looking at right now
    (``state['current_image_url']``) and returns a compact summary the persona
    prompt can quote — what visible objects share the scene with the subject,
    plus the cached scene description / time of day / palette. This is what
    lets the character reference the lantern in the player's hand, the door
    behind them, the fresh boot prints on the floor — instead of talking
    blind against just a text scene description.

    Always returns a JSON-safe dict; degrades to empty fields when vision is
    disabled, when the current scene isn't on disk yet, or on any failure
    (never raises — TALK stays available even if perception is down).

    Returns:
        {
          "visible":   [{"label","kind","cx","cy","w","h","speaks"}, ...],
          "description": str,        # single sentence, from _vision_analyze_all
          "time_of_day": str,        # e.g. "night", "dusk"
          "image_url":  str | None,  # web path of the frame we analyzed
        }
    """
    empty = {"visible": [], "description": "", "time_of_day": "", "image_url": None}
    if not LLM_ENABLED or not VISION_ENABLED:
        return empty
    try:
        st = _load_state(session_id) or {}
    except Exception:
        st = {}
    image_url = st.get("current_image_url") or None
    if not image_url:
        return empty
    resolved = _resolve_image_path(image_url)
    if not resolved or not resolved.exists():
        return empty

    # Two vision calls but both are cache-friendly: _vision_analyze_all is
    # already cached per image path, so on the common case (TALK opened during
    # a scene we've already analyzed for the turn dispatch) this is free. The
    # detection call is fresh but reuses the PR #89 keep-alive session and
    # deterministic prompt path, so tag output stays stable frame-to-frame.
    description = ""
    time_of_day = ""
    try:
        analysis = _vision_analyze_all(str(resolved)) or {}
        description = (analysis.get("description") or "").strip()
        time_of_day = (analysis.get("time_of_day") or "").strip()
    except Exception:
        pass

    scene_prompt = (st.get("current_image_prompt") or "").strip()
    visible = []
    try:
        visible = _detect_objects(str(resolved), max_items=8,
                                  scene_prompt=scene_prompt) or []
    except Exception:
        visible = []

    return {
        "visible": visible,
        "description": description[:600],
        "time_of_day": time_of_day[:32],
        "image_url": image_url,
    }


def _format_vision_for_persona(subject_label: str, snapshot: dict) -> str:
    """Turn a ``_talk_vision_snapshot`` into a short block for persona_prompt.

    We deliberately avoid dumping raw coordinates; the character narrates the
    world, they don't read a bounding-box table. Instead we tell them what's
    around them, roughly WHERE (left/center/right), and quote the scene
    description verbatim so the model can pick vocabulary from it. Objects
    whose label matches the subject (they'd be talking about themselves) are
    dropped so the persona doesn't say "I can see a figure over there" when
    THEY are the figure.
    """
    if not snapshot:
        return ""
    parts = []
    desc = (snapshot.get("description") or "").strip()
    if desc:
        parts.append(f"WHAT YOU CAN SEE RIGHT NOW: {desc}")
    tod = (snapshot.get("time_of_day") or "").strip()
    if tod:
        parts.append(f"LIGHT: {tod}.")

    subj = (subject_label or "").strip().lower()
    around = []
    for obj in snapshot.get("visible") or []:
        label = str(obj.get("label") or "").strip().lower()
        if not label or label == subj:
            continue
        cx = float(obj.get("cx", 0.5))
        if cx < 0.35:
            where = "off to your left"
        elif cx > 0.65:
            where = "off to your right"
        else:
            where = "in front of you"
        around.append(f"{label} {where}")
        if len(around) >= 6:
            break
    if around:
        parts.append("Also visible in the scene: " + "; ".join(around) + ".")

    if not parts:
        return ""
    return (
        "\n\nDIRECT PERCEPTION (use these to react to what is actually in "
        "the scene right now; never invent objects that aren't listed):\n"
        + "\n".join(parts)
    )


def build_talk_context(subject: dict, session_id: str = "default", opening_override: str = "",
                       include_vision: bool = True) -> dict:
    """Assemble a story-aware briefing for a conversation with ``subject``.

    ``subject`` is the SCAN object the player chose to talk to
    (``{"label", "kind", "speaks"}``). Returns a JSON-safe dict describing the
    subject, the current situation, and a ready-to-use persona prompt + opening
    line. This is the single source of "awareness" shared by the text fallback
    and the ElevenLabs voice agent. ``opening_override`` reuses an existing
    opening line instead of spending an LLM call to regenerate one (used when
    only the VOICE is changing mid-conversation).

    The persona is grounded in the CURRENT visible frame (see
    ``_talk_vision_snapshot``) so the character isn't talking blind — they
    can reference what's actually on screen alongside the story situation.

    ``include_vision=False`` skips the (two Gemini vision calls) perception
    snapshot — used by the portrait endpoint, which only needs subject +
    situation and runs CONCURRENTLY with the session's own build_talk_context,
    so doing the vision work twice would just double the latency for nothing.
    """
    subject = subject or {}
    label = _clean_subject_text(subject.get("label"), "figure", 40)
    kind = _clean_subject_text(subject.get("kind"), "person", 20)

    try:
        st = _load_state(session_id) or {}
    except Exception:
        st = {}
    try:
        hist = _load_history(session_id) or []
    except Exception:
        hist = []

    phase = st.get("current_phase", "normal")
    chaos = st.get("chaos_level", 0)
    turn = st.get("turn_count", 0)
    location = st.get("location", "") or ""
    time_of_day = st.get("time_of_day", "") or ""
    world_prompt = (st.get("world_prompt") or "").strip()

    inventory = []
    try:
        from items import ITEMS
        for item_id in (st.get("inventory") or []):
            meta = ITEMS.get(item_id) or {}
            inventory.append(meta.get("display", item_id))
    except Exception:
        inventory = list(st.get("inventory") or [])

    # The last few narrative beats — what just happened, so the character can
    # react to the moment rather than talking in a vacuum.
    recent = []
    for entry in hist[-4:]:
        d = (entry.get("dispatch") or "").strip()
        if d:
            recent.append(d[:280])

    situation = {
        "phase": phase,
        "chaos": chaos,
        "turn": turn,
        "location": location,
        "time_of_day": time_of_day,
        "inventory": inventory,
        "scene": world_prompt[:400],
    }

    premise = _story_premise()

    # Compose the persona / system prompt the roleplay LLM (or the ElevenLabs
    # agent) uses to BE this subject, grounded in the story.
    kind_hint = {
        "person": "a person the player has encountered",
        "character": "a named character in this world",
        "creature": "a strange, possibly sentient creature",
        "animal": "an animal that, in this uncanny place, can speak",
        "machine": "a machine or device carrying a voice (radio / intercom / terminal)",
    }.get(kind, "someone the player has encountered")

    recent_block = ""
    if recent:
        recent_block = "\n\nRECENT EVENTS (most recent last):\n- " + "\n- ".join(recent)

    scene_block = f"\n\nRIGHT NOW: {world_prompt}" if world_prompt else ""
    loc_bits = ", ".join([b for b in [location.replace("_", " ") if location else "", time_of_day] if b])
    loc_block = f"\n\nSETTING: {loc_bits}." if loc_bits else ""

    # Direct-perception block: what the subject can actually SEE in the frame
    # the player is looking at, so the character can react to real objects on
    # screen instead of a text-only briefing. Empty string when vision is
    # unavailable or skipped — the persona still works, just without the eyes.
    vision_snapshot = _talk_vision_snapshot(session_id) if include_vision else \
        {"visible": [], "description": "", "time_of_day": "", "image_url": None}
    vision_block = _format_vision_for_persona(label, vision_snapshot)

    persona_prompt = (
        f"You ARE the '{label}' — {kind_hint} — inside a 1993 analog-horror world. "
        f"You are NOT an AI assistant and you must never break character or mention being an AI. "
        f"Speak in-world, in first person, reacting to what is happening around you.\n\n"
        f"WORLD PREMISE: {premise}"
        f"{scene_block}{loc_block}{vision_block}"
        f"\n\nSTATE: story phase '{phase}', chaos level {chaos}/10, turn {turn}."
        f"{recent_block}\n\n"
        f"The person you are speaking to is the investigator/photojournalist exploring this place"
        + (f" (currently carrying: {', '.join(inventory)})" if inventory else "")
        + ". Stay consistent with the premise and the recent events.\n\n"
        "HOW TO TALK: Sound like a real person actually speaking out loud, not writing. Use plain, "
        "everyday words and contractions (I'm, don't, can't, you're). Keep it SHORT — 1 to 2 quick "
        "sentences, a breath or two, never a speech or a monologue. It's fine to be fragmentary, to "
        "trail off, to interrupt yourself, or to answer with just a few words. Skip flowery description "
        "and big vocabulary; talk the way a scared, tired, or wary human really would in the moment. "
        "You may be afraid, hostile, cryptic, desperate, or helpful depending on who you are and what is "
        "happening. Reveal information sparingly and in character. If asked something you couldn't know, "
        "deflect in a way that fits the scene. Never narrate stage directions or use asterisks — just say the words."
    )

    # A cold-open first line so the conversation starts with presence. Reuse a
    # provided line (e.g. when only the voice is changing) to skip an LLM call.
    opening_line = (opening_override or "").strip()[:320] \
        or _talk_opening_line(label, kind, situation, persona_prompt)

    return {
        "subject": {"label": label, "kind": kind, "speaks": bool(subject.get("speaks", True))},
        "premise": premise,
        "situation": situation,
        "recent": recent,
        "persona_prompt": persona_prompt,
        "opening_line": opening_line,
        # Vision snapshot rides along so the ElevenLabs agent (via
        # dynamic_variables) and any Live-audio path (via system_instruction)
        # can quote the same "what you can see" list the persona was built on.
        "vision": vision_snapshot,
    }


def _talk_llm_failed(text: str) -> bool:
    """True when an ``_ask`` result is an error/placeholder sentinel rather than
    a genuine in-character line, so TALK can fall back gracefully."""
    t = (text or "").strip().lower()
    if not t:
        return True
    sentinels = (
        "signal interrupted", "the transmission wavers",
        "system communications remain static", "narrative paused",
        "the world holds its breath",
    )
    if any(t.startswith(s) or s in t for s in sentinels):
        return True
    return t.startswith(("i cannot", "i can't", "as an", "i'm sorry", "i am sorry"))


def _talk_opening_line(label: str, kind: str, situation: dict, persona_prompt: str) -> str:
    """The subject's first line — spoken before the player says anything.

    Uses the LLM when available (grounded in the persona prompt); falls back to
    a tense, generic-but-fitting line so the mechanic always opens with voice.
    """
    fallback = {
        "machine": f"[the {label} crackles]… is someone there? Say something.",
        "creature": "…you can see me. Most can't. Why are you still standing there?",
        "animal": "…you hear me, don't you. Don't act like the others.",
    }.get(kind, "You. You shouldn't be here. What do you want?")

    if not LLM_ENABLED:
        return fallback
    try:
        prompt = (
            persona_prompt
            + "\n\nThe investigator has just turned to face you. Say your FIRST line to them — "
            "one short sentence, spoken out loud, in character, reacting to this exact moment. "
            "Keep it terse and human. Output ONLY the spoken words."
        )
        line = _ask(prompt, temp=0.9, tokens=50, use_lore=False)
        line = (line or "").strip().strip('"').strip()
        # Guard against the model narrating, refusing, or erroring out.
        if _talk_llm_failed(line) or len(line) > 320:
            return fallback
        return line
    except Exception:
        return fallback


def build_portrait_prompt(context: dict, img2img: bool = False) -> str:
    """Compose a cinematic medium-shot portrait prompt for a Conversation Moment.

    Reuses the same talk context (subject, scene, time of day) the persona was
    built from, but applies ``CONVERSATION_PORTRAIT_STYLE_ANCHOR`` — a distinct
    lens language from the handheld-camcorder world view — so the cut into
    dialogue reads as a register change.

    ``img2img=True`` phrases it as a reframe of the CURRENT scene (a reference
    frame is supplied): keep the environment/lighting, turn the camera to face
    the character. This makes the portrait read as the next shot in the same
    place instead of a brand-new location.
    """
    context = context or {}
    subject = context.get("subject") or {}
    situation = context.get("situation") or {}
    label = _clean_subject_text(subject.get("label"), "figure", 40)
    kind = _clean_subject_text(subject.get("kind"), "person", 20)
    scene = (situation.get("scene") or "").strip()
    if len(scene) > 220:
        scene = scene[:217].rstrip() + "..."
    tod = (situation.get("time_of_day") or "").strip()
    loc = (situation.get("location") or "").replace("_", " ").strip()

    kind_look = {
        "person": "a wary human figure",
        "character": "a named character from this world",
        "creature": "a strange, possibly inhuman presence with a readable face or visage",
        "animal": "an uncanny animal that meets the camera's gaze",
        "machine": "a machine, radio, intercom, or terminal that somehow feels present / watched",
    }.get(kind, "a figure the investigator has encountered")

    if img2img:
        # A reference frame of the CURRENT environment is supplied — describe the
        # reframe, not a fresh scene. The img2img continuity block in
        # gemini_image_utils handles keeping the room; here we name the subject.
        bits = [
            f"Turn the camera to face the '{label}' — {kind_look} — standing in this same place.",
            "Cinematic medium shot, mid-torso up, the character sharp and clearly lit,",
            "the surrounding environment softly out of focus behind them (shallow depth of field).",
            "Keep the room, lighting, palette, and film grain of the reference exactly.",
        ]
        setting = ", ".join([b for b in [loc, tod] if b])
        if setting:
            bits.append(f"Setting: {setting}.")
        return _sanitize_for_image_generation(" ".join(bits))

    bits = [
        CONVERSATION_PORTRAIT_STYLE_ANCHOR.rstrip(". ") + ".",
        f"Subject: the '{label}' — {kind_look}.",
        "Hold a charged, intimate medium shot; the subject fills the frame.",
    ]
    if scene:
        bits.append(f"Background suggests this place (softly, out of focus): {scene}")
    setting = ", ".join([b for b in [loc, tod] if b])
    if setting:
        bits.append(f"Setting cues: {setting}.")
    bits.append(
        "No text, no UI, no letterbox bars inside the image. Photoreal cinematic still."
    )
    prompt = " ".join(bits)
    return _sanitize_for_image_generation(prompt)


def _companion_slug(label: str) -> str:
    """Filesystem-safe, stable slug for a companion label."""
    s = re.sub(r"[^a-z0-9]+", "_", (label or "").strip().lower()).strip("_")
    return (s or "figure")[:40]


def _persist_companion_image(image_path: str, session_id: str, label: str) -> Optional[str]:
    """Copy a freshly-generated portrait to a STABLE, sweep-protected companion
    file (``companion_<slug>.png``) so it survives as part of the roster and can
    be re-referenced when placing the character into future scenes.

    Returns the web URL of the durable copy, or None on failure.
    """
    try:
        src = _resolve_image_path(image_path)
        if not src or not src.exists():
            return None
        img_dir = Path(_get_image_dir(session_id))
        img_dir.mkdir(parents=True, exist_ok=True)
        slug = _companion_slug(label)
        dst = img_dir / f"companion_{slug}.png"
        import shutil as _shutil
        _shutil.copyfile(str(src), str(dst))
        # Mirror the img2img downsample convention so companion images can be
        # used as img2img references efficiently later.
        try:
            small_src = src.with_name(src.name.replace(".png", "_small.png"))
            if small_src.exists():
                _shutil.copyfile(str(small_src), str(img_dir / f"companion_{slug}_small.png"))
        except Exception:
            pass
        return _to_web_image_url(f"companion_{slug}.png", session_id)
    except Exception as e:
        log_error(f"[COMPANION] persist image failed: {e}")
        return None


def _record_companion(session_id: str, subject: dict, portrait_url: str,
                      prompt: str = "", scene: str = "") -> dict:
    """Upsert a COMPANION into the session roster.

    A companion is a character the player has spoken with, stored WITH their
    cinematic portrait so they can be dropped back into later scenes to build a
    continuing story. Additive metadata on session state (``state.companions``),
    keyed by lowercased label; also links the portrait onto the lightweight
    character-memory record. Returns the companion record (JSON-safe).
    """
    subject = subject or {}
    display_label = re.sub(r"\s+", " ", str(subject.get("label") or "")).strip()[:40]
    label = _clean_subject_text(subject.get("label"), "", 40)
    if not label or not portrait_url:
        return {}
    try:
        st = _load_state(session_id) or {}
    except Exception:
        return {}
    companions = st.get("companions")
    if not isinstance(companions, dict):
        companions = {}
    key = label.lower()
    prev = companions.get(key) if isinstance(companions.get(key), dict) else {}
    turn = st.get("turn_count", 0)
    entry = {
        "label": display_label or label,
        "kind": _clean_subject_text(subject.get("kind"), prev.get("kind") or "person", 20),
        "portrait_url": portrait_url,
        "prompt": (prompt or prev.get("prompt") or "")[:400],
        "scene": (scene or prev.get("scene") or "")[:300],
        "first_seen_turn": prev.get("first_seen_turn", turn),
        "last_seen_turn": turn,
        "seen_count": int(prev.get("seen_count") or 0) + 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    # Preserve the ElevenLabs voice block if it was recorded first (voice +
    # portrait are resolved in parallel from Talk.start).
    if isinstance(prev.get("voice"), dict):
        entry["voice"] = prev["voice"]
    companions[key] = entry
    st["companions"] = companions
    # Link the portrait onto the character-memory record too, so the two views
    # (relationship notes + roster art) stay in sync.
    chars = st.get("characters")
    if isinstance(chars, dict) and isinstance(chars.get(key), dict):
        chars[key]["portrait_url"] = portrait_url
        st["characters"] = chars
    try:
        _save_state(st, session_id)
    except Exception as e:
        log_error(f"[COMPANION] save failed: {e}")
    return entry


def _record_companion_voice(session_id: str, subject: dict, voice: dict) -> None:
    """Store the ElevenLabs voice data for a companion so their voice can be
    reused (by id) OR regenerated from scratch (by description) later.

    ``voice`` is the resolver output plus a couple of fields the caller knows::

        {
          "voice_id":     <str>,   # reuse this exact ElevenLabs voice
          "description":  <str>,   # the Voice Design brief — REGENERATION seed
          "source":       <str>,   # designed / cache / fallback / override / ...
          "status":       <str>,
          "cache_key":    <str|None>,
          "model":        <str>,   # ttv model to regenerate with
          "settings":     <dict|None>,  # tts settings (stability/similarity/...)
        }

    Additive: creates a minimal companion stub if the portrait hasn't landed yet
    (voice + portrait are resolved in parallel), WITHOUT bumping seen_count or
    touching the portrait. Never raises.
    """
    subject = subject or {}
    display_label = re.sub(r"\s+", " ", str(subject.get("label") or "")).strip()[:40]
    label = _clean_subject_text(subject.get("label"), "", 40)
    if not label or not isinstance(voice, dict) or not voice.get("voice_id"):
        return
    try:
        st = _load_state(session_id) or {}
    except Exception:
        return
    companions = st.get("companions")
    if not isinstance(companions, dict):
        companions = {}
    key = label.lower()
    entry = companions.get(key) if isinstance(companions.get(key), dict) else {}
    # Don't downgrade a real designed-voice description to an empty one (e.g. a
    # later preset override): only overwrite the description when we have one.
    new_desc = (voice.get("description") or "").strip()
    voice_block = {
        "voice_id": voice.get("voice_id"),
        "description": new_desc or (entry.get("voice") or {}).get("description", ""),
        "source": voice.get("source") or "",
        "status": voice.get("status") or "",
        "cache_key": voice.get("cache_key"),
        "model": voice.get("model") or "",
        "settings": voice.get("settings") or (entry.get("voice") or {}).get("settings"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    entry = dict(entry)
    entry.setdefault("label", display_label or label)
    entry.setdefault("kind", _clean_subject_text(subject.get("kind"), "person", 20))
    entry["voice"] = voice_block
    companions[key] = entry
    st["companions"] = companions
    try:
        _save_state(st, session_id)
    except Exception as e:
        log_error(f"[COMPANION] voice save failed: {e}")


def _save_portrait_reference(frame_b64: str, session_id: str = "default") -> Optional[str]:
    """Decode a client-captured scene frame (data URL) to a file for img2img.

    Read-only w.r.t. sim state (unlike ``_ingest_realtime_frame``): it just
    persists the frame so ``generate_gemini_img2img`` can anchor the portrait on
    the exact environment the player is looking at. Returns the file path, or
    None on a missing / malformed / too-small frame.
    """
    if not frame_b64:
        return None
    import base64 as _b64, re as _re
    m = _re.match(r'^data:image/[^;]+;base64,(.*)$', frame_b64, _re.DOTALL)
    raw = m.group(1) if m else frame_b64
    try:
        img_bytes = _b64.b64decode(raw)
    except Exception:
        return None
    if len(img_bytes) < 512:
        return None
    try:
        img_dir = Path(_get_image_dir(session_id))
        img_dir.mkdir(parents=True, exist_ok=True)
        fname = f"portrait_ref_{int(time.time() * 1000)}.png"
        fpath = img_dir / fname
        fpath.write_bytes(img_bytes)
        return str(fpath)
    except Exception as e:
        log_error(f"[TALK PORTRAIT] reference save failed: {e}")
        return None


def _portrait_cache_key(session_id: str, label: str, world_prompt: str) -> tuple:
    scene_hash = hashlib.sha1((world_prompt or "").encode("utf-8")).hexdigest()[:16]
    return (session_id or "default", (label or "").strip().lower(), scene_hash)


def api_talk_portrait():
    """Generate (or reuse) a cinematic medium-shot portrait for a TALK subject.

    Request JSON: ``{"subject": {"label","kind","speaks"}, "session_id"?,
                     "reference_image"?: <data-url of the current frame>}``
    Response JSON: ``{"image_url": "/images/...", "cached": bool, "prompt": str,
                      "mode": "img2img"|"text2img"}``
      or ``{"image_url": null, "reason": "..."}`` when generation is unavailable.

    When a ``reference_image`` (the frame the player is looking at) is supplied
    we img2img off it so the character reads as the NEXT shot in the SAME
    environment; otherwise we text2img a standalone cinematic portrait. Either
    way we use the fast single-image path — NOT the heavy turn-coupled
    ``_gen_image_impl`` — and cache per ``(session, subject, scene)``.
    """
    try:
        if _rate_limited("talk_portrait", 1.2):
            return jsonify({"image_url": None, "reason": "slow_down"}), 429
        data = request.get_json(silent=True) or {}
        subject = data.get("subject") or {}
        session_id = data.get("session_id", "default")
        reference_b64 = data.get("reference_image") or ""
        if not isinstance(subject, dict) or not (subject.get("label") or "").strip():
            return jsonify({"error": "missing subject", "image_url": None}), 400

        if not IMAGE_ENABLED:
            return jsonify({"image_url": None, "reason": "image_disabled"})

        # Reuse the same briefing Talk built for the persona, but skip BOTH
        # the opening-line LLM call (opening_override=".") AND the vision
        # snapshot (include_vision=False): the portrait only needs the subject
        # + scene, and it runs concurrently with the session's own
        # build_talk_context, so repeating the two Gemini vision calls here
        # would just double the time-to-portrait for no benefit.
        context = build_talk_context(
            subject, session_id, opening_override=".", include_vision=False,
        )
        if context.get("opening_line") == ".":
            context["opening_line"] = ""

        label = context["subject"]["label"]
        try:
            world_prompt = str((_load_state(session_id) or {}).get("world_prompt") or "")
        except Exception:
            world_prompt = ""
        cache_key = _portrait_cache_key(session_id, label, world_prompt)

        with _PORTRAIT_CACHE_LOCK:
            cached = _PORTRAIT_CACHE.get(cache_key)
        if cached:
            return jsonify({"image_url": cached, "cached": True, "subject": context["subject"]})

        spend = _PORTRAIT_SPEND.get(session_id, 0)
        if spend >= CONVERSATION_PORTRAIT_BUDGET:
            return jsonify({
                "image_url": None,
                "reason": "budget",
                "subject": context["subject"],
            })

        img_dir = _get_image_dir(session_id)
        tod = (context.get("situation") or {}).get("time_of_day") or ""

        # Prefer img2img off the current frame so the portrait is the next shot
        # in the SAME room. Fall back to text2img when no usable frame arrives.
        ref_path = _save_portrait_reference(reference_b64, session_id) if reference_b64 else None
        use_img2img = bool(ref_path)
        prompt = build_portrait_prompt(context, img2img=use_img2img)

        t0 = time.time()
        image_path = None
        gen_mode = "img2img" if use_img2img else "text2img"
        try:
            if use_img2img:
                from gemini_image_utils import generate_gemini_img2img
                image_path = generate_gemini_img2img(
                    prompt=prompt,
                    caption=f"portrait_{label}",
                    reference_image_path=ref_path,
                    # Bigger change than a normal turn: we're reframing the shot
                    # onto a character, not nudging the scene forward.
                    strength=0.6,
                    world_prompt=world_prompt[:400] if world_prompt else None,
                    time_of_day=tod,
                    hd_mode=False,
                    output_dir=Path(img_dir),
                    portrait_mode=True,
                )
            else:
                from gemini_image_utils import generate_with_gemini
                image_path = generate_with_gemini(
                    prompt=prompt,
                    caption=f"portrait_{label}",
                    world_prompt=world_prompt[:400] if world_prompt else None,
                    aspect_ratio="16:9",
                    time_of_day=tod,
                    hd_mode=False,
                    output_dir=Path(img_dir),
                    portrait_mode=True,
                )
        except Exception as gen_err:
            log_error(f"[TALK PORTRAIT] generate failed: {gen_err}")
            try:
                cost_tracker.record_usage(
                    session_id, "image", "gemini", "talk_portrait",
                    operation="talk_portrait", output_units=0, unit_type="images",
                    latency_ms=int((time.time() - t0) * 1000), success=False,
                    error_message=str(gen_err)[:200],
                )
            except Exception:
                pass
            return jsonify({"image_url": None, "reason": "generate_failed",
                            "subject": context["subject"]})
        finally:
            # The reference frame is a throwaway — clean it up so it can't leak
            # into the tape/feed or pile up on the session disk.
            if ref_path:
                try:
                    Path(ref_path).unlink(missing_ok=True)
                    _small = Path(ref_path).with_name(Path(ref_path).name.replace(".png", "_small.png"))
                    if _small.exists():
                        _small.unlink(missing_ok=True)
                except Exception:
                    pass

        web = _to_web_image_url(image_path, session_id)
        latency_ms = int((time.time() - t0) * 1000)
        try:
            cost_tracker.record_usage(
                session_id, "image", "gemini", "talk_portrait",
                operation=f"talk_portrait_{gen_mode}", output_units=1.0 if web else 0,
                unit_type="images", latency_ms=latency_ms, success=bool(web),
                error_message=None if web else "no_image_returned",
            )
        except Exception:
            pass

        if not web:
            return jsonify({"image_url": None, "reason": "no_image",
                            "subject": context["subject"]})

        with _PORTRAIT_CACHE_LOCK:
            _PORTRAIT_CACHE[cache_key] = web
            _PORTRAIT_SPEND[session_id] = spend + 1

        # Store this character as a COMPANION: a durable, sweep-protected copy of
        # their portrait plus roster metadata, so they can be placed back into
        # later scenes for a continuing story. Best-effort — never fail the
        # portrait response over roster bookkeeping.
        companion = None
        try:
            durable_url = _persist_companion_image(image_path, session_id, label) or web
            companion = _record_companion(
                session_id, context["subject"], durable_url,
                prompt=prompt, scene=world_prompt,
            )
        except Exception as comp_err:
            log_error(f"[COMPANION] record from portrait failed: {comp_err}")

        return jsonify({
            "image_url": web,
            "cached": False,
            "mode": gen_mode,
            "prompt": prompt[:400],
            "subject": context["subject"],
            "companion": ({
                "label": companion.get("label"),
                "portrait_url": companion.get("portrait_url"),
                "seen_count": companion.get("seen_count"),
                "first_seen": companion.get("seen_count") == 1,
            } if companion else None),
        })
    except Exception as e:
        log_error(f"[TALK PORTRAIT] failed: {e}")
        return jsonify({"image_url": None, "reason": "error", "error": str(e)}), 500


def _record_character_memory(session_id: str, subject: dict, note: str = "") -> dict:
    """Upsert a lightweight per-character memory record on session state.

    Additive metadata only — does not mutate ``history`` / ``feed_log`` / the
    sim turn. Powers future trust/relationship Moments without requiring a
    full NPC database today.
    """
    subject = subject or {}
    display_label = re.sub(r"\s+", " ", str(subject.get("label") or "")).strip()[:40]
    label = _clean_subject_text(subject.get("label"), "", 40)
    if not label:
        return {}
    try:
        st = _load_state(session_id) or {}
    except Exception:
        return {}
    chars = st.get("characters")
    if not isinstance(chars, dict):
        chars = {}
    key = label.lower()
    entry = chars.get(key) if isinstance(chars.get(key), dict) else {}
    first_met = entry.get("first_met_turn")
    if first_met is None:
        first_met = st.get("turn_count", 0)
    notes = list(entry.get("notes") or [])
    clean_note = (note or "").strip()[:240]
    if clean_note and (not notes or notes[-1] != clean_note):
        notes.append(clean_note)
        notes = notes[-8:]  # keep a short rolling window
    entry = {
        # Prefer the player's original casing for display; key stays lowercased.
        "label": display_label or label,
        "kind": _clean_subject_text(subject.get("kind"), entry.get("kind") or "person", 20),
        "first_met_turn": first_met,
        "last_talk_turn": st.get("turn_count", 0),
        "talk_count": int(entry.get("talk_count") or 0) + 1,
        "notes": notes,
        "trust": int(entry.get("trust") or 0),
    }
    chars[key] = entry
    st["characters"] = chars
    try:
        _save_state(st, session_id)
    except Exception as e:
        log_error(f"[TALK] character memory save failed: {e}")
    return entry


def api_companions():
    """List the player's companion roster — characters they've spoken with,
    stored with their cinematic portrait so they can be placed into later
    scenes for a continuing story.

    GET /api/companions?session_id=<id>
    Response: ``{"companions": [{label, kind, portrait_url, seen_count,
      first_seen_turn, last_seen_turn, trust, notes}, ...]}`` — most recently
    seen first.
    """
    try:
        session_id = request.args.get("session_id", "default")
        try:
            st = _load_state(session_id) or {}
        except Exception:
            st = {}
        companions = st.get("companions") or {}
        chars = st.get("characters") or {}
        out = []
        for key, c in companions.items():
            if not isinstance(c, dict):
                continue
            mem = chars.get(key) if isinstance(chars.get(key), dict) else {}
            out.append({
                "label": c.get("label"),
                "kind": c.get("kind"),
                "portrait_url": c.get("portrait_url"),
                "seen_count": c.get("seen_count"),
                "first_seen_turn": c.get("first_seen_turn"),
                "last_seen_turn": c.get("last_seen_turn"),
                "trust": mem.get("trust", 0),
                "notes": (mem.get("notes") or [])[-3:],
                # ElevenLabs voice data: reuse by voice_id, or regenerate from
                # the Voice Design description + model.
                "voice": c.get("voice") or None,
            })
        out.sort(key=lambda r: (r.get("last_seen_turn") or 0), reverse=True)
        return jsonify({"companions": out})
    except Exception as e:
        log_error(f"[COMPANION] list failed: {e}")
        return jsonify({"companions": [], "error": str(e)}), 500


def api_companion_place():
    """Place a stored companion INTO a scene — the primitive for continuing-
    story beats where a known character reappears in the world.

    Request JSON: ``{"label": <str>, "session_id"?, "reference_image"?:
      <data-url of the current frame>, "prompt"?: <what they're doing>}``
    Response: ``{"image_url": "/images/...", "label": <str>}`` — a scene
      featuring the companion, img2img'd from [current frame + companion
      portrait] so they read as the same character standing in this place. On
      any failure returns ``{"image_url": null, "reason": "..."}``.
    """
    try:
        if _rate_limited("companion_place", 1.2):
            return jsonify({"image_url": None, "reason": "slow_down"}), 429
        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id", "default")
        label = _clean_subject_text(data.get("label"), "", 40)
        if not label:
            return jsonify({"error": "missing label", "image_url": None}), 400
        if not IMAGE_ENABLED:
            return jsonify({"image_url": None, "reason": "image_disabled"})

        st = _load_state(session_id) or {}
        companions = st.get("companions") or {}
        comp = companions.get(label.lower())
        if not isinstance(comp, dict) or not comp.get("portrait_url"):
            return jsonify({"image_url": None, "reason": "unknown_companion"})
        portrait_path = _resolve_image_path(comp["portrait_url"])
        if not portrait_path or not portrait_path.exists():
            return jsonify({"image_url": None, "reason": "portrait_missing"})

        who = comp.get("label") or label
        action = _clean_subject_text(data.get("prompt"), "", 200) or \
            (who + " is here in this place with you")
        world_prompt = str(st.get("world_prompt") or "")
        tod = str(st.get("time_of_day") or "")

        # References: the current frame FIRST (environment ground truth), the
        # companion portrait SECOND (who they are) — so the companion appears in
        # this place, consistent with how they looked in conversation.
        refs = []
        ref_frame = _save_portrait_reference(data.get("reference_image") or "", session_id)
        if ref_frame:
            refs.append(ref_frame)
        refs.append(str(portrait_path))

        place_prompt = _sanitize_for_image_generation(
            "In this environment, " + who + " is present: " + action + ". "
            "Keep this location, lighting, palette, and film grain; show them "
            "naturally within the scene."
        )
        t0 = time.time()
        image_path = None
        try:
            from gemini_image_utils import generate_gemini_img2img
            image_path = generate_gemini_img2img(
                prompt=place_prompt,
                caption=f"place_{_companion_slug(who)}",
                reference_image_path=refs,
                strength=0.5,
                world_prompt=world_prompt[:400] if world_prompt else None,
                time_of_day=tod,
                hd_mode=False,
                output_dir=Path(_get_image_dir(session_id)),
                portrait_mode=True,  # person-allowed (skip the anti-person removal)
            )
        except Exception as gen_err:
            log_error(f"[COMPANION] place generate failed: {gen_err}")
            return jsonify({"image_url": None, "reason": "generate_failed"})
        finally:
            if ref_frame:
                try:
                    Path(ref_frame).unlink(missing_ok=True)
                    _small = Path(ref_frame).with_name(
                        Path(ref_frame).name.replace(".png", "_small.png"))
                    if _small.exists():
                        _small.unlink(missing_ok=True)
                except Exception:
                    pass

        web = _to_web_image_url(image_path, session_id)
        try:
            cost_tracker.record_usage(
                session_id, "image", "gemini", "companion_place",
                operation="companion_place", output_units=1.0 if web else 0,
                unit_type="images", latency_ms=int((time.time() - t0) * 1000),
                success=bool(web), error_message=None if web else "no_image_returned",
            )
        except Exception:
            pass
        if not web:
            return jsonify({"image_url": None, "reason": "no_image"})
        # Bump the roster's last-seen so recency reflects the reappearance.
        try:
            _record_companion(session_id, {"label": who, "kind": comp.get("kind")},
                              comp["portrait_url"])
        except Exception:
            pass
        return jsonify({"image_url": web, "label": who})
    except Exception as e:
        log_error(f"[COMPANION] place failed: {e}")
        return jsonify({"image_url": None, "reason": "error", "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Persistent props (jeep, etc.) — companion-shaped durable references
# ---------------------------------------------------------------------------

def _persist_prop_image(image_path: str, session_id: str, slug: str) -> Optional[str]:
    """Copy a freshly-generated prop image to a STABLE, sweep-protected file
    (``prop_<slug>.png``) so it survives and can be re-referenced forever.

    Mirrors ``_persist_companion_image``. Returns the web URL of the durable
    copy, or None on failure.
    """
    try:
        src = _resolve_image_path(image_path)
        if not src or not src.exists():
            return None
        img_dir = Path(_get_image_dir(session_id))
        img_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-z0-9]+", "_", (slug or "").strip().lower()).strip("_") or "prop"
        safe = safe[:40]
        dst = img_dir / f"prop_{safe}.png"
        import shutil as _shutil
        _shutil.copyfile(str(src), str(dst))
        try:
            small_src = src.with_name(src.name.replace(".png", "_small.png"))
            if small_src.exists():
                _shutil.copyfile(str(small_src), str(img_dir / f"prop_{safe}_small.png"))
        except Exception:
            pass
        return _to_web_image_url(f"prop_{safe}.png", session_id)
    except Exception as e:
        log_error(f"[PROP] persist image failed: {e}")
        return None


def _record_prop(session_id: str, slug: str, portrait_url: str,
                 label: str = "", prompt: str = "") -> dict:
    """Upsert a PROP into ``state.props`` — a companion-shaped sibling dict for
    durable recognizable objects (the red jeep, etc.). Additive metadata; does
    not touch turn_count/history. Returns the prop record (JSON-safe).
    """
    safe = re.sub(r"[^a-z0-9]+", "_", (slug or "").strip().lower()).strip("_")
    if not safe or not portrait_url:
        return {}
    try:
        st = _load_state(session_id) or {}
    except Exception:
        return {}
    props = st.get("props")
    if not isinstance(props, dict):
        props = {}
    prev = props.get(safe) if isinstance(props.get(safe), dict) else {}
    turn = st.get("turn_count", 0)
    entry = {
        "label": (label or prev.get("label") or safe).strip()[:60],
        "slug": safe,
        "portrait_url": portrait_url,
        "prompt": (prompt or prev.get("prompt") or "")[:400],
        "first_seen_turn": prev.get("first_seen_turn", turn),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    props[safe] = entry
    st["props"] = props
    try:
        _save_state(st, session_id)
    except Exception as e:
        log_error(f"[PROP] save failed: {e}")
    return entry


def _ensure_jeep_prop(session_id: str) -> dict:
    """Return the durable jeep prop record, generating it once if missing.

    Always prefers the persisted ``prop_jeep.png`` on disk so the vehicle stays
    visually identical across every CAMP (and future mission-transition) visit.
    """
    try:
        st = _load_state(session_id) or {}
    except Exception:
        st = {}
    props = st.get("props") if isinstance(st.get("props"), dict) else {}
    jeep = props.get("jeep") if isinstance(props.get("jeep"), dict) else {}
    url = jeep.get("portrait_url") or ""
    if url:
        path = _resolve_image_path(url)
        if path and path.exists():
            return jeep

    # Also accept a leftover durable file from a prior run whose state lost the
    # props block (e.g. state reset but images kept).
    durable = Path(_get_image_dir(session_id)) / "prop_jeep.png"
    if durable.exists():
        web = _to_web_image_url("prop_jeep.png", session_id)
        return _record_prop(session_id, "jeep", web,
                            label="your red jeep", prompt=_JEEP_PROP_PROMPT)

    if not IMAGE_ENABLED:
        return {}

    t0 = time.time()
    image_path = None
    try:
        from gemini_image_utils import generate_with_gemini
        image_path = generate_with_gemini(
            prompt=_sanitize_for_image_generation(_JEEP_PROP_PROMPT),
            caption="prop_jeep",
            world_prompt=None,
            aspect_ratio="4:3",  # match game stills / camp plates
            time_of_day="dusk, desert evening light",
            hd_mode=False,
            output_dir=Path(_get_image_dir(session_id)),
            portrait_mode=False,  # vehicle plate — anti-person is correct
        )
    except Exception as gen_err:
        log_error(f"[PROP] jeep generate failed: {gen_err}")
        try:
            cost_tracker.record_usage(
                session_id, "image", "gemini", "prop_jeep",
                operation="prop_jeep", output_units=0, unit_type="images",
                latency_ms=int((time.time() - t0) * 1000), success=False,
                error_message=str(gen_err)[:200],
            )
        except Exception:
            pass
        return {}

    web = _to_web_image_url(image_path, session_id) if image_path else None
    try:
        cost_tracker.record_usage(
            session_id, "image", "gemini", "prop_jeep",
            operation="prop_jeep", output_units=1.0 if web else 0,
            unit_type="images", latency_ms=int((time.time() - t0) * 1000),
            success=bool(web), error_message=None if web else "no_image_returned",
        )
    except Exception:
        pass
    if not web:
        return {}
    durable_url = _persist_prop_image(image_path, session_id, "jeep") or web
    return _record_prop(session_id, "jeep", durable_url,
                        label="your red jeep", prompt=_JEEP_PROP_PROMPT)


def _camp_cache_key(session_id: str, labels: list, jeep_url: str) -> tuple:
    labels_sig = ",".join(sorted((l or "").strip().lower() for l in labels))
    jeep_hash = hashlib.sha1((jeep_url or "").encode("utf-8")).hexdigest()[:12]
    return (session_id or "default", _CAMP_CACHE_VERSION, labels_sig, jeep_hash)


def _camp_seat_layout(count: int) -> list:
    n = max(0, min(int(count or 0), 5))
    if n <= 0:
        return []
    return list(_CAMP_SEATS.get(n) or _CAMP_SEATS[5][:n])


def _build_camp_prompt(attendee_labels: list, jeep_included: bool) -> str:
    """Prompt for the camp establishing shot — night fire + jeep + optional cast.

    The red jeep is a non-negotiable visual anchor (Chekhov's gun for leaving
    camp later). Even when a jeep reference image is missing we still describe
    it in text so the plate never comes back as fire-only scrub.
    """
    bits = [
        "First-person handheld establishing shot of a night campsite in remote Four Corners high-desert scrub.",
        "4:3 frame matching the rest of the game. A small campfire burns in the mid-ground,",
        "warm orange firelight pooling on sand and scrub, embers drifting, deep blue-black night sky,",
        "VHS analog grain, 1990s found-footage mood. Not a security camera, not a collage.",
        # Jeep is ALWAYS required in the composition — reference image when we
        # have one, otherwise a hard textual prescription.
        "CRITICAL — THE RED JEEP MUST BE IN FRAME: a dusty bright-red 1990s Jeep "
        "Cherokee/Wrangler parked at the RIGHT edge of the firelight, three-quarter "
        "view, clearly readable silhouette and red paint, mud-caked tires. Do NOT "
        "omit the jeep. Do NOT replace it with a truck or car. The jeep is as "
        "important as the fire.",
    ]
    if jeep_included:
        bits.append(
            "Match the jeep reference image's exact color, body shape, and wear."
        )
    if attendee_labels:
        names = ", ".join(attendee_labels)
        bits.append(
            f"Seated around the flames are these companions, each matching their reference "
            f"portrait face and clothing: {names}. Arrange them in a natural arc around the fire, "
            f"leaving the jeep clearly visible on the right."
        )
    else:
        bits.append(
            "No people — a quiet empty camp. Only the fire and the red jeep."
        )
    bits.append("No text, no UI, no letterbox bars inside the image. Photoreal cinematic still.")
    return _sanitize_for_image_generation(" ".join(bits))


def _build_camp_realtime_prompt(attendee_labels: list, jeep_included: bool = True) -> str:
    """Realtime world-model prompt for a living campsite (fire flicker, night air)."""
    who = ""
    if attendee_labels:
        who = " Companions seated around the fire: " + ", ".join(attendee_labels) + "."
    # Jeep is always part of camp identity — mention even if the prop file failed.
    jeep = " A dusty bright-red 1990s jeep is parked at the edge of the firelight."
    visual = (
        "Night campsite in remote high-desert scrub. A campfire burns in the mid-ground, "
        "embers drifting, warm firelight on sand and brush, deep desert dark."
        + jeep
        + who
        + " First-person handheld view. Firelight flickers. Quiet night wind in the scrub."
    )
    return build_realtime_prompt(visual_scene=visual, narrative=visual, choice="")


def api_camp_enter():
    """Build (or reuse) the CAMP Moment establishing shot.

    Request JSON: ``{"session_id"?: str}``
    Response JSON::

        {
          "image_url": "/images/...",
          "attendees": [
            {"label", "kind", "portrait_url", "seat": {"x_pct", "y_pct"}},
            ...
          ],
          "jeep_included": true,
          "cached": bool
        }

    Side pocket: does NOT mutate turn_count/history. Appends one additive
    display-only ``feed_log`` camp item per visit (flavor for the Story Log).
    """
    try:
        if _rate_limited("camp_enter", 1.2):
            return jsonify({"error": "slow_down", "image_url": None}), 429
        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id", "default")

        try:
            st = _load_state(session_id) or {}
        except Exception:
            st = {}

        # Top 5 companions by last_seen_turn (most recent relationships first).
        companions = st.get("companions") if isinstance(st.get("companions"), dict) else {}
        roster = []
        for key, c in companions.items():
            if not isinstance(c, dict) or not c.get("portrait_url"):
                continue
            path = _resolve_image_path(c["portrait_url"])
            if not path or not path.exists():
                continue
            roster.append({
                "label": c.get("label") or key,
                "kind": c.get("kind") or "person",
                "portrait_url": c["portrait_url"],
                "portrait_path": str(path),
                "last_seen_turn": c.get("last_seen_turn") or 0,
            })
        roster.sort(key=lambda r: (r.get("last_seen_turn") or 0), reverse=True)
        attendees_src = roster[:5]

        jeep = _ensure_jeep_prop(session_id)
        jeep_url = (jeep or {}).get("portrait_url") or ""
        jeep_path = None
        if jeep_url:
            jp = _resolve_image_path(jeep_url)
            if jp and jp.exists():
                jeep_path = str(jp)
        jeep_included = bool(jeep_path)

        labels = [a["label"] for a in attendees_src]
        cache_key = _camp_cache_key(session_id, labels, jeep_url)
        with _CAMP_CACHE_LOCK:
            cached = _CAMP_CACHE.get(cache_key)
        seats = _camp_seat_layout(len(attendees_src))
        attendees_out = []
        for i, a in enumerate(attendees_src):
            seat = seats[i] if i < len(seats) else {"x_pct": 50, "y_pct": 60}
            attendees_out.append({
                "label": a["label"],
                "kind": a["kind"],
                "portrait_url": a["portrait_url"],
                "seat": seat,
            })

        realtime_prompt = _build_camp_realtime_prompt(labels, jeep_included=True)

        if cached:
            # Validate the cached plate still exists on disk — establishing
            # shots are ordinary session images and can be swept; never serve
            # a 404 URL into the Moment chrome.
            cached_path = _resolve_image_path(cached)
            if cached_path and cached_path.exists():
                _camp_append_feed(session_id, cached)
                return jsonify({
                    "image_url": cached,
                    "attendees": attendees_out,
                    "jeep_included": jeep_included,
                    "realtime_prompt": realtime_prompt,
                    "cached": True,
                })
            with _CAMP_CACHE_LOCK:
                _CAMP_CACHE.pop(cache_key, None)

        if not IMAGE_ENABLED:
            return jsonify({
                "image_url": None,
                "attendees": attendees_out,
                "jeep_included": jeep_included,
                "reason": "image_disabled",
            })

        # Refs: jeep first (visual anchor), then up to 5 companion portraits.
        refs = []
        if jeep_path:
            refs.append(jeep_path)
        for a in attendees_src:
            refs.append(a["portrait_path"])

        prompt = _build_camp_prompt(labels, jeep_included)
        world_prompt = str(st.get("world_prompt") or "")[:400] or None
        img_dir = Path(_get_image_dir(session_id))
        t0 = time.time()
        image_path = None
        gen_mode = "ensemble" if refs else "text2img"
        try:
            if refs:
                from gemini_image_utils import generate_gemini_img2img
                image_path = generate_gemini_img2img(
                    prompt=prompt,
                    caption="camp_establish",
                    reference_image_path=refs,
                    # Higher than companion_place (0.5): this is a brand-new
                    # location anchored by people/prop likeness, not the live scene.
                    strength=0.75,
                    world_prompt=world_prompt,
                    time_of_day="night, campfire glow, deep desert dark",
                    hd_mode=False,
                    output_dir=img_dir,
                    ensemble_mode=True,
                    portrait_mode=True,  # allow people (skip anti-person)
                )
            else:
                # No jeep file and no companions — still describe the jeep in
                # text so the plate isn't fire-only scrub.
                from gemini_image_utils import generate_with_gemini
                image_path = generate_with_gemini(
                    prompt=prompt,
                    caption="camp_establish",
                    world_prompt=world_prompt,
                    aspect_ratio="4:3",
                    time_of_day="night, campfire glow, deep desert dark",
                    hd_mode=False,
                    output_dir=img_dir,
                    portrait_mode=False,
                )
        except Exception as gen_err:
            log_error(f"[CAMP] generate failed: {gen_err}")
            try:
                cost_tracker.record_usage(
                    session_id, "image", "gemini", "camp_enter",
                    operation="camp_enter", output_units=0, unit_type="images",
                    latency_ms=int((time.time() - t0) * 1000), success=False,
                    error_message=str(gen_err)[:200],
                )
            except Exception:
                pass
            return jsonify({
                "image_url": None,
                "attendees": attendees_out,
                "jeep_included": jeep_included,
                "reason": "generate_failed",
            })

        web = _to_web_image_url(image_path, session_id) if image_path else None
        try:
            cost_tracker.record_usage(
                session_id, "image", "gemini", "camp_enter",
                operation=f"camp_enter_{gen_mode}",
                output_units=1.0 if web else 0, unit_type="images",
                latency_ms=int((time.time() - t0) * 1000),
                success=bool(web),
                error_message=None if web else "no_image_returned",
            )
        except Exception:
            pass
        if not web:
            return jsonify({
                "image_url": None,
                "attendees": attendees_out,
                "jeep_included": jeep_included,
                "reason": "no_image",
            })

        # Persist a durable, sweep-protected camp plate (prop_camp_<sig>.png)
        # so cache hits survive disk headroom sweeps the same way the jeep does.
        durable_web = web
        try:
            sig = hashlib.sha1(str(cache_key).encode("utf-8")).hexdigest()[:12]
            durable_web = _persist_prop_image(image_path, session_id, f"camp_{sig}") or web
        except Exception as persist_err:
            log_error(f"[CAMP] persist plate failed: {persist_err}")
            durable_web = web

        with _CAMP_CACHE_LOCK:
            _CAMP_CACHE[cache_key] = durable_web
        print(f"[CAMP] establishing shot ready ({gen_mode}, "
              f"{len(attendees_out)} attendees, jeep={jeep_included})", flush=True)

        _camp_append_feed(session_id, durable_web)
        return jsonify({
            "image_url": durable_web,
            "attendees": attendees_out,
            "jeep_included": jeep_included,
            "realtime_prompt": realtime_prompt,
            "cached": False,
        })
    except Exception as e:
        log_error(f"[CAMP] enter failed: {e}")
        return jsonify({"image_url": None, "attendees": [], "error": str(e)}), 500


def _camp_append_feed(session_id: str, image_url: str) -> None:
    """One additive, display-only Story Log beat per camp visit.

    Does not touch turn_count / history / choice generation — camp is a side
    pocket, same as conversation.
    """
    try:
        item = create_feed_item(
            type="camp",
            content="You made camp for the night.",
            image_url=image_url,
            metadata={"source": "camp"},
        )
        with WORLD_STATE_LOCK:
            st = _load_state(session_id) or {}
            _feed_append(st, item)
            _save_state(st, session_id)
    except Exception as e:
        log_error(f"[CAMP] feed append failed: {e}")


def api_talk_session():
    """Open a story-aware conversation with a SCAN subject.

    Request JSON:  {"subject": {"label", "kind", "speaks"}, "session_id"?}
    Response JSON:
        {
          "mode": "voice" | "text",
          "subject": {...},
          "context": {premise, situation, recent, opening_line, ...},
          "agent_id":  <str|null>,   # ElevenLabs Conversational AI agent
          "signed_url": <str|null>,  # short-lived URL for a private agent
          "overrides": {...},        # agent prompt + first-message overrides
          "dynamic_variables": {...} # story variables for the agent/widget
        }

    Voice mode needs only ELEVENLABS_AGENT_ID (a public agent connects with the
    bare id). ELEVENLABS_API_KEY additionally mints a short-lived signed URL for
    a PRIVATE agent — the recommended, more secure setup — without the key ever
    reaching the browser. With neither set we degrade to text mode, which is
    fully functional via /api/talk/message. Read-only: never mutates the sim.
    """
    try:
        if _rate_limited("talk_session", 0.8):
            return jsonify({"error": "slow down", "mode": "text"}), 429
        data = request.get_json(silent=True) or {}
        subject = data.get("subject") or {}
        session_id = data.get("session_id", "default")
        if not isinstance(subject, dict) or not (subject.get("label") or "").strip():
            return jsonify({"error": "missing subject"}), 400

        # When only the VOICE is changing, the client passes the existing opening
        # line so we don't burn an LLM call regenerating an identical greeting.
        context = build_talk_context(subject, session_id, opening_override=data.get("opening_line", ""))

        # Resolve the voice. Precedence:
        #   1) Explicit (validated) client choice — that's what powers the
        #      live voice-switcher pill in the TALK panel.
        #   2) A per-character voice designed on the fly from the persona
        #      brief (ElevenLabs Voice Design), cached per session and
        #      deleted at session end. See voice_design.py.
        #   3) The static by_kind default in voices.json (always available).
        # The resolver also surfaces cache_key + status so the client can
        # poll /api/talk/voice/status and hot-swap the Convai override once
        # the designed voice lands (typically after the fallback opening).
        chosen_voice = _valid_voice_id(data.get("voice_id"))
        try:
            _world_prompt = str((_load_state(session_id) or {}).get("world_prompt") or "")
        except Exception:
            _world_prompt = ""
        voice_resolution = resolve_voice_for_subject(
            context["subject"], session_id, context=context,
            world_prompt=_world_prompt,
            # Short blocking wait catches the fast path when a preview lands
            # quickly, so the very first line is often already in-character.
            wait=0.6,
        )
        if chosen_voice:
            resolved_voice = chosen_voice
            # Client override wins, but we still keep the designed voice
            # cooking in the background — the picker can revert to "auto".
            voice_status = "override"
            voice_cache_key = None
            voice_description = ""
        else:
            resolved_voice = voice_resolution["voice_id"]
            voice_status = voice_resolution["status"]
            voice_cache_key = voice_resolution.get("cache_key")
            voice_description = voice_resolution.get("description", "")
        # Bump the refcount for the voice we're about to hand to Convai so
        # session cleanup can't yank it mid-call. /api/talk/end releases it.
        try:
            import voice_design as _vd
            _vd.acquire(resolved_voice)
        except Exception:
            pass

        # Persist the ElevenLabs voice data onto this character's companion
        # record so their voice can be reused (by id) or REGENERATED (by the
        # Voice Design description) later, for a continuing story. Best-effort.
        try:
            _ttv_model = ""
            try:
                import voice_design as _vd2
                _ttv_model = getattr(_vd2, "TTV_MODEL", "") or ""
            except Exception:
                _ttv_model = ""
            _record_companion_voice(session_id, context["subject"], {
                "voice_id": resolved_voice,
                "description": voice_description,
                "source": ("override" if chosen_voice else voice_resolution.get("source", "")),
                "status": voice_status,
                "cache_key": voice_cache_key,
                # Only designed voices carry a regeneration description; tag the
                # model so a future regen knows which TTV model produced it.
                "model": _ttv_model if voice_description else "",
            })
        except Exception as _ve:
            log_error(f"[COMPANION] voice record failed: {_ve}")

        # Dynamic variables + prompt overrides an ElevenLabs agent can consume to
        # stay aware of the story (see ElevenLabs Conversational AI docs).
        sit = context["situation"]
        vision = context.get("vision") or {}
        # Flatten the vision snapshot into a single string an ElevenLabs
        # agent template can reference as {{visible_now}} without having to
        # walk a JSON array. Kept short so it fits the agent's variable budget.
        visible_names = []
        for _obj in (vision.get("visible") or []):
            _lbl = str(_obj.get("label") or "").strip()
            if _lbl and _lbl.lower() != context["subject"]["label"].lower():
                visible_names.append(_lbl)
            if len(visible_names) >= 6:
                break
        dynamic_variables = {
            "subject_label": context["subject"]["label"],
            "subject_kind": context["subject"]["kind"],
            "story_premise": context["premise"],
            "story_phase": str(sit.get("phase", "")),
            "chaos_level": str(sit.get("chaos", "")),
            "turn": str(sit.get("turn", "")),
            "location": str(sit.get("location", "")),
            "time_of_day": str(sit.get("time_of_day", "")),
            "current_scene": str(sit.get("scene", "")),
            "recent_events": " | ".join(context.get("recent", [])),
            # Direct-perception fields the voice agent can reference in its
            # prompt template so its spoken lines stay grounded in what's
            # actually on the player's screen. Empty strings when vision is
            # unavailable — the agent template can fall back gracefully.
            "visible_now": ", ".join(visible_names),
            "visible_description": str(vision.get("description", "")),
        }
        # The opening line is ALSO a dynamic variable so an agent whose dashboard
        # first-message is "{{opening_line}}" stays story-aware even without
        # runtime overrides.
        dynamic_variables["opening_line"] = context["opening_line"]

        # Overrides completely replace the agent's prompt/first-message with the
        # full per-subject persona. Only send them when allowed (the target agent
        # must have those override fields enabled, or the widget throws).
        overrides = None
        if ELEVENLABS_ALLOW_OVERRIDES:
            overrides = {
                "agent": {
                    "prompt": {"prompt": context["persona_prompt"]},
                    "first_message": context["opening_line"],
                }
            }
            # Voice override (agent has tts.voice_id override enabled) — this is
            # how a live voice switch takes effect on the conversational agent.
            if resolved_voice:
                overrides["tts"] = {"voice_id": resolved_voice}

        agent_id = ELEVENLABS_AGENT_ID
        api_key = ELEVENLABS_API_KEY
        signed_url = None
        # Voice needs only an agent id (public agents connect with it directly).
        mode = "voice" if agent_id else "text"

        if agent_id and api_key:
            # Private agents need a short-lived signed URL minted server-side so
            # the API key never reaches the browser. A signing failure is
            # non-fatal: a public agent can still connect with the bare agent_id.
            try:
                import requests as _rq
                resp = _rq.get(
                    "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url",
                    headers={"xi-api-key": api_key},
                    params={"agent_id": agent_id},
                    timeout=12,
                )
                if resp.status_code == 200:
                    signed_url = (resp.json() or {}).get("signed_url")
                else:
                    log_error(f"[TALK] signed-url {resp.status_code}: {resp.text[:200]}")
            except Exception as e:
                log_error(f"[TALK] signed-url exchange failed: {e}")

        return jsonify({
            "mode": mode,
            "subject": context["subject"],
            "context": {
                "premise": context["premise"],
                "situation": context["situation"],
                "recent": context["recent"],
                "opening_line": context["opening_line"],
            },
            "agent_id": agent_id or None,
            "signed_url": signed_url,
            "voice_id": resolved_voice or None,
            # Extra fields let the client hot-swap the Convai voice once a
            # per-character voice designed in the background is ready.
            "voice_status": voice_status,
            "voice_cache_key": voice_cache_key,
            "voice_description": voice_description,
            "voices": get_voice_registry(),
            "overrides": overrides,
            "dynamic_variables": dynamic_variables,
        })
    except Exception as e:
        import traceback as _tb
        log_error(f"[TALK] session failed: {e}")
        _tb.print_exc()
        return jsonify({"error": str(e), "mode": "text"}), 500


def api_talk_voices():
    """The voice catalog for the TALK mechanic (and narrator). Lets the client
    render a live voice picker without hardcoding ids. Read-only, no secrets."""
    try:
        return jsonify(get_voice_registry())
    except Exception as e:
        log_error(f"[TALK] voices failed: {e}")
        return jsonify({"voices": [], "default": None, "by_kind": {}, "cast": {}}), 500


def api_talk_end():
    """Client fires this when the TALK widget closes (or switches/hot-swaps
    voice) so we can drop the refcount on the voice it was using AND log the
    conversational-agent minutes to the cost ledger. Voices with refcount == 0
    become eligible for session-end cleanup + LRU eviction.

    Request JSON: ``{"voice_id": <str>, "session_id"?: <str>,
    "duration_seconds"?: <float>, "subject"?: {...}, "memory_note"?: <str>}``.
    `duration_seconds` is how long the ElevenLabs Convai channel was actually
    connected — the server never proxies that websocket, so the client is the
    only one who knows. When ``subject`` is present we also upsert a lightweight
    per-character memory record (see ``_record_character_memory``) so future
    Moments / trust systems have somewhere to plug in.
    Response JSON: ``{"ok": true, "refcount": <int>, "character"?: {...}}``.
    Always 200 — this is fire-and-forget from the client; we never let an
    end-of-call cleanup error surface as a user-visible failure.
    """
    try:
        data = request.get_json(silent=True) or {}
        voice_id = str(data.get("voice_id") or "").strip()
        session_id = str(data.get("session_id") or "default")
        try:
            seconds = float(data.get("duration_seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0.0
        if seconds > 0:
            cost_tracker.record_usage(
                session_id, "voice", "elevenlabs", "talk_agent",
                output_units=seconds, unit_type="seconds", success=True,
            )
        character = None
        subject = data.get("subject")
        if isinstance(subject, dict) and (subject.get("label") or "").strip():
            try:
                character = _record_character_memory(
                    session_id, subject, note=str(data.get("memory_note") or ""),
                )
            except Exception as mem_err:
                log_error(f"[TALK] character memory failed: {mem_err}")
        remaining = 0
        if voice_id:
            try:
                import voice_design as _vd
                remaining = _vd.release(voice_id)
            except Exception:
                remaining = 0
        out = {"ok": True, "refcount": remaining}
        if character:
            out["character"] = {
                "label": character.get("label"),
                "talk_count": character.get("talk_count"),
                "trust": character.get("trust"),
                "first_meeting": character.get("talk_count") == 1,
            }
        return jsonify(out)
    except Exception as e:
        log_error(f"[TALK] end failed: {e}")
        return jsonify({"ok": True, "refcount": 0})


def api_talk_voice_status():
    """Poll for a still-designing per-character voice.

    Request: ``GET /api/talk/voice/status?cache_key=<16-hex>``.
    Response JSON: ``{"cache_key", "voice_id", "status", "description"}`` —
    ``status`` cycles through ``"generating"`` -> ``"ready"`` (or ``"failed"``
    / ``"unknown"``). When ``status == "ready"``, the client re-opens the
    Convai override with the new ``voice_id`` so the character voice swaps
    live mid-conversation.
    """
    try:
        cache_key_str = str(request.args.get("cache_key") or "").strip()
        if not cache_key_str:
            return jsonify({"error": "missing cache_key"}), 400
        try:
            import voice_design as _vd
            status = _vd.get_status(cache_key_str) or {
                "cache_key": cache_key_str, "voice_id": None,
                "status": "unknown", "description": "",
            }
        except Exception as _e:
            status = {"cache_key": cache_key_str, "voice_id": None,
                      "status": "unknown", "description": "", "error": str(_e)}
        return jsonify(status)
    except Exception as e:
        log_error(f"[TALK] voice status failed: {e}")
        return jsonify({"error": str(e), "status": "unknown"}), 500


# ═══════════════════════════════════════════════════════════════════
# NARRATOR STREAM — a voice that frames the world (and can BE many voices)
#
# Where TALK is a two-way conversation with a subject IN the scene, the narrator
# is a one-way voice OVER the scene: world-building, lore, cold opens, stingers.
# It can speak as a single "archive voice" or as a small CAST (narrator / man /
# woman / elder / creature / machine / warden…), so a single narration can hand
# off between characters like a radio play. Built on ElevenLabs text-to-speech
# so it's a clean, expandable primitive:
#   • /api/narrator/say       — speak one line (returns audio)
#   • /api/narrator/narrate   — speak a multi-character script (audio per line)
#   • /api/narrator/worldbuild— GENERATE a story-aware narration, then (opt) speak
#   • /api/narrator/cast      — the voices + named cast the client can pick from
# All read-only; none mutate the sim. Requires ELEVENLABS_API_KEY for audio;
# without it these degrade to returning text only (never an error).
# ═══════════════════════════════════════════════════════════════════

def _tts_synthesize(text: str, voice_id: str, settings: dict = None):
    """Synthesize one line of narration to MP3 bytes via ElevenLabs TTS.
    Returns bytes on success, or None (never raises)."""
    text = (text or "").strip()
    if not text or not ELEVENLABS_API_KEY or not voice_id:
        return None
    t0 = time.time()
    try:
        import requests as _rq
        vs = {"stability": 0.5, "similarity_boost": 0.75}
        if isinstance(settings, dict):
            for k in ("stability", "similarity_boost", "style", "speed", "use_speaker_boost"):
                if k in settings and settings[k] is not None:
                    vs[k] = settings[k]
        billed_text = text[:2500]
        resp = _rq.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
            params={"output_format": "mp3_44100_128"},
            json={"text": billed_text, "model_id": ELEVENLABS_TTS_MODEL, "voice_settings": vs},
            timeout=30,
        )
        if resp.status_code == 200:
            cost_tracker.record_usage(
                get_active_session_id(), "voice", "elevenlabs", "tts", operation="tts_synthesize",
                input_units=len(billed_text), unit_type="characters", success=True,
                latency_ms=int((time.time() - t0) * 1000),
            )
            return resp.content
        log_error(f"[NARRATOR] tts {resp.status_code}: {resp.text[:180]}")
        cost_tracker.record_usage(
            get_active_session_id(), "voice", "elevenlabs", "tts", operation="tts_synthesize",
            input_units=len(billed_text), unit_type="characters", success=False,
            error_message=f"http_{resp.status_code}", latency_ms=int((time.time() - t0) * 1000),
        )
        return None
    except Exception as e:
        log_error(f"[NARRATOR] tts failed: {e}")
        cost_tracker.record_usage(
            get_active_session_id(), "voice", "elevenlabs", "tts", operation="tts_synthesize",
            success=False, error_message=str(e), latency_ms=int((time.time() - t0) * 1000),
        )
        return None


def _narrate_segments(segments: list) -> list:
    """Voice a list of {character, text[, voice_id]} into per-segment results
    with base64 audio (when a key is configured). Each output segment:
    {character, text, voice_id, audio?(data-url)}."""
    import base64 as _b64
    out = []
    # Hard cap the number of voiced lines: bounds TTS cost, latency, and the
    # base64 payload size per request.
    for seg in (segments or [])[:6]:
        if not isinstance(seg, dict):
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        character = (seg.get("character") or "narrator").strip().lower()
        cast = resolve_cast(character)
        voice_id = _valid_voice_id(seg.get("voice_id")) or cast.get("voice_id") \
            or ELEVENLABS_NARRATOR_VOICE_ID or _default_voice_id()
        settings = {k: cast[k] for k in ("stability", "speed", "style", "similarity_boost") if k in cast}
        entry = {"character": character, "text": text[:2500], "voice_id": voice_id}
        audio = _tts_synthesize(text, voice_id, settings)
        if audio:
            entry["audio"] = "data:audio/mpeg;base64," + _b64.b64encode(audio).decode("ascii")
        out.append(entry)
    return out


def _mint_signed_url(agent_id: str):
    """Mint a short-lived signed conversation URL for a PRIVATE agent (needs the
    API key). Returns the wss URL or None. A public agent doesn't need this."""
    if not agent_id or not ELEVENLABS_API_KEY:
        return None
    try:
        import requests as _rq
        resp = _rq.get(
            "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url",
            headers={"xi-api-key": ELEVENLABS_API_KEY},
            params={"agent_id": agent_id},
            timeout=12,
        )
        if resp.status_code == 200:
            return (resp.json() or {}).get("signed_url")
        log_error(f"[TALK] signed-url {resp.status_code}: {resp.text[:180]}")
    except Exception as e:
        log_error(f"[TALK] signed-url exchange failed: {e}")
    return None


def _narrator_agent_config() -> dict:
    """Browser voice-agent config for the narrator: the (public) agent id and,
    for a private agent, a fresh signed URL. This is what lets narration play
    LIVE as a generative agent with no server-side TTS key."""
    agent_id = ELEVENLABS_NARRATOR_AGENT_ID
    return {
        "agent_id": agent_id or None,
        "signed_url": _mint_signed_url(agent_id),
        "allow_overrides": bool(ELEVENLABS_ALLOW_OVERRIDES),
    }


def _segment_voice(character: str, voice_id=None) -> str:
    """Resolve the voice a narrator segment should speak in."""
    cast = resolve_cast(character)
    return _valid_voice_id(voice_id) or cast.get("voice_id") \
        or ELEVENLABS_NARRATOR_VOICE_ID or _default_voice_id()


def api_narrator_cast():
    """The voices + named cast the narrator can speak as (client picker), plus
    the browser voice-agent config so narration can play as a generative agent."""
    try:
        reg = get_voice_registry()
        agent = _narrator_agent_config()
        return jsonify({
            "voices": reg["voices"],
            "narrator": reg["narrator"],
            "cast": reg["cast"],
            # TTS (server-side) availability — legacy path; the primary path is
            # now the generative agent below.
            "voice_available": bool(ELEVENLABS_API_KEY),
            # Generative-agent availability: narration speaks live if an agent id
            # is configured (public agent needs no key).
            "agent_available": bool(agent.get("agent_id")),
            "agent": agent,
        })
    except Exception as e:
        log_error(f"[NARRATOR] cast failed: {e}")
        return jsonify({"voices": [], "cast": {}, "voice_available": False, "agent_available": False}), 500


def api_narrator_say():
    """Speak one line of narration. Request: {text, voice_id?|character?,
    settings?}. Returns audio/mpeg (or 503 text JSON if voice isn't configured)."""
    try:
        if _rate_limited("narrator_say", 0.5):
            return jsonify({"error": "slow down"}), 429
        data = request.get_json(silent=True) or {}
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify({"error": "missing text"}), 400
        if not ELEVENLABS_API_KEY:
            return jsonify({"error": "narrator voice not configured", "text": text}), 503
        cast = resolve_cast(data.get("character"))
        voice_id = _valid_voice_id(data.get("voice_id")) or cast.get("voice_id") \
            or ELEVENLABS_NARRATOR_VOICE_ID or _default_voice_id()
        settings = {k: cast[k] for k in ("stability", "speed", "style", "similarity_boost") if k in cast}
        if isinstance(data.get("settings"), dict):
            settings.update(data["settings"])
        audio = _tts_synthesize(text, voice_id, settings)
        if not audio:
            return jsonify({"error": "synthesis failed"}), 502
        from flask import Response as _Resp
        return _Resp(audio, mimetype="audio/mpeg")
    except Exception as e:
        import traceback as _tb
        log_error(f"[NARRATOR] say failed: {e}")
        _tb.print_exc()
        return jsonify({"error": str(e)}), 500


def api_narrator_narrate():
    """Speak a multi-character script. Request: {segments:[{character,text}]}.
    Response: {segments:[{character,text,voice_id,audio?}], voice: bool}."""
    try:
        if _rate_limited("narrator_narrate", 2.0):
            return jsonify({"error": "slow down", "segments": []}), 429
        data = request.get_json(silent=True) or {}
        segments = data.get("segments") or []
        if not isinstance(segments, list) or not segments:
            return jsonify({"error": "missing segments"}), 400
        voiced = _narrate_segments(segments)
        return jsonify({"segments": voiced, "voice": bool(ELEVENLABS_API_KEY)})
    except Exception as e:
        import traceback as _tb
        log_error(f"[NARRATOR] narrate failed: {e}")
        _tb.print_exc()
        return jsonify({"error": str(e), "segments": []}), 500


def _clip_narration_to_one_sentence(text: str) -> str:
    """Trim a narration line down to a SINGLE sentence — the narrator always
    speaks exactly one. LLMs occasionally ignore the "one sentence" instruction
    and stack a second (or trail off), so this is the hard guarantee. Ellipses
    ("...") are preserved as intra-sentence pauses; only a standalone terminator
    (. ! ?) that isn't part of an ellipsis counts as the end of the sentence."""
    t = (text or "").strip()
    if not t:
        return t
    m = re.search(r"[.!?](?!\.)", t)
    if not m:
        return t.rstrip(",;:") + "."
    return t[: m.end()].strip()


def _narrator_script(focus: str, multi: bool, session_id: str) -> list:
    """Generate a short, story-aware world-building narration as a list of
    {character, text} segments. `multi` lets it hand off between cast voices.

    Every returned segment is CLIPPED to a single sentence — the narrator always
    speaks exactly one line, so a bridging beat (e.g. MOVE TO's black loading
    screen) is over before the loading is."""
    try:
        st = _load_state(session_id) or {}
    except Exception:
        st = {}
    try:
        hist = _load_history(session_id) or []
    except Exception:
        hist = []
    premise = _story_premise()
    scene = (st.get("world_prompt") or "").strip()
    recent = []
    for entry in hist[-3:]:
        d = (entry.get("dispatch") or "").strip()
        if d:
            recent.append(d[:220])
    recent_block = ("\n\nRECENT EVENTS:\n- " + "\n- ".join(recent)) if recent else ""
    focus_block = f"\n\nFOCUS THIS NARRATION ON: {focus.strip()}" if (focus or "").strip() else ""

    fallback = [{"character": "narrator",
                 "text": "My hands won't stop shaking. I have to find out what happened here."}]
    if not LLM_ENABLED:
        return fallback

    if multi:
        cast_names = ", ".join((VOICES_CONFIG.get("cast") or {}).keys()) or "narrator"
        prompt = (
            f"You script a 1993 analog-horror world. PREMISE: {premise}"
            f"{(chr(10)+chr(10)+'CURRENT SCENE: '+scene) if scene else ''}{recent_block}{focus_block}\n\n"
            f"Write a SHORT radio-play style world-building narration: 2 to 5 lines that hand off between "
            f"these voices where it fits: {cast_names}. Keep it atmospheric, ominous, concrete — no meta, "
            f"no stage directions. EACH LINE IS EXACTLY ONE SHORT SENTENCE. Respond with ONLY a JSON array, "
            f'each item {{"character": "<one of the voice names>", "text": "<exactly one short sentence>"}}.'
        )
        raw = _ask(prompt, temp=0.9, tokens=320, use_lore=True)
        if _talk_llm_failed(raw):
            return [{"character": s["character"], "text": _clip_narration_to_one_sentence(s["text"])} for s in fallback]
        import json as _json, re as _re
        cleaned = _re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=_re.MULTILINE).strip()
        try:
            arr = _json.loads(cleaned)
        except Exception:
            m = _re.search(r"\[.*\]", cleaned, _re.DOTALL)
            arr = _json.loads(m.group(0)) if m else None
        segs = []
        if isinstance(arr, list):
            for it in arr[:4]:  # cap lines — bounds TTS cost + payload size
                if isinstance(it, dict) and (it.get("text") or "").strip():
                    one = _clip_narration_to_one_sentence((it.get("text") or "").strip()[:400])
                    if one:
                        segs.append({"character": (it.get("character") or "narrator").strip().lower(),
                                     "text": one})
        return segs or [{"character": s["character"], "text": _clip_narration_to_one_sentence(s["text"])} for s in fallback]

    # Single-voice narration — one lone voice thinking out loud, in exactly ONE
    # short sentence (a bridging beat, not a monologue).
    #
    # When a SPECIFIC focus is provided (e.g. MOVE TO's "reveal a dark truth"
    # follow-up), the focus text takes primacy over the generic "afraid /
    # uneasy / must find out what happened here" template — otherwise the
    # baseline mood-instructions overwhelm the focus and every follow-up line
    # collapses back into the same generic "I have to find out what happened
    # here" beat. With no focus, keep the original evocative baseline.
    if (focus or "").strip():
        prompt = (
            f"You are the NARRATOR of a 1993 analog-horror world — one lone person, "
            f"speaking quietly to yourself. PREMISE: {premise}"
            f"{(chr(10)+chr(10)+'CURRENT SCENE: '+scene) if scene else ''}{recent_block}\n\n"
            f"INSTRUCTIONS FOR THIS LINE: {focus.strip()}\n\n"
            f"Speak in FIRST PERSON. Output EXACTLY ONE short, plain sentence — nothing more. "
            f"No meta, no stage directions, no purple prose. Follow the INSTRUCTIONS above exactly."
        )
    else:
        prompt = (
            f"You are the NARRATOR of a 1993 analog-horror world — one lone person, "
            f"speaking quietly to yourself. PREMISE: {premise}"
            f"{(chr(10)+chr(10)+'CURRENT SCENE: '+scene) if scene else ''}{recent_block}\n\n"
            f"Speak in FIRST PERSON. Use ONE short, plain sentence — nothing more. Say how you FEEL right now — "
            f"afraid, uneasy, but determined. Make it clear you have to find out what happened here. "
            f"Output EXACTLY ONE SENTENCE. No meta, no stage directions, no purple prose."
        )
    line = _ask(prompt, temp=0.85, tokens=80, use_lore=True)
    if _talk_llm_failed(line):
        return [{"character": s["character"], "text": _clip_narration_to_one_sentence(s["text"])} for s in fallback]
    return [{"character": "narrator",
             "text": _clip_narration_to_one_sentence((line or "").strip()[:600])}]


def api_narrator_worldbuild():
    """Generate a story-aware world-building narration and (optionally) speak it.

    Request JSON: {"focus"?: str, "multi"?: bool, "speak"?: bool, "session_id"?}
    Response: {"segments": [{character, text, voice_id?, audio?}], "voice": bool}
    With speak=false (or no key) it returns text-only segments the client can
    display and/or send to /api/narrator/narrate later. Read-only."""
    try:
        if _rate_limited("narrator_worldbuild", 3.0):
            return jsonify({"error": "slow down", "segments": []}), 429
        data = request.get_json(silent=True) or {}
        focus = re.sub(r"\s+", " ", str(data.get("focus") or "")).strip()[:240]
        # Optional SECOND focus — appended to the primary script's segments in
        # one round trip so a caller who needs a bridging + follow-up beat (e.g.
        # MOVE TO's black loading transition) never has to fire two requests
        # (which would hit the per-IP rate limit). Always single-voice; the
        # multi-voice path is inherently multi-line and doesn't need this.
        follow_raw = str(data.get("follow_focus") or "")
        follow_focus = re.sub(r"\s+", " ", follow_raw).strip()[:240]
        multi = bool(data.get("multi"))
        speak = data.get("speak", True)  # legacy: server-side TTS audio inline
        session_id = data.get("session_id", "default")
        script = _narrator_script(focus, multi, session_id)
        if follow_focus:
            follow_script = _narrator_script(follow_focus, False, session_id)
            script = list(script or []) + list(follow_script or [])
        # Attach the resolved voice per line so the client's generative agent
        # knows which voice to speak each segment in.
        for seg in script:
            seg["voice_id"] = _segment_voice(seg.get("character"), seg.get("voice_id"))
        agent = _narrator_agent_config()
        # Primary path: return TEXT + voice + agent config; the browser speaks it
        # through the generative agent (works live with no server key). The
        # legacy speak=true path also embeds server-TTS audio when a key exists.
        if speak and ELEVENLABS_API_KEY:
            voiced = _narrate_segments(script)
            return jsonify({"segments": voiced, "voice": True, "agent": agent,
                            "agent_available": bool(agent.get("agent_id"))})
        return jsonify({"segments": script, "voice": False, "agent": agent,
                        "agent_available": bool(agent.get("agent_id"))})
    except Exception as e:
        import traceback as _tb
        log_error(f"[NARRATOR] worldbuild failed: {e}")
        _tb.print_exc()
        return jsonify({"error": str(e), "segments": []}), 500


def api_talk_message():
    """One turn of a text conversation with a SCAN subject (the always-on path).

    Request JSON:
        {
          "subject": {"label", "kind", "speaks"},
          "messages": [{"role": "user"|"assistant", "content": str}, ...],
          "session_id"?: str
        }
    Response JSON: {"reply": str}

    The server rebuilds the story-aware persona each call from CURRENT state, so
    the character stays aware of how the world has evolved between lines. The
    client owns the running transcript and sends it back each turn. Read-only.
    """
    try:
        if _rate_limited("talk_message", 0.8):
            return jsonify({"error": "slow down", "reply": "\u2026"}), 429
        data = request.get_json(silent=True) or {}
        subject = data.get("subject") or {}
        messages = data.get("messages") or []
        session_id = data.get("session_id", "default")
        if not isinstance(subject, dict) or not (subject.get("label") or "").strip():
            return jsonify({"error": "missing subject"}), 400
        if not isinstance(messages, list):
            messages = []
        # Text mode reuses the same opening line the client already showed (skip
        # a redundant LLM call); the transcript itself carries the conversation.
        opening = ""
        for _m in messages:
            if isinstance(_m, dict) and _m.get("role") == "assistant":
                opening = _m.get("content") or ""
                break
        context = build_talk_context(subject, session_id, opening_override=opening)
        persona = context["persona_prompt"]

        # Render the running transcript into the prompt. Keep only the last ~12
        # turns so the prompt stays bounded on long conversations.
        lines = []
        for m in messages[-12:]:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if not content:
                continue
            who = "INVESTIGATOR" if role == "user" else context["subject"]["label"].upper()
            lines.append(f"{who}: {content[:400]}")
        transcript = "\n".join(lines)

        if not LLM_ENABLED:
            return jsonify({"reply": "[static swallows the reply — the channel is dead]"})

        prompt = (
            persona
            + "\n\nCONVERSATION SO FAR:\n"
            + (transcript or "(the investigator has just approached you)")
            + f"\n\nRespond as {context['subject']['label']}, in character, the way a real person would "
            "actually say it out loud — 1 to 2 short sentences, plain and human, never a monologue. "
            "Output ONLY your spoken reply — no name prefix, no stage directions."
        )
        reply = _ask(prompt, temp=0.9, tokens=80, use_lore=False)
        reply = (reply or "").strip().strip('"').strip()
        if _talk_llm_failed(reply):
            reply = "[the channel breaks up — say that again]"
        # Strip an accidental leading "LABEL:" prefix if the model added one.
        label_up = context["subject"]["label"].upper()
        if reply.upper().startswith(label_up + ":"):
            reply = reply[len(label_up) + 1:].strip()
        return jsonify({"reply": reply[:600]})
    except Exception as e:
        import traceback as _tb
        log_error(f"[TALK] message failed: {e}")
        _tb.print_exc()
        return jsonify({"error": str(e), "reply": "…"}), 500


def api_investigate():
    """Store an 'investigation' texture the player captured from the scene.

    The TOUCH tool (and, later, a 'photograph' mechanic) crops a small thumbnail
    from the region around/under the reticle — a close-up specimen of what the
    player is looking at. This persists that crop to disk and records its
    metadata in state['investigations'] so it can seed future scene-driven
    prompt mechanics (interacting with the world by referencing what you've
    examined closely).

    Request JSON: {texture: dataURL, region?:{x,y,w,h} (0..1), kind?, note?, label?}
    Response JSON: the stored entry {id, kind, label, note, region, image_url, ts}.
    The crop is saved as investigation_<ts>.jpg — a .jpg, so (like scan/observe
    grabs) it never leaks into the .png-only VHS tape.
    """
    import base64 as _b64, re as _re, time as _time
    from pathlib import Path as _Path
    global state
    try:
        data = request.get_json(silent=True) or {}
        tex = data.get('texture')
        session_id = data.get('session_id', 'default')
        kind = (str(data.get('kind') or 'touch'))[:24]
        note = (str(data.get('note') or ''))[:400]
        label = (str(data.get('label') or ''))[:120]
        region = data.get('region') if isinstance(data.get('region'), dict) else None
        if not tex:
            return jsonify({"error": "missing texture"}), 400
        m = _re.match(r'^data:image/[^;]+;base64,(.*)$', tex, _re.DOTALL)
        raw = m.group(1) if m else tex
        try:
            img_bytes = _b64.b64decode(raw)
        except Exception:
            return jsonify({"error": "bad texture encoding"}), 400
        if len(img_bytes) < 64:
            return jsonify({"error": "texture too small"}), 400

        img_dir = _Path(_get_image_dir(session_id))
        img_dir.mkdir(parents=True, exist_ok=True)
        ts = int(_time.time() * 1000)
        fname = f"investigation_{ts}.jpg"
        (img_dir / fname).write_bytes(img_bytes)
        entry = {
            "id": ts,
            "kind": kind,
            "label": label,
            "note": note,
            "region": region,
            "image_url": f"/images/{fname}",
            "ts": ts,
        }
        MAX_INVESTIGATIONS = 60  # keep the case file bounded
        with WORLD_STATE_LOCK:
            st = _load_state(session_id)
            invs = st.setdefault('investigations', [])
            invs.append(entry)
            if len(invs) > MAX_INVESTIGATIONS:
                del invs[:-MAX_INVESTIGATIONS]
            _save_state(st, session_id)
            _sync_ambient_state(st, session_id)
        print(f"[INVESTIGATE] stored {kind} specimen {fname}", flush=True)
        return jsonify({"ok": True, **entry})
    except Exception as e:
        log_error(f"[INVESTIGATE] failed: {e}")
        return jsonify({"error": str(e)}), 500


def api_investigations():
    """List stored investigation textures for the session (most recent first)."""
    try:
        session_id = request.args.get('session_id', 'default')
        st = _load_state(session_id)
        invs = list(st.get('investigations', []))
        invs.reverse()
        return jsonify({"investigations": invs})
    except Exception as e:
        log_error(f"[INVESTIGATE] list failed: {e}")
        return jsonify({"investigations": []}), 500


def _spawn_scene_choices_reground(prompt_id, prev_image_url: str, session_id: str = 'default'):
    """Wait (bounded) for THIS turn's guide image to render, then reground the
    live choices on it via vision and push a choices_revised item. Runs entirely
    off the turn's critical path so it can NEVER stall/hang the turn (the prompt
    is already live); if the image never lands it just gives up. Reuses the
    observe reground pipeline so there's one grounding path."""
    if not WORLD_IMAGE_ENABLED or not VISION_ENABLED:
        return

    def _worker():
        try:
            img_dir = _get_image_dir(session_id)
            for _ in range(80):  # ~20s: wait for the async render to land
                st = _load_state(session_id)
                cur = st.get('current_image_url')
                if cur and cur != prev_image_url:
                    fpath = os.path.join(str(img_dir), os.path.basename(cur))
                    if os.path.exists(fpath):
                        # Hand off to the shared reground pipeline (vision +
                        # regenerate choices + push choices_revised).
                        _spawn_observe_reground(fpath, cur, session_id, prompt_id)
                        return
                time.sleep(0.25)
            print(f"[REGROUND] guide image never landed for {session_id}; skipping", flush=True)
        except Exception as e:
            log_error(f"[REGROUND] worker failed: {e}")

    threading.Thread(target=_worker, daemon=True).start()


def _spawn_observe_reground(fpath: str, web: str, session_id: str, prompt_id):
    """Background: run vision on the observed video frame and regenerate choices
    grounded on what's actually visible, then push a 'choices_revised' feed item.
    Kept off the request path (vision + choice LLM calls are slow) so /api/observe
    never blocks or hangs the UI."""
    # Vision + choice LLM calls can be slow/rate-limited; never stack them.
    global _observe_reground_active
    if _observe_reground_active:
        return
    _observe_reground_active = True

    def _worker():
        global state, history, _observe_reground_active
        try:
            print(f"[OBSERVE] reground worker start: {fpath}", flush=True)
            vision = ""
            try:
                vres = _vision_analyze_all(fpath)  # has an internal 30s timeout
                if isinstance(vres, dict):
                    vision = (vres.get("description") or "").strip()
            except Exception as e:
                log_error(f"[OBSERVE] vision failed: {e}")
            print(f"[OBSERVE] vision len={len(vision)}", flush=True)
            if not vision:
                print("[OBSERVE] empty vision — skipping reground", flush=True)
                return

            with WORLD_STATE_LOCK:
                st = _load_state(session_id)
                st['current_observed_vision'] = vision
                hist = _load_history(session_id)
                if hist:
                    hist[-1]['vision_dispatch'] = vision
                    hist[-1]['vision_analysis'] = vision
                    _save_history(hist, session_id)
                    if get_active_session_id() == session_id:
                        history = hist
                _save_state(st, session_id)
                _sync_ambient_state(st, session_id)

            last_dispatch = ""
            for it in reversed(st.get('feed_log', [])):
                if it.get('type') in ('consequence_event', 'narrative_event') and it.get('content'):
                    last_dispatch = it['content']
                    break
            texts = generate_choices(
                client=client,
                prompt_tmpl=PROMPTS["player_choice_generation_instructions"],
                last_dispatch=(last_dispatch or vision),
                image_description=vision,
                image_url=web,
                world_prompt=st.get('world_prompt', ''),
                situation_summary=summarize_world_state(st),
                inventory=st.get('inventory'),
                n=3,
            ) or []
            print(f"[OBSERVE] regenerated {len(texts)} choices", flush=True)
            if not texts:
                return

            item = create_feed_item(
                type="choices_revised",
                content="",
                choices=[{"text": t} for t in texts],
                metadata={"prompt_id": prompt_id},
            )
            with WORLD_STATE_LOCK:
                st = _load_state(session_id)
                st['choices'] = [{"text": t} for t in texts]
                _feed_append(st, item)
                _save_state(st, session_id)
                _sync_ambient_state(st, session_id)
            print(f"[OBSERVE] re-grounded on video frame; {len(texts)} choices", flush=True)
        except Exception as e:
            log_error(f"[OBSERVE] reground failed: {e}")
        finally:
            _observe_reground_active = False

    threading.Thread(target=_worker, daemon=True).start()


def api_regenerate_choices():
    global state
    logging.info("api_regenerate_choices: POST request received.")
    try:
        # Resolve THIS request's session id directly from Flask's request
        # object (see _resolve_request_session_id) rather than the shared
        # get_active_session_id() global — this handler calls a slow LLM
        # (generate_choices) synchronously, so a concurrent different-
        # session request has plenty of time to swap the ambient mirror
        # before we get to the save at the end.
        SID = _resolve_request_session_id()
        with WORLD_STATE_LOCK:
            fresh_state = _load_state(SID)
            current_feed_log = list(fresh_state.get('feed_log', [])) # Operate on a copy for reading context
            state_snapshot_for_context = fresh_state.copy() # For world prompt, etc.

        last_choice_prompt = None
        last_dispatch_text = "No recent dispatch found."

        for item in reversed(current_feed_log):
            if item.get('type') == 'player_choice_prompt':
                last_choice_prompt = item
                # Try to find the narrative dispatch that led to this old choice prompt
                prompt_index = current_feed_log.index(item)
                for prev_item_idx in range(prompt_index -1, -1, -1):
                    prev_item = current_feed_log[prev_item_idx]
                    if prev_item.get('type') == 'narrative_event':
                        last_dispatch_text = prev_item.get('content', last_dispatch_text)
                        break
                break
        
        if not last_choice_prompt:
            logging.warning("api_regenerate_choices: No prior player_choice_prompt found in feed_log.")
            # Use generic context if no prior prompt
            world_prompt_context = state_snapshot_for_context.get("world_prompt", "The situation is unclear.")
            image_desc_context = "The scene is ambiguous."
        else:
            # If we found a previous choice prompt, use its context (or what led to it)
            world_prompt_context = state_snapshot_for_context.get("world_prompt") # Current world prompt is best
            image_desc_context = state_snapshot_for_context.get("current_image_url") # Use current image if any for context
            # If last_choice_prompt had an image_url, that could also be relevant context,
            # but vision_dispatch for current state.current_image_url is better.
            if state_snapshot_for_context.get("current_image_url"):
                 vision_for_regen = _generate_vision_dispatch(last_dispatch_text, world_prompt_context)
                 if vision_for_regen : image_desc_context = vision_for_regen


        from choices import generate_choices
        situation_summary = summarize_world_state(state_snapshot_for_context)
        # generate_choices() returns a plain List[str], not a (choices, meta)
        # tuple — do not unpack it as one (see note in _process_turn_background).
        regenerated_choice_texts = generate_choices(
            client=client,
            prompt_tmpl=PROMPTS["player_choice_generation_instructions"],
            last_dispatch=last_dispatch_text,
            world_prompt=world_prompt_context,
            image_description=image_desc_context,
            situation_summary=situation_summary, # Use general summary
            n=3,
            temperature=0.7 # Slightly higher temp for variety
        )

        prompt_text = last_choice_prompt.get("content", "What do you do now?") if last_choice_prompt else "The path is unclear. Choose wisely."
        # Use current image if available, or the one from the last prompt if that's more relevant
        image_for_new_choices = state_snapshot_for_context.get("current_image_url") # Prefer current image
        if WORLD_IMAGE_ENABLED == False: image_for_new_choices = None


        new_choice_prompt_item = _structure_choices_for_feed(
            regenerated_choice_texts,
            f"[REVISED OPTIONS] {prompt_text}", # Indicate these are regenerated
            image_url=image_for_new_choices
        )

        with WORLD_STATE_LOCK:
            st = _load_state(SID)
            st.setdefault('feed_log', []).append(new_choice_prompt_item)
            st['choices'] = new_choice_prompt_item['choices'] # Update current choices in state
            _save_state(st, SID)
            _sync_ambient_state(st, SID)
        
        logging.info(f"api_regenerate_choices: Regenerated choices. New prompt ID: {new_choice_prompt_item['id']}")
        return jsonify([new_choice_prompt_item])

    except Exception as e:
        log_error(f"Error in api_regenerate_choices: {e}")
        logging.exception("Exception in api_regenerate_choices:")
        error_item = create_feed_item(type="error_event", content=f"Failed to regenerate choices: {str(e)}")
        try:
            err_sid = _resolve_request_session_id()
            with WORLD_STATE_LOCK:
                err_state = _load_state(err_sid)
                err_state.setdefault('feed_log', []).append(error_item)
                _save_state(err_state, err_sid)
                _sync_ambient_state(err_state, err_sid)
        except Exception as e_log:
            log_error(f"Could not save error item to feed_log during api_regenerate_choices error handling: {e_log}")
        return jsonify([error_item]), 500


# ───────── COMBINED dispatch generator (saves 1 API call) ─────────────────────
def _generate_combined_dispatches(choice: str, state: dict, prev_state: dict = None, prev_vision: str = "", current_image: str = None, fate: str = "NORMAL", is_interaction: bool = False) -> tuple[str, str, bool]:
    """
    Generate BOTH narrative dispatch AND vision dispatch in ONE API call.
    Now supports multimodal input - can see the current frame!
    
    Args:
        fate: Luck modifier - "LUCKY", "NORMAL", or "UNLUCKY"

    Returns: (dispatch, vision_dispatch, player_alive, provisional_choices)

    provisional_choices is a best-effort list of next-action options produced in
    THIS same call (see the OUTPUT CONTRACT addendum below). The turn loop can
    use them to skip the separate choice-generation LLM round-trip; it falls
    back to the dedicated generator whenever the list is empty/unusable.
    """
    try:
        # Get previous vision analysis for spatial consistency.
        # Source priority:
        #   1. `prev_vision` arg (passed by caller — preferred, may be richer)
        #   2. `history[-1].vision_analysis` (fallback when caller didn't pass)
        # NOTE: Previously both sources were injected separately ("CURRENT VISUAL
        # SCENE" + "PREVIOUS SCENE"), duplicating identical text and confusing the
        # LLM about which is authoritative. We now use ONE consolidated block.
        prev_vision_analysis = (prev_vision or "").strip()
        if not prev_vision_analysis and history and len(history) > 0:
            last_entry = history[-1]
            if last_entry.get("vision_analysis"):
                prev_vision_analysis = last_entry["vision_analysis"][:300]

        spatial_context = ""
        if prev_vision_analysis:
            spatial_context = (
                f"\n\nCURRENT VISUAL SCENE (the camera is HERE — visible state of the "
                f"world right before your action): {prev_vision_analysis[:300]}\n"
                f"Do NOT change locations unless the choice explicitly moves through a "
                f"door, entrance, or exit. Stay in the same environment. Your "
                f"`visual_scene` field must describe how THIS scene evolves after the "
                f"action — preserve ground type, environment, and visible landmarks."
            )

        # `prev_context` removed: it duplicated `spatial_context`. Both came from
        # the same vision_analysis source. Keeping only the richer label.
        prev_context = ""
        world_prompt = state.get('world_prompt', '')
        
        image_context = ""
        if current_image:
            image_context = "🖼️ ATTACHED IMAGE = CURRENT LOCATION. You are HERE. Do NOT teleport yourself.\n\n"
        
        # Detect timeout penalties
        is_timeout_penalty = any(phrase in choice.lower() for phrase in [
            "crushes you", "hits you", "attacks you", "shoots you", "tears into you",
            "engulfs you", "mauls you", "slams into you", "impacts you", "sustained",
            "to torso", "to limb", "trauma", "burns to", "pressure on", "collapses on"
        ])
        
        # Detect FREE WILL actions (custom actions not in standard choices)
        is_free_will = choice and not any([
            choice.startswith("Approach"),
            choice.startswith("Examine"),
            choice.startswith("Use"),
            choice.startswith("Take"),
            choice.startswith("Look"),
            choice.startswith("Search"),
            choice.startswith("Listen"),
            choice.startswith("Wait"),
            choice == "Intro"
        ])
        
        # Build FREE WILL emphasis if detected
        free_will_header = ""
        if is_free_will:
            try:
                safe_choice = choice[:80].encode('ascii', 'replace').decode('ascii')
                print(f"[FREE WILL ACTION DETECTED] {safe_choice}", flush=True)
            except:
                print(f"[FREE WILL ACTION DETECTED] (choice contains special characters)", flush=True)
            free_will_header = (
                "\n\n🔥🔥🔥 THIS IS A FREE WILL ACTION 🔥🔥🔥\n\n"
                f"The player used FREE WILL to command: \"{choice}\"\n\n"
                "CRITICAL INSTRUCTIONS FOR FREE WILL:\n"
                "1. The player IS PERFORMING this exact action RIGHT NOW\n"
                "2. Describe them DOING the action from first-person perspective\n"
                "3. Show the ATTEMPT - the physical movements, the effort\n"
                "4. THEN show the immediate consequence/result\n"
                "5. Example: If they say 'Kick door' → 'You draw back your leg and slam your boot into the door. [result]'\n"
                "6. Example: If they say 'Climb fence' → 'You grab the chain-link and haul yourself up. [result]'\n"
                "7. Make their command REAL and VISIBLE in the text\n\n"
                "Your dispatch MUST start by showing them performing this specific action.\n"
            )
        
        # Build fate modifier text
        fate_modifier = ""
        if fate == "LUCKY":
            fate_modifier = "\n\n🎰 FATE INTERVENTION - LUCKY:\nSomething breaks your way. Add ONE concrete benefit:\n• Equipment works better than expected\n• Guard patrol turns away at the right moment\n• You find something useful (ammo, cover, distraction)\n• Environmental timing favors you (door unlocked, light goes out)\n• A threat misses or hesitates\nMake it TANGIBLE and HELPFUL, not just flavor text.\n"
        elif fate == "UNLUCKY":
            fate_modifier = "\n\n🎰 FATE INTERVENTION - UNLUCKY:\nFate turns against you. Add ONE concrete dramatic complication. The DEATH FAIRNESS DOCTRINE above still applies — environment can WOUND but only characters/events KILL.\n\n**PRIORITIZE CHARACTER- OR EVENT-DRIVEN DRAMA** (these are the satisfying complications):\n• A character appears or closes in (guard patrol turns toward you, a creature emerges from cover, a sniper acquires you)\n• A dramatic event triggers (explosion the player set off, vehicle screeches in, structural collapse caused by the action)\n• Evidence is discovered against you (alarm trips, camera catches you, radio call alerts the facility)\n• A previously safe ally turns hostile or reveals themselves\n• A creature mauls or a guard fires (still subject to fairness — death only if visible and earned)\n\n**OR SURVIVABLE BODILY HARM** (when no character/event fits — these INJURE, do not kill):\n• Limb is bruised or broken (sprained ankle, fractured wrist, dislocated shoulder)\n• Flesh is burned (caustic splash on hand, scalding steam blistering skin)\n• Tissue is lacerated (barbed wire rips forearm, jagged metal cuts calf — still able to move)\n• Concussion or vision swim from a blow\n• Equipment fails (rope frays, ladder rung breaks, catwalk gives way — fall + injury, not death)\n\n**FORBIDDEN UNDER UNLUCKY (cheap-death patterns):**\n❌ Random impalement on rebar/spike/glass killing the player\n❌ Death by inert environment (falling onto debris with no character driving the scene)\n❌ Spores/contamination killing the player in one beat (slow corruption is fine; sudden death is not)\n\nDescribe injuries with VISCERAL DETAIL when wounds occur — what bruises, what tears, what burns. But unless a CHARACTER or DRAMATIC EVENT is on screen driving it, KEEP `player_alive` = TRUE and let the wound become a persistent burden the player carries forward.\n"
        # NORMAL = no modifier, outcome is purely based on choice

        # ── Entity + injury grounding (fairness) ─────────────────────────────
        # The death-fairness doctrine in the prompt requires that any lethal
        # threat already be visible to the player. Surface the seen entities
        # and persistent injuries so the LLM can honor that contract.
        seen_list = state.get('seen_elements', []) or []
        if isinstance(seen_list, list):
            seen_str = ', '.join(str(e) for e in seen_list[-10:])
        else:
            seen_str = str(seen_list)
        injury_list = state.get('injuries', []) or []
        if isinstance(injury_list, list):
            injury_str = ', '.join(str(i) for i in injury_list) if injury_list else 'none'
        else:
            injury_str = str(injury_list)
        phase_str = state.get('current_phase', 'normal')
        # Phase directives make the STORY PHASE actually STEER the beat — pressure
        # rises turn over turn instead of the world staying flat at "normal".
        phase_directive = {
            "normal": "Establish dread. Threats stay latent and atmospheric, but "
                      "the world is already watching — end on a cue that tightens the noose.",
            "escalating": "STAKES ARE RISING. Press the player: a threat moves closer, a "
                          "complication compounds, or the facility reacts to what they've "
                          "stirred up. Do NOT let this beat idle — something must develop.",
            "critical": "THIS IS THE CLIMAX. Danger is immediate and lethal-capable. Force a "
                        "hard, consequential beat — a threat commits, an event triggers, the "
                        "situation lurches. No stalling, no safe nothing-happens beats.",
        }.get(phase_str, "")

        # SCAN object interactions must MOVE THE STORY, not dead-end on a shrug.
        interaction_directive = ""
        if is_interaction:
            interaction_directive = (
                "\n\nOBJECT INTERACTION: The player is deliberately handling/entering a "
                "specific thing in the scene. This action MUST produce a consequential "
                "development — reveal something new, trigger a mechanism or reaction, "
                "disturb the environment, draw attention, or expose a threat/clue. Never "
                "answer with 'nothing happens' or a purely cosmetic result. Meddling with "
                "the unknown here should carry real risk and push the mystery forward.\n"
            )

        grounding_block = (
            f"\n\nDISCOVERED ENTITIES (these are the only things on the board — "
            f"any LETHAL threat must come from here or the current scene): "
            f"{seen_str or 'none yet'}\n"
            f"INJURY STATE (persistent wounds — reference at least once if non-empty): "
            f"{injury_str}\n"
            f"STORY PHASE: {phase_str} — {phase_directive}\n"
            f"{interaction_directive}"
        )

        # Use JUST the action_consequence_instructions (which has JSON format)
        json_prompt = (
            f"{PROMPTS['action_consequence_instructions']}\n\n"
            f"{free_will_header}"
            f"PLAYER CHOICE: '{choice}'\n"
            f"WORLD CONTEXT: {world_prompt}\n"
            f"{grounding_block}"
            f"{fate_modifier}"
            f"{spatial_context}"
            f"{prev_context}\n\n"
            "Generate the consequence in valid JSON format. In ADDITION to the "
            "mandatory `dispatch`, `visual_scene`, and `player_alive` fields, also "
            "include a fourth field `next_choices`: an array of EXACTLY 3 short "
            "(3-6 word) FIRST-PERSON physical action options for what the player "
            "does NEXT from this new position. Each MUST move the body through the "
            "space or physically manipulate something (NEVER look/observe/wait/"
            "photograph/listen). The three original fields stay mandatory and "
            "their quality must not drop."
        )
        
        # Build parts list (text + optional image)
        parts = [{"text": json_prompt}]
        
        # Add previous timestep image if provided
        if current_image:
            # Use our centralized path resolution
            actual_path = _resolve_image_path(current_image)
            
            if not actual_path or not actual_path.exists():
                print(f"[GEMINI TEXT+IMG] WARNING: Could not resolve image path: {current_image}")
            else:
                # Use pre-downsampled version if available
                small_path = actual_path.parent / actual_path.name.replace(".png", "_small.png")
                use_path = small_path if small_path.exists() else actual_path
                with open(use_path, "rb") as f:
                    import base64
                    image_data = base64.b64encode(f.read()).decode('utf-8')
                
                parts.insert(0, {
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": image_data
                    }
                })
                size_note = "(480x360, 4:3)" if small_path.exists() else "(full-res)"
                print(f"[GEMINI TEXT+IMG] Including PREVIOUS timestep image: {current_image} {size_note}")
        
        # Don't use lore - dispatch is immediate action/consequence (use _ask instead of direct API call)
        import json as json_lib
        result = _ask(
            json_prompt,
            model="gemini",
            temp=1.0,
            tokens=560,  # room for the consequence + the added next_choices array
            image_path=current_image,  # Pass current image if available
            use_lore=False  # Dispatch is mechanical, lore only for world evolution
        )
        
        print("[COMBINED DISPATCH] Complete")
        
        # Strip markdown code fences if present
        if result.startswith("```"):
            lines_raw = result.split("\n")
            if lines_raw[0].startswith("```"):
                lines_raw = lines_raw[1:]
            if lines_raw and lines_raw[-1].strip() == "```":
                lines_raw = lines_raw[:-1]
            result = "\n".join(lines_raw)
        
        # Parse JSON response
        dispatch = ""
        visual_scene = ""
        player_alive = True
        provisional_choices: list = []
        
        try:
            import json as json_lib
            data = json_lib.loads(result)
            dispatch = data.get("dispatch", "")
            visual_scene = data.get("visual_scene", "").strip()
            player_alive = data.get("player_alive", True)
            _raw_choices = data.get("next_choices", []) or []
            if isinstance(_raw_choices, list):
                provisional_choices = [str(c).strip() for c in _raw_choices if str(c).strip()]
            # Safe print with Unicode handling
            try:
                print(f"[DISPATCH] Parsed JSON: dispatch={dispatch[:50]}..., alive={player_alive}")
                if visual_scene:
                    print(f"[DISPATCH] visual_scene={visual_scene[:80]}...")
                else:
                    print(f"[DISPATCH] visual_scene: NOT generated (will fall back to dispatch)")
            except UnicodeEncodeError:
                print(f"[DISPATCH] Parsed JSON: alive={player_alive} (dispatch contains special characters)")
        except Exception as parse_error:
            try:
                print(f"[DISPATCH] JSON parse failed: {parse_error}")
                print(f"[DISPATCH] Raw result: {result[:200]}...")
            except UnicodeEncodeError:
                print(f"[DISPATCH] JSON parse failed (output contains special characters)")
            # Fallback: try to extract dispatch text
            dispatch = result.replace('"dispatch":', '').replace('"player_alive":', '').replace('"visual_scene":', '').replace('{', '').replace('}', '').strip()
            if ',' in dispatch:
                dispatch = dispatch.split(',')[0].strip(' "')
        
        # vision_dispatch = visual scene description for image generation
        # If the LLM generated a proper visual_scene, use it; otherwise fall back to dispatch
        vision_dispatch = visual_scene if visual_scene else dispatch
        
        # Hard cap at 400 characters
        if len(dispatch) > 400:
            dispatch = dispatch[:385] + "...(truncated)"
        if len(vision_dispatch) > 400:
            vision_dispatch = vision_dispatch[:385] + "...(truncated)"
        
        return dispatch, vision_dispatch, player_alive, provisional_choices
        
    except Exception as e:
        try:
            print(f"[COMBINED DISPATCH ERROR] {e}")
        except UnicodeEncodeError:
            print(f"[COMBINED DISPATCH ERROR] (error contains special characters)")
        import traceback
        traceback.print_exc()
        # Fallback to safe defaults
        return "You make a tense move in the chaos.", "The desert stretches ahead.", True, []

def summarize_world_state_diff(prev_state: dict, state: dict) -> str:
    """
    Return a concise summary of the most important differences between two world states.
    """
    diffs = []
    # Major world prompt change
    if prev_state.get('world_prompt', '') != state.get('world_prompt', ''):
        motifs = ["red biome", "creature", "alliance", "alert", "injury", "resource", "threat", "opportunity", "military", "activist", "danger", "quarantine", "mutation", "disaster", "conflict", "chaos", "discovery", "revelation", "attack", "wound", "escape", "surveillance", "protest", "panic", "contamination", "artifact", "ancient", "storm", "explosion", "hostile", "warning", "rumor", "evidence", "mutation", "leader", "broadcast", "rescue", "raid", "sabotage", "betrayal", "alliance broken", "alliance formed"]
        new_prompt = state.get('world_prompt', '').lower()
        if any(m in new_prompt for m in motifs):
            diffs.append(f"World event: {state.get('world_prompt', '')}")
    # Chaos level
    if prev_state.get('chaos_level', 0) != state.get('chaos_level', 0):
        diffs.append(f"Chaos level: {prev_state.get('chaos_level', 0)} -> {state.get('chaos_level', 0)}")
    # Phase
    if prev_state.get('current_phase', 'normal') != state.get('current_phase', 'normal'):
        diffs.append(f"Phase: {prev_state.get('current_phase', 'normal')} -> {state.get('current_phase', 'normal')}")
    # Player state
    if prev_state.get('player_state', {}) != state.get('player_state', {}):
        prev_alive = prev_state.get('player_state', {}).get('alive', True)
        curr_alive = state.get('player_state', {}).get('alive', True)
        if not curr_alive:
            diffs.append("Player is dead or gravely wounded.")
        elif not prev_alive and curr_alive:
            diffs.append("Player revived or recovered.")
        else:
            diffs.append(f"Player state changed")
    # New seen elements
    prev_seen = set(prev_state.get('seen_elements', []))
    curr_seen = set(state.get('seen_elements', []))
    new_seen = curr_seen - prev_seen
    if new_seen:
        motifs = ["red biome", "creature", "alliance", "alert", "injury", "resource", "threat"]
        motif_seen = [e for e in new_seen if any(m in e.lower() for m in motifs)]
        if motif_seen:
            diffs.append(f"New key elements: {', '.join(list(motif_seen)[:3])}")
    if not diffs:
        return "No major world state changes."
    return "; ".join(diffs)

# ───────── story escalation + fate (the "risk" backend) ────────────────────────
# The web/standalone turn loop (which SCAN interactions flow through) used to run
# EVERY turn at a flat NORMAL fate with current_phase pinned to "normal". chaos
# climbed but nothing consumed it, so tension never actually rose and poking an
# object felt inert. The front-end HUD, the escalation sting, the `threat_escalation`
# feed type, the phase-linked prompt directives, and the fate-intervention system
# were all already built — they were simply never driven on the web path. These
# helpers wake that machinery up so every action moves the story forward and
# ratchets risk, and so interacting with a scanned object pushes hardest.
STORY_ESCALATE_AT = 4   # threat pressure at which the story tips into "escalating"
STORY_CRITICAL_AT = 9   # ... and into "critical"

def _phase_for_threat(threat: int) -> str:
    """Map accumulated threat pressure onto the three story phases the rest of
    the engine (prompt directives, world-tick, time-of-day, HUD) already keys off."""
    if threat >= STORY_CRITICAL_AT:
        return "critical"
    if threat >= STORY_ESCALATE_AT:
        return "escalating"
    return "normal"

def compute_fate(risk_bias: float = 0.0) -> str:
    """Roll narrative luck for a turn.

    Base odds mirror the Discord bot: 25% LUCKY / 50% NORMAL / 25% UNLUCKY.
    `risk_bias` (0..0.5) shifts probability out of LUCKY and into UNLUCKY without
    touching the NORMAL band much — so riskier moments (deep escalation, poking
    an unknown object) are genuinely more likely to bite. At bias 0 the split is
    unchanged; higher bias makes fate lean mean.
    """
    bias = max(0.0, min(0.5, risk_bias))
    lucky_cut = max(0.0, 0.25 - bias)          # LUCKY shrinks as risk rises
    unlucky_cut = max(lucky_cut, 0.75 - bias)  # UNLUCKY (roll >= cut) grows with risk
    roll = random.random()
    if roll < lucky_cut:
        return "LUCKY"
    if roll < unlucky_cut:
        return "NORMAL"
    return "UNLUCKY"

def advance_story_dynamics(session_id: str = 'default', risk_boost: int = 0) -> dict:
    """Escalate the persistent story dials for THIS turn and roll its fate.

    • threat_level climbs every turn (base +1, plus `risk_boost` for high-stakes
      moves like SCAN object interactions) so pressure accumulates across a run
      instead of resetting to nothing each turn.
    • current_phase is derived from threat_level (normal → escalating → critical).
      The consequence LLM, world-tick, time-of-day, and HUD systems already react
      to this phase — they were just never handed a rising one on the web path.
    • fate is rolled with a bias that grows as the phase escalates AND when the
      action itself is risky, so late-game turns and object-poking swing harder.

    Mutates + persists session state under the world lock. Returns
    {"fate", "phase", "prev_phase", "threat_level", "escalated"}.
    """
    global state
    with WORLD_STATE_LOCK:
        st = _load_state(session_id)
        prev_phase = st.get("current_phase", "normal")
        threat = int(st.get("threat_level", 0) or 0) + 1 + max(0, int(risk_boost))
        phase = _phase_for_threat(threat)
        st["threat_level"] = threat
        st["current_phase"] = phase
        _save_state(st, session_id)
        _sync_ambient_state(st, session_id)
    phase_bias = {"normal": 0.0, "escalating": 0.12, "critical": 0.22}.get(phase, 0.0)
    # A risk_boost (SCAN interaction / entering the unknown) both accelerated the
    # phase above and tilts THIS turn's luck toward complication.
    risk_bias = phase_bias + (0.15 if risk_boost else 0.0)
    fate = compute_fate(risk_bias)
    escalated = _PHASE_RANK.get(phase, 0) > _PHASE_RANK.get(prev_phase, 0)
    return {"fate": fate, "phase": phase, "prev_phase": prev_phase,
            "threat_level": threat, "escalated": escalated}

_PHASE_RANK = {"normal": 0, "escalating": 1, "critical": 2}

# Short, in-world stings shown when the story tips into a new phase — so the
# player FEELS the stakes climb (paired with the client's escalation sound + HUD).
_PHASE_ESCALATION_BEATS = {
    "escalating": [
        "The facility notices you now. Whatever you stirred is moving.",
        "Something shifts in the dark ahead — the quiet just got heavier.",
        "The air tightens. You are no longer the only thing hunting out here.",
    ],
    "critical": [
        "No more creeping. It's coming for you — fast.",
        "The whole place turns hostile at once. This is where people vanish.",
        "Alarms of instinct scream. Whatever comes next could be the last thing you film.",
    ],
}

def _phase_escalation_beat(phase: str) -> str:
    beats = _PHASE_ESCALATION_BEATS.get(phase)
    return random.choice(beats) if beats else ""

# ───────── game loop ──────────────────────────────────────────────────────────
def advance_turn_image_fast(choice: str, fate: str = "NORMAL", is_timeout_penalty: bool = False, session_id: str = 'default', skip_image: bool = False, skip_evolve: bool = False, interaction: bool = False, local_only: bool = False) -> dict:
    """
    PHASE 1 (FAST): Generate dispatch and image, return immediately.

    skip_image=True   -> don't block on the scene image (feed streams it async).
    skip_evolve=True  -> run the world-evolution rewrite in the background
                         (it only affects the next turn), so the turn's
                         narrative + choices return fast.
    local_only=True   -> operate entirely on LOCAL `state`/`history` variables
                         (loaded + saved per session_id) and never touch the
                         module-global mirrors. The web multi-user path passes
                         this so a concurrent different-session request that
                         swaps the shared mirror can't corrupt this turn's
                         in-flight computation or make its _save_state write the
                         wrong session's data. The Discord bot keeps the default
                         (False), which publishes the result to the globals its
                         interaction handlers read after a turn.
    
    Args:
        session_id: Session ID for state management
    Returns image ASAP so bot can display it while choices are generating.
    
    Args:
        choice: Player's chosen action
        fate: Luck modifier - "LUCKY", "NORMAL", or "UNLUCKY"
        is_timeout_penalty: If True, maintains EXACT camera position (no movement/teleportation)
    """
    # NOTE: `state` / `history` below are LOCAL variables (no `global`
    # declaration on purpose — see local_only in the docstring). The bot path
    # mirrors them into the module globals at the end via _publish_ambient.
    state = None
    history = None

    # CRITICAL: Log everything for Render debugging
    safe_choice = choice[:100].encode('ascii', 'replace').decode('ascii')
    print(f"[ADVANCE_TURN] Choice: '{safe_choice}'")
    print(f"[ADVANCE_TURN] Fate: {fate}")
    print(f"[ADVANCE_TURN] Is Timeout Penalty: {is_timeout_penalty}")
    try:
        # Load session-specific state and history
        state = _load_state(session_id)
        history = _load_history(session_id)
        prev_state = state.copy() if isinstance(state, dict) else dict(state)
        from choices import generate_and_apply_choice, generate_choices
        # Ensure session-specific state path is passed!
        state_file_path = _get_state_path(session_id)
        generate_and_apply_choice(choice, state_path=str(state_file_path))
        state = _load_state(session_id)
        
        # Get previous vision and image
        # Use vision_analysis (actual image analysis) over vision_dispatch (narrative text)
        # for better spatial continuity in the next dispatch generation
        prev_vision = ""
        prev_image = None
        if history and len(history) > 0:
            prev_vision = (history[-1].get("vision_analysis", "") or
                           history[-1].get("vision_dispatch", ""))
            prev_image = history[-1].get("image_url", None)
        
        # TIMEOUT PENALTIES: Use penalty text AS dispatch (don't generate new one)
        provisional_choices: list = []
        if is_timeout_penalty:
            dispatch = choice  # The penalty text IS the consequence
            vision_dispatch = choice
            player_alive = True  # timeout penalties never kill directly; the next turn's consequence LLM judges lethality
            print(f"[TIMEOUT PENALTY] Using penalty text as dispatch: {dispatch[:100]}")
        else:
            # Generate dispatch using FULL StoryGen version (with fate modifier).
            # provisional_choices are produced in the SAME call so the turn loop
            # can skip the separate choice-generation round-trip.
            dispatch, vision_dispatch, player_alive, provisional_choices = _generate_combined_dispatches(choice, state, prev_state, prev_vision, prev_image, fate, is_interaction=interaction)
        
        # SIMPLE DEATH SYSTEM: Just trust the LLM
        state['player_state']['alive'] = player_alive
        
        if not player_alive:
            print(f"[DEATH] Player killed by: {dispatch[:100]}...")
        
        # Save state immediately after death detection
        _save_state(state, session_id)
        print(f"[STATE] Saved - alive={player_alive}, health={state['player_state'].get('health', 100)}")
        
        if not dispatch or dispatch.strip().lower() in {"none", "", "[", "[]"}:
            dispatch = "You make a tense move in the chaos."
        if not vision_dispatch or vision_dispatch.strip().lower() in {"none", "", "[", "[]"}:
            vision_dispatch = dispatch
        
        # Evolve world state.
        consequence_summary = summarize_world_state_diff(prev_state, state)
        if skip_evolve:
            # Feed path: run the (slow, ~1k-token) world evolution in the
            # background so the turn's narrative + choices return fast. It only
            # affects the NEXT turn's world_prompt. evolve_world_state is
            # read-only; the worker merges its result under lock, preserving
            # feed_log.
            _evolve_world_async(session_id, consequence_summary, vision_dispatch)
        else:
            from evolve_prompt_file import evolve_world_state
            state_file_path = _get_state_path(session_id)
            evolution_result = evolve_world_state(history, consequence_summary, state_file=str(state_file_path), vision_description=vision_dispatch)
            state = _load_state(session_id)
            if evolution_result:
                for _k in ("world_prompt", "evolution_summary", "recent_events", "seen_elements"):
                    if _k in evolution_result:
                        state[_k] = evolution_result[_k]
                _save_state(state, session_id)
                print(f"[WORLD EVOLUTION] Applied and saved evolution results for {session_id}")
        
        # Generate image
        mode = state.get("mode", "camcorder")
        frame_idx = len(history) + 1
        # CRITICAL: Timeout penalties NEVER change location
        if is_timeout_penalty:
            hard_transition = False
            print(f"[TIMEOUT PENALTY] Forcing NO location change - maintaining exact camera position")
        else:
            hard_transition = is_hard_transition(choice, dispatch)
        
        consequence_img_url = None
        consequence_img_prompt = ""  # Initialize to prevent undefined variable error
        consequence_video_url = None  # Initialize to prevent UnboundLocalError if _gen_image raises before its internal assignment
        try:
            if skip_image:
                # Feed path streams the image asynchronously; skip the slow
                # inline generation so the turn resolves fast.
                raise _SkipImage()
            last_image_path = None
            if history and len(history) > 0:
                for entry in reversed(history):
                    if entry.get("image"):
                        # DON'T strip leading slash - session images are absolute paths!
                        last_image_path = entry["image"]
                        
                        # Handle both absolute and relative paths
                        if not Path(last_image_path).is_absolute():
                            # Relative path like "/images/..." - make it absolute
                            if last_image_path.startswith("/images/"):
                                last_image_path = str(ROOT / "images" / Path(last_image_path).name)
                        break
            
            print(f"[IMG GEN] About to generate image:")
            safe_choice = choice[:80].encode('ascii', 'replace').decode('ascii')
            print(f"  - Choice: '{safe_choice}'")
            print(f"  - Is timeout penalty: {is_timeout_penalty}")
            print(f"  - Hard transition: {hard_transition}")
            print(f"  - Last image path: {last_image_path}")
            print(f"  - Last image EXISTS: {last_image_path and os.path.exists(last_image_path)}", flush=True)
            print(f"  - Will use img2img: {last_image_path and os.path.exists(last_image_path) and frame_idx > 0}", flush=True)
            
            result = _gen_image(
                vision_dispatch,
                mode,
                choice,
                image_description="",
                use_edit_mode=(last_image_path and os.path.exists(last_image_path)),
                frame_idx=frame_idx,
                dispatch=dispatch,
                world_prompt=state.get("world_prompt", ""),
                hard_transition=hard_transition,
                is_timeout_penalty=is_timeout_penalty,  # Pass flag to image generation
                session_id=session_id,  # Session-specific image directory
                history_ref=history,  # LOCAL history — never the shared global mirror
            )
            consequence_video_url = None
            if result:
                consequence_img_url, consequence_img_prompt, consequence_video_url = result
                print(f"[IMG FAST] Image ready: {consequence_img_url}", flush=True)
            else:
                # Image generation failed (safety block, API error, etc.) - provide graceful fallback
                print(f"[IMG FAST] WARNING: Image generation returned None (likely safety block or API error)")
                print(f"[IMG FAST] Providing fallback 'static' image message")
                consequence_img_url = None  # Explicitly None - bot will show text-only
                consequence_img_prompt = "[Image blocked - content filtered]"
            
            if consequence_video_url:
                print(f"[IMG FAST] Video ready: {consequence_video_url}")
        except _SkipImage:
            pass  # feed path renders the image asynchronously
        except Exception as e:
            print(f"[IMG FAST] Error: {e}")
            import traceback
            traceback.print_exc()
        
        # Bot path only: mirror this turn's result into the module globals its
        # interaction handlers read after the turn. Web (local_only=True) keeps
        # everything local + on-disk so concurrent sessions never collide.
        if not local_only:
            _publish_ambient(st=state, hist=history)

        return {
            "dispatch": dispatch,
            "vision_dispatch": vision_dispatch,
            "consequence_image": consequence_img_url,
            "consequence_image_prompt": consequence_img_prompt,
            "consequence_video": consequence_video_url,  # Video path for HD mode playback
            "hard_transition": hard_transition,  # Track location changes for reference buffer
            "frame_idx": frame_idx,  # for async image generation on the feed path
            "provisional_choices": provisional_choices,  # next-action options from the same LLM call (may be empty)
            "evolution_summary": state.get("evolution_summary", ""),  # Include world changes
            "phase": state["current_phase"],
            "chaos": state["chaos_level"],
            "world_prompt": state.get("world_prompt", ""),
            "mode": state.get("mode", "camcorder")
        }
    except Exception as e:
        print(f"[IMG FAST] Fatal error: {e}")
        import traceback
        traceback.print_exc()
        return {
            "dispatch": f"Error: {e}",
            "vision_dispatch": "",
            "consequence_image": None,
            "phase": "error",
            "chaos": 0,
            "world_prompt": "",
            "mode": "camcorder"
        }

def advance_turn_choices_deferred(consequence_img_url: str, dispatch: str, vision_dispatch: str, choice: str, consequence_img_prompt: str = "", hard_transition: bool = False, session_id: str = 'default', local_only: bool = False, pregenerated_choices: Optional[List[str]] = None) -> dict:
    """
    PHASE 2 (DEFERRED): Generate choices after image is displayed.
    
    Args:
        session_id: Session ID for state management
        local_only: When True (web multi-user path), operate on LOCAL state/
            history only and never touch the module-global mirrors — see
            advance_turn_image_fast's docstring.
    """
    try:
        return _advance_turn_choices_deferred_impl(consequence_img_url, dispatch, vision_dispatch, choice, consequence_img_prompt, hard_transition, session_id, local_only, pregenerated_choices)
    except Exception as e:
        import traceback
        print(f"[PHASE 2] Fatal error in advance_turn_choices_deferred: {e}", flush=True)
        traceback.print_exc()
        return {
            "choices": ["Look around carefully", "Move forward cautiously", "Hold position and observe"],
            "situation_report": "The situation is tense.",
            "consequences": "",
            "player_state": {},
            "evolution_summary": "",
            "streak_reward": None,
            "rare_event": None,
            "danger": False,
            "combat": False
        }


def _advance_turn_choices_deferred_impl(consequence_img_url: str, dispatch: str, vision_dispatch: str, choice: str, consequence_img_prompt: str = "", hard_transition: bool = False, session_id: str = 'default', local_only: bool = False, pregenerated_choices: Optional[List[str]] = None) -> dict:
    """Internal implementation of Phase 2 choice generation.

    `state` / `history` below are LOCAL variables (no `global` on purpose): the
    web path (local_only=True) must not touch the shared module mirrors, and
    the bot path mirrors the result at the end via _publish_ambient."""
    from choices import generate_choices

    state = None
    history = None
    state = _load_state(session_id)
    
    # --- FLIPBOOK GROUNDING ---
    # If flipbook mode is on, analyze BOTH first and last frames for better context
    analysis_img_url = consequence_img_url
    flipbook_first = None
    flipbook_last = None
    
    if state.get("flipbook_mode", False):
        flipbook_first = state.get('flipbook_first_frame')
        flipbook_last = state.get('flipbook_last_frame')
        if flipbook_last and os.path.exists(flipbook_last):
            print(f"[VISION] Flipbook mode - will analyze first and last frames")
            analysis_img_url = flipbook_last  # Primary analysis uses last frame

    # --- VISION ANALYSIS (Moved to top for grounding) ---
    vision_analysis_text  = ""
    _spatial_compass_turn = ""   # directional compass: ahead/left/right/ground/height
    _setting_type_turn    = ""   # environment type: outdoor-desert, indoor-corridor, etc.

    if analysis_img_url and VISION_ENABLED:
        # Analyze BOTH frames if flipbook mode
        if flipbook_first and flipbook_last and os.path.exists(flipbook_first) and os.path.exists(flipbook_last):
            print(f"[VISION] Analyzing FIRST frame: {os.path.basename(flipbook_first)}")
            try:
                first_result = _vision_analyze_all(flipbook_first)
                first_desc   = first_result.get("description", "")
                print(f"[VISION] First frame: {first_desc[:80]}...")
            except Exception as e:
                print(f"[VISION] First frame analysis failed: {e}")
                first_desc = ""

            print(f"[VISION] Analyzing LAST frame: {os.path.basename(flipbook_last)}")
            try:
                last_result = _vision_analyze_all(flipbook_last)
                last_desc   = last_result.get("description", "")
                # Spatial compass comes from the LAST frame (most current position)
                _spatial_compass_turn = last_result.get("spatial", "")
                _setting_type_turn    = last_result.get("setting", "")
                print(f"[VISION] Last frame: {last_desc[:80]}...")
                if _spatial_compass_turn:
                    print(f"[VISION] Spatial compass (last frame): {_spatial_compass_turn[:80]}...")
            except Exception as e:
                print(f"[VISION] Last frame analysis failed: {e}")
                last_desc = ""

            # Combine both descriptions
            if first_desc and last_desc:
                vision_analysis_text = f"ANIMATION CONTEXT:\nStarting position: {first_desc}\nEnding position: {last_desc}"
            elif last_desc:
                vision_analysis_text = last_desc
            elif first_desc:
                vision_analysis_text = first_desc

            print(f"[VISION] Combined flipbook analysis complete")
        else:
            # Single frame analysis (static image or only last frame available)
            print(f"[VISION] Analyzing image for spatial context (source: {'flipbook' if analysis_img_url != consequence_img_url else 'static'})...")
            try:
                vision_result         = _vision_analyze_all(analysis_img_url)
                vision_analysis_text  = vision_result.get("description", "")
                _spatial_compass_turn = vision_result.get("spatial", "")
                _setting_type_turn    = vision_result.get("setting", "")
                if vision_analysis_text:
                    print(f"[VISION] Analysis complete: {vision_analysis_text[:100]}...")
                if _spatial_compass_turn:
                    print(f"[VISION] Spatial compass: {_spatial_compass_turn[:80]}...")
            except Exception as e:
                print(f"[VISION] Analysis failed: {e}")
                vision_analysis_text  = ""

    # FAST PATH: if the consequence call already produced usable next-action
    # options, reuse them and SKIP both the situation-report and choice-generation
    # LLM calls (they exist only to feed choice generation, which we now already
    # have). This collapses the turn's text critical path from ~3 sequential LLM
    # round-trips (dispatch → situation report → choices) down to 1. All state /
    # history bookkeeping below still runs, and the client's vision reground still
    # refines these against the actual rendered frame. Falls back to the full
    # generator whenever the pregenerated list doesn't yield >=2 clean, meaningful
    # options.
    _pregen = None
    if pregenerated_choices:
        try:
            from choices import drop_meaningless_choices, enforce_diversity
            _cleaned = enforce_diversity(drop_meaningless_choices(
                [c for c in pregenerated_choices if c and c.strip()]
            ))
            if len(_cleaned) >= 2:
                _pregen = _cleaned[:3]
        except Exception as _e_pre:
            print(f"[PHASE 2] pregenerated-choices cleaning failed, will generate: {_e_pre}", flush=True)
            _pregen = None

    if _pregen is not None:
        situation_summary = ""
        next_choices = list(_pregen)
        print(f"[PHASE 2] Using {len(next_choices)} provisional choice(s) from the consequence call "
              f"(skipped situation-report + choice LLM calls).", flush=True)
    else:
        # Generate situation summary with BOTH narrative and visual context
        situation_summary = _generate_situation_report(
            current_image=analysis_img_url,
            current_dispatch=dispatch,
            vision_analysis=vision_analysis_text
        )

        next_choices = generate_choices(
            client, PROMPTS["player_choice_generation_instructions"],
            dispatch,
            n=3,
            image_url=analysis_img_url,
            seen_elements=', '.join(state.get('seen_elements', [])[-10:]),  # Last 10 discovered entities
            recent_choices='',
            caption=vision_dispatch,
            image_description=vision_analysis_text, # Now correctly populated!
            world_prompt=state.get('world_prompt', ''),
            temperature=0.7,
            situation_summary=situation_summary,
            injury_state=', '.join(state.get('injuries', []) or []) or 'none',
        )
    
    next_choices = [c for c in next_choices if c and c.strip() and c.strip() != '—']
    if not next_choices:
        next_choices = ["Look around", "Move forward", "Wait"]
    while len(next_choices) < 3:
        next_choices.append("—")
    
    # Save to history (with custom action flag for permanence tracking)
    history = _load_history(session_id)
    is_custom_action = not any(keyword in choice.lower() for keyword in ["move", "advance", "photograph", "examine", "sprint", "climb", "vault", "crawl"])
    
    # CRITICAL TEMPORAL CONTINUITY FIX:
    # In flipbook mode, static images aren't generated, so consequence_img_url is None.
    # Use flipbook_last_frame as the history image so NEXT turn can reference it for img2img continuity!
    history_image = consequence_img_url
    if not history_image and state.get("flipbook_mode", False):
        history_image = state.get("flipbook_last_frame")
        if history_image:
            print(f"[HISTORY] Using flipbook_last_frame as reference for next turn: {os.path.basename(history_image)}")
    
    history_entry = {
        "choice":            choice,
        "is_custom_action":  is_custom_action,
        "dispatch":          dispatch,
        "vision_dispatch":   vision_dispatch,
        "vision_analysis":   vision_analysis_text,
        "spatial_compass":   _spatial_compass_turn,  # directional compass for next-turn anchor
        "setting_type":      _setting_type_turn,     # environment type for next-turn anchor
        "world_prompt":      state.get("world_prompt", ""),
        "image":             history_image,
        "image_url":         history_image,
        "analysis_image":    analysis_img_url,
        "image_prompt":      consequence_img_prompt,
        "hard_transition":   hard_transition,
    }
    history.append(history_entry)
    _save_history(history, session_id)

    # Bot path only: mirror into the module globals its handlers read after a
    # turn. Web (local_only=True) stays fully local + on-disk per session.
    if not local_only:
        _publish_ambient(st=state, hist=history)

    return {
        "choices": next_choices,
        "situation_report": situation_summary,
        "consequences": "",
        "player_state": state.get('player_state', {}),
        "evolution_summary": state.get('evolution_summary', ""), # Include this here too just in case
        "streak_reward": state.get('streak_reward', None),
        "rare_event": state.get('rare_event', None),
        "danger": False,
        "combat": False
    }

def advance_turn(choice: str) -> dict:
    """Atomically advance the simulation by one turn."""
    global state, history, _last_image_path
    try:
        # Phase 1: Image fast
        phase1_result = advance_turn_image_fast(choice)
        
        # Phase 2: Choices deferred
        phase2_result = advance_turn_choices_deferred(
            phase1_result["consequence_image"],
            phase1_result["dispatch"],
            phase1_result["vision_dispatch"],
            choice,
            phase1_result.get("consequence_image_prompt", ""),
            phase1_result.get("hard_transition", False)
        )
        
        # Combine results
        return {
            **phase1_result,
            **phase2_result
        }
    except Exception as e:
        log_error(f"[ADVANCE TURN] {e}")
        import traceback
        traceback.print_exc()
        return {
            "phase": "error",
            "chaos": 0,
            "dispatch": f"Error: {str(e)}",
            "vision_dispatch": "",
            "dispatch_image": None,
            "consequence_image": None,
            "caption": "Error",
            "mode": "camcorder",
            "situation_report": f"An error occurred: {str(e)}",
            "choices": ["Restart", "Continue", "—"],
            "player_state": {},
            "consequences": f"Error: {str(e)}",
            'error': str(e)
        }

# Alias for compatibility
complete_tick = advance_turn

# ───────── state management ──────────────────────────────────────────────────
def get_state(session_id='default'):
    """Get current state for a session"""
    return _load_state(session_id)

def get_history(session_id='default'):
    """Get game history for a session"""
    return _load_history(session_id)

def archive_session(session_id='default', reason='reset'):
    """
    Archive a session before deletion for later review.
    Creates a timestamped archive with all session data.
    
    Args:
        session_id: The session to archive
        reason: Why it's being archived ('reset', 'death', 'manual')
    
    Returns:
        Path to the archive directory, or None if failed
    """
    import shutil
    from datetime import datetime
    
    session_root = ROOT / "sessions" / session_id
    if not session_root.exists():
        print(f"[ARCHIVE] Session {session_id} doesn't exist, nothing to archive")
        return None
    
    # Create archives directory
    archives_root = ROOT / "archives"
    archives_root.mkdir(exist_ok=True)
    
    # Create timestamped archive folder
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_name = f"{session_id}_{timestamp}"
    archive_path = archives_root / archive_name
    
    try:
        print(f"[ARCHIVE] Creating archive: {archive_name}")
        
        # Copy entire session directory
        shutil.copytree(session_root, archive_path)
        
        # Load state and history for metadata
        state = _load_state(session_id)
        history = _load_history(session_id)
        
        # Create archive metadata
        metadata = {
            "session_id": session_id,
            "archive_timestamp": timestamp,
            "archive_reason": reason,
            "turns_completed": state.get('turn_count', 0),
            "player_status": state.get('player_alive', True),
            "final_location": state.get('location', 'unknown'),
            "total_history_entries": len(history),
            "game_duration": "unknown",  # Could calculate from history timestamps
            "world_prompt": state.get('world_prompt', ''),
            "seen_elements": state.get('seen_elements', []),
        }
        
        # Add death info if player died
        if not state.get('player_alive', True) and history:
            last_entry = history[-1]
            metadata["death_turn"] = last_entry.get('turn_count', 0)
            metadata["death_dispatch"] = last_entry.get('dispatch', '')
        
        # Save metadata
        metadata_path = archive_path / "archive_metadata.json"
        metadata_path.write_text(json.dumps(metadata, indent=2))
        
        print(f"[ARCHIVE] SUCCESS - Archived to: {archive_path}")
        print(f"[ARCHIVE]   Turns: {metadata['turns_completed']}")
        print(f"[ARCHIVE]   History entries: {metadata['total_history_entries']}")
        print(f"[ARCHIVE]   Reason: {reason}")
        
        return archive_path
        
    except Exception as e:
        print(f"[ARCHIVE] Failed to create archive: {e}")
        import traceback
        traceback.print_exc()
        return None


def reset_state(session_id='default'):
    """Reset state for a session"""
    global state, history, _last_image_path, _vision_cache
    
    print(f"[RESET] Resetting session: {session_id}")

    # Archive before deletion
    archive_session(session_id, reason='reset')

    # Reset is a session boundary — release any designed voices so we don't
    # keep casts around for a story that no longer exists.
    try:
        import voice_design as _vd
        released = _vd.release_session_voices(session_id)
        if released.get("deleted") or released.get("skipped"):
            print(f"[RESET] voice_design: {released}")
    except Exception as _e:
        print(f"[RESET] voice_design release failed: {_e}")
    
    # Delete session-specific files
    state_path = _get_state_path(session_id)
    history_path = _get_history_path(session_id)
    
    try:
        if state_path.exists():
            os.remove(str(state_path))
            print(f"[RESET] Deleted state file: {state_path}")
    except Exception as e:
        print(f"[RESET] Failed to delete state: {e}")
    
    try:
        if history_path.exists():
            os.remove(str(history_path))
            print(f"[RESET] Deleted history file: {history_path}")
    except Exception as e:
        print(f"[RESET] Failed to delete history: {e}")
    
    # Clear vision cache
    _vision_cache.clear()
    print("[CLEANUP] Cleared vision analysis cache")
    
    # Clear all images from the session's image folder
    image_dir = _get_image_dir(session_id)
    if image_dir.exists():
        image_count = 0
        for image_file in image_dir.glob("*.png"):
            try:
                image_file.unlink()
                image_count += 1
            except Exception as e:
                print(f"[CLEANUP] Failed to delete {image_file.name}: {e}")
        print(f"[CLEANUP] Deleted {image_count} old images from session {session_id}")
    
    # Recreate history as empty list
    _save_history([], session_id)
    
    # Update global history if this is the legacy session
    if session_id == 'legacy':
        history = []
    
    # Generate random starting time/weather/mood for this session
    initial_time = _generate_random_starting_time()
    
    # Recreate state for this session
    intro_state = {
        "world_prompt": PROMPTS["world_initial_state"],
        "current_phase": "normal",
        "chaos_level": 0,
        "turn": 0,
        "time_of_day": initial_time,
        "last_action": "Initial simulation state",
        "situation": "You stand at the edge of the restricted zone, camera in hand.",
        "beat": 0,
        "injuries": [],
        "inventory": ["Nikon F3 camera", "notebook", "flashlight"],
        "location": "desert_edge",
        "environment_type": "desert",
        "turn_count": 0,
        "player_state": {"alive": True}
    }
    _save_state(intro_state, session_id)
    
    # Create/reset session metadata (Minecraft-style world info)
    _create_session_metadata(
        session_id,
        name=f"New Game {session_id[:8]}" if session_id != 'default' else "Default Session",
        description="A new game session in the quarantine zone"
    )
    
    # Update global state if this is the legacy session
    if session_id == 'legacy':
        state = intro_state
    
    _last_image_path = None
    print(f"[RESET] Session {session_id} cleared. Starting fresh.")

def generate_intro_image_fast(session_id='default'):
    """
    PHASE 1 (FAST): Generate ONLY the intro image and basic info.
    Returns immediately so bot can display image while choices are generating.
    """
    global state, _last_image_path
    import random
    
    # Randomize opening scene
    opening_scenes = [
        {
            "prologue": "You survey the Horizon facility from a rocky outcrop in the desert.",
            "vision": "Wide landscape: The Horizon facility sprawls across the red desert valley far below, industrial structures stark against distant mesa formations. Camera positioned on high rocky outcrop overlooking the entire complex from above."
        },
        {
            "prologue": "You observe the facility from a vantage point atop the red mesa.",
            "vision": "Elevated view from mesa top: The entire Horizon facility layout is visible in the valley below—a vast industrial complex surrounded by desert. Wide establishing shot showing the facility small in the distance, with mesa edge and sky visible."
        },
        {
            "prologue": "You observe the facility from a distant ridge overlooking the valley.",
            "vision": "Wide panoramic view: The Horizon research complex sits in the center of a vast desert valley, seen from a high ridge. Afternoon sun illuminates the industrial buildings, with empty desert stretching in all directions."
        },
        {
            "prologue": "You survey the facility from an abandoned lookout tower.",
            "vision": "High vantage point: From the rusted lookout tower platform, the Horizon facility is spread out below in the valley. Wide aerial-style shot showing the full layout of the complex, roads, and surrounding desert terrain."
        },
        {
            "prologue": "You observe the facility from a hilltop to the north.",
            "vision": "Distant overview from elevated hillside: The Horizon facility complex is visible across the valley, industrial buildings and structures clustered together, surrounded by empty desert. Wide landscape composition with facility in middle distance."
        }
    ]
    
    scene = random.choice(opening_scenes)
    prologue = scene["prologue"]
    vision_dispatch = scene["vision"]
    
    state = _load_state(session_id)
    state["world_prompt"] = prologue
    state["current_phase"] = "normal"
    state["chaos_level"] = 0
    state["last_choice"] = ""
    state["seen_elements"] = []
    state["player_state"] = {"alive": True, "health": 100}
    _save_state(state, session_id)
    
    mode = state.get("mode", "camcorder")
    
    # Generate opening image ONLY
    dispatch_img_url = None
    dispatch_img_prompt = ""
    try:
        print("[INTRO FAST] Generating opening image...")
        dispatch_img_url, dispatch_img_prompt, dispatch_video_url = _gen_image(
            vision_dispatch,
            mode,
            "Intro",
            image_description="",
            use_edit_mode=False,
            frame_idx=0,
            dispatch=prologue,
            world_prompt=prologue,
            hard_transition=False,
            session_id=session_id
        )
        # Note: dispatch_video_url not used in intro sequence (Frame 0 is always static image)
        if dispatch_img_url:
            print(f"[INTRO FAST] Image ready for display: {dispatch_img_url}", flush=True)
            _last_image_path = dispatch_img_url
            state['current_image_prompt'] = dispatch_img_prompt
    except Exception as e:
        import traceback
        print(f"[INTRO FAST] Image generation error: {e}", flush=True)
        traceback.print_exc()
    
    return {
        "dispatch": prologue,
        "vision_dispatch": vision_dispatch,
        "dispatch_image": dispatch_img_url,
        "prologue": prologue,
        "world_prompt": prologue,
        "mode": mode
    }

def generate_intro_choices_deferred(image_url: str, prologue: str, vision_dispatch: str, dispatch: str = None, session_id: str = 'default'):
    """
    PHASE 2 (DEFERRED): Generate choices after image is displayed.
    Can run in background while user is looking at the image.
    
    Args:
        session_id: Session ID for state management
    """
    global state, history
    from choices import generate_choices
    
    state = _load_state(session_id)
    
    # --- FLIPBOOK GROUNDING ---
    # If flipbook mode is on, analyze BOTH first and last frames for better context
    analysis_img_url = image_url
    flipbook_first = None
    flipbook_last = None
    
    if state.get("flipbook_mode", False):
        flipbook_first = state.get('flipbook_first_frame')
        flipbook_last = state.get('flipbook_last_frame')
        if flipbook_last and os.path.exists(flipbook_last):
            print(f"[VISION INTRO] Flipbook mode - will analyze first and last frames")
            analysis_img_url = flipbook_last  # Primary analysis uses last frame

    # --- VISION ANALYSIS (Moved to top for grounding) ---
    vision_analysis_text = ""
    if analysis_img_url and VISION_ENABLED:
        # Analyze BOTH frames if flipbook mode
        if flipbook_first and flipbook_last and os.path.exists(flipbook_first) and os.path.exists(flipbook_last):
            print(f"[VISION INTRO] Analyzing FIRST frame: {os.path.basename(flipbook_first)}")
            try:
                first_result = _vision_analyze_all(flipbook_first)
                first_desc = first_result.get("description", "")
                print(f"[VISION INTRO] First frame: {first_desc[:80]}...")
            except Exception as e:
                print(f"[VISION INTRO] First frame analysis failed: {e}")
                first_desc = ""
            
            print(f"[VISION INTRO] Analyzing LAST frame: {os.path.basename(flipbook_last)}")
            try:
                last_result = _vision_analyze_all(flipbook_last)
                last_desc = last_result.get("description", "")
                print(f"[VISION INTRO] Last frame: {last_desc[:80]}...")
            except Exception as e:
                print(f"[VISION INTRO] Last frame analysis failed: {e}")
                last_desc = ""
            
            # Combine both descriptions
            if first_desc and last_desc:
                vision_analysis_text = f"ANIMATION CONTEXT:\nStarting position: {first_desc}\nEnding position: {last_desc}"
            elif last_desc:
                vision_analysis_text = last_desc
            elif first_desc:
                vision_analysis_text = first_desc
                
            print(f"[VISION INTRO] Combined flipbook analysis complete")
        else:
            # Single frame analysis (static image or only last frame available)
            print(f"[VISION INTRO] Analyzing image for spatial context (source: {'flipbook' if analysis_img_url != image_url else 'static'})...")
            try:
                vision_result = _vision_analyze_all(analysis_img_url)
                vision_analysis_text = vision_result.get("description", "")
                if vision_analysis_text:
                    print(f"[VISION INTRO] Analysis complete: {vision_analysis_text[:100]}...")
                else:
                    print(f"[VISION INTRO] No description returned")
            except Exception as e:
                print(f"[VISION INTRO] Analysis failed: {e}")
                vision_analysis_text = ""

    # Generate situation report with vision grounding
    situation_summary = _generate_situation_report(
        current_image=analysis_img_url,
        current_dispatch=dispatch or prologue,
        vision_analysis=vision_analysis_text
    )
    
    # Robust against any failure inside generate_choices — that function now
    # has its own contextual fallback, but a *truly* unexpected exception
    # (e.g. requests library breaking under Render's network) must NOT bubble
    # up to the turn guard and produce "Generating choices failed"
    # filler. We catch it here and let the intro fallback handle the
    # empty list with scene-aware choices.
    try:
        options = generate_choices(
            client, PROMPTS["player_choice_generation_instructions"],
            prologue,  # What's happening in intro
            n=3,
            image_url=analysis_img_url,  # Gemini sees the image directly!
            seen_elements=', '.join(state.get('seen_elements', [])[-10:]),  # Last 10 discovered entities
            recent_choices='',
            caption=vision_dispatch,
            image_description=vision_analysis_text, # Corrected!
            world_prompt=prologue,
            temperature=0.7,
            situation_summary=situation_summary,
            injury_state=', '.join(state.get('injuries', []) or []) or 'none',
        )
    except Exception as _gen_choices_err:
        import traceback
        print(f"[INTRO CHOICES DEFERRED] generate_choices crashed: {_gen_choices_err}", flush=True)
        traceback.print_exc()
        options = []
    
    if len(options) == 1:
        parts = re.split(r"[\/,\x19\x12\-]|  +", options[0])
        options = [p.strip() for p in parts if p.strip()][:3]
    # Don't pad with placeholders - just return what we got
    
    # Save to session-specific history
    # CRITICAL TEMPORAL CONTINUITY FIX (same as regular turns):
    # In flipbook mode, static images aren't generated, so image_url is None.
    # Use flipbook_last_frame as the history image so NEXT turn can reference it for img2img continuity!
    history_image = image_url
    if not history_image and state.get("flipbook_mode", False):
        history_image = state.get("flipbook_last_frame")
        if history_image:
            print(f"[INTRO HISTORY] Using intro flipbook_last_frame as reference for Turn 1: {os.path.basename(history_image)}")
    
    entry = {
        "choice": "Intro",
        "dispatch": prologue,
        "vision_dispatch": vision_dispatch,
        "vision_analysis": vision_analysis_text,  # Now populated from actual vision AI!
        "world_prompt": prologue,
        "image": history_image, # Store reference image for next turn's img2img
        "image_url": history_image,
        "analysis_image": analysis_img_url # Track which image was actually analyzed
    }
    history = [entry]
    _save_history(history, session_id)  # Use session-specific save!
    _save_state(state, session_id)  # Pass session_id!
    
    return {
        "choices": options,
        "phase": state["current_phase"],
        "chaos": state["chaos_level"],
        "player_state": state.get('player_state', {}),
        "vision_analysis": vision_analysis_text
    }

def generate_intro_turn(session_id: str = 'default'):
    """
    Generate the intro turn: dispatch, vision_dispatch, image, and choices,
    using the prologue as the first dispatch and context.
    
    Args:
        session_id: Session ID for state management
    """
    global state, history, _last_image_path
    import random
    from choices import generate_choices
    
    # Randomize opening scene for variety - no specific objects that will haunt subsequent generations
    opening_scenes = [
        {
            "prologue": "You survey the Horizon facility from a rocky outcrop in the desert.",
            "vision": "Wide landscape: The Horizon facility sprawls across the red desert valley far below, industrial structures stark against distant mesa formations. Camera positioned on high rocky outcrop overlooking the entire complex from above."
        },
        {
            "prologue": "You observe the facility from a vantage point atop the red mesa.",
            "vision": "Elevated view from mesa top: The entire Horizon facility layout is visible in the valley below—a vast industrial complex surrounded by desert. Wide establishing shot showing the facility small in the distance, with mesa edge and sky visible."
        },
        {
            "prologue": "You observe the facility from a distant ridge overlooking the valley.",
            "vision": "Wide panoramic view: The Horizon research complex sits in the center of a vast desert valley, seen from a high ridge. Afternoon sun illuminates the industrial buildings, with empty desert stretching in all directions."
        },
        {
            "prologue": "You survey the facility from an abandoned lookout tower.",
            "vision": "High vantage point: From the rusted lookout tower platform, the Horizon facility is spread out below in the valley. Wide aerial-style shot showing the full layout of the complex, roads, and surrounding desert terrain."
        },
        {
            "prologue": "You observe the facility from a hilltop to the north.",
            "vision": "Distant overview from elevated hillside: The Horizon facility complex is visible across the valley, industrial buildings and structures clustered together, surrounded by empty desert. Wide landscape composition with facility in middle distance."
        }
    ]
    
    scene = random.choice(opening_scenes)
    prologue = scene["prologue"]
    vision_dispatch = scene["vision"]
    
    state = _load_state(session_id)
    state["world_prompt"] = prologue
    state["current_phase"] = "normal"
    state["chaos_level"] = 0
    state["last_choice"] = ""
    state["seen_elements"] = []
    state["player_state"] = {"alive": True, "health": 100}
    _save_state(state, session_id)
    
    dispatch = prologue
    mode = state.get("mode", "camcorder")
    
    # Generate opening image to establish the scene
    dispatch_img_url = None
    dispatch_img_prompt = ""
    image_description = ""
    try:
        print("[INTRO] Generating opening image...")
        dispatch_img_url, dispatch_img_prompt, dispatch_video_url = _gen_image(
            vision_dispatch,  # Visual description of the opening scene
            mode,
            "Intro",
            image_description="",
            use_edit_mode=False,  # No previous image
            frame_idx=0,  # First frame
            dispatch=dispatch,
            world_prompt=prologue,
            hard_transition=False,
            session_id=session_id
        )
        # Note: dispatch_video_url not used in intro sequence (Frame 0 is always static image)
        if dispatch_img_url:
            print(f"[INTRO] Opening image generated: {dispatch_img_url}")
            _last_image_path = dispatch_img_url
            state['current_image_prompt'] = dispatch_img_prompt
            image_description = ""  # Not needed anymore
        else:
            print("[INTRO] Image generation returned None")
    except Exception as e:
        print(f"[INTRO] Error generating opening image: {e}")
        import traceback
        traceback.print_exc()
    
    situation_summary = summarize_world_state(state)
    # Same defensive wrapping as generate_intro_choices_deferred — the choice
    # generator already has a built-in contextual fallback, but a fatal
    # exception in any of its internal helpers (e.g. choice_critic LLM call
    # raising a network error) must not propagate into the bot's intro flow.
    try:
        options = generate_choices(
            client, PROMPTS["player_choice_generation_instructions"],
            dispatch,  # What's happening now
            n=3,
            image_url=dispatch_img_url,  # Opening image - Gemini looks at THIS!
            seen_elements=', '.join(state.get('seen_elements', [])[-10:]),  # Last 10 discovered entities
            recent_choices='',
            caption="",  # Let Gemini see the actual image
            image_description="",  # Let Gemini see the actual image
            time_of_day="",  # Removed - prevents outdoor lighting descriptions
            world_prompt=prologue,
            temperature=0.7,
            situation_summary=situation_summary,
            injury_state=', '.join(state.get('injuries', []) or []) or 'none',
        )
    except Exception as _intro_choices_err:
        import traceback
        print(f"[INTRO TURN] generate_choices crashed: {_intro_choices_err}", flush=True)
        traceback.print_exc()
        options = []
    if len(options) == 1:
        parts = re.split(r"[\/,\x19\x12\-]|  +", options[0])
        options = [p.strip() for p in parts if p.strip()][:3]
    # Don't pad with placeholders - just return what we got
    # Analyze intro image with Vision AI for spatial grounding
    vision_analysis_text = ""
    if dispatch_img_url and VISION_ENABLED:
        print(f"[VISION INTRO FULL] Analyzing opening image for spatial context...")
        try:
            vision_result = _vision_analyze_all(dispatch_img_url)
            vision_analysis_text = vision_result.get("description", "")
            if vision_analysis_text:
                print(f"[VISION INTRO FULL] Analysis complete: {vision_analysis_text[:100]}...")
            else:
                print(f"[VISION INTRO FULL] No description returned")
        except Exception as e:
            print(f"[VISION INTRO FULL] Analysis failed: {e}")
            vision_analysis_text = ""
    
    entry = {
        "choice": "Intro",
        "dispatch": dispatch,
        "vision_dispatch": vision_dispatch,
        "vision_analysis": vision_analysis_text,  # Now populated from actual vision AI!
        "world_prompt": prologue,
        "image": dispatch_img_url  # Include opening image
    }
    history = [entry]
    _save_history(history, session_id)
    # _last_image_path is already set above if image was generated
    _save_state(state, session_id)
    return {
        "dispatch": dispatch,
        "vision_dispatch": vision_dispatch,
        "dispatch_image": dispatch_img_url,
        "choices": options,
        "caption": vision_dispatch,
        "mode": mode,
        "phase": state["current_phase"],
        "chaos": state["chaos_level"],
        "player_state": state.get('player_state', {})
    }
