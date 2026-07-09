# Tape & Video Segment Session Isolation - Complete

## Summary

All tape (GIF) animations and video segments (Veo) are now stored per session, ensuring complete isolation between concurrent game instances.

## What Changed

### 1. Bot.py - Tape Creation

**Before:**
- Tapes stored in global `ROOT / "tapes"` directory
- Video segments collected from global `ROOT / "films" / "segments"` directory
- All sessions shared the same tape storage

**After:**
- Added helper functions to get session-specific directories:
  - `_get_tapes_dir()` → `sessions/{session_id}/tapes/`
  - `_get_segments_dir()` → `sessions/{session_id}/films/segments/`
- All tape creation now uses session-specific paths
- Video segment collection scoped to current session

### 2. Engine.py - Video Generation

**Before:**
- Video directories not passed to Veo utilities
- Veo used hardcoded global directories

**After:**
- Added helper functions:
  - `_get_video_segments_dir(session_id)` → `sessions/{session_id}/films/segments/`
  - `_get_video_films_dir(session_id)` → `sessions/{session_id}/films/final/`
- `generate_frame_via_video()` now passes session-specific directories to Veo

### 3. Veo_video_utils.py - Video Segment Storage

**Before:**
- Hardcoded global directories:
  ```python
  VIDEO_SEGMENTS_DIR = Path("films/segments")
  VIDEO_FILMS_DIR = Path("films/final")
  ```
- All sessions wrote to same directories
- Risk of file name collisions and mixed session data

**After:**
- Removed hardcoded global directories
- Updated function signatures to accept directory parameters:
  - `generate_frame_via_video(video_segments_dir, video_films_dir)`
  - `_generate_video_and_extract_frame(video_segments_dir)`
  - `stitch_video_segments(video_segments_dir, video_films_dir)`
  - `get_video_segments_for_session(video_segments_dir)`
- All functions fallback to defaults if no directory provided (backward compatibility)
- Video segments saved to session-specific paths

## Directory Structure

```
sessions/
├── default/
│   ├── state.json
│   ├── history.json
│   ├── images/
│   │   ├── frame_0.png
│   │   ├── frame_1.png
│   │   └── ...
│   ├── tapes/                    # 🆕 Session-specific tape storage
│   │   ├── tape_20251219_193000.gif
│   │   └── tape_20251219_194500.gif
│   ├── films/
│   │   ├── segments/             # 🆕 Session-specific video segments
│   │   │   ├── seg_0_1_timestamp.mp4
│   │   │   ├── seg_1_2_timestamp.mp4
│   │   │   └── ...
│   │   └── final/                # 🆕 Session-specific stitched videos
│   │       └── HD_tape_20251219_193000.mp4
│   └── videos/                   # Existing session videos
│
├── session_abc123/              # Another concurrent session
│   ├── state.json
│   ├── history.json
│   ├── images/
│   ├── tapes/                    # Completely isolated tapes
│   └── films/
│       ├── segments/             # Completely isolated video segments
│       └── final/                # Completely isolated stitched videos
│
└── session_xyz789/              # Yet another concurrent session
    └── ...
```

## Key Benefits

1. **Complete Isolation**: Each session's tapes and video segments are completely isolated
2. **No Collisions**: No risk of file name conflicts between sessions
3. **Concurrent Safety**: Multiple users can create tapes simultaneously without interference
4. **Easy Cleanup**: Delete a session directory to remove all associated media
5. **Easy Debugging**: Each session's visual history is self-contained
6. **Web UI Ready**: Future web UIs can easily access session-specific tape galleries

## Testing

- ✅ Bot starts successfully with new directory structure
- ✅ No linter errors in modified files
- ✅ Session directories created on-demand
- ✅ Backward compatibility maintained (defaults to global paths if no session_id provided)

## Files Modified

1. **bot.py** (lines 125-144)
   - Added `_get_tapes_dir()` and `_get_segments_dir()` helper functions
   - Updated tape creation to use session-specific directories

2. **engine.py** (lines 165-177)
   - Added `_get_video_segments_dir()` and `_get_video_films_dir()` helper functions
   - Updated Veo call to pass session-specific directories

3. **veo_video_utils.py**
   - Removed hardcoded `VIDEO_SEGMENTS_DIR` and `VIDEO_FILMS_DIR`
   - Updated all function signatures to accept directory parameters
   - Added fallback logic for backward compatibility

## Related Documentation

- See `SESSION_REFACTOR_COMPLETE.md` for overall session isolation architecture
- See `API_WRAPPER_DOCUMENTATION.md` for API endpoint support

---
**Status:** ✅ Complete & Deployed
**Date:** December 19, 2025

