"""sound_library.py — the shipped sound library, and which clip of it plays.

Every sound under the picture used to be generated as the game ran: a music
bed per scene, a looping ambience per scene, a foley clip per action, a bed per
consequence, a dozen encounter stingers. When ElevenLabs left (2026-09-25, the
player brings ONE key and neither Gemini nor OpenAI makes sound effects) all of
that went silent. Matt's call the same day: *"precache them FROM my 11 labs and
ship precached whatever you need."*

So the sounds were made once, on 5th Corner's ElevenLabs account, by
``tools/build_sound_library.py``, and ship in ``static/audio/library/``:

    catalog.json            what each file is, what made it, what it is for
    music/*.mp3             instrumental loops by mode x phase x world flavour
    ambience/*.mp3          loopable beds, one per kind of place
    foley/*.mp3             one-shots keyed to the verbs players actually use
    consequence/*.mp3       6-12 s beds by what the turn did
    stinger/*.mp3           the encounter hits the client already knows by id

This module only READS that folder. ``pick()`` is pure and deterministic: the
same text, mode, phase and seed always name the same clip, so a scene keeps its
bed from one turn to the next and a replayed tape sounds the same, while the
seed (a session, an action's own words) lets two different doors land on two
different door clips when the library has more than one. Nothing here calls a
provider, writes a file, or knows a key exists — a runtime module that did
would be a second account again (test_sound_library holds that).

Matching is words, not embeddings. A clip's ``tags`` are short phrases in the
game's own vocabulary ("kick door", "chain link", "blast door"), mined from
the slates and vision reads of real runs. The text is lower-cased, stop words
dropped, and each word cut to a crude stem (``stem``: "kicked" / "kicking" /
"kicks" all read "kick"), and a tag matches when its words appear in order
with at most two words between them — "Kick open the rusted door" matches
"kick door". A matched tag scores its word count squared, so the specific
phrase outranks the loose word: "kick door" (4) beats "door" (1).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
LIBRARY_DIR = ROOT / "static" / "audio" / "library"
CATALOG_PATH = LIBRARY_DIR / "catalog.json"
# Served by Flask's own static route, which local_guard leaves open and
# tools/ship_layout bundles with the rest of static/.
URL_BASE = "/static/audio/library/"

LANES = ("music", "ambience", "foley", "consequence", "stinger")
MODES = ("scene", "conversation", "encounter", "camp", "menu")
PHASES = ("normal", "escalating", "critical")
# The shipped Experience is the Four Corners desert, so a world that names
# nothing else scores as that.
DEFAULT_FLAVOR = "desert"

# How a tag's words may sit in the text: in order, at most this many positions
# apart ("kick open rusted door" still matches "kick door").
_WINDOW = 3

# A place bed that is still nearly as good as the new best keeps playing:
# vision describes the same corridor in new words every turn, and without this
# the ambience crossfaded between two equally good beds on every frame.
PREFER_RATIO = 0.75


# ───────────────────────────── words ───────────────────────────────────────

_STOP = frozenset("""
a an the and or but of to in on at by for from with into onto over under
toward towards through past behind beside near across up down off out as
is are was were be been being it its it's this that these those there here
his her their your my our you he she they them we i me him us who whom
which what while than then so such very just only also some any each every
all both one two three large small big little left right
""".split())

# Words the crude stemmer gets wrong in a way that matters.
_EXCEPT = {
    "deserted": "deserted",   # "a deserted street" is not a desert
    "ran": "run",
    "rainy": "rain",
    "rained": "rain",
    "dove": "dive",
    "fell": "fall",
    "threw": "throw",
    "shot": "shot",
    "shots": "shot",
    "broke": "break",
    "broken": "break",
    "torn": "tear",
    "tore": "tear",
    "lit": "lit",
    "news": "news",
    "glass": "glass",
    "gas": "gas",
    "chaos": "chaos",
    "canvas": "canvas",
    "lens": "lens",
    "press": "press",
    "grass": "grass",
    "brass": "brass",
    "moss": "moss",
    "boss": "boss",
    "pass": "pass",
    "mass": "mass",
    "access": "access",
    "hiss": "hiss",
}

_WORD_RE = re.compile(r"[a-z0-9]+")


def stem(word: str) -> str:
    """Cut a word down to something its other forms share.

    Not linguistics — consistency. Tags and text go through the same function,
    so all that matters is that "slide", "slides" and "sliding" land on one
    string ("slid"), and "door"/"doors" on another.
    """
    w = (word or "").lower()
    if w in _EXCEPT:
        return _EXCEPT[w]
    if len(w) <= 3:
        return w
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith("ing") and len(w) > 5:
        w = w[:-3]
    elif w.endswith("ed") and len(w) > 4:
        w = w[:-2]
    elif w.endswith(("ches", "shes", "sses", "xes", "zes")) and len(w) > 4:
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")) and len(w) > 3:
        w = w[:-1]
    # runn -> run, slamm -> slam, stepp -> step; buzz -> buz either way
    if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "aeiouls":
        w = w[:-1]
    if len(w) > 3 and w.endswith("e"):
        w = w[:-1]
    return w


def tokens(text: str) -> list[str]:
    """Lower-cased, stop words out, every word stemmed, order kept."""
    out = []
    for raw in _WORD_RE.findall(str(text or "").lower().replace("'", "")):
        if raw in _STOP:
            continue
        out.append(stem(raw))
    return out


_TAG_CACHE: dict[str, tuple[str, ...]] = {}


def _tag_tokens(tag: str) -> tuple[str, ...]:
    got = _TAG_CACHE.get(tag)
    if got is None:
        got = tuple(tokens(tag))
        _TAG_CACHE[tag] = got
    return got


def _positions(toks: list[str]) -> dict[str, list[int]]:
    pos: dict[str, list[int]] = {}
    for i, t in enumerate(toks):
        pos.setdefault(t, []).append(i)
    return pos


def _matches(tag_toks: tuple[str, ...], pos: dict[str, list[int]]) -> bool:
    """Every word of the tag, in order, each within _WINDOW of the last."""
    if not tag_toks:
        return False
    first = pos.get(tag_toks[0])
    if not first:
        return False
    if len(tag_toks) == 1:
        return True
    for start in first:
        at = start
        ok = True
        for word in tag_toks[1:]:
            nxt = [p for p in pos.get(word, ()) if at < p <= at + _WINDOW]
            if not nxt:
                ok = False
                break
            at = nxt[0]
        if ok:
            return True
    return False


# ───────────────────────────── the catalog ─────────────────────────────────

_LOCK = threading.Lock()
_CACHE: dict = {"mtime": None, "path": None, "data": None}


def load_catalog(path: Path | None = None) -> dict:
    """The catalog as {"entries": [...]} — re-read when the file changes.

    A missing or unreadable catalog is an empty library, never an exception:
    the game has to boot and play (silently) on a tree without the folder.
    """
    path = Path(path or CATALOG_PATH)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {"entries": []}
    with _LOCK:
        if _CACHE["path"] == str(path) and _CACHE["mtime"] == mtime and _CACHE["data"]:
            return _CACHE["data"]
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        if not isinstance(data.get("entries"), list):
            data["entries"] = []
        _CACHE.update(mtime=mtime, path=str(path), data=data)
        return data


def entries(lane: str | None = None, catalog: dict | None = None) -> list[dict]:
    cat = catalog if catalog is not None else load_catalog()
    rows = [e for e in cat.get("entries", []) if isinstance(e, dict) and e.get("file")]
    if lane:
        rows = [e for e in rows if e.get("lane") == lane]
    return rows


def get(entry_id: str, catalog: dict | None = None) -> dict | None:
    for e in entries(catalog=catalog):
        if e.get("id") == entry_id:
            return e
    return None


def file_path(entry: dict) -> Path:
    return LIBRARY_DIR / str(entry.get("file") or "")


def url(entry: dict | None) -> str | None:
    """Where the client fetches it. None for no entry."""
    if not entry or not entry.get("file"):
        return None
    return URL_BASE + str(entry["file"]).replace("\\", "/")


def available(catalog: dict | None = None) -> bool:
    """True when there is something to play in every lane."""
    rows = entries(catalog=catalog)
    have = {e.get("lane") for e in rows}
    return all(lane in have for lane in LANES)


# ───────────────────────────── scoring ─────────────────────────────────────

def _text_score(entry: dict, pos: dict[str, list[int]],
                keys: tuple[str, ...] = ("tags",)) -> float:
    total = 0.0
    for key in keys:
        for tag in entry.get(key) or ():
            tt = _tag_tokens(str(tag))
            if tt and _matches(tt, pos):
                total += float(len(tt) ** 2)
    return total


# The main text is matched against `tags` — what the thing IS. The context (the
# place) is matched against `tags` and `where`. The split exists for foley:
# "blast door" as a tag made "Head for the Reinforced Blast Door", a MOVE TO,
# play a blast door grinding open. The door belongs in `where`, where it can
# only choose between clips the action words already chose.
_CTX_KEYS = ("tags", "where")


def matched_tags(entry: dict, text: str) -> list[str]:
    """Which of an entry's tags this text hits — the "why" for the editor."""
    pos = _positions(tokens(text))
    return [str(t) for t in (entry.get("tags") or ())
            if _matches(_tag_tokens(str(t)), pos)]


def _contexts(context) -> list[tuple[dict, float]]:
    """context is text (weight 0.35) or [(text, weight), ...]."""
    if not context:
        return []
    if isinstance(context, str):
        pairs = [(context, 0.35)]
    else:
        pairs = [(str(t or ""), float(w)) for t, w in context]
    return [(_positions(tokens(t)), w) for t, w in pairs if t and w > 0]


def _hash(seed: str, entry_id: str) -> int:
    digest = hashlib.sha1(f"{seed}\x1f{entry_id}".encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


# World flavours: which kind of place the RUN is, for the music lane. Scored on
# the world's own brief plus the scene; the desert wins a tie because it is the
# shipped Experience.
FLAVOR_WORDS = {
    "desert": ("desert", "mesa", "four corners", "canyon", "arroyo", "dune",
               "sagebrush", "scrub", "new mexico", "arizona", "utah", "navajo",
               "quarantine", "1993", "badlands", "butte", "red rock"),
    "cyber": ("neon", "cyber", "cyberpunk", "hologram", "holographic",
              "megacity", "chrome", "android", "implant", "augment", "hacker",
              "netrunner", "arcology", "synthetic", "cyborg", "augmented",
              "corporate tower", "drone swarm"),
    # Not "tactical": half the protagonists in any World wear tactical gear.
    "riot": ("riot", "swat", "police", "cop", "officer", "protest",
             "mob", "barricade", "squad", "precinct", "cruiser", "riot shield",
             "tear gas", "martial law", "enforcer"),
}


def flavor_of(text: str) -> str:
    pos = _positions(tokens(text))
    best, best_score = DEFAULT_FLAVOR, 0.0
    # FLAVOR_WORDS lists the desert first and only a strictly higher score
    # displaces it, so a tie (or nothing at all) stays in the desert.
    for flavor, words in FLAVOR_WORDS.items():
        s = sum(float(len(tt) ** 2) for tt in (_tag_tokens(w) for w in words)
                if tt and _matches(tt, pos))
        if s > best_score:
            best, best_score = flavor, s
    return best


_CAMP_WORDS = ("campfire", "camp fire", "campsite", "fire pit", "bonfire",
               "by the fire", "round the fire")


def infer_mode(text: str, mode: str | None) -> str:
    """The music mode for a request: what the caller said, except that a
    scene round a campfire is CAMP (the camp level is scored like any scene —
    the client does not know it is at camp when it asks)."""
    m = (mode or "scene").strip().lower()
    if m not in MODES:
        m = "scene"
    if m == "scene":
        pos = _positions(tokens(text))
        if any(_matches(_tag_tokens(w), pos) for w in _CAMP_WORDS):
            return "camp"
    return m


def _phase_bonus(entry_phase: str, phase: str) -> float:
    ep = (entry_phase or "any").lower()
    if ep == "any":
        return 6.0
    if ep == phase:
        return 10.0
    if ep in PHASES and phase in PHASES and abs(PHASES.index(ep) - PHASES.index(phase)) == 1:
        return 3.0
    return 0.0


def _flavor_bonus(entry_flavor: str, flavor: str) -> float:
    ef = (entry_flavor or "any").lower()
    if ef == flavor:
        return 20.0
    if ef == "any":
        return 8.0
    return 0.0


def score_all(lane: str, text: str = "", *, mode: str | None = None,
              phase: str | None = None, flavor: str | None = None,
              context=None, catalog: dict | None = None,
              pool: list[dict] | None = None) -> list[tuple[float, dict]]:
    """Every candidate with its score, unsorted. ``pick`` is this plus the
    tie-break; exposed so a test (or the editor) can see why."""
    rows = pool if pool is not None else entries(lane, catalog=catalog)
    if not rows:
        return []
    pos = _positions(tokens(text))
    ctx = _contexts(context)
    out: list[tuple[float, dict]] = []

    if lane == "music":
        m = infer_mode(text, mode)
        ph = (phase or "normal").strip().lower()
        if ph not in PHASES:
            ph = "normal"
        fl = (flavor or DEFAULT_FLAVOR).strip().lower()
        in_mode = [e for e in rows if e.get("mode") == m]
        if not in_mode and m != "scene":
            in_mode = [e for e in rows if e.get("mode") == "scene"]
        for e in (in_mode or rows):
            s = (_text_score(e, pos) + _phase_bonus(e.get("phase"), ph)
                 + _flavor_bonus(e.get("flavor"), fl))
            for cpos, w in ctx:
                s += w * _text_score(e, cpos, _CTX_KEYS)
            out.append((s, e))
        return out

    for e in rows:
        s = _text_score(e, pos)
        if lane in ("foley", "consequence"):
            # The words of the action decide WHAT it is; the place only picks
            # between clips that already fit the action. A slate line that names
            # nothing audible ("Give the silhouette what they want") gets the
            # fallback, not a metal footstep because the room has grating.
            if s > 0:
                # The action's own nouns are the best surface cue there is
                # ("Stride across the metallic walkway"), so they count too.
                s += 0.5 * _text_score(e, pos, ("where",))
                for cpos, w in ctx:
                    s += w * _text_score(e, cpos, _CTX_KEYS)
        else:
            for cpos, w in ctx:
                s += w * _text_score(e, cpos, _CTX_KEYS)
        out.append((s, e))
    return out


def pick(lane: str, text: str = "", *, mode: str | None = None,
         phase: str | None = None, flavor: str | None = None, seed: str = "",
         context=None, prefer: str | None = None, generic_only: bool = False,
         catalog: dict | None = None) -> dict | None:
    """The best clip in ``lane`` for ``text``. Always an entry while the lane
    has one; None only for an empty lane.

    - ``mode`` / ``phase`` / ``flavor`` steer music (scene / conversation /
      encounter / camp / menu x normal / escalating / critical x desert /
      cyber / riot). Other lanes ignore them.
    - ``context`` is the place (the frame's vision read, the world's brief):
      text or [(text, weight)], counted at a fraction of the main text.
    - ``seed`` breaks ties by hash, so it is stable for one seed and differs
      across seeds.
    - ``prefer`` (an entry id) keeps the current bed while it is still within
      PREFER_RATIO of the best — hysteresis against a crossfade every turn.
    - ``generic_only`` restricts ambience to the seven generic beds, which is
      what the scene_ambience switch turns the lane down to.
    - With nothing matched the lane's ``fallback`` entries are the pool.
    """
    rows = entries(lane, catalog=catalog)
    if generic_only:
        rows = [e for e in rows if e.get("generic")] or rows
    if not rows:
        return None
    if lane == "stinger":
        exact = [e for e in rows if e.get("id") == str(text or "").strip()]
        if exact:
            return exact[0]
    scored = score_all(lane, text, mode=mode, phase=phase, flavor=flavor,
                       context=context, catalog=catalog, pool=rows)
    best = max(s for s, _ in scored)
    if best <= 0 and lane != "music":
        pool = [e for e in rows if e.get("fallback")] or rows
        return min(pool, key=lambda e: _hash(seed, str(e.get("id"))))
    if prefer:
        for s, e in scored:
            if e.get("id") == prefer and s > 0 and s >= best * PREFER_RATIO:
                return e
    top = [e for s, e in scored if s == best]
    return min(top, key=lambda e: _hash(seed, str(e.get("id"))))
