# 🎬 Veo 3.1 Integration Plan

## Architecture Overview

Following the existing AI provider pattern, Veo 3.1 will be integrated as a **video generation provider** with the same modular, toggleable architecture.

---

## 📁 **Files to Create**

### **1. `veo_video_utils.py`** (New Module)
Core Veo 3.1 video generation logic - mirrors `gemini_image_utils.py` pattern.

```python
"""
veo_video_utils.py - Google Veo 3.1 Video Generation
Frame interpolation for creating cinematic videos from game runs.
"""

import requests
import base64
import time
from pathlib import Path
from typing import Optional, List, Dict

# Configuration
VEO_API_KEY = os.getenv("GEMINI_API_KEY", "")  # Uses same key as Gemini
VEO_MODEL = "veo-3.1-generate-preview"
VEO_FAST_MODEL = "veo-3.1-fast-generate-preview"
VIDEO_DIR = Path("films")

def generate_video_segment(
    first_frame_path: str,
    last_frame_path: str,
    prompt: str,
    duration: int = 8,
    use_fast_model: bool = False
) -> Optional[str]:
    """
    Generate a video segment between two frames using Veo 3.1.
    
    Args:
        first_frame_path: Path to starting frame
        last_frame_path: Path to ending frame
        prompt: Cinematic description of the action
        duration: Video length in seconds (4, 6, or 8)
        use_fast_model: If True, use veo-3.1-fast for speed
    
    Returns:
        Local path to saved video segment (.mp4)
    """
    pass

def stitch_segments(
    segment_paths: List[str],
    output_name: str = "film"
) -> Optional[str]:
    """
    Stitch video segments into a single film using ffmpeg.
    
    Args:
        segment_paths: List of paths to video segments
        output_name: Name for final film
    
    Returns:
        Path to final stitched video
    """
    pass
```

---

### **2. Update `ai_config.json`** (Add Video Provider)

```json
{
  "text_provider": "gemini",
  "text_model": "gemini-2.0-flash",
  "image_provider": "gemini",
  "image_model": "gemini-3-pro-image-preview",
  "video_provider": "veo",
  "video_model": "veo-3.1-generate-preview",
  "video_enabled": false,
  "last_updated": "2025-12-16T00:00:00Z"
}
```

---

### **3. Update `ai_provider_manager.py`** (Add Video Functions)

```python
def get_video_provider() -> str:
    """Get current video generation provider."""
    _ensure_initialized()
    return load_ai_config().get("video_provider", "veo")

def get_video_model() -> str:
    """Get current video generation model."""
    _ensure_initialized()
    return load_ai_config().get("video_model", "veo-3.1-generate-preview")

def is_video_enabled() -> bool:
    """Check if video generation is enabled."""
    _ensure_initialized()
    return load_ai_config().get("video_enabled", False)

def set_video_enabled(enabled: bool) -> None:
    """Enable or disable video generation."""
    config = load_ai_config()
    config["video_enabled"] = enabled
    save_ai_config(config)
```

---

### **4. Add Discord Commands** (In `bot.py`)

```python
@bot.tree.command(name="films", description="🎬 Manage VHS film generation")
async def films_command(interaction: discord.Interaction, action: str):
    """
    Actions:
    - current: Generate film from current session
    - list: Show available sessions
    - status: Check film generation status
    - enable: Enable film generation
    - disable: Disable film generation
    """
    pass

@bot.tree.command(name="film_status", description="📊 Check film generation status")
async def film_status_command(interaction: discord.Interaction):
    """Show video generation configuration and capabilities."""
    pass
```

---

## 🏗️ **Implementation Strategy**

### **Phase 1: Foundation (Day 1)**
✅ Create `veo_video_utils.py` with API client  
✅ Add video provider config to `ai_config.json`  
✅ Update `ai_provider_manager.py` with video functions  
✅ Add master toggle: `VIDEO_ENABLED` (default: False)  

**Files Modified:** 3 new, 2 existing  
**Lines Changed:** ~200 new, ~30 modified  
**Testing:** Unit tests for API client  

---

### **Phase 2: Core Generation (Day 2)**
✅ Implement `generate_video_segment()` with Veo 3.1 API  
✅ Add frame interpolation logic  
✅ Implement polling mechanism (video gen takes ~11s to 6min)  
✅ Add error handling and retries  

**Files Modified:** `veo_video_utils.py`  
**Lines Changed:** ~150  
**Testing:** Generate single segment from 2 test frames  

---

### **Phase 3: Stitching & Storage (Day 3)**
✅ Implement `stitch_segments()` using ffmpeg  
✅ Add video storage in `films/` directory  
✅ Implement cleanup logic (temp segments)  
✅ Add metadata tracking (session ID, duration, frame count)  

**Files Modified:** `veo_video_utils.py`  
**Lines Changed:** ~100  
**Testing:** Stitch 3+ segments into final film  

---

### **Phase 4: Discord Integration (Day 4)**
✅ Add `/films current` command  
✅ Add `/films list` command  
✅ Add progress indicators (embed updates)  
✅ Add film download/upload to Discord  

**Files Modified:** `bot.py`  
**Lines Changed:** ~300  
**Testing:** Generate film from active session  

---

### **Phase 5: Polish & Optimization (Day 5)**
✅ Add VHS post-processing overlay (optional)  
✅ Optimize prompt engineering for cinematic quality  
✅ Add parallel segment generation (3 at a time)  
✅ Add cost tracking and limits  

**Files Modified:** `veo_video_utils.py`, `bot.py`  
**Lines Changed:** ~150  
**Testing:** Full end-to-end run, quality assessment  

---

## 🎯 **Key Design Decisions**

### **1. Use Existing Patterns**
- ✅ Mirrors `gemini_image_utils.py` structure
- ✅ Follows `ai_provider_manager.py` conventions
- ✅ Discord commands match existing `/ai_*` style
- ✅ Configuration in `ai_config.json`

### **2. Non-Invasive Integration**
- ✅ **Zero changes** to `engine.py` game loop
- ✅ **Zero changes** to `_gen_image()` logic
- ✅ **Zero changes** to history storage (already has prompts!)
- ✅ Optional feature - game works with `video_enabled: false`

### **3. Self-Contained Module**
```
veo_video_utils.py
├── API client (Veo 3.1)
├── Frame interpolation
├── Video stitching (ffmpeg)
├── Storage management
└── Error handling
```

No tight coupling to engine - can be developed and tested independently.

### **4. Graceful Degradation**
- If Veo API fails → Keep segments, return as zip
- If stitching fails → Keep segments, manual stitch option
- If upload too large → Provide external link (Google Drive, Cloudinary)
- If API key missing → Clear error message, disable feature

---

## 📊 **Data Flow**

```
User: /films current
    ↓
Load history.json (already has prompts!)
    ↓
Extract frame pairs:
  [(frame_0, frame_1, prompt_1),
   (frame_1, frame_2, prompt_2),
   ...]
    ↓
FOR EACH PAIR (parallel batches of 3):
    ↓
  Veo 3.1 API:
    - first_frame: base64 encoded PNG
    - last_frame: base64 encoded PNG
    - prompt: Cinematic VHS prompt
    - duration: 8 seconds
    ↓
  Poll operation.done (10s intervals)
    ↓
  Download segment → films/segments/seg_N.mp4
    ↓
  Update Discord: "🎞️ Generated 3/9 [███░░░░░] 33%"
    ↓
ALL SEGMENTS COMPLETE
    ↓
ffmpeg concat → films/final/session_123_film.mp4
    ↓
Upload to Discord (or external host if > 25MB)
    ↓
Cleanup segments (optional)
```

---

## 🔧 **Configuration Options**

Add to `ai_config.json`:

```json
{
  "video_provider": "veo",
  "video_model": "veo-3.1-generate-preview",
  "video_enabled": false,
  "veo_settings": {
    "max_parallel_segments": 3,
    "segment_duration": 8,
    "use_fast_model": false,
    "cleanup_segments": true,
    "max_film_duration": 120,
    "quality": "720p",
    "enable_vhs_overlay": false
  }
}
```

---

## 🎬 **Prompt Engineering**

Convert game prompts to cinematic Veo prompts:

```python
def build_veo_prompt(
    game_prompt: str,
    action: str,
    dispatch: str
) -> str:
    """
    Convert image generation prompt to cinematic video prompt.
    """
    base_prompt = f"""
    A cinematic, VHS-quality video shot from first-person perspective.
    
    ACTION: {action}
    RESULT: {dispatch}
    
    VISUAL STYLE:
    - Found footage, 1993 handheld camcorder aesthetic
    - Slight grain, color bleed, analog artifacts
    - Natural handheld movement (subtle camera shake)
    - Smooth transition from first frame to last frame
    
    CAMERA MOVEMENT:
    - Match the action taken (walking forward, turning, etc.)
    - Maintain first-person POV throughout
    - No cuts or edits - continuous motion
    
    ATMOSPHERE:
    - Tense, mysterious, eerie
    - Cinematic lighting and framing
    - Environmental details visible
    
    The video should feel like recovered footage from 1993,
    capturing the exact moment described in the action.
    """
    return base_prompt.strip()
```

---

## 🧪 **Testing Strategy**

### **Unit Tests**
```python
# test_veo_integration.py

def test_veo_api_client():
    """Test Veo 3.1 API connectivity."""
    pass

def test_frame_interpolation():
    """Test video generation between 2 frames."""
    pass

def test_segment_stitching():
    """Test ffmpeg concatenation."""
    pass

def test_prompt_conversion():
    """Test game prompt → cinematic prompt conversion."""
    pass
```

### **Integration Tests**
1. Generate 1 segment (2 frames → 8s video)
2. Generate 3 segments and stitch
3. Full session conversion (intro + 5 choices = 6 segments = 48s)
4. Test error handling (API timeout, missing frames, etc.)

---

## 💰 **Cost Considerations**

### **Veo 3.1 Pricing** (Estimated)
- Per video: ~$0.10-0.50 per 8-second segment
- 9-frame run = 8 segments = ~$0.80-$4.00
- Budget: Set max segments per film (default: 20)

### **Implementation**
```python
# In veo_video_utils.py
MAX_SEGMENTS_PER_FILM = 20  # ~$10 max cost
COST_PER_SEGMENT = 0.25  # USD estimate

def estimate_cost(num_frames: int) -> float:
    """Estimate cost of film generation."""
    num_segments = num_frames - 1
    return min(num_segments, MAX_SEGMENTS_PER_FILM) * COST_PER_SEGMENT
```

---

## 🚀 **Deployment Checklist**

### **Prerequisites**
- ✅ `GEMINI_API_KEY` set (Veo uses same key)
- ✅ `ffmpeg` installed on server
- ✅ Sufficient disk space (~500MB per film)
- ✅ `google-genai` Python package installed

### **Environment Setup**
```bash
# Install dependencies
pip install google-genai

# Verify ffmpeg
ffmpeg -version

# Create directories
mkdir -p films/segments
mkdir -p films/final
```

### **Configuration**
```json
// ai_config.json
{
  "video_enabled": false,  // Start disabled
  "veo_settings": {
    "max_parallel_segments": 3,
    "segment_duration": 8
  }
}
```

### **Enable for Testing**
```
/film_status  → Check configuration
/films enable → Enable feature
/films current → Generate test film
```

---

## 📝 **Summary**

### **What Gets Created**
- 1 new module: `veo_video_utils.py` (~400 lines)
- 3 Discord commands: `/films`, `/film_status`, `/films enable`
- Configuration additions to existing files

### **What Doesn't Change**
- ❌ `engine.py` game loop
- ❌ `_gen_image()` function
- ❌ History storage format (already has prompts!)
- ❌ Existing Discord commands

### **Total Code Addition**
- **New code:** ~600 lines
- **Modified code:** ~50 lines
- **Testing code:** ~200 lines

### **Development Time Estimate**
- **Phase 1-3 (Core):** 3 days
- **Phase 4 (Discord):** 1 day
- **Phase 5 (Polish):** 1 day
- **Total:** ~5 days for full implementation

---

## ✅ **Advantages of This Approach**

1. **Non-Invasive:** Game works exactly the same with feature disabled
2. **Modular:** Entire system in one file (`veo_video_utils.py`)
3. **Toggleable:** Single flag enables/disables entire feature
4. **Extensible:** Easy to add more video providers later
5. **Testable:** Can develop and test independently of game loop
6. **Consistent:** Follows existing AI provider patterns perfectly
7. **Backwards Compatible:** Works with existing history data

---

## 🎯 **Next Steps**

Ready to implement? I can:
1. Create `veo_video_utils.py` with full API client
2. Update `ai_provider_manager.py` with video functions
3. Add Discord commands to `bot.py`
4. Write comprehensive tests

Just say the word!



