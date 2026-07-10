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
import json
import os
import random
import re
import sys
import threading
import time # Added for sleep
from datetime import datetime, timezone
from pathlib import Path
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

# DEBUG: Log API keys at module initialization
print(f"[ENGINE INIT] GEMINI_API_KEY loaded: {'YES' if GEMINI_API_KEY else 'NO (EMPTY!)'}")
if GEMINI_API_KEY:
    print(f"[ENGINE INIT] Key: {GEMINI_API_KEY[:20]}...{GEMINI_API_KEY[-8:]} (len={len(GEMINI_API_KEY)})")
print(f"[ENGINE INIT] Source: os.getenv={bool(os.getenv('GEMINI_API_KEY'))}, config={bool(CONFIG.get('GEMINI_API_KEY'))}")

# Load prompts
PROMPTS = json.load((ROOT/"prompts"/"simulation_prompts.json").open(encoding="utf-8"))

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

IMAGE_ENABLED       = True  # ENABLED for production
WORLD_IMAGE_ENABLED = True  # ENABLED for production
QUALITY_MODE        = True  # Quality mode: False=Gemini Flash (fast), True=Gemini Pro (high quality, slower)
VEO_MODE_ENABLED    = False # DISABLED by default - use video generation instead of images

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

# Use Gemini by default (OpenAI is legacy)
IMAGE_PROVIDER = CONFIG.get("IMAGE_PROVIDER", "gemini").lower()
print(f"[ENGINE INIT] IMAGE_PROVIDER: {IMAGE_PROVIDER}")

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

# Dummy log_error if not present, for robustness
def log_error(message: str):
    print(f"ERROR: {message}", file=sys.stderr, flush=True)

class _SkipImage(Exception):
    """Internal sentinel: skip inline image generation (feed path streams it)."""
    pass


def _to_web_image_url(image_path) -> Optional[str]:
    """Convert an image path from _gen_image into a browser-servable URL for
    the standalone feed.

    _gen_image (Gemini path) returns an absolute filesystem path
    (sessions/<id>/images/<file>.png), which the Discord bot attaches directly
    but a web browser cannot load. The standalone /images/<filename> route
    serves those files by basename, so feed items must reference that URL form.
    Returns None for falsy/failed inputs; passes through values already in
    '/images/..' form.
    """
    if not image_path:
        return None
    s = str(image_path)
    if s.startswith("/images/"):
        return s
    return "/images/" + os.path.basename(s)


# Markers that mean "the LLM/tooling failed" rather than "this is story text".
# When any of these appear as the dispatch, we must NOT show it to the player —
# it breaks immersion (e.g. "Signal interrupted due to API error..."). Instead
# we substitute a diegetic, in-world line so a transient outage still reads as
# part of the 1993 camcorder-horror fiction.
_DISPATCH_FAILURE_MARKERS = (
    "signal interrupted",
    "api error",
    "transmission wavers",
    "could not read image",
    "error:",
    "gemini_api_key",
    "openai_api_key",
    "not configured",
)

# Diegetic fallbacks, phrased as camcorder-glitch beats so the player reads them
# as tension rather than as an app failure. We rotate through them so repeated
# degraded turns don't loop the exact same sentence.
_DIEGETIC_DISPATCHES = [
    "The camcorder viewfinder floods with static, then snaps back. For a heartbeat you saw nothing at all — now the desert is closer than before.",
    "Your tape stutters. The audio drops to a low hum and the frame smears, then steadies. Whatever you did, the world took a breath and held it.",
    "A wave of interference rolls across the lens. When it clears, the light has shifted and the silence feels deliberate, like something waited for the picture to break.",
    "The battery indicator flickers red. The image ghosts, doubles, resolves. You are still moving forward, and the quarantine line is nearer than it should be.",
    "Static swallows the shot. You keep the camera rolling on instinct; when the picture returns the shadows have rearranged themselves and the air tastes of iron.",
]


def _is_failure_dispatch(text) -> bool:
    """True when a dispatch string is actually an error sentinel, not story."""
    t = (text or "").strip().lower()
    if not t:
        return True
    return any(m in t for m in _DISPATCH_FAILURE_MARKERS)


def _diegetic_dispatch(choice: str = "") -> str:
    """Return an in-world line to show when dispatch generation failed, so a
    transient API outage still reads as part of the fiction. Deterministically
    varied by the player's choice + wall clock so consecutive failures differ."""
    seed = f"{choice}|{int(time.time() // 7)}"
    idx = abs(hash(seed)) % len(_DIEGETIC_DISPATCHES)
    return _DIEGETIC_DISPATCHES[idx]


# ───────── simulation feedback helpers (fate / phase / injuries) ─────────────
# The web turn used to hardcode fate=NORMAL, leaving the LUCKY/UNLUCKY drama
# engine (which the Discord path rolls per turn) switched off. This mirrors
# bot.compute_fate() but is phase-aware and dependency-free (no discord import).
def _roll_fate(phase: str = "normal") -> str:
    """Weighted luck roll for a turn. As the phase escalates the odds skew
    toward UNLUCKY so the world gets more hostile the deeper the player goes."""
    import random as _random
    r = _random.random()
    if phase == "critical":       # 15% lucky / 45% normal / 40% unlucky
        return "LUCKY" if r < 0.15 else ("NORMAL" if r < 0.60 else "UNLUCKY")
    if phase == "escalating":     # 20% / 50% / 30%
        return "LUCKY" if r < 0.20 else ("NORMAL" if r < 0.70 else "UNLUCKY")
    return "LUCKY" if r < 0.25 else ("NORMAL" if r < 0.75 else "UNLUCKY")  # normal


# Phase names (normal → escalating → critical) match what the consequence and
# world-tick prompts already reference for time-of-day progression; nothing
# advanced the phase before, so it was a constant. chaos_level bumps +1/turn.
def _escalate_phase(state: dict) -> str:
    chaos = int(state.get("chaos_level", 0) or 0)
    if chaos >= 12:
        phase = "critical"
    elif chaos >= 6:
        phase = "escalating"
    else:
        phase = "normal"
    state["current_phase"] = phase
    return phase


# Injury signals — words that mean the player took persistent bodily harm. We
# require one of these (not bare body-part words) to avoid false positives.
_INJURY_SIGNALS = (
    "bleed", "blood", "wound", "gash", "laceration", "lacerat", "sprain",
    "fracture", "broken bone", "burn", "scorch", "sear", "graze", "grazed",
    "bruise", "bruised", "dislocat", "impaled", "puncture", "torn muscle",
    "gouge", "twisted ankle", "cracked rib", "split lip", "deep cut",
)


def _extract_injury(dispatch: str) -> Optional[str]:
    """Pull a concise, grounded wound label from the dispatch prose when it
    describes bodily harm, so injuries can persist into future turns. Returns
    None when no injury is described."""
    if not dispatch:
        return None
    low = dispatch.lower()
    if not any(sig in low for sig in _INJURY_SIGNALS):
        return None
    import re as _re
    for sentence in _re.split(r'(?<=[.!?])\s+', dispatch.strip()):
        sl = sentence.lower()
        if any(sig in sl for sig in _INJURY_SIGNALS):
            wound = sentence.strip()
            return (wound[:87] + "...") if len(wound) > 90 else wound
    return None


def _apply_injuries(state: dict, dispatch: str, is_timeout_penalty: bool = False) -> None:
    """Persist wounds described in the dispatch onto state['injuries'] (a list
    of short strings the dispatch/choice prompts already read), with natural
    decay: capped at 3 (FIFO), and a chance to heal the oldest on wound-free
    turns so the player isn't permanently crippled."""
    import random as _random
    injuries = [i for i in (state.get("injuries", []) or []) if isinstance(i, str)]
    wound = _extract_injury(dispatch) if not is_timeout_penalty else None
    if wound:
        # Avoid logging a near-duplicate of the most recent wound.
        if not injuries or injuries[-1][:30].lower() != wound[:30].lower():
            injuries.append(wound)
            injuries = injuries[-3:]  # cap → old wounds age out
            print(f"[INJURY] recorded: {wound[:60]}")
    elif injuries and _random.random() < 0.30:
        healed = injuries.pop(0)  # a wound-free turn: oldest wound recovers
        print(f"[INJURY] healed: {healed[:40]}")
    state["injuries"] = injuries

# ───────── prompt fragments ──────────────────────────────────────────────────
choice_tmpl     = PROMPTS["player_choice_generation_instructions"]
dispatch_sys    = PROMPTS["action_consequence_instructions"]
neg_prompt      = PROMPTS["image_negative_prompt"]
narrative_tmpl  = PROMPTS["field_notes_format"]

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

def summarize_world_state(state: dict) -> str:
    """
    Return a single, actionable, dynamic sentence summarizing the most important, immediate world state or threat.
    Prioritize: player danger, pursuit, injury, chaos, visible threats, or urgent objectives.
    
    NOTE: This function is unused (dead code). Uses third-person "Jason" which breaks immersion.
    Consider removing entirely.
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
                return "Signal interrupted due to timeout..."
            except requests.exceptions.HTTPError as e:
                print(f"[GEMINI TEXT ERROR] HTTP error: {e}", flush=True)
                return "Signal interrupted due to API error..."
            except Exception as e:
                print(f"[GEMINI TEXT ERROR] Unexpected error: {type(e).__name__}: {e}", flush=True)
                import traceback
                traceback.print_exc()
                return "Signal interrupted..."
        if response_data is None:
            return "Signal interrupted due to rate limiting..."
        
        # Check for error response from Gemini API
        if "candidates" not in response_data:
            print(f"[ASK GEMINI ERROR] Gemini API error response: {response_data}", flush=True)
            if "error" in response_data:
                error_details = response_data['error']
                print(f"[ASK GEMINI ERROR] Code: {error_details.get('code')}, Message: {error_details.get('message')}", flush=True)
            return "The transmission wavers... static fills the air as the signal struggles to maintain connection."
        
        result = response_data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return result if result else "..."
    except Exception as e:
        # Catch any unexpected errors not handled above
        log_error(f"[ASK GEMINI CRITICAL] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
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
        return result if result else "..."
    except Exception as e:
        log_error(f"[ASK OPENAI] {e}")
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
        return result if result else "..."
    except Exception as e:
        log_error(f"[ASK CLAUDE] {e}")
        import traceback
        traceback.print_exc()
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

# ───────── world report (with vision‑desc) ─────────────────────────────────
def _world_report() -> str:
    base = narrative_tmpl.format(
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
            f"{dispatch_sys}\n\n"
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

def is_hard_transition(choice: str, dispatch: str) -> bool:
    """
    Detect if player's CHOICE indicates a location change.
    ONLY checks the player's choice, NOT the LLM's narrative dispatch.
    This prevents false positives from dramatic language (fall, crumple, etc.)
    """
    # Only check ACTUAL location change keywords (not movement/action keywords)
    location_keywords = [
        'enter ', 'step inside', 'go inside', 'walk inside', 'move inside',
        'step outdoors', 'go outdoors', 'walk outdoors', 'move outdoors',
        'exit ', 'leave ', 'open door', 'open the door', 'through the door',
        'cross into', 'cross over', 'cross through',
        'enter the facility', 'enter facility', 'enter building', 'enter the building',
        'into the facility', 'into facility', 'into building', 'into the building',
        'red biome', 'new location', 'different room', 'different area',
        'teleport', 'wake up in', 'dragged to', 'carried to', 'transported to'
    ]
    
    # ONLY check the player's choice (intentional movement)
    # DO NOT check dispatch (LLM narrative can contain "fall", "crumple", etc.)
    choice_lower = choice.lower()
    
    # Check for exact keyword matches in player choice
    has_transition = any(k in choice_lower for k in location_keywords)
    
    if has_transition:
        safe_choice = choice.encode('ascii', 'replace').decode('ascii')
        print(f"[HARD TRANSITION] Detected in choice: '{safe_choice}' - new location (maintaining lighting/aesthetic)")
    
    return has_transition

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


def _gen_image(caption: str, mode: str, choice: str, previous_image_url: Optional[str] = None, previous_caption: Optional[str] = None, previous_mode: Optional[str] = None, strength: float = 0.25, image_description: str = "", time_of_day: Optional[str] = None, use_edit_mode: bool = False, frame_idx: int = 0, dispatch: str = "", world_prompt: str = "", hard_transition: bool = False, is_timeout_penalty: bool = False, session_id: str = 'default') -> Optional[tuple[str, str, Optional[str]]]:
    """Generate image and return (image_path, prompt_used, video_path).
    
    video_path is None for non-Veo providers or when video generation fails/disabled.
    
    time_of_day: If None, will use state['time_of_day'] for consistency
    session_id: Session ID for storing images in correct directory
    """
    global _last_image_path
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
        prev_img_paths = []
        prev_img_captions = []
        prev_vision_analysis = ""  # Vision AI description of last frame
        prev_spatial         = ""  # Spatial compass: ahead/left/right/ground/height
        prev_setting         = ""  # Environment type: outdoor-desert, indoor-corridor, etc.
        prev_img_path = None
        prev_img_paths_list = []  # List of recent image paths for multi-img2img
        
        if frame_idx > 0 and history:
            last_imgs = []
            # Use 2 reference images for better stability
            num_images_to_collect = 2
            print(f"\n{'='*70}")
            print(f"[IMG2IMG COLLECT] Frame {frame_idx} - Starting reference collection")
            print(f"[IMG2IMG COLLECT] History has {len(history)} entries")
            print(f"[IMG2IMG COLLECT] Collecting up to {num_images_to_collect} reference images")
            print(f"[IMG2IMG COLLECT] Will stop at last hard transition (location change)")
            print(f"{'='*70}\n")
            
            for idx, entry in enumerate(reversed(history)):
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
                    ))
                    print(f"[IMG2IMG COLLECT]   -> Added to reference list (total: {len(last_imgs)})")
                    
                    # CRITICAL: Stop collecting after a hard transition
                    # This creates a "reference buffer" that resets on location changes
                    if was_hard_transition and len(last_imgs) > 0:
                        print(f"[IMG2IMG COLLECT] HARD TRANSITION DETECTED - Stopping collection here")
                        print(f"[IMG2IMG COLLECT] Reference buffer reset at location change")
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
                # Get most recent entry — unpack all 5 fields
                img, cap, vis_analysis, spatial_compass, setting_type = last_imgs[0]
                prev_vision_analysis = vis_analysis
                prev_spatial         = spatial_compass
                prev_setting         = setting_type
                prev_img_path = str(_resolve_image_path(img))
                if not os.path.exists(prev_img_path):
                    prev_img_path = None
                
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
                
                if USE_FRAME_0_ANCHOR and len(history) > 0 and frame_idx > 1:
                    frame_0_image = history[0].get("image")
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
        # ALWAYS maintain lighting/aesthetic continuity, even during location changes
        if prev_img_paths:
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
                    # NOTE: For subsequent turns, we do NOT clear current_flipbook_url (bot.py clears it after display)
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
            raise ValueError(f"Unknown IMAGE_PROVIDER: {active_image_provider}. Supported: 'openai', 'gemini', 'veo'")
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
        client, choice_tmpl,
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
def _process_turn_background(choice: str, initial_player_action_item_id: int, signal_file_path: Optional[str] = None):
    """Standalone feed turn — a thin adapter over the canonical two-phase
    session pipeline.

    This runs in the background thread spawned by api_choose. It delegates the
    actual turn work to advance_turn_image_fast + advance_turn_choices_deferred
    on the 'default' session (the same pipeline the Discord/session path uses),
    then translates their return dicts into the feed items the standalone UI
    polls for. It replaces the previous ~600-line duplicate orchestration so
    there is now ONE turn implementation.

    Design decisions (previously divergent between the two paths):
      • Death: honor the consequence LLM's player_alive verdict (Phase 1),
        not a second check_player_death call.
      • Image + flipbook: produced inside _gen_image (Phase 1); no separate
        standalone flipbook copy.
      • Fate: standalone rolls fate per turn (_roll_fate), phase-biased, so the
        LUCKY/UNLUCKY drama engine is live on the web path (not hardcoded NORMAL).
    """
    if signal_file_path:
        try:
            Path(signal_file_path).write_text("THREAD SPAWNED AND WROTE TO FILE")
        except Exception:
            pass

    global state, history
    time.sleep(0.75)  # brief pacing delay so the client renders the action first

    SID = 'default'
    try:
        # Roll fate per turn (LUCKY / NORMAL / UNLUCKY), biased by the current
        # phase so the world turns more hostile as chaos rises. This connects the
        # web path to the same drama engine the Discord path already uses; it was
        # previously hardcoded to NORMAL, making every web turn flat.
        _pre = _load_state(SID)
        fate = _roll_fate(_pre.get("current_phase", "normal"))
        print(f"[FATE] rolled {fate} (phase={_pre.get('current_phase', 'normal')})", flush=True)

        # ── PHASE 1: consequence dispatch only (fast text; NO image, async evolve) ──
        # Image and world-evolution run in the background so narrative + choices
        # return fast.
        p1 = advance_turn_image_fast(choice, fate=fate, is_timeout_penalty=False, session_id=SID, skip_image=True, skip_evolve=True)
        state = _load_state(SID)

        dispatch_text = (p1.get("dispatch") or "").strip() or "The situation evolves..."
        consequence_img_url = None  # streamed in asynchronously below
        vision_dispatch_text = p1.get("vision_dispatch", "")
        player_alive = state.get("player_state", {}).get("alive", True)

        turn_items: List[Dict[str, Any]] = [
            create_feed_item(
                type="narrative_event",
                content=dispatch_text,
                metadata={"source": "dispatch", "degraded": bool(p1.get("degraded"))},
            )
        ]

        # Item pickup detection (feed notification; inventory itself is in state).
        try:
            from items import detect_item_pickups, add_items_to_inventory, ITEMS
            current_inventory = state.get("inventory", [])
            picked_up = detect_item_pickups(dispatch_text, current_inventory)
            if picked_up:
                updated_inventory, didnt_fit = add_items_to_inventory(current_inventory, picked_up)
                state["inventory"] = updated_inventory
                names = [ITEMS[i]["display"] for i in picked_up if i in ITEMS]
                if names:
                    turn_items.append(create_feed_item(type="inventory_pickup", content=f"\U0001F392 **Picked up:** {', '.join(names)}"))
                overflow = [ITEMS[i]["display"] for i in (didnt_fit or []) if i in ITEMS]
                if overflow:
                    turn_items.append(create_feed_item(type="inventory_full", content=f"\u26A0\uFE0F Inventory full! Couldn't pick up: {', '.join(overflow)}"))
        except Exception as e_pick:
            log_error(f"Error detecting item pickups: {e_pick}")

        with WORLD_STATE_LOCK:
            state.setdefault("feed_log", []).extend(turn_items)
            _save_state(state, SID)

        # ── DEATH: single mechanism — the Phase 1 player_alive verdict ──
        if not player_alive:
            # Still render the death moment's scene image — it streams in
            # behind the "YOU DIED" overlay and lands on the tape.
            _spawn_scene_image_async(
                caption=vision_dispatch_text or dispatch_text,
                dispatch=dispatch_text,
                choice=choice,
                frame_idx=int(p1.get("frame_idx", 1)),
                world_prompt=state.get("world_prompt", ""),
                hard_transition=bool(p1.get("hard_transition", False)),
                session_id=SID,
            )
            game_over_item = create_feed_item(type="game_over", content="You have succumbed to the horrors. The transmission ends.")
            game_over_choices = _structure_choices_for_feed(
                ["Restart Simulation"], "GAME OVER",
                image_url=state.get("current_image_url"),
            )
            with WORLD_STATE_LOCK:
                state.setdefault("feed_log", []).append(game_over_item)
                state.setdefault("feed_log", []).append(game_over_choices)
                state["turn_count"] = int(state.get("turn_count", 0)) + 1
                _save_state(state, SID)
            return

        # ── PHASE 2: choices FIRST so this turn's history entry exists ──
        # advance_turn_choices_deferred appends the history record. Running it
        # before the image spawn lets the async image+vision worker reliably
        # attach its rendered frame + vision analysis to THIS turn's entry,
        # closing the vision→story loop for the next turn.
        p2 = advance_turn_choices_deferred(
            None, dispatch_text, vision_dispatch_text, choice,
            "", p1.get("hard_transition", False), SID,
        )
        state = _load_state(SID)

        # ── Stream the scene image asynchronously (don't block on it). The
        # worker also analyzes the rendered frame and folds it back into the
        # history entry Phase 2 just created. ──
        _spawn_scene_image_async(
            caption=vision_dispatch_text or dispatch_text,
            dispatch=dispatch_text,
            choice=choice,
            frame_idx=int(p1.get("frame_idx", 1)),
            world_prompt=state.get("world_prompt", ""),
            hard_transition=bool(p1.get("hard_transition", False)),
            session_id=SID,
        )

        next_choices = [c for c in (p2.get("choices") or []) if c and c.strip() and c.strip() != "\u2014"]
        prompt_item = _structure_choices_for_feed(
            next_choices, "What do you do next?",
            state.get("current_image_url"),
        )

        with WORLD_STATE_LOCK:
            state.setdefault("feed_log", []).append(prompt_item)
            state["turn_count"] = int(state.get("turn_count", 0)) + 1
            MAX_FEED_LOG_ITEMS = 100  # keep feed_log manageable
            if len(state.get("feed_log", [])) > MAX_FEED_LOG_ITEMS:
                state["feed_log"] = state["feed_log"][-MAX_FEED_LOG_ITEMS:]
            _save_state(state, SID)

    except Exception as e_critical:
        log_error(f"Critical unhandled error in _process_turn_background thread: {e_critical}")
        logging.exception("CRITICAL EXCEPTION in _process_turn_background thread top level:")
        try:
            critical_error_item = create_feed_item(type="error_event", content=f"System critical error during turn processing: {e_critical}")
            with WORLD_STATE_LOCK:
                current_state_for_err = state if ('state' in globals() and state) else _load_state(SID)
                current_state_for_err.setdefault("feed_log", []).append(critical_error_item)
                _save_state(current_state_for_err, SID)
        except Exception as e_final_log:
            log_error(f"Could not even log critical error to feed_log: {e_final_log}")
    # No return value: runs in a thread and mutates global state / feed_log.


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

def _spawn_scene_image_async(caption: str, dispatch: str, choice: str, frame_idx: int,
                             world_prompt: str, hard_transition: bool = False,
                             session_id: str = 'default'):
    """Generate a scene image in the background and append it to the session
    feed when ready, so the turn/intro HTTP response never blocks on the slow
    image call. The browser polls /api/feed and streams the scene in.
    """
    if not WORLD_IMAGE_ENABLED:
        return

    def _worker():
        global state, history
        try:
            # _gen_image reads the module-global `history` to collect img2img
            # reference frames — make sure it reflects this session.
            history = _load_history(session_id)
            result = _gen_image(
                caption=caption or dispatch,
                mode="normal",
                choice=choice,
                dispatch=dispatch,
                world_prompt=world_prompt,
                hard_transition=hard_transition,
                frame_idx=frame_idx,
                session_id=session_id,
            )
            img_path = result[0] if result else None
            if not img_path:
                return
            web = _to_web_image_url(img_path)
            item = create_feed_item(type="scene_image", content="", image_url=web)

            # Close the vision→story loop: analyze what was ACTUALLY rendered so
            # the next turn's dispatch + choices are grounded on the real frame
            # (not just the intended text). Runs here (off the turn's critical
            # path) so it never slows the player-facing response.
            vis_desc = vis_spatial = vis_setting = ""
            if VISION_ENABLED:
                try:
                    va = _vision_analyze_all(img_path)
                    vis_desc = va.get("description", "") or ""
                    vis_spatial = va.get("spatial", "") or ""
                    vis_setting = va.get("setting", "") or ""
                    if vis_desc:
                        print(f"[ASYNC VISION] {vis_desc[:80]}...", flush=True)
                except Exception as _ve:
                    log_error(f"[ASYNC VISION] failed: {_ve}")

            with WORLD_STATE_LOCK:
                st = _load_state(session_id)
                st['current_image_url'] = web
                st['current_image_prompt'] = result[1] if len(result) > 1 else ""
                st.setdefault('feed_log', []).append(item)
                _save_state(st, session_id)
                state = st
                # Write the absolute image path back into the latest history
                # entry so the NEXT turn's img2img can use it for continuity, and
                # fold in the vision analysis so the next dispatch re-grounds on
                # the rendered frame.
                hist = _load_history(session_id)
                if hist:
                    hist[-1]["image"] = img_path
                    hist[-1]["image_url"] = img_path
                    hist[-1]["analysis_image"] = img_path
                    if vis_desc:
                        hist[-1]["vision_analysis"] = vis_desc
                    if vis_spatial:
                        hist[-1]["spatial_compass"] = vis_spatial
                    if vis_setting:
                        hist[-1]["setting_type"] = vis_setting
                    _save_history(hist, session_id)
                    history = hist
            print(f"[ASYNC IMG] scene appended for {session_id}: {web}", flush=True)
        except Exception as e:
            log_error(f"[ASYNC IMG] failed: {e}")

    threading.Thread(target=_worker, daemon=True).start()


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
                state = st
            print(f"[ASYNC EVOLVE] world updated for {session_id}", flush=True)
        except Exception as e:
            log_error(f"[ASYNC EVOLVE] failed: {e}")

    threading.Thread(target=_worker, daemon=True).start()


# Ensure generate_intro_turn_feed_items is defined AFTER _structure_choices_for_feed
def generate_intro_turn_feed_items() -> List[Dict[str, Any]]:
    from choices import generate_choices # Local import
    global state 
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
            prompt_tmpl=choice_tmpl,
            last_dispatch=initial_narrative_content,
            world_prompt=state.get("world_prompt", "System Online."),
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
    state["choices"] = choices_item['choices']

    # The intro scene image renders in the background and streams into the feed,
    # so reset returns immediately instead of blocking on image generation.
    _spawn_scene_image_async(
        caption=initial_narrative_content,
        dispatch=initial_narrative_content,
        choice="Initialize Simulation",
        frame_idx=0,
        world_prompt=state.get("world_prompt", "Initialization sequence."),
        session_id='default',
    )

    return intro_items
    
# --- Internal Reset Logic --- (Moved from api_reset for reusability)
def _perform_game_reset() -> List[Dict[str, Any]]:
    global state, history, _last_image_path, _next_feed_item_id
    logging.info(f"_perform_game_reset: ENTER. Initial global state object id: {id(state)}")
    
    # Reset state variables by loading a fresh copy and then clearing/setting specifics
    current_state_at_reset_start = _load_state() 
    logging.info(f"_perform_game_reset: After _load_state. Loaded state id: {id(current_state_at_reset_start)}. Its feed_log (len {len(current_state_at_reset_start.get('feed_log',[]))}) id: {id(current_state_at_reset_start.get('feed_log')) if current_state_at_reset_start.get('feed_log') is not None else 'None'}")
    
    # Generate random starting time/weather/mood for this session
    starting_time = _generate_random_starting_time()
    
    # Explicitly create a new dictionary for the state to ensure no shared references for critical parts
    state = {
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
    logging.info(f"_perform_game_reset: New state object created. New state id: {id(state)}. Its feed_log (len {len(state['feed_log'])}) id: {id(state['feed_log'])}")

    _last_image_path = None

    # NOTE: Do NOT reset _next_feed_item_id here. Feed item ids must stay
    # monotonically increasing across resets — a connected client tracks the
    # last id it has seen (and dedups by id), so restarting the counter made
    # a fresh intro reuse low ids that the client had already rendered, which
    # got deduped away → "Reset does nothing". Keeping the counter monotonic
    # guarantees the new intro's items are always newer than anything seen.

    history = []
    if history_path.exists():
        try:
            history_path.write_text("[]", encoding='utf-8') # Clear history file
            logging.info("_perform_game_reset: history.json cleared.")
        except Exception as e_hist_clear:
            logging.error(f"_perform_game_reset: Error clearing history.json: {e_hist_clear}")
    else:
        logging.info("_perform_game_reset: history.json does not exist, no need to clear.")

    initial_items = generate_intro_turn_feed_items() # This should use the global `state` implicitly
    logging.info(f"_perform_game_reset: initial_items from generate_intro_turn_feed_items (IDs): {[item['id'] for item in initial_items if item]}")
    
    state['feed_log'].extend(initial_items) # Add to the new state's new feed_log
    logging.info(f"_perform_game_reset: state['feed_log'] before _save_state (IDs): {[item['id'] for item in state['feed_log'] if item]}")
    
    _save_state(state) # Save the completely new state
    logging.info(f"_perform_game_reset: Game reset complete. {len(initial_items)} initial items generated and saved.")
    return initial_items

def api_reset():
    global state # Ensure we're interacting with the global state
    logging.info(f"api_reset: POST request received. Current state ID before reset: {id(state)}")
    try:
        initial_items = _perform_game_reset()
        logging.info(f"api_reset: _perform_game_reset completed. Current state ID after reset: {id(state)}. Feed log length: {len(state.get('feed_log', []))}")
        if not initial_items:
            logging.warning("api_reset: _perform_game_reset returned no items, but this might be okay if feed_log is now populated by it.")
            # Fallback to checking the state's feed_log if initial_items is empty from return
            initial_items = state.get('feed_log', [])

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
        # Return an error item and an empty list if state itself is problematic.
        error_feed_item = create_feed_item(type="error_event", content=f"Failed to reset game: {str(e)}")
        # Attempt to log this error to the feed_log if state is available
        try:
            state.setdefault('feed_log', []).append(error_feed_item)
            _save_state(state)
        except Exception as e_log:
            log_error(f"Could not save error item to feed_log during api_reset error handling: {e_log}")
        return jsonify([error_feed_item]), 500

def api_feed():
    global state
    since_id_str = request.args.get('since_id')
    items_to_return = []
    if state.get('feed_log'):
        with WORLD_STATE_LOCK: # Ensure thread-safe access to state['feed_log']
            feed_log = state.get('feed_log', [])
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

        if DEBUG_MODE: print(f"[DEBUG] api_choose received choice: '{player_choice_text}', context_id: {context_item_id}. Current state ID: {id(state)}", flush=True)

        # 1. Immediately create and log the Player Action
        player_action_item = create_feed_item(
            type="player_action", 
            content=f"{player_choice_text}", # Display the choice text directly
            metadata={"raw_choice": player_choice_text, "context_id": context_item_id}
        )
        with WORLD_STATE_LOCK:
            state.setdefault('feed_log', []).append(player_action_item)
            state['last_choice'] = player_choice_text
            _save_state(state)        
        if DEBUG_MODE: print(f"[DEBUG] api_choose - Player action item ID {player_action_item['id']} logged. Starting background thread for _process_turn_background.", flush=True)

        # 2. Start background processing for the rest of the turn
        # Pass the ID of the player_action_item so the background thread can link its logs if needed.
        
        temp_signal_file = ROOT / f"thread_signal_{player_action_item['id']}.tmp" # Use the correct ID here
        
        try:
            thread = threading.Thread(target=_process_turn_background, args=(player_choice_text, player_action_item['id'], str(temp_signal_file)))
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
                state.setdefault('feed_log', []).append(error_item)
                _save_state(state)
        except Exception as e_log:
            log_error(f"Could not save error item to feed_log during api_choose error handling: {e_log}")
        return jsonify([error_item]), 500


def api_regenerate_choices():
    global state
    logging.info("api_regenerate_choices: POST request received.")
    try:
        with WORLD_STATE_LOCK:
            current_feed_log = list(state.get('feed_log', [])) # Operate on a copy for reading context
            state_snapshot_for_context = state.copy() # For world prompt, etc.

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
            prompt_tmpl=choice_tmpl,
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
            state.setdefault('feed_log', []).append(new_choice_prompt_item)
            state['choices'] = new_choice_prompt_item['choices'] # Update current choices in state
            _save_state(state)
        
        logging.info(f"api_regenerate_choices: Regenerated choices. New prompt ID: {new_choice_prompt_item['id']}")
        return jsonify([new_choice_prompt_item])

    except Exception as e:
        log_error(f"Error in api_regenerate_choices: {e}")
        logging.exception("Exception in api_regenerate_choices:")
        error_item = create_feed_item(type="error_event", content=f"Failed to regenerate choices: {str(e)}")
        try:
            with WORLD_STATE_LOCK:
                state.setdefault('feed_log', []).append(error_item)
                _save_state(state)
        except Exception as e_log:
            log_error(f"Could not save error item to feed_log during api_regenerate_choices error handling: {e_log}")
        return jsonify([error_item]), 500


# ───────── COMBINED dispatch generator (saves 1 API call) ─────────────────────
def _generate_combined_dispatches(choice: str, state: dict, prev_state: dict = None, prev_vision: str = "", current_image: str = None, fate: str = "NORMAL") -> tuple[str, str, bool]:
    """
    Generate BOTH narrative dispatch AND vision dispatch in ONE API call.
    Now supports multimodal input - can see the current frame!
    
    Args:
        fate: Luck modifier - "LUCKY", "NORMAL", or "UNLUCKY"
    
    Returns: (dispatch, vision_dispatch, player_alive)
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
        chaos_val = int(state.get('chaos_level', 0) or 0)
        recent_events = state.get('recent_events', []) or []
        if isinstance(recent_events, list) and recent_events:
            recent_str = ' | '.join(str(e) for e in recent_events[-5:])
        else:
            recent_str = 'none yet (this is an early turn)'

        grounding_block = (
            f"\n\nDISCOVERED ENTITIES (these are the only things on the board — "
            f"any LETHAL threat must come from here or the current scene): "
            f"{seen_str or 'none yet'}\n"
            f"INJURY STATE (persistent wounds — reference at least once if non-empty): "
            f"{injury_str}\n"
            f"RECENT EVENTS (most recent last — your dispatch MUST stay consistent "
            f"with this history; do not contradict or forget it): {recent_str}\n"
            f"STORY PHASE: {phase_str}  |  ESCALATION (chaos level, higher = the "
            f"world is more hostile and the tension should be tighter): {chaos_val}\n"
        )

        # Use JUST the dispatch_sys instructions (which has JSON format)
        json_prompt = (
            f"{dispatch_sys}\n\n"
            f"{free_will_header}"
            f"PLAYER CHOICE: '{choice}'\n"
            f"WORLD CONTEXT: {world_prompt}\n"
            f"{grounding_block}"
            f"{fate_modifier}"
            f"{spatial_context}"
            f"{prev_context}\n\n"
            "Generate the consequence in valid JSON format."
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
            tokens=450,
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
        
        try:
            import json as json_lib
            data = json_lib.loads(result)
            dispatch = data.get("dispatch", "")
            visual_scene = data.get("visual_scene", "").strip()
            player_alive = data.get("player_alive", True)
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
        
        return dispatch, vision_dispatch, player_alive
        
    except Exception as e:
        try:
            print(f"[COMBINED DISPATCH ERROR] {e}")
        except UnicodeEncodeError:
            print(f"[COMBINED DISPATCH ERROR] (error contains special characters)")
        import traceback
        traceback.print_exc()
        # Fallback to safe defaults
        return "You make a tense move in the chaos.", "The desert stretches ahead.", True

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

# ───────── game loop ──────────────────────────────────────────────────────────
def advance_turn_image_fast(choice: str, fate: str = "NORMAL", is_timeout_penalty: bool = False, session_id: str = 'default', skip_image: bool = False, skip_evolve: bool = False) -> dict:
    """
    PHASE 1 (FAST): Generate dispatch and image, return immediately.

    skip_image=True   -> don't block on the scene image (feed streams it async).
    skip_evolve=True  -> run the world-evolution rewrite in the background
                         (it only affects the next turn), so the turn's
                         narrative + choices return fast.
    
    Args:
        session_id: Session ID for state management
    Returns image ASAP so bot can display it while choices are generating.
    
    Args:
        choice: Player's chosen action
        fate: Luck modifier - "LUCKY", "NORMAL", or "UNLUCKY"
        is_timeout_penalty: If True, maintains EXACT camera position (no movement/teleportation)
    """
    global state, history
    
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

        # Advance the story phase from the (now-bumped) chaos level BEFORE the
        # dispatch is generated, so the consequence prompt's grounding block and
        # time-of-day progression see the current escalation tier.
        if not is_timeout_penalty:
            new_phase = _escalate_phase(state)
            if new_phase != prev_state.get("current_phase", "normal"):
                print(f"[PHASE] escalated {prev_state.get('current_phase','normal')} -> {new_phase} (chaos={state.get('chaos_level')})")
            _save_state(state, session_id)

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
        if is_timeout_penalty:
            dispatch = choice  # The penalty text IS the consequence
            vision_dispatch = choice
            player_alive = True  # timeout penalties never kill directly; the next turn's consequence LLM judges lethality
            print(f"[TIMEOUT PENALTY] Using penalty text as dispatch: {dispatch[:100]}")
        else:
            # Generate dispatch using FULL StoryGen version (with fate modifier)
            dispatch, vision_dispatch, player_alive = _generate_combined_dispatches(choice, state, prev_state, prev_vision, prev_image, fate)
        
        # SIMPLE DEATH SYSTEM: Just trust the LLM
        state['player_state']['alive'] = player_alive
        
        if not player_alive:
            print(f"[DEATH] Player killed by: {dispatch[:100]}...")
        
        # Save state immediately after death detection
        _save_state(state, session_id)
        print(f"[STATE] Saved - alive={player_alive}, health={state['player_state'].get('health', 100)}")
        
        if not dispatch or dispatch.strip().lower() in {"none", "", "[", "[]"}:
            dispatch = "You make a tense move in the chaos."
        # Never surface an LLM/tooling error sentinel to the player. Timeout
        # penalties intentionally reuse the choice text as the dispatch, so
        # skip them here.
        degraded = False
        if not is_timeout_penalty and _is_failure_dispatch(dispatch):
            print(f"[DISPATCH] Failure sentinel detected ('{dispatch[:40]}...') — substituting diegetic line")
            dispatch = _diegetic_dispatch(choice)
            vision_dispatch = dispatch
            degraded = True
        if not vision_dispatch or vision_dispatch.strip().lower() in {"none", "", "[", "[]"} or _is_failure_dispatch(vision_dispatch):
            vision_dispatch = dispatch

        # Persist any wound described this turn so future dispatches/choices can
        # reference it (the grounding block + choice prompt already read
        # state['injuries'] — it was just never populated).
        if not degraded:
            _apply_injuries(state, dispatch, is_timeout_penalty)
            _save_state(state, session_id)

        # Evolve world state. Feed it the REAL action + narrative outcome (not
        # just a thin state-diff), so the living world_prompt actually reflects
        # what the player just experienced instead of drifting.
        state_diff = summarize_world_state_diff(prev_state, state)
        consequence_summary = (
            f"PLAYER ACTION: {choice}\n"
            f"WHAT HAPPENED: {dispatch}\n"
            f"STATE CHANGE: {state_diff}"
        )
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
                session_id=session_id  # Session-specific image directory
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
        
        return {
            "dispatch": dispatch,
            "vision_dispatch": vision_dispatch,
            "consequence_image": consequence_img_url,
            "consequence_image_prompt": consequence_img_prompt,
            "consequence_video": consequence_video_url,  # Video path for HD mode playback
            "hard_transition": hard_transition,  # Track location changes for reference buffer
            "degraded": degraded,  # True when dispatch was an error masked as diegetic text (QA signal)
            "frame_idx": frame_idx,  # for async image generation on the feed path
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

def advance_turn_choices_deferred(consequence_img_url: str, dispatch: str, vision_dispatch: str, choice: str, consequence_img_prompt: str = "", hard_transition: bool = False, session_id: str = 'default') -> dict:
    """
    PHASE 2 (DEFERRED): Generate choices after image is displayed.
    
    Args:
        session_id: Session ID for state management
    """
    try:
        return _advance_turn_choices_deferred_impl(consequence_img_url, dispatch, vision_dispatch, choice, consequence_img_prompt, hard_transition, session_id)
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


def _advance_turn_choices_deferred_impl(consequence_img_url: str, dispatch: str, vision_dispatch: str, choice: str, consequence_img_prompt: str = "", hard_transition: bool = False, session_id: str = 'default') -> dict:
    """Internal implementation of Phase 2 choice generation."""
    global state, history
    from choices import generate_choices
    
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

    # Generate situation summary with BOTH narrative and visual context
    situation_summary = _generate_situation_report(
        current_image=analysis_img_url, 
        current_dispatch=dispatch,
        vision_analysis=vision_analysis_text
    )
    
    next_choices = generate_choices(
        client, choice_tmpl,
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
    # up to bot.py's Phase 2 guard and produce "Generating choices failed"
    # filler. We catch it here and let the bot's intro fallback handle the
    # empty list with scene-aware choices.
    try:
        options = generate_choices(
            client, choice_tmpl,
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
            client, choice_tmpl,
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
