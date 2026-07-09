# 🔍 ROBUSTNESS AUDIT REPORT - FRESH REVIEW
**Date:** December 11, 2025  
**Scope:** Deep dive for race conditions, concurrency issues, and edge cases

---

## 🔴 CRITICAL BUGS FOUND

### **BUG #1: Double-Click Race Condition on Choice Buttons** 🔴 CRITICAL
**Severity:** HIGH  
**Location:** `bot.py` - `ChoiceButton.callback()` (line 397+)

#### Problem:
When a player clicks a choice button, the buttons are **NOT immediately disabled**. If the player:
- Double-clicks the same button
- Clicks another button before processing completes
- Has network lag causing delayed clicks

**Result:** Multiple concurrent calls to `engine.advance_turn_image_fast()`, causing:
- Race conditions on global `state` and `history`
- Duplicate image generation (wasted API calls)
- Corrupted game state
- Potential history corruption

#### Current Flow (BROKEN):
```python
async def callback(self, interaction):
    await interaction.response.defer()  # Only prevents Discord timeout
    # ❌ NO BUTTON DISABLING HERE!
    
    # Start processing immediately...
    phase1_task = loop.run_in_executor(None, engine.advance_turn_image_fast, ...)
    # ← Player can still click other buttons during this!
```

#### What Should Happen:
```python
async def callback(self, interaction):
    await interaction.response.defer()
    
    # ✅ IMMEDIATELY disable ALL buttons before processing
    view = self.view
    for item in view.children:
        item.disabled = True
    if hasattr(view, 'last_choices_message') and view.last_choices_message:
        await view.last_choices_message.edit(view=view)
    
    # Now safe to process...
```

#### Impact:
- **High frequency:** Could happen on every turn with laggy connections
- **Severity:** Can corrupt game state requiring full restart
- **User experience:** Confusing behavior, duplicate messages

---

### **BUG #2: Inconsistent State Locking in engine.py** 🟡 MEDIUM
**Severity:** MEDIUM  
**Location:** `engine.py` - Multiple `_save_state()` calls

#### Problem:
The `_save_state()` function has a comment "Lock should be managed by the caller" (line 219), but many callers **do NOT acquire the lock**:

**WITH lock (correct):**
- Line 1132: `with WORLD_STATE_LOCK: _save_state(state)`
- Line 1152: `with WORLD_STATE_LOCK: _save_state(state)`
- Line 1176: `with WORLD_STATE_LOCK: _save_state(state)`

**WITHOUT lock (problematic):**
- Line 1228: `_save_state(state)` ❌
- Line 1395: `_save_state(state)` ❌
- Line 1483: `_save_state(state)` ❌
- Line 1491: `_save_state(state)` ❌
- Line 1516: `_save_state(state)` ❌
- Line 1553: `_save_state(state)` ❌
- **Line 2266: `_save_state(state)` ❌** ← In `advance_turn_image_fast()`, called by Discord bot!

#### Current Flow:
```python
# Two concurrent Discord button clicks:
Thread A: advance_turn_image_fast() → _save_state()
Thread B: advance_turn_image_fast() → _save_state()
# ❌ Both write to world_state.json simultaneously!
```

#### Mitigation:
The code uses `os.replace()` with a temp file, which is atomic on most systems. However:
- Both threads create `.json.tmp` files
- One will overwrite the other's changes
- Race window: Thread A writes temp, Thread B writes temp, Thread A replaces, Thread B replaces
- **Thread A's changes are lost!**

#### Fix Options:
1. **Quick:** Acquire lock inside `_save_state()` (make it self-locking)
2. **Better:** Audit all callers and add locks
3. **Best:** Use a write queue to serialize all state saves

---

## ⚠️ POTENTIAL ISSUES

### **ISSUE #1: History Not Reloaded After Concurrent Writes**
**Severity:** LOW  
**Location:** `engine.py` - Global `history` variable

The `history` list is loaded once at module init (line 211-214) and then mutated with `history.append()`. If multiple concurrent operations modify history, they operate on an in-memory list that may be stale.

**Example:**
```python
Turn 1: history = [entry1, entry2]  # In memory
Turn 2: history.append(entry3)      # In memory: [entry1, entry2, entry3]
Turn 2: Write to disk               # Disk: [entry1, entry2, entry3]
Turn 3: history.append(entry4)      # In memory: [entry1, entry2, entry3, entry4]
Turn 3: Write to disk               # Disk: [entry1, entry2, entry3, entry4]
```

This works IF operations are serialized. But with concurrent Discord bot tasks, it can fail.

**Mitigation:** Discord bot is likely single-channel, so this rarely triggers.

---

### **ISSUE #2: Global ENGINE_ENABLED Flags Modified at Runtime**
**Severity:** LOW  
**Location:** `bot.py` lines 1486, 1616

```python
engine.IMAGE_ENABLED = True   # PlayButton
engine.IMAGE_ENABLED = False  # PlayNoImagesButton
```

These are global flags shared across the entire process. If someone implements multi-channel support, this would break.

**Risk:** Low for current single-channel design, but fragile for future scaling.

---

### **ISSUE #3: No Timeout on Micro-Reaction LLM Call**
**Severity:** LOW  
**Location:** `bot.py` line 431

```python
micro_reaction = await asyncio.get_running_loop().run_in_executor(
    None, lambda: engine._ask(micro_prompt, model="gpt-4o-mini", temp=0.4, tokens=50)
)
```

This has no timeout. If the API hangs, the choice processing is blocked indefinitely. 

**Current mitigation:** Wrapped in try/except with fallback, but still blocks the executor thread.

---

## ✅ THINGS THAT ARE CORRECT

### Countdown Timer Cancellation ✅
Properly uses `CancelledError` and cleans up messages. Race condition between countdown and button click is handled correctly by cancelling the countdown task.

### Image Failure Recovery ✅
Automatically skips `None` images in history when collecting reference images for img2img.

### API Error Handling ✅
Comprehensive error handling for:
- Gemini timeouts (now dynamic based on image count)
- Missing `candidates` in responses
- File I/O failures
- Discord API failures

### Death Sequence Double-Restart ✅
Fixed with event flags to prevent concurrent restarts.

### VHS Tape Reset ✅
Now properly clears `_run_images` on restart.

---

## 📊 PRIORITY FIXES

### P0 (Critical - Fix Immediately):
1. **Double-click protection on ChoiceButton** - Add immediate button disabling
2. **Double-click protection on CustomActionModal** - Same issue

### P1 (High - Fix Soon):
1. **State locking consistency** - Either lock inside `_save_state()` or audit all callers

### P2 (Medium - Consider for next release):
1. **History reload on concurrent access** - Reload from disk before appending
2. **Timeout on micro-reaction** - Add 10s timeout to executor call

### P3 (Low - Monitor):
1. **Global IMAGE_ENABLED flags** - Refactor if multi-channel support is added

---

## 🔧 RECOMMENDED IMMEDIATE FIXES

### Fix #1: Add Button Disabling (CRITICAL)

In `bot.py`, add this immediately after `defer()` in **both** `ChoiceButton.callback()` and `CustomActionModal.on_submit()`:

```python
async def callback(self, interaction: discord.Interaction):
    # ... existing cancel tasks code ...
    
    try:
        await interaction.response.defer()
    except Exception:
        pass
    
    # ✅ ADD THIS: Disable ALL buttons immediately
    view = self.view
    if view and hasattr(view, 'children'):
        for item in view.children:
            item.disabled = True
        try:
            if hasattr(view, 'last_choices_message') and view.last_choices_message:
                await view.last_choices_message.edit(view=view)
                print("[CHOICE] ✅ Buttons disabled to prevent double-click")
        except Exception as e:
            print(f"[CHOICE] Warning: Could not disable buttons: {e}")
    
    # ... rest of existing code ...
```

### Fix #2: Add Lock Inside _save_state() (HIGH PRIORITY)

In `engine.py`, modify `_save_state()`:

```python
def _save_state(st: dict):
    with WORLD_STATE_LOCK:  # ✅ ADD THIS
        st["last_saved"] = datetime.now(timezone.utc).isoformat()
        temp_state_file = STATE_PATH.with_suffix(".json.tmp")
        # ... rest of existing code ...
```

This makes locking automatic and prevents all race conditions.

---

## 🎯 TESTING RECOMMENDATIONS

### To Verify Fix #1 (Double-Click Protection):
1. Start game
2. Rapidly click same button 3-4 times
3. Expected: Only one turn processes
4. Check logs for duplicate `[FATE] Computed:` or `[IMG FAST]` messages

### To Verify Fix #2 (State Locking):
1. Run game for 10+ turns
2. Check `world_state.json` for corruption
3. No missing turns in history
4. Turn counter increments correctly

---

## 📈 OVERALL ASSESSMENT

**Current State:** 🟡 MEDIUM RISK  
**After P0 Fixes:** 🟢 LOW RISK  

The codebase has **excellent error handling** and **graceful degradation**, but has two critical race conditions that can cause corruption with normal use:

1. Double-click race (happens frequently)
2. Concurrent state writes (happens rarely but catastrophic)

**Recommendation:** Fix P0 issues before next deployment. P1-P3 can be addressed in future updates.

---

## 🚀 POST-FIX VALIDATION

After implementing P0 fixes:
- ✅ Run stress test: 20 rapid turns
- ✅ Test double-click behavior
- ✅ Verify history.json integrity
- ✅ Check world_state.json consistency
- ✅ Monitor logs for race condition indicators

**Estimated Fix Time:** 30 minutes  
**Risk Level After Fix:** LOW

