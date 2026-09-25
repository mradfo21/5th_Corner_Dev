"""Keep a test run off the authoring data it is testing — whatever the runner.

This used to live in ``conftest.py`` as a pytest fixture, which made the
protection depend on how you happened to start the tests. Every test module in
this repo tells you in its own docstring to run it with
``python3 -m unittest <module> -v``, and unittest never loads conftest.py — so
the documented way to run the tests was the one way the sandbox could not see.

That is not hypothetical. A ``python -m unittest`` run of the editor e2e suites
on 2026-09-17 wrote ``prompts/harness.generic.json`` over the live prompt file
and over ``worlds/world.json`` (taking the Horizon world document and the Level
sheet with it), and blanked ``tunables.json`` — which silently dropped Flipbook
back to its schema default of off, turning every animated turn into a still with
no error anywhere. ``tunables.json`` is gitignored, so there was nothing to
restore from.

So the guard lives here and engages on IMPORT of any authoring store, which is
long before anything writes. ``python -m unittest``, ``pytest``, an IDE runner,
and a bare ``python -c "import engine"`` from inside a test all get the same
sandbox.

HOW THE DETECTION IS SAFE
Being a test run means "a test framework is already in ``sys.modules``". A runner
always imports its own framework before it imports the test module that imports
us, and the app imports neither: ``import engine`` pulls in no ``unittest``, no
``pytest``, no ``_pytest``. If that ever stops being true the app would start
running against a sandbox, so ``test_authoring_sandbox`` pins it.

WHAT IS AND IS NOT PROTECTED
The sandbox is SEEDED WITH COPIES of the real files, so a test that reads shipped
content still finds it — only the writes are contained. Both channels are
covered: module attributes for this interpreter, and ``SOMEWHERE_*`` environment
variables for the suites that launch the app in a SUBPROCESS (Playwright), which
no in-process patching can reach.

Sessions are deliberately left alone, as before: engine resolves them from its
own ROOT, which also anchors prompts/, assets/, templates/ and static/, so
moving it would mean moving the whole app rather than protecting one directory.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent

# (module, attribute, real path, env var a subprocess reads)
_REDIRECTS: Tuple[Tuple[str, str, Path, str], ...] = (
    ("prompts_store", "PROMPTS_PATH",
     ROOT / "prompts" / "simulation_prompts.json", "SOMEWHERE_PROMPTS_PATH"),
    ("prompts_store", "DEFAULTS_PATH",
     ROOT / "prompts" / "simulation_prompts.defaults.json",
     "SOMEWHERE_PROMPTS_DEFAULTS_PATH"),
    ("worlds_store", "WORLDS_DIR", ROOT / "worlds", "SOMEWHERE_WORLDS_DIR"),
    ("experience_store", "EXPERIENCES_DIR", ROOT / "experiences",
     "SOMEWHERE_EXPERIENCES_DIR"),
    # tunables.json is authoring data too — it is where Flipbook, the frame
    # count and the cutscene hold actually live, and tunables.clear() writes
    # {} over the lot.
    ("tunables", "STORE", ROOT / "tunables.json", "SOMEWHERE_TUNABLES_PATH"),
)

# Stores a test must not write to, and does not need the real contents of:
# redirected to an EMPTY folder. Characters are the player's (characters.py) —
# a suite that creates one must never put it on their roster, and copying a
# roster of 2K renders into every sandbox would cost more than it tells.
_EMPTY_REDIRECTS: Tuple[Tuple[str, str, Path, str], ...] = (
    ("characters", "CHARACTERS_DIR", ROOT / "characters", "SOMEWHERE_CHARACTERS_DIR"),
)

_TEST_FRAMEWORKS = ("pytest", "_pytest", "unittest")

_engaged: Optional[Path] = None
_restore: List[Tuple[str, Any, Any]] = []


def test_framework_in_play() -> str:
    """The test framework driving this process, or "" for the real app."""
    for name in _TEST_FRAMEWORKS:
        if name in sys.modules:
            return name
    return ""


def engaged_at() -> Optional[Path]:
    """The sandbox directory, or None when writes are going to the real files."""
    return _engaged


def engage(reason: str = "") -> Path:
    """Point every authoring path at a seeded copy. Idempotent.

    Returns the sandbox root. Safe to call from a module that is itself
    mid-import: the environment variables are what a store reads to compute its
    path, and attributes are only patched on modules that finished importing.
    """
    global _engaged
    if _engaged is not None:
        return _engaged

    sandbox = Path(tempfile.mkdtemp(prefix="somewhere-authoring-"))
    _engaged = sandbox

    for mod_name, attr, real, env_var in _REDIRECTS:
        dest = sandbox / mod_name / attr.lower()
        if real.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copytree(real, dest, dirs_exist_ok=True)
        else:
            dest.mkdir(parents=True, exist_ok=True)
            dest = dest / real.name
            if real.is_file():
                shutil.copyfile(real, dest)

        _restore.append(("env", env_var, os.environ.get(env_var)))
        os.environ[env_var] = str(dest)

        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, attr):
            _restore.append(("attr", (mod, attr), getattr(mod, attr)))
            setattr(mod, attr, dest)

    for mod_name, attr, _real, env_var in _EMPTY_REDIRECTS:
        dest = sandbox / mod_name / attr.lower()
        dest.mkdir(parents=True, exist_ok=True)
        _restore.append(("env", env_var, os.environ.get(env_var)))
        os.environ[env_var] = str(dest)
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, attr):
            _restore.append(("attr", (mod, attr), getattr(mod, attr)))
            setattr(mod, attr, dest)

    reload_live_prompts()
    # ASCII only: this lands on a Windows console whose default codec is cp1252,
    # and a subprocess reading it back as UTF-8 chokes on an em dash.
    print(f"[SANDBOX] authoring data redirected to {sandbox} "
          f"({reason or 'test run'}). The real prompts, worlds, experiences "
          f"and tunables are not writable from here.", flush=True)
    return sandbox


def release() -> None:
    """Put the real paths back and delete the sandbox."""
    global _engaged
    if _engaged is None:
        return
    for kind, target, value in reversed(_restore):
        if kind == "env":
            if value is None:
                os.environ.pop(target, None)
            else:
                os.environ[target] = value
        else:
            setattr(target[0], target[1], value)
    _restore.clear()
    reload_live_prompts()
    shutil.rmtree(_engaged, ignore_errors=True)
    _engaged = None


def reload_live_prompts() -> None:
    """PROMPTS caches the file's mtime, so it has to be told the file moved."""
    try:
        import prompts_store
        prompts_store.PROMPTS._mtime = None
        prompts_store.PROMPTS._last_check = 0.0
        prompts_store.PROMPTS._reload(force=True)
    except Exception:
        pass


def guard() -> Optional[Path]:
    """Engage the sandbox if a test framework is driving. Called at the top of
    every authoring store, so importing any of them is enough."""
    if _engaged is not None:
        return _engaged
    framework = test_framework_in_play()
    if not framework:
        return None
    sandbox = engage(f"{framework} is driving this process")
    # No session-end hook exists on the unittest path, so clean up on exit
    # rather than leaving a temp tree behind per run.
    atexit.register(lambda: shutil.rmtree(sandbox, ignore_errors=True))
    return sandbox
