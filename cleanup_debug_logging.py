#!/usr/bin/env python3
"""
Script to clean up excessive DEBUG PRINT statements in engine.py
Converts them to proper Python logging with appropriate levels.
"""

import re
from pathlib import Path

def cleanup_debug_logging():
    """Remove or convert excessive debug print statements"""
    
    engine_path = Path("engine.py")
    content = engine_path.read_text(encoding='utf-8')
    original_content = content
    
    # Count original debug prints
    debug_print_count = len(re.findall(r'print\(f"DEBUG PRINT:', content))
    img_debug_count = len(re.findall(r'print\(f"\[IMG DEBUG\]', content))
    
    print(f"Found {debug_print_count} 'DEBUG PRINT' statements")
    print(f"Found {img_debug_count} '[IMG DEBUG]' statements")
    
    # Pattern 1: Remove ultra-verbose thread debugging (keep only critical ones)
    # Remove: "DEBUG PRINT: _process_turn_background - State loaded..."
    verbose_patterns = [
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - State loaded.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - STEP \d+:.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - About to.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - Narrative dispatch generated.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - generate_and_apply_choice completed.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - Consequence summary generated.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - No significant consequence.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - Vision dispatch for image.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - Image generated.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - Vision analysis generated.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - World state evolution complete.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - LLM_ENABLED is False.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - STEP 1: final_choice_prompt_text.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - STEP 2: choices\.generate_choices returned.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - STEP 3: _structure_choices_for_feed returned.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - STEP 4: State saved.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - STEP 4: Adding.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - STEP 4: New choice item.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - STEP 4: No new items.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - check_player_death returned.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - history\.json updated.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - feed_log pruned.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - turn_count updated.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - Exception during check_player_death.*?\n',
        r'\s*print\(f"DEBUG PRINT: _process_turn_background - Exception saving history.*?\n',
        r'\s*print\(f"DEBUG PRINT: api_choose - Before WORLD_STATE_LOCK.*?\n',
        r'\s*print\(f"DEBUG PRINT: api_choose - Acquired WORLD_STATE_LOCK.*?\n',
        r'\s*print\(f"DEBUG PRINT: api_choose - About to call _save_state.*?\n',
        r'\s*print\(f"DEBUG PRINT: api_choose - _save_state completed.*?\n',
        r'\s*print\(f"DEBUG PRINT: api_choose - After WORLD_STATE_LOCK block.*?\n',
        r'\s*print\(f"DEBUG PRINT: api_choose - Thread object created.*?\n',
        r'\s*print\(f"DEBUG PRINT /api/feed: since_id_str.*?\n',
        r'\s*print\(f"DEBUG PRINT /api/feed: Last few item IDs.*?\n',
        r'\s*print\(f"DEBUG PRINT /api/feed: since_id.*?\n',
        r'\s*print\(f"DEBUG PRINT /api/feed: No since_id_str.*?\n',
    ]
    
    removed_count = 0
    for pattern in verbose_patterns:
        matches = re.findall(pattern, content)
        removed_count += len(matches)
        content = re.sub(pattern, '', content)
    
    # Pattern 2: Convert remaining critical debug prints to conditional logging
    # Keep: Thread spawned, critical errors, combat state changes
    # But make them conditional on DEBUG_MODE environment variable
    
    # Add debug mode check at top of file (after imports)
    if 'DEBUG_MODE = os.getenv' not in content:
        import_section_end = content.find('# ═══════════════════════════════════════════════════════════════════════════')
        if import_section_end > 0:
            debug_mode_code = '\n# Debug mode (set DEBUG_MODE=1 environment variable to enable verbose logging)\nDEBUG_MODE = os.getenv("DEBUG_MODE", "0") == "1"\n\n'
            content = content[:import_section_end] + debug_mode_code + content[import_section_end:]
    
    # Convert remaining DEBUG PRINT to conditional
    content = re.sub(
        r'print\(f"DEBUG PRINT: (.*?)", flush=True\)',
        r'if DEBUG_MODE: print(f"[DEBUG] \1", flush=True)',
        content
    )
    
    # Pattern 3: Remove excessive [IMG DEBUG] statements (keep only errors)
    content = re.sub(
        r'\s*print\(f"\[IMG DEBUG\] active_image_provider = .*?\n',
        '',
        content
    )
    content = re.sub(
        r'\s*print\(f"\[IMG DEBUG\] About to call image generation API.*?\n',
        '',
        content
    )
    content = re.sub(
        r'\s*print\(f"\[IMG DEBUG\] -> Calling Veo video generation.*?\n',
        '',
        content
    )
    content = re.sub(
        r'\s*print\(f"\[IMG DEBUG\] Entering Veo branch.*?\n',
        '',
        content
    )
    
    # Write back
    engine_path.write_text(content, encoding='utf-8')
    
    # Count after cleanup
    debug_print_after = len(re.findall(r'DEBUG PRINT:', content))
    img_debug_after = len(re.findall(r'\[IMG DEBUG\]', content))
    conditional_debug = len(re.findall(r'if DEBUG_MODE:', content))
    
    print(f"\nCleanup complete!")
    print(f"   Removed {removed_count} verbose debug statements")
    print(f"   'DEBUG PRINT' statements: {debug_print_count} -> {debug_print_after}")
    print(f"   '[IMG DEBUG]' statements: {img_debug_count} -> {img_debug_after}")
    print(f"   Converted to conditional: {conditional_debug}")
    print(f"\n   Set DEBUG_MODE=1 environment variable to enable verbose logging")
    
    return content != original_content

if __name__ == "__main__":
    changed = cleanup_debug_logging()
    if changed:
        print("\nengine.py has been updated")
    else:
        print("\nNo changes made")

