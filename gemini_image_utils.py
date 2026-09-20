"""
gemini_image_utils.py - Official Google Gemini Image Generation (Nano Banana)
Uses Gemini 2.5 Flash Image for ultra-fast, high-quality image generation
"""

import json
import requests
import base64
from pathlib import Path
from typing import Optional

# Load config
import os
ROOT = Path(__file__).parent
# Load config from file if it exists, otherwise use empty dict (for Render deployment)
try:
    with open(ROOT / "config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
except FileNotFoundError:
    config = {}

# Load prompts — shared, hot-reloadable singleton (see prompts_store.py) so
# edits made through the World Studio editor apply immediately, no restart.
from prompts_store import PROMPTS
import prompts_store

# Cast sheet — the active camera perspective decides whether the player's own
# character belongs in frame, which flips both the anti-person prompt blocks
# below and the POV hand-stripping post-process. See game_identity.py.
import game_identity

# Read from environment variables first, fall back to config.json
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", config.get("GEMINI_API_KEY", ""))
IMAGE_DIR = Path("images")


def write_image_atomic(path, data: bytes) -> None:
    """Write an image so a reader can never see a partial one.

    Writing straight to the final name publishes the file the instant it is
    created and then fills it in, and the client fetches a scene the moment
    the feed item naming it arrives — so a big still could be served
    truncated. The browser then fails to decode it and paints the scene
    layer's background colour instead: a black screen under a live HUD.
    Write beside the target and rename, which is atomic on Windows and POSIX.
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(path))


def save_pil_atomic(img, path, **kwargs) -> None:
    """Same guarantee for the PIL-encoded derivatives."""
    path = Path(path)
    tmp = path.with_name(path.name + ".part")
    img.save(tmp, **kwargs)
    os.replace(str(tmp), str(path))

# CRITICAL DEBUG: Log API key status at import time
if not GEMINI_API_KEY:
    print("[GEMINI INIT] CRITICAL: GEMINI_API_KEY is NOT SET! Images will not generate!")
    print(f"[GEMINI INIT] Environment variable: {os.getenv('GEMINI_API_KEY', 'NOT SET')}")
    print(f"[GEMINI INIT] Config.json value: {config.get('GEMINI_API_KEY', 'NOT SET')}")
else:
    print(f"[GEMINI INIT] GEMINI_API_KEY loaded: {GEMINI_API_KEY[:20]}...{GEMINI_API_KEY[-8:]}")
    print(f"[GEMINI INIT] Ready to generate images")

# Google Gemini models
GEMINI_FLASH_IMAGE = "gemini-3.1-flash-lite-image"  # Fast, cost-effective image generation
GEMINI_PRO_IMAGE = "gemini-3.1-flash-image"  # Slower, higher quality, 4K support


def resolve_model() -> str:
    """The Gemini image model this call should use.

    Every generation used to be pinned to GEMINI_FLASH_IMAGE at 1K right here,
    which made `image_model` in ai_config.json decorative: presets could name a
    model, the status API would report it, and the wire request ignored it. That
    is fine while speed is the only goal and actively wrong for a render, where
    the entire point is spending time to see how far the picture can be pushed.
    The config is now the source of truth, with the fast model as the fallback
    so a missing or non-Gemini setting still generates something.
    """
    try:
        import ai_provider_manager
        if ai_provider_manager.get_image_provider() != "gemini":
            return GEMINI_FLASH_IMAGE
        entry = ai_provider_manager.find_model("image", ai_provider_manager.get_image_model())
        if entry and entry.get("provider") == "gemini":
            return entry["id"]
    except Exception as e:
        print(f"[GEMINI] model lookup failed ({e}); using {GEMINI_FLASH_IMAGE}", flush=True)
    return GEMINI_FLASH_IMAGE


# A frame the player cannot see is a failed frame, whatever the prose said.
#
# Observed: a turn moved the player into a drainage culvert and the render came
# back 88% near-black (mean luma 10 of 255). The run did not stop — it carried on
# generating from that frame — and the damage was not only that nobody could see
# it: SCAN found no objects in it, so the next turn could not be committed at all.
# A dark scene is the point of this game; an unreadable one is a bug.
#
# Stated as a build-level rule rather than in the world's own art direction,
# because it holds for every world and no author should have to remember it.
LEGIBILITY_RULE = (
    "\n\nEXPOSURE — THIS FRAME MUST BE READABLE:\n"
    "Dark, low-light and night are welcome; an unreadable frame is not. Whatever "
    "the scene is, expose it so a viewer can see what and where it is: keep a "
    "practical light source, a sky, an opening, a reflection or a bounce in shot, "
    "and keep the subject and the space separable from the background. "
    "NOT a black frame. NOT a nearly black frame. NOT an underexposed murk with "
    "no legible geometry. If the described place would truly be pitch dark, light "
    "it the way a 1993 film crew would have — available practical light, a torch, "
    "a doorway, a work lamp — rather than delivering darkness."
)


def resolve_image_size() -> str:
    """Output resolution, clamped to what the selected model actually offers."""
    try:
        import ai_provider_manager
        want = ai_provider_manager.get_image_size()
        entry = ai_provider_manager.find_model("image", ai_provider_manager.get_image_model())
        allowed = (entry or {}).get("sizes") or ["1K"]
        return want if want in allowed else allowed[-1]
    except Exception:
        return "1K"


# Gemini imageConfig.aspectRatio values this build will send. 21:9 is the
# closest official preset to a landscape phone; anything else falls back.
_GEMINI_ASPECT_RATIOS = ("16:9", "21:9", "4:3", "3:4", "1:1", "9:16", "3:2", "2:3", "5:4", "4:5")


def resolve_aspect_ratio(requested: str = None) -> str:
    """Aspect ratio for the wire request: caller override, else ai_config."""
    try:
        import ai_provider_manager
        want = requested or ai_provider_manager.get_image_aspect_ratio()
        mapped = ai_provider_manager.normalize_aspect_ratio(want) or str(want or "").strip()
        if mapped in _GEMINI_ASPECT_RATIOS:
            return mapped
    except Exception:
        pass
    raw = str(requested or "").strip()
    return raw if raw in _GEMINI_ASPECT_RATIOS else "4:3"

# Hard ceiling on the assembled prompt.
#
# This used to be 5,000 characters, which was quietly destructive: the t2i
# template alone was 9,281 chars and the i2i one 13,466, so the back HALF of
# every prompt — the "what is in frame" list, the no-text/no-border bans, and
# the optical-reality anchor appended after them — was cut off before the
# request was ever sent. Editing those sections had no effect on the image.
#
# Gemini's image models take a text part far larger than this (tens of
# thousands of tokens), so the cap is a sanity bound rather than a real
# constraint. Deduplicating the two templates (see prompts_store) also brought
# the assembled prompt back under it.
#
# ...and it has since drifted back over: a typed-action playtest logged a 28,409
# char prompt against a 24,000 cap, quietly dropping 4,400 characters of exactly
# the sections named above. The camera contract is stamped on immediately before
# this cut (game_identity.apply, below), and it is the block that decides whether
# a player who typed "get in the truck and drive" is drawn in the cab or standing
# back on his feet beside it — so the tail going missing reads, from the sofa, as
# the game ignoring what you typed.
#
# Raising the bound rather than trimming the prompt: the sections are wanted, the
# model accepts them, and a cap that truncates in silence is worse than no cap.
# Keep the warning — it is what caught this the second time.
MAX_PROMPT_CHARS = 60000

# Track last corrected image for continuity
_last_corrected_image = None

# ============================================================================
# FORWARD MOMENTUM ZOOM - DISABLED: Was causing visual discontinuity
# ============================================================================
# By cropping center and scaling up, we force AI to interpret rather than copy pixels
# This creates a subtle "stepping forward" effect and reduces compression artifacts
# DISABLED: Caused jarring composition changes, breaking visual continuity
ENABLE_FORWARD_ZOOM = False   # Disabled to fix discontinuity issues
ZOOM_FACTOR = 1.10            # Reduced from 1.35 (if re-enabled, use subtle zoom only)
# ============================================================================

# ============================================================================
# IMG2IMG REFERENCE IMAGE QUALITY - Toggle between full-res and downsampled
# ============================================================================
# True  = Use downsampled (480x360) - Faster, less bandwidth, MAY reduce quality
# False = Use full-res (1184x864) - Slower, more bandwidth, preserves quality
USE_DOWNSAMPLED_FOR_IMG2IMG = True   # Set to False to use full-res references
# ============================================================================

def _sanitize_for_safety(prompt: str) -> str:
    """
    Sanitize prompts to avoid Gemini safety blocks while keeping creative intent.
    Replace graphic terms with clinical/abstract equivalents.
    """
    # Map graphic terms to safer equivalents
    replacements = {
        # Violence terms
        "blood": "red liquid",
        "bleeding": "leaking",
        "gore": "visceral damage",
        "guts": "internal matter",
        "viscera": "biological material",
        "mutilated": "severely damaged",
        "dismembered": "separated",
        "decapitated": "severed",
        "flesh": "tissue",
        "wound": "injury site",
        "gunshot": "ballistic impact",
        "stabbed": "pierced",
        "slashed": "cut deeply",
        "torn": "separated",
        "ripped": "forcibly separated",
        "shredded": "fragmented",
        
        # Attack/impact terms (timeout penalties)
        "crushes": "compresses",
        "crushing": "compressing",
        "crushed": "compressed",
        "pressure": "force",
        "mauls": "attacks",
        "mauling": "attacking",
        "mauled": "attacked",
        "tears into": "impacts",
        "tearing into": "impacting",
        "engulfs": "surrounds",
        "engulfing": "surrounding",
        "engulfed": "surrounded",
        "unloads into": "strikes",
        "opens fire": "discharges weapon",
        "shoots": "fires at",
        "shooting": "firing at",
        "shot": "fired at",
        "claws": "appendages",
        "teeth": "dental structures",
        "jaws": "mouth structures",
        
        # Body horror terms
        "bone": "skeletal structure",
        "skull": "cranial structure",
        "organs": "biological systems",
        "intestines": "digestive tract",
        "dissected": "anatomically exposed",
        "mutated": "physically altered",
        "mutations": "physical alterations",
        "deformities": "structural irregularities",
        "tumors": "structural growths",
        "autopsy": "medical examination",
        "blood-crusted": "darkly stained",
        
        # Death terms
        "dead body": "motionless figure",
        "corpse": "remains",
        "killed": "neutralized",
        "dying": "critically injured",
        "death": "cessation",
        
        # Extreme descriptors
        "brutal": "severe",
        "violent": "forceful",
        "graphic": "detailed",
        "gruesome": "disturbing",
        
        # Horror/psychological terms (trigger safety filter)
        "watching": "observing",
        "watched": "observed",
        "watching you": "present",
        "being watched": "under observation",
        "unsettling eye": "circular mark",
        "stylized eye": "circular symbol",
        "eye symbol": "circular marking",
        "dilating": "changing",
        "dilates": "changes",
        "pupil": "center",
        "gaze": "focus",
        "staring": "looking",
        "screams": "sounds",
        "screaming": "vocalizing",
        "scream": "sound",
        "shriek": "noise",
        "horror": "unease",
        "terror": "fear",
        "terrifying": "unsettling",
        "horrifying": "disturbing",
        "nightmare": "bad dream",
        "madness": "confusion",
        "insanity": "mental distress",
        "psychological": "mental",
        "haunted": "occupied",
        "possessed": "influenced",
        "demonic": "otherworldly",
        "evil": "negative",
        "malevolent": "hostile",
        "sinister": "ominous",
        "dread": "unease",
        "panic": "alarm",
    }
    
    sanitized = prompt
    replacements_made = []
    for unsafe, safe in replacements.items():
        # Case-insensitive replacement
        import re
        pattern = re.compile(re.escape(unsafe), re.IGNORECASE)
        if pattern.search(sanitized):
            replacements_made.append(f"{unsafe}->{safe}")
            sanitized = pattern.sub(safe, sanitized)
    
    if replacements_made:
        print(f"[SAFETY SANITIZE] Replaced {len(replacements_made)} terms to avoid content filter")
        # Only log first few replacements to avoid spam
        if len(replacements_made) <= 5:
            print(f"[SAFETY SANITIZE] Changes: {', '.join(replacements_made)}")
    
    return sanitized

def generate_with_gemini(
    prompt: str,
    caption: str,
    world_prompt: str = None,
    aspect_ratio: str = None,
    model: str = None,
    time_of_day: str = "",
    is_first_frame: bool = False,
    action_context: str = "",
    hd_mode: bool = True,
    output_dir: Path = None,
    portrait_mode: bool = False,
    object_subject: bool = False,
    spec: dict | None = None,
    image_size: str | None = None,
) -> str:
    """
    Generate an image using Google Gemini (Nano Banana).
    
    Args:
        prompt: The full image generation prompt WITH ALL DETAILED INSTRUCTIONS
        caption: Short caption for the image (used for filename)
        world_prompt: Narrative world state context
        aspect_ratio: Aspect ratio ("16:9", "21:9", "4:3", "1:1", etc.).
            None reads the current renderer setting (see resolve_aspect_ratio).
        model: Gemini model to use (can be overridden by hd_mode)
        time_of_day: Time of day for lighting consistency
        hd_mode: If True, use Pro model for higher quality (slower). If False, use Flash for speed.
        portrait_mode: When True, skip the anti-person / environment-only
            constraints and emit a cinematic character medium-shot instead.
            Used by Conversation Moments (/api/talk/portrait).
        object_subject: When True with ``portrait_mode``, the subject is a
            machine/object (monitor, radio). Emit a close-up of THAT object
            and keep the anti-person rule so the model cannot invent a face.
        
    Returns:
        Local path to the saved image (e.g., "/images/filename.png")
    """
    # CRITICAL: Ensure caption is safe for all operations (filename, logging, etc.)
    try:
        # Safely encode caption to ASCII to prevent Unicode errors in Windows console
        caption = caption.encode('ascii', 'ignore').decode('ascii')
        print(f"[GEMINI IMG] generate_with_gemini() CALLED - caption: {caption[:50]}", flush=True)
    except:
        caption = "image"  # Fallback if encoding fails
        print(f"[GEMINI IMG] generate_with_gemini() CALLED - caption contains special characters", flush=True)
    print(f"[GEMINI IMG] API key available: {bool(GEMINI_API_KEY)}, length: {len(GEMINI_API_KEY) if GEMINI_API_KEY else 0}", flush=True)
    
    if not GEMINI_API_KEY:
        print("[GEMINI IMG] FATAL: No API key! Cannot generate image!")
        return None
    if not GEMINI_API_KEY or not GEMINI_API_KEY.strip():
        raise ValueError(
            "ERROR: Google Gemini API key not configured!\n"
            "Get your key at: https://aistudio.google.com/apikey\n"
            "Add it to config.json as GEMINI_API_KEY"
        )
    
    # Which model and how big come from ai_config.json (see resolve_model)
    # unless a caller names one — the overload fallback below is the only thing
    # that does, to step down to Flash when the heavier model is busy. The
    # hd_mode arg is retained for call-site compatibility but no longer selects
    # a model, so a render raises the ceiling for every frame at once.
    model = model or resolve_model()
    # A named size is honoured the same way the img2img path honours it (see
    # the long note beside `selected_size` there): a GRID render slices every
    # panel out of one generation, so a 2x2 at the play setting of 1K yields
    # 672x376 panels and the establishing shots come back soft. The opening
    # montage is the only text-to-image caller that asks.
    if image_size:
        import ai_provider_manager
        want = str(image_size).strip().upper()
        allowed = ((ai_provider_manager.find_model("image", model) or {})
                   .get("sizes") or list(ai_provider_manager.IMAGE_SIZES))
        if want in allowed:
            image_size = want
        else:
            image_size = allowed[-1]
            print(f"[GOOGLE GEMINI] {model} does not offer {want}; using "
                  f"{image_size} (offers {allowed})", flush=True)
    else:
        image_size = resolve_image_size()
    print(f"[GOOGLE GEMINI] Using {model} @ {image_size}")
    
    # Load prompt template from JSON (single source of truth!)
    # Renders the template plus the shared art-direction / camera-rules blocks
    # (see prompts_store.render_image_template) so the world only has to be
    # directed in one place.
    structured_prompt = prompts_store.render_image_template("gemini_text_to_image_instructions", prompt)
    structured_prompt = structured_prompt + LEGIBILITY_RULE
    
    # Inject time/weather/mood if provided
    if time_of_day:
        time_injection = f"\n\nLighting: {time_of_day}.\n"
        structured_prompt = structured_prompt + time_injection
    
    # Three ways a person can relate to the frame:
    #   portrait_mode — Conversation Moments; the SUBJECT is a character.
    #   hero_mode     — the player picked a third-person camera, so their own
    #                   character is the subject and the anti-person rule is
    #                   the exact opposite of what they asked for.
    #   neither       — first person; the shipped environment-only rule holds.
    hero_mode = (not portrait_mode) and game_identity.shows_character(spec)
    if hero_mode:
        structured_prompt = structured_prompt + (
            "\n\nCRITICAL - THE PLAYER CHARACTER IS IN THIS SHOT:\n"
            "This is NOT an empty environment plate. The character the player controls is "
            "ON SCREEN and is the subject of the frame — see the CAMERA DIRECTIVE above for "
            "exactly where they sit in the composition. An image with no character in it is "
            "a failed render. Ignore any instruction below that demands an empty scene, a "
            "fixed security-camera view, or zero human presence."
        )
    elif not portrait_mode:
        anti_person = (
            "\n\nNo person in frame: no head, shoulders, back, hands, or silhouette. "
            "Show only the environment."
        )
        structured_prompt = structured_prompt + anti_person
    elif object_subject:
        portrait_anchor = (
            "\n\nCINEMATIC OBJECT CLOSE-UP:\n"
            "This is a stylish cinematic CLOSE-UP of the object described in the prompt.\n"
            "The object fills the frame. Same materials, same wear, same light.\n"
            "Do NOT invent a person, face, figure, or human. The object IS the subject.\n"
            "Keep 1993 analog-horror palette continuity (muted, slightly degraded film stock).\n"
            "NOT a character portrait. NOT a security camera POV. NOT a wide environment plate.\n"
        )
        structured_prompt = structured_prompt + portrait_anchor
    else:
        portrait_anchor = (
            "\n\nCINEMATIC PORTRAIT MODE:\n"
            "This is a stylish cinematic MEDIUM SHOT of the character described in the prompt.\n"
            "Frame from mid-torso up, shallow depth of field, 35mm film look, dramatic rim lighting.\n"
            "The SUBJECT IS THE FOCUS — show their face/figure clearly. Soft bokeh background.\n"
            "Keep 1993 analog-horror palette continuity (muted, slightly degraded film stock).\n"
            "NOT a security camera POV. NOT a wide environment plate. NOT a selfie.\n"
        )
        structured_prompt = structured_prompt + portrait_anchor
    if not portrait_mode:
        structured_prompt = structured_prompt + game_identity.keep_place_instruction(spec)

    # Mode-specific "don't do this" — look and overlays live in image_art_direction
    # / image_camera_rules. Naming REC / timecode / VHS HUD here made the model
    # draw a viewfinder.
    if portrait_mode and object_subject:
        negative_emphasis = (
            "\n\nNot a person, not a face, not a figure, not a wide establishing shot."
        )
    elif portrait_mode:
        negative_emphasis = (
            "\n\nNot a wide establishing shot, not a full-body distant figure, "
            "not an empty room, not a security-camera angle."
        )
    else:
        negative_emphasis = ""
        if not hero_mode:
            negative_emphasis = (
                "\n\nNo person in frame: no head, shoulders, back, hands, or silhouette."
            )

    structured_prompt = structured_prompt + negative_emphasis

    # Cast & camera reconciliation. `prompt` already arrives stamped with the
    # camera directive from engine.build_image_prompt(); this pass rewrites the
    # first-person wording baked into the JSON template and the constant blocks
    # assembled around it. "raw" so we don't stack a second directive.
    if not portrait_mode:
        structured_prompt = game_identity.apply(structured_prompt, "raw", spec)

    # Sanity bound only — see MAX_PROMPT_CHARS. Warn loudly if we ever hit it,
    # because silently dropping the tail of a prompt is invisible from the image.
    if len(structured_prompt) > MAX_PROMPT_CHARS:
        print(f"[GEMINI IMG] WARNING: prompt is {len(structured_prompt)} chars; "
              f"truncating to {MAX_PROMPT_CHARS}. The tail will not reach the model.", flush=True)
        structured_prompt = structured_prompt[:MAX_PROMPT_CHARS]
    
    # Sanitize to avoid safety blocks
    full_prompt = _sanitize_for_safety(structured_prompt)
    
    try:
        print(f"[GOOGLE GEMINI] Generating with {model}...")
        # Safe print - encode to ASCII, replace non-ASCII chars
        safe_prompt = full_prompt[:200].encode('ascii', 'replace').decode('ascii')
        print(f"[GOOGLE GEMINI] Prompt: {safe_prompt}...")
    except UnicodeEncodeError:
        print(f"[GOOGLE GEMINI] Generating with {model}... (prompt contains special characters)")
    except Exception as print_err:
        print(f"[GOOGLE GEMINI] Generating (logging error: {type(print_err).__name__})")
    
    # Official Google Gemini API endpoint
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    
    headers = {
        "x-goog-api-key": GEMINI_API_KEY,
        "Content-Type": "application/json"
    }
    
    # Lowest resolution the Lite model offers (1K) — fastest generation.
    # Portrait Moments prefer a wider cinematic frame unless the caller asked
    # for something else; environment stills follow the renderer setting.
    _ar = resolve_aspect_ratio(aspect_ratio)
    if portrait_mode and (not aspect_ratio or aspect_ratio == "4:3"):
        _ar = "16:9"
    image_config = {"aspectRatio": _ar, "imageSize": image_size}
    
    payload = {
        "contents": [{
            "parts": [
                {"text": full_prompt}
            ]
        }],
        "generationConfig": {
            "responseModalities": ["IMAGE"],  # Image only, no text
            "imageConfig": image_config
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ]
    }
    
    try:
        # Make the request with REDUCED timeout (30s) to prevent death sequence hangs
        # Gemini Pro can hang indefinitely on some prompts, especially death scenes
        max_retries = 1  # Reduced from 2 - don't waste time retrying slow calls
        for attempt in range(max_retries):
            try:
                print(f"[GOOGLE GEMINI] Sending API request (attempt {attempt + 1})...", flush=True)
                response = requests.post(api_url, headers=headers, json=payload, timeout=30)
                print(f"[GOOGLE GEMINI] Got response, status: {response.status_code}", flush=True)
                response.raise_for_status()
                break
            except requests.exceptions.Timeout:
                if attempt < max_retries - 1:
                    print(f"[GOOGLE GEMINI] Timeout on attempt {attempt + 1}, retrying...")
                    continue
                else:
                    print(f"[GOOGLE GEMINI] ERROR: TIMEOUT after 30s - Gemini API not responding!")
                    return None  # Graceful fallback instead of crash
        
        print(f"[GOOGLE GEMINI] Parsing JSON response...", flush=True)
        result = response.json()
        
        # Extract base64 image data from response
        print(f"[GOOGLE GEMINI] Extracting image from response...", flush=True)
        if "candidates" not in result or not result["candidates"]:
            raise RuntimeError(f"No candidates in Gemini response: {result}")
        
        parts = result["candidates"][0]["content"]["parts"]
        image_data_b64 = None
        
        for part in parts:
            if "inlineData" in part:
                image_data_b64 = part["inlineData"]["data"]
                break
        
        if not image_data_b64:
            raise RuntimeError(f"No image data in Gemini response: {result}")
        
        # Decode base64 image
        print(f"[GOOGLE GEMINI] Decoding base64 image...", flush=True)
        image_bytes = base64.b64decode(image_data_b64)
        print(f"[GOOGLE GEMINI] Decoded {len(image_bytes)} bytes", flush=True)
        
        # Save to local storage - use provided output_dir or fall back to IMAGE_DIR
        save_dir = output_dir if output_dir is not None else IMAGE_DIR
        save_dir.mkdir(parents=True, exist_ok=True)
        
        safe_caption = "".join(c if c.isalnum() or c in "_-" else "_" for c in caption[:48])
        filename = f"{hash(caption) & 0xFFFFFFFF}_{safe_caption}.png"
        image_path = save_dir / filename
        
        write_image_atomic(image_path, image_bytes)
        
        # Downsample for API calls (maintain 4:3 aspect ratio) - do this ONCE, not per API call
        # Smaller = faster uploads, and Gemini Flash doesn't need high-res for text/vision tasks
        from PIL import Image as PILImage
        import io
        small_filename = filename.replace(".png", "_small.png")
        small_path = save_dir / small_filename
        
        try:
            img = PILImage.open(io.BytesIO(image_bytes))
            img = img.convert("RGB")
            img = img.resize((480, 360), PILImage.LANCZOS)  # 4:3 aspect ratio (matches full-size)
            save_pil_atomic(img, small_path, format="PNG", optimize=True, quality=85)
            print(f"[GOOGLE GEMINI] Image saved: {image_path} ({len(image_bytes)} bytes)")
            print(f"[GOOGLE GEMINI] Downsampled saved: {small_path} (480x360, 4:3 for API calls)")
        except Exception as e:
            print(f"[GOOGLE GEMINI] WARNING: Downsample failed: {e}")
        
        # Return full path (will be made relative by caller if needed)
        return str(image_path)
        
    except requests.exceptions.HTTPError as e:
        # A heavier model being busy shouldn't cost the run its opening frame —
        # step down to Flash once. Keyed on "not already Flash" rather than one
        # named model, so it still fires for whatever the render is configured to.
        if e.response.status_code == 503 and is_first_frame and model != GEMINI_FLASH_IMAGE:
            print(f"[GOOGLE GEMINI] WARNING: {model} overloaded, falling back to Flash for first frame...")
            return generate_with_gemini(prompt, caption, world_prompt, aspect_ratio, GEMINI_FLASH_IMAGE, time_of_day, is_first_frame=False)
        
        if e.response.status_code == 401 or e.response.status_code == 403:
            print(f"[GOOGLE GEMINI] ERROR: Authentication failed! Check your API key.")
            print(f"   Get your key at: https://aistudio.google.com/apikey")
        elif e.response.status_code == 429:
            print(f"[GOOGLE GEMINI] ERROR: Rate limit exceeded!")
        else:
            print(f"[GOOGLE GEMINI] ERROR: HTTP Error {e.response.status_code}: {e.response.text}")
        raise
    except Exception as e:
        print(f"[GOOGLE GEMINI] ERROR: Unexpected error: {e}")
        print(f"[GOOGLE GEMINI] ERROR: Returning None to allow game to continue")
        return None  # Graceful fallback - don't crash the death sequence!


def _apply_fps_hands_compositing(corrected_path, small_corrected_path, action_context=""):
    """
    Add FPS-style hands to the image to make it feel like an interactive video game.
    Hands are context-aware based on the action being performed.
    """
    try:
        import base64
        
        # Determine hand pose based on action context
        hand_guidance = ""
        if "photograph" in action_context.lower() or "camera" in action_context.lower() or "zoom" in action_context.lower():
            hand_guidance = "Hands holding a 1990s film camera (like a Nikon F3 or Canon AE-1), fingers on shutter button and zoom ring"
        elif "climb" in action_context.lower() or "vault" in action_context.lower() or "grab" in action_context.lower():
            hand_guidance = "Hands reaching forward, fingers spread, grasping motion"
        elif "crouch" in action_context.lower() or "duck" in action_context.lower():
            hand_guidance = "Hands in low defensive position, slightly raised"
        elif "run" in action_context.lower() or "sprint" in action_context.lower():
            hand_guidance = "Hands pumping in running motion, slightly blurred"
        else:
            hand_guidance = "Empty hands in relaxed exploration pose, visible from wrist to fingertips"
        
        compositing_prompt = (
            f"Add realistic FPS video game hands to this image:\n\n"
            f"HANDS POSITION: {hand_guidance}\n\n"
            "STYLE:\n"
            "- Realistic, detailed hands (not cartoon or stylized)\n"
            "- Dirty, worn, weathered appearance (1993 grunge aesthetic)\n"
            "- Proper FPS positioning: lower third of frame, hands visible from wrists to fingertips\n"
            "- Natural lighting that matches the scene\n"
            "- Weathered enough to match the scene\n\n"
            "COMPOSITION:\n"
            "- Hands should look like part of a first-person video game (Metro, Half-Life, Far Cry style)\n"
            "- Seamless integration with the environment\n"
            "- Maintain all scene elements behind the hands\n"
            "- Keep the gritty 1993 atmosphere\n\n"
            "DO NOT:\n"
            "- Make hands too large or too small\n"
            "- Add unrealistic poses\n"
            "- Clean up the image\n"
            "- Change the background scene\n\n"
            "Think: Photorealistic FPS game hands like Metro Exodus or Escape from Tarkov, matching the scene."
        )
        
        # Read the corrected image
        with open(small_corrected_path, "rb") as f:
            img_bytes = f.read()
        img_b64 = base64.b64encode(img_bytes).decode('utf-8')
        
        parts = [
            {"text": compositing_prompt},
            {
                "inlineData": {
                    "mimeType": "image/png",
                    "data": img_b64
                }
            }
        ]
        
        # Deliberately stays on the fast model regardless of render settings:
        # this is a corrective pass over an already-downsampled copy, so paying
        # Pro rates here buys nothing the finished frame can show.
        api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_FLASH_IMAGE}:generateContent"
        
        headers = {
            "x-goog-api-key": GEMINI_API_KEY,
            "Content-Type": "application/json"
        }
        
        payload = {
            "contents": [{
                "parts": parts
            }],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {
                    "aspectRatio": resolve_aspect_ratio()
                }
            }
        }
        
        response = requests.post(api_url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        
        result = response.json()
        if "candidates" not in result or not result["candidates"]:
            print("[FPS COMPOSITING] No result, using corrected image")
            return None
        
        # Safely access parts with error handling
        try:
            image_b64 = result["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
        except (KeyError, IndexError) as e:
            print(f"[FPS COMPOSITING] Error extracting image: {e}, response: {result}")
            return None
        composited_bytes = base64.b64decode(image_b64)
        
        # Save composited version with _fps suffix
        from pathlib import Path
        corrected_pathobj = Path(corrected_path)
        fps_filename = corrected_pathobj.stem.replace("_corrected", "") + "_fps" + corrected_pathobj.suffix
        fps_path = corrected_pathobj.parent / fps_filename
        
        write_image_atomic(fps_path, composited_bytes)
        
        # Create small version too
        from PIL import Image as PILImage
        import io
        img = PILImage.open(io.BytesIO(composited_bytes))
        img = img.convert("RGB")
        img = img.resize((480, 360), PILImage.LANCZOS)  # 4:3 aspect ratio
        
        small_fps_filename = fps_filename.replace(".png", "_small.png")
        small_fps_path = corrected_pathobj.parent / small_fps_filename
        img.save(small_fps_path, format="PNG", optimize=True, quality=85)
        
        print(f"[FPS COMPOSITING] Added hands: {fps_filename}")
        return str(Path("images") / fps_filename)
        
    except Exception as e:
        print(f"[FPS COMPOSITING] Failed: {e}, using corrected image")
        return None


def _apply_pov_correction(original_path, small_path, previous_corrected_path=None):
    """
    Apply POV correction pass to remove foreground limbs/objects and ensure first-person perspective.
    This runs as a post-process after every image generation.
    """
    try:
        import base64
        
        correction_prompt = (
            "Adjust this image to pure first-person perspective:\n\n"
            "REMOVE from the image:\n"
            "- Any foreground hands, arms, legs, feet (camera operator's body parts)\n"
            "- Any foreground held objects (guns, tools, items being carried)\n"
            "- Any character silhouette or body in the foreground\n"
            "- Photo borders, black borders, white borders, letterbox bars, frame edges\n"
            "- Polaroid frames, picture frames, matting\n\n"
            "KEEP borders ONLY if they are:\n"
            "- Binocular view (figure-8 dual circles)\n"
            "- Scope/rifle view (circular reticle)\n"
            "- Night vision goggles view (circular green overlay)\n"
            "- Gas mask view (rounded rectangular with breathing filters visible)\n"
            "These are intentional viewing devices - keep their characteristic frames.\n\n"
            "KEEP in the image:\n"
            "- All distant people (guards, enemies, figures in background) - these are fine\n"
            "- The scene composition and environment\n"
            "- All lighting, atmosphere, and photographic look\n"
            "- Background action and details\n\n"
            "PRESERVE:\n"
            "- Muted, desaturated color palette\n"
            "- Gritty, raw, unpolished photographic quality\n\n"
            "The image should fill edge-to-edge UNLESS it's a viewing device frame.\n"
            "Think: a photoreal environmental view with no camera operator in the foreground."
        )
        
        # Read the current image to correct
        with open(small_path, "rb") as f:
            img_bytes = f.read()
        img_b64 = base64.b64encode(img_bytes).decode('utf-8')
        
        parts = [
            {"text": correction_prompt},
            {
                "inlineData": {
                    "mimeType": "image/png",
                    "data": img_b64
                }
            }
        ]
        
        # Corrective pass over a downsampled copy — stays on the fast model
        # whatever the render settings say, for the same reason as the hands
        # compositing pass above.
        api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_FLASH_IMAGE}:generateContent"
        
        headers = {
            "x-goog-api-key": GEMINI_API_KEY,
            "Content-Type": "application/json"
        }
        
        payload = {
            "contents": [{
                "parts": parts
            }],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {
                    "aspectRatio": resolve_aspect_ratio()
                }
            }
        }
        
        response = requests.post(api_url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        
        result = response.json()
        if "candidates" not in result or not result["candidates"]:
            print("[POV CORRECTION] No result, using original")
            return None
        
        # Safely access parts with error handling
        try:
            image_b64 = result["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
        except (KeyError, IndexError) as e:
            print(f"[POV CORRECTION] Error extracting image: {e}, response: {result}")
            return None
        corrected_bytes = base64.b64decode(image_b64)
        
        # Save corrected version with _corrected suffix
        from pathlib import Path
        original_pathobj = Path(original_path)
        corrected_filename = original_pathobj.stem + "_corrected" + original_pathobj.suffix
        corrected_path = original_pathobj.parent / corrected_filename
        
        write_image_atomic(corrected_path, corrected_bytes)
        
        # Create small version too
        from PIL import Image as PILImage
        import io
        img = PILImage.open(io.BytesIO(corrected_bytes))
        img = img.convert("RGB")
        img = img.resize((480, 360), PILImage.LANCZOS)  # 4:3 aspect ratio
        
        small_corrected_filename = corrected_filename.replace(".png", "_small.png")
        small_corrected_path = original_pathobj.parent / small_corrected_filename
        img.save(small_corrected_path, format="PNG", optimize=True, quality=85)
        
        print(f"[POV CORRECTION] Applied: {corrected_filename}")
        return str(Path("images") / corrected_filename)
        
    except Exception as e:
        print(f"[POV CORRECTION] Failed: {e}, using original")
        return None


def _apply_forward_zoom(image_path: str, zoom_factor: float = 1.35) -> bytes:
    """
    Apply dramatic zoom (crop center + scale up) to create forward momentum.
    
    Crops the center region of the image and scales it back to original size,
    simulating a "camera moving forward" effect. This forces the AI to interpret
    and extend the scene rather than pixel-perfect copying, reducing staleness.
    
    Args:
        image_path: Path to the reference image
        zoom_factor: How much to zoom (1.35 = 35% zoom, uses center 74% of frame)
    
    Returns:
        PNG bytes of the zoomed image (full resolution, LANCZOS resampling)
    """
    from PIL import Image
    import io
    
    with Image.open(image_path) as img:
        width, height = img.size
        
        # Calculate crop box for center region
        # zoom_factor=1.35 means we keep 1/1.35 = 74% of the frame
        crop_width = int(width / zoom_factor)
        crop_height = int(height / zoom_factor)
        
        left = (width - crop_width) // 2
        top = (height - crop_height) // 2
        right = left + crop_width
        bottom = top + crop_height
        
        # Crop center and scale back to original size
        cropped = img.crop((left, top, right, bottom))
        zoomed = cropped.resize((width, height), Image.Resampling.LANCZOS)
        
        # Convert to PNG bytes (no compression to preserve quality)
        buffer = io.BytesIO()
        zoomed.save(buffer, format='PNG', optimize=False)
        return buffer.getvalue()


def make_style_swatch(source_path: str, output_dir=None) -> Optional[str]:
    """Reduce a frame to an abstract, blurred color/light field with no
    legible geometry left in it — a reference an img2img call can use for
    color/lighting continuity WITHOUT anything spatial to copy.

    Telling Gemini "use this reference for style only, not composition"
    does not reliably work when the reference is still a sharp, legible
    photo — a hard transition tried exactly that (a "style-only" note plus
    the previous frame as the sole reference) and the model kept
    reproducing the old frame's camera angle and layout anyway, which is
    why hard transitions send NO reference at all today (see
    engine.py's _gen_image_impl). This takes the other lever: instead of
    asking the model to ignore the photo's content, it destroys the
    content before the request is ever sent. Downsampling to a handful of
    cells averages away every edge and shape; blurring after upscaling
    erases the residual mosaic blockiness so nothing resembling a boundary
    survives. What's left is a smooth field of the frame's dominant colors
    and roughly how bright/warm it was — exactly what color/lighting
    continuity needs, and nothing a model could compose a new scene FROM.
    """
    from PIL import Image, ImageFilter
    try:
        with Image.open(source_path) as src:
            src = src.convert("RGB")
            w, h = src.size
            if not w or not h:
                return None
            # A handful of cells per axis keeps "warm orange on the left,
            # cool blue on the right" but destroys anything as specific as
            # a doorway, a silhouette, or a horizon line.
            tiny_w = max(6, w // 48)
            tiny_h = max(4, h // 48)
            tiny = src.resize((tiny_w, tiny_h), Image.BILINEAR)
            out_w = 512
            out_h = max(1, round(out_w * h / w))
            swatch = tiny.resize((out_w, out_h), Image.BILINEAR)
            swatch = swatch.filter(ImageFilter.GaussianBlur(radius=max(24, out_w // 12)))
        out_dir = Path(output_dir) if output_dir else Path(source_path).parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{Path(source_path).stem}_styleswatch.png"
        swatch.save(out_path)
        print(f"[STYLE SWATCH] {Path(source_path).name} -> {out_path.name} "
              f"({tiny_w}x{tiny_h} cells, averaged + blurred, no legible geometry)",
              flush=True)
        return str(out_path)
    except Exception as e:
        print(f"[STYLE SWATCH] failed for {source_path}: {e}", flush=True)
        return None


def generate_gemini_img2img(
    prompt: str,
    caption: str,
    reference_image_path: str | list[str],
    strength: float = 0.3,
    world_prompt: str = None,
    time_of_day: str = "",
    action_context: str = "",
    hd_mode: bool = True,
    output_dir: Path = None,
    is_flipbook: bool = False,
    flipbook_grid: tuple[int, int] | None = None,
    portrait_mode: bool = False,
    ensemble_mode: bool = False,
    style_only_swatch: bool = False,
    subject_crop: bool = False,
    object_subject: bool = False,
    identity_paths: list[str] | None = None,
    identity_seed: bool = False,
    spec: dict | None = None,
    include_people: bool = False,
    hold_cast: bool = False,
    cast_plates: list[str] | None = None,
    image_size: str | None = None,
    model: str | None = None,
    reference_labels: dict | None = None,
    lead_reference: str | None = None,
) -> str:
    """
    Edit an image using Google Gemini (image-to-image).
    Supports up to 6 reference images for better continuity.

    ``reference_labels`` maps a reference path to the caption that sits next to
    it in the request, overriding the generic one. The generic caption for a
    non-plate attachment is "PREVIOUS FRAME — place, light, and materials only
    … do NOT copy the person in this frame", which is right for a still (it
    stops a recast redrawing the leftover guy) and wrong for a flipbook, whose
    first reference is the start keyframe the grid must continue — pose
    included — and whose last is a blank layout guide that is not a frame at
    all. The flipbook names its own; see engine._flipbook_generate.

    ``lead_reference`` is a path that takes slot 1 whatever else is attached.
    Slot 1 is the attachment the model copies the person from, which is why
    the plates go first for a still. A flipbook turn that continues from a
    previous panel wants that panel there: it already shows the character, in
    the pose and from the side panel 1 must continue, whereas the character
    sheet in slot 1 is a face-on portrait and panel 1 kept coming back face-on.

    ``image_size`` ("1K" / "2K" / "4K") and ``model`` override the configured play
    settings for this one call. Grid renders need both: the panels are slices of a
    single generation, so a 2x2 at the play setting of 1K is only 672x376 per
    panel, and the fast play model tops out at 2K — 4K needs gemini-3-pro-image.
    
    Args:
        prompt: The FULL editing instruction WITH ALL DETAILED POV INSTRUCTIONS
        caption: Short caption for the edit (used for filename)
        reference_image_path: Path(s) to input image(s) - single string or list of up to 6 paths
        strength: 0.0-1.0, controls how much to change (via prompt engineering)
        world_prompt: Optional world context
        time_of_day: Time of day for lighting consistency
        hd_mode: If True, use Pro model for higher quality (slower). If False, use Flash for speed.
        is_flipbook: If True, suppress single-image constraints (like NO BORDERS).
        flipbook_grid: (rows, cols) of the grid being asked for. The grid note
            below used to say "4x4" no matter what the caller had asked the
            model for, which is a contradiction the moment the panel count is a
            setting. Defaults to the historical 4x4 for older call sites.
        ensemble_mode: When True, treat EVERY reference image as an independent
            character/prop portrait to be composited into a BRAND NEW location
            described by `prompt` — NOT the current environment and NOT the
            previous moment. Used by the CAMP moment to gather multiple
            companions (+ the jeep prop) around a campfire that isn't the
            scene the player is standing in. Mutually exclusive in spirit with
            `portrait_mode` (which holds likeness from a SCAN bbox crop of
            ONE character) — pass only one of the two as True.
        subject_crop: When True with ``portrait_mode``, the reference is a
            crop of the tagged subject (their actual pixels), not a wide
            environment plate. Continuity is likeness, not "invent a face
            standing in this room."
        object_subject: When True with ``portrait_mode``, the crop is a
            machine/object. Hold those pixels and do not invent a person.
        cast_plates: Close-ups of subjects the player has ALREADY been shown at
            length — the INTERACT dive's plate — that must appear in this wide
            scene as the same face/object. Rides as an extra labeled reference
            next to ``identity_paths``, never mixed into the continuity list: a
            close-up read as "the previous frame" makes the model reproduce its
            framing and return another close-up instead of a scene. The reason
            this exists at all is that a subject is a few dozen pixels tall in a
            wide frame, which is not enough to redraw them from, so the return
            leg of a dive recast the character the dive had just introduced.
        style_only_swatch: When True, `reference_image_path` points at a
            `make_style_swatch()` output — the previous frame blurred past
            recognition — rather than a legible photo. Swaps in a continuity
            instruction that tells the model there is nothing spatial left
            to copy and to take ONLY color/lighting from it, instead of the
            normal "match camera position and composition" instruction that
            would otherwise fight the swatch's own blur for no benefit.
        
    Returns:
        Local path to the saved image
    """
    # CRITICAL: Ensure caption is safe for all operations (filename, logging, etc.)
    try:
        # Safely encode caption to ASCII to prevent Unicode errors in Windows console
        caption = caption.encode('ascii', 'ignore').decode('ascii')
    except:
        caption = "image"  # Fallback if encoding fails
    
    # Handle single path or list of paths
    if isinstance(reference_image_path, str):
        image_paths = [reference_image_path]
    else:
        image_paths = list(reference_image_path or [])
    identity_paths = [p for p in (identity_paths or []) if p]
    identity_set = set(identity_paths)
    cast_plates = [p for p in (cast_plates or []) if p and p not in identity_set]
    cast_set = set(cast_plates)
    # Character / level plates FIRST. Gemini copies the person in slot 1;
    # putting the previous still there is why MOVE TO redrew the leftover guy
    # even when a woman plate was attached last. Continuity frames follow,
    # labeled as the previous place, not as who to draw.
    if identity_paths and not hold_cast:
        image_paths = identity_paths + [p for p in image_paths if p not in identity_set]
    # Close-up plates sit immediately behind the player's own sheet and ahead of
    # the continuity frames, for the same slot-order reason: trailing plates
    # lose. The player's sheet keeps slot 1 so the protagonist is never the one
    # recast; the discovered subject takes the next slot so their likeness beats
    # the forty pixels of them in the previous frame; place and light follow.
    if cast_plates:
        image_paths = (
            [p for p in image_paths if p in identity_set]
            + cast_plates
            + [p for p in image_paths if p not in identity_set and p not in cast_set]
        )
    if lead_reference and lead_reference in image_paths:
        image_paths = [lead_reference] + [p for p in image_paths if p != lead_reference]
    image_paths = image_paths[:6]
    
    print(f"[GOOGLE GEMINI] Image editing mode with {len(image_paths)} reference image(s)", flush=True)
    
    # Read and encode all reference images. Label each one so a character
    # sheet is not treated as "the previous game frame" (that is how a recast
    # kept drawing the default guy from the leftover still).
    image_parts = []
    labeled_parts = []
    for img_path in image_paths:
        # Choose between downsampled or full-res based on USE_DOWNSAMPLED_FOR_IMG2IMG toggle
        from pathlib import Path
        img_path_obj = Path(img_path)
        
        if USE_DOWNSAMPLED_FOR_IMG2IMG:
            # Try to use downsampled version (faster, less bandwidth)
            small_path = img_path_obj.parent / img_path_obj.name.replace(".png", "_small.png")
            use_path = small_path if small_path.exists() else img_path_obj
            quality_note = "downsampled 480x360" if small_path.exists() else "full-res (no downsample found)"
        else:
            # Force full-res (preserves quality, prevents artifact compounding)
            use_path = img_path_obj
            quality_note = "full-res"
        
        # Apply forward zoom preprocessing if enabled
        if ENABLE_FORWARD_ZOOM:
            image_bytes = _apply_forward_zoom(str(use_path), zoom_factor=ZOOM_FACTOR)
            print(f"[FORWARD ZOOM] Applied {ZOOM_FACTOR}x zoom to {img_path_obj.name} (center {int(100/ZOOM_FACTOR)}% to full frame)")
        else:
            with open(use_path, "rb") as f:
                image_bytes = f.read()
        
        image_b64 = base64.b64encode(image_bytes).decode('utf-8')
        
        # Determine MIME type
        mime_type = "image/png"
        if str(use_path).endswith(('.jpg', '.jpeg')):
            mime_type = "image/jpeg"
        
        print(f"[GOOGLE GEMINI] Reference image {len(image_parts)+1}: {img_path_obj.name} ({quality_note})")
        
        encoded = {
            "inlineData": {
                "mimeType": mime_type,
                "data": image_b64
            }
        }
        image_parts.append(encoded)
        if reference_labels and img_path in reference_labels:
            # The caller has said what this attachment is; that beats every
            # generic caption below.
            labeled_parts.append({"text": str(reference_labels[img_path])})
        elif identity_seed or identity_paths or cast_plates or game_identity.is_viewfinder_spec(spec):
            if img_path in cast_set:
                # Unlabeled, a close-up is just "the previous frame" — and the
                # model obliges by continuing its framing, which turns the
                # return leg of a dive into a second close-up.
                label = (
                    "CLOSE-UP OF A SUBJECT ALREADY IN THIS SCENE — copy this "
                    "exact face, build, hair, clothing, materials and wear. "
                    "This is WHO/WHAT is there, not a previous game frame and "
                    "not the player character. Do NOT copy its framing or "
                    "background: place this subject into the wide scene the "
                    "instruction describes."
                )
            elif style_only_swatch and img_path not in identity_set:
                label = (
                    "COLOR/LIGHT SWATCH — palette only. No person, no place, "
                    "no composition to copy."
                )
            else:
                label = game_identity.reference_part_label(img_path, spec)
            labeled_parts.append({"text": label})
        labeled_parts.append(encoded)
    
    # Identity plates are not a previous game frame. Using the img2img
    # template here ("the attached image is the PREVIOUS moment") is what
    # made a character-sheet recast redraw the default guy at the fence.
    template_key = (
        "gemini_text_to_image_instructions" if identity_seed
        else "gemini_image_to_image_instructions"
    )
    structured_prompt = prompts_store.render_image_template(template_key, prompt)
    structured_prompt = structured_prompt + LEGIBILITY_RULE
    
    # Inject time/weather/mood if provided
    if time_of_day:
        time_injection = f"\n\nLighting: {time_of_day}.\n"
        structured_prompt = structured_prompt + time_injection
    
    # Add continuity instructions - DIFFERENT for flipbook vs single-frame img2img.
    # ensemble_mode takes precedence over portrait_mode when both are set: camp
    # composites pass portrait_mode=True only to allow people (skip anti-person),
    # while the continuity grammar must be the NEW-LOCATION ensemble path.
    if identity_seed:
        continuity_instruction = (
            "\n\n" + game_identity.identity_seed_instruction(spec)
        )
    elif ensemble_mode:
        # ENSEMBLE COMPOSITE: each reference is a stand-alone character/prop
        # portrait (companion portraits + the jeep prop), not the environment
        # the player is currently standing in. The instruction describes a
        # BRAND NEW location (e.g. a night campsite) — build it from scratch
        # and populate it with everyone referenced.
        continuity_instruction = (
            "\n\n🔥 CRITICAL — ENSEMBLE COMPOSITE INTO A NEW LOCATION:\n"
            "═══════════════════════════════════════════════════════════════════\n"
            "Each reference image is an independent PORTRAIT of ONE specific\n"
            "person, OR a reference photo of ONE specific prop/vehicle — captured\n"
            "somewhere else, at some other time. They are NOT the current\n"
            "environment and NOT the previous frame of any video.\n"
            "\n"
            "The instruction describes a NEW location. Build that location from\n"
            "the instruction's own description — do NOT reuse the background,\n"
            "room, or setting visible behind any reference subject.\n"
            "\n"
            "COPY from each PERSON reference (so they read as the SAME person):\n"
            "✅ Face, build, approximate age, hair, and clothing/style\n"
            "✅ Their general demeanor/expression\n"
            "COPY from each PROP/VEHICLE reference (so it reads as the SAME object):\n"
            "✅ Exact color, make/model silhouette, condition (dust, dents, wear)\n"
            "\n"
            "DO NOT COPY from any reference:\n"
            "❌ Its background, lighting setup, or location — that belongs to a\n"
            "  different place and time; the NEW scene has its own lighting\n"
            "❌ Framing/composition — recompose everyone into ONE coherent wide\n"
            "  shot appropriate to the instruction, not a collage of close-ups\n"
            "\n"
            "EVERY person and prop referenced MUST appear, clearly recognizable,\n"
            "placed naturally within the new location described. This is a full\n"
            "environment shot — a wide establishing shot is correct here."
        )
    elif style_only_swatch:
        # The reference here is make_style_swatch()'s output, not a photo of
        # anywhere — every shape and edge in it was destroyed on purpose
        # before this request was built. The normal single-frame branch's
        # "match camera position/composition" language would ask the model
        # to infer a layout from a blur, which is exactly the ambiguity that
        # makes an img2img model default back to whatever it CAN read off
        # the pixels (see the hard-transition history: a "style only" note
        # next to a still-legible photo did not stop it copying that photo's
        # composition). Removing that language, not just softening it, is
        # the point — there is nothing left to copy, so nothing here asks
        # for anything to be copied except color and light.
        continuity_instruction = (
            "\n\n🎨 CRITICAL — THIS REFERENCE IS A COLOR/LIGHT SWATCH, NOT A PLACE:\n"
            "═══════════════════════════════════════════════════════════════════\n"
            "The reference image has been deliberately reduced to an abstract, "
            "blurred field of color. It contains NO shapes, NO objects, NO room, "
            "NO camera angle, and NO composition — there is nothing spatial in "
            "it to copy, because it was destroyed on purpose before you received "
            "it.\n"
            "\n"
            "USE the reference ONLY for:\n"
            "✅ Overall color palette / color grade\n"
            "✅ Rough light level and warmth (bright vs dim, warm vs cool)\n"
            "\n"
            "DO NOT use the reference for anything else:\n"
            "❌ Do not infer a room, a horizon, a doorway, or any shape from its blur\n"
            "❌ Do not hold back on composing a brand-new shot — build the scene "
            "ENTIRELY from the description below, as if this reference were blank\n"
            "\n"
            "The scene description below is the ONLY source of truth for what is "
            "actually in this shot and where the camera is looking."
        )
    elif portrait_mode and object_subject:
        # CONVERSATION CLOSE-UP of a machine/object crop. Those pixels ARE the
        # object. Hold them — do not invent a person standing in a new room.
        continuity_instruction = (
            "\n\n🎬 CRITICAL — THIS IS THE OBJECT (SCAN CROP):\n"
            "═══════════════════════════════════════════════════════════════════\n"
            "The reference image is a CROP of the tagged object, cut from the live\n"
            "realtime video frame using its detected bounding box. Those pixels ARE\n"
            "the object — a monitor, radio, terminal, or other thing being spoken to.\n"
            "This is not a character portrait. Do not invent a person.\n"
            "\n"
            "COPY from the crop (non-negotiable likeness):\n"
            "✅ OBJECT: same shape, materials, wear, markings, screen/faceplate\n"
            "✅ LIGHTING: same light on the object, color temperature, shadow direction\n"
            "✅ PALETTE + FILM LOOK: same grain, color grade\n"
            "✅ BACKGROUND: whatever of the crop's environment remains, softly out of focus\n"
            "\n"
            "CHANGE (the reframe only):\n"
            "→ FRAMING: cinematic close-up, the SAME object filling the frame\n"
            "→ FOCUS: the object is sharp; leftover background is soft\n"
            "\n"
            "Think: you zoomed into the tagged object and held on it. Same object.\n"
            "NO person, NO face, NO figure, NO human in the shot."
        )
    elif portrait_mode and subject_crop:
        # CONVERSATION PORTRAIT from a SCAN bbox crop: those pixels ARE the
        # person. Hold likeness and reframe — do not invent a different face
        # from a wide environment plate.
        continuity_instruction = (
            "\n\n🎬 CRITICAL — THIS IS THE PERSON (SCAN CROP):\n"
            "═══════════════════════════════════════════════════════════════════\n"
            "The reference image is a CROP of the tagged figure, cut from the live\n"
            "frame using their detected bounding box. Those pixels ARE their face,\n"
            "body, clothes, and the light falling on them. This is not a wide room\n"
            "plate. Do not invent a different person.\n"
            "\n"
            "COPY from the crop (non-negotiable likeness):\n"
            "✅ FACE: same features, age, skin, expression, hair\n"
            "✅ BODY / CLOTHES: same build, jacket, shirt, colors, wear\n"
            "✅ LIGHTING: same light on their face, color temperature, shadow direction\n"
            "✅ PALETTE + FILM LOOK: same grain, color grade\n"
            "\n"
            "CHANGE (the reframe only):\n"
            "→ FRAMING: cinematic medium shot, mid-torso up, this SAME figure\n"
            "→ FOCUS: they are sharp; whatever of the crop's background remains is soft\n"
            "→ COMPOSITION: eye-line toward camera, present and lit\n"
            "\n"
            "Think: you zoomed into the tagged person and held on them. Same human."
        )
    elif portrait_mode:
        # Fallback when we only have a full frame and no bbox (or companion
        # placement into the current environment): keep the room and reframe.
        continuity_instruction = (
            "\n\n🎬 CRITICAL — SAME PLACE, NEXT SHOT (CONVERSATION PORTRAIT):\n"
            "═══════════════════════════════════════════════════════════════════\n"
            "The reference image is the EXACT environment the camera is in RIGHT NOW.\n"
            "Generate the NEXT SHOT: a cinematic MEDIUM SHOT of the character being\n"
            "spoken to, standing IN THAT SAME ROOM/PLACE — as if the camera simply\n"
            "turned to face them. It must feel like the same continuous footage.\n"
            "\n"
            "COPY from the reference (non-negotiable continuity):\n"
            "✅ ENVIRONMENT: same room/location, same walls, props, depth, background\n"
            "✅ LIGHTING: same light sources, color temperature, shadow direction\n"
            "✅ PALETTE + FILM LOOK: same grain, color grade\n"
            "✅ TIME OF DAY / ATMOSPHERE: identical to the reference\n"
            "✅ If a person is already visible, keep THAT face/clothes — do not replace them.\n"
            "\n"
            "CHANGE (the reframe):\n"
            "→ FRAMING: a cinematic medium shot (mid-torso up) of the CHARACTER\n"
            "→ FOCUS: the character is the subject, sharp; background soft (shallow DoF)\n"
            "→ COMPOSITION: character centered/eye-line to camera, present and lit\n"
            "\n"
            "The character described in the instruction now OCCUPIES this environment.\n"
            "Think: the operator lowered the camera and turned to face the person\n"
            "they're talking to — same room, one shot later."
        )
    elif is_flipbook:
        # FLIPBOOK MODE. The grid rules the caller composed (flipbook.grid_prompt
        # and engine's action block) are the keyframe contract: panel 1 is the
        # START KEYFRAME continued, the last panel is the END, the rest are
        # in-betweens. This block used to be an older copy of the same
        # contract, written for a first-person desert run — "The FIRST
        # reference image is the FINAL PANEL of the previous sequence" (the
        # first attachment is the character sheet once plates are attached),
        # "VISIBLE LANDMARKS: Mesas, buildings, fences", "GROUND TYPE: desert,
        # concrete, rubble", "Jumping to a new location — continuation, NOT
        # teleportation" (which fights a travelling turn's last panel). Two
        # statements of one contract drift; this one now defers to the other.
        continuity_instruction = (
            "\n\n⚡ CRITICAL — TEMPORAL CONTINUITY: WHERE THE CAMERA IS RIGHT NOW\n"
            "═══════════════════════════════════════════════════════════════════\n"
            "\n"
            "The reference labelled START KEYFRAME is the final panel of the "
            "previous sequence: where the camera stands RIGHT NOW, and how the "
            "subject stands in it. It is the last thing the viewer saw.\n"
            "\n"
            "🎯 PANEL 1 OF YOUR NEW GRID = THE VERY NEXT MOMENT AFTER IT:\n"
            "• Same camera height, same direction, same distance from the subject\n"
            "• Same place, same light, same film grain and palette\n"
            "• The subject in the same spot and the same pose, beginning to move\n"
            "• A viewer watching the START KEYFRAME and then panel 1 sees "
            "uncut footage — not a new scene, not a new angle\n"
            "\n"
            "The KEYFRAMES section above says where the grid goes from there and "
            "how far by the last panel; it governs.\n"
            "\n"
            "WHAT THE OTHER ATTACHMENTS ARE — none of them is a moment:\n"
            "• A CHARACTER SHEET is the face, build and outfit — not the pose "
            "and not the framing. The pose and the side they are seen from "
            "come from the START KEYFRAME.\n"
            "• A LOCATION PLATE is the architecture, materials and palette of "
            "the place — not a composition to reproduce, and nobody standing "
            "in it is in this scene.\n"
            "• A LAYOUT TEMPLATE is the panel layout and nothing else.\n"
            "Where the instruction above says \"the attached image is the "
            "PREVIOUS moment\", that is the START KEYFRAME and only the START "
            "KEYFRAME. Where it says the reference is not a composition to "
            "reproduce, that is about the LAST panel: panel 1 reproduces the "
            "START KEYFRAME's framing and pose.\n"
        )
    elif hold_cast:
        # Encounter resolve: do NOT "keep similar framing" — that freezes the
        # standoff and hides the verb. Faces stay; bodies move.
        continuity_instruction = (
            "\n\n⚡ ACTION RESTAGE — SAME PEOPLE, NEW POSE:\n"
            "The reference is the confrontation photograph. It is the ONLY cast.\n"
            "\n"
            "COPY from the reference (non-negotiable):\n"
            "✅ BOTH faces, hair, clothes, gender, build — pixel-level likeness\n"
            "✅ The same place, light, materials, and sky\n"
            "✅ The same two people. No third person. No character-sheet recast.\n"
            "\n"
            "CHANGE (required — a posed copy is a failure):\n"
            "→ BODY POSITION and CONTACT so the instruction's verb is visible\n"
            "→ Hands on the other body, weight shifting, a torso reacting\n"
            "→ Framing may tighten or widen to show the action\n"
            "\n"
            "Do not return two people standing still facing each other."
        )
    else:
        # SINGLE FRAME MODE: Previous frame is for SMOOTH CONTINUITY
        continuity_instruction = (
            "\n\n⚡ CRITICAL - HOW TO USE REFERENCE IMAGES:\n"
            "The reference images show the PREVIOUS MOMENT. Show smooth, natural progression.\n"
            "\n"
            "COPY from references (maintain continuity):\n"
            "✅ CAMERA POSITION: Keep roughly the same viewpoint unless action explicitly moves camera\n"
            "✅ CAMERA HEIGHT: Maintain same eye-level/perspective height\n"
            "✅ CAMERA ANGLE: Keep similar framing and field of view\n"
            "✅ COMPOSITION: Similar framing with natural evolution\n"
            "✅ VISUAL STYLE: grain, color palette, lighting\n"
            "✅ ENVIRONMENT: Same location, same aesthetic\n"
            "\n"
            "CHANGE naturally (show progression):\n"
            "→ SUBJECT POSITION: Characters/objects move based on the action\n"
            "→ DETAILS: Environmental changes, reactions, consequences\n"
            "→ SUBTLE SHIFT: Very slight camera drift/pan for dynamism (not teleportation)\n"
            "\n"
            "Think: This is a HANDHELD CAMERA recording continuously.\n"
            "The camera operator doesn't teleport - they walk/turn naturally.\n"
            "Show the NEXT MOMENT from a camera that moved smoothly, not a different camera entirely."
        )
    
    structured_prompt = structured_prompt + continuity_instruction
    
    if is_flipbook:
        # For flipbooks, we NEED the grid lines to remain, so we're more relaxed
        rows, cols = flipbook_grid or (4, 4)
        shape = f"{rows}x{cols}"
        flipbook_grid_note = (
            f"\n\nCRITICAL - {shape} GRID STRUCTURE:\n"
            f"Preserve the {shape} grid structure from the layout template. "
            f"Each panel must show a slightly different moment in time. "
            f"The output MUST be a {shape} grid of {rows * cols} panels."
        )
        structured_prompt = structured_prompt + flipbook_grid_note
    
    # Anti-person REMOVAL directive — for environment stills only. Portrait and
    # ensemble modes deliberately INCLUDE people (conversation close-up / camp
    # cast reunion), so skip the removal and instead instruct inclusion.
    if ensemble_mode:
        add_ensemble = (
            "\n\n🔥 CRITICAL - ENSEMBLE CAST MUST APPEAR:\n\n"
            "Unlike the game's empty environment shots, THIS wide establishing\n"
            "shot MUST feature every referenced person AND every referenced prop\n"
            "or vehicle, seated/placed naturally in the NEW location described.\n"
            "Do NOT delete or hide them. Do NOT turn this into an empty plate if\n"
            "people were referenced. If ONLY a prop/vehicle was referenced (no\n"
            "people), show that prop alone in the quiet campsite — no invented\n"
            "extra cast. Firelight is the key light; faces and the vehicle must\n"
            "be recognizable."
        )
        structured_prompt = structured_prompt + add_ensemble
    elif portrait_mode and object_subject:
        add_object = (
            "\n\n🎭 CRITICAL - KEEP THIS EXACT OBJECT:\n\n"
            "The reference IS a crop of the object you are talking to. Hold its\n"
            "shape, materials, wear, and light. Reframe to a cinematic close-up of\n"
            "THIS same object. Do NOT invent a person. Do NOT add a face or figure.\n"
            "Do NOT replace the object with a character portrait."
        )
        structured_prompt = structured_prompt + add_object
    elif portrait_mode and subject_crop:
        add_person = (
            "\n\n🎭 CRITICAL - KEEP THIS EXACT PERSON:\n\n"
            "The reference IS a crop of the person you are talking to. Hold their\n"
            "face, hair, clothes, and build. Reframe to a cinematic medium shot of\n"
            "THIS same figure. Do NOT invent a different person. Do NOT replace\n"
            "their face. Do NOT turn this into an empty room."
        )
        structured_prompt = structured_prompt + add_person
    elif portrait_mode:
        add_person = (
            "\n\n🎭 CRITICAL - THE CHARACTER IS THE SUBJECT:\n\n"
            "Unlike the game's environment shots, THIS shot MUST feature the person.\n"
            "Render the character described in the instruction as a real, present\n"
            "human (or being) standing in the reference environment, framed as a\n"
            "cinematic medium shot. If they are already visible in a reference,\n"
            "keep that same face and clothes. Do NOT delete or hide them. Do NOT\n"
            "turn this into an empty room. The character faces the camera, clearly\n"
            "lit and in focus, with the reference environment softly behind them."
        )
        structured_prompt = structured_prompt + add_person
    elif hold_cast:
        # Encounter resolve: the confrontation still IS the cast. A character
        # sheet in slot 1 recasts the player and drops the challenger; the
        # enter-path "add a new person" invents a third face. Same two bodies,
        # new pose, verb visible.
        structured_prompt = structured_prompt + (
            "\n\n🔥 ACTION RESTAGE — SAME CAST, NEW POSE:\n"
            "The reference photograph already contains every person who exists "
            "in this shot. Copy BOTH faces, hair, clothes, and bodies from that "
            "still. Do not introduce a third person. Do not replace either "
            "person with a character sheet or a new face. Do not change gender.\n"
            "ONLY change pose and contact so the instruction's verb is visible: "
            "weight shifting, hands on the other body, a torso reacting. "
            "A posed conversation with no contact is a failure.\n"
            "Keep the same place and light."
        )
    elif include_people:
        # Encounter / confrontation restage: KEEP the player AND ADD a new
        # person. The default environment path strips humans, which is why
        # strangers were appearing only on the aftermath turn.
        if game_identity.shows_character(spec):
            has_plate = bool(identity_paths) or (
                identity_seed and bool(game_identity.character_reference_paths(spec))
            )
            structured_prompt = structured_prompt + game_identity.keep_character_instruction(
                spec, has_character_plate=has_plate, extras_are_strangers=True,
            )
        structured_prompt = structured_prompt + game_identity.keep_place_instruction(
            spec,
            has_setting_plate=bool(
                identity_paths and game_identity.setting_reference_paths(spec)
            ),
        )
        structured_prompt = structured_prompt + (
            "\n\n🔥 CONFRONTATION — A NEW PERSON MUST BE IN THIS FRAME:\n"
            "In ADDITION to the player character, draw the newly introduced "
            "character described in the instruction. They must be large, "
            "readable, and already in this place — a two-shot or over-shoulder. "
            "They are NOT a second copy of the player. Different face, hair, "
            "clothes, and body. Do not put them in the player's vest, cap, "
            "or press badge. Two outfits, two people.\n"
            "Do NOT render an empty environment. Do NOT delete people. "
            "Do NOT wait for a later frame to introduce them."
        )
    elif game_identity.shows_character(spec):
        has_plate = bool(identity_paths) or (
            identity_seed and bool(game_identity.character_reference_paths(spec))
        )
        structured_prompt = structured_prompt + game_identity.keep_character_instruction(
            spec, has_character_plate=has_plate,
            # Without this the character sheet's own wording ("a previous frame
            # may show a different person — ignore that person") reads as an
            # order to discard the close-up plate, and whoever survives gets
            # dressed in the player's outfit.
            extras_are_strangers=bool(cast_plates),
        )
        structured_prompt = structured_prompt + game_identity.keep_place_instruction(
            spec,
            has_setting_plate=bool(
                identity_paths and game_identity.setting_reference_paths(spec)
            ),
        )
    else:
        structured_prompt = structured_prompt + game_identity.keep_place_instruction(
            spec,
            has_setting_plate=bool(
                identity_paths and game_identity.setting_reference_paths(spec)
            ),
        )
    if not (portrait_mode or ensemble_mode or hold_cast or include_people
            or game_identity.shows_character(spec) or cast_plates):
        anti_person = "\n\n🚨 CRITICAL - REMOVE ANY PEOPLE FROM REFERENCE IMAGE:\n\n" \
                     "The REFERENCE IMAGE may contain a person/character - this is WRONG. Your job is to REMOVE THEM.\n\n" \
                     "GENERATE THE EXACT SAME SCENE but with the person DELETED. Show ONLY the environment.\n\n" \
                     "This is a SECURITY CAMERA view - no camera operator exists. PURE environmental shot.\n\n" \
                     "NEVER INCLUDE:\n" \
                     "- Person visible (standing, walking, crouching, any pose)\n" \
                     "- Head, back of head, shoulders, silhouette\n" \
                     "- Arms, hands, legs, feet, body parts\n" \
                     "- Person from behind, person from side, person from any angle\n" \
                     "- Character visible in any way\n\n" \
                     "ONLY SHOW: Environment, objects, vehicles, structures, sky, ground, debris, fire, smoke - NO HUMANS."
        structured_prompt = structured_prompt + anti_person

    if cast_plates:
        # The dive showed the player this subject up close and the scene has to
        # hand back the same one. Two failure modes to close, and they pull in
        # opposite directions: drop the subject entirely (the environment paths
        # above spend a lot of words asking for empty plates), or copy the
        # close-up's framing and return a portrait instead of a scene.
        structured_prompt = structured_prompt + (
            "\n\n🫱 THE SUBJECT FROM THE CLOSE-UP IS IN THIS SCENE:\n"
            "A CLOSE-UP reference is attached of something the player has just "
            "been looking at in this very place. It is still here and it is "
            "still the same one: copy its face, build, hair, clothing, "
            "materials and wear from that close-up. Do not recast it, do not "
            "substitute something similar, do not remove it from the frame.\n"
            "It is NOT the player character and must not be dressed as them.\n"
            "The output is the WIDE SCENE, not the close-up again. Put this "
            "subject into the space at whatever distance the instruction "
            "describes — a portrait, or a frame filled by this subject alone, "
            "is wrong."
        )

    # Mode-specific "don't do this" — look lives in image_art_direction.
    # Do not name REC / timecode / VHS HUD; that draws a viewfinder.
    if ensemble_mode:
        negative_emphasis = (
            "\n\nNot a collage, not a split-screen of portraits, not floating heads, "
            "not lighting that ignores the campfire."
        )
    elif portrait_mode and object_subject:
        negative_emphasis = (
            "\n\nNot a person, not a face, not a figure, not a different object "
            "than the crop, not a wide establishing shot."
        )
    elif portrait_mode and subject_crop:
        negative_emphasis = (
            "\n\nNot a different person than the crop, not an invented face, "
            "not an empty room, not a wide establishing shot."
        )
    elif portrait_mode:
        negative_emphasis = (
            "\n\nNot an empty room, not a wide establishing shot, "
            "not a different person than the one already in the reference."
        )
    elif hold_cast:
        negative_emphasis = (
            "\n\nNot a posed standoff, not a new face, not a character-sheet "
            "recast, not a third person, not empty hands at a distance."
        )
    elif include_people or game_identity.shows_character(spec):
        negative_emphasis = (
            "\n\nNot an empty scene, not a different person than the character sheet."
            if (identity_paths or identity_seed) else
            "\n\nNot an empty scene, not a missing second person, "
            "not a different person than the ones described."
        )
    elif cast_plates:
        # The default for a bodiless camera is "no person in frame", which would
        # delete the very subject the dive just introduced.
        negative_emphasis = (
            "\n\nNot a missing subject, not a different one than the close-up, "
            "not a portrait, not a frame filled by the subject alone."
        )
    else:
        # Written to keep the player's own body out of a body-cam frame, and
        # read by the model as "draw nobody": a first-person run never had a
        # figure in it to meet. The ban is on the PLAYER's body; people the
        # scene describes are drawn.
        negative_emphasis = (
            "\n\nNo sign of the PLAYER'S own body in frame: not their head, "
            "shoulders, back or silhouette (hands only when the action "
            "reaches). Other people the scene describes ARE in the picture, "
            "as described."
        )
    if cast_plates and "close-up" not in negative_emphasis:
        negative_emphasis += " Not a different subject than the close-up."

    full_prompt = structured_prompt + negative_emphasis

    # Cast & camera reconciliation (see generate_with_gemini). Skipped for the
    # portrait/ensemble moments, which run their own deliberate camera grammar.
    if not (portrait_mode or ensemble_mode or hold_cast):
        full_prompt = game_identity.apply(full_prompt, "raw", spec)
    if include_people and not hold_cast:
        full_prompt = full_prompt + (
            "\n\nThe newly introduced character MUST be visible NOW, large in "
            "frame, in this same place. They are a stranger, not a clone of "
            "the player. Do not leave them for a later shot."
        )

    # Sanitize to avoid safety blocks
    full_prompt = _sanitize_for_safety(full_prompt)
    
    if len(full_prompt) > MAX_PROMPT_CHARS:
        print(f"[GEMINI IMG] WARNING: prompt is {len(full_prompt)} chars; "
              f"truncating to {MAX_PROMPT_CHARS}. The tail will not reach the model.", flush=True)
        full_prompt = full_prompt[:MAX_PROMPT_CHARS]
    
    # Model and resolution come from ai_config.json (see generate_with_gemini),
    # unless the caller names a size. A grid render is the case that needs to:
    # every panel is a SLICE of one generation, so a 2x2 grid at the play setting
    # of 1K yields 672x376 panels, and at that size the early panels come back
    # soft and full of drifting detail. Raising the ceiling for the whole game
    # instead would make every ordinary turn pay for it.
    import ai_provider_manager
    selected_model = model or resolve_model()
    selected_size = resolve_image_size()
    if image_size:
        want = str(image_size).strip().upper()
        allowed = ((ai_provider_manager.find_model("image", selected_model) or {})
                   .get("sizes") or list(ai_provider_manager.IMAGE_SIZES))
        if want in allowed:
            selected_size = want
        else:
            # Asking flash-lite for 4K would be refused on the wire and come back
            # as a blank frame, which reads as a generation failure rather than a
            # bad setting. Say so and use the best the model does offer.
            selected_size = allowed[-1]
            print(f"[GOOGLE GEMINI] {selected_model} does not offer {want}; "
                  f"using {selected_size} (offers {allowed})", flush=True)
    mode_name = "FAST MODE" if selected_model == GEMINI_FLASH_IMAGE else "RENDER MODE"

    print(f"[GOOGLE GEMINI {mode_name}] Editing image to show next moment...")
    print(f"[GOOGLE GEMINI {mode_name}] Using model: {selected_model} @ {selected_size}")
    safe_prompt = prompt[:100].encode('ascii', 'replace').decode('ascii')
    print(f"[GOOGLE GEMINI {mode_name}] Edit instructions: {safe_prompt}...")
    
    # Use selected model based on HD mode
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{selected_model}:generateContent"
    
    headers = {
        "x-goog-api-key": GEMINI_API_KEY,
        "Content-Type": "application/json"
    }
    
    # Build parts array: labeled references FIRST, then text prompt.
    # Labels sit next to each image so a character sheet is not read as
    # "the previous moment" the way unlabeled attachments were.
    parts = labeled_parts + [{"text": full_prompt}]
    
    payload = {
        "contents": [{
            "parts": parts
        }],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {
                # Conversation portraits stay wide; ensemble CAMP plates stay
                # 4:3 so mobile contain-fit matches those plates. Game stills
                # follow the renderer setting (Watch can pick 16:9 / 21:9).
                "aspectRatio": "4:3" if ensemble_mode else ("16:9" if portrait_mode else resolve_aspect_ratio()),
                "imageSize": selected_size
            }
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ]
    }
    
    try:
        # Dynamic timeout based on number of reference images
        # More images = more processing time needed (img2img with multiple refs is slow)
        # HD flipbooks (1200x896) need more time - increased for reliability
        timeout_seconds = 60 + (len(image_paths) * 15)  # 60s base + 15s per extra image (was 30+10)
        print(f"[GOOGLE GEMINI IMG2IMG] Using {timeout_seconds}s timeout for {len(image_paths)} reference image(s)", flush=True)
        
        max_retries = 1  # Don't waste time retrying slow calls
        for attempt in range(max_retries):
            try:
                print(f"[GOOGLE GEMINI IMG2IMG] Calling API NOW (attempt {attempt + 1})...", flush=True)
                response = requests.post(api_url, headers=headers, json=payload, timeout=timeout_seconds)
                print(f"[GOOGLE GEMINI IMG2IMG] API returned! Status: {response.status_code}", flush=True)
                response.raise_for_status()
                break
            except requests.exceptions.Timeout:
                print(f"[GOOGLE GEMINI IMG2IMG] TIMEOUT EXCEPTION CAUGHT!", flush=True)
                if attempt < max_retries - 1:
                    print(f"[GOOGLE GEMINI] Timeout on attempt {attempt + 1}, retrying...", flush=True)
                    continue
                else:
                    print(f"[GOOGLE GEMINI] ERROR: TIMEOUT after {timeout_seconds}s - Gemini API not responding!", flush=True)
                    return None  # Graceful fallback instead of crash
        
        print(f"[GOOGLE GEMINI IMG2IMG] Parsing response JSON...", flush=True)
        result = response.json()
        
        # Check for API errors first
        if "candidates" not in result:
            print(f"[GOOGLE GEMINI] ERROR: API error response: {result}", flush=True)
            if "error" in result:
                error_details = result['error']
                print(f"[GOOGLE GEMINI] ERROR: Error code: {error_details.get('code')}, Message: {error_details.get('message')}", flush=True)
            raise RuntimeError(f"Gemini image API error: {result.get('error', {}).get('message', 'Unknown error')}")
        
        print(f"[GOOGLE GEMINI IMG2IMG] Extracting image data...", flush=True)
        print(f"[GOOGLE GEMINI IMG2IMG] Response has {len(result.get('candidates', []))} candidates", flush=True)
        
        # Check for safety blocks
        if result["candidates"]:
            candidate = result["candidates"][0]
            print(f"[GOOGLE GEMINI IMG2IMG] Candidate keys: {list(candidate.keys())}", flush=True)
            
            # Check for finishReason (safety block detection)
            finish_reason = candidate.get("finishReason", "UNKNOWN")
            finish_message = candidate.get("finishMessage", "")
            print(f"[GOOGLE GEMINI IMG2IMG] Finish reason: {finish_reason}", flush=True)
            if finish_message:
                print(f"[GOOGLE GEMINI IMG2IMG] Finish message: {finish_message}", flush=True)
            
            # Check for any safety-related finish reasons
            if finish_reason in ["SAFETY", "IMAGE_SAFETY", "HARM", "PROHIBITED_CONTENT", "BLOCKED_CONTENT"]:
                safety_ratings = candidate.get("safetyRatings", [])
                print(f"[GOOGLE GEMINI IMG2IMG] SAFETY BLOCK! Reason: {finish_reason}", flush=True)
                print(f"[GOOGLE GEMINI IMG2IMG] Safety ratings: {safety_ratings}", flush=True)
                if finish_message:
                    print(f"[GOOGLE GEMINI IMG2IMG] Block message: {finish_message}", flush=True)
                return None  # Return None for safety blocks
            
            if "content" not in candidate:
                print(f"[GOOGLE GEMINI IMG2IMG] ERROR: No 'content' key in candidate. Full candidate: {candidate}", flush=True)
                return None
        
        # Extract image data
        parts = result["candidates"][0]["content"]["parts"]
        image_data_b64 = None
        
        for part in parts:
            if "inlineData" in part:
                image_data_b64 = part["inlineData"]["data"]
                break
        
        if not image_data_b64:
            raise RuntimeError("No image data in Gemini edit response")
        
        print(f"[GOOGLE GEMINI IMG2IMG] Decoding image data...", flush=True)
        # Decode and save
        image_bytes = base64.b64decode(image_data_b64)
        
        # Use provided output_dir or fall back to IMAGE_DIR
        save_dir = output_dir if output_dir is not None else IMAGE_DIR
        save_dir.mkdir(parents=True, exist_ok=True)
        
        safe_caption = "".join(c if c.isalnum() or c in "_-" else "_" for c in caption[:48])
        filename = f"{hash(caption) & 0xFFFFFFFF}_{safe_caption}.png"
        image_path = save_dir / filename
        
        print(f"[GOOGLE GEMINI IMG2IMG] Saving to {image_path}...", flush=True)
        write_image_atomic(image_path, image_bytes)
        
        # Downsample for API calls (maintain 4:3 aspect ratio) - do this ONCE, not per API call
        from PIL import Image as PILImage
        import io
        small_filename = filename.replace(".png", "_small.png")
        small_path = save_dir / small_filename
        
        try:
            img = PILImage.open(io.BytesIO(image_bytes))
            img = img.convert("RGB")
            img = img.resize((480, 360), PILImage.LANCZOS)  # 4:3 aspect ratio (matches full-size)
            save_pil_atomic(img, small_path, format="PNG", optimize=True, quality=85)
            print(f"[GOOGLE GEMINI] Edited image saved: {image_path}", flush=True)
            print(f"[GOOGLE GEMINI] Downsampled saved: {small_path} (480x360, 4:3 for API calls)", flush=True)
        except Exception as e:
            print(f"[GOOGLE GEMINI] WARNING: Downsample failed: {e}", flush=True)
        
        print(f"[GOOGLE GEMINI IMG2IMG] Returning path: {image_path}", flush=True)
        # Return relative path from session root
        return str(image_path)
        
    except Exception as e:
        print(f"[GOOGLE GEMINI] ERROR: Edit error: {type(e).__name__}: {e}", flush=True)
        import traceback
        print(f"[GOOGLE GEMINI] ERROR: Full traceback:", flush=True)
        traceback.print_exc()
        print(f"[GOOGLE GEMINI] ERROR: Returning None to allow game to continue", flush=True)
        return None  # Graceful fallback - don't crash the death sequence!

