#!/usr/bin/env python3
"""
run_local.py — local launcher for the SOMEWHERE standalone immersive UI.

Runs the SAME Flask app production uses (`api.app`, the one gunicorn
serves via start_production.sh) on your machine, and opens the
AI-Dungeon-style standalone UI at http://localhost:<port>/standalone.

This is the first step toward the long-term goal of a fully local,
installable desktop game: today it's "the production server, running on
your laptop". See DESKTOP_APP_ROADMAP.md for how this evolves into a
double-click desktop app.

Usage:
    python run_local.py                    # Real backends (Gemini/OpenAI per ai_config.json)
    python run_local.py --mock             # Fully offline, zero API calls, zero API keys needed
    python run_local.py --port 5050
    python run_local.py --no-browser
    python run_local.py --config .env      # Load extra env vars from a file first
    python run_local.py --backend gemini   # Force a specific backend for this run

Auto-detects missing API keys: if neither GEMINI_API_KEY nor OPENAI_API_KEY
is set and --mock wasn't requested, automatically falls back to the mock
backend so the app still boots and is playable offline.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent.resolve()


def _load_env_file(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE per line, '#' comments). Avoids adding
    a python-dotenv dependency just for local convenience."""
    if not path.exists():
        print(f"[run_local] --config file not found: {path}", file=sys.stderr)
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    print(f"[run_local] Loaded env vars from {path}")


def _has_real_api_key() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the SOMEWHERE standalone game locally.")
    parser.add_argument("--mock", action="store_true", help="Force the fully offline mock backend (no network, no API keys).")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5001)), help="Port to serve on (default 5001).")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser.")
    parser.add_argument("--config", type=str, default=None, help="Path to a .env-style file to load before startup.")
    parser.add_argument("--backend", type=str, default=None, choices=["gemini", "openai", "anthropic", "mock"],
                         help="Force a specific text/vision backend for this run (overrides ai_config.json).")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind (default 0.0.0.0).")
    return parser.parse_args(argv)


def configure_backend(args: argparse.Namespace) -> str:
    """Decide and apply the active backend. Returns the resolved backend name
    ('mock', 'gemini', 'openai', or 'anthropic') for logging."""
    import ai_provider_manager

    forced = args.backend or ("mock" if args.mock else None)

    if forced is None and not _has_real_api_key():
        print(
            "[run_local] No GEMINI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY found in "
            "the environment — falling back to the offline mock backend. Pass --backend "
            "to force a specific provider, or set an API key and rerun.",
            flush=True,
        )
        forced = "mock"

    if forced:
        ai_provider_manager.set_backend_override(forced)
        os.environ["STORYGEN_BACKEND"] = forced

    if forced == "mock":
        # engine.py already has graceful "disabled" fallbacks for text/image
        # generation (random canned narrative / contextual fallback choices)
        # that exist independently of this harness. Flipping these existing
        # toggles makes mock mode instant and fully offline instead of
        # waiting out real network timeouts with no API key configured.
        import engine

        engine.LLM_ENABLED = False
        engine.IMAGE_ENABLED = False
        engine.WORLD_IMAGE_ENABLED = False
        print("[run_local] Mock mode: engine.LLM_ENABLED / IMAGE_ENABLED / WORLD_IMAGE_ENABLED set to False.", flush=True)

    return ai_provider_manager.active_backend("chat")


def open_browser_when_ready(url: str, timeout_s: float = 15.0) -> None:
    import urllib.request

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            webbrowser.open(url)
            return
        except Exception:
            time.sleep(0.3)
    # Last attempt regardless of whether the health check ever succeeded.
    webbrowser.open(url)


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.config:
        _load_env_file(Path(args.config))

    os.environ.setdefault("FLASK_DEBUG", "0")
    os.environ.setdefault("PORT", str(args.port))

    backend = configure_backend(args)

    # Import AFTER backend configuration so any module-level init in
    # engine.py / api.py observes the right environment.
    from api import app  # noqa: E402  (intentional late import)

    standalone_url = f"http://localhost:{args.port}/standalone"

    print("=" * 70)
    print("SOMEWHERE — Standalone Local Server")
    print("=" * 70)
    print(f"  Backend:      {backend}")
    print(f"  Standalone:   {standalone_url}")
    print(f"  Health:       http://localhost:{args.port}/api/health")
    print(f"  Admin:        http://localhost:{args.port}/admin")
    print("=" * 70)

    if not args.no_browser:
        threading.Thread(target=open_browser_when_ready, args=(standalone_url,), daemon=True).start()

    app.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
