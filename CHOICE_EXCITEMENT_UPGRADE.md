# Choice Generation Excitement Upgrade

**Date:** December 16, 2025  
**Status:** ✅ IMPLEMENTED

---

## 🎯 Problem

Choices were **boring, passive, and generic**:
- ❌ "Move forward"
- ❌ "Look around"
- ❌ "Approach carefully"
- ❌ "Check it out"

They lacked **physical energy** and **bodily action**. Choices felt like suggestions, not visceral actions.

---

## ✅ Solution

### **1. Increased Temperature** (`choices.py`)
- **Before:** 0.7 (safe, predictable)
- **After:** 1.2 (creative, daring, unpredictable)
- **Impact:** More variety and risk in generated choices

### **2. Rewritten System Prompt** (`choices.py`)
Completely rewrote the LLM system prompt to emphasize:

**PHYSICAL BODY VERBS:**
- **LEGS/FEET:** Sprint, Vault, Leap, Scramble, Slide, Dive, Kick, Launch
- **ARMS/HANDS:** Grab, Yank, Wrench, Hurl, Smash, Rip, Pry, Shove
- **TORSO:** Slam, Throw yourself, Barrel through, Roll, Lunge, Charge

**BEFORE:**
```
"Generate 3 SHORT ACTION CHOICES (2-4 words each).
GOOD: 'Scale the fence', 'Examine door lock'
BAD: 'Look around', 'Go inside'"
```

**AFTER:**
```
"Generate 3 VISCERAL, PHYSICAL ACTION CHOICES (3-6 words each).
Emphasize BODILY movement and physical risk.

✅ 'Vault over chain-link fence'
✅ 'Hurl yourself through window'
✅ 'Sprint full-tilt toward shed'
✅ 'Scramble up rocky slope'
✅ 'Wrench open rusted blast door'

Make every choice feel like an ACTION MOVIE."
```

### **3. Removed Risk Filters** (`choices.py`)
**BEFORE:**
```python
def filter_risky_choices(choices, dispatch, vision):
    # Remove aggressive/risky choices like:
    # 'attack', 'fight', 'charge', 'rush', 'sprint'...
    risky_keywords = [...]
    filtered = [c for c in choices if not any(rk in c.lower() for rk in risky_keywords)]
```

**AFTER:**
```python
def filter_risky_choices(choices, dispatch, vision):
    # DISABLED - We WANT risky, daring choices!
    return choices
```

### **4. Updated Main Prompt Template** (`prompts/simulation_prompts.json`)
Rewrote `player_choice_generation_instructions` with:
- **Action movie energy** (Die Hard, Mad Max, The Raid)
- **Extensive physical verb library** (50+ body-focused verbs)
- **Explicit anti-patterns** (no "approach", "proceed", "check")
- **Energy-first philosophy** (make your heart race)

---

## 📊 Comparison

### **OLD CHOICES:**
```
1. Move forward
2. Examine the area
3. Proceed carefully
```

### **NEW CHOICES:**
```
1. Sprint full-tilt toward shed
2. Vault over chain-link fence
3. Hurl yourself through window
```

---

## 🎬 Design Philosophy

**Every choice should:**
1. **Use your BODY** - Emphasize legs, arms, torso in motion
2. **Feel PHYSICAL** - You should feel the exertion reading it
3. **Be ACTION-FIRST** - No passive observation
4. **Create MOMENTUM** - Always moving forward/sideways, never back
5. **Inspire EXCITEMENT** - Make the player think "HELL YES!"

**Inspiration:** Die Hard, Mad Max: Fury Road, The Raid, John Wick
- Characters who throw themselves into danger
- Physical, visceral, high-energy action
- Every movement feels consequential

---

## 🔧 Technical Changes

### **Files Modified:**
1. **`choices.py`**
   - Line 74: Temperature 0.7 → 1.2
   - Lines 101-122: Completely rewritten system prompt
   - Lines 340-350: Disabled risk filtering

2. **`prompts/simulation_prompts.json`**
   - `player_choice_generation_instructions`: Complete rewrite with physical verb emphasis

---

## 🎮 Expected Impact

**Players will see:**
- ✅ **Exciting verb variety** (sprint, vault, hurl, wrench, scramble)
- ✅ **Physical sensation** (choices make you FEEL the movement)
- ✅ **Higher risk options** (no more filtering out "dangerous" choices)
- ✅ **Action movie energy** (every turn feels cinematic)
- ✅ **More creative options** (higher temperature = more variety)

**Players will NOT see:**
- ❌ "Move forward"
- ❌ "Look around"
- ❌ "Approach carefully"
- ❌ "Check it out"
- ❌ "Proceed" (or any corporate language)

---

## 🧪 Testing Notes

Monitor for:
1. **Verb variety** - Are we seeing diverse physical verbs?
2. **Player excitement** - Do choices feel compelling?
3. **Grounding** - Are choices still contextual to visible elements?
4. **Death rate** - Did removing risk filters make game too deadly? (This is fine - we WANT higher difficulty)

---

## 💡 Future Enhancements

If choices are still too tame:
- Increase temperature to 1.4
- Add more extreme physical verbs (plunge, catapult, rocket)
- Create "ultra-risky" choice category (50% chance of death)

If choices are too wild/ungrounded:
- Re-enable some basic grounding checks
- Add vision model to verify objects exist
- Reduce temperature slightly

---

**Result:** Choices are now **EXCITING, PHYSICAL, and VISCERAL** instead of boring and passive! 🎬💥


