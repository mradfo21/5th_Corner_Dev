# 🔧 Prompt Storage Fix - Undefined Variable Bug

## The Problem

Prompt storage was failing with no `image_prompt` in history entries:

```
🧪 TEST: Image Prompt Storage
⚠️ No image_prompt found in previous turn
Choice: 🏃‍♂️ Maintain the flame barrier
```

## Root Cause

In `advance_turn_image_fast` (line ~2816), `consequence_img_prompt` was never initialized:

```python
# BUGGY CODE
consequence_img_url = None  # Initialized
try:
    consequence_img_url, consequence_img_prompt = _gen_image(...)  # Assigned here
    print(f"✅ [IMG FAST] Image ready: {consequence_img_url}")
except Exception as e:
    print(f"❌ [IMG FAST] Error: {e}")

return {
    "consequence_image_prompt": consequence_img_prompt,  # ← UNDEFINED if exception or None!
    ...
}
```

If `_gen_image()` returned `(None, "")`, the unpacking would fail silently, or if there was any path where the assignment didn't happen, `consequence_img_prompt` would be undefined when constructing the return dictionary.

## The Fix

1. **Initialize the variable** before the try block
2. **Check result before unpacking** to handle None case
3. **Add better error logging** with traceback

```python
# FIXED CODE
consequence_img_url = None
consequence_img_prompt = ""  # Initialize to prevent undefined variable error

try:
    result = _gen_image(...)
    if result:  # Check before unpacking
        consequence_img_url, consequence_img_prompt = result
    print(f"✅ [IMG FAST] Image ready: {consequence_img_url}")
except Exception as e:
    print(f"❌ [IMG FAST] Error: {e}")
    import traceback
    traceback.print_exc()  # Better error diagnosis
```

## Why This Matters

This bug prevented the entire **Veo Films** system from working because:
- ❌ No prompts were being stored in `history.json`
- ❌ Frame interpolation would have no prompt data
- ❌ Silent failure - no error messages

Now:
- ✅ Prompts always stored (empty string if generation fails)
- ✅ Safe unpacking with result check
- ✅ Better error logging for diagnosis

## Verified Fixes

Checked all 5 call sites of `_gen_image()`:
1. ✅ `_process_turn_background` - Safe (unpacks directly, no try-except issues)
2. ✅ `begin_tick` intro - Safe (unpacks directly)
3. ✅ `advance_turn_image_fast` - **FIXED** (was the bug)
4. ✅ `begin_tick_fast` - Safe (unpacks directly)
5. ✅ `begin_tick` old path - Safe (unpacks directly)

## Testing

✅ All pre-deployment tests passing  
✅ No linter errors  
✅ Variable always defined before use

## Deployment

Deploy and verify with test beat - should now show green success with prompt preview instead of orange warning.

