# Image Display Bug Fix

## Problem

After implementing session isolation, images were being **generated successfully** but **not displaying** in Discord.

### Root Cause

The `_attach()` function in `bot.py` was hardcoded to look for images in the legacy global directory:

```python
# OLD CODE (BROKEN)
def _attach(image_path: Optional[str], caption: str = "") -> Tuple[Optional[discord.File], Optional[str]]:
    if not image_path:
        return None, None
    # Always resolve relative to ROOT/images
    if image_path.startswith("/images/"):
        local = ROOT / "images" / Path(image_path).name
    else:
        local = ROOT / "images" / Path(image_path).name
    if local.exists():
        return discord.File(local, filename=local.name), local.name
    return None, None
```

### What Was Happening

1. **Engine generates image** → `sessions/default/images/1768043510_Beyond_the_barbed_wire.png` ✅
2. **Engine returns path** → `C:\...\sessions\default\images\1768043510_Beyond_the_barbed_wire.png` ✅
3. **Bot's `_attach()` function** → Tries to find `C:\...\images\1768043510_Beyond_the_barbed_wire.png` ❌
4. **File not found** → No image sent to Discord ❌

The images were being generated and saved correctly, but the bot was looking in the wrong directory!

## Solution

Updated `_attach()` to handle session-specific absolute paths:

```python
# NEW CODE (FIXED)
def _attach(image_path: Optional[str], caption: str = "") -> Tuple[Optional[discord.File], Optional[str]]:
    if not image_path:
        return None, None
    
    # Handle absolute paths (session-specific images)
    if Path(image_path).is_absolute():
        local = Path(image_path)
    # Handle legacy /images/ paths
    elif image_path.startswith("/images/"):
        local = ROOT / "images" / Path(image_path).name
    # Handle legacy relative paths
    else:
        local = ROOT / "images" / Path(image_path).name
    
    if local.exists():
        return discord.File(local, filename=local.name), local.name
    else:
        print(f"[ATTACH ERROR] Image not found: {local}")
        print(f"[ATTACH ERROR] Original path: {image_path}")
        return None, None
```

### Key Changes

1. ✅ **Absolute path support**: If path is absolute (session-specific), use it directly
2. ✅ **Backward compatibility**: Legacy `/images/` paths still work
3. ✅ **Error logging**: Added debug output when images are not found
4. ✅ **Video support**: `_attach_video()` already handled absolute paths correctly

## Testing

```bash
# Clear sessions and restart
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
Remove-Item -Path "sessions" -Recurse -Force -ErrorAction SilentlyContinue
python bot.py
```

## Expected Behavior

Now when you play the game:
1. Images generate → `sessions/default/images/` ✅
2. Bot finds images → Uses absolute path directly ✅
3. Images display → Sent to Discord successfully ✅

---

**Status:** ✅ Fixed and Deployed  
**Date:** December 19, 2025  
**Files Modified:** `bot.py` (lines 467-486)

