# 🎥 Video between frames — the still is drawn, then the move to it is filmed

> **Status: proposed 2026-09-25. Nothing is built.** Phase 0 is a paid probe
> (~$2 on the Gemini key) and it gates everything after it: the one number
> this plan cannot know from the docs is how long Omni takes to make a clip.
> Supersedes [`VEO_VIDEO_BASED_IMAGE_GEN.md`](VEO_VIDEO_BASED_IMAGE_GEN.md),
> which is dormant and broken (below).

Matt: *"instead of the flipbook, we use videos to go between frames … using
just google omni 360p fast, for now … building this in a way where we can use
other tools in the future … i want to still generate stills, when in video
mode — not flipbooks now (but keep this as an option. its most stable and
cheap and fast) … Frame → gameplay → Generate Frame (hidden) → use video to
go from Previous Frame to Generate Frame, and continuing on, seamlessly."*

---

## 1. The loop

```
frame N on screen ── player acts ── prose lands (frame N still up)
                                         │
                      still N+1 drawn exactly as today, NOT painted
                                         │
             ┌───────────────────────────┴──────────────────────────┐
             │ clip job: first frame = frame N (what is on screen)  │   choices for N+1
             │           last frame  = still N+1                    │   are written off
             │           Omni, 360p, 3–4 s                          │   still N+1 in
             └───────────────────────────┬──────────────────────────┘   parallel
                                         │
             clip plays over frame N ── lands on still N+1 ── choices open
```

Motion becomes a third answer to "how does a turn move", beside the two that
exist:

| Motion | What the turn draws | Cost | Status |
|---|---|---|---|
| **Still** | one image, 1.5 s crossfade + glitch | 1 image | shipped |
| **Flipbook** | one grid → N in-between panels, last panel = the still | 1 image | shipped, stays |
| **Video** | one still + one clip from the previous still to it | 1 image + clip seconds | this plan |

## 2. The rule everything hangs on: the still is the truth, the clip is a view of it

Everything downstream of a turn understands one image: SCAN's detector, the
goal sighting (`/api/goal/sight`), the narrator's `_narrator_sees`, the vision
pass the choices are written off, the next turn's img2img reference, PHOTO,
the tape. In video mode **all of that keeps reading the still**, and the clip
is presentation only. Three things follow, and they are the reason for the
design:

- **A clip can fail, or arrive late, and the turn is still a stills turn.** A
  bad clip costs the motion, never the turn. That is the flipbook's contract
  too ("a bad flipbook costs quality, not the turn").
- **Swapping the video model changes nothing downstream.** Omni, Veo 3.1,
  a fal-hosted Kling or whatever comes next all take the same two pictures and
  a prompt and hand back an mp4. Nothing else in the game knows which one it
  was.
- **The continuity chain is unchanged.** Still N+1 is still img2img off still
  N. The clip never becomes a reference for anything.

The 2025 Veo build did it the other way round: video first from frame N and a
prompt, then pull the last frame out of the mp4 and call that the still. The
picture every system reads was then a decoded, compressed video frame that no
prompt had asked for, and there was no way to aim the shot at anything. It is
also broken today (§9). Don't revive it; this plan replaces it.

## 3. What Omni gives, and what it doesn't say

From Google's docs ([video with Omni][omni], [model card][card],
[pricing][pricing]), the GA announcement ([blog][blog]) and
[MarkTechPost][mtp]:

| | |
|---|---|
| Model | `gemini-omni-1.1-flash` (GA 2026-08-27); `gemini-omni-flash-preview` |
| Wire | `POST v1beta/interactions` (**not** `generateContent`, not `predictLongRunning`); SDK `client.interactions.create` (google-genai 2.19.0 in the lock has it) |
| First/last frame | two `{"type":"image"}` entries in `input`, first then last, plus a text entry describing the move. MarkTechPost also mentions `<FIRST_FRAME>` / `<LAST_FRAME>` prompt tags; Google's page doesn't show them |
| `response_format` | `aspect_ratio` `16:9` \| `9:16`; `resolution` `360p` \| `720p` \| `1080p` \| `4k`; `delivery` `base64` \| `uri` (uri for > 4 MB, then poll `files/{id}` until ACTIVE) |
| Length | 3–10 s, 24 fps, mp4 |
| Audio | "generates a video with audio"; no documented off switch |
| Negative prompt | not supported; negatives go in the prompt text |
| Price | $17.50 / M output video tokens; 5,792 tokens/s at 720p ≈ **$0.10/s**. 360p is "a third of the cost" and "up to 60% faster" ≈ **$0.034/s** |

**Not in the docs, so Phase 0 finds out:** how long a 360p clip takes; how
the length is chosen (no duration field is documented); whether the call
blocks until done or needs `background`/polling; whether the clip's last
frame actually lands on our still; and what it does between two frames in
different places (a relocating turn).

Stills today are `gemini-3.1-flash-lite-image` at 1K 16:9 (1376×768),
$0.0336 each. So the aspect already matches, and a 360p clip (640×360) is
about 2× under the still. That softness gap is small, and the VHS overlay is
built to hide it.

**Cost per turn.** Still $0.034, plus clip ≈ $0.10 (3 s) or $0.14 (4 s). So a
video turn costs about **4–5× a stills turn** (~$0.14–0.17), and a 30-turn run
spends ≈ $3–4 on clips. At 720p it would be ≈ $0.30–0.40 per clip.

## 4. Time is the risk

Today (`Ceremony` notes, standalone.js ~4008; `turn_timing` in the play log):
consequence ~2 s → image median 8.8 s, p90 21.9 s → vision + choices ~3 s. The
picture lands ~11 s after the click, the slate a median 3.4 s later.

Video mode adds the clip's generation, **serially after the still**, because
the clip needs the still as its last frame. Nothing can start it earlier. What
the plan does about that:

- **The clip starts the moment the still exists**, in its own thread, parallel
  with Phase 2 (choices) and the world-evolution join. The slate is never
  waiting on the clip; only the *picture* is.
- **Frame N stays up while the clip is made**, with prose on screen and the
  consequence bed (6–12 s, already fired when the consequence lands) under it.
  The Ceremony corner shows "developing".
- **A deadline** (`motion_video_wait_s`, default 45 s from the still). A clip
  that isn't ready by then is dropped: the still is painted exactly as a
  stills turn, the choices open, and the late clip is kept for the tape only.
- **Rough budget if Omni 360p takes 15–25 s:** the picture starts moving
  ~26–36 s after the click, lands 3–4 s later, and the choices are already
  waiting. That is roughly the flipbook's length today. If Phase 0 measures
  60 s+, per-turn video isn't viable as the play mode, and it becomes a Watch
  render / tape feature instead (§8, Phase 4).

## 5. The architecture

### 5a. One contract, many providers: `motion.py`

A flat module (CLAUDE.md: the build is flat, and every module finds its data
with `Path(__file__).parent`). The adapter is a small interface, not the
`generate_with_<p>` naming convention the image utils share. The image utils'
if/elif chain in `_gen_image_impl` is exactly how the Veo branch came to fall
through to a phantom `/images/` URL.

```python
@dataclass
class ClipRequest:
    first_frame: Path          # what is ON SCREEN at the click (see 5c)
    last_frame: Path           # the turn's still
    prompt: str                # built by motion.clip_prompt (5d)
    seconds: float             # asked for; the adapter reports what it made
    resolution: str            # "360p"
    aspect: str                # "16:9"
    cut: bool                  # the consequence model said `relocated`
    session_id: str
    turn: int

@dataclass
class ClipResult:
    path: Optional[Path]       # mp4 under sessions/<sid>/motion/
    seconds: float
    provider: str; model: str; resolution: str
    latency_ms: int
    cost_usd: Optional[float]
    last_frame_mad: Optional[float]   # clip's last frame vs the still (report, don't gate)
    error: Optional[str]

class MotionProvider(Protocol):
    name: str                  # "omni", "mock", later "veo31_lite", "fal_kling"…
    model: str
    capabilities: dict         # {"first_last": True, "seconds": (3, 10),
                               #  "resolutions": [...], "aspects": [...], "audio": True}
    def available(self) -> tuple[bool, str]: ...   # (ok, why-not for the editor)
    def estimate(self, seconds: float, resolution: str) -> float: ...
    def render(self, req: ClipRequest) -> ClipResult: ...
```

- `PROVIDERS = {"omni": …, "mock": …}` is the registry. A second provider is a
  new file (`motion_veo.py`, `motion_fal.py`) plus one registry line.
- **`motion_omni.py`** is the first adapter. It uses raw `requests` against
  `v1beta/interactions` with `x-goog-api-key`, matching how the rest of the
  engine talks to Gemini. It sends the two stills as base64 PNG, `resolution`,
  `aspect_ratio: "16:9"` and `delivery: "uri"` when the clip could pass 4 MB.
- **`mock`** makes a real H.264 mp4 of the two stills with ffmpeg's `xfade`,
  at the requested length and 640×360, in well under a second. The suites, the
  harness and the client path can then all run offline. It is also the
  baseline the real clips have to beat: if Omni doesn't look better than a
  crossfade, that is the finding.

### 5b. Money: logged inside the adapter, like everything else

- `cost_tracker.record_usage(sid, "motion", "gemini", "gemini-omni-1.1-flash",
  operation="motion_clip", output_units=<real seconds>, unit_type="seconds",
  meta={"size": "360p", …})`. BILLING_LIVE_PLAN A4's rule is that the provider
  utility logs, not the caller.
- A new service type `motion`, added to `SERVICE_TYPES`, so receipts read
  "Motion" separately from Reactor's "Live video" (account.js labels `video`).
- pricing.json gets `gemini:gemini-omni-1.1-flash`, `unit_type: seconds`,
  `sizes: {"360P": 0.034, "720P": 0.10, "1080P": 0.152, "4K": 0.304}`.
  `estimate_cost` already upper-cases a `size` from meta. If Omni's response
  carries a usage count, log tokens as well, and let Phase 0 say which one
  matches the bill.
- One billing account: Omni is on the player's Gemini key, the same wallet as
  every still. That was the point of starting here.
- **Gate:** before a clip is submitted, `_spend_blocked(min_usd=<estimate>)`
  (precedent api.py:2141). If it's blocked, the turn is a stills turn. Coin-op
  prices a turn off one image rate (`coinop._turn_cost_cents`) and would
  underprice video 4–5×. That doesn't matter while Matt is testing on his own
  key, but it has to be solved before any player sees the option
  (BILLING_LIVE_PLAN B4: the cost on the button).

### 5c. Engine: where it hooks

- **The from-frame is captured at the click**, in `api_choose`, from
  `st["current_image_url"]` resolved to a path. It is **not**
  `flipbook_last_frame`: encounter grids call `_flipbook_generate` with
  `write_state` on (encounter.py:4640), so that field can point at a fight
  plate. The clip has to start from what the player is looking at.
- **Still first.** `flipbook_active()` answers False when the session's
  motion is `video`, so `_gen_image_impl` takes its still path untouched.
- **Submit.** In `_generate_and_append_scene_image`, right after `img_path` is
  known (engine.py ~10712) and before the `scene_image` item is written:
  `job = motion.submit(ClipRequest(...))` on a small worker pool (1 per
  session; a new turn supersedes a pending one). It **never runs under
  `TURN_LOCK`**: the next turn doesn't depend on the clip.
- **The wire, copied from the flipbook's side channel:**
  - the `scene_image` item carries `metadata.motion = {id, status: "pending",
    from, still, seconds, deadline_ms}`;
  - when the job finishes, the worker appends a `scene_motion` feed item
    `{id, url, seconds, status}` (`ready` / `failed`) through the same
    `_feed_append` path the async death still uses;
  - `/api/status.current_motion` for a reconnect.
- **Serving:** `/motion/<file>?session=` → `send_file(mimetype="video/mp4",
  conditional=True)`; Werkzeug handles Range/206. Files live in
  `sessions/<sid>/motion/`. `/images/` hardcodes image/png (api.py:1519), and
  `/api/sessions/<sid>/videos/` is broken (§9). Add `/motion/` to
  `local_guard.GUARDED_PREFIXES`.
- **`motion_active(st, identity_spec, source)`** is the one place that decides
  whether a turn gets a clip:

| Turn | Clip? | Why |
|---|---|---|
| choice, `typed` (same place) | yes | the ordinary turn |
| MOVE TO (`scan_move` always sets `hard_transition`) and any turn the model says `relocated` | per `motion_on_cut`: `travel` (default) or `skip` | a walk to the thing is the move video is best at, but two unrelated places can morph; Phase 0 decides the default |
| `scan_interact`, `talk` | no | a Moment covers the scene while the turn runs underneath, so nobody would see it |
| viewfinder / PHOTO, encounter, camp, cutscene, death, world transition, drift, blocked image | no | these surfaces own the screen (the same exclusions the flipbook has) |
| Reactor showing | no | the still is only a floor under live video there |
| no Gemini key (OpenAI player), mock-forced, wallet short | no → stills | the guard is `provider_bridge.gemini_key()` present and not `_mock_forced()`. **Not** `can_call_gemini_api()`: that answers True for an OpenAI player because the bridge answers `generateContent`, and nothing answers `interactions` |

### 5d. The clip's prompt

It is built from what the turn already knows, in code first. `flipbook.grid_prompt`
is the model: a generated contract, not free prose.

- **The contract:** one continuous take from the first image to the last; the
  first image is where the camera is now, the last is where it lands; no cuts,
  no new people, no text; the rig and the light hold. On a cut turn it is a
  *travel* shot that arrives by the end.
- **The move:** the choice text and the turn's `visual_scene` caption.
- **The camera:** the `game_identity` camera block (the follow-cam, the
  character from behind). It goes **through `game_identity`**: the Veo builder
  hardcoded "1993 / VHS / X-Files" and would say that over OUTGROWTH.
- **Negatives in the prose**, because Omni has no negative field.

Once there is something worth authoring, it gets promoted to a prompt key
(`motion_video_direction`). That needs the factory, every World
(`tools/edit_prompt_everywhere.py`), the editor, and a live reader so
`unwired_keys()` stays empty. It's not worth doing before Phase 3.

### 5e. Client: `MotionPlayer`

A `<video id="motion-video" muted playsinline preload="auto">` in
`templates/standalone.html` at **z-index 1**: above `.scene` (0), below the
scrim, flash and glitch. That is where `#reactor-video` sits when showing.
Probably `static/js/motion.js` beside `moments.js`, since standalone.js is
27k lines.

1. `applyScene` sees `metadata.motion.status == "pending"`. It decodes the
   still but **does not paint it**, keeps frame N up, marks the Ceremony
   "developing" and starts the deadline timer. `currentStillUrl` stays frame N,
   so SCAN/PHOTO can't act on a picture the player hasn't seen.
2. A `scene_motion` item with the matching id arrives. Set `src`, wait for
   `canplaythrough`, show the video over frame N (its first frame *is* frame
   N, so this should be invisible), fade it in over ~150 ms to hide the 360p
   softness step, then play.
3. `ended` (or `duration − 0.3 s`): paint the still underneath with
   `setScene(…, {silent: true})` (no 1.5 s crossfade, no glitch), then fade the
   video out. `currentStillUrl` = the still; call `Fist.pictureLanded()` and
   `GoalTag.onScene()`. The choices open as they do after a flipbook.
4. On the deadline, `failed`, a decode error or reduced motion: `setScene(still)`
   as a stills turn. A late `scene_motion` for an id that is no longer current
   is ignored.
5. **Muted.** The game's own sound (foley on the click, the consequence bed)
   is already under the move, and Omni's generated audio would fight it. A
   `motion_video_sound` knob can try it later.
6. **Never restack `.scene` over a playing `<video>`** (standalone.js ~10030).
7. `python check_js_syntax.py` after every edit. CDP screenshots **do not show
   the video layer**, so verification reads the element's state the way
   `check_scene_layers.py` does, plus frames pulled out of the mp4.

### 5f. Settings: the editor's Motion group, one control

The in-game World Editor's Image → **Motion** group (editor_graph.js
`sheetImage`, ~5390) is also what `/studio` shows now: it redirects to
`/?mode=create`. So one change covers both editors.

- **`motion_mode`**: enum `still | flipbook | video`, labelled "Motion".
  `flipbook_enabled` keeps working underneath: `tunables.json` has it `true`
  today, and `/api/flipbook` and the suites use it. Setting `flipbook` sets it,
  and `still` / `video` clear it.
- **`motion_video_provider`**: `omni` (+ `mock`), drawn from the registry, with
  each provider's `available()` reason shown when it can't run. A picker option
  nobody can run "isn't a choice, it's a trap" (test_render_mode.py).
- **`motion_video_resolution`**: `360p` / `720p`. **`motion_video_seconds`**:
  3–6, default 3. **`motion_video_wait_s`**: 20–90, default 45.
  **`motion_on_cut`**: `travel` / `skip`.
- Per session: `st["motion_mode"]` wins over the global, like `flipbook_mode`;
  `POST /api/motion {mode, session}` lets a harness or a Watch render run
  video without flipping it for the live game.
- **Don't call any of this "transition".** In this repo that word means an
  Experience graph edge (`TRANSITION_TYPES`, `test_transition_types.py`).
- **Don't put video in `model_catalogue.image`.** test_render_mode pins that
  list and asserts Veo is never offered there. Video is its own kind.

### 5g. The tape keeps the film

`run_tape.record` gains a `clip` on the shot, hard-linked into the run folder
the way frames are. `_keep_frame` silently drops anything that isn't an image
(run_tape.py:74), so this needs its own branch. `shot_list` already exports
`first_frame` / `last_frame` / `video_prompt` per shot, written for exactly
this. The animatic can then cut real clips in place of held stills. That is
the "free film" the Veo doc promised, done from the still-truth side.

## 6. Phases

**Phase 0: probe (half a day, ~$2 on the Gemini key; needs Matt's go-ahead
to spend).** `tools/motion_probe.py`, the `tools/*_probe.py` pattern. Take
~10 consecutive turn pairs from real tapes (`shot_list`'s first/last frames),
including 2–3 relocating turns, and send each to Omni at 360p with a
contract-shaped prompt. Report, per clip and as p50/p90:
- wall-clock latency, and whether the call blocks or needs polling;
- how the length is controlled (a field, or the prompt), and the actual seconds;
- file size, fps, codec, audio present;
- `continuity()` (the mean absolute difference, rule 5: report, don't
  threshold) between the clip's first frame and still N, and its last frame
  and still N+1;
- the cost Google reports against our estimate.

Write a review page: before/after stills with the clip between them. **Look at
it with Matt.** The go/no-go is latency plus whether the move reads as the
action.

**Phase 1: server (no client change).** `motion.py` + `motion_omni.py` + the
mock; the tunables; `motion_active`; the submit hook, the `scene_motion` feed
item, the `/motion/` route and the guard prefix; cost logging and the pricing
row. Suite `test_motion.py` into `tools/ci_suites.txt`, holding these
invariants:
- a failed or late clip leaves a well-formed stills turn;
- the still is what every downstream reader gets, clip or no clip;
- `motion_active` answers no for every surface in 5c's table;
- no Gemini key means no submit, and an OpenAI player never calls
  `interactions`;
- the adapter logs its own seconds;
- the mock's mp4 has the requested length and both end frames.

**Phase 2: client.** `MotionPlayer`, the video element, the Fist and GoalTag
hand-off, the deadline, reduced motion. Checked against a sandboxed mock
server in the browser pane: the mock clip plays and lands on the still, and a
forced failure lands on the still too.

**Phase 3: play it.** A `playtest_app.py` branch, not a fork (rule 4):
`PT_MOTION=video`. Per turn it records played / fell back / late, generation
ms, and the two end-frame numbers, and appends a finding for a picture that
never landed. Then a real run on Omni in the native app, and frames and clips
shown to Matt. The CHANGELOG entry quotes what came back.

**Phase 4: once it's earned.**
- The `motion_video_direction` prompt key.
- A second adapter to prove the contract. The cheapest candidate is **Veo 3.1
  Lite** on the same key ($0.05/s at 720p; the SDK's `GenerateVideosConfig`
  has `last_frame` and `duration_seconds`). Then a fal-hosted
  first/last-frame model for the non-Google path.
- Clips in the tape and the animatic.
- Cost on the button before any player sees the option.

**Later experiments this contract leaves room for:**
- Omni's *extension* (`previous_interaction_id`, up to 40 s cumulative): each
  turn continues the last clip rather than interpolating, so it plays as one
  film.
- Showing the clip's own audio.
- 720p for Watch renders only, where time doesn't matter.

## 7. Risks

- **Latency.** Covered in §4; it is the go/no-go.
- **The last frame doesn't land on the still.** The end fade has to hide the
  difference, and a big miss reads as the picture popping at the end ("it
  popped back to the previous frame" is a bug report this repo has had
  before). Phase 0 measures it.
- **Cut turns morph.** Two places interpolated can melt into each other.
  `motion_on_cut: skip` falls back to the stills cut and glitch.
- **The model invents things between the frames:** a figure, a cut, a
  camera spin. The contract prompt is the lever; the still stays correct
  regardless, because it is the truth.
- **Spend.** 4–5× a stills turn; a clip abandoned at the deadline is still
  billed.

## 8. Where it can end up if it doesn't pay off as play

If per-turn clips are too slow for play, the same contract still makes the
Watch render and the tape export a film. A render has no one waiting, and
`shot_list` already has every pair.

## 9. Found on the way (not this plan's to fix, but true)

- **`veo_video_utils.py` has been broken since 2025-12-19** (00910c5):
  `_extract_last_frame` uses an undefined `IMAGE_DIR`. Every Veo frame bought
  a clip, hit a NameError and fell back. The engine's Veo branch
  (engine.py:8747) can't actually fall through to Gemini: on a None it
  returns an `/images/` URL for a file that was never written.
  `VEO_MODE_ENABLED` (engine.py:1100) gates nothing. It "converts" every
  reference through a paid image call first. Retire it once `motion.py` exists.
- `/api/sessions/<sid>/videos/<f>` (api.py:2854) reads a cwd-relative
  `sessions/…/films/` that is wrong in a packaged build and misses the
  `segments`/`final` folders; nothing calls it. `consequence_video` (engine.py
  ~20998) reaches no client.
- `EXPERIENCE_MODES` / `apply_experience_mode` (engine.py:1282) have no
  production caller. The live switch is the tunables, which is why this plan
  builds on them.

[omni]: https://ai.google.dev/gemini-api/docs/omni
[card]: https://ai.google.dev/gemini-api/docs/models/gemini-omni-flash
[pricing]: https://ai.google.dev/gemini-api/docs/pricing
[blog]: https://blog.google/innovation-and-ai/technology/developers-tools/build-with-gemini-omni-1-1-flash/
[mtp]: https://www.marktechpost.com/2026/08/29/google-ai-releases-gemini-omni-1-1-flash-40-second-scene-extension-first-last-frame-control-and-4k-upscaling/
