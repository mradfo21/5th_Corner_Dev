# 💰 Veo Cost Control Strategy

## The Problem

Veo 3.1 is expensive compared to image generation:
- **Veo:** $0.10-0.50 per 8-second video
- **Gemini Image:** ~$0.01 per image (or free)
- **9-frame run:** $0.80-4.00 (Veo) vs $0.09 (Gemini)

---

## Cost Control Strategies

### **1. Use Fast Model (Recommended)**
✅ **Savings:** ~50% cost reduction  
✅ **Speed:** 2-3x faster  
⚠️ **Trade-off:** Slightly lower quality  

```python
# In veo_video_utils.py
USE_FAST_MODEL = True  # veo-3.1-fast instead of veo-3.1
```

**Cost:** $0.05-0.25 per video (instead of $0.10-0.50)

---

### **2. Shorter Videos (Recommended)**
✅ **Savings:** ~50% cost for 4s vs 8s  
✅ **Speed:** Faster generation  
⚠️ **Trade-off:** Less smooth interpolation  

```python
# In veo_video_utils.py
VIDEO_DURATION = 4  # 4, 6, or 8 seconds
```

**Cost:** Approximately scales linearly with duration

---

### **3. Hybrid Mode (Best Balance)**
✅ **Savings:** 50-75% cost reduction  
✅ **Quality:** Best of both worlds  
✅ **Speed:** Faster overall  

**Strategy:** Use Veo only for important moments, Gemini for filler

```python
# Use Veo for:
- Frame 0 → 1 (intro transition)
- Hard transitions (location changes)
- Key story moments
- Combat/action sequences

# Use Gemini img2img for:
- Normal walking/movement
- Same location frames
- Filler frames
```

---

### **4. Cost Cap System (Recommended)**
✅ **Savings:** Hard spending limit  
✅ **Safety:** Never exceed budget  
✅ **Automatic:** Falls back when limit hit  

```python
# Session-based spending cap
MAX_VEO_COST_PER_SESSION = 2.00  # $2 max per game
CURRENT_SESSION_COST = 0.0

if CURRENT_SESSION_COST + ESTIMATED_COST > MAX_VEO_COST_PER_SESSION:
    # Fall back to Gemini
    use_gemini_instead()
```

---

### **5. Frame Skip Pattern (Aggressive Savings)**
✅ **Savings:** 66-75% cost reduction  
⚠️ **Quality:** Noticeable jumps  

**Pattern:** Only use Veo every N frames

```python
# Example: Veo every 3rd frame
Frame 0: Gemini (seed)
Frame 1: Veo (0→1)
Frame 2: Gemini img2img (1→2)
Frame 3: Gemini img2img (2→3)
Frame 4: Veo (3→4)  # Reset consistency
```

---

### **6. User Choice (Let Players Decide)**
✅ **Savings:** Players control their cost  
✅ **Flexibility:** Different tiers  

```
/ai_switch veo-full      # Every frame (expensive)
/ai_switch veo-hybrid    # Important frames only
/ai_switch veo-minimal   # Frame 0→1 only
/ai_switch gemini        # No Veo (free/cheap)
```

---

## Recommended Configuration

### **Budget-Conscious Setup**
```python
# veo_video_utils.py

# Use fast model (50% cheaper, 3x faster)
USE_FAST_MODEL = True

# Shorter videos (50% cheaper)
VIDEO_DURATION = 4

# Cost cap ($2 max per session)
MAX_SESSION_COST = 2.00

# Hybrid mode (Veo for key frames only)
VEO_MODE = "hybrid"
```

**Expected Cost:**
- Per video: $0.025-0.125 (fast + 4s)
- Per session: $0.20-1.00 (8 frames with hybrid)
- **75-90% savings** vs full Veo!

---

## Cost Tracking

Track spending per session:

```python
# Track costs
session_costs = {
    "veo_calls": 0,
    "total_cost": 0.0,
    "videos_generated": []
}

def estimate_veo_cost(duration, use_fast):
    if use_fast:
        return 0.05 * (duration / 4)  # $0.05 per 4s
    else:
        return 0.25 * (duration / 8)  # $0.25 per 8s

def can_afford_veo():
    estimated = estimate_veo_cost(VIDEO_DURATION, USE_FAST_MODEL)
    return (session_costs["total_cost"] + estimated) <= MAX_SESSION_COST
```

---

## Implementation Priority

### **Phase 1: Immediate (Do First)**
1. ✅ Enable fast model (`USE_FAST_MODEL = True`)
2. ✅ Reduce duration (`VIDEO_DURATION = 4`)
3. ✅ Add cost cap (`MAX_SESSION_COST = 2.00`)

**Result:** ~75% cost reduction, minimal code changes

### **Phase 2: Hybrid Mode (Do Next)**
1. Add logic to decide Veo vs Gemini per frame
2. Use Veo only for hard transitions + frame 0→1
3. Fall back to Gemini for same-location frames

**Result:** Additional 50% savings on top of Phase 1

### **Phase 3: Advanced (Optional)**
1. User-selectable quality tiers
2. Per-user cost tracking
3. Dynamic quality based on budget

---

## Comparison Table

| Strategy | Cost/Frame | Cost/9-Frame Run | Quality | Speed |
|----------|-----------|------------------|---------|-------|
| **Full Veo (8s)** | $0.25 | $2.00 | ⭐⭐⭐⭐⭐ | Slow |
| **Full Veo (4s)** | $0.125 | $1.00 | ⭐⭐⭐⭐ | Medium |
| **Fast Veo (8s)** | $0.125 | $1.00 | ⭐⭐⭐⭐ | Fast |
| **Fast Veo (4s)** ✅ | $0.0625 | $0.50 | ⭐⭐⭐ | Very Fast |
| **Hybrid (Fast 4s)** ✅ | $0.025 | $0.20 | ⭐⭐⭐⭐ | Fast |
| **Gemini Only** | $0.01 | $0.09 | ⭐⭐⭐ | Fast |

**Recommended:** Fast Veo (4s) with hybrid mode = $0.20 per run

---

## Sample Configurations

### **Ultra Budget ($0.20/run)**
```python
USE_FAST_MODEL = True
VIDEO_DURATION = 4
VEO_MODE = "hybrid"
MAX_SESSION_COST = 0.50
```
Use Veo for frames 0→1 only, rest Gemini

### **Balanced ($0.50/run)**
```python
USE_FAST_MODEL = True
VIDEO_DURATION = 4
VEO_MODE = "hybrid"
MAX_SESSION_COST = 2.00
```
Use Veo for hard transitions + intro

### **Quality ($2.00/run)**
```python
USE_FAST_MODEL = False
VIDEO_DURATION = 8
VEO_MODE = "full"
MAX_SESSION_COST = 5.00
```
Use Veo for every frame

---

## Emergency Brake

Hard fail-safe if costs run away:

```python
ABSOLUTE_MAX_COST = 5.00  # Kill switch

if session_costs["total_cost"] >= ABSOLUTE_MAX_COST:
    # Disable Veo for rest of session
    FORCE_GEMINI_MODE = True
    log_warning("Cost limit hit - Veo disabled")
```

---

## Monitoring

Log every Veo call:

```
[VEO COST] Video generated
  Duration: 4s
  Model: veo-3.1-fast
  Estimated Cost: $0.0625
  Session Total: $0.1875 / $2.00
  Remaining Budget: $1.8125
```

---

## Recommendation

**Start with Ultra Budget config:**
- Fast model
- 4-second videos
- Hybrid mode (Veo for frame 0→1 only)
- $0.50 session cap

**Cost:** ~$0.10-0.25 per game
**Quality:** Still excellent (one Veo transition sets the style)
**Safety:** Hard cap prevents runaway costs

Then adjust based on feedback!



