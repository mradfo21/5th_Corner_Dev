"""
bot.py – single definitive version
• Attaches world‑image (snapshot) and dispatch‑image so every client sees them
• Works no matter where the bot is running (no PUBLIC_HOST / IMAGE_PORT)
• Updated: Extended death tape + FIXED Gemini API key loading from env vars (CRITICAL)
"""

print("[STARTUP] bot.py loading...", flush=True)

import os
import json
from pathlib import Path
import random

print("[STARTUP] Basic imports complete", flush=True)

DISCORD_ENABLED = os.getenv("DISCORD_ENABLED", "1") == "1"
RESUME_MODE = os.getenv("RESUME_MODE", "0") == "1"

print(f"[STARTUP] DISCORD_ENABLED={DISCORD_ENABLED}, RESUME_MODE={RESUME_MODE}", flush=True)

if DISCORD_ENABLED:
    print("[STARTUP] Loading Discord libraries...", flush=True)
    import asyncio, logging, random
    from typing import Optional, Tuple

    import discord
    from discord.ext import commands
    from discord.ui import View, Button, Modal, TextInput, Select
    
    print("[STARTUP] Discord imports complete", flush=True)

    import sys
    print("[STARTUP] Loading engine modules...", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    
    try:
        from api_client import api as engine
        print("[STARTUP] - engine (via api_client) imported", flush=True)
        sys.stdout.flush()
    except Exception as e:
        print(f"[STARTUP ERROR] Failed to import api_client: {e}", flush=True)
        sys.stdout.flush()
        import traceback
        traceback.print_exc()
        raise
    
    try:
        from evolve_prompt_file import generate_interim_messages_on_demand
        print("[STARTUP] - evolve_prompt_file imported", flush=True)
        sys.stdout.flush()
    except Exception as e:
        print(f"[STARTUP ERROR] Failed to import evolve_prompt_file: {e}", flush=True)
        sys.stdout.flush()
        import traceback
        traceback.print_exc()
        raise
    
    try:
        import ai_provider_manager
        print("[STARTUP] - ai_provider_manager imported", flush=True)
        sys.stdout.flush()
    except Exception as e:
        print(f"[STARTUP ERROR] Failed to import ai_provider_manager: {e}", flush=True)
        sys.stdout.flush()
        import traceback
        traceback.print_exc()
        raise
    
    try:
        import lore_cache_manager
        print("[STARTUP] - lore_cache_manager imported", flush=True)
        sys.stdout.flush()
    except Exception as e:
        print(f"[STARTUP ERROR] Failed to import lore_cache_manager: {e}", flush=True)
        sys.stdout.flush()
        import traceback
        traceback.print_exc()
        raise
    
    print("[STARTUP] Engine imports complete", flush=True)
    sys.stdout.flush()

    # ───────── configuration ────────────────────────────────────────────────────
    ROOT   = Path(__file__).parent.resolve()
    
    # Load config from file if it exists, otherwise use empty dict (for Render deployment)
    try:
        conf = json.load((ROOT / "config.json").open(encoding="utf-8"))
    except FileNotFoundError:
        conf = {}
    
    # Read from environment variables first, fall back to config.json
    TOKEN  = os.getenv("DISCORD_TOKEN", conf.get("DISCORD_TOKEN"))
    CHAN   = int(os.getenv("CHANNEL_ID", conf.get("CHANNEL_ID", 0)))
    VOTE_S = int(os.getenv("VOTE_SECONDS", conf.get("VOTE_SECONDS", 120)))
    
    # Reset game state on bot startup (unless RESUME_MODE is enabled)
    if not RESUME_MODE:
        print("[STARTUP] Resetting game state (fresh simulation)...", flush=True)
        try:
            engine.reset_state()
            print("[STARTUP] Game state cleared. Starting fresh.", flush=True)
        except Exception as e:
            print(f"[STARTUP ERROR] Failed to reset state: {e}", flush=True)
            import traceback
            traceback.print_exc()

    EMOJI = ["1️⃣", "2️⃣", "3️⃣"]

    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", conf.get("DISCORD_TOKEN"))
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", conf.get("OPENAI_API_KEY"))

    # ───────── discord init ─────────────────────────────────────────────────────
    print("[STARTUP] Initializing Discord bot...", flush=True)
    logging.basicConfig(level=logging.INFO, format="BOT | %(message)s")
    intents = discord.Intents.default(); intents.message_content = True
    bot     = commands.Bot(command_prefix="/", intents=intents)
    print(f"[STARTUP] Bot initialized. TOKEN={'SET' if TOKEN else 'MISSING'}, CHAN={CHAN}", flush=True)

    running = False
    OWNER_ID = None

    # ───────── VHS tape recording (death replay GIFs) ──────────────────────────
    
    # Session-specific directories
    def _get_tapes_dir():
        """Get session-specific tapes directory"""
        session_root = engine._get_session_root(engine.session_id)
        tapes_dir = session_root / "tapes"
        tapes_dir.mkdir(parents=True, exist_ok=True)
        return tapes_dir
    
    def _get_segments_dir():
        """Get session-specific video segments directory"""
        session_root = engine._get_session_root(engine.session_id)
        segments_dir = session_root / "films" / "segments"
        segments_dir.mkdir(parents=True, exist_ok=True)
        return segments_dir
    
    _run_images = []  # Track all images from current run for VHS tape
    import threading
    _tape_creation_lock = threading.Lock()  # Prevent duplicate tape creation (thread-safe)
    _tape_creation_in_progress = False  # Flag to track if tape is being created
    
    # ───────── 5th Corner VHS Color Palette ─────────────────────────────────────
    # Based on 5th Corner brand logo - teal surveillance camera aesthetic
    CORNER_TEAL = 0x6BABAE        # Main content, narrative, actions
    CORNER_TEAL_DARK = 0x3D7175   # Context, world state, backgrounds
    CORNER_BLACK = 0x0D1B1B       # Deep shadows, recessed elements
    CORNER_GREY = 0x2A3838        # System messages, loading states
    VHS_RED = 0x8B0000            # Danger/death ONLY
    
    # ───────── Fate Roll System ─────────────────────────────────────────────────
    def compute_fate():
        """
        Instantly compute fate (no animation)
        Returns: 'LUCKY' (25%), 'NORMAL' (50%), or 'UNLUCKY' (25%)
        """
        roll = random.random()
        if roll < 0.25:
            return "LUCKY"
        elif roll < 0.75:
            return "NORMAL"
        else:
            return "UNLUCKY"
    
    async def animate_fate_roll(channel, fate):
        """
        Show fate roll animation WHILE image is generating in background.
        The outcome is already determined - this is just for show/entertainment.
        """
        # Show rolling animation
        msg = await channel.send(embed=discord.Embed(
            description="🎰 Rolling fate...",
            color=CORNER_GREY
        ))
        await asyncio.sleep(0.4)
        
        # Build tension with bars
        for i in range(1, 11):
            bars = "█" * i
            empty = "░" * (10 - i)
            await msg.edit(embed=discord.Embed(
                description=f"`{bars}{empty}`",
                color=CORNER_GREY
            ))
            await asyncio.sleep(0.15)  # 1.5 seconds total
        
        # Reveal outcome with color coding
        fate_colors = {
            "LUCKY": CORNER_TEAL,
            "NORMAL": CORNER_GREY,
            "UNLUCKY": VHS_RED
        }
        
        await msg.edit(embed=discord.Embed(
            description=f"`[██████████]`\n**{fate}**",
            color=fate_colors[fate]
        ))
        await asyncio.sleep(2.2)  # Display result LONGER - let it sink in
        await msg.delete()
    
    def _create_death_replay_tape_with_lock() -> tuple[Optional[str], str]:
        """
        Thread-safe wrapper for tape creation with duplicate prevention.
        Returns: (tape_path or None, error_message or empty string)
        """
        global _tape_creation_in_progress
        
        with _tape_creation_lock:
            if _tape_creation_in_progress:
                print("[TAPE] WARNING: Duplicate tape creation prevented")
                return None, "Tape creation already in progress"
            
            _tape_creation_in_progress = True
        
        try:
            result = _create_death_replay_tape()
            return result
        finally:
            with _tape_creation_lock:
                _tape_creation_in_progress = False
    
    def _create_death_replay_tape() -> tuple[Optional[str], str]:
        """
        Create a VHS tape from the current run.
        In HD mode: Creates stitched video with audio from Veo segments
        In normal mode: Creates GIF from images
        Returns: (tape_path or None, error_message or empty string)
        """

        
        # Check if we're in HD mode (Veo)
        if hasattr(engine, 'VEO_MODE_ENABLED') and engine.VEO_MODE_ENABLED:
            print(f"[TAPE] HD MODE detected - attempting to stitch video segments...")
            
            # Try to stitch videos first
            try:
                from veo_video_utils import stitch_video_segments
                from pathlib import Path
                
                # Collect video segments from this session
                segments_dir = _get_segments_dir()
                if segments_dir.exists():
                    # Get all .mp4 files, sorted by name (which includes timestamp)
                    video_files = sorted(segments_dir.glob("seg_*.mp4"))
                    
                    if len(video_files) > 0:
                        print(f"[TAPE HD] Found {len(video_files)} video segments to stitch")
                        video_paths = [str(f) for f in video_files]
                        
                        stitched_video, error = stitch_video_segments(
                            video_paths, 
                            output_name="HD_tape",
                            video_segments_dir=_get_segments_dir(),
                            video_films_dir=_get_segments_dir().parent / "final"  # films/final directory
                        )
                        
                        if stitched_video:
                            print(f"[TAPE HD] Successfully stitched HD video: {stitched_video}")
                            return (stitched_video, "")
                        else:
                            print(f"[TAPE HD] Stitching failed: {error}")
                            print(f"[TAPE HD] Falling back to GIF...")
                    else:
                        print(f"[TAPE HD] No video segments found, falling back to GIF")
                else:
                    print(f"[TAPE HD] Segments directory not found, falling back to GIF")
                    
            except Exception as e:
                print(f"[TAPE HD] Error stitching videos: {e}")
                import traceback
                traceback.print_exc()
                print(f"[TAPE HD] Falling back to GIF...")
        
        # Normal mode or fallback: create GIF
        return _create_death_replay_gif()
    
    def _create_death_replay_gif() -> tuple[Optional[str], str]:
        """
        Create a VHS tape (GIF) from all images in the current run.
        Automatically compresses to stay under Discord's 8 MB limit.
        Returns: (tape_path or None, error_message or empty string)
        """
        # Discord file size limits (bytes)
        DISCORD_MAX_SIZE = 8 * 1024 * 1024  # 8 MB for non-Nitro users
        SAFE_MAX_SIZE = 7.5 * 1024 * 1024   # 7.5 MB safety margin
        
        # Check if we have enough frames
        print(f"\n{'='*70}")
        print(f"[TAPE GIF CREATE] Starting GIF creation...")
        print(f"[TAPE GIF CREATE] _run_images contains {len(_run_images) if _run_images else 0} entries")
        print(f"[TAPE GIF CREATE] Frame list:")
        for i, frame_path in enumerate(_run_images if _run_images else []):
            print(f"[TAPE GIF CREATE]   Frame {i}: {frame_path}")
        print(f"{'='*70}\n")
        
        if not _run_images or len(_run_images) < 2:
            error = f"Not enough frames recorded. Need at least 2 frames, but only have {len(_run_images) if _run_images else 0}. Did any images generate during gameplay?"
            print(f"[TAPE ERROR] {error}")
            return None, error
        
        try:
            from PIL import Image
            from datetime import datetime
            import os
            print(f"[TAPE] Recording VHS tape from {len(_run_images)} frame paths...")
            
            # Load all images and normalize to consistent 16:9 resolution
            # First pass: detect the most common resolution from Gemini images (skip logo)
            frames = []
            missing_files = []
            frame_sizes = []
            
            for idx, img_path in enumerate(_run_images):
                # Handle absolute paths (session images) properly
                if Path(img_path).is_absolute():
                    full_path = Path(img_path)
                else:
                    # Legacy relative paths
                    full_path = ROOT / img_path.lstrip("/")
                print(f"[TAPE] Loading frame {idx+1}/{len(_run_images)}: {full_path}")
                if full_path.exists():
                    try:
                        img = Image.open(str(full_path))
                        frame_sizes.append(img.size)
                        frames.append((img, img.size, idx))
                        print(f"[TAPE] Frame {idx+1} loaded: {img.size}")
                    except Exception as e:
                        print(f"[TAPE] ERROR: Failed to open frame {idx+1}: {e}")
                        missing_files.append(f"{img_path} (failed to open)")
                else:
                    print(f"[TAPE] ERROR: Frame {idx+1} not found: {full_path}")
                    missing_files.append(str(img_path))
            
            if len(frames) < 2:
                error = f"Not enough valid frames. Found {len(frames)} readable images out of {len(_run_images)} paths. Missing files: {', '.join(missing_files)}"
                print(f"[TAPE ERROR] {error}")
                return None, error
            
            # Find the target resolution (use the most common Gemini image size, skip frame 0 which is logo)
            gemini_sizes = [size for size, idx in zip(frame_sizes[1:], range(1, len(frame_sizes)))]
            if gemini_sizes:
                # Use the first Gemini image size as the gold standard
                TARGET_SIZE = gemini_sizes[0]
            else:
                # Fallback to first frame
                TARGET_SIZE = frame_sizes[0]
            
            print(f"[TAPE] Target resolution: {TARGET_SIZE} (Gemini gold standard)")
            
            # Second pass: normalize all frames to TARGET_SIZE
            normalized_frames = []
            for img, original_size, idx in frames:
                img_rgb = img.convert("RGB")
                
                if img_rgb.size != TARGET_SIZE:
                    # CROP to TARGET_SIZE (don't stretch!) to maintain aspect ratio
                    target_aspect = TARGET_SIZE[0] / TARGET_SIZE[1]
                    current_aspect = img_rgb.width / img_rgb.height
                    
                    # First, crop to correct aspect ratio
                    if abs(current_aspect - target_aspect) > 0.01:
                        if current_aspect > target_aspect:
                            # Too wide - crop width
                            new_width = int(img_rgb.height * target_aspect)
                            left = (img_rgb.width - new_width) // 2
                            img_cropped = img_rgb.crop((left, 0, left + new_width, img_rgb.height))
                        else:
                            # Too tall - crop height
                            new_height = int(img_rgb.width / target_aspect)
                            top = (img_rgb.height - new_height) // 2
                            img_cropped = img_rgb.crop((0, top, img_rgb.width, top + new_height))
                    else:
                        img_cropped = img_rgb
                    
                    # Then resize to exact TARGET_SIZE (aspect ratio already matches, no stretching)
                    if img_cropped.size != TARGET_SIZE:
                        img_resized = img_cropped.resize(TARGET_SIZE, Image.Resampling.LANCZOS)
                        normalized_frames.append(img_resized)
                        print(f"[TAPE] Frame {idx+1}: {original_size} -> cropped -> {TARGET_SIZE}")
                    else:
                        normalized_frames.append(img_cropped)
                        print(f"[TAPE] Frame {idx+1}: {original_size} -> cropped (already correct)")
                else:
                    normalized_frames.append(img_rgb)
                    print(f"[TAPE] Frame {idx+1}: {original_size} (already matches)")
            
            frames = normalized_frames
            print(f"[TAPE] All {len(frames)} frames normalized to {TARGET_SIZE[0]}x{TARGET_SIZE[1]}")
            
            # Create timestamped tape (they are the memories, never deleted)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            tape_path = _get_tapes_dir() / f"tape_{timestamp}.gif"
            
            # Try progressively smaller scales with color optimization
            # NEVER skip frames - preserve complete narrative!
            compression_attempts = [
                {"scale": 0.75, "colors": 256, "description": "75% scale (high quality)"},
                {"scale": 0.60, "colors": 256, "description": "60% scale"},
                {"scale": 0.50, "colors": 128, "description": "50% scale + 128 colors"},
                {"scale": 0.40, "colors": 96, "description": "40% scale + 96 colors"},
                {"scale": 0.35, "colors": 64, "description": "35% scale + 64 colors"},
                {"scale": 0.30, "colors": 48, "description": "30% scale + 48 colors (maximum compression)"},
            ]
            
            for attempt_num, strategy in enumerate(compression_attempts, 1):
                print(f"[TAPE] Compression attempt {attempt_num}/{len(compression_attempts)}: {strategy['description']}")
                
                # Apply scaling (ALWAYS scale, never skip frames)
                scaled_frames = []
                for frame in frames:
                    new_size = (int(frame.width * strategy["scale"]), int(frame.height * strategy["scale"]))
                    scaled_frames.append(frame.resize(new_size, Image.Resampling.LANCZOS))
                
                print(f"[TAPE] Scaled to {scaled_frames[0].size[0]}x{scaled_frames[0].size[1]} (keeping ALL {len(scaled_frames)} frames)")
                
                # Save with optimization - ALL frames preserved
                scaled_frames[0].save(
                str(tape_path),
                save_all=True,
                    append_images=scaled_frames[1:],
                duration=500,  # 0.5 seconds per frame
                    loop=0,
                    optimize=True,  # Enable GIF optimization
                    colors=strategy["colors"]  # Reduce color palette progressively
                )
                
                # Check file size
                file_size = os.path.getsize(tape_path)
                file_size_mb = file_size / (1024 * 1024)
                print(f"[TAPE] Generated: {file_size_mb:.2f} MB ({len(scaled_frames)} frames, {scaled_frames[0].size[0]}x{scaled_frames[0].size[1]})")
                
                if file_size <= SAFE_MAX_SIZE:
                    print(f"[TAPE] Success! Tape under Discord limit with {strategy['description']}")
                    print(f"[TAPE] VHS tape recorded: {tape_path.name} ({len(scaled_frames)} frames, {file_size_mb:.2f} MB)")
                    return str(tape_path), ""
                else:
                    print(f"[TAPE] Still too large ({file_size_mb:.2f} MB > 7.5 MB), trying next strategy...")
            
            # If we exhausted all strategies, return the last attempt with a warning
            file_size = os.path.getsize(tape_path)
            file_size_mb = file_size / (1024 * 1024)
            if file_size > DISCORD_MAX_SIZE:
                error = f"GIF is {file_size_mb:.2f} MB (max 8 MB). Try a shorter session or contact support."
                print(f"[TAPE ERROR] {error}")
                return str(tape_path), error  # Return path anyway, let user try
            else:
                print(f"[TAPE] WARNING: Tape is large but under limit: {file_size_mb:.2f} MB")
                return str(tape_path), ""
        
        except ImportError as e:
            error = f"PIL/Pillow library not installed! Cannot create GIF. Install with: pip install Pillow"
            print(f"[TAPE ERROR] {error}")
            import traceback
            traceback.print_exc()
            return None, error
        except Exception as e:
            error = f"Unexpected error creating tape: {str(e)}"
            print(f"[TAPE ERROR] {error}")
            import traceback
            traceback.print_exc()
            return None, error

    # ───────── image helper ─────────────────────────────────────────────────────
    def _attach(image_path: Optional[str], caption: str = "") -> Tuple[Optional[discord.File], Optional[str]]:
        if not image_path:
            return None, None
        
        # Handle absolute paths (session-specific images)
        if Path(image_path).is_absolute():
            local = Path(image_path)
        # Handle legacy /images/ paths
        elif image_path.startswith("/images/"):
            local = ROOT / "images" / Path(image_path).name
        # Handle legacy relative paths
        else:
            local = ROOT / "images" / Path(image_path).name
        
        if local.exists():
            return discord.File(local, filename=local.name), local.name
        else:
            print(f"[ATTACH ERROR] Image not found: {local}")
            print(f"[ATTACH ERROR] Original path: {image_path}")
            return None, None
    
    # ───────── video helper (HD mode) ────────────────────────────────────────────
    def _attach_video(video_path: Optional[str]) -> Tuple[Optional[discord.File], Optional[str]]:
        """Attach a video file for HD mode playback. Returns (File, filename) or (None, None).
        
        Respects Discord's file size limits (8 MB for non-Nitro, 25 MB for Nitro).
        """
        if not video_path:
            return None, None
        
        # Discord file size limits
        DISCORD_FILE_SIZE_LIMIT_MB = 8  # Conservative limit for non-Nitro servers
        
        # Handle both relative and absolute paths
        if Path(video_path).is_absolute():
            local = Path(video_path)
        else:
            # Try relative to ROOT
            local = ROOT / video_path
        
        if local.exists():
            file_size_mb = local.stat().st_size / 1024 / 1024
            print(f"[VIDEO ATTACH] Found video: {local} ({file_size_mb:.1f} MB)")
            
            # Check file size against Discord limit
            if file_size_mb > DISCORD_FILE_SIZE_LIMIT_MB:
                print(f"[VIDEO ATTACH] WARNING: Video too large ({file_size_mb:.1f} MB > {DISCORD_FILE_SIZE_LIMIT_MB} MB limit)")
                print(f"[VIDEO ATTACH] Skipping video, will show image only")
                return None, None
            
            return discord.File(local, filename=local.name), local.name
        else:
            print(f"[VIDEO ATTACH] Video not found: {local}")
            return None, None

    # ───────── embed builders ───────────────────────────────────────────────────
    def snap_embed(d: dict, img_name: Optional[str]) -> discord.Embed:
        e = discord.Embed(
            title="Situation Update",
            description=d["report"],
            color=VHS_RED
        )
        e.add_field(
            name="Choices",
            value="\n".join(f"{EMOJI[i]}  {c}" for i, c in enumerate(d["choices"])),
            inline=False
        )
        e.set_footer(text="Cast your vote below!")
        if img_name:
            e.set_image(url=f"attachment://{img_name}")
        return e

    def dispatch_embed(d: dict, win: str, img_name: Optional[str]) -> discord.Embed:
        e = discord.Embed(
            title="🖋️ Dispatch",
            description=d["dispatch"],
            color=CORNER_TEAL
        )
        e.add_field(name="Winning choice", value=win, inline=False)
        if img_name:
            e.set_image(url=f"attachment://{img_name}")
        return e

    # ───────── vote tally helper ────────────────────────────────────────────────
    async def _tally(msg: discord.Message, n: int) -> int:
        await asyncio.sleep(VOTE_S)
        msg = await msg.channel.fetch_message(msg.id)
        scores=[0]*n
        for i,e in enumerate(EMOJI[:n]):
            r=discord.utils.get(msg.reactions,emoji=e)
            if r: scores[i]=len({u async for u in r.users() if not u.bot})
        return scores.index(max(scores))

    def categorize_choice(choice: str) -> tuple[str, str]:
        """Categorize a choice and return (category, emoji)."""
        choice_lower = choice.lower()
        # Expanded action keywords for anything risky, combative, or health-affecting
        action_keywords = [
            "attack", "fight", "grab", "take", "use", "push", "pull", "draw", "signal", "shout", "hide", "run", "climb", "scale", "duck", "barricade", "rally", "raise", "leap", "scramble", "retreat",
            "throw", "shoot", "stab", "punch", "kick", "confront", "break", "smash", "injure", "wound", "harm", "defend", "block", "dodge", "escape", "flee", "ambush", "strike", "hit", "fire", "blast", "charge", "rush", "tackle", "choke", "wrestle", "trap", "sabotage", "destroy", "kill", "murder", "assault", "counter", "parry", "evade", "sprint", "swing", "slash", "bite", "burn", "poison", "shoot at", "fire at", "aim at", "threaten", "challenge", "face off", "stand off", "resist", "survive", "risk", "danger", "hazard", "peril", "bleed", "bleeding", "hurt", "injury", "wound", "damage", "dangerous", "perilous", "hazardous"
        ]
        explore_keywords = ["explore", "search", "look", "scan", "investigate", "inspect", "trace", "survey", "observe", "peek", "scout"]
        new_scene_keywords = ["enter", "leave", "exit", "advance", "proceed", "move on", "set out", "start", "begin", "new scene", "next area", "go to", "return"]
        if any(k in choice_lower for k in explore_keywords):
            return ("explore", "‍♂️")
        if any(k in choice_lower for k in action_keywords):
            return ("action", "⚡")
        if any(k in choice_lower for k in new_scene_keywords):
            return ("new scene", "")
        return ("explore", "‍♂️")

    MAX_BUTTON_LABEL_LENGTH = 80
    def safe_label(label):
        return label[:MAX_BUTTON_LABEL_LENGTH]

    MAX_EMBED_DESC_LEN = 4096

    def safe_embed_desc(text):
        if len(text) > MAX_EMBED_DESC_LEN:
            return text[:MAX_EMBED_DESC_LEN - 15] + '\n...(truncated)'
        return text
    
    def get_movement_indicator():
        """Get visual indicator for detected movement type."""
        movement_type = engine.get_last_movement_type()
        if not movement_type:
            return ""
        
        indicators = {
            'forward_movement': ' **MOVEMENT**',
            'exploration': ' **MOVEMENT**',
            'stationary': ' **MOVEMENT**'
        }
        return indicators.get(movement_type, "")
    
    def get_tension_beat(fate: str, movement_type: str) -> str:
        """
        Generate a post-image tension beat based on fate and movement.
        These appear AFTER the image to build atmosphere without blocking generation.
        """
        import random
        
        # LUCKY beats - something helps you
        lucky_beats = [
            "🍀 The facility seems quieter than expected.",
            "🍀 No patrols visible in your immediate area.",
            "🍀 Equipment here looks functional.",
            "🍀 Cover is plentiful if you need it.",
            "🍀 The path ahead seems clear for now.",
        ]
        
        # UNLUCKY beats - something complicates
        unlucky_beats = [
            "WARNING: You hear distant mechanical sounds.",
            "WARNING: Something shifts in the shadows nearby.",
            "WARNING: The air feels wrong here.",
            "WARNING: You notice fresh boot prints in the dust.",
            "WARNING: Equipment hums to life in the distance.",
        ]
        
        # NORMAL beats - neutral tension
        normal_beats = [
            " The facility holds its breath.",
            " Silence stretches across the desert.",
            " Time passes. The sun continues its arc.",
            " The red mesa looms in the distance.",
            " Nothing moves in your immediate view.",
        ]
        
        if fate == "LUCKY":
            return random.choice(lucky_beats)
        elif fate == "UNLUCKY":
            return random.choice(unlucky_beats)
        else:
            return random.choice(normal_beats)
    
    async def generate_timeout_penalty(dispatch, situation_report, current_image=None):
        """Generate a contextual negative consequence for player inaction using LLM with vision"""
        import requests
        import base64
        from pathlib import Path
        
        # Use the timeout_penalty_instructions from prompts file
        penalty_instructions = engine.PROMPTS.get("timeout_penalty_instructions", "")
        
        image_context = ""
        if current_image:
            image_context = (
                "\n\n🖼️ CRITICAL: Look at the attached image. This is YOUR CURRENT LOCATION.\n"
                "THE CAMERA HAS NOT MOVED. You are STILL in this EXACT spot.\n"
                "If indoor, STAY INDOOR. If outdoor, STAY OUTDOOR.\n"
                "Base the penalty on VISIBLE THREATS in THIS image (structural hazards, debris, environmental danger).\n"
                "DO NOT invent new locations. DO NOT teleport. DO NOT mention 'fence' if no fence visible."
            )
        
        prompt = f"""{penalty_instructions}

🚨 CRITICAL RULE: YOU HAVE NOT MOVED. THE CAMERA IS IN THE EXACT SAME LOCATION.
- If you were in a hallway, you are STILL in that hallway
- If you were outside, you are STILL outside in the same spot
- DO NOT mention new locations, structures, or objects not in the current image
- The penalty happens HERE, where you already are

CURRENT SITUATION:
{situation_report}

WHAT JUST HAPPENED:
{dispatch}{image_context}

Generate the penalty in valid JSON format. MUST stay in current location. Use 'you/your' only."""

        try:
            gemini_api_key = conf.get("GEMINI_API_KEY", "")
            
            # Build parts list (text + optional image)
            parts = [{"text": prompt}]
            
            # Add current image if provided (for visual context)
            if current_image:
                # Convert path to actual file path
                if current_image.startswith("/images/"):
                    actual_path = ROOT / "images" / current_image.replace("/images/", "")
                else:
                    # Handle absolute paths (session images)
                    if Path(current_image).is_absolute():
                        actual_path = Path(current_image)
                    else:
                        actual_path = ROOT / current_image.lstrip("/")
                
                # Try to use small version first
                small_path = actual_path.parent / (actual_path.stem + "_small" + actual_path.suffix)
                if small_path.exists():
                    actual_path = small_path
                
                if actual_path.exists():
                    print(f"[PENALTY] Using image for context: {actual_path.name}")
                    with open(actual_path, "rb") as f:
                        img_data = base64.b64encode(f.read()).decode('utf-8')
                    parts.append({
                        "inline_data": {
                            "mime_type": "image/png",
                            "data": img_data
                        }
                    })
            
            response_data = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
                headers={"x-goog-api-key": gemini_api_key, "Content-Type": "application/json"},
                json={
                    "contents": [{"parts": parts}],
                    "generationConfig": {"temperature": 0.8, "maxOutputTokens": 50}
                },
                timeout=10
            ).json()
            
            # Check for API error response
            if "candidates" not in response_data:
                print(f"[COUNTDOWN] Gemini API error: {response_data.get('error', response_data)}")
                return "The world turns dangerous in your hesitation"  # Contextual fallback
            
            result = response_data["candidates"][0]["content"]["parts"][0]["text"].strip()
            print(f"[COUNTDOWN] Raw result: {result}")
            
            # Parse JSON response
            try:
                import json as json_lib
                # Strip markdown code fences if present
                if result.startswith("```"):
                    lines = result.split("\n")
                    if lines[0].startswith("```"):
                        lines = lines[1:]
                    if lines and lines[-1].strip() == "```":
                        lines = lines[:-1]
                    result = "\n".join(lines)
                
                penalty_data = json_lib.loads(result)
                penalty = penalty_data.get("penalty", "Hesitate too long")
                print(f"[COUNTDOWN] Generated penalty: {penalty}")
                return penalty
            except Exception as parse_error:
                print(f"[COUNTDOWN] JSON parse failed: {parse_error}, using raw: {result}")
                # Fallback to raw text if JSON parsing fails
                return result.replace('"', '').strip()
                
        except Exception as e:
            print(f"[COUNTDOWN] Failed to generate penalty: {e}")
            return "Guard spots you"  # Fallback with correct perspective

    class ChoiceView(View):
        def __init__(self, choices, owner_id=None):
            super().__init__(timeout=None)
            self.choices = choices
            self.owner_id = owner_id  # Store for authorization checks
            self.last_choices_message = None  # Track the last choices message
            for i, choice in enumerate(choices):
                # Skip placeholder choices (—)
                if choice.strip() in ["—", "–", "-", ""]:
                    continue
                _, emoji = categorize_choice(choice)
                btn = ChoiceButton(label=safe_label(f"{emoji} {choice}"), idx=i)
                self.add_item(btn)  # Discord.py automatically sets btn.view = self
            custom_btn = CustomActionButton()
            self.add_item(custom_btn)  # Discord.py automatically sets view
            autoplay_btn = AutoPlayToggleButton(self)
            self.add_item(autoplay_btn)  # Discord.py automatically sets view
            quality_btn = QualityToggleButton(self)
            self.add_item(quality_btn)  # Discord.py automatically sets view
            regen_btn = RegenerateChoicesButton(self)
            self.add_item(regen_btn)  # Discord.py automatically sets view
            # Row 2 buttons
            restart_btn = RestartButton()
            self.add_item(restart_btn)  # Discord.py automatically sets view

    def check_authorization(interaction: discord.Interaction, owner_id) -> bool:
        """Check if user is authorized to use buttons. Returns True if authorized."""
        if owner_id is None:
            return True  # No restriction if owner_id not set
        return interaction.user.id == owner_id
    
    class ChoiceButton(Button):
        def __init__(self, label, idx):
            super().__init__(label=label, style=discord.ButtonStyle.primary)
            self.idx = idx
            # Note: self.view is automatically set by Discord.py when button is added to a View
        
        @staticmethod
        def _do_reset_static():
            """Static reset method that can be called without instance"""
            global _run_images
            try:
                with open(ROOT / "history.json", "w", encoding="utf-8") as f:
                    f.write("[]")

                intro_prompt = engine.PROMPTS["world_initial_state"]
                with open(ROOT / "world_state.json", "w", encoding="utf-8") as f:
                    json.dump({
                        "world_prompt": intro_prompt,
                        "current_phase": "normal",
                        "chaos_level": 0,
                        "last_choice": "",
                        "last_saved": "",
                        "seen_elements": [],
                        "player_state": {"alive": True, "health": 100}
                    }, f, indent=2)
                engine.reset_state()
                
                # Clear tape recording for new run
                _run_images.clear()
                print("[DEATH] Game state reset complete. New tape ready.")
            except Exception as e:
                print(f"[DEATH] Reset error: {e}")

        async def callback(self, interaction: discord.Interaction):
            # Authorization check
            if hasattr(self, 'view') and self.view and hasattr(self.view, 'owner_id'):
                if not check_authorization(interaction, self.view.owner_id):
                    await interaction.response.send_message(
                        "🔒 Only the game owner can make choices.",
                        ephemeral=True
                    )
                    return
            
            global auto_advance_task, countdown_task, auto_play_enabled
            
            # Cancel auto-play timer when user manually makes a choice
            if auto_advance_task and not auto_advance_task.done():
                auto_advance_task.cancel()
                print("[AUTO-PLAY] Manual choice made - cancelling auto-play timer")
            
            # Cancel countdown timer when choice is made
            if countdown_task and not countdown_task.done():
                countdown_task.cancel()
                print("[COUNTDOWN] Choice made - cancelling countdown timer")
            
            # Increment turn counter and check custom action cooldown
            global custom_action_turn_counter, custom_action_available
            custom_action_turn_counter += 1
            
            if not custom_action_available and custom_action_turn_counter >= CUSTOM_ACTION_COOLDOWN:
                custom_action_available = True
                print(f"[CUSTOM ACTION] Recharged! Available again after {CUSTOM_ACTION_COOLDOWN} turns")
            
            try:
                await interaction.response.defer()
            except Exception:
                pass
            
            # CRITICAL: Disable ALL buttons immediately to prevent double-click race condition
            view = self.view
            if view and hasattr(view, 'children'):
                for item in view.children:
                    item.disabled = True
                try:
                    if hasattr(view, 'last_choices_message') and view.last_choices_message:
                        await view.last_choices_message.edit(view=view)
                        print("[CHOICE] Buttons disabled to prevent double-click")
                except Exception as e:
                    print(f"[CHOICE] Warning: Could not disable buttons: {e}")
            

            world_state = engine.get_state().get('world_prompt', '')
            choice_text = self.label
            # Fast LLM call for micro-reaction (10-20 tokens, low temp)
            micro_prompt = (
                "Given the player's choice: '" + choice_text + "', and the current world state: '" + world_state + "', "
                "write a 1-sentence immediate world or NPC reaction. Start with a relevant emoji. Be suspenseful, direct, and avoid spoilers."
            )
            try:
                micro_reaction = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: engine._ask(micro_prompt, model="gpt-4o-mini", temp=0.4, tokens=50)
                )
            except Exception:
                micro_reaction = " The world holds its breath."
            micro_msg = await interaction.channel.send(embed=discord.Embed(
                description=safe_embed_desc(micro_reaction),
                color=CORNER_TEAL  # blue for anticipation
            ))
            
            # PROGRESSIVE FEEDBACK: Show action taken immediately
            await asyncio.sleep(0.6)  # Brief pause after micro-reaction
            action_msg = await interaction.channel.send(embed=discord.Embed(
                description=safe_embed_desc(f"**Action:** {choice_text}"),
                color=CORNER_TEAL  # purple for action
            ))

            # PHASE 1: Generate dispatch and image FAST
            loop = asyncio.get_running_loop()
            
            # Compute fate INSTANTLY (no wait)
            fate = compute_fate()
            print(f"[FATE] Computed: {fate}")
            
            # Start Phase 1 immediately with fate modifier (image generation in background)
            phase1_task = loop.run_in_executor(None, engine.advance_turn_image_fast, self.label, fate)
            
            # WHILE image is generating, show atmospheric beats (fills wait time!)
            # Clean up interim messages first
            try:
                await micro_msg.delete()
            except Exception:
                pass
            try:
                if 'action_msg' in locals():
                    await action_msg.delete()
            except Exception:
                pass
            
            await asyncio.sleep(0.3)  # Brief breath
            
            # BEAT 1: Fate animation (rolling the dice) - KEEP ORIGINAL
            await animate_fate_roll(interaction.channel, fate)
            await asyncio.sleep(0.5)  # Let the fate result sink in before continuing
            
            # BEAT 2: Anticipation based on CHOICE (not outcome - so no wait needed!)
            choice_lower = self.label.lower()
            if any(word in choice_lower for word in ["sprint", "run", "dash", "bolt", "charge"]):
                anticipation = "💨 Movement registered..."
            elif any(word in choice_lower for word in ["smash", "break", "destroy", "kick", "bash"]):
                anticipation = "💥 Impact imminent..."
            elif any(word in choice_lower for word in ["hide", "crouch", "duck", "flatten"]):
                anticipation = "🤫 Concealment attempt..."
            elif any(word in choice_lower for word in ["climb", "vault", "scramble", "ascend"]):
                anticipation = "🧗 Vertical movement..."
            elif any(word in choice_lower for word in ["grab", "take", "pick up", "snatch"]):
                anticipation = "🤲 Interaction detected..."
            else:
                anticipation = "⏳ Action processing..."
            
            await interaction.channel.send(embed=discord.Embed(
                description=anticipation,
                color=CORNER_GREY
            ))
            await asyncio.sleep(0.8)
            
            # BEAT 3: Fate flavor (Lucky/Unlucky hint) - KEEP ORIGINAL
            fate_flavor = get_tension_beat(fate, "")  # Empty movement type for now
            await interaction.channel.send(embed=discord.Embed(
                description=fate_flavor,
                color=CORNER_GREY if fate == "NORMAL" else (CORNER_TEAL if fate == "LUCKY" else VHS_RED)
            ))
            await asyncio.sleep(0.8)
            
            # NOW wait for Phase 1 to complete (image + dispatch generation)
            phase1 = await phase1_task
            
            # BEAT 4: Show IMAGE FIRST (immediately, no delay)
            image_path = phase1.get("consequence_image")
            video_path = phase1.get("consequence_video")
            
            # Track image for VHS tape
            global _run_images
            print(f"[TAPE RECORDING] Checking if image should be recorded...")
            print(f"[TAPE RECORDING]   image_path = {image_path}")
            print(f"[TAPE RECORDING]   video_path = {video_path}")
            print(f"[TAPE RECORDING]   Current tape has {len(_run_images)} frames")
            if image_path:
                _run_images.append(image_path)
                print(f"[TAPE] Frame {len(_run_images)} recorded: {image_path}")
                print(f"[TAPE] Total frames in memory: {_run_images}")
            else:
                print(f"[TAPE] ERROR: NO IMAGE PATH - Frame NOT recorded!")
            
            # HD MODE: If video available, send video (with auto-play)
            # Always send image as fallback/thumbnail
            if video_path:
                print(f"[HD MODE] Video available, displaying video player...")
                video_file, video_name = _attach_video(video_path)
                if video_file:
                    # Send video - Discord auto-plays MP4 files!
                    await interaction.channel.send(
                        content="🎬 **HD VIDEO**",
                        file=video_file
                    )
                    print(f"[BOT] Video displayed with auto-play: {video_name}")
                else:
                    print(f"[HD MODE] Video file not found, falling back to image only")
            
            # Always send the last frame image (for game logic and as fallback)
            if image_path:
                file, name = _attach(image_path, phase1.get("vision_dispatch", ""))
                if file:
                    if not video_path:
                        # Normal mode: just the image
                        await interaction.channel.send(file=file)
                        print(f"[BOT] Image displayed immediately (before choices)!")
                    else:
                        # HD mode: image already sent with video, skip duplicate
                        print(f"[BOT] Image skipped (video already displayed)")
                await asyncio.sleep(0.5)
            else:
                # Image generation blocked (safety filter or API error)
                await interaction.channel.send(embed=discord.Embed(
                    title="⚠️ SIGNAL DISRUPTED",
                    description="Visual feed blocked. Content filtered by safety systems.\nNarrative continues...",
                    color=VHS_RED
                ))
                await asyncio.sleep(0.5)
            
            # BEAT 5: Show consequence text AFTER image (appears underneath)
            dispatch_text = phase1.get("dispatch", "")
            if dispatch_text:
                movement_indicator = get_movement_indicator()
                full_text = dispatch_text.strip()
                if movement_indicator:
                    full_text = f"{movement_indicator}\n\n{full_text}"
                
                await interaction.channel.send(embed=discord.Embed(
                    title="⚡ Consequence",
                    description=safe_embed_desc(full_text),
                    color=VHS_RED
                ))
                await asyncio.sleep(0.3)  # Brief pause so they can read it
            else:
                dispatch_text = ""  # Ensure dispatch_text is defined
            
            # Show "Generating choices..." while Phase 2 runs
            choices_msg = await interaction.channel.send(embed=discord.Embed(
                description="⚙️ Analyzing scene...",
                color=CORNER_GREY
            ))
            
            # PHASE 2: Generate choices in background
            print(f"[BOT DEBUG] Passing to Phase 2: image={image_path}, dispatch={dispatch_text[:30] if dispatch_text else 'N/A'}...")
            
            # Make sure image_path is not None
            if not image_path:
                print(f"[BOT ERROR] No image from Phase 1! Using fallback.")
                image_path = None  # Will skip image in choices
            
            phase2_task = loop.run_in_executor(
                None,
                engine.advance_turn_choices_deferred,
                image_path,
                dispatch_text,
                phase1.get("vision_dispatch", ""),
                self.label
            )
            phase2 = await phase2_task
            
            # Delete "Generating choices..." message
            try:
                await choices_msg.delete()
            except Exception:
                pass
            
            # Combine phase1 and phase2 results for compatibility
            disp = {**phase1, **phase2, "dispatch_image": image_path}

            # CHECK FOR DEATH - Read FRESH state from file
            # Note: get_state() already reloads from disk via API client
            current_state = engine.get_state()
            player_alive = current_state.get("player_state", {}).get("alive", True)
            player_health = current_state.get("player_state", {}).get("health", 100)
            
            print(f"[DEATH CHECK] player_alive = {player_alive}, health = {player_health}")
            
            if not player_alive:
                print("[DEATH] Player has died! Starting death sequence...")
                
                # Disable all choice buttons immediately
                view = self.view
                if view and hasattr(view, 'children'):
                    for item in view.children:
                        item.disabled = True
                    try:
                        if hasattr(view, 'last_choices_message') and view.last_choices_message:
                            await view.last_choices_message.edit(view=view)
                    except Exception as e:
                        print(f"[DEATH] Failed to disable buttons: {e}")
                
                # Show "YOU DIED" message
                await interaction.channel.send(embed=discord.Embed(
                    title="💀 YOU DIED",
                    description="The camera stops recording.",
                    color=VHS_RED
                ))
                await asyncio.sleep(1)
                
                # Show VHS ejecting sequence WHILE tape is being created
                eject_msg = await interaction.channel.send(embed=discord.Embed(
                    description="`[STOP]` ⏏️ EJECTING TAPE...",
                    color=VHS_RED
                ))
                
                # Start tape creation in background
                loop = asyncio.get_running_loop()
                tape_task = loop.run_in_executor(None, _create_death_replay_tape_with_lock)
                
                # VHS eject animation (plays while GIF generates)
                eject_sequence = [
                    (0.8, "`[STOP]` ⏏️\n`REWINDING...`"),
                    (0.8, "`[STOP]` ⏏️\n`[███░░░░░░░]`"),
                    (0.8, "`[STOP]` ⏏️\n`[██████░░░░]`"),
                    (0.8, "`[STOP]` ⏏️\n`[█████████░]`"),
                    (0.8, "`[STOP]` ⏏️\n`FINALIZING...`"),
                    (1.0, "`[STOP]` ⏏️\n`TAPE READY`")
                ]
                
                for delay, message in eject_sequence:
                    done, pending = await asyncio.wait([tape_task], timeout=delay)
                    if done:
                        break
                    try:
                        await eject_msg.edit(embed=discord.Embed(
                            description=message,
                            color=VHS_RED
                        ))
                    except Exception:
                        break
                
                # Wait for completion
                tape_path, error_msg = await tape_task
                
                # Clean up animation
                try:
                    await eject_msg.delete()
                except Exception:
                    pass
                
                # Send tape or error
                if tape_path:
                    await interaction.channel.send(embed=discord.Embed(
                        title=" VHS TAPE RECOVERED",
                        description="Camera footage retrieved from scene.",
                        color=CORNER_GREY
                    ))
                    try:
                        await interaction.channel.send(file=discord.File(tape_path))
                        print("[DEATH]  Tape uploaded - waiting for player to download...")
                    except Exception as e:
                        print(f"[DEATH] Failed to send tape: {e}")
                        await interaction.channel.send(embed=discord.Embed(
                            title="WARNING: Tape Upload Failed",
                            description=f"Tape created but upload failed: {e}",
                            color=VHS_RED
                        ))
                else:
                    await interaction.channel.send(embed=discord.Embed(
                        title="WARNING: No Tape Created",
                        description=f"**Reason:** {error_msg}",
                        color=VHS_RED
                    ))
                
                # Create Play Again button (independent of disabled view)
                manual_restart_done = asyncio.Event()  # Flag to prevent double restart
                
                class PlayAgainButton(Button):
                    def __init__(self):
                        super().__init__(label="️ Play Again", style=discord.ButtonStyle.success)
                    
                    async def callback(self, button_interaction: discord.Interaction):
                        # Authorization check
                        if not check_authorization(button_interaction, OWNER_ID):
                            await button_interaction.response.send_message(
                                "🔒 Only the game owner can restart.",
                                ephemeral=True
                            )
                            return
                        
                        global auto_advance_task, countdown_task, auto_play_enabled
                        print("[DEATH] Play Again button pressed - manual restart")
                        
                        # Mark that manual restart is happening
                        manual_restart_done.set()
                        
                        try:
                            await button_interaction.response.defer()
                        except Exception:
                            pass
                        
                        # Cancel all running tasks
                        if auto_advance_task and not auto_advance_task.done():
                            auto_advance_task.cancel()
                        if countdown_task and not countdown_task.done():
                            countdown_task.cancel()
                        auto_play_enabled = False
                        
                        # Reset game
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, ChoiceButton._do_reset_static)
                        
                        # Show intro
                        await send_intro_tutorial(button_interaction.channel)
                
                # Show Play Again button and leave it (no auto-restart)
                play_again_view = View(timeout=None)
                play_again_view.add_item(PlayAgainButton())
                await interaction.channel.send(
                    embed=discord.Embed(
                        description="💾 **Save the tape!** Press Play Again when ready.",
                        color=CORNER_GREY
                    ),
                    view=play_again_view
                )
                
                print("[DEATH] Play Again button ready - waiting for manual restart (no auto-restart)")
                return  # End turn here - button will handle restart when clicked
            
            # Show world evolution context (skip generic defaults)
            world_context = disp.get("world_prompt", "")
            # Skip if it's just the generic default or empty
            generic_defaults = ["jason is alone", "danger could strike"]
            if world_context and not any(g in world_context.lower() for g in generic_defaults):
                await interaction.channel.send(embed=discord.Embed(
                    description=safe_embed_desc(f"📍 {world_context.strip()}"),
                    color=CORNER_TEAL_DARK  # discord blue
                ))
                await asyncio.sleep(0.5)

            # 5.5. Show rare event if present
            rare_event = disp.get("rare_event", None)
            if rare_event:
                await interaction.channel.send(embed=discord.Embed(
                    description=safe_embed_desc(f"✨ **Rare Event:** {rare_event.strip()}"),
                    color=CORNER_TEAL  # purple for rare
                ))
                await asyncio.sleep(random.uniform(2.5, 3.5))

            # 5.6. Show streak reward if present
            streak_reward = disp.get("streak_reward", None)
            if streak_reward:
                await interaction.channel.send(embed=discord.Embed(
                    description=safe_embed_desc(f"🔥 **Streak!** {streak_reward.strip()}"),
                    color=CORNER_TEAL  # orange for streak
                ))
                await asyncio.sleep(random.uniform(2.5, 3.5))

            # 5.7. Show danger/combat indicator if present
            if disp.get('danger'):
                await interaction.channel.send(embed=discord.Embed(
                    description=safe_embed_desc('WARNING: **Danger! Threat detected in the scene.**'),
                    color=VHS_RED
                ))
                await asyncio.sleep(random.uniform(1.5, 2.5))
            if disp.get('combat'):
                msg = disp.get('combat_message', 'Combat imminent!')
                await interaction.channel.send(embed=discord.Embed(
                    description=safe_embed_desc(f'⚔️ **{msg}**'),
                    color=VHS_RED
                ))
                await asyncio.sleep(random.uniform(1.5, 2.5))

            # Image was already shown immediately in Phase 1 - skip duplicate display!

            # 7. Show new choices
            await interaction.channel.send("🟢 What will you do next?")
            view = ChoiceView(disp["choices"])
            await send_choices(interaction.channel, disp["choices"], view, getattr(self, 'last_choices_message', None))
            
            # Start countdown timer (if not in auto-play mode)
            if not auto_play_enabled:
                start_countdown_timer(
                    interaction.channel,
                    disp["choices"],
                    view,
                    disp.get("dispatch", ""),
                    disp.get("situation_report", ""),
                    disp.get("consequence_image")
                )
            
            # Start auto-advance timer with new choices
            start_auto_advance_timer(interaction.channel, disp["choices"], view)

    # ═══════════════════════════════════════════════════════════════════════════
    # Custom Action Modal and Button
    # ═══════════════════════════════════════════════════════════════════════════
    
    class CustomActionModal(discord.ui.Modal, title="✨ Free Will"):
        action = discord.ui.TextInput(
            label="What do you want to do?",
            placeholder="e.g., 'Kick down the door', 'Sprint to the tower', 'Hide behind debris'",
            max_length=100,
            style=discord.TextStyle.short,
            required=True
        )
        
        async def on_submit(self, interaction: discord.Interaction):
            global countdown_task, auto_advance_task, custom_action_available, custom_action_turn_counter, auto_play_enabled
            
            custom_choice = self.action.value.strip()
            if not custom_choice:
                await interaction.response.send_message("ERROR: Please enter an action!", ephemeral=True)
                return
            
            print(f"[CUSTOM ACTION] Player entered: {custom_choice}")
            
            # Use custom action - put it on cooldown
            custom_action_available = False
            custom_action_turn_counter = 0
            print(f"[CUSTOM ACTION] Used! Now on cooldown for {CUSTOM_ACTION_COOLDOWN} turns")
            
            # Cancel countdown timer
            if countdown_task and not countdown_task.done():
                countdown_task.cancel()
                print("[COUNTDOWN] Custom action - cancelling countdown timer")
            
            # Cancel auto-play timer
            if auto_advance_task and not auto_advance_task.done():
                auto_advance_task.cancel()
                print("[AUTO-PLAY] Custom action - cancelling auto-play timer")
            
            # Process exactly like ChoiceButton callback
            try:
                await interaction.response.defer()
            except Exception:
                pass
            
            # CRITICAL: Disable ALL buttons immediately to prevent double-click race condition
            # Get the view from the interaction's message
            try:
                message = await interaction.channel.fetch_message(interaction.message.id)
                if message and message.components:
                    # Disable the view that spawned this modal
                    for component in message.components:
                        for item in component.children:
                            item.disabled = True
                    await message.edit(view=discord.ui.View.from_message(message))
                    print("[CUSTOM ACTION] Buttons disabled to prevent concurrent actions")
            except Exception as e:
                print(f"[CUSTOM ACTION] Warning: Could not disable buttons: {e}")
            

            world_state = engine.get_state().get('world_prompt', '')
            
            # Get spatial context from last vision analysis
            spatial_context = ""
            try:
                if engine.history and len(engine.history) > 0:
                    last_vision = engine.history[-1].get("vision_analysis", "")
                    if last_vision:
                        spatial_context = f"\n\nCURRENT LOCATION: {last_vision[:150]}\nThis action MUST happen from player's CURRENT position. Interpret within physical constraints of THIS location."
            except Exception as e:
                print(f"[FREE WILL] Could not get spatial context: {e}")
            
            # Fast LLM call for micro-reaction
            micro_prompt = (
                "Given the player's custom action: '" + custom_choice + "', and the current world state: '" + world_state + "'" + spatial_context + ", "
                "write a 1-sentence immediate world or NPC reaction. Start with a relevant emoji. Be suspenseful, direct, and avoid spoilers."
            )
            try:
                micro_reaction = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: engine._ask(micro_prompt, model="gemini", temp=0.4, tokens=50)
                )
                # Ensure we never have empty string for Discord embed
                if not micro_reaction or not micro_reaction.strip():
                    micro_reaction = " The world holds its breath."
            except Exception as e:
                print(f"[CUSTOM ACTION] Micro reaction failed: {e}")
                micro_reaction = " The world holds its breath."
            
            micro_msg = await interaction.channel.send(embed=discord.Embed(
                description=safe_embed_desc(micro_reaction),
                color=CORNER_TEAL
            ))
            
            # Show action taken
            await asyncio.sleep(0.6)
            action_msg = await interaction.channel.send(embed=discord.Embed(
                description=safe_embed_desc(f"**Action:** {custom_choice}"),
                color=CORNER_TEAL  # purple for action
            ))
            
            # Phase 1: Generate dispatch and image FAST
            loop = asyncio.get_running_loop()
            
            # Compute fate INSTANTLY (no wait)
            fate = compute_fate()
            print(f"[FATE CUSTOM] Computed: {fate}")
            
            # Start Phase 1 immediately with fate modifier (image generation in background)
            phase1_task = loop.run_in_executor(None, engine.advance_turn_image_fast, custom_choice, fate)
            
            # WHILE image is generating, show atmospheric beats (fills wait time!)
            # Clean up interim messages first
            try:
                await micro_msg.delete()
            except Exception:
                pass
            try:
                await action_msg.delete()
            except Exception:
                pass
            
            await asyncio.sleep(0.3)  # Brief breath
            
            # BEAT 1: Fate animation (rolling the dice) - KEEP ORIGINAL
            await animate_fate_roll(interaction.channel, fate)
            await asyncio.sleep(0.5)  # Let the fate result sink in before continuing
            
            # BEAT 2: Anticipation based on CHOICE (not outcome - so no wait needed!)
            choice_lower = custom_choice.lower()
            if any(word in choice_lower for word in ["sprint", "run", "dash", "bolt", "charge"]):
                anticipation = "💨 Movement registered..."
            elif any(word in choice_lower for word in ["smash", "break", "destroy", "kick", "bash"]):
                anticipation = "💥 Impact imminent..."
            elif any(word in choice_lower for word in ["hide", "crouch", "duck", "flatten"]):
                anticipation = "🤫 Concealment attempt..."
            elif any(word in choice_lower for word in ["climb", "vault", "scramble", "ascend"]):
                anticipation = "🧗 Vertical movement..."
            elif any(word in choice_lower for word in ["grab", "take", "pick up", "snatch"]):
                anticipation = "🤲 Interaction detected..."
            else:
                anticipation = "⏳ Action processing..."
            
            await interaction.channel.send(embed=discord.Embed(
                description=anticipation,
                color=CORNER_GREY
            ))
            await asyncio.sleep(0.8)
            
            # BEAT 3: Fate flavor (Lucky/Unlucky hint) - KEEP ORIGINAL
            fate_flavor = get_tension_beat(fate, "")  # Empty movement type for now
            await interaction.channel.send(embed=discord.Embed(
                description=fate_flavor,
                color=CORNER_GREY if fate == "NORMAL" else (CORNER_TEAL if fate == "LUCKY" else VHS_RED)
            ))
            await asyncio.sleep(0.8)
            
            # NOW wait for Phase 1 to complete (image + dispatch generation)
            phase1 = await phase1_task
            
            disp = phase1
            choice_text = custom_choice
            
            # BEAT 4: Display IMAGE FIRST (immediately, no delay)
            img_path = disp.get("consequence_image")
            video_path = disp.get("consequence_video")
            
            # Track image for VHS tape
            global _run_images
            print(f"[TAPE RECORDING CUSTOM] Checking if image should be recorded...")
            print(f"[TAPE RECORDING CUSTOM]   img_path = {img_path}")
            print(f"[TAPE RECORDING CUSTOM]   video_path = {video_path}")
            print(f"[TAPE RECORDING CUSTOM]   Current tape has {len(_run_images)} frames")
            if img_path:
                _run_images.append(img_path)
                print(f"[TAPE] Frame {len(_run_images)} recorded: {img_path}")
            else:
                print(f"[TAPE] ERROR: NO IMAGE PATH - Frame NOT recorded!")
            
            # HD MODE: If video available, send video (with auto-play)
            if video_path:
                print(f"[HD MODE CUSTOM] Video available, displaying video player...")
                video_file, video_name = _attach_video(video_path)
                if video_file:
                    await interaction.channel.send(
                        content="🎬 **HD VIDEO**",
                        file=video_file
                    )
                    print(f"[BOT CUSTOM] Video displayed with auto-play: {video_name}")
                else:
                    print(f"[HD MODE CUSTOM] Video file not found, falling back to image only")
            
            # Always send the last frame image (for game logic and as fallback)
            if img_path:
                if not video_path:
                    # Normal mode: just the image
                    print(f"[BOT] Image displayed immediately (before choices)!")
                    # Handle absolute paths properly
                    display_path = Path(img_path) if Path(img_path).is_absolute() else Path(img_path.lstrip("/"))
                    await interaction.channel.send(file=discord.File(display_path))
                else:
                    # HD mode: image already sent with video, skip duplicate
                    print(f"[BOT CUSTOM] Image skipped (video already displayed)")
                await asyncio.sleep(0.5)
            
            # BEAT 5: Show consequence text AFTER image (appears underneath)
            movement_indicator = get_movement_indicator()
            dispatch_text = disp["dispatch"]
            if movement_indicator:
                dispatch_text = f"{movement_indicator}\n\n{dispatch_text}"
            
            await interaction.channel.send(embed=discord.Embed(
                title="⚡ Consequence",
                description=safe_embed_desc(dispatch_text),
                color=VHS_RED
            ))
            await asyncio.sleep(0.3)  # Brief pause so they can read it
            
            # CHECK FOR DEATH - Read FRESH state
            # Note: get_state() already reloads from disk via API client
            current_state = engine.get_state()
            player_alive = current_state.get("player_state", {}).get("alive", True)
            player_health = current_state.get("player_state", {}).get("health", 100)
            
            print(f"[DEATH CHECK CUSTOM] alive={player_alive}, health={player_health}")
            
            if not player_alive:
                print("[DEATH] Player died from custom action!")
                await interaction.channel.send(embed=discord.Embed(
                    title="💀 YOU DIED",
                    description="The camera stops recording.",
                    color=VHS_RED
                ))
                await asyncio.sleep(1)
                
                # Show VHS ejecting sequence WHILE tape is being created
                eject_msg = await interaction.channel.send(embed=discord.Embed(
                    description="`[STOP]` ⏏️ EJECTING TAPE...",
                    color=VHS_RED
                ))
                
                # Start tape creation in background
                loop = asyncio.get_running_loop()
                tape_task = loop.run_in_executor(None, _create_death_replay_tape_with_lock)
                
                # VHS eject animation (plays while GIF generates)
                eject_sequence = [
                    (0.8, "`[STOP]` ⏏️\n`REWINDING...`"),
                    (0.8, "`[STOP]` ⏏️\n`[███░░░░░░░]`"),
                    (0.8, "`[STOP]` ⏏️\n`[██████░░░░]`"),
                    (0.8, "`[STOP]` ⏏️\n`[█████████░]`"),
                    (0.8, "`[STOP]` ⏏️\n`FINALIZING...`"),
                    (1.0, "`[STOP]` ⏏️\n`TAPE READY`")
                ]
                
                for delay, message in eject_sequence:
                    done, pending = await asyncio.wait([tape_task], timeout=delay)
                    if done:
                        break
                    try:
                        await eject_msg.edit(embed=discord.Embed(
                            description=message,
                            color=VHS_RED
                        ))
                    except Exception:
                        break
                
                # Wait for completion
                tape_path, error_msg = await tape_task
                
                # Clean up animation
                try:
                    await eject_msg.delete()
                except Exception:
                    pass
                
                # Send tape or error
                if tape_path:
                    await interaction.channel.send(embed=discord.Embed(
                        title=" VHS TAPE RECOVERED",
                        description="Camera footage retrieved from scene.",
                        color=CORNER_GREY
                    ))
                    try:
                        await interaction.channel.send(file=discord.File(tape_path))
                        print("[DEATH]  Tape uploaded - waiting for player to download...")
                    except Exception as e:
                        print(f"[DEATH] Failed to send tape: {e}")
                        await interaction.channel.send(embed=discord.Embed(
                            title="WARNING: Tape Upload Failed",
                            description=f"Tape created but upload failed: {e}",
                            color=VHS_RED
                        ))
                else:
                    await interaction.channel.send(embed=discord.Embed(
                        title="WARNING: No Tape Created",
                        description=f"**Reason:** {error_msg}",
                        color=VHS_RED
                    ))
                
                # Create Play Again button (independent of disabled view)
                manual_restart_done = asyncio.Event()  # Flag to prevent double restart
                
                class PlayAgainButton(Button):
                    def __init__(self):
                        super().__init__(label="️ Play Again", style=discord.ButtonStyle.success)
                    
                    async def callback(self, button_interaction: discord.Interaction):
                        # Authorization check
                        if not check_authorization(button_interaction, OWNER_ID):
                            await button_interaction.response.send_message(
                                "🔒 Only the game owner can restart.",
                                ephemeral=True
                            )
                            return
                        
                        global auto_advance_task, countdown_task, auto_play_enabled
                        print("[DEATH CUSTOM] Play Again button pressed - manual restart")
                        
                        # Mark that manual restart is happening
                        manual_restart_done.set()
                        
                        try:
                            await button_interaction.response.defer()
                        except Exception:
                            pass
                        
                        # Cancel all running tasks
                        if auto_advance_task and not auto_advance_task.done():
                            auto_advance_task.cancel()
                        if countdown_task and not countdown_task.done():
                            countdown_task.cancel()
                        auto_play_enabled = False
                        
                        # Reset game
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, ChoiceButton._do_reset_static)
                        
                        # Show intro
                        await send_intro_tutorial(button_interaction.channel)
                
                # Show Play Again button and leave it (no auto-restart)
                play_again_view = View(timeout=None)
                play_again_view.add_item(PlayAgainButton())
                await interaction.channel.send(
                    embed=discord.Embed(
                        description="💾 **Save the tape!** Press Play Again when ready.",
                        color=CORNER_GREY
                    ),
                    view=play_again_view
                )
                
                print("[DEATH CUSTOM] Play Again button ready - waiting for manual restart (no auto-restart)")
                return  # End turn here - button will handle restart when clicked
            
            # Phase 2: Generate choices (deferred)
            print(f"[BOT DEBUG] Passing to Phase 2: image={img_path}, dispatch={disp['dispatch'][:50]}...")
            phase2_task = loop.run_in_executor(
                None, 
                engine.advance_turn_choices_deferred,
                img_path,
                disp["dispatch"],
                disp.get("vision_dispatch", ""),
                choice_text
            )
            
            # Show "analyzing scene..." message
            choices_loading_msg = await interaction.channel.send(embed=discord.Embed(
                description="⚙️ Analyzing scene...",
                color=CORNER_GREY
            ))
            
            # Wait for Phase 2
            phase2 = await phase2_task
            disp.update(phase2)  # Merge in the choices
            
            # Delete loading message
            try:
                await choices_loading_msg.delete()
            except Exception:
                pass
            
            # Show situation report (world evolution text)
            if disp.get("situation_report"):
                await interaction.channel.send(embed=discord.Embed(
                    description=safe_embed_desc(f"📍 {disp['situation_report']}"),
                    color=VHS_RED
                ))
            
            # Show new choices
            await interaction.channel.send("🟢 What will you do next?")
            view = ChoiceView(disp["choices"])
            await send_choices(interaction.channel, disp["choices"], view, getattr(self, 'last_choices_message', None))
            
            # Start countdown timer (if not in auto-play mode)
            if not auto_play_enabled:
                start_countdown_timer(
                    interaction.channel,
                    disp["choices"],
                    view,
                    disp.get("dispatch", ""),
                    disp.get("situation_report", ""),
                    disp.get("consequence_image")
                )
            
            # Start auto-advance timer
            start_auto_advance_timer(interaction.channel, disp["choices"], view)
    
    class CustomActionButton(Button):
        def __init__(self):
            global custom_action_available, custom_action_turn_counter
            
            # Check if custom action is on cooldown
            if custom_action_available:
                super().__init__(
                    emoji="⚡",
                    label="Free Will",
                    style=discord.ButtonStyle.danger,
                    row=1,
                    disabled=False
                )
            else:
                turns_remaining = CUSTOM_ACTION_COOLDOWN - custom_action_turn_counter
                super().__init__(
                    emoji="⚡",
                    label=f"Free Will ({turns_remaining})",
                    style=discord.ButtonStyle.secondary,
                    row=1,
                    disabled=True
                )
        
        async def callback(self, interaction: discord.Interaction):
            # Authorization check
            if hasattr(self, 'view') and self.view and hasattr(self.view, 'owner_id'):
                if not check_authorization(interaction, self.view.owner_id):
                    await interaction.response.send_message(
                        "🔒 Only the game owner can use Free Will.",
                        ephemeral=True
                    )
                    return
            
            print("[LOG] CustomActionButton callback triggered")
            modal = CustomActionModal()
            await interaction.response.send_modal(modal)

    class RestartButton(Button):
        def __init__(self):
            super().__init__(emoji="⏏️", style=discord.ButtonStyle.danger, row=2)
            self.label = None  # Emoji only - VHS eject button

        async def callback(self, interaction: discord.Interaction):
            # Authorization check
            if hasattr(self, 'view') and self.view and hasattr(self.view, 'owner_id'):
                if not check_authorization(interaction, self.view.owner_id):
                    await interaction.response.send_message(
                        "🔒 Only the game owner can restart the game.",
                        ephemeral=True
                    )
                    return
            
            global auto_advance_task, countdown_task, auto_play_enabled
            
            print("[LOG] RestartButton callback triggered")
            try:
                await interaction.response.defer()
            except Exception as e:
                print(f"[LOG] RestartButton defer failed: {e}")
                pass
            
            # IMMEDIATELY disable all choice buttons
            view = self.view
            if view and hasattr(view, 'children'):
                for item in view.children:
                    item.disabled = True
                try:
                    if hasattr(view, 'last_choices_message') and view.last_choices_message:
                        await view.last_choices_message.edit(view=view)
                        print("[RESTART] Buttons disabled immediately")
                except Exception as e:
                    print(f"[RESTART] Warning: Could not disable buttons: {e}")
            
            # Cancel all running async tasks
            print("[RESTART] Cancelling all running tasks...")
            
            # Cancel auto-play
            if auto_advance_task and not auto_advance_task.done():
                auto_advance_task.cancel()
                print("[RESTART] Cancelled auto-play task")
            auto_advance_task = None
            auto_play_enabled = False
            
            # Cancel countdown
            if countdown_task and not countdown_task.done():
                countdown_task.cancel()
                print("[RESTART] Cancelled countdown task")
            countdown_task = None
            
            print("[RESTART] All tasks cancelled")
            
            # Show VHS ejecting sequence WHILE tape is being created
            eject_msg = await interaction.channel.send(embed=discord.Embed(
                description="`[STOP]` ⏏️ EJECTING TAPE...",
                color=VHS_RED
            ))
            
            # Start tape creation in background (runs in executor)
            loop = asyncio.get_running_loop()
            tape_task = loop.run_in_executor(None, _create_death_replay_tape)
            
            # VHS eject animation sequence (plays while GIF generates)
            eject_sequence = [
                (0.8, "`[STOP]` ⏏️\n`REWINDING...`"),
                (0.8, "`[STOP]` ⏏️\n`[███░░░░░░░]`"),
                (0.8, "`[STOP]` ⏏️\n`[██████░░░░]`"),
                (0.8, "`[STOP]` ⏏️\n`[█████████░]`"),
                (0.8, "`[STOP]` ⏏️\n`FINALIZING...`"),
                (1.0, "`[STOP]` ⏏️\n`TAPE READY`")
            ]
            
            # Cycle through sequence while tape generates
            for delay, message in eject_sequence:
                done, pending = await asyncio.wait([tape_task], timeout=delay)
                if done:
                    break  # Tape ready, stop animation
                try:
                    await eject_msg.edit(embed=discord.Embed(
                        description=message,
                        color=VHS_RED
                    ))
                except Exception:
                    break
            
            # Wait for tape generation to complete
            tape_path, error_msg = await tape_task
            
            # Clean up ejecting message
            try:
                await eject_msg.delete()
            except Exception:
                pass
            
            # Send tape or error message
            if tape_path:
                await interaction.channel.send(embed=discord.Embed(
                    title=" VHS TAPE SAVED",
                    description="Recording saved before restart.",
                    color=CORNER_GREY
                ))
                try:
                    await interaction.channel.send(file=discord.File(tape_path))
                    print(f"[RESTART] Tape saved: {tape_path}")
                except Exception as e:
                    print(f"[RESTART] Failed to send tape: {e}")
                    await interaction.channel.send(embed=discord.Embed(
                        title="WARNING: Tape Upload Failed",
                        description=f"Tape was created but failed to upload: {e}",
                        color=VHS_RED
                    ))
            else:
                # Tape creation failed - tell the user why
                await interaction.channel.send(embed=discord.Embed(
                    title="WARNING: No Tape Created",
                    description=f"Could not create VHS tape.\n\n**Reason:** {error_msg}",
                    color=VHS_RED
                ))
                print(f"[RESTART] No tape created: {error_msg}")
            
            # 1. Show the welcome embed and Play button
            await send_intro_tutorial(interaction.channel)
            # 2. Do the reset in the background (so it doesn't block Discord)
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, self._do_reset)
            # 3. Reset inactivity timer
            # start_inactivity_timer(interaction.channel)  # TODO: Not implemented yet

        def _do_reset(self):
            global _run_images
            try:
                with open(ROOT / "history.json", "w", encoding="utf-8") as f:
                    f.write("[]")

                intro_prompt = engine.PROMPTS["world_initial_state"]
                with open(ROOT / "world_state.json", "w", encoding="utf-8") as f:
                    json.dump({
                        "world_prompt": intro_prompt,
                        "current_phase": "normal",
                        "chaos_level": 0,
                        "last_choice": "",
                        "last_saved": "",
                        "seen_elements": [],
                        "player_state": {"alive": True, "health": 100}  # Reset player state consistently
                    }, f, indent=2)
                engine.reset_state()
                
                # Clear tape recording for new run (CRITICAL FIX)
                _run_images.clear()
                print("[RESTART] Game state reset complete. New tape ready.")
            except Exception as e:
                print(f"[RESTART] Reset error: {e}")

    class AutoPlayDelayModal(Modal, title="Auto-Play Settings"):
        delay_input = TextInput(
            label="Delay between choices (seconds)",
            placeholder="Enter delay in seconds (e.g., 45)",
            default="45",
            min_length=1,
            max_length=4,
            required=True
        )
        
        def __init__(self, parent_view):
            super().__init__()
            self.parent_view = parent_view
        
        async def on_submit(self, interaction: discord.Interaction):
            global auto_play_enabled, AUTO_PLAY_DELAY, current_choices, current_view
            
            try:
                # Parse the delay value
                delay = int(self.delay_input.value)
                if delay < 1:
                    await interaction.response.send_message("WARNING: Delay must be at least 1 second!", ephemeral=True)
                    return
                
                AUTO_PLAY_DELAY = delay
                auto_play_enabled = True
                
                print(f"[AUTO-PLAY] Enabled with {AUTO_PLAY_DELAY}s delay")
                print(f"[AUTO-PLAY] current_choices: {current_choices}")
                print(f"[AUTO-PLAY] current_view: {current_view}")
                print(f"[AUTO-PLAY] parent_view.choices: {self.parent_view.choices if hasattr(self.parent_view, 'choices') else 'N/A'}")
                
                # Update button appearance
                for item in self.parent_view.children:
                    if isinstance(item, AutoPlayToggleButton):
                        item.label = "🤖 Auto: ON"
                        item.style = discord.ButtonStyle.success
                        break
                
                # Acknowledge the modal
                await interaction.response.send_message(
                    f"🤖 Auto-Play enabled! Making choices every {AUTO_PLAY_DELAY} seconds...",
                    ephemeral=True
                )
                
                # Update the view to show new button state
                try:
                    if hasattr(self.parent_view, 'last_choices_message') and self.parent_view.last_choices_message:
                        await self.parent_view.last_choices_message.edit(view=self.parent_view)
                except Exception as e:
                    print(f"[AUTO-PLAY] Failed to update button: {e}")
                
                # Set the current choices and view from parent_view
                if hasattr(self.parent_view, 'choices'):
                    current_choices = self.parent_view.choices
                    current_view = self.parent_view
                    print(f"[AUTO-PLAY] Set current_choices from parent_view: {current_choices}")
                
                # Make an IMMEDIATE choice
                if current_choices and current_view:
                    print("[AUTO-PLAY] Making immediate first choice...")
                    await auto_advance_turn(interaction.channel)
                else:
                    print("[AUTO-PLAY] No choices available yet - will start on next turn")
                
            except ValueError:
                await interaction.response.send_message("WARNING: Please enter a valid number!", ephemeral=True)

    class AutoPlayToggleButton(Button):
        def __init__(self, parent_view):
            global auto_play_enabled
            label = "🤖 Auto: ON" if auto_play_enabled else "🤖 Auto: OFF"
            style = discord.ButtonStyle.success if auto_play_enabled else discord.ButtonStyle.secondary
            super().__init__(label=label, style=style, row=1)
            self.parent_view = parent_view

        async def callback(self, interaction: discord.Interaction):
            # Authorization check
            if hasattr(self, 'view') and self.view and hasattr(self.view, 'owner_id'):
                if not check_authorization(interaction, self.view.owner_id):
                    await interaction.response.send_message(
                        "🔒 Only the game owner can toggle auto-play.",
                        ephemeral=True
                    )
                    return
            
            global auto_play_enabled, auto_advance_task, countdown_task
            
            if auto_play_enabled:
                # Disable auto-play
                auto_play_enabled = False
                print("[AUTO-PLAY] Disabled by user")
                
                # Update button appearance
                self.label = "🤖 Auto: OFF"
                self.style = discord.ButtonStyle.secondary
                
                # Cancel any running auto-play timer
                if auto_advance_task and not auto_advance_task.done():
                    auto_advance_task.cancel()
                    print("[AUTO-PLAY] Timer cancelled")
                
                # RESTART COUNTDOWN TIMER (bug fix!)
                # When auto-play is OFF, countdown should resume
                if hasattr(self.parent_view, 'last_choices_message') and self.parent_view.last_choices_message:
                    # Get current game state to restart countdown

                    current_state = engine.get_state()
                    
                    # Restart countdown timer
                    if COUNTDOWN_ENABLED:
                        # Get choices from view
                        choices = getattr(self.parent_view, 'choices', [])
                        if choices:
                            print("[AUTO-PLAY] Restarting countdown timer...")
                            start_countdown_timer(
                                interaction.channel,
                                choices,
                                self.parent_view,
                                "",  # dispatch not needed for countdown
                                "",  # situation not needed
                                None  # current_image_path not needed
                            )
                
                # Update the view to show new button state
                try:
                    await interaction.response.edit_message(view=self.parent_view)
                except Exception as e:
                    print(f"[AUTO-PLAY] Failed to update button: {e}")
                    try:
                        await interaction.response.defer()
                    except:
                        pass
            else:
                # Show modal to set delay
                modal = AutoPlayDelayModal(self.parent_view)
                await interaction.response.send_modal(modal)
    
    class QualityToggleButton(Button):
        def __init__(self, parent_view):
            global quality_mode_enabled
            label = "⚡ Quality: HQ" if quality_mode_enabled else "⚡ Quality: FAST"
            style = discord.ButtonStyle.success if quality_mode_enabled else discord.ButtonStyle.secondary
            super().__init__(label=label, style=style, row=1)
            self.parent_view = parent_view
        
        async def callback(self, interaction: discord.Interaction):
            # Authorization check
            if hasattr(self, 'view') and self.view and hasattr(self.view, 'owner_id'):
                if not check_authorization(interaction, self.view.owner_id):
                    await interaction.response.send_message(
                        "🔒 Only the game owner can toggle quality mode.",
                        ephemeral=True
                    )
                    return
            
            global quality_mode_enabled

            
            # Toggle quality mode
            quality_mode_enabled = not quality_mode_enabled
            engine.QUALITY_MODE = quality_mode_enabled
            
            # Update button appearance
            if quality_mode_enabled:
                self.label = "⚡ Quality: HQ"
                self.style = discord.ButtonStyle.success
                print("[QUALITY MODE] HQ - Using Gemini Pro (slower, higher quality)")
            else:
                self.label = "⚡ Quality: FAST"
                self.style = discord.ButtonStyle.secondary
                print("[QUALITY MODE] FAST - Using Gemini Flash (faster, lower quality)")
            
            # Update the view to show new button state
            try:
                await interaction.response.edit_message(view=self.parent_view)
            except Exception as e:
                print(f"[HD MODE] Failed to update button: {e}")
                try:
                    await interaction.response.defer()
                except:
                    pass

    class MapButton(Button):
        def __init__(self):
            super().__init__(emoji="🗺️", style=discord.ButtonStyle.secondary, row=1)
            self.label = None  # Emoji only

        async def callback(self, interaction: discord.Interaction):
            print("[LOG] MapButton callback triggered")
            try:
                await interaction.response.defer()
            except Exception as e:
                print(f"[LOG] MapButton defer failed: {e}")
                pass
            try:
                # Return to hub logic
                hub_choice = "Return to your truck on the outskirts of the Horizon military quarantine perimeter"
                print(f"[LOG] MapButton: sending hub_choice: {hub_choice}")
                await interaction.followup.send("Returning to hub...", ephemeral=True)

                disp = await asyncio.get_running_loop().run_in_executor(None, engine.complete_tick, hub_choice)
                print(f"[LOG] MapButton: engine.complete_tick returned: {disp}")
                file2, name2 = _attach(disp.get("dispatch_image"), disp.get("vision_dispatch", ""))
                print(f"[LOG] MapButton: attached file: {file2}, name: {name2}")
                if file2:
                    await interaction.channel.send(file=file2)
                situation_report = disp.get("situation_report") or disp.get("dispatch") or ""
                world_embed = discord.Embed(
                    description=situation_report.strip(),
                    color=VHS_RED
                )
                await interaction.channel.send(embed=world_embed)
                ch = interaction.channel
                await _tick(ch)
                print("[LOG] MapButton: finished _tick")
            except Exception as e:
                print(f"[LOG] MapButton exception: {e}")
                await interaction.channel.send(f"Failed to return to hub: {e}")

            # Reset auto-advance timer on button press
            if auto_advance_task and not auto_advance_task.done():
                auto_advance_task.cancel()

    class RegenerateChoicesButton(Button):
        def __init__(self, parent_view):
            super().__init__(emoji="⏏️", label="Regenerate Choices", style=discord.ButtonStyle.secondary, row=1)
            self.parent_view = parent_view

        async def callback(self, interaction: discord.Interaction):
            # Authorization check
            if hasattr(self, 'view') and self.view and hasattr(self.view, 'owner_id'):
                if not check_authorization(interaction, self.view.owner_id):
                    await interaction.response.send_message(
                        "🔒 Only the game owner can regenerate choices.",
                        ephemeral=True
                    )
                    return
            
            print("[LOG] RegenerateChoicesButton callback triggered")
            try:
                await interaction.response.defer()
            except Exception as e:
                print(f"[LOG] RegenerateChoicesButton defer failed: {e}")
                pass
            try:

                from choices import generate_choices
                last_vision_dispatch = engine.history[-1].get('vision_dispatch', '') if engine.history else ''
                print(f"[LOG] RegenerateChoicesButton: last_vision_dispatch: {last_vision_dispatch}")
                image_desc = ''
                time_of_day = engine.state.get('time_of_day', '')
                loop = asyncio.get_running_loop()
                new_choices = await loop.run_in_executor(
                    None,
                    generate_choices,
                    engine.client,
                    engine.choice_tmpl,
                    last_vision_dispatch,
                    3,
                    None,
                    '',
                    '',
                    last_vision_dispatch,
                    image_desc,
                    time_of_day,
                    '',
                    None,
                    '',
                    0.8  # Set temperature to 0.8 for more creative choices
                )
                print(f"[LOG] RegenerateChoicesButton: new_choices: {new_choices}")
                new_view = ChoiceView(new_choices)
                # Delete the previous choices message if it exists
                if hasattr(self.parent_view, "last_choices_message") and self.parent_view.last_choices_message:
                    try:
                        await self.parent_view.last_choices_message.delete()
                    except Exception as e:
                        print(f"[LOG] Could not delete previous choices message: {e}")
                # Send the new choices and store the message object
                new_msg = await interaction.channel.send("🟢 What will you do next? (choices regenerated)", view=new_view)
                new_view.last_choices_message = new_msg
                
                # Start auto-advance timer with new choices
                start_auto_advance_timer(interaction.channel, new_choices, new_view)
            except Exception as e:
                print(f"[LOG] RegenerateChoicesButton exception: {e}")
                try:
                    await interaction.followup.send(f"Failed to regenerate choices: {e}", ephemeral=True)
                except Exception:
                    await interaction.channel.send(f"Failed to regenerate choices: {e}")

            # Reset auto-advance timer on button press
            if auto_advance_task and not auto_advance_task.done():
                auto_advance_task.cancel()

    # ───────── per‑tick workflow ────────────────────────────────────────────────
    async def _tick(ch: discord.TextChannel):
        # 1. snapshot
        snap = await asyncio.get_running_loop().run_in_executor(None, engine.begin_tick)
        print("WORLD IMAGE PATH:", snap.get("world_image"))
        file1, name1 = _attach(snap.get("world_image"))
        print("ATTACH FILE:", file1, "NAME:", name1)

        # Send the situation report as a separate message before the choices
        situation_report = snap.get("situation_report", "").strip()
        if situation_report:
            # Remove 'Situation Update' header and prefix
            clean_report = situation_report
            if clean_report.lower().startswith("situation update"):
                # Remove 'Situation Update' and any following dash or colon
                clean_report = clean_report[len("situation update"):].lstrip(" -:")
            clean_report = clean_report.strip()
            embed_sr = discord.Embed(
                title="Time Advances.",
                description=clean_report,
                color=VHS_RED
            )
            await ch.send(embed=embed_sr)

        # Present choices as buttons, with restart button for owner
        view = ChoiceView(snap["choices"], owner_id=OWNER_ID)
        if file1:
            # Attach the choices view directly to the world image file message
            await ch.send(file=file1, view=view)
        else:
            # If no image, send just the view with a minimal content fallback
            await ch.send(content=" ", view=view)

    # ───────── owner commands ───────────────────────────────────────────────────
    @bot.command()
    @commands.is_owner()
    async def pause(ctx): global running; running=False; await ctx.reply("Paused.")
    @bot.command()
    @commands.is_owner()
    async def resume(ctx): global running; running=True; await ctx.reply("Resumed.")
    @bot.command()
    @commands.is_owner()
    async def restart_game(ctx):
        """Owner-only: Reset the game state and start over."""
        try:
            # Reset history.json
            with open(ROOT / "history.json", "w", encoding="utf-8") as f:
                f.write("[]")
            # Reset world_state.json with the original intro prompt

            intro_prompt = engine.PROMPTS["world_initial_state"]
            with open(ROOT / "world_state.json", "w", encoding="utf-8") as f:
                json.dump({
                    "world_prompt": intro_prompt,
                    "current_phase": "normal",
                    "chaos_level": 0,
                    "last_choice": "",
                    "last_saved": "",
                    "seen_elements": []
                }, f, indent=2)
            engine.reset_state()
            intro_result = engine.generate_intro_turn()
            
            # Send intro text (no image, consistent with regular turn flow)
            dispatch_text = intro_result.get("dispatch", "")
            if dispatch_text:
                await ctx.send(embed=discord.Embed(
                    description=safe_embed_desc(dispatch_text.strip()),
                    color=VHS_RED
                ))
            
            # Send choices
            await ctx.send("🟢 What will you do next?")
            view = ChoiceView(intro_result["choices"], owner_id=OWNER_ID)
            await ctx.send(content=" ", view=view)
        except Exception as e:
            await ctx.send(f"Failed to reset game: {e}")

    def beginning_simulation_embed():
        embed = discord.Embed(
            title="🟢 Beginning Simulation",
            color=CORNER_TEAL
        )
        embed.add_field(name="Status", value="The world is initializing...\nPlease stand by.", inline=False)
        embed.set_footer(text="Get ready to explore!")
        return embed

    # --- Helper to send intro tutorial (rules embed + Play button) ---
    async def send_intro_tutorial(channel):
        rules_embed = discord.Embed(
            title=" Welcome to SOMEWHERE: An Analog Horror Story",
            color=CORNER_TEAL
        )
        rules_embed.add_field(
            name="You Are",
            value="Jason Fleece — an investigative journalist with a troubled past.",
            inline=False
        )
        rules_embed.add_field(
            name="Turn Structure",
            value="️ Each turn: a VHS-style image + a narrative segment.",
            inline=False
        )
        rules_embed.add_field(
            name="Controls",
            value="️ Press Play to continue the story.\n🔘 Click buttons to choose your action.",
            inline=False
        )
        rules_embed.add_field(
            name="Pro Tips",
            value="🕰️ Story escalates slowly — pay attention to details!\n🧭 Your choices shape Jason's fate.",
            inline=False
        )
        rules_embed.set_footer(text="Ready? Press ️ Play below to begin.")

        
        class AIProviderSelect(discord.ui.Select):
            """Dropdown menu for selecting AI provider presets."""
            def __init__(self):
                options = [
                    discord.SelectOption(
                        label="Gemini",
                        value="gemini",
                        default=True
                    ),
                    discord.SelectOption(
                        label="OpenAI",
                        value="openai"
                    )
                ]
                super().__init__(
                    placeholder="🎛️ Select AI Model",
                    options=options,
                    row=0
                )
            
            async def callback(self, interaction: discord.Interaction):
                """Handle AI provider selection."""
                preset_name = self.values[0]
                
                # Validate API keys before switching

                if preset_name == "openai" and not engine.OPENAI_API_KEY:
                    await interaction.response.send_message(
                        "ERROR: **OpenAI API key not configured!**\nCannot switch to OpenAI provider.",
                        ephemeral=True
                    )
                    return
                
                # Switch to selected preset
                success = ai_provider_manager.set_preset(preset_name)
                
                if success:
                    status = ai_provider_manager.get_status()
                    embed = discord.Embed(
                        title=f" Switched to `{preset_name}`",
                        description=status,
                        color=CORNER_TEAL
                    )
                else:
                    embed = discord.Embed(
                        title="ERROR: Switch Failed",
                        description=f"Could not switch to `{preset_name}`. Check logs.",
                        color=VHS_RED
                    )
                
                await interaction.response.send_message(embed=embed, ephemeral=True)
        
        class LoreCacheToggle(Button):
            def __init__(self):
                # Check current state
                config = lore_cache_manager.load_config()
                is_enabled = config.get("enabled", False)
                
                if is_enabled:
                    label = "📚 Lore: ON"
                    style = discord.ButtonStyle.success
                else:
                    label = "📚 Lore: OFF"
                    style = discord.ButtonStyle.secondary
                
                super().__init__(label=label, style=style, row=1)
            
            async def callback(self, interaction: discord.Interaction):
                # Toggle lore cache
                config = lore_cache_manager.load_config()
                is_enabled = config.get("enabled", False)
                
                # Flip it
                config["enabled"] = not is_enabled
                lore_cache_manager.save_config(config)
                
                new_state = "ON" if not is_enabled else "OFF"
                
                # Update button appearance
                if not is_enabled:
                    self.label = "📚 Lore: ON"
                    self.style = discord.ButtonStyle.success
                else:
                    self.label = "📚 Lore: OFF"
                    self.style = discord.ButtonStyle.secondary
                
                # Update the view
                await interaction.message.edit(view=self.view)
                
                # Send ephemeral confirmation
                status_emoji = "" if not is_enabled else "ERROR:"
                cost_info = "Cache will be created on first turn (~$0.012/hr)" if not is_enabled else "$0 cost (cache disabled)"
                
                embed = discord.Embed(
                    title=f"{status_emoji} Lore Cache: {new_state}",
                    description=cost_info,
                    color=CORNER_TEAL if not is_enabled else CORNER_GREY
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
        
        class PlayButton(Button):
            def __init__(self):
                super().__init__(label="️ Play", style=discord.ButtonStyle.success, row=1)
            async def callback(self, interaction: discord.Interaction):
                # Authorization check
                if not check_authorization(interaction, OWNER_ID):
                    await interaction.response.send_message(
                        "🔒 Only the game owner can start the game.",
                        ephemeral=True
                    )
                    return
                
                global _run_images  # MUST be at the very top of the function
                
                try:
                    if not interaction.response.is_done():
                        await interaction.response.defer()
                except Exception as e:
                    print(f"[LOG] PlayButton defer failed: {e}")
                    try:
                        await interaction.channel.send("This Play button is no longer active. Please use a newer Play button or refresh the session.")
                    except Exception as e2:
                        print(f"[LOG] PlayButton fallback send failed: {e2}")
                    return
                
                # Delete the intro message with the Play buttons
                try:
                    await interaction.message.delete()
                except Exception as e:
                    print(f"[LOG] Could not delete intro message: {e}")
                
                # === SHOW LOGO FIRST (Frame 0 of VHS tape) ===
                logo_path = ROOT / "static" / "Logo"
                
                # Try different logo file extensions
                logo_file = None
                for ext in [".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG"]:
                    test_path = ROOT / "static" / f"Logo{ext}"
                    if test_path.exists():
                        logo_file = test_path
                        break
                
                if logo_file and logo_file.exists():
                    try:
                        # Crop and resize logo to match Gemini's 4:3 output
                        # Gemini is the GOLD STANDARD - logo must match its resolution exactly
                        from PIL import Image
                        logo_img = Image.open(str(logo_file))
                        
                        # Target: 4:3 aspect ratio (Nano Banana Pro's standard for our use case)
                        target_aspect = 4 / 3
                        
                        # Crop logo to 4:3 if needed
                        current_aspect = logo_img.width / logo_img.height
                        if abs(current_aspect - target_aspect) > 0.01:
                            if current_aspect > target_aspect:
                                # Too wide - crop width
                                new_width = int(logo_img.height * target_aspect)
                                left = (logo_img.width - new_width) // 2
                                logo_cropped = logo_img.crop((left, 0, left + new_width, logo_img.height))
                            else:
                                # Too tall - crop height
                                new_height = int(logo_img.width / target_aspect)
                                top = (logo_img.height - new_height) // 2
                                logo_cropped = logo_img.crop((0, top, logo_img.width, top + new_height))
                        else:
                            # Already 4:3!
                            logo_cropped = logo_img
                        
                        # Save at native 4:3 resolution (will match Gemini output naturally)
                        normalized_logo_path = ROOT / "static" / "Logo_normalized.jpg"
                        logo_cropped.save(str(normalized_logo_path), "JPEG", quality=95)
                        
                        # Send the normalized logo
                        await interaction.channel.send(file=discord.File(str(normalized_logo_path)))
                        # Track normalized logo as Frame 0 of VHS tape
                        _run_images.append(f"/static/Logo_normalized.jpg")
                        print(f"[TAPE] Frame 0 (logo) recorded: Logo_normalized.jpg ({logo_cropped.width}x{logo_cropped.height}, 4:3)")
                    except Exception as e:
                        print(f"[LOGO] Failed to process/send logo: {e}")
                else:
                    print(f"[LOGO] Logo file not found at {logo_path}")
                
                await asyncio.sleep(1.5)  # Let logo display
                
                # === DRAMATIC INTRO TEXT ===
                intro_embed = discord.Embed(
                    title=" RECOVERED VHS TAPE - 1993",
                    description=(
                        "Horizon Industries\n"
                        "Four Corners Facility\n\n"
                        "WARNING: WARNING: Disturbing Content"
                    ),
                    color=VHS_RED
                )
                await interaction.channel.send(embed=intro_embed)
                await asyncio.sleep(2)  # Let it sink in
                
                engine.IMAGE_ENABLED = True
                engine.WORLD_IMAGE_ENABLED = True
                
                # PHASE 1: Generate image FAST (start in background)
                loop = asyncio.get_running_loop()
                image_task = loop.run_in_executor(None, engine.generate_intro_image_fast)
                
                # Show VHS loading sequence WHILE generating
                vhs_msg = await interaction.channel.send(embed=discord.Embed(
                    description="`[00:00:00]` VHS PLAYER\n`LOADING...`",
                    color=CORNER_GREY
                ))
                
                vhs_sequence = [
                    (1.5, "`[00:00:03]` TRACKING HEADS\n`ENGAGING...`"),
                    (1.5, "`[00:00:06]` MAGNETIC STRIP\n`READING...`"),
                    (1.5, "`[00:00:09]` VIDEO SIGNAL\n`DETECTED`"),
                    (1.5, "`[00:00:12]` AUDIO CHANNELS\n`SYNCHRONIZING...`"),
                    (1.5, "`[00:00:15]` PLAYBACK\n`STARTING...`")
                ]
                
                # Cycle through VHS sequence while image generates
                for delay, message in vhs_sequence:
                    done, pending = await asyncio.wait([image_task], timeout=delay)
                    if done:
                        break  # Image ready, stop cycling
                    try:
                        await vhs_msg.edit(embed=discord.Embed(
                            description=message,
                            color=CORNER_GREY
                        ))
                    except Exception:
                        break
                
                # Wait for image generation to complete
                intro_phase1 = await image_task
                
                # Clean up VHS loading message
                try:
                    await vhs_msg.delete()
                except Exception:
                    pass
                
                # Send the intro text AND image IMMEDIATELY
                dispatch_text = intro_phase1.get("dispatch", "")
                dispatch_image_path = intro_phase1.get("dispatch_image")
                
                if dispatch_text:
                    await interaction.channel.send(embed=discord.Embed(
                        description=safe_embed_desc(dispatch_text.strip()),
                        color=VHS_RED
                    ))
                
                # Display the opening image if generated
                if dispatch_image_path:
                    # Track intro image for VHS tape (Frame 1, after logo)
                    # (_run_images already declared as global at top of function)
                    _run_images.append(dispatch_image_path)
                    print(f"[TAPE] Frame 1 (intro) recorded - total frames: {len(_run_images)}")
                    
                    try:
                        # Use _attach to handle path correctly (absolute or relative)
                        discord_file, filename = _attach(dispatch_image_path, "Intro frame")
                        if discord_file:
                            await interaction.channel.send(file=discord_file)
                            print(f"[BOT INTRO] Image displayed: {filename}")
                        else:
                            print(f"[BOT INTRO] Image not found: {dispatch_image_path}")
                    except Exception as e:
                        print(f"[BOT INTRO] Failed to send opening image: {e}")
                
                # Show "Generating choices..." while Phase 2 runs
                choices_msg = await interaction.channel.send(embed=discord.Embed(
                    description=safe_embed_desc("⚙️ Generating choices..."),
                    color=CORNER_GREY
                ))
                
                # PHASE 2: Generate choices in background
                choices_task = loop.run_in_executor(
                    None, 
                    engine.generate_intro_choices_deferred,
                    dispatch_image_path,
                    intro_phase1["prologue"],
                    intro_phase1["vision_dispatch"]
                )
                intro_phase2 = await choices_task
                
                # Delete "Generating choices..." message
                try:
                    await choices_msg.delete()
                except Exception:
                    pass
                
                # Send choices
                await interaction.channel.send("🟢 What will you do next?")
                view = ChoiceView(intro_phase2["choices"], owner_id=OWNER_ID)
                await send_choices(interaction.channel, intro_phase2["choices"], view, None)
                
                # Start countdown timer
                start_countdown_timer(
                    interaction.channel,
                    intro_phase2["choices"],
                    view,
                    intro_phase2.get("dispatch", intro_phase1.get("dispatch", "")),
                    intro_phase2.get("situation_report", ""),
                    intro_phase1.get("consequence_image")
                )
                
                # No auto-advance for regular play mode
                
        class PlayNoImagesButton(Button):
            def __init__(self):
                super().__init__(label="️ Play (No Images)", style=discord.ButtonStyle.secondary)
            async def callback(self, interaction: discord.Interaction):
                # Authorization check
                if not check_authorization(interaction, OWNER_ID):
                    await interaction.response.send_message(
                        "🔒 Only the game owner can start the game.",
                        ephemeral=True
                    )
                    return
                
                try:
                    if not interaction.response.is_done():
                        await interaction.response.defer()
                except Exception as e:
                    print(f"[LOG] PlayNoImagesButton defer failed: {e}")
                    try:
                        await interaction.channel.send("This Play button is no longer active. Please use a newer Play button or refresh the session.")
                    except Exception as e2:
                        print(f"[LOG] PlayNoImagesButton fallback send failed: {e2}")
                    return
                
                # Delete the intro message with the Play buttons
                try:
                    await interaction.message.delete()
                except Exception as e:
                    print(f"[LOG] Could not delete intro message: {e}")
                
                engine.IMAGE_ENABLED = False
                engine.WORLD_IMAGE_ENABLED = False
                
                # Run intro generation in executor (start immediately)
                loop = asyncio.get_running_loop()
                intro_task = loop.run_in_executor(None, engine.generate_intro_turn)
                
                # Show VHS loading sequence WHILE generating
                vhs_msg = await interaction.channel.send(embed=discord.Embed(
                    description="`[00:00:00]` VHS PLAYER\n`LOADING...`",
                    color=CORNER_GREY
                ))
                
                vhs_sequence = [
                    (1.5, "`[00:00:03]` TRACKING HEADS\n`ENGAGING...`"),
                    (1.5, "`[00:00:06]` MAGNETIC STRIP\n`READING...`"),
                    (1.5, "`[00:00:09]` VIDEO SIGNAL\n`DETECTED`"),
                    (1.5, "`[00:00:12]` AUDIO CHANNELS\n`SYNCHRONIZING...`"),
                    (1.5, "`[00:00:15]` PLAYBACK\n`STARTING...`")
                ]
                
                # Cycle through VHS sequence while image generates
                for delay, message in vhs_sequence:
                    done, pending = await asyncio.wait([intro_task], timeout=delay)
                    if done:
                        break  # Image ready, stop cycling
                    try:
                        await vhs_msg.edit(embed=discord.Embed(
                            description=message,
                            color=CORNER_GREY
                        ))
                    except Exception:
                        break
                
                # Wait for result
                intro_result = await intro_task
                
                # Clean up VHS loading message
                try:
                    await vhs_msg.delete()
                except Exception:
                    pass
                # 1. Send the intro narrative as an embed
                embed_dispatch = discord.Embed(
                    title="Prologue",
                    description=intro_result.get("dispatch", ""),
                    color=CORNER_TEAL
                )
                await interaction.channel.send(embed=embed_dispatch)
                # 2. Send the vision description as a second embed (what Jason sees)
                embed_vision = discord.Embed(
                    title="What You See",
                    description=intro_result.get("vision_dispatch", ""),
                    color=CORNER_TEAL
                )
                await interaction.channel.send(embed=embed_vision)
                # 3. Send the choices
                await interaction.channel.send("🟢 What will you do next?")
                view = ChoiceView(intro_result["choices"], owner_id=OWNER_ID)
                await send_choices(interaction.channel, intro_result["choices"], view, None)
                
                # Start countdown timer
                start_countdown_timer(
                    interaction.channel,
                    intro_result["choices"],
                    view,
                    intro_result.get("dispatch", ""),
                    intro_result.get("situation_report", ""),
                    intro_result.get("consequence_image")
                )
                
                # No auto-advance for regular play mode
        
        class PlayCinematicButton(Button):
            """ HD mode - Uses Veo video generation (expensive but beautiful)"""
            def __init__(self):
                super().__init__(label=" Play HD", style=discord.ButtonStyle.danger, row=2)
            
            async def callback(self, interaction: discord.Interaction):
                # Authorization check
                if not check_authorization(interaction, OWNER_ID):
                    await interaction.response.send_message(
                        "🔒 Only the game owner can start the game.",
                        ephemeral=True
                    )
                    return
                
                global _run_images
                
                try:
                    if not interaction.response.is_done():
                        await interaction.response.defer()
                except Exception as e:
                    print(f"[LOG] PlayCinematicButton defer failed: {e}")
                    try:
                        await interaction.channel.send("This button is no longer active. Please refresh.")
                    except Exception:
                        pass
                    return
                
                # Delete the intro message
                try:
                    await interaction.message.delete()
                except Exception as e:
                    print(f"[LOG] Could not delete intro message: {e}")
                
                # SET VEO AS THE IMAGE PROVIDER
                import ai_provider_manager
                ai_provider_manager.set_preset("veo")
                print("[CINEMATIC] Veo mode enabled - video-based image generation")
                
                # Reset Veo session costs for fresh run
                try:
                    from veo_video_utils import _session_costs, MAX_SESSION_COST
                    _session_costs["veo_calls"] = 0
                    _session_costs["total_cost"] = 0.0
                    _session_costs["videos_generated"] = []
                    _session_costs["frames_skipped"] = 0
                    _session_costs["total_frames_generated"] = 0
                    print(f"[CINEMATIC] Session reset - budget limit: ${MAX_SESSION_COST:.2f}")
                except Exception as e:
                    print(f"[CINEMATIC] Could not reset session: {e}")
                
                engine.IMAGE_ENABLED = True
                engine.WORLD_IMAGE_ENABLED = True
                engine.VEO_MODE_ENABLED = True
                
                # Show Veo warning
                from veo_video_utils import MAX_SESSION_COST, ESTIMATED_COST_PER_VIDEO
                max_videos = int(MAX_SESSION_COST / ESTIMATED_COST_PER_VIDEO)
                await interaction.channel.send(embed=discord.Embed(
                    title=" HD MODE",
                    description=(
                        "**High-definition video generation**\n\n"
                        f"WARNING: This mode is expensive (~${ESTIMATED_COST_PER_VIDEO:.2f} per scene)\n"
                        "Each image is the last frame of a 4s video\n"
                        f"Budget limit: ${MAX_SESSION_COST:.2f} (~{max_videos} videos max)\n\n"
                        "*For beautiful visual consistency*"
                    ),
                    color=discord.Color.red()
                ))
                await asyncio.sleep(2)
                
                # === SHOW LOGO (Frame 0) ===
                logo_file = None
                for ext in [".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG"]:
                    test_path = ROOT / "static" / f"Logo{ext}"
                    if test_path.exists():
                        logo_file = test_path
                        break
                
                if logo_file and logo_file.exists():
                    try:
                        from PIL import Image
                        logo_img = Image.open(str(logo_file))
                        target_aspect = 16 / 9  # Veo uses 16:9
                        current_aspect = logo_img.width / logo_img.height
                        if abs(current_aspect - target_aspect) > 0.01:
                            if current_aspect > target_aspect:
                                new_width = int(logo_img.height * target_aspect)
                                left = (logo_img.width - new_width) // 2
                                logo_cropped = logo_img.crop((left, 0, left + new_width, logo_img.height))
                            else:
                                new_height = int(logo_img.width / target_aspect)
                                top = (logo_img.height - new_height) // 2
                                logo_cropped = logo_img.crop((0, top, logo_img.width, top + new_height))
                        else:
                            logo_cropped = logo_img
                        
                        normalized_logo_path = ROOT / "static" / "Logo_normalized_16x9.jpg"
                        logo_cropped.save(str(normalized_logo_path), "JPEG", quality=95)
                        await interaction.channel.send(file=discord.File(str(normalized_logo_path)))
                        _run_images.append(f"/static/Logo_normalized_16x9.jpg")
                        print(f"[CINEMATIC] Frame 0 (logo) recorded")
                    except Exception as e:
                        print(f"[CINEMATIC] Logo processing failed: {e}")
                
                await asyncio.sleep(1.5)
                
                # === DRAMATIC INTRO TEXT ===
                intro_embed = discord.Embed(
                    title=" RECOVERED VHS TAPE - 1993",
                    description=(
                        "Horizon Industries\n"
                        "Four Corners Facility\n\n"
                        "WARNING: WARNING: Disturbing Content"
                    ),
                    color=VHS_RED
                )
                await interaction.channel.send(embed=intro_embed)
                await asyncio.sleep(2)
                
                # PHASE 1: Generate first image (Veo will generate video + extract frame)
                loop = asyncio.get_running_loop()
                image_task = loop.run_in_executor(None, engine.generate_intro_image_fast)
                
                # Show Veo loading sequence
                vhs_msg = await interaction.channel.send(embed=discord.Embed(
                    description="`[00:00:00]`  HD MODE\n`GENERATING VIDEO...`",
                    color=discord.Color.red()
                ))
                
                vhs_sequence = [
                    (5, "`[00:00:05]` VEO 3.1\n`INITIALIZING...`"),
                    (10, "`[00:00:15]` VIDEO FRAMES\n`GENERATING...`"),
                    (10, "`[00:00:25]` INTERPOLATION\n`IN PROGRESS...`"),
                    (10, "`[00:00:35]` FINAL FRAME\n`EXTRACTING...`"),
                    (10, "`[00:00:45]` PLAYBACK\n`STARTING...`")
                ]
                
                for delay, message in vhs_sequence:
                    done, pending = await asyncio.wait([image_task], timeout=delay)
                    if done:
                        break
                    try:
                        await vhs_msg.edit(embed=discord.Embed(
                            description=message,
                            color=discord.Color.red()
                        ))
                    except Exception:
                        break
                
                intro_phase1 = await image_task
                
                try:
                    await vhs_msg.delete()
                except Exception:
                    pass
                
                # Send the intro text
                dispatch_text = intro_phase1.get("dispatch", "")
                dispatch_image_path = intro_phase1.get("dispatch_image")
                
                if dispatch_text:
                    await interaction.channel.send(embed=discord.Embed(
                        description=safe_embed_desc(dispatch_text.strip()),
                        color=VHS_RED
                    ))
                
                # Display the opening image
                if dispatch_image_path:
                    _run_images.append(dispatch_image_path)
                    print(f"[CINEMATIC] Frame 1 recorded - total frames: {len(_run_images)}")
                    
                    try:
                        # Use _attach to handle path correctly (absolute or relative)
                        discord_file, filename = _attach(dispatch_image_path, "Cinematic frame")
                        if discord_file:
                            await interaction.channel.send(file=discord_file)
                            print(f"[CINEMATIC] First frame displayed: {filename}")
                        else:
                            print(f"[CINEMATIC] Image not found: {dispatch_image_path}")
                    except Exception as e:
                        print(f"[CINEMATIC] Failed to send opening image: {e}")
                
                # Show choices generation
                choices_msg = await interaction.channel.send(embed=discord.Embed(
                    description=safe_embed_desc("⚙️ Generating choices..."),
                    color=CORNER_GREY
                ))
                
                # PHASE 2: Generate choices
                choices_task = loop.run_in_executor(
                    None, 
                    engine.generate_intro_choices_deferred,
                    dispatch_image_path,
                    intro_phase1["prologue"],
                    intro_phase1["vision_dispatch"]
                )
                intro_phase2 = await choices_task
                
                try:
                    await choices_msg.delete()
                except Exception:
                    pass
                
                # Send choices
                await interaction.channel.send("🟢 What will you do next?")
                view = ChoiceView(intro_phase2["choices"], owner_id=OWNER_ID)
                await send_choices(interaction.channel, intro_phase2["choices"], view, None)
                
                # Start countdown timer
                start_countdown_timer(
                    interaction.channel,
                    intro_phase2["choices"],
                    view,
                    intro_phase2.get("dispatch", intro_phase1.get("dispatch", "")),
                    intro_phase2.get("situation_report", ""),
                    intro_phase1.get("consequence_image")
                )
                
        play_view = View(timeout=None)
        play_view.add_item(AIProviderSelect())  # Row 0: AI Provider dropdown
        play_view.add_item(LoreCacheToggle())  # Row 1: Lore toggle
        play_view.add_item(PlayButton())  # Row 1: Play button
        play_view.add_item(PlayNoImagesButton())  # Row 1: Play without images
        play_view.add_item(PlayCinematicButton())  # Row 2: Cinematic mode (Veo)
        await channel.send(embed=rules_embed, view=play_view)

    # ───────── startup ─────────────────────────────────────────────────────────
    @bot.event
    async def on_ready():
        global auto_play_enabled, auto_advance_task, countdown_task, custom_action_available, custom_action_turn_counter
        
        print(f"[BOT] {bot.user} is ready!")
        
        # Reset auto-play state on bot startup
        auto_play_enabled = False
        if auto_advance_task and not auto_advance_task.done():
            auto_advance_task.cancel()
            print("[STARTUP] Cancelled existing auto-play task")
        auto_advance_task = None
        
        # Reset countdown state on bot startup
        if countdown_task and not countdown_task.done():
            countdown_task.cancel()
            print("[STARTUP] Cancelled existing countdown task")
        countdown_task = None
        
        # Reset custom action cooldown on bot startup
        custom_action_available = True
        custom_action_turn_counter = 0
        print("[STARTUP] Custom action available")
        
        # Sync slash commands
        try:
            synced = await bot.tree.sync()
            print(f"[BOT] Synced {len(synced)} slash command(s)")
        except Exception as e:
            print(f"[BOT] Failed to sync commands: {e}")
        
        # Send intro to channel
        print(f"[BOT] Attempting to get channel {CHAN}...", flush=True)
        channel = bot.get_channel(CHAN)
        if channel is not None:
            print(f"[BOT] Channel found: {channel.name}", flush=True)
            if not RESUME_MODE:
                # Send intro tutorial with Play buttons
                try:
                    print("[BOT] Calling send_intro_tutorial...", flush=True)
                    await send_intro_tutorial(channel)
                    print(f"[BOT] Sent intro to channel {CHAN}", flush=True)
                except Exception as e:
                    print(f"[BOT ERROR] Failed to send intro: {e}", flush=True)
                    import traceback
                    traceback.print_exc()
            else:
                await channel.send("🟢 Resumed from previous state.")
        else:
            print(f"[BOT ERROR] Channel {CHAN} not found. Bot may not have access to this channel.", flush=True)
        # Set owner ID
        try:
            global OWNER_ID
            print("[BOT] Getting application info...", flush=True)
            app_info = await bot.application_info()
            OWNER_ID = app_info.owner.id
            print(f"[BOT] Owner ID: {OWNER_ID}", flush=True)
        except Exception as e:
            print(f"[BOT ERROR] Failed to get owner ID: {e}", flush=True)
            import traceback
            traceback.print_exc()
        
        # Mark bot as running
        global running
        running = True
        print("[BOT] Bot fully initialized and ready!", flush=True)

    # --- Discord bot startup temporarily disabled for web-only mode ---
    # To re-enable Discord, uncomment the following line:
    # bot.run(TOKEN)

    MAX_FIELD_LOG_WORDS = 20

    def truncate_log(line):
        words = line.split()
        return ' '.join(words[:MAX_FIELD_LOG_WORDS]) + ('...' if len(words) > MAX_FIELD_LOG_WORDS else '')

    # --- Helper to send choices and always delete previous ---
    async def countdown_timer_task(channel, choices, view, dispatch, situation):
        """Run the countdown timer and handle timeout"""
        global countdown_message, countdown_task, current_choices, current_view, auto_advance_task, auto_play_enabled
        
        import time
        start_time = time.time()
        
        try:
            while True:
                elapsed = time.time() - start_time
                remaining = max(0, COUNTDOWN_DURATION - elapsed)
                
                if remaining <= 0:
                    # TIME'S UP - Generate and execute penalty
                    print("[COUNTDOWN] Time's up! Generating penalty...")
                    
                    # IMMEDIATELY disable all buttons and push to Discord UI
                    for item in view.children:
                        item.disabled = True
                    
                    # Update Discord UI IMMEDIATELY (before penalty generation)
                    try:
                        if view.last_choices_message:
                            await view.last_choices_message.edit(view=view)
                            print("[COUNTDOWN] Buttons disabled immediately")
                    except Exception as e:
                        print(f"[COUNTDOWN] Failed to disable buttons: {e}")
                    
                    # Show "generating penalty..." message
                    if countdown_message:
                        try:
                            await countdown_message.edit(
                                content=None,
                                embed=discord.Embed(
                                    description=" **Time's up!** Generating consequence...",
                                    color=VHS_RED
                                )
                            )
                        except Exception:
                            pass
                    
                    # NOW generate penalty (takes time, but buttons are already disabled)
                    penalty_choice = await generate_timeout_penalty(dispatch, situation, current_image_path)
                    
                    # Update with final timeout message
                    timeout_embed = discord.Embed(
                        description=f"**Hesitation has consequences.**\n\n{penalty_choice}",
                        color=VHS_RED
                    )
                    
                    try:
                        if countdown_message:
                            await countdown_message.edit(content=None, embed=timeout_embed)
                    except Exception as e:
                        print(f"[COUNTDOWN] Failed to update timeout message: {e}")
                    
                    # Process penalty as a choice
                    await asyncio.sleep(2)  # Let them see the penalty
                    
                    # === FATE ROLL for timeout penalty ===
                    # 1. Compute fate instantly (before image generation starts)
                    fate = compute_fate()
                    print(f"[COUNTDOWN] Fate rolled: {fate}")
                    
                    # 2. Start image generation in background (don't block)
                    loop = asyncio.get_running_loop()
                    # CRITICAL: Mark as timeout penalty to prevent teleportation
                    phase1_task = loop.run_in_executor(None, lambda: engine.advance_turn_image_fast(penalty_choice, fate, is_timeout_penalty=True))
                    
                    # 3. NOW show the fate animation WHILE image generates
                    await asyncio.sleep(0.1)  # Tiny pause for smoothness
                    await animate_fate_roll(channel, fate)
                    
                    # 4. Wait for image generation to complete
                    phase1_result = await phase1_task
                    
                    # Display dispatch and image
                    dispatch_text = phase1_result.get("dispatch", "")
                    if dispatch_text:
                        # Add movement type indicator
                        movement_indicator = get_movement_indicator()
                        full_text = dispatch_text.strip()
                        if movement_indicator:
                            full_text = f"{movement_indicator}\n\n{full_text}"
                        
                        await channel.send(embed=discord.Embed(
                            description=safe_embed_desc(full_text),
                            color=VHS_RED
                        ))
                    
                    consequence_image_path = phase1_result.get("consequence_image")
                    
                    # Track image for VHS tape
                    global _run_images
                    print(f"[TAPE RECORDING COUNTDOWN] img = {consequence_image_path}, frames = {len(_run_images)}")
                    if consequence_image_path:
                        _run_images.append(consequence_image_path)
                        print(f"[TAPE] Frame {len(_run_images)} recorded: {consequence_image_path}")
                    else:
                        print(f"[TAPE] ERROR: NO IMAGE from countdown penalty")
                    
                    if consequence_image_path:
                        # Handle absolute paths (session images)
                        if Path(consequence_image_path).is_absolute():
                            full_path = Path(consequence_image_path)
                        else:
                            full_path = ROOT / consequence_image_path.lstrip("/")
                        if full_path.exists():
                            try:
                                await channel.send(file=discord.File(str(full_path)))
                            except Exception as e:
                                print(f"[COUNTDOWN] Image send failed: {e}")
                    
                    # CHECK FOR DEATH after timeout penalty
                    # Note: get_state() already reloads from disk via API client
                    current_state = engine.get_state()
                    player_alive = current_state.get("player_state", {}).get("alive", True)
                    
                    if not player_alive:
                        print("[COUNTDOWN DEATH] Player died from timeout penalty!")
                        await channel.send(embed=discord.Embed(
                            title="💀 YOU DIED",
                            description="The camera stops recording.",
                            color=VHS_RED
                        ))
                        await asyncio.sleep(1)
                        
                        # Show VHS ejecting sequence WHILE tape is being created
                        eject_msg = await channel.send(embed=discord.Embed(
                            description="`[STOP]` ⏏️ EJECTING TAPE...",
                            color=VHS_RED
                        ))
                        
                        # Start tape creation in background
                        loop = asyncio.get_running_loop()
                        tape_task = loop.run_in_executor(None, _create_death_replay_tape_with_lock)
                        
                        # VHS eject animation (plays while GIF generates)
                        eject_sequence = [
                            (0.8, "`[STOP]` ⏏️\n`REWINDING...`"),
                            (0.8, "`[STOP]` ⏏️\n`[███░░░░░░░]`"),
                            (0.8, "`[STOP]` ⏏️\n`[██████░░░░]`"),
                            (0.8, "`[STOP]` ⏏️\n`[█████████░]`"),
                            (0.8, "`[STOP]` ⏏️\n`FINALIZING...`"),
                            (1.0, "`[STOP]` ⏏️\n`TAPE READY`")
                        ]
                        
                        for delay, message in eject_sequence:
                            done, pending = await asyncio.wait([tape_task], timeout=delay)
                            if done:
                                break
                            try:
                                await eject_msg.edit(embed=discord.Embed(
                                    description=message,
                                    color=VHS_RED
                                ))
                            except Exception:
                                break
                        
                        # Wait for completion
                        tape_path, error_msg = await tape_task
                        
                        # Clean up animation
                        try:
                            await eject_msg.delete()
                        except Exception:
                            pass
                        
                        # Send tape or error
                        if tape_path:
                            await channel.send(embed=discord.Embed(
                                title=" VHS TAPE RECOVERED",
                                description="Camera footage retrieved from scene.",
                                color=CORNER_GREY
                            ))
                            try:
                                await channel.send(file=discord.File(tape_path))
                                print("[COUNTDOWN DEATH]  Tape uploaded - waiting for player to download...")
                            except Exception as e:
                                print(f"[COUNTDOWN DEATH] Failed to send tape: {e}")
                                await channel.send(embed=discord.Embed(
                                    title="WARNING: Tape Upload Failed",
                                    description=f"Tape created but upload failed: {e}",
                                    color=VHS_RED
                                ))
                        else:
                            await channel.send(embed=discord.Embed(
                                title="WARNING: No Tape Created",
                                description=f"**Reason:** {error_msg}",
                                color=VHS_RED
                            ))
                        
                        # Create Play Again button (independent of disabled view)
                        manual_restart_done = asyncio.Event()  # Flag to prevent double restart
                        
                        class PlayAgainButton(Button):
                            def __init__(self):
                                super().__init__(label="️ Play Again", style=discord.ButtonStyle.success)
                            
                            async def callback(self, button_interaction: discord.Interaction):
                                # Authorization check
                                if not check_authorization(button_interaction, OWNER_ID):
                                    await button_interaction.response.send_message(
                                        "🔒 Only the game owner can restart.",
                                        ephemeral=True
                                    )
                                    return
                                
                                global auto_advance_task, countdown_task, auto_play_enabled
                                print("[COUNTDOWN DEATH] Play Again button pressed - manual restart")
                                
                                # Mark that manual restart is happening
                                manual_restart_done.set()
                                
                                try:
                                    await button_interaction.response.defer()
                                except Exception:
                                    pass
                                
                                # Cancel all running tasks
                                if auto_advance_task and not auto_advance_task.done():
                                    auto_advance_task.cancel()
                                if countdown_task and not countdown_task.done():
                                    countdown_task.cancel()
                                auto_play_enabled = False
                                
                                # Reset game
                                loop = asyncio.get_running_loop()
                                await loop.run_in_executor(None, ChoiceButton._do_reset_static)
                                
                                # Show intro
                                await send_intro_tutorial(button_interaction.channel)
                        
                        # Show Play Again button immediately
                        play_again_view = View(timeout=None)
                        play_again_view.add_item(PlayAgainButton())
                        await channel.send(
                            embed=discord.Embed(
                                description="💾 **Save the tape!** Press Play Again to restart.",
                                color=CORNER_GREY
                            ),
                            view=play_again_view
                        )
                        
                        # Wait 30s for manual restart (check every second if button was clicked)
                        print("[COUNTDOWN DEATH] Waiting 30s for manual restart or auto-restart...")
                        for _ in range(30):
                            if manual_restart_done.is_set():
                                print("[COUNTDOWN DEATH] Manual restart detected - skipping auto-restart")
                                break  # Exit countdown loop, player restarted manually
                            await asyncio.sleep(1)
                        
                        # Only auto-restart if player didn't click button
                        if not manual_restart_done.is_set():
                            print("[COUNTDOWN DEATH] Auto-restarting game...")
                        
                        # Cancel all running tasks
                        if auto_advance_task and not auto_advance_task.done():
                            auto_advance_task.cancel()
                            print("[COUNTDOWN DEATH] Cancelled auto-play task")
                        if countdown_task and not countdown_task.done():
                            countdown_task.cancel()
                            print("[COUNTDOWN DEATH] Cancelled countdown task")
                        auto_play_enabled = False
                        
                        # Reset game
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, ChoiceButton._do_reset_static)
                        await send_intro_tutorial(channel)
                        
                        break  # Exit countdown loop
                    
                    # Show situation report during generation
                    situation_report = phase1_result.get("situation_report", "")
                    if situation_report:
                        await channel.send(embed=discord.Embed(
                            description=f"📍 {situation_report}",
                            color=VHS_RED
                        ))
                    
                    # Phase 2: Generate choices
                    choices_msg = await channel.send(embed=discord.Embed(
                        description=safe_embed_desc("⚙️ Generating choices..."),
                        color=CORNER_GREY
                    ))
                    
                    phase2_task = loop.run_in_executor(
                        None,
                        engine.advance_turn_choices_deferred,
                        consequence_image_path,
                        dispatch_text,
                        phase1_result.get("vision_dispatch", ""),
                        penalty_choice
                    )
                    phase2_result = await phase2_task
                    
                    try:
                        await choices_msg.delete()
                    except:
                        pass
                    
                    # Send new choices
                    new_choices = phase2_result.get("choices", [])
                    if new_choices:
                        await channel.send("🟢 What will you do next?")
                        new_view = ChoiceView(new_choices, owner_id=OWNER_ID)
                        msg = await channel.send(content=" ", view=new_view)
                        new_view.last_choices_message = msg
                        
                        # Start new countdown with new choices
                        start_countdown_timer(
                            channel, 
                            new_choices, 
                            new_view,
                            phase1_result.get("dispatch", ""),
                            phase2_result.get("situation_report", ""),
                            phase1_result.get("consequence_image")
                        )
                    
                    break
                
                # Update countdown display
                bars = "█" * int((remaining / COUNTDOWN_DURATION) * 10)
                empty = "░" * (10 - len(bars))
                
                if remaining > 30:
                    emoji = ""
                elif remaining > 10:
                    emoji = "WARNING:"
                else:
                    emoji = "🚨"
                
                # Get player health

                current_state = engine.get_state()
                alive = current_state.get("player_state", {}).get("alive", True)
                
                # Simple health status
                health_status = "Alive" if alive else "Dead"
                
                countdown_text = f"{emoji} **{int(remaining)}s** [{bars}{empty}]  |  Health: **{health_status}**"
                
                if countdown_message:
                    try:
                        await countdown_message.edit(content=countdown_text)
                    except Exception as e:
                        print(f"[COUNTDOWN] Failed to update countdown: {e}")
                
                await asyncio.sleep(COUNTDOWN_UPDATE_INTERVAL)
                
        except asyncio.CancelledError:
            print("[COUNTDOWN] Timer cancelled by player choice")
            # Clean up countdown message
            if countdown_message:
                try:
                    await countdown_message.delete()
                except:
                    pass
    
    def start_countdown_timer(channel, choices, view, dispatch, situation, current_image=None):
        """Start the countdown timer"""
        global countdown_task, countdown_message, current_choices, current_view, current_dispatch, current_situation, current_image_path, auto_play_enabled
        
        if not COUNTDOWN_ENABLED:
            return
        
        # Don't start countdown if auto-play is enabled (auto-play handles timing)
        if auto_play_enabled:
            print("[COUNTDOWN] Skipping - auto-play is handling timing")
            return
        
        # Cancel any existing countdown
        if countdown_task and not countdown_task.done():
            countdown_task.cancel()
        
        # Store context for penalty generation
        current_choices = choices
        current_view = view
        current_dispatch = dispatch
        current_situation = situation
        current_image_path = current_image
        
        # Start new countdown
        countdown_task = asyncio.create_task(countdown_timer_wrapper(channel, choices, view, dispatch, situation))
        print(f"[COUNTDOWN] Started {COUNTDOWN_DURATION}s countdown")
    
    async def countdown_timer_wrapper(channel, choices, view, dispatch, situation):
        """Wrapper to create countdown message before running timer"""
        global countdown_message
        
        # Create countdown message
        countdown_message = await channel.send(content=f" **{COUNTDOWN_DURATION}s** [██████████]")
        
        # Run the actual countdown
        await countdown_timer_task(channel, choices, view, dispatch, situation)
    
    async def send_choices(channel, choices, view, previous_message=None, file=None):
        # Delete previous choices message if it exists
        if previous_message:
            try:
                await previous_message.delete()
            except Exception as e:
                print(f"[LOG] Could not delete previous choices message: {e}")
        # Send new choices message
        if file:
            msg = await channel.send(file=file, view=view)
        else:
            msg = await channel.send(content=" ", view=view)
        view.last_choices_message = msg
        return msg

    # --- Auto-play mode ---
    # Auto-play automatically picks random choices every 45 seconds
    auto_play_enabled = False  # Track if auto-play mode is active
    auto_advance_task = None
    AUTO_PLAY_DELAY = 45  # seconds between auto choices
    
    # --- Quality mode ---
    # Quality mode toggles between Gemini Flash (fast) and Gemini Pro (high quality)
    # NOTE: This is separate from "HD Mode" (Veo cinematic video generation)
    quality_mode_enabled = True  # Default to HQ mode (Pro) for quality
    current_choices = []  # Track current available choices
    current_view = None  # Track current view for auto-advance
    
    # --- Countdown Timer ---
    COUNTDOWN_ENABLED = True  # Enable/disable countdown timer
    COUNTDOWN_DURATION = 30  # seconds per choice
    COUNTDOWN_UPDATE_INTERVAL = 1  # update display every 1 second (smooth countdown)
    countdown_task = None  # Track active countdown
    countdown_message = None  # Message showing countdown
    current_dispatch = ""  # For penalty generation
    current_situation = ""  # For penalty generation
    
    # --- Custom Action Cooldown ---
    CUSTOM_ACTION_COOLDOWN = 0  # DEMO MODE: Unlimited Free Will (set to 3 for normal cooldown)
    custom_action_turn_counter = 0  # Tracks turns since last custom action
    custom_action_available = True  # Whether custom action can be used

    async def auto_advance_turn(channel):
        """Automatically pick a random choice and advance the turn (auto-play mode)."""
        global auto_advance_task, countdown_task, auto_play_enabled
        
        if not auto_play_enabled:
            return
            
        if not current_choices:
            print("[AUTO-PLAY] No choices available, skipping auto-advance")
            return
            
        # Filter out placeholder choices (same as ChoiceView)
        valid_choices = [c for c in current_choices if c.strip() not in ["—", "–", "-", ""]]
        
        if not valid_choices:
            print("[AUTO-PLAY] No valid choices available (all placeholders), skipping")
            return
            
        # Pick a random valid choice
        chosen = random.choice(valid_choices)
        print(f"[AUTO-PLAY] Auto-selecting: {chosen}")
        
        # Cancel countdown timer when auto-play makes a choice
        if countdown_task and not countdown_task.done():
            countdown_task.cancel()
            print("[AUTO-PLAY] Cancelled countdown timer")
        
        # Send notification that auto-play is happening
        embed = discord.Embed(
            title="🤖 Auto-Play",
            description=f"Automatically selecting: **{chosen}**",
            color=CORNER_TEAL
        )
        await channel.send(embed=embed)
        
        # Disable all buttons in the current view
        if current_view:
            for item in current_view.children:
                item.disabled = True
            try:
                if hasattr(current_view, 'last_choices_message') and current_view.last_choices_message:
                    await current_view.last_choices_message.edit(view=current_view)
            except Exception as e:
                print(f"[AUTO-ADVANCE] Could not disable buttons: {e}")
        
        # Process the choice (same two-phase logic as ChoiceButton callback)
        loop = asyncio.get_running_loop()
        
        # Compute fate INSTANTLY (no wait)
        fate = compute_fate()
        print(f"[FATE AUTO] Computed: {fate}")
        
        # PHASE 1: Generate image fast with fate modifier (start immediately)
        phase1_task = loop.run_in_executor(None, engine.advance_turn_image_fast, chosen, fate)
        
        # WHILE image is generating, show fate animation (fills wait time!)
        await asyncio.sleep(0.1)  # Tiny breath, then action!
        await animate_fate_roll(channel, fate)
        
        # Wait for Phase 1 to complete
        phase1_result = await phase1_task
        
        # Display dispatch and image immediately
        dispatch_text = phase1_result.get("dispatch", "")
        if dispatch_text:
            # Add movement type indicator
            movement_indicator = get_movement_indicator()
            full_text = dispatch_text.strip()
            if movement_indicator:
                full_text = f"{movement_indicator}\n\n{full_text}"
            
            await channel.send(embed=discord.Embed(
                description=safe_embed_desc(full_text),
                color=VHS_RED
            ))
        
        consequence_image_path = phase1_result.get("consequence_image")
        
        # Track image for VHS tape
        global _run_images
        print(f"[TAPE RECORDING AUTO] img = {consequence_image_path}, frames = {len(_run_images)}")
        if consequence_image_path:
            _run_images.append(consequence_image_path)
            print(f"[TAPE] Frame {len(_run_images)} recorded: {consequence_image_path}")
        else:
            print(f"[TAPE] ERROR: NO IMAGE from auto-advance")
        
        if consequence_image_path:
            # Handle absolute paths (session images)
            if Path(consequence_image_path).is_absolute():
                full_path = Path(consequence_image_path)
            else:
                full_path = ROOT / consequence_image_path.lstrip("/")
            if full_path.exists():
                try:
                    await channel.send(file=discord.File(str(full_path)))
                    print(f"[AUTO-PLAY] Image displayed immediately!")
                except Exception as e:
                    print(f"[AUTO-PLAY] Image send failed: {e}")
        
            # CHECK FOR DEATH - Read FRESH state
            # Note: get_state() already reloads from disk via API client
            current_state = engine.get_state()
        player_alive = current_state.get("player_state", {}).get("alive", True)
        player_health = current_state.get("player_state", {}).get("health", 100)
        
        print(f"[DEATH CHECK AUTO] alive={player_alive}, health={player_health}")
        
        if not player_alive:
            print("[AUTO-PLAY] Player died!")
            await channel.send(embed=discord.Embed(
                title="💀 YOU DIED",
                description="The camera stops recording.",
                color=VHS_RED
            ))
            await asyncio.sleep(1)
            
            # Show VHS ejecting sequence WHILE tape is being created
            eject_msg = await channel.send(embed=discord.Embed(
                description="`[STOP]` ⏏️ EJECTING TAPE...",
                color=VHS_RED
            ))
            
            # Start tape creation in background
            loop = asyncio.get_running_loop()
            tape_task = loop.run_in_executor(None, _create_death_replay_tape)
            
            # VHS eject animation (plays while GIF generates)
            eject_sequence = [
                (0.8, "`[STOP]` ⏏️\n`REWINDING...`"),
                (0.8, "`[STOP]` ⏏️\n`[███░░░░░░░]`"),
                (0.8, "`[STOP]` ⏏️\n`[██████░░░░]`"),
                (0.8, "`[STOP]` ⏏️\n`[█████████░]`"),
                (0.8, "`[STOP]` ⏏️\n`FINALIZING...`"),
                (1.0, "`[STOP]` ⏏️\n`TAPE READY`")
            ]
            
            for delay, message in eject_sequence:
                done, pending = await asyncio.wait([tape_task], timeout=delay)
                if done:
                    break
                try:
                    await eject_msg.edit(embed=discord.Embed(
                        description=message,
                        color=VHS_RED
                    ))
                except Exception:
                    break
            
            # Wait for completion
            tape_path, error_msg = await tape_task
            
            # Clean up animation
            try:
                await eject_msg.delete()
            except Exception:
                pass
            
            # Send tape or error
            if tape_path:
                await channel.send(embed=discord.Embed(
                    title=" VHS TAPE RECOVERED",
                    description="Camera footage retrieved from scene.",
                    color=CORNER_GREY
                ))
                try:
                    await channel.send(file=discord.File(tape_path))
                    print("[AUTO-PLAY DEATH]  Tape uploaded - waiting for player to download...")
                except Exception as e:
                    print(f"[AUTO-PLAY] Failed to send tape: {e}")
                    await channel.send(embed=discord.Embed(
                        title="WARNING: Tape Upload Failed",
                        description=f"Tape created but upload failed: {e}",
                        color=VHS_RED
                    ))
            else:
                await channel.send(embed=discord.Embed(
                    title="WARNING: No Tape Created",
                    description=f"**Reason:** {error_msg}",
                    color=VHS_RED
                ))
            
            # Create Play Again button (independent of disabled view)
            manual_restart_done = asyncio.Event()  # Flag to prevent double restart
            
            class PlayAgainButton(Button):
                def __init__(self):
                    super().__init__(label="️ Play Again", style=discord.ButtonStyle.success)
                
                async def callback(self, button_interaction: discord.Interaction):
                    # Authorization check
                    if not check_authorization(button_interaction, OWNER_ID):
                        await button_interaction.response.send_message(
                            "🔒 Only the game owner can restart.",
                            ephemeral=True
                        )
                        return
                    
                    global auto_advance_task, countdown_task, auto_play_enabled
                    print("[AUTO-PLAY DEATH] Play Again button pressed - manual restart")
                    
                    # Mark that manual restart is happening
                    manual_restart_done.set()
                    
                    try:
                        await button_interaction.response.defer()
                    except Exception:
                        pass
                    
                    # Cancel all running tasks
                    if auto_advance_task and not auto_advance_task.done():
                        auto_advance_task.cancel()
                    if countdown_task and not countdown_task.done():
                        countdown_task.cancel()
                    auto_play_enabled = False
                    
                    # Reset game
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, ChoiceButton._do_reset_static)
                    
                    # Show intro
                    await send_intro_tutorial(button_interaction.channel)
            
            # Show Play Again button immediately
            play_again_view = View(timeout=None)
            play_again_view.add_item(PlayAgainButton())
            await channel.send(
                embed=discord.Embed(
                    description="💾 **Save the tape!** Press Play Again to restart.",
                    color=CORNER_GREY
                ),
                view=play_again_view
            )
            
            # Wait 30s for manual restart (check every second if button was clicked)
            print("[AUTO-PLAY DEATH] Waiting 30s for manual restart or auto-restart...")
            for _ in range(30):
                if manual_restart_done.is_set():
                    print("[AUTO-PLAY DEATH] Manual restart detected - skipping auto-restart")
                    return  # Player clicked button, don't auto-restart
                await asyncio.sleep(1)
            
            # Cancel all running tasks
            if auto_advance_task and not auto_advance_task.done():
                auto_advance_task.cancel()
                print("[AUTO-PLAY DEATH] Cancelled auto-play task")
            if countdown_task and not countdown_task.done():
                countdown_task.cancel()
                print("[AUTO-PLAY DEATH] Cancelled countdown task")
            auto_play_enabled = False
            
            await loop.run_in_executor(None, ChoiceButton._do_reset_static)
            await send_intro_tutorial(channel)
            return
        
        # Show "Generating choices..." while Phase 2 runs
        choices_msg = await channel.send(embed=discord.Embed(
            description=safe_embed_desc("⚙️ Generating choices..."),
            color=CORNER_GREY
        ))
        
        # PHASE 2: Generate choices in background
        phase2_task = loop.run_in_executor(
            None,
            engine.advance_turn_choices_deferred,
            consequence_image_path,
            dispatch_text,
            phase1_result.get("vision_dispatch", ""),
            chosen
        )
        phase2_result = await phase2_task
        
        # Delete "Generating choices..." message
        try:
            await choices_msg.delete()
        except Exception:
            pass
        
        # Send new choices
        new_choices = phase2_result.get("choices", [])
        if new_choices:
            # Send world evolution text
            world_prompt = engine.state.get("world_prompt", "")
            if world_prompt:
                await channel.send(embed=discord.Embed(
                    description=f"📍 {world_prompt}",
                    color=VHS_RED
                ))
            
            await channel.send("🟢 What will you do next?")
            view = ChoiceView(new_choices, owner_id=OWNER_ID)
            msg = await channel.send(content=" ", view=view)
            view.last_choices_message = msg
            # Restart the auto-play timer with new choices
            start_auto_advance_timer(channel, new_choices, view)

    def start_auto_advance_timer(channel, choices, view):
        """Start or restart the auto-play timer (only if auto-play is enabled)."""
        global auto_advance_task, current_choices, current_view, auto_play_enabled
        
        if not auto_play_enabled:
            return  # Don't start timer if auto-play is not enabled
            
        # Cancel existing timer
        if auto_advance_task and not auto_advance_task.done():
            auto_advance_task.cancel()
            print("[AUTO-PLAY] Cancelled existing timer, starting new one")
        
        # Store current choices and view for auto-advance
        current_choices = choices
        current_view = view
        # Start new timer
        auto_advance_task = asyncio.create_task(auto_advance_timer_task(channel))
        print(f"[AUTO-PLAY] Started new timer ({AUTO_PLAY_DELAY}s delay)")

    async def auto_advance_timer_task(channel):
        """Wait for delay, then auto-advance (auto-play mode)."""
        try:
            await asyncio.sleep(AUTO_PLAY_DELAY)
            await auto_advance_turn(channel)
        except asyncio.CancelledError:
            pass

    # --- Patch all button callbacks to reset the inactivity timer ---
    # In each button callback, add: start_inactivity_timer(interaction.channel)
    # For ChoiceButton:
    #   Add start_inactivity_timer(interaction.channel) at the start of the callback
    # For RegenerateChoicesButton, MapButton, RestartButton, PlayButton: same

    # --- Helper for alt text (for web UI, but placeholder for Discord) ---
    def get_alt_text(dispatch, vision):
        """Generate alt text for images based on dispatch and vision."""
        if vision:
            return vision
        return dispatch

    # ───────── AI Provider Management Commands ─────────────────────────────────
    
    @bot.tree.command(name="ai_status", description="View current AI model configuration")
    async def ai_status_command(interaction: discord.Interaction):
        """Show current AI provider settings."""
        status = ai_provider_manager.get_status()
        embed = discord.Embed(
            title="🤖 AI Configuration",
            description=status,
            color=CORNER_TEAL
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @bot.tree.command(name="ai_switch", description="Switch AI provider preset")
    async def ai_switch_command(
        interaction: discord.Interaction,
        preset: str
    ):
        """Switch to a different AI provider preset."""
        # Get available presets
        presets = ai_provider_manager.get_available_presets()
        
        if preset not in presets:
            preset_list = "\n".join([f"• `{name}`" for name in presets.keys()])
            await interaction.response.send_message(
                f"ERROR: Unknown preset: `{preset}`\n\n**Available presets:**\n{preset_list}",
                ephemeral=True
            )
            return
        
        # Switch preset
        success = ai_provider_manager.set_preset(preset)
        
        if success:
            status = ai_provider_manager.get_status()
            embed = discord.Embed(
                title=f" Switched to `{preset}`",
                description=status,
                color=CORNER_TEAL
            )
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(
                f"ERROR: Failed to switch to preset: `{preset}`",
                ephemeral=True
            )
    
    @bot.tree.command(name="ai_presets", description="List available AI presets")
    async def ai_presets_command(interaction: discord.Interaction):
        """List all available AI provider presets."""
        presets = ai_provider_manager.get_available_presets()
        
        description = ""
        for name, config in presets.items():
            description += f"**`{name}`**\n"
            description += f"  Text: `{config['text_provider']}/{config['text_model']}`\n"
            description += f"  Image: `{config['image_provider']}/{config['image_model']}`\n\n"
        
        embed = discord.Embed(
            title="🎛️ Available AI Presets",
            description=description,
            color=CORNER_GREY
        )
        embed.set_footer(text="Use /ai_switch <preset> to switch")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @bot.tree.command(name="lore_status", description="View lore cache status")
    async def lore_status_command(interaction: discord.Interaction):
        """Show current lore cache status."""
        status_msg = lore_cache_manager.format_status_message()
        embed = discord.Embed(
            description=status_msg,
            color=CORNER_TEAL
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @bot.tree.command(name="lore_refresh", description="Force refresh lore cache")
    async def lore_refresh_command(interaction: discord.Interaction):
        """Manually refresh the lore cache."""
        await interaction.response.defer(ephemeral=True)
        
        success = lore_cache_manager.refresh_cache()
        
        if success:
            status_msg = lore_cache_manager.format_status_message()
            embed = discord.Embed(
                title=" Lore Cache Refreshed",
                description=status_msg,
                color=CORNER_TEAL
            )
        else:
            embed = discord.Embed(
                title="ERROR: Refresh Failed",
                description="Could not refresh lore cache. Check logs for details.",
                color=VHS_RED
            )
        
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ───────── bot lifecycle ────────────────────────────────────────────────────
    # DUPLICATE REMOVED - This was overwriting the first on_ready handler

if __name__ == "__main__":
    print("[MAIN] Entering main block", flush=True)
    import threading
    import time


    if DISCORD_ENABLED:
        print("[MAIN] Discord enabled - starting bot", flush=True)
        
        # Check if running on Render (needs health check endpoint)
        if os.getenv("RENDER"):
            print("[RENDER] Detected Render environment - starting health check server", flush=True)
            from flask import Flask
            health_app = Flask(__name__)
            
            @health_app.route("/")
            def health():
                return {"status": "ok", "service": "discord_bot"}
            
            @health_app.route("/health")
            def health_check():
                return {"status": "healthy", "bot": "running"}
            
            # Start Flask in background thread
            def run_health_server():
                port = int(os.getenv("PORT", 10000))
                print(f"[RENDER] Health check server starting on port {port}", flush=True)
                health_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
            
            health_thread = threading.Thread(target=run_health_server, daemon=True)
            health_thread.start()
            print("[RENDER] Health check server started in background thread", flush=True)
        
        print(f"[MAIN] Starting bot.run() with TOKEN={'SET' if TOKEN else 'MISSING'}", flush=True)
        try:
            bot.run(TOKEN)
        except Exception as e:
            print(f"[MAIN ERROR] Bot crashed: {e}", flush=True)
            import traceback
            traceback.print_exc()
    else:
        print("WARNING: Discord disabled. Running in local web-only mode.")

        # Start the simulation loop once
        def local_loop():
            print("Starting local simulation tick...")
            while True:
                try:
                    engine.begin_tick()
                    time.sleep(30)
                except Exception as e:
                    print("[Loop error]", e)
                    time.sleep(10)

        threading.Thread(target=local_loop, daemon=True).start()

        # Flask server is already launched inside engine.py
        while True:
            time.sleep(60)
