"""
scene_audio.py — what plays under the picture: scene music, the place's
ambience, the sound of an action, the sound of what it did, encounter hits.

Takes what the game already knows about a moment — vision's read of the frame
that rendered, the choice text, the consequence caption, the mode (scene /
conversation / encounter / camp), the run's phase and its world — and answers
with files to play:

  • a music bed           sound_library lane "music"
  • a looping ambience    "ambience"
  • action foley          "foley"
  • a consequence bed     "consequence"
  • encounter stingers    "stinger"

NOTHING IS GENERATED. All of these used to be made per scene, per action and
per turn by ElevenLabs — a second account, a second bill, 38% of a measured
run's cost. The game now runs on the player's ONE key (Gemini, or OpenAI via
provider_bridge; docs/plans/ONE_KEY_AUDIO_PLAN.md) and neither makes sound
effects, so for a day every lane here was silent. Matt's call (2026-09-25):
*"precache them FROM my 11 labs and ship precached whatever you need."* The
library was made once on 5th Corner's account (tools/build_sound_library.py)
and ships in static/audio/library/; ``sound_library.pick`` chooses a clip; this
module is the wiring from the endpoints the client already calls
(/api/scene_audio, /api/action_foley, /api/consequence_audio, /api/music/*) to
that choice, in the shapes the client already reads. A clip is always on disk,
so every answer is ``cached: true, pending: false`` and the client's retry
loops simply never fire.

What a person can still bring: a locked loop and a title-screen loop (uploaded
in the editor, or a library track locked from a direction), designer one-shots
in static/audio/encounter/, and the /audio/<file> serving helpers for those.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import sound_library

ROOT = Path(__file__).parent.resolve()
import paths as _paths  # where the game writes (M2): the repo from source, %APPDATA%/ABYSS built

MISSING_REASON = "the sound library is missing (static/audio/library)"

# The one encounter stinger /api/scene_audio hands back with an encounter bed.
_ENCOUNTER_STINGER = "encounter_enter"


# ────────────────────────────────────────────────────────────────────────────
# Availability
# ────────────────────────────────────────────────────────────────────────────

def unavailable_reason() -> str | None:
    """None while the shipped library is there — which is always, in a build
    and in the repo. The string is shown to the player and the editor
    (/api/health's music block), so it names the one thing that can be wrong."""
    return None if sound_library.available() else MISSING_REASON


def is_available() -> bool:
    """True when the library can answer every lane.

    Not a key question any more: no key makes sound, and none is needed. Mock
    mode plays it too — the library never touches the network, so the reason
    mock mode had to be kept away from the generator (a "fully offline" run
    once billed music on every scene) no longer applies.
    """
    return unavailable_reason() is None


def _entry_url(entry: dict | None) -> str | None:
    """The library file's URL, or None when the file itself is gone."""
    if not entry:
        return None
    try:
        if not sound_library.file_path(entry).is_file():
            return None
    except OSError:
        return None
    return sound_library.url(entry)


# ────────────────────────────────────────────────────────────────────────────
# What the run knows: phase, world, and what the frame shows
# ────────────────────────────────────────────────────────────────────────────

def _run_context(session_id: str = "default") -> dict:
    """{phase, world, vision} for a session, read-only.

    The client asks for a bed with the scene text alone; the phase (does the
    music escalate?), the world (desert, neon, riot?) and vision's read of the
    frame (where is this fight?) are the server's to know.
    """
    out = {"phase": "normal", "world": "", "vision": ""}
    sid = str(session_id or "default")
    if sid == "legacy":
        return out
    try:
        import engine
        # Not engine._load_state: its path helper creates the session folder,
        # and asking what a session sounds like must not make one.
        path = Path(engine._get_session_root(sid)) / "state.json"
        if not path.is_file():
            return out
        st = json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return out
    out["phase"] = str(st.get("current_phase") or "normal")
    out["world"] = str(st.get("world_prompt") or "")
    out["vision"] = str(st.get("current_vision") or "")
    return out


def _clean_scene_text(scene_prompt: str) -> str:
    """One line, capped. The picker reads the whole description — the old
    last-240-characters rule existed for a generator's prompt budget, and a
    word match does better with the place line AND the shot."""
    if not scene_prompt:
        return ""
    return " ".join(str(scene_prompt).split())[:4000]


# Scene ambience remembers what it chose per session, so a corridor vision
# describes in new words every turn keeps one bed (sound_library.PREFER_RATIO).
_LAST_BED: dict[str, str] = {}
_LAST_BED_LOCK = threading.Lock()


def scene_ambience_enabled() -> bool:
    """On: the whole ambience library, matched to the scene. Off: only the
    seven generic beds (rain, cave, wind, room, urban, forest, industrial) —
    the keyword-matched stock bed every scene used to end up on."""
    try:
        import engine
        return bool(getattr(engine, "SCENE_AMBIENCE_ENABLED", True))
    except Exception:
        return True


def _pick_music(text: str, mode: str, ctx: dict, session_id: str) -> dict | None:
    direction = get_music_direction()
    phase = ctx.get("phase") or "normal"
    # The world's own brief, the scene, and the author's words: "neon
    # synthwave, rain on chrome" in the editor makes a desert World's score the
    # cyber one, which is what writing it there means.
    flavor = sound_library.flavor_of(" ".join((ctx.get("world") or "", text, direction)))
    # The authored direction counts double: it is the author saying what the
    # world should sound like ("slow detuned piano"), not a description of it.
    query = " ".join(p for p in (text, direction, direction) if p)
    return sound_library.pick(
        "music", query, mode=mode, phase=phase, flavor=flavor,
        seed=f"{session_id}|{mode}|{phase}|{flavor}")


def _pick_ambience(text: str, mode: str, ctx: dict, session_id: str) -> dict | None:
    sid = str(session_id or "default")
    direction = get_sfx_direction()
    context = [(ctx.get("vision") or "", 0.35), (direction, 1.0),
               (ctx.get("world") or "", 0.15)]
    with _LAST_BED_LOCK:
        prefer = _LAST_BED.get(sid) if mode == "scene" else None
    entry = sound_library.pick(
        "ambience", text, seed=sid, context=context, prefer=prefer,
        generic_only=not scene_ambience_enabled())
    if entry and mode == "scene":
        with _LAST_BED_LOCK:
            _LAST_BED[sid] = str(entry.get("id"))
    return entry


def get_scene_audio(scene_prompt: str, session_id: str = "default",
                    mode: str = "scene") -> dict | None:
    """Music + ambience URLs for a scene; for an encounter, the stingers too.

    ``mode="conversation"`` picks an intimate bed, ``"encounter"`` a stance-
    coloured confrontation bed (the client sends "stance — kind — label —
    danger — stakes") plus the stinger catalog. A scene round a campfire is
    scored as camp. A locked loop replaces the music everywhere but encounters.
    None only for an empty scene with no loop, or a missing library.
    """
    mode = (mode or "scene").strip().lower()
    if mode not in ("scene", "conversation", "encounter"):
        mode = "scene"
    scene = _clean_scene_text(scene_prompt)
    loop = None if mode == "encounter" else custom_loop()
    if not loop and not scene and mode != "encounter":
        return None
    ctx = _run_context(session_id)
    place = scene or "an unknown place"

    music = None
    if loop:
        music_url = loop["url"]
    else:
        music = _pick_music(place, sound_library.infer_mode(place, mode), ctx, session_id)
        music_url = _entry_url(music)
    bed = _pick_ambience(place, mode, ctx, session_id)
    sfx_url = _entry_url(bed)
    stinger_url = stock_stinger_url(_ENCOUNTER_STINGER) if mode == "encounter" else None
    stingers = encounter_stinger_urls() if mode == "encounter" else None

    if not music_url and not sfx_url and not stinger_url:
        return None
    result = {
        "audio_url": music_url,
        "sfx_url": sfx_url,
        "stinger_url": stinger_url,
        "cached": True,
        "pending_music": False,
        "pending_sfx": False,
        "mode": mode,
        "music_id": (music or {}).get("id"),
        "sfx_id": (bed or {}).get("id"),
        "phase": ctx.get("phase"),
    }
    if not music_url:
        why = unavailable_reason()
        if why:
            result["reason"] = why
    if stingers:
        result["stingers"] = stingers
    if loop:
        result["source"] = loop.get("source") or "custom"
    return result


# ────────────────────────────────────────────────────────────────────────────
# Action foley — the sound of the thing you just did
#
# Everything else here is a BED under the place. This is the one sound tied to
# the PLAYER: pressing a choice used to make no noise beyond a UI blip — you
# acted and the world did not answer. The source is the choice TEXT ("Kick open
# the rusted door" -> a door kicked in), never the render prompt: an earlier
# attempt fed the camera rig and film stock in and got mush. The frame's vision
# read only chooses between clips the words already chose (gravel or catwalk
# under a sprint). The client still prewarms the slate and plays on the click;
# with every clip on disk that is simply a cache it never has to wait for.
# ────────────────────────────────────────────────────────────────────────────

_FOLEY_NUM_RE = re.compile(r"^\s*\d+\s*[.)\-:]?\s*")


def _clean_action(action: str) -> str:
    """The verb, without the slate's numbering or trailing punctuation."""
    text = " ".join(str(action or "").split())
    text = _FOLEY_NUM_RE.sub("", text)
    return text.strip().rstrip(".!?").strip()


def action_foley_enabled() -> bool:
    try:
        import engine
        return bool(getattr(engine, "ACTION_FOLEY_ENABLED", True))
    except Exception:
        return True


def action_foley(action: str, session_id: str = "default") -> dict | None:
    """The sound of one action: {url, id, cached, pending} or None.

    The seed is the action's own words, so the same line always sounds the
    same and two different doors can land on two different door clips.
    """
    if not action_foley_enabled() or not is_available():
        return None
    act = _clean_action(action)
    if len(act) < 2:
        return None
    ctx = _run_context(session_id)
    entry = sound_library.pick("foley", act, seed=act, context=ctx.get("vision") or "")
    url = _entry_url(entry)
    if not url:
        return None
    return {"url": url, "id": entry.get("id"), "cached": True, "pending": False}


# ────────────────────────────────────────────────────────────────────────────
# Consequence bed — the sound of what the choice DID
#
# Foley answers the CLICK. Then the turn renders for half a minute and the
# flipbook plays the outcome out — the most motion this game puts on screen,
# and it had only the room tone under it. This is that beat's own sound,
# chosen from the turn's visual caption (the client sends meta.visual, or the
# prose when there is none): 6-12 seconds, by what happened — a door breached,
# a collapse, gunfire, a discovery, dread when nothing names itself.
#
# It is deliberately NOT a loop. It was one once, holding until the next
# action, and a distinctive gesture repeating under a player who is reading is
# exactly what stops it reading as the world answering. It plays through and
# stops; the scene's ambience underneath is the lane built to loop.
# ────────────────────────────────────────────────────────────────────────────

def _clean_consequence(text: str) -> str:
    out = " ".join(str(text or "").split())
    return _FOLEY_NUM_RE.sub("", out).strip()


def consequence_bed_enabled() -> bool:
    try:
        import engine
        return bool(getattr(engine, "CONSEQUENCE_BED_ENABLED", True))
    except Exception:
        return True


def consequence_bed(text: str, session_id: str = "default") -> dict | None:
    """One turn's outcome, as a single pass: {url, id, cached, pending} or None."""
    if not consequence_bed_enabled() or not is_available():
        return None
    what = _clean_consequence(text)
    # A consequence is prose. Anything this short is a fragment or an error
    # string, and there is nothing in it to choose a sound by.
    if len(what) < 12:
        return None
    ctx = _run_context(session_id)
    entry = sound_library.pick("consequence", what, seed=what,
                               context=ctx.get("vision") or "")
    url = _entry_url(entry)
    if not url:
        return None
    return {"url": url, "id": entry.get("id"), "cached": True, "pending": False}


# ────────────────────────────────────────────────────────────────────────────
# Paths
# ────────────────────────────────────────────────────────────────────────────

def _session_audio_dir(session_id: str = "default", *, create: bool = True) -> Path:
    """Per-session audio dir (clips generated before the library, still served)."""
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


# ────────────────────────────────────────────────────────────────────────────
# THE CHOSEN LOOP
# ────────────────────────────────────────────────────────────────────────────

MUSIC_DIR = _paths.data_root() / "assets" / "music"
_LOOP_META = MUSIC_DIR / "loop.json"
_DIRECTION_PATH = MUSIC_DIR / "direction.json"
_SFX_DIRECTION_PATH = MUSIC_DIR / "sfx_direction.json"
_MENU_META = MUSIC_DIR / "menu.json"
_MENU_DIRECTION_PATH = MUSIC_DIR / "menu_direction.json"
LOOP_EXTS = {"wav": "audio/wav", "mp3": "audio/mpeg", "ogg": "audio/ogg",
             "m4a": "audio/mp4", "mp4": "audio/mp4", "webm": "audio/webm"}
MAX_LOOP_BYTES = 12 * 1024 * 1024
# A lock that was DERIVED from the direction — once generated, now chosen from
# the library by its words — is stale the moment the direction changes.
_DERIVED_SOURCES = ("generated", "library")


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
    """Authored 'how this place sounds', or empty. Steers the ambience pick."""
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
    """The authored 'how this world sounds' line, or empty. Steers the music
    pick; world_regen reads and writes it too."""
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
    """A sample left on disk from before the library, if any."""
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
    if loop and loop.get("source") in _DERIVED_SOURCES:
        clear_custom_loop()
    return text


def _library_music_for(prompt: str, stem: str) -> dict | None:
    """The library track closest to an author's words. The title screen's
    stem asks the menu mode; anything else is read as a scene."""
    mode = "menu" if stem in ("menu", "menu_preview") else "scene"
    return sound_library.pick("music", prompt or "", mode=mode,
                              flavor=sound_library.flavor_of(prompt or ""),
                              seed=f"direction|{prompt}")


def library_preview(prompt: str, stem: str = "preview") -> dict | None:
    """Hear what the library would play for these words, without locking it.
    Writes nothing: the preview is the library file itself."""
    prompt = (prompt or "").strip()
    if not prompt or not is_available():
        return None
    entry = _library_music_for(prompt, "menu_preview" if stem == "menu_preview" else "preview")
    url = _entry_url(entry)
    if not url:
        return None
    return {"url": url, "file": entry.get("file"), "id": entry.get("id"),
            "prompt": prompt[:400], "seconds": entry.get("seconds"),
            "source": "library"}


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


def library_loop(prompt: str, stem: str = "loop") -> dict | None:
    """Lock the library track closest to these words as the only track.

    Copied into the loop slot (not pointed at) so a lock survives a library
    that later renames or drops the file, and so every path that already
    knows how to serve and clear a lock keeps working unchanged.
    """
    prompt = (prompt or "").strip()
    if not prompt or not is_available():
        return None
    entry = _library_music_for(prompt, stem)
    if not entry:
        return None
    path = sound_library.file_path(entry)
    if not path.is_file():
        return None
    return _write_loop(path.read_bytes(), path.suffix.lstrip(".") or "mp3",
                       "library", prompt=prompt, name=str(entry.get("id") or ""),
                       stem=stem)


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
    if loop and loop.get("source") in _DERIVED_SOURCES:
        clear_menu_loop()
    return text


def menu_loop() -> dict | None:
    """The title-screen track somebody LOCKED, or None."""
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


def menu_library() -> dict | None:
    """The shipped title theme: what the start menu plays when nothing is
    locked. A deliberate track, not an audition — the menu used to fall back to
    a ten-second editor sample and loop it forever, which is why only a LOCKED
    track may play there; the library's menu tracks were written as title
    themes. The menu direction, when there is one, chooses between them."""
    entry = sound_library.pick("music", get_menu_direction(), mode="menu",
                               seed="menu")
    url = _entry_url(entry)
    if not url:
        return None
    return {"url": url, "id": entry.get("id"), "source": "library",
            "file": entry.get("file")}


def clear_menu_loop() -> None:
    try:
        for old in MUSIC_DIR.glob("menu.*"):
            old.unlink()
    except OSError:
        pass


# ────────────────────────────────────────────────────────────────────────────
# Stingers and the generic beds — the old "stock" catalog, now library entries
# ────────────────────────────────────────────────────────────────────────────

def stock_stinger_url(kind: str = _ENCOUNTER_STINGER) -> str | None:
    return _entry_url(sound_library.get(kind)) if kind else None


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
    """Every encounter one-shot keyed by its id (encounter_hitch, …) — the
    ids the client's Sound.STOCK_CUE already maps. Designer WAVs still win
    on the client; the built-in synth is the last resort."""
    out = {}
    for entry in sound_library.entries("stinger"):
        url = _entry_url(entry)
        if url:
            out[str(entry["id"])] = url
    return out


def stock_ambience_url(kind: str) -> str | None:
    """One of the seven generic beds by its old name (rain, cave, …)."""
    return _entry_url(sound_library.get(f"amb_{kind}"))


def stock_status() -> dict:
    """The stingers and the generic beds, keyed as the editor's Stock box and
    the client's prefetch have always read them."""
    out = {}
    for entry in sound_library.entries("stinger"):
        url = _entry_url(entry)
        out[str(entry["id"])] = {
            "file": entry.get("file"), "ready": bool(url), "url": url,
            "prompt": entry.get("prompt") or "", "seconds": entry.get("seconds"),
            "loop": False, "kind": "stinger",
        }
    for entry in sound_library.entries("ambience"):
        if not entry.get("generic"):
            continue
        key = str(entry["id"]).removeprefix("amb_")
        url = _entry_url(entry)
        out[key] = {
            "file": entry.get("file"), "ready": bool(url), "url": url,
            "prompt": entry.get("prompt") or "", "seconds": entry.get("seconds"),
            "loop": True, "kind": "ambience",
        }
    return out


def stock_record(key: str) -> dict | None:
    """One stock row as {id, kind, url, cached, prompt, file}, or None."""
    rec = stock_status().get(key)
    if not rec:
        return None
    return {"id": key, "kind": rec["kind"], "url": rec["url"],
            "cached": bool(rec["url"]), "prompt": rec["prompt"],
            "file": rec["file"]}


# ────────────────────────────────────────────────────────────────────────────
# Serving what is not under static/
# ────────────────────────────────────────────────────────────────────────────

def resolve_audio_path(filename: str, session_id: str = "default") -> Path | None:
    """Resolve a served '/audio/<filename>' back to disk (path-traversal safe).

    The library is served from /static/ and never comes through here. This is
    for the locked loops, the editor's old previews, and clips an earlier
    build generated into a session's audio/ dir.
    """
    # A bare file name or nothing: no separators, no parent hops.
    safe = Path(str(filename or "")).name
    if not safe or safe.endswith(".part") or safe != str(filename):
        return None
    for sid in (session_id, "default"):
        if not sid:
            continue
        candidate = _session_audio_dir(sid, create=False) / safe
        if candidate.exists():
            return candidate
    loop = MUSIC_DIR / safe
    if loop.exists() and (
        safe.startswith("loop.") or safe.startswith("preview.")
        or safe.startswith("menu") or safe.startswith("test_")
    ):
        return loop
    return _find_audio_in_any_session(safe)


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


# ────────────────────────────────────────────────────────────────────────────
# The editor's readouts
# ────────────────────────────────────────────────────────────────────────────

def inspect_scene(scene_prompt: str, mode: str = "scene",
                  session_id: str = "default") -> dict:
    """What a scene would play and why — the picks, the words that chose them,
    and the prompt each clip was made from. No side effects."""
    mode = (mode or "scene").strip().lower()
    if mode not in ("scene", "conversation", "encounter"):
        mode = "scene"
    scene = _clean_scene_text(scene_prompt)
    place = scene or "an unknown place"
    ctx = _run_context(session_id)
    music = _pick_music(place, sound_library.infer_mode(place, mode), ctx, session_id)
    bed = sound_library.pick(
        "ambience", place, seed=str(session_id or "default"),
        context=[(ctx.get("vision") or "", 0.35), (get_sfx_direction(), 1.0)],
        generic_only=not scene_ambience_enabled())
    stinger_id = _ENCOUNTER_STINGER if mode == "encounter" else None
    return {
        "mode": mode,
        "scene": scene,
        "phase": ctx.get("phase"),
        "direction": get_music_direction(),
        "sfx_direction": get_sfx_direction(),
        "music_id": (music or {}).get("id"),
        "music_prompt": (music or {}).get("prompt") or "",
        "music_url": _entry_url(music),
        "music_matched": sound_library.matched_tags(music, place) if music else [],
        "ambience_id": (bed or {}).get("id"),
        "ambience_kind": (bed or {}).get("id"),
        "sfx_prompt": (bed or {}).get("prompt") or "",
        "sfx_url": _entry_url(bed),
        "ambience_matched": sound_library.matched_tags(bed, place) if bed else [],
        "stinger_id": stinger_id,
        "stinger_url": stock_stinger_url(stinger_id) if stinger_id else None,
        "can_generate": is_available(),
    }


def library_test_clip(scene_prompt: str, mode: str = "scene",
                      layer: str = "music", session_id: str = "default") -> dict | None:
    """The clip a scene would play on one layer, for the editor to audition."""
    layer = (layer or "music").strip().lower()
    mode = (mode or "scene").strip().lower()
    if mode not in ("scene", "conversation", "encounter"):
        mode = "scene"
    if layer == "stinger":
        entry = sound_library.get(_ENCOUNTER_STINGER)
    elif layer == "sfx":
        entry = sound_library.pick("ambience", _clean_scene_text(scene_prompt),
                                   seed=str(session_id or "default"),
                                   context=get_sfx_direction())
    else:
        place = _clean_scene_text(scene_prompt) or "an unknown place"
        entry = _pick_music(place, sound_library.infer_mode(place, mode),
                            _run_context(session_id), session_id)
    url = _entry_url(entry)
    if not url:
        return None
    return {
        "url": url,
        "file": entry.get("file"),
        "id": entry.get("id"),
        "prompt": entry.get("prompt") or "",
        "layer": layer,
        "mode": mode,
        "seconds": entry.get("seconds"),
    }


def list_generated_cache(session_id: str = "default") -> list[dict]:
    """Clips an earlier build generated into this session, still on disk."""
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
                              ("enc_", "encounter"), ("amb_", "ambience"),
                              ("foley_", "foley"), ("beat_", "consequence")):
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
    """Delete those old per-session clips. The library and locked loops stay."""
    removed = 0
    try:
        audio_dir = _get_audio_dir(session_id)
    except Exception:
        return 0
    for path in list(audio_dir.glob("*")):
        if not path.is_file():
            continue
        if path.name.endswith(".part") or path.name.startswith(
                ("scene_", "convo_", "enc_", "amb_", "foley_", "beat_")):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed
