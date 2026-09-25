# Quickstart

Two ways in. Pick the one that matches what you want to do.

## Just play it

Double-click **`PLAY.bat`**.

That opens the game in a borderless fullscreen window with no browser around it.
Press `F11` to leave fullscreen, `Alt+F4` to quit. First launch takes a few
seconds while the engine warms; you get a title card, not a white screen.

From a terminal, the same thing:

```bash
python play.py              # fullscreen, real models
python play.py --windowed   # a normal resizable window
python play.py --mock       # fully offline: no API keys, no network, instant
python play.py --browser    # skip the native window, use your browser
```

If no API key is set, it falls back to mock mode automatically rather than
hanging on failed calls, so it always boots into *something* playable.

## Build the standalone app

Produces a folder you can move to another machine, with Python and every
dependency inside it. No install required on the target.

```bash
python tools/build_exe.py --clean --run
```

Output is `dist/SOMEWHERE/`. **Ship the whole folder, not just the `.exe`** —
the interpreter, the libraries and the game's content all live beside it. Around
530 MB and a few minutes to build; mediapipe and OpenCV are most of the weight,
and both are needed because the SCAN tool runs its detector on-device.

What is allowed in that folder (and what must never be) is listed in
`tools/ship_layout.py`. Walkthrough: **[docs/operations/SHIPPING.md](docs/operations/SHIPPING.md)**.

Saved games land in `dist/SOMEWHERE/sessions/`. A windowed build has no console,
so anything it prints goes to `dist/SOMEWHERE/logs/somewhere.log`.

## Set up API keys

Without keys the game runs in mock mode: the loop works, the prose is canned and
no images are generated. On a machine with no key the start menu opens ACCOUNT
by itself.

**The player's way: ACCOUNT → PLAYS ON.** Choose Gemini or OpenAI in the
dropdown, paste that key, SAVE. The sheet proves the key with one real call
("Works · story … · pictures …", or why not), stores it in
`%APPDATA%\SOMEWHERE\keys.env` and the choice in `account.json` beside it.
Either provider plays the whole game — story and pictures. With OpenAI chosen,
`provider_bridge.py` answers every Gemini-format call with the matching OpenAI
one; the bottom-left `backend:` tag names the provider actually answering.

**The developer's way: a `.env`:**

```
GEMINI_API_KEY=...
OPENAI_API_KEY=...
```

**Gemini is the default.** The game plays on OpenAI only when OpenAI is picked
in ACCOUNT; an `OPENAI_API_KEY` on its own changes nothing, and everything runs
on `ai_config.json` as it always has. `ANTHROPIC_API_KEY` works for the
narrator set there.

The `.env` is looked for in these places, first one wins:

1. beside `play.py` (or beside `SOMEWHERE.exe` in a build)
2. the folder you launched from
3. up to three directories above — so a build in `dist/SOMEWHERE` finds the
   repo's `.env` without anyone copying secrets into a shippable folder
4. `%APPDATA%\SOMEWHERE\.env` — where an installed copy should keep it

**A packaged build launched from Explorer inherits none of your shell's
environment.** If `GEMINI_API_KEY` is only exported in your terminal, the app
works when you start it from that terminal and runs offline when you
double-click it. The `.env` file is what makes both cases work; the build drops
a `.env.example` beside the exe as a reminder.

## Develop against it

`run_local.py` is the bare server with no window. The end-to-end suites spawn it
as a subprocess, so its flags are load-bearing — leave them alone.

```bash
python run_local.py --mock --no-browser --port 5001
python -m unittest test_standalone_e2e test_providers
```

## Housekeeping

Renders and playthroughs generate a lot of disk. The videos and transcripts are
the deliverable; the frame stills are not, and each MP4 already contains every
frame in order.

```bash
python tools/clean_artifacts.py            # show what would go
python tools/clean_artifacts.py --apply    # sweep it
```

## Where things are

| Path | What |
|---|---|
| `play.py`, `PLAY.bat` | Play the game |
| `tools/build_exe.py` | Build the standalone app |
| `api.py`, `engine.py` | The server and the simulation |
| `templates/`, `static/` | The UI |
| `prompts/` | The prompt layers the narrator uses |
| `sessions/` | Saved playthroughs |
| `playtest_results/` | Render output |
| `docs/plans/` | Designs not yet built |
| `docs/reference/` | How the subsystems work |
| `docs/operations/` | Deploying, testing, resetting |

## When it will not start

Read `logs/somewhere.log` first — the packaged build writes everything there,
including tracebacks that would otherwise vanish with the window.

- **Port already taken** — it picks a free port automatically; pass `--port` to
  force one.
- **No window, no error** — pywebview is missing. `pip install pywebview`, or run
  with `--browser`.
- **Boots but no images** — that is mock mode. Check that your key is in `.env`
  and that `ai_config.json` names a provider you have a key for.
