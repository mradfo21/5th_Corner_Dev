# CRITICAL: World Evolution System Needs Redesign

## Problem Analysis

### Issue 1: Evolution History is Lost on Reset
- `reset_state()` deletes state.json, history.json, all images
- No way to inspect world evolution across sessions
- Evolution debugging impossible

### Issue 2: World Evolution is REPLACING, not ACCUMULATING
Current behavior:
```
Turn 0: [2000-word rich world state]
Turn 1: "A low metallic hum" (ENTIRE state replaced!)
Turn 2: "Wind kicks up dust" (Previous hum lost!)
```

**Root cause:** `prompts/simulation_prompts.json` line 16:
```json
"world_evolution_instructions": "Write ONE short sentence describing a minor atmospheric change"
```

Then `evolve_world_state()` does:
```python
state["world_prompt"] = new_world_prompt  # REPLACES entire state with one sentence!
```

## The Solution

### Option A: Accumulative Evolution (Recommended)
Keep a GROWING world buffer that accumulates context:

```python
# Instead of replacing:
state["world_prompt"] = new_world_prompt

# Accumulate and summarize:
current_context = state.get("world_prompt", "")
new_event = llm_generate_new_event(...)

# Append new event
full_context = f"{current_context}\n{new_event}"

# If too long (>500 words), ask LLM to condense while preserving key elements
if len(full_context.split()) > 500:
    full_context = llm_condense_context(full_context, seen_elements)

state["world_prompt"] = full_context
```

**Result:**
```
Turn 0: "Desert facility. Red mesas. Abandoned."
Turn 1: "Desert facility. Red mesas. Abandoned. Metallic hum from machinery."
Turn 2: "Desert facility. Red mesas. Abandoned. Metallic hum from machinery. Guard patrol visible near east gate."
Turn 3: "Desert facility. Red mesas. Abandoned. Machinery active. Guards patrolling. Player near perimeter fence."
```

### Option B: Structured World State
Use a structured dict instead of a single string:

```python
state["world_state"] = {
    "location": "Outside facility perimeter, east side",
    "immediate_threats": ["Guard patrol 50m away", "Exposed position"],
    "environment": "Desert, red mesas, 7:17pm, overcast",
    "ongoing_events": ["Machinery hum", "Patrol route active"],
    "known_elements": ["Fence", "Guard tower", "East gate", "Vehicle"],
    "recent_changes": ["Player moved to fence", "Guards detected movement"]
}
```

Then `world_prompt` is generated FROM this structure for LLM context.

### Option C: Separate Buffers
- `world_prompt`: Core unchanging setting (desert, facility, 1993, etc.)
- `current_situation`: Dynamic evolving state (location, threats, events)
- `recent_events`: Last 5 turns of major changes

```python
state = {
    "world_prompt": "[STATIC SETTING - never changes]",
    "current_situation": "[EVOLVING STATE - updates each turn]",
    "recent_events": ["Turn 5: Guards alerted", "Turn 4: Door opened", ...]
}
```

## Persistence Across Resets

Add a `world_evolution_archive.json` that is NEVER deleted:

```python
# In evolve_world_state():
archive_path = ROOT / "logs" / "world_evolution_archive.json"
archive_entry = {
    "session_id": session_id,
    "turn": turn_count,
    "timestamp": datetime.now().isoformat(),
    "world_prompt_before": old_world,
    "world_prompt_after": new_world_prompt,
    "player_action": last_choice,
    "consequence": consequence_summary,
    "vision_analysis": vision_description
}

# Append to archive (never cleared by reset)
if archive_path.exists():
    with open(archive_path, 'r') as f:
        archive = json.load(f)
else:
    archive = []

archive.append(archive_entry)

with open(archive_path, 'w') as f:
    json.dump(archive, f, indent=2)
```

Now you can inspect EVERY world evolution across ALL sessions, even after reset!

## Recommended Implementation

**Phase 1: Fix Accumulation (Quick Fix)**
1. Change prompt to say "Add ONE sentence to the current world state"
2. Change code to append instead of replace
3. Add condensation when state gets too long

**Phase 2: Add Archive (Debugging)**
1. Create world_evolution_archive.json
2. Log every evolution (never cleared)
3. Add admin dashboard view for archive

**Phase 3: Structured State (Long-term)**
1. Redesign state structure to separate static/dynamic
2. Migrate existing state to new structure
3. Update LLM prompts to work with structure

## What To Do Right Now

1. **Inspect current state file** to see how bad the replacement is
2. **Check world_evolution.log** to see the before/after states
3. **Decide** which option to pursue (A, B, or C)
4. **Implement** the fix
5. **Test** with a 10-turn game to verify accumulation works

