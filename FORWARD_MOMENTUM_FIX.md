# 🚀 FORWARD MOMENTUM FIX

## 🎯 **PROBLEM:**

Choices were too passive and observational:
- ❌ "CHECK the perimeter"
- ❌ "HIDE behind the fence"
- ❌ "Look around"

**Result:** Gameplay felt stalled, no spatial progression.

---

## ✅ **SOLUTION:**

Rewrote choice generation to **PRIORITIZE FORWARD MOVEMENT**.

---

## 📋 **NEW MANDATORY RULES:**

### **Rule #1: AT LEAST ONE MOVEMENT CHOICE**
Every choice set MUST include at least ONE that advances Jason spatially:
- Climb over fence
- Sprint to buildings
- Approach the entrance
- Advance through terrain
- Push ahead to structure
- Cross to facility
- Vault the barrier

### **Rule #2: Avoid Passive Observation**
**FORBIDDEN:**
- ❌ "Check the perimeter" - Too passive
- ❌ "Look around" - Not advancing
- ❌ "Hide and wait" - No momentum
- ❌ "Observe from distance" - Too cautious
- ❌ "Stay put" - No movement

**INSTEAD:**
- ✅ "Climb the fence"
- ✅ "Sprint to buildings"
- ✅ "Approach entrance"
- ✅ "Advance through terrain"
- ✅ "Push to next cover"

---

## 🎬 **CONTEXTUAL MOVEMENT:**

### **At Perimeter/Outside:**
MUST include forward movement:
- "Climb over the fence"
- "Sprint to buildings"
- "Approach the entrance"
- "Vault the barrier"
- "Breach the perimeter"

### **If Guards Present:**
Still include movement, just tactical:
- "Sprint to next cover" (risky but forward)
- "Crawl to building" (stealth + forward)
- "Circle around to entrance" (evasion + forward)

### **If Exploring:**
Mix investigation with advancement:
- "Photograph then advance"
- "Document while moving"
- "Push deeper into facility"

---

## 📊 **BEFORE vs AFTER:**

### **Before (Passive):**
```
🟢 What will you do next?

🔍 CHECK the perimeter
🧍 HIDE behind the fence
📸 EXAMINE caution sign
```
*All static, no progress*

### **After (Active):**
```
🟢 What will you do next?

🏃 Sprint to buildings
🧗 Climb over fence
📸 Photograph while advancing
```
*All options move forward!*

---

## 🎯 **KEY PRIORITIES:**

**Movement Category is #1 Priority** (was #5 before):
1. ✅ **MOVEMENT/TRAVERSAL** - Spatial advancement
2. AGGRESSIVE/BOLD - High risk actions
3. CLEVER/TACTICAL - Environmental use
4. STEALTH/EVASION - Sneaky forward movement
5. INVESTIGATIVE - Document while moving
6. INTERACTION - Object manipulation

---

## 💡 **EXPLICIT INSTRUCTIONS ADDED:**

```
**MANDATORY: AT LEAST ONE CHOICE MUST BE FORWARD MOVEMENT/ADVANCEMENT**

The player wants to MOVE FORWARD into the scene, not just stand and observe!

AVOID: Check, examine, hide, wait, observe
PREFER: Climb, sprint, approach, advance, push, cross, vault, breach
```

---

## ✅ **EXPECTED RESULTS:**

### **At Fence:**
- "Climb over fence" ✅
- "Sprint to buildings" ✅
- "Vault the barrier" ✅

### **At Building:**
- "Approach the entrance" ✅
- "Push through doorway" ✅
- "Advance to facility" ✅

### **In Facility:**
- "Descend into shaft" ✅
- "Push deeper into corridor" ✅
- "Advance to reactor room" ✅

---

## 🚀 **MOMENTUM:**

**Every turn should feel like:**
1. You SEE something (image + dispatch)
2. You MOVE TOWARD it (choice)
3. You EXPERIENCE consequence (next turn)
4. Repeat

**No more standing still! Always pushing forward!** 🏃‍♂️

---

## 📁 **FILES CHANGED:**

- ✅ `prompts/simulation_prompts.json` - Completely restructured choice priorities

---

**Status:** ✅ READY TO TEST

Restart the bot and watch - choices should now push you INTO the scene! 🎬

