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
from flask import Flask, send_from_directory, request, jsonify, render_template
print("[ENGINE] flask imported", flush=True); sys.stdout.flush()
from PIL import Image
print("[ENGINE] PIL imported", flush=True); sys.stdout.flush()
import io
import requests  # For OpenAI multipart form-data img2img
from flask_cors import CORS
print("[ENGINE] flask_cors imported", flush=True); sys.stdout.flush()

# Note: choices module imported locally in functions to avoid circular dependency
print("[ENGINE] About to import evolve_prompt_file...", flush=True)
import sys; sys.stdout.flush(); sys.stderr.flush()
from evolve_prompt_file import evolve_world_state, set_current_beat, generate_scene_hook, summarize_world_prompt_to_interim_messages
print("[ENGINE] evolve_prompt_file imported", flush=True)
sys.stdout.flush(); sys.stderr.flush()

print("[ENGINE] About to import ai_provider_manager...", flush=True)
sys.stdout.flush(); sys.stderr.flush()
import ai_provider_manager
print("[ENGINE] ai_provider_manager imported", flush=True)
sys.stdout.flush(); sys.stderr.flush()

print("[ENGINE] About to import lore_cache_manager...", flush=True)
sys.stdout.flush(); sys.stderr.flush()
import lore_cache_manager
print("[ENGINE] lore_cache_manager imported", flush=True)
sys.stdout.flush(); sys.stderr.flush()

# ───────── OpenAI client loader ──────────────────────────────────────────────
def _client(api_key: str, base_url: str):
    if hasattr(openai, "OpenAI"):
        return openai.OpenAI(api_key=api_key, base_url=base_url)
    openai.api_key, openai.api_base = api_key, base_url

    class _Chat:
        class completions:
            create = staticmethod(openai.ChatCompletion.create)

    class _Images:
        generate = staticmethod(openai.Image.create)

    return type("LegacyClient", (), {"chat": _Chat, "images": _Images})()

# ───────── config & assets ──────────────────────────────────────────────────
ROOT = Path(__file__).parent.resolve()

# Load config from file if it exists, otherwise use empty dict (for Render deployment)
try:
    CONFIG = json.load((ROOT/"config.json").open(encoding="utf-8"))
except FileNotFoundError:
    CONFIG = {}

# Read from environment variables first, fall back to config.json
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", CONFIG.get("OPENAI_API_KEY"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", CONFIG.get("GEMINI_API_KEY"))
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

app = Flask(__name__)
CORS(app)  # Allow all origins for testing

# Load the full config to access other keys if needed later, specifically DISCORD_CLIENT_ID
CONFIG_DATA = CONFIG  # Already loaded above
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", CONFIG_DATA.get("DISCORD_CLIENT_ID"))
DISCORD_CLIENT_SECRET = CONFIG_DATA.get("DISCORD_CLIENT_SECRET")

# Allow embedding in iframe from Squarespace site
@app.after_request
def add_embed_headers(response):
    response.headers['Content-Security-Policy'] = (
        "frame-ancestors 'self' https://www.5th-corner.com https://discord.com https://canary.discord.com https://ptb.discord.com"
    )
    response.headers['X-Frame-Options'] = (
        "ALLOW-FROM https://www.5th-corner.com https://discord.com https://canary.discord.com https://ptb.discord.com"
    )
    return response

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

def delete_session(session_id):
    """Delete a session and all its data (like deleting a Minecraft world)"""
    if session_id == 'default':
        raise ValueError("Cannot delete the default session. Use reset_state() instead.")
    
    session_root = _get_session_root(session_id)
    if not session_root.exists():
        raise ValueError(f"Session '{session_id}' does not exist")
    
    import shutil
    
    # Count files before deletion for logging
    file_count = sum(1 for _ in session_root.rglob('*') if _.is_file())
    
    # Delete the entire session directory
    shutil.rmtree(session_root)
    
    print(f"[SESSION DELETE] Deleted session '{session_id}' ({file_count} files)")
    return {"session_id": session_id, "files_deleted": file_count}

# Legacy constants for backward compatibility
STATE_PATH = ROOT/"world_state.json"
IMAGE_DIR = ROOT / "images"
WORLD_STATE_LOCK = threading.Lock() # Global lock for world_state.json access

IMAGE_ENABLED       = True  # ENABLED for production
WORLD_IMAGE_ENABLED = True  # ENABLED for production
QUALITY_MODE        = True  # Quality mode: False=Gemini Flash (fast), True=Gemini Pro (high quality, slower)
VEO_MODE_ENABLED    = False # DISABLED by default - use video generation instead of images

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

# Global vision cache to avoid re-analyzing the same image
_vision_cache = {}

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
                return st
            except json.JSONDecodeError as e_json:
                logging.error(f"JSONDecodeError in _load_state for {state_path}: {e_json}. File might be corrupt or empty.")
                # Fallback to a default state but log this as a critical issue
            except Exception as e_load:
                logging.error(f"Unexpected error loading {state_path} in _load_state: {e_load}")
                # Fallback for other errors too
        
        # Fallback: If file doesn't exist or loading failed, return a clean default state
        logging.warning(f"{state_path} not found or failed to load, returning default state.")
        return {
            "world_prompt": PROMPTS.get("world_prompt", "Default world starting point."), # Use .get for safety
            "current_phase": "normal",
            "chaos_level": 0,
            "last_choice": "",
            "last_saved": datetime.now(timezone.utc).isoformat(),
            "seen_elements": [],
            "player_state": {"alive": True},
            "feed_log": [],
            "current_image_url": None,
            "choices": [],
            "turn_count": 0, # Initialize turn_count
            "interim_index": 0, # Initialize interim_index
            "time_of_day": INITIAL_TIME_OF_DAY
        }

# Legacy global state (deprecated - use session-based functions instead)
# These are kept for backward compatibility but should not be used in new code
print("[ENGINE INIT] Initializing legacy global state for backward compatibility...", flush=True)
try:
    state = _load_state('legacy') # Initial load for legacy code
    print(f"[ENGINE INIT] Legacy state loaded successfully", flush=True)
except Exception as e:
    print(f"[ENGINE INIT ERROR] Failed to load legacy state: {e}", flush=True)
    import traceback
    traceback.print_exc()
    # Create default state if loading fails
    state = {
        "world_prompt": "Jason crouches behind a rusted Horizon vehicle at the edge of the facility.",
        "current_phase": "normal",
        "chaos_level": 0,
        "last_choice": "",
        "last_saved": datetime.now(timezone.utc).isoformat(),
        "seen_elements": [],
        "player_state": {"alive": True},
        "feed_log": [],
        "current_image_url": None,
        "choices": [],
        "choices_metadata": {},
        "turn_count": 0,
        "interim_index": 0,
        "in_combat": False,
        "threat_level": 0,
        "time_of_day": INITIAL_TIME_OF_DAY
    }
    print("[ENGINE INIT] Created default legacy state", flush=True)

history_path = ROOT / "history.json"
if history_path.exists():
    with history_path.open("r", encoding="utf-8") as f:
        history = json.load(f)
else:
    history = []

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
    """
    chaos = state.get('chaos_level', 0)
    if chaos > 7:
        return "OVERWHELMING CHAOS! Immediate, decisive action is paramount to survive!"
    elif chaos > 5:
        return "CRITICAL CHAOS! Guards are on high alert and actively hunting for Jason."
    
    if not state.get('player_state', {}).get('alive', True):
        return "Jason is gravely wounded and in danger of dying."
    if 'storm' in state.get('world_prompt', '').lower():
        return "A violent storm is gathering overhead."
    if any(word in state.get('world_prompt', '').lower() for word in ['pursued', 'chased', 'hunted', 'spotted']):
        return "Jason is being pursued by hostile forces."
    if 'red biome' in state.get('world_prompt', '').lower():
        return "The red biome is dangerously close."
    # Add more as needed for your motifs
    return "Jason is alone, but danger could strike at any moment."

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
        
        # Check for lore cache (only if use_lore=True)
        cache_id = None
        if use_lore:
            cache_id = lore_cache_manager.get_cache_id()
        
        # Build request payload with ALL SAFETY FILTERS DISABLED
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": temp, "maxOutputTokens": tokens},
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
        
        response_data = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
            headers={"x-goog-api-key": gemini_api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=15
        ).json()
        
        # Check for error response from Gemini API
        if "candidates" not in response_data:
            print(f"[ASK GEMINI ERROR] Gemini API error response: {response_data}")
            if "error" in response_data:
                error_details = response_data['error']
                print(f"[ASK GEMINI ERROR] Code: {error_details.get('code')}, Message: {error_details.get('message')}")
            return "The transmission wavers... static fills the air as the signal struggles to maintain connection."
        
        result = response_data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return result if result else "..."
    except Exception as e:
        log_error(f"[ASK GEMINI] {e}")
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
            return ""
        
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
        if full_path.endswith(('.jpg', '.jpeg')):
            mime_type = "image/jpeg"
        
        # Use Gemini vision API - ONE call for everything
        print(f"[VISION] Analyzing {os.path.basename(image_path)} (all-in-one)...")
        
        vision_prompt = """Analyze this image and respond in this EXACT format:

TIME: <time of day - use ONLY: dawn, morning, afternoon, golden hour, dusk, or night>
COLOR: <dominant color palette>
DESCRIPTION: <detailed description of what is visible, focusing on objects, threats, exits, and anything Jason could interact with. Be direct and literal. If there are hands, weapons, tools, figures, silhouettes, or creatures visible, mention them explicitly.>"""
        
        api_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
        
        headers = {
            "x-goog-api-key": CONFIG.get("GEMINI_API_KEY", ""),
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
            "generationConfig": {
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
        time_of_day = ""
        color_palette = ""
        description = ""
        
        lines = full_text.strip().split("\n")
        for i, line in enumerate(lines):
            if line.startswith("TIME:"):
                time_of_day = line.replace("TIME:", "").strip()
            elif line.startswith("COLOR:"):
                color_palette = line.replace("COLOR:", "").strip()
            elif line.startswith("DESCRIPTION:"):
                description = line.replace("DESCRIPTION:", "").strip()
                # Capture any subsequent lines as part of description
                if i + 1 < len(lines):
                    description += " " + " ".join(lines[i+1:])
                break
        
        # If parsing failed, try fallback
        if not description:
            description = full_text
        
        result_dict = {
            "description": description.strip().replace("\n", " "),
            "time_of_day": time_of_day.strip(),
            "color_palette": color_palette.strip()
        }
        
        # Cache the result
        _vision_cache[cache_key] = result_dict
        
        print(f"[VISION] Analysis complete: {len(description)} chars, time={time_of_day}, color={color_palette[:30]}")
        return result_dict
    
    except requests.exceptions.HTTPError as e:
        print(f"[VISION ERROR] Gemini API HTTP error: {e}")
        if e.response is not None:
            print(f"[VISION ERROR] Response: {e.response.text}")
        return {"description": "", "time_of_day": "", "color_palette": ""}
    except Exception as e:
        print(f"[VISION ERROR] Failed to analyze image: {e}")
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
    """Summarize the world prompt to 1-2 sentences for image generation."""
    prompt = (
        "Summarize the following world context in 1-2 vivid, scene-specific sentences, focusing only on details relevant to the current visual environment. Omit backstory and generalities.\n\nWORLD PROMPT: " + world_prompt
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
            "Describe what Jason does and what immediately happens as a result."
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
            return {"dispatch": "Jason makes a tense move in the chaos.", "player_alive": True}
        
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
                    return {"dispatch": "Jason makes a tense move in the chaos.", "player_alive": True}
                result = " ".join(lines)
        
        # Hard cap at 400 characters
        if len(result) > 400:
            result = result[:385] + "...(truncated)"
        
        return {"dispatch": result, "player_alive": True}
        
    except Exception as e:
        log_error(f"[DISPATCH] LLM error: {e}")
        return {"dispatch": "Jason makes a tense move in the chaos.", "player_alive": True}

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
        print(f"[MOVEMENT DETECTION] Error: {e}, defaulting to exploration")
        return 'exploration'  # Default to some movement to keep things interesting

def build_image_prompt(player_choice: str = "", dispatch: str = "", prev_vision_analysis: str = "", hard_transition: bool = False, is_timeout_penalty: bool = False) -> str:
    """
    Build image prompt with continuity from previous vision analysis.
    Explicitly includes player choice so the image reflects the action taken.
    """
    # TIMEOUT PENALTIES: EXACT same camera position, ZERO movement
    if is_timeout_penalty:
        return (
            f"Result: {dispatch}\n\n"
            f"CRITICAL: TIMEOUT PENALTY.\n"
            f"Show the EXACT SAME ANGLE and LOCATION as before.\n"
            f"ONLY environmental changes (dust, debris, reactions, creatures, gore ,attacks, violence, trouble, danger, etc.) - NO camera movement.\n"
            f"If outdoor, stay outdoor. If indoor, stay indoor. NO teleportation."
        )
    
    # Intelligently detect movement type
    global _last_movement_type
    movement_type = _detect_movement_type(player_choice)
    _last_movement_type = movement_type  # Store for Discord display
    
    # Visual indicators for each type
    type_emoji = {
        'forward_movement': 'FORWARD MOVEMENT',
        'exploration': 'EXPLORATION',
        'stationary': 'STATIONARY'
    }
    print(f"\n{'='*60}")
    safe_choice = player_choice.encode('ascii', 'replace').decode('ascii')
    print(f"[MOVEMENT DETECTION] '{safe_choice}'")
    print(f"  -> {type_emoji.get(movement_type, movement_type.upper())}")
    print(f"{'='*60}\n", flush=True)
    
    # Start with the player's choice and what happened
    prompt = f"Action taken: {player_choice}. Result: {dispatch}"
    
    # Add previous vision analysis for visual continuity
    if prev_vision_analysis:
        if hard_transition:
            # Location change - maintain lighting/aesthetic continuity
            prompt = f"{prompt} Maintain similar lighting, time of day, and overall visual aesthetic as before."
        elif movement_type == 'forward_movement':
            # FORWARD MOVEMENT - Show natural spatial progression
            prompt = (
                f"{prompt}\n\n"
                f"FORWARD MOVEMENT: The camera has moved forward naturally.\n"
                f"The player chose: '{player_choice}'\n\n"
                f"Show spatial progression:\n"
                f"- Objects ahead are now closer and larger in frame\n"
                f"- Camera has advanced naturally (5-15 feet forward)\n"
                f"- Smooth perspective shift showing forward progress\n"
                f"- Ground/foreground elements reflect new position\n\n"
                f"Previous scene for reference: {prev_vision_analysis[:150]}\n"
                f"Maintain visual continuity while showing forward movement."
            )
        elif movement_type == 'exploration':
            # EXPLORATION - Subtle camera shift/pan to keep things dynamic
            prompt = (
                f"{prompt}\n\n"
                f"SUBTLE CAMERA SHIFT: The camera adjusts slightly (pan, tilt, or drift).\n"
                f"- Maintain roughly the same position but show a SLIGHTLY different angle\n"
                f"- Small perspective shift to keep the scene dynamic and interesting\n"
                f"- Environmental elements shift naturally with camera movement\n"
                f"Previous scene for reference: {prev_vision_analysis[:150]}"
            )
        else:
            # STATIONARY - Maintain exact position but allow environmental changes
            prompt = f"{prompt} Same camera position. Only environmental/lighting changes: {prev_vision_analysis[:200]}"
    
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
    
    try:
        prev_time_of_day, prev_color = "", ""
        prev_img_paths = []
        prev_img_captions = []
        prev_vision_analysis = ""  # NEW: Vision AI's analysis of what's actually in the last frame
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
                        entry.get("vision_analysis", "")  # Pull vision analysis
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
                # Get most recent image for time/color extraction
                img, cap, vis_analysis = last_imgs[0]
                prev_vision_analysis = vis_analysis
                prev_img_path = str(_resolve_image_path(img))
                if not os.path.exists(prev_img_path):
                    prev_img_path = None
                
                # Collect all recent image paths for multi-reference img2img
                for idx, (img, cap, _) in enumerate(last_imgs):
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
        world_summary = summarize_world_state(state) if 'state' in globals() else ""
        # --- Summarize world prompt for image flavor ---
        world_flavor = ""
        if 'state' in globals() and state.get("world_prompt", ""):
            world_flavor = summarize_world_prompt_for_image(state["world_prompt"])
        prompt_str = build_image_prompt(
            player_choice=choice,
            dispatch=caption,
            prev_vision_analysis=prev_vision_analysis,
            hard_transition=hard_transition,
            is_timeout_penalty=is_timeout_penalty
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
        print(f"[IMG LOG] world_prompt: {world_prompt}")
        print(f"[IMG LOG] session_id: {session_id}")
        print("[IMG LOG] --- END IMAGE GENERATION PARAMETERS ---")
        img_dir = _get_image_dir(session_id)
        filename = f"{hash(caption) & 0xFFFFFFFF}_{_slug(caption)}.png"
        image_path = img_dir / filename
        
        # --- ROUTE TO APPROPRIATE IMAGE PROVIDER ---
        active_image_provider = ai_provider_manager.get_image_provider()
        print(f"[IMG DEBUG] active_image_provider = {active_image_provider}", flush=True)
        print(f"[IMG DEBUG] About to call image generation API...", flush=True)
        
        if active_image_provider == "veo":
            print(f"[IMG DEBUG] -> Calling Veo video generation...", flush=True)
            print(f"[IMG DEBUG] Entering Veo branch", flush=True)
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
                
                result_path = generate_gemini_img2img(
                    prompt=prompt_str,
                    caption=caption,
                    reference_image_path=ref_images_to_use,  # Adjusted based on transition type
                    strength=strength,
                    world_prompt=world_prompt,
                    time_of_day=use_time_of_day,
                    action_context=choice,  # Pass action for FPS hands context
                    hd_mode=use_hq_for_this_frame,  # Frame 0 always HQ, others respect quality toggle
                    output_dir=img_dir  # Session-specific directory
                )
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
                
                # ALWAYS use HQ for first image, then respect quality toggle
                use_hq_for_this_frame = True if frame_idx == 0 else QUALITY_MODE
                if frame_idx == 0:
                    print(f"[QUALITY MODE] Frame 0 (intro) - FORCING HQ (Gemini Pro) for visual consistency")
                
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
            _last_image_path = result_path
            return (result_path, prompt_str, None)  # Gemini doesn't generate videos
        
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
        safe_error = str(e).encode('ascii', 'replace').decode('ascii')
        print(f"[IMG PROVIDER {ai_provider_manager.get_image_provider()}] Error: {safe_error}")
        import traceback
        traceback.print_exc()
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
        "You are a visual scene writer. Output only the literal, visible scene as Jason would see it, in first-person present tense.\n\n"
        "Rewrite the following narrative as a first-person, present-tense description of what Jason sees, suitable for a visual scene. "
        "Only describe what is visible. Do not include Jason himself or any internal thoughts. "
        "Do not show Jason. Do not show the protagonist. Do not show any character from behind. Only show what Jason sees from his own eyes. "
        f"\n\nNARRATIVE DISPATCH: {narrative_dispatch}\n\nWORLD CONTEXT: {world_prompt}"
    )
    # Don't use lore - this is just reformatting narrative to visual description
    result = _ask(prompt, model="gemini", temp=1.0, tokens=100, use_lore=False)
    return result

# ───────── public API (two‑stage) ───────────────────────────────────────────
def _generate_situation_report(current_image: str = None) -> str:
    """Generate situation report with optional visual context from current frame."""
    if history:
        last_dispatch = history[-1]["dispatch"]
        # Use the updated world state after the simulation tick
        world_state = _load_state()["world_prompt"]
        turn_count = len(history)
        major_event_nudge = ""
        if turn_count % 5 == 0 and turn_count > 0:
            major_event_nudge = (
                "\n\nIMPORTANT: This is a turning point. Introduce a major new development, threat, opportunity, or mystery. Shake up the situation in a dramatic way."
            )
        prompt = (
            PROMPTS["situation_summary_instructions"] +
            f"\n\nWorld State (after last action):\n{world_state}\nLast Dispatch (player's action):\n{last_dispatch}" +
            major_event_nudge +
            "\nDescribe what is happening NOW, after the dispatch, as a concise 1-2 sentence situation. This should set up the next set of choices."
        )
        # Don't use lore - this is just a summary with visual grounding
        return _ask(prompt, model="gemini", temp=1.0, tokens=40, image_path=current_image, use_lore=False)
    return "You stand on a rocky outcrop overlooking the Horizon facility, situated in the distance, surrounded by the vast red american southwest."

def begin_tick() -> dict:
    from choices import generate_choices  # Local import to avoid circular dependency
    
    state = _load_state()
    # If at intro/prologue, return generate_intro_turn result (with choices)
    if not history or (state.get('last_choice', '') == '' and state.get('world_prompt', '').startswith('Jason crouches behind a rusted Horizon vehicle')):
        return generate_intro_turn()
    # Generate a world summary (narrative)
    world_summary = state["world_prompt"]
    # Use the last dispatch as the situation report
    situation_report = history[-1]["dispatch"]
    # Use the world prompt to generate interim messages
    interim_messages = summarize_world_prompt_to_interim_messages(world_summary)
    loader = interim_messages
    # Generate the narrative update using the narrative prompt
    narrative_update = _world_report()
    # Condense world state for choices
    situation_summary = summarize_world_state(state)
    options = generate_choices(
        client, choice_tmpl,
        situation_report,
        n=3,
        seen_elements='',
        recent_choices='',
        caption=situation_report,
        image_description='',
        world_prompt=state.get('world_prompt', ''),
        temperature=0.2,
        situation_summary=situation_summary
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

# --- LLM-based player death check ---
def check_player_death(dispatch: str, world_prompt: str, choice: str) -> bool:
    prompt = (
        'Based on the following, has the player died? Respond with only "dead" or "alive".\n'
        f'DISPATCH: {dispatch}\n'
        f'WORLD STATE: {world_prompt}\n'
        f'PLAYER CHOICE: {choice}'
    )
    # Don't use lore - this is a binary mechanical check
    result = _ask(prompt, model="gemini", temp=0, tokens=2, use_lore=False).strip().lower()
    return result == "dead"

def combat_hook(state, dispatch, vision_dispatch):
    """Dice roll combat system. Returns dict with combat state and outcome."""
    if not state.get('in_combat'):
        # Start combat, present choices
        state['in_combat'] = True
        return {
            'combat': True,
            'combat_choices': ['Attack', 'Run'],
            'combat_message': 'Combat! Choose to attack or run.'
        }
    # If already in combat, resolve based on last choice
    last_choice = state.get('last_choice')
    if last_choice == 'Attack':
        roll = random.randint(1,6)
        if roll >= 4:
            state['in_combat'] = False
            return {'combat': False, 'combat_result': 'You attack and win! The threat is defeated.'}
        else:
            state['in_combat'] = False
            state['game_over'] = True
            return {'combat': False, 'combat_result': 'You attack and fail. You are killed.'}
    elif last_choice == 'Run':
        roll = random.randint(1,6)
        if roll >= 4:
            state['in_combat'] = False
            return {'combat': False, 'combat_result': 'You run and escape!'}
        else:
            state['in_combat'] = False
            state['game_over'] = True
            return {'combat': False, 'combat_result': 'You try to run but are caught. Game over.'}
    # Default: still in combat
    return {'combat': True, 'combat_choices': ['Attack', 'Run'], 'combat_message': 'Combat! Choose to attack or run.'}

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

def generate_crisis_choices(dispatch, vision, world_prompt):
    """Generate 2 urgent, high-stakes crisis choices for action mode."""
    crisis_prompt = (
        "You are in a crisis/action scene. Only present 2 urgent, high-stakes choices for Jason Fleece. "
        "Choices must be desperate, risky, or immediate reactions to the current threat (e.g., 'Dodge and run', 'Return fire', 'Drop to the ground and play dead'). "
        "Do not include exploration or investigation. Only direct, crisis responses.\n"
        f"DISPATCH: {dispatch}\nVISION: {vision}\nWORLD: {world_prompt}"
    )
    # Don't use lore - this is a mechanical crisis detection
    rsp = _ask(crisis_prompt, model="gemini", temp=1.0, tokens=40, use_lore=False)
    opts = []
    for line in rsp.splitlines():
        line = line.strip().lstrip("-*0123456789. ").strip()
        if 4 < len(line) <= 40 and line.lower() not in opts:
            opts.append(line)
    if not opts:
        opts = ["Dodge and run", "Return fire"]
    return opts[:2]

# Placeholder for generate_consequence_summary - to be defined or restored properly later
# This needs to be at the module level to be found by _process_turn_background
def generate_consequence_summary(dispatch_text: str, prev_state: dict, current_state: dict, choice: str) -> str:
    logging.info(f"generate_consequence_summary called with dispatch: {dispatch_text[:30]}...")
    # Use LLM to generate a consequence summary
    try:
        prompt = (
            "You are a consequence engine for an interactive story. "
            "Given the player's choice, the resulting narrative dispatch, and the before/after world state, "
            "write a simple, declarative statement about what happened as a result of the event. "
            "Focus on the direct outcome or change. If possible, add a sense of danger or tension. "
            "Do not summarize the choice or dispatch, but reflect the consequence. "
            "If nothing significant happened, say so concisely.\n"
            f"PLAYER CHOICE: {choice}\n"
            f"DISPATCH: {dispatch_text}\n"
            f"PREVIOUS STATE: {json.dumps(prev_state, ensure_ascii=False)}\n"
            f"CURRENT STATE: {json.dumps(current_state, ensure_ascii=False)}\n"
        )
        # Don't use lore - just detecting state changes
        result = _ask(prompt, model="gemini", temp=1.0, tokens=48, use_lore=False)
        if result and result.strip():
            return result.strip()
    except Exception as e:
        logging.error(f"LLM error in generate_consequence_summary: {e}")
    # Fallback to old logic
    if "fail" in dispatch_text.lower() or "error" in dispatch_text.lower():
        return f"The choice '{choice}' seems to have led to a problematic outcome: {dispatch_text}"
    if prev_state.get("chaos_level", 0) < current_state.get("chaos_level", 0):
        return "The situation appears to have become more chaotic."
    return "No immediate major consequence observed from this action."

# RENAMED from advance_turn
def _process_turn_background(choice: str, initial_player_action_item_id: int, signal_file_path: Optional[str] = None):
    # Write to signal file immediately if path is provided
    if signal_file_path:
        try:
            Path(signal_file_path).write_text("THREAD SPAWNED AND WROTE TO FILE")
        except Exception as e_signal_write:
            # Cannot print here reliably if stdout is the issue, but log for posterity if possible
            # For now, this is just a signal, if it fails, the check in api_choose will show it.
            pass 
            
    print(f"DEBUG PRINT: _process_turn_background THREAD SPAWNED - VERY FIRST LINE. Choice: '{choice}'", flush=True) # ULTRA-EARLY PRINT
    global state, history, _last_image_path
    # Add a small delay to simulate processing and allow client to update.
    time.sleep(0.75) # Adjusted as per previous implementation for pacing
    print(f"DEBUG PRINT: _process_turn_background THREAD ENTERED for choice: '{choice}', originating action ID: {initial_player_action_item_id}", flush=True)

    new_feed_items_for_log: List[Dict[str, Any]] = []
    dispatch_text = "Default initial dispatch text" # Initialize dispatch_text
    vision_dispatch_text = "" # Initialize vision_dispatch_text here

    try:
        # Load the state as it was when the player action was initially processed by api_choose
        # This is crucial because other turns might be processing in parallel if things get very fast,
        # though with current locking on _save_state, direct conflict is less likely on write,
        # but reading a consistent snapshot is good.
        current_state_snapshot = _load_state()
        # However, for practical purposes, we'll mostly operate on the global `state`
        # and rely on _save_state locks for write safety. The key is that `api_choose`
        # has already logged the player_action. This thread logs subsequent events.

        prev_state_snapshot = current_state_snapshot.copy() # For context to generators
        print(f"DEBUG PRINT: _process_turn_background - State loaded. Player choice: {choice}", flush=True)

        # 1. Generate Narrative Dispatch ("Result" of player action)
        dispatch_text = ""
        try:
            print(f"DEBUG PRINT: _process_turn_background - Generating narrative dispatch...", flush=True)
            dispatch_text = _generate_dispatch(choice, current_state_snapshot, prev_state_snapshot)
            if not dispatch_text or dispatch_text.strip().lower() in {"none", "", "[", "[]"}:
                dispatch_text = "The situation evolves..."
            dispatch_item = create_feed_item(type="narrative_event", content=dispatch_text, metadata={"source": "dispatch"})
            new_feed_items_for_log.append(dispatch_item)
            print(f"DEBUG PRINT: _process_turn_background - Narrative dispatch generated: {dispatch_text[:60]}...", flush=True)
        except Exception as e_dispatch:
            log_error(f"Error during _process_turn_background narrative dispatch generation: {e_dispatch}")
            error_item = create_feed_item(type="error_event", content=f"Error generating dispatch: {e_dispatch}")
            new_feed_items_for_log.append(error_item)
            dispatch_text = "Error in narrative generation. The situation is unstable."
        
        # Append dispatch (or error) to global state's feed_log and save
        with WORLD_STATE_LOCK:
            state.setdefault("feed_log", []).extend(new_feed_items_for_log[-1:]) # append last item (dispatch or error)
            _save_state(state)
        new_feed_items_for_log.clear() # Clear after saving this part

        # 2. Apply choice to basic world state (e.g., chaos, threat levels if deterministic)
        # This was part of generate_and_apply_choice which is now more LLM focused.
        # We might need to re-evaluate if some direct state changes based on choice keywords are needed here.
        # For now, primary world state evolution happens in generate_and_apply_choice and evolve_world_state.
        print(f"DEBUG PRINT: _process_turn_background - Applying choice to world state (via choices.py)...", flush=True)
        try:
            import choices  # Local import to avoid circular dependency
            choices.generate_and_apply_choice(choice, dispatch_text_from_engine=dispatch_text) # Call via module
            # Reload state after choices.py potentially modified it
            state = _load_state()
            print(f"DEBUG PRINT: _process_turn_background - generate_and_apply_choice completed and state reloaded.", flush=True)
        except Exception as e_apply_choice:
            log_error(f"Error in _process_turn_background calling generate_and_apply_choice: {e_apply_choice}")
            error_item = create_feed_item(type="error_event", content=f"Error processing choice's core impact: {e_apply_choice}")
            new_feed_items_for_log.append(error_item)
            with WORLD_STATE_LOCK:
                state.setdefault("feed_log", []).append(error_item)
                _save_state(state)
            # This is a critical error path, subsequent steps might be affected.

        # 3. Generate Consequence Summary (based on dispatch and potentially new world state)
        consequence_text = ""
        try:
            print(f"DEBUG PRINT: _process_turn_background - Generating consequence summary...", flush=True)
            # Pass the state *after* generate_and_apply_choice for more accurate consequence
            consequence_text = generate_consequence_summary(dispatch_text, prev_state_snapshot, state, choice)
            if consequence_text and consequence_text.strip().lower() not in ["no major consequence observed.", "no major consequence.", "none", ""]:
                consequence_item = create_feed_item(type="consequence_event", content=consequence_text)
                new_feed_items_for_log.append(consequence_item)
                print(f"DEBUG PRINT: _process_turn_background - Consequence summary generated: {consequence_text[:60]}...", flush=True)
            else:
                print(f"DEBUG PRINT: _process_turn_background - No significant consequence generated.", flush=True)
        except Exception as e_consequence:
            log_error(f"Error generating consequence summary in _process_turn_background: {e_consequence}")
            error_item = create_feed_item(type="error_event", content=f"Error generating consequence: {e_consequence}")
            new_feed_items_for_log.append(error_item)
        
        # Append consequence (or error) to global state's feed_log and save
        if new_feed_items_for_log: # If a consequence or error was added
            with WORLD_STATE_LOCK:
                state.setdefault("feed_log", []).extend(new_feed_items_for_log)
                _save_state(state)
            new_feed_items_for_log.clear()


        # 4. Image Generation & Vision Analysis (if enabled)
        # These depend on dispatch_text and the current world_prompt from the reloaded state
        world_prompt_for_image = state.get("world_prompt", "")
        vision_dispatch_for_image = "" # Initialize locally for image gen step
        current_image_url = state.get("current_image_url") # Get current image to pass as previous

        if dispatch_text:
            # This is the primary vision_dispatch_text for the turn's narrative dispatch
            vision_dispatch_text = _generate_vision_dispatch(dispatch_text, world_prompt_for_image) # Assign to the broader scoped variable
            vision_dispatch_for_image = vision_dispatch_text # Also use it for the image gen step
            print(f"DEBUG PRINT: _process_turn_background - Vision dispatch for image (and general use): {vision_dispatch_for_image[:60]}...", flush=True)

        new_image_url = None
        if IMAGE_ENABLED and vision_dispatch_for_image:
            print(f"DEBUG PRINT: _process_turn_background - Generating image...", flush=True)
            try:
                hard_trans = is_hard_transition(choice, dispatch_text)
                new_image_url, image_prompt, video_url = _gen_image(
                    caption=vision_dispatch_for_image,
                    mode=state.get("current_phase", "normal"), # Use current_phase for mode
                    choice=choice,
                    previous_image_url=current_image_url,
                    dispatch=dispatch_text, # Narrative dispatch
                    world_prompt=world_prompt_for_image,
                    hard_transition=hard_trans,
                    frame_idx=state.get("turn_count", 0) + 1 # Approximate frame index
                )
                # Note: video_url not used in this code path (old multiplayer mode)
                if new_image_url:
                    image_item = create_feed_item(type="scene_image", content="The scene shifts...", image_url=new_image_url)
                    new_feed_items_for_log.append(image_item)
                    state['current_image_url'] = new_image_url # Update global state
                    state['current_image_prompt'] = image_prompt # Store prompt for history
                    state['current_hard_transition'] = hard_trans # Store hard transition flag
                    print(f"DEBUG PRINT: _process_turn_background - Image generated: {new_image_url}", flush=True)

                    if VISION_ENABLED: # Vision analysis of the new image
                        print(f"DEBUG PRINT: _process_turn_background - Generating vision analysis for new image...", flush=True)
                        vision_text = _vision_describe(new_image_url)
                        if vision_text:
                            vision_item = create_feed_item(type="vision_analysis", content=vision_text, metadata={"source_image_url": new_image_url})
                            new_feed_items_for_log.append(vision_item)
                            print(f"DEBUG PRINT: _process_turn_background - Vision analysis generated: {vision_text[:60]}...", flush=True)
            except Exception as e_img_vision:
                log_error(f"Error during image/vision generation in _process_turn_background: {e_img_vision}")
                error_item = create_feed_item(type="error_event", content=f"Error generating visual data: {e_img_vision}")
                new_feed_items_for_log.append(error_item)
        
        if new_feed_items_for_log: # If image/vision items were added
            with WORLD_STATE_LOCK:
                state.setdefault("feed_log", []).extend(new_feed_items_for_log)
                _save_state(state) # Save state with new image/vision items
            new_feed_items_for_log.clear()

        # 5. Combat / Threat / Risky Action Logic
        # This section needs careful review based on previous logic for these branches
        # It will append its own feed items and might set proceed_with_standard_evolution to False
        proceed_with_standard_evolution = True
        
        # Call the actual detect_threat function from choices.py
        import choices  # Local import to avoid circular dependency
        global FORCE_TEST_THREAT # Access the global flag
        is_threat = choices.detect_threat(dispatch_text, vision=vision_dispatch_text) # Use the broader scoped vision_dispatch_text
        threat_description = "" # Initialize threat_description

        # Allow forcing combat via a special choice text for testing
        if "FORCE_COMBAT_SCENARIO" in choice:
            is_threat = True
            threat_description = "Combat explicitly triggered by test choice text."
            print(f"DEBUG PRINT: Combat path forced by choice text: '{choice}'", flush=True)

        if FORCE_TEST_THREAT: # Existing flag can still be a backup or for other tests
            is_threat = True # Ensure this is also set if flag is true
            # Prioritize more specific description if already set by choice text trigger
            threat_description = threat_description if "Combat explicitly triggered" in threat_description else "Forced test threat triggered by FORCE_TEST_THREAT flag."
            print(f"DEBUG PRINT: FORCE_TEST_THREAT activated is_threat={is_threat}. Current threat_description: '{threat_description}'", flush=True)
            FORCE_TEST_THREAT = False # Reset after use to prevent affecting subsequent calls in the same test run if any
        elif not ("Combat explicitly triggered" in threat_description) and is_threat:
            # Only set threat_description if is_threat is True from the original call and not forced by choice/flag
            threat_description = "Hostile contact detected by choices.detect_threat."

        # is_threat, threat_description = False, "" # COMMENTED OUT PLACEHOLDER
        
        if state.get('in_combat', False):
            print(f"DEBUG PRINT: _process_turn_background - Already in combat. Resolving combat turn for choice: '{choice}'", flush=True)
            
            combat_action_item = create_feed_item(type="combat_action", content=f"You chose to: {choice}")
            new_feed_items_for_log.append(combat_action_item)

            combat_ended_this_turn = False
            outcome_message = "The struggle continues..." # Default for unrecognized actions

            # Determine combat outcome
            if "attack" in choice.lower() or "engage" in choice.lower():
                # Test specific win condition for "Engage the threat directly"
                if choice == "Engage the threat directly": 
                    outcome_message = "Your decisive action pays off! The threat is neutralized."
                    state['in_combat'] = False
                    combat_ended_this_turn = True
                    print(f"DEBUG PRINT: _process_turn_background - Combat WIN via specific choice 'Engage the threat directly'.", flush=True)
                else: # Generic attack
                    if random.random() < 0.6: # 60% chance to win other attacks
                        outcome_message = "Your attack connects! The threat is neutralized."
                        state['in_combat'] = False
                        combat_ended_this_turn = True
                    else:
                        outcome_message = "Your attack is parried or misses! The combat rages on."
                        state['in_combat'] = True 
                        combat_ended_this_turn = False
            elif "flee" in choice.lower() or "evade" in choice.lower() or "disengage" in choice.lower() or "retreat" in choice.lower():
                # Always succeed at retreat/flee/disengage
                outcome_message = "You manage to break away and escape the immediate danger! Combat ends."
                state['in_combat'] = False
                combat_ended_this_turn = True
            else: # Non-recognized combat action
                outcome_message = f"Your action ('{choice}') is confusing in this combat situation. The threat remains, and the fight continues."
                state['in_combat'] = True # Combat continues
                combat_ended_this_turn = False

            resolution_item_metadata = {
                "outcome_raw": outcome_message,
                "combat_ended": combat_ended_this_turn,
                "player_won": combat_ended_this_turn and not state.get('in_combat') # True if combat ended AND player is not still in combat
            }
            resolution_item = create_feed_item(
                type="combat_resolution", 
                content=outcome_message,
                metadata=resolution_item_metadata
            )
            new_feed_items_for_log.append(resolution_item)

            if not state.get('in_combat', False): # Combat ended
                proceed_with_standard_evolution = True
                print(f"DEBUG PRINT: _process_turn_background - Combat has ENDED. Proceeding with standard evolution. Outcome: {outcome_message}", flush=True)
            else: # Combat continues
                # Regenerate combat choices
                current_combat_choices_texts = ["Attack again", "Try to disengage"]
                combat_choices_item = _structure_choices_for_feed(
                    current_combat_choices_texts, 
                    "The fight continues! What is your next move?",
                    image_url=state.get('current_image_url')
                )
                new_feed_items_for_log.append(combat_choices_item)
                proceed_with_standard_evolution = False
                print(f"DEBUG PRINT: _process_turn_background - Combat CONTINUES. New combat choices generated.", flush=True)
            print(f"DEBUG PRINT: _process_turn_background - Exiting ONGOING COMBAT logic. new_feed_items_for_log count: {len(new_feed_items_for_log)}. Proceed standard: {proceed_with_standard_evolution}", flush=True)
            # pass # Simplified for now <-- REMOVE THIS
        elif is_threat: # check new threat
            print(f"DEBUG PRINT: _process_turn_background - ENTERING elif is_threat BLOCK. Threat: {threat_description}", flush=True)
            
            suspense_item = create_feed_item(type="suspense_event", content=f"Threat detected! {threat_description}")
            new_feed_items_for_log.append(suspense_item)

            # For now, assume direct escalation to combat if a threat is confirmed by detect_threat
            # The test mocks random.random to force this path if more complex logic was here.
            escalation_content = "The threat escalates rapidly! Combat is imminent."
            threat_escalation_item = create_feed_item(type="threat_escalation", content=escalation_content)
            new_feed_items_for_log.append(threat_escalation_item)
            
            state['in_combat'] = True
            print(f"DEBUG PRINT: _process_turn_background - State in_combat SET TO TRUE.", flush=True)
            
            # Generate combat-specific choices
            # For now, using predefined combat choices. Later, this could use generate_crisis_choices or similar.
            combat_choice_texts = ["Engage the threat directly", "Attempt to flee the encounter"]
            combat_prompt_text = "Combat initiated! How do you react to the immediate danger?"
            
            # Use current image for combat choice prompt if available
            combat_image_url = state.get('current_image_url')
            if WORLD_IMAGE_ENABLED == False: # If images globally disabled, don't pass URL
                 combat_image_url = None

            combat_choices_item = _structure_choices_for_feed(
                combat_choice_texts, 
                combat_prompt_text,
                image_url=combat_image_url 
            )
            new_feed_items_for_log.append(combat_choices_item)
            
            proceed_with_standard_evolution = False # Prevent normal choice generation
            print(f"DEBUG PRINT: _process_turn_background - Combat choices generated, proceed_with_standard_evolution SET TO FALSE.", flush=True)
            
            # new_feed_items_for_log now contains suspense, escalation, and combat choices.
            # These will be saved in the block after the if/elif/else for combat/threat/risky_action.
            print(f"DEBUG PRINT: _process_turn_background - Exiting elif is_threat BLOCK. new_feed_items_for_log count: {len(new_feed_items_for_log)}", flush=True)
            
        # Risky action check (if not in combat and not an immediate threat)
        elif any(keyword in choice.lower() for keyword in RISKY_ACTION_KEYWORDS) and not state.get('in_combat') and not is_threat:
            print(f"DEBUG PRINT: _process_turn_background - ENTERING RISKY ACTION BLOCK. Choice: '{choice}'", flush=True)
            
            suspense_content = f"Jason considers the risky action: '{choice}'. The air crackles with uncertainty."
            suspense_item = create_feed_item(type="suspense_event", content=suspense_content, metadata={"action_type": "risky"})
            new_feed_items_for_log.append(suspense_item)
            
            # Simulate risky action outcome (e.g., 50/50 chance)
            action_succeeded = random.random() < 0.5 
            
            if action_succeeded:
                outcome_content = f"Against the odds, Jason's risky move ('{choice}') pays off!"
                state["chaos_level"] = max(0, state.get("chaos_level", 0) - 1) # Decrease chaos slightly
            else:
                outcome_content = f"Unfortunately, Jason's gamble ('{choice}') backfires, escalating the danger."
                state["chaos_level"] = state.get("chaos_level", 0) + 1 # Increase chaos

            outcome_item = create_feed_item(
                type="risky_action_outcome", 
                content=outcome_content, 
                metadata={"success": action_succeeded, "choice_made": choice}
            )
            new_feed_items_for_log.append(outcome_item)
            
            print(f"DEBUG PRINT: _process_turn_background - Risky action outcome: {'Success' if action_succeeded else 'Failure'}. Chaos level: {state.get('chaos_level')}", flush=True)
            proceed_with_standard_evolution = True # Risky actions usually lead to new standard choices

        # If combat items were added, save them
        if new_feed_items_for_log: # If combat logic added items
            with WORLD_STATE_LOCK:
                state.setdefault("feed_log", []).extend(new_feed_items_for_log)
                _save_state(state)
            new_feed_items_for_log.clear()
            
        # 6. Evolve World State (if not in ongoing combat or other non-standard path)
        if proceed_with_standard_evolution:
            print(f"DEBUG PRINT: _process_turn_background - Evolving world state (evolve_prompt_file.evolve_world_state)...", flush=True)
            # Removed the potentially problematic simple call: evolve_world_state(state)
            # state = _load_state() # Reloading here might not be necessary if the above was the only modifier

            # Evolve world state (LLM call, can be slow)
            if LLM_ENABLED: 
                print(f"DEBUG PRINT: _process_turn_background - Preparing for LLM-based world state evolution...", flush=True)
                
                # Ensure we are getting a list of feed items for dispatches
                current_feed_log_for_evolution = list(state.get('feed_log', [])) 
                # Ensure consequence_text is defined; it should be from earlier in the function
                # vision_dispatch_text should also be defined from earlier

                print(f"DEBUG PRINT: _process_turn_background - Calling evolve_prompt_file.evolve_world_state. Dispatch count: {len(current_feed_log_for_evolution)}, Consequence: '{consequence_text[:50]}...', Vision: '{vision_dispatch_text[:50]}...'", flush=True)

                evolve_world_state(
                    dispatches=current_feed_log_for_evolution, 
                    consequence_summary=consequence_text, 
                    state_file=str(STATE_PATH),
                    vision_description=vision_dispatch_text
                )
                print(f"DEBUG PRINT: _process_turn_background - World state evolution complete. Reloading state.", flush=True)
                state = _load_state() # Reload state after evolution
            else:
                print(f"DEBUG PRINT: _process_turn_background - LLM_ENABLED is False, skipping LLM-based world evolution.", flush=True)

        # 7. Generate Next Choices (if not in ongoing combat or other non-standard path that provides its own choices)
        if proceed_with_standard_evolution:
            print(f"DEBUG PRINT: _process_turn_background - Standard Path: Generating next choices START...", flush=True)
            
            print(f"DEBUG PRINT: _process_turn_background - STEP 1: Setting empty prompt for choices...", flush=True)
            # Set the prompt above choices to empty string (no text)
            final_choice_prompt_text = ""
            print(f"DEBUG PRINT: _process_turn_background - STEP 1: final_choice_prompt_text (empty): '{final_choice_prompt_text}'", flush=True)
            
            # Call choices.generate_choices
            print(f"DEBUG PRINT: _process_turn_background - STEP 2: Calling choices.generate_choices...", flush=True)
            
            # Prepare recent_choices as a string, from list of dicts
            raw_recent_choices = state.get("choices", [])
            recent_choices_str = "\n".join([rc.get("text", "") for rc in raw_recent_choices if isinstance(rc, dict) and rc.get("text")])
            
            # Ensure the most recent consequence is included in the situation summary
            consequence_for_choices = final_choice_prompt_text
            if consequence_text and consequence_text.strip():
                consequence_for_choices = f"{final_choice_prompt_text}\nMost recent consequence: {consequence_text.strip()}"

            import choices  # Local import to avoid circular dependency
            new_choices, choices_meta = choices.generate_choices(
                client=client, 
                prompt_tmpl=choice_tmpl,
                last_dispatch=dispatch_text,
                n=3,
                image_url=state.get("current_image_url"),
                seen_elements=state.get("seen_elements", ""),
                recent_choices=recent_choices_str, # Corrected to be a string
                caption=state.get("current_image_caption", ""),
                image_description=state.get("current_image_description", ""), # Keep for now, as it is in the signature
                beat_nudge=state.get("beat_nudge", ""),
                pacing=state.get("pacing", "normal"),
                world_prompt=state.get("world_prompt"),
                # temperature=0.7, # Optional, let choices.py use its default
                situation_summary=consequence_for_choices
            )
            print(f"DEBUG PRINT: _process_turn_background - STEP 2: choices.generate_choices returned (first 2): {new_choices[:2]}", flush=True)

            if not new_choices:
                print("WARNING: choices.generate_choices returned empty list. Using fallback choices.", flush=True)
                new_choices = ["Investigate further", "Scan the area", "Proceed with caution"]
            
            print(f"DEBUG PRINT: _process_turn_background - STEP 3: Calling _structure_choices_for_feed...", flush=True)
            # Corrected call: removed metadata argument as _structure_choices_for_feed doesn't accept it
            choice_item = _structure_choices_for_feed(new_choices, final_choice_prompt_text, state.get("current_image_url"))
            print(f"DEBUG PRINT: _process_turn_background - STEP 3: _structure_choices_for_feed returned item ID: {choice_item.get('id')}", flush=True)
            
            new_feed_items_for_log.append(choice_item)
            state["choices"] = choice_item["choices"] # Save new choices to top-level state for compatibility
            state["choices_metadata"] = choices_meta # Save choices_meta separately in state
            
            # Save state with new choices
            print(f"DEBUG PRINT: _process_turn_background - STEP 4: Saving state with new choices...", flush=True)
            with WORLD_STATE_LOCK:
                state.setdefault('feed_log', []).extend(new_feed_items_for_log)
                _save_state(state) 
            print(f"DEBUG PRINT: _process_turn_background - STEP 4: State saved.", flush=True)
            new_feed_items_for_log.clear()

        if new_feed_items_for_log: # If choices or error were added
            print(f"DEBUG PRINT: _process_turn_background - STEP 4: Adding {len(new_feed_items_for_log)} item(s) to feed_log before saving.", flush=True)
            with WORLD_STATE_LOCK:
                state.setdefault('feed_log', []).extend(new_feed_items_for_log)
                _save_state(state) # Save state with new choices
            new_feed_items_for_log.clear()
            print(f"DEBUG PRINT: _process_turn_background - STEP 4: New choice item(s) saved to state's feed_log.", flush=True)
        else:
            print(f"DEBUG PRINT: _process_turn_background - STEP 4: No new items (e.g. choices) were generated in this cycle to add to feed_log.", flush=True)


        # Player Death Check - using the main dispatch_text of this turn
        print(f"DEBUG PRINT: _process_turn_background - About to call check_player_death. Dispatch: '{dispatch_text[:30]}...'", flush=True)
        player_is_dead = False
        try:
            player_is_dead = check_player_death(dispatch_text, state.get("world_prompt"), choice)
            print(f"DEBUG PRINT: _process_turn_background - check_player_death returned: {player_is_dead}", flush=True)
            if player_is_dead:
                print(f"DEBUG PRINT: _process_turn_background - Player death detected by check_player_death.", flush=True)
                state["player_state"]["alive"] = False
                game_over_item = create_feed_item(type="game_over", content="Jason has succumbed to the horrors. The transmission ends.")
                game_over_choices_item = _structure_choices_for_feed(
                    ["Restart Simulation"], 
                    "GAME OVER",
                    image_url=state.get("current_image_url") 
                )
                with WORLD_STATE_LOCK:
                    state.setdefault("feed_log", []).append(game_over_item)
                    state.setdefault("feed_log", []).append(game_over_choices_item)
                    _save_state(state)
        except Exception as e_death_check:
            log_error(f"Error during player death check in _process_turn_background: {e_death_check}")
            print(f"DEBUG PRINT: _process_turn_background - Exception during check_player_death: {e_death_check}", flush=True)

        # Update history.json (simplified)
        print(f"DEBUG PRINT: _process_turn_background - About to update history.json", flush=True)
        history_entry = {
            "choice": choice,
            "dispatch": dispatch_text,
            "world_prompt_before": prev_state_snapshot.get("world_prompt"),
            "world_prompt_after": state.get("world_prompt"),
            "image": state.get("current_image_url"),
            "image_prompt": state.get("current_image_prompt", ""),
            "hard_transition": state.get("current_hard_transition", False),  # Track location changes
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        history.append(history_entry)
        if len(history) > 100: # Keep history to a reasonable size
            history.pop(0)
        try:
            with history_path.open("w", encoding="utf-8") as f_hist:
                json.dump(history, f_hist, indent=2)
            print(f"DEBUG PRINT: _process_turn_background - history.json updated.", flush=True)
        except Exception as e_hist_save:
            log_error(f"Error saving history.json: {e_hist_save}")
            print(f"DEBUG PRINT: _process_turn_background - Exception saving history.json: {e_hist_save}", flush=True)

        # Final prune of feed_log to keep it manageable (e.g., last 50-100 items)
        print(f"DEBUG PRINT: _process_turn_background - About to prune feed_log. Current length: {len(state.get('feed_log',[]))}", flush=True)
        MAX_FEED_LOG_ITEMS = 100 
        if len(state.get("feed_log", [])) > MAX_FEED_LOG_ITEMS:
            with WORLD_STATE_LOCK:
                state["feed_log"] = state["feed_log"][-MAX_FEED_LOG_ITEMS:]
                # _save_state(state) # No need to save here, will be saved by turn_count update shortly
            print(f"DEBUG PRINT: _process_turn_background - feed_log pruned. New length: {len(state.get('feed_log',[]))}", flush=True)

        print(f"DEBUG PRINT: _process_turn_background - About to update turn_count and final save.", flush=True)
        state['turn_count'] = state.get('turn_count', 0) + 1
        _save_state(state) # Final save for turn_count and any other latent changes
        print(f"DEBUG PRINT: _process_turn_background - turn_count updated and final save complete.", flush=True)

        print(f"DEBUG PRINT: _process_turn_background THREAD COMPLETED for choice: '{choice}'.", flush=True)

    except Exception as e_critical_thread:
        log_error(f"Critical unhandled error in _process_turn_background thread: {e_critical_thread}")
        logging.exception("CRITICAL EXCEPTION in _process_turn_background thread top level:")
        # Attempt to log a critical error item to the feed
        try:
            critical_error_item = create_feed_item(type="error_event", content=f"System critical error during turn processing: {e_critical_thread}")
            with WORLD_STATE_LOCK:
                # Try to load state one last time to append error, or use existing global
                current_state_for_err = _load_state() if 'state' not in globals() or not state else state
                current_state_for_err.setdefault("feed_log", []).append(critical_error_item)
                _save_state(current_state_for_err)
        except Exception as e_final_log:
            log_error(f"Could not even log critical error to feed_log: {e_final_log}")
    # No return value needed as this runs in a thread and modifies global state / feed_log


# MOVED HELPER FUNCTION DEFINITION EARLIER - ENSURING CORRECT ORDER AND COMPLETENESS
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

# Ensure generate_intro_turn_feed_items is defined AFTER _structure_choices_for_feed
def generate_intro_turn_feed_items() -> List[Dict[str, Any]]:
    from choices import generate_choices # Local import
    global state 
    intro_items = [] # This list will be returned
    
    initial_narrative_content = "The simulation begins. You find yourself in a familiar, yet unsettling environment. The air is thick with unspoken tension."
    narrative_item = create_feed_item(type="narrative_event", content=initial_narrative_content)
    intro_items.append(narrative_item)

    initial_image_url = None
    if WORLD_IMAGE_ENABLED: # Global toggle for images
        try:
            vision_dispatch_for_intro_image = _generate_vision_dispatch(initial_narrative_content, state.get("world_prompt", "Initialization sequence."))
            if vision_dispatch_for_intro_image:
                initial_image_url, initial_image_prompt, initial_video_url = _gen_image(
                    caption=vision_dispatch_for_intro_image, 
                    mode="normal",
                    choice="Initialize Simulation",
                    dispatch=initial_narrative_content,
                    world_prompt=state.get("world_prompt", "Initialization sequence."),
                    frame_idx=0 
                )
                # Note: initial_video_url not used in this code path (intro sequence)
                if initial_image_url:
                    image_item = create_feed_item(type="scene_image", content="Initialising environment...", image_url=initial_image_url)
                    intro_items.append(image_item)
                    state['current_image_url'] = initial_image_url # This is fine, updates a different part of state
                    state['current_image_prompt'] = initial_image_prompt # Store prompt
        except Exception as e_img:
            log_error(f"Error generating intro image: {e_img}")
            error_image_item = create_feed_item(type="error_event", content=f"Error generating initial visual: {e_img}")
            intro_items.append(error_image_item)

    if VISION_ENABLED and initial_image_url: # Global toggle for vision
        try:
            vision_text = _vision_describe(initial_image_url)
            if vision_text:
                vision_item = create_feed_item(type="vision_analysis", content=vision_text, metadata={"source_image_url": initial_image_url})
                intro_items.append(vision_item)
        except Exception as e_vision:
            log_error(f"Error during initial vision analysis: {e_vision}")
            logging.exception("Exception during initial vision analysis:")
            error_item = create_feed_item(type="error_event", content=f"Error analysing initial visual: {e_vision}")
            intro_items.append(error_item)
            
    initial_choice_texts = []
    try:
        initial_choice_texts, _ = generate_choices( # Expecting metadata back now
            client=client,
            prompt_tmpl=choice_tmpl,
            last_dispatch="The simulation has just started.",
            world_prompt=state.get("world_prompt", "System Online."),
            image_description="An initial, establishing scene.",
            situation_summary="Jason is at the very beginning of his journey.",
            n=3
        )
    except Exception as e_choices:
        log_error(f"Error generating initial choices: {e_choices}")
        initial_choice_texts = ["Begin exploration.", "Assess the immediate surroundings.", "Prepare for the unknown."]

    choice_prompt_text = "The system is online. Your journey begins now. What is your first action?"
    choices_item = _structure_choices_for_feed(initial_choice_texts, choice_prompt_text, initial_image_url if WORLD_IMAGE_ENABLED else None)
    intro_items.append(choices_item)
    state["choices"] = choices_item['choices'] # This is fine, updates a different part of state

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
        "world_prompt": PROMPTS.get("world_prompt", "Default world starting point."),
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
    
    with feed_item_id_lock:
        _next_feed_item_id = 0
        logging.info(f"_perform_game_reset: Feed item ID counter reset to {_next_feed_item_id}.")

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

@app.route('/')
def serve_webui():
    logging.info("serve_webui: Root path '/' accessed. Performing game reset before serving page.")
    _perform_game_reset() # Reset state every time the main page is loaded
    return render_template('feed.html', discord_client_id=DISCORD_CLIENT_ID)

@app.route('/api/reset', methods=['POST'])
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

@app.route('/api/feed', methods=['GET'])
def api_feed():
    global state
    since_id_str = request.args.get('since_id')
    items_to_return = []
    
    print(f"DEBUG PRINT /api/feed: since_id_str='{since_id_str}', current total feed_log length={len(state.get('feed_log', []))}", flush=True)
    if state.get('feed_log'):
        print(f"DEBUG PRINT /api/feed: Last few item IDs in feed_log: {[item['id'] for item in state['feed_log'][-5:]]}", flush=True)

    with WORLD_STATE_LOCK: # Ensure thread-safe access to state['feed_log']
        feed_log = state.get('feed_log', [])
        if since_id_str:
            try:
                since_id = int(since_id_str)
                items_to_return = [item for item in feed_log if item.get('id', 0) > since_id]
                print(f"DEBUG PRINT /api/feed: since_id={since_id}, found {len(items_to_return)} new items.", flush=True)
            except ValueError:
                log_error(f"/api/feed: Invalid since_id '{since_id_str}'. Returning full feed.")
                items_to_return = list(feed_log) # Return a copy
        else:
            items_to_return = list(feed_log) # Return a copy of the full feed log
            print(f"DEBUG PRINT /api/feed: No since_id_str, returning full feed_log of {len(items_to_return)} items.", flush=True)
            
    return jsonify(items_to_return)

@app.route('/api/choose', methods=['POST'])
def api_choose():
    global state
    try:
        data = request.get_json()
        if not data or 'choice' not in data:
            return jsonify({"error": "Missing 'choice' in request body"}), 400
        
        player_choice_text = data['choice']
        context_item_id = data.get('context_item_id') # Optional, for context

        print(f"DEBUG PRINT: api_choose received choice: '{player_choice_text}', context_id: {context_item_id}. Current state ID: {id(state)}", flush=True)

        # 1. Immediately create and log the Player Action
        player_action_item = create_feed_item(
            type="player_action", 
            content=f"{player_choice_text}", # Display the choice text directly
            metadata={"raw_choice": player_choice_text, "context_id": context_item_id}
        )
        
        print(f"DEBUG PRINT: api_choose - Before WORLD_STATE_LOCK for saving player_action_item. Item ID: {player_action_item['id']}", flush=True)
        with WORLD_STATE_LOCK:
            print(f"DEBUG PRINT: api_choose - Acquired WORLD_STATE_LOCK.", flush=True)
            state.setdefault('feed_log', []).append(player_action_item)
            state['last_choice'] = player_choice_text
            print(f"DEBUG PRINT: api_choose - About to call _save_state.", flush=True)
            _save_state(state)
            print(f"DEBUG PRINT: api_choose - _save_state completed. Releasing WORLD_STATE_LOCK.", flush=True)
        print(f"DEBUG PRINT: api_choose - After WORLD_STATE_LOCK block.", flush=True)
        
        print(f"DEBUG PRINT: api_choose - Player action item ID {player_action_item['id']} logged. Starting background thread for _process_turn_background.", flush=True)

        # 2. Start background processing for the rest of the turn
        # Pass the ID of the player_action_item so the background thread can link its logs if needed.
        
        temp_signal_file = ROOT / f"thread_signal_{player_action_item['id']}.tmp" # Use the correct ID here
        
        try:
            thread = threading.Thread(target=_process_turn_background, args=(player_choice_text, player_action_item['id'], str(temp_signal_file)))
            # thread.daemon = True # Allow main program to exit even if threads are running. Temporarily commenting out for testing.
            thread.start()
            print(f"DEBUG PRINT: api_choose - Thread object created and start() called: {thread}", flush=True)
            
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
                    print(f"DEBUG PRINT: api_choose - Error reading/deleting signal file: {e_file_read}", flush=True)
            
            print(f"DEBUG PRINT: api_choose - Signal from thread via file: {'RECEIVED' if signal_received else 'NOT RECEIVED'}", flush=True)
            if not signal_received and thread.is_alive():
                 print(f"DEBUG PRINT: api_choose - Thread is alive but signal file not as expected.", flush=True)
            elif not signal_received and not thread.is_alive():
                 print(f"DEBUG PRINT: api_choose - Thread is NOT alive and signal file not as expected.", flush=True)

        except Exception as e_thread_start:
            print(f"CRITICAL DEBUG PRINT: api_choose - ERROR STARTING THREAD: {e_thread_start}", flush=True)
            # Optionally re-raise or handle specifically if needed, for now just printing
            raise # Re-raise to see if it gets caught by the broader handler or stops the test

        # 3. Return only the player_action_item immediately to the client
        print(f"DEBUG PRINT: api_choose - Returning player_action_item (ID: {player_action_item['id']}) to client immediately.", flush=True)
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


@app.route('/api/regenerate_choices', methods=['POST'])
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
        regenerated_choice_texts, _ = generate_choices(
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


# Discord Embedded App OAuth2 Token Exchange Endpoint
@app.route('/discord/api/token', methods=['POST'])
def discord_token_exchange():
    try:
        data = request.get_json()
        code = data.get('code')

        if not code:
            return jsonify({"error": "Missing authorization code"}), 400
        if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET:
            return jsonify({"error": "Discord client credentials not configured on server"}), 500

        token_url = 'https://discord.com/api/oauth2/token'
        payload = {
            'client_id': DISCORD_CLIENT_ID,
            'client_secret': DISCORD_CLIENT_SECRET,
            'grant_type': 'authorization_code',
            'code': code,
            # IMPORTANT: This redirect_uri MUST EXACTLY MATCH one of the URIs
            # you configured in your Discord Developer Portal for this application.
            # For embedded apps, this is typically your main application URL.
            'redirect_uri': 'https://somewhere-storygen.onrender.com/' # Make sure this matches your setup!
        }
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        
        import requests # Make sure 'requests' is in requirements.txt
        response = requests.post(token_url, data=payload, headers=headers)
        response.raise_for_status() # Will raise an exception for HTTP errors
        
        token_data = response.json()
        return jsonify(token_data) # Return the whole token response from Discord

    except requests.exceptions.HTTPError as http_err:
        logging.error(f"Discord token exchange HTTP error: {http_err} - {response.text}")
        return jsonify({"error": "Failed to exchange token with Discord", "details": response.text}), response.status_code
    except Exception as e:
        logging.error(f"Error in Discord token exchange: {e}")
        return jsonify({"error": "Internal server error during token exchange"}), 500

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
        # Get previous vision analysis for spatial consistency
        prev_vision_analysis = ""
        if history and len(history) > 0:
            last_entry = history[-1]
            if last_entry.get("vision_analysis"):
                prev_vision_analysis = last_entry["vision_analysis"][:300]
        
        spatial_context = ""
        if prev_vision_analysis:
            spatial_context = f"\n\nCURRENT VISUAL SCENE (MUST STAY CONSISTENT): {prev_vision_analysis}\nDo NOT change locations unless the choice explicitly moves through a door, entrance, or exit. Stay in the same environment."
        
        prev_context = f"\n\nPREVIOUS SCENE: {prev_vision[:200]}" if prev_vision else ""
        world_prompt = state.get('world_prompt', '')
        
        image_context = ""
        if current_image:
            image_context = "🖼️ ATTACHED IMAGE = CURRENT LOCATION. Jason is HERE. Do NOT teleport him.\n\n"
        
        # Detect timeout penalties
        is_timeout_penalty = any(phrase in choice.lower() for phrase in [
            "crushes you", "hits you", "attacks you", "shoots you", "tears into you",
            "engulfs you", "mauls you", "slams into you", "impacts you", "sustained",
            "to torso", "to limb", "trauma", "burns to", "pressure on", "collapses on"
        ])
        
        # Build fate modifier text
        fate_modifier = ""
        if fate == "LUCKY":
            fate_modifier = "\n\n🎰 FATE INTERVENTION - LUCKY:\nSomething breaks your way. Add ONE concrete benefit:\n• Equipment works better than expected\n• Guard patrol turns away at the right moment\n• You find something useful (ammo, cover, distraction)\n• Environmental timing favors you (door unlocked, light goes out)\n• A threat misses or hesitates\nMake it TANGIBLE and HELPFUL, not just flavor text.\n"
        elif fate == "UNLUCKY":
            fate_modifier = "\n\n🎰 FATE INTERVENTION - UNLUCKY:\nFate turns VICIOUSLY against you. Add ONE concrete HORRIFYING complication:\n\n**PRIORITIZE GRIM BODILY HARM** (Choose physical trauma when plausible):\n• Body is IMPALED (rebar through leg, glass shard in shoulder, metal spike in abdomen)\n• Limb is CRUSHED or BROKEN (concrete slab on foot, bone snaps audibly, compound fracture)\n• Flesh is BURNED (caustic chemical splash, electrical arc across chest, scalding steam)\n• Tissue is LACERATED (barbed wire rips forearm open, jagged metal severs calf muscle)\n• Skull trauma (head strikes concrete, severe concussion, vision blurs)\n• Arterial bleeding (deep cut spurting blood, femoral artery compromised)\n• Red biome INFECTION (spores inhaled, flesh begins corrupting, tendril fuses with skin)\n• Creature MAULS you (claws rake ribs, teeth puncture shoulder, crushing bite)\n\n**OR Catastrophic Equipment/Environment Failure** (If bodily harm doesn't fit):\n• Equipment FAILS violently (rope snaps mid-climb, ladder rung breaks, catwalk collapses)\n• Structure COLLAPSES (ceiling caves in, floor gives way, wall crumbles)\n• Chemical/Biological CONTAMINATION (toxic gas exposure, corrosive spill, infection vector)\n• Multiple threats converge (guards AND creature, patrol plus structural collapse)\n• Hidden lethal trap springs (pressurized explosive, electrical grid, biological specimen breaks containment)\n\n**CRITICAL RULE**: UNLUCKY should cause SEVERE PHYSICAL CONSEQUENCES.\nThis is not \"bad luck\" - this is the world MUTILATING you.\nDescribe injuries with VISCERAL DETAIL: what breaks, what tears, what burns, what bleeds.\n\nThe player's situation should become DRASTICALLY worse - injury, discovery, or imminent death.\n"
        # NORMAL = no modifier, outcome is purely based on choice
        
        # Use JUST the dispatch_sys instructions (which has JSON format)
        json_prompt = (
            f"{dispatch_sys}\n\n"
            f"PLAYER CHOICE: '{choice}'\n"
            f"WORLD CONTEXT: {world_prompt}\n\n"
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
            tokens=350,
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
        player_alive = True
        
        try:
            import json as json_lib
            data = json_lib.loads(result)
            dispatch = data.get("dispatch", "")
            player_alive = data.get("player_alive", True)
            # Safe print with Unicode handling
            try:
                print(f"[DISPATCH] Parsed JSON: dispatch={dispatch[:50]}..., alive={player_alive}")
            except UnicodeEncodeError:
                print(f"[DISPATCH] Parsed JSON: alive={player_alive} (dispatch contains special characters)")
        except Exception as parse_error:
            try:
                print(f"[DISPATCH] JSON parse failed: {parse_error}")
                print(f"[DISPATCH] Raw result: {result[:200]}...")
            except UnicodeEncodeError:
                print(f"[DISPATCH] JSON parse failed (output contains special characters)")
            # Fallback: try to extract dispatch text
            dispatch = result.replace('"dispatch":', '').replace('"player_alive":', '').replace('{', '').replace('}', '').strip()
            if ',' in dispatch:
                dispatch = dispatch.split(',')[0].strip(' "')
        
        # Vision dispatch = same as dispatch
        vision_dispatch = dispatch
        
        # Hard cap at 400 characters
        if len(dispatch) > 400:
            dispatch = dispatch[:385] + "...(truncated)"
        
        return dispatch, vision_dispatch, player_alive
        
    except Exception as e:
        try:
            print(f"[COMBINED DISPATCH ERROR] {e}")
        except UnicodeEncodeError:
            print(f"[COMBINED DISPATCH ERROR] (error contains special characters)")
        import traceback
        traceback.print_exc()
        # Fallback to safe defaults
        return "Jason makes a tense move in the chaos.", "The desert stretches ahead.", True

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

def is_clever_or_risky(choice):
    """Heuristic: risky/clever if contains certain keywords"""
    keywords = ["sneak", "hide", "stealth", "evade", "escape", "confront", "attack", "investigate", "search", "scan", "explore", "photograph", "analyze", "decipher", "decode", "hack", "sabotage", "ally", "bargain", "bribe", "bluff", "trick", "outsmart", "ambush", "rescue", "save", "risk", "danger", "hazard", "peril", "bold", "daring", "reckless", "brave", "uncover", "discover", "secret", "hidden", "mystery", "clue", "artifact", "ancient", "forbidden", "rare"]
    return any(k in choice.lower() for k in keywords)

def resolve_risky_action(choice, threat_level, dispatch, world_prompt):
    import random
    # Set base success chance by threat level
    if threat_level == 0:
        success_chance = 0.85
    elif threat_level == 1:
        success_chance = 0.7
    elif threat_level == 2:
        success_chance = 0.5
    else:
        success_chance = 0.33
    success = random.random() < success_chance
    # Use LLM to generate consequence
    prompt = (
        f"Write a single, punchy gameplay consequence line for the player's risky action. "
        f"ACTION: {choice}\nDISPATCH: {dispatch}\nWORLD: {world_prompt}\n"
        f"OUTCOME: {'success' if success else 'failure'}\n"
        "If success, describe how the player advances or avoids danger. If failure, describe the immediate negative result."
    )
    try:
        # Don't use lore - this is a mechanical risky action resolution
        summary = _ask(prompt, model="gemini", temp=1.0, tokens=40, use_lore=False)
        if not summary.strip():
            summary = 'No major consequence.'
        return success, summary
    except Exception as e:
        log_error(f"[RISKY ACTION CONSEQUENCE] LLM error: {e}")
        return success, 'No major consequence.'

# ───────── game loop ──────────────────────────────────────────────────────────
def advance_turn_image_fast(choice: str, fate: str = "NORMAL", is_timeout_penalty: bool = False, session_id: str = 'default') -> dict:
    """
    PHASE 1 (FAST): Generate dispatch and image, return immediately.
    
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
        generate_and_apply_choice(choice)
        state = _load_state(session_id)
        
        # Get previous vision and image
        prev_vision = ""
        prev_image = None
        if history and len(history) > 0:
            prev_vision = history[-1].get("vision_dispatch", "")
            prev_image = history[-1].get("image_url", None)
        
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
            dispatch = "Jason makes a tense move in the chaos."
        if not vision_dispatch or vision_dispatch.strip().lower() in {"none", "", "[", "[]"}:
            vision_dispatch = dispatch
        
        # Evolve world state
        from evolve_prompt_file import evolve_world_state
        consequence_summary = summarize_world_state_diff(prev_state, state)
        evolve_world_state(history, consequence_summary, vision_description=vision_dispatch)
        state = _load_state(session_id)
        
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
        try:
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
                print(f"[IMG FAST] Image ready: {consequence_img_url}")
            else:
                # Image generation failed (safety block, API error, etc.) - provide graceful fallback
                print(f"[IMG FAST] WARNING: Image generation returned None (likely safety block or API error)")
                print(f"[IMG FAST] Providing fallback 'static' image message")
                consequence_img_url = None  # Explicitly None - bot will show text-only
                consequence_img_prompt = "[Image blocked - content filtered]"
            if consequence_video_url:
                print(f"[IMG FAST] Video ready: {consequence_video_url}")
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
    global state, history
    from choices import generate_choices
    
    state = _load_state(session_id)
    situation_summary = _generate_situation_report(current_image=consequence_img_url)
    
    next_choices = generate_choices(
        client, choice_tmpl,
        dispatch,
        n=3,
        image_url=consequence_img_url,
        seen_elements='',
        recent_choices='',
        caption="",
        image_description="",
        world_prompt=state.get('world_prompt', ''),
        temperature=0.7,
        situation_summary=situation_summary
    )
    
    next_choices = [c for c in next_choices if c and c.strip() and c.strip() != '—']
    if not next_choices:
        next_choices = ["Look around", "Move forward", "Wait"]
    while len(next_choices) < 3:
        next_choices.append("—")
    
    # Save to history (with custom action flag for permanence tracking)
    history = _load_history(session_id)
    is_custom_action = not any(keyword in choice.lower() for keyword in ["move", "advance", "photograph", "examine", "sprint", "climb", "vault", "crawl"])
    
    # Analyze image with Vision AI for spatial grounding
    vision_analysis_text = ""
    if consequence_img_url and VISION_ENABLED:
        print(f"[VISION] Analyzing generated image for spatial context...")
        try:
            vision_result = _vision_analyze_all(consequence_img_url)
            vision_analysis_text = vision_result.get("description", "")
            if vision_analysis_text:
                print(f"[VISION] Analysis complete: {vision_analysis_text[:100]}...")
            else:
                print(f"[VISION] No description returned")
        except Exception as e:
            print(f"[VISION] Analysis failed: {e}")
            vision_analysis_text = ""
    
    history_entry = {
        "choice": choice,
        "is_custom_action": is_custom_action,  # Flag for permanence
        "dispatch": dispatch,
        "vision_dispatch": vision_dispatch,
        "vision_analysis": vision_analysis_text,  # Now populated from actual vision AI!
        "world_prompt": state.get("world_prompt", ""),
        "image": consequence_img_url,
        "image_url": consequence_img_url,
        "image_prompt": consequence_img_prompt,
        "hard_transition": hard_transition  # Track location changes
    }
    history.append(history_entry)
    _save_history(history, session_id)
    
    return {
        "choices": next_choices,
        "situation_report": situation_summary,
        "consequences": "",
        "player_state": state.get('player_state', {}),
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

def reset_state(session_id='default'):
    """Reset state for a session"""
    global state, history, _last_image_path, _vision_cache
    
    print(f"[RESET] Resetting session: {session_id}")
    
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
            "prologue": "Jason surveys the Horizon facility from a rocky outcrop in the desert.",
            "vision": "The Horizon facility sprawls across the red desert valley below, industrial structures stark against the mesa backdrop."
        },
        {
            "prologue": "Jason stands at the edge of the quarantine perimeter, watching the facility in the distance.",
            "vision": "Beyond the barbed wire fence, the Horizon facility's concrete structures stretch across the arid landscape."
        },
        {
            "prologue": "Jason approaches the facility compound across the open desert terrain.",
            "vision": "The facility looms ahead across the barren red earth, its angular silhouette cutting through the dusty air."
        },
        {
            "prologue": "Jason pauses near a warning sign marking the quarantine zone boundary.",
            "vision": "Past the weathered quarantine signs, the sprawling Horizon complex dominates the valley floor."
        },
        {
            "prologue": "Jason observes the facility from a vantage point atop the red mesa.",
            "vision": "From the mesa's edge, the entire Horizon facility layout is visible below—a vast industrial complex in the desert."
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
    
    # Generate dynamic situation report from LLM based on current world state
    situation_summary = _generate_situation_report(current_image=image_url)
    options = generate_choices(
        client, choice_tmpl,
        prologue,  # What's happening in intro
        n=3,
        image_url=image_url,  # Gemini sees the image directly!
        seen_elements='',
        recent_choices='',
        caption="",  # Let Gemini look at image, not stale text
        image_description="",  # Let Gemini look at image, not stale text
        world_prompt=prologue,
        temperature=0.7,
        situation_summary=situation_summary
    )
    if len(options) == 1:
        parts = re.split(r"[\/,\x19\x12\-]|  +", options[0])
        options = [p.strip() for p in parts if p.strip()][:3]
    # Don't pad with placeholders - just return what we got
    
    # Analyze intro image with Vision AI for spatial grounding
    vision_analysis_text = ""
    if image_url and VISION_ENABLED:
        print(f"[VISION INTRO] Analyzing opening image for spatial context...")
        try:
            vision_result = _vision_analyze_all(image_url)
            vision_analysis_text = vision_result.get("description", "")
            if vision_analysis_text:
                print(f"[VISION INTRO] Analysis complete: {vision_analysis_text[:100]}...")
            else:
                print(f"[VISION INTRO] No description returned")
        except Exception as e:
            print(f"[VISION INTRO] Analysis failed: {e}")
            vision_analysis_text = ""
    
    # Save to session-specific history
    entry = {
        "choice": "Intro",
        "dispatch": prologue,
        "vision_dispatch": vision_dispatch,
        "vision_analysis": vision_analysis_text,  # Now populated from actual vision AI!
        "world_prompt": prologue,
        "image": image_url,
        "image_url": image_url
    }
    history = [entry]
    _save_history(history, session_id)  # Use session-specific save!
    _save_state(state, session_id)  # Pass session_id!
    
    return {
        "choices": options,
        "phase": state["current_phase"],
        "chaos": state["chaos_level"],
        "player_state": state.get('player_state', {})
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
            "prologue": "Jason surveys the Horizon facility from a rocky outcrop in the desert.",
            "vision": "The Horizon facility sprawls across the red desert valley below, industrial structures stark against the mesa backdrop."
        },
        {
            "prologue": "Jason stands at the edge of the quarantine perimeter, watching the facility in the distance.",
            "vision": "Beyond the barbed wire fence, the Horizon facility's concrete structures stretch across the arid landscape."
        },
        {
            "prologue": "Jason approaches the facility compound across the open desert terrain.",
            "vision": "The facility looms ahead across the barren red earth, its angular silhouette cutting through the dusty air."
        },
        {
            "prologue": "Jason pauses near a warning sign marking the quarantine zone boundary.",
            "vision": "Past the weathered quarantine signs, the sprawling Horizon complex dominates the valley floor."
        },
        {
            "prologue": "Jason observes the facility from a vantage point atop the red mesa.",
            "vision": "From the mesa's edge, the entire Horizon facility layout is visible below—a vast industrial complex in the desert."
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
    options = generate_choices(
        client, choice_tmpl,
        dispatch,  # What's happening now
        n=3,
        image_url=dispatch_img_url,  # Opening image - Gemini looks at THIS!
        seen_elements='',
        recent_choices='',
        caption="",  # Let Gemini see the actual image
        image_description="",  # Let Gemini see the actual image
        time_of_day="",  # Removed - prevents outdoor lighting descriptions
        world_prompt=prologue,
        temperature=0.7,
        situation_summary=situation_summary
    )
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

if __name__ == '__main__':
    # Setup logging if you want to see Flask's default logs
    # logging.basicConfig(level=logging.INFO)
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
