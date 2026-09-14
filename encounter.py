"""Encounter Moments — generated character + danger interrupt.

A side pocket like Talk: invents a new person and a concrete danger for the
place the player is standing, restages that place as a cinematic confrontation
plate, and offers three laned bodily actions. Resolving an encounter is
``POST /api/encounter/resolve``: a hard-cut play-out still of the verb (new
composition — style swatch / identity plates only, never the enter plate as
img2img init), a server-owned survive / escape / wounded / die outcome, then
the same ``_process_turn_background`` pipeline as MOVE TO / a typed
``/api/choose`` (``source="encounter"``). Survive / wounded stay locked on
that verb still. Escape generates a new world frame so the punch does not
become the walkable yard. Die keeps the death still. Do not invent a
parallel narrator.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Optional

ENCOUNTER_STANCES = ("hostile", "desperate", "opportunistic")
ENCOUNTER_KINDS = ("person", "creature", "character")
ENCOUNTER_LANES = ("confront", "evade", "use")
ENCOUNTER_OUTCOMES = ("survive", "escape", "wounded", "die")
ENCOUNTER_CONDITIONS = ("ok", "wounded")
# Where the other body is in the exchange. Without this the fight had no
# memory: every round was the same independent roll against the same standoff,
# so committing to a verb could not change the situation, only repeat it or
# end the run. "down" is the win the player previously had no way to reach —
# confront could only loop (50% survive, nothing changes) or kill you.
ENCOUNTER_ENEMY_STATES = ("ready", "staggered", "down")

ENCOUNTER_PLATE_STYLE_ANCHOR = os.getenv(
    "ENCOUNTER_PLATE_STYLE_ANCHOR",
    "stylish cinematic confrontation still, 35mm film, tense medium two-shot or "
    "over-shoulder, analog-horror 1993 muted palette, subtle grain, the same "
    "place restaged from a new lens — not a handheld camcorder, not a portrait",
)
# The beat where a verb LANDS is not the standoff, and it was being rendered
# with the standoff's anchor: "locked-off two-shot" is dialogue grammar, so a
# punch came back as two people standing apart in a wide. Fight coverage is
# close, low, and off-axis.
ENCOUNTER_ACTION_STYLE_ANCHOR = os.getenv(
    "ENCOUNTER_ACTION_STYLE_ANCHOR",
    "stylish cinematic fight still, 35mm film, tight and kinetic — low or "
    "canted angle, the action close to the lens, motion blur on the moving "
    "limb, analog-horror 1993 muted palette, subtle grain — NOT a locked-off "
    "wide, NOT a portrait, NOT two people standing apart",
)
# Shot size escalates with the exchange the way a fight scene cuts in.
ENCOUNTER_SHOT_LADDER = (
    "Medium shot, waist up, the gap between them already closed.",
    "Tighter now — over-the-shoulder, shoulders and arms filling the frame, "
    "the camera inside arm's reach.",
    "Tight and violent — hands, jaw, and eyes near the lens, the frame barely "
    "containing the struggle.",
)

# Travel clock: seconds of TRANSLATION (walk / strafe) until the next
# encounter is due. Looking around does not count — only distance travelled.
# The first budget of a run is shorter so the system introduces itself;
# later budgets stretch so encounters stay unpredictable, not metronomic.
ENCOUNTER_TRAVEL_FIRST_MIN = float(os.getenv("ENCOUNTER_TRAVEL_FIRST_MIN", "12"))
ENCOUNTER_TRAVEL_FIRST_MAX = float(os.getenv("ENCOUNTER_TRAVEL_FIRST_MAX", "22"))
ENCOUNTER_TRAVEL_MIN = float(os.getenv("ENCOUNTER_TRAVEL_MIN", "20"))
ENCOUNTER_TRAVEL_MAX = float(os.getenv("ENCOUNTER_TRAVEL_MAX", "40"))
ENCOUNTER_TRAVEL_DT_MAX = float(os.getenv("ENCOUNTER_TRAVEL_DT_MAX", "2.5"))
# How hard img2img may restage the live frame. High values invent a new room
# (outdoor walk → indoor garage). Keep this a nudge, not a rewrite.
ENCOUNTER_PLATE_STRENGTH = float(os.getenv("ENCOUNTER_PLATE_STRENGTH", "0.48"))
ENCOUNTER_PLATE_RETRY_STRENGTH = float(os.getenv("ENCOUNTER_PLATE_RETRY_STRENGTH", "0.58"))
# Resolve is a hard cut. The enter plate is lighting only (style swatch),
# never composition. Strength is unused on that path; kept for env overrides
# of any leftover img2img fallback.
ENCOUNTER_RESOLVE_STRENGTH = float(os.getenv("ENCOUNTER_RESOLVE_STRENGTH", "0.70"))

_CLOTHING_CLAUSE_RE = re.compile(
    r"^(wearing|dressed|clad in|sporting|in a tattered|in an?\s)",
    re.I,
)
_PERSON_NOUNS = (
    "woman", "man", "worker", "miner", "guard", "figure", "stranger",
    "person", "someone", "creature", "presence",
)

# A vision description narrates a PHOTOGRAPH, so it opens by naming the shot
# ("A first-person perspective shows a hand holding a two-way radio…"). The
# enemy's look is pasted straight into the plate prompt as an appearance, so
# that lead-in stops reading as description and starts reading as a camera
# instruction: Gemini drew the first-person hand it was told about, the
# standoff never happened, and the player became whoever owned that sleeve.
# Strip the framing off before any of it can describe a person.
_CAMERA_LEAD_RE = re.compile(
    r"^\W*(?:in\s+)?(?:a|an|the|this)?\s*"
    r"(?:extreme\s+|medium\s+|wide\s+|close[\s-]?up\s+|low\s+|high\s+)*"
    r"(?:first[\s-]person|third[\s-]person|second[\s-]person|pov|point[\s-]of[\s-]view|"
    r"over[\s-]the[\s-]shoulder|bird'?s[\s-]eye|aerial|overhead|establishing|"
    r"wide|close[\s-]?up|photograph|photo|picture|image|frame|shot|scene|view|"
    r"angle|perspective|composition|camera)\b[^,.;]*?"
    r"\b(?:shows?|showing|depicts?|depicting|captures?|capturing|features?|"
    r"featuring|presents?|of|is|are|we\s+see|you\s+see)\b\s*",
    re.I,
)
# Viewpoint talk anywhere in the clause, not just at the front.
_CAMERA_PHRASE_RE = re.compile(
    r"\b(?:from\s+)?(?:a|an|the)?\s*"
    r"(?:first[\s-]person|third[\s-]person|point[\s-]of[\s-]view|pov|"
    r"over[\s-]the[\s-]shoulder)\s*"
    r"(?:perspective|view|viewpoint|angle|shot|framing)?\b",
    re.I,
)


def strip_camera_language(text: str) -> str:
    """Drop shot/viewpoint narration so a look describes a PERSON, not a frame."""
    out = str(text or "").strip()
    if not out:
        return ""
    for _ in range(3):  # "A wide shot shows a POV of…" nests
        stripped = _CAMERA_LEAD_RE.sub("", out, count=1).strip()
        if stripped == out:
            break
        out = stripped
    out = _CAMERA_PHRASE_RE.sub(" ", out)
    out = re.sub(r"\s{2,}", " ", out).strip(" ,.;:-")
    return out


def look_is_camera_language(text: str) -> bool:
    """True when a 'look' is really a description of the shot, not of a body.

    `_first_person_noun` used to answer yes to "A first-person perspective",
    because a hyphen is a word boundary and `\\bperson\\b` matches inside it —
    so the very phrase that broke the frame also certified itself as a person.
    """
    raw = str(text or "").strip()
    if not raw:
        return True
    cleaned = strip_camera_language(raw)
    if cleaned == raw:
        # No framing talk in it at all. A brief's invented look is allowed to
        # be pure wardrobe ("an oversized yellow raincoat, hood pulled low")
        # with no person noun anywhere, so absence of one proves nothing here.
        return False
    if not cleaned:
        return True
    # Framing WAS stripped, so this started life as a sentence about a shot.
    # It only survives if what is left actually describes a body.
    return not _first_person_noun(cleaned)

ENCOUNTER_BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "character": {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "kind": {"type": "string"},
                "look": {"type": "string"},
                "stance": {"type": "string"},
            },
            "required": ["label", "look", "stance"],
        },
        "danger": {"type": "string"},
        "stakes": {"type": "string"},
        "place_hold": {"type": "string"},
    },
    "required": ["character", "danger", "stakes"],
}

# Place-neutral only. Never invent a pipe, flare, or room the frame does not
# show — those canned props are what made choices name things the plate omitted.
_FALLBACK_BRIEFS = (
    {
        "character": {
            "label": "A stranger",
            "kind": "person",
            "look": "a wary human figure already in this place, clothes matching the light",
            "stance": "hostile",
        },
        "danger": "they are already close enough to hurt you",
        "stakes": "If you hesitate you will not walk away clean.",
        "place_hold": "",
    },
    {
        "character": {
            "label": "A presence",
            "kind": "person",
            "look": "a figure at the edge of this ground, half in shadow",
            "stance": "desperate",
        },
        "danger": "they have already closed the distance across this ground",
        "stakes": "If you freeze they reach you first.",
        "place_hold": "",
    },
    {
        "character": {
            "label": "Someone here",
            "kind": "person",
            "look": "a human shape in this same light, already turned toward you",
            "stance": "opportunistic",
        },
        "danger": "they are between you and the way you came",
        "stakes": "If you lose the opening you lose the way out.",
        "place_hold": "",
    },
)

DEFAULT_CHOICE_OVERLAY = (
    "ENCOUNTER SLATE — this is a confrontation, not exploration.\n"
    "The ATTACHED IMAGE is the only source of truth. A person or creature "
    "is challenging the player IN FRAME. Every choice is about THEM.\n"
    "Do not write choices about the landscape, a fence, a ridge, a door, "
    "a hatch, a pipe, or walking to a landmark. Those are explore choices.\n"
    "Generate exactly 3 bodily verbs that fan:\n"
    "(1) confront THEM — shove, strike, grab, charge\n"
    "(2) evade THEM — dive, break away, slip past\n"
    "(3) turn the moment against THEM — their grip, their weapon, "
    "their momentum. Not the scenery.\n"
    "No Attack/Defend/Item. No observe/wait/photograph. 3-6 words."
)

DEFAULT_CHOICE_INSTRUCTIONS = (
    "This is a confrontation still. A person or creature is already in the "
    "player's face. Write exactly three short bodily actions about THAT "
    "figure — not the place.\n"
    "Return JSON only with keys confront, evade, use. Each value is 3-6 words.\n"
    "confront: shove / strike / grab / charge THEM.\n"
    "evade: dive / break away / slip past THEM.\n"
    "use: turn THEIR grip, weapon, or momentum against them.\n"
    "Name the figure if you can see them. Never name a fence, ridge, door, "
    "pipe, hatch, mesa, or shed unless you use it to hit THEM.\n"
    "No observe, wait, photograph, climb, or walk-to."
)

DEFAULT_BRIEF_INSTRUCTIONS = (
    "Invent ONE new encounter for the attached image of the place the player is in.\n"
    "Return JSON only. The character is a specific human or creature who could "
    "already be in THIS photograph — face, clothes, gender, stance, already "
    "close enough to hurt the player. label is a grounded reading of how they "
    "look (a wounded worker, a man in coveralls) — not a comic title, rank, "
    "or sci-fi class. No Sentinel, Warden, Knight, or robot. This world is "
    "1993 industrial horror: people and flesh, not energy weapons.\n"
    "look is the visible body: hair, clothes, wound, what they hold.\n"
    "The DANGER is what THIS FIGURE is doing to the player right now with "
    "their body. Do not invent a collapse, fire, grate, sparking weapon, or "
    "sealed exit unless it is already visible AND they are using it.\n"
    "If the image is outdoors, stay outdoors. Do not invent an interior.\n"
    "If the image is indoors, stay in that same room.\n"
    "Stakes are one second-person sentence about what THIS FIGURE does "
    "to the player if they hesitate. place_hold is 1-2 visual locks from "
    "the image so a restage cannot teleport (ground, sky, walls, a landmark).\n"
    "stance must be one of: hostile, desperate, opportunistic.\n"
    "kind must be one of: person, creature, character."
)

ENCOUNTER_CHOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "confront": {"type": "string"},
        "evade": {"type": "string"},
        "use": {"type": "string"},
    },
    "required": ["confront", "evade", "use"],
}

# Landscape / travel verbs that made encounter slates read as explore turns.
_PLACE_ONLY_MARKERS = (
    "fence", "ridge", "mesa", "slope", "dune", "canyon", "horizon",
    "shed", "yard", "corridor", "hallway", "hatch", "grate", "pipe",
    "doorway", "blast door", "chain-link", "climb the", "sprint toward",
    "sprint to", "vault over", "cross the", "descend the", "advance on",
    "walk to", "head for", "move to",
)


def _clip(text: Any, fallback: str, n: int) -> str:
    """Shorten to n chars without splitting a word.

    A raw slice put "A person in blue baseball cap and a tactical vest" on a
    choice button as "Break away from A person in blue b". Some of what this
    trims is player-facing copy, so it has to end on a word.
    """
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw:
        return fallback
    if len(raw) <= n:
        cut = raw
    else:
        cut = raw[:n]
        if raw[n] != " " and " " in cut:
            cut = cut[:cut.rfind(" ")]
    cut = cut.rstrip(" ,;:-")
    # Ending on a conjunction or preposition still reads as a broken string:
    # "A man in green combat jacket and" shipped to a choice button that way.
    # Upstream text arrives pre-truncated too, so this is not conditional on
    # having done the cutting here.
    while True:
        m = re.search(r"\s+(and|or|with|in|on|a|an|the|of|at|to|from|for)$", cut, re.I)
        if not m:
            break
        cut = cut[:m.start()].rstrip(" ,;:-")
    return cut or raw[:n]


def _is_clothing_clause_label(text: str) -> bool:
    """True when a nameplate is a gerund clothing phrase, not a person."""
    return bool(_CLOTHING_CLAUSE_RE.match(str(text or "").strip()))


def _first_person_noun(text: str) -> str:
    low = str(text or "").lower()
    # "first-person" / "third-person" are camera talk. A hyphen is a word
    # boundary, so \bperson\b matched inside them and a viewpoint phrase
    # passed every "does this describe a human?" check in the module.
    low = re.sub(r"\b(?:first|second|third)[\s-]person\b", " ", low)
    for noun in _PERSON_NOUNS:
        if re.search(rf"\b{re.escape(noun)}\b", low):
            return noun
    return ""


def resolve_plate_caption(label: str, verb: str, lane: str, outcome: str,
                          nonce: Optional[str] = None) -> str:
    """Unique filename stem per verb so resolves cannot overwrite each other."""
    nonce = nonce or f"{int(time.time() * 1000)}_{os.getpid()}"
    verb_bit = _clip(re.sub(r"[^a-zA-Z0-9]+", "_", verb or "verb").strip("_"),
                     "verb", 24)
    return "_".join((
        "encounter_resolve",
        verb_bit,
        _clip(lane or "lane", "lane", 10),
        _clip(outcome or "out", "out", 10),
        str(nonce),
    ))[:80]


def encounter_turn_skip_image(outcome: str) -> bool:
    """Escape needs a new world frame. Other outcomes keep the verb still."""
    return str(outcome or "").strip().lower() != "escape"


def stakes_after_verb(brief: dict, verb: str, lane: str, outcome: str) -> str:
    """Stakes name the verb that just landed, not the enter-brief sludge line."""
    label = _clip(((brief or {}).get("character") or {}).get("label"), "them", 24)
    if _is_clothing_clause_label(label):
        label = "them"
    verb_s = _clip(verb, "the move", 40).rstrip(".")
    out = str(outcome or "").strip().lower()
    if out == "escape":
        return f"You broke clear of {label}. Keep moving."
    if out == "wounded":
        return f"{verb_s} cost you — {label} is still in reach."
    if out == "die":
        return f"{label} has you."
    _ = lane
    return f"{verb_s} landed. {label} is still here."


def player_wardrobe_text() -> str:
    try:
        import game_identity
        char = game_identity.authored_character()
        return str((char or {}).get("wardrobe") or "").strip()
    except Exception:
        return ""


def player_wardrobe_tokens() -> set:
    """Clothes/face words that belong ONLY to the authored player."""
    blob = ""
    try:
        import game_identity
        char = game_identity.authored_character() or {}
        blob = " ".join(
            str(char.get(k) or "")
            for k in ("wardrobe", "appearance", "signature_gear", "name")
        )
    except Exception:
        blob = ""
    tokens = {
        w for w in re.split(r"[^a-z0-9]+", blob.lower())
        if len(w) > 3 and w not in _WARDROBE_STOP
    }
    if "vest" in tokens or "press" in tokens:
        tokens.update({"vest", "press", "highvis", "visibility"})
    return tokens


# Words that describe almost any person in this setting. An overlap on these
# alone does not mean a description is of the player.
_GENERIC_LOOK_WORDS = frozenset({
    "jacket", "coat", "shirt", "pants", "trousers", "boots", "shoes", "gloves",
    "hair", "face", "eyes", "hood", "hooded", "dark", "light", "heavy", "worn",
    "torn", "adult", "young", "older", "tall", "short", "thin", "build",
    "figure", "person", "human", "male", "female", "wearing", "carries",
    "carrying", "holding", "stands", "standing",
})


def _player_token_hits(text: str) -> set:
    """Player-specific words a description reuses, minus the generic ones.

    This used to be a literal set — {"vest", "press", "gaiter", "respirator",
    "camcorder", "alvarez"} — which is one particular protagonist's costume.
    Every other protagonist got no protection at all, so a plate description
    of the player ("the man on the left wears an olive field jacket", which is
    Jason Fleece's sheet verbatim) was read as the ENEMY and became the cast
    lock for the rest of the fight.
    """
    raw = str(text or "").strip().lower()
    if not raw:
        return set()
    # "not a vest" / "not PRESS gear" is a ban, not a costume.
    raw = re.sub(r"\bnot (?:a |an |the )?[a-z0-9-]+(?: vest| gear| cap)?", " ", raw)
    owned = player_wardrobe_tokens()
    if not owned:
        return set()
    words = {w for w in re.split(r"[^a-z0-9]+", raw) if w}
    return (words & owned) - _GENERIC_LOOK_WORDS


def look_clones_player(text: str) -> bool:
    """True when a description is wearing the player's outfit."""
    raw = str(text or "").strip().lower()
    if not raw:
        return False
    owned = player_wardrobe_tokens()
    if ("high-vis" in raw or "high vis" in raw or "high-visibility" in raw
            or "high visibility" in raw or "highvis" in raw):
        if "vest" in owned or "press" in owned or not owned:
            return True
    words = {w for w in re.split(r"[^a-z0-9]+", raw) if w}
    if words & owned & {"press", "gaiter", "respirator", "camcorder"}:
        return True
    if "vest" in words and ("press" in owned or "vest" in owned):
        return True
    # The protagonist's own name, or two of their distinctive features.
    try:
        import game_identity
        name_words = {
            w for w in re.split(r"[^a-z0-9]+", (game_identity.display_name() or "").lower())
            if len(w) > 3
        }
    except Exception:
        name_words = set()
    if words & name_words:
        return True
    return len(_player_token_hits(raw)) >= 2


def distinct_enemy_look(look: str) -> str:
    """Keep a stranger look that cannot be read as the player's vest.

    Also the last gate before a look reaches the image prompt, so it is where
    camera narration has to die: the plate prompt drops this string in as the
    other person's appearance, and a look that still says "a first-person
    perspective shows…" re-aims the whole frame instead of dressing anybody.
    """
    if look_is_camera_language(look):
        return _DEFAULT_STRANGER_LOOK
    raw = _clip(strip_camera_language(look), "", 160)
    if raw and not look_clones_player(raw):
        return raw
    return _DEFAULT_STRANGER_LOOK


def names_unseen_hazard(text: str, plate_seen: str = "") -> bool:
    """True when copy names a hazard the photograph does not show."""
    blob = str(text or "").lower()
    if not blob:
        return False
    seen = str(plate_seen or "").lower()
    for word in _INVENTED_DANGER:
        if word in blob and word not in seen:
            return True
    return False


def ground_danger_to_visible(brief: dict, plate_seen: str = "") -> dict:
    """If danger names a prop the plate omitted, the threat is the figure."""
    brief = dict(brief or {})
    danger = str(brief.get("danger") or "")
    seen = plate_seen or str(brief.get("plate_seen") or "")
    if not names_unseen_hazard(danger, seen):
        return brief
    look = _clip(((brief.get("character") or {}) if isinstance(
        brief.get("character"), dict) else {}).get("look"), "", 80)
    brief["danger"] = _clip(
        f"They are already in your face — {look}." if look
        else (seen or "They are already in your face."),
        "They are already in your face.",
        140,
    )
    return brief


def _brief_cast_rule() -> str:
    wardrobe = player_wardrobe_text()
    bits = [
        "The new character is someone the player has never met. "
        "Do not dress them in the player's clothes.\n"
        "danger is what THIS FIGURE is doing to the player with their BODY. "
        "Do not invent sludge, fire, collapse, oil, or a prop the attached "
        "photograph does not already show.\n",
    ]
    if wardrobe:
        bits.append(
            f"The player already wears: {wardrobe}. "
            "The new person wears something else — no vest, no PRESS, "
            "no high-vis.\n"
        )
    return "".join(bits)


def separate_cast(brief: dict) -> dict:
    """Stop the stranger from wearing the player's PRESS / high-vis vest."""
    brief = normalize_encounter_brief(brief)
    char = brief.setdefault("character", {})
    look = distinct_enemy_look(char.get("look") or "")
    char["look"] = look
    if look_clones_player(char.get("label") or ""):
        char["label"] = _grounded_label_from_look(look)
    if look_clones_player(char.get("locked_look") or ""):
        char["locked_look"] = look
    return ground_danger_to_visible(brief)


def player_cast_lock() -> str:
    """Hard identity sentence so img2img cannot recast the protagonist."""
    try:
        import game_identity
        who = game_identity.protagonist_line()
        name = game_identity.display_name()
        if not who:
            return ""
        wardrobe = player_wardrobe_text()
        exclusive = ""
        if wardrobe:
            exclusive = (
                f" ONLY {name} wears that wardrobe ({wardrobe}). "
                "The other person must not wear that vest, cap, or PRESS gear. "
                "Do not draw a second high-vis or PRESS vest."
            )
        return (
            f"CAST LOCK — HARD. The player is {who} "
            f"Draw {name} as that exact person: same gender, face, hair, "
            "build, and clothes. Do not recast them as a different sex or face. "
            "A previous frame that shows someone else is WRONG — ignore that person."
            f"{exclusive}"
        )
    except Exception:
        return ""


def encounter_identity_paths() -> list:
    """Character-sheet paths only. Setting plates would teleport the place."""
    try:
        import game_identity
        return [p for p in game_identity.character_reference_paths() if p and Path(p).exists()]
    except Exception:
        return []


def choice_addresses_threat(text: str, brief: Optional[dict] = None) -> bool:
    """True when a choice is about the figure, not the landscape."""
    blob = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if not blob:
        return False
    char = ((brief or {}).get("character") or {}) if isinstance(brief, dict) else {}
    label = str(char.get("label") or "").strip().lower()
    tokens = [
        w for w in re.split(r"[^a-z0-9]+", label)
        if len(w) > 2 and w not in ("the", "and", "for", "with")
    ]
    names_figure = bool(tokens and any(t in blob for t in tokens))
    names_pronoun = any(w in blob for w in (
        "them", "him", "her", "their", "the figure", "the stranger",
        "the creature", "the presence",
    ))
    body_verb = any(w in blob for w in (
        "shove", "tackle", "strike", "grab", "lunge", "charge",
        "dodge", "evade", "break away", "slip past", "grip", "momentum",
    ))
    place_only = any(m in blob for m in _PLACE_ONLY_MARKERS)
    if names_figure or names_pronoun:
        return True
    if body_verb and not place_only:
        return True
    return False


def prefer_threat_choices(structured: list, brief: dict) -> list:
    """Replace landscape-only bars with fallbacks that name the figure."""
    fallback = fallback_encounter_choices(brief)
    seen = str((brief or {}).get("plate_seen") or "")
    out = []
    used = set()
    for i, item in enumerate(structured or []):
        text = str((item or {}).get("text") or "").strip()
        lane = str((item or {}).get("lane") or "").strip().lower()
        if lane not in ENCOUNTER_LANES:
            lane = ENCOUNTER_LANES[i] if i < len(ENCOUNTER_LANES) else "confront"
        grounded = text and choice_addresses_threat(text, brief)
        if grounded and names_unseen_hazard(text, seen):
            grounded = False
        if grounded:
            out.append({"text": text, "lane": lane})
            used.add(lane)
        else:
            fb = next((f for f in fallback if f["lane"] == lane and f["lane"] not in used), None)
            if fb:
                out.append(dict(fb))
                used.add(fb["lane"])
    for fb in fallback:
        if len(out) >= 3:
            break
        if fb["lane"] not in used:
            out.append(dict(fb))
            used.add(fb["lane"])
    return out[:3]


def normalize_encounter_brief(raw: Any, place_hold: str = "") -> dict:
    """Coerce model output (dict or JSON string) into a stable encounter brief."""
    data = raw
    if isinstance(raw, str):
        blob = raw.strip()
        if blob.startswith("```"):
            blob = re.sub(r"^```(?:json)?\s*", "", blob)
            blob = re.sub(r"\s*```$", "", blob)
        try:
            data = json.loads(blob)
        except Exception:
            data = None
    if not isinstance(data, dict):
        return fallback_encounter_brief(place_hold)

    char_in = data.get("character") if isinstance(data.get("character"), dict) else {}
    stance = str(char_in.get("stance") or data.get("stance") or "hostile").strip().lower()
    if stance not in ENCOUNTER_STANCES:
        stance = "hostile"
    kind = str(char_in.get("kind") or data.get("kind") or "person").strip().lower()
    if kind not in ENCOUNTER_KINDS:
        kind = "person"
    label = _clip(char_in.get("label") or data.get("label"), "A stranger", 40)
    look = _clip(char_in.get("look") or char_in.get("appearance") or "", "a wary human figure", 160)
    if _is_clothing_clause_label(label):
        label = _grounded_label_from_look(look, data.get("plate_seen") or "")
    danger = _clip(data.get("danger"), "something in this place can hurt you now", 140)
    stakes = _clip(data.get("stakes"), "If you hesitate you will not walk away clean.", 140)
    hold = _clip(data.get("place_hold") or place_hold, "", 160)
    if stakes and not stakes.endswith((".", "!", "?")):
        stakes = stakes.rstrip() + "."
    locked_look = ""
    if isinstance(char_in, dict):
        locked_look = _clip(char_in.get("locked_look") or "", "", 160)
    out = {
        "character": {
            "label": label,
            "kind": kind,
            "look": look,
            "stance": stance,
        },
        "danger": danger,
        "stakes": stakes,
        "place_hold": hold,
    }
    if locked_look:
        out["character"]["locked_look"] = locked_look
    # Preserve where the exchange got to; normalize rebuilds from scratch and
    # would otherwise reset every round to a fresh standoff.
    enemy_state = str(data.get("enemy_state") or "ready").strip().lower()
    out["enemy_state"] = enemy_state if enemy_state in ENCOUNTER_ENEMY_STATES else "ready"
    try:
        out["round_no"] = max(1, int(data.get("round_no") or 1))
    except Exception:
        out["round_no"] = 1
    seen = _clip(data.get("plate_seen") or "", "", 220)
    if seen:
        out["plate_seen"] = seen
    last = _clip(data.get("last_dispatch") or "", "", 280)
    if last:
        out["last_dispatch"] = last
    return out


_PULP_RANKS = (
    "sentinel", "warden", "harbinger", "revenant", "overseer",
    "colossus", "paladin", "knight", "titan", "specter", "spectre",
    "wraith", "phantom", "oracle", "hierophant", "champion",
    "guardian", "enforcer", "executor", "automaton", "android",
)

_INVENTED_DANGER = (
    "sparking", "spark", "discharge", "plasma", "laser", "energy weapon",
    "unstable weapon", "collapsing", "floor grate", "grate beneath",
    "sludge", "oily black", "oil pit", "dissolving", "encroaching",
    "acid pool", "caustic pool", "melting the", "pier's edge",
)

# Clothes that belong to the authored player. The brief and the image model
# both love to put a second PRESS / high-vis vest on the stranger, then the
# resolve treats the vest-wearer as the one being attacked.
_WARDROBE_STOP = frozenset((
    "over", "with", "wearing", "around", "neck", "early", "thirties",
    "intense", "gaze", "hanging", "tactical", "white", "shirt", "dark",
    "brown", "hair", "adult", "short", "face", "work", "pants", "boots",
))
_DEFAULT_STRANGER_LOOK = "a weathered stranger in a torn work coat and knit cap"


def _strip_player_clauses(seen: str) -> str:
    """Drop the clauses of a plate description that describe the player.

    The encounter plate is deliberately a two-shot, and an over-the-shoulder
    two-shot describes the player FIRST ("A third-person view over the shoulder
    of a character wearing a blue baseball cap and a tactical vest with PRESS,
    aiming at an injured person..."). Scanning that for the first person noun
    therefore named the protagonist as the enemy, which is how a hostile got
    the nameplate "A person in blue baseball cap and a tact".
    """
    raw = str(seen or "").strip()
    if not raw:
        return ""
    owned = player_wardrobe_tokens()
    if not owned:
        return raw
    keep = []
    for clause in re.split(r"(?<=[,.;])\s+|\s+(?=\bbeside\b|\bfacing\b|\bwhile\b|\band beside\b)",
                           raw, flags=re.I):
        if look_clones_player(clause):
            continue
        keep.append(clause)
    # Everything described was the player, so this plate does not show the
    # stranger. Returning the original here is what let the protagonist get
    # promoted to hostile; the caller should fall back to the brief's own look.
    return " ".join(k for k in keep if k).strip()


def plate_stranger_look(plate_seen: str, fallback: str = "") -> str:
    """Describe the other person AS THE PHOTOGRAPH DREW THEM.

    The brief invents a look before any image exists, then the image model
    draws whatever it draws. Nothing reconciled the two, so the cast lock
    pinned a person who was never rendered: the brief said "oversized yellow
    raincoat", the plate drew "a man in a green combat jacket", the nameplate
    took the plate and the prose took the brief. Every later frame then got a
    prompt naming both, and drew a third stranger. The plate is what the
    player actually saw, so the plate wins.
    """
    stripped = _strip_player_clauses(plate_seen)
    best = ""
    for clause in re.split(r"(?<=[,.;])\s+", stripped):
        c = re.sub(
            r"^(?:and|but|while|with|facing|confronting|opposite|before|"
            r"across from|in front of)\s+",
            "", clause.strip(), flags=re.I,
        )
        c = re.sub(
            r"\s+\b(?:stands?|standing|stood|occupy|occupies|occupying|is|are|"
            r"was|were|sits?|sitting)\b.*$",
            "", c, flags=re.I,
        ).strip(" ,.;:")
        # "The man on the left" stops being true the moment the resolve cuts
        # to a new camera, and it is not a description of anybody.
        c = re.sub(
            r"\s+(?:on|to|in|at)\s+the\s+(?:left|right|near|far|back)\b"
            r"(?:\s+(?:side|of\s+(?:the\s+)?frame))?",
            "", c, flags=re.I,
        )
        c = re.sub(r"\s+in\s+the\s+(?:foreground|background|middle|centre|center)",
                   "", c, flags=re.I).strip(" ,.;:")
        if not c or look_clones_player(c) or not _first_person_noun(c):
            continue
        if re.search(r"\b(?:wearing|dressed|in|with)\b", c, re.I):
            best = c
            break
        best = best or c
    if not best:
        return distinct_enemy_look(fallback)
    return distinct_enemy_look(_clip(best, "", 160))


def adopt_plate_look(brief: dict, plate_seen: str) -> dict:
    """Repoint the brief's character at the rendered plate, then relabel."""
    char = brief.setdefault("character", {})
    look = plate_stranger_look(plate_seen, fallback=char.get("look") or "")
    if not look or look == _DEFAULT_STRANGER_LOOK:
        # The plate never showed a usable stranger. Keep the invented look
        # rather than locking onto the default nobody.
        if not char.get("locked_look") or look_clones_player(char.get("locked_look") or ""):
            char["locked_look"] = distinct_enemy_look(char.get("look") or "")
        return brief
    char["look"] = look
    char["locked_look"] = look
    grounded = _grounded_label_from_look(look, plate_seen)
    if grounded and not look_clones_player(grounded) and not _is_clothing_clause_label(grounded):
        char["label"] = grounded
    return brief


def _camera_shows_player() -> bool:
    """Whether the world's camera actually has the player's body in frame."""
    try:
        import game_identity
        return bool(game_identity.shows_character())
    except Exception:
        return True


def _grounded_label_from_look(look: str, plate_seen: str = "") -> str:
    """Name a person, not the first clothing clause of their description.

    'wearing a tattered high-visibility blue vest…' used to become the
    nameplate and the evade fallback ('Break away from Wearing a tattered').
    """
    look_s = str(look or "").strip()
    seen_s = _strip_player_clauses(plate_seen)
    noun = _first_person_noun(look_s) or _first_person_noun(seen_s)
    source = look_s if _first_person_noun(look_s) else (seen_s or look_s)
    if noun:
        article = "An" if noun[0] in "aeiou" else "A"
        cloth = ""
        m = re.search(r"\b(?:wearing|in)\s+(?:a |an |the )?([^,.]+)", source, re.I)
        if m:
            # Test the WHOLE clause. Clipping first hid the giveaway: "blue
            # baseball cap and a tactical vest with PRESS" trimmed to 28 chars
            # loses "vest", so the wardrobe-collision check passed a label that
            # was describing the player.
            cloth = "" if look_clones_player(m.group(1)) else _clip(m.group(1), "", 28)
            if _is_clothing_clause_label(cloth):
                cloth = ""
        if cloth:
            return _clip(f"{article} {noun} in {cloth}", "A stranger", 40)
        return f"{article} {noun}"
    first = _clip((source or look_s or seen_s).split(",")[0], "", 40)
    if _is_clothing_clause_label(first) or not first:
        return "A stranger"
    if first[0].islower():
        first = first[0].upper() + first[1:]
    return first


def align_brief_to_plate(brief: dict, vision: Optional[dict] = None) -> dict:
    """After the plate exists, chrome names what the photograph shows.

    The brief LLM can invent a pulp rank the image model is forbidden to
    draw. The still is what the player believes — relabel and rewrite
    danger when they do not appear in the plate.
    """
    brief = separate_cast(normalize_encounter_brief(brief))
    vis = vision if isinstance(vision, dict) else {}
    seen = _clip(vis.get("description") or brief.get("plate_seen") or "", "", 220)
    char = brief.setdefault("character", {})
    if seen:
        brief["plate_seen"] = seen
        # Lock the stranger to the person the plate DREW, never the whole
        # two-shot description (that copies the player's vest onto them).
        adopt_plate_look(brief, seen)
        char = brief.setdefault("character", {})
    if not seen:
        return ground_danger_to_visible(brief)
    label = str(char.get("label") or "")
    plow = seen.lower()
    pulp = any(r in label.lower() for r in _PULP_RANKS)
    pulp_in_plate = any(r in plow for r in _PULP_RANKS)
    tokens = [
        w for w in re.split(r"[^a-z0-9]+", label.lower())
        if len(w) > 2 and w not in ("the", "and", "for", "with", "stranded",
                                    "lost", "fallen", "lone", "wearing")
    ]
    named_in_plate = bool(tokens) and any(t in plow for t in tokens)
    if (look_clones_player(label) or _is_clothing_clause_label(label)
            or (pulp and not pulp_in_plate) or (tokens and not named_in_plate)):
        grounded = _grounded_label_from_look(char.get("look") or "", seen)
        if look_clones_player(grounded) or _is_clothing_clause_label(grounded):
            grounded = "A stranger"
        char["label"] = grounded
    return ground_danger_to_visible(brief, seen)


def is_outdoor(setting: str = "", text: str = "") -> bool:
    """True when the current frame is an exterior. Indoor wins if labeled."""
    setting_l = (setting or "").strip().lower()
    blob = f"{setting} {text}".lower()
    if setting_l.startswith("indoor") or setting_l.startswith("interior"):
        return False
    if setting_l.startswith("outdoor"):
        return True
    if "indoor" in blob and "outdoor" not in blob:
        return False
    return any(w in blob for w in (
        "desert", "mesa", "ridge", "horizon", "open sky", "fence",
        "canyon", "cliff", "dunes", "wasteland", "outdoor",
    ))


def brief_from_vision(vision: Optional[dict], place_hold: str = "") -> dict:
    """Synthesize a brief from what the frame actually shows — never a canned pipe."""
    vis = vision if isinstance(vision, dict) else {}
    desc = str(vis.get("description") or "")
    setting = str(vis.get("setting") or vis.get("setting_type") or "")
    spatial = str(vis.get("spatial") or vis.get("spatial_compass") or "")
    hold = place_hold or _clip(" ".join(x for x in (setting, spatial or desc[:100]) if x), "", 160)
    low = desc.lower()
    outdoor = is_outdoor(setting, f"{desc} {spatial}")
    threat = ("flesh", "organic", "mass", "growth", "creature", "beast",
              "tendril", "blood", "tumor")
    figure = ("figure", "person", "man", "woman", "miner", "guard",
              "silhouette", "stranger")
    # `desc` narrates the whole photograph from the camera's seat. Handing it
    # over whole as the stranger's appearance is what put "A first-person
    # perspective shows a hand holding a two-way radio" into the plate prompt
    # as a person, so strip the framing and keep only the clause that actually
    # describes somebody.
    body = strip_camera_language(desc)
    if any(w in low for w in threat):
        kind = "creature"
        label = "The growth" if any(w in low for w in ("mass", "flesh", "growth", "tumor")) \
            else "A creature"
        look = _clip(body, "something living in this place", 160)
        danger = "it is already reaching toward you from this place"
    elif any(w in low for w in figure):
        kind = "person"
        label = "A stranger"
        look = plate_stranger_look(body, fallback="a wary human figure already in this place")
        danger = "they are already close enough to hurt you"
    else:
        kind = "person"
        label = "A presence"
        look = "a figure at the edge of this place, clothes matching the light and dust"
        danger = ("they have already closed the distance across this ground"
                  if outdoor else
                  "they are already between you and the way you came")
    return {
        "character": {
            "label": label,
            "kind": kind,
            "look": look,
            "stance": "hostile",
        },
        "danger": danger,
        "stakes": "If you hesitate you will not walk away clean.",
        "place_hold": hold,
    }


def fallback_encounter_brief(place_hold: str = "", seed: str = "",
                             vision: Optional[dict] = None) -> dict:
    vis = vision if isinstance(vision, dict) else {}
    if vis.get("description") or vis.get("setting") or vis.get("spatial"):
        return brief_from_vision(vis, place_hold)
    idx = 0
    if seed:
        idx = sum(ord(c) for c in seed) % len(_FALLBACK_BRIEFS)
    brief = json.loads(json.dumps(_FALLBACK_BRIEFS[idx]))
    if place_hold:
        brief["place_hold"] = _clip(place_hold, "", 160)
    return brief


def read_place_lock(session_id: str, image_path: Optional[str] = None) -> dict:
    """Lock the restage to the current frame: setting, spatial, description."""
    setting = ""
    desc = ""
    spatial = ""
    try:
        import engine
        hist = engine._load_history(session_id) or []
        last = hist[-1] if hist else {}
        if isinstance(last, dict):
            setting = str(last.get("setting_type") or last.get("setting") or "")
            desc = str(last.get("description") or last.get("vision_description")
                       or last.get("caption") or "")
            spatial = str(last.get("spatial") or last.get("spatial_compass") or "")
    except Exception:
        pass
    # Do not call vision here — the brief already sees the attached frame,
    # and a cache-lock / extra Gemini hop was stalling /api/encounter/begin
    # long enough that the Moment died before the plate landed. History
    # setting + spatial is enough to lock outdoor/indoor.
    hold = _clip(" ".join(x for x in (setting, spatial or desc[:120]) if x), "", 160)
    return {
        "setting": setting,
        "description": desc,
        "spatial": spatial,
        "place_hold": hold,
        "outdoor": is_outdoor(setting, f"{desc} {spatial}"),
    }


def encounter_music_prompt(brief: Optional[dict] = None) -> str:
    """Stable scene-audio score key: stance + kind + who + danger.

    The client starts a generic confrontation bed at the flare, then
    re-scores with this string once the brief lands so hostile / desperate /
    opportunistic sit in different registers.
    """
    brief = brief if isinstance(brief, dict) else {}
    char = brief.get("character") if isinstance(brief.get("character"), dict) else {}
    parts = [
        str(char.get("stance") or "hostile").strip().lower(),
        str(char.get("kind") or "person").strip().lower(),
        _clip(char.get("label"), "a stranger", 40),
        _clip(brief.get("danger"), "", 140),
        _clip(brief.get("stakes"), "", 140),
    ]
    return " — ".join(p for p in parts if p)


def _encounter_stinger_urls() -> dict:
    try:
        import scene_audio
        return scene_audio.encounter_stinger_urls() or {}
    except Exception:
        return {}


def encounter_releases(outcome: str, record: Optional[dict] = None) -> bool:
    """Escape returns the player to walking. Die ends the run.

    Putting the other body down also ends it. Without that there was no way to
    WIN a confrontation — confront could only loop you back into the same
    standoff or kill you, so the only winning move was always to run.
    """
    if str(outcome or "").strip().lower() in ("escape", "die"):
        return True
    if isinstance(record, dict):
        return str(record.get("enemy_state") or "").strip().lower() == "down"
    return False


def encounter_choice_overlay(brief: dict, continued: bool = False) -> str:
    brief = brief or {}
    char = (brief.get("character") or {}) if isinstance(brief.get("character"), dict) else {}
    authored = ""
    try:
        import engine
        authored = str((getattr(engine, "PROMPTS", {}) or {}).get("encounter_choice_overlay") or "")
    except Exception:
        authored = ""
    base = (authored or DEFAULT_CHOICE_OVERLAY).strip()
    extra = ""
    if continued:
        extra = (
            "\nThis confrontation is still on. The last action already happened. "
            "One of the three options MUST be getting clear of THIS FIGURE — "
            "that is the only way the player walks away from this interrupt.\n"
        )
    seen = _clip(brief.get("plate_seen"), "", 220)
    seen_line = f"\nTHE IMAGE SHOWS: {seen}\n" if seen else ""
    last = _clip(brief.get("last_dispatch"), "", 280)
    last_line = f"\nLAST BEAT (the world just did this): {last}\n" if last else ""
    return (
        f"{base}{extra}{seen_line}{last_line}\n"
        f"THE FIGURE: {char.get('label') or 'a stranger'} "
        f"({char.get('stance') or 'hostile'}) — {char.get('look') or 'a figure'}.\n"
        f"THEY ARE DOING: {brief.get('danger') or 'challenging you'}.\n"
        f"STAKES: {brief.get('stakes') or 'Hesitation costs you.'}\n"
        "Every choice names them or a verb against them. No landscape."
    )


def apply_turn_dispatch(brief: dict, dispatch: str) -> dict:
    """Ground the interrupt on the engine's consequence — no second narrator."""
    brief = dict(brief or {})
    text = _clip(dispatch, "", 280)
    if text:
        brief["last_dispatch"] = text
    return brief


def hold_after_engine_turn(session_id: str, dispatch: str) -> dict:
    """After the canonical turn pipeline evolved the world, keep the interrupt
    locked and build the next slate from that beat + the action still."""
    import engine
    st = engine._load_state(session_id) or {}
    enc = st.get("encounter") if isinstance(st.get("encounter"), dict) else {}
    rec = st.get("encounter_outcome") if isinstance(st.get("encounter_outcome"), dict) else {}
    brief = normalize_encounter_brief(enc)
    if enc.get("plate_seen"):
        brief["plate_seen"] = enc.get("plate_seen")
    if enc.get("setting"):
        brief["setting"] = enc.get("setting")
    if enc.get("enter_plate_path"):
        brief["enter_plate_path"] = enc.get("enter_plate_path")
    locked = ((enc.get("character") or {}) if isinstance(enc.get("character"), dict) else {}).get("locked_look")
    if locked:
        brief.setdefault("character", {})["locked_look"] = locked
    brief = apply_turn_dispatch(brief, dispatch)
    image_path = enc.get("resolve_path") or enc.get("plate_path")
    web = enc.get("resolve_url") or enc.get("plate_url")
    choices = _continue_encounter(session_id, brief, image_path, web, rec)
    return {
        "dispatch": brief.get("last_dispatch") or dispatch,
        "choices": choices,
        "danger": brief.get("danger") or "",
        "stakes": brief.get("stakes") or "",
        "released": False,
        "alive": True,
    }


def encounter_action_for_turn(choice: str, brief: dict,
                              lane: Optional[str] = None,
                              outcome: Optional[str] = None) -> str:
    """Enrich the short verb for the consequence LLM without changing the feed line."""
    brief = brief or {}
    char = (brief.get("character") or {}) if isinstance(brief.get("character"), dict) else {}
    label = char.get("label") or "a stranger"
    stance = char.get("stance") or "hostile"
    look = char.get("look") or ""
    danger = brief.get("danger") or ""
    stakes = brief.get("stakes") or ""
    verb = (choice or "").strip() or "Hold your ground"
    decided = ""
    lane_l = str(lane or "").strip().lower()
    if lane_l in ENCOUNTER_LANES:
        decided += f" Lane: {lane_l}."
    out_l = str(outcome or "").strip().lower()
    if out_l in ENCOUNTER_OUTCOMES:
        decided += (
            f" Outcome is {out_l} — write that beat. Do not reverse it. "
            "Do not invent a different ending."
        )
        if out_l == "escape":
            decided += (
                " The player has left the confrontation. visual_scene is the "
                "place AFTER they got clear — not the punch, not the standoff."
            )
    return (
        f"[ENCOUNTER] {label} ({stance}) is in this place. {look} "
        f"Danger: {danger} {stakes} "
        f"The player commits: {verb}.{decided} "
        f"The world after this beat is changed — do not restore the prior frame."
    )


_LANE_KEYWORDS = {
    "confront": (
        "shove", "tackle", "strike", "grab", "charge", "lunge", "hit",
        "rush", "confront", "fight", "slam", "punch", "kick them",
        "shoulder", "wrestle", "drive into",
    ),
    "evade": (
        "dive", "dodge", "duck", "flee", "run", "slip", "cover", "evade",
        "retreat", "back away", "roll", "sidestep", "break away", "get clear",
    ),
    "use": (
        "use", "throw", "pull", "wedge", "turn their", "their grip",
        "their weapon", "their momentum", "from them", "against them",
        "grab their", "twist", "rip the",
    ),
}


def classify_encounter_lane(text: str, index: Optional[int] = None) -> str:
    """Keyword first (in case the model reordered), then the 1/2/3 fan."""
    blob = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    hits = []
    for lane, words in _LANE_KEYWORDS.items():
        if any(w in blob for w in words):
            hits.append(lane)
    if len(hits) == 1:
        return hits[0]
    if index is not None:
        try:
            i = int(index)
        except (TypeError, ValueError):
            i = -1
        if 0 <= i < len(ENCOUNTER_LANES):
            return ENCOUNTER_LANES[i]
    if hits:
        return hits[0]
    return "confront"


def structure_encounter_choices(options: Any) -> list:
    """Normalize strings or dicts into ``[{text, lane}, ...]`` (max 3)."""
    raw = options if isinstance(options, (list, tuple)) else []
    out = []
    for i, item in enumerate(raw):
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("label") or "").strip()
            lane = str(item.get("lane") or "").strip().lower()
        else:
            text = str(item or "").strip()
            lane = ""
        if not text or text == "—":
            continue
        if lane not in ENCOUNTER_LANES:
            lane = classify_encounter_lane(text, i)
        text = text.rstrip(" .")
        out.append({"text": text, "lane": lane})
        if len(out) >= 3:
            break
    return out


def short_figure_ref(brief: dict) -> str:
    """A two-word way to name the threat inside a choice button.

    Splicing a clipped nameplate in produced "Break away from A person in blue
    b". The player only needs to know which figure the verb is aimed at, so
    take the person noun and put an article on it.
    """
    char = ((brief or {}).get("character") or {})
    label = str(char.get("label") or "")
    noun = (_first_person_noun(label)
            or _first_person_noun(str(char.get("look") or ""))
            or _first_person_noun(_strip_player_clauses(str((brief or {}).get("plate_seen") or ""))))
    if noun in ("someone", "presence"):
        noun = "figure"
    return f"the {noun}" if noun else "them"


def fallback_encounter_choices(brief: dict) -> list:
    short = short_figure_ref(brief)
    return [
        {"text": f"Shove {short}", "lane": "confront"},
        {"text": f"Break away from {short}", "lane": "evade"},
        {"text": f"Turn {short}'s grip", "lane": "use"},
    ]


def match_encounter_choice(posted_text: str, posted_lane: str,
                           stored: Any) -> tuple[str, str]:
    """Trust the stored slate over the client. Fall back to posted text."""
    verb = (posted_text or "").strip() or "Hold your ground"
    slate = structure_encounter_choices(stored)
    want = verb.casefold()
    for item in slate:
        if (item.get("text") or "").casefold() == want:
            return item["text"], item["lane"]
    lane = str(posted_lane or "").strip().lower()
    if lane not in ENCOUNTER_LANES:
        lane = classify_encounter_lane(verb, None if not slate else 0)
    return verb, lane


def encounter_outcome_weights(lane: str, stance: str = "hostile",
                              kind: str = "person",
                              condition: str = "ok",
                              fate: str = "NORMAL",
                              enemy_state: str = "ready") -> dict:
    """Integer weights for survive / escape / wounded / die.

    `enemy_state` is what makes this a fight rather than a slot machine. A
    confront against a ready body is an opening exchange — it mostly costs the
    other person their balance. A confront against a body already staggered is
    the finish. Rolling both the same way is why committing to a verb used to
    change nothing 75% of the time and kill you the other 25%.
    """
    lane_l = lane if lane in ENCOUNTER_LANES else "confront"
    stance_l = stance if stance in ENCOUNTER_STANCES else "hostile"
    kind_l = kind if kind in ENCOUNTER_KINDS else "person"
    wounded = str(condition or "ok").strip().lower() == "wounded"
    fate_l = str(fate or "NORMAL").strip().upper()
    staggered = str(enemy_state or "ready").strip().lower() == "staggered"

    if lane_l == "confront":
        if kind_l == "creature" or stance_l == "hostile":
            w = {"survive": 55, "escape": 5, "wounded": 32, "die": 8}
        elif stance_l == "desperate":
            w = {"survive": 55, "escape": 10, "wounded": 32, "die": 3}
        else:
            w = {"survive": 70, "escape": 15, "wounded": 15, "die": 0}
        if staggered:
            # They are off their feet. Pressing the advantage should land.
            w = {"survive": 78, "escape": 5, "wounded": 14, "die": 3}
        if wounded:
            w["die"] = w.get("die", 0) + 12
            w["survive"] = max(5, w["survive"] - 15)
    elif lane_l == "evade":
        if kind_l == "creature":
            w = {"survive": 10, "escape": 55, "wounded": 25, "die": 10}
        elif stance_l == "hostile":
            w = {"survive": 10, "escape": 65, "wounded": 20, "die": 5}
        else:
            w = {"survive": 15, "escape": 75, "wounded": 10, "die": 0}
        if wounded:
            w["die"] = w.get("die", 0) + 10
            w["wounded"] = w.get("wounded", 0) + 10
            w["escape"] = max(20, w["escape"] - 15)
    else:
        if stance_l == "opportunistic":
            w = {"survive": 45, "escape": 35, "wounded": 20, "die": 0}
        elif stance_l == "desperate":
            w = {"survive": 55, "escape": 10, "wounded": 35, "die": 0}
        elif kind_l == "creature":
            w = {"survive": 40, "escape": 10, "wounded": 40, "die": 10}
        else:
            w = {"survive": 50, "escape": 15, "wounded": 30, "die": 5}
        if wounded:
            w["die"] = w.get("die", 0) + 10
            w["wounded"] = w.get("wounded", 0) + 10
            w["survive"] = max(15, w["survive"] - 15)

    if fate_l == "LUCKY":
        w["die"] = max(0, w.get("die", 0) - 15)
        w["survive"] = w.get("survive", 0) + 10
        w["escape"] = w.get("escape", 0) + 5
    elif fate_l == "UNLUCKY":
        w["wounded"] = w.get("wounded", 0) + 15
        w["survive"] = max(5, w.get("survive", 0) - 10)
        if lane_l == "confront":
            w["die"] = w.get("die", 0) + 10
    return {k: max(0, int(v)) for k, v in w.items()}


def advance_enemy_state(lane: str, outcome: str, enemy_state: str = "ready",
                        rng: Any = None) -> str:
    """How the other body changes as a result of this exchange.

    This is the escalation the encounter never had. Pressing a confront moves
    them ready -> staggered -> down, so two committed verbs finish a fight and
    the player can win one, which was previously impossible: `encounter_releases`
    only fired on escape or death.
    """
    state = str(enemy_state or "ready").strip().lower()
    if state not in ENCOUNTER_ENEMY_STATES:
        state = "ready"
    if outcome in ("die", "escape") or state == "down":
        return state
    roll = rng.random() if rng is not None else random.random()
    if lane == "confront":
        if state == "staggered":
            return "down" if roll < 0.62 else "staggered"
        return "staggered" if roll < 0.55 else "ready"
    if lane == "use":
        # Turning their own weight or tool against them unbalances, rarely ends it.
        if state == "staggered":
            return "down" if roll < 0.35 else "staggered"
        return "staggered" if roll < 0.40 else "ready"
    # Evading buys distance; they recover their footing.
    return "ready" if roll < 0.6 else state


def roll_encounter_outcome(lane: str, stance: str = "hostile",
                           kind: str = "person", condition: str = "ok",
                           fate: str = "NORMAL", rng: Any = None,
                           enemy_state: str = "ready",
                           round_no: int = 1) -> dict:
    """Server-owned result. The consequence LLM writes this beat; it does not flip it."""
    weights = encounter_outcome_weights(lane, stance, kind, condition, fate,
                                        enemy_state=enemy_state)
    total = sum(weights.values()) or 1
    pick = rng.random() if rng is not None else random.random()
    cursor = 0.0
    outcome = "survive"
    for name in ENCOUNTER_OUTCOMES:
        cursor += weights.get(name, 0) / total
        if pick <= cursor:
            outcome = name
            break
    prev = "wounded" if str(condition or "").strip().lower() == "wounded" else "ok"
    next_enemy = advance_enemy_state(lane, outcome, enemy_state, rng=rng)
    if outcome == "die":
        next_cond = prev
        alive = False
    elif outcome == "wounded":
        next_cond = "wounded"
        alive = True
    else:
        next_cond = prev
        alive = True
    return {
        "outcome": outcome,
        "alive": alive,
        "condition": next_cond,
        "lane": lane if lane in ENCOUNTER_LANES else "confront",
        "enemy_state": next_enemy,
        "round_no": max(1, int(round_no or 1)),
        "weights": weights,
    }


def sample_travel_budget(*, first: bool = False, rng: Any = None) -> float:
    """Seconds of walking until the next encounter. Variance lives here, not
    in a dice roll after the player has already walked far enough."""
    pick = rng.uniform if rng is not None else random.uniform
    lo, hi = (ENCOUNTER_TRAVEL_FIRST_MIN, ENCOUNTER_TRAVEL_FIRST_MAX) if first \
        else (ENCOUNTER_TRAVEL_MIN, ENCOUNTER_TRAVEL_MAX)
    if hi < lo:
        lo, hi = hi, lo
    return round(float(pick(lo, hi)), 2)


def encounter_can_roll(state: Optional[dict], *, now: Optional[float] = None,
                       force: bool = False) -> tuple[bool, str]:
    """Hard gates only. Distance is owned by the travel clock, not a cooldown."""
    if force:
        return True, "forced"
    st = state if isinstance(state, dict) else {}
    if st.get("encounter"):
        return False, "already_open"
    if not (st.get("player_state") or {}).get("alive", True):
        return False, "game_over"
    return True, "ok"


def ensure_travel_clock(state: dict, *, rng: Any = None) -> float:
    """Arm a budget on first walk of the run. Looking never calls this."""
    st = state if isinstance(state, dict) else {}
    raw = st.get("encounter_travel_remain")
    if raw is None:
        first = not bool(st.get("encounter_last_at") or st.get("encounter_last_turn"))
        remain = sample_travel_budget(first=first, rng=rng)
        st["encounter_travel_remain"] = remain
        return remain
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        remain = sample_travel_budget(first=False, rng=rng)
        st["encounter_travel_remain"] = remain
        return remain


def reset_travel_clock(state: dict, *, rng: Any = None) -> float:
    """Arm the NEXT walk after an encounter (or a demo fire)."""
    st = state if isinstance(state, dict) else {}
    remain = sample_travel_budget(first=False, rng=rng)
    st["encounter_travel_remain"] = remain
    return remain


def apply_travel(state: dict, dt: float, *, now: Optional[float] = None,
                 rng: Any = None) -> tuple[bool, str, float]:
    """Count down walking time. ``dt`` is seconds of translation, not look.

    Returns ``(fire, reason, remain)``. Standing / looking send ``dt=0`` and
    must not move the clock. When the clock hits zero the encounter is due —
    no extra dice roll.
    """
    st = state if isinstance(state, dict) else {}
    try:
        dt = float(dt)
    except (TypeError, ValueError):
        dt = 0.0
    if dt < 0:
        dt = 0.0
    if dt > ENCOUNTER_TRAVEL_DT_MAX:
        dt = ENCOUNTER_TRAVEL_DT_MAX

    ok, reason = encounter_can_roll(st, now=now)
    if not ok:
        remain = 0.0
        try:
            remain = max(0.0, float(st.get("encounter_travel_remain") or 0))
        except (TypeError, ValueError):
            remain = 0.0
        return False, reason, remain

    remain = ensure_travel_clock(st, rng=rng)
    if dt <= 0:
        return False, "no_travel", remain

    remain = max(0.0, round(remain - dt, 3))
    st["encounter_travel_remain"] = remain
    if remain > 0:
        return False, "counting", remain

    reset_travel_clock(st, rng=rng)
    return True, "due", 0.0


def build_encounter_plate_prompt(brief: dict, img2img: bool = True,
                                 setting: str = "") -> str:
    """Cinematic restage of THIS place with the new character and danger visible."""
    brief = normalize_encounter_brief(brief)
    char = brief["character"]
    bits = []
    try:
        import game_identity
        anchor = game_identity.world_anchor(
            ENCOUNTER_PLATE_STYLE_ANCHOR,
            include_character=True,
            include_vantage=False,
        )
        if anchor:
            bits.append(anchor.rstrip(". ") + ".")
    except Exception:
        bits.append(ENCOUNTER_PLATE_STYLE_ANCHOR + ".")

    authored = ""
    try:
        import engine
        authored = str((getattr(engine, "PROMPTS", {}) or {}).get("encounter_plate_anchor") or "")
    except Exception:
        authored = ""
    if authored:
        bits.append(authored.strip())

    outdoor = is_outdoor(setting, brief.get("place_hold") or "")
    if img2img:
        bits.append(
            "PLACE LOCK — HARD. The reference is the current exploration "
            "photograph of this exact place. Keep the same location, "
            "architecture, materials, ground, sky, and light. ADD the new "
            "character and danger INTO this photograph. Do not change the "
            "place. Do not teleport."
        )
        if outdoor:
            bits.append(
                "This frame is OUTDOORS. The result MUST stay outdoors. "
                "Same sky, same ground underfoot, same landmarks. "
                "Do not invent an interior, garage, basement, corridor, "
                "hatch, or room."
            )
        elif (setting or "").lower().startswith("indoor"):
            bits.append(
                "This frame is INDOORS. Stay in this same room. Same walls, "
                "same light. Do not go outside or into a different room."
            )
        if _camera_shows_player():
            # The place lock protects the location and then says "ADD the new
            # character", which leaves the people fair game: the newcomer got
            # drawn large in the foreground and the player was dropped out of
            # their own standoff, so the first frame showed a stranger as
            # "me". The resolve never had this problem because hold_cast makes
            # it copy faces out of the reference. The plate needs the same
            # instruction, scoped to the one person already standing there.
            bits.append(
                "CARRY THE PLAYER OVER — HARD. The person already in the "
                "reference photograph IS the player. Copy their face, hair, "
                "build, and clothes from that photograph and keep them IN "
                "FRAME at similar size, turned to face the newcomer, body and "
                "face readable. Do not delete them, do not swap them for the "
                "newcomer, and do not give the frame to the newcomer alone. "
                "Add EXACTLY ONE new person to the photograph."
            )
    look = distinct_enemy_look(char.get("locked_look") or char.get("look") or "")
    if _camera_shows_player():
        cast = player_cast_lock()
        if cast:
            bits.append(cast)
        bits.append(
            f"TWO DISTINCT PEOPLE IN A STANDOFF, not a completed attack. "
            f"(1) The player character stays the same person as the character "
            f"sheet — only THEY wear that outfit. "
            f"(2) A newly introduced {char['kind']} named "
            f"'{char['label']}' — {look} — stands close, "
            f"{char['stance']}, large and readable, facing the player. "
            f"Different face, different clothes — not a second press vest, not a "
            f"high-vis vest, not a copy of the player. Do not merge them. "
            f"Do not swap their genders or faces. Do not clone the player. "
            f"Do not show a choke or takedown already landed — weight ready, "
            f"not a body already winning."
        )
    else:
        # First person. Demanding a two-shot here left the player's body out of
        # frame and handed the composition to the stranger, so the camera read
        # as following THEM around instead of being the player's own eyes.
        bits.append(
            f"FIRST-PERSON POV — this is the player's own eyes. The player's "
            f"body is NOT in frame: no second figure standing in for them, no "
            f"back of a head, no over-the-shoulder onto the player. "
            f"EXACTLY ONE person is visible: a {char['kind']} — {look} — "
            f"squared up close to the camera, {char['stance']}, filling much "
            f"of the frame, looking straight down the lens at the player. "
            f"They are coming at the viewer. Do not add a bystander. "
            f"Do not show a takedown already landed."
        )
    bits.append(
        f"The danger is THIS FIGURE's body, already close: {brief['danger']}. "
        f"Do not invent sludge, fire, collapse, or a prop the reference "
        f"photograph does not already show."
    )
    if brief.get("place_hold"):
        bits.append(f"Hold these place locks: {brief['place_hold']}.")
    if _camera_shows_player():
        bits.append(
            "Cinematic two-shot or over-shoulder. Bodies readable. Empty hands, "
            "no HUD, no game UI, no captions, no letterbox. A finished 1993 photograph."
        )
    else:
        bits.append(
            "Point-of-view framing, one figure close to the lens. "
            "No HUD, no game UI, no captions, no letterbox. A finished 1993 photograph."
        )
    prompt = " ".join(bits)
    try:
        import engine
        if hasattr(engine, "_sanitize_for_image_generation"):
            prompt = engine._sanitize_for_image_generation(prompt)
    except Exception:
        pass
    return prompt


def build_encounter_resolve_prompt(brief: dict, verb: str, lane: str,
                                   outcome: str, setting: str = "") -> str:
    """Hard-cut still of THIS verb landing. Place and cast are text locks."""
    brief = normalize_encounter_brief(brief)
    char = brief["character"]
    verb_s = (verb or "").strip() or "Hold your ground"
    lane_s = lane if lane in ENCOUNTER_LANES else "confront"
    out_s = outcome if outcome in ENCOUNTER_OUTCOMES else "survive"
    bits = []
    try:
        import game_identity
        # Style only. Naming the character-sheet person here recasts the
        # plate (a man in a PRESS vest becomes a different woman).
        anchor = game_identity.world_anchor(
            ENCOUNTER_ACTION_STYLE_ANCHOR,
            include_character=False,
            include_vantage=False,
        )
        if anchor:
            bits.append(anchor.rstrip(". ") + ".")
    except Exception:
        bits.append(ENCOUNTER_ACTION_STYLE_ANCHOR + ".")

    outdoor = is_outdoor(setting, brief.get("place_hold") or "")
    bits.append(
        "PLACE LOCK — TEXT ONLY. Same location, architecture, materials, "
        "ground, sky, and light. Do not teleport. This is a NEW SHOT of that "
        "place, not a pose-edit of the last still. Do not copy the previous "
        "camera angle or blocking."
    )
    if outdoor:
        bits.append(
            "This frame is OUTDOORS. The result MUST stay outdoors. "
            "Same sky, same ground underfoot, same landmarks. "
            "Do not invent an interior, garage, basement, corridor, "
            "hatch, or room."
        )
    elif (setting or "").lower().startswith("indoor"):
        bits.append(
            "This frame is INDOORS. Stay in this same room. Same walls, "
            "same light. Do not go outside or into a different room."
        )
    if brief.get("place_hold"):
        bits.append(f"Hold these place locks: {brief['place_hold']}.")
    locked = distinct_enemy_look(char.get("locked_look") or char.get("look") or "")
    player_name = "the player"
    player_clothes = player_wardrobe_text()
    try:
        import game_identity
        player_name = game_identity.display_name() or player_name
    except Exception:
        pass
    actor = (
        f"The PLAYER ({player_name}"
        f"{', wearing ' + player_clothes if player_clothes else ''}) "
        f"is the one performing the verb."
    )
    motion = {
        "confront": (
            f"{actor} Their hands and weight are already on the other person "
            "— a shove, a strike, bodies colliding. The other person is "
            "receiving the hit, off-balance."
        ),
        "evade": (
            f"{actor} Their body is already past the other person — a miss, "
            "a gap. The other person grabs air."
        ),
        "use": (
            f"{actor} Their hands are on the other person's arm or tool, "
            "turning it against them."
        ),
    }.get(lane_s, f"{actor} Bodies in contact. The player is acting.")
    bits.append(
        f"CAST LOCK — TEXT ONLY. Same two people. Same faces, hair, clothes, "
        f"gender. The other person is {char['label']} — {locked}. "
        f"They are NOT wearing the player's vest or PRESS gear. "
        f"Do not recast. Do not add a third person. Do not draw a character sheet."
    )
    bits.append(
        f"AGENCY LOCK — HARD. {actor} "
        f"The other person is the TARGET, not the attacker. "
        f"If the reference already shows the stranger attacking the player, "
        f"REVERSE the contact: the player's hands are now on them. "
        f"Do not show the stranger choking, striking, or throwing the player "
        f"unless this is a die/wounded beat where the player failed."
    )
    shot = ENCOUNTER_SHOT_LADDER[
        min(max(1, int(brief.get("round_no") or 1)), len(ENCOUNTER_SHOT_LADDER)) - 1
    ]
    bits.append(
        f"THIS IS A HARD CUT. New camera, new blocking. {shot} "
        f"Show the instant the verb lands: {verb_s} ({lane_s}). {motion} "
        f"Bodies are in contact or a hand's width apart — this is an exchange, "
        f"not a conversation. If you return the previous standoff with a small "
        f"pose change, you failed. If the two people are standing apart looking "
        f"at each other, you failed. "
        f"If you show the stranger doing the verb to the player, you failed."
    )
    enemy_state = str(brief.get("enemy_state") or "ready").strip().lower()
    if out_s in ("survive", "wounded") and enemy_state == "down":
        bits.append(
            "THIS IS THE FINISH. The other person is going down — knees "
            "buckling or already on the ground, no longer a threat. The "
            "player is standing over them, breathing hard. No gore."
        )
    elif out_s in ("survive", "wounded") and enemy_state == "staggered":
        bits.append(
            "The blow has rocked them. They are off-balance, reeling, "
            "a hand out for something to catch — but still up, still in it. "
            "This is the middle of the fight, not the end of it."
        )
    elif out_s == "survive":
        bits.append(
            "The verb has already landed. Contact, weight, a body reacting. "
            f"{char['label']} is still readable. This is the instant it works."
        )
    elif out_s == "escape":
        bits.append(
            "The player is already clear in THIS same place — distance, an "
            "opening, the character receding. New camera on that gap. "
            "Do not restore an earlier empty frame or the previous punch."
        )
    elif out_s == "wounded":
        bits.append(
            "The verb happened and the cost is visible on the player's body — "
            "a stagger, a torn sleeve, a hand clutching. No gore, no blood spray. "
            f"{char['label']} is still the same person."
        )
    else:
        bits.append(
            "The danger has them. The verb did not save them. "
            f"{char['label']} dominates the frame. Same face as the reference."
        )
    bits.append(
        "No HUD, no game UI, no captions, no letterbox. "
        "A finished 1993 photograph."
    )
    prompt = " ".join(bits)
    try:
        import engine
        if hasattr(engine, "_sanitize_for_image_generation"):
            prompt = engine._sanitize_for_image_generation(prompt)
    except Exception:
        pass
    return prompt


def build_encounter_brief(session_id: str = "default", image_path: Optional[str] = None,
                          place_hold: str = "", vision: Optional[dict] = None) -> dict:
    """Ask the model for a character + danger grounded on the current frame."""
    import engine
    st = {}
    try:
        st = engine._load_state(session_id) or {}
    except Exception:
        st = {}
    world = str(st.get("world_prompt") or "")[:400]
    seed = world or session_id
    vis = vision if isinstance(vision, dict) else {}
    hold = place_hold or vis.get("place_hold") or ""
    instructions = str(
        (getattr(engine, "PROMPTS", {}) or {}).get("encounter_brief_instructions")
        or DEFAULT_BRIEF_INSTRUCTIONS
    )
    setting = vis.get("setting") or ""
    visible = vis.get("description") or ""
    prompt = (
        f"{instructions}\n\nWORLD (trim): {world}\n"
        f"SETTING: {setting or 'unknown'}\n"
        f"VISIBLE: {visible[:400]}\n"
        "The character and danger MUST fit THIS setting. "
        "If outdoor, do not invent an interior. If indoor, do not go outside.\n"
        "label names the body you can photograph — clothes, wound, job. "
        "Do not invent a rank or sci-fi class this 1993 world cannot show.\n"
        f"{_brief_cast_rule()}"
        "Return one JSON object with character, danger, stakes, place_hold."
    )
    raw = ""
    try:
        raw = engine._ask(
            prompt,
            model="gemini",
            temp=0.9,
            tokens=220,
            image_path=image_path,
            use_lore=False,
            response_schema=ENCOUNTER_BRIEF_SCHEMA,
        )
    except Exception as err:
        try:
            engine.log_error(f"[ENCOUNTER] brief ask failed: {err}")
        except Exception:
            pass
        return fallback_encounter_brief(hold, seed=seed, vision=vis)
    brief = normalize_encounter_brief(raw, place_hold=hold)
    # If the model returned the disabled-LLM placeholder prose, fall back.
    label = (brief.get("character") or {}).get("label") or ""
    if label.lower() in ("you are still",) or "signal interrupted" in str(raw).lower():
        return fallback_encounter_brief(hold, seed=seed, vision=vis)
    if hold and not brief.get("place_hold"):
        brief["place_hold"] = _clip(hold, "", 160)
    return separate_cast(brief)


def plate_shows_confrontation(vision: Optional[dict], brief: Optional[dict] = None) -> bool:
    """True when vision of the plate actually contains a second presence."""
    vis = vision if isinstance(vision, dict) else {}
    desc = str(vis.get("description") or "").lower()
    if not desc:
        return False
    label = ""
    look = ""
    char = (brief or {}).get("character") if isinstance(brief, dict) else {}
    if isinstance(char, dict):
        label = str(char.get("label") or "").strip().lower()
        look = str(char.get("look") or "").strip().lower()
    if label and label in desc:
        return True
    look_hits = 0
    for token in (w for w in re.split(r"[^a-z0-9]+", look) if len(w) > 3):
        if token in desc:
            look_hits += 1
    if look_hits >= 2:
        return True
    figures = 0
    for w in ("person", "people", "figure", "woman", "stranger", "hooded",
              "goggles", "creature", "being", "scavenger"):
        if re.search(r"\b" + w + r"\b", desc):
            figures += 1
    if re.search(r"\bman\b", desc) or re.search(r"\bmen\b", desc):
        figures += 1
    if figures >= 2:
        return True
    if any(w in desc for w in ("two people", "another person", "facing you",
                               "facing the", "in the doorway", "in front of")):
        return True
    return False


def _parse_choice_payload(raw: Any) -> list:
    """Accept JSON {confront,evade,use} or numbered lines."""
    data = raw
    if isinstance(raw, str):
        blob = raw.strip()
        if blob.startswith("```"):
            blob = re.sub(r"^```(?:json)?\s*", "", blob)
            blob = re.sub(r"\s*```$", "", blob)
        try:
            data = json.loads(blob)
        except Exception:
            data = None
            lines = []
            for line in raw.splitlines():
                text = re.sub(r"^\s*(?:\d+[\.)]\s*|[-*]\s*)", "", line).strip()
                text = text.strip("\"'")
                if text and text != "—":
                    lines.append(text)
            return structure_encounter_choices(lines)
    if isinstance(data, dict):
        ordered = []
        for lane in ENCOUNTER_LANES:
            text = str(data.get(lane) or "").strip()
            if text:
                ordered.append({"text": text, "lane": lane})
        return ordered
    if isinstance(data, list):
        return structure_encounter_choices(data)
    return []


def _generate_encounter_choices(brief: dict, image_url: Optional[str],
                                session_id: str = "default",
                                continued: bool = False) -> list:
    """Dedicated confrontation slate — never the explore choice template."""
    import engine
    authored = ""
    try:
        authored = str(
            (getattr(engine, "PROMPTS", {}) or {}).get("encounter_choice_instructions")
            or ""
        )
    except Exception:
        authored = ""
    prompt = (authored or DEFAULT_CHOICE_INSTRUCTIONS).strip()
    prompt = f"{prompt}\n\n{encounter_choice_overlay(brief, continued=continued)}"
    raw = ""
    try:
        raw = engine._ask(
            prompt,
            model="gemini",
            temp=0.9,
            tokens=120,
            image_path=image_url,
            use_lore=False,
            response_schema=ENCOUNTER_CHOICE_SCHEMA,
        )
    except Exception as err:
        try:
            engine.log_error(f"[ENCOUNTER] choices failed: {err}")
        except Exception:
            pass
        raw = ""
    structured = prefer_threat_choices(_parse_choice_payload(raw), brief)
    if len(structured) < 3:
        structured = fallback_encounter_choices(brief)
    return structured[:3]


def _pin_encounter_plate(session_id: str, image_path: Optional[str], web_url: Optional[str],
                         brief: dict) -> None:
    """Make the confrontation plate the img2img reference for the aftermath turn."""
    import engine
    brief = dict(brief or {})
    if web_url:
        brief["plate_url"] = web_url
    if image_path:
        brief["plate_path"] = image_path
        brief["enter_plate_path"] = image_path
    with engine.WORLD_STATE_LOCK:
        st = engine._load_state(session_id) or {}
        if web_url:
            st["current_image_url"] = web_url
        st["encounter"] = brief
        st["encounter_last_turn"] = int(st.get("turn_count") or 0)
        st["encounter_last_at"] = time.time()
        reset_travel_clock(st)
        engine._save_state(st, session_id)
        engine._sync_ambient_state(st, session_id)
    if not image_path:
        return
    try:
        hist = engine._load_history(session_id) or []
        prev = hist[-1] if hist else {}
        hist.append({
            "choice": "__encounter_plate__",
            "dispatch": brief.get("stakes") or "",
            "vision_dispatch": "",
            "world_prompt": prev.get("world_prompt") or "",
            "image": image_path,
            "image_url": image_path,
            "hard_transition": False,
            "encounter": True,
        })
        engine._save_history(hist, session_id)
    except Exception as err:
        try:
            engine.log_error(f"[ENCOUNTER] pin plate history failed: {err}")
        except Exception:
            pass


def _record_encounter_companion(session_id: str, brief: dict, image_path: Optional[str],
                                web_url: Optional[str], prompt: str = "") -> Optional[dict]:
    import engine
    char = (brief or {}).get("character") or {}
    if not char.get("label"):
        return None
    try:
        durable = None
        if image_path:
            durable = engine._persist_companion_image(image_path, session_id, char["label"])
        return engine._record_companion(
            session_id, char, durable or web_url or "",
            prompt=prompt, scene=str((brief or {}).get("danger") or ""),
        )
    except Exception as err:
        try:
            engine.log_error(f"[ENCOUNTER] companion record failed: {err}")
        except Exception:
            pass
        return None


def clear_encounter(session_id: str) -> None:
    import engine
    try:
        with engine.WORLD_STATE_LOCK:
            st = engine._load_state(session_id) or {}
            if "encounter" in st:
                st["encounter"] = None
            st["encounter_resolving"] = False
            engine._save_state(st, session_id)
            engine._sync_ambient_state(st, session_id)
    except Exception:
        pass


def _clear_encounter_resolving(session_id: str) -> None:
    import engine
    try:
        with engine.WORLD_STATE_LOCK:
            st = engine._load_state(session_id) or {}
            st["encounter_resolving"] = False
            engine._save_state(st, session_id)
            engine._sync_ambient_state(st, session_id)
    except Exception:
        pass


def _confrontation_plate_path(session_id: str, brief: Optional[dict] = None) -> Optional[str]:
    """Filesystem path of the confrontation plate (style-swatch source for resolve)."""
    enc = brief if isinstance(brief, dict) else {}
    for key in ("enter_plate_path", "plate_path", "image", "image_path"):
        p = enc.get(key)
        if p and Path(str(p)).exists():
            return str(p)
    try:
        import engine
        hist = engine._load_history(session_id) or []
    except Exception:
        hist = []
    for entry in reversed(hist or []):
        if not isinstance(entry, dict):
            continue
        if entry.get("choice") == "__encounter_resolve__":
            continue
        p = entry.get("image") or entry.get("image_url")
        if p and Path(str(p)).exists():
            return str(p)
        if entry.get("encounter") and p and Path(str(p)).exists():
            return str(p)
    return None


def _pin_encounter_resolve(session_id: str, image_path: Optional[str],
                           web_url: Optional[str], brief: dict,
                           record: dict) -> None:
    """Make the resolve still the walkable world. Keep the encounter open
    until the aftermath thread starts so a failed resolve can abort cleanly."""
    import engine
    brief = dict(brief or {})
    if web_url:
        brief["resolve_url"] = web_url
        brief["plate_url"] = brief.get("plate_url") or web_url
    if image_path:
        brief["resolve_path"] = image_path
    # Carry the exchange forward so the next round opens where this one left
    # off instead of resetting to a fresh standoff.
    brief["enemy_state"] = (
        (record or {}).get("enemy_state") or brief.get("enemy_state") or "ready"
    )
    brief["round_no"] = max(1, int((record or {}).get("round_no") or 1)) + 1
    with engine.WORLD_STATE_LOCK:
        st = engine._load_state(session_id) or {}
        if web_url:
            st["current_image_url"] = web_url
        st["encounter"] = brief
        st["encounter_outcome"] = dict(record or {})
        st["encounter_resolving"] = True
        ps = st.setdefault("player_state", {"alive": True})
        if not isinstance(ps, dict):
            ps = {"alive": True}
            st["player_state"] = ps
        ps["alive"] = bool((record or {}).get("alive", True))
        cond = str((record or {}).get("condition") or ps.get("condition") or "ok")
        ps["condition"] = cond if cond in ENCOUNTER_CONDITIONS else "ok"
        engine._save_state(st, session_id)
        engine._sync_ambient_state(st, session_id)
    if not image_path:
        return
    try:
        hist = engine._load_history(session_id) or []
        prev = hist[-1] if hist else {}
        hist.append({
            "choice": "__encounter_resolve__",
            "dispatch": (record or {}).get("verb") or brief.get("stakes") or "",
            "vision_dispatch": "",
            "world_prompt": prev.get("world_prompt") or "",
            "image": image_path,
            "image_url": image_path,
            "hard_transition": True,
            "encounter": True,
            "encounter_outcome": (record or {}).get("outcome"),
        })
        engine._save_history(hist, session_id)
    except Exception as err:
        try:
            engine.log_error(f"[ENCOUNTER] pin resolve history failed: {err}")
        except Exception:
            pass


def _continue_encounter(session_id: str, brief: dict, image_path: Optional[str],
                        web_url: Optional[str], record: dict) -> list:
    """Stay locked. The reaction still is the new plate; new bars; no walk."""
    import engine
    brief = dict(brief or {})
    if web_url:
        brief["plate_url"] = web_url
    kept_enter = brief.get("enter_plate_path")
    if image_path:
        brief["plate_path"] = image_path
    if kept_enter:
        brief["enter_plate_path"] = kept_enter
    brief.pop("resolve_url", None)
    brief.pop("resolve_prompt", None)
    choices = _generate_encounter_choices(
        brief, image_path, session_id, continued=True,
    )
    brief["choices"] = choices
    with engine.WORLD_STATE_LOCK:
        st = engine._load_state(session_id) or {}
        if web_url:
            st["current_image_url"] = web_url
        st["encounter"] = brief
        st["encounter_outcome"] = dict(record or {})
        st["encounter_resolving"] = False
        engine._save_state(st, session_id)
        engine._sync_ambient_state(st, session_id)
    return choices


def api_begin():
    """POST /api/encounter/begin — invent brief + plate + 3 choices."""
    from flask import jsonify, request
    import engine

    if engine._rate_limited("encounter_begin", 2.0):
        return jsonify({"error": "slow_down"}), 429
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id") or engine._resolve_request_session_id()
    force = bool(data.get("force") or data.get("demo"))
    reference_b64 = data.get("frame") or data.get("reference_image") or ""

    st = engine._load_state(session_id) or {}
    ok, reason = encounter_can_roll(st, force=force)
    if not ok and reason == "already_open":
        existing = st.get("encounter") if isinstance(st.get("encounter"), dict) else None
        if existing:
            return jsonify({
                "encounter": existing,
                "plate_url": existing.get("plate_url"),
                "choices": structure_encounter_choices(
                    existing.get("choices") or fallback_encounter_choices(existing)
                ),
                "cached": True,
                "music_prompt": encounter_music_prompt(existing),
                "stingers": _encounter_stinger_urls(),
            })
    if not ok:
        return jsonify({"error": reason, "fire": False}), 409

    ref_path = engine._save_portrait_reference(reference_b64, session_id) if reference_b64 else None
    if not ref_path:
        # No frame posted (autoplay, a dropped capture, the first beat after a
        # world stitch). Without a reference the plate fell through to
        # text-to-image, which renders a wide establishing shot of the place
        # with no antagonist in it — the "camera angle that doesn't show the
        # fight". The last rendered frame is the place; restage that instead.
        ref_path = _confrontation_plate_path(session_id)
    place = read_place_lock(session_id, ref_path)
    brief = build_encounter_brief(
        session_id, image_path=ref_path,
        place_hold=place.get("place_hold") or "",
        vision=place,
    )
    prompt = build_encounter_plate_prompt(
        brief, img2img=bool(ref_path),
        setting=place.get("setting") or "",
    )

    image_path = None
    web = None
    gen_mode = "none"
    t0 = time.time()
    if getattr(engine, "IMAGE_ENABLED", True):
        try:
            tod = str(st.get("time_of_day") or "")
            img_dir = engine._get_image_dir(session_id)
            label = brief["character"]["label"]
            # Do not pass the full world prompt — lore about interiors will
            # teleport an outdoor walk into a garage. Place lock is enough.
            place_ctx = brief.get("place_hold") or place.get("place_hold") or ""
            if ref_path:
                from gemini_image_utils import generate_gemini_img2img
                identity = encounter_identity_paths()
                image_path = generate_gemini_img2img(
                    prompt=prompt,
                    caption=f"encounter_{label}",
                    reference_image_path=ref_path,
                    strength=ENCOUNTER_PLATE_STRENGTH,
                    world_prompt=place_ctx[:200] if place_ctx else None,
                    time_of_day=tod,
                    hd_mode=False,
                    output_dir=Path(img_dir),
                    include_people=True,
                    identity_paths=identity or None,
                )
                gen_mode = "img2img"
                if image_path:
                    try:
                        plate_vis = engine._vision_analyze_all(image_path) or {}
                    except Exception:
                        plate_vis = {}
                    seen = _clip(plate_vis.get("description") or "", "", 220)
                    if seen:
                        brief["plate_seen"] = seen
                        # Lock the challenger to the person the plate actually
                        # drew, not the one the brief invented before it.
                        adopt_plate_look(brief, seen)
                        label = brief["character"]["label"]
                    if not plate_shows_confrontation(plate_vis, brief):
                        try:
                            engine.log_error(
                                "[ENCOUNTER] plate missing the new character — retrying"
                            )
                        except Exception:
                            pass
                        retry_path = generate_gemini_img2img(
                            prompt=prompt + (
                                " The new character is already standing in this "
                                "frame, large, facing the player. This is the "
                                "confrontation, not an empty place. "
                                "Keep the player as the character-sheet person. "
                                "The other person is a stranger in different clothes "
                                "— not a second copy of the player."
                            ),
                            caption=f"encounter_{label}_retry",
                            reference_image_path=ref_path,
                            strength=ENCOUNTER_PLATE_RETRY_STRENGTH,
                            world_prompt=place_ctx[:200] if place_ctx else None,
                            time_of_day=tod,
                            hd_mode=False,
                            output_dir=Path(img_dir),
                            include_people=True,
                            identity_paths=identity or None,
                        )
                        if retry_path:
                            image_path = retry_path
                            gen_mode = "img2img_retry"
                            try:
                                plate_vis = engine._vision_analyze_all(image_path) or {}
                                seen = _clip(plate_vis.get("description") or "", "", 220)
                                if seen:
                                    brief["plate_seen"] = seen
                                    adopt_plate_look(brief, seen)
                            except Exception:
                                pass
            else:
                from gemini_image_utils import generate_with_gemini
                image_path = generate_with_gemini(
                    prompt=prompt,
                    caption=f"encounter_{label}",
                    world_prompt=place_ctx[:200] if place_ctx else None,
                    aspect_ratio="16:9",
                    time_of_day=tod,
                    hd_mode=False,
                    output_dir=Path(img_dir),
                )
                gen_mode = "text2img"
                # This path used to skip vision entirely, so a text2img plate
                # was never checked for actually containing the antagonist and
                # never grounded the cast lock.
                if image_path:
                    try:
                        plate_vis = engine._vision_analyze_all(image_path) or {}
                    except Exception:
                        plate_vis = {}
                    seen = _clip(plate_vis.get("description") or "", "", 220)
                    if seen:
                        brief["plate_seen"] = seen
                        adopt_plate_look(brief, seen)
                    if not plate_shows_confrontation(plate_vis, brief):
                        try:
                            engine.log_error(
                                "[ENCOUNTER] text2img plate missing the new "
                                "character — retrying"
                            )
                        except Exception:
                            pass
                        retry_path = generate_with_gemini(
                            prompt=prompt + (
                                " The new character is already standing in this "
                                "frame, large, close, facing the player. This is "
                                "the confrontation, not an empty landscape. Do "
                                "not render a wide establishing shot."
                            ),
                            caption=f"encounter_{label}_retry",
                            world_prompt=place_ctx[:200] if place_ctx else None,
                            aspect_ratio="16:9",
                            time_of_day=tod,
                            hd_mode=False,
                            output_dir=Path(img_dir),
                        )
                        if retry_path:
                            image_path = retry_path
                            gen_mode = "text2img_retry"
                            try:
                                plate_vis = engine._vision_analyze_all(image_path) or {}
                                seen = _clip(plate_vis.get("description") or "", "", 220)
                                if seen:
                                    brief["plate_seen"] = seen
                                    adopt_plate_look(brief, seen)
                            except Exception:
                                pass
        except Exception as gen_err:
            try:
                engine.log_error(f"[ENCOUNTER] plate generate failed: {gen_err}")
            except Exception:
                pass
            try:
                engine.cost_tracker.record_usage(
                    session_id, "image", "gemini", "encounter_plate",
                    operation="encounter_plate", output_units=0, unit_type="images",
                    latency_ms=int((time.time() - t0) * 1000), success=False,
                    error_message=str(gen_err)[:200],
                )
            except Exception:
                pass

        web = engine._to_web_image_url(image_path, session_id) if image_path else None
        try:
            engine.cost_tracker.record_usage(
                session_id, "image", "gemini", "encounter_plate",
                operation=f"encounter_plate_{gen_mode}",
                output_units=1.0 if web else 0, unit_type="images",
                latency_ms=int((time.time() - t0) * 1000), success=bool(web),
                error_message=None if web else "no_image_returned",
            )
        except Exception:
            pass

    # The plate is canon. If the brief invented a rank or weapon the still
    # did not draw, the nameplate and slate follow the photograph.
    if brief.get("plate_seen"):
        brief = align_brief_to_plate(brief, {"description": brief.get("plate_seen")})

    # Choices must see the plate (or the captured frame) — never text-only
    # against a leftover canned brief, or they name a pipe that isn't there.
    choice_image = image_path or ref_path
    choices = _generate_encounter_choices(brief, choice_image, session_id)
    brief["choices"] = choices
    brief["setting"] = place.get("setting") or ""
    if web:
        brief["plate_url"] = web
    _pin_encounter_plate(session_id, image_path, web, brief)
    _record_encounter_companion(session_id, brief, image_path, web, prompt)
    try:
        import play_log
        play_log.record("encounter_begin", session_id, {
            **play_log.diagnose_encounter(brief, choices),
            "plate_seen": (brief.get("plate_seen") or "")[:220],
            "choices": [c.get("text") for c in choices],
            "plate_prompt": prompt[:500],
            "mode": gen_mode,
            "player": player_cast_lock()[:240],
        })
    except Exception:
        pass

    if ref_path:
        try:
            Path(ref_path).unlink(missing_ok=True)
            small = Path(ref_path).with_name(Path(ref_path).name.replace(".png", "_small.png"))
            if small.exists():
                small.unlink(missing_ok=True)
        except Exception:
            pass

    hold = brief.get("place_hold") or place.get("place_hold") or ""
    realtime = (
        f"{brief['character']['label']} holds in this exact place. {hold} "
        "The confrontation breathes: dust, light, weight shifting. "
        "Camera holds. No HUD, no captions."
    )
    return jsonify({
        "encounter": {
            "character": brief["character"],
            "danger": brief["danger"],
            "stakes": brief["stakes"],
            "place_hold": hold,
        },
        "plate_url": web,
        "choices": choices,
        "mode": gen_mode,
        "prompt": realtime,
        "plate_prompt": prompt[:400],
        "music_prompt": encounter_music_prompt(brief),
        "stingers": _encounter_stinger_urls(),
    })


def _generate_resolve_plate(session_id: str, brief: dict, prompt: str,
                            ref_path: Optional[str], verb: str = "",
                            lane: str = "", outcome: str = "") -> tuple[Optional[str], str]:
    """Hard-cut verb still. Style swatch / identity only — never the enter plate."""
    import engine
    if not getattr(engine, "IMAGE_ENABLED", True):
        return None, "disabled"
    tod = ""
    try:
        tod = str((engine._load_state(session_id) or {}).get("time_of_day") or "")
    except Exception:
        tod = ""
    img_dir = engine._get_image_dir(session_id)
    label = ((brief or {}).get("character") or {}).get("label") or "encounter"
    place_ctx = (brief or {}).get("place_hold") or ""
    caption = resolve_plate_caption(label, verb, lane, outcome)
    t0 = time.time()
    image_path = None
    gen_mode = "none"
    try:
        from gemini_image_utils import (
            generate_gemini_img2img, generate_with_gemini, make_style_swatch,
        )
        # The standoff plate goes in as a real reference, not a style swatch.
        # make_style_swatch() throws away everything except the palette, so
        # the punch was generated from TEXT alone: new faces, new clothes, and
        # an indoor shed where the standoff had been an outdoor yard. The
        # prompt already forbids re-posing the standoff, so the reference can
        # carry identity and place while the wording moves the camera.
        plate = str(ref_path) if ref_path and Path(str(ref_path)).exists() else ""
        identity = encounter_identity_paths()
        refs = ([plate] if plate else []) + [p for p in identity[:2] if p != plate]
        if refs:
            image_path = generate_gemini_img2img(
                prompt=prompt,
                caption=caption,
                reference_image_path=refs,
                strength=ENCOUNTER_RESOLVE_STRENGTH,
                world_prompt=place_ctx[:200] if place_ctx else None,
                time_of_day=tod,
                hd_mode=False,
                output_dir=Path(img_dir),
                include_people=True,
                hold_cast=True,
                style_only_swatch=False,
                identity_paths=identity or None,
                identity_seed=bool(identity),
            )
            gen_mode = "hard_cut_plate" if plate else "hard_cut_identity"
        if not image_path:
            image_path = generate_with_gemini(
                prompt=prompt,
                caption=caption,
                world_prompt=place_ctx[:200] if place_ctx else None,
                time_of_day=tod,
                hd_mode=False,
                output_dir=Path(img_dir),
            )
            gen_mode = "hard_cut_t2i"
    except Exception as gen_err:
        try:
            engine.log_error(f"[ENCOUNTER] resolve generate failed: {gen_err}")
        except Exception:
            pass
        try:
            engine.cost_tracker.record_usage(
                session_id, "image", "gemini", "encounter_resolve",
                operation="encounter_resolve", output_units=0, unit_type="images",
                latency_ms=int((time.time() - t0) * 1000), success=False,
                error_message=str(gen_err)[:200],
            )
        except Exception:
            pass
        return None, "failed"

    web_ok = bool(image_path)
    try:
        engine.cost_tracker.record_usage(
            session_id, "image", "gemini", "encounter_resolve",
            operation=f"encounter_resolve_{gen_mode}",
            output_units=1.0 if web_ok else 0, unit_type="images",
            latency_ms=int((time.time() - t0) * 1000), success=web_ok,
            error_message=None if web_ok else "no_image_returned",
        )
    except Exception:
        pass
    return image_path, gen_mode


def api_resolve():
    """POST /api/encounter/resolve — play out the verb, then the real turn.

    Body: ``{choice, lane}``. Returns a hard-cut resolve still. The world
    then evolves through ``_process_turn_background``. Survive / wounded /
    die keep that still (``skip_image=True``). Escape generates a new
    world frame so the punch does not become the walkable yard.
    Survive / wounded stay locked and wait for that dispatch. Escape / die
    run it in the background. Do not also POST ``/api/choose``.
    """
    from flask import jsonify, request
    import engine
    import threading

    if engine._rate_limited("encounter_resolve", 2.0):
        return jsonify({"error": "slow_down"}), 429
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id") or engine._resolve_request_session_id()
    posted_text = str(data.get("choice") or data.get("text") or "").strip()
    posted_lane = str(data.get("lane") or "").strip().lower()

    st = engine._load_state(session_id) or {}
    enc = st.get("encounter") if isinstance(st.get("encounter"), dict) else None
    if not enc:
        return jsonify({"error": "no_encounter"}), 409
    if not (st.get("player_state") or {}).get("alive", True):
        return jsonify({"error": "game_over"}), 409
    cached_url = enc.get("resolve_url")
    rec = st.get("encounter_outcome") if isinstance(st.get("encounter_outcome"), dict) else {}
    same_verb = posted_text and (rec.get("verb") or "").casefold() == posted_text.casefold()
    if st.get("encounter_resolving") and cached_url and same_verb:
        return jsonify({
            "resolve_url": cached_url,
            "prompt": enc.get("resolve_prompt") or "",
            "outcome": rec.get("outcome"),
            "lane": rec.get("lane"),
            "verb": rec.get("verb") or posted_text,
            "released": encounter_releases(rec.get("outcome"), rec),
            "choices": structure_encounter_choices(enc.get("choices") or []),
            "dispatch": rec.get("dispatch") or "",
            "cached": True,
        })
    if st.get("encounter_resolving"):
        return jsonify({"error": "already_resolving"}), 409
    if not posted_text:
        return jsonify({"error": "missing_choice"}), 400

    verb, lane = match_encounter_choice(posted_text, posted_lane, enc.get("choices"))
    brief = normalize_encounter_brief(enc)
    brief["choices"] = structure_encounter_choices(
        enc.get("choices") or fallback_encounter_choices(brief)
    )
    brief["plate_url"] = enc.get("plate_url")
    brief["plate_path"] = enc.get("plate_path")
    brief["place_hold"] = brief.get("place_hold") or enc.get("place_hold") or ""
    ps = st.get("player_state") or {}
    condition = str(ps.get("condition") or "ok").strip().lower()
    if condition not in ENCOUNTER_CONDITIONS:
        condition = "ok"
    stance = (brief.get("character") or {}).get("stance") or "hostile"
    kind = (brief.get("character") or {}).get("kind") or "person"

    with engine.WORLD_STATE_LOCK:
        locked = engine._load_state(session_id) or {}
        if locked.get("encounter_resolving"):
            return jsonify({"error": "already_resolving"}), 409
        locked["encounter_resolving"] = True
        engine._save_state(locked, session_id)
        engine._sync_ambient_state(locked, session_id)

    prev_enemy = str(enc.get("enemy_state") or "ready").strip().lower()
    if prev_enemy not in ENCOUNTER_ENEMY_STATES:
        prev_enemy = "ready"
    round_no = max(1, int(enc.get("round_no") or 1))
    rolled = roll_encounter_outcome(
        lane, stance=stance, kind=kind, condition=condition,
        fate=str(st.get("fate") or "NORMAL"),
        enemy_state=prev_enemy, round_no=round_no,
    )
    record = {
        "outcome": rolled["outcome"],
        "alive": rolled["alive"],
        "condition": rolled["condition"],
        "lane": lane,
        "verb": verb,
        "enemy_state": rolled["enemy_state"],
        "round_no": round_no,
    }
    brief["enemy_state"] = rolled["enemy_state"]
    brief["round_no"] = round_no
    brief["stakes"] = stakes_after_verb(brief, verb, lane, rolled["outcome"])
    prompt = build_encounter_resolve_prompt(
        brief, verb, lane, rolled["outcome"],
        setting=enc.get("setting") or brief.get("place_hold") or "",
    )
    ref_path = _confrontation_plate_path(session_id, enc)
    image_path, gen_mode = _generate_resolve_plate(
        session_id, brief, prompt, ref_path,
        verb=verb, lane=lane, outcome=rolled["outcome"],
    )
    web = engine._to_web_image_url(image_path, session_id) if image_path else None
    if not web:
        # Offline / disabled image: the confrontation plate IS the play-out
        # still so the Moment can complete. A failed live gen aborts.
        if gen_mode == "disabled":
            web = enc.get("plate_url")
            image_path = ref_path
        else:
            _clear_encounter_resolving(session_id)
            return jsonify({"error": "no_plate"}), 502

    hold = brief.get("place_hold") or ""
    realtime = (
        f"{verb}. {brief['character']['label']} in this exact place. {hold} "
        "The action has already happened. Camera holds. No HUD, no captions."
    )
    brief["resolve_url"] = web
    brief["resolve_prompt"] = realtime
    _pin_encounter_resolve(session_id, image_path, web, brief, record)
    try:
        import play_log
        play_log.record("encounter_resolve", session_id, {
            **play_log.diagnose_encounter(brief, brief.get("choices")),
            "verb": verb,
            "lane": lane,
            "outcome": rolled["outcome"],
            "resolve_prompt": prompt[:600],
            "plate_seen": (brief.get("plate_seen") or enc.get("plate_seen") or "")[:220],
            "player": player_cast_lock()[:240],
        })
    except Exception:
        pass

    released = encounter_releases(rolled["outcome"], record)
    next_choices = []
    dispatch = ""
    turn_text = encounter_action_for_turn(
        verb, brief, lane=lane, outcome=rolled["outcome"],
    )
    subject = (brief.get("character") or {}).get("label") or ""

    player_action_item = engine.create_feed_item(
        type="player_action",
        content=verb,
        metadata={
            "raw_choice": verb,
            "source": "encounter",
            "lane": lane,
            "outcome": rolled["outcome"],
            "released": released,
        },
    )
    with engine.WORLD_STATE_LOCK:
        st = engine._load_state(session_id) or {}
        engine._feed_append(st, player_action_item)
        st["last_choice"] = verb
        st["drift_count"] = 0
        engine._save_state(st, session_id)
        engine._sync_ambient_state(st, session_id)

    # Object permanence should hold the other person in frame only while the
    # fight is still on. Pinning them through the release is why breaking away
    # was followed by several turns of the camera trailing the person you just
    # escaped from, or standing over the one you put down.
    turn_kwargs = {
        "source": "encounter",
        "session_id": session_id,
        "subject": None if released else engine._permanence_subject(subject),
        "skip_image": encounter_turn_skip_image(rolled["outcome"]),
    }
    if released:
        try:
            thread = threading.Thread(
                target=engine._process_turn_background,
                args=(turn_text, player_action_item["id"], None),
                kwargs=turn_kwargs,
                daemon=True,
            )
            thread.start()
        except Exception as err:
            _clear_encounter_resolving(session_id)
            try:
                engine.log_error(f"[ENCOUNTER] resolve thread failed: {err}")
            except Exception:
                pass
            return jsonify({"error": "thread_failed"}), 500
    else:
        # Same pipeline as MOVE TO / typed choose — the HTTP response
        # needs the engine dispatch + next slate, so this beat is sync.
        try:
            result = engine._process_turn_background(
                turn_text, player_action_item["id"], None,
                pacing=False, **turn_kwargs,
            ) or {}
        except Exception as err:
            _clear_encounter_resolving(session_id)
            try:
                engine.log_error(f"[ENCOUNTER] hold turn failed: {err}")
            except Exception:
                pass
            return jsonify({"error": "turn_failed"}), 500
        dispatch = result.get("dispatch") or ""
        next_choices = result.get("choices") or []
        if result.get("released"):
            released = True
        if result.get("danger"):
            brief["danger"] = result.get("danger")
        if result.get("stakes"):
            brief["stakes"] = result.get("stakes")

    return jsonify({
        "resolve_url": web,
        "prompt": realtime,
        "outcome": rolled["outcome"],
        "lane": lane,
        "verb": verb,
        "alive": rolled["alive"],
        "condition": rolled["condition"],
        "released": released,
        "choices": next_choices,
        "dispatch": dispatch,
        "danger": brief.get("danger") or "",
        "stakes": brief.get("stakes") or "",
        "mode": gen_mode,
        "music_prompt": encounter_music_prompt(brief),
    })


def api_travel():
    """POST /api/encounter/travel — count walking time toward the next encounter.

    Body: ``{dt: seconds}`` of translation (forward/back/strafe). Looking
    around must send nothing, or ``dt: 0``. When the clock hits zero this
    returns ``{fire: true}`` — the client then captures a frame and begins.
    """
    from flask import jsonify, request
    import engine

    if engine._rate_limited("encounter_travel", 0.35):
        return jsonify({"fire": False, "reason": "slow_down"})
    data = request.get_json(silent=True) or {}
    session_id = engine._resolve_request_session_id()
    try:
        dt = float(data.get("dt") or 0)
    except (TypeError, ValueError):
        dt = 0.0
    with engine.WORLD_STATE_LOCK:
        st = engine._load_state(session_id) or {}
        fire, reason, remain = apply_travel(st, dt)
        engine._save_state(st, session_id)
        engine._sync_ambient_state(st, session_id)
    return jsonify({
        "fire": bool(fire),
        "reason": reason,
        "remain": remain,
    })


def api_roll():
    """Back-compat alias: treat a roll as a travel pulse if ``dt`` is sent."""
    return api_travel()
