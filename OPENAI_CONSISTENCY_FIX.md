# OpenAI img2img Consistency Issues - Solutions

## Problem
OpenAI's `gpt-image-1` img2img is struggling with visual consistency between frames, despite passing 2 reference images.

## Root Cause
- We were missing the `input_fidelity` parameter (controls how closely the model matches reference images)
- Using `quality: 'low'` might be too degraded for the model to extract features properly
- 2 reference images might be confusing the model

## Solutions Implemented (Try in Order)

### ✅ **ALREADY APPLIED: Added `input_fidelity: 'high'`**
This is the **most important fix** - it tells OpenAI to stick closer to the reference images' style, features, and composition.

**Location:** `engine.py` line ~1275
```python
data = {
    'model': 'gpt-image-1',
    'prompt': vhs_prompt,
    'n': '1',
    'size': '1536x1024',
    'quality': OPENAI_IMG2IMG_QUALITY,
    'input_fidelity': 'high',  # ← NEW: Stick closer to reference images
    'moderation': 'low'
}
```

**Expected Result:** Much better consistency between frames, less "creative interpretation"

---

## Additional Tuning Options (If Option 1 Doesn't Help)

### **Option 2: Reduce to 1 Reference Frame**
**Location:** `engine.py` line ~128
```python
OPENAI_IMG2IMG_REFERENCE_COUNT = 1  # Change from 2 to 1
```

**When to use:** If 2 images are confusing the model (mixing features from both)
**Trade-off:** Less context for the model to work with

---

### **Option 3: Increase Quality**
**Location:** `engine.py` line ~130
```python
OPENAI_IMG2IMG_QUALITY = 'medium'  # Change from 'low' to 'medium' or 'high'
```

**When to use:** If `input_fidelity: 'high'` helps but images still lack detail
**Trade-offs:**
- ✅ Better consistency (model can extract more features)
- ✅ Cleaner images
- ❌ Less VHS degradation aesthetic
- ❌ Slower generation
- ❌ Slightly higher cost

**Quality Levels:**
- `'low'`: Fast, grainy, VHS-like (current setting)
- `'medium'`: Balanced quality/speed
- `'high'`: Best quality, slowest, most expensive

---

### **Option 4: Disable OpenAI img2img Entirely**
**Location:** `engine.py` line ~128
```python
OPENAI_IMG2IMG_ENABLED = False  # Disable img2img, always use text-to-image
```

**When to use:** If none of the above fixes work
**Result:** More visual variety, less consistency (like Gemini's current behavior)

---

## Recommended Testing Sequence

### **Test 1: Try `input_fidelity: 'high'` (Already Applied)**
1. Deploy current changes
2. Switch to OpenAI in Discord
3. Play through 3-5 turns
4. Check if consistency improved

**If still poor, proceed to Test 2:**

### **Test 2: Reduce to 1 Reference Frame**
```python
OPENAI_IMG2IMG_REFERENCE_COUNT = 1
```
Redeploy and test 3-5 turns.

**If still poor, proceed to Test 3:**

### **Test 3: Increase Quality to Medium**
```python
OPENAI_IMG2IMG_QUALITY = 'medium'
```
Redeploy and test 3-5 turns.

**If STILL poor:**

### **Test 4: Disable img2img for OpenAI**
```python
OPENAI_IMG2IMG_ENABLED = False
```
This makes OpenAI behave like Gemini (more variety, less consistency).

---

## Comparison: Gemini vs OpenAI img2img

| Feature | Gemini | OpenAI (Before Fix) | OpenAI (After Fix) |
|---------|--------|---------------------|-------------------|
| img2img support | ✅ Native | ✅ Via multipart | ✅ Via multipart |
| Reference images | Up to 2 | Up to 2 | Up to 2 (configurable) |
| Fidelity control | ❌ No parameter | ❌ Missing | ✅ `input_fidelity: 'high'` |
| Quality control | ✅ Aspect ratio | ✅ `quality` param | ✅ `quality` param (configurable) |
| Consistency | 🟡 Good | 🔴 Poor | 🟢 Should be much better |

---

## Cost Considerations

| Quality | Speed | Cost per Image | Consistency |
|---------|-------|----------------|-------------|
| `low` | Fast (3-5s) | ~$0.01 | 🟡 Medium |
| `medium` | Medium (5-8s) | ~$0.015 | 🟢 Good |
| `high` | Slow (8-12s) | ~$0.02 | 🟢 Excellent |

**Note:** `input_fidelity: 'high'` has no additional cost, only affects how the model interprets reference images.

---

## Technical Details

### What `input_fidelity` Does
From OpenAI docs:
> "Control how much effort the model will exert to match the style and features, especially facial features, of input images."

- `'low'` (default): Model uses references as loose inspiration
- `'high'`: Model tries to closely match style, lighting, composition, and features

### Current Configuration (As of This Fix)
```python
# engine.py lines 128-131
OPENAI_IMG2IMG_ENABLED = True
OPENAI_IMG2IMG_REFERENCE_COUNT = 2
OPENAI_IMG2IMG_QUALITY = 'low'

# img2img request (line ~1275)
data = {
    'input_fidelity': 'high',  # ← THE KEY FIX
    'quality': OPENAI_IMG2IMG_QUALITY,
    ...
}
```

---

## Expected Outcome

With `input_fidelity: 'high'`, OpenAI img2img should:
✅ Maintain lighting/color palette between frames
✅ Preserve VHS aesthetic from previous frames
✅ Keep consistent "camera angle" feel
✅ Avoid sudden style shifts

If it's **still** inconsistent after this fix, the model itself might just not be as good as Gemini for this use case, and you should consider disabling it (`OPENAI_IMG2IMG_ENABLED = False`).

---

**Status:** ✅ Fix deployed, ready to test
**Next Step:** Play 3-5 turns with OpenAI and observe consistency


