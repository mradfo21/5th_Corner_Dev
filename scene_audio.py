"""
scene_audio.py — scene music and world sound from ElevenLabs.

Turns the scene descriptor that already rides along with every guide image
(the `metadata.prompt` the engine emits) into:

  • a short instrumental bed via ElevenLabs Music (`music_v2`)
  • a looping ambience clip via ElevenLabs Sound Effects
  • encounter stingers (pre-cached stock catalog, not per-scene)

The standalone UI loops the bed + ambience and crossfades on each new scene.
Stock stingers live under ``assets/music/stock/`` so encounter hits do not
wait on a live generation.

Everything degrades gracefully: if `ELEVENLABS_API_KEY` is unset or a call
fails, `get_scene_audio()` returns ``None`` (or stock-only URLs when those
files already exist) and the client stays silent on the missing layer.
"""

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
import paths as _paths  # where the game writes (M2): the repo from source, %APPDATA%/ABYSS built

try:
    import cost_tracker
except Exception:
    class _NoopCostTracker:
        def record_usage(self, *args, **kwargs):
            return None

    cost_tracker = _NoopCostTracker()

try:
    _CONFIG = json.load((ROOT / "config.json").open(encoding="utf-8"))
except Exception:
    _CONFIG = {}

# Seed only. Call `_api_key()` at use-time — keys_store can patch this
# attribute AND os.environ after import.
ELEVENLABS_API_KEY = (
    os.getenv("ELEVENLABS_API_KEY") or _CONFIG.get("ELEVENLABS_API_KEY") or ""
).strip()

MUSIC_MODEL = "music_v2"
SFX_MODEL = "eleven_text_to_sound_v2"
ELEVEN_MUSIC_URL = "https://api.elevenlabs.io/v1/music"
ELEVEN_SFX_URL = "https://api.elevenlabs.io/v1/sound-generation"

# Scene beds used to be 12s Lyria loops. A bit longer hides the loop point
# and Eleven Music's minimum is 3s.
DEFAULT_CLIP_SECONDS = 20
DEFAULT_SFX_SECONDS = 14
# ElevenLabs sound-generation rejects anything longer, with a 400 rather than
# a truncation: "expected a maximum number of 450 characters". Both SFX lanes
# used to clip at 500 and a long scene descriptor simply failed to make any
# sound — silently, because a failed effect is supposed to be survivable.
SFX_TEXT_MAX = 450
_MUSIC_TIMEOUT_SECONDS = 90
_SFX_TIMEOUT_SECONDS = 45


def _api_key() -> str:
    return (
        os.environ.get("ELEVENLABS_API_KEY") or ELEVENLABS_API_KEY or ""
    ).strip()


# ────────────────────────────────────────────────────────────────────────────
# Prompt mapping: scene descriptor -> weighted prompts + generation config
# (same shape the unit tests already assert; flattened for Eleven Music)
# ────────────────────────────────────────────────────────────────────────────

_MOOD_CUES = [
    (("battle", "fight", "chase", "run", "escape", "explosion", "alarm", "attack"),
     "urgent, driving, percussive tension", 128, 0.7),
    (("horror", "terror", "monster", "blood", "corpse", "nightmare", "dread", "haunt"),
     "dark ambient horror, dissonant drones, unsettling", 70, 0.25),
    (("ruin", "abandoned", "decay", "derelict", "empty", "desolate", "wasteland"),
     "bleak, sparse, haunting ambient", 68, 0.3),
    (("forest", "jungle", "garden", "trees", "nature", "meadow", "river", "ocean", "sea"),
     "organic, lush, natural ambience with soft pads", 84, 0.6),
    (("city", "street", "neon", "market", "crowd", "traffic", "station"),
     "cinematic urban underscore, low synth pulse", 96, 0.55),
    (("temple", "shrine", "cathedral", "sacred", "ritual", "ancient"),
     "solemn, reverent, cavernous reverb, choral pads", 66, 0.4),
    (("space", "stars", "void", "cosmic", "nebula", "orbit", "station"),
     "vast cosmic ambient, weightless synth textures", 72, 0.5),
    (("snow", "ice", "frozen", "cold", "winter", "tundra"),
     "cold, crystalline, sparse ambient", 74, 0.45),
    (("cave", "tunnel", "underground", "basement", "sewer", "mine", "dark"),
     "claustrophobic low drones, dripping reverb", 64, 0.2),
    (("dream", "surreal", "strange", "shimmer", "glow", "ethereal"),
     "dreamy, ethereal, shimmering ambient", 80, 0.65),
]

_STYLE_ANCHORS = "cinematic instrumental score, atmospheric, no vocals, no drums lead"

_CONVERSATION_STYLE_ANCHORS = (
    "intimate cinematic underscore, warm low strings, soft piano, hushed pads, "
    "no vocals, no drums lead, dialogue-friendly sparse arrangement"
)
# Encounter beds recolor from the music_prompt the client sends after the
# brief lands: "stance — kind — label — danger — stakes". Creature is
# checked first so kind wins over a generic hostile stance.
_ENCOUNTER_STANCE_CUES = [
    (("creature", "monster", "inhuman", "beast"),
     "unsettling organic confrontation drone, wet texture, held breath", 62, 0.22),
    (("desperate", "frantic", "panic"),
     "frantic pulse, taut strings, short breath, danger accelerating", 92, 0.38),
    (("opportunistic", "sly", "quiet threat"),
     "quiet predatory hush, sparse analog, danger waiting", 64, 0.26),
    (("hostile", "threat", "attack"),
     "tense low-drone confrontation, analog-horror pulse, held breath", 72, 0.28),
]
_CONVERSATION_KIND_CUES = [
    (("machine", "radio", "intercom", "terminal", "static"),
     "cold electronic hum, distant radio static beds, tense intimacy", 72, 0.35),
    (("creature", "monster", "inhuman", "strange"),
     "unsettling intimate drones, close mic texture, held breath", 66, 0.3),
    (("animal", "dog", "cat", "bird"),
     "gentle organic pads, soft flute-like tones, quiet wonder", 76, 0.5),
    (("hostile", "threat", "afraid", "danger", "gun"),
     "taut low strings, heartbeat pulse, whispered tension", 88, 0.4),
]


def _clean_scene_text(scene_prompt: str) -> str:
    """Compress a (possibly long, comma-stuffed image) prompt into a short
    descriptor suitable as a music style cue."""
    if not scene_prompt:
        return ""
    text = " ".join(str(scene_prompt).split())
    # Image prompts open with a style stamp that barely changes. Scoring the
    # first 240 characters made every turn the same piece of music. The unique
    # shot — what the camera sees now — is at the end.
    if len(text) > 240:
        return text[-240:]
    return text


def _scene_to_music_prompt(scene_prompt: str, mode: str = "scene",
                           direction: str | None = None):
    """Map a scene descriptor to (weighted_prompts, generation_config_kwargs).

    Returns plain data (list of {text, weight} dicts + a kwargs dict) so this is
    unit-testable without a network call. Eleven Music gets the flattened text.
    """
    scene = _clean_scene_text(scene_prompt)
    low = scene.lower()
    mode = (mode or "scene").strip().lower()

    if mode == "verbatim":
        return ([{"text": scene, "weight": 1.0}],
                {"bpm": 80, "temperature": 1.0, "guidance": 4.0})

    if mode == "encounter":
        mood_phrase = "tense low-drone confrontation, analog-horror pulse, held breath"
        bpm = 68
        brightness = 0.28
        for keywords, phrase, cue_bpm, cue_bright in _ENCOUNTER_STANCE_CUES:
            if any(k in low for k in keywords):
                mood_phrase = phrase
                bpm = cue_bpm
                brightness = cue_bright
                break
        prompts = [
            {"text": (scene or "a sudden confrontation"), "weight": 1.0},
            {"text": mood_phrase, "weight": 1.2},
            {"text": "sparse analog underscore, no melody, danger in the room", "weight": 0.8},
        ]
        if direction is None:
            direction = get_music_direction()
        if direction:
            prompts.insert(0, {"text": direction, "weight": 1.1})
        return (prompts, {"bpm": bpm, "temperature": 1.05, "guidance": 4.2,
                          "brightness": brightness})

    if mode == "conversation":
        mood_phrase = "warm, intimate, hushed cinematic conversation underscore"
        bpm = 74
        brightness = 0.45
        for keywords, phrase, cue_bpm, cue_bright in _CONVERSATION_KIND_CUES:
            if any(k in low for k in keywords):
                mood_phrase = phrase
                bpm = cue_bpm
                brightness = cue_bright
                break
        if mood_phrase.startswith("warm, intimate"):
            for keywords, phrase, cue_bpm, cue_bright in _MOOD_CUES:
                if any(k in low for k in keywords):
                    mood_phrase = phrase + ", intimate and sparse"
                    bpm = max(60, min(96, cue_bpm - 8))
                    brightness = min(0.6, cue_bright)
                    break
        prompts = [
            {"text": (scene or "a quiet conversation"), "weight": 1.0},
            {"text": mood_phrase, "weight": 1.0},
            {"text": _CONVERSATION_STYLE_ANCHORS, "weight": 0.8},
        ]
        if direction is None:
            direction = get_music_direction()
        if direction:
            prompts.insert(0, {"text": direction, "weight": 1.1})
        config = {"bpm": bpm, "brightness": brightness, "temperature": 1.05}
        return prompts, config

    mood_phrase = "calm, mysterious, exploratory ambient"
    bpm = 78
    brightness = 0.5
    for keywords, phrase, cue_bpm, cue_bright in _MOOD_CUES:
        if any(k in low for k in keywords):
            mood_phrase = phrase
            bpm = cue_bpm
            brightness = cue_bright
            break

    prompts = [
        {"text": (scene or "an unknown place"), "weight": 1.0},
        {"text": mood_phrase, "weight": 0.9},
        {"text": _STYLE_ANCHORS, "weight": 0.6},
    ]
    if direction is None:
        direction = get_music_direction()
    if direction:
        prompts.insert(0, {"text": direction, "weight": 1.25})
    config = {"bpm": bpm, "brightness": brightness, "temperature": 1.1}
    return prompts, config


def flatten_music_prompt(scene_prompt: str, mode: str = "scene",
                         direction: str | None = None) -> str:
    """One natural-language prompt Eleven Music can compose from."""
    prompts, cfg = _scene_to_music_prompt(scene_prompt, mode=mode,
                                          direction=direction)
    parts = [str(p.get("text") or "").strip() for p in prompts if p.get("text")]
    bpm = cfg.get("bpm")
    text = ". ".join(p for p in parts if p)
    extras = [
        "cinematic instrumental underscore",
        "seamless looping",
        "no vocals",
        "no lyrics",
    ]
    if bpm:
        extras.append(f"{int(bpm)} bpm")
    for extra in extras:
        if extra.lower() not in text.lower():
            text = f"{text}. {extra}" if text else extra
    return text[:2000]


# ────────────────────────────────────────────────────────────────────────────
# World SFX prompts + pre-cached stock library
# ────────────────────────────────────────────────────────────────────────────

_AMBIENCE_CUES = [
    (("rain", "storm", "downpour", "wet", "thunder"), "rain"),
    (("cave", "tunnel", "underground", "basement", "sewer", "mine"), "cave"),
    (("forest", "jungle", "trees", "woods", "meadow", "garden"), "forest"),
    (("city", "street", "neon", "traffic", "market", "station", "crowd"), "urban"),
    (("wind", "ruin", "wasteland", "desolate", "empty", "abandoned"), "wind"),
    (("factory", "machine", "steam", "industrial", "pipe", "boiler"), "industrial"),
    (("snow", "ice", "frozen", "tundra", "winter"), "wind"),
    (("ocean", "sea", "river", "shore", "dock"), "rain"),
    (("space", "void", "orbit", "cosmic"), "room"),
]

STOCK_STINGERS = {
    "encounter_enter": {
        "file": "sting_encounter_enter.mp3",
        "prompt": (
            "tense analog-horror confrontation stinger, low cinematic braam, "
            "held breath, no melody, no vocals, short one-shot"
        ),
        "seconds": 3.0,
        "loop": False,
    },
    "encounter_lock": {
        "file": "sting_encounter_lock.mp3",
        "prompt": (
            "heavy metallic lock slam, confrontation plate locking into place, "
            "analog horror, no music, no vocals, short one-shot"
        ),
        "seconds": 2.0,
        "loop": False,
    },
    "encounter_resolve": {
        "file": "sting_encounter_resolve.mp3",
        "prompt": (
            "short committed action impact, analog thud and tape scrape, "
            "no melody, no vocals, one-shot"
        ),
        "seconds": 2.0,
        "loop": False,
    },
    "encounter_exit": {
        "file": "sting_encounter_exit.mp3",
        "prompt": (
            "aftermath release, air leaving a room, distant tape unwind, "
            "soft analog fade, no melody, no vocals, one-shot"
        ),
        "seconds": 2.5,
        "loop": False,
    },
    "encounter_hitch": {
        "file": "sting_encounter_hitch.mp3",
        "prompt": (
            "world hitch-step analog tape jump, brief glitch stutter, "
            "VHS scrape, no music, no vocals, one-shot"
        ),
        "seconds": 1.2,
        "loop": False,
    },
    "encounter_die": {
        "file": "sting_encounter_die.mp3",
        "prompt": (
            "fatal analog collapse, descending low tone and tape death, "
            "no melody, no vocals, short one-shot"
        ),
        "seconds": 2.8,
        "loop": False,
    },
    "encounter_title": {
        "file": "sting_encounter_title.mp3",
        "prompt": (
            "full-screen ENCOUNTER title slam, analog-horror braam, "
            "low cinematic impact, no melody, no vocals, short one-shot"
        ),
        "seconds": 2.2,
        "loop": False,
    },
    "encounter_survive": {
        "file": "sting_encounter_survive.mp3",
        "prompt": (
            "survive the interrupt, air returns, analog release rising, "
            "soft hope without melody, no vocals, one-shot"
        ),
        "seconds": 2.4,
        "loop": False,
    },
    "encounter_stance_hostile": {
        "file": "sting_encounter_stance_hostile.mp3",
        "prompt": (
            "hostile confrontation color, low brass growl, analog tension, "
            "no melody, no vocals, short one-shot"
        ),
        "seconds": 1.8,
        "loop": False,
    },
    "encounter_stance_desperate": {
        "file": "sting_encounter_stance_desperate.mp3",
        "prompt": (
            "desperate confrontation color, rising pulse, taut strings, "
            "short breath, no vocals, short one-shot"
        ),
        "seconds": 1.8,
        "loop": False,
    },
    "encounter_stance_opportunistic": {
        "file": "sting_encounter_stance_opportunistic.mp3",
        "prompt": (
            "opportunistic confrontation color, quiet predatory hush, "
            "sly analog drop, no vocals, short one-shot"
        ),
        "seconds": 1.8,
        "loop": False,
    },
    "encounter_stance_creature": {
        "file": "sting_encounter_stance_creature.mp3",
        "prompt": (
            "creature confrontation color, wet organic drone, inhuman rasp, "
            "held breath, no vocals, short one-shot"
        ),
        "seconds": 1.8,
        "loop": False,
    },
}

STOCK_AMBIENCE = {
    "industrial": {
        "file": "amb_industrial.mp3",
        "prompt": (
            "seamless looping industrial machinery hum, steam pipes, distant "
            "metal, no music, no melody, no vocals"
        ),
        "seconds": 16.0,
        "loop": True,
    },
    "rain": {
        "file": "amb_rain.mp3",
        "prompt": (
            "seamless looping rain on concrete and distant thunder rumble, "
            "no music, no melody, no vocals"
        ),
        "seconds": 16.0,
        "loop": True,
    },
    "cave": {
        "file": "amb_cave.mp3",
        "prompt": (
            "seamless looping cave drip, subterranean room tone, distant echo, "
            "no music, no melody, no vocals"
        ),
        "seconds": 16.0,
        "loop": True,
    },
    "wind": {
        "file": "amb_wind.mp3",
        "prompt": (
            "seamless looping cold wind through ruins, sparse debris, "
            "no music, no melody, no vocals"
        ),
        "seconds": 16.0,
        "loop": True,
    },
    "room": {
        "file": "amb_room.mp3",
        "prompt": (
            "seamless looping quiet indoor room tone, faint electrical hum, "
            "no music, no melody, no vocals"
        ),
        "seconds": 16.0,
        "loop": True,
    },
    "urban": {
        "file": "amb_urban.mp3",
        "prompt": (
            "seamless looping distant night city ambience, low traffic, "
            "no music, no melody, no vocals"
        ),
        "seconds": 16.0,
        "loop": True,
    },
    "forest": {
        "file": "amb_forest.mp3",
        "prompt": (
            "seamless looping night forest insects and distant leaves, "
            "no music, no melody, no vocals"
        ),
        "seconds": 16.0,
        "loop": True,
    },
}

_ENCOUNTER_STINGER = "encounter_enter"


def _ambience_kind(scene_prompt: str, mode: str = "scene") -> str:
    """Which stock ambience bed matches this scene."""
    mode = (mode or "scene").strip().lower()
    if mode == "conversation":
        return "room"
    low = _clean_scene_text(scene_prompt).lower()
    for keywords, kind in _AMBIENCE_CUES:
        if any(k in low for k in keywords):
            return kind
    return "industrial" if mode == "encounter" else "room"


def _scene_to_sfx_prompt(scene_prompt: str, mode: str = "scene",
                         direction: str | None = None) -> str:
    """Short Foley/ambience description — not a second music score."""
    kind = _ambience_kind(scene_prompt, mode=mode)
    spec = STOCK_AMBIENCE.get(kind) or STOCK_AMBIENCE["room"]
    scene = _clean_scene_text(scene_prompt)
    if mode == "conversation":
        text = (
            f"seamless looping quiet room tone under a conversation, "
            f"{spec['prompt']}"
        )
    elif mode == "encounter":
        text = (
            f"seamless looping tense close-mic ambience, danger in the room, "
            f"{spec['prompt']}"
        )
    elif scene:
        text = f"seamless looping environmental ambience of {scene}. {spec['prompt']}"
    else:
        text = spec["prompt"]
    if direction is None:
        direction = get_sfx_direction()
    if direction:
        text = f"{direction.strip()}. {text}"
    return text[:SFX_TEXT_MAX]


# ────────────────────────────────────────────────────────────────────────────
# Action foley — the sound of the thing you just did
#
# Everything else here is a BED: loops under the scene. This is the one sound
# tied to the PLAYER, not the place, and it exists because pressing a choice
# made no noise beyond a UI blip — you acted and the world did not answer.
#
# The prompt source is the choice TEXT, and that is the whole trick. An earlier
# attempt fed the render prompt in ("third-person follow-cam, 1993 consumer
# colour film, the back of the head toward the lens…") and got mush, because
# none of that describes a sound. "Sprint toward the utility truck" is four
# concrete words a Foley model can actually record.
#
# Generated when the CHOICES APPEAR, not when one is clicked — they sit on
# screen for ten or twenty seconds first, which is plenty, and a foley that
# arrives eight seconds after the button is useless. By click time it is a
# cache hit and plays instantly.
# ────────────────────────────────────────────────────────────────────────────

ACTION_FOLEY_SECONDS = 2.0
_FOLEY_NUM_RE = re.compile(r"^\s*\d+\s*[.)\-:]?\s*")


def _clean_action(action: str) -> str:
    """The verb, without the slate's numbering or trailing punctuation."""
    text = " ".join(str(action or "").split())
    text = _FOLEY_NUM_RE.sub("", text)
    return text.strip().rstrip(".!?").strip()


def _action_foley_prompt(action: str, direction: str | None = None) -> str:
    """Short and concrete. Long prompts are what made this sound like nothing."""
    act = _clean_action(action)
    if not act:
        return ""
    text = (
        f"{act}. A single close-mic Foley recording of exactly that action and "
        f"nothing else — the sounds the body, the ground and the objects make. "
        f"Dry, close, real, recorded in the room. One take, one action, a clear "
        f"start and a natural end. No music, no melody, no voice, no words, "
        f"not a loop."
    )
    if direction is None:
        direction = get_sfx_direction()
    if direction:
        text = f"{direction.strip().rstrip('. ')}. {text}"
    return text[:SFX_TEXT_MAX]


def _foley_cache_name(action: str) -> str:
    prompt = _action_foley_prompt(action)
    key = json.dumps({"p": prompt, "s": ACTION_FOLEY_SECONDS,
                      "prov": "eleven-sfx-foley"}, sort_keys=True)
    return "foley_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".mp3"


def action_foley_enabled() -> bool:
    try:
        import engine
        return bool(getattr(engine, "ACTION_FOLEY_ENABLED", True))
    except Exception:
        return True


def action_foley(action: str, session_id: str = "default") -> dict | None:
    """The sound of one action. {url, cached, pending} or None.

    Same non-blocking contract as everything else in here: a miss is kicked to
    the background and reported pending. The client pre-warms on the slate and
    plays on the click, so pending should be rare by the time it matters.
    """
    if not action_foley_enabled() or not is_available():
        return None
    act = _clean_action(action)
    if len(act) < 2:
        return None

    fname = _foley_cache_name(act)
    fpath = _get_audio_dir(session_id) / fname
    url = f"/audio/{fname}"
    if fpath.exists() and fpath.stat().st_size > 32:
        return {"url": _sessionize_url(url, session_id), "cached": True,
                "pending": False}

    prompt = _action_foley_prompt(act)

    def _make():
        try:
            _cached_or_generate(
                fpath,
                lambda: _eleven_sfx(prompt, ACTION_FOLEY_SECONDS, loop=False,
                                    session_id=session_id),
            )
        except Exception as e:
            print(f"[FOLEY] {act[:40]!r} failed: {e}", flush=True)

    _kick(("foley", str(fpath)), _make)
    return {"url": _sessionize_url(url, session_id), "cached": False,
            "pending": True}


# ────────────────────────────────────────────────────────────────────────────
# Consequence bed — the sound of what the choice DID
#
# Action foley above answers the CLICK: two seconds of the verb, fired the
# instant you press it. Then the turn renders for half a minute and the
# flipbook plays the outcome out across four to sixteen frames — the most
# motion this game ever puts on screen, and the only thing under it was the
# room tone that was already playing before you chose. The beat with the most
# to watch had the least to hear.
#
# This is that beat's own bed. Two things make it a different lane rather than
# a longer foley:
#
#   · It is generated from the turn's VISUAL SCENE, not the choice text. Foley
#     is the sound of what you MEANT to do; this is the shot the turn is about
#     to draw, and those are often not the same event.
#   · It is LONG — most of a wait, not a two-second hit — because the gap it
#     covers is the whole image generation.
#
# It is deliberately NOT a loop. It was one, holding until the next action was
# committed, and that is the version that had to be taken out: a distinctive
# 18-second gesture repeating under a player who is reading gets annoying fast,
# and the thing that makes a sound feel like the world answering is that it
# happens ONCE. It plays through and stops. The scene's own ambience is
# underneath it the whole time and is what fills the rest of the wait — that
# lane is built to loop and is generic enough to bear it.
#
# Kicked the moment the consequence lands, which is five pipeline steps ahead
# of the picture (action → consequence → world_update → world_respond →
# actions → guide_image). The client opens it as soon as it is on disk rather
# than holding it for the frames: guide_image alone is 20-40 seconds, the
# ceremony has six short blips to fill that with, and that wait was the
# longest silence in the turn. The prose the bed is made from is already on
# screen by then, so it is not arriving early.
#
# Unlike foley this can never be a cache hit across turns: a consequence is
# written fresh every time, so this is one generation per turn. That is the
# cost of the lane and it is why it has its own switch.
# ────────────────────────────────────────────────────────────────────────────

CONSEQUENCE_BED_SECONDS = 18.0
# The wrapper below runs ~180 characters. The prose gets the rest of the
# budget, and a sound model does nothing useful with more of it than this.
CONSEQUENCE_TEXT_MAX = 220


def _clean_consequence(text: str) -> str:
    """The outcome, trimmed to something a sound model can actually record."""
    out = " ".join(str(text or "").split())
    out = _FOLEY_NUM_RE.sub("", out).strip()
    if len(out) <= CONSEQUENCE_TEXT_MAX:
        return out.rstrip(",;:- ").strip()
    head, sep, _tail = out[:CONSEQUENCE_TEXT_MAX].rpartition(" ")
    return (head if sep else out[:CONSEQUENCE_TEXT_MAX]).rstrip(",;:- ").strip()


def _consequence_bed_prompt(text: str, direction: str | None = None) -> str:
    """Concrete first, instruction after — the same shape foley needed."""
    what = _clean_consequence(text)
    if not what:
        return ""
    out = (
        f"{what}. One continuous recording of that moment and what it leaves "
        f"behind — the place and the movement still in it, close and real. It "
        f"begins, it settles, it dies away. No music, no melody, no voice, no "
        f"words, not a loop."
    )
    if direction is None:
        direction = get_sfx_direction()
    if direction:
        out = f"{direction.strip().rstrip('. ')}. {out}"
    return out[:SFX_TEXT_MAX]


def _consequence_cache_name(text: str) -> str:
    prompt = _consequence_bed_prompt(text)
    key = json.dumps({"p": prompt, "s": CONSEQUENCE_BED_SECONDS,
                      "prov": "eleven-sfx-consequence"}, sort_keys=True)
    return "beat_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".mp3"


def consequence_bed_enabled() -> bool:
    try:
        import engine
        return bool(getattr(engine, "CONSEQUENCE_BED_ENABLED", True))
    except Exception:
        return True


def consequence_bed(text: str, session_id: str = "default") -> dict | None:
    """One turn's outcome, as a single pass. {url, cached, pending} or None.

    Same non-blocking contract as the rest of this module: a miss is kicked to
    a thread and reported pending. The client arms this on the consequence and
    starts it when the frames play, so pending should be long resolved.
    """
    if not consequence_bed_enabled() or not is_available():
        return None
    what = _clean_consequence(text)
    # A consequence is prose. Anything this short is a fragment or an error
    # string, and generating a bed from it produces noise with no subject.
    if len(what) < 12:
        return None

    fname = _consequence_cache_name(what)
    fpath = _get_audio_dir(session_id) / fname
    url = f"/audio/{fname}"
    if fpath.exists() and fpath.stat().st_size > 32:
        return {"url": _sessionize_url(url, session_id), "cached": True,
                "pending": False}

    prompt = _consequence_bed_prompt(what)

    def _make():
        try:
            _cached_or_generate(
                fpath,
                lambda: _eleven_sfx(prompt, CONSEQUENCE_BED_SECONDS, loop=False,
                                    session_id=session_id),
            )
        except Exception as e:
            print(f"[BEAT] {what[:40]!r} failed: {e}", flush=True)

    _kick(("beat", str(fpath)), _make)
    return {"url": _sessionize_url(url, session_id), "cached": False,
            "pending": True}


# ────────────────────────────────────────────────────────────────────────────
# ElevenLabs HTTP
# ────────────────────────────────────────────────────────────────────────────

def _offline_mock() -> bool:
    """True when this process promised not to call the network.

    ``--mock`` / ``MOCK_MODE`` / ``STORYGEN_BACKEND=mock`` still inherit
    ``ELEVENLABS_API_KEY`` from the parent shell. Without this gate, a
    "fully offline" run bills Music + SFX on every scene.
    """
    if (os.environ.get("MOCK_MODE") or "").strip().lower() in ("1", "true", "yes"):
        return True
    if (os.environ.get("STORYGEN_BACKEND") or "").strip().lower() == "mock":
        return True
    try:
        import keys_store
        return bool(keys_store.is_explicit_mock())
    except Exception:
        return False


def unavailable_reason() -> str | None:
    """Why generation cannot run, or None when the key looks usable.

    The dashboard lists keys by ID (bare hex). That value is not a key —
    ElevenLabs rejects it. Same diagnosis as ``engine.elevenlabs_key_problem``.
    """
    if _offline_mock():
        return "offline mock — ElevenLabs is not called"
    key = _api_key()
    if not key:
        return "ELEVENLABS_API_KEY is not set."
    if key.startswith("sk_"):
        return None
    if key.startswith("agent_"):
        return "that's an agent id, not the sk_ secret"
    if len(key) in (32, 64) and all(c in "0123456789abcdefABCDEF" for c in key):
        return "that's the key ID from the dashboard list, not the sk_ secret"
    return "ElevenLabs API keys start with sk_"


def is_available() -> bool:
    """True when we can plausibly generate audio (usable ElevenLabs key)."""
    return unavailable_reason() is None


def _record(session_id: str, model: str, operation: str, seconds: float,
            t0: float, success: bool, error: str = ""):
    cost_tracker.record_usage(
        session_id or "default", "voice", "elevenlabs", model,
        operation=operation,
        output_units=seconds if success else None,
        unit_type="seconds",
        success=success,
        error_message=error or None,
        latency_ms=int((time.time() - t0) * 1000),
    )


def _eleven_music(prompt: str, seconds: int, session_id: str = "default") -> bytes:
    import requests

    seconds = max(3, min(30, int(seconds or DEFAULT_CLIP_SECONDS)))
    t0 = time.time()
    try:
        resp = requests.post(
            ELEVEN_MUSIC_URL,
            headers={"xi-api-key": _api_key(), "Content-Type": "application/json"},
            params={"output_format": "mp3_44100_128"},
            json={
                "prompt": (prompt or "").strip()[:2000],
                "music_length_ms": seconds * 1000,
                "model_id": MUSIC_MODEL,
                "force_instrumental": True,
            },
            timeout=_MUSIC_TIMEOUT_SECONDS,
        )
    except Exception as e:
        _record(session_id, MUSIC_MODEL, "music_compose", seconds, t0, False, str(e))
        raise
    if resp.status_code != 200 or not resp.content:
        err = f"http_{resp.status_code}: {(resp.text or '')[:180]}"
        _record(session_id, MUSIC_MODEL, "music_compose", seconds, t0, False, err)
        raise RuntimeError(f"eleven music {err}")
    _record(session_id, MUSIC_MODEL, "music_compose", seconds, t0, True)
    return resp.content


def _eleven_sfx(prompt: str, seconds: float, loop: bool = True,
                session_id: str = "default") -> bytes:
    import requests

    seconds = max(0.5, min(30.0, float(seconds or DEFAULT_SFX_SECONDS)))
    t0 = time.time()
    try:
        resp = requests.post(
            ELEVEN_SFX_URL,
            headers={"xi-api-key": _api_key(), "Content-Type": "application/json"},
            params={"output_format": "mp3_44100_128"},
            json={
                "text": (prompt or "").strip()[:SFX_TEXT_MAX],
                "model_id": SFX_MODEL,
                "duration_seconds": seconds,
                "prompt_influence": 0.4,
                "loop": bool(loop),
            },
            timeout=_SFX_TIMEOUT_SECONDS,
        )
    except Exception as e:
        _record(session_id, SFX_MODEL, "sfx_generate", seconds, t0, False, str(e))
        raise
    if resp.status_code != 200 or not resp.content:
        err = f"http_{resp.status_code}: {(resp.text or '')[:180]}"
        _record(session_id, SFX_MODEL, "sfx_generate", seconds, t0, False, err)
        raise RuntimeError(f"eleven sfx {err}")
    _record(session_id, SFX_MODEL, "sfx_generate", seconds, t0, True)
    return resp.content


# ────────────────────────────────────────────────────────────────────────────
# Paths / cache
# ────────────────────────────────────────────────────────────────────────────

def _session_audio_dir(session_id: str = "default", *, create: bool = True) -> Path:
    """Per-session scratch dir for generated audio (mirrors the image dir)."""
    safe = Path(str(session_id or "default")).name or "default"
    try:
        import engine
        audio_dir = Path(engine._get_session_root(safe)) / "audio"
    except Exception:
        audio_dir = _paths.data_root() / "sessions" / safe / "audio"
    if create:
        audio_dir.mkdir(parents=True, exist_ok=True)
    return audio_dir


def _get_audio_dir(session_id: str = "default") -> Path:
    return _session_audio_dir(session_id, create=True)


def _cache_name(scene_prompt: str, seconds: int, mode: str = "scene") -> str:
    """Stable filename keyed on the derived music prompt so identical scenes
    reuse the same clip instead of re-billing ElevenLabs."""
    prompts, cfg = _scene_to_music_prompt(scene_prompt, mode=mode)
    key = json.dumps({"p": prompts, "c": cfg, "s": seconds, "m": mode,
                      "prov": "eleven-music"}, sort_keys=True)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    prefix = {"conversation": "convo", "encounter": "enc"}.get(mode, "scene")
    return f"{prefix}_{digest}.mp3"


def _sfx_cache_name(scene_prompt: str, seconds: int, mode: str = "scene") -> str:
    prompt = _scene_to_sfx_prompt(scene_prompt, mode=mode)
    key = json.dumps({"p": prompt, "s": seconds, "m": mode, "prov": "eleven-sfx"},
                     sort_keys=True)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    prefix = {"conversation": "amb_convo", "encounter": "amb_enc"}.get(mode, "amb")
    return f"{prefix}_{digest}.mp3"


_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT = {}


# ────────────────────────────────────────────────────────────────────────────
# THE CHOSEN LOOP
# ────────────────────────────────────────────────────────────────────────────

MUSIC_DIR = _paths.data_root() / "assets" / "music"
STOCK_DIR = MUSIC_DIR / "stock"
_LOOP_META = MUSIC_DIR / "loop.json"
_DIRECTION_PATH = MUSIC_DIR / "direction.json"
_SFX_DIRECTION_PATH = MUSIC_DIR / "sfx_direction.json"
_MENU_META = MUSIC_DIR / "menu.json"
_MENU_DIRECTION_PATH = MUSIC_DIR / "menu_direction.json"
LOOP_EXTS = {"wav": "audio/wav", "mp3": "audio/mpeg", "ogg": "audio/ogg",
             "m4a": "audio/mp4", "mp4": "audio/mp4", "webm": "audio/webm"}
MAX_LOOP_BYTES = 12 * 1024 * 1024


def _loop_url(meta: dict) -> str:
    fname = Path(str(meta.get("file") or "")).name
    return f"/audio/{fname}?v={int(float(meta.get('created_at') or 0) * 1000)}"


def _file_url(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size < 32:
            return None
        stamp = int(path.stat().st_mtime * 1000)
        return f"/audio/{path.name}?v={stamp}"
    except OSError:
        return None


def get_sfx_direction() -> str:
    """Authored 'how this place sounds as Foley', or empty."""
    try:
        if not _SFX_DIRECTION_PATH.exists():
            return ""
        data = json.loads(_SFX_DIRECTION_PATH.read_text(encoding="utf-8")) or {}
        return str(data.get("prompt") or "").strip()
    except Exception:
        return ""


def set_sfx_direction(prompt: str) -> str:
    text = (prompt or "").strip()[:400]
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    _SFX_DIRECTION_PATH.write_text(
        json.dumps({"prompt": text}, indent=2), encoding="utf-8")
    return text


def get_music_direction() -> str:
    """The authored 'how this world sounds' line, or empty."""
    try:
        if not _DIRECTION_PATH.exists():
            return ""
        data = json.loads(_DIRECTION_PATH.read_text(encoding="utf-8")) or {}
        return str(data.get("prompt") or "").strip()
    except Exception:
        return ""


def _preview_name(stem: str = "preview") -> str:
    return "menu_preview" if stem == "menu_preview" else "preview"


def last_preview(stem: str = "preview") -> dict | None:
    """The last generated sample for this stem, if it is still on disk."""
    safe = _preview_name(stem)
    for ext in ("mp3", "wav"):
        fname = f"{safe}.{ext}"
        rec = _file_url(MUSIC_DIR / fname)
        if rec:
            return {"url": rec, "file": fname}
    return None


def _clear_preview(stem: str = "preview") -> None:
    safe = _preview_name(stem)
    for ext in ("mp3", "wav"):
        path = MUSIC_DIR / f"{safe}.{ext}"
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass


def set_music_direction(prompt: str) -> str:
    """Persist the music direction. Next scene (and the next run) uses it."""
    text = (prompt or "").strip()[:400]
    old = get_music_direction()
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    _DIRECTION_PATH.write_text(
        json.dumps({"prompt": text}, indent=2), encoding="utf-8")
    if text != old:
        _clear_preview("preview")
    loop = custom_loop()
    if loop and loop.get("source") == "generated":
        clear_custom_loop()
    return text


def generate_preview(prompt: str, seconds: int = 8, stem: str = "preview") -> dict | None:
    """Hear the prompt without locking it as the game's only track."""
    if not is_available():
        return None
    prompt = (prompt or "").strip()
    if not prompt:
        return None
    seconds = max(3, min(16, int(seconds or 8)))
    data = _eleven_music(flatten_music_prompt(prompt, mode="verbatim"), seconds)
    if not data:
        return None
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    safe = _preview_name(stem)
    for ext in ("mp3", "wav"):
        stale = MUSIC_DIR / f"{safe}.{ext}"
        try:
            if stale.exists():
                stale.unlink()
        except OSError:
            pass
    fname = f"{safe}.mp3"
    (MUSIC_DIR / fname).write_bytes(data)
    stamp = int(time.time() * 1000)
    return {"url": f"/audio/{fname}?v={stamp}", "file": fname,
            "prompt": prompt[:400], "seconds": seconds}


def custom_loop() -> dict | None:
    """The chosen loop as {url, source, prompt, name, seconds?}, or None."""
    try:
        if not _LOOP_META.exists():
            return None
        meta = json.loads(_LOOP_META.read_text(encoding="utf-8")) or {}
        fname = Path(str(meta.get("file") or "")).name
        if not fname or not (MUSIC_DIR / fname).exists():
            return None
        meta["url"] = _loop_url(meta)
        return meta
    except Exception:
        return None


def _write_loop(data: bytes, ext: str, source: str,
                prompt: str = "", name: str = "", stem: str = "loop") -> dict:
    ext = (ext or "wav").lower().lstrip(".")
    stem = "menu" if stem == "menu" else "loop"
    if ext not in LOOP_EXTS:
        raise ValueError(f"unsupported audio type {ext!r}")
    if not data or len(data) > MAX_LOOP_BYTES:
        raise ValueError("audio is empty or too large")
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    for old in MUSIC_DIR.glob(f"{stem}.*"):
        try:
            old.unlink()
        except OSError:
            pass
    fname = f"{stem}.{ext}"
    (MUSIC_DIR / fname).write_bytes(data)
    meta = {"file": fname, "source": source, "prompt": prompt[:400],
            "name": (name or "")[:80], "bytes": len(data),
            "created_at": time.time()}
    meta_path = _MENU_META if stem == "menu" else _LOOP_META
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    meta["url"] = _loop_url(meta)
    return meta


def set_uploaded_loop(data: bytes, ext: str, name: str = "",
                      stem: str = "loop") -> dict:
    """Adopt a file the player uploaded as the loop."""
    return _write_loop(data, ext, "upload", name=name, stem=stem)


def generate_loop(prompt: str, seconds: int = DEFAULT_CLIP_SECONDS,
                  stem: str = "loop") -> dict | None:
    """Generate a loop from a MUSIC prompt and adopt it."""
    if not is_available():
        return None
    prompt = (prompt or "").strip()
    if not prompt:
        return None
    seconds = max(3, min(30, int(seconds or DEFAULT_CLIP_SECONDS)))
    data = _eleven_music(flatten_music_prompt(prompt, mode="verbatim"), seconds)
    if not data:
        return None
    return _write_loop(data, "mp3", "generated", prompt=prompt, name="", stem=stem)


def clear_custom_loop() -> None:
    """Back to scoring each scene as it comes."""
    try:
        for old in MUSIC_DIR.glob("loop.*"):
            old.unlink()
    except OSError:
        pass


def get_menu_direction() -> str:
    try:
        if not _MENU_DIRECTION_PATH.exists():
            return ""
        data = json.loads(_MENU_DIRECTION_PATH.read_text(encoding="utf-8")) or {}
        return str(data.get("prompt") or "").strip()
    except Exception:
        return ""


def set_menu_direction(prompt: str) -> str:
    text = (prompt or "").strip()[:400]
    old = get_menu_direction()
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    _MENU_DIRECTION_PATH.write_text(
        json.dumps({"prompt": text}, indent=2), encoding="utf-8")
    if text != old:
        _clear_preview("menu_preview")
    loop = menu_loop()
    if loop and loop.get("source") == "generated":
        clear_menu_loop()
    return text


def menu_loop() -> dict | None:
    try:
        if not _MENU_META.exists():
            return None
        meta = json.loads(_MENU_META.read_text(encoding="utf-8")) or {}
        fname = Path(str(meta.get("file") or "")).name
        if not fname or not (MUSIC_DIR / fname).exists():
            return None
        meta["url"] = _loop_url(meta)
        return meta
    except Exception:
        return None


def clear_menu_loop() -> None:
    try:
        for old in MUSIC_DIR.glob("menu.*"):
            old.unlink()
    except OSError:
        pass


# ────────────────────────────────────────────────────────────────────────────
# Stock stingers / fallback ambience
# ────────────────────────────────────────────────────────────────────────────

def _stock_path(filename: str) -> Path:
    return STOCK_DIR / Path(filename).name


def _stock_url(filename: str) -> str | None:
    return _file_url(_stock_path(filename))


def stock_stinger_url(kind: str = _ENCOUNTER_STINGER) -> str | None:
    spec = STOCK_STINGERS.get(kind)
    return _stock_url(spec["file"]) if spec else None


def encounter_designer_urls() -> dict:
    """Authored one-shots sitting in static/audio/encounter/<stem>.wav|mp3."""
    folder = ROOT / "static" / "audio" / "encounter"
    out = {}
    if not folder.is_dir():
        return out
    try:
        for path in folder.iterdir():
            if path.suffix.lower() not in (".wav", ".mp3"):
                continue
            if path.stat().st_size < 32:
                continue
            out[path.stem] = f"/static/audio/encounter/{path.name}"
    except OSError:
        return out
    return out


def encounter_stinger_urls() -> dict:
    """Ready stock one-shots keyed by catalog id (encounter_hitch, …).

    Missing files are omitted so the client can fall through to designer
    WAVs and then the built-in synth. Warmup fills these in the background.
    """
    out = {}
    for key in STOCK_STINGERS:
        url = stock_stinger_url(key)
        if url:
            out[key] = url
    return out


def stock_ambience_url(kind: str) -> str | None:
    spec = STOCK_AMBIENCE.get(kind)
    return _stock_url(spec["file"]) if spec else None


def stock_spec(key: str) -> tuple[dict | None, str]:
    """Return (spec, kind) for a catalog id, or (None, "")."""
    if key in STOCK_STINGERS:
        return STOCK_STINGERS[key], "stinger"
    if key in STOCK_AMBIENCE:
        return STOCK_AMBIENCE[key], "ambience"
    return None, ""


def stock_status() -> dict:
    """What's already on disk — used by tests, the editor, and warmup."""
    out = {}
    for key, spec in {**STOCK_STINGERS, **STOCK_AMBIENCE}.items():
        url = _stock_url(spec["file"])
        kind = "stinger" if key in STOCK_STINGERS else "ambience"
        out[key] = {
            "file": spec["file"],
            "ready": bool(url),
            "url": url,
            "prompt": spec["prompt"],
            "seconds": spec["seconds"],
            "loop": spec["loop"],
            "kind": kind,
        }
    return out


def ensure_one_stock(key: str, *, force: bool = False,
                     session_id: str = "default") -> dict | None:
    """Generate or reuse one catalog entry. Returns a status dict."""
    spec, kind = stock_spec(key)
    if not spec:
        return None
    STOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = _stock_path(spec["file"])
    url = _stock_url(spec["file"])
    if url and not force:
        return {"id": key, "kind": kind, "url": url, "cached": True,
                "prompt": spec["prompt"], "file": spec["file"]}
    if not is_available():
        return {"id": key, "kind": kind, "url": url, "cached": bool(url),
                "prompt": spec["prompt"], "file": spec["file"],
                "error": "no_key"}
    data = _eleven_sfx(
        spec["prompt"], spec["seconds"], loop=spec["loop"],
        session_id=session_id,
    )
    path.write_bytes(data)
    return {"id": key, "kind": kind, "url": _stock_url(spec["file"]),
            "cached": False, "prompt": spec["prompt"], "file": spec["file"]}


def ensure_stock_sounds(*, force: bool = False,
                        session_id: str = "default") -> dict:
    """Generate any missing stock stingers and fallback ambience beds.

    Safe to call repeatedly. Missing files are created; present ones are kept.
    """
    if not is_available():
        return {"ok": False, "reason": "no_key", "files": stock_status()}
    STOCK_DIR.mkdir(parents=True, exist_ok=True)
    results = {}
    catalog = list(STOCK_STINGERS.items()) + list(STOCK_AMBIENCE.items())
    for key, spec in catalog:
        path = _stock_path(spec["file"])
        if path.exists() and path.stat().st_size > 32 and not force:
            results[key] = {"url": _stock_url(spec["file"]), "cached": True}
            continue
        try:
            data = _eleven_sfx(
                spec["prompt"], spec["seconds"], loop=spec["loop"],
                session_id=session_id,
            )
            path.write_bytes(data)
            results[key] = {"url": _stock_url(spec["file"]), "cached": False}
            print(f"[SCENE AUDIO] stock {key} -> {path.name} ({len(data)} bytes)",
                  flush=True)
        except Exception as e:
            results[key] = {"error": str(e)}
            print(f"[SCENE AUDIO] stock {key} failed: {e}", flush=True)
    ready = sum(1 for v in results.values() if v.get("url"))
    return {"ok": ready > 0, "ready": ready, "total": len(catalog),
            "files": results}


_STOCK_WARMUP_LOCK = threading.Lock()
_STOCK_WARMUP_STARTED = False


def kick_stock_warmup() -> None:
    """Fill missing stock files in the background. Not called from gameplay
    scoring — that would race the first scene's Music call and stall the worker.
    The editor and /api/music kick this once a usable key is present.
    """
    global _STOCK_WARMUP_STARTED
    if not is_available():
        return
    with _STOCK_WARMUP_LOCK:
        if _STOCK_WARMUP_STARTED:
            return
        _STOCK_WARMUP_STARTED = True
    threading.Thread(target=lambda: ensure_stock_sounds(), daemon=True,
                     name="stock-audio-warmup").start()


# kick_menu_preview() used to live here: /api/music warmed a 10-second sample
# from the menu direction text so the title screen would have something to play.
# Nothing plays it now — the title screen takes the LOCKED menu track and
# nothing else — so warming it only spent ElevenLabs credit on audio no one
# asked for. The editor's Play menu button still previews on demand.


def _with_inflight(ikey, fn):
    with _INFLIGHT_LOCK:
        lock = _INFLIGHT.get(ikey)
        if lock is None:
            lock = threading.Lock()
            _INFLIGHT[ikey] = lock
    with lock:
        try:
            return fn()
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.pop(ikey, None)


def _kick(ikey, fn) -> None:
    threading.Thread(
        target=lambda: _with_inflight(ikey, fn),
        daemon=True, name="scene-audio-gen",
    ).start()


def _cached_or_generate(fpath: Path, generate):
    if fpath.exists() and fpath.stat().st_size > 32:
        return True, False
    data = generate()
    if not data:
        return False, False
    fpath.parent.mkdir(parents=True, exist_ok=True)
    tmp = fpath.with_name(fpath.name + ".part")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, fpath)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return True, True


def _resolve_music(scene_prompt: str, session_id: str, seconds: int,
                   mode: str) -> tuple[str | None, bool, bool]:
    """Return (web_url, cached, pending).

    Uncached music starts in the background. The first scene must not wait
    20–90s on Eleven Music — the client retries until the file lands.
    """
    # A locked explore loop must not steal the confrontation bed.
    if (mode or "scene").strip().lower() != "encounter":
        loop = custom_loop()
        if loop:
            return loop["url"], True, False
    fname = _cache_name(scene_prompt, seconds, mode=mode)
    fpath = _get_audio_dir(session_id) / fname
    web_url = f"/audio/{fname}"
    if fpath.exists() and fpath.stat().st_size > 32:
        return web_url, True, False
    if not is_available():
        return None, False, False

    def _go():
        dest = _get_audio_dir(session_id) / fname
        try:
            _cached_or_generate(
                dest,
                lambda: _eleven_music(
                    flatten_music_prompt(scene_prompt, mode=mode),
                    seconds, session_id=session_id,
                ),
            )
        except Exception as e:
            print(f"[SCENE AUDIO] music failed: {e}", flush=True)

    _kick((session_id, fname), _go)
    return None, False, True


def scene_ambience_enabled() -> bool:
    """Whether a scene gets its OWN ambience or just the stock bed.

    Off falls back to the keyword-matched stock loop, which is what every
    scene used to end up on anyway — see the retry bug in SceneAudio.score.
    """
    try:
        import engine
        return bool(getattr(engine, "SCENE_AMBIENCE_ENABLED", True))
    except Exception:
        return True


def _resolve_sfx(scene_prompt: str, session_id: str, seconds: int,
                 mode: str) -> tuple[str | None, bool, bool]:
    """Scene-specific looping ambience, falling back to a stock bed.

    Stock plays immediately. Scene-specific fill-in (or a first generate when
    stock is missing) always runs in the background.
    """
    kind = _ambience_kind(scene_prompt, mode=mode)
    stock = stock_ambience_url(kind)
    if mode == "conversation":
        return stock, True, False
    if not scene_ambience_enabled():
        return stock, bool(stock), False
    fname = _sfx_cache_name(scene_prompt, seconds, mode=mode)
    fpath = _session_audio_dir(session_id, create=False) / fname
    web_url = f"/audio/{fname}"
    if fpath.exists() and fpath.stat().st_size > 32:
        return web_url, True, False
    if not is_available():
        return stock, bool(stock), False

    def _go():
        dest = _get_audio_dir(session_id) / fname
        try:
            _cached_or_generate(
                dest,
                lambda: _eleven_sfx(
                    _scene_to_sfx_prompt(scene_prompt, mode=mode),
                    seconds, loop=True, session_id=session_id,
                ),
            )
        except Exception as e:
            print(f"[SCENE AUDIO] sfx failed, using stock {kind}: {e}", flush=True)

    _kick((session_id, fname), _go)
    if stock:
        # Stock NOW so the scene is not silent, but pending=True because the
        # scene's own loop is being made this second and the client has to come
        # back for it. This used to claim (stock, cached=True, pending=False) —
        # which was simply untrue, and it was the whole bug: the client had no
        # way to learn the real loop had landed, so every location in the game
        # played one of four keyword-matched stock beds forever while the
        # scene-specific loops piled up on disk, generated, paid for, unheard.
        return stock, False, True
    return None, False, True


def get_scene_audio(scene_prompt: str, session_id: str = "default",
                    seconds: int = DEFAULT_CLIP_SECONDS,
                    mode: str = "scene") -> dict | None:
    """Return music + world-SFX URLs for a scene, or ``None`` when nothing
    can be produced.

    ``mode="conversation"`` selects the intimate Conversation Moment profile.
    ``mode="encounter"`` selects a stance-colored confrontation bed, tense
    ambience, and the stock stinger catalog (``stingers``).

    Uncached ElevenLabs work is kicked to a background thread. The response
    is immediate: stock ambience / a pending flag, then the client retries.
    """
    mode = (mode or "scene").strip().lower()
    if mode not in ("scene", "conversation", "encounter"):
        mode = "scene"
    scene_prompt = _clean_scene_text(scene_prompt)
    loop = None if mode == "encounter" else custom_loop()
    if mode == "encounter":
        if is_available() and not stock_stinger_url(_ENCOUNTER_STINGER):
            try:
                ensure_one_stock(_ENCOUNTER_STINGER, session_id=session_id)
            except Exception as e:
                print(f"[SCENE AUDIO] enter stinger failed: {e}", flush=True)
        kick_stock_warmup()
    if not loop and not scene_prompt and mode != "encounter":
        return None

    seconds = max(3, min(30, int(seconds or DEFAULT_CLIP_SECONDS)))
    sfx_seconds = DEFAULT_SFX_SECONDS
    place = scene_prompt or "an unknown place"

    music_url, music_cached, music_pending = _resolve_music(
        place, session_id, seconds, mode)
    sfx_url, sfx_cached, sfx_pending = _resolve_sfx(
        place, session_id, sfx_seconds, mode)
    stinger_url = stock_stinger_url(_ENCOUNTER_STINGER) if mode == "encounter" else None
    stingers = encounter_stinger_urls() if mode == "encounter" else None

    if not music_url and not sfx_url and not stinger_url and not (
            music_pending or sfx_pending):
        return None

    result = {
        "audio_url": music_url,
        "sfx_url": sfx_url,
        "stinger_url": stinger_url,
        "cached": bool(music_cached and sfx_cached),
        "pending_music": bool(music_pending),
        "pending_sfx": bool(sfx_pending),
        "mode": mode,
    }
    if session_id and session_id != "default":
        result["audio_url"] = _sessionize_url(result["audio_url"], session_id)
        result["sfx_url"] = _sessionize_url(result["sfx_url"], session_id)
    if not music_url and not music_pending:
        why = unavailable_reason()
        if why:
            result["reason"] = why
    if stingers:
        result["stingers"] = stingers
    if loop:
        result["source"] = loop.get("source") or "custom"
    return result


def resolve_audio_path(filename: str, session_id: str = "default") -> Path | None:
    """Resolve a served '/audio/<filename>' back to disk (path-traversal safe).

    Looks in the requested session, then default, then stock / locked loops,
    then any session's audio dir — same last-resort scan as /images so a
    URL that lost its ?session= param still plays.
    """
    safe = Path(filename).name
    if not safe or safe.endswith(".part"):
        return None
    for sid in (session_id, "default"):
        if not sid:
            continue
        candidate = _session_audio_dir(sid, create=False) / safe
        if candidate.exists():
            return candidate
    stock = STOCK_DIR / safe
    if stock.exists():
        return stock
    loop = MUSIC_DIR / safe
    if loop.exists() and (
        safe.startswith("loop.") or safe.startswith("preview.")
        or safe.startswith("menu") or safe.startswith("test_")
    ):
        return loop
    return _find_audio_in_any_session(safe)


def _sessionize_url(url: str | None, session_id: str) -> str | None:
    """Stamp a non-default session onto a generated clip URL."""
    if not url or not session_id or session_id == "default":
        return url
    name = Path(str(url).split("?", 1)[0]).name
    if name.startswith(("loop.", "preview.", "menu", "test_", "sting_")):
        return url
    if name.startswith("amb_") and not name.startswith(("amb_convo_", "amb_enc_")):
        # stock amb_rain.mp3 etc. live outside the session dir
        digest_like = name[4:].split(".", 1)[0]
        if not any(c in "0123456789abcdef" for c in digest_like) or len(digest_like) < 12:
            return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}session={session_id}"


def _find_audio_in_any_session(filename: str) -> Path | None:
    root = _paths.data_root() / "sessions"
    if not root.is_dir():
        return None
    try:
        for audio_dir in root.glob("*/audio"):
            candidate = audio_dir / filename
            if candidate.is_file():
                return candidate
    except OSError:
        return None
    return None


def inspect_scene(scene_prompt: str, mode: str = "scene") -> dict:
    """The prompts that would be sent — no generation, no billing."""
    mode = (mode or "scene").strip().lower()
    if mode not in ("scene", "conversation", "encounter"):
        mode = "scene"
    scene = _clean_scene_text(scene_prompt)
    stinger_id = _ENCOUNTER_STINGER if mode == "encounter" else None
    return {
        "mode": mode,
        "scene": scene,
        "direction": get_music_direction(),
        "sfx_direction": get_sfx_direction(),
        "music_prompt": flatten_music_prompt(scene or "an unknown place", mode=mode),
        "sfx_prompt": _scene_to_sfx_prompt(scene or "an unknown place", mode=mode),
        "ambience_kind": _ambience_kind(scene, mode=mode),
        "stinger_id": stinger_id,
        "stinger_url": stock_stinger_url(stinger_id) if stinger_id else None,
        "can_generate": is_available(),
    }


def generate_test_clip(scene_prompt: str, mode: str = "scene",
                       layer: str = "music", seconds: int | None = None,
                       session_id: str = "default") -> dict | None:
    """Write a one-off test file the editor can play without locking a loop."""
    layer = (layer or "music").strip().lower()
    mode = (mode or "scene").strip().lower()
    if mode not in ("scene", "conversation", "encounter", "verbatim"):
        mode = "scene"
    if layer == "stinger":
        key = _ENCOUNTER_STINGER
        return ensure_one_stock(key, force=False, session_id=session_id)
    if not is_available():
        return None
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    if layer == "sfx":
        seconds = max(3, min(16, int(seconds or 8)))
        prompt = _scene_to_sfx_prompt(scene_prompt, mode=mode)
        data = _eleven_sfx(prompt, seconds, loop=True, session_id=session_id)
        fname = "test_sfx.mp3"
    else:
        seconds = max(3, min(16, int(seconds or 8)))
        prompt = flatten_music_prompt(scene_prompt, mode=mode)
        data = _eleven_music(prompt, seconds, session_id=session_id)
        fname = "test_music.mp3"
    if not data:
        return None
    (MUSIC_DIR / fname).write_bytes(data)
    stamp = int(time.time() * 1000)
    return {
        "url": f"/audio/{fname}?v={stamp}",
        "file": fname,
        "prompt": prompt,
        "layer": layer,
        "mode": mode,
        "seconds": seconds,
    }


def list_generated_cache(session_id: str = "default") -> list[dict]:
    """Session-scored clips currently on disk."""
    out = []
    try:
        audio_dir = _get_audio_dir(session_id)
    except Exception:
        return out
    for path in sorted(audio_dir.glob("*")):
        if not path.is_file() or path.name.endswith(".part"):
            continue
        if path.suffix.lower().lstrip(".") not in LOOP_EXTS:
            continue
        kind = "other"
        for prefix, label in (("scene_", "music"), ("convo_", "conversation"),
                              ("enc_", "encounter"), ("amb_", "ambience")):
            if path.name.startswith(prefix):
                kind = label
                break
        try:
            size = path.stat().st_size
        except OSError:
            continue
        out.append({
            "file": path.name,
            "url": f"/audio/{path.name}",
            "bytes": size,
            "kind": kind,
        })
    return out


def clear_generated_cache(session_id: str = "default") -> int:
    """Delete per-scene generated clips. Does not touch stock or locked loops."""
    removed = 0
    try:
        audio_dir = _get_audio_dir(session_id)
    except Exception:
        return 0
    for path in list(audio_dir.glob("*")):
        if not path.is_file():
            continue
        if path.name.endswith(".part") or path.name.startswith(
                ("scene_", "convo_", "enc_", "amb_")):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed
