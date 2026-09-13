# 🎥 Reactor Realtime World-Model Renderer — Integration Plan

**Goal:** add [Reactor](https://docs.reactor.inc/overview) real-time world-model video
(model `helios`) as a **swappable scene renderer** for SOMEWHERE, alongside the
existing Gemini still-image renderer. We steer the world model by **injecting the
same scene prompt we already build for image generation** (`set_prompt`), so every
turn drives the live video the way it currently drives a still.

The design lets us **flip between renderers with a query param / toggle** during
testing, with the still-image path remaining the default so nothing breaks.

---

## 1. What Reactor gives us (from the docs)

| Fact | Detail | Source |
|------|--------|--------|
| SDKs | JS (`@reactor-team/js-sdk`) + Python (`reactor-sdk`) | `/overview`, `/llms.txt` |
| Model | `helios` — 14B Diffusion Transformer, 33-frame chunks, text-to-video + image-to-video, infinite streaming | Helios reference |
| Transport | WebRTC video track `main_video`; frames arrive as `MediaStreamTrack` (JS) or NumPy `(H,W,3)` (Python) | React/imperative API |
| Latency | Sub-second round trip; steering is live while generating | `/overview` |
| Auth | Exchange API key for short-lived JWT: `POST https://api.reactor.inc/tokens` with header `Reactor-API-Key`. **Never ship the API key to the browser** — proxy through our server. | Authentication |
| Lifecycle | `disconnected → connecting → waiting → ready`; only send commands once `ready` | Connection lifecycle |
| Steering commands (Helios) | `set_prompt {prompt}`, `schedule_prompt {prompt, chunk}`, `set_image {image_b64, transition:"cut"\|"blend"}`, `clear_image`, `set_seed {seed}`, `start`, `pause`, `resume`, `reset` | Helios commands |
| Prompt rules | Must `set_prompt`/`schedule_prompt` at chunk 0 before `start`; can re-prompt live (applies to current chunk if paused, next chunk if running) | Helios commands |
| Messages | `type:"state"` (running, current_frame, current_chunk, current_prompt…) and `type:"event"` (`generation_started`, `prompt_switched`, …) | Helios messages |

**Verified in this repo:** the token exchange works with our key —
`POST https://api.reactor.inc/tokens` returns `{ "jwt": "…", "expires_at": <unix> }`.

---

## 2. How SOMEWHERE renders today

The game is prompt-driven. Each turn the engine builds a **scene image prompt** and
generates a **still** (Gemini Imagen), then streams it to the browser via a polled feed.

**Backend seam — `engine.py`:**

- `build_image_prompt(...)` composes the scene prompt from the player choice, the
  visual dispatch, narrative dispatch, prior vision analysis, spatial compass, world
  flavor/summary, transition flags, etc. (called inside `_gen_image`).
- `_gen_image(...)` returns `(image_path, prompt_used, video_path)`. **`prompt_used`
  is exactly the text we want to inject into Helios.**
- `_spawn_scene_image_async(...)` is the **single choke point** where a scene is
  produced off the turn's critical path. It:
  - calls `_gen_image`,
  - stores `state['current_image_url']` and `state['current_image_prompt']`,
  - appends a `scene_image` feed item (with `image_url`) to `feed_log`.
- `EXPERIENCE_MODES` + `apply_experience_mode()` already model *swappable rendering
  behavior* (`no_images`, `flipbook`, `full_frame`) by toggling `IMAGE_ENABLED` /
  `WORLD_IMAGE_ENABLED` / `flipbook_mode`. This is the natural place to add renderer
  selection.

**Frontend seam — `static/js/standalone.js` + `templates/standalone.html`:**

- Two crossfade layers (`#sceneA` / `#sceneB`) show the scene as a CSS
  `background-image`. `setScene(imageUrl)` swaps + crossfades.
- The client polls `GET /api/feed?since_id=N`; a `scene_image` item's `image_url`
  triggers `setScene`.
- `POST /api/reset` / `POST /api/choose` drive the loop; `GET /api/status` powers the
  HUD (turn, phase, chaos, inventory, backend, `image_enabled`).

**Serving:** `api.py` is the single Flask app (`gunicorn api:app`). `/images/<file>`
serves generated stills. `requests` is already a dependency.

---

## 3. Target architecture — a "Scene Renderer" abstraction

Introduce a renderer boundary on **both** sides that the rest of the game is agnostic to.

```
                       turn resolves → scene prompt built (build_image_prompt)
                                             │
                 ┌───────────────────────────┴───────────────────────────┐
                 ▼                                                         ▼
   RENDERER = "image"  (default)                          RENDERER = "reactor"
   Gemini still → /images/<f>.png                         same prompt → Helios set_prompt
   feed: scene_image{image_url, metadata.prompt}          live WebRTC main_video track
                 │                                                         │
                 ▼                                                         ▼
   Frontend: setScene() crossfade                         Frontend: <video> layer, steer live
```

Key idea: **the feed already carries everything the realtime renderer needs** once we
attach the prompt. We add `metadata.prompt` to `scene_image` items and expose the
current prompt + active renderer via `/api/status`. The browser decides how to render.

### 3.1 Renderer modes

| `SCENE_RENDERER` | Behavior |
|------------------|----------|
| `image` (default) | Current behavior. Gemini still per turn. |
| `reactor` | Live Helios video steered by the per-turn prompt. Still may also render (as fallback/continuity source) or be disabled to save cost. |
| `hybrid` (future) | Still is generated and used as `set_image` seed so Helios video starts from our exact composition, then diverges live. |

Selection sources (first wins): `?renderer=` query param → `localStorage` → server
`SCENE_RENDERER` env default. Query param + localStorage make A/B testing one click.

---

## 4. Prompt-injection strategy (steering Helios)

We reuse `prompt_used` from `_gen_image` / `state['current_image_prompt']` verbatim.

Per turn, the frontend Reactor renderer:

1. **First scene of a run:** on `ready`, `set_prompt {prompt}` then `start {}`.
2. **Subsequent turns:** `set_prompt {prompt}` (Helios applies it to the next chunk
   while running — a smooth live re-steer). Optionally `schedule_prompt {prompt, chunk}`
   for look-ahead pacing tied to `current_chunk` from `state` messages.
3. **Hard transitions (location change):** the engine already flags `hard_transition`.
   Surface it in feed metadata; on a hard cut we can `set_image` (blend/cut) with the
   fresh still, or `set_seed` + re-prompt, to force a decisive scene break instead of a
   morph.
4. **Continuity seed (hybrid):** base64-encode the latest Gemini still and
   `set_image {image_b64, transition:"blend"}` so the world model inherits our framing,
   palette, and first-person bodycam look, then evolves it in motion.
5. **Death / reset:** map game reset → `reset {}`; pause the stream on the death overlay.

**Safety:** the engine already sanitizes prompts for image content filters
(`_sanitize_for_image_generation`). We inject the **sanitized** prompt into Helios too,
so we don't regress on filter behavior.

---

## 5. Concrete changes

### 5.1 Backend (this PR — scaffolding)

- **`api.py`**
  - `POST /api/reactor/token` — server-side proxy to `https://api.reactor.inc/tokens`
    using `REACTOR_API_KEY`. Returns `{jwt, expires_at}`; `503` if unconfigured.
  - `GET /api/reactor/config` — `{enabled, renderer, model_name}` for the client.
  - `GET /api/status` — also returns `renderer` and `current_image_prompt`.
- **`engine.py`**
  - `SCENE_RENDERER` global (env `SCENE_RENDERER`, default `image`).
  - `_spawn_scene_image_async` attaches `metadata={"prompt", "hard_transition"}` to the
    `scene_image` feed item so the realtime renderer gets the steering text in real time.
- **`render.yaml`** — declare `REACTOR_API_KEY` (secret) + `REACTOR_MODEL` /
  `SCENE_RENDERER` env vars.

### 5.2 Frontend (this PR — scaffolding)

- **`templates/standalone.html`** — add a `<video id="reactor-video">` scene layer, a
  renderer toggle mini-button, and load `reactor_renderer.js` before `standalone.js`.
- **`static/js/reactor_renderer.js`** — imperative Reactor SDK driver loaded from an ESM
  CDN (no build step): fetch token → `connect` → attach `main_video` to the `<video>` →
  `set_prompt`/`start` on ready → re-`set_prompt` on each new scene prompt. Exposes
  `window.ReactorRenderer`.
- **`static/js/standalone.js`** — a small `Renderer` facade: in `image` mode it calls the
  existing `setScene`; in `reactor` mode it forwards the scene prompt to
  `ReactorRenderer` (and keeps the still behind the video as a graceful fallback). Toggle
  via button / `G` key / `?renderer=` param, persisted in `localStorage`.

### 5.3 Later phases (not in this PR)

- **Cost controls:** in pure `reactor` mode, optionally skip Gemini still generation
  (guard in `_spawn_scene_image_async`), keeping only the prompt for steering.
- **Tape capture:** Helios has no server-side stills. To keep the VHS-tape feature,
  either (a) grab periodic `<canvas>` snapshots of the `<video>` in the browser, or (b)
  run the **Python SDK** `@reactor.on_frame` alongside the session to save frames →
  reuse `create_flipbook_gif.py` / existing tape pipeline.
- **Discord path:** the Discord bot can't embed WebRTC. Use the Reactor **Python SDK**
  headless to capture frames → post short GIF/MP4 clips per turn (reuse `veo_video_utils`
  plumbing), or keep Discord on the image renderer and web on Reactor.
- **Pacing:** align turn cadence to Helios chunk timing using `state.current_chunk` and
  `schedule_prompt` for choice-driven look-ahead.
- **Session mgmt:** one Helios session per browser tab; handle `ConflictError`,
  `recoverable` errors (reconnect w/ backoff per docs), and session expiry.

---

## 6. Testing path (switch renderers easily)

1. Set `REACTOR_API_KEY` in the environment (Cloud Agent secret / Render secret / local
   env). Do **not** commit it.
2. Run the app (`python api.py` or `bash start_production.sh`). Default renderer =
   `image`, so current behavior is unchanged.
3. Open `/standalone?renderer=reactor` (or press the renderer toggle). The browser mints
   a token via `/api/reactor/token`, connects to Helios, and each turn's scene prompt
   steers the live video. Toggle back to `image` anytime.
4. Verify token proxy: `curl -X POST localhost:5001/api/reactor/token` → `{jwt, …}`.

**Verified now:** token exchange returns a valid JWT; Flask app imports and the two new
endpoints register. Full live-video validation requires a browser (WebRTC) against a live
Helios GPU session, which can't run in CI.

---

## 7. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| API key leakage | Server-side token proxy only; key never sent to client. |
| WebRTC needs a real browser + GPU session | Renderer is opt-in and defaults off; image path untouched. |
| Cost of always-on GPU video | Pause on idle/death; option to disable stills in reactor mode; single session per tab. |
| Content filters on injected prompt | Inject the already-sanitized prompt. |
| Losing VHS tape / Discord support | Phase 5.3 (browser canvas capture or Python-SDK frame capture). |
| SDK breaking changes (beta) | Pin the SDK version; isolate all SDK use in `reactor_renderer.js`. |

---

## 8. Summary

The cleanest integration reuses the existing prompt pipeline: the text we already build
for stills becomes the live steering signal for Helios. A thin renderer abstraction on the
backend feed (`metadata.prompt` + `SCENE_RENDERER`) and the frontend (`Renderer` facade +
`ReactorRenderer`) lets us flip between the still-image renderer and the realtime
world-model renderer with a single toggle — safe to test incrementally, with the current
game as the always-working default.
