#!/usr/bin/env python3
"""ABYSS (SOMEWHERE in the code) - play it.

    python play.py                 # borderless fullscreen, real backends
    python play.py --windowed      # a normal resizable window instead
    python play.py --mock          # fully offline, no API keys, no network
    python play.py --browser       # skip the native window, use your browser

This is the one entry point for playing. `run_local.py` still exists because the
end-to-end test suites spawn it as a subprocess and depend on its exact flags;
it is the bare dev server. This is the game.

What it does that a browser tab does not: picks a free port so it never fights a
dev server you left running, holds a title card up while the engine warms (that
first import is a couple of seconds, and a white flash would break the mood),
then hands over to a window with no address bar, no tabs and no chrome.

    F11   toggle fullscreen
    Esc   leave fullscreen
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

# Frozen by PyInstaller the modules live beside the executable rather than beside
# this file, and the working directory is wherever the user double-clicked from.
# Everything in this app resolves its data with Path(__file__).parent, so the one
# thing that has to be true is that we are *in* the bundle before importing.
FROZEN = getattr(sys, "frozen", False)
ROOT = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app_identity

TITLE = app_identity.LEGACY_DATA_DIR_NAME  # the %APPDATA% folder (keys, characters, account) until M2 moves it
NAME = app_identity.APP_NAME               # what the player sees: the window, the dialogs

# Directories the game writes into. Bundled builds ship them empty; stamp_factory
# in tools/ship_layout.py creates the same set beside the exe.
WRITABLE = ("sessions", "logs", "archives", "worlds", "experiences", "levels",
            "lore/images", "lore/text", "assets/references", "assets/music",
            "playtest_results")


def _prepare_writable() -> None:
    for rel in WRITABLE:
        (ROOT / rel).mkdir(parents=True, exist_ok=True)


def _capture_output() -> None:
    """A windowed build has no console, so give the output somewhere to go.

    Without this, `print` raises on a None stdout under pythonw/PyInstaller and
    any traceback dies with the process, leaving a silent failure to debug.
    """
    if not FROZEN:
        return
    import safe_log  # rotates at 10 MB and blanks key-shaped strings (M1)

    log = ROOT / "logs" / "somewhere.log"
    try:
        stream = safe_log.RotatingRedactingStream(log)
    except OSError:
        return
    sys.stdout = sys.stderr = stream
    stream.write(f"\n{'=' * 60}\n{time.strftime('%Y-%m-%d %H:%M:%S')}  "
                 f"{NAME} {app_identity.VERSION}\n")

# Shown while the engine imports. Dark screen + the mint bar — no wordmark.
# The start menu is where ABYSS appears, once. The status phrase
# "warming the engine" is load-bearing: the error path replaces that exact
# string and unhides #s.
SPLASH = """
<!doctype html><meta charset="utf-8"><title>ABYSS</title>
<style>
  html,body{height:100%;margin:0;overflow:hidden;background:#020504}
  body{display:flex;align-items:center;justify-content:center}
  .bar{width:190px;height:2px;background:rgba(216,240,228,.10);overflow:hidden}
  .bar i{display:block;height:100%;width:38%;background:#8effc1;
         animation:sweep 1.5s cubic-bezier(.5,0,.5,1) infinite}
  #s{display:none;margin:18px 0 0;font:14px/1.4 system-ui,sans-serif;
     color:rgba(216,240,228,.55);text-align:center}
  #s.err{display:block}
  @keyframes sweep{0%{transform:translateX(-100%)}100%{transform:translateX(280%)}}
  @media (prefers-reduced-motion:reduce){.bar i{animation:none;width:100%}}
</style>
<div><p id="s">warming the engine</p><div class="bar"><i></i></div></div>
"""


def _mark_this_boot() -> str:
    """A unique stamp for this launch so HTML/JS/CSS cannot be served stale."""
    boot = str(int(time.time()))
    os.environ["SOMEWHERE_BOOT"] = boot
    return boot


def _stop_other_play_instances() -> int:
    """Close leftover RUN/PLAY and run_local windows so this launch is live.

    A leftover ``run_local.py`` on :5020 used to keep serving yesterday's JS
    while this window thought it was the current build. Set
    SOMEWHERE_KEEP_OTHERS=1 to skip.

    Only servers started from THIS checkout (its absolute path in the command
    line). It used to take any ``run_local.py`` on the machine, and with one
    git worktree per session that meant launching the game in one killed the
    server another session's e2e suite was driving — it did, on 2026-09-25.
    A launch now picks a free port, so a leftover elsewhere cannot serve this
    window stale JS anyway.
    """
    if os.environ.get("SOMEWHERE_KEEP_OTHERS", "").strip().lower() in ("1", "true", "yes"):
        return 0
    my_pid = os.getpid()
    play = str(ROOT / "play.py").replace("/", "\\").lower()
    local = str(ROOT / "run_local.py").replace("/", "\\").lower()
    killed = 0
    try:
        if sys.platform == "win32":
            import json
            import subprocess
            raw = subprocess.check_output(
                [
                    "powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter "
                    "\"Name = 'python.exe' OR Name = 'pythonw.exe'\" | "
                    "Where-Object { $_.CommandLine -match 'play\\.py' "
                    "-or $_.CommandLine -match 'run_local\\.py' } | "
                    "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress",
                ],
                text=True, timeout=8,
            ).strip()
            if not raw:
                return 0
            rows = json.loads(raw)
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                pid = int(row.get("ProcessId") or 0)
                cmd = str(row.get("CommandLine") or "").replace("/", "\\").lower()
                if pid in (0, my_pid):
                    continue
                kind = "play.py" if play in cmd else (
                    "run_local.py" if local in cmd else "")
                if not kind:
                    continue
                try:
                    os.kill(pid, 9)
                    killed += 1
                    print(f"[play] stopped leftover {kind} (pid {pid})")
                except OSError:
                    pass
        else:
            import subprocess
            raw = subprocess.check_output(["ps", "-ax", "-o", "pid=,command="], text=True)
            for line in raw.splitlines():
                line = line.strip()
                if "play.py" not in line and "run_local.py" not in line:
                    continue
                parts = line.split(None, 1)
                pid = int(parts[0])
                cmd = parts[1] if len(parts) > 1 else ""
                if pid == my_pid:
                    continue
                if str(ROOT) not in cmd:
                    continue
                kind = "play.py" if str(ROOT / "play.py") in cmd else "run_local.py"
                try:
                    os.kill(pid, 9)
                    killed += 1
                    print(f"[play] stopped leftover {kind} (pid {pid})")
                except OSError:
                    pass
    except Exception as e:  # noqa: BLE001
        print(f"[play] could not reap leftover servers: {e}")
    return killed


def free_port(preferred: int | None = None) -> int:
    """A port nothing is listening on, preferring the one asked for."""
    if preferred:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", preferred)) != 0:
                return preferred
        print(f"[play] port {preferred} is busy, picking another")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_health(url: str, timeout_s: float = 90.0, headers: dict | None = None) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}),
                                        timeout=2):
                return True
        except Exception:
            time.sleep(0.25)
    return False


def start_server(port: int, mock: bool, backend: str | None, token: str) -> str:
    """Boot the same Flask app production runs, on a background thread."""
    import run_local

    launch = ["--no-browser", "--port", str(port)]
    env_file = ROOT / ".env"
    if env_file.is_file():
        launch += ["--config", str(env_file)]
    args = run_local.parse_args(
        launch
        + (["--mock"] if mock else [])
        + (["--backend", backend] if backend else [])
    )
    if args.config:
        run_local._load_env_file(Path(args.config))
    os.environ.setdefault("FLASK_DEBUG", "0")
    os.environ["PORT"] = str(port)
    resolved = run_local.configure_backend(args)

    import api

    # Arm the EXIT button and the local KEYS pane. Hosted deployments never
    # call these, so a visitor cannot stop the process or write API keys.
    api.enable_shutdown()
    api.enable_local_keys()
    # And answer nobody but this window (local_guard.py): no web page on the
    # machine can reach the run, the keys, or the EXIT button.
    import local_guard
    local_guard.arm(token, port)
    local_guard.write_launch_file(ROOT, token, port)

    threading.Thread(
        target=lambda: api.app.run(host="127.0.0.1", port=port, debug=False,
                                   use_reloader=False, threaded=True),
        daemon=True,
    ).start()
    return resolved


def run_window(game_url: str, health: tuple, fullscreen: bool) -> int:
    try:
        import webview
    except ImportError:
        print("[play] pywebview missing - opening a browser tab instead. "
              "For the real thing: pip install pywebview")
        return run_browser(game_url, health)

    import api as server

    # The start menu's background film plays with its sound from the first
    # frame. Chromium refuses audible autoplay until the page has had a user
    # gesture; the WebView2 runtime reads this variable at start-up, and the
    # desktop window is ours, so the policy is lifted here (a plain browser
    # tab keeps the platform default and the client falls back to muted until
    # the first press).
    _extra = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
    if "--autoplay-policy" not in _extra:
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
            _extra + " --autoplay-policy=no-user-gesture-required").strip()

    bridge = Api()
    window = webview.create_window(
        NAME, html=SPLASH, js_api=bridge,
        fullscreen=fullscreen, frameless=fullscreen,
        width=1600, height=900, min_size=(1024, 640),
        background_color="#020504", easy_drag=False,
    )
    bridge.attach(window, fullscreen)

    # Closing the window is what actually ends the app: it unblocks
    # webview.start() below, so EXIT leaves through main() like a normal quit
    # rather than relying on the server's os._exit backstop.
    server.enable_shutdown(window.destroy)

    # Rebind on every navigation: the game is a single page, but load_url below
    # replaces the splash document and takes the listener with it.
    window.events.loaded += lambda: _bind_keys(window)

    def hand_over() -> None:
        # The splash is already on screen; this only decides when to leave it.
        if not wait_for_health(health[0], headers=health[1]):
            window.load_html(
                SPLASH.replace('id="s"', 'id="s" class="err"').replace(
                    "warming the engine", "engine did not start - see console"))
            return
        time.sleep(0.2)
        # Bypass the WebView document cache: this URL is unique per launch.
        window.load_url(game_url)

    threading.Thread(target=hand_over, daemon=True).start()
    webview.start(debug=False, private_mode=True)

    # We only get here once the window has gone — via EXIT, or via the title
    # bar, or Alt+F4. The last two bypass /api/shutdown entirely, and a render
    # started before them runs in its OWN process: it would outlive this one
    # and keep buying frames for a game that is no longer on screen. So the
    # release happens on the way out of every path, not just the button.
    try:
        server._release_compute()
    except Exception as e:  # noqa: BLE001
        print(f"[play] could not release compute on close: {e}")
    return 0


def _bind_keys(window) -> None:
    """F11 fullscreen, Esc back out. The page owns every other key.

    Capture phase, but only for these two: the game binds arrows, Esc and
    letters of its own, so anything else has to pass straight through.
    """
    try:
        window.evaluate_js("""
        (function(){
          if (window.__sw_keys) return; window.__sw_keys = 1;
          document.addEventListener('keydown', function(e){
            if (!window.pywebview || !window.pywebview.api) return;
            if (e.key === 'F11') { e.preventDefault(); window.pywebview.api.toggle(); }
          }, true);
        })();
        """)
    except Exception:
        pass


def _env_candidates() -> list[Path]:
    """Every place a .env might reasonably be, best first.

    Looking only beside the exe was not enough, and the way it failed was the
    worst kind: launched from a terminal the process inherits the shell's
    environment and everything works, so it tests fine. Double-clicked from
    Explorer it inherits nothing, finds no file, and boots a game where every
    turn silently fails to generate.

    The built app also lives in `dist/SOMEWHERE`, two directories below the
    `.env` it was built from, so walking up finds it during development without
    anyone having to copy secrets into a distributable folder.
    """
    out: list[Path] = []

    def add(p: Path) -> None:
        if p not in out:
            out.append(p)

    add(ROOT / ".env")                       # shipped beside the exe
    if not FROZEN:
        # A packaged build reads its keys from beside the exe and from
        # %APPDATA% only: an installed game walking up the folders above it,
        # or reading the folder it was launched from, would pick up whatever
        # .env happens to be lying there (M1).
        add(Path.cwd() / ".env")             # wherever it was launched from
        for parent in list(ROOT.parents)[:3]:  # the repo, when running from dist/
            add(parent / ".env")
    if os.environ.get("APPDATA"):            # where an installed copy should look
        add(app_identity.appdata_root() / ".env")
    return out


def _load_keys() -> Path | None:
    """Load the first .env found. Returns where it came from, or None."""
    import run_local

    for path in _env_candidates():
        if path.is_file():
            run_local._load_env_file(path)
            return path
    return None


KEY_NAMES = ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def _have_key() -> bool:
    return any(os.environ.get(k) for k in KEY_NAMES)


def _warn_no_keys() -> None:
    """Say so, loudly, instead of opening a game that cannot generate anything.

    Silently degrading is what made this hard to diagnose in the first place:
    the app opened, the UI worked, and only the content was missing.
    """
    where = "\n".join(f"  {p}" for p in _env_candidates())
    message = (
        "No API key found, so ABYSS is starting in OFFLINE MODE.\n\n"
        "The game is fully playable but the text and images are canned "
        "placeholders rather than generated.\n\n"
        "To play for real, open ACCOUNT on the start menu, choose Gemini or "
        "OpenAI and paste that key — or put a file called .env in any of "
        "these places:\n\n"
        f"{where}\n\n"
        "containing a line like:\n\n"
        "  GEMINI_API_KEY=your-key-here\n\n"
        "Then start ABYSS again."
    )
    print(f"[play] no API key found; falling back to offline mode\n{message}")
    # The start menu opens ACCOUNT by itself when there is no key (provider
    # dropdown + paste field), so a first launch no longer stops on a dialog.
    # SOMEWHERE_KEY_DIALOG=1 brings the old box back.
    if FROZEN and os.environ.get("SOMEWHERE_KEY_DIALOG") == "1":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, NAME, 0x40)
        except Exception:
            pass


def _fatal(message: str) -> None:
    """Last resort for a windowed build: say something before dying."""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, NAME, 0x10)
    except Exception:
        pass


def run_browser(game_url: str, health: tuple) -> int:
    import webbrowser

    wait_for_health(health[0], headers=health[1])
    webbrowser.open(game_url)
    print(f"[play] serving {game_url.split('&launch=')[0]}  (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


class Api:
    """Exposed to the page so the key handler above can drive the window.

    The window does not exist when js_api has to be handed to create_window, so
    it is attached a moment later.
    """

    def __init__(self):
        self._w = None
        self._full = False

    def attach(self, window, fullscreen: bool) -> None:
        self._w, self._full = window, fullscreen

    def toggle(self):
        if self._w:
            self._w.toggle_fullscreen()
            self._full = not self._full


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Play ABYSS.")
    ap.add_argument("--windowed", action="store_true",
                    help="Open a normal resizable window instead of fullscreen.")
    ap.add_argument("--browser", action="store_true",
                    help="Use your default browser instead of a native window.")
    ap.add_argument("--mock", action="store_true",
                    help="Fully offline. No API keys, no network, instant.")
    ap.add_argument("--backend", choices=["gemini", "openai", "anthropic", "mock"],
                    help="Force a text/vision backend for this run.")
    ap.add_argument("--port", type=int, default=None,
                    help="Serve on this port (default: pick a free one).")
    args = ap.parse_args(argv)

    _capture_output()
    _prepare_writable()
    boot = _mark_this_boot()
    print(f"[play] source {ROOT}")
    print(f"[play] boot {boot}  (this process is the live build)")
    _stop_other_play_instances()

    source = _load_keys()
    if source:
        print(f"[play] keys loaded from {source}")
    import keys_store
    keys_path = keys_store.load_into_environ()
    if keys_path.is_file():
        print(f"[play] app keys loaded from {keys_path}")
    if args.mock:
        keys_store.mark_explicit_mock()
    if not args.mock and not _have_key() and not keys_store.has_play_key():
        # Better a game that says it is offline than one that looks live and
        # fails on every turn. The start-menu KEYS pane can lift this later.
        args.mock = True
        _warn_no_keys()

    port = free_port(args.port)
    import local_guard
    import safe_log
    token = local_guard.mint()
    safe_log.add_secret(token)
    try:
        backend = start_server(port, args.mock, args.backend, token)
    except Exception:
        import traceback
        traceback.print_exc()
        if FROZEN:
            _fatal(f"The engine failed to start.\n\nSee logs\\somewhere.log")
        raise
    shown_url = f"http://127.0.0.1:{port}/standalone?fresh={boot}"
    # The launch URL is the one place the token travels; the page trades it
    # for a cookie on this first load (local_guard.py). Never printed.
    game_url = f"{shown_url}&{local_guard.QUERY}={token}"
    health_url = f"http://127.0.0.1:{port}/api/health"
    health = (health_url, {local_guard.HEADER: token})

    print(f"{NAME} {app_identity.VERSION}  |  backend {backend}  |  {shown_url}")

    if args.browser:
        return run_browser(game_url, health)
    return run_window(game_url, health, fullscreen=not args.windowed)


if __name__ == "__main__":
    sys.exit(main())
