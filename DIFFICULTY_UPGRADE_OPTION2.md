# Difficulty Upgrade - Option 2 (Significant Increase)

**Date:** December 16, 2025  
**Status:** ✅ IMPLEMENTED

---

## 🎯 Overview

The game has been upgraded from "DANGEROUS but FAIR" to "DANGEROUS" - a hostile world where even smart choices don't guarantee safety.

---

## 📋 Changes Made

### 1. **World Philosophy** (`prompts/simulation_prompts.json`)

**BEFORE:**
```
"CRITICAL: This world is DANGEROUS but FAIR."
"Guards exist but are RARE and can be avoided with stealth."
"The facility is mostly ABANDONED and can be explored safely with caution."
"Smart choices allow survival."
```

**AFTER:**
```
"CRITICAL: This world is DANGEROUS."
"Death can come from poor choices, bad luck, or simply being in the wrong place."
"Guards, horrific creatures, paranormal anomalies, psychopathic miners, 
determined activists and journalists, are present sometimes - appearing 
when dramatically appropriate but frequent enough to maintain tension."
"The facility is a mix of empty desolate areas and active threat zones."
```

**Impact:** Removed the promise of fairness and safety through caution.

---

### 2. **Consequence System** (`prompts/simulation_prompts.json`)

**BEFORE: "FAIR BUT TENSE"**
- Cautious choices = SUCCESS with NO injury
- Movement = Usually succeeds safely if cautious
- Medium risk = Consequences depend on context
- High risk = Severe consequences likely

**AFTER: "HOSTILE WORLD"**
- **CAUTIOUS choices** → Usually succeed BUT ambient threats remain
  - Note: Even careful actions don't guarantee safety
  - Patrols move, creatures hunt, equipment fails
  
- **MOVEMENT** → Moderate to high risk
  - Walking/crawling = safer but can attract attention
  - Sprinting = very high risk of detection, gunfire, or falling
  - Moving in plain sight = guards open fire immediately
  - Climbing/traversing = risk of falls causing serious injury or death

- **MEDIUM RISK** → Severe consequences common
  - Sprint near threats = very likely to be detected AND attacked
  - Vault obstacles = can slip, fall, or attract threats
  - Loud actions = attract multiple patrols or creatures

- **HIGH RISK** → Death or critical injury highly likely
  - Charge guards = death from multiple gunshots
  - Attack creatures = severe mauling, arterial damage, likely death
  - Interact with fire/explosives = severe burns, dismemberment, or death

---

### 3. **UNLUCKY Fate Modifier** (`engine.py`)

**BEFORE:**
```
"Something goes wrong. Add ONE concrete complication:
• Equipment fails or malfunctions
• You make unexpected noise (stumble, knock something)
• Guard patrol appears or changes route toward you
• Injury is slightly worse than expected"
```

**AFTER:**
```
"Something goes SERIOUSLY wrong. Add ONE concrete SEVERE complication:
• Equipment fails CATASTROPHICALLY (gun jams, rope snaps, ladder breaks)
• You make LOUD noise attracting MULTIPLE threats
• Guard patrol or creature appears IMMEDIATELY at your location
• Environmental hazard triggers (collapse, chemical spill, explosive ignition)
• Injury is SEVERE (deep arterial cut, broken bone, concussion, burns)
• Hidden threat reveals itself (trap, ambush, creature lair)
Make it DANGEROUS and IMPACTFUL."
```

**Impact:** UNLUCKY fate now causes serious harm, not just inconvenience.

---

### 4. **Threat Frequency**

**BEFORE:**
- Guards: RARE (only when dramatically necessary)
- Creatures: Occasional
- Environment: Mostly abandoned and safe

**AFTER:**
- Guards: Sometimes (frequent enough to maintain tension)
- Creatures: Sometimes (active hunting presence)
- Psychopathic miners: Sometimes
- Activists/journalists: Sometimes (hostile encounters possible)
- Paranormal anomalies: Sometimes
- Environment: Mix of empty areas AND active threat zones

---

## 🎲 Expected Gameplay Changes

### Player Death Rate
- **Before:** Rare (only from truly reckless choices)
- **After:** Common (from moderate risks + bad luck)

### Cautious Play
- **Before:** Guaranteed safety
- **After:** Safer, but not safe (ambient threats can still strike)

### Movement
- **Before:** Safe if done carefully
- **After:** Always carries risk of detection, injury, or environmental hazards

### UNLUCKY Fate Rolls
- **Before:** Minor complications (equipment failure, noise)
- **After:** Severe harm (broken bones, creature ambush, explosions)

---

## ⚠️ Design Trade-offs

### ✅ **Pros:**
- Much more tense and exciting
- Fate rolls feel meaningful and dangerous
- No more "safe" exploration - always on edge
- Rewards careful planning and risk assessment
- Higher replayability (deaths are expected, not failures)

### ⚠️ **Cons:**
- Higher death rate may frustrate some players
- Less "power fantasy" - you're vulnerable
- Random bad luck can kill even careful players
- Stealth is harder (no guarantee of safety)

---

## 🎮 Player Strategy Adaptation

Players will need to:
1. Accept death as part of the experience
2. Move more cautiously (but know it's not foolproof)
3. Avoid medium-risk actions unless necessary
4. Never take high-risk actions unless desperate
5. Expect UNLUCKY fates to be potentially fatal
6. Use environment creatively to avoid direct confrontation

---

## 🔧 Future Tuning Options

If difficulty feels TOO hard:
- Reduce UNLUCKY fate from 25% to 15%
- Make cautious actions 100% safe (remove ambient threat)
- Lower guard patrol frequency slightly

If difficulty feels TOO easy:
- Increase UNLUCKY fate to 35%
- Make ALL movement carry detection risk
- Add "ambient danger" system that triggers threats randomly

---

## 📊 Testing Recommendations

1. Monitor player death rate over 10 sessions
2. Track deaths by category:
   - Poor choice (deserved)
   - Medium risk + UNLUCKY fate
   - Ambient threat during cautious action
   - Random bad luck
3. Adjust based on feedback

---

**Implemented by:** AI Assistant  
**Approved by:** User (requested "Option 2 but change world from Fair but Dangerous to just Dangerous")

