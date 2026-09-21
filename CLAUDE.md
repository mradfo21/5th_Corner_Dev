# Working on SOMEWHERE

You are picking up a live, long-running project. This file is the orientation:
what the thing is, how it got here, how it is put together, and — most
importantly — **how we test it**, because this codebase has a specific testing
flow and every attempt to improvise one has wasted a session.

Read this, then read `CHANGELOG.md` from the top. The changelog is the real
history and the real design document.

---

## 1. What this is

**SOMEWHERE** is an AI-driven first-person survival horror game where nothing is
pre-drawn. Every frame is generated as you play. You are a photojournalist in
1993 at the fence of a quarantined facility in the Four Corners desert — hence
the repo name, *5th Corner*.

The loop:

```
see the frame -> read the beat -> act -> the world answers -> a new frame
```

Each turn gives you a generated image, a paragraph of what just happened, and a
fresh slate of actions written against **what is actually visible in the
picture**, not against a script.

This repo is **the game plus the studio that authors it**. Same process, same
Flask app. `dist/SOMEWHERE/` (built by `tools/build_exe.py`) is what a player
gets; everything else is the workshop.

It is a **desktop app**, not a website: `play.py` boots a Flask server and wraps
it in a native pywebview window. `--browser` works for debugging.

---

## 2. History, in one paragraph

It began as a Discord bot (`bot.py`, buttons and embeds, death-replay GIFs
posted to a channel). That is **gone**. It became a Flask + HTML/JS app, then a
native windowed app, then a game with an authoring studio attached. About 1045
commits. Along the way it gained: an on-device object detector for SCAN, a
Moments framework for cinematic set-pieces, ElevenLabs voices, generated music
and foley, a graph editor for stitching worlds together, an unattended render
mode, cost tracking and Stripe billing, and a bug-capture button.

The Discord-era documents have been **deleted**, not archived, so you cannot be
misled by them: `AGENT_GUIDE.md`, `ARCHITECTURE_CLARIFICATION.md`,
`API_WRAPPER_DOCUMENTATION.md`, the marketing-site integration guides, three
stale deployment docs, and the whole 76-file `docs/archive/` tree of
"✅ COMPLETE" reports. Git has all of it if you ever want the history; nothing in
the working tree describes a system that no longer exists.

This means **every `.md` still on disk is meant to be true.** If you find one
that is not, fix or delete it rather than leaving it — that is the standard the
tree is now held to. `docs/plans/` is the one nuance: those are design records,
and each opens with a Status line saying whether it shipped.

Current and trustworthy: `README.md`, `QUICKSTART.md`, this file,
`docs/MOMENTS.md`, `docs/operations/TESTING_USE_THIS.md`,
`docs/operations/RESET_INSTRUCTIONS.md`, `docs/operations/SHIPPING.md`,
`docs/operations/DEPLOYMENT.md`, `docs/reference/CAST_AND_CAMERA.md`,
`docs/reference/LOCAL_OBJECT_DETECTION.md`, `docs/reference/COINOP_MVP_SETUP.md`
and `CHANGELOG.md`.

---

## 3. The state of the tree — read this before you touch anything

Branch: `cursor/fix-simulation-playback-coherence-9f80`. `main` is behind it.
Remote is `github.com/mradfo21/5th_Corner_Dev`.

The tree is committed. The September 18 day and the September 20 morning went
in as three commits on 2026-09-20 (`6206295` the docs and dead-code cleanup,
`9dd4a7d` the feature day, `1118d5f` run isolation / region change / the
doctrine guard), the QA loop that afternoon added two more, and the loop trace
that evening (the goal wired through every system, `goal_reached`, the fate
that fights roll with) one more — see the top of `CHANGELOG.md`. Start with
`git status` and `git log --oneline -8` anyway; `prompts/simulation_prompts.json`
will usually show as modified, because binding a World rewrites it (see
section 8), and that diff is not work.

Untracked and safe to ignore or sweep: `_menushots_before/`,
`_menushots_after/`, `_playthrough/`, loose `_*.png` probe images at the root,
and `_claude_pull/` (frames and logs pulled out of a QA run). Those are
screenshot evidence from past sessions.

---

## 4. Architecture

One Flask app. `api.py` owns it (the app object, CORS, embed headers, routes).
`engine.py` owns the simulation and exports plain functions that `api.py` mounts
with `add_url_rule` — that is why you will not find `@app.route('/api/choose')`
anywhere. Grep `add_url_rule` in `api.py` for the gameplay routes and
`@app.route` for the studio, admin, render and billing routes.

**Do not turn `engine.py` into a package.** Every module locates its data with
`Path(__file__).parent`, which is also why the packaged build is flat.

| File | Lines | What it owns |
|---|---|---|
| `engine.py` | ~19k | Turn simulation, state, world evolution, detection, image orchestration, most gameplay endpoints |
| `api.py` | ~4.9k | The Flask app, route mounting, studio/admin/render/billing endpoints, credit gating |
| `static/js/standalone.js` | ~27k | The entire game client |
| `static/css/standalone.css` | ~14.6k | All of the UI |
| `encounter.py` | ~4.1k | Confrontation Moments: briefs, lanes, plates, resolution |
| `game_identity.py` | ~3k | The cast sheet — who you play as, the level, the camera |
| `cutscene.py` | ~1.1k | The 4-shot montage |
| `choices.py` | ~940 | Choice generation, grounded in the current frame |
| `flipbook.py` | | Grid-of-panels image turns, split back into frames; `grid_prompt` is the keyframe contract |
| `local_vision.py` | | On-device MediaPipe detector for SCAN (falls back to Gemini) |
| `ai_provider_manager.py` | | Text/vision/image routing across providers |
| `prompts_store.py` / `worlds_store.py` / `experience_store.py` | | The authoring stores |
| `scene_audio.py` / `voice_design.py` | | Generated music, ambience, foley, ElevenLabs voices |
| `render_jobs.py` | | Unattended playthroughs on the heavy models |
| `coinop.py` / `billing.py` / `cost_tracker.py` / `pricing.py` | | Credits, Stripe, spend accounting |
| `authoring_sandbox.py` | | Guard that stops a test run overwriting live authoring data |

Client-side, `static/js/moments.js` is the Moment stack controller and
`static/js/reactor_renderer.js` is the live world-model video layer.

### The elements the game is built from

**Explore verbs.** The turn interface is an action wheel plus hotspots on the
picture, **not a list of text buttons**:

- **SCAN** — run the detector on the current frame, get tags on objects, click a
  tag to open its sub-actions (MOVE TO / INTERACT / TALK).
- **MOVE TO** — travel to something the detector found. The camera relocates.
- **INTERACT** — a Moment that dives to a generated close-up of the object while
  a same-place turn runs underneath.
- **TALK** — a conversation Moment with a generated portrait and a designed voice.
- **PHOTO** — raise the camera, sweep for a subject, take one shot per raise.
- **ACT** — type or dictate a free-will action. These are sacred: a typed action
  should always resolve into something happening, never "you try but fail".
- **CAMP** — hard-cut into a playable campsite level with your companions round
  the fire. Not a Moment; the full HUD stays live.

**Dials.** `threat` climbs monotonically as the story escalates and derives
the phase — in POINTS (a choice adds 1, a MOVE TO / INTERACT / TALK adds 2)
against the marks `escalate_at` / `critical_at`, which are 3 / 6 everywhere
by default since 2026-09-20 (escalating by turn 2–3, critical by turn 3–6;
authored per Experience in the Pacing sheet). `chaos` spikes and decays. `detection` (hidden → suspicious →
alerted → hunted) moves off the frame witness and the prose. The clock — time
of day and the lighting line — is rolled once at reset, after the World bind,
and never moves: the light is the run's identity, not a tension dial. The run
also knows its goal (`level_goal`) and whether it has been reached
(`goal_reached_turn`, the consequence model's verdict). The goal is one thing you can see — `goal.py`: a name, a why and a look invented at reset, drawn into the first frame, found on every settled picture (`/api/goal/sight`) and tagged by `GoalTag` in standalone.js. Injuries persist in
the prose (the previous beat is always in the prompt), not as HP
(`DAMAGE_SYSTEM_ENABLED` is off); the one mechanical flag,
`player_state.condition`, is read only by the next fight's odds.

**Moments** (`docs/MOMENTS.md` — read it before touching any of them) are
full-screen cinematics layered over a *paused, not destroyed* world.
Conversation and Encounter are shipped types; Cutscene is a third and also a
first-class node in the Experience graph. Encounter deliberately does **not**
restore the paused world: if you survive, the aftermath is what you walk into.

**Companions and props** persist. Every character you talk to is saved with a
stable portrait file and an ElevenLabs voice id plus the description needed to
regenerate it. The jeep is a prop, generated once and reused forever as an
img2img reference so it looks identical every visit.

**Continuity** is img2img. A turn continues the last frame unless the action
genuinely relocates the player — and *the consequence model now reports that*
(`relocated`) rather than the code guessing from the player's wording. That was
a real bug class; see the top two changelog entries.

### It is mostly prompts

80% of the behaviour is in `prompts/simulation_prompts.json` — 33 keys, hot
reloaded, live on the next turn with no restart. Four of them do the redirecting:

- `world_initial_state` — what kind of place this is
- `action_consequence_instructions` — how an action becomes what happened
- `player_choice_generation_instructions` — what you are offered to do
- `image_art_direction` — what every frame looks like

The rest are mechanical rulebooks (camera physics, negative prompt, the two
image templates, encounter briefs, narrator direction) plus the **cast sheet**
(`player_character`, `setting_reference`, `camera_perspective`) — a structured
spec rather than prose, because it has to reach thirty prompt surfaces at once.
See `docs/reference/CAST_AND_CAMERA.md`.

Every key is read by a live code path. `prompts_store.unwired_keys()` is
asserted empty by the suite, so a prompt you can save but that changes nothing
cannot accumulate.

**When something behaves wrong, check the prompts before the code.** And check
the *assembled* prompt, not just the template — the two bugs at the top of the
changelog were both a later, more concrete instruction quietly overriding an
earlier one.

### Worlds, Experiences, Lore

- `worlds/*.json` — a snapshot of every editable key, so saving a World captures
  the prompts, your protagonist, the level and the camera together. Each has a
  `.frame.png` plate.
- `experiences/*.json` — the graph: worlds and cutscenes stitched with edges.
  `experiences/.active` is this machine's pointer and must never ship.
- The game never plays *from* those files. Binding a World copies its snapshot
  over the one live prompt file (`prompts/simulation_prompts.json`), and a run
  is one `sessions/<id>/state.json` + `history.json` regardless of which World
  it is in. So a mid-run switch is three things, all in `engine.py`:
  `_bind_world_prompts` (replace, keep the run's cast),
  `_clear_world_scoped_state` (`_WORLD_SCOPED_KEYS` — everything about the
  place: goal, lighting, heat, threat clock, roster, flipbook keyframe…) and
  `_stitch_history` (a `hard_transition` + `cached_opening` row whose image is
  the destination, so the next frame continues from it instead of the old
  World's last frame). A cutscene that leads to another World binds that World
  *before* it draws (`apply_experience_cutscene`) and draws plate-less like the
  level opening; "departure" is the one mood that stays in the World it leaves.
- `experiences/_lore/` — documents the narrator and worldbuilder read.
- Two editors: **World Studio** at `/studio`, and the in-game **World Editor**
  (backtick, or the EDIT rail button, while playing). A change must land in
  **both**; "wire it into the editor that is actually shipped" is a commit
  message in this repo for a reason.

- `static/menu/background_loop.mp4` — the start menu's background film,
  played muted and looping under the title by `Signal` (standalone.js). Swap
  the file to swap the splash; absent, the menu is the black card. `*.mp4` is
  gitignored, and `tools/ship_layout` bundles `static/`, so a build made with
  it in place carries it.

### Providers and mock mode

Text, vision and images route through `ai_provider_manager`, configured in
`ai_config.json`. Gemini / OpenAI / Anthropic for text; Gemini, Krea, fal and
Reactor for images. **With no key set, everything falls back to a mock backend
that runs the whole loop offline in milliseconds.** That is how the suite stays
fast and how the game still boots on a machine with no credentials — and also
the single most common false alarm: mock mode looks broken because the prose is
canned and there are no pictures. It says so at startup.

---

## 5. Running it

```powershell
python play.py                  # fullscreen native window, real models
python play.py --windowed       # resizable
python play.py --mock           # fully offline, no keys, instant
python play.py --browser        # skip the native window

python run_local.py --mock --no-browser --port 5001   # bare server; e2e suites spawn this
```

Keys go in `.env` (`GEMINI_API_KEY`, optionally `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, ElevenLabs, Stripe). `python tools/build_exe.py --clean --run`
produces `dist/SOMEWHERE/`; what may go in that folder is listed once in
`tools/ship_layout.py`.

Logs: `logs/somewhere.log`, plus structured run logs in `logs/play/*.jsonl`.

---

## 6. How we test — the flow

This is the part to get right. The short version: **there is already a harness,
it drives the real application, and you should extend it rather than write
another one.** Read `docs/operations/TESTING_USE_THIS.md` in full before writing
any test.

### Before a demo or after risky work

```powershell
python tools/demo_check.py --boot
```

Checks the things that have actually broken a first run in this repo, clears the
state a run inherits, then **boots the game and watches the opening happen** —
picture on screen and not black, prose landed, choices present, montage staged
more than zero shots, no console errors. Prints `READY` only then; exit code 0
only when everything passed. `--no-clean` to look without touching anything.

Why this exists: *"when I try and demo the game it always has bugs, and after a
few rounds it works normally."* A first run **inherits** a previous run's
`state.json`, an encounter left open, a World carrying the factory character
sheet, a `tunables.json` a test run blanked. By round three the live run has
overwritten all of it. See `docs/operations/RESET_INSTRUCTIONS.md`.

### Driving the real app (the main harness)

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3
$env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="--remote-debugging-port=9333"
Start-Process -FilePath python -ArgumentList "play.py" -RedirectStandardOutput logs\play_actual.log -RedirectStandardError logs\play_actual.err.log
# poll http://127.0.0.1:9333/json/list until a *standalone* target appears (~8s), then:
$env:PT_TURNS="3"; $env:PT_TURN_TIMEOUT="150"
Start-Process -FilePath python -ArgumentList "-u","playtest_app.py" -RedirectStandardOutput _pt_out.txt -RedirectStandardError _pt_err.txt
```

`playtest_app.py` attaches over CDP and plays the real native app: start menu,
experience picker, SCAN, MOVE TO, INTERACT, PHOTO, ACT, CAMP, encounters. It
detects black screens, stalls, dead buttons, console errors and spurious
recovery UI. **The app must be launched with that env var** or the harness
cannot attach. Read `_pt_out.txt` for the play-by-play and
`logs\play_actual.log` for the server side.

Knobs: `PT_TURNS` (8), `PT_TURN_TIMEOUT` (90; raise to 150 when generation is
slow), `PT_TAG_INDEX`, `PT_RELOAD=1`.

### The rest of the kit

| Tool | For |
|---|---|
| `playtest.py` | API-level playtest, no client. Forced encounter/death probes, malformed-request and double-submit checks. Use when changing server logic. |
| `autoplay.py --url ... --turns N --strategy mixed` | Presses choice buttons through the real `/api/reset` → `/api/choose` → `/api/feed` loop. Fast, headless, PASS/FAIL per turn. |
| `playtest_interactive.py --url ... --plan scan_move,scan_interact,choice` | Plays via SCAN and the detector. Records three frames per turn (VIEW / CHOICES / SELECTED) plus MP4s, so you can see whether what happened follows from what was picked. |
| `playtest_capture.py`, `playtest_analyze.py` | Frame capture and run analysis. |
| `playtest_boot.py` | Asserts the picture appears *before* the UI chrome on a fresh run. |
| `check_js_syntax.py` | **After every single `.js` edit.** |
| `check_scene_layers.py` | When the screen is black. Interrogates the real DOM layers and the Reactor video layer. |
| `run_full_playtests.py` | The whole battery against a running server; writes `playtest_results/full_run_<stamp>/master_report.json`. |
| `tools/*_probe.py` | Single-question probes for prompts, cuts, lanes, staleness, vision cache. Copy the pattern for a new question. |

Unit and e2e suites are `unittest` modules, documented per-file as
`python -m unittest <module> -v`. The e2e suites boot a real server as a
subprocess in mock mode, so they exercise real HTTP paths for free. Playwright
is needed for the browser suites (`pip install -r requirements-dev.txt` then
`playwright install chromium`). `authoring_sandbox` engages on import of any
authoring store, so a test run works on copies — it used to overwrite the live
prompt file, `worlds/world.json` and `tunables.json`.

Fast pre-flight before anything else:

```powershell
python -m py_compile (Get-ChildItem *.py)   # PowerShell does not glob for python
python -c "import api"                      # imports engine, choices, etc. transitively
```

### The rules that each cost us a session

1. **Run `python check_js_syntax.py` after every `.js` edit.** A syntax error in
   `standalone.js` kills the client *silently* — the app launches, the menu
   renders, nothing works, no error dialog.
2. **CDP screenshots do not capture the WebView2 video layer.** A screenshot can
   look perfectly healthy while the user's screen is black. Never conclude "it
   renders fine" from a screenshot; use `check_scene_layers.py`.
3. **A green harness is not a working feature.** It catches black screens,
   stalls, dead buttons and non-resolving turns. It cannot tell you whether a
   close-up is well framed or an encounter feels exciting. Read `SUMMARY.md`,
   look at the contact sheet in the run folder, and show Matt frames. The last
   three "working" reports were all harness-green with a visibly broken feature.
4. **Add to the harness, don't fork it.** New verb → a branch in the turn loop
   next to `scan_move` / `scan_interact` / `photo` / `encounter` / `act` /
   `camp`, and an entry in the `plan` list. New invariant → append to
   `findings`, which is the pass/fail.
5. **Don't threshold on image similarity.** Perceptual hashing was tried and
   produced constant false positives on this game's subtle frame changes. Use
   exact byte hashing for identity and the `continuity()` helper (mean absolute
   difference) for similarity — *report the number, look at it*.
6. **A turn takes ~29–30s on the stills path.** Anything assuming faster reports
   false stalls.
7. **A reset deletes `sessions/` and `.cache/`, nothing else.** Source, `.env`,
   `ai_config.json`, `prompts/`, `worlds/`, `experiences/` and
   `assets/references/` are authoring data. If a reset ever takes those, that is
   the bug, not something to work around. Repair tools live in `tools/`
   (`restore_polluted_world.py`, `sync_character_into_worlds.py`,
   `retire_stale_flipbook_prompts.py`, `edit_prompt_everywhere.py`).
8. **Sweep artifacts.** `python tools/clean_artifacts.py --apply` keeps the
   videos and transcripts and drops the stills; each MP4 already contains every
   frame in order. A 60-turn Pro render at 2K is ~40 minutes and a gigabyte.

---

## 7. Conventions worth matching

**The changelog is the deliverable.** Entries are written in a specific voice:
quote what the player actually reported, explain the *mechanism* of the bug
(usually more interesting than the fix), say what changed, then say **how it was
verified by playing it** — with the actual prose or log line that came back.
Read three entries before writing one. Headings are `## ✅ FIXED:`, `## ✅ NEW:`,
`## 🎨`, `## 🧪` and so on, newest date section at the top of the file.

**Prompts before code.** Most misbehaviour is an instruction, not a bug. And
when you do change a prompt, verify against the *assembled* payload.

**Tests encode invariants, not coverage.** The interesting ones assert things
like "no unwired prompt keys", "the essentials alone can drive every compile
stage", "the picture arrives before the chrome". Add to that register.

**Both editors, or it isn't shipped.**

**Match the surrounding prose style in docs and comments.** This repo's comments
explain *why a thing is the way it is*, usually with the incident that caused
it. Keep that; it is the reason the codebase is navigable at this size.

---

## 8. Open threads

- **The flipbook is a keyframe contract, and reference slot 1 is the start
  keyframe.** Read `flipbook.grid_prompt`'s docstring and the CHANGELOG entry
  ("the flipbook never said what panel 1 was") before touching any flipbook
  prompt text. The shape: panel 1 = the previous sequence's last panel
  (camera, spot AND pose), the last panel = the turn's `visual_scene`, the
  rest = in-betweens of the CHOICE, the rig held and travelling; a cut turn
  keeps the start keyframe and demands arrival by the LAST panel. Three
  things are load-bearing and easy to undo by accident: (1) the previous
  panel goes in reference slot 1 (`lead_reference`) — the img2img template
  says "the attached image is the PREVIOUS moment", singular, and the model
  reads that against slot 1, so with the plates first the *plate* was the
  previous moment (four armed figures in panel 1); (2) `identity_seed` is
  dropped on a continuing turn, cut or not — seeded, the grid was told
  "there is no reference image" under five attachments; (3) the references
  carry their own captions (`reference_labels`) because the image layer's
  generic one says "do NOT copy the person in this frame". There is no init
  image anywhere in this pipeline (`strength` is accepted and never read), so
  continuity is persuasion; the mechanical version — composite the previous
  panel into cell 1 of the grid reference and ask for an extension — is the
  untested next step if persuasion is not enough. The A/B harness for this is
  `_claude_ab_flipbook.py` (gitignored): three real turns replayed through
  `_flipbook_generate` with their recorded references, grids to
  `_claude_pull/ab_flipbook/`.
  Also from that session: a `python -m unittest` run of the flipbook/render
  suites posts `/api/reset` against the REAL `sessions/default` (the sandbox
  leaves sessions alone by design) — it wiped run 9 mid-investigation. Don't
  run the suites with a run you care about on screen.

- **The loop trace, and what it left open.** On 2026-09-20 every handoff in
  the loop was traced in the source and then in four traced runs of the real
  app (`PT_TRACE=1` in `playtest_app.py` records, per turn, what each system
  knew; `_playthrough/loop_trace.json`). The fixes and the evidence are the
  top section of `CHANGELOG.md`; the run's goal now reaches the consequence
  prompt, the slate, the objectives sheet (a GOAL row), the narrator and the
  encounter brief, and the consequence model answers `goal_reached`. Read
  that section before chasing anything below. Still open, in the order they
  matter:
  - **Nothing ends a run but death.** `goal_reached_turn` is a fact the run
    knows now; the Experience graph has no transition type that reads it
    (`condition_met` knows `turn_count`, `game_over`, `immediate`), and both
    shipped Experiences have `transitions: []`. A `goal_reached` edge ("on
    reaching the goal → next World / cutscene") is the obvious next step and
    needs both editors.
  - **Encounters: a person in the frame IS one now** (`encounter.sighting_*`,
    `engine._stage_encounter_sighting`, `api_detect` → `encounter_with` →
    client `openSightingEncounter` → `Encounter.start({subject, sighting})`
    → `api_begin` with the figure's close-up as a cast plate). The walk clock
    (`ENCOUNTER_TRAVEL_*`, 8–15 s / 14–26 s) is the floor under it and still
    draws from the roster when nobody is in frame. `ENCOUNTER_SIGHT_COOLDOWN_TURNS`
    (3) and `encounter_last_label` stop an escape's frame re-opening the same
    fight. Whether "critical + hunted" should force one with nobody in frame
    is still a design call.
  - **A fight leaves the player more hunted than it found them**: each round
    is `apply_detection(interaction=True)` (+1 heat) and threat +2, the odds
    stay pinned to the opening level, nothing resets on a win, and
    `player_state.condition == "wounded"` reaches nothing but the next
    fight's odds. MOVE TO takes the same +1 "meddling" bonus (`scan_move` is
    in `is_interaction`), and a tag labelled *exit* / *open ground* sets
    `fleeing` and cools heat instead.
  - **SCAN goes dark when hunted** (the anti-loop gate returns no tags at
    `DETECT_HUNTED` unless one is an egress) and the player is not told why
    the picture stopped answering.
  - The active World's `image_art_direction` / `image_negative_prompt` are
    the factory copies ("1993… NEVER: neon, sci-fi") on a cyberpunk neon
    level; the plate wins, but the image model reconciles them every frame.
    And that plate still has four armed figures in it — a run-8 aftermath
    drew a silhouette in a doorway nobody wrote. Both are authoring.
  - `also_relocating` (the TRAVERSAL directive) is decided from the wording
    while the cut is decided by the model's `relocated`; a typed relocation
    the model confirms gets no traversal guidance. `generate_and_apply_choice`
    and `_record_companion` read-modify-write the state file outside
    `WORLD_STATE_LOCK`. The drift prompt asks for a
    `world_tick_micro_change_instructions` key that does not exist (drift is
    off by default). A first encounter whose plate render fails holds a black
    "developing" frame for ~60s of retries.
  - `test_world_authoring` (6), `test_somewhere_snapshot` (1) and
    `test_exit_button`'s two browser tests are red on this machine for
    environmental reasons (they read the live cast sheet / assert `somewhere`
    is active / boot the launcher in mock mode against this machine's active
    World). `test_standalone_e2e` used to be in that list and is not any more
    — it engages the authoring sandbox and plays the shipped Experience; the
    same treatment would fix `test_exit_button`.

- **The live prompt file is a union of game doctrine and World authoring, and
  a World bind rewrites it.** Understand this before editing it: whatever you
  put in `prompts/simulation_prompts.json` lasts until the next New Game binds
  the active World's copy over it. Edit a prompt with
  `tools/edit_prompt_everywhere.py` (live + factory + every World) or the
  `_claude_fix_prompts2.py` pattern (same reach, exact match, no tidy pass).
  The guard in `worlds_store` (`DOCTRINE_KEYS`, now four keys including
  `image_camera_rules`) substitutes the factory copy for a stored value that is
  the harness fixture, byte-for-byte OR the fixture plus lines the factory
  itself carries. `world_initial_state` on the active World is still the
  266-char harness placeholder — that one is the World's to author.

- **Anything that boots the server in a subprocess must carry the sandbox's
  environment.** `authoring_sandbox.engage()` only redirects the paths of the
  process that called it; `test_standalone_e2e` shows the pattern (engage,
  copy the `SOMEWHERE_*` vars into the child's env, drop `.active`). A
  subprocess without it plays on — and rebinds — the real live prompt file.

- **`_generate_random_starting_time` must run after the World bind** (it
  does now). The lighting line it rolls goes into every render of the session
  as "Lighting:", and it reads the level's palette off the live prompt file.

- `docs/plans/` is mostly **shipped work**, kept as design records. Every file
  opens with a **Status** line; trust it over the body, and never implement a
  plan marked shipped. Genuinely open: `FATE_ROLL_VISUAL_PLAN.md`,
  `PROMPT_TRIM_PROPOSAL.md` (see above — the tests assumed it), and the "fully
  local models" arc at the end of `DESKTOP_APP_ROADMAP.md`.
  `GAME_DESIGN_LAYERS_PLAN.md` is worth reading as a cautionary tale.
- `worlds/` has accumulated a dozen `new-level*` / `world-N` scratch snapshots
  around the two that matter (`somewhere.json`, `world.json`). Worth a tidy,
  carefully — these are authoring data.

---

## 9. Working with Matt

He has very good instincts for game feel and reports bugs the way a player
experiences them ("it popped back to the previous frame", "being suspicious was
decided by the narrator's word choice"). Take the report literally, find the
mechanism, and don't explain away a symptom. He will tell you when something
feels unfun or unfair; that is the signal that matters most.

Show your work with frames and prose, not with green checkmarks.
