"""
prompt_layers.py — the design surface, filed the way a game developer thinks.

Everything the simulation reads is editable, which is exactly what made it hard
to design with: the prompt surface was grouped by which subsystem consumed it
(world / narrative / image), so authoring a level meant knowing that geography
lives in `world_initial_state` unless `setting_reference` is on, that tone is
prose buried in the same field, and that the look is somewhere else entirely.

This module re-files the same keys into the four layers of a video game, ordered
by how often you touch them and how much a mistake costs:

    ENGINE     How does a turn compute?        Almost never edit.
               Output contracts, JSON schemas, mechanical rulebooks. Breaking
               one stops the simulation working at all.

    GAME       What kind of game is this?      Once per product.
               Genre, tone, threat model, the look, the live world anchor.
               Applies to every level; changing it changes all of them.

    LEVEL      Where am I, what's here?        Every level.
               This place: its brief, geography, landmarks, opening shot.
               Scoped — changing it affects only this level.

    CHARACTER  Who am I?                       Per playthrough.
               The player character sheet and its reference plates.

Nothing here changes what any prompt SAYS or how it is consumed — it is a
taxonomy plus UI metadata over the existing keys. `prompts_store` remains the
source of truth for prompt bodies and `game_identity` for the spec blocks; this
module only answers "which layer does this belong to, and how should a designer
be told about it".

The completeness guarantee mirrors `prompts_store.unwired_keys()`: every
editable key belongs to exactly one layer, enforced by a test, so a new prompt
cannot be added without deciding where a designer would look for it.
"""

from typing import Any, Dict, List, Optional

import prompts_store

# ═══════════════════════════════════════════════════════════════════════════
# LAYERS
# ═══════════════════════════════════════════════════════════════════════════

ENGINE = "engine"
GAME = "game"
LEVEL = "level"
CHARACTER = "character"

LAYER_ORDER: List[str] = [ENGINE, GAME, LEVEL, CHARACTER]

# `risk` is the honest warning a designer needs before opening a layer, and it
# drives how the editor frames each one. "contract" means the value is parsed or
# formatted by code and a careless edit breaks a turn; "content" means it is
# prose the model reads, where the only cost of a bad edit is a bad scene.
RISK_CONTRACT = "contract"
RISK_CONTENT = "content"

LAYERS: List[Dict[str, Any]] = [
    {
        "id": ENGINE,
        "label": "Engine",
        "question": "How does a turn compute?",
        "tagline": "Output contracts and mechanical rulebooks.",
        "blurb": (
            "The machine itself: the JSON a turn must return, how the world "
            "rewrite is constrained, the image templates and camera physics. "
            "You can build an entire game without opening this layer — and a "
            "careless edit here stops turns resolving at all."
        ),
        "volatility": "Almost never edit",
        "risk": RISK_CONTRACT,
        "accent": "#7aa2ff",
        "icon": "\u2699",
        "scope": "Breaking it stops the simulation",
    },
    {
        "id": GAME,
        "label": "Game",
        "question": "What kind of game is this?",
        "tagline": "Genre, tone, threat, and the look — every level inherits it.",
        "blurb": (
            "Your product's identity. Set once and every level is tinted by it: "
            "what genre this is, how it treats the player, what the camera is, "
            "what the world looks like on film, and the anchor the live world "
            "model steers from."
        ),
        "volatility": "Once per product",
        "risk": RISK_CONTENT,
        "accent": "#f0b354",
        "icon": "\u25C8",
        "scope": "Changing it changes every level",
    },
    {
        "id": LEVEL,
        "label": "Level",
        "question": "Where am I, and what's here?",
        "tagline": "One specific place: brief, landmarks, opening shot.",
        "blurb": (
            "The layer you'll live in. A level is a place with its own brief, "
            "geography, landmarks and opening shot. Save as many as you like and "
            "switch between them — the engine, the game and your character stay "
            "exactly as they were."
        ),
        "volatility": "Every level",
        "risk": RISK_CONTENT,
        "accent": "#a78bfa",
        "icon": "\u25F0",
        "scope": "Scoped to this level only",
    },
    {
        "id": CHARACTER,
        "label": "Character",
        "question": "Who am I?",
        "tagline": "The player character and their reference plates.",
        "blurb": (
            "Who the player is: name, role, how they look, what they carry. "
            "Reference plates keep them consistent frame to frame. Independent "
            "of the level, so the same character can walk through any of them."
        ),
        "volatility": "Per playthrough",
        "risk": RISK_CONTENT,
        "accent": "#4ec9a5",
        "icon": "\u25CF",
        "scope": "Scoped to the cast",
    },
]

LAYERS_BY_ID: Dict[str, Dict[str, Any]] = {l["id"]: l for l in LAYERS}


# ═══════════════════════════════════════════════════════════════════════════
# KEY → LAYER
# ═══════════════════════════════════════════════════════════════════════════
#
# Assignment rationale for the choices that aren't obvious:
#
#   world_initial_state -> LEVEL, not GAME. It reads as "the world" but what it
#   actually contains is one place's geography and situation, and it is the seed
#   the world document grows from for THIS run. Filing it under Level is what
#   gives a designer somewhere to go; the cross-level identity that used to be
#   tangled up in it now has its own home in the Game layer's `game_design`.
#
#   camera_perspective -> GAME. First vs third person is a property of the
#   product, not of a room. Moving it per level would fight every image prompt.
#
#   image_art_direction / image_negative_prompt -> GAME. The look is the game's;
#   a level should be able to change the place without relighting the product.
#   (A level still tints the frame through its own palette + era fields.)
#
#   image_camera_rules -> ENGINE, deliberately split from art direction: it is
#   framing physics and no-text/no-border bans, i.e. a rulebook, not a look.

KEY_LAYERS: Dict[str, str] = {
    # ── ENGINE ─────────────────────────────────────────────────────────────
    "action_consequence_instructions": ENGINE,
    "player_choice_generation_instructions": ENGINE,
    "world_evolution_instructions": ENGINE,
    "situation_summary_instructions": ENGINE,
    "field_notes_format": ENGINE,
    "image_camera_rules": ENGINE,
    "gemini_text_to_image_instructions": ENGINE,
    "gemini_image_to_image_instructions": ENGINE,
    "gemini_flipbook_4panel_prefix": ENGINE,
    # ── GAME ───────────────────────────────────────────────────────────────
    "game_design": GAME,
    "image_art_direction": GAME,
    "image_negative_prompt": GAME,
    "camera_perspective": GAME,
    # ── LEVEL ──────────────────────────────────────────────────────────────
    "world_initial_state": LEVEL,
    "setting_reference": LEVEL,
    # ── CHARACTER ──────────────────────────────────────────────────────────
    "player_character": CHARACTER,
}

# The keys a LEVEL owns. Saving or loading a level touches exactly these, which
# is what makes levels swappable without disturbing the rest of the design.
LEVEL_KEYS: List[str] = [k for k, v in KEY_LAYERS.items() if v == LEVEL]


def layer_of(key: str) -> Optional[str]:
    """Which layer a prompt key belongs to, or None if it isn't assigned."""
    return KEY_LAYERS.get(key)


def keys_in(layer: str) -> List[str]:
    """Every assigned key in a layer, in LAYERS-declaration order."""
    return [k for k, v in KEY_LAYERS.items() if v == layer]


def unassigned_keys(data: Optional[Dict[str, Any]] = None) -> List[str]:
    """Editable prompt keys that no layer claims.

    A key nobody has filed is a key a designer cannot find. This is the same
    guarantee `prompts_store.unwired_keys()` gives for reachability, applied to
    discoverability, and it is test-enforced so adding a prompt forces a
    decision about where someone would go looking for it.
    """
    return [k for k in prompts_store.editable_keys(data) if k not in KEY_LAYERS]


def layer_manifest() -> List[Dict[str, Any]]:
    """The layers with their fields resolved — what the editors render from.

    Each layer carries its prompt fields (from PROMPT_SCHEMA) and its spec
    blocks (structured forms owned by game_identity), already ordered, so a
    client can lay out the whole design surface without knowing the taxonomy.
    """
    spec_blocks = set(prompts_store.SPEC_BLOCK_KEYS)
    out: List[Dict[str, Any]] = []
    for layer in LAYERS:
        fields: List[Dict[str, Any]] = []
        blocks: List[str] = []
        for key in keys_in(layer["id"]):
            if key in spec_blocks:
                blocks.append(key)
            elif key in prompts_store.PROMPT_SCHEMA_BY_ID:
                fields.append(prompts_store.PROMPT_SCHEMA_BY_ID[key])
        entry = dict(layer)
        entry["fields"] = [f["id"] for f in fields]
        entry["spec_blocks"] = blocks
        entry["primary_fields"] = [
            f["id"] for f in fields
            if f.get("tier", prompts_store.TIER_PRIMARY) == prompts_store.TIER_PRIMARY
        ]
        out.append(entry)
    return out
