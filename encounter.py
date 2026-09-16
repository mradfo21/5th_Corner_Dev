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
from typing import Any, Dict, Optional

ENCOUNTER_STANCES = ("hostile", "desperate", "opportunistic")
# Anything outside this list was silently rewritten to "person", so a model that
# did answer "anomaly" or "group" had its answer thrown away before any prompt
# saw it — one more reason every encounter ended up being a man.
ENCOUNTER_KINDS = ("person", "group", "creature", "anomaly", "character")
# Three different ANSWERS to a confrontation, not three fighting moves. The
# third lane used to be "use" — turn their grip or their weapon against them —
# which is just a second way to hit someone, so every slate came out as three
# variations on scuffling and none of them felt like a decision.
ENCOUNTER_LANES = ("confront", "evade", "parley")
ENCOUNTER_OUTCOMES = ("survive", "escape", "wounded", "die")
ENCOUNTER_CONDITIONS = ("ok", "wounded")
# Where the other body is in the exchange. Without this the fight had no
# memory: every round was the same independent roll against the same standoff,
# so committing to a verb could not change the situation, only repeat it or
# end the run. "down" is the win the player previously had no way to reach —
# confront could only loop (50% survive, nothing changes) or kill you.
# "standing_down" is that same ending reached without a blow: the parley
# landed and they are no longer coming. Both settle the encounter.
ENCOUNTER_ENEMY_STATES = ("ready", "staggered", "down", "standing_down")
ENCOUNTER_ENEMY_SETTLED = ("down", "standing_down")

ENCOUNTER_PLATE_STYLE_ANCHOR = os.getenv(
    "ENCOUNTER_PLATE_STYLE_ANCHOR",
    # This used to order "the same place restaged from a NEW LENS", which is
    # the opposite of what entering a fight wants. The plate is handed the
    # previous frame as a reference and then told to re-shoot it from
    # somewhere else, so an encounter looked like cutting to a different
    # production. It is the next shot in the same sequence: same camera, same
    # stock, a beat later, with someone now in the way.
    "the next frame of this same sequence, seconds later — same camera, same "
    "lens, same film stock, analog-horror 1993 muted palette, subtle grain. "
    "The air has gone still: the moment before violence, not violence itself",
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
# Every "is this describing a body?" check in the module runs off this list, and
# a noun missing from it means the description gets thrown away as camera talk.
# It held eleven words, all of them a lone civilian human, so a soldier, a pack
# animal or a standing shape was unreadable to the whole module — which mattered
# the moment the roster stopped producing only men in work shirts.
_PERSON_NOUNS = (
    "woman", "man", "men", "women", "people", "worker", "miner", "guard",
    "figure", "stranger", "person", "someone", "creature", "presence",
    "soldier", "trooper", "officer", "scavenger", "animal", "beast", "dog",
    "thing", "shape", "silhouette", "body", "child", "crew", "team",
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
        "motive": {"type": "string"},
        "danger": {"type": "string"},
        "stakes": {"type": "string"},
        "place_hold": {"type": "string"},
    },
    "required": ["character", "motive", "danger", "stakes"],
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
    "Exactly 3 choices, and they are three DIFFERENT ANSWERS to this "
    "moment — not three ways to scuffle:\n"
    "(1) violence — ONE committed, extreme act, with the body or with "
    "something already in the photograph. Never a shove, a grapple, or a "
    "try, and never a weapon the player was not shown holding.\n"
    "(2) escape — leave. Break contact and get out of reach, at a cost.\n"
    "(3) peace — end it with no blow struck. Answer what they came for, "
    "give something up, say the thing that stops them.\n"
    "No Attack/Defend/Item. No observe/wait/photograph. 3-6 words."
)

DEFAULT_CHOICE_INSTRUCTIONS = (
    "This is a confrontation still. A person or creature is already in the "
    "player's face. Write exactly three short actions about THAT figure — "
    "not the place.\n"
    "Return JSON only with keys confront, evade, parley. Each is 3-6 words.\n"
    # Three verbs from the same fistfight is what made every slate feel
    # like no decision at all. These are three different ways the next
    # minute goes, and the player should not be able to have two of them.
    "These are three DIFFERENT ANSWERS. If all three could happen in the "
    "same scuffle, all three are wrong.\n"
    "confront: ONE extreme, committed act of violence — the thing that "
    "cannot be undone. Never shove, scuffle, grapple, wrestle, or 'try to'. "
    "Use the player's body or something already visible in the photograph; "
    "do not arm them with a weapon they were never shown holding.\n"
    "evade: leave. Break contact and get out of their reach, at a cost.\n"
    # The only slot where the player can act on WHY this is happening,
    # which is what makes a fight part of the story and not an obstacle.
    "parley: end it with no blow struck — answer what they came for, hand "
    "something over, or say the thing that stops them. Name it.\n"
    "Name the figure if you can see them. Never name a fence, ridge, door, "
    "pipe, hatch, mesa, or shed unless you use it on THEM.\n"
    "No observe, wait, photograph, climb, or walk-to."
)

DEFAULT_BRIEF_INSTRUCTIONS = (
    "Invent ONE new encounter for the attached image of the place the player is in.\n"
    "Return JSON only.\n"
    "\n"
    "THIS MUST COME OUT OF THE STORY, NOT OUT OF NOWHERE. Read the world and "
    "the recent beats above. Whoever arrives belongs to something already "
    "named there — a faction, an operation, an event, a person already "
    "mentioned, the thing the player has been digging into. They arrive "
    "because of what the player has been doing. A stranger with no "
    "connection to any of it is the one answer that is always wrong.\n"
    "\n"
    "motive is what they WANT, in one clause, and it must be specific to "
    "this story: something to take, something to stop, something to protect, "
    "something to find out, something to settle. 'They are hostile' is not a "
    "motive. The best encounters are ones where the player realises the "
    "other person knows something.\n"
    "\n"
    "label is a grounded reading of what this is, tied to the world — its job, "
    "allegiance, or condition (a Horizon site foreman, a quarantine sentry, a "
    "scavenger who got too close, whatever came up out of Shaft 6). The test "
    "is not the noun, it is the camera: 1993, available light, practical "
    "effects. Anything a 35mm frame could physically catch is fair — people, "
    "raid teams, animals, the changed, an anomaly that has a shape. Nothing "
    "that needs CGI, a glow, an energy weapon, or a comic-book costume.\n"
    "look is the visible body: hair, clothes, hide, wound, what it holds.\n"
    "\n"
    "DANGER is what makes this the wrong moment to be standing here, and it "
    "is happening NOW. Usually that is what this thing is doing with what it "
    "has — its body, its hands, what it carries, what it arrived in. It may "
    "also be what its arrival means: who is behind it, what it is about to "
    "do, what it will take. Do not invent a collapse, fire, grate, or sealed "
    "exit that the photograph does not already show.\n"
    "\n"
    "If the image is outdoors, stay outdoors. Do not invent an interior.\n"
    "If the image is indoors, stay in that same room.\n"
    "Stakes are one second-person sentence about what the player loses if "
    "they hesitate — and losing the thread of the story is a real loss here, "
    "not only losing blood. place_hold is 1-2 visual locks from the image so "
    "a restage cannot teleport (ground, sky, walls, a landmark).\n"
    "stance must be one of: hostile, desperate, opportunistic.\n"
    "kind must be one of: person, group, creature, anomaly, character."
)

ENCOUNTER_CHOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "confront": {"type": "string"},
        "evade": {"type": "string"},
        "parley": {"type": "string"},
    },
    "required": ["confront", "evade", "parley"],
}

# ── What can walk up on you in this world ────────────────────────────────────
# The brief asks a model, cold, to "invent ONE new encounter", and a model asked
# the same question every time answers with its most probable answer every time.
# In a world of raid teams, changed miners and anomalies, that answer was one
# more rough man in a work shirt, turn after turn. Temperature does not move it:
# the modal answer stays modal, and the two examples the instructions happened
# to give ("a wounded worker, a man in coveralls") were the whole ballgame.
#
# So the KIND is rolled in code before the model is asked anything, and the
# model only dresses the roll into the photograph. The list rolled from is read
# out of the run's own lore instead of being a table shipped in this file: a
# table here goes stale the moment somebody rewrites the bible, and the point is
# that changing the lore changes what hunts you.
ENCOUNTER_ROSTER_SIZE = 12

# How many rolls a kind sits out before it can come back. A world's signature
# threat should still recur inside a long run; what it must not do is arrive
# twice running, which is the thing that reads as staleness.
ENCOUNTER_ROSTER_COOLDOWN = 5

ENCOUNTER_LANE_SCHEMA = {
    "type": "object",
    "properties": {
        "lane": {"type": "string", "enum": list(ENCOUNTER_LANES)},
    },
    "required": ["lane"],
}

ENCOUNTER_ROSTER_SCHEMA = {
    "type": "object",
    "properties": {
        "kinds": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["kinds"],
}

DEFAULT_ROSTER_INSTRUCTIONS = (
    "Read this world bible and answer one question: what could walk up on a "
    f"lone photojournalist here? List exactly {ENCOUNTER_ROSTER_SIZE} "
    "DIFFERENT answers.\n"
    "\n"
    "Each answer is a short noun phrase, at most 12 words, naming WHAT arrives "
    "— and where the bible gives you one, the faction, operation or event it "
    "belongs to.\n"
    "\n"
    "SPREAD THEM. Twelve variations on one idea is a failed list. Across the "
    "twelve, cover: an organised armed group; a lone human with a job and an "
    "allegiance; someone this place has already changed or infected; an animal "
    "or a creature; a paranormal anomaly; and a rival who wants the story "
    "rather than the player's blood.\n"
    "\n"
    "Everything must come out of THIS bible — its factions, its accident, its "
    "industry, what it says has been happening here. Generic is the failure "
    "mode: 'a hostile stranger', 'an angry local', 'a mysterious figure' are "
    "the answers to delete.\n"
    "\n"
    "1993, practical effects, available light: whatever you name has to be "
    "something a camera could physically catch. No energy weapons, no CGI, no "
    "comic-book costumes.\n"
    "Each one must be able to arrive on open ground, on foot, without needing "
    "an interior or a prop that may not be in the frame.\n"
    "\n"
    "Return JSON only: {\"kinds\": [\"...\", \"...\"]}"
)

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


def encounter_turn_skip_image(outcome: str, record: Optional[dict] = None) -> bool:
    """Whether the aftermath turn reuses the verb still instead of drawing.

    A fight that is still on keeps the verb still: the player is mid-exchange
    and a fresh world frame there would throw away the blow they just landed.

    A fight that is OVER needs a new frame, and winning one is over. Only
    escape used to qualify, so putting the other body down left the game
    resuming on the standoff plate — the picture of the fight you had already
    finished, held there while normal play carried on around it. Any release
    now draws the place a moment later, with the fight's result in it.

    Death is the exception in the other direction: the killing frame IS the
    last thing the run has to show, so nothing is drawn over it.
    """
    out = str(outcome or "").strip().lower()
    if out == "die":
        return True
    return not encounter_releases(out, record)


def stakes_after_verb(brief: dict, verb: str, lane: str, outcome: str) -> str:
    """Stakes name the verb that just landed, not the enter-brief sludge line."""
    label = _clip(((brief or {}).get("character") or {}).get("label"), "them", 24)
    if _is_clothing_clause_label(label):
        label = "them"
    verb_s = _clip(verb, "the move", 40).rstrip(".")
    out = str(outcome or "").strip().lower()
    lane_s = str(lane or "").strip().lower()
    if out == "escape":
        # Handing something over and walking is not breaking clear, and
        # reporting it as a scramble argued with the frame above it.
        if lane_s == "parley":
            return f"{label} let you go. Keep moving."
        return f"You broke clear of {label}. Keep moving."
    if out == "wounded":
        return f"{verb_s} cost you — {label} is still in reach."
    if out == "die":
        return f"{label} has you."
    # Without these, settling an encounter still reported "still here" and the
    # stakes line argued with the frame that had just shown them giving up.
    state = str((brief or {}).get("enemy_state") or "").strip().lower()
    if state == "standing_down":
        return f"{label} stood down. Nobody swung."
    if state == "down":
        return f"{label} is down. You have the ground."
    return f"{verb_s} landed. {label} is still here."


def player_wardrobe_text() -> str:
    try:
        import game_identity
        char = game_identity.authored_character()
        return str((char or {}).get("wardrobe") or "").strip()
    except Exception:
        return ""


# Garments worth protecting. Scenery words are deliberately absent: learning
# the player's look from a whole frame description would drag the yard in
# with the clothes, and then every later description of the same place would
# look like it was wearing the player's outfit.
_GARMENT_NOUNS = (
    "windbreaker", "balaclava", "waistcoat", "coveralls", "respirator",
    "raincoat", "bandana", "overalls", "fatigues", "backpack", "coverall",
    "hoodie", "flannel", "sweater", "uniform", "goggles", "harness",
    "holster", "beanie", "helmet", "jacket", "poncho", "gaiter", "gloves",
    "jumper", "trousers", "anorak", "armour", "apron", "boots", "shirt",
    "scarf", "smock", "parka", "plaid", "denim", "jeans", "pants", "armor",
    "tunic", "coat", "hood", "mask", "vest", "belt", "cap", "hat", "tee",
)

_GARMENT_RE = re.compile(
    r"\b((?:[a-z][a-z'-]+\s+){0,3})(" + "|".join(_GARMENT_NOUNS) + r")\b",
    re.I,
)

# Words that sit in front of a garment without identifying it.
_GARMENT_FILLER = frozenset({
    "and", "with", "the", "his", "her", "their", "its", "one", "two", "over",
    "under", "wearing", "wears", "worn", "dressed", "man", "woman", "person",
    "figure", "guy", "other", "second", "another", "same", "who", "that",
    "this", "they", "them", "both", "also", "still", "now", "left", "right",
    "foreground", "background", "front", "behind", "near", "stands",
    "standing", "holding", "carrying", "wearing", "some", "sort", "kind",
})


def _look_words(text: str) -> set:
    """Distinctive words in a description, minus the ones everybody shares.

    Garments alone cannot separate two people who are both wearing a hat:
    "an older man in a plaid shirt" and "a man in a baseball cap" tie on
    garment count, and the tie went to whoever was described first — the
    player. Age, build, and condition words break that tie.
    """
    return {
        w for w in re.split(r"[^a-z0-9'-]+", str(text or "").lower())
        if len(w) > 3 and w not in _GENERIC_LOOK_WORDS
        and w not in _GARMENT_FILLER and w not in _WARDROBE_STOP
    }


def wardrobe_tokens_from_text(text: str) -> set:
    """Garment words, plus the adjectives actually attached to them.

    Only what hangs off a garment noun is kept, so "a man in a green vest
    beside a chain-link fence" yields {green, vest} and not the fence.
    """
    out = set()
    for lead, garment in _GARMENT_RE.findall(str(text or "").lower()):
        out.add(garment)
        for w in re.split(r"[^a-z'-]+", lead):
            w = w.strip("'-")
            if len(w) > 2 and w not in _GARMENT_FILLER and w not in _WARDROBE_STOP:
                out.add(w)
    return out


# The session whose frames describe the player right now. Every clone guard
# in this module is called from code that has no session id to hand, so the
# encounter entry points set this once and the guards read it.
_LOOK_SESSION = "default"
# Every clone guard consults the observed look, and a single plate prompt
# runs them dozens of times, so the history read behind it is cached for a
# few seconds rather than hit once per call.
_LOOK_READ_CACHE: Dict[str, tuple] = {}
_LOOK_TOKEN_CACHE: Dict[str, set] = {}
_LOOK_TTL = 5.0


def set_look_session(session_id: str) -> None:
    """Point the wardrobe guards at the session currently being played."""
    global _LOOK_SESSION
    if session_id:
        _LOOK_SESSION = str(session_id)


def _read_player_look(session_id: str) -> str:
    """Newest frame description that can only be describing the player."""
    try:
        import engine
        hist = engine._load_history(session_id) or []
    except Exception:
        return ""
    for entry in reversed(hist):
        if not isinstance(entry, dict):
            continue
        # Encounter frames hold two people and cannot say which is which.
        if entry.get("encounter") or entry.get("choice") == "__encounter_resolve__":
            continue
        # `vision_analysis` is the only field here that is an observation of
        # the rendered frame. `vision_dispatch` next to it is the character
        # sheet restated as prose, so it always agrees with the sheet and can
        # never show that the drawing has drifted away from it.
        desc = str(entry.get("vision_analysis") or entry.get("description")
                   or entry.get("caption") or "").strip()
        if desc:
            return desc
    return ""


def observed_player_look(session_id: str = "") -> str:
    """How the player is being DRAWN, from the last frame that is only them.

    An exploration frame is the one place the protagonist appears alone, so
    a description of it is unambiguously their appearance.
    """
    sid = session_id or _LOOK_SESSION
    hit = _LOOK_READ_CACHE.get(sid)
    now = time.time()
    if hit and hit[0] > now:
        return hit[1]
    desc = _read_player_look(sid)
    _LOOK_READ_CACHE[sid] = (now + _LOOK_TTL, desc)
    return desc


def observed_player_tokens(session_id: str = "") -> set:
    """Wardrobe words the player is actually wearing on screen."""
    desc = observed_player_look(session_id or _LOOK_SESSION)
    if not desc:
        return set()
    if desc not in _LOOK_TOKEN_CACHE:
        _LOOK_TOKEN_CACHE[desc] = wardrobe_tokens_from_text(desc)
    return _LOOK_TOKEN_CACHE[desc]


def observed_player_wardrobe(session_id: str = "") -> str:
    """A short phrase naming what the player has on in the current frame.

    Assembled from the garment matches rather than sliced out of the
    sentence, because an unpunctuated description runs straight past the
    clothes ("a green vest and a dark cap stands in a dirt yard").
    """
    desc = observed_player_look(session_id)
    if not desc:
        return ""
    phrases = []
    for lead, garment in _GARMENT_RE.findall(desc.lower()):
        mods = [w for w in re.split(r"[^a-z'-]+", lead)
                if len(w) > 2 and w not in _GARMENT_FILLER
                and w not in _WARDROBE_STOP]
        phrase = " ".join(mods[-2:] + [garment])
        if phrase not in phrases:
            phrases.append(phrase)
    if not phrases:
        return ""
    phrases = phrases[:3]
    joined = (phrases[0] if len(phrases) == 1
              else ", ".join(phrases[:-1]) + " and " + phrases[-1])
    return _clip(joined, "", 90)


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
    # The sheet is what the player was SPECIFIED as; the frames are what they
    # get DRAWN as, and the two drift. A protagonist written as "olive field
    # jacket" who is rendered in a green vest and cap owns a vest and a cap as
    # far as the next frame is concerned, and nothing here knew that — so that
    # outfit was unguarded and the enemy was free to inherit it.
    return tokens | observed_player_tokens()


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
    # "camcorder" stays for worlds that still author one, but it is no longer
    # the shipped protagonist's gear — the shipped sheet now carries a 35mm
    # stills camera, so "35mm" is the token that identifies them on sight.
    if words & owned & {"press", "gaiter", "respirator", "camcorder", "35mm"}:
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


def distinct_enemy_look(look: str, seed: str = "", kind: str = "") -> str:
    """Keep a stranger look that cannot be read as the player's vest.

    Also the last gate before a look reaches the image prompt, so it is where
    camera narration has to die: the plate prompt drops this string in as the
    other person's appearance, and a look that still says "a first-person
    perspective shows…" re-aims the whole frame instead of dressing anybody.

    ``seed`` / ``kind`` only matter when we fall through to a fallback — see
    default_stranger_look. Callers holding a brief should pass the label and
    the kind so the same encounter keeps meeting the same thing.
    """
    if look_is_camera_language(look):
        return default_stranger_look(seed or look, kind)
    raw = _clip(strip_camera_language(look), "", 160)
    if raw and not look_clones_player(raw):
        # "A first-person view shows a man" survives the camera-language check
        # and then strips down to "a man", which dresses nobody: no garment,
        # no distinguishing word. Handed to the image model that is a blank
        # cheque, and what comes back is the generic rugged stranger. A look
        # that describes nothing is worth less than a fallback that does.
        if wardrobe_tokens_from_text(raw) or _look_words(raw):
            return raw
    return default_stranger_look(seed or look, kind)


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
        # "With their BODY" was written to stop the brief inventing a burst pipe
        # or a fire the frame never had. It also quietly disarmed everything that
        # is not a pair of fists: a raid team's rifles, a creature's teeth, what
        # an anomaly does to the air. What they BROUGHT is theirs; what the place
        # would have to supply is not.
        "danger is what THIS THING is doing to the player right now, with its "
        "body or with what it brought with it. Do not invent sludge, fire, "
        "collapse, oil, or a prop the attached photograph does not already "
        "show and it did not carry in.\n",
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
    look = distinct_enemy_look(
        char.get("look") or "",
        seed=char.get("label") or "",
        kind=char.get("kind") or "",
    )
    char["look"] = look
    if look_clones_player(char.get("label") or ""):
        char["label"] = _grounded_label_from_look(look)
    if look_clones_player(char.get("locked_look") or ""):
        char["locked_look"] = look
    return ground_danger_to_visible(brief)


def _looks_like_sheet(seen: str, wardrobe: str) -> bool:
    """True when the observed outfit is just the sheet restated.

    No point spending prompt on "in the reference they are wearing an olive
    field jacket" when the line above already said olive field jacket.
    """
    a = wardrobe_tokens_from_text(seen)
    b = wardrobe_tokens_from_text(wardrobe)
    return bool(a) and a.issubset(b)


def player_cast_lock(trust_reference: bool = False) -> str:
    """Hard identity sentence so img2img cannot recast the protagonist.

    ``trust_reference`` is for the one caller whose attached frame is known to
    be the player's own scene: the standoff plate. It already says CARRY THE
    PLAYER OVER — copy the face and clothes out of that photograph — and this
    lock's closing sentence said the opposite ("a previous frame that shows
    someone else is WRONG — ignore that person"). Two hard rules about the same
    body, so the model was free to pick, and what came back was the sheet
    loosely re-imagined: right man, wrong coat. That sentence exists for a
    RECAST (the leftover Jason still on disk after the author drew a woman),
    which is not the situation when the reference is this run's own last frame.
    """
    try:
        import game_identity
        who = game_identity.protagonist_line()
        name = game_identity.display_name()
        if not who:
            return ""
        wardrobe = player_wardrobe_text()
        exclusive = ""
        if wardrobe:
            # This used to end "must not wear that vest, cap, or PRESS gear",
            # hardcoded from one long-gone protagonist. For a character sheet
            # that says "olive field jacket" it introduces a vest and a cap
            # the world does not have — and naming a garment, even to ban it,
            # is how it ends up in the picture. Ban the outfit that actually
            # exists instead.
            exclusive = (
                f" ONLY {name} wears that outfit ({wardrobe}). "
                f"Nobody else in the frame wears any part of it — give the "
                f"other person different garments in different colours."
            )
        # What the sheet says and what the last frame drew are not the same
        # thing, and it is the drawn version the next frame will copy. Name
        # it, or the model hands the player's clothes to the stranger.
        seen = observed_player_wardrobe()
        if seen and not _looks_like_sheet(seen, wardrobe):
            exclusive += (
                f" In the reference photograph {name} is wearing {seen}; "
                f"that stays on {name} and goes on nobody else."
            )
        prior = (
            f" The person already in the reference photograph IS {name}: keep "
            "that face and that outfit."
            if trust_reference else
            " A previous frame that shows someone else is WRONG — ignore that "
            "person."
        )
        return (
            f"CAST LOCK — HARD. The player is {who} "
            f"Draw {name} as that exact person: same gender, face, hair, "
            "build, and clothes. Do not recast them as a different sex or face."
            f"{prior}"
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


# Word boundaries matter here: a substring test finds "her" inside "other"
# and "he" inside "shed", so choices about the landscape read as choices
# about the person and sailed through the filter this guard exists for.
_THREAT_PRONOUNS = re.compile(
    r"\b(?:them|they|their|theirs|him|his|he|her|hers|she|"
    r"the\s+(?:figure|stranger|creature|presence|man|woman|kid|boy|girl))\b",
    re.I,
)

# One entry per lane. This list only ever held the grappling verbs of the
# old third lane, so a clean escape ("Sprint away dropping the bag") and a
# clean parley ("Toss him your camera bag") both looked like landscape
# choices and were swapped out for canned text — the player picked from
# three useless moves while the model's actual slate was thrown away.
_THREAT_VERBS = (
    "shove", "tackle", "strike", "grab", "lunge", "charge", "smash",
    "crack", "choke", "stab", "swing", "club", "beat", "crush",
    "put down", "drive into", "bring down", "shatter",
    "dodge", "evade", "break away", "slip past", "flee", "run",
    "sprint", "bolt", "scramble", "retreat", "back away", "get clear",
    "get out", "duck", "dive",
    "hand", "give", "offer", "tell", "say", "talk", "speak", "answer",
    "trade", "toss", "surrender", "stand down", "back down", "plead",
    "bargain", "explain", "warn", "promise", "show", "lower",
    "grip", "momentum",
)


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
    names_pronoun = bool(_THREAT_PRONOUNS.search(blob))
    lane_verb = any(w in blob for w in _THREAT_VERBS)
    place_only = any(m in blob for m in _PLACE_ONLY_MARKERS)
    if names_figure or names_pronoun:
        return True
    if lane_verb and not place_only:
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
    # A brief that came back without a look used to become "a wary human
    # figure", which is the blandest possible answer to a world that offers
    # Horizon security, cryptids and anomalies.
    #
    # The label comes first, though, because the model often puts the whole
    # description THERE and leaves look empty — and a fallback is only an
    # improvement when there is nothing to contradict. Measured in a run: a
    # brief labelled "A panicked facility whistleblower", whose danger line is
    # about them shoving a briefcase at you, drew "a Horizon Industries
    # perimeter guard in a mustard hazard suit" for its look. Vivid, and about
    # a different person than the rest of the brief.
    #
    # The pool pick is random rather than seeded: the brief is persisted right
    # after this, so the encounter keeps whatever it drew, and a seeded pick
    # would hand every look-less brief the same creature — they all arrive
    # with the same fallback label.
    generic_label = label.strip().lower() in (
        "a stranger", "a presence", "a creature", "a figure", "a group",
        "someone", "something",
    )
    look = _clip(
        char_in.get("look") or char_in.get("appearance")
        or ("" if generic_label else label),
        default_stranger_look(kind=kind), 160,
    )
    if _is_clothing_clause_label(label):
        label = _grounded_label_from_look(look, data.get("plate_seen") or "")
    danger = _clip(data.get("danger"), "something in this place can hurt you now", 140)
    motive = _clip(data.get("motive"), "", 120)
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
    # What they want. Carried on the brief so the choices, the prose and the
    # plate can all point at the same intention instead of each inventing
    # their own reason for the fight.
    if motive:
        out["motive"] = motive
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
# The fallback stranger: who shows up when neither the brief nor the plate
# gave a look worth keeping. This was ONE constant — "a weathered stranger in
# a torn work coat and knit cap" — so every encounter that fell back here met
# the same man in the same coat, and the fallback fires more often than it
# looks like it should (a camera-language look, a look that clones the player,
# a plate that described nobody). The world advertises Horizon security, body
# horror, cryptids, anomalies and military raids; the fallback should be the
# most interesting thing in the frame rather than the least.
#
# Keyed by ENCOUNTER_KINDS so a creature does not fall back to a man in a
# coat. `normalize_encounter_brief` already pins kind before the look is
# needed, and the lane odds read kind too (a creature confront is the lethal
# one), so a look that disagrees with its kind is a fight that reads wrong.
DEFAULT_STRANGER_LOOKS: Dict[str, tuple] = {
    "person": (
        "a Horizon Industries perimeter guard in a mustard hazard suit, face "
        "lost behind a fogged gas mask",
        "a shaft miner lacquered in red dust, helmet lamp still burning, eyes "
        "filmed over white",
        "a quarantine sentry in unmarked desert fatigues, respirator strapped "
        "tight, no insignia anywhere on them",
        "a woman in a bleached Horizon lab coat, both hands bandaged to the "
        "elbow, the sleeves stiff with something dried",
        "a drifter wrapped in stitched tarpaulin and copper wire, mouth hidden "
        "under a rag mask",
    ),
    "group": (
        "three Blackwood contractors in rusted riot gear, moving as one body, "
        "every visor turned the same way",
        "a survey crew in matching yellow slickers, standing far closer "
        "together than the space needs",
        "a knot of masked scavengers strung together at the wrist by a length "
        "of mine cable",
    ),
    "creature": (
        "an amorphous wolf-shaped thing, fur slicked into wet spines, walking "
        "on too many joints",
        "a cryptid of fused flesh and mine cable, more shoulders than a body "
        "should carry, no face to find on it",
        "a pack animal skinned back to the muscle, breathing through slits "
        "along its flank",
        "something tall and pale that has grown into a survey tripod, limb and "
        "steel no longer separable",
    ),
    "anomaly": (
        "a shape of heat and red dust that keeps almost resolving into a man "
        "and then losing it",
        "a hazard suit standing upright with nobody inside it, the visor full "
        "of slow moving dark",
        "a silhouette that copies your own posture a half-second late",
        "a seam of air under the mesa where the light bends wrong and the far "
        "fence repeats itself",
    ),
}
# A named character from this world is still a person as far as a look goes.
DEFAULT_STRANGER_LOOKS["character"] = DEFAULT_STRANGER_LOOKS["person"]


def default_stranger_look(seed: str = "", kind: str = "") -> str:
    """One of the fallback strangers, stable for a given seed.

    The pick has to be stable per encounter. This is the last gate before a
    look reaches an image prompt and it runs again for the standoff, for every
    play-out and for the choice slate, so a fresh random pick per call would
    recast the thing halfway through the fight. Seeded on something that does
    not change during an encounter (the label) it lands on the same one every
    time, and `random.Random` takes a string seed deterministically across
    processes, so the look survives a restart mid-run.
    """
    pool = DEFAULT_STRANGER_LOOKS.get(str(kind or "").strip().lower() or "person")
    if not pool:
        pool = tuple(l for looks in DEFAULT_STRANGER_LOOKS.values() for l in looks)
    key = " ".join(str(seed or "").lower().split())
    if key:
        return random.Random(key).choice(pool)
    return random.choice(pool)


def is_default_stranger_look(look: str) -> bool:
    """True when a look is one of the fallbacks rather than something seen.

    The caller that needs this is deciding whether the plate showed a usable
    stranger at all, so it has to recognise every fallback, not just the one
    constant this used to be.
    """
    raw = " ".join(str(look or "").lower().split())
    if not raw:
        return False
    return any(
        raw == " ".join(known.lower().split())
        for looks in DEFAULT_STRANGER_LOOKS.values()
        for known in looks
    )

# Where one person's description ends and the NEXT PERSON begins.
#
# Splitting on punctuation alone fails in both directions. It misses "a man
# in a green vest faces an older man in a plaid shirt", one comma-free
# sentence holding two people. And it over-cuts "The man has long, matted
# hair and a torn shirt" into "The man has long," — a stub that still
# contains a person noun, so it won the enemy's look and the stranger was
# locked to the phrase "The man has long". A boundary only counts when a new
# person is actually introduced after it.
_PERSON_SPLIT_RE = re.compile(
    r"(?:(?<=[,.;])\s+|\s+)"
    r"(?=(?:and|but|while|whilst|facing|faces|confronting|confronts|"
    r"opposite|beside|behind|across\s+from|in\s+front\s+of)?\s*"
    r"(?:a|an|the|another|one|second|other)\s+"
    r"(?:[a-z][a-z'-]+\s+){0,3}"
    r"(?:" + "|".join(_PERSON_NOUNS) + r")\b)",
    re.I,
)


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
    for clause in _PERSON_SPLIT_RE.split(raw):
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
    # The brief invented this stranger before any pixels existed, so it is
    # the best evidence for which of the described people is NOT the player.
    wanted = wardrobe_tokens_from_text(fallback)
    wanted_words = _look_words(fallback)
    owned = player_wardrobe_tokens()
    candidates = []
    for clause in _PERSON_SPLIT_RE.split(stripped):
        # Splitting before "faces" / "and another" leaves the connector at
        # the head of the clause, so strip a whole run of them.
        c = re.sub(
            r"^(?:(?:and|but|while|whilst|with|facing|faces|confronting|"
            r"confronts|opposite|before|behind|beside|another|the other|"
            r"across from|in front of)\s+)+",
            "", clause.strip(), flags=re.I,
        )
        c = re.sub(
            r"\s+\b(?:stands?|standing|stood|occupy|occupies|occupying|is|are|"
            r"was|were|sits?|sitting|steps?|stepping|moves?|moving|walks?|"
            r"walking|advances?|advancing|lunges?|lunging|blocks?|blocking|"
            r"raises?|raising|swings?|swinging|reaches?|reaching)\b.*$",
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
        # Taking the first clause that mentions clothes is what handed the
        # player's outfit to the enemy: the plate is a two-shot and the
        # prompt introduces the player first, so the protagonist's clause is
        # almost always the one that comes first. Pick the clause that looks
        # like the stranger the brief asked for, and lean away from anything
        # wearing what the player wears.
        worn = wardrobe_tokens_from_text(c)
        words = _look_words(c)
        score = (2 * len(worn & wanted) + len(words & wanted_words)
                 - 3 * len(worn & owned) - 2 * len(words & owned))
        if re.search(r"\b(?:wearing|dressed|in|with)\b", c, re.I):
            score += 1
        # Garments this clause wears that the invented stranger was never
        # described in. Rewarding matches alone TIED "a green quilted vest and
        # a dark baseball cap" with "an older man in a plaid shirt" whenever
        # the brief happened to mention a cap too — and a tie keeps the earlier
        # clause, which in a two-shot is the player. That is the whole bug this
        # function exists to prevent, arriving through the back door.
        #
        # Bounded by the credit the clause earned so it can only erode a match,
        # never push a richly-described stranger below a clause that matched
        # nothing at all: the plate always describes people in more detail than
        # the brief invented them in.
        score -= min(len(worn - wanted), 2 * len(worn & wanted))
        # Ties keep the earlier clause, which is the old behaviour.
        candidates.append((score, -len(candidates), c))
    if not candidates:
        return distinct_enemy_look(fallback)
    best = max(candidates)[2]
    return distinct_enemy_look(_clip(best, "", 160))


def adopt_plate_look(brief: dict, plate_seen: str) -> dict:
    """Repoint the brief's character at the rendered plate, then relabel."""
    char = brief.setdefault("character", {})
    look = plate_stranger_look(plate_seen, fallback=char.get("look") or "")
    if not look or is_default_stranger_look(look):
        # The plate never showed a usable stranger. Keep the invented look
        # rather than locking onto the default nobody.
        if not char.get("locked_look") or look_clones_player(char.get("locked_look") or ""):
            char["locked_look"] = distinct_enemy_look(
                char.get("look") or "",
                seed=char.get("label") or "",
                kind=char.get("kind") or "",
            )
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


def brief_from_vision(vision: Optional[dict], place_hold: str = "",
                      rolled: str = "") -> dict:
    """Synthesize a brief from what the frame actually shows — never a canned pipe.

    ``rolled`` is this turn's roll off the run's roster, when there is one. The
    model call is what failed here, not the dice, so the fallback still gets to
    say what arrived instead of falling back to "A stranger" for the tenth time.
    """
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
        look = plate_stranger_look(body, fallback=default_stranger_look(body, kind))
        danger = "they are already close enough to hurt you"
    else:
        kind = "person"
        label = "A presence"
        look = "a figure at the edge of this place, clothes matching the light and dust"
        danger = ("they have already closed the distance across this ground"
                  if outdoor else
                  "they are already between you and the way you came")
    if rolled:
        label = _clip(rolled, label, 40)
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
                             vision: Optional[dict] = None,
                             rolled: str = "") -> dict:
    vis = vision if isinstance(vision, dict) else {}
    if vis.get("description") or vis.get("setting") or vis.get("spatial"):
        return brief_from_vision(vis, place_hold, rolled=rolled)
    idx = 0
    if seed:
        idx = sum(ord(c) for c in seed) % len(_FALLBACK_BRIEFS)
    brief = json.loads(json.dumps(_FALLBACK_BRIEFS[idx]))
    if rolled:
        brief["character"]["label"] = _clip(
            rolled, brief["character"]["label"], 40)
    if place_hold:
        brief["place_hold"] = _clip(place_hold, "", 160)
    return brief


def encounter_lore_context(session_id: str, cap: int = 1500) -> str:
    """What this world is actually about, for the brief that invents people.

    The brief used to get 400 characters of world prompt and ``use_lore``
    switched off, so the only thing it knew was that there was a yard. With
    nothing to draw on it invented the most generic human it could — the
    "random guy" problem. Everything here is already written down somewhere
    in the run; it was simply never handed over.
    """
    import engine
    bits = []

    try:
        import experience_store
        lore = str(experience_store.lore_brief() or "").strip()
        if lore:
            bits.append("WHAT THIS WORLD IS:\n" + lore[:cap])
    except Exception:
        pass

    st = {}
    try:
        st = engine._load_state(session_id) or {}
    except Exception:
        st = {}

    world = str(st.get("world_prompt") or "").strip()
    if world:
        bits.append("WHERE THE STORY HAS GOT TO:\n" + world[:900])

    # The last few beats, so a new arrival can be a consequence of something
    # the player did rather than a stranger who wandered in.
    recent = []
    try:
        for entry in reversed(engine._load_history(session_id) or []):
            if not isinstance(entry, dict):
                continue
            beat = str(entry.get("dispatch") or "").strip()
            if beat:
                recent.append(_clip(beat, "", 180))
            if len(recent) >= 3:
                break
    except Exception:
        pass
    if recent:
        bits.append("JUST HAPPENED (newest first):\n- " + "\n- ".join(recent))

    seen = st.get("seen_elements")
    if isinstance(seen, (list, tuple)) and seen:
        names = [str(s) for s in seen if s][-12:]
        if names:
            bits.append("ALREADY ESTABLISHED IN THIS RUN: " + ", ".join(names))

    phase = str(st.get("current_phase") or "").strip()
    threat = st.get("threat_level")
    tail = []
    if phase:
        tail.append(f"phase {phase}")
    if isinstance(threat, (int, float)):
        tail.append(f"threat {threat}")
    if tail:
        bits.append("PRESSURE: " + ", ".join(tail))

    return "\n\n".join(bits)


def _clean_roster(raw: Any) -> list:
    """Pull a usable list of kinds out of whatever the model returned."""
    data = raw
    if isinstance(raw, str):
        blob = raw.strip()
        if blob.startswith("```"):
            blob = re.sub(r"^```[a-z]*\s*|\s*```$", "", blob).strip()
        try:
            data = json.loads(blob)
        except Exception:
            # No JSON: treat it as the plain list a model often answers with.
            data = {"kinds": [ln for ln in blob.splitlines() if ln.strip()]}
    if isinstance(data, dict):
        items = data.get("kinds") or data.get("roster") or data.get("items") or []
    elif isinstance(data, (list, tuple)):
        items = list(data)
    else:
        items = []

    out: list = []
    seen: set = set()
    for item in items:
        text = str(item or "").strip()
        # Numbering and bullets survive a schema'd answer more often than not.
        text = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s*", "", text).strip(" .")
        if len(text) < 4:
            continue
        text = _clip(text, "", 90)
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= ENCOUNTER_ROSTER_SIZE:
            break
    return out


def build_encounter_roster(session_id: str = "default") -> list:
    """Ask this run's lore what lives here. One cheap text call, once per run."""
    import engine
    lore = ""
    try:
        import experience_store
        lore = str(experience_store.lore_brief() or "").strip()
    except Exception:
        lore = ""
    if not lore:
        # No Experience bible: the run's own world document is the next best
        # description of the place, and it is what the old brief read anyway.
        try:
            st = engine._load_state(session_id) or {}
            lore = str(st.get("world_prompt") or "").strip()
        except Exception:
            lore = ""
    if not lore:
        return []
    try:
        raw = engine._ask(
            DEFAULT_ROSTER_INSTRUCTIONS + "\n\nWORLD BIBLE:\n" + lore[:4000],
            model="gemini",
            # High, deliberately. This call is the only randomness in what a
            # run can meet, so two runs of the same bible should not agree.
            temp=1.0,
            tokens=400,
            use_lore=False,
            response_schema=ENCOUNTER_ROSTER_SCHEMA,
        )
    except Exception as err:
        try:
            engine.log_error(f"[ENCOUNTER] roster ask failed: {err}")
        except Exception:
            pass
        return []
    roster = _clean_roster(raw)
    print(f"[ENCOUNTER] roster for this run ({len(roster)}): "
          f"{' | '.join(roster)}", flush=True)
    return roster


def encounter_roster(session_id: str = "default") -> list:
    """This run's roster, built on first use and kept in the session state.

    Built lazily rather than at reset so that nothing about starting a run waits
    on it. A run's state file is deleted on reset, so the roster is per-run for
    free: a new game reads the lore again and gets a different list.
    """
    import engine
    try:
        st = engine._load_state(session_id) or {}
    except Exception:
        st = {}
    cached = st.get("encounter_roster")
    if isinstance(cached, (list, tuple)):
        kinds = [str(k) for k in cached if str(k or "").strip()]
        if kinds:
            return kinds

    roster = build_encounter_roster(session_id)
    if not roster:
        return []
    try:
        st = engine._load_state(session_id) or {}
        st["encounter_roster"] = roster
        engine._save_state(st, session_id)
    except Exception as err:
        try:
            engine.log_error(f"[ENCOUNTER] roster save failed: {err}")
        except Exception:
            pass
    return roster


def roll_encounter_kind(session_id: str = "default") -> str:
    """Draw what arrives this time, avoiding what arrived recently.

    Returns "" when there is no roster to draw from (lore disabled, or the ask
    failed), and the brief then works exactly as it did before.
    """
    roster = encounter_roster(session_id)
    if not roster:
        return ""
    import engine
    try:
        st = engine._load_state(session_id) or {}
    except Exception:
        st = {}
    used = [str(k) for k in (st.get("encounter_kinds_used") or []) if str(k or "")]
    recent = set(used[-ENCOUNTER_ROSTER_COOLDOWN:])
    pool = [k for k in roster if k not in recent] or list(roster)
    pick = random.choice(pool)
    try:
        st = engine._load_state(session_id) or {}
        st["encounter_kinds_used"] = (used + [pick])[-ENCOUNTER_ROSTER_COOLDOWN * 2:]
        engine._save_state(st, session_id)
    except Exception:
        pass
    print(f"[ENCOUNTER] rolled kind: {pick}", flush=True)
    return pick


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
            # `vision_analysis` is the key a turn actually writes for "what the
            # rendered frame shows". This asked for `description` /
            # `vision_description` / `caption`, none of which any history entry
            # has ever carried, so the description half of the place lock was
            # empty on EVERY encounter — the brief was briefed on "VISIBLE: "
            # and invented a location, and the plate restaged the frame into it.
            desc = str(last.get("vision_analysis") or last.get("description")
                       or last.get("vision_description") or last.get("caption") or "")
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
    standoff or kill you, so the only winning move was always to run. Talking
    them down ends it the same way, or the peace lane would be a choice that
    never resolves anything.
    """
    if str(outcome or "").strip().lower() in ("escape", "die"):
        return True
    if isinstance(record, dict):
        state = str(record.get("enemy_state") or "").strip().lower()
        return state in ENCOUNTER_ENEMY_SETTLED
    return False


def release_verdict(outcome: str, record: Optional[dict] = None,
                    brief: Optional[dict] = None) -> dict:
    """The card and the one line under it for a fight that just ended.

    Winning was the quietest thing that could happen in this game. The resolve
    response carries no dispatch — the aftermath turn writes that later, on
    another thread — so the verdict card came up reading SURVIVED over the
    STAKES line, which is a sentence about what happens if the player hesitates.
    Nothing anywhere said the fight was over, or that they had won it.
    """
    out = str(outcome or "").strip().lower()
    state = str((record or {}).get("enemy_state") or "").strip().lower()
    label = str(((brief or {}).get("character") or {}).get("label") or "").strip()
    who = label or "They"
    if out == "die":
        return {"word": "DEAD", "line": ""}
    if state == "down":
        return {"word": "DOWN",
                "line": f"{who} is down. You are still standing."}
    if state == "standing_down":
        return {"word": "SETTLED",
                "line": f"{who} backs off. There is nothing left here to answer."}
    if out == "escape":
        return {"word": "CLEAR",
                "line": "You broke contact. Whatever that was, it is behind you."}
    if out == "wounded":
        return {"word": "HURT", "line": ""}
    return {"word": "", "line": ""}


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
        # The three lanes are fixed, so a continued round used to come back as
        # the opening slate reworded — "smash their skull with pipe" became
        # "crush his windpipe with force" — and the fight read as the same
        # decision every round. Tell it where the fight has got to, and what it
        # has already said, so the lanes have to escalate inside themselves.
        try:
            round_no = int(brief.get("round_no") or 0)
        except (TypeError, ValueError):
            round_no = 0
        if round_no > 1:
            extra += f"This is round {round_no} of this fight. "
        enemy = _clip(brief.get("enemy_state"), "", 40)
        if enemy:
            extra += f"They are now {enemy}. "
        extra += (
            "Each option must be an act that only makes sense AFTER that last "
            "beat: press the advantage you just won, recover from what just "
            "went wrong, or use what just changed in the frame. Escalate the "
            "fight — do not reset it.\n"
        )
        already = []
        for item in (brief.get("choices") or []):
            text = item.get("text") if isinstance(item, dict) else item
            text = str(text or "").strip()
            if text:
                already.append(text)
        if already:
            extra += (
                "ALREADY OFFERED — do not offer these again and do not reword "
                "them into the same act: " + "; ".join(already[:6]) + "\n"
            )
    seen = _clip(brief.get("plate_seen"), "", 220)
    seen_line = f"\nTHE IMAGE SHOWS: {seen}\n" if seen else ""
    last = _clip(brief.get("last_dispatch"), "", 280)
    last_line = f"\nLAST BEAT (the world just did this): {last}\n" if last else ""
    # A fight the player understands the point of plays very differently
    # from three ways to shove a stranger, so the slate is told what the
    # other person actually wants.
    motive = _clip(brief.get("motive"), "", 120)
    motive_line = f"THEY WANT: {motive}.\n" if motive else ""
    return (
        f"{base}{extra}{seen_line}{last_line}\n"
        f"THE FIGURE: {char.get('label') or 'a stranger'} "
        f"({char.get('stance') or 'hostile'}) — {char.get('look') or 'a figure'}.\n"
        f"{motive_line}"
        f"THEY ARE DOING: {brief.get('danger') or 'challenging you'}.\n"
        f"STAKES: {brief.get('stakes') or 'Hesitation costs you.'}\n"
        "Every choice names them or a verb against them. No landscape.\n"
        "At least one option should go at what they want — take it, deny "
        "it, or get it out of them — not only at their body."
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
        # Winning is also an ending, and it was the one ending with no frame of
        # its own: the run resumed on the standoff plate, so the fight the
        # player had just finished stayed on screen as the world.
        state_l = str(brief.get("enemy_state") or "").strip().lower()
        if out_l != "escape" and state_l in ENCOUNTER_ENEMY_SETTLED:
            settled = (
                "the body on the ground where it fell and the player still on "
                "their feet over it"
                if state_l == "down" else
                "the other one giving it up — backing off, hands where they can "
                "be seen, no longer squared up"
            )
            decided += (
                " The confrontation is OVER and the player came out of it "
                f"standing. visual_scene is THIS SAME PLACE moments later: "
                f"{settled}, and the camera back on the world the player was "
                "walking through. Not the standoff, not the blow landing, and "
                "not the empty frame from before any of this arrived."
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
        # The lane is an extreme act now, so the decisive verbs have to
        # land here rather than falling through to the index fallback.
        "smash", "crack", "choke", "stab", "swing", "bring down",
        "put down", "break their", "drive them", "throw them",
        "crush", "club", "beat",
    ),
    "evade": (
        "dive", "dodge", "duck", "flee", "run", "slip", "cover", "evade",
        "retreat", "back away", "roll", "sidestep", "break away", "get clear",
        "bolt", "sprint", "vault", "get out", "leave", "walk away",
        # Typed actions reach these keywords too, and breaking contact by not
        # being found is the obvious thing a player writes that no verb here
        # covered — it was landing in confront by default.
        "hide", "sneak", "crawl",
    ),
    "parley": (
        "talk", "speak", "answer", "offer", "tell them", "say", "name",
        "hand over", "hand them", "give them", "give up", "surrender",
        "stand down", "lower", "back down", "agree", "promise", "bargain",
        "explain", "apolog", "let them", "show them", "warn them",
        "trade", "concede", "plead", "reason with", "call them",
    ),
}


def lane_keyword_hits(text: str) -> list:
    """Which lanes this wording touches, in lane order. Free, no model.

    Split out of classify_encounter_lane because a TYPED action needs to tell
    "this matched nothing" apart from "this matched confront" — the old return
    value conflated them, and confront is the lane with the worst odds.
    """
    blob = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return [lane for lane, words in _LANE_KEYWORDS.items()
            if any(w in blob for w in words)]


def classify_encounter_lane(text: str, index: Optional[int] = None) -> str:
    """Keyword first (in case the model reordered), then the 1/2/3 fan."""
    hits = lane_keyword_hits(text)
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


def classify_custom_lane(text: str, brief: Optional[dict] = None) -> str:
    """Which lane a player's TYPED action belongs to.

    The lane is not a label — it picks the outcome weights, so it is the
    difference between walking out of this and dying in it. A written action
    gets read by the model when the keyword list cannot honestly answer,
    because the keywords were built to sanity-check three model-authored verbs,
    not to interpret whatever a player decides to do. Keywords stay in front of
    the call: they are free, and they are right about "run" and "hand it over".

    Anything that is neither force nor flight resolves to parley. It is the
    lane for dealing with the thing in front of you by means other than
    violence, which is what most improvised actions are, and the old default of
    confront quietly charged the player combat odds for talking.
    """
    hits = lane_keyword_hits(text)
    if len(hits) == 1:
        return hits[0]
    asked = _ask_custom_lane(text, brief)
    if asked in ENCOUNTER_LANES:
        return asked
    return hits[0] if hits else "parley"


def _ask_custom_lane(text: str, brief: Optional[dict] = None) -> str:
    """One short call: read the action, name the lane. "" on any failure."""
    action = re.sub(r"\s+", " ", str(text or "")).strip()
    if not action:
        return ""
    import engine
    char = ((brief or {}).get("character") or {})
    who = str(char.get("label") or "the other one").strip()
    danger = str((brief or {}).get("danger") or "").strip()
    facing = f"They are {danger}." if danger else ""
    try:
        raw = engine._ask(
            "A player is in a confrontation and has written what they do next. "
            "Decide which of three things that action IS.\n"
            "\n"
            "confront — they use force on it: strike, tackle, grab, throw "
            "something at it, put it down.\n"
            "evade — they break contact: run, hide, dodge past, get out, "
            "refuse to be where it is.\n"
            "parley — anything else: talk, offer, hand something over, "
            "comply, bluff, threaten with words, stall, show it something.\n"
            "\n"
            f"Facing them: {who}. {facing}\n"
            f"They wrote: {action[:300]}\n"
            "\n"
            "Answer with the lane the action IS, not the one that would be "
            "wise. Return JSON only: {\"lane\": \"confront|evade|parley\"}",
            model="gemini",
            temp=0.0,
            tokens=40,
            use_lore=False,
            response_schema=ENCOUNTER_LANE_SCHEMA,
        )
    except Exception as err:
        try:
            engine.log_error(f"[ENCOUNTER] lane ask failed: {err}")
        except Exception:
            pass
        return ""
    if isinstance(raw, dict):
        lane = str(raw.get("lane") or "").strip().lower()
    else:
        lane = str(raw or "").strip().lower()
    for candidate in ENCOUNTER_LANES:
        if candidate in lane:
            return candidate
    return ""


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
        {"text": f"Put {short} down", "lane": "confront"},
        {"text": f"Break away from {short}", "lane": "evade"},
        {"text": f"Give {short} what they want", "lane": "parley"},
    ]


def match_encounter_choice(posted_text: str, posted_lane: str,
                           stored: Any, *, custom: bool = False,
                           brief: Optional[dict] = None) -> tuple[str, str]:
    """Trust the stored slate over the client. Fall back to posted text.

    ``custom`` marks text the player TYPED rather than picked. Off-slate text
    was already kept verbatim as the verb — it reaches the resolve prompt, the
    stakes and the aftermath turn — but its lane fell out of
    ``classify_encounter_lane(verb, 0)``, and that index-0 fallback means
    confront. Every written action the keywords did not recognise was silently
    charged the odds of throwing the first punch. Typed text gets read instead.
    """
    verb = (posted_text or "").strip() or "Hold your ground"
    slate = structure_encounter_choices(stored)
    want = verb.casefold()
    for item in slate:
        if (item.get("text") or "").casefold() == want:
            return item["text"], item["lane"]
    lane = str(posted_lane or "").strip().lower()
    if lane not in ENCOUNTER_LANES:
        if custom:
            lane = classify_custom_lane(verb, brief)
        else:
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
        # Parley. Talking is the safest lane by a wide margin, which is the
        # point — it is the option that costs you something other than blood.
        # It is not free: a creature has no use for what you are offering,
        # and a desperate person is the one most likely to swing anyway.
        if kind_l == "creature":
            w = {"survive": 30, "escape": 12, "wounded": 48, "die": 10}
        elif stance_l == "opportunistic":
            w = {"survive": 80, "escape": 12, "wounded": 8, "die": 0}
        elif stance_l == "desperate":
            w = {"survive": 58, "escape": 10, "wounded": 30, "die": 2}
        else:
            w = {"survive": 65, "escape": 10, "wounded": 22, "die": 3}
        if staggered:
            # They have already had the worst of it and want a way out.
            w["survive"] = w.get("survive", 0) + 15
            w["wounded"] = max(0, w.get("wounded", 0) - 10)
        if wounded:
            # Bleeding in front of someone who wants something is leverage
            # against you, not for you.
            w["wounded"] = w.get("wounded", 0) + 10
            w["survive"] = max(15, w["survive"] - 10)

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
                        rng: Any = None, stance: str = "hostile",
                        kind: str = "person") -> str:
    """How the other body changes as a result of this exchange.

    This is the escalation the encounter never had. Pressing a confront moves
    them ready -> staggered -> down, so two committed verbs finish a fight and
    the player can win one, which was previously impossible: `encounter_releases`
    only fired on escape or death.
    """
    state = str(enemy_state or "ready").strip().lower()
    if state not in ENCOUNTER_ENEMY_STATES:
        state = "ready"
    if outcome in ("die", "escape") or state in ENCOUNTER_ENEMY_SETTLED:
        return state
    roll = rng.random() if rng is not None else random.random()
    if lane == "confront":
        if state == "staggered":
            return "down" if roll < 0.62 else "staggered"
        return "staggered" if roll < 0.55 else "ready"
    if lane == "parley":
        # This is the other way to win, and the only one that does not
        # cost a body. A creature has no use for what the player is
        # offering, so talking at it changes nothing.
        if str(kind or "person").strip().lower() == "creature":
            return state
        chance = 0.55 if str(stance or "").strip().lower() == "opportunistic" else 0.35
        if state == "staggered":
            chance += 0.20
        if outcome == "wounded":
            chance -= 0.25
        return "standing_down" if roll < max(0.0, chance) else state
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
    next_enemy = advance_enemy_state(lane, outcome, enemy_state, rng=rng,
                                     stance=stance, kind=kind)
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


def cinematic_composition(action: bool = False) -> str:
    """Stage inside the frame like a film still instead of a snapshot.

    Nothing told the model where to PUT anybody, only who to draw, so it
    fell back on the safest arrangement it knows: two figures centred,
    equal size, squared up, flat against the background. Every encounter
    came out as the same photograph of two people standing apart.
    """
    bits = [
        "COMPOSITION — this is a frame from a film, not a snapshot. "
        "Put the figures on the thirds: nobody dead centre, nobody "
        "mirrored against the other.",
        "Build depth in three layers — something belonging to this place "
        "close to the lens and out of focus, the people in the midground, "
        "the rest of the location falling away behind them.",
        "Run the line between them diagonally across the frame rather "
        "than flat across it, and make them different sizes: one nearer "
        "the camera, one further back.",
        "Leave the space they are about to move into open, so the frame "
        "leans where this is going.",
    ]
    if action:
        bits.append(
            "Get close and off-axis. Let a body break the edge of the "
            "frame. The horizon can tilt."
        )
    return " ".join(bits)


def build_encounter_plate_prompt(brief: dict, img2img: bool = True,
                                 setting: str = "", target: Optional[dict] = None) -> str:
    """Cinematic restage of THIS place with the new character and danger visible.

    ``target`` marks a fight the player aimed at something already in the
    reference frame. "ADD the new character" is the wrong instruction for that
    — it draws a second copy of the thing, or a stranger beside it. What the
    plate wants is the same object, turned on the player.
    """
    brief = normalize_encounter_brief(brief)
    char = brief["character"]
    bits = []
    try:
        import game_identity
        anchor = game_identity.world_anchor(
            ENCOUNTER_PLATE_STYLE_ANCHOR,
            # The sheet arrives below as the CAST LOCK, in stronger words and
            # right next to the rule about who may wear that outfit. A third
            # copy of it here only spends payload.
            include_character=False,
            # The encounter is still the player's game, shot on the camera
            # they set up in the editor. Dropping the vantage here threw away
            # the follow-cam rig, the lens and the camera notes, so walking
            # into a fight cut from their third-person world to an anonymous
            # two-shot.
            include_vantage=True,
        )
        if anchor:
            bits.append(anchor.rstrip(". ") + ".")
        # A vantage clause is one sentence. Every OTHER frame in the game also
        # gets the camera BLOCK — the rig, the lens, the shot size, "never turn
        # them to face the lens", the cut-to-cut continuity rules — and the
        # plate was the one render that did not, so it was free to answer the
        # standoff with whatever framing it liked while still technically
        # honouring the vantage.
        directive = game_identity.camera_directive()
        if directive:
            bits.append(directive.strip())
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
    aimed = _clip((target or {}).get("label") if isinstance(target, dict) else target, "", 60)
    if img2img:
        place_lock = (
            "PLACE LOCK — HARD. The reference is the frame the player is "
            "looking at right now, and this is the next exposure on that "
            "same roll. Keep the same location, architecture, materials, "
            "ground, sky, and light — and the same camera: same height, "
            "same angle, same distance, same focal length. "
        )
        if aimed:
            place_lock += (
                f"The {aimed} is ALREADY in this photograph — do not add a "
                f"second one and do not put a stranger next to it. The player "
                f"has just struck it, and this is the exposure where it comes "
                f"back at them: the same {aimed}, in the same spot, now "
                f"moving, open, upright, turned on the camera. Do not restage "
                f"the place, do not teleport."
            )
        else:
            place_lock += (
                "ADD the new character and danger INTO this photograph. "
                "Do not restage it, do not change the place, do not teleport."
            )
        bits.append(place_lock)
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
    look = distinct_enemy_look(
        char.get("locked_look") or char.get("look") or "",
        seed=char.get("label") or "", kind=char.get("kind") or "",
    )
    if _camera_shows_player():
        cast = player_cast_lock(trust_reference=img2img)
        if cast:
            bits.append(cast)
        bits.append(
            f"TWO DISTINCT PEOPLE IN A STANDOFF, held one beat before "
            f"anything happens. (1) The player character, the same person as "
            f"the character sheet, squared up and braced — only THEY wear "
            f"that outfit. (2) A newly introduced {char['kind']} named "
            f"'{char['label']}' — {look} — blocking the way, "
            f"{char['stance']}, close enough to reach, large and readable, "
            f"facing the player. They read as two different people: "
            f"different face, different clothes, no shared wardrobe, not a "
            f"copy of the player. Keep their genders and faces distinct. "
            f"Both are caught mid-movement — a step already taken, weight "
            f"already committed, coats and dust still moving — but nothing "
            f"has landed yet: no contact, no choke, no takedown."
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
        # The brief writes danger as something already underway ("swinging a
        # wrench at you"), which fights the held beat the rest of the plate
        # asks for. Frame it as the thing about to land instead.
        f"The danger is THIS FIGURE's body and what they are about to do: "
        f"{brief['danger']} — caught at the edge of happening, not yet done. "
        f"Keep every object in the frame to what the reference photograph "
        f"already shows."
    )
    # An antagonist who wants something photographs differently from one who
    # is simply hostile: it shows in where they are looking and what they
    # are reaching for.
    motive = _clip(brief.get("motive"), "", 120)
    if motive:
        bits.append(
            f"What they came for is readable in the frame: {motive}. "
            f"Let it show in where they are looking and what they are "
            f"reaching toward."
        )
    if brief.get("place_hold"):
        bits.append(f"Hold these place locks: {brief['place_hold']}.")
    # "Hold the established camera" used to be one sentence followed by four
    # that restage the shot — put them on the thirds, run them diagonally, make
    # them different sizes, one nearer the lens. That is a recompose, and it
    # outvoted both the camera block above and the reference underneath. When
    # there IS a reference, the composition is already decided: it is the frame
    # the player is standing in. Only a text-to-image plate has an empty frame
    # to stage, so only it gets staging direction.
    if _camera_shows_player():
        bits.append(
            ("Place the newcomer into the composition that already exists "
             "rather than rebuilding it around them."
             if img2img else
             "Hold the established camera. " + cinematic_composition()) +
            " Both bodies readable, empty hands, no HUD, no game UI, "
            "no captions, no letterbox. A finished 1993 photograph."
        )
    else:
        bits.append(
            "Point-of-view framing, one figure close to the lens." +
            ("" if img2img else " " + cinematic_composition()) +
            " No HUD, no game UI, no captions, no letterbox. "
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
    locked = distinct_enemy_look(
        char.get("locked_look") or char.get("look") or "",
        seed=char.get("label") or "", kind=char.get("kind") or "",
    )
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
        "parley": (
            f"{actor} No blow is being struck. They are still close, but "
            "the violence has gone out of it — an open and empty hand, "
            "something held out or set down between them, the other "
            "person's weight coming off their front foot."
        ),
    }.get(lane_s, f"{actor} Bodies in contact. The player is acting.")
    bits.append(
        f"CAST LOCK — TEXT ONLY. Same two people. Same faces, hair, clothes, "
        f"gender. The other person is {char['label']} — {locked}. "
        f"They are NOT wearing the player's vest or PRESS gear. "
        f"Do not recast. Do not add a third person. Do not draw a character sheet."
    )
    if lane_s == "parley":
        bits.append(
            f"AGENCY LOCK — HARD. {actor} The player is the one defusing "
            f"this. Nobody is being struck, grabbed, or thrown. Do not "
            f"show a blow from either of them."
        )
    else:
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
    if lane_s == "parley":
        # The generic contact rule reads as an order to draw a punch, which
        # is the one thing this beat must not contain.
        contact = (
            "They are still close enough to touch, but the fight is coming "
            "out of the moment and it has to be legible on their bodies — "
            "hands, shoulders, where the weight sits. If you show a blow "
            "landing, you failed. If you return the previous standoff "
            "unchanged, you failed."
        )
    else:
        contact = (
            "Bodies are in contact or a hand's width apart — this is an "
            "exchange, not a conversation. If you return the previous "
            "standoff with a small pose change, you failed. If the two "
            "people are standing apart looking at each other, you failed. "
            "If you show the stranger doing the verb to the player, you failed."
        )
    bits.append(
        f"THIS IS A HARD CUT. New camera, new blocking. {shot} "
        f"Show the instant the verb lands: {verb_s} ({lane_s}). {motion} "
        f"{contact}"
    )
    enemy_state = str(brief.get("enemy_state") or "ready").strip().lower()
    if out_s in ("survive", "wounded") and enemy_state == "standing_down":
        bits.append(
            "THIS IS THE FINISH, AND NOBODY WON IT WITH THEIR HANDS. The "
            "other person has given it up — hands lowered or open, weight "
            "back on the heels, eyes off the player, already turning away. "
            "Nobody is on the ground. Nobody is hurt."
        )
    elif out_s in ("survive", "wounded") and enemy_state == "down":
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
        cinematic_composition(action=True) +
        " No HUD, no game UI, no captions, no letterbox. "
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
                          place_hold: str = "", vision: Optional[dict] = None,
                          target: Optional[dict] = None) -> dict:
    """Ask the model for a character + danger grounded on the current frame.

    ``target`` is the one case where the encounter is not a roll: the player
    walked up to something they could already see and swung at it. What
    arrives is then not a question — it is that thing, already in the
    photograph — so the roster draw is skipped and the brief's only job is to
    say who this thing turns out to be once it fights back.
    """
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
    # 400 characters of world prompt was the entire briefing, which is why
    # every encounter was a man in coveralls with no reason to be there.
    lore = encounter_lore_context(session_id)
    # The dice have already decided WHAT arrives (see roll_encounter_kind). The
    # model's job is to dress that into this photograph and give it a reason to
    # be here — not to choose the thing, because asked to choose it always chose
    # the same thing.
    aimed = _clip((target or {}).get("label") if isinstance(target, dict) else target, "", 60)
    if aimed:
        rolled = aimed
        roll_line = (
            f"THE PLAYER HAS JUST ATTACKED: {aimed}\n"
            "That thing is already in the attached photograph, where they were "
            "standing, and they went for it. It is the encounter — do not roll "
            "up a stranger who happens to be nearby, and do not move the fight "
            "to a different target. The label names THIS thing, the look is the "
            f"{aimed} as the photograph shows it, and the danger is what it "
            "does back now that it has been hit. If it was inert a second ago, "
            "this is the moment it stops being inert: something was inside it, "
            "behind it, or it was never what it looked like.\n\n"
        )
    else:
        rolled = roll_encounter_kind(session_id)
        roll_line = (
            f"THIS ENCOUNTER IS: {rolled}\n"
            "That is the roll for this turn, not a suggestion and not a menu: what "
            "arrives IS that. Everything else you write serves it — the label names "
            "this thing, the look is this thing's body, the motive is what THIS "
            "thing wants from the player. Do not substitute a person for it because "
            "a person is easier to photograph.\n\n"
            if rolled else ""
        )
    prompt = (
        f"{instructions}\n\n{roll_line}{lore or ('WORLD (trim): ' + world)}\n\n"
        f"SETTING: {setting or 'read it off the attached photograph'}\n"
        f"VISIBLE: {visible[:400] or 'the attached photograph — that place, nothing else'}\n"
        "The character and danger MUST fit THIS setting. "
        "If outdoor, do not invent an interior. If indoor, do not go outside.\n"
        # "SETTING: unknown / VISIBLE:" is not a blank to be filled in from the
        # world bible — and the bible is 6000 characters of corridors, labs and
        # concrete floors, so that is exactly what came back for a run standing
        # in open desert. The photograph is attached; say so.
        "Nothing here overrides the photograph: if the frame is open ground at "
        "dusk, the encounter happens on open ground at dusk, however much of "
        "this world happens indoors.\n"
        # This used to end "do not invent a rank or sci-fi class this 1993 world
        # cannot show", which reads as a ban on soldiers in a world whose bible
        # opens with military raids. The real constraint was never the noun, it
        # is whether a 1993 camera could catch the thing: a raid team, a changed
        # miner and a standing column of dust all pass that test.
        "label names what a photograph of this would be captioned — the thing, "
        "and whose it is. Anything the available light and a 35mm lens could "
        "actually catch in 1993 is fair; nothing that needs CGI or a glow.\n"
        f"{_brief_cast_rule()}"
        "Return one JSON object with character, motive, danger, stakes, "
        "place_hold."
    )
    raw = ""
    try:
        raw = engine._ask(
            prompt,
            model="gemini",
            temp=0.9,
            # The brief now answers with a motive as well, and a clipped
            # reply loses the field the whole encounter hangs on.
            tokens=340,
            image_path=image_path,
            use_lore=False,
            response_schema=ENCOUNTER_BRIEF_SCHEMA,
        )
    except Exception as err:
        try:
            engine.log_error(f"[ENCOUNTER] brief ask failed: {err}")
        except Exception:
            pass
        return fallback_encounter_brief(hold, seed=seed, vision=vis, rolled=rolled)
    brief = normalize_encounter_brief(raw, place_hold=hold)
    # If the model returned the disabled-LLM placeholder prose, fall back.
    label = (brief.get("character") or {}).get("label") or ""
    if label.lower() in ("you are still",) or "signal interrupted" in str(raw).lower():
        return fallback_encounter_brief(hold, seed=seed, vision=vis, rolled=rolled)
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
              "goggles", "creature", "being", "scavenger", "soldier",
              "trooper", "animal", "beast", "shape", "silhouette", "mass"):
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


def plate_needs_a_retry(vision: Optional[dict], brief: Optional[dict] = None) -> bool:
    """True only when vision LOOKED at a plate and did not find the other body.

    ``plate_shows_confrontation`` answers "is a second presence visible", and
    no description at all is a No — so a vision hiccup, a disabled vision pass
    or a provider timeout made EVERY beat of a fight generate its plate twice,
    at full price, on no evidence. Absence of evidence is not evidence that the
    plate is wrong, and the duplicate render is also where a custom action's
    framing drifts: the retry prompt appends its own staging instructions.
    """
    vis = vision if isinstance(vision, dict) else {}
    if not str(vis.get("description") or "").strip():
        return False
    return not plate_shows_confrontation(vis, brief)


def _safe_vision_analyze(image_path: Optional[str]) -> dict:
    """``engine._vision_analyze_all``, but never raises. Grounding checks
    that fire off it (plate_shows_confrontation) should degrade to "no
    evidence either way" on a vision hiccup, not take the request down."""
    if not image_path:
        return {}
    import engine
    try:
        return engine._vision_analyze_all(image_path) or {}
    except Exception:
        return {}


def _parse_choice_payload(raw: Any) -> list:
    """Accept JSON {confront,evade,parley} or numbered lines."""
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
        # A model answering in its own words ("use", "talk", "flee") used to
        # cost the whole slate and drop the player onto the canned fallback.
        # Keep the writing and work out the lane from the verb.
        if len(ordered) < len(ENCOUNTER_LANES):
            taken = {c["lane"] for c in ordered}
            for key, value in data.items():
                if key in ENCOUNTER_LANES or not isinstance(value, str):
                    continue
                text = value.strip()
                if not text:
                    continue
                lane = classify_encounter_lane(text)
                if lane in taken:
                    continue
                ordered.append({"text": text, "lane": lane})
                taken.add(lane)
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


def _plate_sequence(session_id: str, prompt: str, ref_path: Optional[str],
                    caption: str = "") -> Optional[dict]:
    """This plate as flipbook frames, or None to stay a still.

    Every stage of a confrontation should move the way the ordinary view does —
    the standoff and each play-out — instead of being the one part of the game
    that freezes. The already-generated plate is the reference, so the motion
    starts from the picture the player is looking at and the last frame is where
    it holds.

    Never fatal: a flipbook that doesn't come back leaves the still in place, so
    a failure costs motion, not the encounter.
    """
    import engine

    try:
        st = engine._load_state(session_id) or {}
        if not engine.flipbook_active(st):
            print(f"[ENCOUNTER] flipbook off for this plate "
                  f"(settings={engine.flipbook_settings(st)}, "
                  f"session_mode={st.get('flipbook_mode')!r})", flush=True)
            return None
        # Only hand over a reference that is actually on disk. A path that has
        # been cleaned up (a temp img2img file, a plate from a previous session)
        # makes the whole grid fail, and then the fight silently loses its
        # animation over a file that was never there.
        use_ref = ""
        if ref_path and Path(str(ref_path)).exists():
            use_ref = str(ref_path)
        elif ref_path:
            print(f"[ENCOUNTER] flipbook plate: reference is gone "
                  f"({os.path.basename(str(ref_path))}) - generating without it",
                  flush=True)

        def _grid(refs):
            return engine._flipbook_generate(
                prompt_str=prompt,
                caption=caption,
                choice=caption,
                dispatch="",
                world_prompt=str(st.get("world_prompt") or ""),
                time_of_day=str(st.get("time_of_day") or ""),
                img_dir=engine._get_image_dir(session_id),
                session_id=session_id,
                st=st,
                refs=refs,
                ref_is_anchor=True,
            )

        seq = _grid([use_ref] if use_ref else None)
        if not seq and use_ref:
            # Losing the reference costs identity lock; losing the grid costs the
            # animation entirely. Prefer the cheaper failure.
            print("[ENCOUNTER] flipbook plate: grid failed with the plate "
                  "reference - retrying from the prompt alone", flush=True)
            seq = _grid(None)
        if not seq:
            print("[ENCOUNTER] flipbook plate: no grid came back - staying a still",
                  flush=True)
            return None
        # The last panel is the plate (see flipbook.sequence_from_grid), so the
        # caller needs both: frames for the client, that panel for everything
        # downstream that only understands one image. The key is `still_path`
        # — reading a `still` that sequence_from_grid has never emitted meant
        # every successful encounter grid was thrown away as a failure.
        payload = engine.flipbook_web_payload(seq, session_id)
        still = str(seq.get("still_path") or "")
        if not payload or not still:
            print(f"[ENCOUNTER] flipbook plate: the grid split into "
                  f"{seq.get('frame_count')} panel(s) but is not playable "
                  f"(still={bool(still)}) - staying a still", flush=True)
            return None
        return {"payload": payload, "still": still}
    except Exception as err:  # noqa: BLE001
        try:
            engine.log_error(f"[ENCOUNTER] flipbook plate failed: {err}")
        except Exception:
            pass
        return None


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
    set_look_session(session_id)
    force = bool(data.get("force") or data.get("demo"))
    reference_b64 = data.get("frame") or data.get("reference_image") or ""
    # A fight the player aimed at something they had already scanned and dived
    # into, rather than one the travel clock rolled. Overrides the roster draw.
    target = data.get("subject") if isinstance(data.get("subject"), dict) else None
    if target and not str(target.get("label") or "").strip():
        target = None

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
        target=target,
    )
    prompt = build_encounter_plate_prompt(
        brief, img2img=bool(ref_path),
        setting=place.get("setting") or "",
        target=target,
    )

    image_path = None
    web = None
    gen_mode = "none"
    t0 = time.time()

    # A flipbook standoff is drawn ONCE, as frames — the captured frame is this
    # pass's img2img init, exactly as the still path used it, so the encounter
    # still opens in the place the player was walking through. The last panel is
    # the plate, so the choice slate, object permanence and the resolve's own
    # reference all still get one true image, and the confrontation arrives
    # breathing instead of frozen. Falls through to the still below if flipbook
    # is off or the grid doesn't come back.
    _fb = _plate_sequence(session_id, prompt, ref_path,
                          caption=f"encounter_{brief['character']['label']}")
    if _fb and _fb.get("still"):
        image_path = _fb["still"]
        brief["_sequence"] = _fb.get("payload")
        gen_mode = "flipbook"

    if image_path is None and getattr(engine, "IMAGE_ENABLED", True):
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
                    if plate_needs_a_retry(plate_vis, brief):
                        try:
                            engine.log_error(
                                "[ENCOUNTER] plate missing the new character — retrying"
                            )
                        except Exception:
                            pass
                        retry_suffix = (
                            f" The {target['label']} fills this frame, close and "
                            f"coming at the player. This is the confrontation, "
                            f"not an empty place. Keep the player as the "
                            f"character-sheet person."
                        ) if target else (
                            " The new character is already standing in this "
                            "frame, large, facing the player. This is the "
                            "confrontation, not an empty place. "
                            "Keep the player as the character-sheet person. "
                            "The other person is a stranger in different clothes "
                            "— not a second copy of the player."
                        )
                        retry_path = generate_gemini_img2img(
                            prompt=prompt + retry_suffix,
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
                    if plate_needs_a_retry(plate_vis, brief):
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

    # A flipbook standoff sets image_path WITHOUT entering the still branch
    # above, and the web URL was computed inside that branch — so the moment
    # the flipbook plate started working, every encounter came back with
    # `plate_url: null`. The frames existed on disk and in the payload; the one
    # image the Moment paints did not have an address. That is the black
    # confrontation.
    if image_path and not web:
        web = engine._to_web_image_url(image_path, session_id)

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
        # The standoff breathes instead of freezing, and holds on its last frame
        # while the player reads the slate. `enter_sequence` was produced by the
        # plate pass, not a second generation.
        "sequence": brief.get("_sequence"),
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

    # Flipbook fights go STRAIGHT to frames — ONE generation, not a still and
    # then a flipbook of it, which put two renders on every beat of a fight.
    #
    # The standoff plate still goes in as the reference. That is not an extra
    # pass, it is this pass's img2img init, and it is load-bearing: generated
    # from text alone the punch came back with new faces, new clothes and an
    # indoor shed where the standoff had been an outdoor yard. The last panel IS
    # the plate, so pinning, SCAN, the vision pass and the next img2img
    # reference all still get one true image (sequence_from_grid guarantees it).
    fb = _plate_sequence(session_id, prompt, ref_path, caption=caption)
    if fb and fb.get("still"):
        if isinstance(brief, dict):
            brief["_sequence"] = fb.get("payload")
        return fb["still"], "flipbook"

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

    # Found by playtest.py's forced-encounter probe: a "die" outcome's own
    # "death still" came back as a calm, empty establishing shot — no
    # antagonist, no violence, nothing that reads as the ending it names.
    # api_begin already has this exact check for the STANDOFF plate
    # (plate_shows_confrontation, with a retry) — it was never applied to
    # the RESOLVE plate, so a fight could win/lose/die on a frame that
    # never actually showed the fight. One retry, same shape as the begin
    # path's, with the same "the other person is here, in frame" push.
    if image_path and plate_needs_a_retry(
        _safe_vision_analyze(image_path), brief
    ):
        try:
            engine.log_error(
                "[ENCOUNTER] resolve plate missing the other body — retrying"
            )
        except Exception:
            pass
        retry_prompt = prompt + (
            " The other person in this fight is here, in frame, close to the "
            "lens, still part of this moment — not an empty place, not a shot "
            "of the player alone. This is the result of what just happened "
            "between the two of them."
        )
        try:
            if gen_mode.startswith("hard_cut_plate") or gen_mode == "hard_cut_identity":
                retry_path = generate_gemini_img2img(
                    prompt=retry_prompt, caption=caption + "_retry",
                    reference_image_path=refs, strength=ENCOUNTER_RESOLVE_STRENGTH,
                    world_prompt=place_ctx[:200] if place_ctx else None,
                    time_of_day=tod, hd_mode=False, output_dir=Path(img_dir),
                    include_people=True, hold_cast=True, style_only_swatch=False,
                    identity_paths=identity or None, identity_seed=bool(identity),
                )
            else:
                retry_path = generate_with_gemini(
                    prompt=retry_prompt, caption=caption + "_retry",
                    world_prompt=place_ctx[:200] if place_ctx else None,
                    time_of_day=tod, hd_mode=False, output_dir=Path(img_dir),
                )
            if retry_path:
                image_path = retry_path
                gen_mode = gen_mode + "_retry"
        except Exception as retry_err:
            try:
                engine.log_error(f"[ENCOUNTER] resolve retry failed: {retry_err}")
            except Exception:
                pass

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

    Body: ``{choice, lane, custom?}``. ``custom`` says the player typed the
    action instead of picking one off the slate, which changes only how the
    lane is decided (see match_encounter_choice) — the text itself has always
    been used as the verb from here on. Returns a hard-cut resolve still. The world
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
    set_look_session(session_id)
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

    brief = normalize_encounter_brief(enc)
    # Ordered before the match so a typed action can be read against WHAT the
    # player is facing: "show them the badge" is a different act depending on
    # whether that is a checkpoint guard or a dog.
    verb, lane = match_encounter_choice(
        posted_text, posted_lane, enc.get("choices"),
        custom=bool(data.get("custom")), brief=brief,
    )
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
        "skip_image": encounter_turn_skip_image(rolled["outcome"], record),
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

    verdict = release_verdict(rolled["outcome"], record, brief)
    # Frames come from the plate generation itself (one pass, not two) — see
    # _generate_resolve_plate. None means this beat stayed a still.
    return jsonify({
        "sequence": brief.get("_sequence"),
        "resolve_url": web,
        "prompt": realtime,
        "outcome": rolled["outcome"],
        "lane": lane,
        "verb": verb,
        "alive": rolled["alive"],
        "condition": rolled["condition"],
        "released": released,
        # What the fight DID, so the client can put a word on the card and a
        # sentence under it. `enemy_state` is the difference between getting
        # away from something and putting it down, and the client had no way
        # to tell those apart.
        "enemy_state": rolled["enemy_state"],
        "verdict_word": verdict["word"],
        "closing": verdict["line"],
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
