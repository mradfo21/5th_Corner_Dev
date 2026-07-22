"""
prompts_store.py — centralized, hot-reloadable, live-shared store for
`prompts/simulation_prompts.json` (the game's "80% of behavior lives here"
prompt file — see README.md).

Problem this solves
--------------------
Before this module existed, `engine.py`, `gemini_image_utils.py`, and
`evolve_prompt_file.py` each ran their own `json.load(...)` of
`prompts/simulation_prompts.json` at import time, and `engine.py` additionally
snapshotted four fields into plain string constants at import time
(`choice_tmpl`, `dispatch_sys`, `neg_prompt`, `narrative_tmpl`). That meant
editing the JSON file on disk had **no effect** on a running process until it
was restarted.

This module mirrors the hot-reload pattern already used by `pricing.py` and
`ai_provider_manager.py` (JSON file + short TTL/mtime cache + `save_*()` that
refreshes the cache immediately), but goes one step further: `PROMPTS` is a
`dict` subclass that transparently re-reads the file when it changes, so
*every* existing `PROMPTS["key"]` / `PROMPTS.get("key")` call site across the
codebase becomes live automatically, with zero changes required at those call
sites. Only the handful of places that used to snapshot a value into a
separate variable name needed to change (see engine.py).

Defaults
--------
`prompts/simulation_prompts.defaults.json` is a frozen, one-time snapshot of
the shipped prompts, taken when World Studio was built. It is never written
to by this module (or the editor) — it exists purely so the editor can show
"what the app originally shipped with" and offer a "Reset to Default" action.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).parent.resolve()
PROMPTS_PATH = ROOT / "prompts" / "simulation_prompts.json"
DEFAULTS_PATH = ROOT / "prompts" / "simulation_prompts.defaults.json"

_LOCK = threading.Lock()
_MIN_RECHECK_INTERVAL = 1.0  # seconds — throttle os.stat() calls, not full reloads

# Keys that begin with this prefix are section-header comments baked into the
# JSON for humans reading the raw file; they're not prompts and shouldn't be
# surfaced as editable fields.
COMMENT_PREFIX = "_comment"


class _LivePrompts(dict):
    """A dict that transparently reloads itself from PROMPTS_PATH when the
    file's mtime changes, so plain `PROMPTS["key"]` / `PROMPTS.get(...)`
    access anywhere in the codebase is automatically live — no explicit
    "reload" call required at the call site."""

    def __init__(self) -> None:
        super().__init__()
        self._mtime: Optional[float] = None
        self._last_check: float = 0.0
        self._reload(force=True)

    def _maybe_reload(self) -> None:
        import time
        now = time.time()
        if (now - self._last_check) < _MIN_RECHECK_INTERVAL:
            return
        self._last_check = now
        try:
            mtime = PROMPTS_PATH.stat().st_mtime
        except OSError:
            return
        if mtime != self._mtime:
            self._reload(force=True)

    def _reload(self, force: bool = False) -> None:
        with _LOCK:
            try:
                with PROMPTS_PATH.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                print(f"[PROMPTS_STORE] {PROMPTS_PATH} not found; PROMPTS left empty.", flush=True)
                return
            except Exception as e:
                print(f"[PROMPTS_STORE ERROR] Failed to load {PROMPTS_PATH}: {e}", flush=True)
                return
            self.clear()
            self.update(data)
            try:
                self._mtime = PROMPTS_PATH.stat().st_mtime
            except OSError:
                pass

    def __getitem__(self, key):
        self._maybe_reload()
        return super().__getitem__(key)

    def get(self, key, default=None):
        self._maybe_reload()
        return super().get(key, default)

    def items(self):
        self._maybe_reload()
        return super().items()

    def keys(self):
        self._maybe_reload()
        return super().keys()

    def values(self):
        self._maybe_reload()
        return super().values()

    def __contains__(self, key):
        self._maybe_reload()
        return super().__contains__(key)


# The shared singleton every module in the codebase should import instead of
# loading its own copy of simulation_prompts.json.
PROMPTS = _LivePrompts()


def reload_prompts() -> Dict[str, Any]:
    """Force an immediate reload from disk (bypasses the mtime throttle).
    Called by the admin save/reset endpoints right after writing the file so
    the *very next* turn in this process sees the change."""
    PROMPTS._reload(force=True)
    return dict(PROMPTS)


def load_defaults(force: bool = False) -> Dict[str, Any]:
    """Load the frozen factory-default prompts. `force` is accepted for
    symmetry with load-style helpers elsewhere but the defaults file never
    changes at runtime, so there's nothing to actually re-check."""
    try:
        with DEFAULTS_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[PROMPTS_STORE WARN] {DEFAULTS_PATH} not found; no defaults available.", flush=True)
        return {}


def _write_and_reload(data: Dict[str, Any]) -> Dict[str, Any]:
    with _LOCK:
        with PROMPTS_PATH.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    return reload_prompts()


def save_prompt_field(key: str, value: Any) -> Dict[str, Any]:
    """Update a single field and persist immediately."""
    data = dict(PROMPTS)
    data[key] = value
    return _write_and_reload(data)


def save_prompts_bulk(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Update many fields at once (e.g. a multi-field editor save) and
    persist immediately."""
    data = dict(PROMPTS)
    data.update(fields)
    return _write_and_reload(data)


def reset_prompt_field(key: str) -> Dict[str, Any]:
    """Restore one field to its factory-default value."""
    defaults = load_defaults()
    if key not in defaults:
        raise KeyError(f"'{key}' has no factory default to reset to.")
    return save_prompt_field(key, defaults[key])


def reset_all_prompts() -> Dict[str, Any]:
    """Restore the entire prompt file to its factory defaults."""
    defaults = load_defaults()
    if not defaults:
        raise RuntimeError("No defaults file found — refusing to wipe simulation_prompts.json.")
    return _write_and_reload(dict(defaults))


def editable_keys(data: Optional[Dict[str, Any]] = None) -> List[str]:
    """All keys that represent an actual editable prompt/story field (i.e.
    everything except the `_comment_*` section-header keys)."""
    source = data if data is not None else PROMPTS
    return [k for k in source.keys() if not k.startswith(COMMENT_PREFIX)]


# ─────────────────────────────────────────────────────────────────────────
# Placeholder validation
#
# A handful of fields are run through Python's `str.format(**kwargs)` at
# call time (see PROMPT_SCHEMA `format_vars` below). Any `{token}` in those
# fields that ISN'T one of the whitelisted kwargs — or any stray unmatched
# `{`/`}` — will raise at request time deep inside a game turn. Everything
# else in the file is inserted as a literal value (f-string or plain
# concatenation), so curly braces there are always safe.
# ─────────────────────────────────────────────────────────────────────────

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def find_placeholders(value: str) -> List[str]:
    """All `{identifier}`-shaped tokens present in a string."""
    if not isinstance(value, str):
        return []
    return _PLACEHOLDER_RE.findall(value)


def _brace_balance_error(value: str) -> Optional[str]:
    """Detect a stray single `{` or `}` that isn't part of a valid
    `{identifier}` token and isn't a doubled `{{`/`}}` literal-brace escape.
    Returns a human-readable message, or None if the braces look fine."""
    # Strip valid {identifier} tokens and doubled braces, then see if any
    # bare '{' or '}' remain.
    scrubbed = _PLACEHOLDER_RE.sub("", value)
    scrubbed = scrubbed.replace("{{", "").replace("}}", "")
    if "{" in scrubbed or "}" in scrubbed:
        return (
            "Contains a stray '{' or '}' that isn't a recognized "
            "placeholder. Python's str.format() will raise an error at "
            "runtime unless literal braces are doubled ('{{' / '}}')."
        )
    return None


def validate_prompt_value(key: str, value: Any) -> Tuple[bool, List[str]]:
    """Validate a candidate new value for `key` before it's saved.

    Returns (ok, warnings). `ok=False` means the value would very likely
    crash the game at runtime the next time this field is used, and the
    caller should require an explicit override to save it anyway. Fields
    that aren't run through `.format()` always return ok=True (their braces
    are always literal/safe).
    """
    field = PROMPT_SCHEMA_BY_ID.get(key)
    warnings: List[str] = []

    if field is None or not field.get("format_safe_required"):
        return True, warnings

    if not isinstance(value, str):
        return True, warnings

    allowed = set(field.get("format_vars", []))
    found = set(find_placeholders(value))
    unknown = sorted(found - allowed)
    if unknown:
        warnings.append(
            "Unknown placeholder(s) "
            + ", ".join(f"{{{u}}}" for u in unknown)
            + f" — only {', '.join('{' + v + '}' for v in sorted(allowed)) or '(none)'} "
            "are recognized here. This will raise a KeyError the next time "
            "this prompt runs."
        )

    brace_error = _brace_balance_error(value)
    if brace_error:
        warnings.append(brace_error)

    return (len(warnings) == 0), warnings


# ─────────────────────────────────────────────────────────────────────────
# Schema — drives the World Studio UI (grouping, descriptions, placeholder
# legends, and "used by" tags). Deliberately only lists fields that are
# actually read by a live code path (see `code_refs`) — a handful of keys
# still live in simulation_prompts.json for legacy/future reasons but are
# not wired into any generation logic, so World Studio doesn't show them
# at all (showing an editable field with zero effect on the game is worse
# than not showing it).
# ─────────────────────────────────────────────────────────────────────────

PROMPT_SCHEMA: List[Dict[str, Any]] = [
    {
        "id": "world_initial_state",
        "label": "World & Opening Setting",
        "group": "world",
        "type": "longtext",
        "description": "The full opening world-state brief — setting, tone, era, threats, and rules the whole story is grounded in. Re-used every turn as the base of the evolving world prompt.",
        "code_refs": ["engine.py"],
        "live": True,
    },
    {
        "id": "action_consequence_instructions",
        "label": "Action & Consequence Rules",
        "group": "narrative",
        "type": "longtext",
        "description": "How the model turns a player action into a narrative dispatch + visual scene + life/death outcome. The single biggest lever on how the game feels turn to turn.",
        "code_refs": ["engine.py"],
        "live": True,
    },
    {
        "id": "situation_summary_instructions",
        "label": "Situation Summary",
        "group": "narrative",
        "type": "longtext",
        "description": "How the model writes the short 'what's urgent right now' bulletin shown between turns.",
        "code_refs": ["engine.py"],
        "live": True,
    },
    {
        "id": "field_notes_format",
        "label": "Field Notes Format",
        "group": "narrative",
        "type": "longtext",
        "description": "Format/voice rules for the player's in-universe field notes / journal entries.",
        "code_refs": ["engine.py"],
        "live": True,
    },
    {
        "id": "player_choice_generation_instructions",
        "label": "Player Choice / Submission Prompt",
        "group": "submissions",
        "type": "longtext",
        "description": "The prompt that generates the slate of actions the player can submit each turn. This is the 'submission prompt' — it decides what kinds of choices are even possible.",
        "code_refs": ["choices.py", "engine.py"],
        "live": True,
        "format_safe_required": True,
        "format_vars": [
            "dispatch", "seen_elements", "recent_choices", "caption",
            "image_description", "time_of_day", "beat_nudge",
            "situation_summary", "injury_state",
        ],
    },
    {
        "id": "gemini_text_to_image_instructions",
        "label": "Image Gen — First Frame (text-to-image)",
        "group": "image",
        "type": "longtext",
        "description": "Wraps every intro/first-shot image prompt — camera, POV, film stock, and safety framing rules.",
        "code_refs": ["gemini_image_utils.py"],
        "live": True,
        "format_safe_required": True,
        "format_vars": ["prompt"],
    },
    {
        "id": "gemini_image_to_image_instructions",
        "label": "Image Gen — Continuation (image-to-image)",
        "group": "image",
        "type": "longtext",
        "description": "Wraps every subsequent-turn image prompt — spatial continuity, POV, and visual style rules that keep frames coherent.",
        "code_refs": ["gemini_image_utils.py", "krea_image_utils.py", "fal_image_utils.py"],
        "live": True,
        "format_safe_required": True,
        "format_vars": ["prompt"],
    },
    {
        "id": "image_negative_prompt",
        "label": "Image Negative Prompt",
        "group": "image",
        "type": "longtext",
        "description": "Everything the image model should avoid generating (CGI look, borders, faces, sci-fi elements, UI overlays, etc).",
        "code_refs": ["engine.py"],
        "live": True,
    },
    {
        "id": "gemini_flipbook_4panel_prefix",
        "label": "Flipbook Sequence Rules",
        "group": "image",
        "type": "longtext",
        "description": "Rules for the optional 16-frame flipbook/GIF mode — temporal sequencing and perspective-lock across frames.",
        "code_refs": ["engine.py"],
        "live": True,
    },
]

PROMPT_SCHEMA_BY_ID: Dict[str, Dict[str, Any]] = {f["id"]: f for f in PROMPT_SCHEMA}

GROUP_LABELS: Dict[str, str] = {
    "world": "World & Setting",
    "narrative": "Narrative Engine",
    "submissions": "Player Submissions",
    "image": "Image Generation",
}
