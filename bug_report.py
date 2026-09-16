"""One button, one folder: everything needed to act on "this looks wrong".

Reproducing a reported bug is the expensive part of fixing one here. A turn
costs ~30 seconds and real money, and every frame and every line of prose is
generated fresh, so the exact situation a player was looking at cannot be
reached again by playing towards it. By the time a report says "the plate went
black" the evidence is gone, and the next five minutes go on trying to get back
to something that never repeats.

So capture it at the moment of the complaint. `capture()` writes one folder
holding the frame that was on screen, the layer stack that was drawing it, the
state that produced it, the prose and choices that were offered, the tail of the
server log and the last turns of history -- then a REPORT.md that opens with the
checks worth making mechanically: was the picture actually black, was something
opaque sitting over it, did the client throw, was a turn still in flight.

Nothing in here may raise into the game. A capture that takes down the run it
was documenting is worse than no capture at all, so every step is individually
guarded and the report records what it could not collect.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
BUGS_DIR = ROOT / "bugs"

# Enough server log to cover a whole turn including a retry, which is where the
# interesting lines are ([ENCOUNTER] retrying, [FLIPBOOK] no grid came back).
# A turn prints on the order of a hundred lines, so this holds roughly the last
# ten turns -- and it is a deque, so it costs a bounded amount of memory.
LOG_TAIL_LINES = 1200
_LOG_TAIL: deque = deque(maxlen=LOG_TAIL_LINES)
_LOG_LOCK = threading.Lock()
_TAP_INSTALLED = False


class _Tee:
    """Pass writes through to the real stream, keeping the last lines in memory.

    The server logs by printing to stderr, and where that lands depends on how
    the app was started: a redirect file under logs/ when the harness starts it,
    a console when a person does, Render's pipeline in production. A capture
    cannot go looking for a file that may not exist, so hold the tail in the
    process itself and let the report carry it.
    """

    def __init__(self, stream):
        self._stream = stream
        self._partial = ""

    def write(self, text):
        try:
            self._stream.write(text)
        except Exception:
            pass
        try:
            self._partial += text
            if "\n" in self._partial:
                *lines, self._partial = self._partial.split("\n")
                stamp = time.strftime("%H:%M:%S")
                with _LOG_LOCK:
                    for line in lines:
                        _LOG_TAIL.append(f"{stamp} {line}")
        except Exception:
            self._partial = ""
        return len(text or "")

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self._stream.isatty()
        except Exception:
            return False

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install_log_tap() -> None:
    """Start keeping the tail of stderr/stdout. Safe to call more than once.

    Skipped under pytest. The tap holds a reference to whatever stream it wrapped,
    but pytest swaps sys.stderr per test, so a tap installed during one test would
    keep writing into a stream that later tests have closed -- and quietly lose
    their output. Tests that want the behaviour build a `_Tee` themselves.
    """
    global _TAP_INSTALLED
    if _TAP_INSTALLED or "pytest" in sys.modules:
        return
    _TAP_INSTALLED = True
    try:
        sys.stderr = _Tee(sys.stderr)
        sys.stdout = _Tee(sys.stdout)
    except Exception:
        pass


def log_tail(limit: int = LOG_TAIL_LINES) -> list:
    with _LOG_LOCK:
        lines = list(_LOG_TAIL)
    return lines[-limit:]


def _safe_name(value: Any, fallback: str = "default") -> str:
    text = "".join(c for c in str(value or "") if c.isalnum() or c in "-_")
    return text or fallback


def _read_json(path: Path) -> Optional[Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


# Werkzeug's per-request access log. Hundreds of these per turn (the client
# polls /api/feed every 800ms), and they buried the handful of lines that
# actually say what went wrong. Kept in full in server_log.txt; filtered out of
# the summary a reader sees first.
_ACCESS_LOG = re.compile(r'"(GET|POST|PUT|DELETE|HEAD|OPTIONS) \S+ HTTP/[\d.]+" \d{3}')


def _interesting_log(lines: list) -> list:
    return [line for line in lines if not _ACCESS_LOG.search(line)]


def _tail_lines(path: Path, count: int) -> list:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()[-count:]
    except Exception:
        return []


def _frame_from_client(data_url: str, destination: Path) -> Optional[str]:
    """Write the pixels the client sent. Returns a note, or None if it could not.

    This is the reliable path. A scene can be a generated frame under /images/,
    an authored plate from /api/worlds/<id>/frame, or a moment portrait; making
    the server resolve each URL shape back to a file meant guessing, and the
    first version of this guessed wrong and captured a frame nobody had seen.
    The client re-fetches whatever the layer is using and hands over the bytes.
    """
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        return None
    try:
        import base64
        header, _, payload = data_url.partition(",")
        if not payload or ";base64" not in header:
            return None
        raw = base64.b64decode(payload, validate=False)
        if len(raw) < 64:
            return None
        destination.write_bytes(raw)
        return "the exact pixels the active scene layer was showing"
    except Exception:
        return None


def _frame_on_screen(session_id: str, screen: dict) -> tuple:
    """Fallback: the file behind the picture, when the client sent no pixels.

    Prefers the URL the client read off the active scene layer, because that is
    the frame actually painted -- a turn may already have written a newer file
    that nobody has seen yet, and reporting that one would describe a bug the
    player never saw. Falls back to the newest frame in the session.
    """
    import engine

    named = ""
    for key in ("sceneUrl", "momentImage", "currentImage"):
        raw = str((screen or {}).get(key) or "")
        match = re.search(r"([\w.\-]+\.(?:png|jpg|jpeg|gif|webp))", raw, re.I)
        if match:
            named = match.group(1)
            break

    images = None
    try:
        images = Path(engine._get_image_dir(_safe_name(session_id)))
    except Exception:
        images = None

    if named and images is not None:
        direct = images / named
        if direct.exists():
            return direct, "the frame the active scene layer was showing"
    if named:
        try:
            import api
            found = api._find_image_in_any_session(named)
            if found is not None:
                return Path(found), "the frame on screen (found in another session's images/)"
        except Exception:
            pass
    if images is not None and images.exists():
        try:
            frames = [p for p in images.iterdir()
                      if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
                      and not p.name.endswith("_small.png")]
            if frames:
                newest = max(frames, key=lambda p: p.stat().st_mtime)
                return newest, "newest frame in the session (the on-screen URL did not resolve)"
        except Exception:
            pass
    return None, "no frame could be located"


def _luma(path: Path) -> Optional[dict]:
    """How dark the captured frame is, so "it went black" is a number.

    A black screen is this project's most common and most confusing failure,
    because the still on disk is usually fine and the fault is in what is
    painted over it. Measuring the file separates those two cases immediately.
    """
    try:
        from PIL import Image
        with Image.open(path) as im:
            # tobytes on an 8-bit greyscale image is the raw pixel run, which
            # avoids getdata() (deprecated in Pillow 14) entirely.
            pixels = im.convert("L").resize((64, 64)).tobytes()
        mean = sum(pixels) / float(len(pixels))
        dark = sum(1 for p in pixels if p < 12) / float(len(pixels))
        return {"mean": round(mean, 1), "dark_fraction": round(dark, 3)}
    except Exception:
        return None


def _verdicts(screen: dict, luma: Optional[dict]) -> list:
    """The checks worth making mechanically, phrased as findings.

    Deliberately conservative: each one states what was observed rather than
    guessing a cause, because a confident wrong diagnosis in this codebase has
    cost whole sessions. Absence of a finding is not a clean bill of health.
    """
    out = []
    screen = screen if isinstance(screen, dict) else {}

    if luma and luma["mean"] < 12:
        out.append(f"The frame ON DISK is black (mean luma {luma['mean']}). "
                   "The generation failed, not the paint.")
    elif luma and luma["dark_fraction"] > 0.9:
        out.append(f"The frame on disk is {int(luma['dark_fraction'] * 100)}% near-black "
                   f"(mean luma {luma['mean']}) -- very dark, but not empty.")
    elif luma:
        out.append(f"The frame on disk looks fine (mean luma {luma['mean']}). "
                   "If the screen looked black, the fault is in what was painted over it.")

    for layer in screen.get("layers") or []:
        if layer.get("covering"):
            out.append(f"`{layer.get('sel')}` was opaque and covering the viewport "
                       f"(opacity {layer.get('op')}, z-index {layer.get('z')}). "
                       "Screenshots do not show the video layer -- this does.")

    errors = screen.get("errors") or []
    if errors:
        out.append(f"The client threw {len(errors)} error(s); the first is "
                   f"`{str(errors[0].get('message'))[:160]}`. A JS error mid-turn "
                   "leaves the UI half-built and looks like a freeze.")

    if screen.get("turnActive"):
        out.append("A turn was still in flight when this was captured, so a "
                   "half-drawn screen may be normal rather than stuck. Check the "
                   "elapsed time below against the ~30s a turn takes.")

    if screen.get("gated"):
        out.append("The boot gate was still up (`awaiting-first-scene`): the "
                   "client had not accepted a first painted scene yet.")

    if not screen:
        out.append("No client payload arrived, so only the server side of this "
                   "capture is trustworthy.")
    return out


def _report_markdown(meta: dict, screen: dict, luma: Optional[dict],
                     verdicts: list, frame_note: str, files: list,
                     server_log: list, play_log: list, history: list) -> str:
    screen = screen if isinstance(screen, dict) else {}
    lines = [
        f"# Bug capture {meta['id']}",
        "",
        f"- Captured: {meta['captured_at']}",
        f"- Session: `{meta['session_id']}`",
        f"- Turn: {meta.get('turn', '?')}",
        f"- Renderer: {screen.get('mode') or '?'}",
        f"- Viewport: {screen.get('viewport') or '?'}",
        "",
    ]

    note = str(meta.get("note") or "").strip()
    lines += ["## What the player said", "", f"> {note}" if note
              else "> (no note -- the button was pressed without one)", ""]

    lines += ["## What the checks say", ""]
    lines += [f"- {v}" for v in verdicts] if verdicts else ["- (nothing flagged automatically)"]
    lines += ["", "## The picture", "", f"`frame.png` -- {frame_note}."]
    if luma:
        lines.append(f"Mean luma {luma['mean']}, {int(luma['dark_fraction'] * 100)}% of pixels near-black.")
    lines += ["", "![frame](frame.png)", ""]

    lines += ["## What was on screen", ""]
    lines.append(f"- body class: `{screen.get('cls') or '?'}`")
    lines.append(f"- turn in flight: {bool(screen.get('turnActive'))}")
    tags = screen.get("tags") or []
    if tags:
        lines.append("- scanned objects: " + ", ".join(
            f"`{t.get('label')}`" for t in tags[:12]))
    choices = screen.get("choices") or []
    if choices:
        lines.append("- choices offered:")
        lines += [f"    {i + 1}. {c}" for i, c in enumerate(choices[:8])]
    prose = str(screen.get("prose") or "").strip()
    if prose:
        lines += ["", "Last prose on screen:", "", "```", prose[-1200:], "```"]

    layers = screen.get("layers") or []
    if layers:
        lines += ["", "### Layer stack", "",
                  "Which elements were actually drawing, in the order the "
                  "compositor sees them. A screenshot cannot answer this "
                  "because it does not capture the WebView2 video surface.", ""]
        for layer in layers:
            if layer.get("missing"):
                lines.append(f"- `{layer.get('sel')}` -- element missing")
                continue
            lines.append(
                f"- `{layer.get('sel')}` hidden={layer.get('hidden')} "
                f"opacity={layer.get('op')} z={layer.get('z')} "
                f"display={layer.get('disp')} size={layer.get('rect')}"
                + (f" video={layer.get('vw')}x{layer.get('vh')} "
                   f"readyState={layer.get('readyState')} paused={layer.get('paused')}"
                   if layer.get("readyState") is not None else "")
                + ("  <-- COVERING" if layer.get("covering")
                   else "  <-- this is the picture" if layer.get("intended") else ""))

    errors = screen.get("errors") or []
    if errors:
        lines += ["", "### Client errors", ""]
        for err in errors[:12]:
            lines.append(f"- `{str(err.get('message'))[:200]}`"
                         + (f" ({err.get('source')}:{err.get('line')})" if err.get("source") else ""))

    lines += ["", "## Recent history", ""]
    if history:
        for entry in history:
            action = str(entry.get("action") or entry.get("choice") or "?")[:90]
            text = str(entry.get("narrative") or entry.get("text") or "")[:220]
            lines.append(f"- **{action}** -- {text}")
    else:
        lines.append("- (no history could be read)")

    lines += ["", "## Files in this capture", ""]
    lines += [f"- `{name}` -- {why}" for name, why in files]

    signal = _interesting_log(server_log)
    lines += ["", "## Server log tail", "",
              f"The last {min(len(signal), 200)} lines that were not routine request "
              f"logging, newest last. All {len(server_log)} lines including the "
              f"access log are in `server_log.txt`.", "",
              "```"] + signal[-200:] + ["```"]

    if play_log:
        lines += ["", "## Play log tail", "",
                  "Structured turn events from `logs/play/`.", "",
                  "```"] + play_log[-40:] + ["```"]

    return "\n".join(lines) + "\n"


def capture(session_id: str = "default", screen: Optional[dict] = None,
            note: str = "", frame_data_url: str = "") -> dict:
    """Write one bug folder. Returns a summary; never raises."""
    started = time.time()
    session_id = _safe_name(session_id)
    screen = screen if isinstance(screen, dict) else {}
    problems = []

    bug_id = time.strftime("%Y%m%d_%H%M%S")
    folder = BUGS_DIR / bug_id
    try:
        suffix = 1
        while folder.exists():
            suffix += 1
            folder = BUGS_DIR / f"{bug_id}_{suffix}"
        folder.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return {"ok": False, "error": f"could not create the capture folder: {exc}"}

    files = []

    def _write(name: str, why: str, payload: Any, raw: bool = False) -> None:
        try:
            path = folder / name
            if raw:
                path.write_text(str(payload), encoding="utf-8")
            else:
                path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                                encoding="utf-8")
            files.append((name, why))
        except Exception as exc:
            problems.append(f"could not write {name}: {exc}")

    # The picture, and whether it is black on disk or only on screen.
    luma = None
    frame_note = _frame_from_client(frame_data_url, folder / "frame.png")
    if frame_note:
        files.append(("frame.png", frame_note))
        luma = _luma(folder / "frame.png")
    else:
        frame_path, frame_note = _frame_on_screen(session_id, screen)
        if frame_path is not None:
            try:
                shutil.copy2(frame_path, folder / "frame.png")
                files.append(("frame.png", frame_note))
                luma = _luma(folder / "frame.png")
            except Exception as exc:
                problems.append(f"could not copy the frame: {exc}")
                frame_note = f"copy failed: {exc}"

    # State, history and the structured play log: what produced that frame.
    turn = None
    try:
        import engine
        state = engine.get_state(session_id)
        turn = (state or {}).get("turn_count")
        _write("state.json", "full engine state at the moment of capture", state)
    except Exception as exc:
        problems.append(f"could not read engine state: {exc}")

    history = []
    try:
        import engine
        history_path = Path(engine._get_session_root(session_id)) / "history.json"
        raw_history = _read_json(history_path)
        entries = raw_history if isinstance(raw_history, list) else (raw_history or {}).get("turns") or []
        history = [e for e in entries if isinstance(e, dict)][-4:]
        _write("history_tail.json", "the last four turns that led here", history)
    except Exception as exc:
        problems.append(f"could not read history: {exc}")

    play_log = []
    try:
        import play_log as play_log_mod
        play_log = _tail_lines(play_log_mod.play_log_path(session_id), 80)
        if play_log:
            _write("play_log.jsonl", "structured turn events, newest last",
                   "\n".join(play_log), raw=True)
    except Exception as exc:
        problems.append(f"could not read the play log: {exc}")

    server_log = log_tail()
    if server_log:
        _write("server_log.txt", "tail of everything the server printed",
               "\n".join(server_log), raw=True)
    else:
        problems.append("the server log tail was empty -- install_log_tap() may not be running")

    if screen:
        _write("screen.json", "raw client payload: layers, prose, choices, errors", screen)

    verdicts = _verdicts(screen, luma)
    meta = {
        "id": folder.name,
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": session_id,
        "turn": turn,
        "note": note,
    }
    _write("meta.json", "ids and timings for this capture", meta)

    try:
        (folder / "REPORT.md").write_text(
            _report_markdown(meta, screen, luma, verdicts, frame_note,
                             files + [("REPORT.md", "this summary")],
                             server_log, play_log, history),
            encoding="utf-8")
    except Exception as exc:
        problems.append(f"could not write REPORT.md: {exc}")

    try:
        import play_log as play_log_mod
        play_log_mod.record("bug_capture", session_id,
                            {"bug_id": folder.name, "note": note[:200],
                             "verdicts": verdicts[:4]})
    except Exception:
        pass

    print(f"[BUG] captured {folder.name} in {time.time() - started:.1f}s "
          f"({len(files)} files){' -- ' + '; '.join(problems) if problems else ''}",
          file=sys.stderr, flush=True)

    return {
        "ok": True,
        "id": folder.name,
        "path": str(folder),
        "relative_path": f"bugs/{folder.name}",
        "turn": turn,
        "verdicts": verdicts,
        "files": [name for name, _ in files],
        "problems": problems,
    }


def recent(limit: int = 20) -> list:
    """Captures newest first, for anyone who wants to list them."""
    try:
        if not BUGS_DIR.exists():
            return []
        folders = sorted((p for p in BUGS_DIR.iterdir() if p.is_dir()),
                         key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    except Exception:
        return []
    out = []
    for folder in folders:
        meta = _read_json(folder / "meta.json") or {}
        out.append({
            "id": folder.name,
            "captured_at": meta.get("captured_at"),
            "turn": meta.get("turn"),
            "note": meta.get("note"),
            "relative_path": f"bugs/{folder.name}",
        })
    return out
