# Flipbook Temporal Continuity Deep Dive

## **Problem Statement**
User reports: "The images feel wildly different from each other. We need temporal continuity."

Flipbook sequences (16-frame animations) are not maintaining visual coherence between turns. Each new flipbook feels like a visual "restart" rather than a natural progression from the previous sequence.

---

## **Current Implementation Analysis**

### **1. Reference Image Passing**
**Location:** `engine.py` lines 1676-1700

**Current Logic:**
```python
prev_grid = st_temp.get('flipbook_last_grid')  # Full 4x4 grid from previous turn

if prev_grid and os.path.exists(prev_grid):
    flipbook_refs.append(prev_grid)  # Passes entire previous 16-frame grid
    flipbook_prefix = "🎞️ PREVIOUS SEQUENCE ATTACHED: The 4x4 grid reference image shows the 16 sequential frames that JUST HAPPENED..."
```

**Status:** ✅ **GOOD** - We ARE passing the previous grid as a reference

---

### **2. Strength Parameter**
**Location:** `engine.py` line 1708

**Current Value:**
```python
strength=strength  # Defaults to 0.25 (from _gen_image function signature)
```

**Analysis:**
- `strength=0.25` means: "25% adherence to reference, 75% freedom"
- **TOO LOW** for temporal continuity!
- For comparison:
  - `0.25` = Loose reference (what we have now)
  - `0.50` = Balanced (recommended for continuity)
  - `0.65` = Strong adherence (best for temporal consistency)
  - `0.80+` = Almost exact copy

**Status:** ❌ **PROBLEM IDENTIFIED** - Strength is too weak!

---

### **3. Prompt Structure**
**Location:** `simulation_prompts.json` - `gemini_flipbook_4panel_prefix`

**Current Instruction (when prev_grid exists):**
```
🎞️ PREVIOUS SEQUENCE ATTACHED: The 4x4 grid reference image shows the 16 sequential frames that JUST HAPPENED. 
The scene evolved from frame 1 (top-left) to frame 16 (bottom-right). 
Panel 1 of your new grid MUST follow naturally from the bottom-right frame of that previous grid.
```

**Analysis:**
- ✅ Instructs model to look at frame 16 of previous grid
- ✅ Says panel 1 of new grid should follow from it
- ⚠️ BUT: Doesn't explicitly say HOW to maintain continuity
- ⚠️ Missing: "Match the lighting, colors, camera angle, and environment of frame 16"

**Status:** ⚠️ **NEEDS ENHANCEMENT** - Good foundation, needs specificity

---

### **4. Missing: Last Frame as Direct Reference**
**What We Have:**
- Full 4x4 grid (16 frames) as reference
- Instructions to "follow from frame 16"

**What's Missing:**
- We're NOT passing `flipbook_last_frame` (the isolated frame 16) as a SEPARATE reference
- Model sees the full grid but has to "figure out" which part to match closely

**Status:** ❌ **PROBLEM IDENTIFIED** - Should pass BOTH grid AND last frame

---

## **Root Causes**

### **Primary Issue: Strength Too Low**
`strength=0.25` gives the model too much creative freedom. It can see the previous grid but feels free to:
- Change lighting dramatically
- Alter color palette
- Shift camera angle
- Reimagine the environment

### **Secondary Issue: Prompt Lacks Explicit Visual Matching Instructions**
Current: "Panel 1 should follow naturally from frame 16"
Needed: "Panel 1 MUST match frame 16's lighting, colors, camera position, and environment EXACTLY. Maintain visual continuity."

### **Tertiary Issue: Not Using Last Frame as Direct Reference**
Passing the full grid is good for context, but the model needs the LAST FRAME isolated as a strong visual anchor.

---

## **Recommended Fixes**

### **Fix 1: Increase Strength to 0.60** ⭐ **CRITICAL**
**File:** `engine.py` line 1708

**Change:**
```python
# OLD:
strength=strength,  # 0.25 default

# NEW:
strength=0.60,  # Strong adherence for temporal continuity
```

**Impact:** Model will maintain 60% visual consistency with reference grid
**Risk:** Low - 0.60 is balanced (not too rigid, not too loose)

---

### **Fix 2: Pass Both Grid AND Last Frame as References** ⭐ **HIGH PRIORITY**
**File:** `engine.py` lines 1689-1700

**Change:**
```python
# OLD:
if prev_grid and os.path.exists(prev_grid):
    flipbook_refs.append(prev_grid)

# NEW:
prev_last = st_temp.get('flipbook_last_frame')  # Isolated frame 16

if prev_grid and os.path.exists(prev_grid):
    flipbook_refs.append(prev_grid)  # Full context
    
    if prev_last and os.path.exists(prev_last):
        flipbook_refs.append(prev_last)  # Strong visual anchor
        print(f"[FLIPBOOK] Passing grid + last frame for continuity")
```

**Impact:** Model gets BOTH context (full grid) AND a strong visual target (frame 16)
**Risk:** Low - More references = better guidance

---

### **Fix 3: Enhance Prompt for Explicit Visual Matching** ⭐ **HIGH PRIORITY**
**File:** `engine.py` lines 1692-1694

**Change:**
```python
# OLD:
flipbook_prefix = f"🎞️ PREVIOUS SEQUENCE ATTACHED: The 4x4 grid reference image shows the 16 sequential frames that JUST HAPPENED. " \
                 f"The scene evolved from frame 1 (top-left) to frame 16 (bottom-right). " \
                 f"Panel 1 of your new grid MUST follow naturally from the bottom-right frame of that previous grid.\n\n" + flipbook_prefix

# NEW:
flipbook_prefix = f"🎞️ PREVIOUS SEQUENCE ATTACHED: The 4x4 grid reference image shows the 16 sequential frames that JUST HAPPENED. " \
                 f"The scene evolved from frame 1 (top-left) to frame 16 (bottom-right). " \
                 f"⚠️ CRITICAL VISUAL CONTINUITY: Panel 1 of your NEW grid MUST match frame 16 of the PREVIOUS grid in ALL visual aspects: " \
                 f"SAME lighting and shadows, SAME color palette and tones, SAME camera angle and framing, SAME environment and atmosphere. " \
                 f"This is a CONTINUATION of the previous sequence - maintain seamless visual flow!\n\n" + flipbook_prefix
```

**Impact:** Model gets explicit instructions on WHAT to match
**Risk:** None - Clearer instructions = better output

---

### **Fix 4: Add Visual Similarity Verification Prompt** ⚡ **OPTIONAL ENHANCEMENT**
**File:** `engine.py` after line 1701

**Addition:**
```python
# Add explicit visual matching instruction
flipbook_prompt += "\n\n🎨 VISUAL CONTINUITY CHECK: Before generating, verify that your panel 1 visually matches " \
                   "the lighting, colors, and framing of the previous sequence's final frame. " \
                   "Maintain temporal consistency - this is ONE continuous experience."
```

**Impact:** Reinforces visual matching as a requirement
**Risk:** None - Redundancy helps with AI models

---

## **Implementation Priority**

### **Phase 1: Critical Fixes (Immediate)**
1. ✅ **Fix 1:** Increase strength to 0.60
2. ✅ **Fix 3:** Enhance prompt with explicit matching instructions

### **Phase 2: High Priority (Same session)**
3. ✅ **Fix 2:** Pass both grid AND last frame as references

### **Phase 3: Optional Enhancements (If needed after testing)**
4. ⚠️ **Fix 4:** Add visual similarity verification prompt

---

## **Expected Outcomes**

**Before Fixes:**
- Flipbook frame 16 (end of turn N): Desert facility, golden hour lighting, camera at fence
- Flipbook frame 1 (start of turn N+1): DIFFERENT lighting, camera teleported, colors changed

**After Fixes:**
- Flipbook frame 16 (end of turn N): Desert facility, golden hour lighting, camera at fence  
- Flipbook frame 1 (start of turn N+1): SAME lighting, camera smoothly advanced, colors preserved ✅

---

## **Testing Plan**

1. Apply fixes locally
2. Generate intro flipbook
3. Make a choice and observe the NEXT flipbook
4. Verify:
   - Frame 1 of new flipbook matches frame 16 of previous flipbook
   - Lighting consistent
   - Colors consistent
   - Camera position naturally advanced (not teleported)
   - Environment continuous

---

## **Metrics for Success**

- **Visual Continuity Score:** User should perceive "smooth flow" between sequences
- **Color Consistency:** Palette should remain stable turn-to-turn
- **Lighting Stability:** No dramatic lighting shifts without narrative reason
- **Camera Coherence:** Movement feels natural, not jarring


