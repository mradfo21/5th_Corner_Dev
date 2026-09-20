# Desktop App Roadmap

> **Status: the desktop app shipped.** `play.py` / `PLAY.bat` boot the game in a
> native pywebview window, and `tools/build_exe.py` produces the movable
> `dist/SOMEWHERE/` folder (contents governed by `tools/ship_layout.py`, verified
> by `tools/smoke_exe.py`). For how to run and build it, read `QUICKSTART.md` and
> `docs/operations/SHIPPING.md`, not this roadmap. What remains open here is the
> longer arc — fully local models — not the packaging.

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

## Step 2 (superseded): `run_desktop.py`

A thin pywebview wrapper, replaced by `play.py` below. Deleted.

## Step 3 (shipped): `play.py` and a real bundle

`play.py` is the single way to play. It picks a free port so it never fights a
dev server left running, boots the same Flask app in a background thread, holds
an animated title card up while the engine imports, then hands over to a
borderless fullscreen window. `F11` toggles fullscreen. It falls back to a
browser tab if pywebview is missing, and to the mock backend if no API key is
set, so it always boots into something playable.

`PLAY.bat` double-clicks it via `pythonw`, so there is no console window.

```bash
python tools/build_exe.py --clean --run   # -> dist/SOMEWHERE/ (~530 MB)
python tools/smoke_exe.py                 # prove the build is playable
```

**Decisions worth knowing if you touch the packaging:**

- **One folder, not one file.** Every module resolves its data with
  `Path(__file__).parent`. Under `--onefile` that is a temp directory that is
  wiped on exit, so saved games would vanish.
- **`contents_directory="."` on `EXE`.** Without it PyInstaller puts the payload
  in `_internal/`, so bundled modules write saves to `_internal/sessions` while
  everything else looks beside the exe. Flattening makes the packaged layout
  identical to the source layout. Note this belongs on `EXE`, not `COLLECT` —
  `COLLECT` reads it off the `EXE` object and silently ignores its own kwarg.
- **`console=False`,** so it opens like a game. That means stdout has nowhere to
  go, so `play.py` redirects both streams to `logs/somewhere.log` when frozen and
  puts a message box up if the engine fails to start. Without that a crash is
  completely silent.
- **mediapipe and OpenCV are most of the 530 MB.** Both are needed: SCAN runs its
  detector on-device. They are pulled in with `collect_all` because neither is
  visible to the dependency walker — `local_vision` imports mediapipe inside a
  function.

## Step 3b (not done): zero-setup first run

The bundle still expects a `.env` for real models and drops to mock without one.
A first-run settings screen that takes a key and writes it would remove the last
piece of manual setup.

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
