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
import copy
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import prompts_store
from prompts_store import PROMPTS

ROOT = Path(__file__).parent.resolve()

# Where uploaded character sheets / level plates are stored. Overridable because
# only `sessions/` is on Render's persistent disk (see
# RENDER_STORAGE_LIMITATION.md) — production points this inside that mount so
# an uploaded portrait survives a deploy. Local dev keeps them in the repo tree.
REFERENCES_DIR = Path(
    os.getenv("REFERENCES_DIR") or (ROOT / "assets" / "references")
)
SESSIONS_DIR = Path(os.getenv("SESSIONS_DIR") or (ROOT / "sessions"))

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
        "rig": "a camera held at eye level by the player",
        "vantage": "first-person eye-level walking vantage, the camera is the player's own eyes",
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
        "vantage": "third-person over-the-shoulder vantage, the camera trailing a metre behind the character who stays visible in frame",
        "follow_lock": (
            "Over-the-shoulder chase cam: tight behind the character, they face INTO the "
            "space ahead"
        ),
        "image_rules": [
            "The PLAYER CHARACTER is visible in frame, seen from behind and slightly above, "
            "occupying the lower-left or lower-right third of the composition.",
            "The character's head and shoulders are sharp and clearly readable; the world opens up "
            "past them into the depth of the shot.",
            "The camera trails the character — it moves where they move, never overtakes them, "
            "and never cuts to their face.",
            "Whatever the character is holding stays visible in their hands, read from behind.",
            "Keep a stable over-the-shoulder grammar shot to shot: chest-to-head height, "
            "medium-wide, lead room in the direction they face or move. No face-on reverse, "
            "no empty POV plate, no worm's-eye hide shot.",
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
        "tagline": "Full-body follow cam from behind — Tomb Raider, Gears of War, The Last of Us.",
        "phrase": "third-person",
        "tag": "THIRD-PERSON FOLLOW CAM",
        "shows_body": True,
        "hands_default": False,
        "camera_header": "THIRD-PERSON FOLLOW-CAM VIEW",
        "rig": "a camera three to five metres behind the character at chest height, looking over their back into the space they face",
        "vantage": "third-person action-game follow-cam, several metres behind the character who faces into the scene — back of the head and shoulders toward the lens, the space ahead filling the depth of the frame",
        "follow_lock": (
            "Action-game follow cam: camera behind the character, they face INTO the space ahead. "
            "We see the back of the head, the shoulders, the gait"
        ),
        "image_rules": [
            "The PLAYER CHARACTER is fully visible — head to feet — as the clear subject of the shot.",
            "This is a third-person action-game follow cam (Tomb Raider, Gears of War, The Last of Us): "
            "the camera sits behind the character and they face INTO the space ahead. We see the back of "
            "the head, the shoulders, and the walk; the destination is readable in the depth past them.",
            "The character reads at roughly a third to a half of the frame height, with the "
            "environment opening up beyond them so the place stays legible.",
            "Never turn them to face the lens. No walking-toward-camera arrival, no front-facing portrait, "
            "no face-on reverse. A sliver of cheek or profile is the most face the shot may show.",
            # The rules above describe a person ON FOOT, and they were being
            # obeyed over the scene. A player who typed "drive truck" got prose
            # about flooring the accelerator, a visual_scene reading "the pickup
            # truck speeds across the desert", and a rendered frame of a man
            # standing at a fence with a camera — because "fully visible head to
            # feet", "the walk" and "a third to a half of the frame height" are
            # impossible inside a cab, and the camera contract is the more
            # emphatic instruction. The choices are generated from the picture,
            # so the next turn offered sprinting and vaulting a fence, and the
            # action was erased from the world. Reported as "the game isn't
            # taking the response of the custom action and injecting it into the
            # world simulator".
            "IN OR ON SOMETHING? THAT IS THE SHOT. If the scene puts the character in a vehicle, "
            "a machine, water, a crawlspace, a doorway or behind cover, frame THAT with them in it — "
            "the truck travelling away into the space ahead, the cab from behind the driver, the "
            "shape hunched in the gap. Every framing rule above bends to this one: they may be a "
            "head and shoulders behind glass, a silhouette in a cab, a body half out of frame. Do "
            "NOT stand them back up in the open to satisfy 'full body'. What the player DID decides "
            "where they are; the camera only decides where it stands.",
            "Continuity across cuts: stay in a medium-wide / full-body band at chest height, "
            "preserve screen direction (do not flip left/right travel), give lead room ahead "
            "of their motion, and never spin to a face-on reverse or an empty first-person plate. "
            "This band is for a character on foot — once they are carried by something, the "
            "vehicle keeps the frame and they keep their place inside it.",
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
            "character facing the camera", "walking toward camera",
            "front-facing portrait", "character looking at camera",
            "hero approaching the lens",
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
        "vantage": "fixed cinematic camera angle locked off in the corner of the space, the character small inside the composed frame",
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

DEFAULT_MODE = "third_person"


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

# The shipped world brief already assumes this person (Jason, photojournalist).
# It is NOT extra prompt law — it is the CAST the compiler
# below already knows how to emit, filled only when the camera can see a body
# and the player never authored one. Leaving that slot empty while third-person
# is on is how every hard-cut frame invented a new stranger (man one shot,
# woman the next): the image model was told "the subject is the player
# character" with no face, no clothes, and no plate to copy.
SHIPPED_PROTAGONIST: Dict[str, str] = {
    "name": "Jason Fleece",
    "pronouns": "he/him",
    "role": "investigative photojournalist",
    "appearance": "adult man, short dark hair, weathered face, stubble",
    "wardrobe": "olive field jacket, dark work pants, boots",
    # Not a camcorder. Naming a VHS camcorder as carried gear put "VHS" and
    # "camcorder" into a payload that asks for a photograph, and the image model
    # answered with the furniture that comes with them: tape timestamps, REC
    # dots, viewfinder brackets burned into the frame.
    "signature_gear": "a battered 35mm stills camera on a neck strap",
}

# SOMEWHERE's Level card. Same leftover problem as Jason's wardrobe: a recast
# that names a new place used to keep emitting this fence one-liner (and the
# Horizon bible still led the world seed), so the image model redrew the
# shipped yard.
SHIPPED_SETTING: Dict[str, str] = {
    "name": "SOMEWHERE",
    "summary": "1993. The fence. Four Corners. The shipped demo.",
}

SETTING_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "name": "",
    "summary": "",
    "goal": "",
    "era": "",
    "palette": "",
    "landmarks": "",
    "opening_shot": "",
    "reference_images": [],
}

CAMERA_DEFAULTS: Dict[str, Any] = {
    "mode": DEFAULT_MODE,
    "show_hands": False,
    "lens": "",
    "notes": "",
    "schemes": {},
}

# Semantic drive tokens the browser already understands. A scheme is a
# per-camera key map onto these — not a second pair of named "modes".
_DRIVE_ACTIONS = frozenset({
    "fwd", "back", "strafeL", "strafeR", "lookL", "lookR", "pitchUp", "pitchDown",
})
# The mouse is the same map as the keys: `keys.mouse = "look"` steers the
# camera. Older worlds stored a sibling `mouseLook` bool — both forms load.
_DEVICE_KEY = "mouse"
_DEVICE_ACTIONS = frozenset({"look"})

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
        elif isinstance(default, dict):
            out[field] = dict(value) if isinstance(value, dict) else dict(default)
        else:
            out[field] = _clean_text(value)
    if key == CAMERA_KEY:
        if out["mode"] not in PERSPECTIVE_MODES:
            out["mode"] = DEFAULT_MODE
        out["schemes"] = _clean_schemes(source.get("schemes", out.get("schemes")))
    return out


def _clean_schemes(raw: Any) -> Dict[str, Any]:
    """Keep only real camera ids and real drive tokens. Everything else drops."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    for sid, body in raw.items():
        if sid not in PERSPECTIVE_MODES or not isinstance(body, dict):
            continue
        keys_in = body.get("keys") if isinstance(body.get("keys"), dict) else {}
        keys = {}
        mouse_from_keys = None
        for k, act in keys_in.items():
            key = str(k or "").strip().lower()
            action = str(act or "").strip()
            if key == _DEVICE_KEY:
                mouse_from_keys = action if action in _DEVICE_ACTIONS else ""
                continue
            if key and action in _DRIVE_ACTIONS:
                keys[key] = action
        mouse_look = (
            mouse_from_keys == "look"
            if mouse_from_keys is not None
            else bool(body.get("mouseLook"))
        )
        if mouse_look:
            keys[_DEVICE_KEY] = "look"
        out[sid] = {
            "mouseLook": mouse_look,
            "invertY": bool(body.get("invertY")),
            "keys": keys,
        }
    return out


def spec_from_prompts(prompts: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """Build a normalized cast sheet from a stored prompt snapshot.

    Used by the per-World first-frame cache so a World that is not currently
    loaded can still describe its opening shot without swapping the live file.
    """
    src = prompts if isinstance(prompts, dict) else {}
    return {
        CHARACTER_KEY: _normalize(CHARACTER_KEY, src.get(CHARACTER_KEY)),
        SETTING_KEY: _normalize(SETTING_KEY, src.get(SETTING_KEY)),
        CAMERA_KEY: _normalize(CAMERA_KEY, src.get(CAMERA_KEY)),
    }


def get_spec() -> Dict[str, Dict[str, Any]]:
    """The full, normalized cast sheet as the engine sees it right now.

    Reads through ``PROMPTS``, so an edit saved by either editor is live on the
    next call with no restart (same hot-reload contract as every other prompt).
    """
    return spec_from_prompts(dict(PROMPTS))


def default_spec() -> Dict[str, Dict[str, Any]]:
    return {
        CHARACTER_KEY: dict(CHARACTER_DEFAULTS),
        SETTING_KEY: dict(SETTING_DEFAULTS),
        CAMERA_KEY: {**CAMERA_DEFAULTS, "schemes": {}},
    }


# ═══════════════════════════════════════════════════════════════════════════
# VIEWFINDER — a one-shot first-person overlay of the live sheet
#
# PHOTO restages the current still as eyes-on-the-world without saving a
# camera-mode change. The world's authored perspective stays third-person;
# only this copied spec is first-person, and only for that render.
# ═══════════════════════════════════════════════════════════════════════════

VIEWFINDER_FLAG = "_viewfinder"

VIEWFINDER_NOTES = (
    "Do not draw a camcorder, viewfinder, film HUD, or any body part of the "
    "player. The player is looking with their own eyes; the UI overlay is "
    "the camera. EMPTY FOREGROUND — no person standing in front of the lens."
)

VIEWFINDER_PLACE_LOCK = (
    "VIEWFINDER PLACE LOCK — this attachment is a follow-cam still of the "
    "SAME place and light. Copy architecture, materials, palette, weather, "
    "and illumination. ERASE the person from the attachment: they are the "
    "CAMERA OPERATOR standing in the scene. Do not draw them, their back, "
    "silhouette, face, clothes, or gear. Do not walk them around to face "
    "the lens. The space they occupied is empty air. Restage as first-person: "
    "standing where they stood, looking the direction they faced. This is "
    "WHAT THEY SEE, not a continuation of their pose."
)

_FOLLOW_CAM_PHRASES = re.compile(
    r"\b("
    r"is in frame|seen by the camera|stands before|stands in front of|"
    r"walks along|walks toward|walks towards|steps onto|kneels before|"
    r"the camera follows|follow(?:-|\s)?cam|from behind|over the shoulder|"
    r"head to feet|fully visible"
    r")\b",
    re.IGNORECASE,
)


def viewfinder_spec(spec: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A one-shot first-person overlay of the live cast sheet.

    Copy, do not save. ``shows_character`` / ``hands_visible`` become false,
    which turns off hero mode, the character plate, and KEEP-IN-FRAME.
    """
    out = copy.deepcopy(spec or get_spec())
    cam = dict(out.get(CAMERA_KEY) or {})
    cam["mode"] = "first_person"
    cam["show_hands"] = False
    notes = str(cam.get("notes") or "").strip()
    if VIEWFINDER_NOTES not in notes:
        cam["notes"] = (notes + "\n" + VIEWFINDER_NOTES).strip() if notes else VIEWFINDER_NOTES
    out[CAMERA_KEY] = cam
    out[VIEWFINDER_FLAG] = True
    return out


def is_viewfinder_spec(spec: Optional[Dict[str, Any]] = None) -> bool:
    """True only for a spec produced by :func:`viewfinder_spec`."""
    if not isinstance(spec, dict):
        return False
    return bool(spec.get(VIEWFINDER_FLAG))


def viewfinder_place_lock_label() -> str:
    return VIEWFINDER_PLACE_LOCK


def viewfinder_hero_ban(spec: Optional[Dict[str, Any]] = None) -> str:
    """Ban any person from the viewfinder. Do not name the hero.

    Naming the protagonist (``Jason Fleece is the camera operator``) is how
    the image model and the world model summon them into an empty-eyes shot.
    The 3P follow-cam still is also not a person reference — never attach it.
    """
    return (
        "EMPTY FOREGROUND. No person, body, back, silhouette, face, clothes, "
        "or gear in front of the lens. Uninhabited view. The operator is "
        "behind the camera and must never appear."
    )


def strip_follow_cam_prose(text: str, spec: Optional[Dict[str, Any]] = None) -> str:
    """Drop player-name and follow-cam verbs so a 3P vision caption can seed FP."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    who = display_name(spec)
    if who and who.lower() != "the player character":
        raw = re.sub(re.escape(who), "", raw, flags=re.IGNORECASE)
        for part in re.findall(r"[A-Za-z]+", who):
            if len(part) > 2:
                raw = re.sub(rf"\b{re.escape(part)}\b", "", raw, flags=re.IGNORECASE)
    raw = _FOLLOW_CAM_PHRASES.sub("", raw)
    raw = re.sub(r"\s{2,}", " ", raw)
    raw = re.sub(r"\s+([,.;])", r"\1", raw)
    return raw.strip(" ,.;")


# Filling one of these in is unambiguous intent to use the block, so doing it
# while the block is switched off turns it on (see save_spec). Deliberately
# excludes reference_images: deleting a plate calls save_spec too, and a delete
# must never enable anything.
_INTENT_FIELDS = ("name", "role", "appearance", "wardrobe", "signature_gear",
                  "demeanor", "backstory", "summary", "goal", "era", "palette",
                  "landmarks", "opening_shot")
# Level copy that must compile even if the leftover off-switch is still down.
# SOMEWHERE ships Level-off so the Horizon bible owns the place; typing into
# the sheet is the opt-in, same as Character.
_SETTING_COPY_FIELDS = ("name", "summary", "goal", "landmarks", "opening_shot",
                        "era", "palette")


def save_spec(partial: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Merge a partial cast-sheet update into the live prompt file.

    Accepts any subset of the three blocks and any subset of their fields, so
    the editors can PUT just what changed.

    Typing into a switched-off block switches it on. The toggle exists so you
    can A/B a character or level without deleting it, but as a *gate* it was a
    trap: you'd write a protagonist, save every field successfully, watch the
    game ignore all of it, and have no way to tell why. Nothing about naming
    your character means "and don't use them".
    """
    current = get_spec()
    old_name = character_name(current)
    fields: Dict[str, Any] = {}
    for key in SPEC_KEYS:
        if key not in partial:
            continue
        incoming = partial[key]
        if not isinstance(incoming, dict):
            continue
        merged = dict(current[key])
        merged.update(incoming)
        if (
            key in (CHARACTER_KEY, SETTING_KEY)
            and "enabled" not in incoming          # never override an explicit choice
            and not current[key].get("enabled")
            and any(str(incoming.get(f, "")).strip() for f in _INTENT_FIELDS)
        ):
            merged["enabled"] = True
        fields[key] = _normalize(key, merged)
    if fields:
        prompts_store.save_prompts_bulk(fields)
    spec = get_spec()
    if CHARACTER_KEY in fields:
        recast_stored_prompts(old_name, spec)
        spec = get_spec()
    return spec


def reset_spec() -> Dict[str, Dict[str, Any]]:
    """Clear the cast sheet back to 'unset' (first person, nobody, nowhere)."""
    prompts_store.save_prompts_bulk(default_spec())
    return get_spec()


def clear_block(key: str) -> Dict[str, Dict[str, Any]]:
    """Empty one spec block back to shipped defaults (off, blank, unwired).

    Character and level turn *off* so a Clear cannot leave the "switched on
    but every field is blank" warning. Camera returns to first person with
    no extra notes. Reference image files stay on disk; they are just
    unwired from the sheet.
    """
    if key not in SPEC_KEYS:
        raise KeyError(key)
    return save_spec({key: dict(_DEFAULTS_BY_KEY[key])})


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

# Every field carries a `tier`. "essential" fields are the ones that visibly
# change the game the moment you fill them in; "advanced" fields refine what
# they establish. The editors show essentials and fold the rest behind one
# disclosure, because twenty equal-looking inputs is how a form stops reading
# as "who am I and where am I" and starts reading as paperwork.
TIER_ESSENTIAL = "essential"
TIER_ADVANCED = "advanced"

IDENTITY_SCHEMA: List[Dict[str, Any]] = [
    {
        "id": CHARACTER_KEY,
        "label": "Your Character",
        "icon": "🧍",
        "description": (
            "Who you play as. Name and role reach everything; the look only reaches the "
            "picture when the camera can actually see you."
        ),
        "supports_images": True,
        "images_label": "Character reference",
        "images_hint": (
            "A portrait or character sheet. Locks your character to the same face in every frame."
        ),
        "fields": [
            {"id": "enabled", "label": "Use this character", "type": "toggle",
             "tier": TIER_ESSENTIAL,
             "help": "Switches on the moment you fill anything in. Turn it off to play without them, keeping the details."},
            {"id": "name", "label": "Name", "type": "text", "tier": TIER_ESSENTIAL,
             "placeholder": "Wren Alvarez"},
            {"id": "role", "label": "Role", "type": "text", "tier": TIER_ESSENTIAL,
             "placeholder": "freelance salvage diver",
             "help": "Drives the kinds of actions the game offers you."},
            {"id": "appearance", "label": "Look", "type": "longtext", "tier": TIER_ESSENTIAL,
             "placeholder": "Early thirties, close-cropped black hair, burn scar across the left jaw. Patched orange dive suit.",
             "help": "Face, build, and what they're wearing — what the image model locks onto."},
            {"id": "wardrobe", "label": "Wardrobe", "type": "longtext",
             "tier": TIER_ADVANCED,
             "placeholder": "Patched orange dive suit, mismatched boots, canvas satchel.",
             "help": "Only needed if you want clothing called out separately from Look."},
            {"id": "signature_gear", "label": "Signature gear", "type": "text",
             "tier": TIER_ADVANCED,
             "placeholder": "dented Nikon F3, sodium lamp",
             "help": "Visible in frame and usable in the fiction."},
            {"id": "pronouns", "label": "Pronouns", "type": "text", "tier": TIER_ADVANCED,
             "placeholder": "she/her"},
            {"id": "demeanor", "label": "Temperament", "type": "text", "tier": TIER_ADVANCED,
             "placeholder": "dry, unflappable, talks to herself",
             "help": "Colours how the narrator writes their reactions."},
            {"id": "backstory", "label": "Backstory", "type": "longtext", "tier": TIER_ADVANCED,
             "placeholder": "Came back for the sister who never filed a flight plan."},
        ],
    },
    {
        "id": SETTING_KEY,
        "label": "The Level",
        "icon": "🗺️",
        "description": (
            "The structured facts about this place: what to call it, what's in it, "
            "and the frame it opens on. Its landmarks keep every later frame "
            "anchored to the same space. Write the situation itself in the Level "
            "Brief below."
        ),
        "supports_images": True,
        "images_label": "Setting reference",
        "images_hint": (
            "A photo, concept plate, or screenshot. Anchors architecture, materials, and palette."
        ),
        "fields": [
            {"id": "enabled", "label": "Use this level", "type": "toggle",
             "tier": TIER_ESSENTIAL,
             "help": "Switches on the moment you fill anything in. Turn it off to fall back to the shipped opening."},
            {"id": "name", "label": "Name", "type": "text", "tier": TIER_ESSENTIAL,
             "placeholder": "The Kettle Yard"},
            {"id": "summary", "label": "What it is", "type": "longtext", "tier": TIER_ESSENTIAL,
             "placeholder": "A flooded shipbreaking yard on a tidal flat, half the hulls still standing."},
            # The one thing the opening montage needs that nothing else carried:
            # somewhere to point the camera. Written as a PLACE you can see from
            # outside, not a verb — the establishing shots put it on the horizon,
            # unreached, which is the whole hook. Draftable from the lore.
            {"id": "goal", "label": "What you're here for", "type": "longtext",
             "tier": TIER_ESSENTIAL, "fillable_from_lore": True,
             "placeholder": "The pump house with the red door, where the yard's manifests are still bolted to the wall.",
             "help": "The thing you came to reach. The opening shots show it in the distance, so name something visible."},
            # "…that must recur" explained the prompt mechanism (these get
            # re-injected every turn so the place stays the same place). That's
            # our problem, not the author's; the placeholder shows what to write.
            {"id": "landmarks", "label": "Landmarks", "type": "longtext",
             "tier": TIER_ESSENTIAL,
             "placeholder": "The listing tanker, the crane gantry, the pump house with the red door.",
             "help": "Named geography every frame keeps returning to, so the space feels real."},
            {"id": "opening_shot", "label": "Opening shot", "type": "longtext",
             "tier": TIER_ESSENTIAL,
             "placeholder": "Low tide at dawn. The tanker's hull fills the right of frame; mud flats run out to the gantry.",
             "help": "The literal first frame. Leave blank to derive it from the description."},
            {"id": "era", "label": "Era / tech level", "type": "text", "tier": TIER_ADVANCED,
             "placeholder": "1993, analog only"},
            {"id": "palette", "label": "Palette & light", "type": "text", "tier": TIER_ADVANCED,
             "placeholder": "rust orange, sodium haze, low grey sky",
             "help": "Narrower than Look in the prompt tabs — this is this level's light specifically."},
        ],
    },
    {
        "id": CAMERA_KEY,
        "label": "Camera",
        "icon": "🎥",
        "description": (
            "A real switch, not a suggestion: it rewrites the perspective language inside every "
            "prompt in the game and flips the negative prompt to match."
        ),
        "supports_images": False,
        "fields": [
            {"id": "mode", "label": "Perspective", "type": "mode", "tier": TIER_ESSENTIAL},
            {"id": "show_hands", "label": "Show your hands in frame", "type": "toggle",
             "tier": TIER_ESSENTIAL,
             "help": "First person only — whether your hands may enter the bottom of frame."},
            {"id": "lens", "label": "Lens / framing", "type": "text", "tier": TIER_ADVANCED,
             "placeholder": "28mm wide, handheld, slight dutch"},
            {"id": "notes", "label": "Extra camera notes", "type": "longtext",
             "tier": TIER_ADVANCED,
             "placeholder": "Keep the horizon low. Never look straight down."},
        ],
    },
]


def identity_schema() -> List[Dict[str, Any]]:
    """The cast-sheet form definition, with the live perspective options baked in."""
    schema = json.loads(json.dumps(IDENTITY_SCHEMA))
    for block in schema:
        for field in block["fields"]:
            field.setdefault("tier", TIER_ESSENTIAL)
            if field.get("type") == "mode":
                field["options"] = mode_options()
    return schema


# ═══════════════════════════════════════════════════════════════════════════
# DRAFT FROM A REFERENCE IMAGE
#
# Drop a plate onto an empty Character or Level sheet and the empty text
# fields fill from vision. Authored text is never overwritten. Camera has no
# image slot; any later schema block with supports_images inherits this.
# Uses ai_provider_manager.vision() (Gemini flash-lite, or mock).
# ═══════════════════════════════════════════════════════════════════════════

_FILL_FIELD_TYPES = ("text", "longtext")
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
IMAGE_FILL_MAX_TOKENS = 800


def image_fillable_blocks() -> List[str]:
    """Identity blocks that can draft their empty text fields from a plate."""
    return [b["id"] for b in IDENTITY_SCHEMA if b.get("supports_images")]


def text_fillable_blocks() -> List[str]:
    """Blocks that can draft from the world's prose — a wider set than images.

    A photograph is one source and the world bible is another, and they do not
    cover the same blocks. `camera_perspective` rightly does not support images
    (you cannot read a lens choice off a plate) and it does have two fillable text
    fields, `lens` and `notes` — which meant nothing could ever fill them, because
    every fill path was gated on supports_images. Both sat empty.
    """
    return [b["id"] for b in IDENTITY_SCHEMA if fillable_text_fields(b["id"])]


def apply_text_fill(block_id: str, *, overwrite: bool = False) -> Dict[str, Any]:
    """Draft a block's fields from the world's prose and persist.

    The text-only sibling of apply_image_fill, and the only fill path open to a
    block that has no plate to read.
    """
    if block_id not in text_fillable_blocks():
        return {"filled": {}, "skipped": True, "reason": "not_fillable"}
    wanted = [f["id"] for f in fillable_text_fields(block_id)]
    if not overwrite:
        wanted = empty_fill_fields(block_id)
        if not wanted:
            return {"filled": {}, "skipped": True, "reason": "all_filled"}

    inferred = infer_fields_from_text(block_id, wanted)
    current = get_spec().get(block_id) or {}
    patch = {}
    for key, value in inferred.items():
        if not value:
            continue
        if overwrite or not str(current.get(key, "") or "").strip():
            patch[key] = value
    if patch:
        save_spec({block_id: patch})
    return {
        "filled": patch,
        "skipped": not bool(patch),
        "reason": "" if patch else "nothing_inferred",
        "fields": list(patch.keys()),
        "source": "text",
    }


def block_for_image_kind(kind: str) -> Optional[str]:
    """Map an upload `kind` (character/setting) or a block id to a fillable sheet."""
    raw = (kind or "").strip().lower()
    aliases = {
        "character": CHARACTER_KEY,
        "player_character": CHARACTER_KEY,
        "setting": SETTING_KEY,
        "level": SETTING_KEY,
        "setting_reference": SETTING_KEY,
    }
    block_id = aliases.get(raw, raw)
    return block_id if block_id in image_fillable_blocks() else None


def fillable_text_fields(block_id: str) -> List[Dict[str, Any]]:
    for block in IDENTITY_SCHEMA:
        if block["id"] != block_id:
            continue
        return [f for f in block["fields"] if f.get("type") in _FILL_FIELD_TYPES]
    return []


def empty_fill_fields(block_id: str, spec: Optional[Dict[str, Any]] = None) -> List[str]:
    """Text field ids on this sheet that are currently blank."""
    spec = spec or get_spec()
    block = spec.get(block_id) or {}
    return [
        f["id"]
        for f in fillable_text_fields(block_id)
        if not str(block.get(f["id"], "") or "").strip()
    ]


def _identity_fill_prompt(block_id: str, field_ids: List[str]) -> str:
    import ai_provider_manager
    block = next((b for b in IDENTITY_SCHEMA if b["id"] == block_id), None)
    wanted = {f["id"]: f for f in fillable_text_fields(block_id) if f["id"] in field_ids}
    kind = "character" if block_id == CHARACTER_KEY else "place"
    # NEVER HAND THE MODEL THE PLACEHOLDER AS THE ANSWER.
    #
    # This used to read `field.get("placeholder") or field.get("help")`, so the
    # request for a wardrobe was literally:
    #     - "wardrobe": Wardrobe — Patched orange dive suit, mismatched boots…
    # and the model did the obvious thing and echoed it back. A drafted sheet
    # came out wearing the example: orange dive suit, "Dented Nikon F3, sodium
    # lamp", she/her, "talks to herself", and the sister who never filed a
    # flight plan — five of the eight character fields, verbatim, on a
    # photograph of somebody else entirely. Those fields are `advanced` and
    # hidden in the minimal editor, so the author never saw where the orange
    # jumpsuit in their game was coming from.
    #
    # `help` describes the field. The placeholder is UI furniture, and it only
    # goes in clearly marked as a shape to avoid. _drop_placeholder_echoes is
    # the guarantee — a model shown an example will sometimes copy it however
    # it is labelled.
    lines = []
    examples = []
    for fid in field_ids:
        field = wanted.get(fid)
        if not field:
            continue
        hint = field.get("help") or ""
        lines.append(f'- "{fid}": {field["label"]}' + (f" — {hint}" if hint else ""))
        ph = str(field.get("placeholder") or "").strip()
        if ph:
            examples.append(f'- "{fid}": {ph}')
    label = (block or {}).get("label") or block_id
    dont_copy = (
        "\n\nThese are the editor's own example strings, shown so you can see "
        "the SHAPE and length expected. They are about a different character "
        "in a different story. Do not reuse their content, and never return "
        "one of them as an answer:\n" + "\n".join(examples)
    ) if examples else ""
    return (
        f"{ai_provider_manager.IDENTITY_DRAFT_MARKER}\n"
        f"block={block_id}\n"
        f"Look at this reference image of a {kind} ({label}). "
        f"Write a short playable draft for a game sheet from what you can see.\n"
        f"Return ONLY a JSON object with these keys (omit a key if you cannot tell):\n"
        + "\n".join(lines)
        + "\nRules: short and concrete; visual first; a real name, not Unknown; "
        "no plot. Omit any key you would be guessing at — a blank field is "
        "fixable, an invented one is not. JSON only, no markdown."
        + dont_copy
    )


def _drop_placeholder_echoes(block_id: str, drafted: Dict[str, str]) -> Dict[str, str]:
    """Discard drafted values that are just the field's own example back.

    Belt to the prompt's braces: told not to copy the examples, a model still
    will when it cannot read the answer off the picture — and that is exactly
    when the echo is most convincing and most wrong.
    """
    if not drafted:
        return drafted

    # _norm_field alone is not enough here: it lowercases and collapses space
    # but keeps punctuation, so an echo that arrives with a full stop welded on
    # ("…sodium lamp.") reads as a different string from the example it copied.
    def _same(text: str) -> str:
        return _norm_field(text).strip(" .,;:!?\"'")

    placeholders = {
        f["id"]: _same(str(f.get("placeholder") or ""))
        for f in fillable_text_fields(block_id)
    }
    out: Dict[str, str] = {}
    for fid, value in drafted.items():
        ph = placeholders.get(fid) or ""
        if ph and _same(str(value or "")) == ph:
            print(f"[IDENTITY] {block_id}.{fid}: draft echoed the placeholder "
                  f"— dropped", flush=True)
            continue
        out[fid] = value
    return out


def _parse_fill_json(raw: str) -> Dict[str, str]:
    if not raw or "Signal interrupted" in raw:
        return {}
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    data: Any = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(text)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in data.items():
        if value is None:
            continue
        cleaned = _clean_text(value)
        if cleaned:
            out[str(key)] = cleaned
    return out


def infer_fields_from_image(
    block_id: str,
    image_path: str,
    only_fields: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Ask vision for a draft of the named fields. Does not persist."""
    import ai_provider_manager
    allowed = {f["id"] for f in fillable_text_fields(block_id)}
    wanted = [f for f in (only_fields or sorted(allowed)) if f in allowed]
    if not wanted or not image_path:
        return {}
    raw = ai_provider_manager.vision(
        image_path=image_path,
        prompt=_identity_fill_prompt(block_id, wanted),
        max_tokens=IMAGE_FILL_MAX_TOKENS,
    )
    parsed = _parse_fill_json(raw)
    return _drop_placeholder_echoes(
        block_id, {fid: parsed[fid] for fid in wanted if parsed.get(fid)})


def infer_fields_from_text(
    block_id: str,
    only_fields: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Draft the named fields from the world's own prose. Does not persist.

    The other half of the fill contract, and it was missing. Every path into
    autofill went through vision, so a sheet could only be drafted from an
    attached plate — and a world authored in words has no plate. The result was
    that each field added to the schema stayed permanently empty on those worlds:
    `setting_reference.goal` was "", so level_goal() fell back to naming a
    landmark, and the opening montage was told the player had come here to reach
    a chain-link fence they were already standing at.

    There is plenty to read without a picture: the world bible is thousands of
    words, and whatever fields the author HAS filled describe the rest.
    """
    allowed = {f["id"] for f in fillable_text_fields(block_id)}
    wanted = [f for f in (only_fields or sorted(allowed)) if f in allowed]
    if not wanted:
        return {}

    bible = ""
    try:
        import prompts_store
        bible = str(prompts_store.PROMPTS.get("world_initial_state") or "")
    except Exception:
        bible = ""

    current = get_spec().get(block_id) or {}
    known = {k: v for k, v in current.items()
             if k not in ("enabled", "reference_images")
             and str(v or "").strip() and k not in wanted}
    if not bible.strip() and not known:
        return {}

    prompt = (
        f"{_identity_fill_prompt(block_id, wanted)}\n\n"
        "You have no photograph this time. Draft the fields from the world "
        "described below, and from the fields that are already filled in — stay "
        "consistent with both. Be specific and concrete; a field that restates "
        "the question ('a navigable space you can walk through') is worse than "
        "useless, because the rest of the game reads it as though it meant "
        "something.\n\n"
        f"ALREADY FILLED IN (do not contradict):\n{json.dumps(known, indent=2)}\n\n"
        f"THE WORLD:\n{bible[:6000]}\n"
    )

    try:
        import engine
        raw = engine._ask(prompt, temp=0.9, tokens=IMAGE_FILL_MAX_TOKENS,
                          use_lore=False)
    except Exception as err:
        print(f"[IDENTITY] text fill failed: {err}", flush=True)
        return {}
    parsed = _parse_fill_json(raw)
    return _drop_placeholder_echoes(
        block_id, {fid: parsed[fid] for fid in wanted if parsed.get(fid)})


def apply_image_fill(
    block_id: str,
    image_path: Optional[str] = None,
    ref_id: Optional[str] = None,
    *,
    overwrite: bool = False,
    allow_text: bool = False,
) -> Dict[str, Any]:
    """Draft sheet fields from a reference image and persist.

    Default keeps author text (fill blanks only). ``overwrite=True`` treats
    the plate as the only input and rewrites every fillable field — that is
    what a Character / Level upload in the editor wants.
    """
    import ai_provider_manager
    if block_id not in image_fillable_blocks():
        return {"filled": {}, "skipped": True, "reason": "not_fillable"}

    path = image_path
    if not path:
        if not ref_id:
            refs = (get_spec().get(block_id) or {}).get("reference_images") or []
            ref_id = refs[0] if refs else None
        resolved = reference_path(ref_id) if ref_id else None
        path = str(resolved) if resolved else None

    wanted = [f["id"] for f in fillable_text_fields(block_id)]
    if not overwrite:
        wanted = empty_fill_fields(block_id)
        if not wanted:
            return {"filled": {}, "skipped": True, "reason": "all_filled"}

    # No plate is no longer the end of it. Returning "no_image" here is what left
    # a text-authored world permanently hollow: the fields existed, the editor
    # offered to fill them, and nothing ever did.
    if path:
        inferred = infer_fields_from_image(block_id, path, wanted)
        source = "vision"
        # A plate answers what a place LOOKS like and not what the player is
        # doing there, so anything it could not see is drafted from the prose.
        still_blank = [f for f in wanted if not str(inferred.get(f) or "").strip()]
        if still_blank:
            inferred = dict(inferred, **infer_fields_from_text(block_id, still_blank))
            source = "vision+text"
    else:
        inferred = infer_fields_from_text(block_id, wanted)
        source = "text"

    current = get_spec().get(block_id) or {}
    patch = {}
    for key, value in inferred.items():
        if not value:
            continue
        if overwrite or not str(current.get(key, "") or "").strip():
            patch[key] = value
    if patch:
        save_spec({block_id: patch})
    return {
        "filled": patch,
        "skipped": not bool(patch),
        "reason": "" if patch else "nothing_inferred",
        "fields": list(patch.keys()),
        "source": source,
        "backend": ai_provider_manager.active_backend("vision"),
        "model": ai_provider_manager.resolve_model(None, "vision"),
    }


def attach_reference_and_fill(
    block_id: str,
    ref_id: str,
    *,
    overwrite: bool = True,
) -> Dict[str, Any]:
    """Wire a plate and draft the sheet from it in one save.

    Attaching first, then filling, dirtied the World on leftover CAST text
    (the shipped photojournalist) before vision had named the new person.
    The editor must not mark the node dirty until this returns.
    """
    import ai_provider_manager
    if block_id not in image_fillable_blocks():
        return {"filled": {}, "skipped": True, "reason": "not_fillable"}
    current = get_spec().get(block_id) or {}
    refs = list(current.get("reference_images") or [])
    if ref_id and ref_id not in refs:
        refs.append(ref_id)
    resolved = reference_path(ref_id) if ref_id else None
    path = str(resolved) if resolved else None
    wanted = [f["id"] for f in fillable_text_fields(block_id)]
    if not overwrite:
        wanted = [
            fid for fid in wanted
            if not str(current.get(fid, "") or "").strip()
        ]
    inferred: Dict[str, str] = {}
    if wanted:
        if path:
            inferred = infer_fields_from_image(block_id, path, wanted)
            # What the plate could not see, the prose can. Choosing a new
            # reference image has to leave the sheet COMPLETE, not just
            # complete in the fields a photograph happens to answer.
            blank = [f for f in wanted if not str(inferred.get(f) or "").strip()]
            if blank:
                inferred = dict(inferred, **infer_fields_from_text(block_id, blank))
        else:
            inferred = infer_fields_from_text(block_id, wanted)
    filled: Dict[str, str] = {}
    for key, value in inferred.items():
        if not value:
            continue
        if overwrite or not str(current.get(key, "") or "").strip():
            filled[key] = value
    # A NEW PLATE IS A NEW PERSON, NOT A PATCH OVER THE OLD ONE.
    #
    # Only the fields vision could answer were written, so everything it could
    # not stayed behind from whoever was on the sheet before — and most of
    # those fields are `advanced`, so the author never saw them. A sheet was
    # found reading name "Jason Fleece" (the shipped default), pronouns
    # "she/her" (left from an earlier recast), Look "blue press flak jacket"
    # (the new upload) and Wardrobe "Patched orange dive suit" (the editor's
    # own placeholder text, saved as a value). Every prompt then carried two
    # outfits and two genders for one person, and the image model picked a
    # different answer per frame.
    #
    # An overwriting draft therefore CLEARS what it could not read. A blank
    # field is visibly missing and the author can fill it; a stale one is
    # invisible and contradicts the photograph. `filled` stays the fields that
    # got real text — it is what the editor reports back — so the clears go
    # straight onto the patch.
    cleared = [fid for fid in wanted if fid not in filled] if overwrite else []
    reason = (
        "" if filled else
        ("no_image" if not path else
         "all_filled" if not wanted else "nothing_inferred")
    )
    # Wire the plate even when nothing could be read off it. This used to be
    # `if filled:`, so a photograph vision had no words for was silently not
    # attached at all — the upload looked like it worked and changed nothing.
    attaching = bool(ref_id) and refs != list(current.get("reference_images") or [])
    if filled or attaching:
        patch: Dict[str, Any] = {"reference_images": refs, "enabled": True}
        for fid in cleared:
            patch[fid] = ""
        patch.update(filled)
        save_spec({block_id: patch})
    return {
        "filled": filled,
        "skipped": not bool(filled),
        "reason": reason,
        "fields": list(filled.keys()),
        "cleared": cleared,
        "attached": ref_id if (filled or attaching) else "",
        "backend": ai_provider_manager.active_backend("vision"),
        "model": ai_provider_manager.resolve_model(None, "vision"),
    }


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
    # An uploaded character sheet DEFINES the protagonist even with the text
    # fields left blank. Without this, an image-only character read as "no
    # character" — so uses_shipped_protagonist() flipped true and every prompt's
    # PROSE fell back to "Jason Fleece" while the plate drew the upload, and the
    # image model coin-flipped between the two (the "randomly switches to Jason"
    # bug, worst in encounters where several text prompts name the protagonist).
    # A plate that is not on disk is not a plate. See live_reference_ids: this
    # used to read `char.get("reference_images")`, so a dead id reported an
    # image-only character with no image.
    if live_reference_ids(char):
        return True
    return any(char.get(f) for f in ("name", "role", "appearance", "wardrobe", "signature_gear"))


def setting_enabled(spec: Optional[Dict[str, Any]] = None) -> bool:
    spec = spec or get_spec()
    setting = spec[SETTING_KEY]
    if not setting.get("enabled"):
        return False
    return any(setting.get(f) for f in ("name", "summary", "goal", "landmarks",
                                        "opening_shot"))


def setting_authored(spec: Optional[Dict[str, Any]] = None) -> bool:
    """True when the Level sheet has anything to send, toggle or not.

    The off-switch used to swallow landmarks / palette / era, so the editor
    looked saved and the live scene never heard the words.
    """
    spec = spec or get_spec()
    setting = spec[SETTING_KEY]
    return any(str(setting.get(f) or "").strip() for f in _SETTING_COPY_FIELDS)


def character_name(spec: Optional[Dict[str, Any]] = None) -> str:
    spec = spec or get_spec()
    return spec[CHARACTER_KEY].get("name", "").strip()


def uses_shipped_protagonist(spec: Optional[Dict[str, Any]] = None) -> bool:
    """Camera can see a body, and the player never filled in who that is."""
    spec = spec or get_spec()
    return shows_character(spec) and not character_enabled(spec)


def effective_character(spec: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The CAST the image/narrative compilers should emit.

    Authored fields win. Otherwise, if the camera is looking at a person, the
    shipped protagonist — the one ``world_initial_state`` already names —
    fills the empty sheet so a hard cut has *someone* to draw instead of a
    coin-flip stranger. First person with no character stays empty.
    """
    spec = spec or get_spec()
    if character_enabled(spec):
        return spec[CHARACTER_KEY]
    if shows_character(spec):
        out = dict(CHARACTER_DEFAULTS)
        out.update(SHIPPED_PROTAGONIST)
        out["enabled"] = True
        return out
    return spec[CHARACTER_KEY]


def _norm_field(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def is_shipped_cast(spec: Optional[Dict[str, Any]] = None) -> bool:
    """True when this sheet is still the shipped Jason identity.

    A recast (new name, new look, or a character plate) is someone else even
    if hidden fields still hold Jason's wardrobe and pronouns — the Experience
    editor only shows Name / Role / Look, so those leftovers are not author
    intent.
    """
    spec = spec or get_spec()
    char = effective_character(spec)
    name = _norm_field(char.get("name"))
    look = _norm_field(char.get("appearance"))
    if name and name != _norm_field(SHIPPED_PROTAGONIST["name"]):
        return False
    if look and look != _norm_field(SHIPPED_PROTAGONIST["appearance"]):
        return False
    if live_reference_ids(char):
        return False
    return True


# Values SHIPPED_PROTAGONIST used to hold. A sheet saved before the default
# changed still carries the old string, and the leftover-drop below matches by
# value — so retiring a default without listing it here silently promotes every
# stale copy of it to author intent.
_RETIRED_SHIPPED_FIELDS: Dict[str, Tuple[str, ...]] = {
    "signature_gear": ("1993 VHS camcorder",),
}


def _shipped_field_values(field: str) -> Tuple[str, ...]:
    current = SHIPPED_PROTAGONIST.get(field, "")
    return (current,) + _RETIRED_SHIPPED_FIELDS.get(field, ())


def authored_character(spec: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """CAST fields that should actually reach the image and story prompts.

    SOMEWHERE ships Jason's pronouns, jacket, and camcorder into fields the
    minimal editor does not show. Changing Look to "adult woman" used to
    still emit ``he/him`` + olive field jacket as if the author chose them,
    and the image model redrew the default guy. Those leftovers are dropped
    once the sheet is no longer the shipped cast.
    """
    spec = spec or get_spec()
    char = dict(effective_character(spec))
    if is_shipped_cast(spec):
        return char
    for field in ("pronouns", "wardrobe", "signature_gear"):
        current = _norm_field(char.get(field))
        if any(current == _norm_field(v) for v in _shipped_field_values(field)):
            char[field] = ""
    # NAME / ROLE / LOOK ARE THE AUTHOR'S, WHATEVER THEY SAY.
    #
    # These three used to be blanked as well whenever they matched the shipped
    # values and a plate existed, to cover a different bug: the upload skipped
    # filling a field that already had text, so a photograph of a woman still
    # compiled as "Jason Fleece, adult man". That skip is gone — an overwriting
    # draft now clears what it could not read (see attach_reference_and_fill),
    # so a new plate cannot leave the previous person's name behind and there
    # is nothing left to compensate for.
    #
    # What the blanking could never do is tell "the author left the default
    # there" from "the author typed that name", so it made Jason Fleece an
    # unusable name for anybody who uploaded a photo: the sheet said Jason, the
    # prose said "the player character", and the author had no way to see why.
    # These three are the fields the minimal editor SHOWS. A field you can see
    # and edit is your choice by definition. The leftover drop above stays,
    # because those fields are hidden and genuinely are not.
    return char


def is_shipped_setting(spec: Optional[Dict[str, Any]] = None) -> bool:
    """True when the Level sheet is still the shipped SOMEWHERE fence plate.

    Empty counts as shipped (Play falls back to the Horizon openers). A new
    name, summary, landmark, opening shot, or location plate is a recast.
    """
    spec = spec or get_spec()
    if not setting_authored(spec):
        return True
    setting = spec[SETTING_KEY]
    if setting.get("reference_images"):
        return False
    if any(str(setting.get(f) or "").strip()
           for f in ("goal", "landmarks", "opening_shot", "era", "palette")):
        return False
    name = _norm_field(setting.get("name"))
    summary = _norm_field(setting.get("summary"))
    if name and name != _norm_field(SHIPPED_SETTING["name"]):
        return False
    if summary and summary != _norm_field(SHIPPED_SETTING["summary"]):
        return False
    return True


def authored_setting(spec: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Level fields that should actually reach the image and story prompts.

    Same leftover hole as Jason's Name / Look: a location plate over the
    shipped SOMEWHERE fence used to keep emitting that one-liner, so MOVE TO
    redrew the yard. Shipped name / summary drop once the sheet is a recast.
    """
    spec = spec or get_spec()
    setting = dict(spec[SETTING_KEY])
    if is_shipped_setting(spec):
        return setting
    for field in ("name", "summary"):
        if _norm_field(setting.get(field)) == _norm_field(SHIPPED_SETTING.get(field, "")):
            setting[field] = ""
    return setting


def display_name(spec: Optional[Dict[str, Any]] = None) -> str:
    """What to call the protagonist in prompt prose when a name isn't set."""
    name = (authored_character(spec).get("name") or "").strip()
    if name:
        return name
    if uses_shipped_protagonist(spec):
        return SHIPPED_PROTAGONIST["name"]
    return "the player character"


def is_active(spec: Optional[Dict[str, Any]] = None) -> bool:
    """True when the design sheet is doing anything at all.

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

    # No precedence clause. This header used to open "THIS OVERRIDES ANY
    # CONFLICTING CAMERA LANGUAGE BELOW", which was an accurate description of
    # the payload at the time — `image_camera_rules` really did contradict it,
    # because it bypassed `retune`/`reconcile` on its way into the template (see
    # prompts_store._perspective_corrected). Both halves of that are fixed: the
    # shared blocks are corrected now, and they no longer carry perspective
    # language of their own. Announcing a conflict that no longer exists just
    # tells the model to go looking for one and to treat this block as
    # contested rather than simply true.
    return (
        f"🎥 CAMERA: {cfg['camera_header']} ({cfg['label']}).\n"
        f"RIG: {cfg['rig']}.\n"
        f"{_bullets(rules)}"
    )


def character_visual_sheet(
    spec: Optional[Dict[str, Any]] = None,
    hands_only: bool = False,
) -> str:
    """The CAST block — what the protagonist physically looks like.

    Only meaningful to the image model when the body (or hands) can be seen, so
    callers should gate on :func:`shows_character` / :func:`hands_visible`.

    ``hands_only`` is the first-person case, where a forearm is the single part
    of the player that can enter frame. The full sheet used to ship there too,
    so the payload named a face, a hairline and a carried-gear list, demanded
    "the SAME person in every single frame", and told the model not to spin them
    to face the lens — while the same payload's tail said "No person in frame:
    no head, shoulders, back, hands, or silhouette". Three positions on one
    question, and the render split the difference from frame to frame.
    """
    spec = spec or get_spec()
    char = authored_character(spec)
    if not any(char.get(f) for f in ("name", "role", "appearance", "wardrobe", "signature_gear")):
        return ""

    if hands_only:
        if not char.get("wardrobe"):
            return ""
        return (
            "🧍 THE PLAYER'S OWN HANDS\n"
            "If a hand or forearm enters the bottom of frame it is the player's, "
            f"dressed in: {char['wardrobe'].rstrip('.')}. Nothing above the wrists."
        )

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
        "must not drift between shots. Lock identity from the back of the head and "
        "wardrobe; do not spin them to face the lens just to prove the face matches."
    )
    return "🧍 PLAYER CHARACTER — WHO IS ON SCREEN\n" + "\n".join(lines) + f"\n{tail}"


def setting_plate(spec: Optional[Dict[str, Any]] = None) -> str:
    """The LOCATION block — the level the whole run takes place in."""
    spec = spec or get_spec()
    if not setting_authored(spec):
        return ""
    setting = authored_setting(spec)

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
    char = authored_character(spec)
    if not any(char.get(f) for f in ("role", "appearance", "wardrobe")):
        return ""
    bits = [char.get("role"), char.get("appearance"), char.get("wardrobe")]
    tags = ", ".join(b.rstrip(". ") for b in bits if b)
    return tags[:220]


def image_directive(spec: Optional[Dict[str, Any]] = None) -> str:
    """Camera + cast + location, assembled for an image prompt."""
    spec = spec or get_spec()
    blocks = [camera_directive(spec)]
    if shows_character(spec):
        sheet = character_visual_sheet(spec)
    elif hands_visible(spec):
        sheet = character_visual_sheet(spec, hands_only=True)
    else:
        sheet = ""
    if sheet:
        blocks.append(sheet)
    # A viewfinder restage copies THIS frame. The authored level plate
    # describes the opening location and fights the live grab.
    if not is_viewfinder_spec(spec):
        plate = setting_plate(spec)
        if plate:
            blocks.append(plate)
    return "\n\n".join(blocks)


def visual_scene_guidance(spec: Optional[Dict[str, Any]] = None) -> str:
    """How to write `visual_scene` for the active camera.

    The consequence template historically described places as "somebody
    standing in them would see them" — pure first-person looking-ahead prose.
    When the camera is third-person that text never names the body, so the
    image model either drops the character (reads as a POV cut) or invents a
    reverse/face-on angle to force them in. This block is the corrective: it
    travels with the consequence prompt and must stay short.
    """
    spec = spec or get_spec()
    cfg = mode_config(spec)
    who = display_name(spec)

    if not cfg["shows_body"]:
        return (
            "WRITING visual_scene FOR THIS CAMERA\n"
            "Describe the place ahead through the player's eyes after the action. "
            "No face, head, back, or torso of the player — hands only if they are "
            "reaching. Never use frame-relative language (\"lower left\", \"edge of "
            "frame\"). If they moved, describe where they arrived, not the doorway "
            "they already passed through."
        )

    lock = cfg.get("follow_lock") or (
        "Hold the shipped follow grammar: behind or beside them, chest height, "
        "medium-wide / full-body, with lead room in the direction they face or move"
    )
    return (
        f"WRITING visual_scene FOR THIS CAMERA\n"
        f"This is a {cfg['phrase']} shot. Name {who} in the sentence — what they "
        f"are doing and where they stand in the place. Write it as an exterior "
        f"observation of them in the room, not as what they see looking forward.\n"
        f"{lock}.\n"
        f"Do not write an empty environment plate, a hands-only POV, a "
        f"face-on reverse, a walking-toward-camera hero shot, a worm's-eye hide shot, "
        f"or a tight product shot of the destination alone. Never use frame-relative "
        f"language (\"lower left\", \"edge of frame\")."
    )


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

    lines: List[str] = []

    lines.append(f"CAMERA: {cfg['label']} — {cfg['tagline']}")
    lines.append(cfg["narrative_rule"])

    if character_enabled(spec) or uses_shipped_protagonist(spec):
        who = display_name(spec)
        char = authored_character(spec)
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
            # The permission and the address had to be said in one breath.
            # This sentence used to end "...you may describe their body, stance,
            # and visible injuries as the player sees them" — a licence to
            # describe a body, sitting two lines under "write to the player as
            # 'you'" and immediately after a proper noun. A concrete name beats
            # an abstract rule, and the model reconciled the two the obvious
            # way: by narrating in the third person. Measured across 32 live
            # turns, 6% of the prose came back as "Isaac surges from the
            # darkness, his electrified baton humming" — and once it slipped it
            # stayed slipped into the following turn. The body is describable;
            # the name is not a word the player ever reads.
            lines.append(
                f"Because the camera watches {who} from outside, you may describe their body, "
                "stance, and visible injuries — but always as \"you\" and \"your\". "
                f"Never write \"{who}\" in the prose and never switch to he/she/they. "
                "The name is how you know who to draw, not a word the player reads."
            )

    if setting_authored(spec):
        setting = authored_setting(spec)
        if setting.get("name"):
            lines.append(f"THE PLACE: {setting['name']}.")
        if setting.get("summary"):
            lines.append(setting["summary"])
        if setting.get("landmarks"):
            lines.append(f"Established landmarks to stay consistent with: {setting['landmarks']}")

    return "🎬 DIRECTOR'S SHEET — CAST, CAMERA, AND PLACE\n" + "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# COMPACT SURFACES
#
# A dozen prompts in the pipeline are too small to take the full directive: a
# realtime world-model prompt is capped at 2000 characters, a vision-analysis
# prompt has to stay terse or the model stops answering in the requested
# format, and a talk persona is three sentences. Bolting the 400-character
# CAMERA/CAST/LOCATION block onto those either blows the budget or drowns the
# actual instruction.
#
# They still need to know who and where, though — and until they did, the
# authored level lost every argument with the hardcoded one. These helpers are
# the one-line version of the same facts, so a short prompt can carry the cast
# sheet without being taken over by it. Each returns "" when nothing has been
# authored, so callers can concatenate unconditionally.
# ═══════════════════════════════════════════════════════════════════════════

def vantage(spec: Optional[Dict[str, Any]] = None) -> str:
    """The active camera rig as a single descriptive clause."""
    return mode_config(spec)["vantage"]


def place_line(spec: Optional[Dict[str, Any]] = None) -> str:
    """One line naming the level and what it looks like, or "" if unauthored."""
    spec = spec or get_spec()
    if not setting_authored(spec):
        return ""
    setting = authored_setting(spec)
    head = setting.get("name") or "the level"
    bits: List[str] = []
    if setting.get("summary"):
        bits.append(setting["summary"].rstrip("."))
    if setting.get("era"):
        bits.append(setting["era"].rstrip("."))
    if setting.get("palette"):
        bits.append(setting["palette"].rstrip("."))
    if setting.get("landmarks"):
        bits.append("landmarks: " + setting["landmarks"].rstrip("."))
    return f"{head} — " + "; ".join(bits) + "." if bits else f"{head}."


def place_summary(spec: Optional[Dict[str, Any]] = None) -> str:
    """Just the level's name and one-line description — no palette or landmarks.

    For prompts that want the place named but already carry their own look
    direction (image descriptions, situation reports).
    """
    spec = spec or get_spec()
    if not setting_authored(spec):
        return ""
    setting = authored_setting(spec)
    name = setting.get("name") or "the level"
    summary = (setting.get("summary") or "").rstrip(".")
    return f"{name} — {summary}." if summary else f"{name}."


# Leftover biome phrases from the shipped SOMEWHERE look. A recast Level
# that names a new place used to still emit these as image law.
_SHIPPED_PLACE_MARKERS = (
    "american southwest",
    "four corners",
    "horizon industries",
    "horizon facility",
    "horizon research",
)

# An ALL-CAPS section label in the art-direction block ("LOOK", "WORLD & ERA").
_HEADING_RE = re.compile(r"^[A-Z0-9][A-Z0-9 &/'\-]{0,38}$")


def _is_art_heading(line: str) -> bool:
    return bool(_HEADING_RE.match(line.strip()))


def strip_shipped_place(raw: str) -> str:
    """Drop shipped-biome SENTENCES, then any heading left with no body.

    This used to drop whole LINES, and the shipped art direction puts the year,
    the period-technology list and the touchstones on the same line as "American
    Southwest". A recast Level therefore deleted its own medium anchor and kept
    the label, so the image model received a bare "WORLD & ERA" followed by "A
    photoreal still from that year" — with no year left anywhere in the payload
    for "that year" to refer to. Nothing then stated the medium at all, and the
    register drifted between a photograph and a game render frame to frame.
    """
    kept: List[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or _is_art_heading(stripped):
            kept.append(line)
            continue
        survivors = [
            s for s in re.split(r"(?<=[.!?])\s+", stripped)
            if not any(m in s.lower() for m in _SHIPPED_PLACE_MARKERS)
        ]
        if survivors:
            kept.append(" ".join(survivors))
    # A heading whose body was just deleted is worse than no heading: it names a
    # concern the payload then says nothing about.
    out: List[str] = []
    for i, line in enumerate(kept):
        if _is_art_heading(line.strip()):
            nxt = next((l.strip() for l in kept[i + 1:] if l.strip()), "")
            if not nxt or _is_art_heading(nxt):
                continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def authored_art_direction(spec: Optional[Dict[str, Any]] = None) -> str:
    """GAME look, minus leftover shipped biome when the Level is a recast.

    ``image_art_direction`` is a GAME prompt — it is not rewritten when you
    type a new Level name. The leftover Southwest / Horizon sentences then
    beat the Level plate the same way Jason's jacket beat a recast Look.
    """
    spec = spec or get_spec()
    raw = str(PROMPTS.get("image_art_direction") or "").strip()
    if is_shipped_setting(spec):
        return raw
    look = strip_shipped_place(raw)
    setting = spec[SETTING_KEY]
    lead: List[str] = []
    place = place_summary(spec)
    if place:
        # No "not the shipped Horizon desert fence" here. Naming the thing to
        # avoid puts it in the payload, and a diffusion model reads the noun
        # long before it reads the negation attached to it.
        lead.append(f"WORLD & PLACE\nThis run is at {place}")
    if setting.get("era"):
        lead.append(f"ERA: {setting['era']}")
    if setting.get("palette"):
        lead.append(f"PALETTE: {setting['palette']}")
    head = "\n".join(lead)
    if head and look:
        return f"{head}\n\n{look}"
    return head or look


# Words that cannot end a sentence. A word-boundary cut can land on any of
# them and leave the anchor promising something it never says.
_DANGLING_TAIL = re.compile(
    r"\s+(?:never|and|or|but|with|without|the|an?|of|to|in|on|for|from|than|"
    r"that|which|while|as|at|by|is|are|was|were|not|no|only)\s*$",
    re.I,
)


def look_line(spec: Optional[Dict[str, Any]] = None) -> str:
    """A short LOOK for the live world, from the GAME art-direction prompt.

    The stills bible is a multi-section block. The video model gets one
    sentence cluster or it drowns the scene. Empty when nothing is authored.
    A recast Level drops leftover Southwest sentences and names the new
    place first so live video does not keep walking the fence.
    """
    spec = spec or get_spec()
    raw = str(PROMPTS.get("image_art_direction") or "").strip()
    if raw and not is_shipped_setting(spec):
        raw = strip_shipped_place(raw)
    if not raw:
        return ""
    lines: List[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("_"):
            continue
        if s.isupper() and len(s) < 28:
            continue
        lines.append(s)
    text = " ".join(lines)
    if not text:
        return ""
    if len(text) > 180:
        # Cutting on a word boundary and bolting a full stop onto the end
        # turned "Threats are human or biological - never robots or future
        # tech" into "Threats are human or biological - never." A dangling
        # negation with no object, sitting in every image prompt the game
        # sends. Prefer the last whole sentence that fits.
        head = text[:180]
        stop = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
        if stop > 60:
            text = head[:stop + 1].strip()
        else:
            sp = head.rfind(" ")
            text = (head[:sp] if sp > 80 else head).rstrip(" ,;:-\u2014")
            while True:
                trimmed = _DANGLING_TAIL.sub("", text)
                if trimmed == text:
                    break
                text = trimmed
            text = text.rstrip(" ,;:-\u2014") + "."
    return text


def protagonist_line(spec: Optional[Dict[str, Any]] = None) -> str:
    """One line naming the protagonist and what they look like, or ""."""
    spec = spec or get_spec()
    char = authored_character(spec)
    if not any(char.get(f) for f in ("name", "role", "appearance", "wardrobe", "signature_gear")):
        return ""
    head = display_name(spec)
    if char.get("pronouns"):
        head += f" ({char['pronouns']})"
    bits: List[str] = []
    for field in ("role", "appearance", "wardrobe"):
        if char.get(field):
            bits.append(char[field].rstrip("."))
    if char.get("signature_gear"):
        bits.append("carrying " + char["signature_gear"].rstrip("."))
    return f"{head} — " + "; ".join(bits) + "." if bits else f"{head}."


def scene_grounding(spec: Optional[Dict[str, Any]] = None) -> str:
    """Who / where / which camera, in at most three lines.

    The compact counterpart to :func:`image_directive`, for prompts that
    analyse or describe the scene rather than render it.
    """
    spec = spec or get_spec()
    if not is_active(spec):
        return ""
    lines = [f"CAMERA: {vantage(spec)}."]
    place = place_line(spec)
    if place:
        lines.append(f"PLACE: {place}")
    who = protagonist_line(spec)
    if who:
        lines.append(f"PLAYER CHARACTER: {who}")
    return "\n".join(lines)


def world_anchor(
    default: str,
    spec: Optional[Dict[str, Any]] = None,
    include_character: bool = True,
    include_vantage: bool = True,
) -> str:
    """The style/vantage anchor for realtime and video prompts.

    ``default`` is the shipped anchor — a single paragraph of medium, era, and
    film-stock language with the first-person vantage written into it. Rather
    than replace it (which would throw away the look the game is built on),
    this retunes its perspective wording and appends whatever the level and
    camera blocks authored, so the anchor ends up describing the player's
    world instead of the one it shipped describing.

    Deliberately does NOT run :func:`reconcile`: that deletes whole *lines*,
    and an anchor is one line, so a single anti-person clause would delete the
    entire anchor.

    ``include_character`` / ``include_vantage`` are for surfaces whose subject
    or framing isn't the player — a conversation portrait is a deliberate
    register change to a locked-off medium shot of somebody else, and it still
    wants the level's era and palette.
    """
    spec = spec or get_spec()
    look = look_line()
    if not is_active(spec):
        if look:
            return default.rstrip(". ") + ". " + look
        return default
    parts = [retune(default, spec).strip().rstrip(". ")]
    if include_vantage:
        parts.append(vantage(spec))

    setting = spec[SETTING_KEY]
    if setting_authored(spec):
        for field in ("era", "palette"):
            if setting.get(field):
                parts.append(setting[field].rstrip(". "))
    cam = spec[CAMERA_KEY]
    if cam.get("lens") and include_vantage:
        parts.append(cam["lens"].rstrip(". "))
    notes = (cam.get("notes") or "").strip()
    if notes and include_vantage:
        parts.append(notes.rstrip(". "))
    if look:
        parts.append(look.rstrip(". "))
    if include_character and shows_character(spec) and character_enabled(spec):
        who = protagonist_line(spec).rstrip(". ")
        parts.append(f"The player character stays in frame: {who}")
    return ". ".join(p for p in parts if p) + "."


def motion_clause(spec: Optional[Dict[str, Any]] = None) -> str:
    """How to phrase "the player just did X" for the live world model.

    One clause, no trailing action: callers append the verb phrase. Shared by
    the server's :func:`engine.realtime_action_beat` and the browser's live
    re-steers, which is the point — the browser used to hardcode the
    first-person half of this and quietly argue with the camera on every
    movement between turns.
    """
    if shows_character(spec):
        return f"the camera follows as {display_name(spec)}"
    return "the view shifts as you"


def movement_clause(spec: Optional[Dict[str, Any]] = None) -> str:
    """The style note appended to a camera-motion re-steer."""
    cfg = mode_config(spec)
    if not cfg["shows_body"]:
        return "Smooth continuous first-person motion, the environment flowing past."
    who = display_name(spec)
    lock = cfg.get("follow_lock")
    if lock:
        return (
            f"Smooth continuous {cfg['phrase']} motion, {lock}. "
            f"{who} stays in frame as the environment flows past."
        )
    return (
        f"Smooth continuous {cfg['phrase']} motion, the camera travelling with "
        f"{who}, who stays in frame as the environment flows past."
    )


def scene_floor(spec: Optional[Dict[str, Any]] = None) -> str:
    """The neutral "we have no scene yet" prompt a live re-steer can build on."""
    cfg = mode_config(spec)
    if not cfg["shows_body"]:
        return "First-person cinematic view of the current scene."
    phrase = cfg["phrase"]
    who = display_name(spec)
    lock = cfg.get("follow_lock")
    if lock:
        return (
            f"{phrase[0].upper()}{phrase[1:]} cinematic view of the current scene, "
            f"{who} in frame. {lock}."
        )
    return (
        f"{phrase[0].upper()}{phrase[1:]} cinematic view of the current scene, "
        f"{who} in frame."
    )


def live_prefix(spec: Optional[Dict[str, Any]] = None) -> str:
    """One paragraph every live instance must hear: camera, level, and cast.

    LingBot and Helios have no create_world knobs for those. The prompt is the
    wire. Without this, an editor save updated stills and the next Play turn
    while the running video kept walking as a pair of eyes in the old place.
    """
    spec = spec or get_spec()
    parts: List[str] = [vantage(spec)]
    lens = (spec[CAMERA_KEY].get("lens") or "").strip()
    if lens:
        parts.append(lens.rstrip(". "))
    notes = (spec[CAMERA_KEY].get("notes") or "").strip()
    if notes:
        parts.append(notes.rstrip(". "))
    look = look_line()
    # Character look is a costume sheet. A viewfinder restage that hears it
    # will draw that person in front of the lens.
    if look and not is_viewfinder_spec(spec):
        parts.append(look.rstrip(". "))
    place = place_line(spec)
    if place:
        parts.append(place.rstrip(". "))
    shot = (spec[SETTING_KEY].get("opening_shot") or "").strip()
    # Opening shots are written for the authored follow-cam and often name
    # the hero walking into frame. A viewfinder restage must not hear that.
    if shot and not is_viewfinder_spec(spec):
        parts.append(shot.rstrip(". "))
    if is_viewfinder_spec(spec):
        parts.append(viewfinder_hero_ban())
    if shows_character(spec):
        who = protagonist_line(spec)
        if who:
            lock = mode_config(spec).get("follow_lock")
            if lock:
                parts.append(f"{who.rstrip('. ')} stays in frame. {lock}")
            else:
                parts.append(f"{who.rstrip('. ')} stays in frame")
    return ". ".join(p for p in parts if p) + "."


# The live world model only distinguishes first from third person — it has one
# `perspective` argument at world creation and no vocabulary for over-the-
# shoulder vs locked-off. Every mode that puts the body on screen therefore
# maps to "third_person"; the finer framing is carried by the prompt.
def world_model_perspective(spec: Optional[Dict[str, Any]] = None) -> str:
    return "third_person" if shows_character(spec) else "first_person"


def live_camera_contract(spec: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Everything the browser needs to keep the live world on the authored camera.

    The realtime renderer is not a passive display: it BUILDS the navigable
    world (``create_world`` takes its own ``perspective``) and re-steers it on
    every movement, nudge, and idle drift between turns. All of that ran on
    hardcoded first-person text and a per-browser localStorage toggle, so
    selecting a third-person camera in the editor changed every still frame and
    none of the live video — the one surface the player is actually looking at.

    Handed to the client as ready-made clauses rather than raw spec fields, so
    the phrasing has exactly one home.
    """
    spec = spec or get_spec()
    cfg = mode_config(spec)
    return {
        "mode": camera_mode(spec),
        "label": cfg["label"],
        "phrase": cfg["phrase"],
        # What create_world is given. First vs third is all it understands.
        "perspective": world_model_perspective(spec),
        "shows_character": shows_character(spec),
        "subject": display_name(spec) if shows_character(spec) else "",
        "vantage": vantage(spec),
        "motion_clause": motion_clause(spec),
        "movement_clause": movement_clause(spec),
        "scene_floor": scene_floor(spec),
        # Level + cast compressed for the live prompt. Same facts as
        # preview.compact, riding the camera payload so one apply() restages
        # the video when ANY of the sheet changes, not only the VIEW switch.
        "place_line": place_line(spec),
        "protagonist_line": protagonist_line(spec) if shows_character(spec) else "",
        "lens": (spec[CAMERA_KEY].get("lens") or "").strip(),
        "notes": (spec[CAMERA_KEY].get("notes") or "").strip(),
        # Viewfinder is the operator's eyes. Character look-lines name the
        # hero and will walk them into the live restage if they ride along.
        "look": "" if is_viewfinder_spec(spec) else look_line(),
        "prefix": live_prefix(spec),
        # How you DRIVE each camera. Empty means the browser uses its built-in
        # layout for that view; authored remaps travel with the world.
        "schemes": spec[CAMERA_KEY].get("schemes") or {},
    }


def structure_lines(spec: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """WHO / WHERE / ENVIRONMENT / TONE for the world-evolution template.

    The per-turn world rewrite is handed a section skeleton to fill in. While
    that skeleton said "ENVIRONMENT: Four Corners desert", every turn quietly
    dragged an authored level back toward the shipped one — the rewrite is
    authoritative for the next turn's prompts, so a few turns of that and the
    player's world was gone. Empty strings mean "no override", and the caller
    keeps its shipped wording.
    """
    spec = spec or get_spec()
    out = {"who": "", "where": "", "environment": "", "tone": ""}

    if character_enabled(spec):
        char = spec[CHARACTER_KEY]
        out["who"] = ", ".join(
            p for p in (display_name(spec), char.get("role"), char.get("demeanor")) if p
        )

    if setting_enabled(spec):
        setting = authored_setting(spec)
        name = setting.get("name") or "the level"
        out["where"] = f"Current position inside {name} (updated!)"
        out["environment"] = ", ".join(
            p for p in (name, setting.get("summary"), setting.get("landmarks")) if p
        )
        out["tone"] = ", ".join(
            p for p in (setting.get("era"), setting.get("palette")) if p
        )

    return out


def world_brief(base: str, spec: Optional[Dict[str, Any]] = None) -> str:
    """Seed the run's evolving world document with the cast sheet.

    Appended to ``world_initial_state`` at reset so the protagonist and level
    are part of the world state the whole run reasons from — not just a
    per-prompt garnish that the world-evolution pass would erase.

    A recast Level plate LEADS. The shipped Horizon bible is thousands of
    words about the fence; if it stays first, the image model redraws that
    yard no matter what the Level card says.
    """
    spec = spec or get_spec()
    directive = narrative_directive(spec)
    plate = setting_plate(spec)
    parts: List[str] = []
    if not is_shipped_setting(spec):
        if plate:
            parts.append(plate)
            parts.append(
                "THE PLACE above is the current level. Any later description of a "
                "different biome, facility, or region is background lore, not the "
                "location of this run."
            )
        elif setting_reference_paths(spec):
            parts.append(
                "🗺️ LEVEL PLATE — THE PLACE THIS RUN HAPPENS IN\n"
                "A location plate is attached. That plate is the current level. "
                "Any later description of a different biome, facility, or region "
                "is background lore, not the location of this run."
            )
    if base:
        parts.append(base)
    if directive:
        parts.append(directive)
    return "\n\n".join(p for p in parts if p)


def opening_shot(spec: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, str]]:
    """The authored first frame.

    Setting copy wins when it exists. A named character with the body in
    frame is enough on its own — otherwise changing Character on a World
    whose Level sheet is off (SOMEWHERE) never reached the opening still.
    Returns None only when neither sheet has anything to draw, so Play
    can fall back to the shipped Horizon openers.
    """
    spec = spec or get_spec()
    char_on = character_enabled(spec) and shows_character(spec)
    setting = authored_setting(spec)
    shot = (setting.get("opening_shot") or "").strip()
    summary = (setting.get("summary") or "").strip()
    # Written Level copy is the scene, even if the toggle was left off.
    # SOMEWHERE ships Level-off; typing an opening shot still has to
    # reach the first frame and the live video.
    setting_on = setting_enabled(spec) or setting_authored(spec)
    place = (setting.get("name") or "").strip() or "the location"
    if not setting_on and not char_on:
        return None
    if setting_on and not (shot or summary) and not char_on and not setting_authored(spec):
        return None

    who_bits: List[str] = []
    place_bits: List[str] = []
    if shot or summary:
        lead = (shot or summary).rstrip()
        place_bits.append(lead if lead.endswith((".", "!", "?")) else lead + ".")
    if setting.get("landmarks"):
        place_bits.append(f"Visible landmarks: {setting['landmarks']}.")
    if setting.get("palette"):
        place_bits.append(f"Light and palette: {setting['palette']}.")
    if setting.get("era") and not (shot or summary):
        place_bits.append(f"{setting['era'].rstrip('.')}.")

    if char_on:
        who = display_name(spec)
        char = authored_character(spec)
        look = ", ".join(
            p for p in (char.get("role"), char.get("appearance"), char.get("wardrobe")) if p
        )
        cfg = mode_config(spec)
        # How the body sits in the frame, and what it is DOING. This is the
        # most load-bearing sentence in the game: it draws the World plate,
        # which is the opening montage's reference and the frame turn one
        # continues from, so whatever it describes propagates through the run.
        #
        # It used to read "is in frame, seen by the camera ... standing in
        # <place>", and both halves were wrong. "Seen by the camera" describes
        # the exact shot the follow-cam rig bans two paragraphs earlier — "no
        # walking-toward-camera arrival, no front-facing portrait" — and it won,
        # because the CAMERA block is rules in capitals while this is concrete
        # prose about the subject, and concrete wins (see the precedence note in
        # engine.build_image_prompt). Every follow-cam world opened on the
        # protagonist strolling at the lens. "Standing", meanwhile, is why the
        # opening had no charge: the first frame of a horror game was a man
        # stood still, facing front, waiting to be looked at.
        if cfg.get("follow_lock"):
            vantage = (
                f"{who} is seen from BEHIND — the back of the head and the "
                f"shoulders toward the lens, facing INTO the place, which opens "
                f"away from them into the depth of the frame"
            )
            stance = (
                "Caught mid-stride with the weight already thrown forward, one "
                "step further in — arrested motion, not a pose. Put them off "
                "centre on a third, near enough the lens to read, with the "
                "place running away past them and somewhere to walk to."
            )
        else:
            # Fixed cinematic: a locked-off angle the character walks into.
            # "From behind" would fight the rig, so this states the framing the
            # mode actually wants while still refusing the face-on portrait.
            vantage = (
                f"{who} is somewhere inside the composed frame, small against "
                f"the architecture and not looking at the lens"
            )
            stance = (
                "Caught mid-movement rather than posed, dwarfed by the space "
                "around them, the angle doing the drama."
            )
        if setting_on:
            prologue = f"{who} arrives at {place}."
            body = f"{vantage}, stepping into the space"
        else:
            prologue = f"{who} is here."
            body = vantage
        if look:
            body += f" — {look}"
        who_bits.append(body.rstrip(". ") + ".")
        who_bits.append(stance)
    else:
        prologue = f"You arrive at {place}."

    # WHO leads. The shipped SOMEWHERE one-liner ("1993. The fence. The
    # shipped demo.") used to open the vision string, so the image model
    # redrew the default opening and treated the new look as garnish.
    vision_bits = who_bits + place_bits
    if not vision_bits:
        return None
    return {"prologue": prologue, "vision": " ".join(vision_bits)}


def level_goal(spec: Optional[Dict[str, Any]] = None, fallback: bool = True) -> str:
    """The thing the player came to this level to reach.

    ``fallback`` walks back to the landmarks and then the summary, because the
    opening montage always needs SOMETHING to put on the horizon: a level whose
    Goal was never filled in still has to establish toward something, and the
    landmark list is the next most concrete thing on the sheet. Pass
    ``fallback=False`` to ask the narrower question of whether a goal was
    actually authored.
    """
    setting = authored_setting(spec)
    goal = (setting.get("goal") or "").strip()
    if goal or not fallback:
        return goal
    landmarks = (setting.get("landmarks") or "").strip()
    if landmarks:
        # Landmarks are written as a list of the whole space. Only the first one
        # is a destination; the rest are the geography either side of the walk.
        return re.split(r"[.,;]", landmarks)[0].strip()
    return (setting.get("summary") or "").strip()


def establishing_shot(spec: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """The level's opening montage — arriving, with the goal still out of reach.

    Sibling to :func:`opening_shot`, and deliberately not a replacement for it.
    ``opening_shot`` describes the frame the level has ARRIVED at: it is what
    the editor renders as the World's cached plate, and that plate is the look
    reference this montage is drawn from. This describes the shots BEFORE it —
    the approach, with the goal a shape in the distance nobody has reached yet.

    Never returns None. An unauthored level still opens on a montage; it just
    establishes the place generically instead of naming it.
    """
    spec = spec or get_spec()
    setting = authored_setting(spec)
    place = (setting.get("name") or "").strip()
    goal = level_goal(spec)

    approach = (
        f"{display_name(spec)} is arriving on foot, small in frame, "
        f"seen from outside — not yet inside the space"
        if shows_character(spec)
        else "The approach is on foot, from outside the space — the way in is "
             "ahead and has not been taken yet"
    )

    anchors: List[str] = []
    if setting.get("landmarks"):
        anchors.append(f"Visible landmarks: {setting['landmarks'].rstrip('. ')}.")
    if setting.get("palette"):
        anchors.append(f"Light and palette: {setting['palette'].rstrip('. ')}.")
    if setting.get("era"):
        anchors.append(f"{setting['era'].rstrip('. ')}.")

    return {
        "title": place or "SOMEWHERE",
        "goal": goal,
        "approach": approach + ".",
        "place": " ".join(anchors),
        "prologue": (f"You are almost at {place}." if place
                     else "You are almost there."),
    }


# The goal is the one Level field with no visual answer — vision can read a
# plate and tell you what a place looks like, but not what you came there for.
# That has to come from the fiction, so this drafts from the Experience bible
# instead of from an image (see infer_fields_from_image for the plate path).
GOAL_DRAFT_MAX_TOKENS = 160


def draft_level_goal(lore: str = "", world_prompt: str = "",
                     spec: Optional[Dict[str, Any]] = None) -> str:
    """Draft "what you're here for" from the lore. Does not persist.

    Returns "" when there is nothing on the sheet to go on. A provider error
    raises, same as the plate-driven fills above, so the editor can say the
    draft failed instead of leaving the author staring at a field that silently
    refused to fill.
    """
    import ai_provider_manager

    setting = authored_setting(spec)
    known = " ".join(p for p in (
        f"Place: {setting['name']}." if setting.get("name") else "",
        f"What it is: {setting['summary']}." if setting.get("summary") else "",
        f"Landmarks: {setting['landmarks']}." if setting.get("landmarks") else "",
        (lore or "").strip()[:1400],
        (world_prompt or "").strip()[:600],
    ) if p).strip()
    if not known:
        return ""

    raw = ai_provider_manager.chat(
        [{"role": "user", "content": (
            "You are writing one line of a game level's design sheet.\n\n"
            f"{known}\n\n"
            "Write WHAT THE PLAYER CAME HERE TO REACH: the one thing this "
            "level is about getting to.\n\n"
            "IT MUST BE BIG AND IT MUST BE FAR AWAY. A tower, a rig, a dam, a "
            "dish, a stack, a hull, a wall, a wound in the ground — a single "
            "massive structure or landmark that can be SEEN FROM MILES OFF and "
            "that dominates the horizon when it is in frame. The opening shots "
            "put it in the distance, and every later frame can put it there "
            "again, so it has to be the thing the eye goes to across open "
            "ground.\n\n"
            "Not a door, a room, a vehicle, a body, a crate, a sign or a piece "
            "of equipment — those are too small to see from far away and the "
            "player would be standing at them already. Not a person and not an "
            "event. One object, on the skyline.\n\n"
            "Name it and say in the same breath why it matters.\n\n"
            "One sentence, under 25 words. No second person, no verbs of "
            "instruction ('go to', 'find'), no markdown, no quotes. Reply "
            "with the sentence and nothing else."
        )}],
        temperature=0.8,
        max_tokens=GOAL_DRAFT_MAX_TOKENS,
    )
    return _clean_text(raw)


def opening_narration(spec: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """The first paragraph the player reads, written from the cast sheet.

    Returns None when nothing has been authored, so the shipped 1993 Four
    Corners opener stands.
    """
    spec = spec or get_spec()
    if not (character_enabled(spec) or setting_authored(spec)):
        return None
    char = authored_character(spec)
    setting = authored_setting(spec)

    sentences: List[str] = []
    if setting.get("era"):
        sentences.append(f"{setting['era'].rstrip('.')}.")
    if character_enabled(spec):
        who = display_name(spec)
        role = char.get("role")
        sentences.append(f"You are {who}{', ' + role if role else ''}.")
        if char.get("backstory"):
            sentences.append(char["backstory"].rstrip(".") + ".")
    if setting_authored(spec) and (setting.get("name") or setting.get("summary")):
        place = setting.get("name") or "this place"
        summary = setting.get("summary", "").rstrip(".")
        sentences.append(f"Ahead of you: {place}{' — ' + summary if summary else ''}.")
    if char.get("signature_gear"):
        sentences.append(f"You carry {char['signature_gear']}.")

    return " ".join(s for s in sentences if s) or None


def intro_place_state(spec: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """situation / location / environment_type for a fresh run.

    Reset used to hardcode the restricted-zone fence and ``desert_edge``
    even after the Level card named somewhere else.
    """
    spec = spec or get_spec()
    authored_open = opening_shot(spec)
    place = place_summary(spec) or place_line(spec)
    recast_place = setting_authored(spec) and not is_shipped_setting(spec)
    setting_name = (authored_setting(spec).get("name") or "").strip()
    situation = (authored_open or {}).get("prologue") or (
        f"You arrive at {place}" if recast_place and place else
        "You stand at the edge of the restricted zone, camera in hand."
    )
    if recast_place:
        location = setting_name or "authored_place"
        environment_type = "the authored location"
    else:
        location = "desert_edge"
        environment_type = "desert"
    return {
        "situation": situation,
        "location": location,
        "environment_type": environment_type,
    }


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


def recast_stored_prompts(
    old_name: str = "", spec: Optional[Dict[str, Any]] = None
) -> int:
    """Rewrite persisted prompt strings so the world bible matches the sheet.

    ``recast`` only ran at apply-time, so the editor's Character fields
    could say Wren while ``world_initial_state`` still named Jason. A name
    change now walks the live prompt file. Appearance-only edits leave
    the bible alone — CAST still compiles on the next turn.
    """
    spec = spec or get_spec()
    new_name = character_name(spec)
    if not new_name or not character_enabled(spec):
        return 0
    old = str(old_name or "").strip()
    if old.lower() == new_name.lower():
        return 0
    changed: Dict[str, Any] = {}
    for key, val in dict(PROMPTS).items():
        if key in SPEC_KEYS or not isinstance(val, str):
            continue
        out = recast(val, spec)
        if old and old.lower() not in ("jason", "jason fleece"):
            out = re.sub(rf"\b{re.escape(old)}\b", new_name, out)
        if out != val:
            changed[key] = out
    if changed:
        prompts_store.save_prompts_bulk(changed)
    return len(changed)


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
    """Strip lines that forbid exactly what the active mode requires.

    Line-granular by design — the rules being removed live in editable JSON
    that can't safely be rewritten word by word. The consequence is that a
    caller who hands this a prompt written as ONE long line can have the whole
    thing deleted by a single offending clause, which is a silent, total
    failure (an empty image prompt renders whatever the model feels like). So
    a reconcile that removes everything is treated as a caller mistake and
    declines to apply: better a prompt with one stale anti-person line in it,
    which the directive above it outranks, than no prompt at all.
    """
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
    if not any(ln.strip() for ln in kept):
        return text
    # Deleting a body line can leave its ALL-CAPS section label standing alone.
    # `image_camera_rules`' CONTINUITY paragraph is one line and mentions "the
    # subject's POV", so third person used to receive a bare "CONTINUITY" with
    # nothing under it — a heading that names a concern the payload then says
    # nothing about, in a payload whose whole problem is too many headings.
    out: List[str] = []
    for i, line in enumerate(kept):
        if _is_art_heading(line.strip()):
            nxt = next((l.strip() for l in kept[i + 1:] if l.strip()), "")
            if not nxt or _is_art_heading(nxt):
                continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


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

    # Reconcile BEFORE retune, not after. Reconcile's job is to judge what the
    # AUTHOR wrote — retune's job is to rewrite it. Run the other way round,
    # retune would rewrite "first-person" to "third-person" in place and then
    # reconcile would delete that whole line for containing a third-person
    # framing term next to a prohibition, which is its signature for "a rule
    # forbidding what the active mode requires". Stage 2 handed stage 3 the
    # evidence to destroy rules that were never about perspective at all.
    #
    # That is how LEAVE CAMP lost its entire no-vehicle-cabin constraint — a
    # line about not putting the camera in a truck, deleted for mentioning a
    # vantage — but only in third person, and only silently.
    body = retune(reconcile(text, spec), spec)

    if surface == "image":
        head = image_directive(spec)
    elif surface == "narrative":
        head = narrative_directive(spec)
    else:
        head = ""

    if not head:
        return body
    return f"{head}\n\n{body}" if body else head


def block_preview(spec: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, str]]:
    """The compiled text each editor CARD is individually responsible for.

    The editors used to show one shared blob per block: the director's sheet
    for the character and level cards, the image directive for the camera card.
    That made half the fields look broken — appearance, wardrobe, era, palette
    and the landmark list are compiled into the IMAGE blocks and appear nowhere
    in the director's sheet, so typing into them changed nothing on screen and
    there was no way to tell whether they had reached anything.
    """
    spec = spec or get_spec()
    return {
        CHARACTER_KEY: {
            "image": (
                character_visual_sheet(spec) if shows_character(spec)
                else character_visual_sheet(spec, hands_only=True) if hands_visible(spec)
                else ""
            ),
            "narrative": narrative_directive(spec),
        },
        SETTING_KEY: {
            "image": setting_plate(spec),
            "narrative": narrative_directive(spec),
        },
        CAMERA_KEY: {
            "image": camera_directive(spec),
            "negative": negative_prompt(spec=spec),
        },
    }


def wiring_notes(spec: Optional[Dict[str, Any]] = None) -> Dict[str, List[str]]:
    """Per-block warnings about fields that are set but can't reach anything.

    The cast sheet has real internal dependencies — a character's appearance is
    only ever sent to the image model when the camera can actually see them,
    and a block that is switched off compiles to nothing at all. Both are
    invisible from the form, and both read as "the editor is inconsistent".
    """
    spec = spec or get_spec()
    notes: Dict[str, List[str]] = {
        CHARACTER_KEY: [], SETTING_KEY: [], CAMERA_KEY: [],
    }
    char = spec[CHARACTER_KEY]
    setting = spec[SETTING_KEY]

    if char.get("enabled") and not character_enabled(spec):
        notes[CHARACTER_KEY].append(
            "This character is switched on but every field is blank, so nothing is sent. "
            "Fill in at least a name or a role."
        )
    if character_enabled(spec) and not (shows_character(spec) or hands_visible(spec)):
        notes[CHARACTER_KEY].append(
            "The camera is first person with hands hidden, so it never sees your character: "
            "appearance, wardrobe, gear, and any character reference plate are not sent to the "
            "image model. Name, role, temperament, and backstory still steer the writing."
        )
    if char.get("reference_images") and not (shows_character(spec) or hands_visible(spec)):
        notes[CHARACTER_KEY].append(
            "Your character reference plate is not being attached for the same reason."
        )
    # A sheet can name a plate whose file is gone — swept with an old session, or
    # carried over when the sheet was edited from one protagonist to another. The
    # image call silently attaches nothing and the look then drifts frame to
    # frame, which is a hard symptom to trace back to a missing file.
    _dead = [str(i) for i in (char.get("reference_images") or [])
             if not reference_path(str(i))]
    if _dead:
        notes[CHARACTER_KEY].append(
            f"{len(_dead)} character reference plate(s) are missing from disk "
            f"({', '.join(_dead)}), so nothing is locking your character's look "
            f"and it will drift between frames. Re-upload the plate, or remove it "
            f"and let the written appearance do the work."
        )
    _dead_setting = [str(i) for i in (setting.get("reference_images") or [])
                     if not reference_path(str(i))]
    if _dead_setting:
        notes[SETTING_KEY].append(
            f"{len(_dead_setting)} level plate(s) are missing from disk "
            f"({', '.join(_dead_setting)}) and are not being attached."
        )

    if not char.get("enabled") and any(char.get(f) for f in _INTENT_FIELDS):
        notes[CHARACTER_KEY].append(
            "Switched off, so none of this is being used. Turn it back on to play as them."
        )

    if character_enabled(spec) and not is_shipped_cast(spec):
        # Hidden fields the minimal editor does not show can contradict the
        # Look it does. Two outfits for one person is drawn as two people.
        _look = _norm_field(char.get("appearance"))
        _fit = _norm_field(char.get("wardrobe"))
        if _look and _fit and _fit not in _look and _look not in _fit:
            notes[CHARACTER_KEY].append(
                "Wardrobe (under Advanced) describes different clothes from Look, and both "
                "are sent. Clear one of them, or the image model picks between them frame "
                "to frame."
            )

    if setting.get("enabled") and not setting_enabled(spec):
        notes[SETTING_KEY].append(
            "This level is switched on but every field is blank, so nothing is sent. "
            "Fill in at least a name or a description."
        )
    if not setting.get("enabled") and setting_authored(spec):
        notes[SETTING_KEY].append(
            "The Level switch is off, but the words you wrote are still compiled "
            "into the live scene. Flip the switch on if you want this to replace "
            "the shipped opening for Play."
        )
    if setting_enabled(spec) and not setting.get("opening_shot"):
        notes[SETTING_KEY].append(
            "No opening shot, so the first frame is derived from the description. "
            "Write one to control the literal first image of the run."
        )
    if not spec[CAMERA_KEY].get("show_hands") and shows_character(spec):
        notes[CAMERA_KEY].append(
            "Show-hands only applies in first person; this mode already shows your whole character."
        )
    return notes


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
        # What each editor card compiles to on its own, plus why a field might
        # legitimately be having no effect.
        "blocks": block_preview(spec),
        "notes": wiring_notes(spec),
        # The camera as the live world model and the browser's re-steers need
        # it. Carried in the preview so an editor save can push the new camera
        # straight into a running world instead of waiting for a reconnect.
        "camera": live_camera_contract(spec),
        # The short forms the non-image surfaces receive (live world model,
        # vision analysis, camp, talk). Shown so the editor can prove the cast
        # sheet reaches more than the still frames.
        "compact": {
            "scene_grounding": scene_grounding(spec),
            "place_line": place_line(spec),
            "protagonist_line": protagonist_line(spec),
            "vantage": vantage(spec),
        },
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


def live_reference_ids(block: Optional[Dict[str, Any]]) -> List[str]:
    """The block's reference ids whose files are actually on disk.

    An id is a promise, not a plate. `reference_path` has always returned None
    for a file that is gone, so the image call correctly attached nothing — but
    the functions that REASON about the sheet asked whether `reference_images`
    was non-empty, which is a different question and the wrong one.

    Found on 2026-09-17: the shipped Character sheet named
    `character_54a7f7d76882` and no such file existed anywhere in the repo. So
    `character_enabled` reported an "image-only character" with no image,
    `is_shipped_cast` reported a recast, and `drop_shipped_leftovers` was willing
    to blank name / role / appearance in the belief that a plate would supply
    them. Nothing would have. The visible symptom was wardrobe drifting frame to
    frame — the sheet says "olive field jacket", the run rendered a teal jumpsuit
    and then added red gloves — because the identity lock was text alone while
    the code believed it had a photograph.
    """
    return [str(i) for i in ((block or {}).get("reference_images") or [])
            if reference_path(str(i))]


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


def reference_annotation(
    paths: List[str],
    spec: Optional[Dict[str, Any]] = None,
) -> str:
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
        # "…and do NOT copy anyone standing in it." The plate on the active
        # level of the machine this was found on is a concept still with four
        # armed figures in it, and it rides as reference slot 1 on every frame
        # — so a second armoured figure kept walking into the corridor, SCAN
        # tagged it "character", MOVE TO drew the player's twin face to face
        # with him, and the encounter that followed was against a man in the
        # player's own suit. A plate is WHERE; the people in it are not cast.
        lines.append(
            "• One reference is a LOCATION PLATE — a photo of the place this run happens in. "
            "Copy its architecture, materials, palette, and mood. Do NOT copy its framing, "
            "and do NOT copy any person, figure or creature standing in it — they are not "
            "in this scene. The only people in this frame are the ones the scene names."
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


def reference_part_label(
    path: str,
    spec: Optional[Dict[str, Any]] = None,
) -> str:
    """A short label to sit next to one attached image in the Gemini parts list.

    Unlabeled attachments all read as "the previous frame". The character
    sheet then lost to whatever Jason still was sitting in slot one.
    """
    spec = spec or get_spec()
    path = str(path or "")
    char_paths = set(character_reference_paths(spec))
    if path in char_paths:
        who = display_name(spec)
        return (
            f"CHARACTER SHEET for {who} — copy this face, body, hair, and clothes. "
            "Do NOT copy this photo's background, pose, or framing. "
            "This is WHO to draw, not a previous game frame."
        )
    if path in set(setting_reference_paths(spec)):
        return (
            "LOCATION PLATE — copy architecture, materials, palette, and mood. "
            "Do NOT copy this photo's framing. This is WHERE, not a previous frame."
        )
    if is_viewfinder_spec(spec):
        return viewfinder_place_lock_label()
    return (
        "PREVIOUS FRAME — place, light, and materials only. "
        "If a CHARACTER SHEET is also attached, do NOT copy the person in this frame."
    )


def identity_seed_instruction(spec: Optional[Dict[str, Any]] = None) -> str:
    """Grammar for a first frame seeded from plates, not from a previous still."""
    spec = spec or get_spec()
    who = display_name(spec)
    place = place_summary(spec) or place_line(spec)
    bits = [
        "IDENTITY SEED — these attachments are reference plates, NOT a previous "
        "game frame. Compose a NEW opening shot from the scene text."
    ]
    if place:
        bits.append(
            f"If a location plate is attached, the place is {place} — copy its "
            "architecture, materials, and palette. Do not invent the shipped "
            "Horizon desert fence unless the Level sheet is that place. "
            "Do not copy the plate's framing."
        )
    bits.append(
        f"If a character sheet is attached, {who} in the new shot MUST be THAT "
        "person — same face, body, hair, and clothes. Do not invent the shipped "
        "default photojournalist. Do not copy the plate's background or pose."
    )
    return "\n".join(bits)


def keep_place_instruction(
    spec: Optional[Dict[str, Any]] = None,
    *,
    has_setting_plate: bool = False,
) -> str:
    """Where to copy when a Level plate or recast place is in play."""
    spec = spec or get_spec()
    if not setting_authored(spec):
        return ""
    place = place_line(spec)
    if has_setting_plate:
        return (
            f"\n\n🗺️ KEEP THIS PLACE:\n\n"
            f"A LOCATION PLATE is attached. That plate is WHERE this is"
            f"{' — ' + place if place else ''}. "
            "Copy architecture, materials, and palette from the LOCATION PLATE, "
            "NOT from any previous game frame. A previous frame may show the "
            "shipped desert fence or another World — ignore that place."
        )
    if not is_shipped_setting(spec):
        return (
            f"\n\n🗺️ KEEP THIS PLACE:\n\n"
            f"The level is {place} "
            "Do not replace it with the shipped Horizon desert fence."
        )
    return ""


def keep_character_instruction(
    spec: Optional[Dict[str, Any]] = None,
    *,
    has_character_plate: bool = False,
    extras_are_strangers: bool = False,
) -> str:
    """Who to copy when a body belongs in frame.

    Without a plate, "keep the person in the reference" copies whoever is in
    the previous still — which is how a recast kept drawing Jason. With a
    plate, that sheet is the person; a previous frame is only the place.

    ``extras_are_strangers`` is for two-shots: the sheet is ONLY the player.
    Other people must not inherit that face or outfit (PRESS vest clones).
    """
    spec = spec or get_spec()
    who = display_name(spec)
    if has_character_plate:
        body = (
            f"\n\n🕹️ KEEP {who} IN FRAME:\n\n"
            f"A CHARACTER SHEET is attached. That sheet is who {who} is. "
            "Copy face, build, hair, and outfit from the CHARACTER SHEET, "
            "NOT from any previous game frame. A previous frame may show a "
            f"different person — ignore that person. Draw {who}. "
            "Do not erase them. Do not render an empty environment plate."
        )
    else:
        body = (
            f"\n\n🕹️ KEEP {who} IN FRAME:\n\n"
            f"The player character is {who}. Carry them as the SAME person — "
            "same face, build, hair, and outfit — re-posed to match the action. "
            "Do not erase them. Do not swap them for a different person. "
            "Do not render an empty environment plate."
        )
    if extras_are_strangers:
        body += (
            f"\nONLY {who} copies that sheet. Any other person is a stranger — "
            "different face, hair, clothes, and body. Do not clone the player. "
            "Do not dress anyone else in the player's vest, cap, or badge."
        )
    return body
