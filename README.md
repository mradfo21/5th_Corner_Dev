# SOMEWHERE

An AI-driven first-person survival horror game. Every frame is generated as you
play; nothing is pre-drawn. You are a photojournalist in 1993, at the fence of a
quarantined facility in the Four Corners desert, and the world reacts to what you
actually do.

![Python](https://img.shields.io/badge/python-3.11+-blue.svg)
![Gemini](https://img.shields.io/badge/images-Nano%20Banana-orange.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

## Play it

Double-click **`PLAY.bat`**, or:

```bash
python play.py            # fullscreen, no browser chrome
python play.py --mock     # fully offline, no API keys needed
```

To build a standalone app you can move to another machine:

```bash
python tools/build_exe.py --clean --run   # -> dist/SOMEWHERE/
```

Full setup, keys and troubleshooting: **[QUICKSTART.md](QUICKSTART.md)**.

## The loop

```
see the frame -> read the beat -> act -> the world answers -> a new frame
```

Each turn you get a generated image, a paragraph of what just happened, and a
fresh slate of choices written against what is actually visible in the picture,
not against a script. Three ways to act:

- **MOVE** — go to something the detector found in the frame. The camera travels.
- **SCAN** — point at an object on screen and interact with that specific thing.
- **TALK** — speak to someone who is present.

Underneath, a handful of dials move: `threat` climbs monotonically as the story
escalates, `chaos` spikes and decays with what happens to you, the clock advances
with the phase, and injuries persist. Scenes are img2img continuations of the
last frame unless you do something that genuinely changes where you are, which is
what keeps a run looking like one place rather than a slideshow.

## Render mode

The **RENDER** button in the rail runs an unattended playthrough on the heavy
models and hands back the footage. Pick the turn count, the image model
(including Nano Banana Pro at up to 4K), the text model, and whether it plays
MOVE-driven or straight choices. It takes over the renderer for the duration and
gives it back when it finishes.

Output is browsable in the same panel: a turn-by-turn reviewer with the frame,
the prose and the dials, plus MP4 flipbooks of the scene, scan and result passes,
and a zip of the lot.

Renders are not cheap in time or disk — a 60-turn run on Pro at 2K is about 40
minutes and a gigabyte of stills. Sweep them with:

```bash
python tools/clean_artifacts.py --apply
```

That keeps the videos and transcripts and drops the stills, which costs nothing
real because each MP4 already contains every frame in order.

## It is mostly prompts

80% of the behaviour lives in `prompts/simulation_prompts.json`. Four keys do the
redirecting — edit these and the game changes:

- `world_initial_state` — what kind of place this is
- `action_consequence_instructions` — how an action becomes what happened
- `player_choice_generation_instructions` — what you are even offered to do
- `image_art_direction` — what every frame looks like

The rest is mechanical rulebooks (camera physics, negative prompt, the two image
templates, the between-turn bulletin) plus the **cast sheet** — who you play as,
the level, and where the camera sits — which is a structured spec rather than
prose (see [docs/reference/CAST_AND_CAMERA.md](docs/reference/CAST_AND_CAMERA.md)).
Both editors surface the four above and fold the rulebooks behind one disclosure.

Every key in that file is read by a live code path and editable in both editors.
`prompts_store.unwired_keys()` is asserted empty by the test suite, so a prompt
you can save but that changes nothing cannot accumulate again.

## Layout

What a **player** gets is `dist/SOMEWHERE/` — the folder `tools/build_exe.py`
writes. What this **repo** is is that game plus the studio that made it.

```
# Product (double-click, then ship the folder)
play.py  PLAY.bat        the game
RUN.bat                  windowed + console, for looking at logs
tools/build_exe.py       -> dist/SOMEWHERE/
tools/ship_layout.py     the only list of what may go in that folder
worlds/somewhere.json    the shipped Horizon World
experiences/somewhere.json
prompts/                 factory defaults; live file is rebuilt on stamp
templates/  static/      the UI
models/                  on-device SCAN weights

# Runtime on this machine (never ship)
sessions/  experiences/.active  worlds/new-level.json  .env

# Studio / engine (same process as Play; do not relocate)
api.py  engine.py  experience_store.py  worlds_store.py  ...

# Dev
tools/                   smoke the exe, sweep artefacts, playtests
docs/operations/SHIPPING.md
```

Do not move `engine.py` into a package. Every module finds its data with
`Path(__file__).parent`; that is also why the packaged layout is flat.

What goes in a build is listed once in `tools/ship_layout.py`. Author Worlds,
this machine's Experience pointer, and `simulation_prompts.json` are rebuilt
from the SOMEWHERE snapshot at stamp time so a folder you copy to another
computer is the Horizon demo, not yesterday's New Level.

## Providers

Text, vision and images each route through `ai_provider_manager`, so swapping
models is a config change in `ai_config.json` rather than a code change. Gemini,
OpenAI and Anthropic are wired for text; Gemini, Krea, fal and Reactor for
images. The catalogue in `ai_config.json` is what the render form offers.

With no key set, everything falls back to a mock backend that runs the whole loop
offline in milliseconds — which is how the test suite stays fast and how the game
still boots on a machine with no credentials.

## Testing

```bash
python -m unittest test_standalone_e2e test_providers test_render_mode
python run_full_playtests.py            # the full battery
python tools/smoke_exe.py               # prove the packaged build is playable
```

The end-to-end suites boot a real server as a subprocess in mock mode, so they
exercise the actual HTTP paths without spending anything.

## Documentation

- **[QUICKSTART.md](QUICKSTART.md)** — get it running
- **[AGENT_GUIDE.md](AGENT_GUIDE.md)** — technical guide for working on it
- **[CHANGELOG.md](CHANGELOG.md)** — what changed and why
- **[docs/operations/SHIPPING.md](docs/operations/SHIPPING.md)** — the player folder
- **[docs/operations/DEPLOYMENT.md](docs/operations/DEPLOYMENT.md)** — cloud deployment

## License

MIT.
