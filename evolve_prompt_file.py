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
def _get_api_key():
    """Helper to get Gemini API key from environment or config."""
    key = os.getenv("GEMINI_API_KEY")
    if key:
        return key
    
    config_path = Path("config.json")
    if config_path.exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = json.load(f)
                return config_data.get("GEMINI_API_KEY", "")
        except:
            pass
    return ""

GEMINI_API_KEY = _get_api_key()

# Load prompts — shared, hot-reloadable singleton (see prompts_store.py).
from prompts_store import PROMPTS

# Every prompt in the game reads this document, so its size is the game's
# per-turn token floor. Measured after the static lore is appended.
_WORLD_PROMPT_CAP = int(os.getenv("WORLD_PROMPT_CAP", "11000"))


def _trim_to_sentence(text: str) -> str:
    """Drop a trailing half-sentence.

    The world state is regenerated against a hard token ceiling, so the reply
    routinely stopped mid-word. That fragment then rode into every downstream
    prompt for the rest of the turn.
    """
    t = (text or "").rstrip()
    if not t or t[-1] in ".!?\"'»":
        return t
    cut = max(t.rfind(". "), t.rfind(".\n"), t.rfind("! "), t.rfind("? "))
    if cut > len(t) * 0.5:
        return t[:cut + 1]
    return t


# Structured-output schema for the single-call evolution: world_prompt +
# player-facing summary + discovered entities in ONE request instead of three.
# evolve_world_state falls back to the legacy prose call + two helper calls on
# any parse failure, so the world document is never put at risk.
_EVOLUTION_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "world_prompt": {"type": "STRING"},
        "evolution_summary": {"type": "STRING"},
        "entities": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["world_prompt"],
}


def _filter_entities(entities: List[str]) -> List[str]:
    """Drop generic scenery / names and order characters + threats first.

    Shared by the dedicated extractor and the single-call evolution path so both
    produce an identically filtered, deduped, priority-sorted list (max 7)."""
    filtered = [
        e for e in (entities or [])
        if e and e.lower() not in [
            "jason", "you", "player", "the facility", "tension", "air", "none",
            "entities: none", "entities", "wall", "ground", "sand", "rock",
            "dust", "smoke", "concrete", "metal",
        ]
        and len(e.split()) >= 1
        and not e.lower().startswith("entities:")
    ]
    character_keywords = ["guard", "figure", "person", "silhouette", "human",
                          "scientist", "soldier", "creature", "mutant",
                          "infected", "patrol", "armed"]

    def entity_priority(entity: str) -> int:
        el = entity.lower()
        if any(k in el for k in character_keywords):
            return 0
        elif len(entity.split()) > 2:
            return 1
        return 2

    filtered.sort(key=entity_priority)
    return list(dict.fromkeys(filtered))[:7]


def evolve_world_state(
    dispatches: List[Dict],
    consequence_summary: str,
    state_file: str = "state.json",
    vision_description: str = "",
    player_action: str = "",
) -> dict:
    """
    Evolve the world state based on player actions and consequences.
    
    Returns:
        {
            "world_prompt": str,  # 1200-1500 words full world state (for LLM)
            "evolution_summary": str  # 15-25 words player-facing update
        }
    """
    api_key = _get_api_key()
    if not api_key:
        print("[WORLD EVOLUTION V3] ERROR: No API key found!")
        return {"world_prompt": "", "evolution_summary": ""}

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
    # Everything the world did to ITSELF while the player was deciding (see
    # engine.world_drift_tick). Those beats have already been shown to the
    # player and pushed to the live world model, so if this rewrite ignores
    # them the world state silently contradicts what the player just watched.
    # Folding them in here is what makes drift accumulate into real history
    # instead of evaporating at the next turn.
    ambient_beats = [b for b in (state.get("ambient_beats") or []) if b]
    
    # The action THIS turn. The caller passes it explicitly, because `dispatches`
    # (the history) does NOT yet contain the current turn — Phase 2 appends this
    # turn's entry only after the slate is built, so `dispatches[-1]` is the
    # PREVIOUS turn's choice. Reading it from history therefore attributed every
    # world rewrite (and its recent_events line + archive record) to the prior
    # move. Fall back to the history-derived value only when no explicit action
    # was threaded through, preserving the old behavior for any legacy caller.
    last_choice = (player_action or "").strip()
    if not last_choice and dispatches and len(dispatches) > 0:
        last_entry = dispatches[-1]
        last_choice = last_entry.get("choice", last_entry.get("user_input", ""))

    player_action = last_choice if last_choice else "exploring"

    # The world_prompt is seeded at reset with the player's cast sheet (who they
    # are, the level, the camera). This pass REWRITES that whole document every
    # turn, so without an explicit preservation rule it quietly launders the
    # player's character back into the shipped photojournalist within a few turns.
    import game_identity
    cast_sheet = game_identity.narrative_directive()
    cast_rule = (
        f"\n\n{cast_sheet}\n\n"
        "THE DIRECTOR'S SHEET ABOVE IS FIXED. Carry it through verbatim in the "
        "rewritten world state — never rename the protagonist, never relocate the "
        "story to a different setting, never change the camera perspective.\n"
        if cast_sheet else ""
    )
    # Every line of the STRUCTURE skeleton below is authoritative for the next
    # turn — the rewrite it produces IS the world state every other prompt then
    # reads. While the skeleton said "ENVIRONMENT: Four Corners desert", an
    # authored level lost a little more ground every turn until it was gone.
    _lines = game_identity.structure_lines()
    who_line = _lines["who"] or "Jason Fleece, photojournalist, 1993"
    where_line = _lines["where"] or "Current location in facility (updated!)"
    environment_line = _lines["environment"] or "Four Corners desert, facility details"
    tone_line = _lines["tone"] or "VHS horror, grounded 1993 realism, body horror"
    setting_line = _lines["tone"] or "1993 VHS horror, Four Corners facility"
    if _lines["environment"]:
        setting_line = ", ".join(p for p in (_lines["tone"], _lines["environment"]) if p)

    # `world_evolution_instructions` lives in simulation_prompts.json and was
    # read by nothing. That made the single prompt that rewrites the world
    # every turn the one prompt the World Editor could not touch — you could
    # rewrite the opening world state and watch this pass quietly walk it back.
    house_rules = (PROMPTS.get("world_evolution_instructions") or "").strip()
    house_rules_block = f"\n\nHOUSE RULES FOR HOW THIS WORLD CHANGES:\n{house_rules}\n" if house_rules else ""
    try:
        import experience_store
        if experience_store.lore_brief():
            house_rules_block += (
                "\nLORE IS CANON. Historical background already in the current "
                "world state (company, year, place, what they buried) is fixed. "
                "Rewrite the living situation; do not invent a different setting.\n"
            )
    except Exception:
        pass

    # Build evolution prompt
    prompt = f"""You are evolving a dynamic world state for a survival horror game.{cast_rule}{house_rules_block}

CRITICAL PHILOSOPHY:
The world_prompt is a LIVING DOCUMENT that grows richer as the player progresses.
It is NOT a static setting - it EVOLVES to reflect the player's journey.

CURRENT WORLD STATE:
{old_world_prompt}

RECENT EVENTS (last 10 turns):
{chr(10).join(f"- {event}" for event in recent_events[-10:]) if recent_events else "- [First turn]"}

DISCOVERED ENTITIES:
{', '.join(seen_elements[-20:]) if seen_elements else "[None yet]"}

PLAYER'S LATEST ACTION:
{player_action}

CONSEQUENCE OF ACTION:
{consequence_summary}

WHAT THE WORLD DID ON ITS OWN WHILE THE PLAYER DELIBERATED:
{chr(10).join(f"- {beat}" for beat in ambient_beats) if ambient_beats else "- [Nothing drifted]"}

VISION ANALYSIS (what your camera sees right now):
{vision_description if vision_description else "[No visual analysis]"}

YOUR TASK:
Rewrite the ENTIRE world_prompt (500-650 words) to incorporate this new turn.

CRITICAL RULES:
1. PRESERVE the core setting and tone ({setting_line})
2. INTEGRATE new discoveries, locations, threats from this turn
3. UPDATE spatial position (where the protagonist is NOW)
4. MAINTAIN narrative continuity (what's happened so far)
5. KEEP it 500-650 words. This is the LIVING state — who is here, where they
   are, what just changed, what is closing in. The static background (company,
   year, region, what they buried, what the biome looks like) is appended
   separately and is NOT your job to restate. Do not pad it back in.
6. AMPLIFY tension and horror as story progresses
7. CARRY the world's own drift forward — the player SAW those changes happen, so
   they are now facts about this place, not weather that resets

STRUCTURE (maintain these sections):
- WHO: {who_line}
- WHERE: {where_line}
- WHAT'S HAPPENED: Journey so far (accumulated discoveries)
- THREATS: Known dangers, guards, creatures, hazards
- ENVIRONMENT: {environment_line}
- TONE: {tone_line}

Write the NEW world_prompt (500-650 words) that reflects everything up to this moment.
End on a complete sentence.

RETURN ONE JSON OBJECT with these fields and nothing else:
  "world_prompt": the FULL evolved world state (500-650 words of prose, ending on a complete sentence). This is the living document every other prompt reads — write it exactly as you would the standalone state, just placed in this field.
  "evolution_summary": ONE tense, atmospheric sentence (15-25 words) for the player describing the single most significant change this turn. No labels, no prefix, just the sentence.
  "entities": a list of the significant NEW physical entities introduced this turn — people/characters first, then creatures/threats, then major objects/landmarks. Empty list if none. Exclude generic scenery (wall, ground, sand, rock, dust, smoke, concrete, metal).
"""

    # Call LLM to evolve world prompt
    try:
        print("[WORLD EVOLUTION V3] Calling LLM to evolve world prompt...")
        response = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent",
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json"
            },
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"thinkingConfig": {"thinkingBudget": 0},
                    "temperature": 0.7,
                    # Has to clear the word target with headroom. At 900 against
                    # a "1200-1500 words" ask the reply was cut off mid-word
                    # every turn, so the world document permanently ended in a
                    # half-sentence ("...agitated by") that every downstream
                    # prompt then read. Raised further here because this one call
                    # now also carries the summary + entities in the same JSON.
                    "maxOutputTokens": 1400,
                    # Single-call evolution: world_prompt + summary + entities in
                    # one structured response instead of three sequential calls.
                    "responseMimeType": "application/json",
                    "responseSchema": _EVOLUTION_RESPONSE_SCHEMA,
                }
            },
            timeout=20
        )
        
        if response.status_code != 200:
            print(f"[WORLD EVOLUTION V3] API error: {response.status_code}")
            return {"world_prompt": old_world_prompt, "evolution_summary": ""}
        
        result = response.json()
        _raw = result["candidates"][0]["content"]["parts"][0]["text"].strip()
        # Parse the combined JSON defensively. On ANY failure, treat the raw text
        # as the world prose (legacy behavior) and let the two helper calls below
        # fill summary + entities — so the world document is never regressed by
        # this optimization, and we never pay for a second rewrite call.
        _structured_summary = None
        _structured_entities = None
        if _raw.startswith("```"):
            _fence = _raw.split("\n")
            if _fence and _fence[0].startswith("```"):
                _fence = _fence[1:]
            if _fence and _fence[-1].strip() == "```":
                _fence = _fence[:-1]
            _raw = "\n".join(_fence).strip()
        try:
            _data = json.loads(_raw)
            new_world_prompt = (_data.get("world_prompt") or "").strip()
            if isinstance(_data.get("evolution_summary"), str):
                _structured_summary = _data["evolution_summary"].strip()
            if isinstance(_data.get("entities"), list):
                _structured_entities = [str(e).strip() for e in _data["entities"] if str(e).strip()]
            if not new_world_prompt:
                # Valid JSON but no usable world_prompt — fall back to raw prose.
                new_world_prompt = _raw
                _structured_summary = None
                _structured_entities = None
        except Exception:
            # Not JSON (older behavior / a plain-prose reply) — use it as-is and
            # let the dedicated summary + entity calls run below.
            new_world_prompt = _raw
        new_world_prompt = _trim_to_sentence(new_world_prompt)

        if len(new_world_prompt) < 800:
            print(f"[WORLD EVOLUTION V3] WARNING: New world prompt too short ({len(new_world_prompt)} chars), keeping old")
            new_world_prompt = old_world_prompt

        print(f"[WORLD EVOLUTION V3] World prompt evolved: {len(old_world_prompt)} -> {len(new_world_prompt)} chars")
        try:
            import experience_store
            lore_chars = len(experience_store.lore_brief())
            new_world_prompt = experience_store.with_lore(new_world_prompt)
            if lore_chars:
                print(f"[WORLD EVOLUTION V3] + {lore_chars} chars static lore "
                      f"= {len(new_world_prompt)} chars total")
        except Exception:
            pass

        # The ceiling has to sit AFTER the lore goes in, or the one thing that
        # actually makes this document huge is the one thing it never measures.
        # The lore now LEADS the document rather than trailing it, so what this
        # cut takes is the tail of the evolved direction instead of the tail of
        # the author's bible — which is the right way round: the direction is
        # rewritten every turn, the bible is the source of truth.
        if len(new_world_prompt) > _WORLD_PROMPT_CAP:
            print(f"[WORLD EVOLUTION V3] WARNING: world prompt {len(new_world_prompt)} chars "
                  f"> cap {_WORLD_PROMPT_CAP}, truncating "
                  f"{len(new_world_prompt) - _WORLD_PROMPT_CAP} chars off the end")
            new_world_prompt = _trim_to_sentence(new_world_prompt[:_WORLD_PROMPT_CAP])
        
    except Exception as e:
        print(f"[WORLD EVOLUTION V3] Evolution failed: {e}")
        return {"world_prompt": old_world_prompt, "evolution_summary": ""}
    
    # Player-facing summary: reuse the one the single evolution call already
    # produced. Only fall back to the dedicated summarizer (an extra round-trip)
    # when the combined call did not return a usable summary.
    if _structured_summary:
        evolution_summary = _structured_summary
        print(f"[EVOLUTION SUMMARY] From combined call: {evolution_summary}")
    else:
        evolution_summary = _generate_evolution_summary(
            old_world=old_world_prompt,
            new_world=new_world_prompt,
            consequence=consequence_summary,
            vision=vision_description,
            api_key=api_key
        )
    
    # Update state with new world prompt
    state["world_prompt"] = new_world_prompt
    
    # Add to recent events buffer (cap at 10). The turn loop may already
    # have written this beat; do not append a second line.
    event_summary = f"{player_action} -> {consequence_summary[:80]}"
    existing = state.get("recent_events") or []
    last = existing[-1] if existing else ""
    if not (last and str(last).startswith(f"{player_action} ->")):
        existing = list(existing)
        existing.append(event_summary)
        if len(existing) > 10:
            existing = existing[-10:]
        state["recent_events"] = existing
    
    # Entities: reuse the ones the single evolution call already named (run
    # through the same filter the dedicated extractor uses, so scenery / names
    # are dropped identically). Only fall back to the extractor (an extra round-
    # trip) when the combined call did not return an entities list.
    if _structured_entities is not None:
        new_entities = _filter_entities(_structured_entities)
    else:
        # One extraction over both channels. These were two calls, and while the
        # narrative and the caption were the same string it was the same request
        # twice a turn for the same answer.
        entity_source = "\n".join(
            dict.fromkeys(t for t in (consequence_summary, vision_description) if t)
        )
        new_entities = _extract_entities_from_text(entity_source, api_key=api_key)
    
    if new_entities:
        state["seen_elements"].extend(new_entities)
        # Remove duplicates, keep order
        state["seen_elements"] = list(dict.fromkeys(state["seen_elements"]))
        # Cap at 50, trim to 40 if exceeded
        if len(state["seen_elements"]) > 50:
            state["seen_elements"] = state["seen_elements"][-40:]
        print(f"[WORLD EVOLUTION V3] Added entities: {new_entities}")
    
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
        "evolution_summary": evolution_summary,
        "recent_events": state["recent_events"],
        "seen_elements": state["seen_elements"],
        # Consumed: these drifts are part of the rewritten world state now, so
        # the caller clears them and the next decision point drifts from here.
        "ambient_beats": [],
    }


def _generate_evolution_summary(
    old_world: str,
    new_world: str,
    consequence: str,
    vision: str,
    api_key: str = ""
) -> str:
    """
    Generate a short, atmospheric player-facing summary (15-25 words).
    Fast LLM call to extract most significant change.
    """
    if not api_key:
        return ""
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
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent",
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json"
            },
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}, 
                    "temperature": 0.3,
                    "maxOutputTokens": 50
                }
            },
            timeout=8
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


def _extract_entities_from_text(text: str, api_key: str = "") -> List[str]:
    """
    Extract significant physical entities (characters, objects, landmarks, threats)
    from text using LLM.
    
    PRIORITY: Characters and threats are MOST important!
    """
    if not text or len(text) < 20 or not api_key:
        return []
    
    prompt = f"""From the following text, extract a list of significant physical entities.

CRITICAL PRIORITY ORDER:
1. PEOPLE/CHARACTERS (guards, figures, silhouettes, personnel, humans, scientists, etc.) - HIGHEST PRIORITY
2. CREATURES/THREATS (mutants, infected, animals, hostile entities)
3. MAJOR OBJECTS (vehicles, equipment, weapons, doors, structures)
4. LANDMARKS (buildings, towers, unique structures)

EXCLUDE: Abstract concepts, weather, generic descriptions (walls, ground, sand, rock - unless unique)

Focus on NARRATIVE SIGNIFICANCE:
- Any person, guard, figure, silhouette = CRITICAL to include
- Any creature, mutant, threat = CRITICAL to include
- Unique objects = important
- Generic environment = skip unless unique (e.g. "breached wall" is good, "concrete wall" is generic)

CRITICAL: If NO significant entities are found, return the word "NONE" by itself.
If entities ARE found, return ONLY the comma-separated list (no labels, no "Entities:" prefix).

Text: "{text[:300]}"

Examples of GOOD responses:
- "Guard in doorway, armed figure, concrete barriers"
- "Silhouetted person, warning signs, guard tower"
- "Mutated creature, dissection table, specimen jars"
- "Two guards, patrol vehicle, checkpoint gate"
- "NONE"

PRIORITIZE CHARACTERS/THREATS OVER ENVIRONMENT!

Return ONLY the entities or "NONE":"""
    
    try:
        response = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent",
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json"
            },
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}, 
                    "temperature": 0.1,
                    "maxOutputTokens": 100
                }
            },
            timeout=8
        )
        
        if response.status_code == 200:
            result = response.json()
            entities_str = result["candidates"][0]["content"]["parts"][0]["text"].strip()
            
            # Check if response is "NONE" or empty
            if entities_str.upper() == "NONE" or not entities_str:
                return []
            
            # Split by comma, then filter/prioritize/dedupe with the shared
            # helper (also used by the single-call evolution path).
            entities = [e.strip() for e in entities_str.split(',') if e.strip()]
            return _filter_entities(entities)
        
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
