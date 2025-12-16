"""
veo_video_utils.py - Google Veo 3.1 Video-Based Image Generation

Instead of generating static images, we generate short videos and extract
the last frame. This provides natural consistency and cinematic motion.

Flow:
1. Take previous frame as first_frame
2. Generate 4-second video with action prompt
3. Extract last frame as new "image"
4. Save video for later film compilation
"""

import os
import json
import time
import base64
import requests
from pathlib import Path
from typing import Optional, Tuple, List
from datetime import datetime, timezone
import cv2
import numpy as np

# Check if we can use ffmpeg for audio-preserved video stitching
import subprocess
import shutil

# Try to use imageio-ffmpeg's bundled ffmpeg binary
FFMPEG_PATH = None
try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    print(f"[VIDEO STITCH] Using imageio-ffmpeg bundled binary: {FFMPEG_PATH}")
except:
    # Fall back to system ffmpeg
    FFMPEG_PATH = shutil.which("ffmpeg")
    if FFMPEG_PATH:
        print(f"[VIDEO STITCH] Using system ffmpeg: {FFMPEG_PATH}")
    else:
        print("[VIDEO STITCH] Warning: ffmpeg not found, audio will be lost in stitched videos")

FFMPEG_AVAILABLE = FFMPEG_PATH is not None

# Load config
ROOT = Path(__file__).parent
try:
    with open(ROOT / "config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
except FileNotFoundError:
    config = {}

# API Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", config.get("GEMINI_API_KEY", ""))
VEO_MODEL = "veo-3.1-generate-preview"
VEO_FAST_MODEL = "veo-3.1-fast-generate-preview"

# Directories
IMAGE_DIR = Path("images")
VIDEO_SEGMENTS_DIR = Path("films/segments")
VIDEO_SEGMENTS_DIR.mkdir(parents=True, exist_ok=True)
VIDEO_FILMS_DIR = Path("films/final")
VIDEO_FILMS_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================================
# COST CONTROL SETTINGS
# ============================================================================
# Veo is expensive! These settings help control costs.

# Model Selection
USE_FAST_MODEL = True   #  RECOMMENDED: Use veo-3.1-fast (50% cheaper, 3x faster)

# Video Duration
VIDEO_DURATION = 4      #  RECOMMENDED: 4 seconds (MINIMUM - Veo API limitation)
                        # Options: 4, 6, or 8 seconds ONLY
                        # 4s is the cheapest and fastest option available

# Cost Caps (per session)
MAX_FRAMES = 999              #  REMOVED: No hard frame limit, only budget limit
MAX_SESSION_COST = 2.00       # Maximum spend per game session ($USD) - increased for longer sessions
WARN_AT_COST = 1.50           # Warn when approaching limit
ESTIMATED_COST_PER_VIDEO = 0.05  # Conservative estimate (fast model, 4s)

# Hybrid Mode (use Veo selectively)
VEO_MODE = "full"             # Options: "full", "hybrid", "minimal"
                              # "full": Use Veo for all frames (expensive)
                              # "hybrid": Use Veo only for Frame 0→1 and hard transitions
                              # "minimal": Use Veo only for Frame 0→1, then Gemini img2img

# Polling
MAX_POLL_TIME = 300  # 5 minutes max wait for video generation

# ============================================================================
# SESSION COST TRACKING
# ============================================================================
_session_costs = {
    "veo_calls": 0,
    "total_cost": 0.0,
    "videos_generated": [],
    "frames_skipped": 0,
    "total_frames_generated": 0,
    "last_video_operation": None  # Store the last video for extension
}

# ============================================================================
# COST TRACKING FUNCTIONS
# ============================================================================

def _track_video_cost(duration: int, use_fast: bool = True):
    """Track cost for a generated video."""
    # Pricing (as of 2024):
    # veo-3.1-generate-preview: ~$0.05 per 4s video
    # veo-3.1-fast-generate-preview: ~$0.05 per 4s video (same price, faster)
    # Duration multiplier: 4s = 1x, 6s = 1.5x, 8s = 2x
    
    base_cost = 0.05  # Base cost per 4-second video
    duration_multiplier = duration / 4.0
    
    cost = base_cost * duration_multiplier
    
    _session_costs["veo_calls"] += 1
    _session_costs["total_cost"] += cost
    _session_costs["videos_generated"].append({
        "duration": duration,
        "cost": cost,
        "timestamp": datetime.now(timezone.utc).isoformat()
    })
    
    print(f"[VEO COST] Video Generated")
    print(f"[VEO COST]   Duration: {duration}s")
    print(f"[VEO COST]   Model: veo-3.1-{'fast' if use_fast else 'preview'}")
    print(f"[VEO COST]   Estimated Cost: ${cost:.4f}")
    print(f"[VEO COST]   Session Total: ${_session_costs['total_cost']:.2f} / ${MAX_SESSION_COST:.2f}")
    print(f"[VEO COST]   Videos Generated: {_session_costs['veo_calls']}")
    print(f"[VEO COST]   Frames Skipped: {_session_costs['frames_skipped']}")
    
    # Warn if approaching limit
    if _session_costs["total_cost"] >= WARN_AT_COST:
        print(f"[VEO COST] WARNING: Approaching budget limit!")
        print(f"[VEO COST] Remaining: ${MAX_SESSION_COST - _session_costs['total_cost']:.2f}")


def _can_afford_veo() -> bool:
    """Check if we can afford another Veo video."""
    estimated_cost = ESTIMATED_COST_PER_VIDEO
    remaining = MAX_SESSION_COST - _session_costs["total_cost"]
    return remaining >= estimated_cost


def _should_use_veo(frame_idx: int, is_hard_transition: bool = False) -> bool:
    """
    Decide whether to use Veo for this frame based on mode and budget.
    
    Args:
        frame_idx: Current frame index
        is_hard_transition: Whether this is a location change
    
    Returns:
        True if should use Veo, False to fall back to Gemini
    """
    # BUDGET LIMIT: Only stop if we can't afford another video
    # Frame limit removed - rely on budget control only
    
    # Frame 0 always uses Gemini (seed)
    if frame_idx == 0:
        return False
    
    # Check budget first
    if not _can_afford_veo():
        print(f"[VEO COST] Budget limit reached - stopping generation")
        print(f"[VEO COST] Spent: ${_session_costs['total_cost']:.2f} / ${MAX_SESSION_COST:.2f}")
        return False
    
    # Mode-based logic
    if VEO_MODE == "full":
        return True  # Use Veo for all frames
    elif VEO_MODE == "hybrid":
        # Use Veo for Frame 0→1 and hard transitions
        if frame_idx == 1 or is_hard_transition:
            return True
        return False
    elif VEO_MODE == "minimal":
        # Use Veo only for Frame 0→1
        return frame_idx == 1
    
    return False


def generate_frame_via_video(
    prompt: str,
    first_frame_path: Optional[str],
    caption: str,
    frame_idx: int,
    world_prompt: str = "",
    action_context: str = "",
    reference_frames: Optional[List[str]] = None
) -> Tuple[Optional[str], str, Optional[str]]:
    """
    Generate a new frame by creating a video from the previous frame.
    
    Args:
        prompt: Full image generation prompt (will be converted to cinematic)
        first_frame_path: Path to starting frame for the video (None for frame 0)
        caption: Short caption for filename
        frame_idx: Frame index
        world_prompt: World context
        action_context: Player action
        reference_frames: List of previous frame paths to use as visual references (1-2 frames)
    
    Returns:
        Tuple of (frame_path, prompt_used, video_path)
        video_path is None for Frame 0 (static image) or if video generation fails
    """
    print(f"\n{'='*70}")
    print(f"[VEO] Generating frame {frame_idx} via video interpolation")
    print(f"{'='*70}")
    
    # FRAME 0: Generate static image with Gemini (seed frame)
    if frame_idx == 0 or first_frame_path is None:
        # No frame limit check - budget control handles it
        
        print(f"[VEO] Frame 0 - Generating seed image with Gemini")
        from gemini_image_utils import generate_with_gemini
        
        result_path = generate_with_gemini(
            prompt=prompt,
            caption=caption,
            world_prompt=world_prompt,
            aspect_ratio="4:3",
            time_of_day="",
            is_first_frame=True,
            action_context=action_context,
            hd_mode=True
        )
        
        # Track frame count
        _session_costs["total_frames_generated"] += 1
        
        print(f"[VEO] Seed frame generated: {result_path}")
        print(f"[VEO] Frame {_session_costs['total_frames_generated']} generated (cost: ${_session_costs['total_cost']:.2f}/${MAX_SESSION_COST:.2f})")
        return (result_path, prompt, None)  # No video for Frame 0
    
    # COST CONTROL: Check if we should use Veo for this frame
    # Detect hard transition from action context
    is_hard_transition = any(word in action_context.lower() for word in [
        "enter", "leave", "climb", "descend", "teleport", "warp"
    ])
    
    use_veo = _should_use_veo(frame_idx, is_hard_transition)
    
    if not use_veo:
        # Stop generating frames - frame limit or budget reached
        print(f"[VEO] Stopping frame generation at frame {frame_idx}")
        return (None, "", None)
    
    # FRAMES 1+: Generate video, extract last frame
    try:
        # Convert image prompt to cinematic video prompt
        veo_prompt = _build_veo_cinematic_prompt(prompt, action_context)
        
        safe_path = str(first_frame_path).encode('ascii', 'replace').decode('ascii')
        print(f"[VEO] First frame: {safe_path}")
        safe_prompt = veo_prompt[:200].encode('ascii', 'replace').decode('ascii')
        print(f"[VEO] Prompt: {safe_prompt}...")
        
        # Generate video via Veo 3.1 (image-to-video with reference images)
        video_path, last_frame_path, operation = _generate_video_and_extract_frame(
            first_frame_path=first_frame_path,
            prompt=veo_prompt,
            frame_idx=frame_idx,
            caption=caption,
            reference_frames=reference_frames  # Pass previous frames as references
        )
        
        safe_video_path = str(video_path).encode('ascii', 'replace').decode('ascii') if video_path else "None"
        safe_last_frame = str(last_frame_path).encode('ascii', 'replace').decode('ascii') if last_frame_path else "None"
        print(f"[VEO] Video generated: {safe_video_path}")
        print(f"[VEO] Last frame extracted: {safe_last_frame}")
        
        return (last_frame_path, veo_prompt, video_path)  # Return video path for playback
        
    except Exception as e:
        safe_error = str(e).encode('ascii', 'replace').decode('ascii')
        print(f"[VEO] Error: {safe_error}")
        import traceback
        traceback.print_exc()
        print(f"[VEO] Falling back to Gemini image generation")
        
        # Fallback to static image generation
        from gemini_image_utils import generate_gemini_img2img
        
        result_path = generate_gemini_img2img(
            prompt=prompt,
            caption=caption,
            reference_image_path=first_frame_path,
            strength=0.3,
            world_prompt=world_prompt,
            time_of_day="",
            action_context=action_context,
            hd_mode=True
        )
        
        return (result_path, prompt, None)  # No video on fallback


def _build_veo_cinematic_prompt(base_prompt: str, action: str) -> str:
    """
    Streamlined Veo prompt optimized for video generation.
    Focuses on essential visual rules without verbose explanations.
    
    Args:
        base_prompt: Visual description of the scene/consequence (the vision_dispatch)
        action: Player's choice text
    
    Both are included to give Veo full context of what happened and what to show.
    """
    # Condensed prompt optimized for Veo (keeps critical rules, removes verbose explanations)
    condensed_prompt = f"""SCENE: {base_prompt}

FIRST-PERSON POV (CRITICAL):
Camera IS your eyes. NEVER show the player character. Pure environmental view from eye-level (4-5 feet). Handheld VHS camcorder. You cannot see your own body - only what's in front of you.

VHS FOUND FOOTAGE AESTHETIC (CRITICAL):
Consumer-grade 1990s VHS camcorder on DEGRADED magnetic tape. Heavy analog artifacts: prominent grain, color bleeding, chromatic aberration, severe desaturation, overexposed highlights blown to white, crushed blacks, motion blur, interlacing, lo-fi NTSC (480i). Think Blair Witch Project - raw, degraded, 30-year-old tape quality. NOT clean digital video.

ABSOLUTELY NO TEXT OR OVERLAYS:
NO timecodes, NO "DEC 14 1993", NO timestamps, NO battery indicators, NO REC symbols, NO tracking noise borders, NO UI elements. Image fills entire frame edge-to-edge. Degradation is IN the image quality itself, not as overlays.

1993 GROUNDED REALISM (CRITICAL):
ONLY 1993 technology and threats. Human guards (black tactical gear, rifles), biological creatures (mutated flesh/bone - NOT robots), environmental hazards. NO sci-fi: NO robots, mechs, cyborgs, energy weapons, holograms, lasers, futuristic vehicles. ONLY 1993 tech: VHS cameras, CRT monitors, chain-link fences, concrete bunkers, 1990s trucks/Humvees. X-Files aesthetic - grounded and gritty.

VIDEO MOTION (Action: {action}):
FIRST-PERSON handheld movement. Camera physically moves with your body: walking = gentle bob, running = aggressive shake, turning = smooth pan. Natural head movements looking around. Found footage style - amateur, shaky, reactive. Camera reacts to environment (stumbling, recoiling). Smooth 4-second continuous shot from first frame to final frame. NEVER third-person - camera IS your eyes.

CONTINUITY:
Reference images show CURRENT location. Stay in SAME environment unless action explicitly moves you (e.g., "enter door", "climb ladder"). Same lighting, structures, spatial arrangement. Show PROGRESSION of action - dramatic forward movement, consequences, changes. Not stasis.

PERSPECTIVE RULES:
When YOU act: Show the OUTCOME ("raises binoculars" = view THROUGH lenses, "opens door" = what's BEYOND door).
When threats act ON YOU: Show from VICTIM perspective ("guard grabs you" = hands REACHING TOWARD camera, "sniper aims" = rifle barrel POINTED AT camera).
Camera IS your eyes - show world as YOU experience it.

PHYSICAL CAMERA CONSTRAINTS:
Camera held by human. Height: 4-5 feet (lower if crouching ~2-3 feet). Minimum distance to objects: 1.5-3 feet (human body has depth - cannot press flush against walls). Always show environmental CONTEXT - WHERE you are, not just isolated objects. Wide field of view maintaining spatial awareness."""
    
    return condensed_prompt.strip()


def _generate_video_and_extract_frame(
    first_frame_path: str,
    prompt: str,
    frame_idx: int,
    caption: str,
    reference_frames: Optional[List[str]] = None
) -> Tuple[str, str, any]:
    """
    Generate video using Veo 3.1 API and extract last frame.
    
    Returns:
        Tuple of (video_path, last_frame_path)
    """
    from google import genai
    from google.genai import types
    from PIL import Image
    import base64
    
    if not GEMINI_API_KEY:
        raise Exception("GEMINI_API_KEY not set")
    
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    # Choose model
    model = VEO_FAST_MODEL if USE_FAST_MODEL else VEO_MODEL
    
    print(f"[VEO API] Sending request to {model}...")
    safe_path = str(first_frame_path).encode('ascii', 'replace').decode('ascii')
    print(f"[VEO API] First frame: {safe_path}")
    print(f"[VEO API] Duration: {VIDEO_DURATION}s")
    
    # Always use IMAGE-TO-VIDEO mode (no video extension to avoid compression accumulation)
    # Use reference images from previous frames for continuity
    
    if first_frame_path and Path(first_frame_path).exists():
        # IMAGE-TO-VIDEO: Use seed frame as input
        print(f"[VEO API] Mode: IMAGE-TO-VIDEO (using seed frame: {safe_path})")
        
        # Convert the first frame to Veo-compatible format
        pil_image = Image.open(first_frame_path)
        
        # Convert PIL Image to bytes for Gemini
        import io
        img_byte_arr = io.BytesIO()
        pil_image.save(img_byte_arr, format='PNG')
        img_byte_arr.seek(0)
        image_data = img_byte_arr.read()
        
        # Encode to base64 for inline_data
        image_b64 = base64.b64encode(image_data).decode('utf-8')
        
        # Load the seed frame through Gemini to get it in Veo-compatible format
        gemini_image_response = client.models.generate_content(
            model="gemini-2.5-flash-image",
            contents=[
                types.Part(
                    inline_data=types.Blob(
                        mime_type="image/png",
                        data=image_b64
                    )
                ),
                "Output this exact image with zero modifications. Preserve every pixel, color, lighting, composition, and detail precisely as shown."
            ],
            config={"response_modalities": ['IMAGE']}
        )
        
        first_image = gemini_image_response.parts[0].as_image()
        print(f"[VEO API] Seed frame converted to Veo-compatible format")
        
        # Process reference frames (previous frames for visual continuity)
        reference_image_objects = []
        if reference_frames and len(reference_frames) > 0:
            print(f"[VEO API] Processing {len(reference_frames)} reference frames for continuity...")
            for idx, ref_path in enumerate(reference_frames[:3]):  # Veo supports up to 3 references
                try:
                    ref_pil = Image.open(ref_path)
                    ref_io = io.BytesIO()
                    ref_pil.save(ref_io, format='PNG')
                    ref_io.seek(0)
                    ref_data = ref_io.read()
                    ref_b64 = base64.b64encode(ref_data).decode('utf-8')
                    
                    # Convert to Veo-compatible format
                    ref_response = client.models.generate_content(
                        model="gemini-2.5-flash-image",
                        contents=[
                            types.Part(
                                inline_data=types.Blob(
                                    mime_type="image/png",
                                    data=ref_b64
                                )
                            ),
                            "Output this exact image with zero modifications."
                        ],
                        config={"response_modalities": ['IMAGE']}
                    )
                    
                    ref_image = ref_response.parts[0].as_image()
                    reference_image_objects.append(
                        types.VideoGenerationReferenceImage(
                            image=ref_image,
                            reference_type="style"  # Use for style/aesthetic continuity
                        )
                    )
                    print(f"[VEO API]   Reference {idx+1} added: {Path(ref_path).name}")
                except Exception as e:
                    print(f"[VEO API]   Warning: Failed to load reference {idx+1}: {e}")
        
        # Use the FULL prompt that was built with Gemini img2img instructions + video instructions
        # This includes ALL the VHS aesthetic, camera constraints, 1993 rules, etc.
        print(f"[VEO API] Using FULL prompt with complete VHS aesthetic instructions")
        print(f"[VEO API] Prompt length: {len(prompt)} characters (preserving all style/aesthetic detail)")
        if reference_image_objects:
            print(f"[VEO API] Including {len(reference_image_objects)} reference images for visual continuity")
        
        # Build config with or without reference images
        config = types.GenerateVideosConfig(
            aspect_ratio="16:9",
            number_of_videos=1,
        )
        
        if reference_image_objects:
            config.reference_images = reference_image_objects
        
        operation = client.models.generate_videos(
            model=model,
            prompt=prompt,  # Use FULL prompt, not truncated motion_prompt
            image=first_image,
            config=config
        )
    
    else:
        # TEXT-TO-VIDEO: No image or video input (fallback)
        print(f"[VEO API] Mode: TEXT-TO-VIDEO (no reference frame)")
        operation = client.models.generate_videos(
            model=model,
            prompt=prompt,
            config=types.GenerateVideosConfig(
                aspect_ratio="16:9",
                number_of_videos=1,
            )
        )
    
    # Poll for completion using SDK
    start_time = time.time()
    
    while not operation.done:
        elapsed = time.time() - start_time
        if elapsed > MAX_POLL_TIME:
            raise Exception(f"Video generation timeout after {MAX_POLL_TIME}s")
        
        print(f"[VEO API] Still generating... ({int(elapsed)}s elapsed)")
        time.sleep(10)
        operation = client.operations.get(operation)
    
    elapsed = time.time() - start_time
    print(f"[VEO API] Complete after {int(elapsed)}s")
    
    # Get generated video from response
    if not operation.response or not operation.response.generated_videos:
        raise Exception(f"No video generated in response")
    
    generated_video = operation.response.generated_videos[0]
    
    # Download and save video using SDK
    video_filename = f"seg_{frame_idx-1}_{frame_idx}_{int(time.time())}.mp4"
    video_path = VIDEO_SEGMENTS_DIR / video_filename
    
    print(f"[VEO API] Downloading video...")
    
    # Download video content (returns bytes directly)
    video_bytes = client.files.download(file=generated_video.video)
    
    # Write to file manually to ensure it's saved
    with open(video_path, 'wb') as f:
        f.write(video_bytes)
    
    # Verify file was written
    if not video_path.exists():
        raise Exception(f"Video file was not saved to {video_path}")
    
    # Get file size after saving
    video_size_mb = video_path.stat().st_size / 1024 / 1024
    safe_video_path = str(video_path).encode('ascii', 'replace').decode('ascii')
    print(f"[VEO] Video saved: {safe_video_path} ({video_size_mb:.1f} MB)")
    print(f"[VEO] Video verified on disk: {video_path.exists()}")
    
    # Extract last frame using OpenCV
    last_frame_path = _extract_last_frame(video_path, frame_idx, caption)
    
    # Track cost and frame count
    _track_video_cost(VIDEO_DURATION, USE_FAST_MODEL)
    _session_costs["total_frames_generated"] += 1
    
    print(f"[VEO] Frame {_session_costs['total_frames_generated']} generated (cost: ${_session_costs['total_cost']:.2f}/${MAX_SESSION_COST:.2f})")
    
    return (str(video_path), last_frame_path, operation)


def _extract_last_frame(video_path: Path, frame_idx: int, caption: str) -> str:
    """
    Extract the last frame from a video using OpenCV.
    
    Returns:
        Path to extracted frame image
    """
    import cv2
    
    print(f"[VEO] Extracting last frame with OpenCV...")
    
    # Open video
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise Exception(f"Failed to open video: {video_path}")
    
    # Get total frame count
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    
    # Seek to last frame
    cap.set(cv2.CAP_PROP_POS_FRAMES, total_frames - 1)
    
    # Read last frame
    ret, frame = cap.read()
    cap.release()
    
    if not ret:
        raise Exception(f"Failed to read last frame from video")
    
    # Generate filename from caption
    safe_caption = "".join(c for c in caption[:50] if c.isalnum() or c in (' ', '-', '_')).strip()
    safe_caption = safe_caption.replace(' ', '_')
    if not safe_caption:
        safe_caption = f"frame_{frame_idx}"
    
    output_filename = f"frame_{frame_idx}_{safe_caption}.png"
    output_path = IMAGE_DIR / output_filename
    
    # Save frame
    cv2.imwrite(str(output_path), frame)
    
    safe_output = str(output_path).encode('ascii', 'replace').decode('ascii')
    print(f"[VEO] Last frame extracted: {safe_output}")
    
    return str(output_path)


def stitch_video_segments(segment_paths: List[str], output_name: str = "tape") -> Tuple[Optional[str], str]:
    """
    Stitch multiple video segments into a single video file.
    Uses moviepy (with audio) if available, falls back to OpenCV (no audio).
    
    Args:
        segment_paths: List of paths to video segment files (in order)
        output_name: Base name for output file
    
    Returns:
        Tuple of (output_path or None, error_message or empty string)
    """
    if not segment_paths:
        return None, "No video segments provided"
    
    # Verify all segment files exist
    missing = []
    for seg_path in segment_paths:
        if not Path(seg_path).exists():
            missing.append(seg_path)
    
    if missing:
        return None, f"Missing video segments: {', '.join(missing)}"
    
    # Create output path
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"{output_name}_{timestamp}.mp4"
    output_path = VIDEO_FILMS_DIR / output_filename
    
    # Try ffmpeg first (preserves audio)
    if FFMPEG_AVAILABLE:
        print(f"[VIDEO STITCH] Stitching {len(segment_paths)} segments using ffmpeg (with audio)...")
        try:
            # Create a text file listing all segments
            concat_file = VIDEO_SEGMENTS_DIR / f"concat_list_{timestamp}.txt"
            with open(concat_file, 'w', encoding='utf-8') as f:
                for seg_path in segment_paths:
                    # Use forward slashes for ffmpeg compatibility
                    seg_path_str = str(seg_path).replace('\\', '/')
                    f.write(f"file '{seg_path_str}'\n")
            
            print(f"[VIDEO STITCH] Created concat file: {concat_file}")
            
            # Use ffmpeg concat demuxer (preserves encoding and audio)
            ffmpeg_cmd = [
                FFMPEG_PATH,  # Use the detected ffmpeg binary
                '-f', 'concat',
                '-safe', '0',
                '-i', str(concat_file),
                '-c', 'copy',  # Copy streams without re-encoding
                '-y',  # Overwrite output
                str(output_path)
            ]
            
            print(f"[VIDEO STITCH] Running ffmpeg...")
            result = subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            # Clean up concat file
            try:
                concat_file.unlink()
            except:
                pass
            
            if result.returncode == 0 and output_path.exists():
                file_size_mb = output_path.stat().st_size / 1024 / 1024
                
                # Get video duration using ffprobe if available
                try:
                    # Try to use imageio-ffmpeg's bundled ffprobe
                    ffprobe_path = FFMPEG_PATH.replace('ffmpeg', 'ffprobe')
                    probe_cmd = [
                        ffprobe_path,
                        '-v', 'error',
                        '-show_entries', 'format=duration',
                        '-of', 'default=noprint_wrappers=1:nokey=1',
                        str(output_path)
                    ]
                    probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
                    duration = float(probe_result.stdout.strip()) if probe_result.returncode == 0 else 0
                except:
                    duration = 0
                
                print(f"[VIDEO STITCH] Complete!")
                print(f"[VIDEO STITCH]   Duration: {duration:.1f}s")
                print(f"[VIDEO STITCH]   File size: {file_size_mb:.1f} MB")
                print(f"[VIDEO STITCH]   Output: {output_path}")
                print(f"[VIDEO STITCH]   Audio: Preserved (ffmpeg concat)")
                
                return str(output_path), ""
            else:
                error_msg = result.stderr if result.stderr else "Unknown error"
                print(f"[VIDEO STITCH] ffmpeg failed: {error_msg}")
                print(f"[VIDEO STITCH] Falling back to OpenCV (no audio)...")
            
        except Exception as e:
            print(f"[VIDEO STITCH] ffmpeg failed: {e}")
            import traceback
            traceback.print_exc()
            print(f"[VIDEO STITCH] Falling back to OpenCV (no audio)...")
    
    # Fallback to OpenCV (no audio)
    print(f"[VIDEO STITCH] Stitching {len(segment_paths)} segments using OpenCV (no audio)...")
    
    try:
        # Read first video to get properties
        first_cap = cv2.VideoCapture(str(segment_paths[0]))
        if not first_cap.isOpened():
            return None, f"Could not open first video: {segment_paths[0]}"
        
        fps = int(first_cap.get(cv2.CAP_PROP_FPS))
        width = int(first_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(first_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        first_cap.release()
        
        print(f"[VIDEO STITCH] Output: {width}x{height} @ {fps}fps")
        
        # Create video writer
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(str(output_path), fourcc, fps, (width, height))
        
        if not out.isOpened():
            return None, "Could not create output video writer"
        
        # Process each segment
        total_frames = 0
        for i, seg_path in enumerate(segment_paths):
            print(f"[VIDEO STITCH] Processing segment {i+1}/{len(segment_paths)}: {Path(seg_path).name}")
            
            cap = cv2.VideoCapture(str(seg_path))
            if not cap.isOpened():
                print(f"[VIDEO STITCH] Warning: Could not open {seg_path}, skipping")
                continue
            
            frames_written = 0
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                
                # Resize frame if dimensions don't match
                if frame.shape[1] != width or frame.shape[0] != height:
                    frame = cv2.resize(frame, (width, height))
                
                out.write(frame)
                frames_written += 1
                total_frames += 1
            
            cap.release()
            print(f"[VIDEO STITCH]   Wrote {frames_written} frames")
        
        # Release output writer
        out.release()
        
        if output_path.exists() and total_frames > 0:
            duration = total_frames / fps
            file_size_mb = output_path.stat().st_size / 1024 / 1024
            print(f"[VIDEO STITCH] Complete!")
            print(f"[VIDEO STITCH]   Duration: {duration:.1f}s")
            print(f"[VIDEO STITCH]   File size: {file_size_mb:.1f} MB")
            print(f"[VIDEO STITCH]   Output: {output_path}")
            print(f"[VIDEO STITCH]   Audio: Lost (OpenCV doesn't support audio)")
            return str(output_path), ""
        else:
            return None, "No frames written to output video"
    
    except Exception as e:
        print(f"[VIDEO STITCH] Error: {e}")
        import traceback
        traceback.print_exc()
        safe_error = str(e).encode('ascii', 'replace').decode('ascii')
        return None, f"Stitching error: {safe_error}"


def get_video_segments_for_session() -> List[str]:
    """
    Get all video segments generated in the current session.
    
    Returns:
        List of video segment file paths, sorted by frame order
    """
    segments = []
    
    # Get all segment files from the segments directory
    if VIDEO_SEGMENTS_DIR.exists():
        segment_files = sorted(VIDEO_SEGMENTS_DIR.glob("seg_*.mp4"))
        
        # Sort by frame indices in filename (seg_0_1_timestamp.mp4)
        def get_frame_indices(path):
            parts = path.stem.split("_")
            if len(parts) >= 3:
                try:
                    return (int(parts[1]), int(parts[2]))  # (start_frame, end_frame)
                except ValueError:
                    return (0, 0)
            return (0, 0)
        
        segment_files.sort(key=get_frame_indices)
        segments = [str(f) for f in segment_files]
    
    return segments


# ============================================================================
# INITIALIZATION
# ============================================================================

if GEMINI_API_KEY:
    print(f"[VEO INIT] API key loaded")
else:
    print(f"[VEO INIT] GEMINI_API_KEY not set - Veo will not work")

print(f"[VEO INIT] Model: {VEO_FAST_MODEL if USE_FAST_MODEL else VEO_MODEL} (FAST)")
print(f"[VEO INIT] Duration: {VIDEO_DURATION}s per video (MINIMUM)")
print(f"[VEO INIT] Budget Limit: ${MAX_SESSION_COST:.2f} per session")
print(f"[VEO INIT] Cost per video: ${ESTIMATED_COST_PER_VIDEO:.2f} (4s, fast model)")
max_videos = int(MAX_SESSION_COST / ESTIMATED_COST_PER_VIDEO)
print(f"[VEO INIT] Est. Max Videos: ~{max_videos} videos before budget limit")
print(f"[VEO INIT] Strategy:")
print(f"[VEO INIT]   Frame 0: Gemini (seed) - FREE")
print(f"[VEO INIT]   Frames 1+: Veo (video interpolation) until budget reached")
print(f"[VEO INIT]   Budget limit: ${MAX_SESSION_COST:.2f} (hard stop)")
print(f"[VEO INIT] Segments: {VIDEO_SEGMENTS_DIR}")
