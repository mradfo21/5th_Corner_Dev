"""
engine.py – core story simulator
2025‑05‑14 patch‑vision (import fix)

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
from datetime import datetime
from pathlib import Path
from typing import Optional
import logging

import openai
from openai import OpenAIError
from flask import Flask, send_from_directory, request, jsonify
from PIL import Image
import io

from choices import generate_and_apply_choice, generate_choices, categorize_choice, detect_threat
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
CONFIG = json.load((ROOT/"config.json").open(encoding="utf-8"))
OPENAI_API_KEY = CONFIG["OPENAI_API_KEY"]
PROMPTS = json.load((ROOT/"prompts"/"simulation_prompts.json").open(encoding="utf-8"))

STATE_PATH = ROOT/"world_state.json"
IMAGE_DIR = ROOT / "images"

IMAGE_ENABLED       = True
WORLD_IMAGE_ENABLED = True
HD_MODE             = True  # HD mode for high-quality images (slower)

DEFAULT_BASE = "https://api.openai.com/v1"
API_BASE     = (os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE).strip() or DEFAULT_BASE

client      = _client(OPENAI_API_KEY, API_BASE)
LLM_ENABLED = True

VISION_ENABLED = CONFIG.get("USE_VISION", False)

IMAGE_PROVIDER = CONFIG.get("IMAGE_PROVIDER", "openai").lower()

# Track the last dispatch image path for vision continuity
_last_image_path: Optional[str] = None

# Add a global counter for choices since last reset
_choices_since_edit_reset = 0

# Interior/exterior tracking removed - was unused

print("ENGINE importing... (vision patch, import fixed)", flush=True)

# ───────── prompt fragments ──────────────────────────────────────────────────
choice_tmpl     = PROMPTS["player_choice_generation_instructions"]
dispatch_sys    = PROMPTS["action_consequence_instructions"]
neg_prompt      = PROMPTS["image_negative_prompt"]
narrative_tmpl  = PROMPTS["field_notes_format"]

# ───────── world‑state helpers ───────────────────────────────────────────────
def _load_state() -> dict:
    if STATE_PATH.exists():
        st = json.load(STATE_PATH.open())
        # Ensure player_state exists
        if 'player_state' not in st:
            st['player_state'] = {'alive': True, 'health': 100}
        elif 'health' not in st.get('player_state', {}):
            st['player_state']['health'] = 100
        return st
    
    # Random time of day for variety
    import random
    time_options = [
        "golden hour (late afternoon warm light)",
        "midday (harsh overhead sunlight)",
        "early morning (soft blue-tinted light)",
        "late afternoon (long shadows)",
        "overcast daylight (diffuse gray light)"
    ]
    initial_time = random.choice(time_options)
    
    return {
        "world_prompt": PROMPTS["world_initial_state"],
        "current_phase": "normal",
        "chaos_level": 0,
        "last_choice": "",
        "last_saved": datetime.utcnow().isoformat(),
        "seen_elements": [],
        "player_state": {"alive": True, "health": 100},
        "time_of_day": initial_time
    }

state = _load_state()
history_path = ROOT / "history.json"
if history_path.exists():
    with history_path.open("r", encoding="utf-8") as f:
        history = json.load(f)
else:
    history = []

def _save_state(st: dict):
    st["last_saved"] = datetime.utcnow().isoformat()
    STATE_PATH.write_text(json.dumps(st, indent=2))

def summarize_world_state(state: dict) -> str:
    """
    Return a single, actionable, dynamic sentence summarizing the most important, immediate world state or threat.
    Prioritize: player danger, pursuit, injury, chaos, visible threats, or urgent objectives.
    """
    if not state.get('player_state', {}).get('alive', True):
        return "Jason is gravely wounded and in danger of dying."
    if state.get('chaos_level', 0) > 5:
        return "Guards are on high alert and searching for Jason."
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

def _ask(prompt: str, model="gemini", temp=0.8, tokens=90, image_path: str = None) -> str:
    """Use Gemini Flash for all text generation - much faster than GPT-4o
    
    Args:
        prompt: Text prompt
        model: Model to use (default: "gemini")
        temp: Temperature (0-1)
        tokens: Max output tokens
        image_path: Optional path to image for multimodal input (e.g. "/images/file.png")
    """
    if not LLM_ENABLED:
        return random.choice([
            "System communications remain static; awaiting new data.",
            "Narrative paused until resources are replenished.",
            "The world holds its breath for new directives."
        ])
    
    # Use Gemini Flash for speed (supports multimodal!)
    import requests
    import base64
    from pathlib import Path
    gemini_api_key = CONFIG.get("GEMINI_API_KEY", "")
    
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
                size_note = "(480x270)" if small_path.exists() else "(full-res)"
                print(f"[GEMINI TEXT+IMG] Including image: {image_path} {size_note}")
        
        response_data = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
            headers={"x-goog-api-key": gemini_api_key, "Content-Type": "application/json"},
            json={
                "contents": [{"parts": parts}],
                "generationConfig": {"temperature": temp, "maxOutputTokens": tokens}
            },
            timeout=15
        ).json()
        
        result = response_data["candidates"][0]["content"]["parts"][0]["text"].strip()
        print(f"[GEMINI TEXT] ✅ _ask() complete")
        return result or ""
    except Exception as e:
        log_error(f"[ASK GEMINI] {e}")
        return ""

# ───────── vision description helper ────────────────────────────────────────
def _downscale_for_vision(image_path: str, size=(640, 426)) -> io.BytesIO:
    # Handle various path formats: /images/file.png, images/file.png, file.png
    from pathlib import Path
    if image_path.startswith("/images/"):
        full = IMAGE_DIR / Path(image_path).name  # Extract just filename
    elif image_path.startswith("images/"):
        full = IMAGE_DIR / Path(image_path).name  # Extract just filename
    else:
        full = IMAGE_DIR / image_path
    
    buf = io.BytesIO()
    if not full.exists():
        print(f"[VISION] Image not found at: {full}")
        return None
    try:
        img = Image.open(full)
        img = img.convert("RGB")
        img = img.resize(size, Image.LANCZOS)
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception as e:
        print(f"[VISION] Downscale error: {e}", file=sys.stderr)
        return None

# Global vision cache to avoid re-analyzing the same image
_vision_cache = {}

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
        full_path = image_path.lstrip("/")
        if not os.path.exists(full_path):
            full_path = os.path.join("images", os.path.basename(image_path))
        
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
            print(f"[VISION] Using pre-downsampled image (480x270)")
        
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
                    {"text": vision_prompt},
                    {
                        "inlineData": {
                            "mimeType": mime_type,
                            "data": image_b64
                        }
                    }
                ]
            }],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 800
            }
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
        
        print(f"[VISION] ✅ Analysis complete: {len(description)} chars, time={time_of_day}, color={color_palette[:30]}")
        return result_dict
    
    except requests.exceptions.HTTPError as e:
        print(f"[VISION ERROR] ❌ Gemini API HTTP error: {e}")
        if e.response is not None:
            print(f"[VISION ERROR] Response: {e.response.text}")
        return {"description": "", "time_of_day": "", "color_palette": ""}
    except Exception as e:
        print(f"[VISION ERROR] ❌ Failed to analyze image: {e}")
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
        f"[{tone.upper()} TONE] {PROMPTS['situation_summary_instructions']}"
    )
    return _ask(prompt, tokens=60)

# ───────── dispatch helpers ────────────────────────────────────────────────
def summarize_world_prompt_for_image(world_prompt: str) -> str:
    """Summarize the world prompt to 1-2 sentences for image generation."""
    prompt = (
        "Summarize the following world context in 1-2 vivid, scene-specific sentences, focusing only on details relevant to the current visual environment. Omit backstory and generalities.\n\nWORLD PROMPT: " + world_prompt
    )
    return _ask(prompt, model="gemini", temp=0.9, tokens=48)

def _generate_dispatch(choice: str, state: dict, prev_state: dict = None) -> dict:
    """Generate dispatch with death detection. Returns dict with 'dispatch' and 'player_alive' keys."""
    try:
        # Get previous vision analysis for spatial consistency
        prev_vision = ""
        if history and len(history) > 0:
            last_entry = history[-1]
            if last_entry.get("vision_analysis"):
                prev_vision = last_entry["vision_analysis"][:300]
        
        spatial_context = ""
        if prev_vision:
            spatial_context = f"\n\nCURRENT VISUAL SCENE (MUST STAY CONSISTENT): {prev_vision}\nDo NOT change locations unless the choice explicitly moves through a door, entrance, or exit. Stay in the same environment."
        
        prompt = (
            f"PLAYER CHOICE: '{choice}'\n"
            f"WORLD CONTEXT: {state['world_prompt']}\n"
            f"PREVIOUS: {prev_state['world_prompt'] if prev_state else ''}"
            f"{spatial_context}\n\n"
            "Describe what Jason does and what immediately happens as a result."
        )
        rsp = _call(
            client.chat.completions.create,
            model="gpt-4o",
            messages=[{"role":"system","content":dispatch_sys},
                      {"role":"user","content":prompt}],
            temperature=1.0,
            max_tokens=250,  # Increased to prevent truncation
        )
        result = rsp.choices[0].message.content.strip()
        
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

# ───────── imaging helpers ─────────────────────────────────────────────────
def _slug(s: str) -> str:
    return "".join(c for c in s.lower().replace(" ","_") if c.isalnum() or c=="_")[:48]

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

# _vision_is_inside removed - was expensive and never used

def is_hard_transition(choice: str, dispatch: str) -> bool:
    keywords = [
        'enter', 'inside', 'run into', 'escape', 'wake up', 'thrown', 'move to', 'new location', 'different room', 'facility', 'red biome', 'emerge elsewhere', 'relocated', 'teleport', 'suddenly', 'abruptly', 'scene shifts', 'transition', 'burst into', 'step inside', 'step outdoors', 'exit', 'leave', 'outdoors', 'open air', 'out in the open', 'emerge', 'open door', 'cross into', 'cross over', 'arrive at', 'find yourself in', 'appear in', 'shift to', 'cut to', 'fade to', 'wake up in', 'dragged to', 'carried to', 'transported to', 'relocate', 'relocated', 'new area', 'new scene', 'different area', 'different scene',
        'retreat', 'flee', 'run away', 'fall back', 'withdraw', 'bolt', 'dash away', 'make a getaway', 'escape to', 'rush out', 'rush away', 'scramble away', 'sprint away', 'break for cover', 'retreat to', 'flee to', 'run for', 'run from', 'run toward', 'run towards', 'run back', 'run off', 'run out', 'run inside', 'run outside', 'run indoors', 'run outdoors', 'run into', 'run through', 'run across', 'run down', 'run up', 'run along', 'run past', 'run behind', 'run ahead', 'run forward', 'run backward', 'run left', 'run right', 'run behind cover', 'run for safety', 'run for shelter', 'run for the exit', 'run for the door', 'run for the truck', 'run for the perimeter', 'run for the fence', 'run for the hills', 'run for the mesa', 'run for the desert', 'run for the facility', 'run for the building', 'run for the shadows', 'run for the light', 'run for the darkness', 'run for the open', 'run for the open air', 'run for the open ground', 'run for the open desert', 'run for the open mesa', 'run for the open facility', 'run for the open building', 'run for the open shadows', 'run for the open light', 'run for the open darkness', 'run for the open perimeter', 'run for the open fence', 'run for the open hills', 'run for the open truck', 'run for the open door', 'run for the open exit', 'run for the open shelter', 'run for the open safety', 'run for the open cover', 'run for the open ground', 'run for the open area', 'run for the open scene', 'run for the open location', 'run for the open place', 'run for the open spot', 'run for the open zone', 'run for the open region', 'run for the open sector', 'run for the open quadrant', 'run for the open sector', 'run for the open quadrant', 'run for the open region', 'run for the open zone', 'run for the open place', 'run for the open spot', 'run for the open area', 'run for the open ground', 'run for the open cover', 'run for the open safety', 'run for the open shelter', 'run for the open exit', 'run for the open door', 'run for the open truck', 'run for the open hills', 'run for the open fence', 'run for the open perimeter', 'run for the open mesa', 'run for the open desert', 'run for the open facility', 'run for the open building', 'run for the open shadows', 'run for the open light', 'run for the open darkness', 'run for the open perimeter', 'run for the open fence', 'run for the open hills', 'run for the open truck', 'run for the open door', 'run for the open exit', 'run for the open shelter', 'run for the open safety', 'run for the open cover', 'run for the open ground', 'run for the open area', 'run for the open scene', 'run for the open location', 'run for the open place', 'run for the open spot', 'run for the open zone', 'run for the open region', 'run for the open sector', 'run for the open quadrant'
    ]
    text = f"{choice} {dispatch}".lower()
    return any(k in text for k in keywords)

def build_image_prompt(player_choice: str = "", dispatch: str = "", prev_vision_analysis: str = "", hard_transition: bool = False) -> str:
    """
    Build image prompt with continuity from previous vision analysis.
    Explicitly includes player choice so the image reflects the action taken.
    """
    # Start with the player's choice and what happened
    prompt = f"Action taken: {player_choice}. Result: {dispatch}"
    
    # Add previous vision analysis for visual continuity (unless hard transition)
    if prev_vision_analysis and not hard_transition:
        prompt = f"{prompt} Continue from previous scene: {prev_vision_analysis[:200]}"
    
    return prompt

def _gen_image(caption: str, mode: str, choice: str, previous_image_url: Optional[str] = None, previous_caption: Optional[str] = None, previous_mode: Optional[str] = None, strength: float = 0.25, image_description: str = "", time_of_day: str = "", use_edit_mode: bool = False, frame_idx: int = 0, dispatch: str = "", world_prompt: str = "", hard_transition: bool = False) -> Optional[str]:
    global _last_image_path
    import random
    if not (IMAGE_ENABLED and LLM_ENABLED):
        print("[IMG] Image or LLM disabled, returning None")
        return None
    try:
        prev_time_of_day, prev_color = "", ""
        prev_img_paths = []
        prev_img_captions = []
        prev_vision_analysis = ""  # NEW: Vision AI's analysis of what's actually in the last frame
        prev_img_path = None
        prev_img_paths_list = []  # List of recent image paths for multi-img2img
        
        if frame_idx > 0 and history:
            last_imgs = []
            # Determine number of reference images based on action type
            # ACTIONS (movement, interaction) = 1 image (show dramatic change)
            # STATIONARY (photograph, observe) = 2 images (maintain continuity)
            action_keywords = ["move", "advance", "run", "sprint", "climb", "vault", "enter", "approach", 
                             "walk", "crawl", "dash", "jump", "dive", "charge", "rush", "slide",
                             "kick", "grab", "throw", "punch", "strike", "smash", "pry", "wrench"]
            is_action = any(kw in choice.lower() for kw in action_keywords)
            num_images_to_collect = 1 if is_action else 2
            
            if is_action:
                print(f"[IMG2IMG] Action detected - using 1 reference image for dramatic change")
            else:
                print(f"[IMG2IMG] Stationary action - using 2 reference images for continuity")
            
            for entry in reversed(history):
                if entry.get("image") and entry.get("vision_dispatch"):
                    last_imgs.append((
                        entry["image"],
                        entry["vision_dispatch"],
                        entry.get("vision_analysis", "")  # Pull vision analysis
                    ))
                if len(last_imgs) == num_images_to_collect:
                    break
            
            if len(last_imgs) >= 1:
                # Get most recent image for time/color extraction
                img, cap, vis_analysis = last_imgs[0]
                prev_vision_analysis = vis_analysis
                prev_img_path = img.lstrip("/")
                if not os.path.exists(prev_img_path):
                    prev_img_path = os.path.join("images", os.path.basename(prev_img_path))
                
                # Collect all recent image paths for multi-reference img2img
                for idx, (img, cap, _) in enumerate(last_imgs):
                    img_path = img.lstrip("/")
                    if not os.path.exists(img_path):
                        img_path = os.path.join("images", os.path.basename(img_path))
                    if os.path.exists(img_path):
                        # Verify the _small version exists too
                        small_path = img_path.replace(".png", "_small.png")
                        if os.path.exists(small_path):
                            print(f"[IMG2IMG DEBUG] Ref {idx+1}: {os.path.basename(img_path)} (small: {os.path.exists(small_path)})")
                        else:
                            print(f"[IMG2IMG WARNING] Ref {idx+1}: {os.path.basename(img_path)} - NO SMALL VERSION!")
                        prev_img_paths_list.append(img_path)
                        prev_img_captions.append(cap)
                    else:
                        print(f"[IMG2IMG ERROR] Ref {idx+1}: {img_path} NOT FOUND!")
                
                print(f"[IMG2IMG] Frame {frame_idx}: Using {len(prev_img_paths_list)} reference image(s) for continuity")
                
                # Skip time extraction - we maintain it in state already
                prev_time_of_day = ""
                prev_color = ""
        use_time_of_day = time_of_day or prev_time_of_day or (state.get('time_of_day', '') if 'state' in globals() else '')
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
            hard_transition=hard_transition
        )
        # Inject world flavor and location for image model only
        if world_flavor:
            prompt_str += f" World flavor: {world_flavor}."
        if world_summary:
            prompt_str += f" Background context: {world_summary}."
        if prev_img_paths and not hard_transition:
            prompt_str = (
                f"{prompt_str}\nMatch the lighting, time of day, and color palette to the previous image."
            )
        # --- LOGGING ---
        print("[IMG LOG] --- IMAGE GENERATION PARAMETERS ---")
        print(f"[IMG LOG] frame_idx: {frame_idx}")
        print(f"[IMG LOG] mode: {mode}")
        print(f"[IMG LOG] choice: {choice}")
        print(f"[IMG LOG] caption (vision_dispatch): {caption}")
        print(f"[IMG LOG] dispatch (narrative): {dispatch}")
        print(f"[IMG LOG] time_of_day: {use_time_of_day}")
        print(f"[IMG LOG] prompt_str (full): {prompt_str}")
        print(f"[IMG LOG] previous_image_path (actual): {prev_img_path if prev_img_path else 'None'}")
        print(f"[IMG LOG] reference_images_list: {len(prev_img_paths_list)} images")
        print(f"[IMG LOG] use_edit_mode: {use_edit_mode}")
        print(f"[IMG LOG] world_prompt: {world_prompt}")
        print("[IMG LOG] --- END IMAGE GENERATION PARAMETERS ---")
        IMAGE_DIR.mkdir(exist_ok=True)
        filename = f"{hash(caption) & 0xFFFFFFFF}_{_slug(caption)}.png"
        image_path = IMAGE_DIR / filename
        
        # --- ROUTE TO APPROPRIATE IMAGE PROVIDER ---
        if IMAGE_PROVIDER == "gemini":
            # Use Google Gemini (Nano Banana) - OFFICIAL API
            print(f"[IMG] Using Google Gemini (Nano Banana) provider")
            from gemini_image_utils import generate_with_gemini, generate_gemini_img2img
            
            if use_edit_mode and prev_img_paths_list and frame_idx > 0:
                print(f"[IMG] Gemini img2img mode with {len(prev_img_paths_list)} reference images")
                result_path = generate_gemini_img2img(
                    prompt=prompt_str,
                    caption=caption,
                    reference_image_path=prev_img_paths_list,  # Pass list of recent images
                    strength=strength,
                    world_prompt=world_prompt,
                    time_of_day=use_time_of_day,
                    action_context=choice,  # Pass action for FPS hands context
                    hd_mode=HD_MODE  # Use global HD mode setting
                )
            else:
                print(f"[IMG] Gemini text-to-image mode")
                result_path = generate_with_gemini(
                    prompt=prompt_str,
                    caption=caption,
                    world_prompt=world_prompt,
                    aspect_ratio="4:3",  # Faster generation, smaller files (1184x864)
                    time_of_day=use_time_of_day,
                    is_first_frame=(frame_idx == 0),  # Use Pro for first frame
                    action_context=choice,  # Pass action for FPS hands context
                    hd_mode=HD_MODE  # Use global HD mode setting
                )
            _last_image_path = result_path
            return result_path
        
        elif IMAGE_PROVIDER == "openai":
            # Use OpenAI DALL-E (original provider)
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
            return _last_image_path
        
        else:
            raise ValueError(f"Unknown IMAGE_PROVIDER: {IMAGE_PROVIDER}. Supported: 'openai', 'gemini'")
        # Skip time extraction - we already set time_of_day in state before generation
        # No need to extract it back from the image we just generated!
        return f"/images/{filename}"
    except Exception as e:
        print(f"[IMG PROVIDER {IMAGE_PROVIDER}] Error:", e)
        return None

# ───────── COMBINED dispatch generator (saves 1 API call) ─────────────────────
def _generate_combined_dispatches(choice: str, state: dict, prev_state: dict = None, prev_vision: str = "", current_image: str = None) -> tuple[str, str, bool]:
    """
    Generate BOTH narrative dispatch AND vision dispatch in ONE API call.
    Now supports multimodal input - can see the current frame!
    
    Args:
        choice: Player's choice
        state: Current game state
        prev_state: Previous game state
        prev_vision: Previous vision dispatch text
        current_image: Path to current image (optional, for multimodal context)
    
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
        
        # Detect timeout penalties (consequences that happen TO the player, not actions they take)
        is_timeout_penalty = any(phrase in choice.lower() for phrase in [
            "crushes you", "hits you", "attacks you", "shoots you", "tears into you",
            "engulfs you", "mauls you", "slams into you", "impacts you", "sustained",
            "to torso", "to limb", "trauma", "burns to", "pressure on", "collapses on"
        ])
        
        if is_timeout_penalty:
            # This is a CONSEQUENCE EVENT from hesitation, not a player action
            prompt = (
                "You are writing FIRST-PERSON BODY CAMERA FOOTAGE narration for photojournalist Jason Fleece.\n\n"
                + image_context +
                f"CONSEQUENCE EVENT (from hesitation): '{choice}'\n"
                f"WORLD CONTEXT: {world_prompt}\n\n"
                "This is NOT a player action - it's a CONSEQUENCE that happened due to hesitation. Describe what happens.\n\n"
                "Generate this format:\n\n"
                "NARRATIVE: [ONE sentence: What happens to you as a result of this consequence? Describe the physical effects you feel.]\n\n"
                "VISUAL: [ONE sentence describing ONLY what's visible in the camera frame from Jason's chest-mounted POV. What does the CAMERA see?]\n\n"
                "ALIVE: [true or false - Is Jason still alive after this consequence? Only false if explicitly fatal: direct gunshot, stabbing, fatal fall, lethal biome infection, etc.]\n\n"
                "CRITICAL POV RULES:\n\n"
                "• CAMERA POV ONLY: Describe what the CAMERA sees looking OUT from Jason's chest. Never describe Jason himself, his hands, his body, his feelings, or his internal state.\n"
                "• CONSEQUENCE VISIBLE IN FRAME: Show the RESULT of what happened - impacts, injuries, environmental effects, threats reacting.\n"
                "• EXTERNAL VIEW: You are the camera watching the world react to what happened TO Jason.\n\n"
                f"Describe what happens as a direct result of '{choice}'. This is a CONSEQUENCE EVENT, not a player action.\n"
            )
        else:
            # Normal player action
            prompt = (
                "You are writing FIRST-PERSON BODY CAMERA FOOTAGE narration for photojournalist Jason Fleece.\n\n"
                + image_context +
                f"PLAYER CHOICE: '{choice}'\n"
                f"WORLD CONTEXT: {world_prompt}\n\n"
                "Generate this format:\n\n"
                "NARRATIVE: [ONE sentence: Jason performs this action. What happens as a direct result?]\n\n"
                "VISUAL: [ONE sentence describing ONLY what's visible in the camera frame from Jason's chest-mounted POV. What does the CAMERA see?]\n\n"
                "ALIVE: [true or false - Is Jason still alive after this action? Only false if explicitly fatal: direct gunshot, stabbing, fatal fall, lethal biome infection, etc.]\n\n"
                "CRITICAL POV RULES:\n\n"
                "• CAMERA POV ONLY: Describe what the CAMERA sees looking OUT from Jason's chest. Never describe Jason himself, his hands, his body, his feelings, or his internal state.\n"
                "• CONSEQUENCE VISIBLE IN FRAME: If Jason punches someone, describe THEIR reaction (guard stumbles back, grabs face). If Jason cuts fence, describe the FENCE (metal parts with sparks, wire curls back). Show the RESULT of the action happening TO the environment/others.\n"
                "• EXTERNAL VIEW: You are the camera watching the world react to Jason's actions, NOT Jason experiencing them.\n\n"
                "CRITICAL ACTION RULES:\n\n"
                f"1. DIRECT CONSEQUENCE: The result MUST be a direct consequence of '{choice}'. If punching → describe guard's reaction. If cutting → describe fence breaking. If photographing → describe what's revealed. NO unrelated events.\n\n"
                "2. PHYSICAL & CONCRETE: Describe tangible results. Sparks flying, debris scattering, people reacting, objects breaking, impacts, explosions. NO vague atmosphere.\n\n"
                "3. STAY IN THE MOMENT: The action happens HERE and NOW. Show its immediate effect in this frame. Don't fast-forward or change location.\n"
            )
        
        # Use Gemini Flash for speed (with multimodal support!)
        import requests
        import base64
        from pathlib import Path
        gemini_api_key = CONFIG.get("GEMINI_API_KEY", "")
        
        print("[GEMINI TEXT] Calling Gemini 2.0 Flash for combined dispatches...")
        
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
                    image_data = base64.b64encode(f.read()).decode('utf-8')
                
                parts.insert(0, {
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": image_data
                    }
                })
                size_note = "(480x270)" if small_path.exists() else "(full-res)"
                print(f"[GEMINI TEXT+IMG] Including PREVIOUS timestep image: {current_image} {size_note}")
        
        response_data = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
            headers={"x-goog-api-key": gemini_api_key, "Content-Type": "application/json"},
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
        
        # Vision dispatch = same as dispatch (we only need one narrative now)
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
            PROMPTS["situation_summary_instructions"] +
            f"\n\nWorld State (after last action):\n{world_state}\nLast Dispatch (player's action):\n{last_dispatch}" +
            major_event_nudge +
            "\nDescribe what is happening NOW, after the dispatch, as a concise 1-2 sentence situation. This should set up the next set of choices."
        )
        return _ask(prompt, model="gemini", temp=0.7, tokens=40)
    return "You stand on a rocky outcrop overlooking the Horizon facility, the quarantine fence stretching across the red desert. Patrol lights sweep the landscape as distant thunder rumbles."

def begin_tick() -> dict:
    state = _load_state()
    # Generate a world summary (narrative)
    world_summary = state["world_prompt"]
    # Use the last dispatch as the situation report
    situation_report = history[-1]["dispatch"] if history else ""
    # —————— INTERIM MESSAGE (loading bar text) ——————
    # Use the world prompt to generate interim messages
    interim_messages = summarize_world_prompt_to_interim_messages(world_summary)
    loader = interim_messages
    # Generate the narrative update using the narrative prompt
    narrative_update = _world_report()
    # Condense world state for choices
    situation_summary = summarize_world_state(state)
    # Do NOT generate or return an image here. Only show text and choices.
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
        temperature=0.7,
        situation_summary=situation_summary
    )
    if len(options) == 1:
        parts = re.split(r"[\/,\-]|  +", options[0])
        options = [p.strip() for p in parts if p.strip()][:3]
    # Don't pad with placeholders - just return what we got
    return {
        "situation_report": situation_report,  # Return this as a separate field
        "choices": options,
        "interim_loader": loader,
    }

def _extract_time_and_color(image_path: str) -> tuple[str, str]:
    """Extract time of day and color palette (uses cached unified analysis)."""
    result = _vision_analyze_all(image_path)
    return result["time_of_day"], result["color_palette"]

# --- LLM-based player death check ---
def check_player_death(dispatch: str, world_prompt: str, choice: str) -> bool:
    prompt = (
        'Based on the following, has the player died? Respond with only "dead" or "alive".\n'
        f'DISPATCH: {dispatch}\n'
        f'WORLD STATE: {world_prompt}\n'
        f'PLAYER CHOICE: {choice}'
    )
    result = _ask(prompt, model="gemini", temp=0.5, tokens=2).strip().lower()
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

def advance_turn_image_fast(choice: str) -> dict:
    """
    PHASE 1 (FAST): Generate dispatch and image, return immediately.
    Returns image ASAP so bot can display it while choices are generating.
    
    Returns: {
        "dispatch": str,
        "vision_dispatch": str,
        "consequence_image": str,
        "phase": str,
        "chaos": int
    }
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
        from choices import generate_and_apply_choice
        generate_and_apply_choice(choice)
        state = _load_state()
        
        # Get previous vision and image
        prev_vision = ""
        prev_image = None
        if history and len(history) > 0:
            prev_vision = history[-1].get("vision_dispatch", "")
            prev_image = history[-1].get("image_url", None)
        
        # Generate dispatch
        dispatch, vision_dispatch, player_alive = _generate_combined_dispatches(choice, state, prev_state, prev_vision, prev_image)
        
        # SIMPLE DEATH SYSTEM: Just trust the LLM
        # If the LLM says you're dead, you're dead. No complex calculations.
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
        use_edit = True
        
        consequence_img_url = None
        try:
            last_image_path = None
            if history and len(history) > 0:
                for entry in reversed(history):
                    if entry.get("image"):
                        last_image_path = entry["image"].lstrip("/")
                        if not os.path.exists(last_image_path):
                            last_image_path = os.path.join("images", os.path.basename(last_image_path))
                        break
            
            consequence_img_url = _gen_image(
                vision_dispatch,
                mode,
                choice,
                image_description="",
                time_of_day=state.get('time_of_day', ''),
                use_edit_mode=(use_edit and last_image_path and os.path.exists(last_image_path)),
                frame_idx=frame_idx,
                dispatch=dispatch,
                world_prompt=state.get("world_prompt", ""),
                hard_transition=hard_transition
            )
            print(f"✅ [IMG FAST] Image ready for display: {consequence_img_url}")
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
    
    Returns: {
        "choices": list,
        "situation_report": str,
        "consequences": str,
        etc.
    }
    """
    global state, history
    
    print(f"[PHASE 2 DEBUG] Received image for choice generation: {consequence_img_url}")
    print(f"[PHASE 2 DEBUG] Dispatch: {dispatch[:50]}...")
    print(f"[PHASE 2 DEBUG] Vision: {vision_dispatch[:50]}...")
    
    state = _load_state()
    # Generate dynamic situation report from LLM based on current world state
    situation_summary = _generate_situation_report()
    
    # Generate choices with multimodal image input
    print(f"[PHASE 2 DEBUG] Passing image to generate_choices: {consequence_img_url}")
    next_choices = generate_choices(
        client, choice_tmpl,
        dispatch,  # What just happened
        n=3,
        image_url=consequence_img_url,  # Current scene - Gemini sees it directly!
        seen_elements='',
        recent_choices='',
        caption="",  # Don't pass text - let Gemini look at the image
        image_description="",  # Don't pass text - let Gemini look at the image
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
    """Atomically advance the simulation by one turn, guaranteeing all outputs are in sync."""
    global state, history, _last_image_path
    try:
        state = _load_state()
        history_path = ROOT / "history.json"
        if history_path.exists():
            with history_path.open("r", encoding="utf-8") as f:
                history = json.load(f)
        else:
            history = []
        prev_state = state.copy() if isinstance(state, dict) else dict(state)
        from choices import generate_and_apply_choice, generate_choices, categorize_choice, detect_threat
        generate_and_apply_choice(choice)
        state = _load_state()
        world_prompt = state.get("world_prompt", "")
        
        # Get previous vision and image for spatial continuity
        prev_vision = ""
        prev_image = None
        if history and len(history) > 0:
            prev_vision = history[-1].get("vision_dispatch", "")
            prev_image = history[-1].get("image_url", None)  # Previous timestep image
        
        # COMBINED: Generate BOTH dispatch and vision_dispatch in ONE API call
        # Pass the previous image so Gemini can SEE what happened before
        dispatch, vision_dispatch, player_alive = _generate_combined_dispatches(choice, state, prev_state, prev_vision, prev_image)
        
        # Update player state with death status
        state['player_state']['alive'] = player_alive
        
        # PROGRESSIVE FEEDBACK: Return dispatch immediately for player feedback
        progressive_data = {
            "type": "dispatch",
            "dispatch": dispatch,
            "vision_dispatch": vision_dispatch
        }
        # Note: This will be used by bot to show immediate feedback
        print(f"[PROGRESSIVE] Dispatch ready: {dispatch[:50]}...")
        
        # PATCH: Guarantee dispatch is never None or empty
        if not dispatch or dispatch.strip().lower() in {"none", "", "[", "[]"}:
            dispatch = "Jason makes a tense move in the chaos."
        if not vision_dispatch or vision_dispatch.strip().lower() in {"none", "", "[", "[]"}:
            vision_dispatch = dispatch  # Fallback to narrative if vision fails
        # --- Threat/risky action handling ---
        threat_level = 0
        if detect_threat(dispatch, vision_dispatch):
            # Simple threat level: count keywords
            threat_keywords = [
                'threat', 'danger', 'spotted', 'weapons raised', 'hostile', 'attack', 'confront', 'pursue', 'chase', 'ambush', 'alarm', 'alert', 'gun', 'rifle', 'shoot', 'fire', 'combat', 'fight', 'enemy', 'creature', 'biome', 'red biome', 'guards', 'soldier', 'military', 'aggressive', 'pursued', 'hunted', 'trap', 'injury', 'wound', 'bleed', 'blood', 'panic', 'critical', 'hazard', 'peril', 'dangerous', 'hazardous', 'explosion', 'contamination', 'hostile', 'alert', 'critical', 'warning', 'disaster', 'explosion', 'panic', 'contamination', 'artifact', 'ancient', 'storm', 'hostile', 'rumor', 'evidence', 'mutation', 'leader', 'broadcast', 'rescue', 'raid', 'sabotage', 'betrayal'
            ]
            text = f"{dispatch} {vision_dispatch}".lower()
            threat_count = sum(1 for k in threat_keywords if k in text)
            if threat_count >= 4:
                threat_level = 3
            elif threat_count >= 2:
                threat_level = 2
            elif threat_count == 1:
                threat_level = 1
        # Determine if the choice is risky
        risky_keywords = [
            'sneak', 'attack', 'fight', 'confront', 'steal', 'disarm', 'run', 'escape', 'charge', 'rush', 'tackle', 'sabotage', 'destroy', 'kill', 'ambush', 'evade', 'dodge', 'hide', 'crawl', 'climb', 'scale', 'jump', 'leap', 'scramble', 'slide', 'advance', 'move past', 'move closer', 'approach', 'investigate', 'photograph the guards', 'photograph guards', 'photograph the enemy', 'shoot', 'fire', 'risk', 'danger', 'hazard', 'peril', 'hazardous', 'dangerous', 'perilous'
        ]
        # Store risky action result for later use, but don't return early
        # Let the normal flow continue so images get generated
        is_risky = any(rk in choice.lower() for rk in risky_keywords)
        risky_consequence = None
        if is_risky and threat_level > 0:
            success, risky_consequence = resolve_risky_action(choice, threat_level, dispatch, world_prompt)
            # Update world state minimally for fail (could expand later)
            if not success:
                state['chaos_level'] = int(state.get('chaos_level', 0)) + 1
                _save_state(state)
            print(f"[RISKY ACTION] Resolved: {risky_consequence}")
        # --- Combat/Threat UI indicator ---
        threat = detect_threat(dispatch, vision_dispatch)
        # Evolve world state BEFORE generating the image
        from evolve_prompt_file import evolve_world_state
        consequence_summary = summarize_world_state_diff(prev_state, state)
        evolve_world_state(history, consequence_summary, vision_description=vision_dispatch)
        state = _load_state()
        # Summarize world prompt for image
        summarized_world_prompt = summarize_world_prompt_for_image(state.get("world_prompt", ""))
        category, _ = categorize_choice(choice)
        
        # --- Generate dispatch image (DISABLED - only showing consequence image) ---
        mode = state.get("mode", "camcorder")
        frame_idx = len(history) + 1
        # Check if this is a hard transition (location change, major scene shift)
        hard_transition = is_hard_transition(choice, dispatch)
        # ENABLE img2img for visual continuity
        use_edit = True
        prior_context = ""
        
        if hard_transition:
            print(f"[IMG] Hard transition detected! But still using img2img for continuity.")
        # SKIP dispatch image - we only want ONE image (the consequence)
        dispatch_img_url = None
        print(f"[IMG] Skipping dispatch image, will generate consequence image only")
        
        # --- Consequence fallback ---
        def generate_consequence_summary(dispatch, prev_state, state, choice):
            diff_summary = summarize_world_state_diff(prev_state, state)
            prompt = (
                "Write a single, punchy gameplay consequence line (not a paragraph) describing the IMMEDIATE result of the player's last action. "
                "Use direct verbs and clear outcomes, like a game log or resolution. Be creative, concise, and avoid generic or verbose summaries. "
                "If nothing major changed, say 'No major consequence.'\n"
                f"DISPATCH: {dispatch}\n"
                f"WORLD STATE CHANGES: {diff_summary}\n"
                f"PLAYER CHOICE: {choice}"
            )
            try:
                # Use Gemini Flash for speed
                import requests
                gemini_api_key = CONFIG.get("GEMINI_API_KEY", "")
                
                print("[GEMINI TEXT] Calling Gemini 2.0 Flash for consequence summary...")
                response_data = requests.post(
                    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
                    headers={"x-goog-api-key": gemini_api_key, "Content-Type": "application/json"},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"temperature": 1.0, "maxOutputTokens": 40}
                    },
                    timeout=10
                ).json()
                print("[GEMINI TEXT] ✅ Consequence summary complete")
                
                summary = response_data["candidates"][0]["content"]["parts"][0]["text"].strip()
                
                if summary.strip().lower() in {"no major consequence.", "no major consequence", "none", "no consequence", "no consequences", ""}:
                    return "No major consequence this turn."
                return summary
            except Exception as e:
                log_error(f"[CONSEQUENCE] Gemini error: {e}")
                return "No major consequence this turn."
        # Limit history to last 3 dispatches for consequence context
        limited_prev_state = prev_state.copy() if isinstance(prev_state, dict) else dict(prev_state)
        limited_state = state.copy() if isinstance(state, dict) else dict(state)
        def trim_world_prompt(world_prompt):
            sentences = re.split(r'(?<=[.!?]) +', world_prompt)
            return ' '.join(sentences[-3:]) if len(sentences) > 3 else world_prompt
        limited_prev_state['world_prompt'] = trim_world_prompt(limited_prev_state.get('world_prompt', ''))
        limited_state['world_prompt'] = trim_world_prompt(limited_state.get('world_prompt', ''))
        # Use risky_consequence if it was generated, otherwise generate normal consequence
        if risky_consequence:
            consequence_summary = risky_consequence
            print(f"[CONSEQUENCE] Using risky action result: {consequence_summary}")
        else:
            consequence_summary = generate_consequence_summary(dispatch, limited_prev_state, limited_state, choice)
        
        # --- Generate ONE image showing the consequence/result ---
        print(f"[IMG] Generating consequence image (result of action)...")
        consequence_img_url = None
        try:
            # Get the last image from history for img2img continuity
            last_image_path = None
            if history and len(history) > 0:
                for entry in reversed(history):
                    if entry.get("image"):
                        last_image_path = entry["image"].lstrip("/")
                        if not os.path.exists(last_image_path):
                            last_image_path = os.path.join("images", os.path.basename(last_image_path))
                        break
            
            print(f"[IMG] Last image path: {last_image_path}")
            print(f"[IMG] Vision dispatch: {vision_dispatch[:100]}...")
            
            # Generate ONE image showing the consequence/result
            consequence_img_url = _gen_image(
                vision_dispatch,  # Visual description of the consequence
                mode,
                choice,
                image_description="",
                time_of_day=state.get('time_of_day', ''),
                use_edit_mode=(use_edit and last_image_path and os.path.exists(last_image_path)),
                frame_idx=frame_idx,
                dispatch=dispatch,
                world_prompt=state.get("world_prompt", ""),
                hard_transition=hard_transition
            )
            print(f"✅ [IMG] Consequence image generated: {consequence_img_url}")
            
            # Set dispatch_img_url to the same as consequence for compatibility
            dispatch_img_url = consequence_img_url
            
        except Exception as e:
            print(f"❌ [IMG] Error generating consequence image: {e}")
            import traceback
            traceback.print_exc()
            consequence_img_url = None
            dispatch_img_url = None
        
        # --- Skip vision analysis - choices now see the image directly via multimodal! ---
        image_description = ""  # Not needed anymore, kept for compatibility
        
        # --- Streak tracking, rare events, etc. (unchanged) ---
        streak = state.get('streak', 0)
        rare_event = state.get('rare_event', None)
        streak_reward = state.get('streak_reward', None)
        situation_summary = summarize_world_state(state)
        # Generate next choices for normal flow
        # Pass the newly generated image so choices are grounded in what's visible
        next_choices = generate_choices(
            client, choice_tmpl,
            dispatch,
            n=3,
            image_url=consequence_img_url,  # Current timestep image - Gemini can SEE it!
            seen_elements='',
            recent_choices='',
            caption=vision_dispatch,
            image_description=image_description,  # Use analyzed image content
            time_of_day=state.get('time_of_day', ''),
            world_prompt=state.get('world_prompt', ''),
            temperature=0.7,
            situation_summary=situation_summary
        )
        # Remove placeholder-only choices
        next_choices = [c for c in next_choices if c and c.strip() and c.strip() != '—']
        if not next_choices:
            next_choices = ["Look around", "Move forward", "Wait"]
        while len(next_choices) < 3:
            next_choices.append("—")
        # --- Save turn to history ---
        history_entry = {
            "choice": choice,
            "dispatch": dispatch,
            "vision_dispatch": vision_dispatch,
            "vision_analysis": image_description,  # NEW: Save vision analysis for next frame continuity
            "world_prompt": state.get("world_prompt", ""),
            "image": dispatch_img_url
        }
        history.append(history_entry)
        (ROOT / "history.json").write_text(json.dumps(history, indent=2))
        
        # --- Return atomic result ---
        return {
            "phase":    state["current_phase"],
            "chaos":    state["chaos_level"],
            "dispatch": dispatch,
            "vision_dispatch": vision_dispatch,
            "dispatch_image": dispatch_img_url,
            "consequence_image": consequence_img_url,  # NEW: Continuous FPS footage
            "caption":  vision_dispatch,
            "mode":     state.get("mode", "camcorder"),
            "situation_report": summarize_world_state(state),
            "choices":  next_choices,
            "player_state": state.get('player_state', {}),
            "consequences": consequence_summary,
            'streak_reward': streak_reward,
            'rare_event': rare_event,
            'danger': threat,
            'combat': False,
            'combat_message': ""
        }
    except Exception as e:
        log_error(f"[ADVANCE TURN] {e}")
        import traceback
        traceback.print_exc()
        # Return a valid structure so the game doesn't crash
        return {
            "phase": state.get("current_phase", "normal") if 'state' in locals() else "normal",
            "chaos": state.get("chaos_level", 0) if 'state' in locals() else 0,
            "dispatch": f"Error: {str(e)}",
            "vision_dispatch": f"Error occurred: {str(e)}",
            "dispatch_image": None,
            "consequence_image": None,
            "caption": "Error",
            "mode": "camcorder",
            "situation_report": f"An error occurred: {str(e)}",
            "choices": ["Restart", "Continue", "—"],
            "player_state": {},
            "consequences": f"Error: {str(e)}",
            'streak_reward': None,
            'rare_event': None,
            'danger': False,
            'combat': False,
            'combat_message': "",
            'error': str(e)
        }

# Replace all uses of complete_tick with advance_turn
complete_tick = advance_turn

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
    
    with (ROOT / "world_state.json").open("w", encoding="utf-8") as f:
        json.dump({
            "world_prompt": PROMPTS["world_initial_state"],
            "current_phase": "normal",
            "chaos_level": 0,
            "last_choice": "",
            "last_saved": datetime.utcnow().isoformat(),
            "seen_elements": [],
            "player_state": {"alive": True},
            "time_of_day": initial_time
        }, f, indent=2)
    _last_image_path = None
    _choices_since_edit_reset = 0

# --- State/history reset policy ---
# State and history are only reset:
#   1. On app reload (see bottom of this file)
#   2. On explicit reset (reset button, /api/reset, or Discord command)
#   3. If the player chooses the explicit 'Return to your truck on the outskirts of the Horizon military quarantine perimeter' option (soft narrative reset)
# No other code path should reset state/history. This ensures story progression and vision continuity.

# ───────── tiny Flask image server ──────────────────────────────────────────
_flask = Flask(__name__)
@_flask.route("/images/<path:name>")
def _serve(name):
    return send_from_directory(IMAGE_DIR, name)

@_flask.route("/")
def serve_webui():
    return send_from_directory(str(ROOT / "templates"), "webui.html")

# --- Minimal API for web frontend ---
@_flask.route("/api/state", methods=["GET"])
def api_state():
    # Return the current situation, image, and choices
    snap = begin_tick()
    return jsonify({
        "situation_report": snap.get("situation_report", ""),
        "world_image": snap.get("world_image"),
        "choices": snap.get("choices", []),
        "interim_loader": snap.get("interim_loader", [])
    })

@_flask.route("/api/choose", methods=["POST"])
def api_choose():
    data = request.get_json(force=True)
    choice = data.get("choice", "")
    if not choice:
        return jsonify({"error": "No choice provided"}), 400
    result = complete_tick(choice)
    return jsonify(result)

@_flask.route("/api/reset", methods=["POST"])
def api_reset():
    reset_state()
    return jsonify({"ok": True})

@_flask.route("/api/regenerate_choices", methods=["POST"])
def api_regenerate_choices():
    state = _load_state()
    # Use the last dispatch as the situation report
    if history:
        situation_report = history[-1]["dispatch"]
    else:
        situation_report = state.get("world_prompt", "")
    options = generate_choices(
        client, choice_tmpl,
        situation_report,
        n=3,
        seen_elements='',
        recent_choices='',
        caption=situation_report,
        image_description='',
        time_of_day=state.get('time_of_day', ''),
        temperature=0.2
    )
    if len(options) == 1:
        parts = re.split(r"[\/,\-]|  +", options[0])
        options = [p.strip() for p in parts if p.strip()][:3]
    # Don't pad with placeholders - just return what we got
    return jsonify({"choices": options})

def _run_flask():
    port = int(os.environ.get("PORT", CONFIG.get("IMAGE_PORT", 8000)))
    _flask.run(host="0.0.0.0", port=port, debug=False)
    print(f"[StoryGen] Web UI and API running at http://localhost:{port}/")

# --- FAST: Return image immediately, before choice generation ---
def generate_intro_image_fast():
    """
    PHASE 1 (FAST): Generate ONLY the intro image and basic info.
    Returns immediately so bot can display image while choices are generating.
    
    Returns: {
        "dispatch": str,
        "vision_dispatch": str,
        "dispatch_image": str,
        "prologue": str,
        "mode": str
    }
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
            
            # Skip time extraction - Gemini infers lighting from reference images
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
    
    Args:
        image_url: Path to the generated intro image
        prologue: The intro narrative text
        vision_dispatch: Visual description of the scene
        dispatch: Same as prologue (for consistency with other calls)
    
    Returns: {
        "choices": list,
        "phase": str,
        "chaos": int,
        "player_state": dict
    }
    """
    global state, history
    
    state = _load_state()
    
    # Generate choices
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

# --- Patch: Use prologue as intro turn driver ---
def generate_intro_turn():
    """
    Generate the intro turn: dispatch, vision_dispatch, image, and choices,
    using the prologue as the first dispatch and context.
    """
    global state, history, _last_image_path
    import random
    
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
            
            # Skip time extraction - Gemini infers lighting from reference images
            
            # Skip vision analysis - choices now see the image directly via multimodal!
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

def summarize_world_state_diff(prev_state: dict, state: dict) -> str:
    """
    Return a concise summary of the most important differences between two world states.
    Prioritize changes that affect danger, alliances, resources, major world events, or key motifs.
    De-emphasize cosmetic or minor changes.
    """
    diffs = []
    # Major world prompt change
    if prev_state.get('world_prompt', '') != state.get('world_prompt', ''):
        # Only include if the new prompt contains key motifs
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
    # Player state (injury, death, etc.)
    if prev_state.get('player_state', {}) != state.get('player_state', {}):
        prev_alive = prev_state.get('player_state', {}).get('alive', True)
        curr_alive = state.get('player_state', {}).get('alive', True)
        if not curr_alive:
            diffs.append("Player is dead or gravely wounded.")
        elif not prev_alive and curr_alive:
            diffs.append("Player revived or recovered.")
        else:
            diffs.append(f"Player state changed: {prev_state.get('player_state', {})} → {state.get('player_state', {})}")
    # New seen elements (only if they match motifs)
    prev_seen = set(prev_state.get('seen_elements', []))
    curr_seen = set(state.get('seen_elements', []))
    new_seen = curr_seen - prev_seen
    if new_seen:
        motif_seen = [e for e in new_seen if any(m in e.lower() for m in motifs)]
        if motif_seen:
            diffs.append(f"New key elements: {', '.join(list(motif_seen)[:3])}")
    if not diffs:
        return "No major world state changes."
    return "; ".join(diffs)

# --- Streak and rare event helpers ---
def is_clever_or_risky(choice):
    # Heuristic: risky/clever if contains certain keywords
    keywords = ["sneak", "hide", "stealth", "evade", "escape", "confront", "attack", "investigate", "search", "scan", "explore", "photograph", "analyze", "decipher", "decode", "hack", "sabotage", "ally", "ally with", "bargain", "bribe", "bluff", "trick", "outsmart", "ambush", "rescue", "save", "risk", "danger", "hazard", "peril", "bold", "daring", "reckless", "brave", "uncover", "discover", "secret", "hidden", "mystery", "clue", "artifact", "ancient", "forbidden", "rare", "secret"]
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
    # Use LLM to generate a punchy, context-appropriate consequence
    prompt = (
        f"Write a single, punchy gameplay consequence line for the player's risky action. "
        f"ACTION: {choice}\nDISPATCH: {dispatch}\nWORLD: {world_prompt}\n"
        f"OUTCOME: {'success' if success else 'failure'}\n"
        "If success, describe how the player advances or avoids danger. If failure, describe the immediate negative result (e.g., spotted, injured, alarms, etc)."
    )
    try:
        summary = _ask(prompt, model="gemini", temp=1.0, tokens=40)
        if not summary.strip():
            summary = 'No major consequence.'
        return success, summary
    except Exception as e:
        log_error(f"[RISKY ACTION CONSEQUENCE] LLM error: {e}")
        return success, 'No major consequence.'

if __name__ == "__main__":
    reset_state()  # Always clear state/history on fresh run
    threading.Thread(target=_run_flask, daemon=True).start()

LOG_FILE = "logs/error.log"
def log_error(msg):
    """Log error messages to a file with timestamp."""
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.utcnow().isoformat()}] {msg}\n")
