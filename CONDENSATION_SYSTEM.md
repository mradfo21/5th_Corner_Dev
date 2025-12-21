# 🗜️ AUTOMATIC CONDENSATION SYSTEM

## ✅ Yes, it will condense if it gets too long!

The world evolution system now has **multi-layered condensation** to prevent unbounded growth.

---

## 📊 Condensation Mechanisms

### **1. Situation Length Limiting (Per-Turn)**

**Trigger:** `current_situation` > 100 words

**Action:**
```python
if situation_word_count > 100:
    # Ask LLM to condense to 50-70 words
    condensed = llm_condense(situation)
    
    # Fallback: truncate to first 100 words if API fails
```

**Example:**
```
Input (127 words):
"Jason remains at the perimeter fence of the Four Corners facility, 
the skeletal guard tower a stark silhouette against the desert sky. 
The presence of two guards in black tactical gear near the east gate, 
50 meters away, shatters the initial silence, indicating an active 
security presence. A palpable tension rises as the secrets of the 
abandoned Horizon facility threaten to surface, no longer a silent 
mystery. The wind picks up, dust swirling around the concrete 
barriers. Distant machinery hums continuously. The harsh desert sun 
beats down relentlessly. Jason's camera equipment feels heavy. 
His breathing is measured and careful..."

Output (58 words):
"Jason at the perimeter fence, guard tower visible. Two guards in 
black tactical gear 50m away at east gate. Active security presence. 
Tension rising as facility secrets threaten to surface. Wind picking 
up, dust swirling. Machinery humming in distance."
```

**Result:** Keeps situation descriptions concise and focused.

---

### **2. Recent Events Buffer (Rolling Window)**

**Standard Limit:** 10 entries  
**Deep Condensation:** Trimmed to 8 entries every 30 turns

**Behavior:**
```python
recent_events.append(f"Turn {turn}: {action}")

if len(recent_events) > 10:
    recent_events = recent_events[-10:]  # Keep last 10

# Every 30 turns:
if turn_count % 30 == 0:
    recent_events = recent_events[-8:]  # Extra trim
```

**Example:**
```
Turn 1: Reached fence
Turn 2: Spotted guards
Turn 3: Photographed facility
...
Turn 10: Crouched behind barrier
Turn 11: Waited for patrol (oldest entry drops off)
```

**Result:** Always have recent context, never unbounded growth.

---

### **3. Seen Elements Buffer (Rolling Window)**

**Standard Limit:** 50 elements  
**Deep Condensation:** Trimmed to 40 elements every 30 turns

**Behavior:**
```python
new_elements = extract_from(current_situation)
for elem in new_elements:
    if elem not in seen_elements:
        seen_elements.append(elem)

if len(seen_elements) > 50:
    seen_elements = seen_elements[-50:]  # Keep last 50

# Every 30 turns:
if turn_count % 30 == 0:
    seen_elements = seen_elements[-40:]  # Extra trim
```

**Example:**
```
Elements: [
  "guard tower", "chain-link fence", "east gate", 
  "guards in tactical gear", "concrete barrier", 
  "machinery hum", "desert wind", ...
]

(Oldest elements drop off when limit reached)
```

**Result:** Maintains discovery history without bloat.

---

### **4. Persistent Archive (Capped)**

**Limit:** 1000 entries (across all sessions)

**Behavior:**
```python
archive.append(new_entry)

if len(archive) > 1000:
    archive = archive[-1000:]  # Keep last 1000
```

**Result:** Long-term inspection capability without infinite file growth.

---

### **5. World Prompt (Static)**

**Behavior:** `world_prompt` is **NEVER modified** after initialization

The core world description (1365 words) remains constant:
- Four Corners setting
- 1993 era
- Red biome lore
- Facility details
- Game rules

**Result:** No growth, ever. Core lore preserved.

---

## 📈 Growth Limits Over Time

### **Short Game (10 turns):**
```
world_prompt:        1365 words (static)
current_situation:   50-70 words (replaced each turn)
recent_events:       10 entries × 20 words = 200 words
seen_elements:       30 elements × 5 words = 150 words
---
Total context:       ~1785 words
```

### **Medium Game (50 turns):**
```
world_prompt:        1365 words (static)
current_situation:   50-70 words (replaced each turn)
recent_events:       10 entries × 20 words = 200 words
seen_elements:       50 elements × 5 words = 250 words
---
Total context:       ~1885 words
```

### **Long Game (100 turns):**
```
world_prompt:        1365 words (static)
current_situation:   50-70 words (replaced each turn)
recent_events:       8 entries × 20 words = 160 words (trimmed at turn 30, 60, 90)
seen_elements:       40 elements × 5 words = 200 words (trimmed at turn 30, 60, 90)
---
Total context:       ~1785 words
```

### **Epic Game (1000 turns):**
```
world_prompt:        1365 words (static)
current_situation:   50-70 words (replaced each turn)
recent_events:       8 entries × 20 words = 160 words
seen_elements:       40 elements × 5 words = 200 words
---
Total context:       ~1785 words (stable!)
```

---

## 🎯 Key Insight

**The context size is BOUNDED:**

- ✅ Turn 10: ~1785 words
- ✅ Turn 50: ~1885 words
- ✅ Turn 100: ~1785 words
- ✅ Turn 1000: ~1785 words

**After turn 30, growth stops!**

The periodic deep condensation ensures that even infinite games maintain a stable memory footprint of **~1800 words of context**.

---

## 🔬 How Condensation Works

### **Situation Condensation (LLM-Based):**

When `current_situation` exceeds 100 words:

1. **Primary:** Ask Gemini Flash to condense:
   ```
   "Condense this to 2-3 sentences (50-70 words max):
    Keep: Location, threats, discoveries, current action/state.
    Remove: Redundant details, flowery language."
   ```

2. **Fallback:** If API fails, truncate to first 100 words

3. **Logging:** Reports word reduction:
   ```
   [CONDENSATION] Reduced from 127 to 58 words
   ```

### **Buffer Trimming (Automatic):**

Every 30 turns:
```
[DEEP CONDENSATION] Turn 30 - performing periodic cleanup
  Trimmed recent_events to 8
  Trimmed seen_elements to 40
```

### **Archive Rotation:**

Every save:
```python
if len(archive) > 1000:
    archive = archive[-1000:]  # Keep most recent 1000
```

---

## 🎮 User Experience

### **What You'll Notice:**

1. **Situation stays concise:** Never see bloated descriptions
2. **Recent context maintained:** Last ~10 actions always remembered
3. **Long-term consistency:** Discovered elements tracked (but oldest forgotten)
4. **No performance degradation:** Memory footprint stable even in hour-long games

### **What You Won't Notice:**

- Automatic condensation happens transparently
- Old events gracefully drop off
- Archive stays manageable
- No manual intervention needed

---

## 📊 Comparison

### **Without Condensation:**
```
Turn 10:   2,000 words
Turn 50:   5,000 words
Turn 100:  10,000 words
Turn 1000: 100,000 words (!!!) 
```
→ Unbounded growth, eventual memory issues, slow LLM processing

### **With Condensation:**
```
Turn 10:   1,785 words
Turn 50:   1,885 words
Turn 100:  1,785 words
Turn 1000: 1,785 words
```
→ Stable footprint, fast processing, long-game viable

---

## 🔍 Inspection

### **Check Current Sizes:**
```python
import engine
state = engine.get_state('default')

print(f"world_prompt: {len(state['world_prompt'].split())} words")
print(f"current_situation: {len(state.get('current_situation', '').split())} words")
print(f"recent_events: {len(state.get('recent_events', []))} entries")
print(f"seen_elements: {len(state.get('seen_elements', []))} elements")
```

### **View Condensation Logs:**
```
[CONDENSATION] Situation too long (127 words), condensing...
[CONDENSATION] Reduced from 127 to 58 words

[DEEP CONDENSATION] Turn 30 - performing periodic cleanup
  Trimmed recent_events to 8
  Trimmed seen_elements to 40
```

---

## ✅ Summary

**Q: Will it condense if it gets too long?**

**A: YES!** Multi-layered automatic condensation:

1. ✅ **Per-turn:** Situations >100 words condensed to 50-70
2. ✅ **Rolling buffers:** Recent events (max 10) and seen elements (max 50)
3. ✅ **Periodic trimming:** Every 30 turns, extra cleanup
4. ✅ **Archive capping:** Max 1000 total entries
5. ✅ **Static core:** world_prompt never grows

**Result:** Stable ~1800 word context, even in 1000+ turn games! 🎯

---

**The simulation has memory, but not too much memory.** 🧠✂️

