"""
Runtime knobs the editor can actually turn.

Everything in here used to be an environment variable read once at boot, which
made a whole class of setting unreachable from inside the game: the editor could
*report* that SCAN was falling back to Gemini, or that camp seats five, but not
change either. A panel full of facts you cannot act on reads as broken UI.

So: a tiny store with three jobs.

  · Hold the knobs on disk (``tunables.json``) so a change survives a restart.
  · Apply them to the live modules the moment they change — these are module
    globals in engine/local_vision, and the whole point is not having to redeploy.
  · Refuse anything out of range, because this is reachable from a browser.

Deliberately small. A knob belongs here only if it is safe to change while a
game is running and someone would plausibly want to. Anything that needs a
process restart stays an environment variable, and anything secret (keys, agent
ids) never comes near it.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "tunables.json"

_LOCK = threading.Lock()

# name -> (kind, default, validator). `kind` is what the UI should draw.
SCHEMA: Dict[str, Dict[str, Any]] = {
    "detect_backend": {
        "kind": "enum",
        "label": "Detector",
        "options": ["gemini", "local", "auto"],
        "default": None,          # None = "leave whatever the server booted with"
        "help": "Which detector answers SCAN. On-device is faster and free; "
                "Gemini is slower and costs a call.",
    },
    "detect_min_score": {
        "kind": "number",
        "label": "Min score",
        "min": 0.05,
        "max": 0.9,
        "step": 0.01,
        "default": None,
        "help": "How sure the on-device detector has to be before it names "
                "something. Lower finds more, and more rubbish.",
    },
    "camp_companion_cap": {
        "kind": "number",
        "label": "Seats",
        "min": 1,
        "max": 5,
        "step": 1,
        "default": 5,
        "help": "How many companions come to the fire. The image model takes "
                "six references, and the jeep is one of them.",
    },
    "camp_include_jeep": {
        "kind": "bool",
        "label": "Bring the jeep",
        "default": True,
        "help": "Include the jeep as a reference so camp keeps the same vehicle.",
    },
    "default_voice_id": {
        "kind": "voice",
        "label": "Default voice",
        "default": None,
        "help": "Who speaks when a character has not been cast.",
    },
    "narrator_voice_id": {
        "kind": "voice",
        "label": "Narrator",
        "default": None,
        "help": "The voice that reads the story back to you.",
    },
}


def _coerce(name: str, value: Any) -> Any:
    """Validate one knob, or raise ValueError. None always means 'unset'."""
    spec = SCHEMA.get(name)
    if spec is None:
        raise ValueError(f"unknown setting {name!r}")
    if value is None or value == "":
        return None
    kind = spec["kind"]
    if kind == "enum":
        v = str(value).strip().lower()
        if v not in spec["options"]:
            raise ValueError(f"{name} must be one of {spec['options']}")
        return v
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if kind == "number":
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number")
        if v < spec["min"] or v > spec["max"]:
            raise ValueError(f"{name} must be between {spec['min']} and {spec['max']}")
        # Whole numbers stay whole, so a seat count is 4 and not 4.0.
        return int(v) if float(spec.get("step") or 0) >= 1 else round(v, 3)
    if kind == "voice":
        v = str(value).strip()
        # Voice ids are opaque; the only thing worth enforcing is that this is
        # an id and not a paragraph.
        if len(v) > 64 or not v.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"{name} does not look like a voice id")
        return v
    raise ValueError(f"unhandled kind {kind!r}")


def load() -> Dict[str, Any]:
    with _LOCK:
        if not STORE.exists():
            return {}
        try:
            data = json.loads(STORE.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            return {}
    return {k: v for k, v in data.items() if k in SCHEMA}


def _write(data: Dict[str, Any]) -> None:
    with _LOCK:
        tmp = STORE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, STORE)


def current() -> Dict[str, Any]:
    """Every knob's EFFECTIVE value: what is stored, or what the live module is
    actually using. The UI needs the second one — a select showing nothing
    because the value came from the environment is the same dead text this store
    exists to replace.
    """
    saved = load()
    out: Dict[str, Any] = {}
    for name, spec in SCHEMA.items():
        if name in saved and saved[name] is not None:
            out[name] = saved[name]
            continue
        out[name] = _boot_value(name)
    return out


# What the process booted with, captured before anything is applied. Without it,
# clearing a knob had nothing to restore: the "live" value it fell back to was
# the one we had already overwritten, so a cleared setting stayed in force until
# the next restart.
_BOOT: Dict[str, Any] = {}


def _boot_value(name: str) -> Any:
    if name not in _BOOT:
        _BOOT[name] = _live(name, SCHEMA[name])
    return _BOOT[name]


def _snapshot_boot() -> None:
    for name in SCHEMA:
        _boot_value(name)


def _live(name: str, spec: Dict[str, Any]) -> Any:
    """Read the value the running process is using, so nothing shows as blank."""
    try:
        if name == "detect_backend":
            import engine
            return getattr(engine, "DETECT_BACKEND", None)
        if name == "detect_min_score":
            import local_vision
            return getattr(local_vision, "MIN_SCORE", None)
        if name == "camp_companion_cap":
            import engine
            return getattr(engine, "CAMP_COMPANION_CAP", spec.get("default"))
        if name == "camp_include_jeep":
            import engine
            return getattr(engine, "CAMP_INCLUDE_JEEP", spec.get("default"))
        if name == "default_voice_id":
            import engine
            return engine._default_voice_id()
        if name == "narrator_voice_id":
            import engine
            return getattr(engine, "ELEVENLABS_NARRATOR_VOICE_ID", None) or None
    except Exception:  # noqa: BLE001
        pass
    return spec.get("default")


def apply_all() -> None:
    """Push the stored knobs into the live modules. Called at import time by the
    API and again after every write, so a saved setting holds across restarts.
    """
    _snapshot_boot()
    for name, value in load().items():
        if value is None:
            continue
        try:
            _apply_one(name, value)
        except Exception as e:  # noqa: BLE001
            print(f"[TUNABLES] could not apply {name}={value!r}: {e}", flush=True)


def _apply_one(name: str, value: Any) -> None:
    if name == "detect_backend":
        import engine
        engine.DETECT_BACKEND = value
    elif name == "detect_min_score":
        import local_vision
        local_vision.MIN_SCORE = float(value)
    elif name == "camp_companion_cap":
        import engine
        engine.CAMP_COMPANION_CAP = int(value)
    elif name == "camp_include_jeep":
        import engine
        engine.CAMP_INCLUDE_JEEP = bool(value)
    elif name == "narrator_voice_id":
        import engine
        engine.ELEVENLABS_NARRATOR_VOICE_ID = value
    elif name == "default_voice_id":
        import engine
        engine.ELEVENLABS_VOICE_ID = value


def update(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Validate, store and apply. Returns the new effective values."""
    if not isinstance(patch, dict):
        raise ValueError("expected an object")
    clean = {name: _coerce(name, patch[name]) for name in patch}
    data = load()
    for name, value in clean.items():
        if value is None:
            data.pop(name, None)
        else:
            data[name] = value
    _snapshot_boot()
    _write(data)
    # Anything cleared goes back to what the process booted with FIRST, then the
    # rest are applied — otherwise apply_all(), which only walks what is stored,
    # would leave a cleared knob exactly as it was.
    for name, value in clean.items():
        if value is None:
            _restore(name)
    apply_all()
    return current()


def _restore(name: str) -> None:
    boot = _boot_value(name)
    if boot is None:
        return
    try:
        _apply_one(name, boot)
    except Exception as e:  # noqa: BLE001
        print(f"[TUNABLES] could not restore {name}: {e}", flush=True)


def clear() -> Dict[str, Any]:
    """Forget every stored knob and put the live modules back to boot values."""
    _snapshot_boot()
    _write({})
    for name in SCHEMA:
        _restore(name)
    return current()


def schema() -> Dict[str, Any]:
    """The knobs, for a UI that wants to draw them without hardcoding a list."""
    return {name: {k: v for k, v in spec.items() if k != "default"}
            for name, spec in SCHEMA.items()}
