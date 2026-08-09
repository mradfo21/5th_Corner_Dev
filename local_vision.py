"""
local_vision.py — on-device object detection for the SCAN tool.

WHY THIS EXISTS
───────────────
``engine._detect_objects`` originally asked Gemini to name and box the things in
the live frame. That works, but it is a network round-trip per scan: ~1-3 s of
latency, a per-call bill, and a hard dependency on GEMINI_API_KEY, which means
SCAN is simply dead in local/mock development. This module answers the same
question on the box, in ~20 ms, for free, offline.

WHY IT IS NOT JUST "RUN MEDIAPIPE"
──────────────────────────────────
MediaPipe's object detector is fast and accurate, but it is a CLOSED-VOCABULARY
model: EfficientDet-Lite is trained on COCO's 80 everyday classes (person, car,
dog, chair, tv...). Measured against this game's actual frames — a 1993
found-footage world of silos, chain-link fences, gas pumps, corridors and
figures in fog — a bare COCO detector finds almost nothing usable, and what it
does find is often wrong: a lone figure under a sodium lamp comes back "tv", an
abandoned filling station comes back as six phantom "car"s. Shipping that alone
would quietly gut SCAN.

So we do not use MediaPipe as the source of LABELS. We use it as the source of
BOXES, and we take the labels from something that already knows exactly what is
in the frame: the scene prompt the world model was steered with to render it
(``state['current_image_prompt']``). In a generated world, "what is out there"
is not something we have to infer from pixels — we wrote it. Only "where is it
on screen" needs looking at.

That split gives each half a job it is actually good at, and lets each check the
other:

  1. PIXELS (MediaPipe) → boxes, plus a coarse COCO category.
  2. PROMPT (lexicon scan) → the open, story-specific vocabulary
     ("armored personnel carrier", not "truck"), plus rough spatial hints
     ("to your left", "in the distance").
  3. CORROBORATION → a COCO hit is only allowed to become a tag if it is either
     a high-value class we trust on its own (people, vehicles, animals — the
     ones SCAN's TALK affordance hangs off) or a class the prompt independently
     mentions. This is what kills the "tv" in the middle of a forest.
  4. ANCHORING → prompt nouns that MediaPipe cannot see at all (the silo, the
     fence, the doorway) are placed on the most salient region of the frame that
     matches their spatial hint, so the tag lands on something rather than
     nowhere.

Output is deliberately the SAME intermediate shape Gemini's structured response
parses into — a list of ``{"label", "box_2d", "kind"}`` with ``box_2d`` as
[ymin, xmin, ymax, xmax] on a 0-1000 grid. That means every downstream rule in
``engine`` (the underwhelming-label filter, the player's-own-hands geometry
backstop, dedupe, the speaks/kind classifier, the max_items cap) applies to
local detections verbatim, with no second copy to keep in sync.

Everything here is optional and defensive: if ``mediapipe`` is not installed or
the model file is missing, ``available()`` is False and the caller falls back to
its previous behaviour. Nothing in this module raises into a request handler.
"""

from __future__ import annotations

import atexit
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).parent.resolve()

# The detector weights. EfficientDet-Lite0, int8-quantized: chosen after
# measuring all of MediaPipe's published object-detector variants on this
# game's own frames. It is the smallest (4.6 MB, so it can live in the repo
# rather than being fetched at boot on an ephemeral filesystem) AND the fastest
# (~16 ms/frame on a Render CPU vs ~20 ms for float32 lite0 and ~39 ms for
# lite2), with recall on this content at least as good as the 13.8 MB float32
# build. Override with DETECT_MODEL_PATH to try another .tflite.
DEFAULT_MODEL_PATH = ROOT / "models" / "efficientdet_lite0_int8.tflite"

# Below this MediaPipe confidence a box is noise. Deliberately lower than you
# would use on photographs: these frames are dark, grainy, VHS-degraded video,
# so true positives come back weak. The corroboration rule below — not the
# threshold — is what keeps precision up.
MIN_SCORE = float(os.getenv("DETECT_LOCAL_MIN_SCORE", "0.22"))

# How many raw boxes to pull before filtering. Cheap, so ask for slack.
MAX_RAW_RESULTS = 12

# Set DETECT_LOCAL_ANCHOR=0 to emit ONLY pixel-backed tags (step 1-3) and skip
# the prompt-anchored ones (step 4). Anchored tags are what make SCAN feel alive
# in a world COCO cannot see, but their positions are inferred rather than
# measured, so there is a switch for anyone who wants boxes they can trust to
# the pixel.
ANCHOR_PROMPT_NOUNS = os.getenv("DETECT_LOCAL_ANCHOR", "1").strip().lower() not in (
    "0", "false", "no", "off",
)


# ─────────────────────────────────────────────────────────────────────────────
# COCO → this world
#
# Two tiers, because the two are trusted differently.
#
# TRUSTED classes are the ones COCO is genuinely good at and that this game
# cares most about: a person is the subject of the entire TALK mechanic, and a
# vehicle or animal is a story beat on its own. These become tags on the
# detector's word alone.
#
# CORROBORATE classes are plausible in a 1993 rural-industrial world but are
# also exactly where a dark frame produces nonsense, so they only survive if the
# scene prompt independently mentions something compatible.
#
# Everything else in COCO (surfboards, giraffes, broccoli, toothbrushes...) is
# absent from both tables and therefore dropped outright — it cannot exist here,
# and a confident false positive on one is pure noise.
# ─────────────────────────────────────────────────────────────────────────────
_COCO_TRUSTED: Dict[str, Tuple[str, str]] = {
    # COCO class          (game kind,  fallback label if the prompt names nothing)
    "person":             ("person",   "figure"),
    "car":                ("object",   "car"),
    "truck":              ("object",   "truck"),
    "bus":                ("object",   "bus"),
    "motorcycle":         ("object",   "motorcycle"),
    "bicycle":            ("object",   "bicycle"),
    "train":              ("object",   "train car"),
    "boat":               ("object",   "boat"),
    "airplane":           ("object",   "aircraft"),
    "dog":                ("animal",   "dog"),
    "cat":                ("animal",   "cat"),
    "bird":               ("animal",   "bird"),
    "horse":              ("animal",   "horse"),
    "cow":                ("animal",   "cattle"),
    "sheep":              ("animal",   "livestock"),
    "bear":               ("animal",   "bear"),
}

_COCO_CORROBORATE: Dict[str, Tuple[str, str]] = {
    "traffic light":      ("machine",  "traffic light"),
    "stop sign":          ("object",   "stop sign"),
    "fire hydrant":       ("object",   "fire hydrant"),
    "parking meter":      ("object",   "parking meter"),
    "bench":              ("object",   "bench"),
    "chair":              ("object",   "chair"),
    "couch":              ("object",   "couch"),
    "bed":                ("object",   "bed"),
    "dining table":       ("object",   "table"),
    "toilet":             ("object",   "toilet"),
    "sink":               ("object",   "sink"),
    "refrigerator":       ("object",   "refrigerator"),
    "oven":               ("object",   "stove"),
    "microwave":          ("object",   "microwave"),
    "tv":                 ("machine",  "television"),
    "laptop":             ("machine",  "computer"),
    "cell phone":         ("machine",  "phone"),
    "clock":              ("object",   "clock"),
    "book":               ("object",   "book"),
    "bottle":             ("object",   "bottle"),
    "cup":                ("object",   "cup"),
    "bowl":               ("object",   "bowl"),
    "knife":              ("object",   "knife"),
    "scissors":           ("object",   "scissors"),
    "vase":               ("object",   "jar"),
    "potted plant":       ("object",   "plant"),
    "backpack":           ("object",   "backpack"),
    "handbag":            ("object",   "bag"),
    "suitcase":           ("object",   "case"),
    "umbrella":           ("object",   "umbrella"),
}

# Which prompt-lexicon categories a COCO class is allowed to borrow a specific
# label from. "truck" may become "armored personnel carrier" because both are
# vehicles; it may not become "silo".
_COCO_COMPATIBLE_CATEGORIES: Dict[str, Tuple[str, ...]] = {
    "person":       ("person", "creature"),
    "car":          ("vehicle",),
    "truck":        ("vehicle",),
    "bus":          ("vehicle",),
    "motorcycle":   ("vehicle",),
    "bicycle":      ("vehicle",),
    "train":        ("vehicle",),
    "boat":         ("vehicle",),
    "airplane":     ("vehicle",),
    "dog":          ("animal", "creature"),
    "cat":          ("animal", "creature"),
    "bird":         ("animal", "creature"),
    "horse":        ("animal", "creature"),
    "cow":          ("animal", "creature"),
    "sheep":        ("animal", "creature"),
    "bear":         ("animal", "creature"),
    "traffic light": ("machine",),
    "tv":           ("machine",),
    "laptop":       ("machine",),
    "cell phone":   ("machine",),
    "bed":          ("prop",),
    "chair":        ("prop",),
    "couch":        ("prop",),
    "dining table": ("prop",),
    "bench":        ("prop",),
}


# ─────────────────────────────────────────────────────────────────────────────
# The prompt lexicon — this world's open vocabulary.
#
# The scene prompt is prose, and we have no POS tagger (and do not want a
# 500 MB NLP dependency for one regex's worth of work). So instead of parsing
# English, we recognise it: a curated set of nouns this world is actually built
# out of, matched with optional adjective modifiers so "rusted corrugated silo"
# survives as one specific tag instead of collapsing to "silo".
#
# This is the same trick the engine already uses for _UNDERWHELMING_LABELS and
# _SPEAKER_LABEL_RE, and it has the same virtue: it is inspectable, and widening
# the world's vocabulary is a one-line edit rather than a retrain.
#
# Category drives both the tag's `kind` and where an un-boxed noun gets anchored
# vertically (sky things high, terrain low, structures across the middle).
# ─────────────────────────────────────────────────────────────────────────────
_LEXICON: Dict[str, Tuple[str, ...]] = {
    "person": (
        "figure", "figures", "silhouette", "man", "woman", "boy", "girl",
        "child", "children", "stranger", "survivor", "guard", "guards",
        "soldier", "soldiers", "sentry", "scientist", "technician", "worker",
        "workers", "officer", "sheriff", "deputy", "ranger", "trooper",
        "driver", "operator", "pilot", "nurse", "doctor", "patient",
        "hitchhiker", "prisoner", "body", "bodies", "corpse", "remains",
        "crowd", "onlooker", "attendant", "watchman", "crew",
    ),
    "creature": (
        "creature", "creatures", "beast", "monster", "thing", "mutant",
        "humanoid", "specimen", "organism", "host", "swarm", "hound",
    ),
    "animal": (
        "dog", "dogs", "coyote", "wolf", "crow", "crows", "raven", "ravens",
        "bird", "birds", "deer", "cattle", "cow", "horse", "rat", "rats",
        "owl", "moth", "moths", "insects", "flies", "snake", "cat",
    ),
    "vehicle": (
        "truck", "trucks", "van", "vans", "sedan", "station wagon", "wagon",
        "pickup", "jeep", "bus", "ambulance", "cruiser", "patrol car",
        "squad car", "car", "cars", "trailer", "semi", "tractor",
        "motorcycle", "helicopter", "chopper", "boat", "tanker",
        "armored personnel carrier", "armored carrier", "armored truck",
        "humvee", "transport", "convoy", "bulldozer", "excavator", "forklift",
        "school bus", "camper", "rv", "hearse", "flatbed",
    ),
    "structure": (
        "barn", "silo", "silos", "water tower", "farmhouse", "house",
        "motel", "gas station", "filling station", "service station",
        "warehouse", "hangar", "bunker", "shed", "outbuilding", "cabin",
        "church", "chapel", "factory", "plant", "facility", "complex",
        "refinery", "processing plant", "tower", "radio tower", "antenna",
        "bridge", "overpass", "underpass", "tunnel", "fence", "fencing",
        "gate", "gates", "gateway", "checkpoint", "guard post", "guardhouse",
        "substation", "transformer", "pumphouse", "wall", "walls", "doorway",
        "door", "doors", "hatch", "window", "windows", "staircase", "stairs",
        "stairwell", "corridor", "hallway", "porch", "roof", "rooftop",
        "chimney", "smokestack", "billboard", "signpost", "kiosk", "booth",
        "trailer home", "mobile home", "diner", "storefront", "garage",
        "loading dock", "catwalk", "scaffolding", "silo cluster", "dam",
        "greenhouse", "morgue", "laboratory", "lab", "clinic", "hospital",
        "schoolhouse", "grain elevator", "quonset hut", "watchtower",
    ),
    "machine": (
        "radio", "payphone", "telephone", "phone", "intercom", "generator",
        "pump", "gas pump", "fuel pump", "valve", "valve wheel",
        "control panel", "console", "terminal", "television", "monitor",
        "screen", "camera", "floodlight", "floodlights", "streetlight",
        "street lamp", "lamp", "lantern", "projector", "tape deck",
        "transmitter", "receiver", "satellite dish", "dish", "compressor",
        "turbine", "breaker box", "fuse box", "switchboard", "speaker",
        "loudspeaker", "siren", "klaxon", "centrifuge", "incubator",
        "respirator", "ventilator", "fan", "conveyor", "winch", "crane",
        "pipe", "pipes", "pipework", "ductwork", "boiler", "furnace",
        "meter", "dial", "gauge cluster", "antenna array", "spotlight",
    ),
    "prop": (
        "crate", "crates", "barrel", "barrels", "drum", "drums", "canister",
        "canisters", "toolbox", "briefcase", "duffel bag", "folder", "file",
        "files", "dossier", "map", "clipboard", "notebook", "journal",
        "ledger", "photograph", "photographs", "badge", "keycard", "key",
        "keys", "lantern", "syringe", "vial", "vials", "specimen jar",
        "gurney", "stretcher", "cot", "mattress", "bed", "chair", "chairs",
        "desk", "table", "cabinet", "locker", "lockers", "shelf",
        "shelving", "sign", "signs", "warning sign", "placard", "notice",
        "tarp", "tarpaulin", "chain", "chains", "padlock", "ladder", "rope",
        "cable", "cables", "wire", "wiring", "toolbelt", "bucket", "crowbar",
        "shovel", "axe", "rifle", "shotgun", "pistol", "casing", "casings",
        "bootprints", "footprints", "tire tracks", "graffiti", "poster",
        "envelope", "letter", "tape", "cassette", "reel", "canvas bag",
        "bag", "bags", "box", "boxes", "container", "containers", "pallet",
    ),
    "terrain": (
        "road", "roadway", "highway", "dirt road", "gravel road", "gravel",
        "driveway", "field", "fields", "cornfield", "pasture", "treeline",
        "forest", "woods", "pines", "brush", "scrub", "sagebrush", "ditch",
        "culvert", "creek", "stream", "river", "pond", "lake", "puddle",
        "puddles", "mud", "ridge", "mesa", "butte", "hill", "hills",
        "cliff", "canyon", "quarry", "pit", "crater", "clearing", "path",
        "trail", "railroad", "railway", "tracks", "powerline", "powerlines",
        "power lines", "telephone pole", "utility pole", "fenceline",
        "embankment", "shoulder", "median", "parking lot", "lot", "yard",
        "snow", "sand", "dunes", "grass", "weeds", "stumps", "boulder",
        "boulders", "rocks", "rubble field", "salt flat", "marsh", "swamp",
    ),
    "hazard": (
        "fire", "flames", "smoke", "blood", "bloodstain", "wreckage",
        "debris", "rubble", "carcass", "growth", "fungus", "mold", "spores",
        "slime", "tendrils", "sinkhole", "barbed wire", "razor wire", "trap",
        "spill", "contamination", "leak", "sparks", "steam", "crack",
        "collapse", "cave-in", "quarantine tent", "biohazard", "warning tape",
        "police tape", "hazard tape", "crater rim", "ash", "soot",
    ),
    # Only things up there you would actually point a camera AT. Pure
    # atmosphere — sunset, overcast, haze, mist — is deliberately absent: it is
    # in almost every prompt this world writes, it is never a point of interest,
    # and it would win a tag slot on every single scan. The Gemini prompt draws
    # the same line ("skip generic background like 'sky', 'ground', 'wall'").
    "sky": (
        "moon", "stars", "lightning", "searchlight", "flare",
        "helicopter light", "smoke column", "storm front",
    ),
}

# Adjectives allowed in front of a lexicon noun, so the tag keeps the prompt's
# specificity ("rusted silo", "collapsed barn", "chain-link fence"). Capped at
# two modifiers by the pattern below, which keeps labels inside the 1-3 word
# shape the client's tag layout was designed around.
_ADJECTIVES = (
    "rusted", "rusting", "rusty", "abandoned", "derelict", "collapsed",
    "collapsing", "burnt", "burned", "charred", "broken", "shattered",
    "cracked", "weathered", "corroded", "dented", "overgrown", "wet",
    "muddy", "bloodied", "bloody", "dark", "darkened", "distant", "nearby",
    "massive", "huge", "enormous", "small", "tall", "low", "narrow", "wide",
    "old", "ancient", "makeshift", "military", "tactical", "chain-link",
    "chainlink", "corrugated", "concrete", "steel", "metal", "wooden",
    "brick", "glass", "plastic", "canvas", "sodium", "fluorescent",
    "flickering", "humming", "idling", "open", "closed", "locked", "sealed",
    "empty", "dead", "rotting", "pale", "white", "black", "red", "green",
    "blue", "yellow", "orange", "grey", "gray", "silver", "rust-streaked",
    "half-buried", "windowless", "unlit", "floodlit", "smoldering",
    "waterlogged", "frozen", "twisted", "toppled", "listing", "sagging",
)

# Nouns that name the PLAYER's own body or gear. The found-footage conceit puts
# these in the prompt constantly ("your flashlight beam", "the camcorder's
# viewfinder"), and they are never a point of interest — the engine already
# filters them out of Gemini's answers, so there is no reason to manufacture
# them here in the first place.
_SELF_NOUNS = frozenset({
    "flashlight", "torch", "camcorder", "camera lens", "viewfinder",
    "hand", "hands", "glove", "gloves", "arm", "arms", "boot", "boots",
    "steering wheel", "dashboard", "windshield", "mirror", "seat",
    "gauge", "gauges", "speedometer", "wiper", "wipers", "lens",
})

# Longest-first so "armored personnel carrier" wins over "carrier", and
# "gas pump" over "pump".
_ALL_NOUNS = sorted(
    {n for nouns in _LEXICON.values() for n in nouns} - _SELF_NOUNS,
    key=lambda s: (-len(s), s),
)
_NOUN_CATEGORY: Dict[str, str] = {}
for _cat, _nouns in _LEXICON.items():
    for _n in _nouns:
        # First category to claim a noun wins; the ordering of _LEXICON puts the
        # story-louder categories (person, creature) before the generic ones, so
        # an ambiguous word like "bed" lands on "prop" rather than fighting.
        _NOUN_CATEGORY.setdefault(_n, _cat)

_NOUN_PHRASE_RE = re.compile(
    r"\b(?:(?:{adj})\s+){{0,2}}(?:{nouns})\b".format(
        adj="|".join(re.escape(a) for a in _ADJECTIVES),
        nouns="|".join(re.escape(n) for n in _ALL_NOUNS),
    ),
    re.IGNORECASE,
)
_NOUN_HEAD_RE = re.compile(
    r"(?:{nouns})\b".format(nouns="|".join(re.escape(n) for n in _ALL_NOUNS)),
    re.IGNORECASE,
)

# Spatial language the prompts genuinely use — the image-prompt templates even
# emit an explicit "🗺️ SPATIAL ANCHOR" preamble describing where things sit
# relative to the camera. Free localization signal; worth reading.
_LEFT_RE = re.compile(r"\b(?:to (?:your|the) left|on (?:your|the) left|left of|leftward|port side)\b", re.I)
_RIGHT_RE = re.compile(r"\b(?:to (?:your|the) right|on (?:your|the) right|right of|rightward|starboard)\b", re.I)
_AHEAD_RE = re.compile(r"\b(?:ahead|in front|straight on|directly before you|centered|center of)\b", re.I)
_ABOVE_RE = re.compile(r"\b(?:above|overhead|up high|skyward|against the sky|silhouetted against)\b", re.I)
_BELOW_RE = re.compile(r"\b(?:below|underfoot|at your feet|on the ground|ground level|beneath)\b", re.I)
_FAR_RE = re.compile(r"\b(?:distant|in the distance|far off|far away|on the horizon|miles|beyond)\b", re.I)
_NEAR_RE = re.compile(r"\b(?:close|closer|near(?:by)?|just ahead|right there|looming|inches)\b", re.I)

# Where a category sits vertically when nothing else says otherwise, as a
# fraction of frame height (0 = top). Derived from how these frames are actually
# composed: a horizon around 0.45, sky above it, ground falling away below.
_CATEGORY_VERTICAL: Dict[str, float] = {
    "sky":       0.16,
    "structure": 0.42,
    "machine":   0.52,
    "person":    0.55,
    "creature":  0.55,
    "animal":     0.62,
    "vehicle":   0.58,
    "hazard":    0.62,
    "prop":      0.68,
    "terrain":   0.78,
}

# Rough on-screen footprint per category, before the depth hint scales it. These
# only need to be plausible: the client draws a tag at the box CENTER and uses
# the size for prominence ordering and the photo-target bracket, so being a
# little off is invisible, whereas being wildly off (a full-frame box) trips the
# engine's degenerate-box guard.
_CATEGORY_SIZE: Dict[str, Tuple[float, float]] = {
    "sky":       (0.26, 0.16),
    "structure": (0.30, 0.28),
    "machine":   (0.16, 0.20),
    "person":    (0.10, 0.26),
    "creature":  (0.14, 0.24),
    "animal":     (0.12, 0.14),
    "vehicle":   (0.28, 0.22),
    "hazard":    (0.20, 0.18),
    "prop":      (0.14, 0.14),
    "terrain":   (0.34, 0.20),
}

# Which categories deserve a tag slot first when the prompt names more things
# than SCAN can show. Prompt order alone is a poor ranking: these prompts often
# mention an incidental "length of chain" in the opening sentence and the silo
# that dominates the frame in the third. Story weight beats word order —
# a person you can TALK to outranks a padlock every time.
_CATEGORY_PRIORITY: Dict[str, int] = {
    "person":    0,
    "creature":  1,
    "vehicle":   2,
    "structure": 3,
    "machine":   4,
    "hazard":    5,
    "animal":     6,
    "prop":      7,
    "terrain":   8,
    "sky":       9,
}

# Categories we are willing to place from the prompt alone, without the detector
# having seen anything there.
#
# The asymmetry is the whole point. For a silo, a doorway or a fence, COCO has no
# class at all, so "the detector found nothing" carries no information and the
# prompt is our only witness — anchoring is strictly better than showing the
# player an empty screen. For a PERSON or a CREATURE the reverse is true: people
# are the class COCO is best at, so silence really is evidence of absence. And
# the cost of being wrong is asymmetric too — a person tag sets ``speaks``, which
# offers the player a TALK conversation with a patch of empty gravel. So those
# two must be backed by actual pixels or not appear at all.
#
# Vehicles sit on the permissive side deliberately: a misplaced truck tag is a
# cosmetic miss, not a broken mechanic, and in frames this dark the detector
# routinely cannot see a vehicle the prompt explicitly put there.
_ANCHORABLE_CATEGORIES = frozenset({
    "vehicle", "structure", "machine", "prop", "terrain", "hazard", "sky",
    "animal",
})

_CATEGORY_KIND: Dict[str, str] = {
    "person":    "person",
    "creature":  "creature",
    "animal":     "animal",
    "machine":   "machine",
    "vehicle":   "object",
    "structure": "object",
    "prop":      "object",
    "terrain":   "object",
    "hazard":    "object",
    "sky":       "object",
}


# ─────────────────────────────────────────────────────────────────────────────
# Detector lifecycle
#
# MediaPipe's ObjectDetector is NOT thread-safe, and this app serves on gunicorn
# gthread with several threads. One process-wide instance behind a lock is the
# right trade: inference is ~16 ms, SCAN fires at a ~2.5 s cadence, so threads
# effectively never queue, and we avoid paying a fresh graph build (hundreds of
# ms) per call or holding N copies of the model in RAM.
# ─────────────────────────────────────────────────────────────────────────────
_detector = None
_detector_lock = threading.Lock()
_load_failed = False
_load_error = ""


def _close_detector() -> None:
    """Release the detector while the interpreter is still intact.

    MediaPipe's ObjectDetector closes itself from ``__del__``, which during
    interpreter shutdown runs after the module globals it needs have already
    been torn down — producing an "Exception ignored in __del__: TypeError:
    'NoneType' object is not callable" on every clean exit. Closing at atexit
    happens early enough to avoid it, and keeps production logs free of a
    scary-looking traceback that means nothing.
    """
    global _detector
    detector, _detector = _detector, None
    if detector is not None:
        try:
            detector.close()
        except Exception:  # noqa: BLE001
            pass


atexit.register(_close_detector)


def model_path() -> Path:
    override = (os.getenv("DETECT_MODEL_PATH") or "").strip()
    return Path(override).expanduser().resolve() if override else DEFAULT_MODEL_PATH


def _log(message: str) -> None:
    print(f"[LOCAL VISION] {message}", flush=True)


def _load_detector():
    """Build the process-wide detector, or record why we can't. Caller holds the lock."""
    global _detector, _load_failed, _load_error
    if _detector is not None or _load_failed:
        return _detector

    path = model_path()
    if not path.exists():
        _load_failed = True
        _load_error = f"model file not found at {path}"
        _log(f"unavailable: {_load_error}")
        return None
    try:
        # Imported lazily and exactly once: `mediapipe` pulls in TensorFlow Lite
        # plus OpenCV and costs real time and memory at import. A box that never
        # runs local detection should never pay for it.
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision

        options = mp_vision.ObjectDetectorOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(path)),
            running_mode=mp_vision.RunningMode.IMAGE,
            score_threshold=MIN_SCORE,
            max_results=MAX_RAW_RESULTS,
        )
        _detector = mp_vision.ObjectDetector.create_from_options(options)
        _log(f"detector ready ({path.name}, score>={MIN_SCORE})")
    except Exception as e:  # noqa: BLE001 — absence must never break the app
        _load_failed = True
        _load_error = f"{type(e).__name__}: {e}"
        _log(f"unavailable: {_load_error}")
        _detector = None
    return _detector


def available() -> bool:
    """True if local detection can actually run right now."""
    if _load_failed:
        return False
    with _detector_lock:
        return _load_detector() is not None


def warmup() -> bool:
    """Build the detector ahead of the first request.

    Worth calling at boot from the main thread: it moves the one-off mediapipe
    import and graph build off the critical path of a player's first SCAN, and
    keeps a heavyweight import from happening inside a worker thread (this
    codebase has been bitten by threaded import locks before — see the warm-up
    block at the top of engine.py).
    """
    return available()


def status() -> Dict[str, Any]:
    """Diagnostics for /api/health-style reporting."""
    return {
        "available": available(),
        "model": str(model_path()),
        "model_present": model_path().exists(),
        "min_score": MIN_SCORE,
        "anchor_prompt_nouns": ANCHOR_PROMPT_NOUNS,
        "error": _load_error or None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Prompt reading
# ─────────────────────────────────────────────────────────────────────────────
class PromptNoun:
    """One candidate object lifted out of the scene prompt.

    ``order`` is its position in the prompt, which is a decent prominence proxy:
    these prompts lead with the subject of the shot and trail off into ambience.
    """

    __slots__ = ("phrase", "head", "category", "order", "horizontal", "depth")

    def __init__(self, phrase: str, head: str, category: str, order: int,
                 horizontal: Optional[str], depth: Optional[str]):
        self.phrase = phrase
        self.head = head
        self.category = category
        self.order = order
        self.horizontal = horizontal   # "left" | "right" | "center" | None
        self.depth = depth             # "near" | "far" | None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"PromptNoun({self.phrase!r}, {self.category}, "
                f"h={self.horizontal}, d={self.depth})")


def _is_proper_name(text: str, match: "re.Match") -> bool:
    """True if this match is part of a place or person NAME, not a thing.

    These prompts are full of invented proper nouns built from ordinary words —
    "Hollis Ridge", "Kestrel Plant", "Cold Creek" — and matching the common noun
    inside one produces a tag for a landmark that is nowhere on screen. A
    capitalized match immediately preceded by another capitalized word is
    overwhelmingly a name; a sentence-initial capital is not, so require the
    preceding word to be capitalized rather than just checking our own casing.
    """
    if not match.group(0)[:1].isupper():
        return False
    before = text[max(0, match.start() - 40):match.start()].rstrip()
    if not before or before[-1] in ".!?":
        return False
    prior_word = re.search(r"([A-Za-z][\w'-]*)$", before)
    return bool(prior_word and prior_word.group(1)[:1].isupper())


_SENTENCE_BREAK_RE = re.compile(r"[.!?\n;]")


def _sentence_around(text: str, match: "re.Match") -> str:
    """The sentence a mention sits in.

    Spatial hints have to be read from the SAME sentence as the noun they
    describe. A fixed character window bleeds across the boundary and mislabels
    things: in "A barn sits to your left. A distant water tower breaks the
    horizon on the right." a backward window from "water tower" reaches "to your
    left" and puts the tower on the wrong side of the frame.
    """
    starts = [m.end() for m in _SENTENCE_BREAK_RE.finditer(text, 0, match.start())]
    start = starts[-1] if starts else 0
    end_match = _SENTENCE_BREAK_RE.search(text, match.end())
    end = end_match.start() if end_match else len(text)
    return text[start:end]


def _clause_hints(clause: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Read (horizontal, vertical, depth) hints out of the text around a noun."""
    horizontal = None
    if _LEFT_RE.search(clause):
        horizontal = "left"
    elif _RIGHT_RE.search(clause):
        horizontal = "right"
    elif _AHEAD_RE.search(clause):
        horizontal = "center"

    vertical = None
    if _ABOVE_RE.search(clause):
        vertical = "high"
    elif _BELOW_RE.search(clause):
        vertical = "low"

    depth = None
    if _FAR_RE.search(clause):
        depth = "far"
    elif _NEAR_RE.search(clause):
        depth = "near"
    return horizontal, vertical, depth


def prompt_nouns(scene_prompt: str, limit: int = 24) -> List[PromptNoun]:
    """Extract candidate objects, in prompt order, from a scene prompt.

    Deduplicated on the head noun, so "the silo" and "that rusted silo" later in
    the same prompt produce one tag, keeping the FIRST (usually most specific)
    phrasing.
    """
    text = (scene_prompt or "").strip()
    if not text:
        return []

    found: List[PromptNoun] = []
    seen_heads = set()
    for order, match in enumerate(_NOUN_PHRASE_RE.finditer(text)):
        raw = match.group(0).strip()
        if _is_proper_name(text, match):
            continue
        phrase = re.sub(r"\s+", " ", raw.lower())
        head_match = _NOUN_HEAD_RE.search(phrase)
        if not head_match:
            continue
        head = head_match.group(0).lower()
        if head in _SELF_NOUNS or head in seen_heads:
            continue
        # A phrase whose modifier is itself a self-noun is the player's own gear
        # dressed up ("gloved hand", "camcorder viewfinder"). Skip it here so it
        # never costs a slot; engine's filter would drop it anyway.
        if any(w in _SELF_NOUNS for w in phrase.split()):
            continue
        category = _NOUN_CATEGORY.get(head)
        if not category:
            continue
        seen_heads.add(head)

        horizontal, vertical, depth = _clause_hints(_sentence_around(text, match))
        noun = PromptNoun(phrase[:40], head, category, order, horizontal, depth)
        if vertical == "high":
            noun.category = "sky" if category in ("terrain", "hazard") else category
        found.append(noun)
        if vertical == "low" and category not in ("terrain",):
            noun.depth = noun.depth or "near"
        if len(found) >= limit:
            break

    found.sort(key=lambda n: (_CATEGORY_PRIORITY.get(n.category, 9), n.order))
    return found


# ─────────────────────────────────────────────────────────────────────────────
# Pixels
# ─────────────────────────────────────────────────────────────────────────────
def _decode_rgb(image_bytes: bytes):
    """Frame bytes → (contiguous uint8 RGB ndarray, width, height).

    Downscaled to at most 640 px wide. The client already caps its capture near
    that, but a path-based caller (the TALK snapshot reads a full scene render
    off disk) can hand us something much bigger, and EfficientDet-Lite resizes
    to 320x320 internally anyway — so shrinking first is free accuracy-neutral
    speed.
    """
    import io

    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as im:
        im = im.convert("RGB")
        if im.width > 640:
            scale = 640.0 / float(im.width)
            im = im.resize((640, max(1, int(round(im.height * scale)))), Image.BILINEAR)
        arr = np.ascontiguousarray(np.asarray(im, dtype=np.uint8))
    return arr, arr.shape[1], arr.shape[0]


def _mediapipe_boxes(rgb) -> List[Tuple[str, float, Tuple[float, float, float, float]]]:
    """Run the detector. Returns [(coco_label, score, (x0, y0, x1, y1) in 0..1)]."""
    import mediapipe as mp

    height, width = rgb.shape[0], rgb.shape[1]
    image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

    with _detector_lock:
        detector = _load_detector()
        if detector is None:
            return []
        result = detector.detect(image)

    out: List[Tuple[str, float, Tuple[float, float, float, float]]] = []
    for det in getattr(result, "detections", None) or []:
        categories = getattr(det, "categories", None) or []
        box = getattr(det, "bounding_box", None)
        if not categories or box is None:
            continue
        name = (categories[0].category_name or "").strip().lower()
        score = float(categories[0].score or 0.0)
        x0 = max(0.0, min(1.0, box.origin_x / float(width)))
        y0 = max(0.0, min(1.0, box.origin_y / float(height)))
        x1 = max(0.0, min(1.0, (box.origin_x + box.width) / float(width)))
        y1 = max(0.0, min(1.0, (box.origin_y + box.height) / float(height)))
        if x1 <= x0 or y1 <= y0:
            continue
        out.append((name, score, (x0, y0, x1, y1)))
    out.sort(key=lambda t: -t[1])
    return out


# A pixel detection smaller than this fraction of the frame is a pinpoint, not
# a thing to look at: the tag would have nothing under it, and on these grainy
# transfers sub-2% boxes are reliably tape noise rather than objects. (Measured:
# a 1.7% x 1.6% "person" at 0.23 confidence sitting on a frame's burned-in
# timecode.)
_MIN_BOX_EDGE = 0.02
_MIN_BOX_AREA = 0.0015


def _is_operator_foreground(box: Tuple[float, float, float, float]) -> bool:
    """True for the camera operator's own hand / arm / gear / vehicle hood.

    This is a found-footage, camera-in-hand conceit, so in EVERY first-person
    frame the player's own body juts up from the bottom edge — and it is the one
    thing COCO is confident about, because a hand gripping a flashlight reads as
    a textbook "person". Measured on this game's own frames, the single
    highest-confidence detection in the flagship exterior shot is the player's
    hand at 0.66, and left alone it becomes a "figure" the UI offers to TALK to.

    The tell is that the operator's body is CLIPPED by the frame: it runs off the
    bottom edge and its mass sits below the midline. A subject that is genuinely
    in the world — even one lying in the near foreground — almost always has some
    ground visible beneath it, so its box stops short of the edge.

    ``engine`` has its own version of this backstop for Gemini's answers, but
    that one requires a tall, narrow column (h >= 0.5 and h > w) and so lets this
    hand through: it is only 0.377 tall. Hence a stricter, edge-based rule here.
    """
    _x0, y0, _x1, y1 = box
    return y1 >= 0.96 and (y0 + y1) / 2.0 >= 0.58


def _saliency_grid(rgb, grid_w: int = 64):
    """A coarse "where is there something to look at" map, as a 2D float array.

    Not a learned saliency model — just local structure plus local contrast,
    which on these frames tracks the things a player would actually notice: lit
    windows, silhouettes against sky, the edge of a structure against fog. It is
    only ever used to CHOOSE AMONG candidate positions for a label we already
    know is in the frame, so cheap and roughly right beats slow and precise.
    """
    import numpy as np
    from PIL import Image

    height, width = rgb.shape[0], rgb.shape[1]
    grid_h = max(8, int(round(grid_w * height / float(max(1, width)))))

    # Box-filtered downsample = mean pooling, which is the aggregation we want.
    with Image.fromarray(rgb) as im:
        small = np.asarray(
            im.convert("L").resize((grid_w, grid_h), Image.BOX), dtype=np.float32
        )

    gy, gx = np.gradient(small)
    structure = np.hypot(gx, gy)
    contrast = np.abs(small - float(small.mean()))

    def norm(a):
        span = float(a.max() - a.min())
        return (a - a.min()) / span if span > 1e-6 else np.zeros_like(a)

    sal = 0.7 * norm(structure) + 0.3 * norm(contrast)

    # Blur so a single hot pixel (a speck of tape noise) can't beat a broad,
    # genuinely interesting region.
    padded = np.pad(sal, 1, mode="edge")
    blurred = sum(
        padded[dy:dy + grid_h, dx:dx + grid_w]
        for dy in range(3) for dx in range(3)
    ) / 9.0
    return blurred


def _overlap(a: Tuple[float, float, float, float],
             b: Tuple[float, float, float, float]) -> float:
    """Intersection over the smaller box — a better "is this the same spot?"
    test than IoU when the two boxes are very different sizes."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return inter / smaller if smaller > 1e-6 else 0.0


def _anchor(noun: PromptNoun, sal, occupied: List[Tuple[float, float, float, float]]):
    """Place a prompt noun on the frame: the most salient free spot in its zone.

    Returns (x0, y0, x1, y1) in 0..1, or None if the zone is already taken.
    """
    import numpy as np

    grid_h, grid_w = sal.shape
    base_w, base_h = _CATEGORY_SIZE.get(noun.category, (0.18, 0.18))
    if noun.depth == "far":
        base_w, base_h = base_w * 0.55, base_h * 0.55
    elif noun.depth == "near":
        base_w, base_h = base_w * 1.45, base_h * 1.45
    base_w = min(0.8, max(0.04, base_w))
    base_h = min(0.8, max(0.04, base_h))

    # Horizontal search window from the prompt's own words; vertical from the
    # category's habitual place in the frame, widened enough to let saliency
    # actually choose rather than just confirming the prior.
    if noun.horizontal == "left":
        x_lo, x_hi = 0.02, 0.42
    elif noun.horizontal == "right":
        x_lo, x_hi = 0.58, 0.98
    elif noun.horizontal == "center":
        x_lo, x_hi = 0.30, 0.70
    else:
        x_lo, x_hi = 0.02, 0.98

    cy_prior = _CATEGORY_VERTICAL.get(noun.category, 0.55)
    y_lo, y_hi = max(0.02, cy_prior - 0.18), min(0.98, cy_prior + 0.18)

    col_lo = int(x_lo * grid_w)
    col_hi = max(col_lo + 1, int(x_hi * grid_w))
    row_lo = int(y_lo * grid_h)
    row_hi = max(row_lo + 1, int(y_hi * grid_h))

    window = sal[row_lo:row_hi, col_lo:col_hi]
    if window.size == 0:
        return None

    # Walk the zone's peaks from brightest down until one lands somewhere not
    # already claimed by another tag.
    flat = np.argsort(window.ravel())[::-1]
    for idx in flat[:48]:
        row, col = np.unravel_index(idx, window.shape)
        cx = (col_lo + col + 0.5) / float(grid_w)
        cy = (row_lo + row + 0.5) / float(grid_h)
        x0 = max(0.0, cx - base_w / 2.0)
        x1 = min(1.0, cx + base_w / 2.0)
        y0 = max(0.0, cy - base_h / 2.0)
        y1 = min(1.0, cy + base_h / 2.0)
        candidate = (x0, y0, x1, y1)
        if all(_overlap(candidate, taken) < 0.45 for taken in occupied):
            return candidate
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Fusion — the actual entry point
# ─────────────────────────────────────────────────────────────────────────────
def detect(image_bytes: bytes,
           mime_type: Optional[str] = None,
           max_items: int = 8,
           scene_prompt: str = "") -> List[Dict[str, Any]]:
    """Detect objects in a frame, locally.

    Returns entries in the SAME shape ``engine._detect_objects`` parses out of
    Gemini's structured response, so the caller's normalization and filtering
    apply unchanged::

        {"label": str,
         "box_2d": [ymin, xmin, ymax, xmax] on a 0-1000 grid,
         "kind": one of person/character/creature/animal/machine/object,
         "source": "pixels" | "prompt"}

    ``source`` is extra (Gemini never sends it) and purely for debugging and
    tests: "pixels" means MediaPipe measured that box, "prompt" means we placed
    a label the prompt named onto the most salient matching region. The client
    ignores unknown keys.

    Never raises: returns [] on any failure, exactly like the Gemini path.
    """
    if not image_bytes:
        return []
    try:
        rgb, _width, _height = _decode_rgb(image_bytes)
    except Exception as e:  # noqa: BLE001
        _log(f"could not decode frame: {type(e).__name__}: {e}")
        return []

    try:
        raw = _mediapipe_boxes(rgb)
    except Exception as e:  # noqa: BLE001
        _log(f"detector failed: {type(e).__name__}: {e}")
        raw = []

    nouns = prompt_nouns(scene_prompt)
    unclaimed = list(nouns)
    results: List[Dict[str, Any]] = []
    occupied: List[Tuple[float, float, float, float]] = []
    used_labels = set()

    def emit(label: str, kind: str, box: Tuple[float, float, float, float], source: str) -> bool:
        key = label[:24]
        if not label or key in used_labels:
            return False
        used_labels.add(key)
        x0, y0, x1, y1 = box
        results.append({
            "label": label,
            # 0-1000 ints in [ymin, xmin, ymax, xmax] order: Gemini's convention,
            # which engine._detect_objects already knows how to normalize.
            "box_2d": [round(y0 * 1000), round(x0 * 1000),
                       round(y1 * 1000), round(x1 * 1000)],
            "kind": kind,
            "source": source,
        })
        occupied.append(box)
        return True

    # ── 1-3. Pixel-backed detections, relabelled from the prompt where possible.
    for coco_label, score, box in raw:
        if len(results) >= max_items:
            break
        x0, y0, x1, y1 = box
        if (x1 - x0) < _MIN_BOX_EDGE or (y1 - y0) < _MIN_BOX_EDGE:
            continue
        if (x1 - x0) * (y1 - y0) < _MIN_BOX_AREA:
            continue
        if _is_operator_foreground(box):
            continue
        trusted = coco_label in _COCO_TRUSTED
        entry = _COCO_TRUSTED.get(coco_label) or _COCO_CORROBORATE.get(coco_label)
        if entry is None:
            continue  # not a thing that can exist in this world
        kind, fallback_label = entry

        # Borrow the prompt's specific name for this thing when it named one.
        compatible = _COCO_COMPATIBLE_CATEGORIES.get(coco_label, ())
        claimed = None
        for noun in unclaimed:
            if noun.category in compatible:
                claimed = noun
                break

        if claimed is None and not trusted:
            # A corroborate-tier class with nothing in the prompt to back it up:
            # this is where dark frames invent televisions in forests. Drop it.
            continue

        if claimed is not None:
            unclaimed.remove(claimed)
            label = claimed.phrase
            kind = _CATEGORY_KIND.get(claimed.category, kind)
        else:
            label = fallback_label
        emit(label, kind, box, "pixels")

    # ── 4. Everything the prompt named that the detector cannot see.
    if ANCHOR_PROMPT_NOUNS:
        try:
            sal = _saliency_grid(rgb)
        except Exception as e:  # noqa: BLE001
            _log(f"saliency failed: {type(e).__name__}: {e}")
            sal = None
        if sal is not None:
            for noun in unclaimed:
                if len(results) >= max_items:
                    break
                if noun.category not in _ANCHORABLE_CATEGORIES:
                    continue
                box = _anchor(noun, sal, occupied)
                if box is None:
                    continue
                emit(noun.phrase, _CATEGORY_KIND.get(noun.category, "object"),
                     box, "prompt")

    return results[:max_items]
