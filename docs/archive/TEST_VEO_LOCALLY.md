# 🧪 Testing Veo 3.1 Locally

Complete guide for testing Veo video-based image generation before deployment.

---

## Quick Start

```bash
# Run the test suite
python test_veo_local.py
```

---

## What Gets Tested

### **1. Prerequisites** ✅
- Python 3.8+
- GEMINI_API_KEY set
- ffmpeg installed
- Required directories exist
- Required packages installed

### **2. Module Import** ✅
- `veo_video_utils.py` imports successfully
- All required functions present

### **3. Prompt Conversion** ✅
- Game prompts → Cinematic video prompts
- Verifies key elements included

### **4. Seed Frame Generation** ✅ (Makes API Call)
- Generates Frame 0 with Gemini
- Verifies file created and valid
- **Cost:** ~$0.01 (or free in free tier)

### **5. Video Generation** ⚠️ (Optional, Makes API Call)
- Generates 8-second video with Veo 3.1
- Extracts last frame
- Verifies video and frame saved
- **Cost:** ~$0.10-0.50
- **Time:** 20 seconds to 6 minutes

### **6. Engine Integration** ✅
- Verifies routing in `_gen_image()`
- Checks `ai_config.json` preset

---

## Step-by-Step Testing

### **Step 1: Check Prerequisites**

```bash
# Check Python version
python --version  # Need 3.8+

# Check API key
echo $GEMINI_API_KEY  # Should show your key

# Check ffmpeg
ffmpeg -version  # Should show version info

# If ffmpeg missing:
sudo apt-get install ffmpeg  # Ubuntu/Debian
brew install ffmpeg           # macOS
```

### **Step 2: Run Test Suite**

```bash
# From project root
python test_veo_local.py
```

**Expected Output:**
```
╔════════════════════════════════════════════════════════════════════╗
║                  VEO 3.1 LOCAL TEST SUITE                         ║
║                                                                    ║
║  Testing Veo video-based image generation before deployment       ║
╚════════════════════════════════════════════════════════════════════╝

======================================================================
TEST 1: Prerequisites
======================================================================

Checking Python version...
✅ Python 3.11.5

Checking GEMINI_API_KEY...
✅ API key loaded: AIzaSyDY0-pt3XMLG79b...fjuaarn8

Checking ffmpeg installation...
✅ ffmpeg installed: ffmpeg version 6.0

Checking directories...
✅ Directory exists: images/
✅ Directory exists: films/segments/

Checking Python packages...
✅ Package installed: requests

======================================================================
TEST 2: Module Import
======================================================================

Importing veo_video_utils...
✅ veo_video_utils imported successfully

Checking functions...
✅ Function exists: generate_frame_via_video
✅ Function exists: _build_veo_cinematic_prompt
✅ Function exists: _generate_video_and_extract_frame
✅ Function exists: _extract_last_frame

...
```

### **Step 3: Review Results**

**If all tests pass:**
```
✅ ALL TESTS PASSED - SAFE TO DEPLOY

To enable Veo in production:
  1. Edit ai_config.json
  2. Set: "image_provider": "veo"
  3. Deploy and restart bot
```

**If tests fail:**
- Review error messages
- Fix issues (see Troubleshooting below)
- Re-run tests

---

## Test Modes

### **Fast Mode (Skips Veo API)**

Test everything except actual video generation:

```bash
# When prompted "Continue with Veo test?", answer: no
```

This tests:
- ✅ Prerequisites
- ✅ Module import
- ✅ Prompt conversion
- ✅ Seed frame (Gemini - fast & cheap)
- ⏭️ Video generation (skipped)
- ✅ Integration check

**Use when:** Just want to verify code structure

---

### **Full Mode (Includes Veo API)**

Test everything including actual video generation:

```bash
# When prompted "Continue with Veo test?", answer: yes
```

**Warning:**
- Makes real Veo API call
- Costs ~$0.10-0.50
- Takes 20 seconds to 6 minutes
- Creates actual video file

**Use when:** Ready to verify Veo API works

---

## Troubleshooting

### **"GEMINI_API_KEY not set"**

```bash
# Option 1: Environment variable
export GEMINI_API_KEY="your_key_here"

# Option 2: Add to config.json
{
  "GEMINI_API_KEY": "your_key_here"
}
```

### **"ffmpeg not found"**

```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install ffmpeg

# macOS
brew install ffmpeg

# Windows
# Download from: https://ffmpeg.org/download.html
```

### **"Module import failed"**

```bash
# Check file exists
ls -la veo_video_utils.py

# Check for syntax errors
python -m py_compile veo_video_utils.py

# Re-run with verbose errors
python test_veo_local.py
```

### **"Seed frame generation failed"**

- Check API key is valid
- Check internet connection
- Check Gemini API quota (free tier: 15 requests/min)
- Wait 1 minute and retry

### **"Video generation timeout"**

- Veo can take up to 6 minutes during peak hours
- Check internet connection
- Retry during off-peak hours
- Or use fast model: Set `USE_FAST_MODEL = True` in `veo_video_utils.py`

### **"Frame extraction failed"**

- Verify video file exists: `ls films/segments/`
- Check video file is valid: `ffmpeg -i films/segments/seg_0_1_*.mp4`
- Check ffmpeg version: `ffmpeg -version` (need 4.0+)
- Check disk space: `df -h`

---

## Manual Testing

### **Test 1: Generate Single Frame**

```python
# test_single_frame.py
from veo_video_utils import generate_frame_via_video

# Generate seed
frame0, prompt0 = generate_frame_via_video(
    prompt="Desert facility at sunset, VHS quality",
    first_frame_path=None,
    caption="manual_test",
    frame_idx=0,
    world_prompt="Test",
    action_context="Initialize"
)

print(f"Frame 0: {frame0}")
```

### **Test 2: Generate Video Segment**

```python
# test_video_segment.py
from veo_video_utils import generate_frame_via_video

# Assuming frame0 exists from Test 1
frame1, prompt1 = generate_frame_via_video(
    prompt="Walking forward through desert, camera moving",
    first_frame_path=frame0,  # Use frame0 from above
    caption="manual_test_video",
    frame_idx=1,
    world_prompt="Test",
    action_context="Move forward"
)

print(f"Frame 1: {frame1}")
print(f"Video: films/segments/seg_0_1_*.mp4")
```

### **Test 3: Check Integration**

```python
# test_integration.py
import engine
import ai_provider_manager

# Check current provider
current = ai_provider_manager.get_image_provider()
print(f"Current provider: {current}")

# Test switching to Veo
import json
with open("ai_config.json", "r") as f:
    config = json.load(f)

config["image_provider"] = "veo"

with open("ai_config.json", "w") as f:
    json.dump(config, f, indent=2)

print("Switched to Veo provider")
```

---

## Verification Checklist

Before deploying, verify:

- [ ] `test_veo_local.py` passes all tests
- [ ] `films/segments/` directory exists
- [ ] Seed frame generates successfully (Gemini)
- [ ] Video segment generates successfully (Veo) - if tested
- [ ] Last frame extracts from video (ffmpeg)
- [ ] Files are correct format (.mp4 video, .png frame)
- [ ] `ai_config.json` has "veo" preset
- [ ] `_gen_image()` has Veo routing
- [ ] No linter errors: `python test_before_deploy.py`

---

## Cost Estimate

**For local testing:**
- Seed frame (Gemini): $0.00 - $0.01
- One video segment (Veo): $0.10 - $0.50
- **Total:** ~$0.10 - $0.51

**For production use:**
- 9-frame run = 1 seed + 8 videos
- **Total:** ~$0.80 - $4.00 per game

---

## Next Steps

### **After Tests Pass:**

1. **Enable in config:**
   ```json
   {
     "image_provider": "veo"
   }
   ```

2. **Deploy to production**

3. **Monitor logs:**
   ```
   [VEO] Generating frame 1 via video interpolation
   [VEO API] Sending request to veo-3.1-generate-preview...
   [VEO API] Operation started: operations/abc123
   [VEO API] Still generating... (20s elapsed)
   [VEO API] ✅ Complete after 45s
   [VEO] Video saved: films/segments/seg_0_1_1234.mp4 (4.2 MB)
   [VEO FFMPEG] ✅ Frame extracted: images/frame_1.png
   ```

4. **Verify in Discord:**
   - Make 2-3 choices
   - Check consistency between frames
   - Check `films/segments/` for videos
   - Frames should show smooth transitions

### **If Issues in Production:**

1. **Check logs** for Veo errors
2. **Fallback automatically happens** - game won't break
3. **Switch back to Gemini** if needed:
   ```json
   {
     "image_provider": "gemini"
   }
   ```

---

## Support

**Issues?**
- Review test output carefully
- Check error messages in red
- See troubleshooting section above
- Test individual components manually

**Questions?**
- See `VEO_VIDEO_BASED_IMAGE_GEN.md` for full documentation
- See `veo_video_utils.py` source code
- Check Veo 3.1 API docs: https://ai.google.dev/gemini-api/docs/video



