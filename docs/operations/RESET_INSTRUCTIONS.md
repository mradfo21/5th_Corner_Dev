# Getting a clean run — and proving it before you demo

## Before a demo, run this

```powershell
python tools/demo_check.py --boot
```

It checks, cleans, then **plays the game** and watches the opening happen. It
prints `READY` only after it has seen a picture arrive, prose land and choices
appear. Exit code is 0 only when everything passed, so it can gate a script.

Drop `--boot` to check and clean without spending a generation. Add
`--no-clean` to look without touching anything.

## Why a first run misbehaves and a third one doesn't

This is the complaint that produced the tool: *"when I try and demo the game it
always has bugs or is broken... after a few rounds it seems to work normally."*

Not superstition, and not the model warming up. **A first run inherits things.**
By round three the live run has overwritten all of it, which is exactly why it
settles down. Every check below is a real incident from this repo:

| What it inherits | What the demo does |
|---|---|
| A previous run's `state.json` — its last frame, feed, history | Opens on last night's picture; the montage hands off to a stale frame |
| An `encounter` block left open | Opens *inside* a fight nobody started |
| A World carrying the factory character sheet | Binding it overwrites your character; a stranger walks the level |
| A character plate NAMED but missing from disk | Every frame is a guess at who the player is |
| A blank Level sheet | `shots=0`, the opening montage never plays, "toward (no goal authored)" |
| `tunables.json` blanked by a test run | Flipbook silently back to OFF — every animated turn is a still, no error anywhere |
| A stale 4×4 flipbook prompt in a World | The authored art direction is dropped on every turn |
| No `GEMINI_API_KEY` | Mock mode: the loop runs, prose is canned, no pictures |

`tunables.json` is gitignored, so when a test run wrote `{}` over it there was
nothing to restore from. That one is worth knowing about on its own.

## What `--boot` actually verifies

A file check cannot tell you the game works. This drives the real app over CDP,
the same way `playtest_app.py` does, and asserts what a person in the room would
see:

- the app comes up at all
- the run reaches a playable turn
- there is a **picture** on screen and it is not black
- prose arrived
- there are choices to press
- the opening montage staged more than zero shots
- the montage has a goal to head toward
- no client-side errors on boot

## Doing it by hand

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Remove-Item sessions\* -Recurse -Force      # the run's inheritance
Remove-Item .cache -Recurse -Force          # vision reads, replays
python play.py
```

Sessions and `.cache` are the only two things a reset should delete. The repair
tools below are for authoring data, which a reset must never touch:

| Problem | Tool |
|---|---|
| Live sheets replaced by the factory copies | `tools/restore_polluted_blocks.py <block> --apply` |
| A World snapshot carrying the same damage | `tools/restore_polluted_world.py worlds/x.json --apply` |
| Worlds still on the factory character | `tools/sync_character_into_worlds.py --apply` |
| A stale 4×4 flipbook prompt | `tools/retire_stale_flipbook_prompts.py --apply` |
| Editing the world bible everywhere at once | `tools/edit_prompt_everywhere.py <block> --replace A B --apply` |

## What is never deleted

Source, `.env`, `ai_config.json`, `prompts/`, `worlds/`, `experiences/` and
`assets/references/`. If a reset ever takes authoring data with it, that is the
bug — not the thing to work around.
