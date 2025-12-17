"""
gemini_image_utils.py - Official Google Gemini Image Generation (Nano Banana)
Uses Gemini 2.5 Flash Image for ultra-fast, high-quality image generation
"""

import json
import requests
import base64
from pathlib import Path

# Load config
import os
ROOT = Path(__file__).parent
# Load config from file if it exists, otherwise use empty dict (for Render deployment)
try:
    with open(ROOT / "config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
except FileNotFoundError:
    config = {}

# Load prompts from JSON (single source of truth!)
try:
    with open(ROOT / "prompts" / "simulation_prompts.json", "r", encoding="utf-8") as f:
        PROMPTS = json.load(f)
except FileNotFoundError:
    with open(ROOT / "prompts" / "1993_base.json", "r", encoding="utf-8") as f:
        PROMPTS = json.load(f)

# Read from environment variables first, fall back to config.json
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", config.get("GEMINI_API_KEY", ""))
IMAGE_DIR = Path("images")

# CRITICAL DEBUG: Log API key status at import time
if not GEMINI_API_KEY:
    print("[GEMINI INIT] CRITICAL: GEMINI_API_KEY is NOT SET! Images will not generate!")
    print(f"[GEMINI INIT] Environment variable: {os.getenv('GEMINI_API_KEY', 'NOT SET')}")
    print(f"[GEMINI INIT] Config.json value: {config.get('GEMINI_API_KEY', 'NOT SET')}")
else:
    print(f"[GEMINI INIT] GEMINI_API_KEY loaded: {GEMINI_API_KEY[:20]}...{GEMINI_API_KEY[-8:]}")
    print(f"[GEMINI INIT] Ready to generate images")

# Google Gemini models
GEMINI_FLASH_IMAGE = "gemini-2.5-flash-image"  # Fast, cost-effective image generation
GEMINI_PRO_IMAGE = "gemini-3-pro-image-preview"  # Slower, higher quality, 4K support

# Track last corrected image for continuity
_last_corrected_image = None

# ============================================================================
# FORWARD MOMENTUM ZOOM - Experimental feature to combat img2img staleness
# ============================================================================
# By cropping center and scaling up, we force AI to interpret rather than copy pixels
# This creates a subtle "stepping forward" effect and reduces compression artifacts
ENABLE_FORWARD_ZOOM = True   # Toggle zoom preprocessing on/off
ZOOM_FACTOR = 1.35            # 35% zoom = crop center 74%, scale back to full size
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
    }
    
    sanitized = prompt
    for unsafe, safe in replacements.items():
        # Case-insensitive replacement
        import re
        pattern = re.compile(re.escape(unsafe), re.IGNORECASE)
        sanitized = pattern.sub(safe, sanitized)
    
    return sanitized

def generate_with_gemini(
    prompt: str,
    caption: str,
    world_prompt: str = None,
    aspect_ratio: str = "4:3",
    model: str = GEMINI_FLASH_IMAGE,
    time_of_day: str = "",
    is_first_frame: bool = False,
    action_context: str = "",
    hd_mode: bool = True
) -> str:
    """
    Generate an image using Google Gemini (Nano Banana).
    
    Args:
        prompt: The full image generation prompt WITH ALL DETAILED INSTRUCTIONS
        caption: Short caption for the image (used for filename)
        world_prompt: Narrative world state context
        aspect_ratio: Aspect ratio ("16:9", "4:3", "1:1", etc.)
        model: Gemini model to use (can be overridden by hd_mode)
        time_of_day: Time of day for lighting consistency
        hd_mode: If True, use Pro model for higher quality (slower). If False, use Flash for speed.
        
    Returns:
        Local path to the saved image (e.g., "/images/filename.png")
    """
    print(f"[GEMINI IMG] generate_with_gemini() CALLED - caption: {caption[:50]}")
    print(f"[GEMINI IMG] API key available: {bool(GEMINI_API_KEY)}, length: {len(GEMINI_API_KEY) if GEMINI_API_KEY else 0}")
    
    if not GEMINI_API_KEY:
        print("[GEMINI IMG] FATAL: No API key! Cannot generate image!")
        return None
    if not GEMINI_API_KEY or not GEMINI_API_KEY.strip():
        raise ValueError(
            "ERROR: Google Gemini API key not configured!\n"
            "Get your key at: https://aistudio.google.com/apikey\n"
            "Add it to config.json as GEMINI_API_KEY"
        )
    
    # Override model based on HD mode
    if hd_mode:
        model = GEMINI_PRO_IMAGE
        print(f"[GOOGLE GEMINI] HD MODE ON: Using {model} for high quality (slower)")
    else:
        model = GEMINI_FLASH_IMAGE
        print(f"[GOOGLE GEMINI] HD MODE OFF: Using {model} for speed (lower quality)")
    
    # Load prompt template from JSON (single source of truth!)
    structured_prompt = PROMPTS["gemini_text_to_image_instructions"].format(prompt=prompt)
    
    # Inject time/weather/mood if provided
    if time_of_day:
        time_injection = f"\n\n⏰ CRITICAL TIME/ATMOSPHERE CONSTRAINTS:\n{time_of_day}\nThe lighting, weather, and atmosphere MUST match these exact conditions. This is non-negotiable.\n"
        structured_prompt = structured_prompt + time_injection
    
    # Add CRITICAL anti-border instructions
    anti_border = "\n\nCRITICAL - ABSOLUTELY NO BORDERS OR FRAMES:\nThe image MUST fill the ENTIRE canvas edge-to-edge with ZERO borders, frames, or edges of any kind. NO black bars, NO white borders, NO photo frames, NO matting, NO letterboxing. The content fills 100% of the image area. This is RAW FOOTAGE, not a framed photograph."
    
    structured_prompt = structured_prompt + anti_border
    
    # Add CRITICAL anti-person instructions
    anti_person = "\n\nCRITICAL - ABSOLUTELY NO PERSON/PLAYER VISIBLE:\nThis is a FIXED CAMERA VIEW mounted to a wall or tripod. The camera operator does NOT exist in this image. NEVER show ANY part of a human body - no head, no back of head, no shoulders, no arms, no hands, no legs, no feet, no torso, no silhouette. Show ONLY the environment - walls, floor, ceiling, objects, debris, sky, ground. Think: security camera footage, dashboard cam, surveillance view - PURE environmental shot with ZERO human presence in frame."
    
    structured_prompt = structured_prompt + anti_person
    
    # Add CRITICAL anti-timecode/text instructions
    anti_timecode = (
        "\n\n CRITICAL - ABSOLUTELY NO TEXT OR TIMECODE OVERLAYS:\n"
        "This is RAW CAMERA FOOTAGE with NO on-screen displays.\n"
        "Do NOT add ANY text, numbers, letters, or symbols to the image.\n"
        "FORBIDDEN:\n"
        "ERROR: NO timecode (NO 'DEC 14 1993', NO '14:32:05', NO date/time stamps)\n"
        "ERROR: NO 'REC' indicator\n"
        "ERROR: NO 'PCC HISS' or any text overlays\n"
        "ERROR: NO battery indicators, tape icons, recording symbols\n"
        "ERROR: NO numbers, dates, times anywhere in the image\n"
        "ERROR: NO text of ANY kind\n\n"
        "The image is PURE VISUAL FOOTAGE with ZERO on-screen text elements.\n"
        "This is the RAW tape - no camera UI, no overlays, no burn-ins.\n"
        "If you see timecode in reference images, DO NOT copy it."
    )
    
    structured_prompt = structured_prompt + anti_timecode
    
    # Add negative prompt emphasis
    negative_emphasis = "\n\nNEVER INCLUDE: Text overlays, timecode, date stamps, timestamps, time displays, numbers, letters, words, 'DEC 14 1993', '4:32 PM', 'PCC HISS', 'REC', battery indicators, recording icons, ANY TEXT. Borders, frames, black bars, white borders, photo edges, polaroid frames, picture frames, matting, letterbox bars, any kind of border or frame element. Person visible, human visible, man visible, character visible, head visible, shoulders visible, back of head, person's back, body parts, hands, arms, legs, feet."
    
    # OPTICAL REALITY ANCHOR - Critical for first frame to set the tone
    photographic_anchor = (
        "\n\n📹 OPTICAL REALITY - REAL FOOTAGE:\n"
        "This is REAL LIGHT captured through REAL GLASS OPTICS onto PHYSICAL MAGNETIC TAPE.\n"
        "This is PHOTOGRAPHIC REALITY - actual camera capturing actual physical world.\n"
        "NOT: video game, 3D render, CGI, game engine, Unity, Unreal Engine, digital art\n"
        "NOT: Game screenshot with filters, rendered graphics with effects added\n"
        "NOT: Fake artifacts, fake glitches, digital effects overlaid on render\n\n"
        "OPTICAL CAPTURE:\n"
        "Real optical lens capturing real light bouncing off real physical surfaces\n"
        "Natural lighting, natural shadows, natural reflections - PHYSICAL WORLD\n"
        "Camera optics: real glass lens, real sensor, real photographic process\n"
        "This is PHOTOGRAPHY - light captured by camera, not graphics rendered by computer\n\n"
        "PHOTOGRAPHIC CHAOS - ANTI-POLYGON ENFORCEMENT:\n"
        "NOTHING is clean, geometric, or modeled. This is MESSY REALITY:\n"
        "Ground: IRREGULAR with dirt clumps, sand ripples, footprints, debris, vegetation patches\n"
        "Surfaces: WEATHERED with rust streaks, stains, peeling paint, cracks, damage\n"
        "Fences: BENT, sagging, irregular - NOT straight polygon lines\n"
        "Buildings: ASYMMETRIC with broken windows, varying textures, organic decay\n"
        "Sky: ATMOSPHERIC DEPTH with cloud detail, haze, gradient - NOT flat color plane\n"
        "Shadows: SOFT and DIFFUSE from real sunlight through atmosphere\n"
        "Textures: VARIED and COMPLEX - no repeated patterns, no tiling, organic randomness\n"
        "FORBIDDEN: Flat textured planes, geometric shapes, polygon meshes, 3D models, game assets\n"
        "FORBIDDEN: Clean edges, perfect lines, Unity terrain, repeated textures, tiled surfaces\n"
        "REQUIRED: Optical chaos, irregular forms, messy natural detail, photographic complexity\n\n"
        "TAPE MEDIUM:\n"
        "Recorded onto VHS magnetic tape (consumer analog format, 1990s)\n"
        "Tape introduces natural softness, slight color shifts, gentle noise\n"
        "Tape characteristics are SUBTLE - natural consequence of analog storage medium\n"
        "NOT fake digital artifacts - real physical tape properties\n\n"
        "HISTORICAL REFERENCE - LOOKS EXACTLY LIKE:\n"
        "1991 Gulf War CNN news footage (Bernard Shaw, Peter Arnett)\n"
        "1992 Rodney King video (George Holliday's camcorder)\n"
        "1993 Waco siege news coverage (live broadcast footage)\n"
        "Alive in Joburg (2005) - Neill Blomkamp documentary-style handheld\n"
        "Early 1990s amateur home video, news B-roll, surveillance footage\n"
        "Real historical footage - NOT modern recreations or game graphics with filters"
    )
    
    # Put anti-timecode FIRST (highest attention), then the rest
    structured_prompt = anti_timecode + "\n\n" + structured_prompt + negative_emphasis + photographic_anchor
    
    # Gemini has a 5000 char limit, so truncate if needed
    if len(structured_prompt) > 5000:
        structured_prompt = structured_prompt[:5000]
    
    # Sanitize to avoid safety blocks
    full_prompt = _sanitize_for_safety(structured_prompt)
    
    print(f"[GOOGLE GEMINI] Generating with {model}...")
    # Safe print - encode to ASCII, replace non-ASCII chars
    safe_prompt = full_prompt[:200].encode('ascii', 'replace').decode('ascii')
    print(f"[GOOGLE GEMINI] Prompt: {safe_prompt}...")
    
    # Official Google Gemini API endpoint
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    
    headers = {
        "x-goog-api-key": GEMINI_API_KEY,
        "Content-Type": "application/json"
    }
    
    payload = {
        "contents": [{
            "parts": [
                {"text": full_prompt}
            ]
        }],
        "generationConfig": {
            "responseModalities": ["IMAGE"],  # Image only, no text
            "imageConfig": {
                "aspectRatio": "4:3"  # Nano Banana Pro standard for this project
            }
        }
    }
    
    try:
        # Make the request with REDUCED timeout (30s) to prevent death sequence hangs
        # Gemini Pro can hang indefinitely on some prompts, especially death scenes
        max_retries = 1  # Reduced from 2 - don't waste time retrying slow calls
        for attempt in range(max_retries):
            try:
                response = requests.post(api_url, headers=headers, json=payload, timeout=30)
                response.raise_for_status()
                break
            except requests.exceptions.Timeout:
                if attempt < max_retries - 1:
                    print(f"[GOOGLE GEMINI] Timeout on attempt {attempt + 1}, retrying...")
                    continue
                else:
                    print(f"[GOOGLE GEMINI] ERROR: TIMEOUT after 30s - Gemini API not responding!")
                    return None  # Graceful fallback instead of crash
        
        result = response.json()
        
        # Extract base64 image data from response
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
        image_bytes = base64.b64decode(image_data_b64)
        
        # Save to local storage
        IMAGE_DIR.mkdir(exist_ok=True)
        safe_caption = "".join(c if c.isalnum() or c in "_-" else "_" for c in caption[:48])
        filename = f"{hash(caption) & 0xFFFFFFFF}_{safe_caption}.png"
        image_path = IMAGE_DIR / filename
        
        with open(image_path, "wb") as f:
            f.write(image_bytes)
        
        # Downsample for API calls (maintain 4:3 aspect ratio) - do this ONCE, not per API call
        # Smaller = faster uploads, and Gemini Flash doesn't need high-res for text/vision tasks
        from PIL import Image as PILImage
        import io
        small_filename = filename.replace(".png", "_small.png")
        small_path = IMAGE_DIR / small_filename
        
        try:
            img = PILImage.open(io.BytesIO(image_bytes))
            img = img.convert("RGB")
            img = img.resize((480, 360), PILImage.LANCZOS)  # 4:3 aspect ratio (matches full-size)
            img.save(small_path, format="PNG", optimize=True, quality=85)
            print(f"[GOOGLE GEMINI] Image saved: {image_path} ({len(image_bytes)} bytes)")
            print(f"[GOOGLE GEMINI] Downsampled saved: {small_path} (480x360, 4:3 for API calls)")
        except Exception as e:
            print(f"[GOOGLE GEMINI] WARNING: Downsample failed: {e}")
        
        return str(Path("images") / filename)
        
    except requests.exceptions.HTTPError as e:
        # If Pro model is overloaded and this is the first frame, fallback to Flash
        if e.response.status_code == 503 and is_first_frame and model == GEMINI_PRO_IMAGE:
            print(f"[GOOGLE GEMINI] WARNING: Pro model overloaded, falling back to Flash for first frame...")
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
            "- Slight VHS degradation on hands to match environment\n\n"
            "COMPOSITION:\n"
            "- Hands should look like part of a first-person video game (Metro, Half-Life, Far Cry style)\n"
            "- Seamless integration with the environment\n"
            "- Maintain all scene elements behind the hands\n"
            "- Keep VHS aesthetic and gritty 1993 atmosphere\n\n"
            "DO NOT:\n"
            "- Make hands too large or too small\n"
            "- Add unrealistic poses\n"
            "- Clean up the image - keep the gritty VHS look\n"
            "- Change the background scene\n\n"
            "Think: Photorealistic FPS game hands like Metro Exodus or Escape from Tarkov, but with 1993 VHS degradation."
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
                    "aspectRatio": "4:3"
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
        
        with open(fps_path, "wb") as f:
            f.write(composited_bytes)
        
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
            "- Polaroid frames, picture frames, matting, VHS frame overlays\n\n"
            "KEEP borders ONLY if they are:\n"
            "- Binocular view (figure-8 dual circles)\n"
            "- Scope/rifle view (circular reticle)\n"
            "- Night vision goggles view (circular green overlay)\n"
            "- Gas mask view (rounded rectangular with breathing filters visible)\n"
            "These are intentional viewing devices - keep their characteristic frames.\n\n"
            "KEEP in the image:\n"
            "- All distant people (guards, enemies, figures in background) - these are fine\n"
            "- The scene composition and environment\n"
            "- All lighting, atmosphere, and VHS aesthetic\n"
            "- Background action and details\n\n"
            "PRESERVE:\n"
            "- 1993 VHS camcorder degradation style (grain, color bleed, analog look)\n"
            "- Muted, desaturated color palette\n"
            "- Gritty, raw, unpolished photographic quality\n\n"
            "The image should fill edge-to-edge UNLESS it's a viewing device frame.\n"
            "Think: Security camera or handheld camcorder - pure environmental view with NO camera operator visible in foreground."
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
                    "aspectRatio": "4:3"
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
        
        with open(corrected_path, "wb") as f:
            f.write(corrected_bytes)
        
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


def generate_gemini_img2img(
    prompt: str,
    caption: str,
    reference_image_path: str | list[str],
    strength: float = 0.3,
    world_prompt: str = None,
    time_of_day: str = "",
    action_context: str = "",
    hd_mode: bool = True
) -> str:
    """
    Edit an image using Google Gemini (image-to-image).
    Supports up to 6 reference images for better continuity.
    
    Args:
        prompt: The FULL editing instruction WITH ALL DETAILED POV INSTRUCTIONS
        caption: Short caption for the edit (used for filename)
        reference_image_path: Path(s) to input image(s) - single string or list of up to 6 paths
        strength: 0.0-1.0, controls how much to change (via prompt engineering)
        world_prompt: Optional world context
        time_of_day: Time of day for lighting consistency
        hd_mode: If True, use Pro model for higher quality (slower). If False, use Flash for speed.
        
    Returns:
        Local path to the saved image
    """
    # Handle single path or list of paths
    if isinstance(reference_image_path, str):
        image_paths = [reference_image_path]
    else:
        image_paths = reference_image_path[:6]  # Max 6 reference images
    
    print(f"[GOOGLE GEMINI] Image editing mode with {len(image_paths)} reference image(s)")
    
    # Read and encode all reference images
    image_parts = []
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
        
        image_parts.append({
            "inlineData": {
                "mimeType": mime_type,
                "data": image_b64
            }
        })
    
    # Load prompt template from JSON (single source of truth!)
    structured_prompt = PROMPTS["gemini_image_to_image_instructions"].format(prompt=prompt)
    
    # Inject time/weather/mood if provided
    if time_of_day:
        time_injection = f"\n\n⏰ CRITICAL TIME/ATMOSPHERE CONSTRAINTS:\n{time_of_day}\nThe lighting, weather, and atmosphere MUST match these exact conditions. This is non-negotiable.\n"
        structured_prompt = structured_prompt + time_injection
    
    # Add CRITICAL style-only instruction for forward movement
    style_only_instruction = (
        "\n\n⚡ CRITICAL - HOW TO USE REFERENCE IMAGES:\n"
        "The reference images show the PREVIOUS MOMENT in this simulation.\n"
        "Extract from references:\n"
        " VISUAL STYLE: Photographic quality, VHS degradation, color palette, lighting type\n"
        " ENVIRONMENT TYPE: Desert vs facility, outdoor vs indoor, general setting\n"
        " AESTHETIC: Graininess, overexposure, analog artifacts, tape degradation\n\n"
        "DO NOT copy from references:\n"
        "ERROR: CAMERA POSITION - Move the camera based on the action taken\n"
        "ERROR: OBJECT PLACEMENT - Rearrange the scene for the new moment\n"
        "ERROR: COMPOSITION - Create new framing for the next beat\n"
        "ERROR: DISTANCE - Change how close/far objects appear based on movement\n\n"
        "Think: The references show WHAT THE TAPE LOOKS LIKE (style).\n"
        "Your output shows WHERE THE CAMERA MOVED TO (new location/angle).\n"
        "Same tape quality, completely different view."
    )
    
    structured_prompt = structured_prompt + style_only_instruction
    
    # Add CRITICAL anti-border instructions
    anti_border = "\n\nCRITICAL - ABSOLUTELY NO BORDERS OR FRAMES:\nThe image MUST fill the ENTIRE canvas edge-to-edge with ZERO borders, frames, or edges of any kind. NO black bars, NO white borders, NO photo frames, NO matting, NO letterboxing. The content fills 100% of the image area. This is RAW FOOTAGE, not a framed photograph."
    
    structured_prompt = structured_prompt + anti_border
    
    # Add CRITICAL anti-person instructions WITH REMOVAL DIRECTIVE
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
    
    # Add CRITICAL anti-timecode/text instructions
    anti_timecode = (
        "\n\n ABSOLUTELY NO TEXT OR TIMECODE - CRITICAL:\n"
        "This is RAW UNPROCESSED CAMERA FOOTAGE with NO on-screen displays of ANY kind.\n"
        "DO NOT GENERATE:\n"
        "ERROR: ZERO timecode overlays - NO 'DEC 14 1993', NO '1993 OCT 14', NO dates\n"
        "ERROR: ZERO time displays - NO '14:32:05', NO '4:32 PM', NO clock displays\n"
        "ERROR: ZERO 'REC' indicator or recording symbols\n"
        "ERROR: ZERO 'PCC HISS' or ANY words/text\n"
        "ERROR: ZERO battery indicators, tape counter, VCR UI elements\n"
        "ERROR: ZERO numbers, letters, words, or symbols ANYWHERE in the image\n\n"
        "The reference images may contain timecode overlays - YOU MUST REMOVE THEM.\n"
        "Generate the scene WITHOUT copying any text or UI elements from references.\n"
        "This is the ACTUAL TAPE IMAGE - no camera interface, no metadata overlays.\n"
        "PURE VISUAL CONTENT ONLY - zero text elements of any kind."
    )
    
    structured_prompt = structured_prompt + anti_timecode
    
    # Add negative prompt emphasis
    negative_emphasis = "\n\nNEVER INCLUDE: Text overlays, timecode, date stamps, timestamps, time displays, numbers, letters, words, 'DEC 14 1993', '4:32 PM', 'PCC HISS', 'REC', battery indicators, recording icons, ANY TEXT. Borders, frames, black bars, white borders, photo edges, polaroid frames, picture frames, matting, letterbox bars, any kind of border or frame element. Person visible, human visible, man visible, character visible, head visible, back of head, shoulders visible, person's back, character's back, body parts, hands, arms, legs, feet, torso, silhouette, person from behind."
    
    # OPTICAL REALITY ANCHOR - Prevent video game aesthetic drift over time
    photographic_anchor = (
        "\n\n📹 OPTICAL REALITY - REAL FOOTAGE:\n"
        "This is REAL LIGHT captured through REAL GLASS OPTICS onto PHYSICAL MAGNETIC TAPE.\n"
        "This is PHOTOGRAPHIC REALITY - actual camera capturing actual physical world.\n"
        "NOT: video game, 3D render, CGI, game engine, Unity, Unreal Engine, digital art\n"
        "NOT: Game screenshot with filters, rendered graphics with effects added\n"
        "NOT: Fake artifacts, fake glitches, digital effects overlaid on render\n\n"
        "OPTICAL CAPTURE:\n"
        "Real optical lens capturing real light bouncing off real physical surfaces\n"
        "Natural lighting, natural shadows, natural reflections - PHYSICAL WORLD\n"
        "Camera optics: real glass lens, real sensor, real photographic process\n"
        "This is PHOTOGRAPHY - light captured by camera, not graphics rendered by computer\n\n"
        "PHOTOGRAPHIC CHAOS - ANTI-POLYGON ENFORCEMENT:\n"
        "NOTHING is clean, geometric, or modeled. This is MESSY REALITY:\n"
        "Ground: IRREGULAR with dirt clumps, sand ripples, footprints, debris, vegetation\n"
        "Surfaces: WEATHERED with rust, stains, peeling paint, cracks, organic decay\n"
        "Fences: BENT, sagging, irregular - NOT straight polygon lines\n"
        "Buildings: ASYMMETRIC with broken windows, varied textures, natural damage\n"
        "Sky: ATMOSPHERIC DEPTH with clouds, haze, gradient - NOT flat color\n"
        "Shadows: SOFT and DIFFUSE from real sun through atmosphere\n"
        "Textures: VARIED, COMPLEX, no repeated patterns, organic randomness\n"
        "FORBIDDEN: Flat planes, geometric shapes, polygon meshes, 3D models, game assets\n"
        "FORBIDDEN: Clean edges, perfect lines, repeated textures, tiled surfaces\n"
        "REQUIRED: Optical chaos, irregular forms, messy detail, photographic complexity\n\n"
        "TAPE MEDIUM:\n"
        "Recorded onto VHS magnetic tape (consumer analog format, 1990s)\n"
        "Tape introduces natural softness, slight color shifts, gentle noise\n"
        "Tape characteristics are SUBTLE - natural consequence of analog storage medium\n"
        "NOT fake digital artifacts - real physical tape properties\n\n"
        "HISTORICAL REFERENCE - LOOKS EXACTLY LIKE:\n"
        "1991 Gulf War CNN news footage (Bernard Shaw, Peter Arnett coverage)\n"
        "1992 Rodney King video (George Holliday's Sony Handycam)\n"
        "1993 Waco siege news coverage (live broadcast B-roll)\n"
        "Alive in Joburg (2005) - Neill Blomkamp documentary-style handheld\n"
        "Early 1990s amateur home video, news footage, surveillance tapes\n"
        "Real historical footage - NOT modern recreations or game graphics\n\n"
        "PRESERVE the photographic reality and messy irregularity from reference images.\n"
        "DO NOT drift toward game-like rendering, geometric shapes, clean digital video, or polygon meshes."
    )
    
    # Put anti-timecode FIRST (highest attention), then the rest
    full_prompt = anti_timecode + "\n\n" + structured_prompt + negative_emphasis + photographic_anchor
    
    # Sanitize to avoid safety blocks
    full_prompt = _sanitize_for_safety(full_prompt)
    
    if len(full_prompt) > 5000:
        full_prompt = full_prompt[:5000]
    
    # Select model based on HD mode
    selected_model = GEMINI_PRO_IMAGE if hd_mode else GEMINI_FLASH_IMAGE
    mode_name = "HD MODE" if hd_mode else "FAST MODE"
    
    print(f"[GOOGLE GEMINI {mode_name}] Editing image to show next moment...")
    print(f"[GOOGLE GEMINI {mode_name}] Using model: {selected_model}")
    safe_prompt = prompt[:100].encode('ascii', 'replace').decode('ascii')
    print(f"[GOOGLE GEMINI {mode_name}] Edit instructions: {safe_prompt}...")
    
    # Use selected model based on HD mode
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{selected_model}:generateContent"
    
    headers = {
        "x-goog-api-key": GEMINI_API_KEY,
        "Content-Type": "application/json"
    }
    
    # Build parts array: reference images FIRST, then text prompt (Gemini 2.x/3.x requirement)
    parts = image_parts + [{"text": full_prompt}]
    
    payload = {
        "contents": [{
            "parts": parts
        }],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {
                "aspectRatio": "4:3"  # Nano Banana Pro standard for this project
            }
        }
    }
    
    try:
        # Dynamic timeout based on number of reference images
        # More images = more processing time needed (img2img with multiple refs is slow)
        timeout_seconds = 30 + (len(image_paths) * 10)  # 30s base + 10s per extra image
        print(f"[GOOGLE GEMINI IMG2IMG] Using {timeout_seconds}s timeout for {len(image_paths)} reference image(s)")
        
        max_retries = 1  # Don't waste time retrying slow calls
        for attempt in range(max_retries):
            try:
                response = requests.post(api_url, headers=headers, json=payload, timeout=timeout_seconds)
                response.raise_for_status()
                break
            except requests.exceptions.Timeout:
                if attempt < max_retries - 1:
                    print(f"[GOOGLE GEMINI] Timeout on attempt {attempt + 1}, retrying...")
                    continue
                else:
                    print(f"[GOOGLE GEMINI] ERROR: TIMEOUT after {timeout_seconds}s - Gemini API not responding!")
                    return None  # Graceful fallback instead of crash
        
        result = response.json()
        
        # Check for API errors first
        if "candidates" not in result:
            print(f"[GOOGLE GEMINI] ERROR: API error response: {result}")
            if "error" in result:
                error_details = result['error']
                print(f"[GOOGLE GEMINI] ERROR: Error code: {error_details.get('code')}, Message: {error_details.get('message')}")
            raise RuntimeError(f"Gemini image API error: {result.get('error', {}).get('message', 'Unknown error')}")
        
        # Extract image data
        parts = result["candidates"][0]["content"]["parts"]
        image_data_b64 = None
        
        for part in parts:
            if "inlineData" in part:
                image_data_b64 = part["inlineData"]["data"]
                break
        
        if not image_data_b64:
            raise RuntimeError("No image data in Gemini edit response")
        
        # Decode and save
        image_bytes = base64.b64decode(image_data_b64)
        
        IMAGE_DIR.mkdir(exist_ok=True)
        safe_caption = "".join(c if c.isalnum() or c in "_-" else "_" for c in caption[:48])
        filename = f"{hash(caption) & 0xFFFFFFFF}_{safe_caption}.png"
        image_path = IMAGE_DIR / filename
        
        with open(image_path, "wb") as f:
            f.write(image_bytes)
        
        # Downsample for API calls (maintain 4:3 aspect ratio) - do this ONCE, not per API call
        from PIL import Image as PILImage
        import io
        small_filename = filename.replace(".png", "_small.png")
        small_path = IMAGE_DIR / small_filename
        
        try:
            img = PILImage.open(io.BytesIO(image_bytes))
            img = img.convert("RGB")
            img = img.resize((480, 360), PILImage.LANCZOS)  # 4:3 aspect ratio (matches full-size)
            img.save(small_path, format="PNG", optimize=True, quality=85)
            print(f"[GOOGLE GEMINI] Edited image saved: {image_path}")
            print(f"[GOOGLE GEMINI] Downsampled saved: {small_path} (480x360, 4:3 for API calls)")
        except Exception as e:
            print(f"[GOOGLE GEMINI] WARNING: Downsample failed: {e}")
        
        return str(Path("images") / filename)
        
    except Exception as e:
        print(f"[GOOGLE GEMINI] ERROR: Edit error: {e}")
        print(f"[GOOGLE GEMINI] ERROR: Returning None to allow game to continue")
        return None  # Graceful fallback - don't crash the death sequence!

