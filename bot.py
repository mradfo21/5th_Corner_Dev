"""
bot.py – single definitive version
• Attaches world‑image (snapshot) and dispatch‑image so every client sees them
• Works no matter where the bot is running (no PUBLIC_HOST / IMAGE_PORT)
• Updated: Extended death tape + FIXED Gemini API key loading from env vars (CRITICAL)
"""

import os
import json
from pathlib import Path
import random

DISCORD_ENABLED = os.getenv("DISCORD_ENABLED", "1") == "1"
RESUME_MODE = os.getenv("RESUME_MODE", "0") == "1"

if DISCORD_ENABLED:
    import asyncio, logging, random
    from typing import Optional, Tuple

    import discord
    from discord.ext import commands
    from discord.ui import View, Button, Modal, TextInput

    import engine
    from evolve_prompt_file import generate_interim_messages_on_demand

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
        print("[STARTUP] Resetting game state (fresh simulation)...")
        engine.reset_state()
        print("[STARTUP] Game state cleared. Starting fresh.")

    EMOJI = ["1️⃣", "2️⃣", "3️⃣"]

    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", conf.get("DISCORD_TOKEN"))
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", conf.get("OPENAI_API_KEY"))

    # ───────── discord init ─────────────────────────────────────────────────────
    logging.basicConfig(level=logging.INFO, format="BOT | %(message)s")
    intents = discord.Intents.default(); intents.message_content = True
    bot     = commands.Bot(command_prefix="/", intents=intents)

    running = False
    OWNER_ID = None

    # ───────── VHS tape recording (death replay GIFs) ──────────────────────────
    TAPES_DIR = ROOT / "tapes"
    TAPES_DIR.mkdir(exist_ok=True)
    _run_images = []  # Track all images from current run for VHS tape
    
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
        await asyncio.sleep(1.5)  # Display result
        await msg.delete()
    
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
        print(f"[TAPE] Checking frames... _run_images contains {len(_run_images) if _run_images else 0} entries")
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
                full_path = ROOT / img_path.lstrip("/")
                print(f"[TAPE] Loading frame {idx+1}/{len(_run_images)}: {full_path}")
                if full_path.exists():
                    try:
                        img = Image.open(str(full_path))
                        frame_sizes.append(img.size)
                        frames.append((img, img.size, idx))
                        print(f"[TAPE] ✅ Frame {idx+1} loaded: {img.size}")
                    except Exception as e:
                        print(f"[TAPE] ❌ Failed to open frame {idx+1}: {e}")
                        missing_files.append(f"{img_path} (failed to open)")
                else:
                    print(f"[TAPE] ❌ Frame {idx+1} not found: {full_path}")
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
                    # Resize to exactly TARGET_SIZE, stretching if needed to fill frame
                    img_resized = img_rgb.resize(TARGET_SIZE, Image.Resampling.LANCZOS)
                    normalized_frames.append(img_resized)
                    print(f"[TAPE] Frame {idx+1}: {original_size} → {TARGET_SIZE} (resized to match)")
                else:
                    normalized_frames.append(img_rgb)
                    print(f"[TAPE] Frame {idx+1}: {original_size} (already correct size)")
            
            frames = normalized_frames
            print(f"[TAPE] ✅ All {len(frames)} frames normalized to {TARGET_SIZE[0]}x{TARGET_SIZE[1]}")
            
            # Create timestamped tape (they are the memories, never deleted)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            tape_path = TAPES_DIR / f"tape_{timestamp}.gif"
            
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
                    print(f"[TAPE] ✅ Success! Tape under Discord limit with {strategy['description']}")
                    print(f"[TAPE] ▶ VHS tape recorded: {tape_path.name} ({len(scaled_frames)} frames, {file_size_mb:.2f} MB)")
                    return str(tape_path), ""
                else:
                    print(f"[TAPE] ⚠️ Still too large ({file_size_mb:.2f} MB > 7.5 MB), trying next strategy...")
            
            # If we exhausted all strategies, return the last attempt with a warning
            file_size = os.path.getsize(tape_path)
            file_size_mb = file_size / (1024 * 1024)
            if file_size > DISCORD_MAX_SIZE:
                error = f"GIF is {file_size_mb:.2f} MB (max 8 MB). Try a shorter session or contact support."
                print(f"[TAPE ERROR] {error}")
                return str(tape_path), error  # Return path anyway, let user try
            else:
                print(f"[TAPE] ⚠️ Tape is large but under limit: {file_size_mb:.2f} MB")
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
        # Always resolve relative to ROOT/images
        if image_path.startswith("/images/"):
            local = ROOT / "images" / Path(image_path).name
        else:
            local = ROOT / "images" / Path(image_path).name
        if local.exists():
            return discord.File(local, filename=local.name), local.name
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
            return ("explore", "🏃‍♂️")
        if any(k in choice_lower for k in action_keywords):
            return ("action", "⚡")
        if any(k in choice_lower for k in new_scene_keywords):
            return ("new scene", "🎬")
        return ("explore", "🏃‍♂️")

    MAX_BUTTON_LABEL_LENGTH = 80
    def safe_label(label):
        return label[:MAX_BUTTON_LABEL_LENGTH]

    MAX_EMBED_DESC_LEN = 4096

    def safe_embed_desc(text):
        if len(text) > MAX_EMBED_DESC_LEN:
            return text[:MAX_EMBED_DESC_LEN - 15] + '\n...(truncated)'
        return text
    
    async def generate_timeout_penalty(dispatch, situation_report, current_image=None):
        """Generate a contextual negative consequence for player inaction using LLM with vision"""
        import requests
        import engine
        import base64
        from pathlib import Path
        
        # Use the timeout_penalty_instructions from prompts file
        penalty_instructions = engine.PROMPTS.get("timeout_penalty_instructions", "")
        
        image_context = ""
        if current_image:
            image_context = "\n\n🖼️ CRITICAL: Look at the attached image. This is what's currently visible. Base the penalty on VISIBLE THREATS in the image (guards, helicopters, creatures, environmental hazards)."
        
        prompt = f"""{penalty_instructions}

CURRENT SITUATION:
{situation_report}

WHAT JUST HAPPENED:
{dispatch}{image_context}

Generate the penalty in valid JSON format with 'you/your' only. The penalty MUST match visible threats in the image."""

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
            self.last_choices_message = None  # Track the last choices message
            for i, choice in enumerate(choices):
                # Skip placeholder choices (—)
                if choice.strip() in ["—", "–", "-", ""]:
                    continue
                _, emoji = categorize_choice(choice)
                self.add_item(ChoiceButton(label=safe_label(f"{emoji} {choice}"), idx=i))
            self.add_item(CustomActionButton())  # Custom action button (row 1)
            self.add_item(AutoPlayToggleButton(self))  # Auto-play toggle (row 1)
            self.add_item(HDToggleButton(self))  # HD mode toggle (row 1)
            self.add_item(RegenerateChoicesButton(self))  # Regenerate (row 1)
            # Row 2 buttons
            self.add_item(RestartButton())  # Restart (row 2)

    class ChoiceButton(Button):
        def __init__(self, label, idx):
            super().__init__(label=label, style=discord.ButtonStyle.primary)
            self.idx = idx
        
        @staticmethod
        def _do_reset_static():
            """Static reset method that can be called without instance"""
            global _run_images
            try:
                with open(ROOT / "history.json", "w", encoding="utf-8") as f:
                    f.write("[]")
                import engine
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
                        print("[CHOICE] ✅ Buttons disabled to prevent double-click")
                except Exception as e:
                    print(f"[CHOICE] Warning: Could not disable buttons: {e}")
            
            import engine
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
                micro_reaction = "👀 The world holds its breath."
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
            
            # WHILE image is generating, show fate animation (fills wait time!)
            await asyncio.sleep(0.1)  # Tiny breath, then action!
            await animate_fate_roll(interaction.channel, fate)
            
            # Wait for Phase 1 to complete (should be done or almost done by now)
            phase1 = await phase1_task

            # Clean up interim messages
            try:
                await micro_msg.delete()
            except Exception:
                pass
            try:
                if 'action_msg' in locals():
                    await action_msg.delete()
            except Exception:
                pass

            # Show dispatch IMMEDIATELY from Phase 1 (what you feel/experience)
            dispatch_text = phase1.get("dispatch", "")
            if dispatch_text:
                await interaction.channel.send(embed=discord.Embed(
                    description=safe_embed_desc(dispatch_text.strip()),
                    color=VHS_RED
                ))
                await asyncio.sleep(0.8)  # Brief pause
            
            # Show IMAGE IMMEDIATELY from Phase 1
            image_path = phase1.get("consequence_image")
            
            # Track image for VHS tape
            global _run_images
            if image_path:
                _run_images.append(image_path)
                print(f"[TAPE] Frame {len(_run_images)} recorded")
            
            if image_path:
                file, name = _attach(image_path, phase1.get("vision_dispatch", ""))
                if file:
                    await interaction.channel.send(file=file)
                    print(f"[BOT] ✅ Image displayed immediately (before choices)!")
                await asyncio.sleep(0.5)
            
            # Show "Generating choices..." while Phase 2 runs
            choices_msg = await interaction.channel.send(embed=discord.Embed(
                description="⚙️ Analyzing scene...",
                color=CORNER_GREY
            ))
            
            # PHASE 2: Generate choices in background
            print(f"[BOT DEBUG] Passing to Phase 2: image={image_path}, dispatch={dispatch_text[:30]}...")
            
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
            import engine
            engine.state = engine._load_state()  # Force reload from disk
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
                tape_task = loop.run_in_executor(None, _create_death_replay_gif)
                
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
                        title="📼 VHS TAPE RECOVERED",
                        description="Camera footage retrieved from scene.",
                        color=CORNER_GREY
                    ))
                    try:
                        await interaction.channel.send(file=discord.File(tape_path))
                        print("[DEATH] 📼 Tape uploaded - waiting for player to download...")
                    except Exception as e:
                        print(f"[DEATH] Failed to send tape: {e}")
                        await interaction.channel.send(embed=discord.Embed(
                            title="⚠️ Tape Upload Failed",
                            description=f"Tape created but upload failed: {e}",
                            color=VHS_RED
                        ))
                else:
                    await interaction.channel.send(embed=discord.Embed(
                        title="⚠️ No Tape Created",
                        description=f"**Reason:** {error_msg}",
                        color=VHS_RED
                    ))
                
                # Create Play Again button (independent of disabled view)
                manual_restart_done = asyncio.Event()  # Flag to prevent double restart
                
                class PlayAgainButton(Button):
                    def __init__(self):
                        super().__init__(label="▶️ Play Again", style=discord.ButtonStyle.success)
                    
                    async def callback(self, button_interaction: discord.Interaction):
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
                
                # Show Play Again button immediately
                play_again_view = View(timeout=None)
                play_again_view.add_item(PlayAgainButton())
                await interaction.channel.send(
                    embed=discord.Embed(
                        description="💾 **Save the tape!** Press Play Again to restart.",
                        color=CORNER_GREY
                    ),
                    view=play_again_view
                )
                
                # Wait 30s for manual restart (check every second if button was clicked)
                print("[DEATH] Waiting 30s for manual restart or auto-restart...")
                for _ in range(30):
                    if manual_restart_done.is_set():
                        print("[DEATH] Manual restart detected - skipping auto-restart")
                        return  # Player clicked button, don't auto-restart
                    await asyncio.sleep(1)
                
                # Auto-restart the game (fallback if player didn't click)
                print("[DEATH] Auto-restarting game...")
                
                # Cancel all running tasks
                if auto_advance_task and not auto_advance_task.done():
                    auto_advance_task.cancel()
                    print("[DEATH] Cancelled auto-play task")
                if countdown_task and not countdown_task.done():
                    countdown_task.cancel()
                    print("[DEATH] Cancelled countdown task")
                auto_play_enabled = False
                
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._do_reset_static)
                
                # Show intro tutorial
                await send_intro_tutorial(interaction.channel)
                return  # End turn here
            
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
                    description=safe_embed_desc('⚠️ **Danger! Threat detected in the scene.**'),
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

            # Reset auto-advance timer on button press
            if auto_advance_task and not auto_advance_task.done():
                auto_advance_task.cancel()

    # ═══════════════════════════════════════════════════════════════════════════
    # Custom Action Modal and Button
    # ═══════════════════════════════════════════════════════════════════════════
    
    class CustomActionModal(discord.ui.Modal, title="✊ Custom Action"):
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
                await interaction.response.send_message("❌ Please enter an action!", ephemeral=True)
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
                    print("[CUSTOM ACTION] ✅ Buttons disabled to prevent concurrent actions")
            except Exception as e:
                print(f"[CUSTOM ACTION] Warning: Could not disable buttons: {e}")
            
            import engine
            world_state = engine.get_state().get('world_prompt', '')
            
            # Fast LLM call for micro-reaction
            micro_prompt = (
                "Given the player's custom action: '" + custom_choice + "', and the current world state: '" + world_state + "', "
                "write a 1-sentence immediate world or NPC reaction. Start with a relevant emoji. Be suspenseful, direct, and avoid spoilers."
            )
            try:
                micro_reaction = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: engine._ask(micro_prompt, model="gemini", temp=0.4, tokens=50)
                )
                # Ensure we never have empty string for Discord embed
                if not micro_reaction or not micro_reaction.strip():
                    micro_reaction = "👀 The world holds its breath."
            except Exception as e:
                print(f"[CUSTOM ACTION] Micro reaction failed: {e}")
                micro_reaction = "👀 The world holds its breath."
            
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
            
            # WHILE image is generating, show fate animation (fills wait time!)
            await asyncio.sleep(0.1)  # Tiny breath, then action!
            await animate_fate_roll(interaction.channel, fate)
            
            # Wait for Phase 1 to complete (should be done or almost done by now)
            phase1 = await phase1_task
            
            # Clean up interim messages
            try:
                await micro_msg.delete()
            except Exception:
                pass
            try:
                await action_msg.delete()
            except Exception:
                pass
            
            disp = phase1
            # Extract the choice text without emoji (handle custom action)
            choice_text = custom_choice
            
            # Display dispatch (what you feel/experience)
            await interaction.channel.send(embed=discord.Embed(description=safe_embed_desc(disp["dispatch"]), color=CORNER_TEAL))
            
            # Display image IMMEDIATELY
            img_path = disp.get("consequence_image")
            
            # Track image for VHS tape
            global _run_images
            if img_path:
                _run_images.append(img_path)
                print(f"[TAPE] Frame {len(_run_images)} recorded")
            
            if img_path:
                print(f"[BOT] ✅ Image displayed immediately (before choices)!")
                await interaction.channel.send(file=discord.File(img_path.lstrip("/")))
            
            # CHECK FOR DEATH - Read FRESH state
            import engine
            engine.state = engine._load_state()  # Force reload from disk
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
                tape_task = loop.run_in_executor(None, _create_death_replay_gif)
                
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
                        title="📼 VHS TAPE RECOVERED",
                        description="Camera footage retrieved from scene.",
                        color=CORNER_GREY
                    ))
                    try:
                        await interaction.channel.send(file=discord.File(tape_path))
                        print("[DEATH] 📼 Tape uploaded - waiting for player to download...")
                    except Exception as e:
                        print(f"[DEATH] Failed to send tape: {e}")
                        await interaction.channel.send(embed=discord.Embed(
                            title="⚠️ Tape Upload Failed",
                            description=f"Tape created but upload failed: {e}",
                            color=VHS_RED
                        ))
                else:
                    await interaction.channel.send(embed=discord.Embed(
                        title="⚠️ No Tape Created",
                        description=f"**Reason:** {error_msg}",
                        color=VHS_RED
                    ))
                
                # Create Play Again button (independent of disabled view)
                manual_restart_done = asyncio.Event()  # Flag to prevent double restart
                
                class PlayAgainButton(Button):
                    def __init__(self):
                        super().__init__(label="▶️ Play Again", style=discord.ButtonStyle.success)
                    
                    async def callback(self, button_interaction: discord.Interaction):
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
                
                # Show Play Again button immediately
                play_again_view = View(timeout=None)
                play_again_view.add_item(PlayAgainButton())
                await interaction.channel.send(
                    embed=discord.Embed(
                        description="💾 **Save the tape!** Press Play Again to restart.",
                        color=CORNER_GREY
                    ),
                    view=play_again_view
                )
                
                # Wait 30s for manual restart (check every second if button was clicked)
                print("[DEATH CUSTOM] Waiting 30s for manual restart or auto-restart...")
                for _ in range(30):
                    if manual_restart_done.is_set():
                        print("[DEATH CUSTOM] Manual restart detected - skipping auto-restart")
                        return  # Player clicked button, don't auto-restart
                    await asyncio.sleep(1)
                
                # Cancel all running tasks
                if auto_advance_task and not auto_advance_task.done():
                    auto_advance_task.cancel()
                    print("[DEATH] Cancelled auto-play task")
                if countdown_task and not countdown_task.done():
                    countdown_task.cancel()
                    print("[DEATH] Cancelled countdown task")
                auto_play_enabled = False
                
                await loop.run_in_executor(None, ChoiceButton._do_reset_static)
                await send_intro_tutorial(interaction.channel)
                return
            
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
            
            # Reset auto-advance timer
            if auto_advance_task and not auto_advance_task.done():
                auto_advance_task.cancel()
    
    class CustomActionButton(Button):
        def __init__(self):
            global custom_action_available, custom_action_turn_counter
            
            # Check if custom action is on cooldown
            if custom_action_available:
                super().__init__(
                    emoji="👊",
                    label="Custom",
                    style=discord.ButtonStyle.danger,
                    row=1,
                    disabled=False
                )
            else:
                turns_remaining = CUSTOM_ACTION_COOLDOWN - custom_action_turn_counter
                super().__init__(
                    emoji="👊",
                    label=f"Custom ({turns_remaining} turns)",
                    style=discord.ButtonStyle.secondary,
                    row=1,
                    disabled=True
                )
        
        async def callback(self, interaction: discord.Interaction):
            print("[LOG] CustomActionButton callback triggered")
            modal = CustomActionModal()
            await interaction.response.send_modal(modal)

    class RestartButton(Button):
        def __init__(self):
            super().__init__(emoji="⏏️", style=discord.ButtonStyle.danger, row=2)
            self.label = None  # Emoji only - VHS eject button

        async def callback(self, interaction: discord.Interaction):
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
                        print("[RESTART] ✅ Buttons disabled immediately")
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
            tape_task = loop.run_in_executor(None, _create_death_replay_gif)
            
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
                    title="📼 VHS TAPE SAVED",
                    description="Recording saved before restart.",
                    color=CORNER_GREY
                ))
                try:
                    await interaction.channel.send(file=discord.File(tape_path))
                    print(f"[RESTART] Tape saved: {tape_path}")
                except Exception as e:
                    print(f"[RESTART] Failed to send tape: {e}")
                    await interaction.channel.send(embed=discord.Embed(
                        title="⚠️ Tape Upload Failed",
                        description=f"Tape was created but failed to upload: {e}",
                        color=VHS_RED
                    ))
            else:
                # Tape creation failed - tell the user why
                await interaction.channel.send(embed=discord.Embed(
                    title="⚠️ No Tape Created",
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
                import engine
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
                    await interaction.response.send_message("⚠️ Delay must be at least 1 second!", ephemeral=True)
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
                await interaction.response.send_message("⚠️ Please enter a valid number!", ephemeral=True)

    class AutoPlayToggleButton(Button):
        def __init__(self, parent_view):
            global auto_play_enabled
            label = "🤖 Auto: ON" if auto_play_enabled else "🤖 Auto: OFF"
            style = discord.ButtonStyle.success if auto_play_enabled else discord.ButtonStyle.secondary
            super().__init__(label=label, style=style, row=1)
            self.parent_view = parent_view

        async def callback(self, interaction: discord.Interaction):
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
                    import engine
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
    
    class HDToggleButton(Button):
        def __init__(self, parent_view):
            global hd_mode_enabled
            label = "🎬 HD: ON" if hd_mode_enabled else "🎬 HD: OFF"
            style = discord.ButtonStyle.success if hd_mode_enabled else discord.ButtonStyle.secondary
            super().__init__(label=label, style=style, row=1)
            self.parent_view = parent_view
        
        async def callback(self, interaction: discord.Interaction):
            global hd_mode_enabled
            import engine
            
            # Toggle HD mode
            hd_mode_enabled = not hd_mode_enabled
            engine.HD_MODE = hd_mode_enabled
            
            # Update button appearance
            if hd_mode_enabled:
                self.label = "🎬 HD: ON"
                self.style = discord.ButtonStyle.success
                print("[HD MODE] Enabled - High quality images (slower)")
            else:
                self.label = "🎬 HD: OFF"
                self.style = discord.ButtonStyle.secondary
                print("[HD MODE] Disabled - Fast images (lower quality)")
            
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
                import engine
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
            print("[LOG] RegenerateChoicesButton callback triggered")
            try:
                await interaction.response.defer()
            except Exception as e:
                print(f"[LOG] RegenerateChoicesButton defer failed: {e}")
                pass
            try:
                import engine
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
            import engine
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
            title="📼 Welcome to SOMEWHERE: An Analog Horror Story",
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
            value="▶️ Press Play to continue the story.\n🔘 Click buttons to choose your action.",
            inline=False
        )
        rules_embed.add_field(
            name="Pro Tips",
            value="🕰️ Story escalates slowly — pay attention to details!\n🧭 Your choices shape Jason's fate.",
            inline=False
        )
        rules_embed.set_footer(text="Ready? Press ▶️ Play below to begin.")
        import engine
        class PlayButton(Button):
            def __init__(self):
                super().__init__(label="▶️ Play", style=discord.ButtonStyle.success)
            async def callback(self, interaction: discord.Interaction):
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
                
                # === SHOW LOGO IMMEDIATELY (Frame 0 of VHS tape) ===
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
                        # Crop and resize logo to match Gemini's exact 16:9 output
                        # Gemini is the GOLD STANDARD - logo must match its resolution exactly
                        from PIL import Image
                        logo_img = Image.open(str(logo_file))
                        
                        # Target: 16:9 aspect ratio (Gemini's standard)
                        # We'll determine exact resolution from first Gemini image, but use 16:9 for now
                        target_aspect = 16 / 9
                        
                        # Crop logo to 16:9 if needed
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
                            # Already 16:9!
                            logo_cropped = logo_img
                        
                        # Save at native 16:9 resolution (will match Gemini output naturally)
                        normalized_logo_path = ROOT / "static" / "Logo_normalized.jpg"
                        logo_cropped.save(str(normalized_logo_path), "JPEG", quality=95)
                        
                        # Send the normalized logo
                        await interaction.channel.send(file=discord.File(str(normalized_logo_path)))
                        # Track normalized logo as Frame 0 of VHS tape
                        _run_images.append(f"/static/Logo_normalized.jpg")
                        print(f"[TAPE] Frame 0 (logo) recorded: Logo_normalized.jpg ({logo_cropped.width}x{logo_cropped.height}, 16:9)")
                    except Exception as e:
                        print(f"[LOGO] Failed to process/send logo: {e}")
                else:
                    print(f"[LOGO] Logo file not found at {logo_path}")
                
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
                        full_path = dispatch_image_path.lstrip("/")
                        if os.path.exists(full_path):
                            await interaction.channel.send(file=discord.File(full_path))
                            print(f"[BOT INTRO] ✅ Image displayed immediately!")
                        else:
                            print(f"[BOT INTRO] Image not found: {full_path}")
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
                super().__init__(label="▶️ Play (No Images)", style=discord.ButtonStyle.secondary)
            async def callback(self, interaction: discord.Interaction):
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
                
        play_view = View(timeout=None)
        play_view.add_item(PlayButton())
        play_view.add_item(PlayNoImagesButton())
        await channel.send(embed=rules_embed, view=play_view)

    # ───────── startup ─────────────────────────────────────────────────────────
    @bot.event
    async def on_ready():
        global auto_play_enabled, auto_advance_task, countdown_task, custom_action_available, custom_action_turn_counter
        
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
        
        print(f"BOT | Logged in as {bot.user}")
        channel = bot.get_channel(CHAN)
        if channel is not None:
            if not RESUME_MODE:
                # Only play intro if not resuming
                await channel.send("🟢 What will you do next?")
                # Optionally, trigger intro turn logic here if needed
            else:
                await channel.send("[RESUME MODE] Resuming previous session. No intro.")
        else:
            print("BOT | Channel not found.")
        global OWNER_ID
        app_info = await bot.application_info()
        OWNER_ID = app_info.owner.id
        global running; running=True
        await channel.send(embed=beginning_simulation_embed())
        await send_intro_tutorial(channel)

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
                            print("[COUNTDOWN] ✅ Buttons disabled immediately")
                    except Exception as e:
                        print(f"[COUNTDOWN] Failed to disable buttons: {e}")
                    
                    # Show "generating penalty..." message
                    if countdown_message:
                        try:
                            await countdown_message.edit(
                                content=None,
                                embed=discord.Embed(
                                    description="⏱️ **Time's up!** Generating consequence...",
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
                    print(f"[COUNTDOWN] 🎰 Fate rolled: {fate}")
                    
                    # 2. Start image generation in background (don't block)
                    loop = asyncio.get_running_loop()
                    phase1_task = loop.run_in_executor(None, engine.advance_turn_image_fast, penalty_choice, fate)
                    
                    # 3. NOW show the fate animation WHILE image generates
                    await asyncio.sleep(0.1)  # Tiny pause for smoothness
                    await animate_fate_roll(channel, fate)
                    
                    # 4. Wait for image generation to complete
                    phase1_result = await phase1_task
                    
                    # Display dispatch and image
                    dispatch_text = phase1_result.get("dispatch", "")
                    if dispatch_text:
                        await channel.send(embed=discord.Embed(
                            description=safe_embed_desc(dispatch_text.strip()),
                            color=VHS_RED
                        ))
                    
                    consequence_image_path = phase1_result.get("consequence_image")
                    
                    # Track image for VHS tape
                    global _run_images
                    if consequence_image_path:
                        _run_images.append(consequence_image_path)
                        print(f"[TAPE] Frame {len(_run_images)} recorded")
                    
                    if consequence_image_path:
                        full_path = ROOT / consequence_image_path.lstrip("/")
                        if full_path.exists():
                            try:
                                await channel.send(file=discord.File(str(full_path)))
                            except Exception as e:
                                print(f"[COUNTDOWN] Image send failed: {e}")
                    
                    # CHECK FOR DEATH after timeout penalty
                    import engine
                    engine.state = engine._load_state()
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
                        tape_task = loop.run_in_executor(None, _create_death_replay_gif)
                        
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
                                title="📼 VHS TAPE RECOVERED",
                                description="Camera footage retrieved from scene.",
                                color=CORNER_GREY
                            ))
                            try:
                                await channel.send(file=discord.File(tape_path))
                                print("[COUNTDOWN DEATH] 📼 Tape uploaded - waiting for player to download...")
                            except Exception as e:
                                print(f"[COUNTDOWN DEATH] Failed to send tape: {e}")
                                await channel.send(embed=discord.Embed(
                                    title="⚠️ Tape Upload Failed",
                                    description=f"Tape created but upload failed: {e}",
                                    color=VHS_RED
                                ))
                        else:
                            await channel.send(embed=discord.Embed(
                                title="⚠️ No Tape Created",
                                description=f"**Reason:** {error_msg}",
                                color=VHS_RED
                            ))
                        
                        # Create Play Again button (independent of disabled view)
                        manual_restart_done = asyncio.Event()  # Flag to prevent double restart
                        
                        class PlayAgainButton(Button):
                            def __init__(self):
                                super().__init__(label="▶️ Play Again", style=discord.ButtonStyle.success)
                            
                            async def callback(self, button_interaction: discord.Interaction):
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
                    emoji = "⏱️"
                elif remaining > 10:
                    emoji = "⚠️"
                else:
                    emoji = "🚨"
                
                # Get player health
                import engine
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
        countdown_message = await channel.send(content=f"⏱️ **{COUNTDOWN_DURATION}s** [██████████]")
        
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
    
    # --- HD mode ---
    # HD mode uses high-resolution Gemini image generation (slower but better quality)
    hd_mode_enabled = True  # Default to HD mode ON
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
    CUSTOM_ACTION_COOLDOWN = 3  # Custom action available every 3 turns
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
            await channel.send(embed=discord.Embed(
                description=safe_embed_desc(dispatch_text.strip()),
                color=VHS_RED
            ))
        
        consequence_image_path = phase1_result.get("consequence_image")
        
        # Track image for VHS tape
        global _run_images
        if consequence_image_path:
            _run_images.append(consequence_image_path)
            print(f"[TAPE] Frame {len(_run_images)} recorded")
        
        if consequence_image_path:
            full_path = ROOT / consequence_image_path.lstrip("/")
            if full_path.exists():
                try:
                    await channel.send(file=discord.File(str(full_path)))
                    print(f"[AUTO-PLAY] ✅ Image displayed immediately!")
                except Exception as e:
                    print(f"[AUTO-PLAY] Image send failed: {e}")
        
        # CHECK FOR DEATH - Read FRESH state
        engine.state = engine._load_state()  # Force reload from disk
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
            tape_task = loop.run_in_executor(None, _create_death_replay_gif)
            
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
                    title="📼 VHS TAPE RECOVERED",
                    description="Camera footage retrieved from scene.",
                    color=CORNER_GREY
                ))
                try:
                    await channel.send(file=discord.File(tape_path))
                    print("[AUTO-PLAY DEATH] 📼 Tape uploaded - waiting for player to download...")
                except Exception as e:
                    print(f"[AUTO-PLAY] Failed to send tape: {e}")
                    await channel.send(embed=discord.Embed(
                        title="⚠️ Tape Upload Failed",
                        description=f"Tape created but upload failed: {e}",
                        color=VHS_RED
                    ))
            else:
                await channel.send(embed=discord.Embed(
                    title="⚠️ No Tape Created",
                    description=f"**Reason:** {error_msg}",
                    color=VHS_RED
                ))
            
            # Create Play Again button (independent of disabled view)
            manual_restart_done = asyncio.Event()  # Flag to prevent double restart
            
            class PlayAgainButton(Button):
                def __init__(self):
                    super().__init__(label="▶️ Play Again", style=discord.ButtonStyle.success)
                
                async def callback(self, button_interaction: discord.Interaction):
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

if __name__ == "__main__":
    import threading
    import time
    import engine

    if DISCORD_ENABLED:
        bot.run(TOKEN)
    else:
        print("⚠️ Discord disabled. Running in local web-only mode.")

        # Start the simulation loop once
        def local_loop():
            print("🌀 Starting local simulation tick...")
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
