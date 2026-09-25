"""THE TAPE — every run kept, played back, and exported.

A run used to exist only while it was being played. New Game called
``purge_run_media``, which deletes every picture in the session's images
folder, and ``tape_frames`` — the only ordered record of what the run
showed — kept one URL per turn (the last flipbook panel), none of the
fights, none of the opening, and nothing to say what happened in each
frame. So there was nothing to watch once a run ended, and nothing to hand
to a video model afterwards.

This module is the run's own record, written as the run happens:

    sessions/<sid>/runs/current.json          which run is being recorded
    sessions/<sid>/runs/<run>/tape.json       the manifest (below)
    sessions/<sid>/runs/<run>/frames/<name>   every frame it showed

A frame is HARD-LINKED into the run's folder the moment it is recorded
(copied where the filesystem cannot link), so the purge at the next New
Game deletes the images folder's name for it and the tape keeps its own.
That costs no disk while the run is live and survives any reset after.

THE MANIFEST is a list of SHOTS in the order they were on screen. A shot is
one beat of the game with everything a viewer or a video model needs:

    {"n", "kind", "t", "turn", "world",
     "action",   # what the player did, in the second person
     "caption",  # what came of it, one short line (a fight's outcome)
     "prose",    # the narration of that beat
     "prompt",   # the text the frames were generated from
     "frames",   # file names, in playing order
     "frame_ms"} # the game's own panel timing, when the beat animated

Kinds: ``montage`` (the opening's establishing shots), ``opening`` (the
arrival), ``turn``, ``encounter`` (a fight opening), ``round`` (a round of
a fight), ``death``. ``chapters`` marks where the run crossed into another
World.

``timing(shot)`` is the ONE pacing rule: the player and the exported
animatic both read it, so the film you export is the film you watched.

The endpoints are in api.py under ``/api/reel/`` (``/api/replay/`` is the
model-call replay cache). The player is ``Reel`` in standalone.js.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
SESSIONS_ROOT = ROOT / "sessions"

KEEP_RUNS = 24          # finished runs kept per session, newest first
MAX_SHOTS = 800         # a runaway run stops recording, it does not fill a disk
MIN_SHOTS_TO_KEEP = 1   # a run with nothing on it is not a run

ANIMATIC_FPS = 24
ANIMATIC_SIZE = (1376, 768)   # the flipbook grid's own resolution

_SID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_RUN_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-.]{1,200}$")
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")

_locks: Dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock(sid: str) -> threading.RLock:
    with _locks_guard:
        lk = _locks.get(sid)
        if lk is None:
            lk = _locks[sid] = threading.RLock()
        return lk


def _log(msg: str) -> None:
    try:
        print(f"[TAPE] {msg}", flush=True)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────
# Paths and files
# ──────────────────────────────────────────────────────────────────────────

def _clean_sid(sid: Any) -> str:
    s = str(sid or "default").strip() or "default"
    return s if _SID_RE.match(s) else "default"


def valid_run_id(rid: Any) -> bool:
    return bool(_RUN_RE.match(str(rid or "")))


def _session_dir(sid: str) -> Path:
    return SESSIONS_ROOT / _clean_sid(sid)


def _runs_dir(sid: str) -> Path:
    return _session_dir(sid) / "runs"


def _run_dir(sid: str, rid: str) -> Path:
    if not valid_run_id(rid):
        raise ValueError(f"not a run id: {rid!r}")
    return _runs_dir(sid) / rid


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _tape_path(sid: str, rid: str) -> Path:
    return _run_dir(sid, rid) / "tape.json"


def _current_path(sid: str) -> Path:
    return _runs_dir(sid) / "current.json"


def frame_file(sid: str, rid: str, name: str) -> Optional[Path]:
    """The file behind one frame of a run, or None. Names are validated: a
    request can never walk out of the run's folder."""
    if not valid_run_id(rid) or not _NAME_RE.match(str(name or "")) or ".." in str(name):
        return None
    p = _run_dir(sid, rid) / "frames" / str(name)
    return p if p.is_file() else None


def _source_path(sid: str, ref: Any) -> Optional[Path]:
    """A frame as the game passes it around — an absolute path, or a
    ``/images/<name>?session=<id>`` URL — as a file on disk."""
    s = str(ref or "").strip()
    if not s:
        return None
    if s.startswith("/images/") or s.startswith("images/"):
        u = urlparse(s)
        name = os.path.basename(unquote(u.path))
        q = dict(p.split("=", 1) for p in u.query.split("&") if "=" in p)
        owner = _clean_sid(unquote(q.get("session", "")) or sid)
        for base in (_session_dir(owner) / "images", _session_dir(sid) / "images"):
            cand = base / name
            if cand.is_file():
                return cand
        return None
    p = Path(s)
    return p if p.is_file() else None


def _keep_frame(src: Path, dest_dir: Path) -> Optional[str]:
    """Hard-link (else copy) one frame into the run. Returns its name."""
    if src.suffix.lower() not in _IMAGE_EXTS or src.name.endswith("_small.png"):
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        return dest.name
    try:
        os.link(src, dest)
    except OSError:
        try:
            shutil.copy2(src, dest)
        except OSError as e:
            _log(f"could not keep {src.name}: {e}")
            return None
    return dest.name


# ──────────────────────────────────────────────────────────────────────────
# Recording
# ──────────────────────────────────────────────────────────────────────────

def _new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


def current_id(sid: str) -> Optional[str]:
    rid = str((_read_json(_current_path(sid), {}) or {}).get("run") or "")
    if valid_run_id(rid) and _tape_path(sid, rid).is_file():
        return rid
    return None


def _describe(state: Optional[dict]) -> Dict[str, str]:
    """Who and where, read off the run's state: the Experience (the picker's
    tile), its current World, and the protagonist's name."""
    st = state if isinstance(state, dict) else {}
    out = {"experience": str(st.get("experience_id") or "").strip(),
           "experience_name": "", "world": "", "protagonist": ""}
    try:
        import experience_store
        slug = out["experience"] or experience_store.get_active_slug()
        out["experience"] = slug
        exp = experience_store.get_experience(slug)
        out["experience_name"] = str(exp.get("name") or "").strip()
        wid = str(st.get("experience_world_id") or "").strip()
        if wid:
            world = experience_store.world_by_id(exp, wid) or {}
            out["world"] = str(world.get("name") or "").strip()
    except Exception:
        pass
    try:
        import game_identity
        name = str(game_identity.display_name() or "").strip()
        if name and not name.lower().startswith("the player"):
            out["protagonist"] = name
        # The run's Character (characters.py): the export ships the looks it
        # wore, so a video pass has the same person the game drew.
        meta = game_identity.bound_character()
        if meta.get("id"):
            out["character"] = meta["id"]
            out["looks"] = [meta.get("look")] if meta.get("look") else []
    except Exception:
        pass
    return out


def note_look(sid: str, cid: str, look_id: str, line: str = "", state: Optional[dict] = None) -> None:
    """The character put something on: the look joins the tape (the export
    ships its turnaround) and the timeline gets a chapter mark."""
    try:
        sid = _clean_sid(sid)
        with _lock(sid):
            rid = _ensure(sid, state)
            path = _tape_path(sid, rid)
            tape = _read_json(path, None)
            if not isinstance(tape, dict):
                return
            tape["character"] = cid or tape.get("character")
            looks = tape.setdefault("looks", [])
            if look_id and look_id not in looks:
                looks.append(look_id)
            if line:
                tape.setdefault("chapters", []).append(
                    {"at": len(tape.get("shots") or []), "title": _clip(f"Suited up \u2014 {line}", 120),
                     "t": time.time(), "kind": "look"})
            _write_json(path, tape)
    except Exception as e:  # noqa: BLE001
        _log(f"look note failed: {e}")


def begin(sid: str, state: Optional[dict] = None) -> str:
    """Start recording a new run. Whatever was being recorded is closed first
    (as abandoned, if nothing ended it)."""
    sid = _clean_sid(sid)
    with _lock(sid):
        prev = current_id(sid)
        if prev:
            _close(sid, prev, "abandoned")
        rid = _new_run_id()
        now = time.time()
        tape = {"id": rid, "session": sid, "started": now, "updated": now,
                "ended": None, "ending": None, "cause": "",
                **_describe(state), "shots": [], "chapters": []}
        _write_json(_tape_path(sid, rid), tape)
        _write_json(_current_path(sid), {"run": rid})
        prune(sid)
        _log(f"recording run {rid} for session {sid}")
        return rid


def _ensure(sid: str, state: Optional[dict] = None) -> str:
    return current_id(sid) or begin(sid, state)


def _close(sid: str, rid: str, ending: str, cause: str = "") -> None:
    path = _tape_path(sid, rid)
    tape = _read_json(path, None)
    if not isinstance(tape, dict):
        return
    if not tape.get("ended"):
        tape["ended"] = time.time()
        tape["ending"] = ending
        tape["cause"] = cause or tape.get("cause") or ""
        _write_json(path, tape)
    if len(tape.get("shots") or []) < MIN_SHOTS_TO_KEEP:
        shutil.rmtree(_run_dir(sid, rid), ignore_errors=True)


def record(sid: str, kind: str, frames: List[Any], *, frame_ms: Optional[int] = None,
           action: str = "", caption: str = "", prose: str = "", prompt: str = "",
           turn: Optional[int] = None, state: Optional[dict] = None,
           title: str = "") -> Optional[dict]:
    """Put one beat on the tape. Never raises: the game does not stop for
    its own recording."""
    try:
        sid = _clean_sid(sid)
        with _lock(sid):
            rid = _ensure(sid, state)
            path = _tape_path(sid, rid)
            tape = _read_json(path, None)
            if not isinstance(tape, dict):
                return None
            shots = tape.setdefault("shots", [])
            if len(shots) >= MAX_SHOTS:
                return None
            fdir = _run_dir(sid, rid) / "frames"
            names: List[str] = []
            for ref in frames or []:
                src = _source_path(sid, ref)
                if not src:
                    continue
                name = _keep_frame(src, fdir)
                if name and name not in names:
                    names.append(name)
            if not names:
                return None
            # The same picture landing twice (a still re-announced, a plate
            # the resolve reuses) is one shot, not two.
            if shots and set(names) <= set(shots[-1].get("frames") or []):
                return None
            shot = {
                "n": len(shots) + 1,
                "kind": str(kind or "turn"),
                "t": time.time(),
                "turn": int(turn) if isinstance(turn, (int, float)) else None,
                "world": _describe(state).get("world", "") if state else "",
                "title": _clip(title, 120),
                "action": _clip(action, 300),
                "caption": _clip(caption, 300),
                "prose": _clip(prose, 1400),
                "prompt": _clip(prompt, 2400),
                "frames": names,
                "frame_ms": int(frame_ms) if frame_ms else None,
            }
            shots.append(shot)
            tape["updated"] = time.time()
            if state and not tape.get("protagonist"):
                tape.update({k: v for k, v in _describe(state).items() if v and not tape.get(k)})
            _write_json(path, tape)
            return shot
    except Exception as e:  # noqa: BLE001
        _log(f"record failed ({kind}): {e}")
        return None


def chapter(sid: str, title: str, state: Optional[dict] = None) -> None:
    """The run crossed into another World: a chapter mark on the timeline."""
    try:
        sid = _clean_sid(sid)
        with _lock(sid):
            rid = _ensure(sid, state)
            path = _tape_path(sid, rid)
            tape = _read_json(path, None)
            if not isinstance(tape, dict):
                return
            tape.setdefault("chapters", []).append(
                {"at": len(tape.get("shots") or []), "title": _clip(title, 120), "t": time.time()})
            _write_json(path, tape)
    except Exception as e:  # noqa: BLE001
        _log(f"chapter failed: {e}")


def end(sid: str, ending: str, cause: str = "") -> None:
    """The run is over (``died``). The tape stays current — it is what the
    death screen's WATCH THE TAPE plays — until the next run begins."""
    try:
        sid = _clean_sid(sid)
        with _lock(sid):
            rid = current_id(sid)
            if not rid:
                return
            path = _tape_path(sid, rid)
            tape = _read_json(path, None)
            if isinstance(tape, dict) and not tape.get("ended"):
                tape["ended"] = time.time()
                tape["ending"] = ending
                tape["cause"] = _clip(cause, 200)
                _write_json(path, tape)
    except Exception as e:  # noqa: BLE001
        _log(f"end failed: {e}")


def prune(sid: str, keep: int = KEEP_RUNS) -> None:
    """Keep the newest ``keep`` runs of a session; the one being recorded is
    never counted or removed."""
    root = _runs_dir(sid)
    if not root.is_dir():
        return
    cur = current_id(sid)
    runs = sorted((d for d in root.iterdir() if d.is_dir() and valid_run_id(d.name)
                   and d.name != cur), key=lambda d: d.name, reverse=True)
    for d in runs[keep:]:
        shutil.rmtree(d, ignore_errors=True)


def _clip(text: Any, n: int) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


# ──────────────────────────────────────────────────────────────────────────
# Pacing — one rule for the player and the animatic
# ──────────────────────────────────────────────────────────────────────────

def timing(shot: dict) -> List[int]:
    """Milliseconds on screen for each frame of a shot.

    A flipbook beat plays a little slower than the game plays it (the game
    is hurrying to the next choice; the tape is not) and then HOLDS its last
    panel long enough to read the line under it. A single still holds for
    the read. The opening's establishing shots are slow; a death is slower.
    """
    frames = list(shot.get("frames") or [])
    n = len(frames)
    if not n:
        return []
    words = len(f"{shot.get('action') or ''} {shot.get('caption') or ''}".split())
    read = max(2400, min(6500, 1400 + 280 * words))
    kind = str(shot.get("kind") or "")
    if kind == "montage":
        return [3400] * n
    if n == 1:
        return [max(3200, read)]
    if kind == "death":
        step = 1150
        return [step] * (n - 1) + [max(4200, read)]
    fm = int(shot.get("frame_ms") or 420)
    step = max(560, min(1400, int(fm * 1.6)))
    return [step] * (n - 1) + [max(2000, read - step * (n - 1))]


# ──────────────────────────────────────────────────────────────────────────
# Reading
# ──────────────────────────────────────────────────────────────────────────

def _frame_url(sid: str, rid: str, name: str) -> str:
    return f"/api/reel/frame/{rid}/{name}?session_id={sid}"


def _summary(sid: str, tape: dict, cur: Optional[str]) -> dict:
    shots = tape.get("shots") or []
    rid = str(tape.get("id") or "")
    turns = [s.get("turn") for s in shots if isinstance(s.get("turn"), int)]
    last = next((s for s in reversed(shots) if s.get("frames")), None)
    return {
        "id": rid,
        "started": tape.get("started"),
        "ended": tape.get("ended"),
        "ending": tape.get("ending"),
        "cause": tape.get("cause") or "",
        "experience": tape.get("experience") or "",
        "experience_name": tape.get("experience_name") or "",
        "world": tape.get("world") or "",
        "protagonist": tape.get("protagonist") or "",
        "shots": len(shots),
        "turns": max(turns) if turns else sum(1 for s in shots if s.get("kind") == "turn"),
        "fights": sum(1 for s in shots if s.get("kind") == "encounter"),
        "frames": sum(len(s.get("frames") or []) for s in shots),
        "duration_ms": sum(sum(timing(s)) for s in shots),
        "cover": _frame_url(sid, rid, last["frames"][-1]) if last else None,
        "current": rid == cur,
    }


def runs(sid: str, experience: str = "") -> List[dict]:
    """Every kept run of a session, newest first (optionally one Experience's)."""
    sid = _clean_sid(sid)
    root = _runs_dir(sid)
    if not root.is_dir():
        return []
    cur = current_id(sid)
    out = []
    for d in sorted(root.iterdir(), key=lambda d: d.name, reverse=True):
        if not d.is_dir() or not valid_run_id(d.name):
            continue
        tape = _read_json(d / "tape.json", None)
        if not isinstance(tape, dict) or not tape.get("shots"):
            continue
        if experience and str(tape.get("experience") or "") != experience:
            continue
        out.append(_summary(sid, tape, cur))
    return out


def load(sid: str, rid: str) -> Optional[dict]:
    """A run's manifest as the player reads it: frame URLs and timings in."""
    sid = _clean_sid(sid)
    if rid in ("current", "", None):
        rid = current_id(sid) or (runs(sid) or [{}])[0].get("id") or ""
    if not valid_run_id(rid):
        return None
    tape = _read_json(_tape_path(sid, rid), None)
    if not isinstance(tape, dict):
        return None
    out = dict(tape)
    out["summary"] = _summary(sid, tape, current_id(sid))
    shots = []
    for s in tape.get("shots") or []:
        s2 = dict(s)
        s2["urls"] = [_frame_url(sid, rid, n) for n in s.get("frames") or []]
        s2["timing"] = timing(s)
        shots.append(s2)
    out["shots"] = shots
    return out


# ──────────────────────────────────────────────────────────────────────────
# Export
# ──────────────────────────────────────────────────────────────────────────

def _tape_name(tape: dict) -> str:
    title = tape.get("experience_name") or tape.get("world") or "ABYSS"
    stamp = time.strftime("%Y-%m-%d %H%M", time.localtime(float(tape.get("started") or time.time())))
    raw = f"{title} {stamp}"
    return re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-")[:80] or "ABYSS-tape"


def _video_prompt(shot: dict) -> str:
    """What happens in this shot, for an image-to-video model: the action,
    what came of it, the narration. Not the still's own prompt (that
    describes one frame; this is the motion between two)."""
    parts = [shot.get("action") or "", shot.get("caption") or "", shot.get("prose") or ""]
    text = " ".join(p.strip().rstrip(".") + "." for p in parts if p and p.strip())
    return _clip(text, 900)


def shot_list(tape: dict, frame_names: Dict[str, str]) -> dict:
    """shots.json: one entry per shot with its first and last frame — the
    unit an image-to-video model takes — and the words that connect them."""
    out_shots = []
    clock = 0
    for s in tape.get("shots") or []:
        files = [frame_names[n] for n in s.get("frames") or [] if n in frame_names]
        if not files:
            continue
        dur = sum(timing(s))
        out_shots.append({
            "shot": len(out_shots) + 1,
            "kind": s.get("kind"),
            "turn": s.get("turn"),
            "world": s.get("world") or tape.get("world") or "",
            "start_s": round(clock / 1000, 3),
            "duration_s": round(dur / 1000, 3),
            "first_frame": files[0],
            "last_frame": files[-1],
            "frames": files,
            "action": s.get("action") or "",
            "caption": s.get("caption") or "",
            "prose": s.get("prose") or "",
            "video_prompt": _video_prompt(s),
            "image_prompt": s.get("prompt") or "",
        })
        clock += dur
    return {
        "title": tape.get("experience_name") or "",
        "world": tape.get("world") or "",
        "protagonist": tape.get("protagonist") or "",
        "recorded": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(tape.get("started") or 0))),
        "ending": tape.get("ending") or "",
        "cause": tape.get("cause") or "",
        "duration_s": round(clock / 1000, 3),
        "chapters": tape.get("chapters") or [],
        "how_to_use": (
            "Each shot is one beat of the run. first_frame and last_frame are the "
            "keyframes an image-to-video model (Seedance, Kling, Veo, Runway) takes "
            "as start and end image; video_prompt is the motion between them. Shot "
            "N+1 starts where shot N ends, so generating the shots in order and "
            "cutting them together gives one continuous film. captions.srt and "
            "animatic.mp4 are timed to duration_s."),
        "shots": out_shots,
    }


def _srt_time(ms: int) -> str:
    h, rem = divmod(int(ms), 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def captions_srt(tape: dict) -> str:
    lines, clock, i = [], 0, 0
    for s in tape.get("shots") or []:
        dur = sum(timing(s))
        text = "\n".join(t for t in (s.get("action"), s.get("caption")) if t) \
            or _clip(s.get("prose") or "", 140)
        if text:
            i += 1
            lines += [str(i), f"{_srt_time(clock)} --> {_srt_time(clock + dur)}", text, ""]
        clock += dur
    return "\n".join(lines)


def _ffmpeg() -> Optional[str]:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def build_animatic(sid: str, rid: str, out_path: Path, tape: Optional[dict] = None) -> Optional[Path]:
    """The tape as one MP4, paced by ``timing`` — hard cuts, every frame on
    one 1376×768 canvas (the panels are upscaled, the montage fits as is)."""
    tape = tape or _read_json(_tape_path(sid, rid), {})
    exe = _ffmpeg()
    if not exe:
        _log("ffmpeg not found — no animatic")
        return None
    entries = []
    fdir = _run_dir(sid, rid) / "frames"
    for s in tape.get("shots") or []:
        for name, ms in zip(s.get("frames") or [], timing(s)):
            p = fdir / name
            if p.is_file():
                entries.append((p, ms))
    if not entries:
        return None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    concat = out_path.with_suffix(".concat.txt")
    rows = []
    for p, ms in entries:
        q = str(p.resolve()).replace("\\", "/").replace("'", "'\\''")
        rows += [f"file '{q}'", f"duration {ms / 1000:.3f}"]
    last = str(entries[-1][0].resolve()).replace("\\", "/").replace("'", "'\\''")
    rows.append(f"file '{last}'")
    concat.write_text("\n".join(rows), encoding="utf-8")
    w, h = ANIMATIC_SIZE
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,fps={ANIMATIC_FPS},format=yuv420p")
    cmd = [exe, "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-an", "-vf", vf,
           "-r", str(ANIMATIC_FPS), "-c:v", "libx264", "-preset", "veryfast",
           "-tune", "stillimage", "-crf", "18", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", str(out_path)]
    try:
        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: no console flash
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=900, **kw)
        if proc.returncode != 0:
            _log(f"animatic failed: {(proc.stderr or '')[-600:]}")
            return None
        return out_path
    except Exception as e:  # noqa: BLE001
        _log(f"animatic failed: {e}")
        return None
    finally:
        try:
            concat.unlink()
        except OSError:
            pass


_README = """THE TAPE — {name}

{summary}

frames/        every frame the run showed, numbered in the order it played
               (NNNN_sSSS.png — frame number, then the shot it belongs to)
shots.json     one entry per shot: first_frame, last_frame, video_prompt,
               duration. This is the unit an image-to-video model takes:
               give it the first frame as the start image, the last frame as
               the end image, and video_prompt as the prompt. Shot N+1 begins
               where shot N ends, so the generated clips cut together into
               one continuous film.
captions.srt   the action and outcome of each shot, timed to the animatic
animatic.mp4   the tape as you watched it, hard cuts, {fps} fps
"""


def export_pack(sid: str, rid: str, out_dir: Path, *, animatic: bool = True) -> Optional[Path]:
    """The shot pack: a zip of frames, shots.json, captions.srt, README and
    (when ffmpeg is there) animatic.mp4."""
    tape = _read_json(_tape_path(sid, rid), None)
    if not isinstance(tape, dict) or not tape.get("shots"):
        return None
    name = _tape_name(tape)
    out_dir.mkdir(parents=True, exist_ok=True)
    zpath = out_dir / f"{name}.zip"
    fdir = _run_dir(sid, rid) / "frames"
    frame_names: Dict[str, str] = {}
    ordered: List[tuple] = []
    k = 0
    for s in tape.get("shots") or []:
        for n in s.get("frames") or []:
            if n in frame_names or not (fdir / n).is_file():
                continue
            k += 1
            ext = Path(n).suffix.lower() or ".png"
            arc = f"frames/{k:04d}_s{int(s.get('n') or 0):03d}{ext}"
            frame_names[n] = arc
            ordered.append((fdir / n, arc))
    shots = shot_list(tape, frame_names)
    summ = _summary(sid, tape, None)
    summary = (f"{summ['turns']} turns · {summ['fights']} fights · {summ['frames']} frames · "
               f"{int(summ['duration_ms'] // 60000)}:{int(summ['duration_ms'] // 1000 % 60):02d}")
    tmp = zpath.with_suffix(".part")
    mp4 = None
    if animatic:
        mp4 = build_animatic(sid, rid, out_dir / f"{name}.mp4", tape)
    with zipfile.ZipFile(tmp, "w") as z:
        z.writestr(f"{name}/README.txt", _README.format(name=name, summary=summary, fps=ANIMATIC_FPS),
                   compress_type=zipfile.ZIP_DEFLATED)
        z.writestr(f"{name}/shots.json", json.dumps(shots, ensure_ascii=False, indent=2),
                   compress_type=zipfile.ZIP_DEFLATED)
        z.writestr(f"{name}/captions.srt", captions_srt(tape), compress_type=zipfile.ZIP_DEFLATED)
        for src, arc in ordered:
            z.write(src, f"{name}/{arc}", compress_type=zipfile.ZIP_STORED)
        if mp4 and mp4.is_file():
            z.write(mp4, f"{name}/animatic.mp4", compress_type=zipfile.ZIP_STORED)
        for arc, src in _character_refs(tape):
            z.write(src, f"{name}/character/{arc}", compress_type=zipfile.ZIP_STORED)
    os.replace(tmp, zpath)
    return zpath


def _character_refs(tape: dict) -> List[tuple]:
    """(archive name, file) for each look this run's character wore: the
    turnaround the game drew them from, four sides on grey."""
    out: List[tuple] = []
    cid = str(tape.get("character") or "")
    if not cid:
        return out
    try:
        import characters
        for lid in tape.get("looks") or []:
            src = characters.look_dir(cid, str(lid)) / "turnaround_ref.jpg"
            if src.is_file():
                out.append((f"{lid}_turnaround.jpg", src))
    except Exception as e:  # noqa: BLE001
        _log(f"character refs skipped: {e}")
    return out


_jobs: Dict[str, dict] = {}
_jobs_guard = threading.Lock()


def export_job(sid: str, rid: str, kind: str) -> dict:
    """Start (or report) an export. ``kind`` is ``pack`` or ``animatic``.
    Returns {state: running|done|failed, file?, name?, error?}."""
    sid = _clean_sid(sid)
    if rid in ("current", "", None):
        rid = current_id(sid) or ""
    if not valid_run_id(rid) or not _tape_path(sid, rid).is_file():
        return {"state": "failed", "error": "no such run"}
    kind = "animatic" if kind == "animatic" else "pack"
    key = f"{sid}/{rid}/{kind}"
    stamp = _tape_path(sid, rid).stat().st_mtime
    with _jobs_guard:
        job = _jobs.get(key)
        if job and job.get("state") == "running":
            return dict(job)
        if job and job.get("state") == "done" and job.get("stamp") == stamp \
                and Path(job.get("file") or "").is_file():
            return dict(job)
        job = {"state": "running", "kind": kind, "run": rid, "stamp": stamp, "started": time.time()}
        _jobs[key] = job

    def work():
        out_dir = _run_dir(sid, rid) / "export"
        try:
            if kind == "animatic":
                tape = _read_json(_tape_path(sid, rid), {})
                path = build_animatic(sid, rid, out_dir / f"{_tape_name(tape)}.mp4", tape)
            else:
                path = export_pack(sid, rid, out_dir)
            if not path:
                raise RuntimeError("nothing to export" if kind == "pack" else "ffmpeg could not build it")
            saved = deliver(path)
            with _jobs_guard:
                job.update(state="done", file=str(path), name=path.name,
                           size=path.stat().st_size, saved_to=saved)
        except Exception as e:  # noqa: BLE001
            _log(f"export {key} failed: {e}")
            with _jobs_guard:
                job.update(state="failed", error=str(e)[:300])

    threading.Thread(target=work, name=f"tape-export-{rid}", daemon=True).start()
    return dict(job)


# ──────────────────────────────────────────────────────────────────────────
# Delivery (the desktop app has no download bar)
# ──────────────────────────────────────────────────────────────────────────

LOCAL_APP = False   # api.enable_local_keys() arms this for play.py / run_local


def export_folder() -> Path:
    """Where the desktop app puts an export: Videos/ABYSS Tapes (Movies on a
    Mac), else the home folder."""
    home = Path.home()
    for sub in ("Videos", "Movies"):
        if (home / sub).is_dir():
            return home / sub / "ABYSS Tapes"
    return home / "ABYSS Tapes"


def deliver(path: Path) -> Optional[str]:
    """Copy a finished export into the player's own folder, when this is the
    desktop app. A hosted server never writes outside its sessions."""
    if not LOCAL_APP:
        return None
    try:
        dest_dir = export_folder()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        shutil.copy2(path, dest)
        return str(dest)
    except Exception as e:  # noqa: BLE001
        _log(f"could not save to the player's folder: {e}")
        return None


def reveal(path: Optional[str] = None) -> bool:
    """Open the export folder (or select a file in it) in the OS file browser."""
    if not LOCAL_APP:
        return False
    folder = export_folder()
    target = Path(path) if path else folder
    try:
        target.resolve().relative_to(folder.resolve())
    except (ValueError, OSError):
        target = folder   # only ever the tapes folder, whatever was asked
    if target.is_file():
        target = target.parent
    if not target.exists():
        return False
    try:
        if sys.platform == "win32":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"could not open the folder: {e}")
        return False
