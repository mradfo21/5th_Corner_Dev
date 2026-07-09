# 🚀 Quick Test: Veo Integration

## One Command

```bash
python test_veo_local.py
```

---

## What It Tests

✅ Prerequisites (Python, ffmpeg, API key)  
✅ Module import  
✅ Prompt conversion  
✅ Seed frame generation (Gemini - $0.01)  
⚠️ Video generation (Veo - $0.10-0.50) - **Optional, asks permission**  
✅ Engine integration  

---

## Expected Output (Success)

```
╔════════════════════════════════════════════════════════════════════╗
║                  VEO 3.1 LOCAL TEST SUITE                         ║
╚════════════════════════════════════════════════════════════════════╝

TEST 1: Prerequisites
✅ Python 3.11.5
✅ API key loaded
✅ ffmpeg installed
✅ Directories created
✅ Packages installed

TEST 2: Module Import
✅ veo_video_utils imported
✅ All functions present

TEST 3: Prompt Conversion
✅ Cinematic prompt generated

TEST 4: Seed Frame Generation
✅ Seed frame generated: /images/frame_0_test_seed.png
✅ File exists: 1248.3 KB

TEST 5: Video Generation
⚠️ THIS WILL MAKE A REAL VEO API CALL
Continue with Veo test? (yes/no): _
```

### If you answer **no** (skip expensive test):
```
ℹ️  Skipping Veo test

TEST 6: Engine Integration
✅ Veo routing found
✅ Veo preset in config

TEST SUMMARY
✅ Prerequisites
✅ Module Import
✅ Prompt Conversion
✅ Seed Frame Generation
❌ Video Generation (Optional) - SKIPPED
✅ Engine Integration

Results: 5/6 tests passed

✅ SAFE TO DEPLOY (Video test optional)
```

### If you answer **yes** (full test):
```
[VEO API] Sending request to veo-3.1-generate-preview...
[VEO API] Operation started
[VEO API] Still generating... (20s elapsed)
[VEO API] ✅ Complete after 45s
[VEO] Video saved: 4.2 MB
✅ Frame extracted

Results: 6/6 tests passed

✅ ALL TESTS PASSED - SAFE TO DEPLOY
```

---

## Quick Troubleshooting

### "GEMINI_API_KEY not set"
```bash
export GEMINI_API_KEY="your_key_here"
```

### "ffmpeg not found"
```bash
sudo apt-get install ffmpeg  # Ubuntu
brew install ffmpeg           # macOS
```

### "Module import failed"
```bash
ls -la veo_video_utils.py  # Check file exists
```

---

## After Tests Pass

1. **Edit `ai_config.json`:**
   ```json
   {
     "image_provider": "veo"
   }
   ```

2. **Deploy and restart bot**

3. **Make 2-3 choices**

4. **Check `films/segments/` for videos**

---

## Files Generated During Test

```
images/
  ├─ frame_0_test_seed.png        # Seed frame (Gemini)
  └─ frame_1_test_frame_1.png     # Frame 1 (last frame of video)

films/segments/
  └─ seg_0_1_1234567890.mp4       # Video segment (8 seconds)
```

---

## Cost

**Skip video test (no):** ~$0.01  
**Full test (yes):** ~$0.10-0.51

---

## Documentation

- Full guide: `TEST_VEO_LOCALLY.md`
- Architecture: `VEO_VIDEO_BASED_IMAGE_GEN.md`
- Integration plan: `VEO_INTEGRATION_PLAN.md`



