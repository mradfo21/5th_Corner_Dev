"""The run's look book: a contact sheet the whole run is drawn against.

Why this exists
---------------
Every frame in this game is generated, and until now nothing decided what the
world's people and creatures LOOK like. The consequence model writes "a Horizon
security guard steps out", the image model invents a guard, and the next frame
that mentions a guard invents another one. Measured on 2026-09-21 (see
CHANGELOG, "The run gets a look book"): the same one-line guard, rendered in
four places, came back as a hazmat suit, a lab tech, an olive gas-mask soldier
and a cop — 3.3/10 on design consistency. With a designed plate of that guard
attached it was the same black-helmeted guard with red goggles every time
(7.0/10). A rival who was only DESCRIBED in text scored 3.0 and bled into the
player (the model handed Jason the rival's camcorder); with his own plate, 6.7
and the player intact.

So, once per run, in the background, this module:

  1. takes the run's encounter roster (encounter.build_encounter_roster, the
     same 12 draws encounters roll from — built here at reset instead of at the
     first encounter, so the book and the fights agree on who exists);
  2. asks one text call, as the production designer, for a BRIEF: look rules,
     nine frames (row 1 cast, row 2 conflicts, row 3 sets) and a designed look
     plus match terms for every roster entry;
  3. shoots two sheets in parallel: the WORLD sheet (3x3) and the ROSTER sheet
     (one full-body design shot per roster entry), in the game's own medium,
     with the player's character sheet attached;
  4. slices both on their black gutters into plates.

What reads it:

  * encounters — the rolled roster entry's plate rides as a cast plate on the
    standoff and every play-out, and its designed look is handed to the brief
    so the words and the pixels describe the same person;
  * ordinary turns — the world sheet rides LAST as a design-only reference, and
    a roster plate rides when the scene text names that roster entry.

What deliberately does NOT read it (each was tried and measured worse):

  * the conflict row as an image reference — handed "spotted at the fence",
    the model drew its guard into a beat about a rival journalist;
  * a written look with no plate — held some designs, lost others, and bled
    into the player when the threat resembled him;
  * a painted/key-art sheet — stylish, and pulled frames off the game's medium.

Everything lives in sessions/<id>/look_book/, outside images/ so the frame
sweeps never touch it, and is rebuilt when a run starts or the bound World
changes (the book is keyed on the World's content, not its name). Nothing here
is ever fatal: no book means the game renders exactly as it did before.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── switches (tunables.py exposes all three to the editor) ─────────────────
# Build the book at all. Off = the game renders exactly as before.
LOOK_BOOK_ENABLED = os.getenv("SOMEWHERE_LOOK_BOOK", "1").strip().lower() not in ("0", "false", "no", "off")
# Attach the world sheet to ordinary turns (stills and flipbook grids).
LOOK_BOOK_SHEET_ON_TURNS = True
# Attach roster plates (encounters, and turns whose text names a roster entry).
LOOK_BOOK_ROSTER_PLATES = True

# The brief writer. 3.8-flash wrote the most specific designs of the four
# models tried (21 s; 3.1-pro took 45 s and was blander; flash-lite was 9 s
# and generic). Falls back down the list on any failure.
BRIEF_MODELS = ("gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite")
# The sheets. The fast play model tops out below 2K; a nine-panel sheet at 1K
# is ~300 px a panel, which is not enough face to copy.
SHEET_MODEL = "gemini-3.1-flash-image"
SHEET_SIZE = "2K"
# What is attached to a turn: re-encoded, bounded JPEGs, not the 6 MB PNGs.
SHEET_REF_MAX = 1600
PLATE_REF_MAX = 1024

BOOK_FILE = "book.json"
_API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# All three are keyed by _slot(): "<session>/<world slug>".
_LOCKS: Dict[str, threading.Lock] = {}
_BUILDING: Dict[str, str] = {}          # slot -> world key being built
_GUARD = threading.Lock()
# Every spawn takes a new ticket; a build whose ticket is no longer the
# session's latest stops at its next save (see _save / _Superseded). Without
# it, GENERATE on a new World waited a whole book for the old World's build
# to finish, and that build's saves overwrote the new one's progress.
_TICKET: Dict[str, int] = {}
# GENERATE shoots the book for the run that closing the editor starts; that
# reset claims it instead of throwing it away (session -> world key).
_PREBUILT: Dict[str, str] = {}


class _Superseded(Exception):
    """A newer build for this session started; this one stops quietly."""


# ── where things are ───────────────────────────────────────────────────────

def _engine():
    import engine
    return engine


def _safe_slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]", "-", str(s or "")).strip("-")[:60]


def world_slug(session_id: str = "default") -> str:
    """Which World's shelf a book lives on.

    Books are kept per World, inside the run's session: navigating the editor
    from THE FIFTH CORNER to SWAT and back shows each World's own book, and
    shooting one never throws another away. A build uses the World it started
    in (_SNAP.slug) whatever the editor binds after; everything else uses the
    World bound now — or, before anything has been bound since the server
    started, the World the session's saved run is in.
    """
    snap = getattr(_SNAP, "slug", None)
    if snap:
        return snap
    try:
        import worlds_store
        bound = _safe_slug(worlds_store.bound_slug())
        if bound:
            return bound
    except Exception:
        pass
    try:
        import experience_store
        st = _engine()._load_state(session_id) or {}
        exp = experience_store.get_experience(st.get("experience_id") or "")
        world = experience_store.world_by_id(exp, st.get("experience_world_id") or "") or {}
        slug = _safe_slug(world.get("slug") or "")
        if slug:
            return slug
    except Exception:
        pass
    return "_live"


def _slot(session_id: str, slug: Optional[str] = None) -> str:
    """The build bookkeeping key: one run, one World."""
    return f"{session_id}/{slug or world_slug(session_id)}"


def book_dir(session_id: str = "default", slug: Optional[str] = None) -> Path:
    d = _engine()._get_session_root(session_id) / "look_book" / (slug or world_slug(session_id))
    d.mkdir(parents=True, exist_ok=True)
    return d


def world_key() -> str:
    """A fingerprint of the World the book was shot for.

    Content, not name: editing the protagonist or the level in the editor
    must invalidate the book as surely as switching Worlds does, or the run
    keeps copying a guard designed for a place that no longer exists.
    """
    try:
        from prompts_store import PROMPTS
        parts = [
            str(PROMPTS.get("world_initial_state") or "")[:4000],
            json.dumps(PROMPTS.get("player_character") or {}, sort_keys=True, default=str),
            json.dumps(PROMPTS.get("setting_reference") or {}, sort_keys=True, default=str),
            str(PROMPTS.get("image_art_direction") or ""),
        ]
    except Exception:
        parts = ["?"]
    return hashlib.sha1("\n".join(parts).encode("utf-8", "ignore")).hexdigest()[:12]


def enabled() -> bool:
    if not LOOK_BOOK_ENABLED:
        return False
    # Never under a test run. The level now WAITS for its book (api_reset ->
    # prepare_level_look_book), so a suite that resets with a real key in the
    # environment would spend on image calls and sit for a minute per reset.
    # Tests that exercise the book patch enabled() themselves.
    try:
        import authoring_sandbox
        if authoring_sandbox.engaged_at() is not None:
            return False
    except Exception:
        pass
    try:
        import ai_provider_manager
        if ai_provider_manager.is_mock_active("image") or ai_provider_manager.is_mock_active("chat"):
            return False
    except Exception:
        pass
    if _api_key():
        return True
    try:
        import provider_bridge   # OpenAI chosen in ACCOUNT answers the book's calls
        return provider_bridge.active()
    except Exception:
        return False


def _api_key() -> str:
    try:
        import gemini_image_utils
        return str(getattr(gemini_image_utils, "GEMINI_API_KEY", "") or "").strip()
    except Exception:
        return str(os.getenv("GEMINI_API_KEY") or "").strip()


# ── reading the book ────────────────────────────────────────────────────────

def load(session_id: str = "default") -> dict:
    try:
        return json.loads((book_dir(session_id) / BOOK_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(session_id: str, book: dict) -> None:
    mine = getattr(_SNAP, "ticket", None)
    if mine is not None and _TICKET.get(_slot(session_id)) != mine:
        raise _Superseded()
    path = book_dir(session_id) / BOOK_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(book, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def current(session_id: str = "default", *, rebuild_if_stale: bool = True) -> dict:
    """The book for the World that is bound NOW, or {}.

    A stale book (the World changed under the run) is never returned — a
    guard designed for the desert has no business in the neon level — and,
    unless told otherwise, a rebuild is started for the new World.
    """
    book = load(session_id)
    key = world_key()
    if book and book.get("world_key") == key:
        return book
    if rebuild_if_stale and enabled() and _BUILDING.get(_slot(session_id)) != key:
        spawn(session_id, reason="world changed" if book else "no book")
    return {}


def _file(session_id: str, name: str) -> Optional[str]:
    if not name:
        return None
    p = book_dir(session_id) / name
    return str(p) if p.exists() else None


def roster(session_id: str = "default") -> List[str]:
    book = current(session_id, rebuild_if_stale=False)
    return [str(k) for k in (book.get("roster") or []) if str(k or "").strip()]


def wait_for_roster(session_id: str = "default", timeout: float = 6.0) -> List[str]:
    """The roster, waiting briefly if the book is being built right now.

    The roster is the first thing the build writes (~2 s after reset), and an
    encounter can fire on the first turn. Waiting a few seconds here is what
    keeps the fight and the book agreeing on who exists.
    """
    deadline = time.time() + max(0.0, timeout)
    while True:
        r = roster(session_id)
        if r or _BUILDING.get(_slot(session_id)) != world_key() or time.time() >= deadline:
            return r
        time.sleep(0.4)


def world_sheet(session_id: str = "default") -> Optional[str]:
    if not LOOK_BOOK_SHEET_ON_TURNS:
        return None
    # Reading never builds. The editor's GENERATE draws through the private
    # frame session (wf-<world>), which has no book; building one here spent a
    # whole book (~60 s of image calls) on every GENERATE, for a picture that
    # is drawn once and never played. A run's book is started by reset.
    book = current(session_id, rebuild_if_stale=False)
    if book.get("status") != "ready":
        return None
    return _file(session_id, book.get("world_sheet_ref") or "")


def _entry_for(book: dict, kind: str) -> Optional[dict]:
    k = _norm(kind)
    if not k:
        return None
    for e in book.get("roster_looks") or []:
        if _norm(e.get("kind")) == k:
            return e
    # The rolled string and the stored one can differ by punctuation/case or a
    # trailing clause; accept a clear prefix match, never a loose one.
    for e in book.get("roster_looks") or []:
        ek = _norm(e.get("kind"))
        if ek and (ek.startswith(k[:40]) or k.startswith(ek[:40])) and min(len(ek), len(k)) >= 20:
            return e
    return None


def look_for(session_id: str, kind: str) -> str:
    """The designed look of a roster entry, for the encounter brief."""
    e = _entry_for(current(session_id, rebuild_if_stale=False), kind)
    return str((e or {}).get("look") or "").strip()


def plate_for(session_id: str, kind: str) -> Optional[str]:
    if not LOOK_BOOK_ROSTER_PLATES:
        return None
    book = current(session_id, rebuild_if_stale=False)
    if book.get("status") != "ready":
        return None
    e = _entry_for(book, kind)
    return _file(session_id, (e or {}).get("plate") or "")


# Words that would match the player, a crowd, or anything at all.
_STOP_TERMS = {
    "figure", "figures", "man", "men", "woman", "women", "person", "people",
    "someone", "somebody", "shape", "thing", "things", "crowd", "stranger",
    "silhouette", "body", "bodies", "shadow", "shadows", "creature", "creatures",
    "voice", "worker", "workers", "local", "locals", "group", "team",
    # Words for ANY dead or changed thing. A live run matched "carcass" — the
    # jackrabbit the player had just killed — to a different creature's plate
    # and drew that creature's design over the body for three turns.
    "carcass", "corpse", "remains", "beast", "beasts", "animal", "animals",
    "mutant", "mutants", "monster", "monsters", "predator", "vermin",
    "infected", "abomination", "husk", "casualty", "operator", "specialist",
    "survivor", "victim", "intruder", "threat", "enemy",
}


def _player_words() -> set:
    try:
        from prompts_store import PROMPTS
        pc = PROMPTS.get("player_character") or {}
        if isinstance(pc, str):
            pc = json.loads(pc)
        blob = " ".join(str(pc.get(k) or "") for k in ("name", "role", "appearance", "wardrobe", "signature_gear"))
    except Exception:
        blob = ""
    return set(re.findall(r"[a-z]+", blob.lower()))


def plates_named_in(session_id: str, text: str, limit: int = 1) -> List[str]:
    """Roster plates for entries the scene text names, earliest mention first.

    This is how the guard from turn 4's fight is the same guard when turn 9
    says "a guard shouts from the catwalk". Match terms come from the brief
    and are filtered against the player's own description, so a
    photojournalist protagonist never pulls in the rival's plate for himself.
    """
    if not LOOK_BOOK_ROSTER_PLATES or not text:
        return []
    book = current(session_id, rebuild_if_stale=False)
    if book.get("status") != "ready":
        return []
    low = " " + str(text).lower() + " "
    usable = usable_terms(book)
    hits = []
    for e in book.get("roster_looks") or []:
        plate = _file(session_id, e.get("plate") or "")
        if not plate:
            continue
        best = None
        for t in usable.get(e.get("kind") or "", []):
            forms = re.escape(t) + r"(?:s|es)?"
            if t.endswith("y"):
                forms = "(?:" + forms + "|" + re.escape(t[:-1]) + "ies)"
            # A compound's everyday tail counts: "rabbit" names the jackrabbit,
            # "hound" the hellhound. Only a short head may be dropped.
            heads = [t[k:] for k in range(1, 5) if len(t) - k >= 5]
            if heads:
                forms = "(?:" + forms + "|" + "|".join(re.escape(h) + r"s?" for h in heads) + ")"
            m = re.search(r"(?<![a-z])" + forms + r"(?![a-z])", low)
            if m and (best is None or m.start() < best):
                best = m.start()
        if best is not None:
            hits.append((best, plate))
    hits.sort()
    out = []
    for _, p in hits:
        if p not in out:
            out.append(p)
        if len(out) >= limit:
            break
    return out


def _too_close(term: str, words: set) -> bool:
    """True if a term could be describing the player.

    "journalist" is inside "photojournalist" and "photographer" shares its
    first five letters; either would pull the rival's plate into a frame that
    only mentions the player doing their job.
    """
    for w in words:
        if len(w) < 4:
            continue
        if term == w or term in w or w in term or term[:5] == w[:5]:
            return True
    return False


def usable_terms(book: dict) -> Dict[str, List[str]]:
    """kind -> match terms that are safe to act on.

    Dropped: stop words, anything that could describe the player, and any term
    two roster entries both claim ("scavenger" for the scrap picker AND the
    coyote) — an ambiguous word attaching the wrong design is worse than none.
    """
    mine = _player_words()
    raw: Dict[str, List[str]] = {}
    count: Dict[str, int] = {}
    for e in book.get("roster_looks") or []:
        ts = []
        for term in e.get("terms") or []:
            t = _norm(term)
            if not t or len(t) < 4 or t in _STOP_TERMS or _too_close(t, mine):
                continue
            if t not in ts:
                ts.append(t)
        raw[e.get("kind") or ""] = ts
        for t in ts:
            count[t] = count.get(t, 0) + 1
    return {k: [t for t in ts if count.get(t, 0) == 1] for k, ts in raw.items()}


# ── the story half (LOOK_BOOK_STORY; see the 2026-09-21 investigation) ─────
# The sheet's rows are also a plan: row 2 is three set pieces at their most
# dangerous instant, row 3 is three places full of story. ON since the full-run A/B (CHANGELOG 2026-09-21): pictures alone lost on
# prose/picture agreement; with the story half the run won every criterion.
LOOK_BOOK_STORY = os.getenv("SOMEWHERE_LOOK_BOOK_STORY", "1").strip().lower() in ("1", "true", "yes", "on")

_PHASE_PIECE = {"normal": 0, "escalating": 1, "critical": 2}


def _frames(book: dict, row: str) -> List[dict]:
    return [f for f in book.get("frames") or []
            if isinstance(f, dict) and str(f.get("row") or "").strip().upper().startswith(row)]


def story_directive(session_id: str, state: dict) -> str:
    """The designed world, as direction for the consequence model.

    Three things, short: who lives here (the roster's designed looks, so the
    prose names the same guard the plate draws), where the run can go (the
    three designed sets), and the set piece this phase is building toward.
    """
    if not LOOK_BOOK_STORY:
        return ""
    book = current(session_id, rebuild_if_stale=False)
    if book.get("status") != "ready":
        return ""
    phase = str((state or {}).get("current_phase") or "normal")
    pieces = _frames(book, "CONFLICT")
    sets = _frames(book, "SET")
    looks = [e for e in book.get("roster_looks") or [] if e.get("look")]
    lines = ["\n\nTHIS RUN'S DESIGNED WORLD (the look book every frame is drawn against — "
             "name things the way it designs them):"]
    if looks:
        lines.append("WHO CAN ARRIVE (as designed): " + " | ".join(
            f"{_short_kind(e['kind'])}: {e['look'][:110]}" for e in looks[:8]))
    if sets:
        lines.append("DESIGNED PLACES this run can reach: " + " | ".join(
            f"{s.get('title')}: {s.get('subject')} — {s.get('story') or ''}" for s in sets[:3]))
    i = _PHASE_PIECE.get(phase)
    if pieces and i is not None and i < len(pieces):
        p = pieces[i]
        lines.append(
            f"THIS PHASE'S SET PIECE ({phase}): {p.get('title')} — {p.get('subject')}; "
            f"{p.get('staging') or ''}. Build toward it and, when the player's action "
            "gives you the opening, STAGE IT in this beat — its danger, at its most "
            "dangerous instant, in this place. Never force it against the action "
            "the player chose, and never teleport to reach it.")
    return "\n".join(lines) + "\n"


def _short_kind(kind: str) -> str:
    k = re.sub(r"^(an?|the)\s+", "", str(kind or "").strip(), flags=re.I)
    return k[:60]


def set_panel_for(session_id: str, text: str) -> Optional[str]:
    """The designed set a relocation just arrived in, if the text names it."""
    if not LOOK_BOOK_STORY or not text:
        return None
    book = current(session_id, rebuild_if_stale=False)
    if book.get("status") != "ready":
        return None
    panels = book.get("panels") or []
    low = str(text).lower()
    best, best_hits = None, 0
    for f in _frames(book, "SET"):
        try:
            idx = int(f.get("n")) - 1
        except Exception:
            continue
        if not (0 <= idx < len(panels)):
            continue
        words = set(re.findall(r"[a-z]{5,}", f"{f.get('title')} {f.get('subject')}".lower()))
        words -= {"empty", "abandoned", "people", "horizon", "desert", "facility"}
        hits = sum(1 for w in words if re.search(r"(?<![a-z])" + re.escape(w), low))
        if hits > best_hits:
            best, best_hits = panels[idx], hits
    if best and best_hits >= 2:
        return _file(session_id, best)
    return None


# ── labels the image layer puts next to these attachments ─────────────────

SHEET_LABEL = (
    "ART DIRECTION CONTACT SHEET for this world — nine production stills: row 1 "
    "the cast, row 2 the conflicts, row 3 the sets. Use it for DESIGN ONLY: how "
    "the people and creatures of this world look (silhouette, wardrobe, colour, "
    "materials), how its places are dressed, its palette and its film look. If "
    "the frame text names someone or something the sheet designs, draw it the "
    "way the sheet designs it. Do NOT reproduce the sheet's layout, gutters, "
    "borders or any single still's composition, and do not add anyone the "
    "frame text does not call for. The output is ONE photograph, not a grid. "
    "THE PLAYER IS NEVER COPIED FROM THIS SHEET: the sheet was shot before "
    "they last changed, and its protagonist can be wearing an older outfit or "
    "show a face the player now covers. Who the player is and what they have "
    "on comes only from the CHARACTER SHEET (the turnaround)."
)


def _plate_label(kind: str) -> str:
    return (
        f"ROSTER PLATE — this is WHO or WHAT arrives in this frame: {kind}. Copy "
        "its exact design (face, build, wardrobe, gear, anatomy, colours). It is "
        "NOT the player character and NOT a composition: place it into the scene "
        "the text describes, at the distance the text describes."
    )


def label_for(path: Any) -> Optional[str]:
    """The caption for a look-book attachment, or None if it isn't one."""
    try:
        p = Path(str(path))
    except Exception:
        return None
    # sessions/<sid>/look_book/<world>/<file> (one shelf per World), or the
    # older flat sessions/<sid>/look_book/<file>.
    if "look_book" not in (p.parent.name, p.parent.parent.name):
        return None
    if p.name.startswith("world_sheet"):
        return SHEET_LABEL
    if p.name.startswith("panel_"):
        return (
            "DESIGNED SET — the art-directed look of the place this frame has "
            "arrived in: its materials, dressing, light and the story its "
            "evidence tells. Build THIS frame's place to that design from the "
            "camera position the text gives. Do not copy the panel's framing, "
            "and add nobody the text does not call for."
        )
    if p.name.startswith("plate_"):
        try:
            book = json.loads((p.parent / BOOK_FILE).read_text(encoding="utf-8"))
            for e in book.get("roster_looks") or []:
                if e.get("plate") == p.name:
                    return _plate_label(str(e.get("kind") or "the roster entry"))
        except Exception:
            pass
        return _plate_label("the roster entry")
    return None


def is_book_path(path: Any) -> bool:
    return label_for(path) is not None


# ── building the book ───────────────────────────────────────────────────────

def spawn(session_id: str = "default", reason: str = "", part: str = "all",
          plate: Optional[int] = None) -> bool:
    """Start building (or rebuilding part of) this session's book. Never blocks.

    part: "all" (new roster, new everything), "brief" (keep the roster, rewrite
    the brief and reshoot both sheets), "world" (reshoot the world sheet),
    "roster" (reshoot the casting sheet and recut its plates), "plate" (reshoot
    one plate, index ``plate``). Everything but "all" works on the book that is
    there, so the editor can judge and redo one stage at a time.
    """
    if not enabled():
        return False
    if str(session_id or "").startswith(_FRAME_SESSION_PREFIX):
        return False
    key = world_key()
    slug = world_slug(session_id)
    slot = _slot(session_id, slug)
    with _GUARD:
        if _BUILDING.get(slot) == key:
            return False
        _BUILDING[slot] = key
        ticket = _TICKET.get(slot, 0) + 1
        _TICKET[slot] = ticket
    snap = _snapshot()
    if part == "all":
        _placeholder(session_id, key, reason, slug)
    t = threading.Thread(target=_build_safely, args=(session_id, key, reason, part, plate, snap, ticket, slug),
                         name=f"look-book-{slot}", daemon=True)
    t.start()
    return True


# world_frames draws each World's opening still in a private session named
# wf-<slug>. Those are not runs and never get a book.
_FRAME_SESSION_PREFIX = "wf-"


def building(session_id: str = "default") -> bool:
    return _BUILDING.get(_slot(session_id)) == world_key()


def _placeholder(session_id: str, key: str, reason: str, slug: Optional[str] = None) -> None:
    """Say at once that a new book is coming, for the progress line.

    The build itself may wait on the session lock for a superseded build to
    stop; until then the old book.json (another World, maybe finished) is
    what the editor would read, and its log said 5/5.
    """
    now = time.time()
    try:
        path = book_dir(session_id, slug) / BOOK_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "world_key": key, "status": "roster", "created": now, "started": now, "reason": reason,
            "timings": {}, "log": [{"t": 0.0, "stage": "start", "msg": f"queued ({reason})"}],
            "world_name": _world_name(_live_prompts()),
        }, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def prebuild_for_generate(session_id: str = "default") -> bool:
    """GENERATE: shoot the book for the World just bound, now.

    Closing the editor after GENERATE restarts the run in that World, and
    reset_for_new_run then claims this book rather than rolling a second one —
    so the author watches it being made in the editor, and the run starts
    with it already done (or already under way). Pressing GENERATE again
    while this World's book is still being shot keeps that build.
    """
    if not enabled() or str(session_id or "").startswith(_FRAME_SESSION_PREFIX):
        return False
    key = world_key()
    _PREBUILT[session_id] = key
    if _BUILDING.get(_slot(session_id)) == key:
        return True
    return spawn(session_id, reason="generate")


# The level does not start without its book (asked: "make sure the look book
# generation is completed BEFORE starting the level. Even if it adds to the
# wait times"). This caps the wait so a stuck build can never hold a run
# forever; past it the level starts and frames go without the book.
LOOK_BOOK_WAIT_S = float(os.getenv("SOMEWHERE_LOOK_BOOK_WAIT_S", "300") or 300)

# A build has a budget, and every call inside it is cut to what is left. The
# first Fifth Corner run in the harness sat on "shooting the world sheet and
# the roster sheet" past five minutes: both sheet calls hung on a 300s socket
# timeout with a retry behind each, so the book could never beat the level's
# wait and the run started on nothing. Now the whole build ends inside
# LOOK_BOOK_BUDGET_S — whatever landed by then is the book — and no single
# image call can hold for longer than SHEET_TIMEOUT_S / PLATE_TIMEOUT_S, wall
# clock (see _post).
LOOK_BOOK_BUDGET_S = float(os.getenv("SOMEWHERE_LOOK_BOOK_BUDGET_S", "240") or 240)
SHEET_TIMEOUT_S = 150
PLATE_TIMEOUT_S = 90


def _left(book: dict) -> float:
    """Seconds this build has left of its budget."""
    start = book.get("started") or book.get("created") or time.time()
    return float(start) + LOOK_BOOK_BUDGET_S - time.time()


def _cut(book: dict, cap: float) -> int:
    """A call's timeout: its own cap, or what the build has left if that is less."""
    return int(max(10.0, min(float(cap), _left(book))))


def prepare_for_level(session_id: str = "default") -> bool:
    """Start this World's book for the run about to begin, ahead of the reset.

    The client calls this before /api/reset (via /api/look_book/prepare, with
    the World already bound) and waits on it with the loading screen up; the
    reset then claims the book (reset_for_new_run) instead of rolling another.
    Calling it again while that book is under way, or once it is done, starts
    nothing. Returns whether a book is (or will be) there.
    """
    if not enabled() or str(session_id or "").startswith(_FRAME_SESSION_PREFIX):
        return False
    key = world_key()
    book = load(session_id)
    if _PREBUILT.get(session_id) == key and (
            _BUILDING.get(_slot(session_id)) == key
            or (book.get("world_key") == key and book.get("status") in ("roster", "brief", "shooting", "ready"))):
        return True
    try:
        shutil.rmtree(book_dir(session_id), ignore_errors=True)
    except Exception:
        pass
    _PREBUILT[session_id] = key
    spawn(session_id, reason="new run")
    return True


def ensure_for_level(session_id: str = "default", reason: str = "arrival") -> bool:
    """A World the run has just arrived in gets its book if it has none.

    Mid-run arrivals (a cutscene into another World, or a direct edge) bind a
    World the run's book was never shot for. The shelf may already hold this
    World's book from earlier in the run; otherwise shoot one now.
    """
    if not enabled() or str(session_id or "").startswith(_FRAME_SESSION_PREFIX):
        return False
    key = world_key()
    if _BUILDING.get(_slot(session_id)) == key:
        return True
    book = load(session_id)
    if book.get("world_key") == key and book.get("status") == "ready":
        return True
    return spawn(session_id, reason=reason)


def wait_ready(session_id: str = "default", timeout: Optional[float] = None) -> str:
    """Block until the bound World's book is finished. Returns its status.

    "ready" or "failed" when the build ended, "none" when nothing is being
    built for this World, "timeout" past ``timeout`` (LOOK_BOOK_WAIT_S), "off"
    when the book is switched off. Never call it holding a lock other runs need.
    """
    if not enabled():
        return "off"
    limit = LOOK_BOOK_WAIT_S if timeout is None else float(timeout)
    deadline = time.time() + max(0.0, limit)
    waited = False
    while True:
        key = world_key()
        if _BUILDING.get(_slot(session_id)) != key:
            book = load(session_id)
            status = str(book.get("status") or "none") if book.get("world_key") == key else "none"
            if waited:
                print(f"[LOOK BOOK] the level waited for its book: {status}", flush=True)
            return status
        if time.time() >= deadline:
            print(f"[LOOK BOOK] gave up waiting after {limit:.0f}s; the level starts without it",
                  flush=True)
            return "timeout"
        waited = True
        time.sleep(0.5)


def reset_for_new_run(session_id: str = "default") -> None:
    """A new run gets a new book: new roster, new cast. Called from reset."""
    key = world_key()
    if _PREBUILT.pop(session_id, None) == key:
        book = load(session_id)
        if _BUILDING.get(_slot(session_id)) == key or (
                book.get("world_key") == key and book.get("status") in ("roster", "brief", "shooting", "ready")):
            print("[LOOK BOOK] the run takes the book already shot for it", flush=True)
            return
    # Only this World's shelf: the others' books stay for the editor.
    try:
        shutil.rmtree(book_dir(session_id), ignore_errors=True)
    except Exception:
        pass
    spawn(session_id, reason="new run")


def _build_safely(session_id: str, key: str, reason: str, part: str = "all",
                  plate: Optional[int] = None, snap: Optional[dict] = None,
                  ticket: Optional[int] = None, slug: Optional[str] = None) -> None:
    slot = _slot(session_id, slug)
    lock = _LOCKS.setdefault(slot, threading.Lock())
    _SNAP.prompts = snap
    _SNAP.ticket = ticket
    _SNAP.slug = slug
    try:
        with lock:
            if ticket is not None and _TICKET.get(slot) != ticket:
                raise _Superseded()
            book = load(session_id)
            if part == "all" or not book or book.get("world_key") != key or not book.get("roster"):
                _build(session_id, key, reason)
            else:
                _rebuild_part(session_id, key, book, part, plate)
    except _Superseded:
        print(f"[LOOK BOOK] build for {key} stopped: a newer one started", flush=True)
    except Exception as err:
        print(f"[LOOK BOOK] build failed: {err}", flush=True)
        try:
            book = load(session_id)
            if book.get("world_key") == key:
                book["status"] = "failed"
                book["error"] = str(err)[:300]
                _log(session_id, book, f"failed: {err}")
        except Exception:
            pass
    finally:
        _SNAP.ticket = None
        _SNAP.slug = None
        with _GUARD:
            if _BUILDING.get(slot) == key and _TICKET.get(slot) == ticket:
                _BUILDING.pop(slot, None)


def _log(session_id: str, book: dict, msg: str, stage: str = "") -> None:
    """One line of the build's story, for the editor's timeline and the log."""
    t0 = book.get("started") or book.get("created") or time.time()
    book.setdefault("log", []).append({"t": round(time.time() - t0, 1), "stage": stage, "msg": msg})
    book["log"] = book["log"][-60:]
    _save(session_id, book)
    print(f"[LOOK BOOK] {msg}", flush=True)


def _stage_roster(session_id: str, book: dict) -> None:
    import encounter
    eng = _engine()
    rost: List[str] = []
    try:
        st = eng._load_state(session_id) or {}
        cached = st.get("encounter_roster")
        if isinstance(cached, (list, tuple)):
            rost = [str(k) for k in cached if str(k or "").strip()]
    except Exception:
        rost = []
    src = "the run's own"
    if not rost:
        rost = encounter.build_encounter_roster(session_id) or []
        src = "drawn from the bible"
    book["roster"] = rost
    _log(session_id, book, f"roster: {len(rost)} entries ({src})", "roster")


def _stage_brief(session_id: str, book: dict) -> None:
    t1 = time.time()
    book["status"] = "brief"
    _log(session_id, book, "brief: the production designer is writing…", "brief")
    rost = book.get("roster") or []
    brief = _write_brief(session_id, rost)
    looks = []
    by_kind = {_norm(r.get("kind")): r for r in brief.get("roster_looks") or [] if isinstance(r, dict)}
    ordered = [r for r in brief.get("roster_looks") or [] if isinstance(r, dict)]
    for i, kind in enumerate(rost):
        # By name first; the brief is told to keep the order, so by position
        # is the fallback when it paraphrased the kind.
        r = by_kind.get(_norm(kind)) or (ordered[i] if i < len(ordered) else {})
        looks.append({
            "kind": kind,
            "look": str((r or {}).get("look") or "").strip(),
            "terms": [str(t).strip().lower() for t in ((r or {}).get("terms") or []) if str(t).strip()][:5],
        })
    book.update({"look_rules": brief.get("look_rules") or {},
                 "frames": brief.get("frames") or [],
                 "roster_looks": looks,
                 "items": _clean_items(brief.get("items"))})
    book["timings"]["brief"] = round(time.time() - t1, 1)
    _log(session_id, book, f"brief: {len(book['frames'])} frames, {len(looks)} designed looks, "
                           f"{len(book.get('items') or [])} treasures "
                           f"in {book['timings']['brief']}s", "brief")
    kinds = [i["kind"] for i in book.get("items") or []]
    for it in book.get("items") or []:
        print(f"[LOOK BOOK] {it['kind']}: {it['name']} — "
              f"{it.get('power') or it.get('use')}", flush=True)
    missing = [k for k in ITEM_KINDS[:3] if k not in kinds]
    if missing:
        print(f"[LOOK BOOK] the gear has no {', '.join(missing)} in it — "
              f"the run can only hand out {', '.join(sorted(set(kinds)))}", flush=True)


def _shoot(session_id: str, book: dict, name: str, fn) -> Optional[bytes]:
    # One retry: the sheet model sometimes answers with text and no image
    # (seen on the first live build — the world sheet simply was not there),
    # and a second ask almost always lands.
    for attempt in (1, 2):
        if attempt == 2 and _left(book) < 30:
            print(f"[LOOK BOOK] {name} sheet: no time left in the budget for a second ask", flush=True)
            return None
        try:
            data = fn(session_id, book)
        except Exception as err:
            data = None
            print(f"[LOOK BOOK] {name} sheet attempt {attempt} failed: {err}", flush=True)
        if data:
            return data
        print(f"[LOOK BOOK] {name} sheet attempt {attempt} came back empty", flush=True)
    return None


def _stage_world(session_id: str, book: dict, data: Optional[bytes] = None) -> None:
    d = book_dir(session_id)
    t = time.time()
    if data is None:
        _log(session_id, book, "world sheet: shooting…", "world")
        data = _shoot(session_id, book, "world", _shoot_world_sheet)
    if not data:
        _log(session_id, book, "world sheet: the model returned no image twice", "world")
        return
    (d / "world_sheet.png").write_bytes(data)
    _save_ref(d / "world_sheet.png", d / "world_sheet_ref.jpg", SHEET_REF_MAX)
    book["world_sheet"] = "world_sheet.png"
    book["world_sheet_ref"] = "world_sheet_ref.jpg"
    names = []
    for i, im in enumerate(_slice(d / "world_sheet.png", 3, 3), 1):
        n = f"panel_{i:02d}.jpg"
        im.save(d / n, quality=90)
        names.append(n)
    book["panels"] = names
    book["version"] = int(book.get("version") or 0) + 1
    _log(session_id, book, f"world sheet: done ({round(time.time() - t, 1)}s)", "world")


def _stage_roster_sheet(session_id: str, book: dict, data: Optional[bytes] = None) -> None:
    d = book_dir(session_id)
    looks = book.get("roster_looks") or []
    if not looks:
        return
    for e in looks:
        e.pop("plate", None)
        e.pop("plate_source", None)
    if data is None:
        _log(session_id, book, "roster sheet: shooting…", "roster_sheet")
        data = _shoot(session_id, book, "roster", _shoot_roster_sheet)
    for stale in book_dir(session_id).glob("plate_*.jpg"):
        try:
            stale.unlink()
        except Exception:
            pass
    cells = []
    if data:
        (d / "roster_sheet.png").write_bytes(data)
        book["roster_sheet"] = "roster_sheet.png"
        # Row by row: the model does not always draw the grid it is asked
        # for (a live sheet came back 4 / 5 / 4 with numbers printed in the
        # corners), and an even split of that cut frames in half.
        cells = _slice_cells(d / "roster_sheet.png")
        _log(session_id, book, f"roster sheet: {len(cells)} frames cut", "roster_sheet")
    else:
        _log(session_id, book, "roster sheet: no image — every plate will be shot on its own", "roster_sheet")
    # A plate on the wrong roster entry is worse than none (the first live
    # uneven sheet put a hazmat suit on the activist). So the CROPS — not the
    # sheet — are shown to a vision pass, numbered by us, and each is kept
    # only where it is clearly one whole roster entry.
    t3 = time.time()
    placement = _place_crops(d, cells, looks) if cells else {}
    book["placement"] = {str(k): v for k, v in placement.items()}
    book["placement_check"] = "placement_check.jpg" if (d / "placement_check.jpg").exists() else None
    for i, e in enumerate(looks):
        cell = placement.get(i)
        if cell is None:
            continue
        n = f"plate_{i + 1:02d}.jpg"
        im = cells[cell].copy()
        im.thumbnail((PLATE_REF_MAX, PLATE_REF_MAX))
        im.save(d / n, quality=90)
        e["plate"] = n
        e["plate_source"] = f"sheet frame {cell + 1}"
    _log(session_id, book, f"crop check: {len(placement)}/{len(looks)} crops placed", "placement")
    # Whatever the sheet did not deliver cleanly is shot on its own.
    missing = [i for i, e in enumerate(looks) if not e.get("plate")]
    if missing:
        _log(session_id, book, f"shooting {len(missing)} plate(s) one by one…", "plates")
        _shoot_single_plates(session_id, book, d, missing)
    book["timings"]["placement"] = round(time.time() - t3, 1)
    book["version"] = int(book.get("version") or 0) + 1
    _log(session_id, book, f"plates: {sum(1 for e in looks if e.get('plate'))}/{len(looks)}", "plates")


def _finish(session_id: str, book: dict, key: str) -> None:
    looks = book.get("roster_looks") or []
    ok = bool(book.get("world_sheet") or any(e.get("plate") for e in looks))
    # The build shot the World frozen at its start (_SNAP), so the book is
    # right for that World whatever the editor bound since; current() is what
    # keeps it away from a different one. Marking it "stale" here threw away a
    # good book whenever the editor was open during a build.
    book["status"] = "ready" if ok else "failed"
    book["timings"]["total"] = round(time.time() - (book.get("started") or time.time()), 1)
    its = book.get("items") or []
    _log(session_id, book, f"{book['status']} in {book['timings']['total']}s — world sheet "
                           f"{'yes' if book.get('world_sheet') else 'NO'}, "
                           f"{sum(1 for e in looks if e.get('plate'))}/{len(looks)} roster plates, "
                           f"{sum(1 for e in its if e.get('plate'))}/{len(its)} treasures", "done")


def _build(session_id: str, key: str, reason: str) -> None:
    d = book_dir(session_id)
    for stale in d.iterdir():
        try:
            if stale.is_file():
                stale.unlink()
        except Exception:
            pass
    now = time.time()
    book: Dict[str, Any] = {"world_key": key, "status": "roster", "created": now, "started": now,
                            "reason": reason, "timings": {}, "log": [],
                            "world_name": _world_name(_prompts())}
    _log(session_id, book, f"building ({reason})", "start")
    t0 = time.time()
    _stage_roster(session_id, book)
    book["timings"]["roster"] = round(time.time() - t0, 1)
    _stage_brief(session_id, book)

    # Two sheets, shot in parallel, then stored in order.
    book["status"] = "shooting"
    _log(session_id, book, "shooting the world, the cast and the props…", "shoot")
    t2 = time.time()
    results: Dict[str, Optional[bytes]] = {}
    jobs = [("world", _shoot_world_sheet)]
    if book.get("roster_looks"):
        jobs.append(("roster", _shoot_roster_sheet))
    # The treasures ride along in the same parallel batch, so six objects with
    # pictures cost one image call and almost no wall clock on a loading
    # screen the run already waits through.
    if book.get("items"):
        jobs.append(("items", _shoot_item_sheet))
    threads = [threading.Thread(target=_carry(lambda n=n, f=f: results.__setitem__(n, _shoot(session_id, book, n, f))),
                                daemon=True) for n, f in jobs]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    book["timings"]["sheets"] = round(time.time() - t2, 1)
    if results.get("world"):
        _stage_world(session_id, book, results["world"])
    else:
        _log(session_id, book, "world sheet: the model returned no image twice", "world")
    if book.get("roster_looks"):
        _stage_roster_sheet(session_id, book, results.get("roster") or b"")
    if book.get("items"):
        _stage_item_sheet(session_id, book, results.get("items") or b"")
    _finish(session_id, book, key)


def _rebuild_part(session_id: str, key: str, book: dict, part: str, plate: Optional[int]) -> None:
    book["started"] = time.time()
    book["timings"] = {}
    book["log"] = []
    book["error"] = None
    book["status"] = "shooting"
    _log(session_id, book, f"regenerating: {part}{'' if plate is None else f' #{plate + 1}'}", "start")
    if part == "brief":
        _stage_brief(session_id, book)
        results: Dict[str, Optional[bytes]] = {}
        threads = [threading.Thread(target=_carry(lambda n=n, f=f: results.__setitem__(n, _shoot(session_id, book, n, f))),
                                    daemon=True)
                   for n, f in (("world", _shoot_world_sheet), ("roster", _shoot_roster_sheet))]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        if results.get("world"):
            _stage_world(session_id, book, results["world"])
        _stage_roster_sheet(session_id, book, results.get("roster") or b"")
    elif part == "world":
        _stage_world(session_id, book)
    elif part == "roster":
        _stage_roster_sheet(session_id, book)
    elif part == "plate" and plate is not None and 0 <= plate < len(book.get("roster_looks") or []):
        _shoot_single_plates(session_id, book, book_dir(session_id), [plate])
        book["version"] = int(book.get("version") or 0) + 1
    _finish(session_id, book, key)


def _place_crops(d: Path, cells: list, looks: list, *, key: str = "kind",
                 what: str = "roster", check_name: str = "placement_check.jpg",
                 strictly: str = "") -> Dict[int, int]:
    """entry index -> crop index, checked by eye on the crops themselves.

    The crops are laid out on a montage WE number, so the answer cannot be
    fooled by numbers the sheet model printed, and a crop that caught half of
    two frames reads as what it is. No answer means no plates from the sheet
    (they are then shot one by one) — never a blind "frame i is entry i".
    """
    from PIL import Image, ImageDraw
    n = len(cells)
    cols = 4 if n > 9 else 3
    tw = 360
    th = int(tw * 0.75)
    rows = (n + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (tw + 10) + 10, rows * (th + 46) + 10), (0, 0, 0))
    draw = ImageDraw.Draw(sheet)
    for k, im in enumerate(cells):
        x = 10 + (k % cols) * (tw + 10)
        y = 10 + (k // cols) * (th + 46)
        t = im.copy()
        t.thumbnail((tw, th))
        sheet.paste(t, (x, y))
        draw.rectangle((x, y + th + 4, x + 60, y + th + 40), fill=(255, 255, 255))
        draw.text((x + 8, y + th + 10), f"#{k + 1}", fill=(0, 0, 0))
    path = d / check_name
    sheet.save(path, quality=88)
    menu = "\n".join(f"{i + 1}. {e.get(key)} — {e.get('look') or ''}"
                     for i, e in enumerate(looks))
    strictly = strictly or ("Be strict: a coyote is not a dog pack, a guard is "
                            "not a soldier squad, a lab coat is not a hazmat suit.")
    prompt = (
        f"Each numbered tile (#1..#{n}, number in the white box under it) is one "
        f"crop from a {what} sheet. Below is the list of entries.\n\n"
        + menu + "\n\n"
        "For EVERY tile, give the number of the list entry it clearly shows, or 0 "
        "if it shows none of them, shows parts of two different frames, is cut "
        "off so the subject is not whole, or is blank. " + strictly + " "
        f"Return JSON: {{\"tiles\": [e1, e2, ...]}} with exactly {n} numbers."
    )
    try:
        data = _post("gemini-3.5-flash", [_img_part(str(path), 1600), {"text": prompt}],
                     {"temperature": 0, "responseMimeType": "application/json"},
                     timeout=90, operation="look_book_placement", service="text")
        raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", _text_of(data).strip())
        got = json.loads(raw)
        tiles = got.get("tiles") if isinstance(got, dict) else got
    except Exception as err:
        print(f"[LOOK BOOK] placement check failed ({err}) - plates will be shot one by one", flush=True)
        return {}
    out: Dict[int, int] = {}
    for cell, entry in enumerate(tiles or []):
        try:
            k = int(entry) - 1
        except Exception:
            continue
        if 0 <= k < len(looks) and k not in out and cell < n:
            out[k] = cell
    moved = sum(1 for k, c in out.items() if k != c)
    print(f"[LOOK BOOK] {what} crops: {n} cut, {len(out)}/{len(looks)} placed"
          f"{f', {moved} out of order' if moved else ''}", flush=True)
    return out


def _shoot_single_plates(session_id: str, book: dict, d: Path, idxs: List[int]) -> None:
    """One design shot per roster entry the sheet did not deliver cleanly."""
    looks = book.get("roster_looks") or []
    char = _character_parts()

    def one(i):
        e = looks[i]
        prompt = (
            f"{_medium(book)}\n\nONE full-body design shot of: {e['kind']} — "
            f"{e.get('look') or e['kind']}.\nPhotographed ON LOCATION in this world's own "
            "terrain and light, three-quarter to camera, the whole silhouette readable "
            "head to toe, simple uncluttered ground and sky behind. Not a studio, no "
            "backdrop, no text, no border."
            + (" It must look clearly different from the protagonist in the attached "
               "reference, who does NOT appear." if char else "")
        )
        parts = ([{"text": "PROTAGONIST (for contrast only — do not draw them):"}] + char[1:2]
                 if char else []) + [{"text": prompt}]
        for attempt in (1, 2):
            if _left(book) < 20:
                print(f"[LOOK BOOK] single plate {i + 1}: the budget is spent", flush=True)
                return
            try:
                data = _post(SHEET_MODEL, parts,
                             {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "4:3", "imageSize": "1K"}},
                             timeout=_cut(book, PLATE_TIMEOUT_S), operation="look_book_plate", service="image")
                img = _image_of(data)
                if img:
                    from PIL import Image
                    im = Image.open(io.BytesIO(img)).convert("RGB")
                    im.thumbnail((PLATE_REF_MAX, PLATE_REF_MAX))
                    n = f"plate_{i + 1:02d}.jpg"
                    im.save(d / n, quality=90)
                    e["plate"] = n
                    e["plate_source"] = "shot on its own"
                    return
            except Exception as err:
                print(f"[LOOK BOOK] single plate {i + 1} attempt {attempt} failed: {err}", flush=True)

    threads = [threading.Thread(target=_carry(one), args=(i,), daemon=True) for i in idxs]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    print(f"[LOOK BOOK] shot {sum(1 for i in idxs if looks[i].get('plate'))}/{len(idxs)} "
          f"roster plate(s) one by one", flush=True)


def roster_grid(n: int):
    """(cols, rows) for n roster plates."""
    if n <= 4:
        return (2, 2)
    if n <= 6:
        return (3, 2)
    if n <= 9:
        return (3, 3)
    return (4, 3)


# ── the calls ───────────────────────────────────────────────────────────────

def _post(model: str, parts: list, gen_cfg: dict, timeout: int, operation: str,
          service: str) -> dict:
    import requests
    t0 = time.time()
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": gen_cfg,
        "safetySettings": [{"category": c, "threshold": "BLOCK_NONE"} for c in (
            "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")],
    }
    ok, err, data = False, None, {}
    try:
        # requests' timeout is per socket read, not per call: a response that
        # trickles in never trips it. The call runs on its own thread and is
        # abandoned at the wall-clock limit.
        box: Dict[str, Any] = {}

        def call():
            try:
                box["r"] = requests.post(_API.format(model=model), headers={"x-goog-api-key": _api_key()},
                                         json=body, timeout=(15, timeout))
            except Exception as e:  # noqa: BLE001
                box["e"] = e
        th = threading.Thread(target=call, daemon=True)
        th.start()
        th.join(timeout + 5)
        if th.is_alive():
            raise TimeoutError(f"no answer in {timeout}s")
        if "e" in box:
            raise box["e"]
        r = box["r"]
        if r.status_code != 200:
            err = f"{r.status_code} {r.text[:200]}"
        else:
            data = r.json()
            ok = True
    except Exception as e:
        err = repr(e)
    try:
        eng = _engine()
        eng.cost_tracker.record_usage(
            eng.get_active_session_id(), service, "gemini", model,
            operation=operation, input_units=1 if service == "image" else None,
            unit_type="images" if service == "image" else "tokens",
            latency_ms=int((time.time() - t0) * 1000), success=ok, error_message=err)
    except Exception:
        pass
    if not ok:
        raise RuntimeError(err or "no response")
    return data


def _text_of(data: dict) -> str:
    cands = data.get("candidates") or []
    parts = ((cands[0] if cands else {}).get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def _image_of(data: dict) -> Optional[bytes]:
    cands = data.get("candidates") or []
    for p in ((cands[0] if cands else {}).get("content") or {}).get("parts") or []:
        if "inlineData" in p:
            return base64.b64decode(p["inlineData"]["data"])
    why = (cands[0].get("finishReason") if cands else None) or \
        (data.get("promptFeedback") or {}).get("blockReason") or "no image part"
    print(f"[LOOK BOOK] sheet model returned no image ({why}): {_text_of(data)[:160]!r}", flush=True)
    return None


def _img_part(path: str, max_side: int = 1536) -> dict:
    from PIL import Image
    im = Image.open(path).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    return {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}}


# The World a build is shooting, frozen when the build starts. The live prompt
# file is shared with the editor, which binds whichever World the author opens
# — a build that read it stage by stage wrote the brief for SWAT and shot the
# sheets for THE FIFTH CORNER when the author switched Worlds mid-build.
_SNAP = threading.local()


def _live_prompts() -> dict:
    try:
        from prompts_store import PROMPTS
        return PROMPTS
    except Exception:
        return {}


def _snapshot() -> dict:
    try:
        return json.loads(json.dumps(dict(_live_prompts()), default=str))
    except Exception:
        return dict(_live_prompts())


def _carry(fn):
    """Run ``fn`` on another thread with this thread's frozen World and ticket."""
    snap = getattr(_SNAP, "prompts", None)
    ticket = getattr(_SNAP, "ticket", None)
    slug = getattr(_SNAP, "slug", None)

    def run(*a, **k):
        _SNAP.prompts = snap
        _SNAP.ticket = ticket
        _SNAP.slug = slug
        try:
            return fn(*a, **k)
        except _Superseded:
            return None
    return run


def _prompts() -> dict:
    snap = getattr(_SNAP, "prompts", None)
    return snap if snap is not None else _live_prompts()


def _world_name(P: dict) -> str:
    sr = _as_dict(P.get("setting_reference"))
    pc = _as_dict(P.get("player_character"))
    bits = [str(sr.get("name") or "").strip(), str(pc.get("name") or "").strip()]
    return " · ".join(b for b in bits if b)[:80]


def _as_dict(v) -> dict:
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v) if v else {}
    except Exception:
        return {}


def _bible(session_id: str) -> str:
    P = _prompts()
    bits = []
    try:
        import experience_store
        lore = str(experience_store.lore_brief() or "").strip()
        if lore:
            bits.append(lore[:3000])
    except Exception:
        pass
    bits.append(str(P.get("world_initial_state") or "")[:6000])
    return "\n\n".join(b for b in bits if b)


BRIEF_INSTRUCTIONS = """You are the production designer and director of photography for a game whose every frame is generated. Before shooting, you make ONE contact sheet that art-directs the whole run: the cast in their defining poses, the conflicts at their most dangerous instant, and the sets as places full of story. Every later frame is generated with this sheet beside it, so it must fix DESIGN decisions the text never makes: silhouettes, colours, materials, wardrobe specifics, how creatures are built, how each place is dressed, how danger is staged.

Use ONLY what this world already contains. Sharpen it; do not replace it. Everything must be something the world's own camera and era could photograph."""


def _write_brief(session_id: str, rost: List[str]) -> dict:
    P = _prompts()
    pc = _as_dict(P.get("player_character"))
    # A run that is a Character designs ITS protagonist, not the World's old
    # cast sheet — the brief wrote "Jason Fleece, PRESS flak jacket" into the
    # frames of a run whose player was a masked cyborg, and the sheet then rode
    # beside every turn saying so.
    try:
        import game_identity
        if game_identity.bound_character().get("id"):
            _pc = game_identity.get_spec()[game_identity.CHARACTER_KEY]
            pc = {k: _pc.get(k) for k in ("name", "role", "appearance", "wardrobe", "signature_gear", "demeanor")}
    except Exception:
        pass
    st = _as_dict(P.get("setting_reference"))
    prompt = f"""{BRIEF_INSTRUCTIONS}

WORLD BIBLE:
{_bible(session_id)}

PROTAGONIST (a reference image may exist; keep him/her exactly): {json.dumps({k: pc.get(k) for k in ('name', 'role', 'appearance', 'wardrobe', 'signature_gear', 'demeanor') if pc.get(k)})}
LEVEL: {json.dumps({k: st.get(k) for k in ('name', 'summary', 'era', 'palette', 'landmarks') if st.get(k)})}
ART DIRECTION: {str(P.get('image_art_direction') or '')[:2500]}
NEVER: {str(P.get('image_negative_prompt') or '')[:1200]}
THIS RUN'S ENCOUNTER ROSTER: {json.dumps(rost)}

Design a 3x3 sheet:
ROW 1 CAST — frame 1 the protagonist in a defining, stylish pose; frames 2-3 the two most important adversaries from the roster (one human faction, one non-human if the roster has one), each a full-body design shot that makes their silhouette unmistakable.
ROW 2 CONFLICT — three extreme conflicts at the most dangerous instant, each a different kind of danger, mid-action, readable at a glance, with the protagonist in frame.
ROW 3 SETS — three distinct locations from the bible, empty of people, dressed with narrative evidence so each tells a story by itself; each different from the others in terrain, light and material.

For each frame: n, row, title (<=4 words, caps), subject, staging (pose/action + camera), light, design_specifics (3-5 concrete, visual, reusable decisions: exact colours, materials, markings, shapes), story (one clause).
look_rules: palette (5 named colours with hex), film (stock/grain/contrast), lens, motifs (3 recurring visual motifs).
items: NINE PIECES OF GEAR — the loot of this world, in two tiers. Think of the best loot in a game you love — it is specific, it has a story stuck to it, it is visibly used, and the moment you see it you know what it is for.
SIX TREASURES (tier "treasure") — what the player crosses a level to get, the things a player would be THRILLED to find: gear that changes what they can do, drawn out of this world's own materials and history — and they must cover at least one of each of the first three kinds:
  weapon — what they fight with. Not a generic gun: this world's weapon.
  armor — what keeps them alive. Worn or carried, and visibly so.
  upgrade — something that permanently changes what they are capable of: sight, reach, breath, speed, access.
  tool — opens the world: cuts, climbs, unlocks, crosses.
  relic — proves or reveals something. The story object.
THREE SPOILS (tier "spoil") — what this world's hostiles carry, and drop when the player beats them: lesser than a treasure but REAL and useful — a sidearm or blade, a plate or helmet, a stim or filter, a pass or a tool. Named with the same care; each clearly the kind of thing one of the roster above would have on them.
For each: name (2-3 words, Title Case, the label printed on the picture — a NAME with character, never a generic noun like Core, Module, Key, Device or Unit on its own); tier (treasure or spoil); kind (exactly one of weapon, armor, upgrade, tool, relic); look (ONE sentence for an image prompt: what it is, what it is made of, its size against a hand, its wear and damage — the object alone, no setting, no proper nouns); power (ONE short line, the way a game states what a thing does for you — concrete and specific, never a stat and never vague: "Cuts a sealed door in one pass", "Lets you breathe where the air is bad", "Shows what is behind a wall"); use (one plain sentence on how it is actually used); worth (one sentence: why someone in this world would kill for it).
Each must be small enough for one person to carry out in their hands. All nine clearly different from each other — never two of the same idea.
roster_looks: for EVERY roster entry, in the same order: kind (copied exactly), look (<=28 words, a visible design consistent with the sheet — what the game draws when this arrives; make each entry look clearly different from the others and from the protagonist), terms (2-4 lowercase nouns a narrator would use for it in a sentence — always include the plain everyday noun, e.g. "guard", "sentry", "rabbit" for a jackrabbit; never words that would also describe the protagonist, and never words that fit any creature or body, like figure, man, person, creature, beast, carcass, mutant).
Return JSON: {{"look_rules":{{...}},"frames":[...],"roster_looks":[{{"kind":"...","look":"...","terms":["..."]}}],"items":[{{"name":"...","tier":"treasure|spoil","kind":"weapon|armor|upgrade|tool|relic","look":"...","power":"...","use":"...","worth":"..."}}]}}"""
    last = None
    for model in BRIEF_MODELS:
        try:
            data = _post(model, [{"text": prompt}],
                         {"temperature": 0.8, "responseMimeType": "application/json"},
                         timeout=120, operation="look_book_brief", service="text")
            raw = _text_of(data).strip()
            raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", raw)
            out = json.loads(raw)
            if isinstance(out, list):
                out = out[0] if out else {}
            if out.get("frames"):
                return out
            last = "no frames"
        except Exception as e:
            last = e
            print(f"[LOOK BOOK] brief via {model} failed: {e}", flush=True)
    raise RuntimeError(f"brief failed: {last}")


def _medium(book: dict) -> str:
    lr = book.get("look_rules") or {}
    P = _prompts()
    art = str(P.get("image_art_direction") or "").strip()
    film = lr.get("film") or ""
    lens = lr.get("lens") or ""
    return (
        "A CONTACT SHEET of production stills shot in this world's own medium — "
        f"{film}{', ' + lens if lens else ''}. Staged by a great cinematographer: "
        "stylish, iconic poses and compositions, but every frame must read as the "
        "same medium the game itself is drawn in, never as illustration or concept "
        "art.\nTHE GAME'S ART DIRECTION (the sheet obeys it):\n" + art[:2000] +
        "\n\nHARD RULES: every frame is a photograph — not a painting, not an "
        "illustration, not CGI, not concept art, not a game render. NO TEXT OF ANY "
        "KIND anywhere: no row labels (not \"CAST\", not \"SETS\"), no frame "
        "numbers, no captions, no titles, no watermarks."
    )


def _character_parts() -> list:
    try:
        import game_identity
        paths = [p for p in game_identity.character_reference_paths(game_identity.get_spec()) if p and os.path.exists(p)]
    except Exception:
        paths = []
    parts = []
    for p in paths[:2]:
        parts += [{"text": "PROTAGONIST REFERENCE:"}, _img_part(p, 1536)]
    return parts


def _protagonist_line() -> str:
    # A run that is a Character (characters.py) is that character, whatever
    # the World's snapshot of the old cast sheet says: the roster is designed
    # to look unlike THEM.
    try:
        import game_identity
        meta = game_identity.bound_character()
        if meta.get("id"):
            spec = game_identity.get_spec()
            pc = spec[game_identity.CHARACTER_KEY]
            look = str(meta.get("wardrobe_line") or pc.get("wardrobe") or "")
            bits = ", ".join(b for b in (str(pc.get("appearance") or ""), look) if b)
            return f"The protagonist is {pc.get('name') or 'the protagonist'}: {bits}." if bits \
                else f"The protagonist is {pc.get('name') or 'the protagonist'}."
    except Exception:
        pass
    pc = _as_dict(_prompts().get("player_character"))
    look = ", ".join(str(pc.get(k)) for k in ("appearance", "wardrobe", "signature_gear") if pc.get(k))
    name = pc.get("name") or "the protagonist"
    return f"The protagonist is {name}: {look}." if look else f"The protagonist is {name}."


def _shoot_world_sheet(session_id: str, book: dict) -> Optional[bytes]:
    lr = book.get("look_rules") or {}
    lines = []
    for f in book.get("frames") or []:
        if not isinstance(f, dict):
            continue
        ds = f.get("design_specifics") or []
        if isinstance(ds, str):
            ds = [ds]
        lines.append(f"FRAME {f.get('n')} ({f.get('row')}) — {f.get('title')}: {f.get('subject')}. "
                     f"{f.get('staging')}. Light: {f.get('light')}. Design: {'; '.join(map(str, ds))}.")
    neg = str(_prompts().get("image_negative_prompt") or "")[:800]
    prompt = f"""{_medium(book)}

LAYOUT — exact and non-negotiable: a 3 x 3 grid of NINE equal 4:3 frames separated by thin solid BLACK gutters on a black sheet. Reading order left to right, top to bottom. No text, numbers, captions, labels or logos anywhere on the sheet. Each frame is its own complete image; nothing crosses a gutter.
Row 1 = CAST (character design shots). Row 2 = CONFLICT (the most dangerous instant). Row 3 = SETS (empty locations full of story).

{_protagonist_line()} If a protagonist reference is attached, copy that face, build, hair and exact outfit into every frame they appear in.
PALETTE: {json.dumps(lr.get('palette') or {})}. RECURRING MOTIFS: {'; '.join(map(str, lr.get('motifs') or []))}.

{chr(10).join(lines)}

Inside the frames, avoid: {neg}"""
    data = _post(SHEET_MODEL, _character_parts() + [{"text": prompt}],
                 {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "4:3", "imageSize": SHEET_SIZE}},
                 timeout=_cut(book, SHEET_TIMEOUT_S), operation="look_book_world_sheet", service="image")
    return _image_of(data)


# ── THE WORLD'S TREASURES ───────────────────────────────────────────────────
#
# Six objects of extreme value, designed beside the cast and the sets by the
# same production-designer pass, shot on their own sheet in the same medium,
# and handed out as goal rewards (goal.advance draws from this table).
#
# They live here rather than being invented per goal because a reward is only
# worth crossing a level for if it is SCARCE and belongs to this world. An
# object invented to suit the place it is in is worth nothing; one drawn from
# a table of six that this world authored for itself is a find.
#
# The old inventory was twelve hardcoded nouns with emoji (items.py), keyword
# matched out of the narrator's prose — the one system in a game that
# generates every pixel that had no picture in it.

ITEM_COUNT = int(os.getenv("SOMEWHERE_LOOK_BOOK_ITEMS", "9"))
# The first six are TREASURES — what a goal holds; the rest are SPOILS, what
# a beaten hostile drops (goal.award_spoil). A brief that did not label its
# rows is tiered by position.
TREASURE_COUNT = 6
ITEM_TIERS = ("treasure", "spoil")
ITEM_PLATE_MAX = 512
# What a piece of gear is FOR, in the language a player already reads. The
# first three are the spread the brief insists on — a run that hands out three
# relics in a row has given the player nothing to do with them.
ITEM_KINDS = ("weapon", "armor", "upgrade", "tool", "relic")


def _clean_items(raw: Any) -> List[dict]:
    """The brief's item rows, tidied. Anything without a name and a look is
    dropped: a treasure with no picture prompt cannot be shot."""
    out: List[dict] = []
    seen = set()
    for row in (raw or []):
        if not isinstance(row, dict):
            continue
        name = re.sub(r"\s+", " ", str(row.get("name") or "").strip())[:40]
        look = str(row.get("look") or "").strip()[:300]
        if not name or not look:
            continue
        k = _norm(name)
        if k in seen:
            continue
        seen.add(k)
        kind = _norm(row.get("kind"))
        if kind not in ITEM_KINDS:
            # Unlabelled gear still exists; it just does not get to claim a
            # slot in the spread. "relic" is the honest default for a thing
            # whose use nobody stated.
            kind = "relic"
        tier = _norm(row.get("tier"))
        if tier not in ITEM_TIERS:
            tier = "treasure" if len(out) < TREASURE_COUNT else "spoil"
        out.append({"name": name, "kind": kind, "tier": tier, "look": look,
                    "power": str(row.get("power") or "").strip()[:160],
                    "use": str(row.get("use") or "").strip()[:220],
                    "worth": str(row.get("worth") or "").strip()[:220]})
        if len(out) >= ITEM_COUNT:
            break
    return out


def items(session_id: str = "default") -> List[dict]:
    """This world's treasures, with their pictures where the sheet delivered
    one. [] when the book is not ready — the caller falls back to inventing."""
    book = current(session_id, rebuild_if_stale=False)
    if not isinstance(book, dict) or book.get("status") != "ready":
        return []
    out = []
    for it in (book.get("items") or []):
        row = dict(it)
        row["plate"] = _file(session_id, it.get("plate") or "") or ""
        row["ref"] = _file(session_id, it.get("ref") or "") or ""
        row["tier"] = row.get("tier") or "treasure"
        out.append(row)
    return out


def plate_url(path: Any) -> str:
    """A plate on disk as the client fetches it: /api/look_book/<run>/file/<name>.

    Books live under sessions/<run>/look_book/<world>/, outside images/, so a
    disk path is no use to the browser — and the path handed to goal.py once
    went out as ?path=C:\\... to a route that never existed, which is why every
    find card showed a broken picture."""
    p = Path(str(path or "").strip())
    if not p.name:
        return ""
    try:
        slug, book, run = p.parent.name, p.parent.parent.name, p.parent.parent.parent.name
    except Exception:
        return ""
    if book != "look_book" or not run or not slug:
        return ""
    v = ""
    try:
        v = f"&t={int(p.stat().st_mtime)}"
    except Exception:
        pass
    return f"/api/look_book/{run}/file/{p.name}?w={slug}{v}"


# The props are shot on a flat key colour and cut out, so the pack shows the
# THING — item art, the way a game shows loot — and not a photograph of a
# table with a thing on it. "they need to be with a transparent background so
# it doesn't seem like a random ai stock photo. that look is awful" (Matt,
# 2026-09-22). Green unless the world or its gear is green, then magenta.
KEY_COLOURS = {"green": ("#00FF00", (0, 255, 0)), "magenta": ("#FF00FF", (255, 0, 255))}
_GREEN_WORDS = r"\b(green|greens|emerald|jade|lime|olive|moss|mossy|verdigris|chartreuse|viridian|toxic)\b"
_PINK_WORDS = r"\b(magenta|pink|fuchsia|violet|purple|neon pink|hot pink|orchid|mauve|lilac)\b"


def _key_colour(book: dict) -> str:
    """Which flat colour the props are shot on: the one this world uses least."""
    import colorsys
    blob = (json.dumps(book.get("look_rules") or {}) + " "
            + " ".join(str(i.get("look") or "") for i in (book.get("items") or []))).lower()
    score = {"green": len(re.findall(_GREEN_WORDS, blob)),
             "magenta": len(re.findall(_PINK_WORDS, blob))}
    for h in re.findall(r"#([0-9a-f]{6})\b", blob):
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
        hue, sat, val = colorsys.rgb_to_hsv(r, g, b)
        if sat < 0.35 or val < 0.25:
            continue
        if 0.2 < hue < 0.47:
            score["green"] += 2
        elif 0.75 < hue < 0.95:
            score["magenta"] += 2
    return "magenta" if score["green"] > score["magenta"] else "green"


def _cut_out(im, *, feather: float = 0.6):
    """One props-sheet frame -> the object alone on transparency, or None.

    Keyed on chroma, not on RGB distance: the background is found on the
    frame's own border (whatever key the model actually painted, not the one
    it was asked for), and a pixel is background in proportion to how far its
    colour points the SAME WAY as that key and no other way. Luma is left out
    of it, so a soft shadow on the key goes with the key instead of leaving a
    dark green halo under the object, while a dark olive strap — which points
    another way — stays. The edge band is then despilled (the key's hue taken
    out of it), which is what stops the green fringe that makes a cut-out look
    pasted on.

    None when the frame is not on a key at all (a model that painted a room
    or a white sweep) or when what is left is not one object of sensible size:
    a missing plate shows a monogram, a bad one shows a lie."""
    try:
        import numpy as np
        from PIL import Image, ImageFilter
    except Exception:
        return None
    rgb = np.asarray(im.convert("RGB")).astype(np.float32)
    H, W = rgb.shape[:2]
    if H < 16 or W < 16:
        return None
    b = max(2, int(min(H, W) * 0.03))
    ring = np.concatenate([rgb[:b].reshape(-1, 3), rgb[-b:].reshape(-1, 3),
                           rgb[:, :b].reshape(-1, 3), rgb[:, -b:].reshape(-1, 3)])
    bg = np.median(ring, axis=0)

    def ycc(a):
        r, g, bl = a[..., 0], a[..., 1], a[..., 2]
        y = 0.299 * r + 0.587 * g + 0.114 * bl
        cb = -0.168736 * r - 0.331264 * g + 0.5 * bl
        cr = 0.5 * r - 0.418688 * g - 0.081312 * bl
        return y, np.stack([cb, cr], -1)

    ybg, kv = ycc(bg)
    kn = float(np.linalg.norm(kv))
    # A grey, white or black border is not a key: there is nothing to cut on.
    if kn < 45:
        return None
    # ...and a border that is a mess of colours is a room, not a key.
    _, ring_c = ycc(ring)
    spread = float(np.median(np.linalg.norm(ring_c - kv, axis=-1)))
    if spread > kn * 0.35:
        return None
    khat = kv / kn
    kperp = np.array([-khat[1], khat[0]], dtype=np.float32)
    y, c = ycc(rgb)
    p = c @ khat
    q = np.abs(c @ kperp)
    key_abs = (p - 1.25 * q) / kn
    # The same measure with the pixel's brightness brought up to the key's,
    # so a shadow cast ON the key reads as key. Trusted only where there is
    # enough light to have a colour at all.
    scale = (float(ybg) / np.maximum(y, 1.0))[..., None]
    cr_ = c * scale
    key_rel = ((cr_ @ khat) - 1.25 * np.abs(cr_ @ kperp)) / kn
    trust = np.clip((y - 12.0) / 30.0, 0.0, 1.0)
    keyness = np.maximum(key_abs, np.minimum(key_rel, 1.0) * trust)
    alpha = 1.0 - np.clip((keyness - 0.12) / (0.45 - 0.12), 0.0, 1.0)
    a8 = Image.fromarray((alpha * 255).astype(np.uint8), "L")
    # Salt from grain on the key, and pinholes in the object.
    a8 = a8.filter(ImageFilter.MedianFilter(3))
    if feather:
        a8 = a8.filter(ImageFilter.GaussianBlur(feather))
    alpha = np.asarray(a8).astype(np.float32) / 255.0
    # A thing the model let run off its frame (a lanyard out of the top, a
    # barrel past the side) would end in a straight cut. It fades out instead.
    fade = max(3.0, min(H, W) * 0.05)
    yy = np.minimum(np.arange(H), np.arange(H)[::-1])[:, None]
    xx = np.minimum(np.arange(W), np.arange(W)[::-1])[None, :]
    alpha = alpha * np.clip(np.minimum(yy, xx) / fade, 0.0, 1.0)
    solid = alpha > 0.5
    cover = float(solid.mean())
    if cover < 0.02 or cover > 0.85:
        return None
    ys, xs = np.nonzero(alpha > 0.03)
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
    # Despill the edge band: take the key's hue out of anything near a
    # transparent pixel, keep its brightness.
    edge_src = Image.fromarray(((alpha < 0.98) * 255).astype(np.uint8), "L")
    band = np.asarray(edge_src.filter(ImageFilter.MaxFilter(5))).astype(bool)
    pk = np.maximum(c @ khat, 0.0)
    c2 = c - pk[..., None] * khat[None, None, :] * band[..., None]
    cb, cr = c2[..., 0], c2[..., 1]
    out = np.stack([y + 1.402 * cr,
                    y - 0.344136 * cb - 0.714136 * cr,
                    y + 1.772 * cb], -1)
    out = np.clip(out, 0, 255)
    # ...and the key seen THROUGH the object (clear plastic, glass, a gap in a
    # grille) is taken out everywhere, in the classic colour-safe way: for a
    # magenta key, whatever red AND blue stand above green; for a green key,
    # whatever green stands above both red and blue. A red wax seal or a
    # yellow cap is untouched; a pink cast on a clear reel goes grey.
    r_, g_, b_ = out[..., 0], out[..., 1], out[..., 2]
    if bg[1] > bg[0] + 40 and bg[1] > bg[2] + 40:            # a green key
        spill = np.maximum(g_ - np.maximum(r_, b_), 0.0)
        out[..., 1] = g_ - spill
    elif bg[0] > bg[1] + 40 and bg[2] > bg[1] + 40:          # a magenta key
        spill = np.maximum(np.minimum(r_, b_) - g_, 0.0)
        out[..., 0] = r_ - spill
        out[..., 2] = b_ - spill
    out[alpha <= 0.01] = 0
    rgba = np.dstack([out, alpha * 255]).astype(np.uint8)
    cut = Image.fromarray(rgba, "RGBA")
    # Framed like inventory art: the object centred on a square with air
    # around it, so every plate in the pack sits the same way in its slot.
    w, h = x1 - x0, y1 - y0
    side = int(max(w, h) * 1.14) + 2
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(cut.crop((x0, y0, x1, y1)), ((side - w) // 2, (side - h) // 2))
    return canvas


def _ref_of(cut, bg=(118, 118, 118)):
    """A cut-out flattened onto neutral grey: what an image model is handed as
    'this exact object'. A transparent PNG goes out as RGB, and whatever sits
    under the transparency (black here) would be read as part of the thing."""
    from PIL import Image
    flat = Image.new("RGB", cut.size, bg)
    flat.paste(cut, (0, 0), cut)
    return flat


def _shoot_item_sheet(session_id: str, book: dict) -> Optional[bytes]:
    """One grid of every piece of gear, shot as cut-out item art on a key."""
    its = book.get("items") or []
    if not its:
        return None
    cols, rows = roster_grid(len(its))
    lr = book.get("look_rules") or {}
    key = _key_colour(book)
    book["item_key"] = key
    hexv = KEY_COLOURS[key][0]
    lines = "\n".join(
        f"FRAME {i + 1}: {e.get('name')}"
        + (f" ({e['kind']})" if e.get("kind") else "")
        + f" — {e.get('look') or ''}"
        for i, e in enumerate(its))
    spare = (f"Leave any frame beyond {len(its)} solid black."
             if len(its) < cols * rows else "")
    prompt = f"""{_medium(book)}

This is the PROPS sheet — the gear of this world, shot as cut-out item art, the way a great game shows its loot.
LAYOUT — exact: a {cols} x {rows} grid ({cols} columns, {rows} rows) of {cols * rows} equal 4:3 frames separated by thin solid BLACK gutters on a black sheet. No text, numbers, captions or labels anywhere. {spare}
BACKGROUND — every frame is filled edge to edge with ONE perfectly flat, uniform, solid {key} ({hexv}) chroma-key colour. No floor, no table, no surface, no horizon, no cast shadow, no reflection, no gradient, no vignette, no texture and no grain on the background — nothing in the frame but the object and that flat {key}.
THE OBJECT — ONE object per frame, ALONE and WHOLE, floating in the middle of its frame with flat {key} all the way round it (never touching or cut off by the frame edge), filling about two thirds of the frame, at a three-quarter hero angle. It has been used and carried — scuffed, stained, repaired, real — and it is lit by this world's own light, a hard key from one side in the palette below, never flat studio light. Close enough to read its material and its wear. No hand, no person, no second object, and no {key} anywhere on the object itself. Frames follow the numbered order below exactly, left to right, top to bottom. Every object must look clearly different from the others.
PALETTE {json.dumps(lr.get('palette') or {})}.
{lines}"""
    data = _post(SHEET_MODEL, [{"text": prompt}],
                 {"responseModalities": ["IMAGE"],
                  "imageConfig": {"aspectRatio": "4:3", "imageSize": SHEET_SIZE}},
                 timeout=_cut(book, SHEET_TIMEOUT_S),
                 operation="look_book_item_sheet", service="image")
    return _image_of(data)


def _stage_item_sheet(session_id: str, book: dict, data: Optional[bytes] = None) -> None:
    """Cut the props sheet into one cut-out plate per piece of gear.

    The same path the cast takes: slice row by row (the model does not always
    draw the grid it was asked for), then check the CROPS by eye against the
    list — a plate on the wrong item is worse than no plate — and keep each
    only where it is clearly one whole object. Each kept crop is then keyed
    off its background (_cut_out) into item_NN.png, with a flattened
    item_NN_ref.jpg beside it for image models."""
    d = book_dir(session_id)
    its = book.get("items") or []
    if not its:
        return
    for e in its:
        e.pop("plate", None)
        e.pop("ref", None)
    for pat in ("item_*.jpg", "item_*.png"):
        for stale in d.glob(pat):
            if stale.name == "item_sheet.png":
                continue
            try:
                stale.unlink()
            except Exception:
                pass
    if not data:
        _log(session_id, book, "props sheet: no image — the gear keeps its "
                               "words and loses its pictures", "items")
        return
    (d / "item_sheet.png").write_bytes(data)
    book["item_sheet"] = "item_sheet.png"
    cells = _slice_cells(d / "item_sheet.png")
    _log(session_id, book, f"props sheet: {len(cells)} frames cut", "items")
    if not cells:
        return
    placement = _place_crops(
        d, cells, its, key="name", what="props", check_name="item_check.jpg",
        strictly=("Be strict: a tile showing a person, a room, or two objects "
                  "together is 0, and so is one where the object is cut off."))
    uncut = []
    for i, e in enumerate(its):
        cell = placement.get(i)
        if cell is None:
            continue
        cut = _cut_out(cells[cell])
        if cut is None:
            uncut.append(e.get("name") or f"#{i + 1}")
            continue
        cut.thumbnail((ITEM_PLATE_MAX, ITEM_PLATE_MAX))
        n = f"item_{i + 1:02d}.png"
        cut.save(d / n, optimize=True)
        r = f"item_{i + 1:02d}_ref.jpg"
        try:
            _ref_of(cut).save(d / r, quality=90)
            e["ref"] = r
        except Exception:
            pass
        e["plate"] = n
    if uncut:
        _log(session_id, book, f"props sheet: no clean cut-out for {', '.join(uncut)} — "
                               "shown by name, never on a background", "items")
    _log(session_id, book,
         f"treasures: {sum(1 for e in its if e.get('plate'))}/{len(its)} with pictures "
         f"(cut out on {book.get('item_key') or 'green'})",
         "items")


def gear_ref_for(session_id: str, name: str) -> Optional[str]:
    """The flattened plate of one piece of this world's gear, by name — the
    picture an image model is handed so the thing in the room, the thing in
    the hand and the thing in the pack are the same thing."""
    want = _norm(name)
    if not want:
        return None
    for it in items(session_id):
        if _norm(it.get("name")) == want:
            return it.get("ref") or None
    return None


def _shoot_roster_sheet(session_id: str, book: dict) -> Optional[bytes]:
    looks = book.get("roster_looks") or []
    cols, rows = roster_grid(len(looks))
    lr = book.get("look_rules") or {}
    lines = "\n".join(f"FRAME {i + 1}: {e['kind']} — {e.get('look') or e['kind']}" for i, e in enumerate(looks))
    char = _character_parts()
    prompt = f"""{_medium(book)}

This is the CASTING sheet.
LAYOUT — exact: a {cols} x {rows} grid ({cols} columns, {rows} rows) of {cols * rows} equal 4:3 frames separated by thin solid BLACK gutters on a black sheet. No text, numbers, captions or labels anywhere. {'Leave any frame beyond ' + str(len(looks)) + ' solid black.' if len(looks) < cols * rows else ''}
Each frame is ONE full-body design shot of ONE roster entry, photographed ON LOCATION in this world's own terrain and light — never a studio, never a white, grey or seamless backdrop — three-quarter to camera, the whole silhouette readable head to toe, the ground and sky behind it simple and uncluttered. Frames follow the numbered order below exactly, left to right, top to bottom. Every entry must look clearly different from the others{' and from the protagonist in the attached reference (the protagonist does NOT appear on this sheet)' if char else ''}.
PALETTE {json.dumps(lr.get('palette') or {})}.
{lines}"""
    parts = ([{"text": "PROTAGONIST (for contrast only — do not draw them):"}] + char[1:2] if char else []) + [{"text": prompt}]
    data = _post(SHEET_MODEL, parts,
                 {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "4:3", "imageSize": SHEET_SIZE}},
                 timeout=_cut(book, SHEET_TIMEOUT_S), operation="look_book_roster_sheet", service="image")
    return _image_of(data)


# ── slicing ─────────────────────────────────────────────────────────────────

def _runs(mask) -> list:
    out, s = [], None
    for i, v in enumerate(mask):
        if v and s is None:
            s = i
        if not v and s is not None:
            out.append((s, i))
            s = None
    if s is not None:
        out.append((s, len(mask)))
    return out


def _spans(profile, length: int, n: int) -> list:
    """Content spans along one axis: runs that are not black gutter.

    Falls back to an even split when the sheet does not show exactly n clean
    runs (a gutter the model drew grey, a frame with a black sky touching the
    edge) — a slightly wrong crop is still a usable plate; no plate is not.
    """
    try:
        dark = [v < 14 for v in profile]
        content = [r for r in _runs([not x for x in dark]) if r[1] - r[0] > length * (0.5 / n)]
        if len(content) == n:
            return content
    except Exception:
        pass
    return [(int(length * i / n), int(length * (i + 1) / n)) for i in range(n)]


def _slice(path: Path, rows: int, cols: int) -> list:
    from PIL import Image
    im = Image.open(path).convert("RGB")
    W, H = im.size
    try:
        import numpy as np
        a = np.asarray(im).astype(float).mean(2)
        row_prof, col_prof = list(a.mean(1)), list(a.mean(0))
    except Exception:
        g = im.convert("L")
        px = g.load()
        row_prof = [sum(px[x, y] for x in range(0, W, 8)) / len(range(0, W, 8)) for y in range(H)]
        col_prof = [sum(px[x, y] for y in range(0, H, 8)) / len(range(0, H, 8)) for x in range(W)]
    rs, cs = _spans(row_prof, H, rows), _spans(col_prof, W, cols)
    out = []
    for (y0, y1) in rs:
        for (x0, x1) in cs:
            out.append(im.crop((x0 + 3, y0 + 3, x1 - 3, y1 - 3)))
    return out


def _slice_cells(path: Path) -> list:
    """Every frame on a sheet, in reading order, however many a row holds.

    Rows are found on the horizontal gutters, then each row's frames on ITS
    vertical gutters — a 4 / 5 / 4 sheet is cut as drawn. A small inset keeps
    gutter edges (and the frame numbers the model sometimes prints in the
    corners) out of the plate.
    """
    from PIL import Image
    im = Image.open(path).convert("RGB")
    W, H = im.size
    try:
        import numpy as np
        a = np.asarray(im).astype(float).mean(2)
    except Exception:
        return _slice(path, *_grid_guess(W, H))
    rows = [r for r in _runs(list(a.mean(1) >= 14)) if r[1] - r[0] > H * 0.12]
    if not rows:
        return _slice(path, *_grid_guess(W, H))
    out = []
    for (y0, y1) in rows:
        band = a[y0:y1]
        cols = [c for c in _runs(list(band.mean(0) >= 14)) if c[1] - c[0] > W * 0.08]
        for (x0, x1) in cols:
            # 5%: the sheet model prints frame numbers in the corners against
            # orders, and a number left on a plate can be copied into a scene.
            ix, iy = int((x1 - x0) * 0.05), int((y1 - y0) * 0.05)
            out.append(im.crop((x0 + ix, y0 + iy, x1 - ix, y1 - iy)))
    return out


def _grid_guess(W: int, H: int):
    return (3, 4) if W >= H else (4, 3)


def _save_ref(src: Path, dst: Path, max_side: int) -> None:
    from PIL import Image
    im = Image.open(src).convert("RGB")
    im.thumbnail((max_side, max_side))
    im.save(dst, quality=88)


def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", str(s or "").lower()).strip()


# ── for the editor ──────────────────────────────────────────────────────────

def summary(session_id: str = "default") -> dict:
    """Everything the editor's Look Book view shows: every stage, as data."""
    book = load(session_id)
    key = world_key()
    v = int(book.get("version") or 0)

    shelf = world_slug(session_id)

    def url(n):
        # The World is in the URL: every World's book counts its versions from
        # 1, so plate_01.jpg?v=1 alone would show one World's plate cached as
        # another's.
        return f"/api/look_book/{session_id}/file/{n}?v={v}&w={shelf}" if n else None

    usable = usable_terms(book) if book else {}
    panels = book.get("panels") or []
    frames = []
    for f in book.get("frames") or []:
        if not isinstance(f, dict):
            continue
        try:
            idx = int(f.get("n")) - 1
        except Exception:
            idx = -1
        frames.append(dict(f, panel=url(panels[idx]) if 0 <= idx < len(panels) else None))
    return {
        "session": session_id,
        "enabled": enabled(),
        "switches": {"sheet_on_turns": LOOK_BOOK_SHEET_ON_TURNS, "roster_plates": LOOK_BOOK_ROSTER_PLATES,
                     "story": LOOK_BOOK_STORY},
        "status": book.get("status") or "none",
        "world": shelf,
        # Seconds since this book was started, by the server's clock: the
        # editor's line counts from here, so it is right after switching
        # Worlds mid-build too.
        "elapsed": round(time.time() - float(book.get("started") or time.time()), 1),
        "world_name": book.get("world_name") or "",
        "stale": bool(book) and book.get("world_key") != key,
        "building": _BUILDING.get(session_id) == key,
        "reason": book.get("reason"),
        "timings": book.get("timings") or {},
        "log": book.get("log") or [],
        "world_sheet": url(book.get("world_sheet")),
        "roster_sheet": url(book.get("roster_sheet")),
        "placement_check": url(book.get("placement_check")),
        "look_rules": book.get("look_rules") or {},
        "frames": frames,
        "roster": [dict(e, index=i, plate=url(e.get("plate")), usable_terms=usable.get(e.get("kind") or "", []))
                   for i, e in enumerate(book.get("roster_looks") or [])],
        "roster_only": [k for k in (book.get("roster") or [])] if not book.get("roster_looks") else [],
        "error": book.get("error"),
    }


def file_path(session_id: str, name: str, slug: Optional[str] = None) -> Optional[Path]:
    if not re.fullmatch(r"[a-z_]+(?:_\d{2})?\.(?:png|jpg)", name or ""):
        return None
    if slug is not None and (not slug or _safe_slug(slug) != slug):
        return None
    p = book_dir(session_id, slug) / name
    return p if p.exists() else None
