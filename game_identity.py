"""
game_identity.py — WHO you play as, WHERE you play, and WHERE THE CAMERA SITS.

Why this exists
---------------
The simulation could already be re-authored end to end (World Studio edits every
prompt that drives it), but three things a player actually cares about were not
addressable at all:

1. **Who am I?** The protagonist was hardcoded prose inside
   ``world_initial_state`` ("an investigative photojournalist"). You could
   rewrite that paragraph, but nothing downstream *knew* a character existed, so
   the image model was never told what you look like and the narrator had no
   stable identity to write toward.
2. **Where am I?** Same story for the level: the opening shot was a hardcoded
   list of five Horizon-facility descriptions in ``engine.generate_intro_image_fast``.
3. **Where is the camera?** "First person" was not a setting — it was ~40
   hardcoded strings spread across the prompt JSON, ``engine.py``, and every
   image provider. Asking for third person meant editing all of them, and the
   ones you missed would fight the ones you changed.

This module turns all three into a single structured, editable, snapshot-able
spec — the **cast sheet** — and compiles it into prompt text that the rest of
the pipeline injects.

Where the spec lives
--------------------
Inside ``prompts/simulation_prompts.json`` under three object keys
(``player_character`` / ``setting_reference`` / ``camera_perspective``). That is
deliberate: ``prompts_store`` already hot-reloads that file, and
``worlds_store`` already snapshots every editable key — so saving a world
captures your protagonist, your level plate, and your camera rig for free, and
loading one swaps the whole package.

Reference images (a character sheet, a photo of the level) are stored as files
under ``assets/references/`` and referenced by id from the spec, so the JSON
stays small and diffable.

The four-stage prompt pipeline
------------------------------
Perspective can't just be appended — the shipped prompts are saturated with
first-person language that would contradict a third-person request. So every
prompt surface runs through :func:`apply`, which does four things in order:

1. **Compile** — build an authoritative CAMERA / CAST / LOCATION directive and
   put it FIRST, where image models weight hardest.
2. **Retune** — rewrite perspective *nouns* inline ("first-person" → "third-person
   over-the-shoulder", "POV" → the mode's tag), case-preserving.
3. **Reconcile** — delete lines that flatly contradict the active mode (the
   "NEVER show any part of a human body" rules must go when you've asked to see
   your character).
4. **Negate** — recompute the negative prompt for the mode, so "third person
   perspective" stops being a banned phrase the moment you select third person.

Everything degrades to today's behavior when the spec is left at its defaults:
first person, no named character, no setting override.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import prompts_store
from prompts_store import PROMPTS

ROOT = Path(__file__).parent.resolve()
REFERENCES_DIR = ROOT / "assets" / "references"

# Spec keys as they appear in prompts/simulation_prompts.json.
CHARACTER_KEY = "player_character"
SETTING_KEY = "setting_reference"
CAMERA_KEY = "camera_perspective"
SPEC_KEYS = (CHARACTER_KEY, SETTING_KEY, CAMERA_KEY)

_LOCK = threading.Lock()

# Uploaded reference images are capped so a pasted 12MP phone photo can't wedge
# the request or blow past the image API's inline-data budget.
MAX_REFERENCE_BYTES = 8 * 1024 * 1024
MAX_REFERENCES_PER_SLOT = 3

_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


# ═══════════════════════════════════════════════════════════════════════════
# PERSPECTIVE MODES
#
# A mode is a complete camera contract: how to describe it, whether the
# protagonist's body is in frame, what the narrator calls the player, and what
# the negative prompt should (and should NOT) forbid. Adding a mode here makes
# it selectable in both editors with no other code changes.
# ═══════════════════════════════════════════════════════════════════════════

# Everything the shipped negative prompt bans that a visible protagonist needs.
# Shared by every third-person mode: the negative prompt is one long
# comma-separated string, and any clause left in it is actively arguing against
# the character the player asked to see. Matched as case-insensitive substrings
# against each comma-separated clause (see `negative_prompt`).
_CHARACTER_NEGATIVE_STRIP = [
    # Framing bans
    "third person", "over shoulder", "behind character", "following someone",
    # Presence bans
    "person visible", "human visible", "man visible", "character visible",
    "protagonist shown", "someone else visible", "full body in frame",
    "person from behind", "character's back", "person's back",
    # Body-part bans
    "body parts", "head visible", "shoulders visible", "back of head",
    "face visible", "reflection of face", "silhouette",
]

PERSPECTIVE_MODES: Dict[str, Dict[str, Any]] = {
    "first_person": {
        "label": "First person",
        "tagline": "The camera is your eyes. You never see yourself.",
        "phrase": "first-person",
        "tag": "FIRST-PERSON POV",
        "shows_body": False,
        "hands_default": True,
        "camera_header": "FIRST-PERSON CAMERA VIEW",
        "rig": "a camcorder held at eye level by the player",
        "image_rules": [
            "The camera IS the player's eyes — everything in frame is what they are looking at right now.",
            "The player's own face, head, back, torso, and legs are NEVER in frame.",
            "Eye-level height, natural human field of view, handheld micro-motion.",
        ],
        "hands_rule": (
            "The player's own hands/forearms MAY enter the bottom of frame when reaching, "
            "carrying, or bracing — nothing above the wrists, never the face."
        ),
        "no_hands_rule": "No body parts of the player in frame at all — pure environment.",
        "narrative_person": "second",
        "narrative_rule": (
            "Write to the player as \"you\". They experience the world through their own eyes; "
            "describe sensation, not appearance."
        ),
        "negative_add": [
            "third person perspective", "over shoulder view", "behind character",
            "following someone", "player character visible in frame",
        ],
        "negative_strip": [],
    },
    "over_shoulder": {
        "label": "Over the shoulder",
        "tagline": "Camera rides tight behind your character — Resident Evil 4, The Last of Us.",
        "phrase": "third-person over-the-shoulder",
        "tag": "OVER-THE-SHOULDER CHASE CAM",
        "shows_body": True,
        "hands_default": False,
        "camera_header": "OVER-THE-SHOULDER THIRD-PERSON VIEW",
        "rig": "a camera floating roughly one metre behind and slightly above the character's shoulder",
        "image_rules": [
            "The PLAYER CHARACTER is visible in frame, seen from behind and slightly above, "
            "occupying the lower-left or lower-right third of the composition.",
            "The character's head and shoulders are sharp and clearly readable; the world opens up "
            "past them into the depth of the shot.",
            "The camera trails the character — it moves where they move, never overtakes them, "
            "and never cuts to their face.",
            "Whatever the character is holding stays visible in their hands, read from behind.",
        ],
        "hands_rule": "",
        "no_hands_rule": "",
        "narrative_person": "second",
        "narrative_rule": (
            "Write to the player as \"you\", but they can SEE their own character on screen — "
            "their posture, their gear, how their body reacts is fair game to describe."
        ),
        "negative_add": [
            "first person view", "camera as eyes", "empty foreground with no character",
            "character facing the camera", "selfie",
        ],
        "negative_strip": _CHARACTER_NEGATIVE_STRIP,
    },
    "third_person": {
        "label": "Third person",
        "tagline": "Full-body follow cam with room to breathe — Tomb Raider, Souls.",
        "phrase": "third-person",
        "tag": "THIRD-PERSON FOLLOW CAM",
        "shows_body": True,
        "hands_default": False,
        "camera_header": "THIRD-PERSON FOLLOW-CAM VIEW",
        "rig": "a camera three to five metres back from the character at chest height",
        "image_rules": [
            "The PLAYER CHARACTER is fully visible — head to feet — as the clear subject of the shot.",
            "The character reads at roughly a third to a half of the frame height, with the "
            "environment framing them so the space is legible around them.",
            "Camera stays behind or to the side of the character and follows their motion; "
            "the world is always shown WITH them in it, never without them.",
        ],
        "hands_rule": "",
        "no_hands_rule": "",
        "narrative_person": "second",
        "narrative_rule": (
            "Write to the player as \"you\", but the camera watches their character from outside — "
            "their full body, stance, gait, and injuries are visible and worth describing."
        ),
        "negative_add": [
            "first person view", "camera as eyes", "empty scene with no character",
            "close-up portrait", "cropped body",
        ],
        "negative_strip": _CHARACTER_NEGATIVE_STRIP,
    },
    "fixed_cinematic": {
        "label": "Fixed cinematic",
        "tagline": "Locked dramatic angles the character walks into — classic Resident Evil, Silent Hill.",
        "phrase": "fixed cinematic third-person",
        "tag": "FIXED CINEMATIC ANGLE",
        "shows_body": True,
        "hands_default": False,
        "camera_header": "FIXED CINEMATIC CAMERA ANGLE",
        "rig": "a camera locked off on a tripod in the corner of the space, watching it like a stage",
        "image_rules": [
            "The camera is BOLTED IN PLACE — a dramatic, composed angle (high corner, low floor "
            "level, through a doorway) that observes the whole space.",
            "The PLAYER CHARACTER is visible somewhere inside that composed frame, small-to-medium "
            "in scale, dwarfed by the architecture around them.",
            "No handheld shake. The frame is still and deliberate; only the character and the "
            "world inside it move.",
        ],
        "hands_rule": "",
        "no_hands_rule": "",
        "narrative_person": "second",
        "narrative_rule": (
            "Write to the player as \"you\". The camera is a detached observer watching their "
            "character move through the space — describe how small and exposed they look in it."
        ),
        "negative_add": [
            "first person view", "camera as eyes", "handheld shake", "empty scene with no character",
        ],
        "negative_strip": _CHARACTER_NEGATIVE_STRIP,
    },
}

DEFAULT_MODE = "first_person"


def mode_options() -> List[Dict[str, Any]]:
    """Editor-facing list of selectable perspectives."""
    return [
        {
            "id": key,
            "label": cfg["label"],
            "tagline": cfg["tagline"],
            "shows_body": cfg["shows_body"],
            "supports_hands": bool(cfg.get("hands_rule")),
        }
        for key, cfg in PERSPECTIVE_MODES.items()
    ]


# ═══════════════════════════════════════════════════════════════════════════
# SPEC DEFAULTS + NORMALIZATION
# ═══════════════════════════════════════════════════════════════════════════

CHARACTER_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "name": "",
    "pronouns": "they/them",
    "role": "",
    "appearance": "",
    "wardrobe": "",
    "signature_gear": "",
    "demeanor": "",
    "backstory": "",
    "reference_images": [],
}

SETTING_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "name": "",
    "summary": "",
    "era": "",
    "palette": "",
    "landmarks": "",
    "opening_shot": "",
    "reference_images": [],
}

CAMERA_DEFAULTS: Dict[str, Any] = {
    "mode": DEFAULT_MODE,
    "show_hands": True,
    "lens": "",
    "notes": "",
}

_DEFAULTS_BY_KEY = {
    CHARACTER_KEY: CHARACTER_DEFAULTS,
    SETTING_KEY: SETTING_DEFAULTS,
    CAMERA_KEY: CAMERA_DEFAULTS,
}

# Free-text spec fields are hard-capped: they are concatenated into prompts that
# already run close to the image API's 5000-char ceiling, and an essay pasted
# into "appearance" would silently push the actual scene description out.
_MAX_FIELD_CHARS = 600


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text[:_MAX_FIELD_CHARS]


def _clean_refs(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value:
        ref_id = str(item or "").strip()
        if ref_id and ref_id not in out and _REF_ID_RE.match(ref_id):
            out.append(ref_id)
    return out[:MAX_REFERENCES_PER_SLOT]


def _normalize(key: str, raw: Any) -> Dict[str, Any]:
    """Coerce whatever is in the JSON into a complete, well-typed spec block.

    Stored values come from a hand-editable file and from world snapshots that
    may predate a field, so every read normalizes rather than trusting shape.
    """
    defaults = _DEFAULTS_BY_KEY[key]
    out = dict(defaults)
    source = raw if isinstance(raw, dict) else {}
    for field, default in defaults.items():
        value = source.get(field, default)
        if isinstance(default, bool):
            out[field] = bool(value)
        elif isinstance(default, list):
            out[field] = _clean_refs(value)
        else:
            out[field] = _clean_text(value)
    if key == CAMERA_KEY:
        if out["mode"] not in PERSPECTIVE_MODES:
            out["mode"] = DEFAULT_MODE
    return out


def get_spec() -> Dict[str, Dict[str, Any]]:
    """The full, normalized cast sheet as the engine sees it right now.

    Reads through ``PROMPTS``, so an edit saved by either editor is live on the
    next call with no restart (same hot-reload contract as every other prompt).
    """
    return {
        CHARACTER_KEY: _normalize(CHARACTER_KEY, PROMPTS.get(CHARACTER_KEY)),
        SETTING_KEY: _normalize(SETTING_KEY, PROMPTS.get(SETTING_KEY)),
        CAMERA_KEY: _normalize(CAMERA_KEY, PROMPTS.get(CAMERA_KEY)),
    }


def default_spec() -> Dict[str, Dict[str, Any]]:
    return {
        CHARACTER_KEY: dict(CHARACTER_DEFAULTS),
        SETTING_KEY: dict(SETTING_DEFAULTS),
        CAMERA_KEY: dict(CAMERA_DEFAULTS),
    }


def save_spec(partial: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Merge a partial cast-sheet update into the live prompt file.

    Accepts any subset of the three blocks and any subset of their fields, so
    the editors can PUT just what changed.
    """
    current = get_spec()
    fields: Dict[str, Any] = {}
    for key in SPEC_KEYS:
        if key not in partial:
            continue
        incoming = partial[key]
        if not isinstance(incoming, dict):
            continue
        merged = dict(current[key])
        merged.update(incoming)
        fields[key] = _normalize(key, merged)
    if fields:
        prompts_store.save_prompts_bulk(fields)
    return get_spec()


def reset_spec() -> Dict[str, Dict[str, Any]]:
    """Clear the cast sheet back to 'unset' (first person, nobody, nowhere)."""
    prompts_store.save_prompts_bulk(default_spec())
    return get_spec()


def ensure_spec_keys() -> None:
    """Backfill the three spec keys if this install's prompt file predates them.

    Called once at import by consumers so the editors always have something to
    render and ``worlds_store`` always round-trips the block.
    """
    missing = {k: dict(_DEFAULTS_BY_KEY[k]) for k in SPEC_KEYS if PROMPTS.get(k) is None}
    if missing:
        prompts_store.save_prompts_bulk(missing)


# ═══════════════════════════════════════════════════════════════════════════
# EDITOR SCHEMA
#
# Deliberately separate from prompts_store.PROMPT_SCHEMA: those are longtext
# prompt bodies, these are structured form fields with image slots. Both
# editors render from this, so adding a field here surfaces it in World Studio
# and the in-game World Editor at once.
# ═══════════════════════════════════════════════════════════════════════════

IDENTITY_SCHEMA: List[Dict[str, Any]] = [
    {
        "id": CHARACTER_KEY,
        "label": "Your Character",
        "icon": "🧍",
        "description": (
            "Who you actually play as. Name and temperament steer the writing; appearance, "
            "wardrobe, and gear steer the picture — and matter most when the camera can see you."
        ),
        "supports_images": True,
        "images_label": "Character reference",
        "images_hint": (
            "A portrait or character sheet. Passed to the image model as an identity lock so "
            "your character is the same person in every frame."
        ),
        "fields": [
            {"id": "enabled", "label": "Use this character", "type": "toggle",
             "help": "Off means the story falls back to whatever the World & Setting prompt describes."},
            {"id": "name", "label": "Name", "type": "text", "placeholder": "Wren Alvarez"},
            {"id": "pronouns", "label": "Pronouns", "type": "text", "placeholder": "she/her"},
            {"id": "role", "label": "Role", "type": "text",
             "placeholder": "freelance salvage diver", "help": "What they do — drives the kinds of actions the game offers."},
            {"id": "appearance", "label": "Appearance", "type": "longtext",
             "placeholder": "Early thirties, close-cropped black hair, burn scar across the left jaw.",
             "help": "Physical description the image model locks onto."},
            {"id": "wardrobe", "label": "Wardrobe", "type": "longtext",
             "placeholder": "Patched orange dive suit, mismatched boots, canvas satchel."},
            {"id": "signature_gear", "label": "Signature gear", "type": "text",
             "placeholder": "dented Nikon F3, sodium lamp",
             "help": "Visible in frame and usable in the fiction."},
            {"id": "demeanor", "label": "Temperament", "type": "text",
             "placeholder": "dry, unflappable, talks to herself",
             "help": "Colours how the narrator writes their reactions."},
            {"id": "backstory", "label": "Backstory", "type": "longtext",
             "placeholder": "Came back for the sister who never filed a flight plan."},
        ],
    },
    {
        "id": SETTING_KEY,
        "label": "The Level",
        "icon": "🗺️",
        "description": (
            "The place the run happens in. The opening shot replaces the shipped intro, and the "
            "landmarks keep every later frame anchored to the same geography."
        ),
        "supports_images": True,
        "images_label": "Setting reference",
        "images_hint": (
            "A photo, concept plate, or screenshot of the place. Passed to the image model as a "
            "location anchor for architecture, materials, and palette."
        ),
        "fields": [
            {"id": "enabled", "label": "Use this level", "type": "toggle",
             "help": "Off means the shipped Horizon-facility opening is used."},
            {"id": "name", "label": "Name", "type": "text", "placeholder": "The Kettle Yard"},
            {"id": "summary", "label": "What it is", "type": "longtext",
             "placeholder": "A flooded shipbreaking yard on a tidal flat, half the hulls still standing."},
            {"id": "era", "label": "Era / tech level", "type": "text", "placeholder": "1993, analog only"},
            {"id": "palette", "label": "Palette & light", "type": "text",
             "placeholder": "rust orange, sodium haze, low grey sky"},
            {"id": "landmarks", "label": "Landmarks that must recur", "type": "longtext",
             "placeholder": "The listing tanker, the crane gantry, the pump house with the red door.",
             "help": "Named geography the image model must keep returning to, so the space feels real."},
            {"id": "opening_shot", "label": "Opening shot", "type": "longtext",
             "placeholder": "Low tide at dawn. The tanker's hull fills the right of frame; mud flats run out to the gantry.",
             "help": "The literal first frame of the run. Leave blank to auto-derive it from the summary."},
        ],
    },
    {
        "id": CAMERA_KEY,
        "label": "Camera & Perspective",
        "icon": "🎥",
        "description": (
            "Where the lens sits. This is a real switch, not a suggestion — it rewrites the "
            "perspective language inside every shipped prompt and flips the negative prompt to match."
        ),
        "supports_images": False,
        "fields": [
            {"id": "mode", "label": "Perspective", "type": "mode"},
            {"id": "show_hands", "label": "Show your hands in frame", "type": "toggle",
             "help": "First person only — whether your own hands/forearms may enter the bottom of frame."},
            {"id": "lens", "label": "Lens / framing", "type": "text",
             "placeholder": "28mm wide, handheld, slight dutch"},
            {"id": "notes", "label": "Extra camera notes", "type": "longtext",
             "placeholder": "Keep the horizon low. Never look straight down."},
        ],
    },
]


def identity_schema() -> List[Dict[str, Any]]:
    """The cast-sheet form definition, with the live perspective options baked in."""
    schema = json.loads(json.dumps(IDENTITY_SCHEMA))
    for block in schema:
        for field in block["fields"]:
            if field.get("type") == "mode":
                field["options"] = mode_options()
    return schema


# ═══════════════════════════════════════════════════════════════════════════
# ACCESSORS — small questions the rest of the pipeline asks constantly
# ═══════════════════════════════════════════════════════════════════════════

def camera_mode(spec: Optional[Dict[str, Any]] = None) -> str:
    spec = spec or get_spec()
    return spec[CAMERA_KEY].get("mode", DEFAULT_MODE)


def mode_config(spec: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return PERSPECTIVE_MODES.get(camera_mode(spec), PERSPECTIVE_MODES[DEFAULT_MODE])


def is_first_person(spec: Optional[Dict[str, Any]] = None) -> bool:
    return not mode_config(spec)["shows_body"]


def shows_character(spec: Optional[Dict[str, Any]] = None) -> bool:
    """True when the protagonist's body belongs IN the frame.

    This is the single switch that flips the anti-person rules, the POV
    hand-stripping post-process, and the negative prompt.
    """
    spec = spec or get_spec()
    return bool(mode_config(spec)["shows_body"])


def hands_visible(spec: Optional[Dict[str, Any]] = None) -> bool:
    spec = spec or get_spec()
    cfg = mode_config(spec)
    if not cfg.get("hands_rule"):
        return False
    return bool(spec[CAMERA_KEY].get("show_hands", True))


def character_enabled(spec: Optional[Dict[str, Any]] = None) -> bool:
    spec = spec or get_spec()
    char = spec[CHARACTER_KEY]
    if not char.get("enabled"):
        return False
    return any(char.get(f) for f in ("name", "role", "appearance", "wardrobe", "signature_gear"))


def setting_enabled(spec: Optional[Dict[str, Any]] = None) -> bool:
    spec = spec or get_spec()
    setting = spec[SETTING_KEY]
    if not setting.get("enabled"):
        return False
    return any(setting.get(f) for f in ("name", "summary", "landmarks", "opening_shot"))


def character_name(spec: Optional[Dict[str, Any]] = None) -> str:
    spec = spec or get_spec()
    return spec[CHARACTER_KEY].get("name", "").strip()


def display_name(spec: Optional[Dict[str, Any]] = None) -> str:
    """What to call the protagonist in prompt prose when a name isn't set."""
    return character_name(spec) or "the player character"


def is_active(spec: Optional[Dict[str, Any]] = None) -> bool:
    """True when the cast sheet is doing anything at all.

    When False every helper here is a no-op and the game behaves exactly as it
    did before this module existed.
    """
    spec = spec or get_spec()
    return (
        character_enabled(spec)
        or setting_enabled(spec)
        or camera_mode(spec) != DEFAULT_MODE
        or not hands_visible(spec)
    )


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 1 — COMPILE: the authoritative directive blocks
# ═══════════════════════════════════════════════════════════════════════════

def _bullets(lines: List[str]) -> str:
    return "\n".join(f"• {line}" for line in lines if line)


def camera_directive(spec: Optional[Dict[str, Any]] = None) -> str:
    """The CAMERA block — where the lens is and who is in front of it.

    Emitted first in every image prompt. Image models weight leading text
    hardest, which is exactly what we need when the mode contradicts prose
    further down that we couldn't safely rewrite.
    """
    spec = spec or get_spec()
    cfg = mode_config(spec)
    cam = spec[CAMERA_KEY]

    rules = list(cfg["image_rules"])
    if cfg["shows_body"]:
        who = display_name(spec)
        rules.insert(0, f"The subject on screen is {who} — the character the player is controlling.")
    if hands_visible(spec) and cfg.get("hands_rule"):
        rules.append(cfg["hands_rule"])
    elif not cfg["shows_body"] and cfg.get("no_hands_rule"):
        rules.append(cfg["no_hands_rule"])
    if cam.get("lens"):
        rules.append(f"Lens / framing: {cam['lens']}.")
    if cam.get("notes"):
        rules.append(cam["notes"])

    return (
        "🎥 CAMERA DIRECTIVE — THIS OVERRIDES ANY CONFLICTING CAMERA LANGUAGE BELOW\n"
        f"PERSPECTIVE: {cfg['camera_header']} ({cfg['label']}).\n"
        f"RIG: {cfg['rig']}.\n"
        f"{_bullets(rules)}"
    )


def character_visual_sheet(spec: Optional[Dict[str, Any]] = None) -> str:
    """The CAST block — what the protagonist physically looks like.

    Only meaningful to the image model when the body (or hands) can be seen, so
    callers should gate on :func:`shows_character` / :func:`hands_visible`.
    """
    spec = spec or get_spec()
    if not character_enabled(spec):
        return ""
    char = spec[CHARACTER_KEY]

    lines: List[str] = []
    header = display_name(spec)
    if char.get("pronouns"):
        header += f" ({char['pronouns']})"
    if char.get("role"):
        header += f" — {char['role']}"
    lines.append(f"IDENTITY: {header}")
    if char.get("appearance"):
        lines.append(f"APPEARANCE: {char['appearance']}")
    if char.get("wardrobe"):
        lines.append(f"WARDROBE: {char['wardrobe']}")
    if char.get("signature_gear"):
        lines.append(f"CARRIED / WORN: {char['signature_gear']}")

    tail = (
        "This is the SAME person in every single frame — face, build, hair, and outfit "
        "must not drift between shots."
    )
    return "🧍 PLAYER CHARACTER — WHO IS ON SCREEN\n" + "\n".join(lines) + f"\n{tail}"


def setting_plate(spec: Optional[Dict[str, Any]] = None) -> str:
    """The LOCATION block — the level the whole run takes place in."""
    spec = spec or get_spec()
    if not setting_enabled(spec):
        return ""
    setting = spec[SETTING_KEY]

    lines: List[str] = []
    if setting.get("name"):
        lines.append(f"LOCATION: {setting['name']}")
    if setting.get("summary"):
        lines.append(f"WHAT IT IS: {setting['summary']}")
    if setting.get("era"):
        lines.append(f"ERA / TECH LEVEL: {setting['era']}")
    if setting.get("palette"):
        lines.append(f"PALETTE & LIGHT: {setting['palette']}")
    if setting.get("landmarks"):
        lines.append(f"LANDMARKS THAT MUST RECUR: {setting['landmarks']}")
    if not lines:
        return ""
    return "🗺️ LEVEL PLATE — THE PLACE THIS RUN HAPPENS IN\n" + "\n".join(lines)


def character_sdxl_tags(spec: Optional[Dict[str, Any]] = None) -> str:
    """The protagonist as a short comma-separated tag list.

    SDXL is conditioned by a CLIP encoder that reads ~77 tokens and responds to
    descriptors, not prose, so the fal path needs the character compressed into
    tags rather than the paragraph the Gemini path receives.
    """
    spec = spec or get_spec()
    if not character_enabled(spec):
        return ""
    char = spec[CHARACTER_KEY]
    bits = [char.get("role"), char.get("appearance"), char.get("wardrobe")]
    tags = ", ".join(b.rstrip(". ") for b in bits if b)
    return tags[:220]


def image_directive(spec: Optional[Dict[str, Any]] = None) -> str:
    """Camera + cast + location, assembled for an image prompt."""
    spec = spec or get_spec()
    blocks = [camera_directive(spec)]
    if shows_character(spec) or hands_visible(spec):
        sheet = character_visual_sheet(spec)
        if sheet:
            blocks.append(sheet)
    plate = setting_plate(spec)
    if plate:
        blocks.append(plate)
    return "\n\n".join(blocks)


def narrative_directive(spec: Optional[Dict[str, Any]] = None) -> str:
    """The block injected into story/consequence/choice prompts.

    Narrative doesn't care what the character's jacket looks like — it cares who
    they are, how they carry themselves, and whether the prose can describe them
    from outside (which only makes sense once the camera can see them).
    """
    spec = spec or get_spec()
    if not is_active(spec):
        return ""
    cfg = mode_config(spec)
    char = spec[CHARACTER_KEY]

    lines: List[str] = [f"CAMERA: {cfg['label']} — {cfg['tagline']}", cfg["narrative_rule"]]

    if character_enabled(spec):
        who = display_name(spec)
        bits = []
        if char.get("role"):
            bits.append(char["role"])
        if char.get("pronouns"):
            bits.append(char["pronouns"])
        suffix = f" ({', '.join(bits)})" if bits else ""
        lines.append(f"THE PLAYER IS: {who}{suffix}.")
        if char.get("demeanor"):
            lines.append(f"TEMPERAMENT: {char['demeanor']} — this colours how they react under pressure.")
        if char.get("backstory"):
            lines.append(f"BACKSTORY: {char['backstory']}")
        if char.get("signature_gear"):
            lines.append(f"THEY CARRY: {char['signature_gear']} — usable in the fiction.")
        if cfg["shows_body"]:
            lines.append(
                f"Because the camera watches {who} from outside, you may describe their body, "
                "stance, and visible injuries as the player sees them."
            )

    if setting_enabled(spec):
        setting = spec[SETTING_KEY]
        if setting.get("name"):
            lines.append(f"THE PLACE: {setting['name']}.")
        if setting.get("summary"):
            lines.append(setting["summary"])
        if setting.get("landmarks"):
            lines.append(f"Established landmarks to stay consistent with: {setting['landmarks']}")

    return "🎬 DIRECTOR'S SHEET — CAST, CAMERA, AND PLACE\n" + "\n".join(lines)


def world_brief(base: str, spec: Optional[Dict[str, Any]] = None) -> str:
    """Seed the run's evolving world document with the cast sheet.

    Appended to ``world_initial_state`` at reset so the protagonist and level
    are part of the world state the whole run reasons from — not just a
    per-prompt garnish that the world-evolution pass would erase.
    """
    spec = spec or get_spec()
    directive = narrative_directive(spec)
    if not directive:
        return base
    return f"{base}\n\n{directive}" if base else directive


def opening_shot(spec: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, str]]:
    """The authored first frame, when the setting spec provides one.

    Returns ``{"prologue", "vision"}`` shaped like the hardcoded opening scenes
    in ``engine.generate_intro_image_fast`` so it can drop straight in, or None
    to fall back to the shipped Horizon openers.
    """
    spec = spec or get_spec()
    if not setting_enabled(spec):
        return None
    setting = spec[SETTING_KEY]
    shot = setting.get("opening_shot", "").strip()
    summary = setting.get("summary", "").strip()
    name = setting.get("name", "").strip()
    if not (shot or summary):
        return None

    place = name or "the location"
    lead = (shot or summary).rstrip()
    vision_bits = [lead if lead.endswith((".", "!", "?")) else lead + "."]
    if setting.get("landmarks"):
        vision_bits.append(f"Visible landmarks: {setting['landmarks']}.")
    if setting.get("palette"):
        vision_bits.append(f"Light and palette: {setting['palette']}.")

    if shows_character(spec) and character_enabled(spec):
        who = display_name(spec)
        prologue = f"{who} arrives at {place}."
        vision_bits.append(f"{who} is in frame, seen by the camera, entering the space.")
    else:
        prologue = f"You arrive at {place}."

    return {"prologue": prologue, "vision": " ".join(vision_bits)}


def opening_narration(spec: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """The first paragraph the player reads, written from the cast sheet.

    Returns None when nothing has been authored, so the shipped 1993 Four
    Corners opener stands.
    """
    spec = spec or get_spec()
    if not (character_enabled(spec) or setting_enabled(spec)):
        return None
    char = spec[CHARACTER_KEY]
    setting = spec[SETTING_KEY]

    sentences: List[str] = []
    if setting.get("era"):
        sentences.append(f"{setting['era'].rstrip('.')}.")
    if character_enabled(spec):
        who = display_name(spec)
        role = char.get("role")
        sentences.append(f"You are {who}{', ' + role if role else ''}.")
        if char.get("backstory"):
            sentences.append(char["backstory"].rstrip(".") + ".")
    if setting_enabled(spec):
        place = setting.get("name") or "this place"
        summary = setting.get("summary", "").rstrip(".")
        sentences.append(f"Ahead of you: {place}{' — ' + summary if summary else ''}.")
    if char.get("signature_gear"):
        sentences.append(f"You carry {char['signature_gear']}.")

    return " ".join(s for s in sentences if s) or None


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 2 — RETUNE: rewrite perspective nouns already baked into the prompts
# ═══════════════════════════════════════════════════════════════════════════

# "first person" / "first-person" / "FIRST-PERSON" …
_FIRST_PERSON_RE = re.compile(r"\bfirst[-\s]person\b", re.IGNORECASE)
_POV_RE = re.compile(r"\bPOV\b")
_EYES_RE = re.compile(r"the camera IS (your|the player's) eyes", re.IGNORECASE)


def _match_case(sample: str, replacement: str) -> str:
    """Echo the casing of the text we're replacing, so an ALL-CAPS heading stays
    an ALL-CAPS heading and inline prose stays lowercase."""
    if sample.isupper():
        return replacement.upper()
    if sample[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


# The protagonist the game shipped with. His name is written directly into a
# dozen prompt strings across engine.py, choices.py, evolve_prompt_file.py and
# lore_cache_manager.py, which is fine right up until you name your own
# character — at which point the model is being told two different people are
# holding the camera. Recasting swaps him out everywhere in one pass.
SHIPPED_PROTAGONIST_RE = re.compile(r"\bJason(?:\s+Fleece)?\b")


def recast(text: str, spec: Optional[Dict[str, Any]] = None) -> str:
    """Rename the shipped protagonist to the player's character."""
    if not text:
        return text
    spec = spec or get_spec()
    name = character_name(spec)
    if not name or not character_enabled(spec):
        return text
    return SHIPPED_PROTAGONIST_RE.sub(name, text)


def retune(text: str, spec: Optional[Dict[str, Any]] = None) -> str:
    """Rewrite hardcoded first-person wording to match the active mode.

    No-op in first person (the shipped default), so the vast majority of runs
    pay nothing for this.
    """
    if not text:
        return text
    spec = spec or get_spec()
    text = recast(text, spec)
    if is_first_person(spec):
        return text
    cfg = mode_config(spec)
    who = display_name(spec)

    text = _FIRST_PERSON_RE.sub(lambda m: _match_case(m.group(0), cfg["phrase"]), text)
    text = _POV_RE.sub(cfg["tag"], text)
    text = _EYES_RE.sub(f"the camera follows {who}", text)
    return text


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 3 — RECONCILE: delete rules that contradict the active mode
# ═══════════════════════════════════════════════════════════════════════════

# Lines whose entire purpose is "no human in frame". They are correct for first
# person and actively wrong the moment the player asked to SEE their character,
# and they're spread across editable JSON we can't safely rewrite word by word —
# so when the character is on screen, the whole line goes.
_ANTI_PERSON_LINE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"no\s+(people|person|human|humans|body\s+parts|figures?)\b",
        r"zero\s+human",
        r"never\s+show\s+(any\s+part\s+of\s+)?(a\s+)?(human|person|your\s+face|the\s+player)",
        r"camera\s+operator\s+does\s+not\s+exist",
        r"absolutely\s+no\s+person",
        r"pure\s+environmental\s+shot",
        r"environment\s+only",
        r"show\s+only\s+the\s+environment",
        r"person\s+visible,\s*human\s+visible",
        r"no\s+one\s+is\s+visible",
        r"empty\s+of\s+people",
    )
]

# The player-character rules that only make sense when they're NOT on screen.
_SELF_INVISIBLE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"never\s+show\s+your\s+(face|head|body)",
        r"(you|the\s+camera\s+operator).{0,40}\bnever\s+visible\b",
        r"no\s+part\s+of\s+(you|your\s+body)\s+(exists|is\s+visible)",
        r"your\s+own\s+body\s+is\s+not\s+visible",
        r"\byou\s+are\s+behind\s+the\s+camera\b",
        r"hands\s+can\s+be\s+visible",
        r"\bthe\s+camera\s+is\s+your\s+eyes\b",
    )
]


# Lines that explicitly BAN third-person framing. Retune can't help here — it
# rewrites "first-person", and these say "third-person" on purpose — so a line
# like "FORBIDDEN: showing the player from behind" survives into a mode that
# requires exactly that. Matched as "a prohibition AND a third-person framing
# term in the same line", which is specific enough not to eat the directive's
# own rules (none of which pair the two).
_PROHIBITION_RE = re.compile(
    r"❌|\bforbidden\b|\bnever\b|\bdo not\b|\bdon't\b|\bavoid\b|\binvalidate\b|"
    r"\bbanned\b|\bnot allowed\b|\bwrong\b",
    re.IGNORECASE,
)
_THIRD_PERSON_FRAMING_RE = re.compile(
    r"third[-\s]person|over[-\s](?:the[-\s])?shoulder|behind\s+(?:the\s+)?character|"
    r"following\s+(?:a\s+character|someone)|player\s+from\s+(?:behind|the\s+side)|"
    r"character\s+from\s+behind|player'?s\s+body|character'?s\s+back|person'?s\s+back|"
    r"chase\s+cam",
    re.IGNORECASE,
)


def _forbids_visible_character(line: str) -> bool:
    return bool(_PROHIBITION_RE.search(line) and _THIRD_PERSON_FRAMING_RE.search(line))


def reconcile(text: str, spec: Optional[Dict[str, Any]] = None) -> str:
    """Strip lines that forbid exactly what the active mode requires."""
    if not text:
        return text
    spec = spec or get_spec()
    if not shows_character(spec):
        return text
    patterns = _ANTI_PERSON_LINE_PATTERNS + _SELF_INVISIBLE_PATTERNS
    kept = [
        ln for ln in text.split("\n")
        if not any(p.search(ln) for p in patterns) and not _forbids_visible_character(ln)
    ]
    return "\n".join(kept)


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 4 — NEGATE: a negative prompt that agrees with the camera
# ═══════════════════════════════════════════════════════════════════════════

def negative_prompt(base: Optional[str] = None, spec: Optional[Dict[str, Any]] = None) -> str:
    """The negative prompt, corrected for the active perspective.

    The shipped negative bans "third person perspective, over shoulder view,
    behind character" — which silently fights a third-person request. This drops
    the phrases the mode needs and adds the ones it should be forbidding instead.
    """
    spec = spec or get_spec()
    if base is None:
        base = PROMPTS.get("image_negative_prompt", "") or ""
    cfg = mode_config(spec)

    strip = [s.lower() for s in cfg["negative_strip"]]
    if strip:
        # Split on commas AND sentence boundaries. The shipped negative prompt
        # runs sentences together inside comma-separated clauses ("following
        # someone. ABSOLUTELY NO: Black borders"), so clause-level removal alone
        # would take unrelated bans down with the one being stripped.
        parts = [p.strip() for p in re.split(r"[,\n]|(?<=\.)\s+", base) if p.strip()]
        out = ", ".join(p for p in parts if not any(s in p.lower() for s in strip))
    else:
        # First person strips nothing, so leave the author's text exactly as
        # written rather than reformatting it into a comma list.
        out = base.strip()

    additions = [e for e in cfg["negative_add"] if e.lower() not in out.lower()]
    if additions:
        out = (out + ", " if out else "") + ", ".join(additions)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# THE PIPELINE — one call for every prompt surface
# ═══════════════════════════════════════════════════════════════════════════

def apply(text: str, surface: str = "image", spec: Optional[Dict[str, Any]] = None) -> str:
    """Run a prompt through compile → retune → reconcile for the given surface.

    ``surface`` is one of:
      * ``"image"``     — full camera/cast/location directive on top
      * ``"narrative"`` — director's sheet on top
      * ``"raw"``       — retune + reconcile only, no directive prepended (for
                          prompts that already carry a directive from a caller
                          further up, so we don't stack two of them)
    """
    spec = spec or get_spec()
    if not is_active(spec):
        return text

    body = reconcile(retune(text, spec), spec)

    if surface == "image":
        head = image_directive(spec)
    elif surface == "narrative":
        head = narrative_directive(spec)
    else:
        head = ""

    if not head:
        return body
    return f"{head}\n\n{body}" if body else head


def preview() -> Dict[str, Any]:
    """Exactly what the cast sheet compiles to, for the editors' live preview.

    The whole point of the editor surfacing this is that you can see the real
    text the model will receive instead of guessing whether a field mattered.
    """
    spec = get_spec()
    cfg = mode_config(spec)
    return {
        "active": is_active(spec),
        "mode": camera_mode(spec),
        "mode_label": cfg["label"],
        "shows_character": shows_character(spec),
        "hands_visible": hands_visible(spec),
        "image_directive": image_directive(spec),
        "narrative_directive": narrative_directive(spec),
        "negative_prompt": negative_prompt(spec=spec),
        "opening_shot": opening_shot(spec),
        "reference_images": {
            "character": reference_manifest(spec[CHARACTER_KEY].get("reference_images", [])),
            "setting": reference_manifest(spec[SETTING_KEY].get("reference_images", [])),
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
# REFERENCE IMAGES
#
# A character sheet and a photo of the level, stored on disk and threaded into
# the image call as extra img2img references. Gemini takes up to 6 references
# and weights the FIRST hardest, so callers decide ordering; this module just
# resolves ids to paths.
# ═══════════════════════════════════════════════════════════════════════════

_REF_ID_RE = re.compile(r"^[a-z]+_[0-9a-f]{12}$")
_DATA_URL_RE = re.compile(r"^data:(?P<mime>image/[a-zA-Z0-9.+-]+);base64,(?P<data>.+)$", re.DOTALL)


def _ensure_ref_dir() -> None:
    REFERENCES_DIR.mkdir(parents=True, exist_ok=True)


def save_reference(data_url: str, kind: str = "character", label: str = "") -> Dict[str, Any]:
    """Persist an uploaded data-URL image and return its manifest entry.

    ``kind`` is only used to make the filename self-describing — the spec
    decides which slot an id is actually wired into.
    """
    match = _DATA_URL_RE.match((data_url or "").strip())
    if not match:
        raise ValueError("Expected a base64 image data URL (data:image/png;base64,...).")

    mime = match.group("mime").lower()
    ext = _MIME_EXT.get(mime)
    if not ext:
        raise ValueError(f"Unsupported image type '{mime}'. Use PNG, JPEG, WebP, or GIF.")

    try:
        raw = base64.b64decode(match.group("data"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Image data isn't valid base64: {exc}") from exc

    if not raw:
        raise ValueError("Image data is empty.")
    if len(raw) > MAX_REFERENCE_BYTES:
        raise ValueError(
            f"Image is {len(raw) // 1024}KB — the limit is {MAX_REFERENCE_BYTES // 1024}KB. "
            "Downscale it and try again."
        )

    slug = re.sub(r"[^a-z]", "", (kind or "ref").lower()) or "ref"
    ref_id = f"{slug}_{uuid.uuid4().hex[:12]}"
    _ensure_ref_dir()
    with _LOCK:
        (REFERENCES_DIR / f"{ref_id}{ext}").write_bytes(raw)
        meta = {
            "id": ref_id,
            "kind": slug,
            "label": _clean_text(label),
            "mime": mime,
            "bytes": len(raw),
            "created": time.time(),
        }
        (REFERENCES_DIR / f"{ref_id}.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )
    return {**meta, "url": reference_url(ref_id)}


def reference_url(ref_id: str) -> str:
    return f"/api/studio/reference/{ref_id}"


def reference_path(ref_id: str) -> Optional[Path]:
    """Resolve a reference id to its image file, or None if it's gone."""
    if not ref_id or not _REF_ID_RE.match(ref_id):
        return None
    for ext in (".png", ".jpg", ".webp", ".gif"):
        candidate = REFERENCES_DIR / f"{ref_id}{ext}"
        if candidate.exists():
            return candidate
    return None


def reference_manifest(ref_ids: List[str]) -> List[Dict[str, Any]]:
    """Editor-facing metadata for a list of ids, skipping ones that vanished."""
    out: List[Dict[str, Any]] = []
    for ref_id in ref_ids or []:
        path = reference_path(ref_id)
        if not path:
            continue
        meta: Dict[str, Any] = {"id": ref_id, "url": reference_url(ref_id)}
        meta_path = REFERENCES_DIR / f"{ref_id}.json"
        if meta_path.exists():
            try:
                meta.update(json.loads(meta_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
        meta["url"] = reference_url(ref_id)
        out.append(meta)
    return out


def delete_reference(ref_id: str) -> bool:
    """Remove the file and unwire the id from whichever slot referenced it."""
    path = reference_path(ref_id)
    removed = False
    with _LOCK:
        if path and path.exists():
            path.unlink()
            removed = True
        meta_path = REFERENCES_DIR / f"{ref_id}.json"
        if meta_path.exists():
            meta_path.unlink()

    spec = get_spec()
    updates: Dict[str, Any] = {}
    for key in (CHARACTER_KEY, SETTING_KEY):
        refs = spec[key].get("reference_images", [])
        if ref_id in refs:
            updates[key] = {"reference_images": [r for r in refs if r != ref_id]}
    if updates:
        save_spec(updates)
    return removed


def character_reference_paths(spec: Optional[Dict[str, Any]] = None) -> List[str]:
    """Character-sheet image paths for the image call (identity anchor)."""
    spec = spec or get_spec()
    ids = spec[CHARACTER_KEY].get("reference_images", [])
    return [str(p) for p in (reference_path(i) for i in ids) if p]


def setting_reference_paths(spec: Optional[Dict[str, Any]] = None) -> List[str]:
    """Level-plate image paths for the image call (place anchor)."""
    spec = spec or get_spec()
    ids = spec[SETTING_KEY].get("reference_images", [])
    return [str(p) for p in (reference_path(i) for i in ids) if p]


def identity_reference_paths(
    include_character: bool = True,
    include_setting: bool = True,
    spec: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Every user-supplied reference plate, de-duplicated, setting first.

    Setting leads because the level is the thing continuity is judged against;
    the character sheet rides behind it as an identity lock.
    """
    spec = spec or get_spec()
    out: List[str] = []
    if include_setting:
        out.extend(setting_reference_paths(spec))
    if include_character:
        out.extend(character_reference_paths(spec))
    seen: set = set()
    return [p for p in out if not (p in seen or seen.add(p))]


def reference_annotation(paths: List[str], spec: Optional[Dict[str, Any]] = None) -> str:
    """Tell the image model what the extra plates ARE.

    Without this, a character sheet appended to the reference list reads as "the
    previous frame" and the model tries to continue the *pose* instead of
    reusing the *person*.
    """
    if not paths:
        return ""
    spec = spec or get_spec()
    char_paths = set(character_reference_paths(spec))
    set_paths = set(setting_reference_paths(spec))
    lines: List[str] = []
    if any(p in set_paths for p in paths):
        lines.append(
            "• One reference is a LOCATION PLATE — a photo of the place this run happens in. "
            "Copy its architecture, materials, palette, and mood. Do NOT copy its framing."
        )
    if any(p in char_paths for p in paths):
        who = display_name(spec)
        lines.append(
            f"• One reference is a CHARACTER SHEET for {who} — the player's own character. "
            "Copy their face, build, hair, and outfit exactly so they stay the same person. "
            "Do NOT copy its background, pose, or framing."
        )
    if not lines:
        return ""
    return (
        "\n\n📎 SUPPLIED REFERENCE PLATES — WHAT THEY ARE:\n"
        + "\n".join(lines)
        + "\nThese plates define WHO and WHERE. The scene description defines WHAT IS HAPPENING."
    )
