# Desktop App Roadmap

**Goal (owner's words):** "the long term goal is local... an AI game that's
fully playable as a desktop application... we expect the technology to
improve, generation times to come down, let's be ready."

This document lays out how the standalone web UI shipped in this PR turns
into an installable, double-click, fully local desktop game over time. It's
written as a sequence of independent, low-risk steps — each one is shippable
and playable on its own, so there's no "big bang" rewrite required.

## Where we are today

```
┌─────────────────────────────────────────────────────────────┐
│ Render (cloud)                                               │
│   gunicorn api:app  →  /standalone, /api/*, session API,    │
│                        admin, Discord bot                    │
└─────────────────────────────────────────────────────────────┘
                          ▲
                          │ same Flask app, same code
                          ▼
┌─────────────────────────────────────────────────────────────┐
│ Your laptop                                                   │
│   python run_local.py [--mock]  →  opens http://localhost/   │
│                                      standalone in a browser  │
└─────────────────────────────────────────────────────────────┘
```

`run_local.py` already runs the *exact* production Flask app (`api.app`)
locally, with one flag (`--mock`) to play fully offline with zero API keys
and zero network calls. That's the foundation everything below builds on —
there is intentionally no separate "local-only" codebase to maintain.

## Step 1 (shipped in this PR): `run_local.py`

- `python run_local.py` — real backends (Gemini/OpenAI per `ai_config.json`)
- `python run_local.py --mock` — fully offline, deterministic, no keys
- Opens `/standalone` in your default browser automatically

This is "the production server, running on your machine." It's already a
real, playable local game today.

## Step 2 (shipped in this PR, experimental): `run_desktop.py`

A thin wrapper that runs the same server in a background thread and opens
it in a native OS window via [pywebview](https://pywebview.flowrl.com/)
instead of a browser tab — no address bar, no tabs, just the game window.
Falls back to a normal browser tab automatically if `pywebview` isn't
installed (e.g. CI, headless environments).

```bash
pip install pywebview
python run_desktop.py --mock
```

This is the first thing that *feels* like an app instead of a website.

## Step 3 (next): Real packaging

Turn `run_desktop.py` into something a non-technical player can double-click:

- **PyInstaller** (`pyinstaller --onefile run_desktop.py`) or
  **briefcase** to produce a `.exe` (Windows) / `.app` (macOS) bundle.
- Bundle `templates/`, `static/`, `prompts/`, and `ai_config.json` as data
  files (PyInstaller's `--add-data`).
- Ship a default `ai_config.json` preset that points at `--mock` out of the
  box, so the unzipped app is playable with **zero setup**; a settings
  screen (or `.env` file) lets a player drop in their own Gemini/OpenAI key
  for the "real" experience.
- Session data (`sessions/`, `archives/`) already lives on local disk —
  no server-side database to migrate.

**Risk/complexity notes for whoever picks this up:**
- `engine.py` currently shells out to network APIs sychronously inside
  request-handling threads; for a desktop app this is fine (one player,
  one machine) but the UI should keep showing the existing "processing
  veil" / interim messages during generation, especially once image
  generation is involved (real image gen is much slower than mock text).
- Discord bot (`bot.py`) and the admin dashboard are server-oriented and
  should be excluded from the desktop bundle (or left disabled via
  `DISCORD_ENABLED=0`) — the desktop app only needs `api.py` + `engine.py`
  + the standalone UI.

## Step 4 (ongoing): Stay ready for faster/cheaper generation

The owner's framing — "generation times will come down, let's be ready" —
is already designed for in this PR's architecture:

- `ai_provider_manager.active_backend()` / `chat()` / `vision()` /
  `generate_image()` are the single seam all *new* code should call through.
  As faster/cheaper models land, swapping them in is a config change
  (`ai_config.json` presets), not a code change.
- The mock backend (`set_backend_override("mock")`) keeps the whole UI
  loop (`/api/reset`, `/api/choose`, `/api/regenerate_choices`,
  `/api/feed`, `/api/status`) testable in milliseconds regardless of how
  slow or fast real generation is — `test_providers.py` and
  `test_standalone_e2e.py` both run fully offline today.
- The standalone UI's "processing veil" with rotating interim messages
  (`static/js/standalone.js`) is the buffer that absorbs generation
  latency now; as latency drops, this becomes less necessary but stays
  harmless (it just resolves faster).

## Step 5 (future): "Sellable" polish

Not started in this PR, listed here so the roadmap is honest about scope:

- Settings/onboarding screen for API keys (no more `.env` editing)
- Save/load multiple playthroughs from the UI (the session API already
  supports multiple sessions — `/api/sessions/*` — the standalone UI only
  drives the single legacy "default" session today)
- Auto-update mechanism for the packaged app
- Code signing / notarization for macOS & Windows distribution
- Optional telemetry opt-in (crash reports, anonymized playtime) to learn
  what to improve before charging for it
