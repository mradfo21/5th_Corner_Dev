# Reference Frame Count Change: 2 → 1

## Change Summary
Reduced img2img reference frames from **2** to **1** (most recent frame only).

## Code Change
**Location:** `engine.py` line ~1035

```python
# BEFORE
num_images_to_collect = 2  # Use 2 reference images

# AFTER
num_images_to_collect = 1  # Use 1 reference image (most recent frame only)
```

## Why This Change?

### Pros of 1 Reference Frame:
- ✅ **More variation** - Less constraint on composition
- ✅ **Simpler** - Only considers most recent state
- ✅ **Faster uploads** - Only 1 image to encode/send (though still full-res)
- ✅ **Potentially less "sticky"** - Easier for major changes to happen

### Cons of 1 Reference Frame:
- ⚠️ **Less stability** - Might drift more over time
- ⚠️ **Less continuity** - Lighting/color could shift more between frames
- ⚠️ **More sensitive to prompts** - Single reference has less anchoring power

## Expected Behavior

### Before (2 Frames):
- Frame N uses references: [Frame N-1, Frame N-2]
- Strong visual continuity from multiple angles
- More "locked in" to recent aesthetic

### After (1 Frame):
- Frame N uses reference: [Frame N-1] only
- Still has continuity, but more flexible
- Prompt has stronger influence relative to reference

## Special Cases Still Apply

### Frame 1 (Intro → First Choice):
Still uses **all available references** (Frame 0 only) due to special case:
```python
if frame_idx == 1:
    ref_images_to_use = prev_img_paths_list  # Use all (Frame 0)
    print(f"🎬 FRAME 1 SPECIAL CASE")
```

### Hard Transitions:
Still use only **1 reference** (for lighting continuity only):
```python
if hard_transition:
    ref_images_to_use = prev_img_paths_list[:1]
```

Since we're now collecting only 1 frame, hard transitions effectively behave the same as normal transitions.

## Impact on Issues

### Creature Appearing/Disappearing:
- **Might happen more often** - Less visual "memory" from previous frames
- **But** world simulator still tracks persistent elements
- Text prompts still strong influence on what appears

### "1993" Text Overlay:
- **Unaffected** - This is a Gemini behavior issue, not related to reference count
- Need stronger anti-timecode prompting to fix

### Visual Consistency:
- **Might be less stable** - Only 1 frame of reference
- **Monitor for issues** - If drift becomes problematic, can revert to 2

## Verification

After deploying, check logs for:
```
[IMG2IMG COLLECT] Collecting up to 1 reference image(s)  ← Changed from 2
[IMG2IMG COLLECT] ✅ Collected 1 references, stopping search
🔷 [GOOGLE GEMINI] Image editing mode with 1 reference image(s)  ← Changed from 2
🔷 [GOOGLE GEMINI] Reference image 1: frame_XXX.png (full-res for quality)
```

## If Issues Arise

Can easily revert by changing back to:
```python
num_images_to_collect = 2
```

Or make it configurable:
```python
# At top of engine.py
GEMINI_IMG2IMG_REFERENCE_COUNT = 1  # Adjustable setting
```

---

**Status:** ✅ Changed to 1 reference frame
**Reason:** User preference for more variation
**Tests:** All passing
**Risk:** Low (easily reversible)


