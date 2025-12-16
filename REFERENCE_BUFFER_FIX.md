# 🔧 Reference Buffer Fix - Hard Transition Reset

## The Problem

When using img2img with 2 reference frames, a bug occurred where frames from **before** a hard transition were leaking into frames **after** the transition:

### Observed Pattern
```
Frame 1: Normal scene (desert)
Frame 2: Uses Frames 0+1 (desert) ✅
Frame 3: HARD TRANSITION (enters building) ← Location change detected
Frame 4: Uses Frames 2+3 ❌ Problem! Frame 2 is still desert!
```

**Result:** Frame 4 blends desert aesthetics with building interior, creating visual inconsistency.

## Root Cause

The reference collection logic blindly grabbed the last N frames from history without considering whether a **hard transition (location change)** occurred between them:

```python
# OLD CODE - Blindly collects last 2 frames
for idx, entry in enumerate(reversed(history)):
    if entry.get("image"):
        last_imgs.append(entry)
    if len(last_imgs) == 2:
        break  # Always stops at 2, regardless of transitions
```

## The Solution

Implemented a **"reference buffer"** that automatically resets on hard transitions:

### 1. Tag Each History Entry
Added `hard_transition` flag to track location changes:
```python
history_entry = {
    "choice": choice,
    "image": image_url,
    "image_prompt": prompt,
    "hard_transition": hard_transition,  # NEW!
    ...
}
```

### 2. Stop Collection at Hard Transitions
Modified reference collection to stop when it encounters a hard transition:
```python
# NEW CODE - Stops at location changes
for idx, entry in enumerate(reversed(history)):
    if entry.get("image"):
        last_imgs.append(entry)
        
        # CRITICAL: Stop after collecting a hard transition frame
        if entry.get("hard_transition", False) and len(last_imgs) > 0:
            print("[IMG2IMG] 🚧 HARD TRANSITION - Reference buffer reset")
            break  # Don't collect frames from before location change
    
    if len(last_imgs) == 2:
        break  # Normal limit still applies
```

## How It Works

The "reference buffer" now automatically resets on location changes:

### New Pattern (Fixed)
```
Frame 1: Normal scene (desert)
Frame 2: Uses Frames 1+0 (desert) ✅
Frame 3: HARD TRANSITION (enters building) - uses Frames 2+1 (desert frames)
         ↓ Frame 3 is tagged with hard_transition=True
Frame 4: Sees Frame 3 was hard_transition
         → Stops collecting at Frame 3
         → Only uses Frame 3 (building) ✅
Frame 5: Uses Frames 4+3 (both building) ✅
```

**Result:** After a location change, only frames from the **new location** are used as references.

## Benefits

✅ **Visual Continuity Within Scenes:** Normal movements still use 2 reference frames for smooth transitions  
✅ **Clean Transitions Between Locations:** Location changes don't pollute the reference buffer  
✅ **Automatic Reset:** No manual tracking needed - the buffer resets itself  
✅ **Always Has References:** Always collects at least 1 frame (most recent)  

## Implementation Details

### Files Modified
- `engine.py`:
  - Added `hard_transition` flag to state storage (line ~1715)
  - Added `hard_transition` to `advance_turn_image_fast` return dict (line ~2841)
  - Added `hard_transition` to both history entry creation points (lines ~2036, ~2906)
  - Modified reference collection logic to stop at hard transitions (line ~1050)

### Logging
New debug output shows when buffer resets:
```
[IMG2IMG COLLECT] History[0]: image=True, hard_transition=False
[IMG2IMG COLLECT]   → ✅ Added to reference list (total: 1)
[IMG2IMG COLLECT] History[1]: image=True, hard_transition=True
[IMG2IMG COLLECT]   → ✅ Added to reference list (total: 2)
[IMG2IMG COLLECT] 🚧 HARD TRANSITION DETECTED - Stopping collection here
[IMG2IMG COLLECT] Reference buffer reset at location change
```

## Testing

✅ All pre-deployment tests passing  
✅ No linter errors  
✅ Backwards compatible (old history entries without `hard_transition` default to `False`)  

## Deployment

Ready to deploy. After deployment, watch for the new logging:
- `[IMG2IMG COLLECT] Will stop at last hard transition (location change)`
- `[IMG2IMG COLLECT] 🚧 HARD TRANSITION DETECTED - Stopping collection here`

This confirms the reference buffer is resetting correctly.

