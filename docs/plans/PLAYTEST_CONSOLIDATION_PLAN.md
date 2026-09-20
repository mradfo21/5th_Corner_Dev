# 🎮 One PLAYTEST — consolidation plan

> **Status: partly done, and the answer landed somewhere else.** The single
> harness that matters turned out to be `playtest_app.py` — it drives the **real
> native app** over CDP — with `playtest.py` keeping the API-level probes. The
> 59 test files were not consolidated. For what to actually run and why, read
> [`../operations/TESTING_USE_THIS.md`](../operations/TESTING_USE_THIS.md); it
> supersedes this plan's Part 3 and Part 5.

## The problem, stated plainly

There are 59 `test_*.py` files (≈24,000 lines, ~1,900 `def test_*`) and the
game still breaks in ways none of them catch. Reading `CHANGELOG.md` — 1,295
lines of dated entries — makes the pattern obvious: almost every real bug
("encounters casting the player as the hostile," "a finished encounter never
ending," "the enemy inheriting the player's clothes," "the choice slate
grounded on the picture we asked for, not the one we got") was found by
**someone actually playing the game**, then pinned afterward as a narrow,
heavily-mocked unit test on the specific function that broke.

That ordering is the root cause. A test written after the fact, against a
mock of one function, proves that *exact* regression can't recur — it proves
nothing about the next one, because it never runs the thing that actually
failed: the live turn loop, with a real server, a real feed, a real image
call, real state mutation across turns. Meanwhile 59 files of that shape cost
real time (to write, to maintain, to not-quite-trust) without buying
confidence that the game currently plays.

The good news: the tool that *would* catch these bugs already exists, in
pieces, unused by default:

| File | What it already does |
|---|---|
| `playtest_interactive.py` (1,580 lines) | Boots against a running server, plays N turns via the real `/api/*` endpoints, drives SCAN→MOVE object interactions, ticks world drift, saves every frame + a review video, and already has a `summarize()`/`checks` verdict system |
| `autoplay.py` | Same idea, choice-button only, checks image freshness / narrative fallback / choice regeneration |
| `playtest_capture.py` + `playtest_analyze.py` | Captures a full session to JSON, then scores feed-ID monotonicity, turn structure, choice variety |
| `play_log.py` | Append-only JSONL of every begin/resolve/turn, plus `diagnose_encounter()` flagging wardrobe collisions and unseen-hazard mentions |
| `run_full_playtests.py` | Runs all of the above plus a handful of `unittest` modules and writes one `master_report.json` |

Nobody runs `run_full_playtests.py` on a schedule or as a gate. `playtest_results/`
is full of one-off diagnostic folders (`hardcut_diag`, `antiloop`, `egressfix`)
— proof this is already the actual debugging workflow, just manual and
per-incident instead of continuous and canonical. And none of it ever forces
an **encounter** — `/api/encounter/begin` exists and can be called directly,
but every harness only ever waits for one to happen (or doesn't check).

## The plan in one sentence

Merge the existing playtest scripts into **one canonical script**,
`playtest.py`, that plays a full session end-to-end — normal turns, SCAN/MOVE,
a custom free-text action, a forced encounter through its own resolution, a
forced death and restart, across more than one World — logging everything and
failing loudly on any error, stall, or broken invariant; then triage the 59
unit tests against it: fold the ones that duplicate a real gameplay behavior
into `playtest.py`'s check registry and delete the test file, keep the ones
that are cheap, pure, and check something a full play session can't (money
math, packaging, key handling).

---

## Part 2 — being honest about what will actually work

The first pass at this section proposed five named "personas," an LLM
critic that grades the story on a rubric and emits its own severity
levels, and an unattended loop that decides for itself when to stop. That's
overreach. An LLM judging its own game's coherence, running unattended, with
no one checking its verdicts, is not a QA department — it's a coin flip
that's confident about itself either way. Committing to that as the plan
would mean trusting a model's self-graded "this looks fine" the same way the
current bug list got there in the first place: something claimed to work,
nobody looked closely enough, it shipped anyway.

So split this by what's actually reliable:

**Code is reliable at exactly the things code is reliable at.** Decoding an
image and checking whether it's a solid color is deterministic, cheap, and
either right or wrong — no judgment involved, nothing to trust blindly. Same
for hashing two frames to see if they're actually different, tailing a log
for a traceback, or checking that a turn counter went up. All of this stays
in `playtest.py` exactly as originally scoped, and none of it needs an LLM
call to work.

**An LLM grading its own output, unattended, is not reliable**, and building
a five-persona autonomous critic loop around that premise doesn't change it.
Where judgment is genuinely needed — is this scene interesting, does this
frame look right, has the story stalled — the honest answer is that this
needs a real reasoning pass by someone actually looking, not a scripted API
call pretending to be that. The capability that's actually proven, right now,
in this conversation, is: an agent session (this one, or another Cursor
session) can look at real frames and a real transcript and say what's wrong
with it, the way I'd review any other artifact you handed me. That's a much
smaller claim than "the system judges itself," and it's the one that's true.

### 2.1 What code checks on its own (no LLM, every run, `--mock` included)

- **Solid-color / placeholder detection.** Decode the frame, check for a
  single dominant color over most of the pixels — catches a black screen,
  a grey error tile, and specifically the mint-green ramp
  `world_frames._placeholder_png()` produces (fingerprint its exact gradient
  and match against it directly, not just "is it flat"). This is the same
  bug class commit `96425e9` fixed for one call site (the World's cached
  opening frame, via `world_frames.is_real_still()`); running the same kind
  of check against every frame of every turn closes the gap for the rest of
  the run.
- **Frame-hash diff against the previous turn.** `scene_changed` today is
  `new_url != old_url` — two different URLs can be pixel-identical. A cheap
  perceptual hash catches "claims to be new, looks the same" as its own
  distinct, code-only finding.
- **Text-repetition check across turns.** Not "is this good writing" — just
  "is this turn's narrative near-identical to an earlier one" (a plain
  string-similarity ratio, no model call). That alone catches the
  mechanical half of "the story is stuck in a loop," which is a real,
  checkable thing, separate from "is the story any good," which isn't.
- Everything already in Part 3/4 below: turn resolution, error events,
  server tracebacks, choice-slate size, chaos/time-of-day movement.

None of this pretends to have taste. All of it is either right or wrong, and
stays right every time it's run.

### 2.2 What still needs a real look — and who actually does that

Drop the in-script LLM critic and the persona table. Replace both with one
concrete step: `playtest.py` ends every run by writing a **small, reviewable
bundle** — `SUMMARY.md`, a contact sheet (one grid image, not a folder of
forty PNGs) of the frames from each forced turn kind, and the narrative text
for each turn — sized so a person, or an agent session, can actually read
the whole thing in one pass instead of drowning in a `session.json`.

Then: **an agent session reviews that bundle directly**, the same way I'd
review any screenshot or log you handed me — actually looking at the contact
sheet, actually reading the transcript, flagging "turn 6's frame is a black
rectangle," "turns 9–12 are the same beat with different nouns," "the
encounter never resolved." That's real capability, not simulated judgment,
and it's the thing that should be trusted with the parts of this that
genuinely need judgment. It doesn't run unattended and it doesn't grade
itself — it runs when you (or I, on your ask) decide to run it, and the
output is a person or an agent actually saying what's wrong, same as this
conversation.

### 2.3 Variety without a persona system

Drop the per-turn LLM policy call and the five personas — that's a model
call spent on picking a button, which is expensive and not obviously better
at finding bugs than just making sure the run visits every path on purpose.
Replace with:

- **Forced probes, every run, no randomness needed to reach them:** one
  SCAN→MOVE turn, one custom free-text action, one encounter forced via
  `/api/encounter/begin` through to resolution, one forced death and
  restart. These are the turn kinds `CHANGELOG.md`'s bugs actually live in
  — reaching them on purpose beats reaching them by chance or by persona
  flavor.
- **A logged random seed for the ordinary choice turns** in between, so the
  run isn't always "press button 1" but still reproduces exactly if it finds
  something. This is plenty of variety for a mechanical harness; it doesn't
  need a taxonomy of fictional playtesters to get there.
- Cycle through more than one World across runs (still true from the first
  draft) — that's still the highest-value source of variety, and it's free.

### 2.4 What "self-improve until bug-free" actually looks like

Not an unattended loop with its own stopping logic. A habit, with a cheap
mechanical half and a real-judgment half:

1. **Run `playtest.py`.** Mechanical checks (§2.1) either pass or don't —
   no judgment call needed to read the result.
2. **Review the bundle** (§2.2) — you, or an agent session asked to. This is
   the step that catches what code can't: does this actually look right,
   does this read as progress, is anything here just quietly bad.
3. **Fix what's found, then pin it** exactly as already planned: the fix for
   anything caught in step 1 *or* step 2 becomes one new named check in
   `playtest.py`'s registry — confirmed red before the fix, green after — so
   the mechanical half of the check surface keeps growing and step 2 has
   less and less to find over time.
4. **Repeat as a habit**, not a background process: before a ship, when
   something feels off, or periodically on request. The system gets less
   buggy because the loop keeps running and each pass adds a permanent
   check, not because an unattended process declared victory on its own.

That's the honest version: the mechanical layer is trustworthy because it's
just code; the judgment layer is trustworthy because it's an actual review,
by someone capable of reviewing, not a script wearing a rubric.

---

## Part 3 — What `playtest.py` needs to do that nothing today does

### 1. Force the things that are supposed to happen, don't wait for RNG

- Call `POST /api/encounter/begin` directly instead of hoping one triggers
  in N turns of ordinary play. Play the encounter to a resolution
  (`survive` / `escape` / `die`) via `POST /api/encounter/resolve`, then
  confirm the session hands back to ordinary play cleanly (no stuck plate,
  no leftover choice slate from the fight, next turn's frame is not the
  standoff still).
- Force a death (either via the encounter's `die` outcome or by whatever
  ordinary path ends the game) and confirm `POST /api/reset` / the
  "Restart Simulation" choice actually restarts, rather than leaving a dead
  session that a harness keeps politely "playing" (this exact bug is
  already pinned in `test_playtest_interactive.py::TestDeathIsNoticed`
  — it belongs in the real harness, not a harness-of-the-harness).
- Drive at least one SCAN→MOVE turn, one plain choice turn, one custom
  free-text action, and (when `WORLD_DRIFT=1`) one ambient world-drift tick
  — every turn *kind* the client can produce, every run, not spread across
  four separate scripts that each only exercise their own slice.
- Play more than one World. Today every harness defaults to whatever is
  loaded (`SOMEWHERE`). `worlds/` has a dozen authored levels sitting
  unexercised — a playtest that always runs the same World cannot catch a
  world-specific prompt-authoring bug (the class of bug most of
  `CHANGELOG.md`'s August/September entries are).

### 2. Detect errors, not just missing happy-paths

Right now "error detection" is scattered and mostly implicit (a turn that
times out, a `status` that never resolves). `playtest.py` needs an explicit,
first-class error surface:

- **Feed-level:** any `error_event` item, any `[object Object]` leaking into
  narrative/choices/labels (a real bug class per `test_playtest_interactive.py`),
  any `metadata.degraded` narrative counted and reported, not silently passed
  as "real text."
- **Transport-level:** any non-2xx from any `/api/*` call, any exception raised
  by the harness itself (currently several scripts swallow this into a
  timeout with no traceback attached).
- **Server-level:** tail the server's stdout/stderr (or `logs/somewhere.log`
  in a frozen build) for the run's duration; a Python traceback anywhere in
  that window fails the run even if the HTTP response looked fine — this is
  exactly the "critical unhandled error" path in `engine.py`'s
  `_process_turn_background`, which today can log a traceback while the feed
  item still says something plausible.
- **Invariant-level:** the checks that already exist across `autoplay.py`,
  `playtest_interactive.py`, and `playtest_analyze.py` — turn resolves,
  choices regenerate and never fall below a full slate, image is new (not a
  stale reused frame), scanned subject persists into the next scene, chaos/
  time-of-day actually move, no wardrobe collision (`play_log.py`'s
  `diagnose_encounter`) — become one shared **check registry** instead of
  three overlapping, slightly-different implementations.

### 3. Log everything, in one place, in a form built for triage

- One run = one folder in `playtest_results/<stamp>/`: `session.json` (full
  transcript), `SUMMARY.md` (human-readable, checks + narrative excerpts,
  already what `playtest_interactive.py` produces), frames + review video for
  any turn with a picture, and a JSONL event log via `play_log.py` for
  anything that isn't naturally a "turn" (server errors, forced-encounter
  begin/resolve, world switches).
- Exit code is the gate: `0` only if every check passed and zero errors were
  seen. `run_full_playtests.py`'s pattern of writing a report but still
  returning cleanly is what let this rot unused; `playtest.py` must be the
  kind of script someone actually looks at when it turns red.

### 4. Two modes, one script

- `--mock` — offline, no keys, deterministic-ish, fast. This is the one that
  can run on every push with zero cost and zero flake budget.
- `--live` — real backend, real images, real encounter prose. Slower, costs
  API budget, but is the one that actually exercises the prompt-authoring
  bugs `CHANGELOG.md` is full of. Run before every ship, and on-demand.

---

## Part 4 — Triage the 59 existing test files

Categories, not a line-by-line audit (available on request, but the pattern
holds file to file). "Fold" means: extract the specific behavior it protects
into `playtest.py`'s check registry — as a live-run assertion, not a mock —
then delete the file.

### Delete or fold: gameplay-loop regression pins built on mocks (37 files)

These test one function of the live loop by mocking everything around it.
Each one exists because a real play session found a real bug — which is the
argument for folding the *behavior* into `playtest.py`, not for keeping the
mock. A mock of `gemini_image_utils.generate_gemini_img2img` proves the
caption logic is right; it does not prove the fight actually ends, or that
the next scene isn't the standoff plate.

`test_encounter.py`, `test_encounter_fight.py`, `test_encounter_roster.py`,
`test_encounter_custom_action.py`, `test_narrator_grounding.py`,
`test_object_permanence.py`, `test_scene_objects.py`, `test_detect_filter.py`,
`test_choices_robustness.py`, `test_simulation_pacing.py`, `test_world_drift.py`,
`test_danger_simulation.py`, `test_move_moves_the_camera.py`,
`test_turn_memory.py`, `test_camp_companions.py`, `test_conversation_moments.py`,
`test_director.py`, `test_cutscene.py`, `test_viewfinder.py`,
`test_third_person_orbit.py`, `test_transition_types.py`, `test_ceremony_hud.py`,
`test_watch_overlay.py`, `test_flipbook.py`, `test_scene_audio.py`,
`test_talk_voice.py`, `test_voice_design.py`, `test_local_vision.py`,
`test_local_vision_e2e.py`, `test_providers.py`, `test_feed_engine.py`,
`test_game_identity.py`, `test_world_authoring.py`, `test_world_frames.py`,
`test_render_mode.py`, `test_krea_provider.py`, `test_fal_provider.py`

**Not a blind delete.** Each file's docstring already explains the exact
regression it exists to prevent (they're unusually well-documented for this
codebase) — that paragraph is the spec for the `playtest.py` check that
replaces it. Phase 1 below is "read the docstring, write the live-run
assertion, delete the file," file by file, not "delete 37 files."

### Fold, cautiously: browser/editor E2E (11 files)

`test_standalone_e2e.py`, `test_editor_graph_e2e.py`, `test_editor_wiring.py`,
`test_editor_lands_e2e.py`, `test_editor_responsiveness.py`,
`test_movement_mode_e2e.py`, `test_realtime_e2e.py`, `test_experience_mode.py`,
`test_experience_graph.py`, `test_concurrent_sessions.py`, `test_exit_button.py`

These are Playwright-driven and actually click a real browser against a real
server — closer in spirit to a playtest than the mocked group. But they test
the **World Editor / authoring UI**, not the play loop. That's a genuinely
different surface (nobody "plays" the editor) and folding it into a game
*playtest* would blur what failed. Recommendation: leave these as-is for now;
they're candidates for a second, sibling harness (`edittest.py`?) later, not
casualties of this plan. Revisit only if they prove to duplicate coverage
`playtest.py` gets for free (e.g. `test_concurrent_sessions.py` may already be
subsumed once `playtest.py` runs two sessions in parallel).

### Keep: cheap, pure, and checking something a play session can't (11 files)

`test_pricing.py`, `test_cost_tracker.py`, `test_billing.py`, `test_coinop.py`,
`test_usage_limits.py`, `test_keys_store.py`, `test_outbound_dns.py`,
`test_analytics_api.py`, `test_somewhere_snapshot.py`, `test_prompts_store.py`

Money math, packaging/shipping correctness, and key-handling security are
exactly what a unit test is *for*: fast, deterministic, no server, no mocks
of the interesting part. `test_somewhere_snapshot.py` is explicitly the ship
gate in `docs/operations/SHIPPING.md` and stays one.

### Becomes part of the harness, not a companion to it (1 file)

`test_playtest_interactive.py` — its whole point is keeping the harness
honest against the client's actual source (`static/js/standalone.js`). That
job doesn't go away; it moves to being `playtest.py`'s own self-test
(`test_playtest.py`), checked against the merged script instead of the one
being retired.

---

## Part 5 — Rollout

1. **Build `playtest.py`** by merging `playtest_interactive.py` (keep as the
   spine — it already has frames/video/checks/world-drift/live-beats),
   `autoplay.py` (fold its narrative-fallback + image-freshness checks in),
   `playtest_capture.py` + `playtest_analyze.py` (fold feed-ID monotonicity +
   turn-structure checks in), and `play_log.py` (used for the error/event
   log alongside the run folder). Add: forced encounter turn kind, forced
   death + restart verification, multi-World cycling, server-log tailing for
   tracebacks.
2. **Add the code-only checks (§2.1).** Solid-color/placeholder detection,
   frame-hash diffing, text-repetition detection. All free, all deterministic,
   all run in `--mock` too — no model call, nothing to trust blindly.
3. **Add the forced probes and the seeded picker (§2.3).** Every run visits
   SCAN→MOVE, a custom action, a forced encounter through resolution, and a
   forced death+restart on purpose; ordinary choice turns use a logged
   random seed instead of always index 0. No persona system, no per-turn
   model call.
4. **Add the review bundle (§2.2).** `SUMMARY.md` + one contact-sheet image
   + the narrative transcript, sized to actually be read in one pass. This
   is the deliverable a person or an agent session reviews — not a scripted
   verdict.
5. **Retire `run_full_playtests.py`** — replaced by `playtest.py --live`
   itself; it becomes the thing that gets run, not the thing that runs things.
6. **Triage pass, file by file**, per Part 4: for each "delete or fold" file,
   turn its docstring's regression into a named check in `playtest.py`,
   confirm the check fails without the original fix (git-stash the fix,
   confirm red, restore), then delete the test file. This is the "does the
   new test actually catch the old bug" proof the current suite never had to
   provide.
7. **Wire it as a gate.** There's no CI in this repo today (no
   `.github/workflows/`), so "gate" means a local habit backed by a script,
   not a GitHub check: add `playtest.py --mock` to whatever pre-push/pre-ship
   step exists (`docs/operations/SHIPPING.md`'s "Prove it" section), and make
   `--live` plus a bundle review mandatory before a Ship.
8. **Make the review a habit, not a service.** §2.4's loop is "run, review,
   fix, pin, repeat" — on request, before a ship, or whenever something
   feels off. Nothing here runs unattended or grades itself.

## Open questions

Decisions this plan doesn't make for you:

1. **How big the review bundle should be** — how many forced-probe frames
   go in the contact sheet, how much transcript is enough for a one-pass
   read without losing the turn that actually broke.
2. **How the text-repetition threshold gets tuned** — a plain similarity
   ratio needs a cutoff that flags real stalls without false-positiving on
   two turns that are legitimately similar (two ordinary "walk forward"
   beats in a row is not a bug).
3. **Cadence for the habit** — run-and-review before every ship is the
   floor; whether it's also worth doing on some regular cadence in between
   is a call about time/budget, not something this plan should fix in
   advance.
