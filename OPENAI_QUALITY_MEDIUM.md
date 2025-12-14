# OpenAI Quality Increased to Medium

## Changes Applied

### 1. **Increased Quality Setting to 'medium'**
**Location:** `engine.py` line ~130
```python
OPENAI_IMG2IMG_QUALITY = 'medium'  # Changed from 'low' to 'medium'
```

### 2. **Applied to Both img2img and text-to-image**
- **img2img** (line ~1276): Uses `OPENAI_IMG2IMG_QUALITY` variable
- **text-to-image** (line ~1360): Now also uses `OPENAI_IMG2IMG_QUALITY` variable

Both modes now generate at **medium quality** for consistency.

---

## Current Configuration

```python
# OpenAI img2img settings
OPENAI_IMG2IMG_ENABLED = True
OPENAI_IMG2IMG_REFERENCE_COUNT = 2  # Using 2 reference frames
OPENAI_IMG2IMG_QUALITY = 'medium'  # ← CHANGED from 'low'

# img2img request parameters
data = {
    'model': 'gpt-image-1',
    'quality': 'medium',  # ← Better detail extraction
    'input_fidelity': 'high',  # ← Stick closer to references
    'size': '1536x1024',
    ...
}

# text-to-image request parameters
response = client.images.generate(
    model='gpt-image-1',
    quality='medium',  # ← Matches img2img quality
    size='1536x1024',
    ...
)
```

---

## Expected Results

### ✅ **Improvements**
1. **Better consistency** between frames
   - Model can extract more detail from reference images
   - Cleaner feature matching with `input_fidelity: 'high'`

2. **Higher quality images**
   - Less compression artifacts
   - Sharper details
   - Better color fidelity

3. **Still maintains VHS aesthetic**
   - Medium quality is a good balance
   - Not as "clean" as high quality
   - VHS prompting still applies degradation effects

### ⚠️ **Trade-offs**
1. **Slightly slower generation**
   - Low: ~3-5 seconds
   - Medium: ~5-8 seconds
   - Still acceptable for Discord UX

2. **Slightly higher cost**
   - Low: ~$0.01 per image
   - Medium: ~$0.015 per image
   - About 50% more expensive

3. **Less "natural" VHS degradation**
   - Images will be cleaner than real VHS
   - But prompting should still add grain/artifacts

---

## Testing Plan

1. **Switch to OpenAI** in Discord
2. **Play 3-5 turns** and observe:
   - Visual consistency between frames
   - Lighting/color palette stability
   - Overall image quality
3. **Check logs** for:
   ```
   [OPENAI IMG2IMG] ✅ Edit complete with 2 reference(s)
   ```
4. **Compare to Gemini** for consistency

---

## If Consistency Still Poor

Try these in order:

### Option A: Reduce to 1 Reference Frame
```python
OPENAI_IMG2IMG_REFERENCE_COUNT = 1
```

### Option B: Increase to High Quality
```python
OPENAI_IMG2IMG_QUALITY = 'high'
```
(May lose too much VHS aesthetic)

### Option C: Disable img2img
```python
OPENAI_IMG2IMG_ENABLED = False
```
(Fall back to text-to-image only, like Gemini)

---

## Status

✅ **Applied:** `input_fidelity: 'high'` + `quality: 'medium'`
✅ **Tests:** All passing
✅ **Ready:** For deployment

**Next Step:** Deploy and test in production with 3-5 turns.

