"""Install version N from its real Setup.exe, then watch it update itself to N+1.

    python tools/release_local.py v0.1.0-beta.1 --skip-gate
    copy Releases\\*-Setup.exe somewhere          # N's installer
    python tools/release_local.py v0.1.0-beta.2 --skip-gate --skip-smoke
    python tools/rehearse_update.py <N's Setup.exe>

The M4 "done means" as a script (docs/plans/DISTRIBUTION_MVP_PLAN.md):

  1. silent install into a scratch folder (the real installer, not a copy)
  2. the installed app starts; /api/health says version N
  3. a run is started, so there is a save to keep
  4. the updater finds N+1 in Releases/ and downloads it (/api/update → ready)
  5. POST /api/update/apply — what the start menu's UPDATE READY does
  6. the app comes back on its own; /api/health says N+1
  7. the save is still there, and its state file is unchanged

Everything runs against scratch folders: APPDATA (so no real keys, saves or
characters are read or written), the install folder, and no API key at all (so
the game is in offline mode and spends nothing). The app is uninstalled at
the end. Velopack still writes an uninstall entry and shortcuts while it is
installed; the uninstall takes them back.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import app_identity  # noqa: E402

RELEASES = ROOT / "Releases"


def say(msg: str) -> None:
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


def call(port: int, token: str, path: str, body: dict | None = None, timeout: float = 30):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=None if body is None else json.dumps(body).encode(),
        method="GET" if body is None else "POST",
        headers={"X-Launch-Token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def wait_launch(launch: Path, not_pid: int | None, seconds: float) -> dict:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            info = json.loads(launch.read_text(encoding="utf-8"))
            if info.get("pid") != not_pid:
                call(info["port"], info["token"], "/api/health", timeout=3)
                return info
        except Exception:
            pass
        time.sleep(1)
    raise TimeoutError(f"the app never came up ({launch})")


def _button_on_screen(shot: Path) -> str:
    """Attach to the installed window over CDP and look for the button the
    player would press. The API saying "ready" is not the same thing."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        return "unchecked (playwright missing)"
    try:
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp("http://127.0.0.1:9334")
            page = next(p for c in b.contexts for p in c.pages if "standalone" in p.url)
            # With no key, ACCOUNT opens by itself and the start menu sits
            # behind it (visibility: hidden). Close it the way a player does:
            # "not [hidden]" is not "on screen" — the first version of this
            # check passed while the screenshot showed only ACCOUNT.
            if page.is_visible("#age-yes"):     # a packaged build's first launch asks 18+
                page.click("#age-yes")
            if page.is_visible("#acct-close"):
                page.click("#acct-close")
            page.wait_for_selector("#start-update", state="visible", timeout=30000)
            text = page.eval_on_selector("#start-update", "e => e.textContent")
            tag = page.eval_on_selector("#start-build", "e => e.textContent")
            page.screenshot(path=str(shot))
            return f"yes — {text!r}, tag {tag!r}"
    except Exception as e:  # noqa: BLE001
        return f"NO ({type(e).__name__}: {str(e)[:120]})"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    setup = Path(argv[0]).resolve()
    scratch = Path(tempfile.mkdtemp(prefix="abyss-rehearse-"))
    appdata, install = scratch / "appdata", scratch / "install"
    appdata.mkdir()
    env = {k: v for k, v in os.environ.items()
           if not k.endswith("_API_KEY") and not k.startswith("SOMEWHERE_")}
    env.update(APPDATA=str(appdata), ABYSS_UPDATE_REPO=str(RELEASES),
               WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="--remote-debugging-port=9334")
    data = appdata / app_identity.DATA_DIR_NAME
    launch = data / "logs" / "launch.json"
    ok = False
    try:
        say(f"1. installing {setup.name} -> {install}")
        subprocess.run([str(setup), "--silent", "--installto", str(install),
                        "--log", str(scratch / "setup.log")], env=env, check=True, timeout=600)

        try:
            info = wait_launch(launch, None, 45)
        except TimeoutError:
            # A silent install may not start the app; start it the way the
            # Start Menu shortcut does (the root stub, not current/).
            say("   (the installer did not start it; starting the shortcut's target)")
            subprocess.Popen([str(install / f"{app_identity.APP_NAME}.exe")], env=env)
            info = wait_launch(launch, None, 180)
        port, token = info["port"], info["token"]
        h = call(port, token, "/api/health")
        v1 = h["build"]["version"]
        say(f"2. installed app is up on :{port}, version {v1}")

        call(port, token, "/api/reset", {"session_id": "rehearse"}, timeout=120)
        state = data / "sessions" / "rehearse" / "state.json"
        # The opening keeps writing the run for a moment after /api/reset
        # answers; fingerprint the save once it has been still for 10 s.
        quiet_since, last = time.time(), None
        while time.time() - quiet_since < 10:
            m = state.stat().st_mtime_ns if state.exists() else None
            if m != last:
                last, quiet_since = m, time.time()
            time.sleep(1)
        before = hashlib.sha256(state.read_bytes()).hexdigest() if state.exists() else None
        say(f"3. a run exists: {state.exists()} ({state})")

        deadline, st = time.time() + 600, {}
        while time.time() < deadline:
            t0 = time.time()
            try:
                st = call(port, token, "/api/update", timeout=5)
            except Exception as e:  # noqa: BLE001 — a frozen server is the finding
                say(f"   /api/update did not answer in 5 s ({type(e).__name__}) — the server is blocked")
                time.sleep(2)
                continue
            if time.time() - t0 > 1:
                say(f"   /api/update took {time.time() - t0:.1f}s")
            if st.get("state") in ("ready", "error", "current", "not-installed"):
                break
            time.sleep(3)
        say(f"4. updater: {st.get('state')} -> {st.get('available')} {st.get('error') or ''}")
        if st.get("state") != "ready":
            return 1

        shot = scratch / "start_menu_update_ready.png"
        seen = _button_on_screen(shot)
        say(f"   the start menu shows it: {seen}  ({shot})")
        if shot.exists():
            keep = ROOT / "_claude_pull" / "update_ready.png"
            keep.parent.mkdir(exist_ok=True)
            shutil.copy2(shot, keep)

        say("5. applying (what UPDATE READY — RESTART does)")
        call(port, token, "/api/update/apply", {})
        info2 = wait_launch(launch, info.get("pid"), 300)
        v2 = call(info2["port"], info2["token"], "/api/health")["build"]["version"]
        say(f"6. back up on :{info2['port']}, version {v2}")

        after = hashlib.sha256(state.read_bytes()).hexdigest() if state.exists() else None
        kept = before is not None and after == before
        say(f"7. the save survived the update: {kept}")
        ok = v2 == st.get("available") and v2 != v1 and kept
        try:
            call(info2["port"], info2["token"], "/api/shutdown", {})
        except Exception:
            pass
        return 0 if ok else 1
    finally:
        time.sleep(3)
        upd = install / "Update.exe"
        if upd.exists():
            subprocess.run([str(upd), "--uninstall", "--silent"], env=env, timeout=300)
            say(f"uninstalled ({'folder gone' if not (install / 'current').exists() else 'folder left'})")
        say(f"{'PASS' if ok else 'FAIL'} — scratch kept at {scratch}" if not ok else "PASS")
        if ok:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
