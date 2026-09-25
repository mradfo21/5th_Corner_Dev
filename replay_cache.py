"""Record every generation once; replay it for free, instantly, forever.

The verification loop here is the expensive part of engineering this game. A turn
takes ~30 seconds and about four cents, and because every frame and every line of
prose is generated fresh, two runs of *identical code* produce different output.
That is the real problem: when something looks wrong after a change, there is no
way to tell whether the change broke it or the model simply rolled differently.
A six-turn check costs five minutes and a quarter and still proves nothing.

So: while playing normally, write down what came back from every model call,
keyed by everything that went into it. Then a verification run can be told to
answer from those recordings instead of the network. It costs nothing, returns
immediately, and -- the point -- is *deterministic*, so a frame that changes
means the code changed.

Recording is on by default and does not alter what play does: the real call
still happens, and its result is what the game uses. Replay is opt-in, because
real generation is the product; this only exists to check it.

    SOMEWHERE_REPLAY=record   # default. play normally, remember everything
    SOMEWHERE_REPLAY=replay   # answer from the recordings; call out on a miss
    SOMEWHERE_REPLAY=off      # do not intercept anything at all

Nothing here may change the outcome of a call it does not have an answer for. A
miss falls through to the real function, is counted, and is reported -- a replay
run that silently invents a blank frame would be worse than no replay at all.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent
import paths as _paths  # where the game writes (M2): the repo from source, %APPDATA%/ABYSS built
CACHE_DIR = _paths.data_root() / ".cache" / "replay"

MODE_OFF, MODE_RECORD, MODE_REPLAY = "off", "record", "replay"

_LOCK = threading.Lock()
_INSTALLED = False
_STATS = {
    "hits": 0,          # answered from a recording: free and instant
    "misses": 0,        # no recording, so the real call happened
    "records": 0,       # new recordings written
    "seconds_saved": 0.0,
    "miss_detail": [],  # what was asked for that we did not have
}

# Arguments that legitimately differ between two runs of the same turn and must
# not take part in the key. `output_dir` is a per-session (sometimes per-temp-dir)
# destination, so including it would make every key unique and the cache useless.
VOLATILE_ARGS = {"output_dir", "session_id", "session", "out_dir", "dest", "destination"}

# How much of the prompt to keep alongside a recording. Enough to recognise what
# a miss was asking for without storing the whole prompt corpus twice.
EXCERPT = 220


def generation_is_live() -> bool:
    """Is this process actually able to generate, or is it a stub?

    `engine._ask` answers with a placeholder ("You are still in <place>. The next
    move is yours.") whenever LLM_ENABLED is false, and any process that imports
    api.py without keys -- a test, a tool, a --mock run -- is in exactly that
    state. Recording those answers poisons the library: a later replay serves the
    placeholder as if it were prose, and the run silently falls back to defaults
    with nothing in the log to say why. Observed happening, hence this guard.
    """
    try:
        import engine
        return bool(getattr(engine, "LLM_ENABLED", False))
    except Exception:
        return False


def looks_like_a_stub(result: Any) -> bool:
    """The shape of engine's LLM-disabled answer, wherever it came from."""
    if not isinstance(result, str):
        return False
    return "The next move is yours." in result and "You are still in" in result


def mode() -> str:
    # A packaged build defaults to OFF: recording keeps every paid answer on
    # disk with no bound, which is what makes a studio replay free and what
    # would slowly fill a player's drive (M2). SOMEWHERE_REPLAY still wins.
    import sys
    default = MODE_OFF if getattr(sys, "frozen", False) else MODE_RECORD
    raw = str(os.getenv("SOMEWHERE_REPLAY", default)).strip().lower()
    if raw in ("off", "0", "no", "false", "none"):
        return MODE_OFF
    if raw in ("replay", "play", "reuse"):
        return MODE_REPLAY
    return MODE_RECORD


def _hash_file(path: Any) -> Optional[str]:
    """Content hash of a file argument.

    Reference images have to be keyed by their *pixels*, not their filename. The
    filenames carry random ids, so keying on them would never hit -- and worse,
    two different frames can occupy the same temp path within a run.
    """
    try:
        p = Path(str(path))
        if not p.is_file():
            return None
        h = hashlib.sha1()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(262144), b""):
                h.update(chunk)
        return "file:" + h.hexdigest()
    except Exception:
        return None


def _stable(value: Any, depth: int = 0) -> Any:
    """Turn an argument into something JSON-stable, hashing files by content."""
    if depth > 6:
        return "<deep>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (str, os.PathLike)):
        text = str(value)
        # Only pay for a hash when the string plausibly names an image on disk;
        # prompts are long strings too and must be keyed as themselves.
        if len(text) < 512 and ("/" in text or "\\" in text) and Path(text).is_file():
            hashed = _hash_file(text)
            if hashed:
                return hashed
        return text
    if isinstance(value, (list, tuple)):
        return [_stable(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {str(k): _stable(v, depth + 1) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    return f"<{type(value).__name__}>"


def _key(kind: str, func: Callable, args: tuple, kwargs: dict) -> tuple:
    """One key for one call, plus the excerpt that names it in a report.

    Bound through the real signature so a positional call and a keyword call to
    the same function land on the same key -- the engine does both.
    """
    try:
        bound = inspect.signature(func).bind_partial(*args, **kwargs)
        bound.apply_defaults()
        supplied = dict(bound.arguments)
    except Exception:
        supplied = {f"arg{i}": v for i, v in enumerate(args)}
        supplied.update(kwargs)

    payload = {name: _stable(value) for name, value in supplied.items()
               if name not in VOLATILE_ARGS}
    blob = json.dumps({"kind": kind, "args": payload}, sort_keys=True, default=str)
    excerpt = ""
    for candidate in ("prompt", "text", "instruction", "caption"):
        if isinstance(supplied.get(candidate), str) and supplied[candidate].strip():
            excerpt = supplied[candidate].strip()[:EXCERPT]
            break
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest(), excerpt, payload


def _entry_dir(kind: str, key: str) -> Path:
    # Two levels of fan-out: a long session writes thousands of entries and a
    # single flat directory of those is slow to list on Windows.
    return CACHE_DIR / kind / key[:2] / key


def _load(kind: str, key: str) -> Optional[dict]:
    try:
        meta_path = _entry_dir(kind, key) / "meta.json"
        if not meta_path.is_file():
            return None
        with meta_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _note_miss(kind: str, excerpt: str, payload: Optional[dict] = None) -> None:
    with _LOCK:
        _STATS["misses"] += 1
        index = _STATS["misses"]
        if len(_STATS["miss_detail"]) < 40:
            _STATS["miss_detail"].append({"kind": kind, "asked_for": excerpt})
    # Keep the whole key payload for a miss. "27% hit rate" on its own is not
    # actionable -- the useful question is *which argument* differed from the
    # recording, and that can only be answered by comparing the two payloads.
    if payload is None:
        return
    try:
        folder = CACHE_DIR / "_misses"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / f"{index:04d}_{kind}.json").open("w", encoding="utf-8") as fh:
            json.dump({"kind": kind, "excerpt": excerpt, "args": payload},
                      fh, indent=2, ensure_ascii=False, default=str)
    except Exception:
        pass


# ── recording and replaying a plain value (text, choices, vision) ────────────

def _record_value(kind: str, key: str, excerpt: str, result: Any, elapsed: float,
                  payload: Optional[dict] = None) -> None:
    try:
        folder = _entry_dir(kind, key)
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "meta.json").open("w", encoding="utf-8") as fh:
            # `args` is the key payload. Kept so a later miss can be diffed
            # against it -- knowing a run missed is useless without knowing
            # which argument moved.
            json.dump({"kind": kind, "key": key, "excerpt": excerpt,
                       "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       # Which mode wrote this. An entry written during a replay
                       # run is a *miss that fell through*, so it must not be
                       # treated as evidence of what the recording session did --
                       # diffing a miss against it always shows a perfect match
                       # and explains nothing.
                       "during_mode": mode(),
                       "seconds": round(elapsed, 2), "kind_of_result": "value",
                       "args": payload, "result": result},
                      fh, ensure_ascii=False, default=str)
        with _LOCK:
            _STATS["records"] += 1
    except Exception as exc:
        print(f"[REPLAY] could not record {kind}: {exc}", file=sys.stderr, flush=True)


def _wrap_value(kind: str, func: Callable) -> Callable:
    def wrapper(*args, **kwargs):
        current = mode()
        key, excerpt, payload = _key(kind, func, args, kwargs)
        if current == MODE_REPLAY:
            found = _load(kind, key)
            if found is not None and "result" in found:
                with _LOCK:
                    _STATS["hits"] += 1
                    _STATS["seconds_saved"] += float(found.get("seconds") or 0.0)
                return found["result"]
            _note_miss(kind, excerpt, payload)
        started = time.time()
        result = func(*args, **kwargs)
        elapsed = time.time() - started
        # Only worth remembering a real answer. A None or an empty string is a
        # failure, and caching failures would make a replay run reproduce them
        # forever with no way to tell why. A stub is worse than a failure,
        # because it looks like prose.
        if (result is not None and result != "" and result != []
                and not looks_like_a_stub(result) and generation_is_live()):
            _record_value(kind, key, excerpt, result, elapsed, payload)
        return result

    wrapper._original = func
    wrapper._replay_kind = kind
    try:
        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        wrapper.__wrapped__ = func
    except Exception:
        pass
    return wrapper


# ── recording and replaying an image (a file on disk plus its path) ──────────

def _record_image(kind: str, key: str, excerpt: str, returned: Any, elapsed: float,
                  payload: Optional[dict] = None) -> None:
    """Keep the bytes as well as the path.

    The generators write a PNG and return its path. Session directories get swept
    and temp directories vanish, so a recording that stored only the path would
    rot; keeping the bytes means a replay can put the frame back wherever this
    run wants it.
    """
    try:
        source = Path(str(returned))
        if not source.is_file():
            # Some paths are returned relative to the repo root.
            alt = ROOT / str(returned)
            if not alt.is_file():
                return
            source = alt
        folder = _entry_dir(kind, key)
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, folder / "frame.png")
        with (folder / "meta.json").open("w", encoding="utf-8") as fh:
            json.dump({"kind": kind, "key": key, "excerpt": excerpt,
                       "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "during_mode": mode(),
                       "seconds": round(elapsed, 2), "kind_of_result": "image",
                       "filename": source.name, "args": payload,
                       "returned_path": str(returned)},
                      fh, ensure_ascii=False, default=str)
        with _LOCK:
            _STATS["records"] += 1
    except Exception as exc:
        print(f"[REPLAY] could not record {kind} image: {exc}", file=sys.stderr, flush=True)


def _replay_image(found: dict, kwargs: dict, args: tuple, func: Callable) -> Optional[str]:
    """Put the recorded frame where this run expects it and return that path.

    The rest of the pipeline pins this file, runs vision over it and uses it as
    the next img2img reference, so it has to be a real file in the real place --
    not a path into the cache.
    """
    folder = Path(found["_folder"])
    source = folder / "frame.png"
    if not source.is_file():
        return None
    filename = found.get("filename") or "replayed.png"

    destination_dir = None
    try:
        bound = inspect.signature(func).bind_partial(*args, **kwargs)
        bound.apply_defaults()
        raw = bound.arguments.get("output_dir")
        if raw:
            destination_dir = Path(str(raw))
    except Exception:
        destination_dir = None
    if destination_dir is None:
        destination_dir = _paths.data_root() / "images"
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
        target = destination_dir / filename
        shutil.copy2(source, target)
        return str(target)
    except Exception:
        return None


def _wrap_image(kind: str, func: Callable) -> Callable:
    def wrapper(*args, **kwargs):
        current = mode()
        key, excerpt, payload = _key(kind, func, args, kwargs)
        if current == MODE_REPLAY:
            found = _load(kind, key)
            if found is not None:
                found["_folder"] = str(_entry_dir(kind, key))
                replayed = _replay_image(found, kwargs, args, func)
                if replayed:
                    with _LOCK:
                        _STATS["hits"] += 1
                        _STATS["seconds_saved"] += float(found.get("seconds") or 0.0)
                    return replayed
            _note_miss(kind, excerpt, payload)
        started = time.time()
        result = func(*args, **kwargs)
        elapsed = time.time() - started
        if result and generation_is_live():
            _record_image(kind, key, excerpt, result, elapsed, payload)
        return result

    wrapper._original = func
    wrapper._replay_kind = kind
    try:
        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        wrapper.__wrapped__ = func
    except Exception:
        pass
    return wrapper


# ── installation ─────────────────────────────────────────────────────────────
#
# Patched onto the modules at runtime rather than written into them. engine.py is
# 16,000 lines and the seams are called from dozens of places inside it; wrapping
# the module attribute catches every one of those call sites without editing any
# of them, which is also how the existing tests stub these functions out.

def install() -> dict:
    """Wrap the model-call seams. Safe to call more than once."""
    global _INSTALLED
    if _INSTALLED or mode() == MODE_OFF:
        return {"installed": False, "mode": mode()}

    wrapped = []

    def patch(module, name: str, kind: str, image: bool = False) -> None:
        try:
            original = getattr(module, name, None)
            if original is None or getattr(original, "_replay_kind", None):
                return
            setattr(module, name, (_wrap_image if image else _wrap_value)(kind, original))
            wrapped.append(f"{module.__name__}.{name}")
        except Exception as exc:
            print(f"[REPLAY] could not wrap {name}: {exc}", file=sys.stderr, flush=True)

    try:
        import engine
        # Every text generation in the game funnels through _ask, including the
        # multimodal vision calls, so this one seam covers the prose, the briefs
        # and the schema-constrained asks.
        patch(engine, "_ask", "ask")
        patch(engine, "_vision_analyze_all", "vision")
        patch(engine, "_vision_describe", "vision_describe")
    except Exception as exc:
        print(f"[REPLAY] engine seams unavailable: {exc}", file=sys.stderr, flush=True)

    try:
        import gemini_image_utils
        patch(gemini_image_utils, "generate_with_gemini", "image", image=True)
        patch(gemini_image_utils, "generate_gemini_img2img", "img2img", image=True)
    except Exception as exc:
        print(f"[REPLAY] image seams unavailable: {exc}", file=sys.stderr, flush=True)

    try:
        # Choices bypass engine._ask and call Gemini over HTTP directly, so they
        # are their own billed path and need their own seam.
        import choices
        patch(choices, "generate_choices", "choices")
    except Exception as exc:
        print(f"[REPLAY] choices seam unavailable: {exc}", file=sys.stderr, flush=True)

    _INSTALLED = True
    print(f"[REPLAY] mode={mode()} wrapped={len(wrapped)} seams -> {CACHE_DIR}",
          file=sys.stderr, flush=True)
    return {"installed": True, "mode": mode(), "seams": wrapped}


def stats() -> dict:
    """What the current process has hit, missed and recorded.

    `hit_rate` is the number that matters for a verification run: anything below
    1.0 means part of that run was freshly generated and therefore not
    reproducible, and the misses say which part.
    """
    with _LOCK:
        snapshot = dict(_STATS)
        snapshot["miss_detail"] = list(_STATS["miss_detail"])
    looked_up = snapshot["hits"] + snapshot["misses"]
    snapshot["mode"] = mode()
    snapshot["lookups"] = looked_up
    snapshot["hit_rate"] = round(snapshot["hits"] / looked_up, 3) if looked_up else None
    snapshot["seconds_saved"] = round(snapshot["seconds_saved"], 1)
    snapshot["deterministic"] = bool(looked_up) and snapshot["misses"] == 0
    return snapshot


def reset_stats() -> None:
    """Zero the counters so one run's hit rate is not another's."""
    with _LOCK:
        _STATS.update({"hits": 0, "misses": 0, "records": 0, "seconds_saved": 0.0})
        _STATS["miss_detail"] = []


def library() -> dict:
    """What has been recorded so far, by kind."""
    out = {"path": str(CACHE_DIR), "kinds": {}, "entries": 0, "bytes": 0}
    try:
        if not CACHE_DIR.exists():
            return out
        for kind_dir in CACHE_DIR.iterdir():
            if not kind_dir.is_dir():
                continue
            count = 0
            size = 0
            for meta in kind_dir.rglob("meta.json"):
                count += 1
                try:
                    size += meta.stat().st_size
                    frame = meta.with_name("frame.png")
                    if frame.is_file():
                        size += frame.stat().st_size
                except OSError:
                    pass
            out["kinds"][kind_dir.name] = {"entries": count, "bytes": size}
            out["entries"] += count
            out["bytes"] += size
    except Exception:
        pass
    return out
