#!/usr/bin/env python3
"""Offline render jobs — a playtest you can start from the game UI.

Playtesting has always been a terminal errand: pick a harness, remember the
flags, remember to point it at the right port, remember that switching the
image model means hand-editing ai_config.json. That friction is why every run
so far used the same fast model at the same 1K, which is a fine way to check
that the simulation works and a useless way to find out how good it can look.

A render is the other mode: deliberately slow, deliberately expensive, aimed at
the ceiling rather than the loop. You say how many turns, which image model and
at what size, and which model writes the prose; the server plays a full run
against itself and hands back the frames, a flipbook and the verdict.

Two things are load-bearing:

  * **The run is a real player.** It drives the same HTTP endpoints the browser
    does, in a session of its own, so it inherits whatever the editor currently
    says — level, character, camera, prompts, tunables. There is no separate
    "render config" to drift out of sync with the game.
  * **A render owns the renderer.** Model choice is global (see
    ai_provider_manager), so the job snapshots the current settings, applies its
    own, and puts the originals back when it finishes — including when it fails
    or is cancelled. One at a time, for the same reason.
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
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import ai_provider_manager

ROOT = Path(__file__).parent.resolve()
RENDER_ROOT = ROOT / "playtest_results" / "renders"
# Zips live under the render root so the one guarded resolver serves them too.
# Underscore-prefixed so the history scan doesn't mistake it for a run.
BUNDLE_DIR = RENDER_ROOT / "_bundles"

# How the run plays itself. MOVE-driven scanning is the default because it is
# the path that actually relocates the camera each turn, which is what you want
# to look at when you are judging pictures; plain choices are the comparison.
MODES = {
    "scan_move": "SCAN each frame, then MOVE TO a detected object",
    "auto": "Press the generated choices, no scanning",
}

# Turns are effectively open — a "long-form" playtest is the whole point of the
# studio. The ceiling is just a sanity bound against a fat-fingered input that
# would spend for hours; it is not meant as a real limit.
#
# 80 contradicted that comment and contradicted the test written in the same
# commit, which asserts 200 is accepted and only "the absurd (<=0 or into the
# thousands)" refused. So test_turn_count_is_bounded has never passed — a red
# test from birth, which is the kind that gets explained away rather than read.
# The comment above and the test agree with each other; the number was the
# odd one out.
MIN_TURNS, MAX_TURNS = 1, 200

# Progress line printed by playtest_interactive.play() at the top of each turn.
_TURN_RE = re.compile(r"^\s*turn\s+(\d+)/(\d+)\b")

_LOCK = threading.Lock()
_job: Optional["RenderJob"] = None


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


class RenderJob:
    """One render, from spawn to artifacts."""

    def __init__(self, spec: Dict[str, Any], base_url: str):
        self.spec = spec
        self.base_url = base_url.rstrip("/")
        self.id = f"render_{_stamp()}"
        self.out_dir = RENDER_ROOT / self.id
        self.session_id = self.id
        self.state = "starting"
        self.turn = 0
        self.turns = int(spec["turns"])
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.error: Optional[str] = None
        self.verdict: Optional[Dict[str, Any]] = None
        self.log: list[str] = []
        self.restored: Optional[Dict[str, Any]] = None
        self._proc: Optional[subprocess.Popen] = None
        self._cancelled = False
        # Put the renderer back exactly as we found it, whatever happens next.
        self._snapshot = ai_provider_manager.model_settings()
        self.applied = ai_provider_manager.apply_models(
            image_model=spec.get("image_model"),
            image_size=spec.get("image_size"),
            text_model=spec.get("text_model"),
            aspect_ratio=spec.get("aspect_ratio"),
        )

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable, "-u", str(ROOT / "playtest_interactive.py"),
            "--url", self.base_url,
            "--turns", str(self.turns),
            "--mode", self.spec["mode"],
            "--session", self.session_id,
            "--out", str(self.out_dir),
            # A Pro frame at 4K is minutes, not seconds. The CLI defaults assume
            # the fast model and would call a healthy render a timeout.
            "--turn-timeout", str(self.spec["turn_timeout"]),
            "--image-grace", str(self.spec["image_grace"]),
        ]
        if self.spec.get("world_drift"):
            cmd.append("--world-drift")
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        self._proc = subprocess.Popen(
            cmd, cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        )
        self.state = "running"
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        """Follow the harness's output so the UI can show where the run is."""
        try:
            for line in self._proc.stdout:  # type: ignore[union-attr]
                line = line.rstrip()
                if not line:
                    continue
                self.log.append(line)
                del self.log[:-400]
                m = _TURN_RE.match(line)
                if m:
                    self.turn = int(m.group(1))
        except Exception as e:
            self.log.append(f"[render] output stream ended: {e}")
        finally:
            self._finish()

    def _finish(self) -> None:
        code = self._proc.wait() if self._proc else -1
        self.finished_at = time.time()
        has_session = self._read_session()
        if self._cancelled:
            # cancel() asks the harness to stop cooperatively (see the .stop
            # file), so it gets to run its own finalize step before exiting —
            # a stopped run has a session, a gif and videos exactly like a
            # normal finish, just shorter. Only a run force-killed (app
            # shutdown) or stopped before turn one landed has nothing, and
            # that empty folder is discarded rather than kept as a dead tile.
            self.state = "stopped" if has_session else "cancelled"
        elif code == 0:
            self.state = "done"
        else:
            # The harness exits non-zero when its own checks fail, which is a
            # result worth keeping rather than an error — the frames are still
            # on disk. Only a missing transcript means the run truly broke.
            self.state = "done" if has_session else "failed"
            if self.state == "failed":
                self.error = f"render exited with code {code}"
        if self.state == "cancelled":
            self._discard_empty_run()
        else:
            self._write_sidecar()
        self.restored = ai_provider_manager.apply_models(
            image_model=self._snapshot["image_model"],
            image_size=self._snapshot["image_size"],
            text_model=self._snapshot["text_model"],
            aspect_ratio=self._snapshot.get("aspect_ratio"),
        )
        print(f"[RENDER] {self.id} {self.state} — renderer restored to "
              f"{self.restored['image_model']} @ {self.restored['image_size']}", flush=True)

    def _read_session(self) -> bool:
        path = self.out_dir / "session.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        self.verdict = data.get("verdict")
        return True

    def _write_sidecar(self) -> None:
        """What this run was, in one small file beside the transcript.

        The harness records what happened but not what it was *asked for* — the
        models in particular, which are the whole reason a render exists and are
        otherwise only recoverable by digging into a per-turn status blob. The
        browsing list reads this instead of parsing a multi-megabyte transcript
        to print one line.
        """
        try:
            (self.out_dir / "render.json").write_text(json.dumps({
                "id": self.id,
                "state": self.state,
                "mode": self.spec["mode"],
                "turns_requested": self.turns,
                "turns_done": self.turn,
                "models": self.applied,
                "started_at": datetime.fromtimestamp(
                    self.started_at, timezone.utc).isoformat(),
                "elapsed_s": round((self.finished_at or time.time()) - self.started_at, 1),
                "error": self.error,
            }, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[RENDER] could not write render.json: {e}", flush=True)

    # How long to wait for a cooperative stop before giving up on it and
    # killing the process anyway — well past the heaviest single turn (a
    # Pro/4K image can eat most of turn_timeout + image_grace on its own).
    _STOP_GRACE_S = 15 * 60

    def cancel(self, force: bool = False) -> None:
        """Ask the run to stop.

        Graceful (the STOP button): touch a sentinel the harness checks at
        the next turn boundary, so it falls through to its own finalize step
        instead of being killed mid-turn — a stopped run keeps its frames,
        gif and videos. Forced (app shutdown): also kill it immediately,
        because at that point the only thing that matters is that a paid
        model stops being called, not how tidy the partial run looks.
        """
        self._cancelled = True
        self.state = "stopping"
        try:
            (self.out_dir / ".stop").touch()
        except OSError:
            pass
        if force:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
        else:
            watchdog = threading.Timer(self._STOP_GRACE_S, self._force_if_still_stuck)
            watchdog.daemon = True
            watchdog.start()

    def _force_if_still_stuck(self) -> None:
        if self.state == "stopping" and self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def _discard_empty_run(self) -> None:
        """Nothing landed before the stop — drop the folder rather than leave
        an empty, unplayable stub for the library to have to skip around."""
        try:
            if self.out_dir.is_dir():
                shutil.rmtree(self.out_dir, ignore_errors=True)
        except OSError:
            pass

    # ── reporting ────────────────────────────────────────────────────────
    def latest_frame(self) -> Optional[str]:
        """The newest view frame on disk, so the panel can show the run live."""
        frames = sorted((self.out_dir / "frames").glob("turn_*_view.png"))
        return f"{self.id}/frames/{frames[-1].name}" if frames else None

    def frames_so_far(self) -> List[str]:
        """Every view frame captured so far, in order — lets the live panel
        scrub back through the run in progress, not just watch the newest."""
        if not self.out_dir.is_dir():
            return []
        return _frames(self.out_dir)

    def sequences_so_far(self) -> Dict[str, List[str]]:
        """This run's flipbook frames so far, per turn — the motion behind each
        view frame, for a stage that can loop it while the next turn renders."""
        if not self.out_dir.is_dir():
            return {}
        return _sequences(self.out_dir)

    def beats_so_far(self) -> List[Dict[str, Any]]:
        """What's actually happening, turn by turn — the choice offered, what
        got taken, and the line of narration it produced. Read fresh off disk
        every call (the harness rewrites live.json after each turn) so the
        panel has something to watch besides a frame count while it waits."""
        try:
            data = json.loads((self.out_dir / "live.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data.get("beats") or []

    def artifacts(self) -> Dict[str, Any]:
        if self.state not in ("done", "cancelled", "stopped"):
            return {}
        return _artifacts_of(self.out_dir)

    def to_dict(self) -> Dict[str, Any]:
        elapsed = (self.finished_at or time.time()) - self.started_at
        return {
            "id": self.id,
            "state": self.state,
            "turn": self.turn,
            "turns": self.turns,
            "mode": self.spec["mode"],
            "elapsed_s": round(elapsed, 1),
            "models": self.applied,
            "restored": self.restored,
            "error": self.error,
            "verdict": self.verdict,
            "latest_frame": self.latest_frame(),
            "frames": self.frames_so_far(),
            "sequences": self.sequences_so_far(),
            # How long each flipbook frame is meant to hold. The panel plays the
            # frames itself, so without this it would have to guess at a speed
            # the player already chose.
            "sequence_frame_ms": _flipbook_frame_ms(),
            "beats": self.beats_so_far(),
            "artifacts": self.artifacts(),
            "log": self.log[-40:],
            "out_dir": str(self.out_dir),
        }


# ── module API ───────────────────────────────────────────────────────────

def options() -> Dict[str, Any]:
    """Everything the render form needs to draw itself, straight from config."""
    return {
        # Only offer models this build can actually run right now — one
        # missing KREA_API_KEY/FAL_API_KEY/OPENAI_API_KEY/ANTHROPIC_API_KEY
        # shouldn't mean a render that silently fails or falls back to a
        # different model than the one the picker said it was using.
        "image_models": ai_provider_manager.available_model_catalogue("image"),
        "text_models": ai_provider_manager.available_model_catalogue("text"),
        "sizes": list(ai_provider_manager.IMAGE_SIZES),
        "aspect_ratios": ai_provider_manager.aspect_ratio_options(),
        "modes": [{"id": k, "label": v} for k, v in MODES.items()],
        "current": ai_provider_manager.model_settings(),
        "turn_limits": {"min": MIN_TURNS, "max": MAX_TURNS},
        "defaults": {
            "turns": 40,
            "mode": "scan_move",
            # Same room as Play: Fast stills. Pro is a desk option, not a splash.
            "image_model": "gemini-3.1-flash-lite-image",
            "image_size": "1K",
            "aspect_ratio": "16:9",
            "text_model": ai_provider_manager.get_text_model(),
        },
    }


def validate(body: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a request body into a spec, or raise ValueError saying why not."""
    try:
        turns = int(body.get("turns", 40))
    except (TypeError, ValueError):
        raise ValueError("turns must be a whole number")
    if not MIN_TURNS <= turns <= MAX_TURNS:
        raise ValueError(f"turns must be between {MIN_TURNS} and {MAX_TURNS}")

    mode = str(body.get("mode") or "scan_move")
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")

    spec: Dict[str, Any] = {
        "turns": turns,
        "mode": mode,
        "world_drift": bool(body.get("world_drift", True)),
    }
    for key, kind in (("image_model", "image"), ("text_model", "text")):
        value = body.get(key)
        if value:
            if not ai_provider_manager.find_model(kind, value):
                raise ValueError(f"unknown {key}: {value}")
            spec[key] = value
    size = body.get("image_size")
    if size:
        if str(size).upper() not in ai_provider_manager.IMAGE_SIZES:
            raise ValueError(f"unknown image_size: {size}")
        spec["image_size"] = str(size).upper()
    ratio = body.get("aspect_ratio")
    if ratio:
        mapped = ai_provider_manager.normalize_aspect_ratio(ratio)
        if not mapped:
            raise ValueError(f"unknown aspect_ratio: {ratio}")
        spec["aspect_ratio"] = mapped

    # A Pro frame at 4K can take minutes. Scale the per-turn patience with the
    # work being asked for, so a slow render isn't mistaken for a hung one.
    heavy = spec.get("image_size") in ("2K", "4K") or "pro" in (spec.get("image_model") or "")
    spec["turn_timeout"] = int(body.get("turn_timeout") or (600 if heavy else 180))
    spec["image_grace"] = float(body.get("image_grace") or (180 if heavy else 25))
    return spec


def start(body: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    """Kick off a render. Raises RuntimeError if one is already going."""
    global _job
    spec = validate(body)
    with _LOCK:
        if _job is not None and _job.state in ("starting", "running"):
            raise RuntimeError("a render is already running")
        job = RenderJob(spec, base_url)
        _job = job
    job.start()
    print(f"[RENDER] {job.id} started — {spec['turns']} turns, {spec['mode']}, "
          f"{job.applied['image_model']} @ {job.applied['image_size']}", flush=True)
    return job.to_dict()


def status() -> Dict[str, Any]:
    job = _job
    if job is None:
        return {"state": "idle", "models": ai_provider_manager.model_settings()}
    return job.to_dict()


def cancel(force: bool = False) -> Dict[str, Any]:
    job = _job
    if job is None or job.state not in ("starting", "running"):
        raise RuntimeError("no render is running")
    job.cancel(force=force)
    return job.to_dict()


# Cap the live film upload so a runaway recorder can't fill the disk. A long
# Watch session is minutes of low-bitrate webm; this is generous headroom.
_LIVE_FILM_MAX_BYTES = 512 * 1024 * 1024


def _ffmpeg_exe() -> Optional[str]:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def save_live(job_id: str, film_bytes: bytes, timeline: Any = None) -> Dict[str, Any]:
    """Store the browser-recorded live world film for a run as its Film cut.

    Only the run this browser is (or just was) driving may be written — the id
    must match the current/last job so a stray POST can't drop a file into some
    other run's folder. Writes the raw webm, an optional overlay timeline, and
    (best-effort) a remuxed mp4 so every player can open it.
    """
    job = _job
    if job is None or not job_id or job.id != job_id:
        raise ValueError("no matching render for this live film")
    if not film_bytes:
        raise ValueError("empty film")
    if len(film_bytes) > _LIVE_FILM_MAX_BYTES:
        raise ValueError("live film exceeds the size cap")
    out_dir = job.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    webm = out_dir / "playtest_live.webm"
    tmp = out_dir / ".playtest_live.webm.part"
    tmp.write_bytes(film_bytes)
    tmp.replace(webm)

    # Overlay timeline: [{t, turn, phase}] mapping playback seconds onto beats.
    if isinstance(timeline, str):
        try:
            timeline = json.loads(timeline)
        except (ValueError, TypeError):
            timeline = None
    if isinstance(timeline, list):
        try:
            (out_dir / "live_timeline.json").write_text(
                json.dumps({"timeline": timeline}), encoding="utf-8")
        except OSError:
            pass

    mp4_written = False
    ffmpeg = _ffmpeg_exe()
    if ffmpeg:
        mp4 = out_dir / "playtest_live.mp4"
        mp4_tmp = out_dir / ".playtest_live.mp4.part"
        try:
            # A clean, background-ready movie: H.264 High/yuv420p so every
            # browser and desktop player can decode it, even dimensions (libx264
            # rejects odd width/height), a visually-lossless CRF, faststart so it
            # starts instantly as a looping backdrop, and no audio (the film is
            # a silent background element).
            proc = subprocess.run(
                [ffmpeg, "-y", "-i", str(webm),
                 "-an",
                 "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                 "-c:v", "libx264", "-profile:v", "high", "-preset", "veryfast",
                 "-crf", "20", "-pix_fmt", "yuv420p",
                 "-movflags", "+faststart", str(mp4_tmp)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=600,
            )
            if proc.returncode == 0 and mp4_tmp.exists() and mp4_tmp.stat().st_size > 0:
                mp4_tmp.replace(mp4)
                mp4_written = True
            else:
                mp4_tmp.unlink(missing_ok=True)
        except Exception as e:
            print(f"[RENDER] live film remux failed for {job_id}: {e}", flush=True)
            try:
                mp4_tmp.unlink(missing_ok=True)
            except OSError:
                pass

    print(f"[RENDER] {job_id} live film saved ({len(film_bytes)} bytes"
          f"{', +mp4' if mp4_written else ''})", flush=True)
    return {"ok": True, "mp4": mp4_written, "bytes": len(film_bytes)}


def live_timeline_of(run: Path) -> List[Dict[str, Any]]:
    """The recorded live-film overlay timeline, or [] when the run has none."""
    data = _read_json(run / "live_timeline.json")
    tl = data.get("timeline") if isinstance(data, dict) else None
    return tl if isinstance(tl, list) else []


# ── reviewing finished renders ───────────────────────────────────────────
#
# A render's whole output is a folder on disk, and it outlives the job object,
# the process and the machine reboot. So everything below reads the folder
# rather than the registry: the last render is not special, and the one from
# Tuesday is just as reviewable as the one that finished a minute ago.

# Each catalogue key maps to the canonical filename first, then any legacy
# aliases from older renders — so a run made before the naming settled is still
# catalogued and still plays back through the app rather than showing an empty
# player. `clean_artifacts.py` keeps every .mp4/.gif/.md/.json, so these all
# survive a stills sweep and a run stays fully reviewable on disk indefinitely.
_ARTIFACTS = {
    "gif": ["playtest.gif"],
    # The animated world-model film, recorded in the Watch TV during a live run
    # (see WatchFilm in standalone.js). mp4 first (remuxed for broad playback),
    # else the raw webm the browser uploaded.
    "live_video": ["playtest_live.mp4", "playtest_live.webm"],
    "review_video": ["playtest_review.mp4"],
    "view_video": ["playtest_view.mp4", "playtest_scene.mp4"],
    "choices_video": ["playtest_choices.mp4", "playtest_scan.mp4"],
    "selected_video": ["playtest_selected.mp4", "playtest_result.mp4"],
    "summary": ["SUMMARY.md"],
    "transcript": ["session.json"],
}

# Preference order when the catalogue needs a single "play this" video: the
# animated live film if it exists, else the causal walkthrough, else the
# scene/view pass, else anything.
_VIDEO_KEYS = ("live_video", "review_video", "view_video", "selected_video", "choices_video")


def _run_dirs() -> List[Path]:
    if not RENDER_ROOT.is_dir():
        return []
    return sorted((d for d in RENDER_ROOT.iterdir()
                   if d.is_dir() and not d.name.startswith("_")),
                  key=lambda d: d.name, reverse=True)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _models_of(run: Path, session: Dict[str, Any]) -> Dict[str, Any]:
    """Which models drew this run.

    Prefer the sidecar; fall back to the per-turn status blob so renders made
    before the sidecar existed still say what they were drawn with.
    """
    side = _read_json(run / "render.json")
    if side.get("models"):
        return side["models"]
    for turn in session.get("turns", []):
        status = turn.get("game_status") or {}
        if status.get("image_model"):
            return {
                "image_model": status.get("image_model"),
                "image_provider": status.get("image_provider"),
                "image_size": status.get("image_size", ""),
                "text_model": status.get("text_model", ""),
            }
    return {}


def _frames(run: Path, kind: str = "view") -> List[str]:
    return [f"{run.name}/frames/{p.name}"
            for p in sorted((run / "frames").glob(f"turn_*_{kind}.png"))]


def _flipbook_frame_ms() -> int:
    try:
        import engine
        return int(engine.FLIPBOOK_FRAME_MS)
    except Exception:
        return 420


def _sequences(run: Path) -> Dict[str, List[str]]:
    """Flipbook frames per turn: {"turn_03": [".../turn_03_seq_01.png", ...]}.

    A flipbook turn's view frame is only where its action ENDED, so on its own
    the stage cuts between outcomes. Keyed by the turn prefix of the view frame
    it belongs to, which is all the panel needs to pair them up — and kept out
    of `_frames` on purpose, so the scrubber still has one stop per turn instead
    of one per in-between.
    """
    out: Dict[str, List[str]] = {}
    for p in sorted((run / "frames").glob("turn_*_seq_*.png")):
        key = p.name.split("_seq_")[0]
        out.setdefault(key, []).append(f"{run.name}/frames/{p.name}")
    return {k: v for k, v in out.items() if len(v) > 1}


def _first_existing(run: Path, names: List[str]) -> Optional[str]:
    """The first of `names` that is actually on disk, as a URL-shaped rel path."""
    for n in names:
        if (run / n).exists():
            return f"{run.name}/{n}"
    return None


def _artifacts_of(run: Path) -> Dict[str, Any]:
    found: Dict[str, Any] = {}
    for k, names in _ARTIFACTS.items():
        rel = _first_existing(run, names)
        if rel:
            found[k] = rel
    for kind in ("view", "choices", "selected"):
        got = _frames(run, kind)
        if got:
            found[f"{kind}_frames"] = got
    found["frames"] = found.get("view_frames", [])
    seqs = _sequences(run)
    if seqs:
        found["sequences"] = seqs
    return found


def _best_video(artifacts: Dict[str, Any]) -> Optional[str]:
    """The one video to open when the catalogue says 'play this run'."""
    for k in _VIDEO_KEYS:
        if artifacts.get(k):
            return artifacts[k]
    return None


def history(limit: int = 40) -> Dict[str, Any]:
    """Every render still on disk, newest first — one line each.

    A run that never got past its first turn — a stop before anything
    landed, or a crash before that — has nothing to browse or play back, so
    it is skipped here rather than showing a dead tile in the library.
    """
    out = []
    for run in _run_dirs():
        if len(out) >= limit:
            break
        session = _read_json(run / "session.json")
        side = _read_json(run / "render.json")
        verdict = session.get("verdict") or {}
        frames = _frames(run)
        artifacts = _artifacts_of(run)
        video = _best_video(artifacts)
        turns_done = len(session.get("turns", []))
        if turns_done == 0 and not video and not artifacts.get("gif"):
            continue
        out.append({
            "id": run.name,
            "state": side.get("state") or ("done" if session else "unknown"),
            "started_at": side.get("started_at") or session.get("started_at"),
            "elapsed_s": side.get("elapsed_s"),
            "mode": side.get("mode") or session.get("mode"),
            "turns_requested": side.get("turns_requested") or session.get("turns_requested"),
            "turns_done": turns_done,
            "models": _models_of(run, session),
            "passed": verdict.get("passed"),
            # A swept run has no stills left but keeps its GIF, so the catalogue
            # still shows a preview rather than a blank tile.
            "thumbnail": frames[0] if frames else artifacts.get("gif"),
            "frame_count": len(frames),
            # Start-menu wordmark + catalogue tiles can cycle stills without a
            # second /api/render/run round-trip. Cap keeps the list light.
            "frames": frames[:24],
            "gif": artifacts.get("gif"),
            # So the catalogue can route a click to the video player vs the
            # frame reviewer, and show a play badge only when there's footage.
            "has_video": bool(video),
            "video": video,
            "bytes": sum(p.stat().st_size for p in run.rglob("*") if p.is_file()),
        })
    return {"renders": out}


def overlay_beats_of(run: Path, session: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Beats Watch paints from. Prefer live.json (written during the run);
    fall back to session.json turns so an older folder without live.json
    still has choices / scan geometry. Missing data → [] (video still plays)."""
    live = _read_json(run / "live.json")
    beats = live.get("beats") if isinstance(live, dict) else None
    if isinstance(beats, list) and beats:
        return beats
    if session is None:
        session = _read_json(run / "session.json")
    turns = (session or {}).get("turns") or []
    if not turns:
        return []
    try:
        from playtest_interactive import _beat_from_turn
    except Exception:
        return []
    return [_beat_from_turn(t) for t in turns]


def overlay_timing(session: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """How playback maps video time onto those beats."""
    session = session or {}
    gif_ms = session.get("gif_ms") or 1100
    try:
        review_page_s = max(0.1, float(gif_ms) / 1000.0)
    except (TypeError, ValueError):
        review_page_s = 1.1
    try:
        flipbook_page_s = max(0.1, float(session.get("video_pause_s") or 0.5))
    except (TypeError, ValueError):
        flipbook_page_s = 0.5
    burned = any(
        t.get("choices_frame") or t.get("selected_frame")
        for t in (session.get("turns") or [])
        if isinstance(t, dict)
    )
    return {
        "review_page_s": review_page_s,
        "flipbook_page_s": flipbook_page_s,
        "burned_in": bool(burned),
    }


def _overlay_label(value: Any) -> str:
    if value is None or value is False:
        return ""
    if isinstance(value, (list, tuple)):
        return " · ".join(filter(None, (_overlay_label(v) for v in value)))
    if isinstance(value, dict):
        return _overlay_label(
            value.get("label") or value.get("subject") or value.get("text")
            or value.get("name") or value.get("choice") or ""
        )
    text = str(value).strip()
    return "" if text == "[object Object]" else text


def _overlay_box(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    try:
        cx, cy, w, h = float(raw["cx"]), float(raw["cy"]), float(raw["w"]), float(raw["h"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(map(lambda n: n == n and abs(n) != float("inf"), (cx, cy, w, h))):
        return None
    return {
        "label": _overlay_label(raw) or "?",
        "cx": cx, "cy": cy,
        "w": max(0.02, w), "h": max(0.02, h),
    }


def overlay_state(beat: Optional[Dict[str, Any]], phase: Optional[str] = None) -> Dict[str, Any]:
    """What Watch paints for one recorded beat.

    Soft-fail: missing beat → empty overlay. Scan geometry becomes boxes;
    a choice slate becomes type on the frame; a resolved pick locks the
    selected line or box. Captions only appear when the run recorded them.
    """
    empty = {
        "boxes": [], "pick": None, "choices": [], "selected": "",
        "caption": "", "caption_kind": "", "phase": "", "kind": "",
    }
    if not beat or not isinstance(beat, dict):
        return dict(empty)
    p = phase if phase is not None else (beat.get("phase") or "")
    kind = str(beat.get("kind") or beat.get("action_kind") or "")
    boxes = []
    for raw in beat.get("boxes") or []:
        box = _overlay_box(raw)
        if box:
            boxes.append(box)
    pick = _overlay_box(beat.get("pick")) if isinstance(beat.get("pick"), dict) else None
    if pick is None and isinstance(beat.get("pick"), dict):
        name = _overlay_label(beat.get("pick"))
        pick = {"label": name} if name else None
    subject = _overlay_label(beat.get("subject"))
    if pick is None and subject:
        pick = {"label": subject}
    choices = [_overlay_label(c) for c in (beat.get("choices") or [])]
    choices = [c for c in choices if c]
    selected = _overlay_label(beat.get("choice") or beat.get("choice_text"))
    offering = p in ("scanning", "choosing", "pending")
    locked = p in ("chosen", "waiting", "resolved")
    if offering:
        pick = None
        selected = ""
    narrative = str(beat.get("narrative") or "").strip()
    caption, caption_kind = "", ""
    if locked and narrative:
        caption, caption_kind = narrative, "narrator"
    kind_l = kind.lower()
    if "camp" in kind_l:
        caption_kind = "camp"
        if not caption:
            caption = selected or subject or "CAMP"
    elif kind_l.startswith("scan_move") and subject and locked:
        if not caption:
            caption, caption_kind = "MOVE TO " + subject, "move"
    return {
        "boxes": boxes,
        "pick": pick if (locked or p == "scanned") else None,
        "choices": [] if boxes else choices,
        "selected": selected if locked else "",
        "caption": caption[:180],
        "caption_kind": caption_kind,
        "phase": p,
        "kind": kind,
    }


def overlay_state_at_page(
    beats: List[Dict[str, Any]],
    page: int,
    variant: str = "review_video",
    burned_in: bool = False,
) -> Dict[str, Any]:
    """Playback: map a video page index onto overlay state.

    review_video is VIEW / CHOICES / SELECTED per turn (then a final VIEW).
    Flipbooks are one page per turn. If the walkthrough already burned
    choices into CHOICES/SELECTED pages, those pages stay empty so type
    does not double-print; VIEW pages (and view_video) still overlay.
    """
    empty = overlay_state(None)
    if not beats:
        return empty
    page = max(0, int(page))
    if variant == "review_video":
        triplet = len(beats) * 3
        if page >= triplet:
            last = dict(beats[-1])
            last["phase"] = "resolved"
            last["boxes"] = []
            last["pick"] = None
            last["choices"] = []
            return overlay_state(last, phase="resolved")
        turn = beats[min(page // 3, len(beats) - 1)]
        slot = page % 3
        if burned_in and slot in (1, 2):
            return empty
        if slot == 2:
            return overlay_state(turn, phase="chosen")
        offer = "scanned" if (turn.get("boxes") or []) else "choosing"
        return overlay_state(turn, phase=offer)
    beat = beats[min(page, len(beats) - 1)]
    if variant == "choices_video":
        return empty if burned_in else overlay_state(beat, phase="choosing")
    if variant == "selected_video":
        return empty if burned_in else overlay_state(beat, phase="chosen")
    # view_video (and unknown): caller splits the page into offer → pick
    # via overlay_state_at_time; this helper exposes the offer state.
    return overlay_state(beat, phase="scanned" if (beat.get("boxes") or []) else "choosing")


def overlay_state_at_time(
    beats: List[Dict[str, Any]],
    t: float,
    variant: str = "review_video",
    review_page_s: float = 1.1,
    flipbook_page_s: float = 0.5,
    burned_in: bool = False,
) -> Dict[str, Any]:
    """Playback: map video currentTime onto overlay state."""
    if not beats:
        return overlay_state(None)
    try:
        t = max(0.0, float(t))
    except (TypeError, ValueError):
        t = 0.0
    if variant == "review_video":
        page_s = review_page_s if review_page_s > 0 else 1.1
        return overlay_state_at_page(beats, int(t / page_s), variant, burned_in)
    page_s = flipbook_page_s if flipbook_page_s > 0 else 0.5
    page = int(t / page_s)
    if variant in ("choices_video", "selected_video"):
        return overlay_state_at_page(beats, page, variant, burned_in)
    if page >= len(beats):
        last = dict(beats[-1])
        last["phase"] = "resolved"
        last["boxes"] = []
        last["choices"] = []
        return overlay_state(last, phase="resolved")
    frac = (t % page_s) / page_s if page_s else 1.0
    beat = beats[page]
    if frac < 0.45:
        offer = "scanned" if (beat.get("boxes") or []) else "choosing"
        return overlay_state(beat, phase=offer)
    return overlay_state(beat, phase="chosen")


def detail(run_id: str) -> Dict[str, Any]:
    """Everything the reviewer needs for one run, turn by turn.

    The transcript carries a lot that only matters to the pass/fail checks. What
    a person reviewing footage wants is narrower: the picture, what was done to
    get there, and what the game said happened — so that is what comes back,
    rather than shipping several megabytes of detector boxes to the browser.

    Watch also needs the decision theater (choices / scan / pick). That rides
    in ``beats`` + ``overlay`` — reconstructed from live.json or session.json,
    empty when an old run recorded neither.
    """
    run = _resolve_run(run_id)
    session = _read_json(run / "session.json")
    side = _read_json(run / "render.json")
    turns = []
    for t in session.get("turns", []):
        status = t.get("game_status") or {}
        turns.append({
            "turn": t.get("turn"),
            "action": t.get("choice_text"),
            "subject": t.get("subject"),
            "kind": t.get("action_kind"),
            "narrative": t.get("narrative"),
            "view": _rel(run, t.get("view_frame")),
            "choices": _rel(run, t.get("choices_frame")),
            "selected": _rel(run, t.get("selected_frame")),
            "scene_changed": t.get("scene_changed"),
            "time_of_day": status.get("time_of_day"),
            "phase": status.get("phase"),
            "chaos": status.get("chaos"),
            "detection": status.get("detection"),
        })
    intro = session.get("reset") or {}
    artifacts = _artifacts_of(run)
    # Distinguish "this run has no stills left" from "this run never rendered".
    # The former is the normal state after a sweep and the videos still play.
    swept = bool(turns) and not any(t["view"] or t["selected"] or t["choices"]
                                    for t in turns)
    beats = overlay_beats_of(run, session)
    timing = overlay_timing(session)
    # A live film maps playback time onto beats by recorded wall-clock, not the
    # fixed flipbook cadence. When present, hand the player that timeline so its
    # overlays land on the right scene.
    live_tl = live_timeline_of(run)
    if live_tl:
        timing["timeline"] = live_tl
        timing["kind"] = "live"
    return {
        "id": run.name,
        "stills_swept": swept,
        "state": side.get("state") or ("done" if session else "unknown"),
        "started_at": side.get("started_at") or session.get("started_at"),
        "finished_at": session.get("finished_at"),
        "elapsed_s": side.get("elapsed_s"),
        "mode": side.get("mode") or session.get("mode"),
        "mode_title": session.get("mode_title"),
        "models": _models_of(run, session),
        "verdict": session.get("verdict"),
        "intro": " ".join(intro.get("narratives") or [])[:1200],
        "turns": turns,
        # The generation the LAST turn's selected choice produced — closes
        # the causal chain the per-turn triplets are otherwise missing one
        # link of at the very end.
        "final_view": _rel(run, session.get("final_view_frame")),
        "artifacts": artifacts,
        "beats": beats,
        "overlay": timing,
        "bundle": f"_bundles/{run.name}.zip",
    }


def _rel(run: Path, frame: Optional[str]) -> Optional[str]:
    """Transcript frame paths are OS-native and run-relative; the browser needs
    a URL-shaped path under the render root.

    Returns None when the still is no longer on disk. `tools/clean_artifacts.py`
    sweeps frames but keeps the videos, so the transcript outlives the images it
    names; handing those paths to the browser would render broken tiles instead
    of the reviewer's empty state.
    """
    if not frame:
        return None
    if not (run / str(frame).replace("\\", "/")).exists():
        return None
    return f"{run.name}/" + str(frame).replace("\\", "/")


def _resolve_run(run_id: str) -> Path:
    """A run directory named by the caller, contained under the render root."""
    run = (RENDER_ROOT / run_id).resolve()
    if RENDER_ROOT.resolve() != run.parent:
        raise ValueError("not a render directory")
    if not run.is_dir():
        raise FileNotFoundError(run_id)
    return run


def bundle(run_id: str) -> Path:
    """Zip the whole run for export, rebuilding only when it's gone stale."""
    run = _resolve_run(run_id)
    BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = BUNDLE_DIR / f"{run.name}.zip"
    newest = max((p.stat().st_mtime for p in run.rglob("*") if p.is_file()),
                 default=0)
    if zip_path.exists() and zip_path.stat().st_mtime >= newest:
        return zip_path
    tmp = zip_path.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w") as z:
        for p in sorted(run.rglob("*")):
            if not p.is_file():
                continue
            # PNG, GIF and MP4 are already compressed; deflating them costs
            # seconds per hundred megabytes and saves close to nothing.
            store = p.suffix.lower() in (".png", ".gif", ".mp4", ".jpg", ".jpeg")
            z.write(p, arcname=f"{run.name}/{p.relative_to(run).as_posix()}",
                    compress_type=zipfile.ZIP_STORED if store else zipfile.ZIP_DEFLATED)
    tmp.replace(zip_path)
    return zip_path


def artifact_path(rel: str) -> Path:
    """Resolve a reported artifact path under the render root, or raise.

    Reported paths are server-generated, but they arrive back over HTTP where
    anything can be typed, so the containment check is the real guard.
    """
    target = (RENDER_ROOT / rel).resolve()
    root = RENDER_ROOT.resolve()
    if root not in target.parents:
        raise ValueError("path escapes the render directory")
    if not target.is_file():
        raise FileNotFoundError(rel)
    return target
