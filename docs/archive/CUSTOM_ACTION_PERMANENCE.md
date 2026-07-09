# 🎯 CUSTOM ACTION PERMANENCE SYSTEM

## ✨ **FEATURE: Player Actions Have Lasting Impact**

**Goal:** When a player uses a custom action, the world MUST respond to it directly and remember the consequences.

---

## 🔧 **IMPLEMENTATION:**

### **1. Strengthened LLM Instructions**

**File:** `prompts/simulation_prompts.json`  
**Section:** `action_consequence_instructions`

**New Rules:**
```
**CRITICAL: YOU MUST RESPOND TO THE PLAYER'S ACTUAL ACTION.**

The player chose a specific action. The world MUST react to that EXACT action. 
Don't ignore it. Don't deflect it. Make it HAPPEN and show the CONSEQUENCE.

**IF THE PLAYER DOES SOMETHING, IT AFFECTS THE WORLD PERMANENTLY:**
- Break something → It stays broken, mentioned in future turns
- Leave evidence → It can be found later
- Make noise → Guards react if nearby
- Take an item → You now have it
- Damage something → Damage persists
- Change location → You are now THERE
```

**Player Agency Rules:**
- The player's action HAPPENS - don't negate it
- If they say "Smash window" → Window gets smashed
- If they say "Photograph evidence" → They capture the photo
- If they say "Climb fence" → They climb (or fail with injury if risky)
- Custom actions are POWERFUL - let them succeed in doing the thing, then show the consequence

---

### **2. History Tracking**

**File:** `engine.py`  
**Function:** `advance_turn_choices_deferred()`

**New Field:** `is_custom_action` in history entries

```python
# Save to history (with custom action flag for permanence tracking)
is_custom_action = not any(keyword in choice.lower() for keyword in [
    "move", "advance", "photograph", "examine", "sprint", "climb", "vault", "crawl"
])

history_entry = {
    "choice": choice,
    "is_custom_action": is_custom_action,  # Flag for permanence
    "dispatch": dispatch,
    "vision_dispatch": vision_dispatch,
    "vision_analysis": "",
    "world_prompt": state.get("world_prompt", ""),
    "image": consequence_img_url,
    "image_url": consequence_img_url
}
```

---

## 🎮 **HOW IT WORKS:**

### **Example 1: Smash Spotlight**

**Player Action:** "Smash the spotlight bulb"

**LLM Response (OLD - BAD):**
```
❌ "You consider smashing the spotlight, but hesitate."
❌ "As you approach the light, you hear guards nearby."
```
*Action was ignored/deflected*

**LLM Response (NEW - GOOD):**
```
✅ "Glass shatters as you smash the spotlight bulb. Darkness floods the area."
```
*Action happened, consequence shown*

---

### **Example 2: Break Window**

**Player Action:** "Kick through the window"

**Turn 1:**
```
✅ "Your boot smashes through the glass. Shards spray across the floor."
```

**Turn 3 (Later):**
```
✅ "You pass the broken window you kicked through earlier, glass still crunching underfoot."
```
*Permanence - action is remembered*

---

### **Example 3: Steal Uniform**

**Player Action:** "Grab the guard uniform"

**Turn 1:**
```
✅ "You snatch the tactical vest from the locker. Heavy Kevlar."
```

**Turn 5 (Later):**
```
✅ "The stolen guard vest hangs loose on your frame as you move."
```
*Item acquisition persists*

---

## 📊 **SYSTEM FLOW:**

```
Player submits custom action
        ↓
Bot sends to engine.advance_turn_image_fast()
        ↓
engine._generate_combined_dispatches() with strengthened prompt:
   - "Player's action HAPPENS"
   - "Show the consequence, don't negate it"
   - "Break something → It stays broken"
        ↓
LLM generates dispatch showing action actually occurring
        ↓
History entry saved with is_custom_action flag
        ↓
Future turns can reference this action via history context
        ↓
World state evolution includes permanent changes
```

---

## 🎯 **KEY PRINCIPLES:**

### **1. Player Agency**
- Actions are not suggestions - they are what Jason DOES
- The LLM must respect player intent
- Success or failure based on risk, but action always ATTEMPTS

### **2. Consequences Over Negation**
**WRONG:**
- "You try to X but..."
- "As you approach X, Y happens instead"
- Ignoring the action entirely

**RIGHT:**
- "You X. [Consequence]"
- "Glass shatters. [What happens next]"
- Action happens, then show result

### **3. Permanence**
- Broken = stays broken
- Taken = you have it
- Changed = it's different now
- History remembers

---

## 🧪 **TESTING:**

### **Test Case 1: Destructive Action**
1. Use custom action: "Smash control panel"
2. Check dispatch: Should say panel is smashed
3. Next turn: Panel should still be broken in narrative

### **Test Case 2: Creative Action**
1. Use custom action: "Throw rock at distant window"
2. Check dispatch: Rock should fly, window should react
3. If guards nearby: They should react to sound

### **Test Case 3: Item Interaction**
1. Use custom action: "Pry open locked crate"
2. Check dispatch: Crate opens (or fails with consequence)
3. Future turns: Reference opened crate or tools inside

---

## 🔄 **HISTORY CONTEXT:**

The LLM receives history in prompts, so custom actions naturally carry forward:

```python
# History entry example:
{
  "choice": "Smash the spotlight bulb",
  "is_custom_action": true,  # Flagged for importance
  "dispatch": "Glass shatters as you smash the spotlight bulb. Darkness floods the area.",
  "world_prompt": "...",
  "image": "/images/frame_123.png"
}
```

Future LLM calls include this history, so it "remembers" the broken spotlight.

---

## ✅ **BENEFITS:**

**For Players:**
- Actions feel meaningful
- Creative solutions are rewarded
- World feels reactive and persistent
- Player agency respected

**For Narrative:**
- Emergent storytelling
- Consequences build on each other
- Unique playthroughs
- Memorable moments

---

## 🚀 **STATUS:**

**Implementation:** ✅ COMPLETE  
**Testing:** Ready for player testing  
**Production:** Ready to deploy  

**The world now remembers your actions!** 🎬

