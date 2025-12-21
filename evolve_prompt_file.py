"""
World Evolution System - Dynamic World Prompt with Evolution Summary

This module handles the evolution of the game's world state as the player progresses.

KEY DESIGN:
- world_prompt is FULLY DYNAMIC after Turn 0 (evolves with each action)
- world_prompt maintains 1200-1500 words of rich context
- evolution_summary provides 15-25 word player-facing updates
- recent_events buffer stores last 10 turn summaries
- seen_elements tracks discovered entities for choice grounding
"""

import json
import os
import requests
from pathlib import Path
from typing import List, Dict, Optional

# Get config and prompts
GEMINI_API_KEY = ""
config_path = Path("config.json")
if config_path.exists():
    with open(config_path, 'r', encoding='utf-8') as f:
        config_data = json.load(f)
        GEMINI_API_KEY = config_data.get("GEMINI_API_KEY", "")

if not GEMINI_API_KEY:
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Load prompts
prompts_path = Path("prompts/simulation_prompts.json")
if prompts_path.exists():
    with open(prompts_path, 'r', encoding='utf-8') as f:
        PROMPTS = json.load(f)
else:
    PROMPTS = {}


def evolve_world_state(
    dispatches: List[Dict],
    consequence_summary: str,
    state_file: str = "state.json",
    vision_description: str = ""
) -> dict:
    """
    Evolve the world state based on player actions and consequences.
    
    Returns:
        {
            "world_prompt": str,  # 1200-1500 words full world state (for LLM)
            "evolution_summary": str  # 15-25 words player-facing update
        }
    """
    print("[WORLD EVOLUTION V3] Starting dynamic world evolution...")
    
    # Load state
    state_path = Path(state_file)
    if not state_path.exists():
        print(f"[WORLD EVOLUTION V3] State file not found: {state_file}")
        return {"world_prompt": "", "evolution_summary": ""}
    
    with open(state_path, 'r', encoding='utf-8') as f:
        state = json.load(f)
    
    # Get current world prompt
    old_world_prompt = state.get("world_prompt", "")
    if not old_world_prompt or len(old_world_prompt) < 50:
        print("[WORLD EVOLUTION V3] World prompt too short, skipping evolution")
        return {"world_prompt": old_world_prompt, "evolution_summary": ""}
    
    # Initialize accumulative buffers if not present
    if "recent_events" not in state:
        state["recent_events"] = []
    if "seen_elements" not in state:
        state["seen_elements"] = []
    
    # Get recent context
    recent_events = state.get("recent_events", [])
    seen_elements = state.get("seen_elements", [])
    
    # Extract player action and consequence from dispatches
    last_choice = ""
    if dispatches and len(dispatches) > 0:
        last_entry = dispatches[-1]
        last_choice = last_entry.get("choice", last_entry.get("user_input", ""))
    
    player_action = last_choice if last_choice else "exploring"
    
    # Build evolution prompt
    prompt = f"""You are evolving a dynamic world state for a survival horror game.

CRITICAL PHILOSOPHY:
The world_prompt is a LIVING DOCUMENT that grows richer as the player progresses.
It is NOT a static setting - it EVOLVES to reflect the player's journey.

CURRENT WORLD STATE (1200-1500 words):
{old_world_prompt}

RECENT EVENTS (last 10 turns):
{chr(10).join(f"- {event}" for event in recent_events[-10:]) if recent_events else "- [First turn]"}

DISCOVERED ENTITIES:
{', '.join(seen_elements[-20:]) if seen_elements else "[None yet]"}

PLAYER'S LATEST ACTION:
{player_action}

CONSEQUENCE OF ACTION:
{consequence_summary}

VISION ANALYSIS (what Jason's camera sees right now):
{vision_description if vision_description else "[No visual analysis]"}

YOUR TASK:
Rewrite the ENTIRE world_prompt (1200-1500 words) to incorporate this new turn.

CRITICAL RULES:
1. PRESERVE the core setting and tone (1993 VHS horror, Four Corners facility)
2. INTEGRATE new discoveries, locations, threats from this turn
3. UPDATE spatial position (where Jason is NOW)
4. MAINTAIN narrative continuity (what's happened so far)
5. KEEP it 1200-1500 words (rich but not bloated)
6. AMPLIFY tension and horror as story progresses

STRUCTURE (maintain these sections):
- WHO: Jason Fleece, photojournalist, 1993
- WHERE: Current location in facility (updated!)
- WHAT'S HAPPENED: Journey so far (accumulated discoveries)
- THREATS: Known dangers, guards, creatures, hazards
- ENVIRONMENT: Four Corners desert, facility details
- TONE: VHS horror, grounded 1993 realism, body horror

Write the NEW world_prompt (1200-1500 words) that reflects everything up to this moment.

RETURN ONLY THE NEW WORLD PROMPT TEXT - NO PREAMBLE, NO EXPLANATION, JUST THE EVOLVED STATE.
"""

    # Call LLM to evolve world prompt
    try:
        print("[WORLD EVOLUTION V3] Calling LLM to evolve world prompt...")
        response = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
            headers={
                "x-goog-api-key": GEMINI_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": 2000  # ~1500 words
                }
            },
            timeout=30
        )
        
        if response.status_code != 200:
            print(f"[WORLD EVOLUTION V3] API error: {response.status_code}")
            return {"world_prompt": old_world_prompt, "evolution_summary": ""}
        
        result = response.json()
        new_world_prompt = result["candidates"][0]["content"]["parts"][0]["text"].strip()
        
        # Validate length (should be 1000-2000 words, ~5000-10000 chars)
        if len(new_world_prompt) < 800:
            print(f"[WORLD EVOLUTION V3] WARNING: New world prompt too short ({len(new_world_prompt)} chars), keeping old")
            new_world_prompt = old_world_prompt
        elif len(new_world_prompt) > 12000:
            print(f"[WORLD EVOLUTION V3] WARNING: New world prompt too long ({len(new_world_prompt)} chars), truncating")
            new_world_prompt = new_world_prompt[:12000] + "..."
        
        print(f"[WORLD EVOLUTION V3] World prompt evolved: {len(old_world_prompt)} -> {len(new_world_prompt)} chars")
        
    except Exception as e:
        print(f"[WORLD EVOLUTION V3] Evolution failed: {e}")
        return {"world_prompt": old_world_prompt, "evolution_summary": ""}
    
    # Generate evolution summary (player-facing, 15-25 words)
    evolution_summary = _generate_evolution_summary(
        old_world=old_world_prompt,
        new_world=new_world_prompt,
        consequence=consequence_summary,
        vision=vision_description
    )
    
    # Update state with new world prompt
    state["world_prompt"] = new_world_prompt
    
    # Add to recent events buffer (cap at 10)
    event_summary = f"{player_action} -> {consequence_summary[:80]}"
    state["recent_events"].append(event_summary)
    if len(state["recent_events"]) > 10:
        state["recent_events"] = state["recent_events"][-10:]
    
    # Extract and update seen elements
    new_entities = _extract_entities_from_text(consequence_summary)
    if vision_description:
        new_entities.extend(_extract_entities_from_text(vision_description))
    
    if new_entities:
        state["seen_elements"].extend(new_entities)
        # Remove duplicates, keep order
        state["seen_elements"] = list(dict.fromkeys(state["seen_elements"]))
        # Cap at 50, trim to 40 if exceeded
        if len(state["seen_elements"]) > 50:
            state["seen_elements"] = state["seen_elements"][-40:]
        print(f"[WORLD EVOLUTION V3] Added entities: {new_entities}")
    
    # Save updated state
    with open(state_path, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    
    # Log to archive
    _log_to_archive(
        turn_count=state.get("turn_count", 0),
        old_world=old_world_prompt,
        new_world=new_world_prompt,
        player_action=player_action,
        consequence=consequence_summary,
        evolution_summary=evolution_summary
    )
    
    print(f"[WORLD EVOLUTION V3] Complete!")
    print(f"  Old: {len(old_world_prompt)} chars")
    print(f"  New: {len(new_world_prompt)} chars")
    print(f"  Summary: {evolution_summary}")
    
    return {
        "world_prompt": new_world_prompt,
        "evolution_summary": evolution_summary
    }


def _generate_evolution_summary(
    old_world: str,
    new_world: str,
    consequence: str,
    vision: str
) -> str:
    """
    Generate a short, atmospheric player-facing summary (15-25 words).
    Fast LLM call to extract most significant change.
    """
    print("[EVOLUTION SUMMARY] Generating player-facing update...")
    
    # Extract first 500 chars of each for comparison
    old_snippet = old_world[:500]
    new_snippet = new_world[:500]
    
    prompt = f"""Extract the SINGLE MOST SIGNIFICANT change or development from this turn.

Write ONE atmospheric sentence (15-25 words) for the player to see during image generation.

Focus on:
- New immediate threats
- Location changes  
- Environmental developments
- Discoveries made
- Danger escalation

LAST EVENT: {consequence[:200]}
VISUAL CONTEXT: {vision[:200] if vision else "N/A"}

PREVIOUS STATE: {old_snippet}...
CURRENT STATE: {new_snippet}...

CRITICAL: Return ONLY the sentence itself - NO labels, NO prefixes like "Significant Change:", NO "Atmospheric Sentence:", NO preamble.
Just the raw sentence for the player to read.

Write a tense, atmospheric sentence (15-25 words) describing what's changed:"""

    try:
        response = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
            headers={
                "x-goog-api-key": GEMINI_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.3,
                    "maxOutputTokens": 50
                }
            },
            timeout=10
        )
        
        if response.status_code == 200:
            result = response.json()
            summary = result["candidates"][0]["content"]["parts"][0]["text"].strip()
            
            # Clean up quotes if present
            summary = summary.strip('"').strip("'")
            
            # Remove common LLM labels/prefixes
            labels_to_remove = [
                "Significant Change:",
                "Atmospheric Sentence:",
                "Evolution Summary:",
                "Change:",
                "Summary:",
                "Update:"
            ]
            for label in labels_to_remove:
                if summary.startswith(label):
                    summary = summary[len(label):].strip()
            
            # Truncate if too long (aim for ~20 words)
            words = summary.split()
            if len(words) > 28:
                summary = ' '.join(words[:25]) + "..."
            
            print(f"[EVOLUTION SUMMARY] Generated: {summary}")
            return summary
        else:
            print(f"[EVOLUTION SUMMARY] API error: {response.status_code}")
            return ""
    
    except Exception as e:
        print(f"[EVOLUTION SUMMARY] Failed: {e}")
        return ""


def _extract_entities_from_text(text: str) -> List[str]:
    """
    Extract significant physical entities (characters, objects, landmarks, threats)
    from text using LLM.
    """
    if not text or len(text) < 20:
        return []
    
    prompt = f"""From the following text, extract a list of significant physical entities.
Focus on characters, objects, landmarks, and explicit threats.
Exclude abstract concepts, generic descriptions, actions, and the protagonist (Jason/you).

CRITICAL: If NO significant entities are found, return the word "NONE" by itself.
If entities ARE found, return ONLY the comma-separated list (no labels, no "Entities:" prefix).

Text: "{text[:300]}"

Examples of GOOD responses:
- "Guard tower, chain-link fence, concrete barriers, two guards"
- "Rusted pickup truck, east gate structure, warning signs"
- "Red biome growth, pulsating tendrils, mutated creature"
- "NONE"

Return ONLY the entities or "NONE":"""
    
    try:
        response = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
            headers={
                "x-goog-api-key": GEMINI_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": 100
                }
            },
            timeout=10
        )
        
        if response.status_code == 200:
            result = response.json()
            entities_str = result["candidates"][0]["content"]["parts"][0]["text"].strip()
            
            # Check if response is "NONE" or empty
            if entities_str.upper() == "NONE" or not entities_str:
                return []
            
            # Split by comma
            entities = [e.strip() for e in entities_str.split(',') if e.strip()]
            
            # Filter out non-entities and malformed responses
            filtered = [
                e for e in entities
                if e.lower() not in ["jason", "you", "player", "the facility", "tension", "air", "none", "entities: none", "entities"]
                and len(e.split()) > 1  # Prefer multi-word for specificity
                and not e.lower().startswith("entities:")  # Remove label prefix
            ]
            
            return list(dict.fromkeys(filtered))[:7]  # Unique, max 7
        
    except Exception as e:
        print(f"[ENTITY EXTRACTION] Failed: {e}")
    
    return []


def _log_to_archive(
    turn_count: int,
    old_world: str,
    new_world: str,
    player_action: str,
    consequence: str,
    evolution_summary: str
):
    """Log world evolution to persistent archive."""
    archive_path = Path("logs/world_evolution_archive.json")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Load existing archive
    archive = []
    if archive_path.exists():
        try:
            with open(archive_path, 'r', encoding='utf-8') as f:
                archive = json.load(f)
        except:
            archive = []
    
    # Add new entry
    archive.append({
        "turn": turn_count,
        "player_action": player_action,
        "consequence": consequence[:200],
        "evolution_summary": evolution_summary,
        "world_prompt_length_before": len(old_world),
        "world_prompt_length_after": len(new_world),
        "timestamp": str(Path(archive_path).stat().st_mtime) if archive_path.exists() else ""
    })
    
    # Cap at 1000 entries
    if len(archive) > 1000:
        archive = archive[-1000:]
    
    # Save archive
    with open(archive_path, 'w', encoding='utf-8') as f:
        json.dump(archive, f, indent=2, ensure_ascii=False)
