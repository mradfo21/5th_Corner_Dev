# 🔀 Mid-Game World-Model Switching — Integration Plan

**Goal:** let a player switch **live, mid-game** between the two Reactor world models
we already build around — **LingBot World 2** (the model the realtime renderer targets
today) and **Helios** (the model the original `REACTOR_INTEGRATION_PLAN.md` was written
for) — using those two as the **base pair**, with room to add more Reactor models later.

Today the realtime path is single-model: `static/js/reactor_renderer.js` bakes in
**one** model's wire protocol (LingBot World 2), and the model name is only
configurable at deploy time via the `REACTOR_MODEL` env var. This plan turns that one
hard-wired path into a **per-model driver** behind the existing `window.ReactorRenderer`
facade, plus a session-swap + a UI selector, so we can flip world models the same way we
already flip between the still-image and realtime renderers — without the rest of the
game caring.

> **Status — base pair implemented (testable):** the driver abstraction (§3), the live
> session swap (§4), the backend model registry (§5.1), and a simple **switcher UI
> attached to the world-model log** (§5.2) are now in the code for the LingBot World 2 +
> Helios base pair. Open the standalone view, press **L** to reveal the WORLD MODEL log,
> and click **Stills / LingBot World 2 / Helios** to flip live. Live video for each model
> still needs a real browser + a configured `REACTOR_API_KEY` (WebRTC + GPU) to fully
> validate; without a key the switcher falls back to stills.

---

## 1. What Reactor gives us (the base pair)

Reactor exposes several real-time models through one SDK (`@reactor-team/js-sdk`). The two
we already have infrastructure for:

| Model | Reactor name | Character | Steering protocol (the part that differs) |
|-------|--------------|-----------|--------------------------------------------|
| **LingBot World 2** | `lingbot-world-2` | Action-controlled world generation; image-conditioned. | **Reference image is LOCKED once a run starts.** `uploadFile → set_image → set_prompt → start`; a NEW guide image requires `reset` + a fresh stage. Prompt-only re-steers hot-swap on the next chunk. *(This is exactly what `reactor_renderer.js` implements today.)* |
| **Helios** | `helios` | Interactive real-time video generation, infinite 33-frame-chunk streaming; text-to-video **and** image-to-video. | `schedule_prompt {prompt, chunk:0}` (or `set_prompt`) → `start`. **`set_image {image_b64, transition:"cut"\|"blend"}` works DURING generation** — so a new guide image does NOT need a full `reset`; blend/cut in place. `schedule_prompt` enables look-ahead; `set_seed` for reproducibility. |

> The model-name strings the SDK expects are `helios`, `lingbot`, `lingbot-world-2`,
> `longlive-v2`, `sana-streaming`. Our repo currently passes `reactor/lingbot-world-2`
> via `REACTOR_MODEL`; **verify the exact accepted string per model** against a live
> token before shipping (this is a one-line config concern, isolated to the driver
> registry below).

**Why a driver abstraction is required (not just a config swap):** the two models have
**materially different wire protocols**. LingBot re-stages (`reset`) on every new guide
image because its reference image is locked; Helios blends a new image in mid-stream with
no reset. Swapping `modelName` alone would send LingBot's command choreography to Helios
(and vice-versa) and produce broken or sub-optimal output. Each model needs its own
"how to apply a scene" logic.

---

## 2. How the realtime renderer works today

**Backend seam:**

- `engine.py`
  - `SCENE_RENDERER` (env, default `"reactor"`) — a hint the web client reads.
  - `build_realtime_prompt()` / `build_realtime_base()` / `realtime_action_beat()` build a
    clean, video-model-appropriate prompt (style anchor + scene + one action beat),
    distinct from the still-image diffusion prompt. **Model-agnostic** — reused as-is.
- `api.py`
  - `GET /api/reactor/config` → `{enabled, renderer, model_name}` (model from `REACTOR_MODEL`).
  - `POST /api/reactor/token` → mints a short-lived JWT (key never leaves the server).
  - `GET /api/status` → also returns `renderer` + `current_image_prompt`.
  - `/realtime` route forces `renderer="reactor"` via `forced_renderer`.
- Feed items carry `metadata.prompt` + `hard_transition`, which is how each scene reaches
  the realtime renderer.

**Frontend seam:**

- `static/js/reactor_renderer.js` — `window.ReactorRenderer` facade:
  `enable/disable/applyScene/reset/pause/resume/captureFrame` + `onStatus/onEvent/onGuideImage`.
  It connects with `new Reactor({ modelName: cfg.model_name })` (**one** model per session)
  and hard-codes **LingBot's** command sequence (`establish()` uploads + `set_image` +
  `set_prompt` + `start`; `flush()` re-`reset`s and re-stages on every new guide image).
  A **freeze back-buffer canvas** (`#reactor-freeze`) covers the `<video>` during warmup /
  re-anchor so switches never flash black or the underlying still.
- `static/js/standalone.js` — the `Renderer` facade toggles `mode` between `"image"` and
  `"reactor"` (query param > localStorage > server default), owns the toggle button
  (`#btn-renderer`, `G` key), auto-falls back to stills on error, and forwards each scene
  via `ReactorRenderer.applyScene({prompt, imageUrl, hardTransition})`.
- `templates/standalone.html` — `<video id="reactor-video">`, `<canvas id="reactor-freeze">`,
  the renderer button, and loads `reactor_renderer.js` before `standalone.js`.

**Key insight:** the game already speaks in model-agnostic scenes (`{prompt, imageUrl,
hardTransition}`). Everything below only needs to change *how a scene is realized on a
specific model* and *how we hand off between models* — the turn loop stays untouched.

---

## 3. Target architecture — a per-model driver behind one facade

```
                 game turn → scene {prompt, imageUrl, hardTransition}
                                        │
                          window.ReactorRenderer (facade — unchanged API)
                                        │  owns: 1 live session, freeze buffer, status
                                        ▼
                         ┌──────── ModelDriver (active) ────────┐
                         │  establish(scene) / applyScene(scene) │
                         │  reAnchor(scene) / reset/pause/resume │
                         └───────────────────────────────────────┘
                            ▲                         ▲
              LingbotWorld2Driver            HeliosDriver
              (reset per guide image)        (set_image blend mid-run)
                            │                         │
                            └── shared: token, connect, track attach, freeze,
                                frame-watch, watchdog, event routing ──┘
```

- **`ReactorRenderer` (facade):** keeps its exact public API. Gains:
  - a **driver registry** keyed by world-model id;
  - `getModel()` / `setModel(id)` for live switching;
  - the connect/track-attach/freeze/frame-watch/watchdog/event plumbing it already has,
    factored into a shared **session core** the drivers call into.
- **`ModelDriver` interface** (per model), each implementing:
  - `modelName` (the SDK string) + `requiresSeedImage` (LingBot: true; Helios: false);
  - `establish(session, scene)` — first scene of a run;
  - `applyScene(session, scene, {isNewGuideImage, hardTransition})` — the per-turn logic
    that differs between models;
  - `reset/pause/resume` (thin; mostly shared).
- **`LingbotWorld2Driver`:** the current `establish()`/`flush()` behavior verbatim
  (reset + re-stage on each new guide image; deferred start until a seed exists).
- **`HeliosDriver`:** `schedule_prompt {chunk:0}` + `start`; on a new guide image
  `set_image {image_b64, transition:"blend"}` (or `"cut"` on `hard_transition`) **without**
  a reset; prompt-only re-steer via `set_prompt`. Optionally `schedule_prompt` for
  look-ahead tied to `current_chunk` from `state` messages, and `set_seed` on reset.

This is a refactor of one file plus small backend/UI additions — no change to the engine's
turn loop, feed schema, or `build_realtime_prompt`.

---

## 4. Live model switching (the swap)

Reactor is **one model per session**, so switching models = tear down the current session
and bring up a new one with the other `modelName`. We make it seamless by reusing the
freeze buffer that already exists:

1. **Capture** the current live video frame onto `#reactor-freeze` (`captureVideoToFreeze()`)
   and show it instantly — the player keeps seeing the last frame, no black gap.
2. **Disconnect** the current Reactor session; swap the active driver in the registry.
3. **Connect** a new session with the new model (`new Reactor({ modelName })`), mint a
   fresh JWT via `/api/reactor/token` (no key exposure).
4. On `ready`, **re-apply `lastScene`** through the new driver's `establish()` so the new
   model picks up exactly where we are (same prompt, same latest still as the seed).
5. When the new stream's **first real frame** arrives, the existing `armFreezeReveal()`
   crossfades the freeze out — the switch looks like a VCR glitch, matching current UX.

Only **one** GPU session is live at a time (tear down before bring up) to control cost and
avoid `ConflictError`. Persist the chosen model per browser in `localStorage`
(`world_model`), same pattern as `scene_renderer`.

---

## 5. Concrete changes

### 5.1 Backend

- **`api.py` — `GET /api/reactor/config`:** advertise the pair instead of a single name:
  ```json
  {
    "enabled": true,
    "renderer": "reactor",
    "model_name": "lingbot-world-2",
    "world_model": "lingbot-world-2",
    "available_models": [
      { "id": "lingbot-world-2", "label": "LingBot World 2", "requires_seed_image": true },
      { "id": "helios",          "label": "Helios",          "requires_seed_image": false }
    ]
  }
  ```
  Keep `model_name` for back-compat; add `world_model` (server default) + `available_models`.
- **`engine.py`:** add `REACTOR_WORLD_MODEL` (env, default `lingbot-world-2`) as the server
  default; `build_realtime_prompt` stays shared. If a model ever needs a slightly different
  prompt shape, add an optional per-model hook later — not needed for the base pair.
- **`render.yaml`:** document `REACTOR_WORLD_MODEL` alongside the existing `REACTOR_MODEL`
  (which can become an alias / the default id).
- **`/api/reactor/token`:** unchanged — the JWT is model-independent; the model is chosen
  client-side at `connect`.

### 5.2 Frontend

- **`static/js/reactor_renderer.js`** — the main work:
  - Extract shared session plumbing (`enable/disable`, token, track attach, freeze,
    frame-watch, reveal watchdog, event routing) into a **session core**.
  - Introduce a **driver registry** `{ "lingbot-world-2": LingbotWorld2Driver, "helios": HeliosDriver }`
    and an active-model pointer seeded from `/api/reactor/config` (or `?model=` /
    `localStorage['world_model']`).
  - Move the current `establish()`/`flush()` command choreography into `LingbotWorld2Driver`;
    add `HeliosDriver` per §3.
  - Add `getModel()` / `setModel(id)` to `window.ReactorRenderer` implementing the §4 swap.
- **`static/js/standalone.js`:**
  - Extend the `Renderer` facade selection to a **3-state cycle** on the existing button /
    `G` key (or a small popover): `STILL → LingBot World 2 → Helios → …`. Reuse
    `showRendererToast`, `updateRendererButton`, and the `world_model` localStorage key.
  - On model change while in `"reactor"` mode, call `ReactorRenderer.setModel(id)` and let
    the freeze-buffer swap handle continuity; re-apply `Renderer.lastScene`.
- **`templates/standalone.html`:** optionally add a `data-model` label to the button and a
  `?model=` accepted alongside `?renderer=` for one-click A/B. No new DOM layers needed —
  the `<video>` + `<canvas>` already cover the swap.

### 5.3 Later (not the base pair)

- Add `longlive-v2`, `sana-streaming`, or `lingbot` as extra registry entries once their
  drivers are written — the registry makes each a self-contained addition.
- Per-model tuned prompt hooks if a model benefits from a different style anchor.
- Remember per-model last-good scene so switching back is instant.

---

## 6. Prompt strategy across models

We keep **one** prompt pipeline (`build_realtime_prompt`): style/camera anchor + physical
scene + one action beat, already sanitized for content filters. The **driver** decides how
that single prompt string is delivered:

- **LingBot World 2:** `set_prompt {prompt}`; new guide image ⇒ `reset` + re-stage from the
  new seed (reference locked). *(current behavior)*
- **Helios:** `schedule_prompt {prompt, chunk:0}` on first start, `set_prompt {prompt}` to
  re-steer live; new guide image ⇒ `set_image {image_b64, transition:"blend"}` in place
  (`"cut"` when `hard_transition`), no reset. Optional `schedule_prompt` look-ahead.

Because the prompt text is identical, switching models mid-scene keeps continuity — the
same scene simply renders through a different world model.

---

## 7. Testing path

1. Set `REACTOR_API_KEY` (Cloud Agent / Render secret / local env). Do **not** commit it.
2. `GET /api/reactor/config` → confirm `available_models` lists both, `world_model` = default.
3. `python api.py` / `bash start_production.sh`. Open `/standalone` (or `/realtime`).
4. **Base flow:** default model streams as today (no regression on LingBot World 2).
5. **Switch:** cycle the renderer button `STILL → LingBot World 2 → Helios`. Verify:
   - freeze buffer holds the last frame during the swap (no black flash);
   - the new model re-applies the current scene and reveals on first real frame;
   - `?model=helios` and `localStorage['world_model']` both select at load.
6. `POST /api/reactor/token` → `{jwt, …}` unchanged.
7. Confirm image mode (`renderer=image`) is untouched and remains the safety fallback.

> Full live-video validation needs a real browser + GPU session per model (WebRTC), which
> can't run in CI. CI can cover: config shape, token proxy, and that both drivers register
> and expose the facade API.

---

## 8. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Divergent model protocols | Encapsulated per-driver; the facade + game loop are model-agnostic. |
| Swap flashes black / loses the scene | Reuse the freeze back-buffer + reveal-on-first-frame that already exist. |
| Two GPU sessions / cost | One live session at a time — disconnect before connect; single session per tab. |
| `ConflictError` / session churn on rapid toggling | Debounce `setModel`, ignore switches while a swap is in flight, reconnect with backoff. |
| Wrong model-name string | Centralized in the driver registry; verify each against a live token before ship. |
| API key leakage | Unchanged server-side token proxy; key never reaches the client. |
| Regression on the working path | LingBot driver = today's code moved verbatim; image mode stays the default fallback. |

---

## 9. Summary

The game already emits model-agnostic scenes and already flips between the still and
realtime renderers behind `window.ReactorRenderer`. To switch **world models** mid-game we
(1) split the one hard-wired LingBot protocol in `reactor_renderer.js` into per-model
**drivers** behind the same facade, (2) add a **session-swap** that reuses the existing
freeze buffer for a seamless hand-off, and (3) advertise + select the model via
`/api/reactor/config`, `?model=`, `localStorage`, and the existing toggle. LingBot World 2
and Helios are the **base pair**; the driver registry makes every additional Reactor model
a drop-in.
