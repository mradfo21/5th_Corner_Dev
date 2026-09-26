# 🎞️ The tape as a film — the run's key frames, filmed between

> **Status: proposed 2026-09-25. Nothing is built.** Phase 0 is a paid probe
> (~$3 on the Gemini key, Matt's go-ahead first). It gates the rest: Omni's
> docs don't say how long a clip takes, how its length is chosen, or how
> cleanly two clips that share a key frame join.
> Supersedes [`VEO_VIDEO_BASED_IMAGE_GEN.md`](VEO_VIDEO_BASED_IMAGE_GEN.md)
> (dormant and broken, §8).

Matt, 2026-09-25. First, video between frames while playing: *"using just
google omni 360p fast … building this in a way where we can use other tools
in the future."* Then, on seeing the timeline that needed: *"i feel thats just
too slow to get into the flow state."* So:

> *"for the tape mode.. lets find a way to take our frames, just the KEY
> frames, not all flipbook frames, and use them to create a film, via omni,
> using first frame last frame as an extension technique (or if there is a
> more native method to that) so that after you die (or at any moment), you
> can render the film of your playthrough."*

## 1. Why the film and not the turn

A clip between two turns needs the second turn's still as its last frame, so
it can only start once the still exists. Its whole generation time lands on
every turn, on top of the ~11 s the still already takes. The tape has no one
waiting. Every key frame already exists, every pair is known, and all of them
can be filmed at once. The same contract (§4) can come back to per-turn play
if video models ever get fast enough.

## 2. What is already there

THE TAPE (`run_tape.py`, CHANGELOG) records every run as it plays:

- `sessions/<sid>/runs/<run>/tape.json`: a list of SHOTS in the order they
  were on screen. Each has a kind (`montage`, `opening`, `turn`, `encounter`,
  `round`, `death`), `turn`, `world`, `action`, `caption`, `prose`, the
  still's `prompt`, and its `frames`.
- `frames/`: every frame, hard-linked, so the run survives New Game's purge.
- `chapters`: where the run crossed into another World, or changed look.
- `timing(shot)`: the one pacing rule the player and the export share.
- **The Reel** (standalone.js `Reel`). It opens from the death screen (WATCH
  THE TAPE, T), the pause sheet (TAPE), T at any moment, and the world
  picker. It has an EXPORT sheet with two options, SHOT PACK and ANIMATIC,
  run by `run_tape.export_job` and delivered to `Videos/ABYSS Tapes`.
  **FILM is a third option on that sheet.** "After you die, or at any moment"
  needs no new way in.

One real run, `default/runs/20260924-144903-42e9` (Jean-Luc, The FIFTH
CORNER, died in round 2 against the Vibrating Air), shows the shape:

- 50 shots: 6 montage, 2 opening, 19 turn, 6 encounter, 16 round, 1 death.
- 188 frames on disk. Every non-montage shot is a 4-panel flipbook.
- The key frames are **672×376**: a flipbook panel is a quarter of a 1K
  grid. The montage stills are 1376×768.
- Omni's 360p (640×360) is therefore the tape's own resolution. Filming it
  loses nothing, and 720p would upscale our own frames at 3× the price.

## 3. The film

### 3a. Key frames: one per shot, its last frame

A shot's last frame **is the turn's still**, the frame the game treated as
the truth of that beat. The flipbook's in-betweens are never key frames.
`montage` shots are the one exception: every frame in one is a key frame.
The montage is "four cameras on one moment with time held" (tunables.py), so
each of its frames is a separate camera, not an in-between.

### 3b. Segments: a clip from each key frame to the next

The keys K1…Kn are walked in order. Each boundary is one of two things:

| Boundary | What the film does |
|---|---|
| **Continuous** (the default) | one Omni clip, **first frame Ki, last frame Ki+1**, prompted with what happened in shot i+1 |
| **Cut**: into, out of, or inside a montage; a World chapter; a shot recorded `cut` | a hard cut. A held still (the montage) gets a slow push-in with ffmpeg `zoompan`, which costs nothing. The Reel already pushes in on a hold |

Adjacent clips share their key frame exactly: one ends on Ki+1 and the next
starts on it. That shared frame is the "first frame / last frame as
extension" Matt described. It is the native way to chain a film through key
frames you already chose. The stitcher drops the duplicate frame at each
join.

**Omni's own extension** (`previous_interaction_id`: 10 s steps, 40 s
cumulative) is the other native method, and on its own it can't make this
film:
- it caps at 40 s, and this run is 41 moves (2–3 minutes of film);
- it has no documented way to aim at a key frame, so the film would drift
  away from the run that was played;
- each step waits for the last, so it can't run in parallel.

It may still help *inside* a scene (§6, Phase 0 variant c).

**Nothing is recorded to say a turn moved somewhere.** A shot doesn't carry
the turn's `hard_transition`, so on old tapes only montages and chapters cut,
and a MOVE TO is interpolated as the walk there. That may be exactly right: a
walk to the thing is the move a video model is best at. From Phase 1 on,
`run_tape.record` takes `cut=` from the turn's `hard_transition`
(engine.py ~10852), so the film can choose per move once Phase 0 has shown
what a relocation looks like interpolated.

### 3c. Each clip's words come from the tape alone

- **What happens:** `_video_prompt(shot)`, which already exists. It is the
  action, what came of it, and the narration, for "the motion between two"
  frames. This is its first live reader.
- **The contract**, generated in code the way `flipbook.grid_prompt` is: one
  continuous take from the first image to the second; the camera holds its
  rig (the tape's own render prompt opens with its camera block); the
  character keeps their look; no cuts, no text, no one who isn't in the
  frames. Negatives go in the prose, because Omni has no negative field.
- **Who:** the look the character wore at that shot. `tape.looks`, the
  `look` chapters and `_character_refs` already give its turnaround sheet,
  which is attached as an image reference (Omni takes `<IMAGE_REF_N>`). It is
  never the first frame: "a portrait there produces eight seconds of that
  portrait" (CAST_AND_CAMERA).
- **Never the live prompt file.** A World bind rewrites it, so an OUTGROWTH
  film rendered while CYBER HORROR is bound would come back in neon. The tape
  is self-contained, and the film is made from it and nothing else.

### 3d. Length and cost

- **Clip length, a choice on the sheet.** TAPE pacing is `timing(shot)`
  clamped to 3–6 s (Omni's floor is 3), so the film is paced like the tape
  the player watched and one wordy beat can't buy 10 s. TIGHT is a flat 3 s.
- **The Jean-Luc run, computed from its tape:** 41 clips (World chapters
  fall on montages here, so no extra cuts), plus 41 s of free montage holds.

  | Pacing | Generated | Film | 360p | 720p |
  |---|---|---|---|---|
  | TAPE | 190 s | 3:51 | **$6.46** | $19.00 |
  | TIGHT | 123 s | 2:44 | **$4.18** | $12.30 |

  A 10-turn run is about $1.50–2.50.
- **Render time:** every clip is independent, so they go out 3 at a time.
  Omni's 429s back the pool off to one. If a 360p clip takes 30 s, that run
  is ready in about 7 minutes. Phase 0 measures the real number.
- **Resumable and incremental.** Clips are cached in
  `runs/<run>/film/clips/` under a key of (both frames' bytes, prompt,
  provider, model, resolution, seconds). Rendering again later films only
  what's new: render at turn 10, die at turn 25, and the second render buys
  15 clips, not 25. It also makes "retake this shot" a cache delete (Phase 4).

### 3e. Stitching

- ffmpeg, the imageio-ffmpeg build that already ships (ABYSS.spec collects
  it).
- Every clip is normalised to one size, fps and codec, and the duplicate
  first frame of each continuing clip is dropped.
- An optional 2–3 frame dissolve at each join hides two renditions of the
  same key frame disagreeing.
- Montage holds are made with `zoompan`. H.264 `yuv420p +faststart`, the
  recipe `build_animatic` and `render_jobs.save_live` already use.
- **Captions:** the shot's `action` / `caption` lines are burned over their
  clip, using `tools/cut_clips.py`'s own renderer (`caption_frames`: PIL
  PNGs, no libass). `captions.srt` is written beside the film.
- **Sound.** Omni makes audio with every clip, whether we want it or not, and
  per-clip audio won't join. Phase 1 keeps it or mutes it, whichever Phase 0
  says is better. Phase 3 scores the film from the shipped sound library.
  `sound_library.pick` is pure, so the tape's kinds and captions choose the
  same beds and hits the game played, for nothing.

## 4. One contract, any video model

- **`clip_providers.py`** holds the contract, the registry and the mock:

  ```python
  ClipRequest(first_frame, last_frame | None, prompt, seconds, resolution,
              aspect, refs: list[Path], session_id, tag)
  ClipResult(path, seconds, provider, model, resolution, latency_ms, cost_usd, error)
  class ClipProvider: name, model, capabilities, available() -> (ok, why),
                      estimate(seconds, resolution) -> usd, render(req) -> ClipResult
  ```

- **`clip_omni.py`** is the first provider: `gemini-omni-1.1-flash`,
  `POST v1beta/interactions` with the two frames as `image` inputs (first,
  then last), `response_format {resolution: "360p", aspect_ratio: "16:9",
  delivery: "uri"}`, on the player's Gemini key.
- **The `mock`** is ffmpeg `xfade` between the two frames. Suites, the harness
  and a keyless install all get a real (dissolve) film, labelled as one.
- **Later**, each is a file plus a registry line: `clip_veo.py` (Veo 3.1 Lite
  on the same key, whose SDK config has `last_frame`), `clip_fal.py`
  (first/last-frame models hosted on fal).
  - The planner, the cache, the stitcher and the Reel never learn which
    provider made a clip.
  - The providers go in a new `model_catalogue.video` in ai_config.json, not
    under `image`: test_render_mode pins that list and forbids video in it.

## 5. Money

- **Logged inside the adapter** (BILLING_LIVE_PLAN A4): each clip calls
  `cost_tracker.record_usage(sid, "film", "gemini", "gemini-omni-1.1-flash",
  operation="tape_film", output_units=<real seconds>, unit_type="seconds",
  meta={"size": "360p", "run": rid})`.
- **Pricing and receipts.** A new service type `film`, so receipts say "Tape
  films" instead of lumping it with Reactor's "Live video". pricing.json gets
  `gemini:gemini-omni-1.1-flash`: seconds, `sizes {"360P": 0.034, "720P":
  0.10, "1080P": 0.152, "4K": 0.304}`.
- **Same key, same wallet** as every still. That is why Omni goes first.
- **`/api/reel/` is in `_WALLET_FREE_PREFIXES`** (api.py ~747), because an
  export never spent. The film does, so its route takes its own
  `_spend_blocked(min_usd=<estimate>)` hold (precedent api.py:2141, and the
  render route at :3522). Nothing is spent until the player has seen the
  estimate and pressed FILM: the first POST returns the estimate, and the
  second carries `confirm_usd`. A job stops starting new clips once it reaches
  the confirmed figure.
- **The guard is a real Gemini key:** `provider_bridge.gemini_key()` and not
  `_mock_forced()`. It is **not** `can_call_gemini_api()`: that is True for an
  OpenAI player because the bridge answers `generateContent`, and nothing
  answers `interactions`. With no key, FILM says why instead of pretending
  (the test_render_mode rule: an option nobody can run is a trap).

## 6. Phases

**Phase 0: probe (~$3, Matt's go-ahead first).** `tools/film_probe.py`,
following the `tools/*_probe.py` pattern, on the Jean-Luc run. Two scenes:

- a walk and a drive: shots 43–47 ("You move to the staircase" → "You find
  the truck" → "You drive throi" → "You drive to top of mesa" → "You head
  for the Acid Catch Basin"), which includes relocations;
- a fight: shots 39–42 (encounter → two rounds → the turn after).

Each scene is filmed three ways:

- (a) independent pairs;
- (b) pairs with the previous clip's last ~2 s passed as a video reference,
  so momentum carries across the join;
- (c) Omni extension with a last frame, if the API accepts one.

Per clip, and as p50/p90, report:

- wall-clock time;
- whether the call blocks or needs polling;
- how the length is set (a field or the prompt), and the length that came
  back;
- audio;
- what 3 parallel calls do (429s?);
- the cost Google reports against the estimate;
- at every join, `continuity()` (rule 5: report the number and look at it)
  between the two renditions of the shared key frame.

Write a review page with each scene stitched three ways, beside its key
frames. **Watch it with Matt.** That choice (joins, relocations, sound)
decides Phase 1's defaults.

**Phase 1: a film from a tape, server side.**
- `clip_providers.py` + `clip_omni.py` + the mock.
- `tape_film.py`:
  - `plan(tape)`: key frames, boundaries, prompts, seconds, estimate;
  - the job: pool, cache, progress, spend cap, cancel;
  - `stitch`.
- `export_job` kind `film`, with progress: `{done, total, spent_usd, eta}`.
- Routes: `POST /api/reel/film/<rid>` (estimate / confirm),
  `GET /api/reel/film/<rid>` (progress), `/api/reel/film/<rid>/file`.
- The spend hold, pricing, service type, and `cut=` recorded on new shots.
- `test_tape_film.py` into `tools/ci_suites.txt`. The invariants:
  - one key frame per shot, and it is the last frame;
  - never a flipbook in-between;
  - montages and chapters cut;
  - the plan is deterministic, and the cache key is stable;
  - a second render of a longer tape films only the new pairs;
  - nothing is spent without `confirm_usd`, and never past it;
  - no Gemini key means no call, with a reason;
  - an OpenAI player never calls `interactions`;
  - the adapter logs its own seconds;
  - the mock film is the planned length and its joins land on the key frames.

**Phase 2: the Reel.**
- A FILM option in the export sheet, with TAPE / TIGHT pacing. Its spec line
  is the estimate: "41 clips · 3:51 · ≈ $6.46 on your Gemini key · ~7 min".
- Progress as clips land: a thumbnail per shot on the Reel's timeline.
- The finished film plays in the Reel (a `<video>` mode) and is delivered to
  `Videos/ABYSS Tapes` like the other exports.
- Checked in the browser pane against a sandboxed mock server; `check_js_syntax.py`
  after every edit.
- Then one real film of a real run, watched with Matt; the CHANGELOG quotes it.

**Phase 3: sound and words.** The library score (beds by kind and World,
consequence hits by caption), and the caption burn as a choice. Maybe the
narrator reads each shot's prose in the character's own voice; that is the
one part that would cost TTS.

**Phase 4: once it has earned it.**
- Retake one shot (drop its cached clip, re-render, re-stitch).
- 720p for a keeper.
- `clip_veo.py` as the second provider, to prove the contract.
- Watch renders that finish with a film.
- Perhaps the /get page's footage cut from films.

## 7. Risks

- **The joins.** Each clip eases out of and into its key frames, so the film
  may breathe at every beat. That may read as rhythm, or as a stutter. Two
  renditions of the same key frame may also disagree, which reads as a pop.
  Phase 0's variants (b) and (c) and the join dissolve are the levers.
- **Relocations morph.** Two different places interpolated can melt into each
  other. Recording `cut` from Phase 1 means the film can hard-cut those if it
  has to.
- **The model invents between the frames:** a figure, a cut, a spin. The
  contract prompt is the lever. The key frames are what was really played, so
  every beat lands on the truth even when the move between is wrong.
- **Rendering during play** shares the key's rate limit with the turns. While
  a run is live, the pool runs one clip at a time.
- **Pruning.** `KEEP_RUNS = 24` deletes a run's folder, cached clips
  included. The delivered film in `Videos/ABYSS Tapes` is what stays.

## 8. Found on the way (true today, not this plan's to fix)

- **`veo_video_utils.py` has been broken since 2025-12-19** (00910c5):
  `_extract_last_frame` uses an undefined `IMAGE_DIR`, so every Veo frame
  bought a clip and then fell back. The engine's Veo branch (engine.py ~8747)
  can't fall through to Gemini: on a None it returns an `/images/` URL for a
  file that was never written. `VEO_MODE_ENABLED` gates nothing, and every
  reference is first "converted" through a paid image call. Retire it once
  `clip_providers.py` exists.
- `/api/sessions/<sid>/videos/<f>` (api.py ~2854) reads a cwd-relative
  `sessions/…/films/` that is wrong in a packaged build, and nothing calls it.
  `consequence_video` (engine.py ~20998) reaches no client.
- The shot pack's `how_to_use` says each shot's first and last frame are the
  key frames a video model takes. Those are the flipbook's panel 1 and panel
  4 of one beat, not beat to beat. Once the film exists, the README should say
  which is which.

**Sources.**
- Omni: [video with Omni](https://ai.google.dev/gemini-api/docs/omni),
  [model card](https://ai.google.dev/gemini-api/docs/models/gemini-omni-flash),
  [pricing](https://ai.google.dev/gemini-api/docs/pricing),
  [GA announcement](https://blog.google/innovation-and-ai/technology/developers-tools/build-with-gemini-omni-1-1-flash/),
  [MarkTechPost](https://www.marktechpost.com/2026/08/29/google-ai-releases-gemini-omni-1-1-flash-40-second-scene-extension-first-last-frame-control-and-4k-upscaling/).
- The 360p figure (~$0.034/s, "a third of the cost" of 720p) is from the
  announcement and third-party coverage; Google's pricing page prices only
  720p. Phase 0 checks it against what Google bills.
