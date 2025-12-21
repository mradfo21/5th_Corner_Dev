# 4-Panel Flipbook Feature Design

## What Happened
Gemini *accidentally* generated a 4-panel comic/storyboard layout showing progressive action.
User reaction: "I think this is actually... great.. it tells more story tbh. more action."

## Why It's Powerful
- **Sequential storytelling**: Shows action progression (4 beats of a scene)
- **More narrative density**: One image tells a mini-story
- **Cinematic**: Like comic panels or storyboards
- **Natural for action**: Sprint, explosion, creature attack all have clear beats

## Design Goals
1. Make 4-panel generation **consistent and predictable**
2. Add toggle in Discord ("Flipbook Mode" button)
3. Ensure panels show **progression** (beat 1 → beat 2 → beat 3 → beat 4)
4. Eventually: compile panels into animated flipbook video

## Prompt Strategy

### Current Blocker
```
"THIS MUST BE ONE IMAGE, NOT A GRID, PANEL, SEQUENCE, OR STORYBOARD."
```
This PREVENTS 4-panel outputs!

### Proposed Flipbook Prompt Addition
```
FLIPBOOK MODE - 4-PANEL SEQUENTIAL STORYTELLING:

Generate a 4-panel comic/storyboard layout showing the ACTION PROGRESSING across 4 distinct beats.

PANEL LAYOUT (2x2 grid):
┌─────────┬─────────┐
│ PANEL 1 │ PANEL 2 │  Top row: Setup → Escalation
├─────────┼─────────┤
│ PANEL 3 │ PANEL 4 │  Bottom row: Peak → Aftermath
└─────────┴─────────┘

PANEL CONTENT STRUCTURE:
• Panel 1 (Top-Left): INITIAL MOMENT - Action begins, threat visible
• Panel 2 (Top-Right): ESCALATION - Action intensifies, movement progresses
• Panel 3 (Bottom-Left): PEAK ACTION - Maximum intensity, climax of event
• Panel 4 (Bottom-Right): CONSEQUENCE - Immediate aftermath, new situation

CRITICAL RULES:
- Each panel is a DISTINCT moment (0.5-1 second apart)
- Show SPATIAL PROGRESSION (camera/subject moves between panels)
- Maintain SAME PERSPECTIVE across all 4 panels (first-person POV)
- Each panel is 1993 VHS footage aesthetic (grain, degradation)
- NO panel borders, NO numbers, NO text - just 4 raw images in grid
- Panels flow as ONE CONTINUOUS ACTION SEQUENCE

EXAMPLES:

Action: "Sprint toward burning compound"
• Panel 1: Starting position, compound visible ahead
• Panel 2: Mid-sprint, closer to compound, smoke thickening
• Panel 3: Nearly at entrance, heat waves visible, flames intense
• Panel 4: At threshold, looking into burning interior

Action: "Creature lunges at you"
• Panel 1: Creature in distance, beginning lunge motion
• Panel 2: Creature mid-air, closing distance rapidly
• Panel 3: Creature's claws/face filling frame, impact imminent
• Panel 4: Aftermath - on ground, creature looming above

Action: "Guard opens fire"
• Panel 1: Guard raises rifle, aiming
• Panel 2: Muzzle flash, you diving for cover
• Panel 3: Bullets impacting nearby, debris flying
• Panel 4: Behind cover, dust settling, guard reloading

Each panel is PHOTOGRAPHIC, GRITTY, VHS-DEGRADED - not clean comic art.
Think: Security camera sequential frames, surveillance montage, real action sequence.
```

## Implementation Plan

### Phase 1: Core Toggle
- [ ] Add `flipbook_mode` boolean to game state (default: False)
- [ ] Add Discord button to toggle flipbook mode on/off
- [ ] Modify image generation prompt based on mode

### Phase 2: Prompt Refinement
- [ ] Create separate prompt template for flipbook mode
- [ ] Test and refine 4-panel consistency
- [ ] Ensure panels tell coherent progressive story

### Phase 3: UI/UX
- [ ] Display mode indicator in Discord ("🎬 FLIPBOOK MODE")
- [ ] Show evolution summary with panel indicator
- [ ] Admin dashboard: Show flipbook images in grid layout

### Phase 4: Video Generation (Future)
- [ ] Extract 4 panels from generated image
- [ ] Sequence panels into short video (1 second per panel)
- [ ] Add VHS-style transitions between panels
- [ ] Generate 4-second micro-movies from flipbook frames

## Technical Considerations

### Aspect Ratio
- Current: 4:3 or 16:9 single image
- Flipbook: Keep same overall ratio, but internally divided into 4 panels
- Each panel is roughly square-ish

### Continuity
- Img2img still works: Previous flipbook's PANEL 4 becomes reference for next generation
- Or use ALL 4 panels as reference (multi-image input)

### Performance
- Flipbook generation might be slower (more complex prompt)
- Consider making it opt-in only (not default)

### Fallback
- If Gemini can't generate 4-panel, falls back to single image
- Detection: Check if output has grid-like structure

## User Control

### Discord Interface
```
🎬 Flipbook: OFF  [Toggle]
⚡ Free Will
🎒 Inventory
```

When ON:
```
🎬 Flipbook: ON  [Toggle]
⚡ Free Will
🎒 Inventory
```

### Admin Dashboard
Show flipbook frames in 2x2 grid layout when detected

## Example Prompt Modification

### In `_gen_image()` (engine.py):
```python
if state.get("flipbook_mode", False):
    # Use flipbook prompt template
    prompt_str = build_flipbook_image_prompt(
        player_choice=choice,
        dispatch=caption,
        prev_vision_analysis=prev_vision_analysis
    )
else:
    # Use standard single-frame prompt
    prompt_str = build_image_prompt(...)
```

### New Function:
```python
def build_flipbook_image_prompt(...):
    """Build 4-panel sequential prompt"""
    base_prompt = f"""
    Action: {player_choice}
    Result: {dispatch}
    
    FLIPBOOK MODE - Generate 4-panel progression:
    Panel 1: Initial moment as action begins
    Panel 2: Action escalates and progresses  
    Panel 3: Peak intensity of the action
    Panel 4: Immediate consequence and aftermath
    
    Show natural camera progression across panels.
    Each panel is 1993 VHS aesthetic.
    """
    return base_prompt
```

## Open Questions

1. **Default ON or OFF?**
   - Suggestion: OFF by default, let users discover and toggle
   - Avoids breaking existing gameplay flow

2. **Per-turn or persistent?**
   - Suggestion: Persistent toggle (stays on until turned off)
   - Store in game state: `flipbook_mode: true/false`

3. **Choice-specific flipbook?**
   - Some actions are better for flipbook (sprint, attack, explosions)
   - Others less so (observe, wait, crouch)
   - Auto-enable for high-action choices? Or always respect user toggle?

4. **Aspect ratio change?**
   - Keep 16:9 but show 4 panels within it?
   - Or use 1:1 square for cleaner 2x2 grid?

## Next Steps

1. **Prototype the prompt** - Test if Gemini consistently generates 4-panel with explicit instructions
2. **Add toggle** - Simple boolean in state + Discord button
3. **Test consistency** - Does it reliably create 4 coherent panels?
4. **Refine prompts** - Iterate based on what actually generates
5. **Ship it** - Deploy for user testing

## Expected Impact

- **More immersive**: Seeing action unfold feels cinematic
- **Better storytelling**: 4 beats instead of 1 static moment
- **Unique feature**: No other text adventure does this
- **Foundation for video**: Panels → animated flipbook in future

