# Shipping ABYSS

Two products, one codebase.

| Who | How | What they get |
|---|---|---|
| Player, this machine | `PLAY.bat` / `python play.py` | Source tree. Their `.env`, their saves. |
| Player, another machine | The installer from `/get` (docs/operations/RELEASING.md) | `ABYSS-win-Setup.exe`: installs, updates itself. |
| Tester, a folder | `python tools/build_exe.py --clean` then copy `dist/ABYSS/` | A folder. No Python install. |
| Hosted | `bash start_production.sh` (`api:app`) | Same Flask app, gunicorn. |

Ship **the whole `dist/ABYSS/` folder**, not `ABYSS.exe` alone. Releases to
players go through Velopack and the releases repo: **[RELEASING.md](RELEASING.md)**.

## What is allowed in that folder

`tools/ship_layout.py` is the list. In short:

- The engine, UI, models, and factory prompts
- `worlds/somewhere.json` and `experiences/somewhere.json` (the Horizon demo)
- Nothing the player writes: saves, keys, characters and logs go to
  `%APPDATA%\ABYSS\` (`paths.py`), because an update replaces this folder
- `.env.example` — never a real `.env`

Not in the folder:

- This machine's other Worlds (`new-level`, `yard`, …)
- `experiences/.active` (Play's factory door is SOMEWHERE when the pointer is missing)
- `experiences/default.json`
- Sessions, playtests, keys, Discord leftovers

`stamp_factory()` runs after PyInstaller and writes `simulation_prompts.json`
from `simulation_prompts.defaults.json` + the SOMEWHERE World snapshot. The
live prompt file in this repo is authoring state and must not be the thing
a stranger boots.

## Prove it

```bash
python -m unittest test_somewhere_snapshot -v
python tools/build_exe.py --clean
python tools/smoke_exe.py
```

The snapshot tests fail if the freeze is missing or if a stamp would copy
author leftovers. The smoke boot is mock-mode and does not spend keys; it makes
the install folder read-only first and fails if the game writes anything there
(or if a save does not land in its scratch data root).

## Keys on a shipped copy

A friend never edits a file: on first launch ACCOUNT opens by itself, they
pick Gemini or OpenAI, paste the key, and the sheet checks it with one real
call. It lands in `%APPDATA%\ABYSS\keys.env` (never the game folder, never
the zip). `.env` beside the exe or in `%APPDATA%\ABYSS\` still works for
`GEMINI_API_KEY` / `OPENAI_API_KEY`. A packaged build launched from Explorer
inherits none of your shell. No key → offline mock, still playable.

`tools/publish_build.py` zips the build as `ABYSS/ABYSS.exe` (the name the /get
page tells friends to run). To test what a friend gets: build with
`tools/build_exe.py --out <temp>` so `dist/` is left alone, make the zip with
`publish_build.py --dry-run --dist <temp>/ABYSS`, unzip it anywhere, and run
`ABYSS.exe` with `APPDATA` pointed at an empty folder.
