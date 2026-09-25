"""Updates: found in the background, applied only when the player says so.

An installed ABYSS (Velopack, docs/plans/DISTRIBUTION_MVP_PLAN.md M4) checks the
public releases repo once per launch, downloads a newer build quietly, and the
start menu then offers UPDATE READY — RESTART. Nothing here ever interrupts a
run: the check is a daemon thread, the download is a daemon thread, and the
restart happens only from POST /api/update/apply, which the start menu sends
when the player presses it. "Later" is always allowed; a downloaded update that
is never applied is applied by Velopack the next time the game starts.

Everything degrades to "not installed" rather than failing: from source, from
the portable zip, or with the velopack package missing, `status()` says so and
the start menu shows nothing.

    boot()      — first thing play.py does (Velopack's install/update hooks
                  run and exit here when the installer invokes the exe)
    start()     — begin the background check (after the window is up)
    status()    — {state, current, available, error}
    apply()     — restart into the downloaded version
"""
from __future__ import annotations

import os
import threading
from typing import Any, Dict, Optional

import app_identity

_lock = threading.Lock()
_state: Dict[str, Any] = {"state": "idle", "current": app_identity.VERSION,
                          "available": None, "notes": "", "error": None}
_manager = None
_pending = None  # UpdateInfo once downloaded


def _velopack():
    try:
        import velopack  # type: ignore
        return velopack
    except Exception:  # noqa: BLE001 — optional at runtime
        return None


def boot() -> None:
    """Velopack's startup hook. Must run before anything else in play.py:
    when the installer runs the exe with --veloapp-* it does its work here
    and exits the process."""
    vp = _velopack()
    if vp is None:
        return
    try:
        vp.App().run()
    except Exception as e:  # noqa: BLE001
        print(f"[update] startup hook failed: {e}")


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def status() -> Dict[str, Any]:
    with _lock:
        return dict(_state)


def _source(vp):
    repo = (os.environ.get("ABYSS_UPDATE_REPO") or app_identity.RELEASES_REPO).strip()
    # A local folder of packed releases (vpk pack --outputDir) is a source too:
    # that is how an update is rehearsed without publishing anything.
    if os.path.isdir(repo):
        return repo
    url = repo if repo.startswith("http") else f"https://github.com/{repo}"
    token = (os.environ.get("ABYSS_UPDATE_TOKEN") or "").strip() or None
    # Pre-releases carry the beta channel; Velopack itself keeps a player on
    # the channel they installed from (--channel at pack time).
    return vp.GithubSource(url, token, True)


def _run() -> None:
    global _manager, _pending
    vp = _velopack()
    if vp is None:
        _set(state="not-installed", error="velopack is not available")
        return
    try:
        _manager = vp.UpdateManager(_source(vp))
        _set(current=_manager.get_current_version())
    except Exception as e:  # noqa: BLE001 — source checkout / portable zip
        _set(state="not-installed", error=str(e)[:200])
        return
    try:
        waiting = _manager.get_update_pending_restart()
        if waiting is not None:
            _pending = waiting
            _set(state="ready", available=waiting.Version, notes=waiting.NotesMarkdown or "")
            return
        _set(state="checking")
        info = _manager.check_for_updates()
        if info is None:
            _set(state="current")
            return
        target = info.TargetFullRelease
        _set(state="downloading", available=target.Version, notes=target.NotesMarkdown or "")
        _manager.download_updates(info)
        _pending = info
        _set(state="ready")
        print(f"[update] {target.Version} downloaded; offered on the start menu")
    except Exception as e:  # noqa: BLE001 — offline, rate limited, repo empty
        _set(state="error", error=str(e)[:200])
        print(f"[update] check failed: {e}")


def start() -> None:
    if os.environ.get("ABYSS_NO_UPDATE_CHECK") == "1":
        _set(state="disabled")
        return
    threading.Thread(target=_run, name="update-check", daemon=True).start()


def apply() -> Optional[str]:
    """Hand the downloaded update to Velopack, which waits for this process to
    exit, applies it, and starts the new version. Returns an error or None;
    the caller then quits the game the normal way (the EXIT path)."""
    if _manager is None or _pending is None or status().get("state") != "ready":
        return "no update is ready"
    try:
        _manager.wait_exit_then_apply_updates(_pending, False, True, None)
    except Exception as e:  # noqa: BLE001
        return str(e)[:200]
    _set(state="applying")
    return None
