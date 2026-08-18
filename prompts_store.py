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
# Image template rendering
#
# The first-frame (text-to-image) and continuation (image-to-image) templates
# used to be near-duplicates — 81 of their ~130 lines were identical — so
# redirecting how the world LOOKS meant editing the same paragraphs twice and
# hoping they stayed in sync. The shared material now lives in two fields:
#
#   image_art_direction  — the creative dial: era, film stock, palette, horror
#                          register. This is the ONE field you edit to redirect
#                          the world's look.
#   image_camera_rules   — the mechanical rulebook: POV, human body physics,
#                          framing, what may be in frame, no-text bans.
#
# Both are substituted into the two templates via {art_direction} and
# {camera_rules}, leaving each template holding only its genuine delta (a first
# frame has nothing to continue from; a continuation has a reference to honour).
#
# Every image provider renders through `render_image_template` so there is a
# single place this composition happens.
# ─────────────────────────────────────────────────────────────────────────

ART_DIRECTION_KEY = "image_art_direction"
CAMERA_RULES_KEY = "image_camera_rules"
IMAGE_TEMPLATE_KEYS = (
    "gemini_text_to_image_instructions",
    "gemini_image_to_image_instructions",
)
# The placeholders a template uses to pull the shared blocks in.
SHARED_IMAGE_VARS = ("art_direction", "camera_rules")


def render_image_template(template_key: str, prompt: str) -> str:
    """Render an image template with the scene and the shared direction blocks.

    Templates that don't reference `{art_direction}` / `{camera_rules}` are
    rendered as-is. That's the backwards-compatible path: an install whose
    prompts were customized before this split still has all that material
    written inline, and injecting it again would duplicate it.
    """
    template = PROMPTS.get(template_key, "") or ""
    return template.format(
        prompt=prompt,
        art_direction=PROMPTS.get(ART_DIRECTION_KEY, "") or "",
        camera_rules=PROMPTS.get(CAMERA_RULES_KEY, "") or "",
    )


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

    Returns (ok, messages). `ok=False` means the value would very likely
    crash the game at runtime the next time this field is used, and the
    caller should require an explicit override to save it anyway. Fields
    that aren't run through `.format()` always return ok=True (their braces
    are always literal/safe).

    Messages can also be purely advisory — dropping `{art_direction}` from an
    image template is a legitimate choice (a fully bespoke template), but it
    silently disconnects that render path from the shared direction, which is
    invisible from the resulting image. Those warn without blocking the save.
    """
    field = PROMPT_SCHEMA_BY_ID.get(key)
    blocking: List[str] = []
    advisory: List[str] = []

    if field is None or not isinstance(value, str):
        return True, []

    if key in IMAGE_TEMPLATE_KEYS:
        dropped = [v for v in SHARED_IMAGE_VARS if f"{{{v}}}" not in value]
        if dropped:
            advisory.append(
                "This template no longer includes "
                + " or ".join(f"{{{v}}}" for v in dropped)
                + ", so edits to that shared field will not reach this render "
                "path. That's fine if you meant to write a fully bespoke "
                "template — otherwise put the placeholder back."
            )

    if not field.get("format_safe_required"):
        return True, advisory

    allowed = set(field.get("format_vars", []))
    found = set(find_placeholders(value))
    unknown = sorted(found - allowed)
    if unknown:
        blocking.append(
            "Unknown placeholder(s) "
            + ", ".join(f"{{{u}}}" for u in unknown)
            + f" — only {', '.join('{' + v + '}' for v in sorted(allowed)) or '(none)'} "
            "are recognized here. This will raise a KeyError the next time "
            "this prompt runs."
        )

    brace_error = _brace_balance_error(value)
    if brace_error:
        blocking.append(brace_error)

    return (len(blocking) == 0), blocking + advisory


# ─────────────────────────────────────────────────────────────────────────
# Schema — drives both editors (grouping, tier, descriptions, placeholder
# legends).
#
# Two rules keep this honest:
#
# 1. Every key in simulation_prompts.json is either listed here or is a
#    cast-sheet block. An editable field with no effect on the game is worse
#    than no field at all, and four keys had quietly become exactly that
#    (~9KB of prompt text read by nothing, snapshotted into every saved
#    world, and pointed at by the README). `unwired_keys()` below plus a test
#    stop that from coming back.
#
# 2. Every field declares a `tier`. There are only four PRIMARY prompts —
#    the world, how an action becomes a consequence, what actions are even
#    offered, and how the world looks. Those four plus the Cast & Camera
#    sheet are the whole creative surface. Everything else is a mechanical
#    rulebook you can go a long time without opening, so the editors keep it
#    behind one disclosure instead of presenting twelve equal-looking
#    paragraphs and letting you guess which one matters.
# ─────────────────────────────────────────────────────────────────────────

TIER_PRIMARY = "primary"
TIER_ADVANCED = "advanced"

PROMPT_SCHEMA: List[Dict[str, Any]] = [
    {
        "id": "world_initial_state",
        "label": "Level Brief",
        "group": "world",
        "tier": TIER_PRIMARY,
        "type": "longtext",
        "description": "This place, in prose: what it is, what's happening here, what's dangerous about it. The run's world state grows out of this and is rewritten from it every turn. The form above pins down the structured facts (name, landmarks, opening shot); this is where you write the situation they sit in.",
        "code_refs": ["engine.py"],
        "live": True,
    },
    {
        "id": "action_consequence_instructions",
        "label": "How Actions Play Out",
        "group": "narrative",
        "tier": TIER_PRIMARY,
        "type": "longtext",
        "description": "Turns an action into what happened, what you now see, and whether you survived. The biggest single lever on how the game feels.",
        "code_refs": ["engine.py"],
        "live": True,
    },
    {
        "id": "player_choice_generation_instructions",
        "label": "What You Can Do",
        "group": "narrative",
        "tier": TIER_PRIMARY,
        "type": "longtext",
        "description": "Writes the actions offered each turn — so it decides what kind of game this is to play, not just to read.",
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
        "id": "image_art_direction",
        "label": "How The World Looks",
        "group": "image",
        "tier": TIER_PRIMARY,
        "type": "longtext",
        "description": "Era, film stock, palette, degradation, horror register. Reaches the first frame and every continuation at once — this is the field to edit to redirect the look.",
        "code_refs": ["gemini_image_utils.py", "krea_image_utils.py"],
        "live": True,
    },
    {
        "id": "world_evolution_instructions",
        "label": "What May Change Between Turns",
        "group": "world",
        "tier": TIER_ADVANCED,
        "type": "longtext",
        "description": "Guardrails for the pass that rewrites the world state after every action. Its output becomes the world the next turn reads, so this is what stops a desert drifting into a forest.",
        "code_refs": ["evolve_prompt_file.py"],
        "live": True,
    },
    {
        "id": "situation_summary_instructions",
        "label": "Between-Turn Bulletin",
        "group": "narrative",
        "tier": TIER_ADVANCED,
        "type": "longtext",
        "description": "The one-line \"what's urgent right now\" shown between turns.",
        "code_refs": ["engine.py"],
        "live": True,
    },
    {
        "id": "field_notes_format",
        "label": "Field Notes Voice",
        "group": "narrative",
        "tier": TIER_ADVANCED,
        "type": "longtext",
        "description": "Format and voice of the in-universe journal. Place {context} and {last_choice} where you want them, or leave them out and they're appended.",
        "code_refs": ["engine.py"],
        "live": True,
        "format_safe_required": True,
        "format_vars": ["context", "last_choice"],
    },
    {
        "id": "image_camera_rules",
        "label": "Camera Physics",
        "group": "image",
        "tier": TIER_ADVANCED,
        "type": "longtext",
        "description": "The mechanical rulebook shared by both image templates: framing distance, what may appear in frame, no-text and no-border bans. Perspective itself is a switch in Cast & Camera, not something to edit here.",
        "code_refs": ["gemini_image_utils.py", "krea_image_utils.py"],
        "live": True,
    },
    {
        "id": "image_negative_prompt",
        "label": "What Never Appears",
        "group": "image",
        "tier": TIER_ADVANCED,
        "type": "longtext",
        "description": "What the image model must avoid. The perspective clauses in here are recomputed to match your camera, so you don't have to.",
        "code_refs": ["engine.py"],
        "live": True,
    },
    {
        "id": "narrator_direction",
        "label": "The Narrator",
        "group": "narrative",
        "tier": TIER_ADVANCED,
        "type": "longtext",
        "description": (
            "Who the narrator is and how they speak — the single line they say "
            "when you ask for narration. {world} is the place, {self} is your "
            "character, {premise} the story so far, {scene} what's on screen and "
            "{recent} the last few beats. {focus} is filled in when something "
            "specific has to be said (a travel beat, a reveal) and is empty "
            "otherwise, so put it where it should take precedence."
        ),
        "code_refs": ["engine.py"],
        "live": True,
        "format_safe_required": True,
        "format_vars": ["world", "self", "premise", "scene", "recent", "focus"],
    },
    {
        "id": "camp_scene_prompt",
        "label": "The Camp Shot",
        "group": "image",
        # Advanced on purpose: the primary surface is the four fields that
        # describe the whole game, and camp is one specific scene inside it. The
        # editor's Camp window shows this regardless of tier — tier only governs
        # what the flat list puts in front of you.
        "tier": TIER_ADVANCED,
        "type": "longtext",
        "description": (
            "How camp looks: the fire, the night, the vehicle, the mood. "
            "{vantage} is your camera, {terrain} is the place you're in, and "
            "{who} becomes the roster around the fire (or the empty-camp line "
            "when you're travelling alone). The reference-image map is appended "
            "by the engine, because it has to name the exact portraits being sent."
        ),
        "code_refs": ["engine.py"],
        "live": True,
        "format_safe_required": True,
        "format_vars": ["vantage", "terrain", "who"],
    },
    {
        "id": "gemini_text_to_image_instructions",
        "label": "Template — First Frame",
        "group": "image",
        "tier": TIER_ADVANCED,
        "type": "longtext",
        "description": "Only what's unique to the first frame, which has nothing to continue from. Keep {art_direction} and {camera_rules} in it or this render path stops receiving the shared look.",
        "code_refs": ["gemini_image_utils.py"],
        "live": True,
        "format_safe_required": True,
        "format_vars": ["prompt", "art_direction", "camera_rules"],
    },
    {
        "id": "gemini_image_to_image_instructions",
        "label": "Template — Continuation",
        "group": "image",
        "tier": TIER_ADVANCED,
        "type": "longtext",
        "description": "Only what's unique to a continuation: honouring the previous frame as a spatial lock and not cleaning up its grain. Keep {art_direction} and {camera_rules} in it.",
        "code_refs": ["gemini_image_utils.py", "krea_image_utils.py", "fal_image_utils.py"],
        "live": True,
        "format_safe_required": True,
        "format_vars": ["prompt", "art_direction", "camera_rules"],
    },
    {
        "id": "gemini_flipbook_4panel_prefix",
        "label": "Flipbook Sequence Rules",
        "group": "image",
        "tier": TIER_ADVANCED,
        "type": "longtext",
        "description": "Temporal sequencing for the optional 16-frame flipbook mode. Only read when flipbook is on.",
        "code_refs": ["engine.py"],
        "live": True,
    },
]

PROMPT_SCHEMA_BY_ID: Dict[str, Dict[str, Any]] = {f["id"]: f for f in PROMPT_SCHEMA}

# Three tabs, ordered the way you'd actually answer the questions: what place
# is this, how does it play, how does it look. "Player Submissions" used to be
# a fourth tab holding exactly one field, which made the choice prompt feel
# like a separate subsystem rather than half of how the game plays.
GROUP_LABELS: Dict[str, str] = {
    "world": "World",
    "narrative": "Story & Play",
    "image": "Look",
}

# One line per tab, shown above its fields. A tab that opens onto a 19,000
# character paragraph with no framing is where "I don't know what I'm editing"
# starts.
GROUP_BLURBS: Dict[str, str] = {
    "world": "The place and its rules. Seeds every run; a fresh run picks up changes.",
    "narrative": "How a turn resolves and what you're offered next.",
    "image": "What every frame looks like.",
}


# The cast-sheet blocks. Structured objects rather than prompt bodies, edited
# through their own form (see game_identity.IDENTITY_SCHEMA), so they're absent
# from PROMPT_SCHEMA but are still legitimate keys in the file. Named here so
# `unwired_keys` doesn't flag them; game_identity remains the source of truth.
SPEC_BLOCK_KEYS = ("player_character", "setting_reference", "camera_perspective")


def primary_keys() -> List[str]:
    """The short list: the prompts worth reaching for first."""
    return [f["id"] for f in PROMPT_SCHEMA if f.get("tier", TIER_PRIMARY) == TIER_PRIMARY]


def unwired_keys(data: Optional[Dict[str, Any]] = None) -> List[str]:
    """Editable keys in the prompt file that no editor and no code path reads.

    A prompt you can save but that changes nothing is the most expensive kind
    of confusion: it costs you an edit, a restart, and your trust in every
    other field. Asserted empty by the test suite.
    """
    known = set(PROMPT_SCHEMA_BY_ID) | set(SPEC_BLOCK_KEYS)
    return [k for k in editable_keys(data) if k not in known]
