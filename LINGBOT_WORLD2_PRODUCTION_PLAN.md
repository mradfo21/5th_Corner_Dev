# SOMEWHERE × LingBot World 2 — Production Plan

**Goal:** Ship one focused, playable horror experience to a gameplay tester — video-streamed world, choice-driven narrative, no per-turn image/vision bottleneck.

**Non-goal:** A "play anything" sandbox. One curated run: Jason Fleece, Four Corners / Horizon / The Gate, 1993 VHS found-footage.

---

## 1. Where we are

### What works today
- Core loop is playable and fun: see → dispatch → choose → consequence → world evolves → repeat until death.
- Discord bot + standalone web (`/standalone`) both drive the same story primitives.
- Prompt system in `prompts/simulation_prompts.json` already encodes tone, fairness, choice energy, and visual contracts.
- Session isolation, archives, VHS tape GIFs, fate rolls, inventory, timeout penalties exist.
- Experience modes (No Images / Flipbook / Full Frame) prove the presentation layer is already pluggable.

### What's holding us back
| Problem | Why it hurts |
|---------|----------------|
| **Per-turn Gemini img2img + vision + choices** | 10–30s+ dead air; biggest cost and fragility |
| **Presentation** | Stills / late GIFs feel like a slideshow, not a world |
| **Dual turn pipelines** | Discord (`advance_turn_*`) vs standalone (`_process_turn_background`) diverge |
| **Fragile story simulator** | Giant `world_prompt` string rewritten every turn; no structured beats |
| **Building blocks buried** | Prompts, phases, items, experience modes not designer-facing |
| **Hard to scale** | Global state paths, Render ephemeral disk, no spend caps on Gemini |

### Product truth
We're close because the **game design** works. Presentation and generation architecture are the bottleneck — not "is this fun."

---

## 2. Why LingBot World 2 fits

[LingBot World 2 on Reactor](https://www.reactor.inc/lingbot-world-2) is an **image-anchored realtime world model**:

- Seed with one reference image + text prompt → continuous `main_video` stream (~1664×960, ~48fps).
- Hot-swap `set_prompt` mid-stream (next chunk) to inject events / atmosphere / consequences.
- Drive camera with `set_move_longitudinal` / `set_move_lateral` / look axes, or `set_camera_pose`.
- Native `start` / `pause` / `resume` / `reset` — exactly the "unfold for N seconds, then freeze for choices" loop.
- Python SDK (`reactor-sdk`) and JS SDK (`@reactor-team/js-sdk` / `@reactor-models/lingbot-world-2`) both exist.

**Mapping to our loop:**

```
TODAY                              WITH LINGBOT WORLD 2
─────                              ────────────────────
Generate still/GIF                 Stream live video (already running)
Vision analyze frame               Capture pause-frame (optional light vision)
LLM: dispatch + visual_scene       LLM: dispatch + world_prompt fragment + camera intent
LLM: 3 choices                     Same (keep — this is the game)
Player picks choice                Inject set_prompt + movement; resume ~8–12s
Pause for next slate               pause → show choices over frozen/last frame
```

**What we stop doing (or demote):**
- Per-turn Gemini image generation as the primary visual path.
- Vision-as-hard-dependency every turn (use sparingly at pause points, or skip when prompt+seed stay aligned).
- Flipbook grid / Veo-as-frame-extractor as the default experience.

**What we keep:**
- Consequence LLM (`dispatch`, `player_alive`, fairness doctrine).
- Choice generation (visceral, grounded).
- Fate, inventory, timeout, death, tape archive (tape becomes video segments, not still GIFs).
- Focused setting bible (trimmed into a **seed world** + **beat sheet**, not an infinite rewrite).

---

## 3. Target experience (one cool game)

### Pitch for testers
> You're Jason Fleece, 1993. Handheld camcorder. Horizon's desert facility. The Gate is underground. You get ~10 seconds of live footage after each choice — then the tape freezes and you decide what to do next. Hesitate and the world tightens. Die and the tape ends.

### Scope lock (critical)
Ship **one authored scenario**, ~8–15 decision beats, not open-world infinity:

1. **Arrival** — golden hour, perimeter fence, abandoned vehicles  
2. **Breach** — get past fence / patrol  
3. **Interior** — corridor / lab evidence  
4. **Contact** — guard or red-biome tell  
5. **The Gate approach** — climax beat  
6. **Death or escape** — short ending

Author a fixed **seed image** (best possible opening still — generate once offline, or commission) and a **layered LingBot prompt template** (see Reactor prompt guide: static base + swappable event fragments). Do not let `world_prompt` grow unbounded each turn.

### Player-facing loop (canonical)

```
[PAUSED] Video frozen on last frame
         Dispatch text + 3 choices (+ custom)
              │
              ▼ player selects
[INJECT] set_prompt(consequence fragment)
         set_move_* / set_camera_pose from action intent
         resume
              │
              ▼ ~10s stream (configurable 8–12)
[PAUSE]  pause after chunk
         LLM: short dispatch from action + optional pause-frame
         LLM: 3 new choices
         show UI again
```

Custom actions still sacred: map free text → prompt fragment + camera intent, same as button choices.

---

## 4. Architecture

### Principle: keep story brain, replace eyes

```
┌─────────────────────────────────────────────────────────────┐
│  PLAY SURFACE (web-first for testers)                       │
│  Full-bleed <video> from Reactor main_video                 │
│  Overlay: dispatch, choices, HUD, VHS chrome                │
└───────────────────────────┬─────────────────────────────────┘
                            │ JWT + session id
┌───────────────────────────▼─────────────────────────────────┐
│  GAME API (existing Flask api.py / engine)                  │
│  • Session create / state / history                         │
│  • Turn: consequence + choices (text LLM only)              │
│  • Mint Reactor client token                                │
│  • Map choice → { prompt_fragment, camera_intent, duration } │
└───────────────┬─────────────────────────────┬───────────────┘
                │                             │
     Gemini text (keep)              Reactor LingBot World 2
     dispatch / choices /            set_image (once)
     death / inventory               set_prompt (each beat)
                                     move / pause / resume
```

### New modules (thin, not another monolith)

| Module | Responsibility |
|--------|----------------|
| `reactor_world.py` | Session lifecycle wrapper: connect helpers, prompt compose, pause timer, camera intent mapping |
| `scenario/horizon_gate.json` | Seed image path, base prompt layers, beat sheet, max turns, ending conditions |
| `prompt_layers.py` | Compose LingBot prompts: `BASE + LOCATION + EVENT + MOTION` (Reactor layered style) |
| Experience mode `WORLD_STREAM` | New mode alongside Full Frame / Flipbook / No Images |

### Choice → world injection contract

Each choice (and custom action) resolves to:

```json
{
  "dispatch": "...",
  "player_alive": true,
  "lingbot_prompt": "First-person VHS camcorder, red desert mesa, chain-link fence ahead, dust in golden light. Handheld shake. A Horizon patrol jeep rolls past left to right.",
  "camera_intent": {
    "longitudinal": "forward",
    "lateral": "idle",
    "look_horizontal": "idle",
    "look_vertical": "idle",
    "hold_seconds": 10
  },
  "motion_note": "Keep motion verbs out of base; put them only in the event fragment while moving."
}
```

Implementation note from Reactor docs: **text must agree with action**. If we send `forward`, the prompt must not say "standing still." Prefer static base + short event fragment swapped for the unfold window, then revert/idle on pause.

### Vision strategy (cost cut)

| Mode | Vision |
|------|--------|
| Default playtest | **Off** — choices grounded on `dispatch` + `seen_elements` + last `visual_scene` we authored into the prompt |
| Soft grounding | Capture 1 pause-frame every N turns or on hard transitions only |
| Debug | Full vision (current behavior) |

This alone removes a major latency/cost stack while LingBot carries spatial continuity.

### Discord vs web

- **Playtester path = web** (`/standalone` evolved into stream UI). Discord cannot host a live Reactor WebRTC/video track cleanly.
- Keep Discord as optional text/still fallback; do not block production on Discord parity for World Stream.

---

## 5. Presentation (make it feel like a game)

Current standalone is close aesthetically (VHS grain, scrim, HUD) but still-image based and poll-wait heavy.

### Must-ship for testers
1. **Full-bleed live video** as the only hero plane (no inset cards).
2. **Freeze frame on pause** — choices appear over the held last frame; video does not keep drifting behind UI.
3. **Unfold timer** — subtle tape counter / 10s bar during stream (diegetic, not dashboard clutter).
4. **One composition first viewport** — brand/title → Start Tape → immediately into seed world. No mode pickers, admin chrome, or stats in the first screen.
5. **Processing only for LLM** — after pause, show short "rewinding / labeling tape" veil for choice gen (1–3s), never for image gen.
6. **Death** — hard pause + static + tape end card; optional download of recorded segments later.
7. **Mobile**: touch choice buttons; video `playsInline`; no WASD required (camera driven by choice intent).

### Explicitly cut from tester build
- Experience mode picker (lock `WORLD_STREAM`)
- Flipbook / HD toggles
- Admin dashboard in player URL
- Open custom-world / "type any setting"

---

## 6. Stabilizing the story simulator (without boiling the ocean)

The giant evolving `world_prompt` is fragile. For production of *one* experience:

### Replace unbounded evolution with a beat sheet
- Author ~10–15 beats in `scenario/horizon_gate.json` (phase, location tag, threat budget, allowed entities).
- Each turn: LLM fills **local** consequence inside the current beat; advance beat on triggers (fence crossed, entered lab, etc.).
- Keep a short rolling memory: `seen_elements[]`, `injury_state`, `recent_events` (last 3), not a 1500-word rewrite.

### Expose building blocks (minimum viable)
Config-only surface for designers (files, not UI yet):

```
scenario/
  horizon_gate.json      # beats, seed, endings
prompts/
  simulation_prompts.json  # keep narrative voice
  lingbot_layers.json      # BASE / LOCATION / EVENT templates
items.py                 # already exists — normalize inventory IDs
```

Later: admin read-only viewer. Not required for first tester.

### Unify turn pipeline
One path for web playtests:

`POST /api/sessions/{id}/act` → consequence LLM → return injection payload → client drives Reactor → on pause → `POST .../resolve_pause` → choices.

Retire standalone's `_process_turn_background` for the World Stream mode (leave old path behind a flag for still-image demos).

---

## 7. Phased delivery to a gameplay tester

### Phase 0 — Spike (prove the loop)
**Outcome:** Local page streams LingBot World 2 from our seed image; button injects a hardcoded prompt + forward for 10s; pauses; shows fake choices.

- Reactor account + `REACTOR_API_KEY`
- Token mint endpoint on Flask
- Minimal HTML: `<video>` + 3 buttons
- Seed: best existing session frame or `static/img` desert still
- **Success:** You feel "I'm in a world," not "I'm waiting for an image"

### Phase 1 — Wire story brain (no vision, no img2img)
**Outcome:** Real dispatches + real choices drive real `set_prompt` / camera intents.

- New experience mode `WORLD_STREAM`
- Map `visual_scene` → LingBot prompt fragment (new prompt key; shorter, motion-aligned)
- Choice gen without attaching images (text-grounded + seen_elements)
- Fixed 10s unfold; pause; overlay choices on standalone
- Death fairness + inventory still from existing LLM path
- **Success:** Full run of 8+ turns without calling Gemini image APIs

### Phase 2 — Focused scenario pack
**Outcome:** One authored Horizon Gate experience, not freeform chaos.

- `scenario/horizon_gate.json` beat sheet
- Cap turns / force climax
- Opening title → Start Tape
- Trim world evolution to beat-local updates
- Cost caps: Reactor session budget + Gemini text budget logged per run
- **Success:** Two internal playthroughs feel like the same game with different outcomes

### Phase 3 — Tester-ready hardening
**Outcome:** Link you can send.

- Single web entry URL, auth via simple share token or password gate
- Session persistence (S3/R2 or at least durable volume — Render disk is not enough)
- Error recovery: Reactor disconnect → reconnect with last pause frame as new seed if needed
- Double-submit guard on choices
- Basic telemetry: turn latency, Reactor errors, deaths, turn count
- Short tester brief (1 page): controls, what to look for, known issues
- **Success:** External tester completes a run without you on call

### Phase 4 — Polish (after first external feedback)
- Optional soft vision on pause every 3 turns
- Video segment archive → death "tape" mp4
- Subtle diegetic audio bed (optional; keep silent-capable)
- Discord announcement embed that links to web run (not Discord-native stream)

---

## 8. Implementation sketch (concrete hooks)

### Existing code to reuse
- `choices.py` — choice generation (drop image attachment in World Stream)
- `engine._generate_combined_dispatches` — keep JSON contract; add `lingbot_prompt` + `camera_intent` fields
- `api.py` session routes — extend, don't fork a second game
- `templates/standalone.html` + `static/js/standalone.js` — replace scene `background-image` with Reactor video track
- `ai_provider_manager.py` — text provider stays; image provider unused in this mode

### New API surface (minimal)

```
POST /api/sessions                     → { session_id, reactor_jwt, scenario_id }
GET  /api/sessions/{id}/boot           → seed image URL, base prompt, opening dispatch/choices
POST /api/sessions/{id}/act            → { choice } → { dispatch, injection, player_alive, hold_seconds }
POST /api/sessions/{id}/pause_resolve  → after client pause → { choices, inventory, phase }
POST /api/reactor/token                → short-lived JWT for client SDK
```

### Client sequence
1. Create session, connect Reactor with JWT  
2. `uploadFile(seed)` → `set_image` → `set_prompt(base)` → `start` → immediately `pause` (or start with idle and pause after first chunk) so first choices show on a live-looking freeze  
3. On choice: `act` → apply injection → `resume` → timer → `pause` → `pause_resolve` → render choices  
4. On death: `pause` + end card; `reset` Reactor session

### Prompt layering (LingBot-specific)
Follow Reactor guidance:
- **Base:** static world identity matching seed (desert, VHS, POV, 1993, no sci-fi)
- **Location fragment:** current beat location
- **Event fragment:** consequence of the choice (short, visual, present tense)
- **Motion fragment:** only while camera_intent ≠ idle; clear on pause

Do **not** dump the full `world_initial_state` bible into `set_prompt` — it will fight the seed and drift.

---

## 9. Risks and mitigations

| Risk | Mitigation |
|------|------------|
| LingBot drifts from horror / 1993 tone | Strong seed image; short prompts; periodic `trigger_kv_cache_reset` on hard scene changes; authored beats |
| Prompt vs movement disagreement | Central `prompt_layers.compose()` enforces alignment |
| Reactor cost / availability | Session time caps; max turns; pause when tab hidden |
| Gore / safety filters | Keep clinical visual language in LingBot prompts; put visceral detail in text dispatch |
| Reconnect mid-run | Save last pause frame; `reset` + re-seed from that frame |
| Scope creep ("play anything") | Scenario pack is the product; reject open setting input in tester build |
| Dual pipelines keep biting us | World Stream uses only the new act/pause_resolve path |

---

## 10. Tester send checklist

Before sharing a link:

- [ ] Phase 0 spike signed off (feels alive)
- [ ] Full run ≥8 turns without image API calls
- [ ] Death ends cleanly; restart works
- [ ] Mobile Safari + desktop Chrome smoke test
- [ ] Password or token gate on URL
- [ ] Cost dashboard or log for one full run
- [ ] One-page tester brief
- [ ] Known issues listed (drift, rare disconnects, etc.)
- [ ] You can watch session history / tape after their run

---

## 11. Recommended immediate next step

**Do Phase 0 this week as a vertical slice in-repo:**

1. Add `REACTOR_API_KEY` + token mint route  
2. New route `/stream-spike` with Reactor JS SDK + our desert seed  
3. Hardcoded 3 choices that only change `set_prompt` + forward/strafe for 10s  
4. Play it. If the body feels right, Phase 1 is mostly wiring existing LLMs into that skeleton.

Everything else (Discord polish, Flipbook, Veo, lore cache, marketing site) is **parked** until an external tester has completed a World Stream run.

---

## 12. Success definition

**Playtestable production** means:

1. A stranger opens one URL.  
2. They play a focused Horizon Gate tape with live video and meaningful choices.  
3. A run finishes (death or escape) in a satisfying cinematic way.  
4. You receive feedback about *game feel and story*, not "why am I staring at a loading veil."  

That's the bar. Not infinite worlds. Not provider abstraction perfection. One cool experience that finally looks as good as it already plays.
