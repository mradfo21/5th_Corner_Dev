# choices.py – aligned with {world_state} + {dispatch}, no more KeyError
from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import List, Union

# generate_interim_messages removed in dynamic world evolution rewrite
# Evolution summaries now stored in state["evolution_summary"]
import engine
import difflib

# No longer using OpenAI - everything uses Gemini now!
def _ensure_client(c):
    """Legacy function - no longer needed, Gemini is used directly"""
    return None

# ──────────────────────────────────────────────────────────────────────────────
def filter_choices(choices, seen_elements, recent_choices, dispatch='', image_description='', world_prompt=''):
    # Only keep choices that reference something present in the dispatch, image, or world_prompt
    allowed_context = f"{dispatch} {image_description} {world_prompt}".lower()
    filtered = []
    for c in choices:
        c_lower = c.lower()
        # If the choice mentions a person/object not in context, drop it
        tokens = re.findall(r"\b\w+\b", c_lower)
        if any(tok for tok in tokens if tok not in allowed_context):
            # If the choice is too out-of-context, skip
            if not any(tok in allowed_context for tok in tokens):
                continue
        filtered.append(c)
    return filtered or ["Try something relevant"]

def is_too_similar(a, b):
    """Return True if two choices are too similar (substring or high similarity)."""
    if a in b or b in a:
        return True
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return ratio > 0.75

def truncate_choice(choice, max_len=60):
    if len(choice) <= max_len:
        return choice
    # Try to cut at the last space before max_len
    cut = choice[:max_len].rsplit(' ', 1)[0]
    if len(cut) < max_len // 2:
        cut = choice[:max_len]
    return cut.rstrip() + '…'

def enforce_diversity(choices):
    """Remove choices that are too similar to each other and truncate them."""
    unique = []
    for c in choices:
        c_trunc = truncate_choice(c)
        if all(not is_too_similar(c_trunc.lower(), u.lower()) for u in unique):
            unique.append(c_trunc)
    return unique

def generate_choices(
    client = None,  # No longer used - Gemini is called directly
    prompt_tmpl: str = "",
    last_dispatch: str = "",
    n: int = 3,
    image_url: str = None,
    seen_elements: str = "",
    recent_choices: str = "",
    caption: str = "",
    image_description: str = "",
    time_of_day: str = "",
    beat_nudge: str = "",
    pacing: str = None,
    world_prompt: str = "",
    temperature: float = 1.2,
    situation_summary: str = "",
    inventory: list = None  # Player inventory items
) -> List[str]:
    """
    Ask the model for up to n choices. The template must contain:
      • {dispatch}     — the last dispatch text
      • {caption}      — the image caption (new)
      • {image_description} — description of the current image (if any)
      • {time_of_day}  — the current time of day (if any)
      • {beat_nudge}   — the current story beat nudge (if any)
      • {situation_summary} — a single actionable summary of the world state (if any)
    """
    # No longer using OpenAI client - everything uses Gemini now
    # Update the prompt to require unique, contextually grounded, and diverse choices
    prompt = prompt_tmpl.replace('2-4 words', '2-5 words').replace(
        'Suggest a consequence, risk, or emotional cue',
        'Suggest a consequence, risk, or emotional cue\n- Each choice must be unique, contextually grounded, and not a variant of "photograph", "sneak", or "search" unless those are truly the only logical actions.\n- Use a wide variety of verbs, including interact, distract, signal, hide, decode, analyze, negotiate, etc.\n- Encourage emotional/moral dilemmas and environmental interactions.\n- Avoid generic or repetitive phrasing.'
    ).format(
        dispatch=last_dispatch.strip(),
        seen_elements=seen_elements,
        recent_choices=recent_choices,
        caption=caption,
        image_description=image_description or "",
        time_of_day=time_of_day or "",
        beat_nudge=beat_nudge,
        situation_summary=situation_summary  # RE-ENABLED: This is now grounded via Vision AI in Phase 2!
    )
    
    # Format inventory for prompt
    inventory_text = ""
    if inventory and len(inventory) > 0:
        try:
            from items import ITEMS
            item_names = [ITEMS[item_id]["display"] for item_id in inventory if item_id in ITEMS]
            if item_names:
                inventory_text = f"\n\n**PLAYER INVENTORY:** {', '.join(item_names)}\n- You may generate choices that USE these items when contextually appropriate\n- Format item-using choices as: 'Action description [Item Name]'\n- Example: 'Pry open door [Crowbar]' or 'Illuminate corridor [Flashlight]'\n"
        except Exception as e:
            print(f"[CHOICES] Error formatting inventory: {e}")
    
    system_prompt = {"role": "system", "content": (
        "Generate 3 VISCERAL, PHYSICAL ACTION CHOICES (3-6 words each). Emphasize BODILY movement and physical risk.\n\n"
        f"{inventory_text}"
        "CRITICAL: Use VIVID, PHYSICAL VERBS that emphasize what the player's BODY does:\n\n"
        "PHYSICAL BODY VERBS (PRIORITIZE THESE):\n"
        "- LEGS/FEET: Sprint, Vault, Leap, Scramble, Slide, Dive, Kick, Stomp, Brace, Plant, Launch\n"
        "- ARMS/HANDS: Grab, Yank, Wrench, Hurl, Smash, Rip, Pry, Claw, Shove, Swing, Heave\n"
        "- TORSO: Slam, Throw yourself, Barrel through, Roll, Twist, Duck, Drop, Lunge, Charge\n"
        "- FULL BODY: Hurl yourself, Fling yourself, Propel forward, Burst through, Crash into\n\n"
        "GROUNDING: Base ALL choices on the ATTACHED IMAGE and the provided IMAGE DESCRIPTION. The image and its description are the absolute source of truth for Jason's current position.\n\n"
        "EXAMPLES OF EXCITING CHOICES:\n"
        "✅ 'Vault over chain-link fence'\n"
        "✅ 'Hurl yourself through window'\n"
        "✅ 'Sprint full-tilt toward shed'\n"
        "✅ 'Yank open rusted blast door'\n"
        "✅ 'Scramble up rocky slope'\n"
        "✅ 'Dive behind concrete barrier'\n"
        "✅ 'Wrench free the metal grate'\n"
        "✅ 'Barrel through the doorway'\n\n"
        "❌ BORING (DO NOT USE):\n"
        "- 'Look around'\n"
        "- 'Go inside'\n"
        "- 'Move forward'\n"
        "- 'Check it out'\n"
        "- 'Approach carefully'\n\n"
        "GROUNDING: Only reference what's VISIBLE in the image, but use EXCITING physical language.\n\n"
        "MOMENTUM: Jason is ALWAYS aggressive and forward-moving. Even 'safe' choices should feel ACTIVE and DECISIVE.\n\n"
        "Make every choice feel like an ACTION MOVIE. Use words that make you FEEL the physical exertion."
    )}
    if image_url:
        messages = [
            system_prompt,
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": image_url}
                ]
            }
        ]
    else:
        messages = [system_prompt, {"role": "user", "content": prompt}]
    # Use Gemini Flash for speed (with multimodal support!)
    import requests
    import base64
    import os
    from pathlib import Path
    # CRITICAL: Use the same API key and model as the engine for consistency and 403 prevention
    from engine import GEMINI_API_KEY as gemini_api_key
    import ai_provider_manager
    model_name = ai_provider_manager.get_text_model()
    
    # DEBUG: Log API key status
    if gemini_api_key:
        print(f"[CHOICES DEBUG] API key loaded from engine: {gemini_api_key[:20]}...{gemini_api_key[-8:]} (len={len(gemini_api_key)})")
    else:
        print(f"[CHOICES DEBUG] ERROR - API key is EMPTY or None!")
    
    # Combine system and user messages
    if isinstance(messages[1].get("content"), list):
        # Extract text from multimodal content
        full_prompt = system_prompt["content"] + "\n\n" + messages[1]["content"][0]["text"]
    else:
        full_prompt = system_prompt["content"] + "\n\n" + (messages[1]["content"] if len(messages) > 1 else prompt)
    
    # Add visual context if image is provided
    if image_url:
        full_prompt = "🖼️ CRITICAL: LOOK AT THE ATTACHED IMAGE FIRST. The image shows the CURRENT scene (what Jason sees RIGHT NOW). Base ALL your choices on what is ACTUALLY, VISIBLY present in this image. Ignore any text descriptions that contradict what you see in the image. The image is the source of truth.\n\n" + full_prompt
    
    # Build parts list (text + optional image)
    parts = [{"text": full_prompt}]
    
    # Add current timestep image if provided
    if image_url:
        print(f"[CHOICES DEBUG] Received image_url: {image_url}")
        
        # Use pre-downsampled version if available
        if image_url.startswith("/images/"):
            actual_path = Path("images") / image_url.replace("/images/", "")
        else:
            actual_path = Path(image_url)
        
        small_path = actual_path.parent / actual_path.name.replace(".png", "_small.png")
        use_path = small_path if small_path.exists() else actual_path
        
        print(f"[CHOICES DEBUG] Using file: {use_path}")
        print(f"[CHOICES DEBUG] File exists: {use_path.exists()}")
        
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
            print(f"[GEMINI TEXT+IMG] Including CURRENT timestep image for choices: {image_url} {size_note}")
        else:
            print(f"[CHOICES ERROR] Image file not found: {use_path}")
    
    print(f"[GEMINI TEXT] Calling {model_name} for choice generation...", flush=True)
    
    try:
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
            headers={"x-goog-api-key": gemini_api_key, "Content-Type": "application/json"},
            json={
                "contents": [{"parts": parts}],
                "generationConfig": {"temperature": temperature, "maxOutputTokens": 200}
            },
            timeout=15
        )
        print(f"[GEMINI TEXT] API returned status: {response.status_code}", flush=True)
        response.raise_for_status()
        response_data = response.json()
        print("[GEMINI TEXT] Choice generation complete", flush=True)
    except requests.exceptions.Timeout:
        print(f"[CHOICES ERROR] Gemini API timeout after 15 seconds", flush=True)
        return ["Look around", "Move forward", "Wait"]
    except requests.exceptions.HTTPError as e:
        print(f"[CHOICES ERROR] Gemini API HTTP error: {e}", flush=True)
        if hasattr(e, 'response') and e.response is not None:
            print(f"[CHOICES ERROR] Response: {e.response.text}", flush=True)
        return ["Look around", "Move forward", "Wait"]
    except Exception as e:
        print(f"[CHOICES ERROR] Unexpected error calling Gemini API: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return ["Look around", "Move forward", "Wait"]
    
    # Create a mock OpenAI response object
    class GeminiResp:
        def __init__(self, text):
            self.choices = [type('obj', (object,), {'message': type('obj', (object,), {'content': text})()})]
    
    # Check for API errors
    if "candidates" not in response_data:
        print(f"[CHOICES ERROR] Gemini API error response: {response_data}")
        if "error" in response_data:
            print(f"[CHOICES ERROR] Error details: {response_data['error']}")
        # Return fallback choices
        return ["Look around", "Move forward", "Wait"]
    
    result_text = response_data["candidates"][0]["content"]["parts"][0]["text"].strip()
    rsp = GeminiResp(result_text)
    raw = rsp.choices[0].message.content.strip()
    print("[CHOICES RAW LLM OUTPUT]", repr(raw))
    opts: List[str] = []
    seen = set()
    for line in raw.splitlines():
        line = line.strip().lstrip("-*0123456789. ").strip()
        line_lower = line.lower()
        # Skip preamble text and meta-commentary
        if (
            4 < len(line) <= 40
            and not line.endswith(("...", "-", "—"))
            and line_lower not in seen
            and " choices" not in line_lower  # Filter ANY line mentioning " choices"
            and " action choices" not in line_lower  # Specific filter for "action choices"
            and not line_lower.startswith(("scene:", "narrative:", "option:", "choice:", "here are", "here's", "here is"))
            and "for jason" not in line_lower  # Filter any meta-commentary about Jason
        ):
            opts.append(line)
            seen.add(line_lower)
    # Stricter filtering: remove out-of-context choices
    opts = filter_choices(opts, seen_elements, recent_choices, dispatch=last_dispatch, image_description=image_description, world_prompt=world_prompt)
    # Filter out repeated choices
    # Remove any choices containing 'retreat' or 'flee' (case-insensitive)
    opts = [c for c in opts if 'retreat' not in c.lower() and 'flee' not in c.lower()]
    # Final diversity and generic filter
    opts = enforce_diversity(opts)
    opts = [c for c in opts if c.lower() not in {"photograph the chaos", "sneak past the guards", "search for hidden passage"}]
    if not opts:
        opts = ["Look around", "Move forward", "Wait"]
    # Enforce diversity: try to include at least one action, one explore, and one move/escape (not retreat/flee)
    categorized = {"action": [], "explore": [], "move": []}
    for c in opts:
        cl = c.lower()
        if any(w in cl for w in ["attack", "fight", "grab", "use", "push", "pull", "break", "smash", "defend", "block", "dodge", "strike", "hit", "fire", "blast", "charge", "tackle", "sabotage", "destroy", "kill", "counter", "parry", "evade", "swing", "slash", "burn", "poison", "threaten", "challenge", "face off", "stand off", "resist", "survive", "risk", "danger", "hazard", "peril", "bleed", "hurt", "injury", "damage", "dangerous", "hazardous"]):
            categorized["action"].append(c)
        elif any(w in cl for w in ["explore", "search", "look", "scan", "investigate", "inspect", "trace", "survey", "observe", "peek", "scout", "examine", "analyze", "study", "decode", "translate", "repair", "fix", "unlock", "bypass", "hack", "question", "interrogate", "persuade", "inspect", "analyze", "study", "examine", "inspect"]):
            categorized["explore"].append(c)
        elif any(w in cl for w in ["run", "move", "advance", "proceed", "escape", "leave", "exit", "go to", "rush", "sprint", "dodge", "duck", "climb", "scale", "jump", "leap", "scramble", "slide", "crawl", "backtrack", "return", "withdraw", "step back", "fall back", "get away", "hide"]):
            categorized["move"].append(c)
    # Build a diverse set if possible
    diverse = []
    if categorized["action"]:
        diverse.append(categorized["action"][0])
    if categorized["explore"]:
        diverse.append(categorized["explore"][0])
    if categorized["move"]:
        diverse.append(categorized["move"][0])
    # Fill up to n with remaining unique options
    for c in opts:
        if c not in diverse and len(diverse) < n:
            diverse.append(c)
    opts = diverse[:n]
    # Final diversity check
    opts = enforce_diversity(opts)
    # After generating choices, run the critic
    vision = image_description if image_description else ''
    recent = []
    if recent_choices:
        if isinstance(recent_choices, list):
            recent = recent_choices
        elif isinstance(recent_choices, str):
            recent = [recent_choices]
    improved_choices = choice_critic(last_dispatch, vision, opts, world_prompt, recent_choices=recent)
    # Persist recent choices in world_state.json
    try:
        path = Path("world_state.json")
        if path.exists():
            state = json.loads(path.read_text())
        else:
            state = {}
        state["recent_choices"] = improved_choices
        path.write_text(json.dumps(state, indent=2))
    except Exception as e:
        print("[CHOICES] Failed to persist recent choices:", e)
    return improved_choices

# --- Threat detection groundwork ---
def detect_threat(dispatch, vision=None):
    """Return True if the dispatch or vision contains threat/danger cues."""
    threat_keywords = [
        'threat', 'danger', 'spotted', 'weapons raised', 'hostile', 'attack', 'confront', 'pursue', 'chase', 'ambush', 'alarm', 'alert', 'gun', 'rifle', 'shoot', 'fire', 'combat', 'fight', 'enemy', 'creature', 'biome', 'red biome', 'guards', 'soldier', 'military', 'aggressive', 'pursued', 'hunted', 'trap', 'injury', 'wound', 'bleed', 'blood', 'panic', 'critical', 'hazard', 'peril', 'dangerous', 'hazardous', 'explosion', 'contamination', 'hostile', 'alert', 'critical', 'warning', 'disaster', 'explosion', 'panic', 'contamination', 'artifact', 'ancient', 'storm', 'hostile', 'rumor', 'evidence', 'mutation', 'leader', 'broadcast', 'rescue', 'raid', 'sabotage', 'betrayal'
    ]
    text = f"{dispatch} {vision or ''}".lower()
    return any(k in text for k in threat_keywords)

# --- Scene element extraction ---
def extract_scene_elements(dispatch, vision=None):
    """Extract key nouns and verbs from dispatch/vision for anchoring choices."""
    import re
    text = f"{dispatch} {vision or ''}"
    # Simple noun/verb extraction (could be replaced with spaCy/LLM for more power)
    words = re.findall(r'\b\w+\b', text.lower())
    # Remove stopwords and short words
    stopwords = set(['the', 'and', 'a', 'an', 'of', 'in', 'on', 'at', 'to', 'for', 'with', 'by', 'as', 'from', 'is', 'are', 'was', 'were', 'it', 'he', 'she', 'they', 'his', 'her', 'their', 'this', 'that', 'but', 'or', 'if', 'then', 'so', 'do', 'did', 'has', 'have', 'had', 'be', 'been', 'will', 'would', 'can', 'could', 'should', 'may', 'might', 'must', 'not', 'no', 'yes', 'just', 'now', 'out', 'up', 'down', 'over', 'under', 'into', 'back', 'off', 'all', 'any', 'some', 'more', 'most', 'other', 'such', 'only', 'own', 'same', 'so', 'than', 'too', 'very'])
    elements = set(w for w in words if len(w) > 2 and w not in stopwords)
    return elements

# --- Enhanced filtering ---
def filter_choices_strict(choices, dispatch, vision, world_prompt, recent_choices=None):
    # Extract scene elements
    elements = extract_scene_elements(dispatch, vision)
    # Remove choices that do not reference any scene element
    filtered = []
    for c in choices:
        c_lower = c.lower()
        if any(e in c_lower for e in elements):
            filtered.append(c)
    # Remove repeats
    if recent_choices:
        filtered = [c for c in filtered if c not in recent_choices[-2:]]
    # If all choices are filtered out, fallback to original
    if not filtered:
        filtered = choices[:]
    return filtered

# --- Contextual risk assessment ---
def filter_risky_choices(choices, dispatch, vision):
    # DISABLED - We WANT risky, daring choices!
    # Let the player make bold, dangerous decisions
    return choices

def choice_critic(dispatch, vision, choices, world_prompt, recent_choices=None):
    # Remove placeholders and duplicates first
    filtered = [c for c in choices if c and c.strip() and c.strip() != '—']
    seen = set()
    filtered = [c for c in filtered if not (c in seen or seen.add(c))]
    # Remove choices that are exact repeats of recent choices
    if recent_choices:
        filtered = [c for c in filtered if c not in recent_choices[-2:]]
    # Stricter: Only allow choices referencing scene elements
    filtered = filter_choices_strict(filtered, dispatch, vision, world_prompt, recent_choices)
    # Contextual risk assessment
    filtered = filter_risky_choices(filtered, dispatch, vision)
    # Build critic prompt
    critic_prompt = (
        "You are a choice critic for an interactive story. Given the scene and choices, remove any choices that are illogical, impossible, or not grounded in the current context. "
        "If a choice is not logical, suggest a replacement that fits the scene. "
        "Do not repeat choices from the last two turns. "
        "Only allow choices that reference visible objects, characters, or threats in the current scene. "
        "If there is a threat or danger, avoid suggesting risky or aggressive actions unless contextually justified. "
        "Return a list of 2-3 final, contextually coherent choices.\n"
        f"SCENE: {dispatch}\n"
    )
    if vision:
        critic_prompt += f"VISION: {vision}\n"
    critic_prompt += f"WORLD: {world_prompt}\n"
    critic_prompt += "CHOICES:\n" + "\n".join(f"- {c}" for c in filtered)
    critic_prompt += "\nReturn only the improved list of choices, no commentary."
    # Use LLM to review and rewrite choices (don't use lore - this is mechanical choice refinement)
    try:
        improved = engine._ask(critic_prompt, temp=0.3, tokens=48, use_lore=False)
        # Parse as list
        import re
        lines = [l.strip('-* ",') for l in improved.splitlines() if l.strip()]
        # Remove any empty or duplicate lines
        seen2 = set()
        final = [l for l in lines if l and l not in seen2 and not seen2.add(l)]
        # Fallback: if LLM output is not a list, use filtered
        if not final or len(final) < 2:
            return filtered[:3]
        return final[:3]
    except Exception as e:
        print("[CHOICE CRITIC] LLM error:", e)
        return filtered[:3]

def generate_and_apply_choice(
    choice: str,
    state_path: Union[str, Path] = "world_state.json"
) -> None:
    """
    Persist the winning choice into world_state.json and bump chaos_level by 1.
    """
    path = Path(state_path)
    if path.exists():
        state = json.loads(path.read_text(encoding='utf-8'))
    else:
        state = {
            "world_prompt": "",
            "current_phase": "normal",
            "chaos_level": 0,
            "last_choice": "",
        }

    state["last_choice"] = choice
    state["chaos_level"] = int(state.get("chaos_level", 0)) + 1
    # Reset index so we hand out from the top:
    state["interim_index"] = 0
    # Persist the updated world_state
    path.write_text(json.dumps(state, indent=2), encoding='utf-8')

def categorize_choice(choice: str) -> tuple[str, str]:
    """Categorize a choice and return (category, emoji)."""
    choice_lower = choice.lower()
    # Expanded action keywords for more narrative diversity
    action_keywords = [
        "attack", "fight", "grab", "take", "use", "push", "pull", "draw", "signal", "shout", "hide", "run", "climb", "scale", "duck", "barricade", "rally", "raise", "leap", "scramble", "retreat",
        "throw", "shoot", "stab", "punch", "kick", "confront", "break", "smash", "injure", "wound", "harm", "defend", "block", "dodge", "escape", "flee", "ambush", "strike", "hit", "fire", "blast", "charge", "rush", "tackle", "choke", "wrestle", "trap", "sabotage", "destroy", "kill", "murder", "assault", "counter", "parry", "evade", "sprint", "swing", "slash", "bite", "burn", "poison", "shoot at", "fire at", "aim at", "threaten", "challenge", "face off", "stand off", "resist", "survive", "risk", "danger", "hazard", "peril", "bleed", "bleeding", "hurt", "injury", "wound", "damage", "dangerous", "perilous", "hazardous",
        # New: moral, alliance, and puzzle options
        "ally", "betray", "negotiate", "trade", "exploit", "barter", "resolve", "choose mercy", "choose violence", "make a deal", "form alliance", "break alliance", "solve puzzle", "decode", "translate", "repair", "fix", "unlock", "disarm", "bypass", "hack", "bribe", "confess", "forgive", "accuse", "protect", "sacrifice", "warn", "trust", "distrust", "question", "interrogate", "persuade", "intimidate"
    ]
    explore_keywords = [
        "explore", "search", "look", "scan", "investigate", "inspect", "trace", "survey", "observe", "peek", "scout", "enter", "search inside",
        "navigate tunnels", "ascend rooftop", "manipulate puzzle", "solve lock", "examine artifact", "study glyphs", "analyze clues"
    ]
    new_scene_keywords = [
        "leave", "exit", "move on", "go to next area", "next area", "return to hub",
        "retreat to safe zone", "engage in diplomacy", "enter truce area", "advance story", "change location"
    ]
    if any(k in choice_lower for k in explore_keywords):
        return ("explore", "🧭")
    if any(k in choice_lower for k in action_keywords):
        return ("action", "⚡")
    if any(k in choice_lower for k in new_scene_keywords):
        return ("new scene", "")
    return ("explore", "🧭")
