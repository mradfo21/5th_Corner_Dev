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

# Google Gemini models
GEMINI_FLASH_IMAGE = "gemini-2.5-flash-image"  # Fast, 1-2 seconds
GEMINI_PRO_IMAGE = "gemini-3-pro-image-preview"  # Advanced, 4K support

# Track last corrected image for continuity
_last_corrected_image = None

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
    aspect_ratio: str = "16:9",
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
    if not GEMINI_API_KEY or not GEMINI_API_KEY.strip():
        raise ValueError(
            "❌ Google Gemini API key not configured!\n"
            "Get your key at: https://aistudio.google.com/apikey\n"
            "Add it to config.json as GEMINI_API_KEY"
        )
    
    # Override model based on HD mode
    if hd_mode:
        model = GEMINI_PRO_IMAGE
        print(f"🔷 [GOOGLE GEMINI] HD MODE ON: Using {model} for high quality (slower)")
    else:
        model = GEMINI_FLASH_IMAGE
        print(f"🔷 [GOOGLE GEMINI] HD MODE OFF: Using {model} for speed (lower quality)")
    
    # Load prompt template from JSON (single source of truth!)
    structured_prompt = PROMPTS["gemini_text_to_image_instructions"].format(prompt=prompt)
    
    # Add CRITICAL anti-border instructions
    anti_border = "\n\nCRITICAL - ABSOLUTELY NO BORDERS OR FRAMES:\nThe image MUST fill the ENTIRE canvas edge-to-edge with ZERO borders, frames, or edges of any kind. NO black bars, NO white borders, NO photo frames, NO matting, NO letterboxing. The content fills 100% of the image area. This is RAW FOOTAGE, not a framed photograph."
    
    structured_prompt = structured_prompt + anti_border
    
    # Add CRITICAL anti-person instructions
    anti_person = "\n\nCRITICAL - ABSOLUTELY NO PERSON/PLAYER VISIBLE:\nThis is a FIXED CAMERA VIEW mounted to a wall or tripod. The camera operator does NOT exist in this image. NEVER show ANY part of a human body - no head, no back of head, no shoulders, no arms, no hands, no legs, no feet, no torso, no silhouette. Show ONLY the environment - walls, floor, ceiling, objects, debris, sky, ground. Think: security camera footage, dashboard cam, surveillance view - PURE environmental shot with ZERO human presence in frame."
    
    structured_prompt = structured_prompt + anti_person
    
    # Add negative prompt emphasis
    negative_emphasis = "\n\nNEVER INCLUDE: Borders, frames, black bars, white borders, photo edges, polaroid frames, picture frames, matting, letterbox bars, any kind of border or frame element. Person visible, human visible, man visible, character visible, head visible, shoulders visible, back of head, person's back, body parts, hands, arms, legs, feet."
    
    structured_prompt = structured_prompt + negative_emphasis
    
    # Gemini has a 5000 char limit, so truncate if needed
    if len(structured_prompt) > 5000:
        structured_prompt = structured_prompt[:5000]
    
    # Sanitize to avoid safety blocks
    full_prompt = _sanitize_for_safety(structured_prompt)
    
    print(f"🔷 [GOOGLE GEMINI] Generating with {model}...")
    print(f"🔷 [GOOGLE GEMINI] Prompt: {full_prompt[:200]}...")
    
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
                "aspectRatio": "16:9"  # Authentic found footage aspect ratio
            }
        }
    }
    
    try:
        # Make the request with increased timeout and retry logic
        max_retries = 2
        for attempt in range(max_retries):
            try:
                response = requests.post(api_url, headers=headers, json=payload, timeout=120)
                response.raise_for_status()
                break
            except requests.exceptions.Timeout:
                if attempt < max_retries - 1:
                    print(f"⏱️ [GOOGLE GEMINI] Timeout on attempt {attempt + 1}, retrying...")
                    continue
                else:
                    raise
        
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
        
        # Downsample for API calls (maintain 16:9 aspect ratio) - do this ONCE, not per API call
        # Smaller = faster uploads, and Gemini Flash doesn't need high-res for text/vision tasks
        from PIL import Image as PILImage
        import io
        small_filename = filename.replace(".png", "_small.png")
        small_path = IMAGE_DIR / small_filename
        
        try:
            img = PILImage.open(io.BytesIO(image_bytes))
            img = img.convert("RGB")
            img = img.resize((480, 270), PILImage.LANCZOS)  # 16:9 aspect ratio for API calls
            img.save(small_path, format="PNG", optimize=True, quality=85)
            print(f"✅ [GOOGLE GEMINI] Image saved: {image_path} ({len(image_bytes)} bytes)")
            print(f"✅ [GOOGLE GEMINI] Downsampled saved: {small_path} (480x270 for API calls)")
        except Exception as e:
            print(f"⚠️ [GOOGLE GEMINI] Downsample failed: {e}")
        
        return f"/images/{filename}"
        
    except requests.exceptions.HTTPError as e:
        # If Pro model is overloaded and this is the first frame, fallback to Flash
        if e.response.status_code == 503 and is_first_frame and model == GEMINI_PRO_IMAGE:
            print(f"⚠️ [GOOGLE GEMINI] Pro model overloaded, falling back to Flash for first frame...")
            return generate_with_gemini(prompt, caption, world_prompt, aspect_ratio, GEMINI_FLASH_IMAGE, time_of_day, is_first_frame=False)
        
        if e.response.status_code == 401 or e.response.status_code == 403:
            print(f"❌ [GOOGLE GEMINI] Authentication failed! Check your API key.")
            print(f"   Get your key at: https://aistudio.google.com/apikey")
        elif e.response.status_code == 429:
            print(f"❌ [GOOGLE GEMINI] Rate limit exceeded!")
        else:
            print(f"❌ [GOOGLE GEMINI] HTTP Error {e.response.status_code}: {e.response.text}")
        raise
    except Exception as e:
        print(f"❌ [GOOGLE GEMINI] Unexpected error: {e}")
        raise


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
                    "aspectRatio": "16:9"
                }
            }
        }
        
        response = requests.post(api_url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        
        result = response.json()
        if "candidates" not in result or not result["candidates"]:
            print("⚠️ [FPS COMPOSITING] No result, using corrected image")
            return None
        
        image_b64 = result["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
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
        img = img.resize((480, 270), PILImage.LANCZOS)
        
        small_fps_filename = fps_filename.replace(".png", "_small.png")
        small_fps_path = corrected_pathobj.parent / small_fps_filename
        img.save(small_fps_path, format="PNG", optimize=True, quality=85)
        
        print(f"✅ [FPS COMPOSITING] Added hands: {fps_filename}")
        return f"/images/{fps_filename}"
        
    except Exception as e:
        print(f"⚠️ [FPS COMPOSITING] Failed: {e}, using corrected image")
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
            "Think: Security camera or chest-mounted GoPro - pure environmental view with NO camera operator visible in foreground."
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
                    "aspectRatio": "16:9"
                }
            }
        }
        
        response = requests.post(api_url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        
        result = response.json()
        if "candidates" not in result or not result["candidates"]:
            print("⚠️ [POV CORRECTION] No result, using original")
            return None
        
        image_b64 = result["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
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
        img = img.resize((480, 270), PILImage.LANCZOS)
        
        small_corrected_filename = corrected_filename.replace(".png", "_small.png")
        small_corrected_path = original_pathobj.parent / small_corrected_filename
        img.save(small_corrected_path, format="PNG", optimize=True, quality=85)
        
        print(f"✅ [POV CORRECTION] Applied: {corrected_filename}")
        return f"/images/{corrected_filename}"
        
    except Exception as e:
        print(f"⚠️ [POV CORRECTION] Failed: {e}, using original")
        return None


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
    
    print(f"🔷 [GOOGLE GEMINI] Image editing mode with {len(image_paths)} reference image(s)")
    
    # Read and encode all reference images (use downsampled versions if available)
    image_parts = []
    for img_path in image_paths:
        # Use pre-downsampled version if available (faster upload!)
        from pathlib import Path
        img_path_obj = Path(img_path)
        small_path = img_path_obj.parent / img_path_obj.name.replace(".png", "_small.png")
        use_path = small_path if small_path.exists() else img_path_obj
        
        with open(use_path, "rb") as f:
            image_bytes = f.read()
        image_b64 = base64.b64encode(image_bytes).decode('utf-8')
        
        # Determine MIME type
        mime_type = "image/png"
        if str(use_path).endswith(('.jpg', '.jpeg')):
            mime_type = "image/jpeg"
        
        size_note = "(480x270)" if small_path.exists() else "(full-res)"
        print(f"🔷 [GOOGLE GEMINI] Reference image {len(image_parts)+1}: {img_path_obj.name} {size_note}")
        
        image_parts.append({
            "inlineData": {
                "mimeType": mime_type,
                "data": image_b64
            }
        })
    
    # Load prompt template from JSON (single source of truth!)
    structured_prompt = PROMPTS["gemini_image_to_image_instructions"].format(prompt=prompt)
    
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
    
    # Add negative prompt emphasis
    negative_emphasis = "\n\nNEVER INCLUDE: Borders, frames, black bars, white borders, photo edges, polaroid frames, picture frames, matting, letterbox bars, any kind of border or frame element. Person visible, human visible, man visible, character visible, head visible, back of head, shoulders visible, person's back, character's back, body parts, hands, arms, legs, feet, torso, silhouette, person from behind."
    
    full_prompt = structured_prompt + negative_emphasis
    
    # Sanitize to avoid safety blocks
    full_prompt = _sanitize_for_safety(full_prompt)
    
    if len(full_prompt) > 5000:
        full_prompt = full_prompt[:5000]
    
    # Select model based on HD mode
    selected_model = GEMINI_PRO_IMAGE if hd_mode else GEMINI_FLASH_IMAGE
    mode_name = "HD MODE" if hd_mode else "FAST MODE"
    
    print(f"🔷 [GOOGLE GEMINI {mode_name}] Editing image to show next moment...")
    print(f"🔷 [GOOGLE GEMINI {mode_name}] Using model: {selected_model}")
    print(f"🔷 [GOOGLE GEMINI {mode_name}] Edit instructions: {prompt[:100]}...")
    
    # Use selected model based on HD mode
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{selected_model}:generateContent"
    
    headers = {
        "x-goog-api-key": GEMINI_API_KEY,
        "Content-Type": "application/json"
    }
    
    # Build parts array: text prompt + all reference images
    parts = [{"text": full_prompt}] + image_parts
    
    payload = {
        "contents": [{
            "parts": parts
        }],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {
                "aspectRatio": "16:9"  # Authentic found footage aspect ratio
            }
        }
    }
    
    try:
        # Make the request with increased timeout and retry logic
        max_retries = 2
        for attempt in range(max_retries):
            try:
                response = requests.post(api_url, headers=headers, json=payload, timeout=120)
                response.raise_for_status()
                break
            except requests.exceptions.Timeout:
                if attempt < max_retries - 1:
                    print(f"⏱️ [GOOGLE GEMINI] Timeout on attempt {attempt + 1}, retrying...")
                    continue
                else:
                    raise
        
        result = response.json()
        
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
        
        # Downsample for API calls (maintain 16:9 aspect ratio) - do this ONCE, not per API call
        from PIL import Image as PILImage
        import io
        small_filename = filename.replace(".png", "_small.png")
        small_path = IMAGE_DIR / small_filename
        
        try:
            img = PILImage.open(io.BytesIO(image_bytes))
            img = img.convert("RGB")
            img = img.resize((480, 270), PILImage.LANCZOS)  # 16:9 aspect ratio for API calls
            img.save(small_path, format="PNG", optimize=True, quality=85)
            print(f"✅ [GOOGLE GEMINI] Edited image saved: {image_path}")
            print(f"✅ [GOOGLE GEMINI] Downsampled saved: {small_path} (480x270 for API calls)")
        except Exception as e:
            print(f"⚠️ [GOOGLE GEMINI] Downsample failed: {e}")
        
        return f"/images/{filename}"
        
    except Exception as e:
        print(f"❌ [GOOGLE GEMINI] Edit error: {e}")
        raise

