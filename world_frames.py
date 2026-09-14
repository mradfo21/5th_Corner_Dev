"""
world_frames.py — per-World first-frame cache. This is the app's load time.

Each World (a prompt snapshot in ``worlds/<slug>.json``) gets a sidecar still:

    worlds/<slug>.frame.png
    worlds/<slug>.frame.json   {fingerprint, updated, source, prompt}

The fingerprint is a hash of the prompt keys that actually change the opening
shot. When it matches, Play / Watch / the editor reuse the still. When it
doesn't (identity saved in the editor, persist, a prompt that steers the
camera), we regenerate in the background and swap the file atomically.

Play never waits on that render if a still — even a slightly stale one — is
already on disk. The cached frame is the first thing on screen; a dirty regen
lands later if one is needed.

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
                   prompt: str = "") -> Dict[str, Any]:
    slug = _safe_slug(slug)
    _worlds_dir().mkdir(parents=True, exist_ok=True)
    dest = frame_path(slug)
    tmp = dest.with_suffix(".png.tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    _write_meta(slug, {
        "fingerprint": fp,
        "updated": time.time(),
        "source": source,
        "prompt": _meta_prompt(slug, prompt=prompt),
    })
    return record(slug)


def install_from_file(slug: str, src: str, fp: str = "", source: str = "generated",
                      prompt: str = "") -> Dict[str, Any]:
    """Copy an existing still into the World cache. Used by intro gen + plates."""
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
        "updated": time.time(),
        "source": source,
        "prompt": _meta_prompt(slug, prompt=prompt),
    })
    log.info("[WORLD FRAMES] cached %s from %s (%s)", slug, source, fp[:8])
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
        install_from_file(
            slug, img_path, fingerprint_for_slug(slug),
            source="intro", prompt=prompt,
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


def _generate_paid(slug: str, prompts: Dict[str, Any], fp: str) -> Optional[str]:
    """Render an opening still into a private session. Does not touch play state."""
    import engine
    import game_identity

    spec = game_identity.spec_from_prompts(prompts)
    shot = game_identity.opening_shot(spec)
    if shot:
        vision = shot["vision"]
        prologue = shot["prologue"]
    else:
        vision = DEFAULT_FRAME_VISION
        prologue = "The run begins."
    session_id = f"{_GEN_SESSION_PREFIX}{_safe_slug(slug)}"[:80]
    # Use this World's sheet, not whoever happens to be loaded live. The
    # plate is an img2img reference inside _gen_image, not the cached still.
    result = engine._gen_image(
        vision,
        "camcorder",
        "Intro",
        image_description="",
        use_edit_mode=False,
        frame_idx=0,
        dispatch=prologue,
        world_prompt=prologue,
        hard_transition=True,
        session_id=session_id,
        history_ref=[],
        identity_spec=spec,
    )
    img_path = result[0] if result else None
    if img_path and Path(img_path).is_file():
        install_from_file(
            slug, img_path, fp, source="generated",
            prompt=scene_prompt_for(prompts, vision),
        )
        return str(img_path)
    return None


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
