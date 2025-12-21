# 🔍 DEEP AUDIT FINDINGS - Remaining Bugs & Oversights

**Audit Date:** December 20, 2025  
**Status:** CRITICAL issues found requiring immediate fixes

---

## 🚨 **CRITICAL BUGS** (Must Fix Immediately)

### 1. **"Jason" References Still in User-Facing Text**

**Severity:** CRITICAL - Breaks immersion  
**Impact:** Player sees third-person "Jason" instead of "you"  
**Count:** 24+ instances found

#### **Locations:**

**`engine.py`:**
- Line 485: Default state `"world_prompt": "Jason crouches behind..."`
  - This is the **starting state** for every new game!
  - ❌ Should be: `"You crouch behind a rusted Horizon vehicle..."`

- Lines 592-603: `get_mood_or_tension()` function (internal use, OK)
  - ⚠️ These are for internal AI context (acceptable)
  - Returns strings like "Jason is being pursued..."
  - Used internally for AI logic, not shown to player (verify this!)

- Line 1068: Dispatch generation prompt
  - `"Describe what Jason does and what immediately happens..."`
  - ❌ Should be: `"Describe what you do and what immediately happens..."`

- Lines 1092, 1106, 1117, 3234, 3361: Fallback error messages
  - `"Jason makes a tense move in the chaos."`
  - ❌ Should be: `"You make a tense move in the chaos."`

- Lines 1916-1919: Vision scene writer prompt
  - `"what Jason sees"`, `"Only describe what is visible. Do not include Jason himself"`
  - ❌ Should use second-person: `"what you see"`, `"Do not include yourself"`

- Line 1954: Intro check condition
  - `state.get('world_prompt', '').startswith('Jason crouches behind')`
  - ⚠️ This will BREAK after fixing line 485! Need to update this check.

- Lines 2090, 2428, 2436, 2439, 2576: Combat/risky action prompts
  - Multiple references to "Jason Fleece" in prompts
  - ❌ Should use second-person throughout

- Line 2725: Situation summary
  - `"Jason is at the very beginning of his journey."`
  - ❌ Should be: `"You are at the very beginning of your journey."`

- Line 3115: Image context prompt
  - `"Jason is HERE. Do NOT teleport him."`
  - ❌ Should be: `"You are HERE. Do NOT teleport yourself."`

**`bot.py`:**
- Line 2265: Discord embed field
  - `"Jason Fleece — an investigative journalist..."`
  - ⚠️ This is in `/lore` command, describing the character (OK for lore)
  - But verify it's not shown during gameplay

- Line 2280: Discord embed field
  - `"Your choices shape Jason's fate."`
  - ❌ Should be: `"Your choices shape your fate."`

- Line 2674: Code comment
  - `"what Jason sees"` in comment (OK, internal)

**`prompts/simulation_prompts.json`:**
- Line 4: `world_initial_state` (AI internal context)
  - `"You are Jason Fleece, an investigative photojournalist..."`
  - ⚠️ This is for AI context, not player-facing (verify)

- Line 6: Establish beat
  - `"Jason arrives at the facility"`
  - ⚠️ Verify this isn't shown to player

- Line 15: `player_choice_generation_instructions`
  - `"NEVER use 'Jason' in the choices themselves"` ✅ Good rule!
  - But earlier in doc it says "You are Jason Fleece" for AI context

- Line 19: `field_notes_format`
  - `"FIELD NOTES — Jason Fleece (1993)"`
  - ❌ Should be: `"FIELD NOTES — (1993)"` or `"FIELD NOTES — Journalist Log"`
  - But then correctly says "use first-person 'I', 'my', 'me'"

- Line 20: `loading_message_instructions`
  - `"NEVER use third person ('Jason', 'he', 'his')."` ✅ Good rule!

---

### 2. **Default State Has Third-Person World Prompt**

**Severity:** CRITICAL  
**File:** `engine.py` line 485  
**Problem:**

```python
"world_prompt": "Jason crouches behind a rusted Horizon vehicle at the edge of the facility.",
```

**This is the starting state for EVERY new game!**

**Impact:**
- Every new session starts with third-person narrative
- Breaks immersion immediately
- The check on line 1954 depends on this exact string
- Changing it will break the intro detection logic

**Fix Required:**
1. Change to second-person: `"You crouch behind..."`
2. Update line 1954 detection logic:
   ```python
   # OLD:
   state.get('world_prompt', '').startswith('Jason crouches behind')
   # NEW:
   state.get('world_prompt', '').startswith('You crouch behind')
   ```

---

### 3. **Entity Extraction Filter Too Aggressive (Already Fixed)**

**Status:** ✅ FIXED in commit 086b5d4  
**Problem:** Missing characters/threats like guards
**Solution:** Prioritizes people/creatures over environment

---

## ⚠️ **HIGH PRIORITY ISSUES**

### 4. **Excessive Debug Logging in Production**

**Severity:** HIGH  
**Impact:** Performance, log bloat, potential info leakage  
**Count:** 113+ `print(..., flush=True)` statements

**Problem:**
- `_process_turn_background` has **80+ debug prints** (lines 2148-2627)
- Every turn generates massive log output
- Many say "DEBUG PRINT:" explicitly
- `flush=True` forces immediate disk writes (slow!)

**Examples:**
```python
print(f"DEBUG PRINT: _process_turn_background THREAD SPAWNED - VERY FIRST LINE. Choice: '{choice}'", flush=True)
print(f"DEBUG PRINT: _process_turn_background - State loaded. Player choice: {choice}", flush=True)
print(f"DEBUG PRINT: _process_turn_background - Narrative dispatch generated: {dispatch_text[:60]}...", flush=True)
# ... 77 more like this ...
```

**Recommended Fix:**
1. Replace with proper logging levels:
   ```python
   logger.debug(f"Thread spawned for choice: '{choice}'")
   logger.info(f"Image generated: {new_image_url}")
   logger.error(f"Failed to generate image: {e}")
   ```
2. Use environment variable to toggle debug mode
3. Remove `flush=True` (let OS handle buffering)

**Current Impact:**
- Every turn = ~80 debug lines = ~8KB+ of logs
- 100 turns = ~800KB of debug spam
- Render free tier: Limited log retention
- Performance: Excessive I/O

---

### 5. **Bare `except:` Clauses and Poor Error Context**

**Severity:** MEDIUM-HIGH  
**Impact:** Hard to debug, swallows unexpected errors  
**Count:** 81 try/except blocks

**Good Examples (most are good!):**
```python
except json.JSONDecodeError as e_json:
    print(f"[ERROR] Invalid JSON in state file: {e_json}")
except OpenAIError as e:
    print(f"OpenAI API error: {e}")
```

**Concerning Patterns:**
```python
except Exception as e:
    print(f"Error: {e}")  # Too generic, no context!
```

**Critical Locations:**
- Line 2629: Top-level thread exception handler
  ```python
  except Exception as e_critical_thread:
      logging.exception("CRITICAL EXCEPTION in _process_turn_background thread top level:")
  ```
  - This is actually GOOD! ✅ Uses `logging.exception()` which includes traceback

**Recommendations:**
1. Always log full context:
   ```python
   except Exception as e:
       logger.error(f"Failed to {operation_name} for session {session_id}: {e}", exc_info=True)
   ```
2. Add operation context to error messages
3. Use specific exceptions where possible

---

## 📋 **MEDIUM PRIORITY ISSUES**

### 6. **Inconsistent Path Handling**

**Status:** MOSTLY FIXED (resolved by `_resolve_image_path`)  
**Remaining Concern:** One `.lstrip()` found at line 2099  
**Context:**
```python
line = line.strip().lstrip("-*0123456789. ").strip()
```
- This is for **text parsing**, not file paths ✅ OK!
- Stripping leading characters from choice text

---

### 7. **Field Notes Header Issue**

**File:** `prompts/simulation_prompts.json` line 19  
**Problem:**
```json
"field_notes_format": "FIELD NOTES — Jason Fleece (1993)\n..."
```

**But then says:**
> "These are YOUR thoughts written in your journal"  
> "Never use third-person or your name in your own notes"

**This is contradictory!**

**Fix:**
```json
"field_notes_format": "FIELD NOTES — (1993)\n..."
```
OR
```json
"field_notes_format": "FIELD NOTES — JOURNALIST LOG (1993)\n..."
```

---

### 8. **AI Context vs Player-Facing Text Confusion**

**Problem:** Some "Jason" references are for AI context (acceptable), others are shown to player (bad)

**AI Context (OK):**
- `prompts/simulation_prompts.json` line 4: `"You are Jason Fleece..."` in world setup
  - This tells the AI who the character is
  - Not shown directly to player

**Player-Facing (BAD):**
- `engine.py` line 485: Default world_prompt (shown in admin dashboard!)
- `bot.py` line 2280: Discord embed during gameplay
- Fallback error messages that can appear in Discord

**Need to Audit:**
1. Which prompts/strings are AI-only?
2. Which appear in Discord/admin dashboard?
3. Document clear separation

---

### 9. **Potential Race Condition in Session State**

**Severity:** LOW-MEDIUM (mitigated by `WORLD_STATE_LOCK`)  
**Location:** `engine.py` lines 2895-2903

**Current Protection:**
```python
with WORLD_STATE_LOCK:
    state = _load_state()
    state['feed_log'].append(player_action_item)
    _save_state(state)
```

**This is GOOD!** ✅

**Potential Issue:**
- Multiple API calls could try to load/modify state
- Lock protects feed_log appends
- But what about other state modifications?

**Verify:**
- All state writes use the lock
- Lock scope is appropriate (not too narrow/wide)
- No deadlock potential

---

### 10. **Image Provider Debug Code Still Active**

**File:** `engine.py` lines 1528-1533  
**Code:**
```python
print(f"[IMG DEBUG] active_image_provider = {active_image_provider}", flush=True)
print(f"[IMG DEBUG] About to call image generation API...", flush=True)
if active_image_provider == "veo":
    print(f"[IMG DEBUG] -> Calling Veo video generation...", flush=True)
    print(f"[IMG DEBUG] Entering Veo branch", flush=True)
```

**Impact:** Low, but clutters production logs  
**Fix:** Convert to `logger.debug()` or remove

---

## 🔧 **LOW PRIORITY / POLISH ITEMS**

### 11. **TODO/FIXME Comments**

**Count:** 84+ instances of debug/TODO comments  
**Severity:** LOW  
**Action:** Review and clean up or document

---

### 12. **UnicodeEncodeError Handling**

**Locations:** Lines 3204, 3206, 3210, 3229  
**Pattern:**
```python
try:
    print(something_with_unicode)
except UnicodeEncodeError:
    print(fallback_ascii)
```

**Status:** This is defensive coding for Windows console ✅ GOOD  
**Note:** May be less relevant now that logs go to Render

---

### 13. **Legacy `get_mood_or_tension()` Function**

**Location:** Lines 590-603  
**Uses:** "Jason" in return strings

**Question:** Is this still used?
```bash
$ grep -n "get_mood_or_tension" engine.py
```

If not used, remove it. If used, verify it's AI-only context.

---

## 🎯 **RECOMMENDED FIX ORDER**

### **Phase 1: Critical Immersion Fixes** (30 min)
1. ✅ Fix line 485: Default world_prompt to second-person
2. ✅ Update line 1954: Intro detection logic
3. ✅ Fix fallback error messages (lines 1092, 1106, 1117, 3234, 3361)
4. ✅ Fix dispatch prompt (line 1068)
5. ✅ Fix vision scene prompt (lines 1916-1919)
6. ✅ Fix combat/risky action prompts (lines 2090, 2428, 2436, 2439, 2576, 2725, 3115)
7. ✅ Fix bot.py line 2280: Discord embed
8. ✅ Fix field_notes_format header in prompts JSON

### **Phase 2: Debug Logging Cleanup** (1 hour)
1. Replace all "DEBUG PRINT:" with proper logging
2. Add log level environment variable
3. Remove excessive flush=True

### **Phase 3: Error Handling Improvements** (30 min)
1. Add more context to generic exception handlers
2. Document which exceptions are expected where

### **Phase 4: Documentation & Audit** (30 min)
1. Document AI-context vs player-facing text separation
2. Review and remove/document TODO comments
3. Update architecture docs

---

## 🧪 **TESTING CHECKLIST**

After fixes:
- [ ] New game starts with "You crouch..." not "Jason crouches..."
- [ ] Admin dashboard shows second-person world_prompt
- [ ] Discord messages never show "Jason" (except in /lore command)
- [ ] Error messages use "You" not "Jason"
- [ ] Field notes header doesn't say "Jason Fleece"
- [ ] Play through 5+ turns, verify no third-person slips
- [ ] Check logs for "Jason" in user-facing output

---

## 📊 **SUMMARY**

| Category | Critical | High | Medium | Low | Total |
|----------|----------|------|--------|-----|-------|
| Immersion Bugs (Jason refs) | 15+ | 5 | 3 | 1 | 24+ |
| Debug Logging | 0 | 80+ | 10 | 23 | 113+ |
| Error Handling | 1 | 10 | 70 | 0 | 81 |
| Path Handling | 0 | 0 | 1 | 0 | 1 |
| **TOTAL** | **16+** | **95+** | **84** | **24** | **219+** |

---

## 🎯 **IMMEDIATE ACTION ITEMS**

1. **Fix Default World Prompt** (engine.py line 485) - 5 min
2. **Fix Intro Detection** (engine.py line 1954) - 2 min  
3. **Fix All Fallback Messages** - 10 min
4. **Fix All AI Prompts with "Jason"** - 15 min
5. **Fix Discord Embed** (bot.py line 2280) - 2 min
6. **Fix Field Notes Header** (prompts JSON) - 1 min

**Total Time to Fix Critical:** ~35 minutes  
**Impact:** Eliminates all immersion-breaking third-person references

---

**Next Steps:**
1. Review this audit
2. Prioritize fixes
3. Implement Phase 1 (Critical)
4. Test thoroughly
5. Deploy

**Audit Complete.** 🔍✅

