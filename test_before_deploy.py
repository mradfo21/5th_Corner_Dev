#!/usr/bin/env python3
"""
PRE-DEPLOYMENT TEST SUITE
Run this BEFORE every git push to catch issues locally.

Usage: python test_before_deploy.py
"""

import sys
import time
import subprocess

print("=" * 70)
print("PRE-DEPLOYMENT TEST SUITE")
print("=" * 70)

failed_tests = []

# ============================================================================
# TEST 1: Python Syntax Check
# ============================================================================
print("\n[TEST 1] Python Syntax Check...")
files_to_check = ["bot.py", "engine.py", "choices.py", "evolve_prompt_file.py", 
                  "ai_provider_manager.py", "lore_cache_manager.py", 
                  "gemini_image_utils.py"]

for file in files_to_check:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", file],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            print(f"  [OK] {file} - syntax OK")
        else:
            print(f"  [FAIL] {file} - SYNTAX ERROR")
            print(f"    {result.stderr}")
            failed_tests.append(f"Syntax check: {file}")
    except Exception as e:
        print(f"  [FAIL] {file} - ERROR: {e}")
        failed_tests.append(f"Syntax check: {file}")

# ============================================================================
# TEST 2: Module Import Test (Sequential)
# ============================================================================
print("\n[TEST 2] Module Import Test...")

modules_to_test = [
    ("ai_provider_manager", "import ai_provider_manager"),
    ("lore_cache_manager", "import lore_cache_manager"),
    # generate_interim_messages_on_demand was removed during the dynamic
    # world evolution rewrite (see choices.py header comment).  Just verify
    # the module imports + the current public entrypoint exists.
    ("evolve_prompt_file", "from evolve_prompt_file import evolve_world_state"),
    ("choices", "import choices"),
    ("engine", "import engine"),
]

for name, import_stmt in modules_to_test:
    try:
        print(f"  Testing {name}...", end=" ")
        sys.stdout.flush()
        
        result = subprocess.run(
            [sys.executable, "-c", import_stmt],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0:
            print("[OK]")
        else:
            print("[FAIL]")
            print(f"    Error: {result.stderr[:200]}")
            failed_tests.append(f"Import: {name}")
    except subprocess.TimeoutExpired:
        print("[FAIL] TIMEOUT (likely circular import)")
        failed_tests.append(f"Import timeout: {name}")
    except Exception as e:
        print(f"[FAIL] {e}")
        failed_tests.append(f"Import: {name}")

# ============================================================================
# TEST 3: Discord Bot Init Test
# ============================================================================
print("\n[TEST 3] Discord Bot Initialization...")

test_bot_code = """
import sys
import os

# Set test mode.  IMPORTANT: keep RESUME_MODE=1 so the bot's import path does
# not call engine.reset_state() and wipe the real session state every time the
# pre-deploy test is run locally.
os.environ['DISCORD_ENABLED'] = '1'
os.environ.setdefault('RESUME_MODE', '1')

# Try to import bot
try:
    # Import should complete without hanging
    import bot
    print("[OK] Bot imported successfully")
    sys.exit(0)
except Exception as e:
    print(f"[FAIL] Bot import failed: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
"""

try:
    print("  Testing bot.py import...", end=" ")
    sys.stdout.flush()
    
    result = subprocess.run(
        [sys.executable, "-c", test_bot_code],
        capture_output=True,
        text=True,
        timeout=15
    )
    
    if result.returncode == 0 and "[OK]" in result.stdout:
        print("[OK]")
    else:
        print("[FAIL]")
        print(f"    stdout: {result.stdout[:200]}")
        print(f"    stderr: {result.stderr[:200]}")
        failed_tests.append("Bot initialization")
except subprocess.TimeoutExpired:
    print("[FAIL] TIMEOUT")
    failed_tests.append("Bot init timeout")
except Exception as e:
    print(f"✗ {e}")
    failed_tests.append("Bot initialization")

# ============================================================================
# TEST 4: Check for Common Issues
# ============================================================================
print("\n[TEST 4] Common Issue Detection...")

# Check for __future__ imports not at top
print("  Checking __future__ import placement...", end=" ")
for file in ["engine.py", "bot.py", "choices.py"]:
    try:
        with open(file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            
        # Find first non-comment, non-docstring, non-blank line
        in_docstring = False
        first_code_line = None
        
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            
            # Track docstrings
            if stripped.startswith('"""') or stripped.startswith("'''"):
                if in_docstring:
                    in_docstring = False
                    continue
                else:
                    in_docstring = True
                    continue
            
            if in_docstring:
                continue
                
            # Skip comments and blank lines
            if not stripped or stripped.startswith("#"):
                continue
            
            # This is the first real code line
            first_code_line = (i, stripped)
            break
        
        # Check if it's a __future__ import
        if first_code_line and "from __future__ import" in first_code_line[1]:
            # Good!
            pass
        elif first_code_line and any(keyword in first_code_line[1] for keyword in ["import ", "from ", "print(", "class ", "def "]):
            # Check if there's a __future__ import later
            has_future_import = any("from __future__ import" in line for line in lines)
            if has_future_import:
                # Find where it is
                for i, line in enumerate(lines, 1):
                    if "from __future__ import" in line:
                        print(f"\n    [FAIL] {file}:{i} - __future__ import must be first!")
                        failed_tests.append(f"__future__ placement in {file}")
                        break
    except Exception as e:
        print(f"\n    [WARN] Error checking {file}: {e}")

if "__future__ placement" not in str(failed_tests):
    print("[OK]")

# Check for circular imports in top-level imports
print("  Checking for potential circular imports...", end=" ")
circular_issues = []

# Check engine.py doesn't import choices at module level
try:
    with open("engine.py", "r", encoding="utf-8") as f:
        lines = f.readlines()
    
    for i, line in enumerate(lines[:50], 1):  # Check first 50 lines
        if line.strip().startswith("import choices") or line.strip().startswith("from choices import"):
            if "# Local import" not in line and "def " not in "".join(lines[max(0,i-5):i]):
                circular_issues.append(f"engine.py:{i} imports choices at module level")
except Exception as e:
    print(f"\n    ? Error: {e}")

if circular_issues:
    print("[FAIL]")
    for issue in circular_issues:
        print(f"    {issue}")
    failed_tests.append("Circular import detection")
else:
    print("[OK]")

# ============================================================================
# TEST 5: Critical Configuration Files
# ============================================================================
print("\n[TEST 5] Critical Configuration Files...")
print("  Checking ai_config.json exists...", end=" ")
from pathlib import Path
import json
config_path = Path(__file__).parent / "ai_config.json"
if not config_path.exists():
    print("[FAIL]")
    failed_tests.append("ai_config.json missing")
else:
    # Verify correct provider and model
    with open(config_path, 'r') as f:
        config = json.load(f)
    image_provider = config.get('image_provider', '')
    image_model = config.get('image_model', '')
    
    # Should be using Gemini (not Veo) by default
    if image_provider != 'gemini':
        print("[FAIL]")
        print(f"    Expected provider: gemini, Got: {image_provider}")
        failed_tests.append("ai_config.json wrong provider")
    # Should be using Flash model for fast mode by default
    elif image_model != 'gemini-2.5-flash-image':
        print("[FAIL]")
        print(f"    Expected model: gemini-2.5-flash-image, Got: {image_model}")
        failed_tests.append("ai_config.json wrong model")
    else:
        print("[OK]")

# ============================================================================
# TEST 6: Quality Mode Configuration
# ============================================================================
print("\n[TEST 6] Quality Mode Configuration...")
print("  Checking QUALITY_MODE default...", end=" ")

try:
    with open("engine.py", "r", encoding="utf-8") as f:
        lines = f.readlines()
    
    quality_mode_line = None
    for line in lines:
        if "QUALITY_MODE" in line and "=" in line and not line.strip().startswith("#"):
            quality_mode_line = line
            break
    
    if quality_mode_line:
        # Should be True for HQ mode by default
        if "= True" in quality_mode_line or "=True" in quality_mode_line:
            print("[OK]")
        else:
            print("[FAIL]")
            print(f"    QUALITY_MODE should be True by default for HQ mode")
            print(f"    Found: {quality_mode_line.strip()}")
            failed_tests.append("QUALITY_MODE default")
    else:
        print("[FAIL]")
        print("    QUALITY_MODE not found in engine.py")
        failed_tests.append("QUALITY_MODE missing")
except Exception as e:
    print(f"[FAIL] {e}")
    failed_tests.append("QUALITY_MODE check")

# ============================================================================
# TEST 7: First Frame Quality Override
# ============================================================================
print("\n[TEST 7] First Frame Quality Override...")
print("  Checking frame 0 always uses HQ...", end=" ")

try:
    with open("engine.py", "r", encoding="utf-8") as f:
        content = f.read()
    
    # Look for the quality override logic
    has_frame_0_check = "frame_idx == 0" in content
    has_quality_override = "use_hq_for_this_frame" in content
    has_force_message = "FORCING HQ" in content
    
    if has_frame_0_check and has_quality_override and has_force_message:
        print("[OK]")
    else:
        print("[FAIL]")
        if not has_frame_0_check:
            print("    Missing frame_idx == 0 check")
        if not has_quality_override:
            print("    Missing use_hq_for_this_frame logic")
        if not has_force_message:
            print("    Missing quality override logging")
        failed_tests.append("First frame quality override")
except Exception as e:
    print(f"[FAIL] {e}")
    failed_tests.append("First frame quality override check")

# ============================================================================
# TEST 8: Consequence/Turn Engine Safety Checks
# ============================================================================
print("\n[TEST 8] Consequence/Turn Engine Safety Checks...")

try:
    with open("engine.py", "r", encoding="utf-8") as f:
        engine_content = f.read()

    # 8a: consequence_video_url must be initialized before the try block
    print("  Checking consequence_video_url initialized before try...", end=" ")
    if "consequence_video_url = None  # Initialize to prevent" in engine_content:
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("consequence_video_url not initialized before try block")

    # 8b: _vision_analyze_all must not return bare "" string
    print("  Checking _vision_analyze_all return type...", end=" ")
    if 'return ""' not in engine_content or engine_content.count('return ""') == 0:
        print("[OK]")
    else:
        # Check it's not in _vision_analyze_all specifically
        import re
        fn_match = re.search(r'def _vision_analyze_all.*?(?=\ndef |\Z)', engine_content, re.DOTALL)
        if fn_match and 'return ""' in fn_match.group(0):
            print("[FAIL]")
            failed_tests.append("_vision_analyze_all returns bare string instead of dict")
        else:
            print("[OK]")

    # 8c: advance_turn_choices_deferred must have an exception handler
    print("  Checking advance_turn_choices_deferred has exception handler...", end=" ")
    if "_advance_turn_choices_deferred_impl" in engine_content:
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("advance_turn_choices_deferred missing exception handler")

except Exception as e:
    print(f"[FAIL] {e}")
    failed_tests.append("Engine safety checks")

try:
    with open("bot.py", "r", encoding="utf-8") as f:
        bot_content = f.read()

    # 8d: ChoiceButton.callback must have a top-level turn guard
    print("  Checking ChoiceButton.callback has top-level turn guard...", end=" ")
    if "TOP-LEVEL TURN GUARD" in bot_content:
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("ChoiceButton.callback missing top-level turn guard")

    # 8e: CustomActionModal.on_submit turn guard
    print("  Checking CustomActionModal.on_submit has turn guard...", end=" ")
    if "CUSTOM ACTION TURN GUARD" in bot_content:
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("CustomActionModal.on_submit missing turn guard")

    # 8f: auto_advance_turn turn guard
    print("  Checking auto_advance_turn has turn guard...", end=" ")
    if "AUTO-ADVANCE TURN GUARD" in bot_content:
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("auto_advance_turn missing turn guard")

    # 8g: stale flipbook URL is cleared before new generation
    print("  Checking stale flipbook URL cleared before new generation...", end=" ")
    if "Cleared stale flipbook URL before starting" in engine_content or "current_flipbook_url'] = None" in engine_content:
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("Stale flipbook URL not cleared before new generation")

except Exception as e:
    print(f"[FAIL] {e}")
    failed_tests.append("Bot safety checks")

# ============================================================================
# TEST 9: Countdown Timer Safety Checks
# (This catches the bug that froze the channel after every timeout penalty)
# ============================================================================
print("\n[TEST 9] Countdown Timer Safety Checks...")

try:
    with open("bot.py", "r", encoding="utf-8") as f:
        bot_content = f.read()

    # 9a: countdown_timer_task must define loop before using loop.run_in_executor.
    #     Previously 'loop' was undefined in this function, causing a silent
    #     NameError that killed the task right after showing "Hesitation has
    #     consequences." — leaving the channel permanently frozen.
    print("  Checking countdown_timer_task defines 'loop' before run_in_executor...", end=" ")
    import re

    # Extract the body of countdown_timer_task
    cdt_match = re.search(
        r'async def countdown_timer_task\(.*?\n(.*?)(?=\n    (?:async def|def )\w)',
        bot_content,
        re.DOTALL
    )
    if cdt_match:
        cdt_body = cdt_match.group(0)
        # Find first occurrence of loop.run_in_executor and loop = asyncio.get_running_loop()
        first_use = cdt_body.find("loop.run_in_executor(")
        first_def = cdt_body.find("loop = asyncio.get_running_loop()")
        if first_def == -1:
            print("[FAIL]")
            print("    countdown_timer_task never defines 'loop = asyncio.get_running_loop()'")
            failed_tests.append("countdown_timer_task: loop undefined (will crash on every timeout)")
        elif first_use != -1 and first_def > first_use:
            print("[FAIL]")
            print(f"    loop.run_in_executor() used at offset {first_use} BEFORE loop is defined at {first_def}")
            failed_tests.append("countdown_timer_task: loop used before definition")
        else:
            print("[OK]")
    else:
        print("[WARN] Could not extract countdown_timer_task body for analysis")

    # 9b: countdown_timer_task must have a general exception handler (not just CancelledError).
    #     Without this, any crash in the penalty path silently kills the task.
    print("  Checking countdown_timer_task has general exception handler...", end=" ")
    if cdt_match:
        cdt_body = cdt_match.group(0)
        has_general_except = bool(re.search(r'except\s+Exception\s+as\s+\w+', cdt_body))
        if has_general_except:
            print("[OK]")
        else:
            print("[FAIL]")
            print("    countdown_timer_task only catches CancelledError — any other exception silently freezes the channel")
            failed_tests.append("countdown_timer_task: missing general exception handler")
    else:
        print("[WARN] Could not verify exception handler")

    # 9c: The general exception handler must post fallback choices (not just log).
    print("  Checking countdown_timer_task fallback posts choices on error...", end=" ")
    if "COUNTDOWN ERROR" in bot_content and "fallback_choices" in bot_content:
        print("[OK]")
    else:
        print("[FAIL]")
        print("    countdown_timer_task error handler does not post fallback choices")
        failed_tests.append("countdown_timer_task: error handler missing fallback choices")

    # 9d: Post-phase2 choices must always be posted — empty-choices must use
    #     hardcoded fallback instead of silently leaving the channel buttonless.
    print("  Checking post-phase2 choices always posted (empty guard)...", end=" ")
    if 'not new_choices' in bot_content and 'phase2 returned empty choices' in bot_content:
        print("[OK]")
    else:
        print("[FAIL]")
        print("    countdown path skips choice post when new_choices is empty — channel freezes")
        failed_tests.append("countdown_timer_task: empty new_choices guard missing")

except Exception as e:
    print(f"[FAIL] {e}")
    failed_tests.append("Countdown timer safety checks")

# ============================================================================
# TEST 10: API Resilience Checks
# ============================================================================
print("\n[TEST 10] API Resilience Checks (429 retry, empty-response guards)...")

try:
    with open("engine.py", "r", encoding="utf-8") as f:
        engine_content_r = f.read()
    with open("choices.py", "r", encoding="utf-8") as f:
        choices_content_r = f.read()

    # 10a: _ask_gemini should retry once on 429 before returning fallback
    print("  Checking _ask_gemini retries on 429...", end=" ")
    if "Rate limited (429)" in engine_content_r and "retrying in" in engine_content_r:
        print("[OK]")
    else:
        print("[FAIL]")
        print("    _ask_gemini does not retry on 429 — rate limits immediately return fallback text")
        failed_tests.append("_ask_gemini: no 429 retry")

    # 10b: generate_choices should retry once on 429 before returning fallback
    print("  Checking generate_choices retries on 429...", end=" ")
    if "Rate limited (429)" in choices_content_r and "retrying in" in choices_content_r:
        print("[OK]")
    else:
        print("[FAIL]")
        print("    generate_choices does not retry on 429 — rate limits immediately return fallback choices")
        failed_tests.append("generate_choices: no 429 retry")

except Exception as e:
    print(f"[FAIL] {e}")
    failed_tests.append("API resilience checks")

# ============================================================================
# TEST 11: Experience Mode System
# ============================================================================
print("\n[TEST 11] Experience Mode System...")

try:
    with open("engine.py", "r", encoding="utf-8") as f:
        engine_content_em = f.read()
    with open("api_client.py", "r", encoding="utf-8") as f:
        apiclient_content_em = f.read()
    with open("bot.py", "r", encoding="utf-8") as f:
        bot_content_em = f.read()

    # 11a: The three mode constants must be defined in engine.py
    print("  Checking experience mode constants in engine.py...", end=" ")
    has_no_images  = 'EXPERIENCE_MODE_NO_IMAGES'  in engine_content_em
    has_flipbook   = 'EXPERIENCE_MODE_FLIPBOOK'   in engine_content_em
    has_full_frame = 'EXPERIENCE_MODE_FULL_FRAME' in engine_content_em
    if has_no_images and has_flipbook and has_full_frame:
        print("[OK]")
    else:
        print("[FAIL]")
        for name, present in [
            ("EXPERIENCE_MODE_NO_IMAGES",  has_no_images),
            ("EXPERIENCE_MODE_FLIPBOOK",   has_flipbook),
            ("EXPERIENCE_MODE_FULL_FRAME", has_full_frame),
        ]:
            if not present:
                print(f"    Missing: {name}")
        failed_tests.append("Experience mode constants missing from engine.py")

    # 11b: apply_experience_mode() must be defined in engine.py
    print("  Checking apply_experience_mode() in engine.py...", end=" ")
    if "def apply_experience_mode(" in engine_content_em:
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("apply_experience_mode() missing from engine.py")

    # 11c: EXPERIENCE_MODES dict must exist in engine.py
    print("  Checking EXPERIENCE_MODES dict in engine.py...", end=" ")
    if "EXPERIENCE_MODES" in engine_content_em and "EXPERIENCE_MODES: dict" in engine_content_em:
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("EXPERIENCE_MODES dict missing from engine.py")

    # 11d: api_client must proxy apply_experience_mode
    print("  Checking apply_experience_mode proxy in api_client.py...", end=" ")
    if "def apply_experience_mode(" in apiclient_content_em:
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("apply_experience_mode() proxy missing from api_client.py")

    # 11e: api_client must expose the three mode constants as properties
    print("  Checking mode constant properties in api_client.py...", end=" ")
    missing_props = [
        p for p in ("EXPERIENCE_MODE_NO_IMAGES", "EXPERIENCE_MODE_FLIPBOOK",
                    "EXPERIENCE_MODE_FULL_FRAME")
        if p not in apiclient_content_em
    ]
    if not missing_props:
        print("[OK]")
    else:
        print("[FAIL]")
        for p in missing_props:
            print(f"    Missing property: {p}")
        failed_tests.append("Experience mode constant properties missing from api_client.py")

    # 11f: ExperienceModeSelect must be present in bot.py
    print("  Checking ExperienceModeSelect in bot.py...", end=" ")
    if "class ExperienceModeSelect" in bot_content_em:
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("ExperienceModeSelect class missing from bot.py")

    # 11g: IntroView must be used instead of plain View for the intro
    print("  Checking IntroView class in bot.py...", end=" ")
    if "class IntroView" in bot_content_em and "play_view = IntroView()" in bot_content_em:
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("IntroView class or play_view=IntroView() missing from bot.py")

    # 11h: PlayButton must call apply_experience_mode
    print("  Checking PlayButton calls apply_experience_mode...", end=" ")
    if "engine.apply_experience_mode(" in bot_content_em:
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("PlayButton does not call engine.apply_experience_mode()")

    # 11i: PlayNoImagesButton must NOT be added to play_view (superseded by select)
    print("  Checking PlayNoImagesButton removed from play_view...", end=" ")
    if "play_view.add_item(PlayNoImagesButton())" not in bot_content_em:
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append(
            "PlayNoImagesButton still added to play_view — should be removed "
            "(mode select handles no-images)"
        )

    # 11j: Dedicated test suite must exist
    print("  Checking test_experience_mode.py exists...", end=" ")
    if Path("test_experience_mode.py").exists():
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("test_experience_mode.py does not exist")

except Exception as e:
    print(f"[FAIL] {e}")
    failed_tests.append("Experience mode system checks")

# ============================================================================
# TEST 12: Temporal Consistency Architecture
# ============================================================================
print("\n[TEST 12] Temporal Consistency Architecture...")

try:
    with open("engine.py", "r", encoding="utf-8") as f:
        engine_tc = f.read()
    with open("gemini_image_utils.py", "r", encoding="utf-8") as f:
        utils_tc = f.read()
    import json as _json
    with open("prompts/simulation_prompts.json", "r", encoding="utf-8") as f:
        prompts_tc = _json.load(f)

    # 12a: Vision analysis must extract spatial compass
    print("  Checking spatial compass in _vision_analyze_all...", end=" ")
    if "SPATIAL:" in engine_tc and "spatial_compass" in engine_tc:
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("_vision_analyze_all missing SPATIAL field")

    # 12b: spatial_compass must be stored in history entries
    print("  Checking spatial_compass stored in history entries...", end=" ")
    if '"spatial_compass"' in engine_tc and "_spatial_compass_turn" in engine_tc:
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("spatial_compass not persisted in history entries")

    # 13c: build_image_prompt must accept prev_spatial
    print("  Checking build_image_prompt accepts prev_spatial...", end=" ")
    if "prev_spatial" in engine_tc and "SPATIAL ANCHOR" in engine_tc:
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("build_image_prompt missing prev_spatial / SPATIAL ANCHOR injection")

    # 12d: Flipbook reference order — panel 16 must be first
    print("  Checking flipbook references: panel_16 first...", end=" ")
    if "Panel 16 (spatial ground truth) FIRST" in engine_tc:
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("Flipbook reference order wrong — panel_16 must come first")

    # 12e: is_flipbook=True continuity must use panel 16 as spatial anchor
    print("  Checking is_flipbook continuity = spatial anchor...", end=" ")
    if "PANEL 16" in utils_tc.upper() and "SPATIAL" in utils_tc.upper():
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("is_flipbook=True continuity instruction missing panel-16 spatial anchor language")

    # 12f: flipbook prefix must require Frame 1 to continue from reference
    print("  Checking flipbook prefix: Frame 1 continuation rule...", end=" ")
    flipbook_prefix = prompts_tc.get("gemini_flipbook_4panel_prefix", "")
    if "FRAME 1" in flipbook_prefix and ("CONTINUATION" in flipbook_prefix or "reference image" in flipbook_prefix.lower()):
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("gemini_flipbook_4panel_prefix missing Frame 1 continuation rule")

    # 12g: flipbook prefix must allow actions to complete by frame 16
    print("  Checking flipbook prefix: progression to near-completion...", end=" ")
    if "NEAR-COMPLETION" in flipbook_prefix or "near completion" in flipbook_prefix.lower() or "SIGNIFICANTLY ADVANCED" in flipbook_prefix:
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("gemini_flipbook_4panel_prefix still uses 'show beginning only' — actions never complete visually")

    # 12h: image_to_image instructions must reference spatial anchor
    print("  Checking gemini_image_to_image_instructions has spatial anchor note...", end=" ")
    img2img_instructions = prompts_tc.get("gemini_image_to_image_instructions", "")
    if "SPATIAL ANCHOR" in img2img_instructions or "SPATIAL GROUND TRUTH" in img2img_instructions:
        print("[OK]")
    else:
        print("[FAIL]")
        failed_tests.append("gemini_image_to_image_instructions missing SPATIAL ANCHOR/GROUND TRUTH language")

except Exception as e:
    print(f"[FAIL] {e}")
    failed_tests.append("Temporal consistency architecture checks")

# ============================================================================
# TEST 13: Claude Opus Provider + No-Images Bugfixes
# ============================================================================
print("\n[TEST 13] Claude Opus Provider + No-Images Bugfixes...")
try:
    import json
    from pathlib import Path

    # 1. anthropic preset in ai_config.json
    print("  Checking anthropic preset in ai_config.json...", end=" ")
    cfg = json.loads((Path("ai_config.json")).read_text())
    presets = cfg.get("available_configs", {})
    assert "anthropic" in presets, "anthropic preset missing"
    assert presets["anthropic"]["text_provider"] == "anthropic"
    assert "claude" in presets["anthropic"]["text_model"].lower()
    print("[OK]")

    # 2. _ask_claude exists in engine.py
    print("  Checking _ask_claude function in engine.py...", end=" ")
    engine_src = Path("engine.py").read_text(encoding="utf-8")
    assert "def _ask_claude(" in engine_src, "_ask_claude missing from engine.py"
    assert "anthropic" in engine_src.lower(), "engine.py must import/use anthropic"
    print("[OK]")

    # 3. _ask routes anthropic provider
    print("  Checking _ask routes 'anthropic' to _ask_claude...", end=" ")
    assert 'elif provider == "anthropic"' in engine_src
    assert "_ask_claude(" in engine_src
    print("[OK]")

    # 4. anthropic in requirements.txt
    print("  Checking anthropic in requirements.txt...", end=" ")
    reqs = Path("requirements.txt").read_text()
    assert "anthropic" in reqs, "anthropic missing from requirements.txt"
    print("[OK]")

    # 5. bot.py AIProviderSelect has Claude Opus option
    print("  Checking bot.py AIProviderSelect has Claude Opus...", end=" ")
    bot_src = Path("bot.py").read_text(encoding="utf-8")
    assert "Claude Opus" in bot_src, "bot.py must show Claude Opus in dropdown"
    assert "ANTHROPIC_API_KEY" in bot_src, "bot.py must check ANTHROPIC_API_KEY"
    print("[OK]")

    # 6. No-images PlayButton has top-level guard
    print("  Checking no-images PlayButton has exception guard...", end=" ")
    assert "TOP-LEVEL NO-IMAGES GUARD" in bot_src, "no-images path missing exception guard"
    print("[OK]")

    # 7. Resume restores experience mode
    print("  Checking on_ready resume restores experience mode...", end=" ")
    assert "Restored experience mode" in bot_src, "on_ready resume must restore experience mode"
    print("[OK]")

    # 8. No-images path uses safe_embed_desc
    print("  Checking no-images path uses safe_embed_desc...", end=" ")
    guard_start = bot_src.find("TOP-LEVEL NO-IMAGES GUARD")
    guard_end = bot_src.find("return  # No-images path complete", guard_start)
    section = bot_src[guard_start:guard_end]
    assert "safe_embed_desc" in section, "no-images path must use safe_embed_desc"
    print("[OK]")

except Exception as e:
    print(f"[FAIL] {e}")
    failed_tests.append("Claude Opus provider + no-images bugfixes")

# ============================================================================
# RESULTS
# ============================================================================
print("\n" + "=" * 70)

if failed_tests:
    print("[FAIL] TESTS FAILED - DO NOT DEPLOY!")
    print("=" * 70)
    print("\nFailed tests:")
    for i, test in enumerate(failed_tests, 1):
        print(f"  {i}. {test}")
    print("\n" + "=" * 70)
    sys.exit(1)
else:
    print("[PASS] ALL TESTS PASSED - SAFE TO DEPLOY")
    print("=" * 70)
    print("\nYou can now run:")
    print("  git add .")
    print("  git commit -m 'your message'")
    print("  git push")
    print("=" * 70)
    sys.exit(0)

