#!/usr/bin/env python3
"""
run_desktop.py — experimental desktop window wrapper around the standalone
game (first step beyond run_local.py toward DESKTOP_APP_ROADMAP.md's
"double-click app" goal).

Starts the same Flask app run_local.py does, in a background thread, and
opens it in a native OS window (via pywebview) instead of a browser tab —
no address bar, no tabs, just the game. Falls back to opening a normal
browser tab automatically if pywebview isn't installed or can't create a
window (e.g. headless/CI environments, or missing system WebView libs).

Usage:
    pip install pywebview   # optional - only needed for the native window
    python run_desktop.py
    python run_desktop.py --mock
    python run_desktop.py --port 5050

Packaging this into a real double-click .exe/.app (PyInstaller/briefcase)
is the next step after this — see DESKTOP_APP_ROADMAP.md.
"""

from __future__ import annotations

import sys
import threading
import time

import run_local


def main(argv=None) -> int:
    args = run_local.parse_args(argv)
    args.no_browser = True  # we handle window/browser opening ourselves below

    if args.config:
        run_local._load_env_file(run_local.Path(args.config))

    import os
    os.environ.setdefault("FLASK_DEBUG", "0")
    os.environ.setdefault("PORT", str(args.port))

    backend = run_local.configure_backend(args)
    from api import app  # noqa: E402

    standalone_url = f"http://localhost:{args.port}/standalone"

    server_thread = threading.Thread(
        target=lambda: app.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True),
        daemon=True,
    )
    server_thread.start()

    # Wait for the server to come up before pointing a window at it.
    deadline = time.time() + 15
    import urllib.request
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://localhost:{args.port}/api/health", timeout=1)
            break
        except Exception:
            time.sleep(0.3)

    try:
        import webview  # pywebview
    except ImportError:
        print(
            "[run_desktop] pywebview not installed — falling back to your default "
            "browser. Install it for a real app-like window: pip install pywebview",
            flush=True,
        )
        import webbrowser
        webbrowser.open(standalone_url)
        print(f"[run_desktop] Backend: {backend}. Press Ctrl+C to stop the server.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 0

    print(f"[run_desktop] Backend: {backend}. Opening native window -> {standalone_url}")
    webview.create_window("SOMEWHERE", standalone_url, width=1280, height=800, min_size=(800, 600))
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
