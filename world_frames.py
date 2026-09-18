"""
world_frames.py — per-World first-frame cache. This is the app's load time.

Each World (a prompt snapshot in ``worlds/<slug>.json``) gets a sidecar still:

    worlds/<slug>.frame.png
    worlds/<slug>.frame.json   {fingerprint, drawn, updated, source, prompt}

The fingerprint is a hash of the prompt keys that actually change the opening
shot, taken from the World snapshot. When it matches, the editor reuses the
still. When it doesn't (identity saved in the editor, persist, a prompt that
steers the camera), we regenerate in the background and swap the file
atomically.

``drawn`` is a second hash: the prompts that actually produced the picture,
which is not always the snapshot's. A run reads the LIVE prompt file, and the
snapshot only moves when somebody saves the level — so editing the
protagonist changed who the run was about while leaving the cached frame
stamped clean. Play opened on a picture of the previous hero and cut to the
new one on turn two. Play now checks ``drawn`` against the live prompts and
renders its own opening rather than starting on a stranger.

Mock / no-keys: copy an authored level plate, else a tiny mint placeholder.
Never fails the graph, never bills, never touches a play session's images/.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import struct
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

import worlds_store

log = logging.getLogger(__name__)

# Keys whose text actually reaches the opening still. Everything else (choice
# copy, camp, narrator) can change without invalidating the first frame.
FINGERPRINT_KEYS = (
    "setting_reference",
    "player_character",
    "camera_perspective",
    "gemini_text_to_image_instructions",
    "gemini_image_to_image_instructions",
    "image_art_direction",
    "image_camera_rules",
    "image_negative_prompt",
    "world_initial_state",
)

_DEBOUNCE_S = 2.8
_GEN_SESSION_PREFIX = "wf-"
# Happy Oyster's world prompt cap is 2000 chars; leave room for the camera prefix.
STEER_PROMPT_MAX = 1800
DEFAULT_FRAME_VISION = (
    "Wide establishing shot of the authored location at the start of play. "
    "Cinematic, grounded, no text overlays."
)


def clip_steer_prompt(text: str) -> str:
    """One line, under the world-model prompt cap."""
    t = " ".join(str(text or "").split())
    if len(t) <= STEER_PROMPT_MAX:
        return t
    return t[: STEER_PROMPT_MAX - 3].rstrip() + "..."


def scene_prompt_for(prompts: Optional[Dict[str, Any]] = None, vision: str = "") -> str:
    """The text that described the still — what a world model should animate."""
    text = clip_steer_prompt(vision)
    if text:
        return text
    try:
        import game_identity
        shot = game_identity.opening_shot(game_identity.spec_from_prompts(prompts or {}))
        if shot and shot.get("vision"):
            return clip_steer_prompt(shot["vision"])
    except Exception:
        pass
    return DEFAULT_FRAME_VISION


# The plate used to be described from the Cast & Camera sheet alone. A level
# authored entirely in the bible — Level and Character toggles off, nine
# thousand words in ``world_initial_state`` — therefore fell all the way
# through ``opening_shot`` to DEFAULT_FRAME_VISION, and the cached frame was a
# stock establishing wide of nowhere. The opening montage reads that same bible
# (see cutscene.mystery_shotlist), so the run showed four photographs of the
# authored place and then cut to a stranger: the teleport at the top of every
# run. This asks the bible the one question the plate needs answered.
OPENING_VISION_INSTRUCTIONS = (
    "Below is the bible for a game level. Describe THE SINGLE FIRST FRAME the "
    "player sees when the level begins: the establishing view of the place they "
    "are standing in.\n"
    "Describe only what the camera sees — architecture, ground, light, weather, "
    "materials, the landmarks that make this place itself and nowhere else. "
    "Name nothing that is not visible. No story, no history, no backstory, no "
    "names, no interpretation, no camera or lens direction, no film-stock or "
    "grain notes: those are set elsewhere in the prompt.\n"
    "Do NOT put a person, a figure, a body part or a crowd in it.\n"
    "Answer with at most 60 words of plain prose. No preamble, no heading."
)
# Bible text is long and this is asked on every regeneration of every World in
# the carousel. One answer per distinct bible, in-process.
_OPENING_VISION_CACHE: Dict[str, str] = {}
OPENING_VISION_MIN_BIBLE = 400


def lore_opening_vision(prompts: Optional[Dict[str, Any]] = None) -> str:
    """One establishing description of this World, read from its own bible.

    Returns "" when there is no bible worth reading or the model is unavailable,
    in which case the caller keeps the sheet's description (or the generic
    fallback) exactly as before — a plate is never worth failing a render over.
    """
    bible = str((prompts or {}).get("world_initial_state") or "").strip()
    if len(bible) < OPENING_VISION_MIN_BIBLE:
        return ""
    key = hashlib.sha256(bible.encode("utf-8", "replace")).hexdigest()
    if key in _OPENING_VISION_CACHE:
        return _OPENING_VISION_CACHE[key]
    try:
        import engine
        place = ""
        try:
            import game_identity
            place = game_identity.place_summary(
                game_identity.spec_from_prompts(prompts or {})) or ""
        except Exception:
            place = ""
        answer = engine._ask(
            f"{OPENING_VISION_INSTRUCTIONS}\n\n"
            f"LEVEL BIBLE:\n{bible[:6000]}\n\n"
            + (f"THE PLACE THIS OPENS IN: {place}\n" if place else ""),
            temp=0.4, tokens=160, use_lore=False,
        )
    except Exception:
        log.warning("[WORLD FRAMES] opening vision ask failed", exc_info=True)
        return ""
    text = " ".join(str(answer or "").split())
    if not text:
        return ""
    _OPENING_VISION_CACHE[key] = text
    log.info("[WORLD FRAMES] plate described from the bible: %s", text[:110])
    return text


def _meta_prompt(slug: str, prompts: Optional[Dict[str, Any]] = None,
                 prompt: str = "") -> str:
    text = clip_steer_prompt(prompt)
    if text:
        return text
    existing = str(_read_meta(slug).get("prompt") or "").strip()
    if existing:
        return clip_steer_prompt(existing)
    if prompts is None:
        try:
            data = worlds_store.get_world(slug)
            prompts = data.get("prompts") or {}
        except Exception:
            prompts = {}
    return scene_prompt_for(prompts)

_lock = threading.Lock()
_timers: Dict[str, threading.Timer] = {}
_generating: Dict[str, str] = {}  # slug -> fingerprint in flight
_last_all_kick = 0.0


def _worlds_dir() -> Path:
    return worlds_store.WORLDS_DIR


def _safe_slug(slug: str) -> str:
    return worlds_store._slug(slug or "")


def frame_path(slug: str) -> Path:
    return _worlds_dir() / f"{_safe_slug(slug)}.frame.png"


def meta_path(slug: str) -> Path:
    return _worlds_dir() / f"{_safe_slug(slug)}.frame.json"


def url_for(slug: str, fingerprint: str = "", updated: Any = None) -> str:
    slug = _safe_slug(slug)
    url = f"/api/worlds/{slug}/frame"
    bits = []
    tag = (fingerprint or "")[:12]
    if tag:
        bits.append(f"v={tag}")
    try:
        stamp = float(updated or 0)
    except (TypeError, ValueError):
        stamp = 0.0
    if stamp:
        bits.append(f"t={int(stamp)}")
    if bits:
        url += "?" + "&".join(bits)
    return url


def fingerprint(prompts: Optional[Dict[str, Any]]) -> str:
    payload = {k: (prompts or {}).get(k) for k in FINGERPRINT_KEYS}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def fingerprint_for_slug(slug: str) -> str:
    slug = _safe_slug(slug)
    if not slug:
        return fingerprint({})
    try:
        data = worlds_store.get_world(slug)
    except KeyError:
        return fingerprint({})
    return fingerprint(data.get("prompts") or {})


def live_fingerprint() -> str:
    """The hash of the prompts a run will actually be played on.

    Not the same thing as the World snapshot's hash. ``worlds/<slug>.json``
    is a saved copy that only moves when somebody saves the level, while a
    run reads the live prompt file — so editing the protagonist in the editor
    changes who the run is about without changing the snapshot at all.
    """
    try:
        from prompts_store import PROMPTS
        return fingerprint(PROMPTS)
    except Exception:
        return ""


def drawn_from_live(rec: Optional[Dict[str, Any]]) -> bool:
    """Is this frame a picture of the character the run is about to play?

    Frames record the prompts that actually drew them. A frame with no such
    record predates this and could be anybody, so it does not count — the
    run renders its own opening rather than starting on a stranger.
    """
    drawn = str((rec or {}).get("drawn") or "")
    live = live_fingerprint()
    return bool(drawn and live and drawn == live)


def _read_meta(slug: str) -> Dict[str, Any]:
    path = meta_path(slug)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_meta(slug: str, payload: Dict[str, Any]) -> None:
    _worlds_dir().mkdir(parents=True, exist_ok=True)
    tmp = meta_path(slug).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(meta_path(slug))


def record(slug: str, prompts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Live status for one World. Safe to call from any request."""
    slug = _safe_slug(slug)
    empty = {
        "slug": slug,
        "url": "",
        "path": "",
        "status": "missing",
        "dirty": True,
        "generating": False,
        "fingerprint": "",
        "drawn": "",
        "source": "",
        "prompt": "",
    }
    if not slug:
        return empty
    want = fingerprint(prompts) if prompts is not None else fingerprint_for_slug(slug)
    png = frame_path(slug)
    meta = _read_meta(slug)
    have = str(meta.get("fingerprint") or "")
    exists = png.is_file() and png.stat().st_size > 32
    generating = _generating.get(slug) == want or (
        slug in _generating and bool(_generating.get(slug))
    )
    if exists:
        dirty = (not have) or have != want
        # A mint placeholder stamped with the current hash used to look
        # "ready", so ensure() never replaced it. That is how the
        # Experience carousel went empty after a mock/no-key pass.
        if not dirty and str(meta.get("source") or "") in ("placeholder", "plate") and _images_enabled():
            dirty = True
        if dirty:
            status = "generating" if generating else "dirty"
        else:
            status = "ready"
        return {
            "slug": slug,
            "url": url_for(slug, have or want, meta.get("updated")),
            "path": str(png.resolve()),
            "status": status,
            "dirty": dirty,
            "generating": generating and dirty,
            "fingerprint": have or want,
            "drawn": str(meta.get("drawn") or ""),
            "source": str(meta.get("source") or ""),
            "prompt": str(meta.get("prompt") or ""),
        }
    status = "generating" if generating else "missing"
    return {
        "slug": slug,
        "url": "",
        "path": "",
        "status": status,
        "dirty": True,
        "generating": generating,
        "fingerprint": want,
        "drawn": "",
        "source": "",
        "prompt": str(meta.get("prompt") or ""),
    }


def annotate_experience(exp: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Stamp frame_url / frame_status onto each World. Does not persist."""
    if not isinstance(exp, dict):
        return exp or {}
    for world in exp.get("worlds") or []:
        if not isinstance(world, dict):
            continue
        rec = record(world.get("slug") or "")
        world["frame_url"] = rec.get("url") or ""
        world["frame_status"] = rec.get("status") or "missing"
        world["frame_generating"] = bool(rec.get("generating"))
        world["frame_source"] = rec.get("source") or ""
        world["frame_prompt"] = rec.get("prompt") or ""
    return exp


def start_world_slug(exp: Optional[Dict[str, Any]] = None) -> str:
    try:
        import experience_store
    except Exception:
        return ""
    if exp is None:
        try:
            exp = experience_store.get_experience()
        except Exception:
            return ""
    if not isinstance(exp, dict):
        return ""
    start = None
    try:
        start = experience_store.landing_world(exp)
    except Exception:
        start = experience_store.world_by_id(exp, exp.get("start_world") or "")
    if start is None and (exp.get("worlds") or []):
        start = exp["worlds"][0]
    return str((start or {}).get("slug") or "")


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def _placeholder_png(width: int = 64, height: int = 64) -> bytes:
    """A dark mint still so empty Worlds still read as a place, not a hole."""
    rows = []
    for y in range(height):
        row = bytearray(1 + width * 4)
        row[0] = 0
        for x in range(width):
            # Soft vignette, mint in the middle.
            dx = (x / max(width - 1, 1)) - 0.5
            dy = (y / max(height - 1, 1)) - 0.5
            fall = min(1.0, (dx * dx + dy * dy) * 2.4)
            r = int(8 + 18 * (1.0 - fall))
            g = int(28 + 90 * (1.0 - fall))
            b = int(22 + 36 * (1.0 - fall))
            i = 1 + x * 4
            row[i:i + 4] = bytes((r, g, b, 255))
        rows.append(bytes(row))
    raw = b"".join(rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


def _install_bytes(slug: str, data: bytes, fp: str, source: str,
                   prompt: str = "", drawn: str = "") -> Dict[str, Any]:
    slug = _safe_slug(slug)
    _worlds_dir().mkdir(parents=True, exist_ok=True)
    dest = frame_path(slug)
    tmp = dest.with_suffix(".png.tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    _write_meta(slug, {
        "fingerprint": fp,
        "drawn": drawn,
        "updated": time.time(),
        "source": source,
        "prompt": _meta_prompt(slug, prompt=prompt),
    })
    return record(slug)


def install_from_file(slug: str, src: str, fp: str = "", source: str = "generated",
                      prompt: str = "", drawn: str = "") -> Dict[str, Any]:
    """Copy an existing still into the World cache. Used by intro gen + plates.

    ``drawn`` is the fingerprint of the prompts that actually produced the
    picture, which is not always this World's snapshot — an intro rendered
    during play comes from the live prompt file. Play compares it against the
    live prompts before it will open a run on the frame.
    """
    slug = _safe_slug(slug)
    path = Path(src)
    if not slug or not path.is_file():
        return record(slug)
    fp = fp or fingerprint_for_slug(slug)
    _worlds_dir().mkdir(parents=True, exist_ok=True)
    dest = frame_path(slug)
    tmp = dest.with_suffix(".png.tmp")
    shutil.copyfile(path, tmp)
    tmp.replace(dest)
    _write_meta(slug, {
        "fingerprint": fp,
        "drawn": drawn,
        "updated": time.time(),
        "source": source,
        "prompt": _meta_prompt(slug, prompt=prompt),
    })
    log.info("[WORLD FRAMES] cached %s from %s (%s, drawn %s)",
             slug, source, fp[:8], (drawn or "-")[:8])
    return record(slug)


def remember_from_play(img_path: str, session_id: str = "default") -> None:
    """Intro (or any frame 0) just rendered — keep it as this World's first frame."""
    if not img_path:
        return
    slug = ""
    prompt = ""
    try:
        import engine
        st = engine._load_state(session_id)
        wid = str((st or {}).get("experience_world_id") or "")
        prompt = clip_steer_prompt(
            (st or {}).get("current_render_prompt")
            or (st or {}).get("current_image_prompt")
            or ""
        )
        import experience_store
        exp = experience_store.get_experience()
        world = experience_store.world_by_id(exp, wid) if wid else None
        if world is None:
            slug = start_world_slug(exp)
        else:
            slug = str(world.get("slug") or "")
    except Exception:
        slug = start_world_slug()
    if not slug:
        return
    try:
        # Play rendered this against the LIVE prompts, so that — not the
        # World snapshot — is what the picture is actually of.
        install_from_file(
            slug, img_path, fingerprint_for_slug(slug),
            source="intro", prompt=prompt, drawn=live_fingerprint(),
        )
    except Exception as e:
        log.warning("[WORLD FRAMES] remember_from_play failed: %s", e)


def _plate_path(prompts: Optional[Dict[str, Any]]) -> Optional[Path]:
    try:
        import game_identity
    except Exception:
        return None
    spec = game_identity.spec_from_prompts(prompts or {})
    for raw in game_identity.setting_reference_paths(spec):
        p = Path(raw)
        if p.is_file():
            return p
    for raw in game_identity.character_reference_paths(spec):
        p = Path(raw)
        if p.is_file():
            return p
    return None


def _images_enabled() -> bool:
    try:
        import engine
        return bool(getattr(engine, "IMAGE_ENABLED", False) and getattr(engine, "LLM_ENABLED", False))
    except Exception:
        return False


def _generate_paid(slug: str, prompts: Dict[str, Any], fp: str, *,
                   drawn: str = "", source: str = "generated") -> Optional[str]:
    """Render an opening still into a private session. Does not touch play state.

    ``drawn`` is the fingerprint of the prompts this picture is actually OF, and
    defaults to ``fp`` because the cache-warming path renders from the same
    snapshot it stamps. ``render_live_plate`` separates the two: it draws from
    the live prompt file while keeping the snapshot's hash in ``fingerprint``,
    so the editor's dirty/ready reporting is unaffected.
    """
    import engine
    import game_identity

    spec = game_identity.spec_from_prompts(prompts)
    shot = game_identity.opening_shot(spec)
    bible = str((prompts or {}).get("world_initial_state") or "")
    lore_vision = lore_opening_vision(prompts)
    if shot:
        # The sheet leads — it names the cast and the framing — and the bible
        # grounds it in the place the montage is about to photograph.
        vision = f"{shot['vision']} {lore_vision}".strip() if lore_vision else shot["vision"]
        prologue = shot["prologue"]
    else:
        vision = lore_vision or DEFAULT_FRAME_VISION
        prologue = "The run begins."
    session_id = f"{_GEN_SESSION_PREFIX}{_safe_slug(slug)}"[:80]
    # Use this World's sheet, not whoever happens to be loaded live. The
    # plate is an img2img reference inside _gen_image, not the cached still.
    #
    # ``world_prompt`` is the bible, not the prologue. This render has no
    # session state behind it, so it is the only way the level's own text
    # reaches the visual-tone gloss — the same channel a played frame uses.
    result = engine._gen_image(
        vision,
        "camcorder",
        "Intro",
        image_description="",
        use_edit_mode=False,
        frame_idx=0,
        dispatch=prologue,
        world_prompt=bible or prologue,
        hard_transition=True,
        session_id=session_id,
        history_ref=[],
        identity_spec=spec,
    )
    img_path = result[0] if result else None
    if img_path and Path(img_path).is_file():
        install_from_file(
            slug, img_path, fp, source=source,
            prompt=scene_prompt_for(prompts, vision), drawn=drawn or fp,
        )
        return str(img_path)
    return None


def render_live_plate(slug: str) -> Dict[str, Any]:
    """Draw this World's opening plate NOW, from the prompts the run will use.

    Every other render path here goes through the World SNAPSHOT
    (``worlds/<slug>.json``), which is a copy that only moves when somebody
    saves the level — while a run is played on the live prompt file. The two
    drift the moment anybody edits anything, and a plate drawn from the snapshot
    is then a photograph of the level as it used to be.

    That drift is the entire reason the opening had a cache check in front of it
    (``drawn_from_live``), and the reason editing a world silently cost the next
    run its montage: the cached frame failed the check, ``ensure`` could only
    have redrawn it wrong, so the run opened with no cutscene at all and a log
    line nobody sees. Play now calls this instead of reading the cache, so the
    plate is always a picture of the level being played and the opening never
    has a reason to be skipped.

    ``fingerprint`` keeps the snapshot's hash and ``drawn`` carries the live one,
    matching what ``remember_from_play`` writes — stamping the live hash in both
    would leave ``record`` reporting the frame permanently dirty, and the
    editor would schedule a snapshot re-render straight over the top of it.

    Returns the resulting record. On any failure (images off, no key, the render
    came back empty) that is whatever was already on disk, and the caller
    decides what to do with it.
    """
    slug = _safe_slug(slug)
    if not slug:
        return record("")
    if not _images_enabled():
        log.info("[WORLD FRAMES] live plate for %s skipped: images are off", slug)
        return record(slug)
    try:
        from prompts_store import PROMPTS
        live = dict(PROMPTS)
    except Exception as e:
        log.warning("[WORLD FRAMES] live plate for %s: no live prompts (%s)", slug, e)
        return record(slug)
    if not live:
        return record(slug)

    fp = fingerprint_for_slug(slug)
    drawn = live_fingerprint()
    t0 = time.time()
    print(f"[WORLD FRAMES] drawing {slug}'s opening plate from the live prompts "
          f"({(drawn or '-')[:8]})", flush=True)
    # Claim the slot so a debounced editor ensure() for the same World does not
    # render a second, snapshot-based plate straight over this one.
    with _lock:
        _generating[slug] = drawn or fp
    try:
        path = _generate_paid(slug, live, fp, drawn=drawn, source="intro")
    except Exception as e:
        log.warning("[WORLD FRAMES] live plate for %s failed: %s", slug, e)
        path = None
    finally:
        with _lock:
            _generating.pop(slug, None)

    rec = record(slug)
    if path:
        print(f"[WORLD FRAMES] {slug}'s plate is this run's own "
              f"({time.time() - t0:.1f}s)", flush=True)
    else:
        print(f"[WORLD FRAMES] live plate for {slug} did not render "
              f"({time.time() - t0:.1f}s) — falling back to whatever is cached",
              flush=True)
    return rec


def is_real_still(rec: Optional[Dict[str, Any]]) -> bool:
    """A rendered opening shot, not the mint square standing in for one.

    Public because Play has to ask the same question. ``ensure`` installs a
    placeholder the moment a frame goes missing and only replaces it if the
    paid render succeeds, so "there is a file on disk" is not the same as
    "there is something worth showing a player".
    """
    return str((rec or {}).get("source") or "") in ("generated", "intro", "plate")


_is_real_still = is_real_still


def _fill_without_paid(slug: str, prompts: Dict[str, Any], fp: str) -> Dict[str, Any]:
    existing = record(slug, prompts)
    # A reference plate is input to the opening shot, not the shot itself.
    # Keep a generated / intro still. Only copy the plate when we cannot
    # render and there is no real scene on disk yet (mock / no keys).
    if existing.get("path") and _is_real_still(existing):
        return existing
    vision = scene_prompt_for(prompts)
    plate = _plate_path(prompts)
    if plate is not None and not _images_enabled():
        return install_from_file(slug, str(plate), fp, source="plate", prompt=vision)
    return _install_bytes(slug, _placeholder_png(), fp, source="placeholder", prompt=vision)


def _run_ensure(slug: str) -> Dict[str, Any]:
    slug = _safe_slug(slug)
    if not slug:
        return record("")
    try:
        data = worlds_store.get_world(slug)
    except KeyError:
        return record(slug)
    prompts = data.get("prompts") or {}
    fp = fingerprint(prompts)
    rec = record(slug, prompts)
    images_on = _images_enabled()
    placeholder = str(rec.get("source") or "") == "placeholder"
    if rec.get("status") == "ready" and rec.get("path"):
        return rec
    # Stale generated still + no image backend: keep what is on disk.
    # Restamp the current fingerprint so a force-reset (or a prompt edit
    # in mock) does not sit on "dirty" forever.
    if rec.get("path") and _is_real_still(rec) and not images_on:
        if rec.get("dirty"):
            plate = _plate_path(prompts)
            if plate is not None:
                return install_from_file(slug, str(plate), fp, source="plate")
            src = str(rec.get("source") or "generated")
            return install_from_file(slug, rec["path"], fp, source=src)
        return rec

    with _lock:
        if _generating.get(slug) == fp:
            rec["generating"] = True
            rec["status"] = "generating"
            return rec
        _generating[slug] = fp

    try:
        # Mock / no keys: fill a stand-in. Never paste the reference plate
        # over a generated scene — that is how the editor started showing
        # the upload instead of the opening shot.
        if not images_on:
            _fill_without_paid(slug, prompts, fp)
        elif not rec.get("path") or placeholder:
            _fill_without_paid(slug, prompts, "interim")

        if _images_enabled():
            try:
                _generate_paid(slug, prompts, fp)
            except Exception as e:
                log.warning("[WORLD FRAMES] paid gen failed for %s: %s", slug, e)
            rec = record(slug, prompts)
            if not rec.get("path"):
                _fill_without_paid(slug, prompts, fp)
    finally:
        with _lock:
            if _generating.get(slug) == fp:
                _generating.pop(slug, None)
    return record(slug, prompts)


def invalidate(slug: str, *, reason: str = "reset") -> Dict[str, Any]:
    """Stamp the cache stale so ``ensure`` will regenerate even if the
    prompt hash has not changed. RESET on the desk uses this."""
    slug = _safe_slug(slug)
    if not slug:
        return record("")
    meta = _read_meta(slug)
    meta["fingerprint"] = "force-%s-%d" % (reason or "reset", int(time.time() * 1000))
    meta["updated"] = time.time()
    if "source" not in meta:
        meta["source"] = ""
    _write_meta(slug, meta)
    rec = record(slug)
    rec["dirty"] = True
    if rec.get("status") == "ready":
        rec["status"] = "dirty"
    return rec


def force_reset(slug: str, *, wait: bool = False) -> Dict[str, Any]:
    """Dirty this World's first frame and kick a regen."""
    invalidate(slug)
    return ensure(slug, wait=wait)


def ensure(slug: str, *, wait: bool = False) -> Dict[str, Any]:
    """Make sure this World's first frame is on disk. Non-blocking by default."""
    slug = _safe_slug(slug)
    rec = record(slug)
    if rec.get("status") == "ready":
        return rec
    if wait:
        return _run_ensure(slug)

    def _worker():
        try:
            _run_ensure(slug)
        except Exception as e:
            log.warning("[WORLD FRAMES] ensure failed for %s: %s", slug, e)

    threading.Thread(target=_worker, daemon=True, name=f"world-frame-{slug}").start()
    rec["generating"] = True
    rec["status"] = "generating" if not rec.get("path") else rec.get("status") or "generating"
    return rec


def schedule_ensure(slug: str, delay: float = _DEBOUNCE_S) -> None:
    """Debounced regen so typing in the editor does not fire a render per key."""
    slug = _safe_slug(slug)
    if not slug:
        return

    def _fire():
        with _lock:
            _timers.pop(slug, None)
        ensure(slug, wait=False)

    with _lock:
        old = _timers.pop(slug, None)
        if old is not None:
            try:
                old.cancel()
            except Exception:
                pass
        timer = threading.Timer(max(0.05, float(delay)), _fire)
        timer.daemon = True
        _timers[slug] = timer
        timer.start()


def schedule_ensure_all(exp: Optional[Dict[str, Any]] = None, delay: float = 0.4) -> None:
    try:
        import experience_store
        if exp is None:
            exp = experience_store.get_experience()
    except Exception:
        return
    for world in (exp or {}).get("worlds") or []:
        slug = str((world or {}).get("slug") or "")
        if slug:
            schedule_ensure(slug, delay=delay)


def maybe_kick_all(exp: Optional[Dict[str, Any]] = None) -> None:
    """Warm every World in the active Experience, at most once per few seconds."""
    global _last_all_kick
    now = time.time()
    if now - _last_all_kick < 8.0:
        return
    _last_all_kick = now
    schedule_ensure_all(exp, delay=0.2)


def slugs_in_experience(exp: Optional[Dict[str, Any]] = None) -> List[str]:
    try:
        import experience_store
        if exp is None:
            exp = experience_store.get_experience()
    except Exception:
        return []
    out: List[str] = []
    for world in (exp or {}).get("worlds") or []:
        slug = str((world or {}).get("slug") or "")
        if slug:
            out.append(slug)
    return out
