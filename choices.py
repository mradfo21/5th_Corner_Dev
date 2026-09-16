# choices.py – aligned with {world_state} + {dispatch}, no more KeyError
from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path
from typing import List, Union

# generate_interim_messages removed in dynamic world evolution rewrite
# Evolution summaries now stored in state["evolution_summary"]
import engine
import difflib
import game_identity

# How many options a turn hands the player. The standalone UI lays out this
# many buttons and binds number keys 1..SLATE_SIZE, so a short slate reads as
# the game running out of ideas rather than as a filter having done its job.
SLATE_SIZE = 3

# Latency of the last generate_choices() call, split into the choice LLM and
# the optional critic pass. Read by engine._advance_turn_choices_deferred_impl
# for the per-stage turn-timing log so the choices/critic split is measured
# instead of inferred. Best-effort: assumes one active turn at a time (the same
# tradeoff engine._active_session_id already documents).
LAST_CHOICE_TIMING: dict = {"choices_ms": 0, "critic_ms": 0}

# The choice slate is generated with the rendered frame attached, so it is
# already grounded in what is on screen. choice_critic() is a THIRD read of
# that same frame; skip it on the frame-attached path and lean on the
# diversity / ban / egress backstops below instead of paying another
# round-trip. Set SOMEWHERE_KEEP_CRITIC=1 to force the critic back on.
SKIP_CRITIC_WHEN_FRAME_ATTACHED = os.getenv("SOMEWHERE_KEEP_CRITIC", "") not in ("1", "true", "True")

# Overlay / HUD buttons cannot hold a sentence. The model is told 3–6 words;
# this is the hard backstop so a long clause never lands with an ellipsis.
CHOICE_MAX_WORDS = 6
CHOICE_MAX_CHARS = 40
_CHOICE_TAIL_STOP = frozenset({
    "the", "a", "an", "to", "from", "of", "into", "onto", "toward", "towards",
    "at", "for", "with", "and", "or", "your", "my",
})
_CHOICE_HEDGE_PREFIX = ("attempt to ", "try to ", "try and ")

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

# ──────────────────────────────────────────────────────────────────────────────
# "Meaningless" action detection.
#
# The single biggest complaint about generated choices is that they default to
# camera / observation / waiting actions ("photograph the scene", "look around",
# "wait and listen"). These are DEAD TURNS: they neither move the player through
# the space nor physically change anything in it, so time never meaningfully
# advances. We forbid them in the prompts AND strip them here as a hard backstop,
# because the model still slips them in.
_MEANINGLESS_LEAD_VERBS = {
    # Observation (changes nothing)
    "look", "observe", "watch", "study", "examine", "inspect", "scan",
    "survey", "peer", "gaze", "assess", "consider", "review", "eye", "scout",
    # Waiting (time stalls, world unchanged)
    "wait", "listen", "stay", "pause", "linger", "hesitate",
    # Repositioning in place (fidgeting — goes nowhere, changes nothing)
    "hunker", "cower", "flatten", "cling",
    # Camera (the player films passively on their own — never a turn choice)
    "photograph", "film", "record", "document", "zoom", "monitor",
}
# Camera cues anywhere in the text — catches camera MODEL names and "to your eye"
# style phrasing that a lead-verb check would miss (e.g. "Press your back against
# the ribs and lift the Panasonic AG-450 to your eye").
_CAMERA_MARKERS = (
    "camcorder", "camera", "photograph", "footage", "snapshot",
    "on tape", "on film", "the lens", "a picture", "pictures of",
    "panasonic", "handycam", "ag-450", "ag-4", "viewfinder",
    "to your eye", "to my eye", "get it on tape", "the tape",
)
# In-place stall phrases anywhere in the text — the body braces/anchors/presses
# but never travels and nothing in the world changes.
_STALL_MARKERS = (
    "your back against", "back against the", "press your back",
    "brace against", "to slow your", "slow your descent", "slow your fall",
    "anchor your weight", "anchor yourself", "steady yourself",
    "hold your breath", "hold your position", "hunker down",
    "hug the wall", "flatten against", "flatten yourself", "cling to",
    "cower", "to scout", "scout the", "to observe", "to inspect",
    "to examine", "to survey", "get a better look", "for a better look",
)

# Adverbs that commonly PREFIX a choice ("Carefully inspect…", "Quietly watch…"),
# hiding the real verb from a naive first-word check. We skip these (and any other
# -ly adverb) to find the ACTUAL action verb.
_LEADING_ADVERBS = {
    "carefully", "cautiously", "quietly", "silently", "slowly", "gently",
    "quickly", "warily", "tentatively", "hesitantly", "calmly", "deliberately",
    "casually", "nervously", "gingerly", "stealthily", "methodically", "intently",
    "closely", "briefly", "just", "then", "steadily", "patiently", "carefully",
}
# -ly words that are actually action VERBS, not adverbs — don't skip these.
_LY_VERBS = {"apply", "rely", "reply", "comply", "imply", "supply", "multiply", "ally"}

def _lead_action_verb(c: str) -> str:
    """The first real action verb, skipping any leading adverb(s) (explicit set
    or generic -ly words) so 'Carefully inspect the mass' resolves to 'inspect'
    (a dead turn), not 'carefully'."""
    for w in re.findall(r"[a-z]+", c):
        if w in _LEADING_ADVERBS:
            continue
        if w.endswith("ly") and w not in _LY_VERBS:
            continue
        return w
    return ""

def is_meaningless_choice(choice: str) -> bool:
    """Return True if the choice fails to ADVANCE THE ACTION — i.e. it is a
    camera, observation, waiting, or repositioning action that neither moves the
    player through the space nor physically alters it (a 'dead turn')."""
    c = (choice or "").strip().lower()
    if not c:
        return True
    # Skip any leading adverb so adverb-dressed observation ("carefully inspect",
    # "quietly study", "slowly examine") is still caught by the verb check.
    lead = _lead_action_verb(c)
    if lead in _MEANINGLESS_LEAD_VERBS:
        return True
    if any(marker in c for marker in _CAMERA_MARKERS):
        return True
    if any(marker in c for marker in _STALL_MARKERS):
        return True
    return False

def drop_meaningless_choices(choices):
    """Filter out camera/observation/waiting choices, preserving order."""
    return [c for c in choices if not is_meaningless_choice(c)]

def is_too_similar(a, b):
    """Return True if two choices are too similar (substring or high similarity)."""
    if a in b or b in a:
        return True
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return ratio > 0.75

def truncate_choice(choice, max_len=CHOICE_MAX_CHARS):
    """Clip a choice to a short command. Never ellipsis — a cut-off clause
    on the TV is worse than a slightly plainer verb+noun."""
    raw = (choice or "").replace("…", " ").replace("...", " ").strip()
    low = raw.lower()
    for hedge in _CHOICE_HEDGE_PREFIX:
        if low.startswith(hedge):
            raw = raw[len(hedge):].lstrip()
            break
    words = re.findall(r"[A-Za-z0-9']+", raw)
    if not words:
        return (choice or "").strip()

    def _join(ws):
        while ws and ws[-1].lower() in _CHOICE_TAIL_STOP:
            ws = ws[:-1]
        return " ".join(ws)

    kept = words[:CHOICE_MAX_WORDS]
    out = _join(kept)
    cap = max_len if max_len else CHOICE_MAX_CHARS
    while out and len(out) > cap and len(kept) > 3:
        kept = kept[:-1]
        out = _join(kept)
    if not out:
        out = " ".join(words[:3])
    if out and out[0].islower():
        out = out[0].upper() + out[1:]
    return out


def clip_choice_slate(choices):
    """Hard-clip a slate. Dedupes after clipping so two long clauses that
    collapse to the same verb+noun don't both survive."""
    out = []
    seen = set()
    for c in choices or []:
        t = truncate_choice(c)
        key = t.lower()
        if t and key not in seen:
            seen.add(key)
            out.append(t)
    return out

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
    n: int = SLATE_SIZE,
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
    inventory: list = None,  # Player inventory items
    overlay: str = "",
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
    # Per-stage timing (see LAST_CHOICE_TIMING). Reset up front so a fallback
    # return never reports a previous turn's split.
    _gc_start = time.time()
    LAST_CHOICE_TIMING["choices_ms"] = 0
    LAST_CHOICE_TIMING["critic_ms"] = 0

    # No longer using OpenAI client - everything uses Gemini now
    # Update the prompt to require unique, contextually grounded, and diverse choices
    prompt = prompt_tmpl.replace('2-4 words', '2-5 words').replace(
        'Suggest a consequence, risk, or emotional cue',
        'Suggest a consequence, risk, or emotional cue\n- Each choice must be unique and contextually grounded.\n- EVERY choice must MOVE the player through the space OR physically MANIPULATE something in the space, and must advance the passage of time (the world is different afterward).\n- NEVER generate camera/observation/waiting choices ("photograph", "film", "record", "look", "observe", "watch", "examine", "scan", "wait", "listen") — those are dead turns that change nothing.\n- Use a wide variety of MOTION and INTERACTION verbs: sprint, vault, climb, crawl, slip, pry, wrench, smash, drag, force, ignite, topple, tear.\n- Avoid generic or repetitive phrasing.'
    ).format(
        dispatch=last_dispatch.strip(),
        seen_elements=seen_elements,
        recent_choices=recent_choices,
        caption=caption,
        image_description=image_description or "",
        time_of_day=time_of_day or "",
        beat_nudge=beat_nudge,
        situation_summary=situation_summary,  # RE-ENABLED: This is now grounded via Vision AI in Phase 2!
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
        "Generate 3 VISCERAL, PHYSICAL ACTION CHOICES. 3 to 6 words each. Never longer.\n\n"
        "LENGTH IS HARD: if a choice would run past 6 words, cut adjectives and keep the verb + the thing. "
        "Wrong: 'Attempt to pry the jagged, shattered glass shards from the monitor'. "
        "Right: 'Pry the glass free'. Never trail off. Never use an ellipsis.\n\n"
        f"{inventory_text}"
        "🚫 PRIME DIRECTIVE: EVERY choice MUST (1) MOVE the player through the space OR physically MANIPULATE something in the space, AND (2) advance the passage of time — the world must be materially different afterward. NO exceptions.\n\n"
        "❌ ABSOLUTELY BANNED (meaningless dead turns that change nothing):\n"
        "- CAMERA actions: photograph, film, record, capture footage, raise the camcorder, snap a photo, zoom in, document\n"
        "- OBSERVATION actions: look around, observe, watch, study, examine, inspect, scan, survey, peer, gaze\n"
        "- WAITING actions: wait, listen, hold position, stay, catch your breath\n"
        "The player already films passively on their own — filming is NEVER a turn-advancing choice. Watching and waiting make the game feel stuck. If an action does not move the body or change an object, DO NOT generate it.\n\n"
        "CRITICAL: Use VIVID, PHYSICAL VERBS that emphasize what the player's BODY does:\n\n"
        "PHYSICAL BODY VERBS (PRIORITIZE THESE):\n"
        "- LEGS/FEET: Sprint, Vault, Leap, Scramble, Slide, Dive, Kick, Stomp, Brace, Plant, Launch\n"
        "- ARMS/HANDS: Grab, Yank, Wrench, Hurl, Smash, Rip, Pry, Claw, Shove, Swing, Heave\n"
        "- TORSO: Slam, Throw yourself, Barrel through, Roll, Twist, Duck, Drop, Lunge, Charge\n"
        "- FULL BODY: Hurl yourself, Fling yourself, Propel forward, Burst through, Crash into\n\n"
        "GROUNDING: Base ALL choices on the ATTACHED IMAGE and the provided IMAGE DESCRIPTION. The image and its description are the absolute source of truth for the protagonist's current position.\n\n"
        "EXAMPLES OF EXCITING CHOICES (movement + interaction, time advances):\n"
        "✅ 'Vault over chain-link fence'\n"
        "✅ 'Hurl yourself through window'\n"
        "✅ 'Sprint full-tilt toward shed'\n"
        "✅ 'Yank open rusted blast door'\n"
        "✅ 'Scramble up rocky slope'\n"
        "✅ 'Dive behind concrete barrier'\n"
        "✅ 'Wrench free the metal grate'\n"
        "✅ 'Barrel through the doorway'\n\n"
        "❌ BORING / MEANINGLESS (DO NOT USE):\n"
        "- 'Photograph the scene' / 'Film the fence' / 'Raise the camcorder'\n"
        "- 'Look around' / 'Observe the area' / 'Scan the terrain'\n"
        "- 'Wait and listen' / 'Hold position'\n"
        "- 'Go inside' / 'Move forward' / 'Check it out' / 'Approach carefully'\n\n"
        "STEALTH STILL MOVES: quiet options must still cover ground — 'Creep to the next doorway', 'Slip along the fence line', 'Crawl beneath the pipe rack'. Never a static held pose.\n\n"
        "GROUNDING: Only reference what's VISIBLE in the image, but use EXCITING physical language.\n\n"
        "MOMENTUM: Jason is ALWAYS aggressive and forward-moving. Even 'safe' choices should feel ACTIVE and DECISIVE — and always progress the situation.\n\n"
        "Make every choice feel like an ACTION MOVIE. Use words that make you FEEL the physical exertion."
    )}
    if overlay and str(overlay).strip():
        system_prompt["content"] = system_prompt["content"] + "\n\n" + str(overlay).strip()
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
        full_prompt = (
            "🚨🚨🚨 ABSOLUTE COMMAND - READ THIS FIRST 🚨🚨🚨\n\n"
            "THE ATTACHED IMAGE IS THE ONLY SOURCE OF TRUTH.\n\n"
            "⚠️ CRITICAL RULES:\n"
            "1. The image shows what is ACTUALLY visible right now from the current camera\n"
            "2. ONLY generate choices for objects/places VISIBLE in the attached image\n"
            "3. If the text mentions 'air conditioning unit' but image shows desert -> IGNORE THE TEXT, USE THE IMAGE\n"
            "4. If the text mentions 'wrench' but image shows hands/ground -> IGNORE THE TEXT, USE THE IMAGE\n"
            "5. If the text mentions 'access panel' but image shows outdoor scene -> IGNORE THE TEXT, USE THE IMAGE\n\n"
            "❌ DO NOT generate choices about:\n"
            "- Objects mentioned in text but NOT visible in image\n"
            "- Background lore or world context that isn't visually present\n"
            "- Items from previous turns that aren't in current frame\n\n"
            "✅ DO generate choices about:\n"
            "- Terrain/environment visible in image\n"
            "- Objects clearly shown in image\n"
            "- Actions possible given what's visually present\n\n"
            "The 'world_prompt' and 'dispatch' text below are BACKGROUND CONTEXT ONLY.\n"
            "They describe the overall situation, but YOU MUST PRIORITIZE WHAT'S IN THE IMAGE.\n"
            "If there's ANY conflict between text and image -> IMAGE WINS.\n\n"
            "═══════════════════════════════════════════════════════\n\n"
        ) + full_prompt

    # Cast & camera pass. This prompt is full of "Jason" and "from his eyes" —
    # both wrong the moment the player names their own character or moves the
    # camera behind them, and both of them shape what the choice slate offers.
    full_prompt = game_identity.apply(full_prompt, "narrative")

    # Build parts list (text + optional image)
    parts = [{"text": full_prompt}]

    # Whether the model actually got to LOOK at the frame, and the file it read.
    # Everything below that gates a choice against text has to know this:
    # `dispatch` and `image_description` describe the shot we ASKED the renderer
    # for, so gating on them deletes choices about what actually came back.
    frame_attached = False
    attached_frame_path = None

    # Add current timestep image if provided
    if image_url:
        print(f"[CHOICES DEBUG] Received image_url: {image_url}")
        from engine import _resolve_image_path, _sniff_image_mime

        # Session stills and live observe grabs live under
        # sessions/<id>/images/, not the legacy images/ folder. The old
        # Path("images") / basename lookup missed every observed_ frame, so
        # realtime choice-reground never attached the video the player sees.
        resolved = _resolve_image_path(str(image_url))
        actual_path = Path(resolved) if resolved is not None else Path(str(image_url).split("?", 1)[0])
        small_path = actual_path.parent / actual_path.name.replace(".png", "_small.png")
        use_path = small_path if small_path.exists() else actual_path
        
        print(f"[CHOICES DEBUG] Using file: {use_path}")
        print(f"[CHOICES DEBUG] File exists: {use_path.exists()}")
        
        if use_path.exists():
            # Compact JPEG attach (falls back to raw bytes on any decode error).
            _img_part = engine._encode_image_inline_part(use_path)
            if _img_part is None:
                with open(use_path, "rb") as f:
                    image_data = base64.b64encode(f.read()).decode('utf-8')
                _img_part = {"inlineData": {"mimeType": _sniff_image_mime(use_path), "data": image_data}}
            parts.insert(0, _img_part)
            frame_attached = True
            attached_frame_path = str(use_path)
            size_note = "(480x270)" if small_path.exists() else "(full-res)"
            print(f"[GEMINI TEXT+IMG] Including CURRENT timestep image for choices: {image_url} {size_note}")
        else:
            print(f"[CHOICES ERROR] Image file not found: {use_path}")
    
    print(f"[GEMINI TEXT] Calling {model_name} for choice generation...", flush=True)
    
    # Contextual fallback choices used whenever the LLM call/parse fails.
    # We try hard to keep the player in the game with SOMETHING actionable
    # rather than always returning generic "Look around" filler. The bot
    # tracks "[CHOICES FALLBACK]" log lines to surface upstream failures.
    def _contextual_fallback() -> List[str]:
        # Callers (e.g. api_regenerate_choices) sometimes pass None instead of
        # "" for caption/image_description/world_prompt/last_dispatch (e.g. when
        # there's no current image yet) — coerce so concatenation never raises.
        ctx = (
            (caption or "") + " " + (image_description or "") + " " +
            (world_prompt or "") + " " + (last_dispatch or "")
        ).lower()
        opts: List[str] = []
        if any(k in ctx for k in ("fence", "perimeter", "chain-link")):
            opts.append("Vault over the fence")
        if any(k in ctx for k in ("cliff", "ledge", "outcrop", "ridge", "mesa", "tower", "lookout", "hill")):
            opts.append("Scramble down the slope")
        if any(k in ctx for k in ("facility", "building", "complex", "structure", "warehouse", "lab")):
            opts.append("Advance toward the facility")
        if any(k in ctx for k in ("door", "entrance", "gate", "hatch", "opening")):
            opts.append("Push through the doorway")
        if any(k in ctx for k in ("corridor", "hallway", "passage", "tunnel")):
            opts.append("Sprint down the corridor")
        if any(k in ctx for k in ("crate", "barrel", "cover", "debris", "wall", "barrier")):
            opts.append("Shove the crate aside and push through")
        # Moving-stealth options — quiet, but the body still covers ground.
        opts.append("Creep to the next patch of cover")
        opts.append("Crawl forward into the shadows")
        # Always have more than three to choose from. A scene that matched no
        # keywords used to fall through with only the two stealth options and
        # return a slate of TWO, leaving the player a short row of buttons with
        # no explanation — the caller and the UI both expect three.
        opts.extend([
            "Vault forward over the obstacle",
            "Wrench the nearest door open",
            "Shoulder past the blockage",
            "Break for the nearest opening",
        ])
        # De-dupe while preserving order
        seen_local: set = set()
        deduped: List[str] = []
        for o in opts:
            if o.lower() not in seen_local:
                seen_local.add(o.lower())
                deduped.append(o)
        # Rotate the window rather than always serving the first three. Several
        # degraded turns in a row used to offer the identical slate every time,
        # which reads as the game having frozen even though it is still taking
        # input. Seeded by the scene, so it's stable within a situation and
        # different once the situation moves.
        if len(deduped) > 3:
            import hashlib
            offset = int(hashlib.md5(ctx.strip()[:200].encode("utf-8")).hexdigest(), 16) % len(deduped)
            deduped = [deduped[(offset + i) % len(deduped)] for i in range(len(deduped))]
        return clip_choice_slate(deduped)[:3]

    # Offline/mock backend short-circuit: when ai_provider_manager has been
    # told to use the "mock" backend (e.g. by run_local.py --mock or the
    # offline test harness), skip the network call entirely instead of
    # letting it fail/timeout. This is the only place generate_choices()
    # needs to know about the provider manager's backend override.
    if ai_provider_manager.is_mock_active("chat"):
        print("[CHOICES] Mock backend active — returning contextual fallback choices (no network).", flush=True)
        return _contextual_fallback()

    import time as _time
    # Latency/cost instrumentation: choice generation is one of the two text
    # LLM calls on the turn critical path, so record it (with the same
    # operation/service labels engine._ask uses) into the admin Analytics tab.
    # This closes the gap where only the dispatch call was measured, making the
    # true text-vs-image time split visible per model.
    _choices_t0 = _time.time()

    def _record_choices_usage(success, response_data=None, error_message=None):
        try:
            import cost_tracker
            in_tok = out_tok = None
            if response_data:
                um = response_data.get("usageMetadata", {}) or {}
                in_tok = um.get("promptTokenCount")
                out_tok = um.get("candidatesTokenCount")
            cost_tracker.record_usage(
                engine.get_active_session_id(), "text", "gemini", model_name,
                operation="choices",
                input_units=in_tok, output_units=out_tok, unit_type="tokens",
                latency_ms=int((_time.time() - _choices_t0) * 1000),
                success=success, error_message=error_message,
            )
        except Exception as _e_rec:
            print(f"[COST TRACKER] choices usage record failed (non-fatal): {_e_rec}", flush=True)

    _choices_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
    _choices_headers = {"x-goog-api-key": gemini_api_key, "Content-Type": "application/json"}
    # CRITICAL: include BLOCK_NONE safety settings so a dark dispatch (e.g. with
    # "blood", "viscera", or a graphic visual_scene) does not silently strip the
    # `parts` from the model's response and trigger a parse failure downstream.
    _choices_payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": 200},
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT",        "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH",       "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    }
    response_data = None
    for _attempt in range(2):  # one retry on 429
        try:
            response = requests.post(_choices_url, headers=_choices_headers, json=_choices_payload, timeout=20)
            print(f"[GEMINI TEXT] API returned status: {response.status_code}", flush=True)
            if response.status_code == 429 and _attempt == 0:
                print(f"[CHOICES] Rate limited (429) — retrying in 4s...", flush=True)
                _time.sleep(4)
                continue
            response.raise_for_status()
            response_data = response.json()
            print("[GEMINI TEXT] Choice generation complete", flush=True)
            _record_choices_usage(True, response_data)
            break
        except requests.exceptions.Timeout:
            print(f"[CHOICES ERROR] Gemini API timeout after 20 seconds", flush=True)
            print(f"[CHOICES FALLBACK] timeout — using contextual fallback", flush=True)
            _record_choices_usage(False, error_message="timeout")
            return _contextual_fallback()
        except requests.exceptions.HTTPError as e:
            print(f"[CHOICES ERROR] Gemini API HTTP error: {e}", flush=True)
            if hasattr(e, 'response') and e.response is not None:
                print(f"[CHOICES ERROR] Response: {e.response.text}", flush=True)
            print(f"[CHOICES FALLBACK] http-error — using contextual fallback", flush=True)
            _record_choices_usage(False, error_message=str(e))
            return _contextual_fallback()
        except Exception as e:
            print(f"[CHOICES ERROR] Unexpected error calling Gemini API: {e}", flush=True)
            import traceback
            traceback.print_exc()
            print(f"[CHOICES FALLBACK] unexpected — using contextual fallback", flush=True)
            _record_choices_usage(False, error_message=str(e))
            return _contextual_fallback()
    if response_data is None:
        print("[CHOICES ERROR] Gemini API still rate-limited after retry — using fallback", flush=True)
        print(f"[CHOICES FALLBACK] 429-retry-exhausted — using contextual fallback", flush=True)
        _record_choices_usage(False, error_message="429 rate-limited (retry exhausted)")
        return _contextual_fallback()

    # Create a mock OpenAI response object
    class GeminiResp:
        def __init__(self, text):
            self.choices = [type('obj', (object,), {'message': type('obj', (object,), {'content': text})()})]

    # Robust response parser — Gemini will sometimes return a candidates entry
    # with `finishReason: SAFETY` and NO `content.parts`, or `parts` containing
    # only a `functionCall` instead of `text`. Either case used to throw an
    # unhandled IndexError/KeyError that bubbled up through the turn guard
    # and produced fallback choices, which is what the player saw as
    # "Generating choices failed". Now we extract text defensively and fall
    # back to contextual choices if no usable text is found.
    if "candidates" not in response_data or not response_data["candidates"]:
        print(f"[CHOICES ERROR] No candidates in Gemini response: {response_data}", flush=True)
        if "error" in response_data:
            print(f"[CHOICES ERROR] Error details: {response_data['error']}", flush=True)
        if "promptFeedback" in response_data:
            print(f"[CHOICES ERROR] promptFeedback: {response_data['promptFeedback']}", flush=True)
        print(f"[CHOICES FALLBACK] no-candidates — using contextual fallback", flush=True)
        return _contextual_fallback()

    candidate0 = response_data["candidates"][0]
    finish_reason = candidate0.get("finishReason", "")
    content_obj = candidate0.get("content") or {}
    cand_parts = content_obj.get("parts") or []
    result_text = ""
    for p in cand_parts:
        if isinstance(p, dict) and isinstance(p.get("text"), str):
            result_text += p["text"]
    result_text = result_text.strip()

    if not result_text:
        # Common reasons: SAFETY block, MAX_TOKENS without text, recitation.
        print(
            f"[CHOICES ERROR] Empty result_text; finishReason={finish_reason!r}; "
            f"parts={cand_parts!r}",
            flush=True,
        )
        print(f"[CHOICES FALLBACK] empty-text ({finish_reason or 'unknown'}) — using contextual fallback", flush=True)
        return _contextual_fallback()

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
            4 < len(line) <= 160
            and not line.endswith(("...", "-", "—"))
            and " choices" not in line_lower  # Filter ANY line mentioning " choices"
            and " action choices" not in line_lower  # Specific filter for "action choices"
            and not line_lower.startswith(("scene:", "narrative:", "option:", "choice:", "here are", "here's", "here is"))
            and "for jason" not in line_lower  # Filter any meta-commentary about Jason
        ):
            clipped = truncate_choice(line)
            key = clipped.lower()
            if len(clipped) > 4 and key not in seen:
                opts.append(clipped)
                seen.add(key)
    # Stricter filtering: remove out-of-context choices. Skipped when the frame
    # was attached — the model was looking at the picture, and this gate would
    # drop a choice about a barrel that rendered in favour of one about the
    # crate we asked for and didn't get.
    if not frame_attached:
        opts = filter_choices(opts, seen_elements, recent_choices, dispatch=last_dispatch, image_description=image_description, world_prompt=world_prompt)
    # Filter out repeated choices
    # Remove any choices containing 'retreat' or 'flee' (case-insensitive)
    opts = [c for c in opts if 'retreat' not in c.lower() and 'flee' not in c.lower()]
    # Drop meaningless camera/observation/waiting choices — every choice must
    # move through or interact with the space and advance time.
    _pre_meaningless = list(opts)
    opts = drop_meaningless_choices(opts)
    if not opts and _pre_meaningless:
        print(f"[CHOICES] All options were camera/observation/waiting — using contextual fallback", flush=True)
    # Final diversity and generic filter
    opts = enforce_diversity(opts)
    opts = [c for c in opts if c.lower() not in {"photograph the chaos", "sneak past the guards", "search for hidden passage"}]
    if not opts:
        # Don't fall back to corporate language — use the contextual builder so
        # the player sees scene-appropriate, physical options.
        print(f"[CHOICES FALLBACK] parse-stripped-everything — using contextual fallback", flush=True)
        opts = _contextual_fallback()
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
    # After generating choices, run the critic. If the critic LLM throws OR
    # strips everything, KEEP the LLM-generated `opts` — they are already
    # diverse, grounded, and physical. The critic is a polish step, not a
    # gate; we cannot let it produce an empty slate on the intro turn.
    vision = image_description if image_description else ''
    recent = []
    if recent_choices:
        if isinstance(recent_choices, list):
            recent = recent_choices
        elif isinstance(recent_choices, str):
            recent = [recent_choices]
    _critic_t0 = time.time()
    if frame_attached and SKIP_CRITIC_WHEN_FRAME_ATTACHED:
        # The slate was generated with the actual rendered frame attached, so
        # it is already grounded in what is on screen. Skip the critic's third
        # read of that frame (see plan) — the diversity / ban / egress
        # backstops below still run, so a junk option is still dropped.
        print("[CHOICE CRITIC] Skipped (frame attached — slate already image-grounded)", flush=True)
        improved_choices = opts
    else:
        try:
            improved_choices = choice_critic(last_dispatch, vision, opts, world_prompt,
                                             recent_choices=recent, frame_attached=frame_attached,
                                             frame_path=attached_frame_path)
        except Exception as _critic_err:
            print(f"[CHOICE CRITIC] Crashed: {_critic_err} — keeping un-critiqued options", flush=True)
            improved_choices = opts
        if not improved_choices:
            print(f"[CHOICE CRITIC] Returned empty — keeping un-critiqued options", flush=True)
            improved_choices = opts
    LAST_CHOICE_TIMING["critic_ms"] = int((time.time() - _critic_t0) * 1000)
    # Backstop again: the critic LLM can reintroduce camera/observation choices.
    _critic_kept = drop_meaningless_choices(improved_choices)
    if _critic_kept:
        improved_choices = _critic_kept
    else:
        # Everything the critic returned was a dead turn — fall back to the
        # movement/interaction options we already had (also cleaned).
        improved_choices = drop_meaningless_choices(opts) or opts
    if not improved_choices:
        improved_choices = _contextual_fallback()
    # Critic rewrites ignore the 3–6 word contract; clip before top-up so a
    # collapsed duplicate can be replaced instead of served with an ellipsis.
    improved_choices = clip_choice_slate(improved_choices)
    # Top the slate back up to `n`. Every stage above (diversity, the
    # meaningless-choice drop, the critic's own de-duping against recent
    # turns) can REMOVE an option, and only an empty result was ever
    # refilled — so a turn that lost one option to a filter served two
    # buttons where the UI lays out three, which reads as the game running
    # out of ideas. Refill from the options we already generated first, and
    # only then from the contextual builder, so a topped-up slate still
    # belongs to this scene.
    if len(improved_choices) < n:
        for candidate in drop_meaningless_choices(opts) + _contextual_fallback():
            if len(improved_choices) >= n:
                break
            merged = enforce_diversity(improved_choices + [candidate])
            if len(merged) > len(improved_choices):
                improved_choices = merged
        if len(improved_choices) < n:
            print(f"[CHOICES] slate still short after top-up: {len(improved_choices)}/{n}", flush=True)
    improved_choices = clip_choice_slate(improved_choices)[:n]
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
    LAST_CHOICE_TIMING["choices_ms"] = int((time.time() - _gc_start) * 1000)
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

def choice_critic(dispatch, vision, choices, world_prompt, recent_choices=None,
                  frame_attached: bool = False, frame_path: str = None):
    """Polish a slate. `frame_attached` means the generator was looking at the
    rendered still, so the noun-overlap gate below is skipped: it can only
    compare against text, and the text is the render REQUEST.

    `frame_path` puts that same still in front of the critic. Judging "grounded
    in the current context" from prose alone is what made this step the drift:
    told to keep only choices that reference visible objects, and shown nothing
    but the caption we asked the renderer for, it rewrote committed actions on
    what was on screen into actions on props that never rendered."""
    # Remove placeholders and duplicates first
    filtered = [c for c in choices if c and c.strip() and c.strip() != '—']
    seen = set()
    filtered = [c for c in filtered if not (c in seen or seen.add(c))]
    # Remove choices that are exact repeats of recent choices
    if recent_choices:
        filtered = [c for c in filtered if c not in recent_choices[-2:]]
    # Stricter: Only allow choices referencing scene elements
    if not frame_attached:
        filtered = filter_choices_strict(filtered, dispatch, vision, world_prompt, recent_choices)
    # Contextual risk assessment
    filtered = filter_risky_choices(filtered, dispatch, vision)
    # Build critic prompt
    # The critic used to be briefed in isolation, so it rewrote a slate of
    # committed physical actions into "Photograph the pulsating growth" and
    # "Inspect the nearby lockers" — the two categories the generator above
    # bans outright — and was told to "avoid risky or aggressive actions",
    # which is the opposite of how this world is supposed to read. It gets the
    # house rules now, and its answer is re-filtered below rather than trusted.
    critic_prompt = (
        ("THE ATTACHED IMAGE IS THE FRAME ON SCREEN AND THE ONLY SOURCE OF TRUTH. "
         "SCENE and VISION below are background prose and may describe props that "
         "never rendered. Keep choices that act on what you can SEE; delete a "
         "choice only when its target is absent from the image. Never replace a "
         "choice with one about something the text mentions but the image does "
         "not show.\n" if frame_path else "")
        + "You are a choice critic for an interactive story. Given the scene and choices, remove any choices that are illogical, impossible, or not grounded in the current context. "
        "If a choice is not logical, suggest a replacement that fits the scene. "
        "Do not repeat choices from the last two turns. "
        "Only allow choices that reference visible objects, characters, or threats in the current scene. "
        "EVERY choice must move the body or change an object. NEVER return camera, "
        "observation or waiting actions (photograph, film, record, document, look, "
        "observe, watch, study, examine, inspect, scan, wait, listen, hold position) — "
        "those are dead turns. Danger is not a reason to soften a choice; this world "
        "is hostile and the options should stay committed. "
        "The three choices must be genuinely different things to do, not three verbs "
        "for the same movement toward the same object. "
        "Each choice MUST be 3 to 6 words, never a long clause, never an ellipsis. "
        "Return a list of exactly 3 final, contextually coherent choices.\n"
        f"SCENE: {dispatch}\n"
    )
    if vision:
        critic_prompt += f"VISION: {vision}\n"
    # A 48-token answer does not need the whole world document; the scene and
    # the vision line are what "grounded in the current context" means here.
    world_brief = " ".join(str(world_prompt or "").split())[:1200]
    if world_brief:
        critic_prompt += f"WORLD: {world_brief}\n"
    critic_prompt += "CHOICES:\n" + "\n".join(f"- {c}" for c in filtered)
    critic_prompt += "\nReturn only the improved list of choices, no commentary."
    # Use LLM to review and rewrite choices (don't use lore - this is mechanical choice refinement)
    try:
        improved = engine._ask(critic_prompt, temp=0.3, tokens=48, use_lore=False,
                               image_path=frame_path)
        # Parse as list
        import re
        lines = [l.strip('-* ",') for l in improved.splitlines() if l.strip()]
        # Remove any empty or duplicate lines
        seen2 = set()
        final = [l for l in lines if l and l not in seen2 and not seen2.add(l)]
        # The critic's slate goes back through the bans. Returning it raw is how
        # a filtered slate got observation choices put back into it, which the
        # engine then dropped again and backfilled with generic movement verbs —
        # the reason three near-identical "go to the crane" options kept showing
        # up on the same turn.
        final = drop_meaningless_choices(final)
        if not final or len(final) < 2:
            return clip_choice_slate(filtered)[:3]
        return clip_choice_slate(final)[:3]
    except Exception as e:
        print("[CHOICE CRITIC] LLM error:", e)
        return clip_choice_slate(filtered)[:3]

def generate_and_apply_choice(
    choice: str,
    state_path: Union[str, Path] = "world_state.json"
) -> None:
    """
    Persist the winning choice into world_state.json.

    This used to also do `chaos_level += 1`, which made chaos a second turn
    counter: it rose on every turn regardless of what the turn contained, and
    never fell. engine.apply_chaos owns the dial now and moves it by what
    actually happened (see the note above CHAOS_MAX).
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
