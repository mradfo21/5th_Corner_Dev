# Session Logic Deep Dive Audit Report
**Date:** December 22, 2025  
**Scope:** Complete project-wide session handling review  
**Status:** 🔴 CRITICAL ISSUES FOUND

---

## Executive Summary

The session system has **fundamental architectural issues** that prevent true multi-channel support. While the core engine and API client are properly designed for sessions, the Discord bot layer has several critical bugs that cause all games to share the same session state.

### 🚨 Critical Issues Found: **4**
### ⚠️ Medium Issues Found: **3**
### ✅ Working Correctly: **Most engine/API code**

---

## 🚨 CRITICAL ISSUES

### Issue #1: Bot Never Sets engine.session_id ❌ CRITICAL

**Location:** `bot.py` - All game flows  
**Severity:** CRITICAL  
**Impact:** All channels share the 'default' session, making multi-channel play impossible

**Problem:**
```python
# bot.py line 2649 (and 8 other places)
session_id = str(interaction.channel_id) if interaction.channel_id else 'default'

# Then we pass it to functions:
engine.generate_intro_image_fast(session_id)

# BUT the engine object is a GameEngineClient with session_id='default'
# And we never update it!
```

The bot calculates the correct `session_id` from the channel ID, but only passes it to individual function calls. The `engine` object's `session_id` property remains `'default'` forever.

**Why This Breaks:**
1. `_run_images` is a single global list shared across all channels
2. `_get_tapes_dir()` uses `engine.session_id` which is always 'default'
3. `_get_segments_dir()` uses `engine.session_id` which is always 'default'
4. Death tapes always save to the wrong session

**Fix Required:**
```python
# Option 1: Set engine.session_id at the start of each game flow
session_id = str(interaction.channel_id) if interaction.channel_id else 'default'
engine.session_id = session_id  # CRITICAL FIX

# Option 2: Make _run_images session-aware (dict of lists)
_run_images = {}  # {session_id: [images]}

# Option 3: Pass session_id to _create_death_replay_tape_with_lock()
```

**Recommendation:** Implement all three options for robustness.

---

### Issue #2: _run_images is Global (Not Session-Aware) ❌ CRITICAL

**Location:** `bot.py` line 149  
**Severity:** CRITICAL  
**Impact:** Multiple games will corrupt each other's tape recordings

**Problem:**
```python
_run_images = []  # Single global list!

# Channel A records frames: [A1, A2, A3]
# Channel B starts and records: [A1, A2, A3, B1, B2]
# Channel A dies: Tape contains B's frames!
```

**Fix Required:**
```python
# Make it a dictionary keyed by session_id
_run_images_by_session = {}  # {session_id: [images]}

def _record_frame(session_id: str, image_path: str):
    """Record a frame for a specific session"""
    if session_id not in _run_images_by_session:
        _run_images_by_session[session_id] = []
    _run_images_by_session[session_id].append(image_path)

def _create_death_replay_tape_with_lock(session_id: str):
    """Pass session_id to tape creation"""
    images = _run_images_by_session.get(session_id, [])
    # ... create tape from images
```

---

### Issue #3: Reset Functions Don't Pass session_id ❌ HIGH

**Location:** `bot.py` lines 838, 2017, 2389  
**Severity:** HIGH  
**Impact:** Reset commands always reset 'default' session, not the active channel

**Problem:**
```python
# Line 838 - ChoiceButton._do_reset_static()
engine.reset_state()  # ❌ Always resets 'default'!

# Line 2017 - RestartButton callback
engine.reset_state()  # ❌ Always resets 'default'!

# Line 2389 - /restart command
engine.reset_state()  # ❌ Always resets 'default'!
engine.generate_intro_turn()  # ❌ Always uses 'default'!
```

**Fix Required:**
```python
# Need to determine session_id before reset
session_id = str(interaction.channel_id) if interaction.channel_id else 'default'
engine.reset_state(session_id)
engine.generate_intro_turn(session_id)
```

**Affected Code Locations:**
- `bot.py:838` - `_do_reset_static()` 
- `bot.py:2017` - `RestartButton` callback
- `bot.py:2389` - `/restart` command handler

---

### Issue #4: Tape Creation Functions Don't Accept session_id ❌ HIGH

**Location:** `bot.py` lines 231, 252, 307  
**Severity:** HIGH  
**Impact:** Death tapes always save to 'default' session directory

**Problem:**
```python
def _create_death_replay_tape_with_lock() -> tuple[Optional[str], str]:
    # No session_id parameter!
    result = _create_death_replay_tape()
    return result

def _create_death_replay_tape() -> tuple[Optional[str], str]:
    # Uses _get_segments_dir() which reads engine.session_id
    # But engine.session_id is always 'default'!
    segments_dir = _get_segments_dir()
```

**Fix Required:**
```python
def _create_death_replay_tape_with_lock(session_id: str = 'default') -> tuple[Optional[str], str]:
    result = _create_death_replay_tape(session_id)
    return result

def _create_death_replay_tape(session_id: str = 'default') -> tuple[Optional[str], str]:
    # Get session-specific directories
    tapes_dir = Path("sessions") / session_id / "tapes"
    segments_dir = Path("sessions") / session_id / "films" / "segments"
    
    # Use session-specific image list
    images = _run_images_by_session.get(session_id, [])
```

**All Call Sites Need Update:**
- `bot.py:1083` - ChoiceButton death handler
- `bot.py:1502` - CustomActionModal death handler  
- `bot.py:1932` - countdown_timer_task death handler
- `bot.py:3420` - auto_advance_turn death handler
- `bot.py:3871` - auto_advance_turn (second occurrence)

---

## ⚠️ MEDIUM ISSUES

### Issue #5: Startup Reset Doesn't Specify Session ⚠️

**Location:** `bot.py` line 104  
**Severity:** MEDIUM  
**Impact:** Bot startup always resets 'default', may not be intended behavior

**Current Code:**
```python
if not RESUME_MODE:
    engine.reset_state()  # Always resets 'default'
```

**Recommendation:**
This is probably **intended behavior** (reset the default session on startup), but should be documented clearly.

---

### Issue #6: _get_tapes_dir() and _get_segments_dir() Use getattr() ⚠️

**Location:** `bot.py` lines 133, 143  
**Severity:** MEDIUM  
**Impact:** Brittle session detection, falls back to 'default' silently

**Problem:**
```python
session_id = getattr(engine, 'session_id', 'default')
```

This assumes `engine.session_id` exists and is set correctly. As we've seen, it's always 'default'.

**Fix Required:**
Pass `session_id` explicitly to these functions:
```python
def _get_tapes_dir(session_id: str = 'default'):
    """Get session-specific tapes directory"""
    from pathlib import Path
    session_root = Path("sessions") / session_id
    tapes_dir = session_root / "tapes"
    tapes_dir.mkdir(parents=True, exist_ok=True)
    return tapes_dir
```

---

### Issue #7: Auto-Advance Uses channel.id But May Not Be Consistent ⚠️

**Location:** `bot.py` line 3716  
**Severity:** LOW  
**Impact:** Auto-advance should use the same session_id logic as manual play

**Current Code:**
```python
session_id = str(channel.id) if hasattr(channel, 'id') else 'default'
```

**Recommendation:**
Consistent with manual play, but should be verified that `channel.id` === `interaction.channel_id` in all contexts.

---

## ✅ WHAT'S WORKING CORRECTLY

### Engine Functions ✅
- All engine functions accept `session_id` with 'default' as fallback
- State files properly save to `sessions/{session_id}/state.json`
- History files properly save to `sessions/{session_id}/history.json`
- Images save to `sessions/{session_id}/images/`
- Videos save to `sessions/{session_id}/films/`

### API Client ✅
- `GameEngineClient` has `session_id` property
- All methods accept optional `session_id` parameter
- Falls back to `self.session_id` if not provided
- Properly routes to engine or HTTP API based on mode

### Bot Session ID Calculation ✅
- Bot correctly derives session_id from channel ID
- Consistently uses `str(interaction.channel_id)` pattern
- Passes session_id to most engine function calls

---

## 📋 RECOMMENDED FIX PRIORITY

### Phase 1: Critical Fixes (Must Do Before Multi-Channel Support)

1. **Make _run_images session-aware** ⚠️ CRITICAL
   - Change to dictionary: `_run_images_by_session = {}`
   - Update all append operations
   - Update tape creation to read from dict

2. **Update tape creation functions to accept session_id** ⚠️ CRITICAL
   - `_create_death_replay_tape_with_lock(session_id)`
   - `_create_death_replay_tape(session_id)`
   - `_create_death_replay_gif(session_id)`
   - Update all 6 call sites

3. **Fix reset functions to pass session_id** ⚠️ HIGH
   - Update 3 reset locations to pass session_id
   - Test reset in non-default channels

4. **Fix directory helper functions** ⚠️ MEDIUM
   - `_get_tapes_dir(session_id)`
   - `_get_segments_dir(session_id)`
   - Remove `getattr(engine, 'session_id')` pattern

### Phase 2: Robustness (Nice to Have)

5. **Consider setting engine.session_id dynamically**
   - Would make getattr() patterns work
   - But introduces state mutation concerns
   - May not be necessary if Phase 1 is complete

6. **Add session isolation tests**
   - Test two simultaneous games in different channels
   - Verify tapes don't cross-contaminate
   - Verify state files don't conflict

---

## 🎯 CURRENT STATE SUMMARY

### What Works for Single-Channel (Default) Play ✅
- All engine functions
- API client
- State persistence
- Image generation
- History tracking
- Death detection

### What Breaks for Multi-Channel Play ❌
- Death tape recording (wrong session)
- Image tracking across games (shared list)
- Reset operations (wrong session)
- Session directory lookups (always 'default')

### What Works for Multi-Channel Play ✅
- State isolation (engine level)
- Image generation (engine level)  
- History isolation (engine level)
- Flipbook generation (if session_id passed correctly)

---

## 🔧 EXAMPLE FIX (Issue #1 & #2 Combined)

```python
# bot.py - Top of Discord-enabled section

# Session-aware image tracking
_run_images_by_session = {}  # {session_id: [image_paths]}
_session_locks = {}  # {session_id: threading.Lock}

def _record_frame(session_id: str, image_path: str):
    """Record a frame for a specific session's tape"""
    if session_id not in _run_images_by_session:
        _run_images_by_session[session_id] = []
    _run_images_by_session[session_id].append(image_path)
    print(f"[TAPE] Frame recorded for session {session_id}: {image_path}")

def _get_session_frames(session_id: str) -> list:
    """Get all recorded frames for a session"""
    return _run_images_by_session.get(session_id, [])

def _clear_session_frames(session_id: str):
    """Clear recorded frames for a session (after successful tape creation)"""
    if session_id in _run_images_by_session:
        _run_images_by_session[session_id] = []

# Then update tape creation:
def _create_death_replay_tape_with_lock(session_id: str = 'default') -> tuple[Optional[str], str]:
    """Thread-safe wrapper for tape creation"""
    # Get or create session-specific lock
    if session_id not in _session_locks:
        _session_locks[session_id] = threading.Lock()
    
    with _session_locks[session_id]:
        return _create_death_replay_tape(session_id)

def _create_death_replay_tape(session_id: str = 'default') -> tuple[Optional[str], str]:
    """Create tape from session-specific frames"""
    images = _get_session_frames(session_id)
    
    if len(images) < 2:
        return None, f"Not enough frames for session {session_id}"
    
    # Create tape...
    # Use session-specific tapes directory
    tapes_dir = Path("sessions") / session_id / "tapes"
    tapes_dir.mkdir(parents=True, exist_ok=True)
    
    # ... rest of tape creation
```

---

## 📊 IMPACT ANALYSIS

### If Fixes Are NOT Applied:
- ❌ Multi-channel play will fail (all channels share state)
- ❌ Death tapes will be corrupted/wrong
- ❌ Reset commands won't work correctly
- ❌ Production deployment unsafe for multiple Discord servers

### If Fixes ARE Applied:
- ✅ True multi-channel support
- ✅ Isolated game sessions per channel
- ✅ Correct tape creation per session
- ✅ Safe for production deployment
- ✅ Foundation for web portal (multi-user)

---

## 🚀 DEPLOYMENT SAFETY

**Current Status:** 🟡 SAFE FOR SINGLE-CHANNEL TESTING  
**Recommended Status:** 🔴 NOT SAFE FOR MULTI-CHANNEL PRODUCTION

### Safe Deployment Conditions:
1. Only one Discord channel uses the bot at a time
2. Reset between games (clears shared state)
3. No simultaneous games across channels

### Unsafe Deployment Scenarios:
1. Multiple Discord channels playing simultaneously
2. Multiple Discord servers with the bot
3. Web portal + Discord bot running together

---

## 📝 CONCLUSION

The session system is **architecturally sound** at the engine/API layer, but the Discord bot layer has **critical bugs** that prevent true multi-session support. 

**The good news:** All bugs are fixable with localized changes to `bot.py`.

**The bad news:** Without these fixes, multi-channel/multi-server deployment will cause data corruption.

**Recommendation:** Implement Phase 1 fixes before any production deployment beyond single-channel testing.

---

**Audit Completed:** December 22, 2025  
**Next Steps:** Implement Phase 1 critical fixes

