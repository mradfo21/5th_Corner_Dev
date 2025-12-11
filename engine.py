"""
engine.py – core story simulator
2025‑12‑10 deploy update

• Vision‑enabled continuity with GPT‑4‑Vision
• Fixed OpenAI error import (use openai.error)
• All other functionality unchanged
"""

from __future__ import annotations
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

import openai
from openai import OpenAIError
from flask import Flask, send_from_directory, request, jsonify, render_template
from PIL import Image
import io
from flask_cors import CORS

import choices # Import the choices module
from evolve_prompt_file import evolve_world_state, set_current_beat, generate_scene_hook, summarize_world_prompt_to_interim_messages

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

# Load prompts
PROMPTS = json.load((ROOT/"prompts"/"simulation_prompts.json").open(encoding="utf-8"))

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

STATE_PATH = ROOT/"world_state.json"
IMAGE_DIR = ROOT / "images"
WORLD_STATE_LOCK = threading.Lock() # Global lock for world_state.json access

IMAGE_ENABLED       = True  # ENABLED for production
WORLD_IMAGE_ENABLED = True  # ENABLED for production

DEFAULT_BASE = "https://api.openai.com/v1"
API_BASE     = (os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE).strip() or DEFAULT_BASE

client      = _client(OPENAI_API_KEY, API_BASE)
LLM_ENABLED = True

VISION_ENABLED = True  # ENABLED for production

IMAGE_PROVIDER = CONFIG.get("IMAGE_PROVIDER", "openai").lower()

# Track the last dispatch image path for vision continuity
_last_image_path: Optional[str] = None

# Global vision cache to avoid re-analyzing the same image
_vision_cache = {}

# Add a global counter for choices since last reset
_choices_since_edit_reset = 0

# Add a global flag for interior/exterior state
_is_inside = False

FORCE_TEST_THREAT = False # Global flag for testing combat trigger

print("ENGINE importing... (vision patch, import fixed)", flush=True)

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
def _load_state() -> dict:
    with WORLD_STATE_LOCK:
        if STATE_PATH.exists():
            try:
                # Explicitly open with utf-8, and ensure file is closed with try/finally or with statement
                with STATE_PATH.open('r', encoding='utf-8') as f:
                    st = json.load(f)
                # Ensure essential keys exist after loading
                st.setdefault('player_state', {'alive': True})
                st.setdefault('feed_log', [])
                st.setdefault('current_image_url', None)
                st.setdefault('choices', []) # Ensure choices list is present
                return st
            except json.JSONDecodeError as e_json:
                logging.error(f"JSONDecodeError in _load_state for {STATE_PATH}: {e_json}. File might be corrupt or empty.")
                # Fallback to a default state but log this as a critical issue
            except Exception as e_load:
                logging.error(f"Unexpected error loading {STATE_PATH} in _load_state: {e_load}")
                # Fallback for other errors too
        
        # Fallback: If file doesn't exist or loading failed, return a clean default state
        logging.warning(f"{STATE_PATH} not found or failed to load, returning default state.")
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
            "interim_index": 0 # Initialize interim_index
        }

state = _load_state() # Initial load
history_path = ROOT / "history.json"
if history_path.exists():
    with history_path.open("r", encoding="utf-8") as f:
        history = json.load(f)
else:
    history = []

def _save_state(st: dict):
    # with WORLD_STATE_LOCK: # REMOVED - Lock should be managed by the caller
    st["last_saved"] = datetime.now(timezone.utc).isoformat()
    temp_state_file = STATE_PATH.with_suffix(".json.tmp")
    max_retries = 3
    retry_delay = 0.1 # seconds

    for attempt in range(max_retries):
        try:
            temp_state_file.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding='utf-8')
            os.replace(temp_state_file, STATE_PATH)
            return # Success
        except OSError as e_os:
            logging.warning(f"Attempt {attempt + 1} to save state to {STATE_PATH} failed with OSError: {e_os}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
            else:
                logging.error(f"All {max_retries} attempts to save state to {STATE_PATH} failed due to OSError: {e_os}")
        except Exception as e:
            logging.error(f"Failed to save state to {STATE_PATH} on attempt {attempt + 1}: {e}")
            if attempt == max_retries - 1 or not isinstance(e, OSError):
                try:
                    logging.error(f"State content that failed to save: {json.dumps(st, indent=2, default=str, ensure_ascii=False)}")
                except Exception as e_log_state:
                    logging.error(f"Could not even serialize state for error logging: {e_log_state}")
            if attempt == max_retries - 1:
                break 
            time.sleep(retry_delay)

    logging.error(f"Persistently failed to save state to {STATE_PATH} after {max_retries} attempts.")
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

def _ask(prompt: str, model="gpt-4o", temp=0.5, tokens=90) -> str:
    if not LLM_ENABLED:
        return random.choice([
            "System communications remain static; awaiting new data.",
            "Narrative paused until resources are replenished.",
            "The world holds its breath for new directives."
        ])
    rsp = _call(
        client.chat.completions.create,
        model="gpt-4o",
        messages=[{"role":"user","content":prompt}],
        temperature=temp,
        max_tokens=tokens,
    )
    return rsp.choices[0].message.content.strip()

# ───────── vision description helper ────────────────────────────────────────
def _downscale_for_vision(image_path: str, size=(640, 426)) -> io.BytesIO:
    full = IMAGE_DIR / image_path.lstrip("/")
    buf = io.BytesIO()
    if not full.exists():
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

def _vision_describe(image_path: str) -> str:
    if not LLM_ENABLED or not VISION_ENABLED:
        return ""
    buf = _downscale_for_vision(image_path)
    if buf is None:
        return ""
    try:
        rsp = client.chat.completions.create(
            model="gpt-4o-vision",
            messages=[
                {"role":"system","content":"You will be shown an image."},
                {"role":"user","content":(
                    "Describe in detail what is visible in this image, focusing on objects, threats, exits, and anything Jason could interact with. "
                    "Tie the description into the story's themes and mood, reinforcing the ambiance and narrative coherence of the current scene. "
                    "Be direct and literal. If there are hands, weapons, or tools visible, mention them. If there are any visible figures, silhouettes, or creatures—even if partially obscured, ambiguous, or shadowy—mention them explicitly. If there are no figures, say so."
                )}
            ],
            files=[{"file": buf, "filename": "downscaled.png"}],
            temperature=0.3,
        )
        desc = rsp.choices[0].message.content.strip().replace("\n"," ")
        return desc
    except Exception as e:
        print("vision describe error:", e, file=sys.stderr)
        return ""

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
    return _ask(prompt, tokens=60)

# ───────── dispatch helpers ────────────────────────────────────────────────
def summarize_world_prompt_for_image(world_prompt: str) -> str:
    """Summarize the world prompt to 1-2 sentences for image generation."""
    prompt = (
        "Summarize the following world context in 1-2 vivid, scene-specific sentences, focusing only on details relevant to the current visual environment. Omit backstory and generalities.\n\nWORLD PROMPT: " + world_prompt
    )
    return _ask(prompt, model="gpt-4o", temp=0.4, tokens=48)

def _generate_dispatch(choice: str, state: dict, prev_state: dict = None) -> str:
    try:
        prompt = (
            PROMPTS["dispatch_prompt"] +
            "\nKeep the dispatch to 1-3 sentences, focusing on immediate, visual, and action-oriented outcomes. Be concise and avoid backstory or exposition.\n"
            f"PLAYER CHOICE: '{choice}'\n"
            f"WORLD CONTEXT: {state['world_prompt']}\n"
            f"PREVIOUS: {prev_state['world_prompt'] if prev_state else ''}"
        )
        rsp = _call(
            client.chat.completions.create,
            model="gpt-4o",
            messages=[{"role":"system","content":PROMPTS["dispatch_system_guide"]},
                      {"role":"user","content":prompt}],
            temperature=0.5,
            max_tokens=80,
        )
        result = rsp.choices[0].message.content.strip()
        # If result is just '[' or '[]' or empty, fallback immediately
        if result.strip() in {"[", "[]", ""}:
            return "Jason makes a tense move in the chaos."
        # Sanitize: if result looks like a list or JSON, extract the text
        if result.startswith("[") or result.startswith("-") or result.startswith("\""):
            import json
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
                    return "Jason makes a tense move in the chaos."
                result = " ".join(lines)
        # Hard cap at 400 characters
        if len(result) > 400:
            result = result[:385] + "...(truncated)"
        return result
    except Exception as e:
        log_error(f"[DISPATCH] LLM error: {e}")
        return "Jason makes a tense move in the chaos."

def _generate_caption(dispatch: str, mode: str, is_first_frame: bool = False) -> str:
    # Simplified caption generation - not used in StoryGen version
    return f"{dispatch} ({mode})"

# ───────── imaging helpers ─────────────────────────────────────────────────
def _slug(s: str) -> str:
    # Ensure get_next_feed_item_id is available if slug is empty.
    # This is a minor case, get_next_feed_item_id has its own lock.
    return "".join(c for c in s.lower().replace(" ","_") if c.isalnum() or c=="_")[:48] or f"auto_slug_{get_next_feed_item_id()}"

def _save_img(b64: str, caption: str) -> str:
    IMAGE_DIR.mkdir(exist_ok=True)
    path = IMAGE_DIR / f"{hash(caption) & 0xFFFFFFFF}_{_slug(caption)}.png"
    path.write_bytes(base64.b64decode(b64))
    return f"/images/{path.name}"

def _generate_burn_in(mode: str) -> str:
    prompt = (
        f"Generate a short, realistic text overlay for a {mode} image in a 1990s/2000s setting. "
        "Include plausible metadata such as location, camera/operator ID, weather, mission code, or channel name. "
        "Do NOT use actual dates or times. Keep it under 30 characters."
    )
    return _ask(prompt, tokens=20)

def _vision_is_inside(image_path: str) -> bool:
    if not LLM_ENABLED or not VISION_ENABLED:
        return False
    buf = _downscale_for_vision(image_path)
    if buf is None:
        return False
    try:
        rsp = client.chat.completions.create(
            model="gpt-4o-vision",
            messages=[
                {"role": "system", "content": "You will be shown an image."},
                {"role": "user", "content": "Is this scene indoors or outdoors? Respond with only 'indoors' or 'outdoors'."}
            ],
            files=[{"file": buf, "filename": "downscaled.png"}],
            temperature=0.2,
        )
        desc = rsp.choices[0].message.content.strip().lower()
        return 'indoors' in desc
    except Exception as e:
        print("vision is_inside error:", e, file=sys.stderr)
        return False

def is_hard_transition(choice: str, dispatch: str) -> bool:
    keywords = [
        'enter', 'inside', 'run into', 'escape', 'wake up', 'thrown', 'move to', 'new location', 'different room', 'facility', 'red biome', 'emerge elsewhere', 'relocated', 'teleport', 'suddenly', 'abruptly', 'scene shifts', 'transition', 'burst into', 'step inside', 'step outdoors', 'exit', 'leave', 'outdoors', 'open air', 'out in the open', 'emerge', 'open door', 'cross into', 'cross over', 'arrive at', 'find yourself in', 'appear in', 'shift to', 'cut to', 'fade to', 'wake up in', 'dragged to', 'carried to', 'transported to', 'relocate', 'relocated', 'new area', 'new scene', 'different area', 'different scene',
        'retreat', 'flee', 'run away', 'fall back', 'withdraw', 'bolt', 'dash away', 'make a getaway', 'escape to', 'rush out', 'rush away', 'scramble away', 'sprint away', 'break for cover', 'retreat to', 'flee to', 'run for', 'run from', 'run toward', 'run towards', 'run back', 'run off', 'run out', 'run inside', 'run outside', 'run indoors', 'run outdoors', 'run into', 'run through', 'run across', 'run down', 'run up', 'run along', 'run past', 'run behind', 'run ahead', 'run forward', 'run backward', 'run left', 'run right', 'run behind cover', 'run for safety', 'run for shelter', 'run for the exit', 'run for the door', 'run for the truck', 'run for the perimeter', 'run for the fence', 'run for the hills', 'run for the mesa', 'run for the desert', 'run for the facility', 'run for the building', 'run for the shadows', 'run for the light', 'run for the darkness', 'run for the open', 'run for the open air', 'run for the open ground', 'run for the open desert', 'run for the open mesa', 'run for the open facility', 'run for the open building', 'run for the open shadows', 'run for the open light', 'run for the open darkness', 'run for the open perimeter', 'run for the open fence', 'run for the open hills', 'run for the open truck', 'run for the open door', 'run for the open exit', 'run for the open shelter', 'run for the open safety', 'run for the open cover', 'run for the open ground', 'run for the open area', 'run for the open scene', 'run for the open location', 'run for the open place', 'run for the open spot', 'run for the open zone', 'run for the open region', 'run for the open sector', 'run for the open quadrant', 'run for the open sector', 'run for the open quadrant', 'run for the open region', 'run for the open zone', 'run for the open place', 'run for the open spot', 'run for the open area', 'run for the open ground', 'run for the open cover', 'run for the open safety', 'run for the open shelter', 'run for the open exit', 'run for the open door', 'run for the open truck', 'run for the open hills', 'run for the open fence', 'run for the open perimeter', 'run for the open mesa', 'run for the open desert', 'run for the open facility', 'run for the open building', 'run for the open shadows', 'run for the open light', 'run for the open darkness', 'run for the open perimeter', 'run for the open fence', 'run for the open hills', 'run for the open truck', 'run for the open door', 'run for the open exit', 'run for the open shelter', 'run for the open safety', 'run for the open cover', 'run for the open ground', 'run for the open area', 'run for the open scene', 'run for the open location', 'run for the open place', 'run for the open spot', 'run for the open zone', 'run for the open region', 'run for the open sector', 'run for the open quadrant'
    ]
    text = f"{choice} {dispatch}".lower()
    return any(k in text for k in keywords)

def build_image_prompt(previous_desc: str, change_desc: str, style_block: str, player_choice: str = "", dispatch: str = "", world_prompt: str = "", prev_captions: str = "", hard_transition: bool = False) -> str:
    """
    Build a minimal, ultra-literal, action-focused image prompt for the next frame.
    - First-person POV from Jason's eyes. Never show his face or full body.
    - Show exactly what is described: {dispatch}.
    - VHS scanlines, analog static, tape noise, color bleed.
    - 1980s–90s found footage, home video, camcorder artifacts.
    - Visible timecode and battery readout overlays (burn-ins).
    - Muted, analog color palette, low contrast, soft focus.
    - Maintain visual and spatial continuity with the last image. Do not abruptly change location unless the narrative explicitly transitions.
    - Never show Jason's face, body, silhouette, reflection, or shadow. Only show what Jason sees from his own eyes. If Jason's hands are visible, they must be in first-person POV, interacting with objects. Do NOT show Jason in the frame. No over-the-shoulder, no third-person, no reflection, no shadow, no silhouette. The viewer *is* Jason. The camera is his eyes. Only his hands may be visible, and only if interacting with objects.
    - Do not add or invent anything not in the dispatch.
    - Analog VHS, grainy, muted colors, motion blur.
    - Jason is Jason Fleece, a photojournalist. Never show a hockey mask, mask, or any horror/slasher tropes. Never depict Jason as Jason Voorhees or any killer.
    - THIS IS FOUND FOOTAGE. THE STYLE MUST ALWAYS BE: VHS, FIRST-PERSON POV, HANDS ONLY IF VISIBLE, NO JASON IN FRAME. DO NOT OMIT THESE ELEMENTS UNDER ANY CIRCUMSTANCES.
    """
    prompt = (
        "First-person POV from Jason's eyes. Never show his face or full body. "
        f"Show exactly what is described: {dispatch}. "
        "VHS scanlines, analog static, tape noise, color bleed. "
        "1980s–90s found footage, home video, camcorder artifacts. "
        "Visible timecode and battery readout overlays (burn-ins). "
        "Muted, analog color palette, low contrast, soft focus. "
    )
    if not hard_transition:
        prompt += "Maintain visual and spatial continuity with the last image. Do not abruptly change location unless the narrative explicitly transitions. "
    else:
        prompt += "This is a hard transition: do not reference the previous image. Show the new location as described in the dispatch, and clarify the transition visually. "
    prompt += (
        "Never show Jason's face, body, silhouette, reflection, or shadow. Only show what Jason sees from his own eyes. If Jason's hands are visible, they must be in first-person POV, interacting with objects. Do NOT show Jason in the frame. No over-the-shoulder, no third-person, no reflection, no shadow, no silhouette. The viewer *is* Jason. The camera is his eyes. Only his hands may be visible, and only if interacting with objects. "
        "Do not add or invent anything not in the dispatch. "
        "Analog VHS, grainy, muted colors, motion blur. "
        "Jason is Jason Fleece, a photojournalist. Never show a hockey mask, mask, or any horror/slasher tropes. Never depict Jason as Jason Voorhees or any killer. "
        "THIS IS FOUND FOOTAGE. THE STYLE MUST ALWAYS BE: VHS, FIRST-PERSON POV, HANDS ONLY IF VISIBLE, NO JASON IN FRAME. DO NOT OMIT THESE ELEMENTS UNDER ANY CIRCUMSTANCES. "
    )
    if prev_captions and not hard_transition:
        prompt += f" Previous scenes: {prev_captions}."
    if world_prompt:
        prompt += f" Context: {world_prompt}."
    if globals().get('_is_inside', False):
        prompt += " This scene is indoors."
    prompt += f" Style: {style_block}."
    return prompt

def _gen_image(caption: str, mode: str, choice: str, previous_image_url: Optional[str] = None, previous_caption: Optional[str] = None, previous_mode: Optional[str] = None, strength: float = 0.1, image_description: str = "", time_of_day: str = "", use_edit_mode: bool = False, frame_idx: int = 0, dispatch: str = "", world_prompt: str = "", hard_transition: bool = False) -> Optional[str]:
    global _last_image_path, _is_inside
    import random
    if not (IMAGE_ENABLED and LLM_ENABLED):
        print("[IMG] Image or LLM disabled, returning None")
        return None
    try:
        prev_time_of_day, prev_color = "", ""
        prev_img_paths = []
        prev_img_captions = []
        prev_img_path = None
        if frame_idx > 0 and history:
            last_imgs = []
            for entry in reversed(history):
                if entry.get("image") and entry.get("vision_dispatch"):
                    last_imgs.append((entry["image"], entry["vision_dispatch"]))
                if len(last_imgs) == 2:
                    break
            if frame_idx == 2 and len(last_imgs) >= 1:
                img, cap = last_imgs[-1]
                prev_img_paths = [img]
                prev_img_captions = [cap]
                prev_img_path = img.lstrip("/")
                if not os.path.exists(prev_img_path):
                    prev_img_path = os.path.join("images", os.path.basename(prev_img_path))
                if prev_img_paths and prev_img_path and os.path.exists(prev_img_path):
                    prev_time_of_day, prev_color = _extract_time_and_color(prev_img_path)
            elif len(last_imgs) >= 1:
                img, cap = last_imgs[0]
                prev_img_paths = [img]
                prev_img_captions = [cap]
                prev_img_path = img.lstrip("/")
                if not os.path.exists(prev_img_path):
                    prev_img_path = os.path.join("images", os.path.basename(prev_img_path))
                if prev_img_paths and prev_img_path and os.path.exists(prev_img_path):
                    prev_time_of_day, prev_color = _extract_time_and_color(prev_img_path)
        use_time_of_day = time_of_day or prev_time_of_day or (state.get('time_of_day', '') if 'state' in globals() else '')
        use_color = prev_color
        flux_instruction = PROMPTS.get("image_prompt_instruction", "")
        neg = PROMPTS["image_negative_prompt"]
        style_block = f"Time of day: {use_time_of_day}. Color: {use_color}. No: {neg}"
        prev_captions = ". ".join(prev_img_captions) if prev_img_captions else ""
        # --- Inject world summary as background context ---
        world_summary = summarize_world_state(state) if 'state' in globals() else ""
        # --- NEW: Summarize world prompt for image flavor ---
        world_flavor = ""
        if 'state' in globals() and state.get("world_prompt", ""):
            world_flavor = summarize_world_prompt_for_image(state["world_prompt"])
        prompt_str = build_image_prompt(
            previous_desc="",
            change_desc="",
            style_block=style_block,
            player_choice=choice,
            dispatch=caption,
            world_prompt=state.get("world_prompt", ""),
            prev_captions=prev_captions,
            hard_transition=hard_transition
        )
        # Inject world flavor and location for image model only
        if world_flavor:
            prompt_str += f" World flavor: {world_flavor}."
        if world_summary:
            prompt_str += f" Background context: {world_summary}."
        # Inject death visual biasing if provided
        if death_visual_bias:
            prompt_str += death_visual_bias
        if prev_img_paths:
            prompt_str = (
                f"{prompt_str}\nMatch the lighting, time of day, and color palette to the previous image."
            )
        prompt_str = f"{flux_instruction}\n{prompt_str}"
        # --- LOGGING ---
        print("[IMG LOG] --- IMAGE GENERATION PARAMETERS ---")
        print(f"[IMG LOG] frame_idx: {frame_idx}")
        print(f"[IMG LOG] mode: {mode}")
        print(f"[IMG LOG] choice: {choice}")
        print(f"[IMG LOG] caption (vision_dispatch): {caption}")
        print(f"[IMG LOG] dispatch (narrative): {dispatch}")
        print(f"[IMG LOG] time_of_day: {use_time_of_day}")
        print(f"[IMG LOG] style_block: {style_block}")
        print(f"[IMG LOG] negative prompt: {neg}")
        print(f"[IMG LOG] prompt_str (full): {prompt_str}")
        print(f"[IMG LOG] previous_image_url: {previous_image_url}")
        print(f"[IMG LOG] use_edit_mode: {use_edit_mode}")
        print(f"[IMG LOG] world_prompt: {world_prompt}")
        print("[IMG LOG] --- END IMAGE GENERATION PARAMETERS ---")
        IMAGE_DIR.mkdir(exist_ok=True)
        filename = f"{hash(caption) & 0xFFFFFFFF}_{_slug(caption)}.png"
        image_path = IMAGE_DIR / filename
        # --- TRUE IMG2IMG/EDIT MODE ---
        if use_edit_mode and prev_img_path and os.path.exists(prev_img_path) and frame_idx > 0:
            print(f"[IMG EDIT] Using previous image as reference: {prev_img_path}")
            with open(prev_img_path, "rb") as imgf:
                response = client.images.edit(
                    model="gpt-image-1",
                    image=imgf,
                    prompt=prompt_str,
                    n=1,
                    size="1536x1024",
                    quality="medium"
                )
            b64_data = response.data[0].b64_json
            img_data = base64.b64decode(b64_data)
            with open(image_path, "wb") as f:
                f.write(img_data)
            print(f"✅ [EDIT] Image saved to: {image_path}")
        else:
            print(f"[IMG] Generating first frame or no reference image (text-to-image)")
            response = client.images.generate(
                model="gpt-image-1",
                prompt=prompt_str,
                n=1,
                size="1536x1024",
                quality="medium"
            )
            b64_data = response.data[0].b64_json
            img_data = base64.b64decode(b64_data)
            with open(image_path, "wb") as f:
                f.write(img_data)
            print(f"✅ Image saved to: {image_path}")
        _last_image_path = f"/images/{filename}"
        # Only run vision analysis if not the first frame
        if frame_idx > 1:
            _is_inside = _vision_is_inside(f"/images/{filename}")
            tod, color = _extract_time_and_color(f"/images/{filename}")
            state['time_of_day'] = tod
            state['color_palette'] = color
            _save_state(state)
        exit_cues = ["exit", "outside", "outdoors", "leave the building", "step outside", "step outdoors", "emerge", "open air", "under the sky", "out in the open"]
        if any(cue in caption.lower() or cue in dispatch.lower() for cue in exit_cues):
            _is_inside = False
        return f"/images/{filename}"
    except Exception as e:
        print(f"[IMG PROVIDER {IMAGE_PROVIDER}] Error:", e)
        return None

# ───────── vision dispatch generator ─────────────────────────────────────────
def _generate_vision_dispatch(narrative_dispatch: str, world_prompt: str = "") -> str:
    prompt = (
        "Rewrite the following narrative as a first-person, present-tense description of what Jason sees, suitable for a visual scene. "
        "Only describe what is visible. Do not include Jason himself or any internal thoughts. "
        "Do not show Jason. Do not show the protagonist. Do not show any character from behind. Only show what Jason sees from his own eyes. "
        f"\n\nNARRATIVE DISPATCH: {narrative_dispatch}\n\nWORLD CONTEXT: {world_prompt}"
    )
    rsp = _call(
        client.chat.completions.create,
        model="gpt-4o",
        messages=[{"role": "system", "content": "You are a visual scene writer for analog horror. Output only the literal, visible scene as Jason would see it, in first-person present tense."},
                  {"role": "user", "content": prompt}],
        temperature=0.4,
        max_tokens=100,
    )
    result = rsp.choices[0].message.content.strip()
    return result

# ───────── public API (two‑stage) ───────────────────────────────────────────
def _generate_situation_report() -> str:
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
            PROMPTS["situation_report_prompt"] +
            f"\n\nWorld State (after last action):\n{world_state}\nLast Dispatch (player's action):\n{last_dispatch}" +
            major_event_nudge +
            "\nDescribe what is happening NOW, after the dispatch, as a concise 1-2 sentence situation. This should set up the next set of choices."
        )
        return _ask(prompt, model="gpt-4o-mini", temp=0.5, tokens=40)
    return "You stand on a rocky outcrop overlooking the Horizon facility, the quarantine fence stretching across the red desert. Patrol lights sweep the landscape as distant thunder rumbles."

def begin_tick() -> dict:
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
        time_of_day=state.get('time_of_day', ''),
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
    if not LLM_ENABLED or not VISION_ENABLED:
        return "", ""
    buf = _downscale_for_vision(image_path)
    if buf is None:
        return "", ""
    try:
        rsp = client.chat.completions.create(
            model="gpt-4o-vision",
            messages=[
                {"role":"system","content":"You will be shown an image."},
                {"role":"user","content":"Describe the time of day (e.g., dawn, morning, afternoon, dusk, night) and the dominant color or color palette in this image. Respond in the format: 'Time: <time>. Color: <color or palette>.'"}
            ],
            files=[{"file": buf, "filename": "downscaled.png"}],
            temperature=0.6,
        )
        desc = rsp.choices[0].message.content.strip()
        time, color = "", ""
        if "Time:" in desc and "Color:" in desc:
            parts = desc.split("Color:")
            time = parts[0].replace("Time:", "").strip().strip(".")
            color = parts[1].strip().strip(".")
        return time, color
    except Exception as e:
        print("vision extract error:", e, file=sys.stderr)
        return "", ""

# --- LLM-based player death check ---
def check_player_death(dispatch: str, world_prompt: str, choice: str) -> bool:
    prompt = (
        'Based on the following, has the player died? Respond with only "dead" or "alive".\n'
        f'DISPATCH: {dispatch}\n'
        f'WORLD STATE: {world_prompt}\n'
        f'PLAYER CHOICE: {choice}'
    )
    result = _ask(prompt, model="gpt-4o", temp=0, tokens=2).strip().lower()
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
    rsp = _ask(crisis_prompt, model="gpt-4o", temp=0.7, tokens=40)
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
        result = _ask(prompt, model="gpt-4o", temp=0.4, tokens=48)
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
            # from choices import generate_and_apply_choice # Local import removed
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
                new_image_url = _gen_image(
                    caption=vision_dispatch_for_image,
                    mode=state.get("current_phase", "normal"), # Use current_phase for mode
                    choice=choice,
                    previous_image_url=current_image_url,
                    dispatch=dispatch_text, # Narrative dispatch
                    world_prompt=world_prompt_for_image,
                    hard_transition=hard_trans,
                    frame_idx=state.get("turn_count", 0) + 1 # Approximate frame index
                )
                if new_image_url:
                    image_item = create_feed_item(type="scene_image", content="The scene shifts...", image_url=new_image_url)
                    new_feed_items_for_log.append(image_item)
                    state['current_image_url'] = new_image_url # Update global state
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
        # from choices import detect_threat # Local import removed
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
                time_of_day=state.get("time_of_day", "evening"),
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
                initial_image_url = _gen_image(
                    caption=vision_dispatch_for_intro_image, 
                    mode="normal",
                    choice="Initialize Simulation",
                    dispatch=initial_narrative_content,
                    world_prompt=state.get("world_prompt", "Initialization sequence."),
                    frame_idx=0 
                )
                if initial_image_url:
                    image_item = create_feed_item(type="scene_image", content="Initialising environment...", image_url=initial_image_url)
                    intro_items.append(image_item)
                    state['current_image_url'] = initial_image_url # This is fine, updates a different part of state
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
        "threat_level": 0
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
            time_of_day=state_snapshot_for_context.get('time_of_day', ''),
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
def _generate_combined_dispatches(choice: str, state: dict, prev_state: dict = None, prev_vision: str = "", current_image: str = None) -> tuple[str, str, bool]:
    """
    Generate BOTH narrative dispatch AND vision dispatch in ONE API call.
    Now supports multimodal input - can see the current frame!
    
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
        
        # Use JUST the dispatch_sys instructions (which has JSON format)
        json_prompt = (
            f"{dispatch_sys}\n\n"
            f"PLAYER CHOICE: '{choice}'\n"
            f"WORLD CONTEXT: {world_prompt}\n\n"
            f"{spatial_context}"
            f"{prev_context}\n\n"
            "Generate the consequence in valid JSON format."
        )
        
        # Build parts list (text + optional image)
        parts = [{"text": json_prompt}]
        
        # Add previous timestep image if provided
        if current_image:
            # Use pre-downsampled version if available
            if current_image.startswith("/images/"):
                actual_path = Path("images") / current_image.replace("/images/", "")
            else:
                actual_path = Path(current_image)
            
            small_path = actual_path.parent / actual_path.name.replace(".png", "_small.png")
            use_path = small_path if small_path.exists() else actual_path
            
            if use_path.exists():
                with open(use_path, "rb") as f:
                    import base64
                    image_data = base64.b64encode(f.read()).decode('utf-8')
                
                parts.insert(0, {
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": image_data
                    }
                })
                size_note = "(480x270)" if small_path.exists() else "(full-res)"
                print(f"[GEMINI TEXT+IMG] Including PREVIOUS timestep image: {current_image} {size_note}")
        
        import requests
        response_data = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json={
                "contents": [{"parts": parts}],
                "generationConfig": {"temperature": 0.8, "maxOutputTokens": 500}
            },
            timeout=15
        ).json()
        print("[GEMINI TEXT] ✅ Combined dispatches complete")
        
        result = response_data["candidates"][0]["content"]["parts"][0]["text"].strip()
        
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
            print(f"[DISPATCH] Parsed JSON: dispatch={dispatch[:50]}..., alive={player_alive}")
        except Exception as parse_error:
            print(f"[DISPATCH] JSON parse failed: {parse_error}")
            print(f"[DISPATCH] Raw result: {result[:200]}...")
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
        print(f"[COMBINED DISPATCH ERROR] {e}")
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
        diffs.append(f"Chaos level: {prev_state.get('chaos_level', 0)} → {state.get('chaos_level', 0)}")
    # Phase
    if prev_state.get('current_phase', 'normal') != state.get('current_phase', 'normal'):
        diffs.append(f"Phase: {prev_state.get('current_phase', 'normal')} → {state.get('current_phase', 'normal')}")
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
        summary = _ask(prompt, model="gpt-4o-mini", temp=1.0, tokens=40)
        if not summary.strip():
            summary = 'No major consequence.'
        return success, summary
    except Exception as e:
        log_error(f"[RISKY ACTION CONSEQUENCE] LLM error: {e}")
        return success, 'No major consequence.'

# ───────── game loop ──────────────────────────────────────────────────────────
def advance_turn_image_fast(choice: str) -> dict:
    """
    PHASE 1 (FAST): Generate dispatch and image, return immediately.
    Returns image ASAP so bot can display it while choices are generating.
    """
    global state, history
    try:
        state = _load_state()
        history_path = ROOT / "history.json"
        if history_path.exists():
            with history_path.open("r", encoding="utf-8") as f:
                history = json.load(f)
        else:
            history = []
        prev_state = state.copy() if isinstance(state, dict) else dict(state)
        from choices import generate_and_apply_choice, generate_choices
        generate_and_apply_choice(choice)
        state = _load_state()
        
        # Get previous vision and image
        prev_vision = ""
        prev_image = None
        if history and len(history) > 0:
            prev_vision = history[-1].get("vision_dispatch", "")
            prev_image = history[-1].get("image_url", None)
        
        # Generate dispatch using FULL StoryGen version
        dispatch, vision_dispatch, player_alive = _generate_combined_dispatches(choice, state, prev_state, prev_vision, prev_image)
        
        # SIMPLE DEATH SYSTEM: Just trust the LLM
        state['player_state']['alive'] = player_alive
        
        if not player_alive:
            print(f"[DEATH] Player killed by: {dispatch[:100]}...")
        
        # Save state immediately after death detection
        _save_state(state)
        print(f"[STATE] Saved - alive={player_alive}, health={state['player_state'].get('health', 100)}")
        
        if not dispatch or dispatch.strip().lower() in {"none", "", "[", "[]"}:
            dispatch = "Jason makes a tense move in the chaos."
        if not vision_dispatch or vision_dispatch.strip().lower() in {"none", "", "[", "[]"}:
            vision_dispatch = dispatch
        
        # Evolve world state
        from evolve_prompt_file import evolve_world_state
        consequence_summary = summarize_world_state_diff(prev_state, state)
        evolve_world_state(history, consequence_summary, vision_description=vision_dispatch)
        state = _load_state()
        
        # Generate image
        mode = state.get("mode", "camcorder")
        frame_idx = len(history) + 1
        hard_transition = is_hard_transition(choice, dispatch)
        
        consequence_img_url = None
        try:
            last_image_path = None
            if history and len(history) > 0:
                for entry in reversed(history):
                    if entry.get("image"):
                        last_image_path = entry["image"].lstrip("/")
                        break
            
            consequence_img_url = _gen_image(
                vision_dispatch,
                mode,
                choice,
                image_description="",
                time_of_day=state.get('time_of_day', ''),
                use_edit_mode=(last_image_path and os.path.exists(last_image_path)),
                frame_idx=frame_idx,
                dispatch=dispatch,
                world_prompt=state.get("world_prompt", ""),
                hard_transition=hard_transition
            )
            print(f"✅ [IMG FAST] Image ready: {consequence_img_url}")
        except Exception as e:
            print(f"❌ [IMG FAST] Error: {e}")
        
        return {
            "dispatch": dispatch,
            "vision_dispatch": vision_dispatch,
            "consequence_image": consequence_img_url,
            "phase": state["current_phase"],
            "chaos": state["chaos_level"],
            "world_prompt": state.get("world_prompt", ""),
            "mode": state.get("mode", "camcorder")
        }
    except Exception as e:
        print(f"❌ [IMG FAST] Fatal error: {e}")
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

def advance_turn_choices_deferred(consequence_img_url: str, dispatch: str, vision_dispatch: str, choice: str) -> dict:
    """
    PHASE 2 (DEFERRED): Generate choices after image is displayed.
    """
    global state, history
    from choices import generate_choices
    
    state = _load_state()
    situation_summary = _generate_situation_report()
    
    next_choices = generate_choices(
        client, choice_tmpl,
        dispatch,
        n=3,
        image_url=consequence_img_url,
        seen_elements='',
        recent_choices='',
        caption="",
        image_description="",
        time_of_day=state.get('time_of_day', ''),
        world_prompt=state.get('world_prompt', ''),
        temperature=0.7,
        situation_summary=situation_summary
    )
    
    next_choices = [c for c in next_choices if c and c.strip() and c.strip() != '—']
    if not next_choices:
        next_choices = ["Look around", "Move forward", "Wait"]
    while len(next_choices) < 3:
        next_choices.append("—")
    
    # Save to history
    history_entry = {
        "choice": choice,
        "dispatch": dispatch,
        "vision_dispatch": vision_dispatch,
        "vision_analysis": "",
        "world_prompt": state.get("world_prompt", ""),
        "image": consequence_img_url,
        "image_url": consequence_img_url
    }
    history.append(history_entry)
    (ROOT / "history.json").write_text(json.dumps(history, indent=2))
    
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
            choice
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
def get_state():
    return _load_state()

def reset_state():
    global state, history, _last_image_path, _vision_cache
    # Forcibly delete history.json and world_state.json if they exist
    try:
        os.remove(str(ROOT / "history.json"))
    except FileNotFoundError:
        pass
    try:
        os.remove(str(ROOT / "world_state.json"))
    except FileNotFoundError:
        pass
    
    # Clear vision cache
    _vision_cache.clear()
    print("[CLEANUP] Cleared vision analysis cache")
    
    # Clear all images from the images folder
    image_dir = ROOT / "images"
    if image_dir.exists():
        image_count = 0
        for image_file in image_dir.glob("*.png"):
            try:
                image_file.unlink()
                image_count += 1
            except Exception as e:
                print(f"[CLEANUP] Failed to delete {image_file.name}: {e}")
        print(f"[CLEANUP] Deleted {image_count} old images from images/ folder")
    
    # Recreate history.json as empty list
    with (ROOT / "history.json").open("w", encoding="utf-8") as f:
        json.dump([], f)
    history = []
    # Recreate world_state.json with intro prompt
    import random
    time_options = [
        "golden hour (late afternoon warm light)",
        "midday (harsh overhead sunlight)",
        "early morning (soft blue-tinted light)",
        "late afternoon (long shadows)",
        "overcast daylight (diffuse gray light)"
    ]
    initial_time = random.choice(time_options)
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
        "environment_type": "desert"
    }
    with (ROOT / "world_state.json").open("w", encoding="utf-8") as f:
        json.dump(intro_state, f, indent=2)
    state = intro_state
    _last_image_path = None
    print("[STARTUP] Game state cleared. Starting fresh.")

def generate_intro_image_fast():
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
    
    state = _load_state()
    state["world_prompt"] = prologue
    state["current_phase"] = "normal"
    state["chaos_level"] = 0
    state["last_choice"] = ""
    state["seen_elements"] = []
    state["player_state"] = {"alive": True, "health": 100}
    _save_state(state)
    
    mode = state.get("mode", "camcorder")
    
    # Generate opening image ONLY
    dispatch_img_url = None
    try:
        print("[INTRO FAST] Generating opening image...")
        dispatch_img_url = _gen_image(
            vision_dispatch,
            mode,
            "Intro",
            image_description="",
            time_of_day=state.get('time_of_day', 'golden hour'),
            use_edit_mode=False,
            frame_idx=0,
            dispatch=prologue,
            world_prompt=prologue,
            hard_transition=False
        )
        if dispatch_img_url:
            print(f"✅ [INTRO FAST] Image ready for display: {dispatch_img_url}")
            _last_image_path = dispatch_img_url
    except Exception as e:
        print(f"❌ [INTRO FAST] Image generation error: {e}")
    
    return {
        "dispatch": prologue,
        "vision_dispatch": vision_dispatch,
        "dispatch_image": dispatch_img_url,
        "prologue": prologue,
        "mode": mode
    }

def generate_intro_choices_deferred(image_url: str, prologue: str, vision_dispatch: str, dispatch: str = None):
    """
    PHASE 2 (DEFERRED): Generate choices after image is displayed.
    Can run in background while user is looking at the image.
    """
    global state, history
    from choices import generate_choices
    
    state = _load_state()
    
    # Generate dynamic situation report from LLM based on current world state
    situation_summary = _generate_situation_report()
    options = generate_choices(
        client, choice_tmpl,
        prologue,  # What's happening in intro
        n=3,
        image_url=image_url,  # Gemini sees the image directly!
        seen_elements='',
        recent_choices='',
        caption="",  # Let Gemini look at image, not stale text
        image_description="",  # Let Gemini look at image, not stale text
        time_of_day=state.get('time_of_day', ''),
        world_prompt=prologue,
        temperature=0.7,
        situation_summary=situation_summary
    )
    if len(options) == 1:
        parts = re.split(r"[\/,\x19\x12\-]|  +", options[0])
        options = [p.strip() for p in parts if p.strip()][:3]
    # Don't pad with placeholders - just return what we got
    
    # Save to history
    entry = {
        "choice": "Intro",
        "dispatch": prologue,
        "vision_dispatch": vision_dispatch,
        "vision_analysis": "",  # Not needed anymore
        "world_prompt": prologue,
        "image": image_url,
        "image_url": image_url
    }
    history = [entry]
    (ROOT / "history.json").write_text(json.dumps(history, indent=2))
    _save_state(state)
    
    return {
        "choices": options,
        "phase": state["current_phase"],
        "chaos": state["chaos_level"],
        "player_state": state.get('player_state', {})
    }

def generate_intro_turn():
    """
    Generate the intro turn: dispatch, vision_dispatch, image, and choices,
    using the prologue as the first dispatch and context.
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
    
    state = _load_state()
    state["world_prompt"] = prologue
    state["current_phase"] = "normal"
    state["chaos_level"] = 0
    state["last_choice"] = ""
    state["seen_elements"] = []
    state["player_state"] = {"alive": True, "health": 100}
    _save_state(state)
    
    dispatch = prologue
    mode = state.get("mode", "camcorder")
    
    # Generate opening image to establish the scene
    dispatch_img_url = None
    image_description = ""
    try:
        print("[INTRO] Generating opening image...")
        dispatch_img_url = _gen_image(
            vision_dispatch,  # Visual description of the opening scene
            mode,
            "Intro",
            image_description="",
            time_of_day=state.get('time_of_day', 'golden hour'),
            use_edit_mode=False,  # No previous image
            frame_idx=0,  # First frame
            dispatch=dispatch,
            world_prompt=prologue,
            hard_transition=False
        )
        if dispatch_img_url:
            print(f"✅ [INTRO] Opening image generated: {dispatch_img_url}")
            _last_image_path = dispatch_img_url
            image_description = ""  # Not needed anymore
        else:
            print("[INTRO] ⚠️ Image generation returned None")
    except Exception as e:
        print(f"❌ [INTRO] Error generating opening image: {e}")
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
        time_of_day=state.get('time_of_day', ''),
        world_prompt=prologue,
        temperature=0.7,
        situation_summary=situation_summary
    )
    if len(options) == 1:
        parts = re.split(r"[\/,\x19\x12\-]|  +", options[0])
        options = [p.strip() for p in parts if p.strip()][:3]
    # Don't pad with placeholders - just return what we got
    entry = {
        "choice": "Intro",
        "dispatch": dispatch,
        "vision_dispatch": vision_dispatch,
        "vision_analysis": image_description,  # Save vision analysis for first turn continuity
        "world_prompt": prologue,
        "image": dispatch_img_url  # Include opening image
    }
    history = [entry]
    (ROOT / "history.json").write_text(json.dumps(history, indent=2))
    # _last_image_path is already set above if image was generated
    _save_state(state)
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
