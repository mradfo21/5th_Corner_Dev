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
    # ── Danger ────────────────────────────────────────────────────────────
    # These decide whether a run is survival horror or a stroll, and the right
    # answer depends on how long it is meant to be — a 60-turn render and a
    # ten-minute sitting want different numbers. They were constants in
    # engine.py, which meant editing code to change the difficulty.
    #
    # "Wound cost" and "Recovery" used to live here, driving a hit-point pool.
    # That pool is gone (see engine's "how a run ends"), so detection is the
    # difficulty surface now: how fast the world notices you and how long it
    # stays interested is what decides whether a run is tense or a stroll.
    "detect_cool": {
        "kind": "number",
        "label": "Losing them",
        "min": 0,
        "max": 5,
        "step": 1,
        "default": 1,
        "help": "Heat shed by a turn that draws no attention. 0 means once "
                "you are seen you stay hunted for the rest of the run.",
    },
    "story_escalate_at": {
        "kind": "number",
        "label": "Act two at",
        "min": 2,
        "max": 40,
        "step": 1,
        "default": 4,
        "help": "Accumulated threat before the story tips into escalating. "
                "Raise it for a slower burn.",
    },
    "story_critical_at": {
        "kind": "number",
        "label": "Act three at",
        "min": 3,
        "max": 80,
        "step": 1,
        "default": 9,
        "help": "Accumulated threat before the story tips into critical. "
                "Keep it above act two or the middle act never happens.",
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
    # ── Flipbook ──────────────────────────────────────────────────────────
    # A flipbook turn asks for a grid of panels and splits it back into the
    # in-between frames of the action, instead of one still. The count is a
    # knob because it is a straight quality trade: the panels are slices of ONE
    # generation, so 16 of them are a quarter the width and height of 4. The
    # old build hardcoded 16 and looked like a bootleg.
    "flipbook_enabled": {
        "kind": "bool",
        "label": "Flipbook",
        "default": False,
        "help": "Draw each turn as a short sequence of in-between frames "
                "instead of one still. Costs the same as a still (one "
                "generation) but takes longer to come back. Gemini images only.",
    },
    "flipbook_frames": {
        "kind": "enum",
        "label": "Frames",
        "options": ["2", "4", "8", "16"],
        "default": "4",
        "help": "How many in-betweens per turn. They are slices of one "
                "generation, so fewer means each frame is bigger: 4 frames are "
                "twice the width of 16. Raise the image size to go higher.",
    },
    "flipbook_frame_ms": {
        "kind": "number",
        "label": "Frame hold",
        "min": 80,
        "max": 1000,
        "step": 10,
        "default": 420,
        "help": "Milliseconds each frame stays on screen. 4 frames at 420ms is "
                "under two seconds of motion per turn.",
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
        if name == "detect_cool":
            import engine
            return getattr(engine, "DETECT_COOL", spec.get("default"))
        if name == "story_escalate_at":
            import engine
            return getattr(engine, "STORY_ESCALATE_AT", spec.get("default"))
        if name == "story_critical_at":
            import engine
            return getattr(engine, "STORY_CRITICAL_AT", spec.get("default"))
        if name == "camp_companion_cap":
            import engine
            return getattr(engine, "CAMP_COMPANION_CAP", spec.get("default"))
        if name == "camp_include_jeep":
            import engine
            return getattr(engine, "CAMP_INCLUDE_JEEP", spec.get("default"))
        if name == "flipbook_enabled":
            import engine
            return getattr(engine, "FLIPBOOK_ENABLED", spec.get("default"))
        if name == "flipbook_frames":
            import engine
            return str(getattr(engine, "FLIPBOOK_FRAMES", 0) or spec["default"])
        if name == "flipbook_frame_ms":
            import engine
            return getattr(engine, "FLIPBOOK_FRAME_MS", spec.get("default"))
        if name == "default_voice_id":
            import engine
            return engine._default_voice_id()
        if name == "narrator_voice_id":
            import engine
            return engine._narrator_voice_id()
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
    elif name == "detect_cool":
        import engine
        # Moving has always shed heat faster than standing still; keep that
        # relationship rather than letting the two knobs cross over.
        engine.DETECT_COOL = int(value)
        engine.DETECT_COOL_MOVING = int(value) * 2
    elif name == "story_escalate_at":
        import engine
        engine.STORY_ESCALATE_AT = int(value)
    elif name == "story_critical_at":
        import engine
        engine.STORY_CRITICAL_AT = int(value)
    elif name == "camp_companion_cap":
        import engine
        engine.CAMP_COMPANION_CAP = int(value)
    elif name == "camp_include_jeep":
        import engine
        engine.CAMP_INCLUDE_JEEP = bool(value)
    elif name == "flipbook_enabled":
        import engine
        engine.FLIPBOOK_ENABLED = bool(value)
    elif name == "flipbook_frames":
        import engine
        import flipbook
        # The enum stores a string; the engine wants a count it can build a
        # grid out of, and normalize_frames is the one place that decides.
        engine.FLIPBOOK_FRAMES = flipbook.normalize_frames(value)
    elif name == "flipbook_frame_ms":
        import engine
        engine.FLIPBOOK_FRAME_MS = int(value)
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
