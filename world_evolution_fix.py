"""
FIXED World Evolution System

This module replaces the destructive world evolution with an ACCUMULATIVE system.

Key changes:
1. World state is STRUCTURED (location, threats, environment, events)
2. New events are ADDED, not REPLACED
3. Periodic condensation maintains reasonable size
4. Persistent archive logs all evolution (survives resets)
5. Rich context is preserved across turns
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

# Constants
ROOT = Path(__file__).parent
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    with open("config/api_config.json", "r") as f:
        config = json.load(f)
        GEMINI_API_KEY = config.get("GEMINI_API_KEY", "")

with open("prompts/simulation_prompts.json", "r", encoding="utf-8") as f:
    PROMPTS = json.load(f)

# Load initial world state
INITIAL_WORLD_STATE = PROMPTS["world_initial_state"]


def evolve_world_state_v2(
    dispatches,
    consequence_summary=None,
    state_file="world_state.json",
    vision_description=None,
    session_id="default"
):
    """
    ACCUMULATIVE world evolution system.
    
    Instead of replacing world_prompt with a single sentence, we:
    1. Keep the core world description
    2. Maintain current situation context
    3. Add new events to the event buffer
    4. Condense periodically when buffer gets too long
    """
    
    # Load state
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        print(f"[ERROR] Failed to read {state_file}: {e}")
        return False
    
    # Increment turn counter
    turn_count = state.get("turn_count", 0) + 1
    state["turn_count"] = turn_count
    
    print(f"[WORLD EVOLUTION V2] Turn {turn_count}")
    
    # Get current world state structure
    old_world_prompt = state.get("world_prompt", INITIAL_WORLD_STATE)
    current_situation = state.get("current_situation", "")
    recent_events = state.get("recent_events", [])
    last_choice = state.get("last_choice", "")
    seen_elements = state.get("seen_elements", [])
    
    # Determine if we need to initialize structure
    if len(old_world_prompt) < 100:
        # Old system left us with a tiny prompt - restore initial state
        print("[WORLD EVOLUTION V2] Detected legacy minimal state - restoring full world description")
        old_world_prompt = INITIAL_WORLD_STATE
        state["world_prompt"] = old_world_prompt
    
    # Build prompt for new event generation
    location_context = ""
    if vision_description:
        location_context = f"JASON'S CURRENT VIEW: {vision_description[:200]}\n\n"
    else:
        location_context = f"CURRENT SITUATION: {current_situation if current_situation else 'Exploring facility'}\n\n"
    
    prompt = f"""You are tracking the evolving world state in a survival horror game.

CORE WORLD (never changes):
This is the Four Corners facility - a 1993 desert horror setting with abandoned Horizon facilities, red biome mutations, and military quarantine.

CURRENT SITUATION:
{current_situation if current_situation else "Player is exploring the facility perimeter"}

RECENT EVENTS (last 3 turns):
{chr(10).join(f"- {event}" for event in recent_events[-3:]) if recent_events else "- Game just started"}

{location_context}

LAST PLAYER ACTION:
{last_choice if last_choice else "Starting game"}

CONSEQUENCE OF ACTION:
{consequence_summary if consequence_summary else "None yet"}

VISION ANALYSIS (what player sees):
{vision_description if vision_description else "Not available"}

---

Generate a 2-3 sentence update describing:
1. WHERE the player is now (location/environment)
2. WHAT has changed (new threats, discoveries, environmental shifts)
3. WHAT is currently happening or building (tension, threats, atmosphere)

This should ACCUMULATE knowledge, not replace it. Include specifics from the vision and consequence.
Be concrete about location, threats, and discoveries. This is persistent memory.

Format as a natural paragraph, present tense, atmospheric."""

    try:
        # Call Gemini Flash for evolution
        import requests
        
        print("[GEMINI TEXT] Generating world evolution update...")
        response_data = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.7, "maxOutputTokens": 150}
            },
            timeout=30
        ).json()
        
        new_situation_text = response_data["candidates"][0]["content"]["parts"][0]["text"].strip()
        
        print(f"[WORLD EVOLUTION V2] Generated update: {new_situation_text[:100]}...")
        
        # Update current situation (replace, since this is a summary of NOW)
        state["current_situation"] = new_situation_text
        
        # Add to recent events buffer (append, not replace)
        event_summary = f"Turn {turn_count}: {consequence_summary if consequence_summary else 'Explored area'}"
        recent_events.append(event_summary)
        
        # Keep only last 10 events
        if len(recent_events) > 10:
            recent_events = recent_events[-10:]
        
        state["recent_events"] = recent_events
        
        # Update seen_elements
        new_elements = [e.strip() for e in re.split(r'[.,\n]', new_situation_text) if len(e.strip()) > 5]
        for elem in new_elements:
            if elem and elem not in seen_elements:
                seen_elements.append(elem)
        
        # Keep seen_elements manageable (last 50)
        if len(seen_elements) > 50:
            seen_elements = seen_elements[-50:]
        
        state["seen_elements"] = seen_elements
        
        # Save state
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        
        # Log to persistent archive
        log_to_evolution_archive(
            session_id=session_id,
            turn=turn_count,
            world_prompt=old_world_prompt,  # Core world (unchanged)
            situation_before=current_situation,
            situation_after=new_situation_text,
            player_action=last_choice,
            consequence=consequence_summary,
            vision=vision_description
        )
        
        # Log to console
        print(f"[WORLD EVOLUTION V2]")
        print(f"  SITUATION BEFORE: {current_situation[:80] if current_situation else '(none)'}...")
        print(f"  SITUATION AFTER:  {new_situation_text[:80]}...")
        print(f"  Recent events: {len(recent_events)}")
        print(f"  Seen elements: {len(seen_elements)}")
        
        return True
        
    except Exception as e:
        print(f"[ERROR] World evolution failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def log_to_evolution_archive(
    session_id,
    turn,
    world_prompt,
    situation_before,
    situation_after,
    player_action,
    consequence,
    vision
):
    """Log evolution to persistent archive (survives resets)"""
    
    archive_path = ROOT / "logs" / "world_evolution_archive.json"
    
    entry = {
        "session_id": session_id,
        "turn": turn,
        "timestamp": datetime.now().isoformat(),
        "world_prompt": world_prompt[:200] + "...",  # Truncate core world
        "situation_before": situation_before,
        "situation_after": situation_after,
        "player_action": player_action,
        "consequence": consequence,
        "vision_analysis": vision[:100] + "..." if vision and len(vision) > 100 else vision
    }
    
    # Load existing archive
    if archive_path.exists():
        try:
            with open(archive_path, 'r', encoding='utf-8') as f:
                archive = json.load(f)
        except:
            archive = []
    else:
        archive = []
        os.makedirs(archive_path.parent, exist_ok=True)
    
    archive.append(entry)
    
    # Keep only last 1000 entries to prevent file bloat
    if len(archive) > 1000:
        archive = archive[-1000:]
    
    # Save archive
    try:
        with open(archive_path, 'w', encoding='utf-8') as f:
            json.dump(archive, f, indent=2)
        print(f"[ARCHIVE] Logged evolution to {archive_path}")
    except Exception as e:
        print(f"[ARCHIVE ERROR] Failed to log: {e}")


def get_evolution_history(session_id=None, limit=10):
    """Retrieve evolution history from archive"""
    
    archive_path = ROOT / "logs" / "world_evolution_archive.json"
    
    if not archive_path.exists():
        return []
    
    try:
        with open(archive_path, 'r', encoding='utf-8') as f:
            archive = json.load(f)
        
        if session_id:
            archive = [e for e in archive if e.get("session_id") == session_id]
        
        return archive[-limit:] if limit else archive
        
    except Exception as e:
        print(f"[ARCHIVE ERROR] Failed to read: {e}")
        return []


def analyze_evolution_quality():
    """Analyze the quality of world evolution"""
    
    archive = get_evolution_history(limit=100)
    
    if not archive:
        print("No evolution history available.")
        return
    
    print("="*70)
    print("WORLD EVOLUTION QUALITY ANALYSIS")
    print("="*70)
    
    # Group by session
    sessions = {}
    for entry in archive:
        sid = entry.get("session_id", "unknown")
        if sid not in sessions:
            sessions[sid] = []
        sessions[sid].append(entry)
    
    for session_id, entries in sessions.items():
        print(f"\nSession: {session_id}")
        print(f"  Turns: {len(entries)}")
        
        # Check for accumulation
        situations = [e.get("situation_after", "") for e in entries]
        avg_length = sum(len(s.split()) for s in situations) / len(situations) if situations else 0
        
        print(f"  Avg situation length: {avg_length:.0f} words")
        
        if avg_length < 20:
            print("  [WARNING] Situations are too short - not enough context")
        elif avg_length > 100:
            print("  [WARNING] Situations are too long - may need condensation")
        else:
            print("  [GOOD] Situation length is healthy")
        
        # Show last 3 situations
        print("\n  Recent evolution:")
        for entry in entries[-3:]:
            turn = entry.get("turn", 0)
            situation = entry.get("situation_after", "")
            print(f"    Turn {turn}: {situation[:80]}...")


if __name__ == "__main__":
    print("World Evolution Fix Module")
    print("This module provides an accumulative world evolution system.")
    print("\nTo use:")
    print("  1. Replace `evolve_world_state` calls with `evolve_world_state_v2`")
    print("  2. Test with a full game to verify accumulation")
    print("  3. Check logs/world_evolution_archive.json for persistence")

