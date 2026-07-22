"""
cast_store.py — hot-reloadable store for `cast.json`, the game's "story
bible" of named characters (Jason Fleece, Kane Fleece, Kelsey Rowe, Hoc,
etc.).

Note: as of this writing `cast.json` is not loaded by any code path in
engine.py / choices.py / gemini_image_utils.py — the protagonist's name and
relationships are currently hardcoded inline in prompt-building code instead.
This store (and its World Studio "Story Bible" panel) exposes and persists
`cast.json` transparently anyway, since it's clearly story content the user
may want to shape, but edits here are reference-only until a future pass
wires character data into the live prompt pipeline. See prompts_store.py's
PROMPT_SCHEMA `live` flags for the fields that *do* affect gameplay today.

Mirrors the same frozen-defaults + immediate-write-and-reload pattern as
prompts_store.py / pricing.py.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).parent.resolve()
CAST_PATH = ROOT / "cast.json"
DEFAULTS_PATH = ROOT / "cast.defaults.json"

_LOCK = threading.Lock()
_cached_cast: List[Dict[str, Any]] = []
_cache_mtime: float = 0.0


def _reload(force: bool = False) -> List[Dict[str, Any]]:
    global _cached_cast, _cache_mtime
    with _LOCK:
        try:
            mtime = CAST_PATH.stat().st_mtime
        except OSError:
            return _cached_cast
        if not force and _cached_cast and mtime == _cache_mtime:
            return _cached_cast
        try:
            with CAST_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("cast.json must contain a JSON array of character objects.")
            _cached_cast = data
            _cache_mtime = mtime
        except FileNotFoundError:
            print(f"[CAST_STORE] {CAST_PATH} not found; cast left empty.", flush=True)
        except Exception as e:
            print(f"[CAST_STORE ERROR] Failed to load {CAST_PATH}: {e}", flush=True)
        return _cached_cast


def load_cast(force: bool = False) -> List[Dict[str, Any]]:
    """Current live cast roster (list of character dicts)."""
    return _reload(force=force)


def load_defaults() -> List[Dict[str, Any]]:
    """The frozen factory-default cast, for diff/reset in the editor."""
    try:
        with DEFAULTS_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[CAST_STORE WARN] {DEFAULTS_PATH} not found; no defaults available.", flush=True)
        return []


def save_cast(cast: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Replace the whole roster and persist immediately."""
    if not isinstance(cast, list):
        raise ValueError("cast must be a list of character objects.")
    for entry in cast:
        if not isinstance(entry, dict) or not str(entry.get("name", "")).strip():
            raise ValueError("Every cast entry needs at least a non-empty 'name'.")
    with _LOCK:
        with CAST_PATH.open("w", encoding="utf-8") as f:
            json.dump(cast, f, indent=2, ensure_ascii=False)
    return _reload(force=True)


def reset_cast() -> List[Dict[str, Any]]:
    """Restore the factory-default cast."""
    defaults = load_defaults()
    if not defaults:
        raise RuntimeError("No defaults file found — refusing to wipe cast.json.")
    return save_cast(defaults)


# Warm the cache at import time.
_reload(force=True)
