# 🔥 CAMP — a new wheel button and cinematic Moment

**Status: proposed / not yet implemented.** This is a design + integration plan, written
against the current codebase, for a "CAMP" action on the action-wheel that teleports the
player to a night campsite with every companion they've met, sitting around a fire — a
breather beat, distinct from a combat/investigation turn. It leans hard on infrastructure
that already exists (`Moments`, the companion roster, the portrait pipeline) rather than
inventing a parallel system.

> Per the brief: this plan covers **getting the moment itself right** — arriving at camp,
> who's there, and talking to them. **How the player leaves camp and starts a new
> mission is deliberately left as an open question** (§9) with a recommended default and
> two follow-up directions, to be decided once the arrival moment is validated.

---

## 1. The moment, in one paragraph

Press **CAMP** and the world holds its breath (same glitch/letterbox choreography as a
conversation) and hard-cuts to: night, a fire, the jeep parked at the edge of the light,
and every companion the player has met so far seated around the flames — the "cast
reunion" shot. It's not a close-up like a conversation portrait; it's a **wide
establishing shot** of a place, populated with people the player actually recognizes,
because it's built from their own persisted portraits. Walking up to (tapping) any
companion opens the exact same TALK experience already in the game, just re-lit by
firelight instead of wherever they were first met. Leaving camp is instant and free —
no image regenerates, the player's actual scene is exactly where they left it, same as
exiting a conversation today.

The novelty *is* the roster payoff: the more people you've talked to, the fuller the camp
gets. It's the game's first moment that looks backward across the whole run instead of
forward into the next turn.

---

## 2. Fit with existing architecture (reuse vs. new)

| Piece | Reuse as-is | New |
|---|---|---|
| Cinematic chrome (letterbox, glitch, HUD-hide, pause/resume underlay) | `Moments.push/pop`, `Renderer.pauseUnderlay/resumeUnderlay` | — |
| Companion roster (who you've met, portraits, voices) | `state.companions`, `GET /api/companions` | — |
| Talking to someone | `Talk.start(subj)`, `/api/talk/session`, `/api/talk/message`, `/api/talk/portrait`, `/api/talk/end` | — |
| Multi-reference compositing | `generate_gemini_img2img(reference_image_path=[...])` (up to 6 refs) | Camp-specific prompt + ref selection |
| Action-wheel button pattern | `#action-wheel` absolute-positioned buttons, `turn-active`/`fw-open` gating classes | `#camp-btn` + its own gating |
| Full-screen scene backdrop (not a small circular portrait) | — | **new** `#moment-scene` chrome (see §5) |
| A persistent, recognizable vehicle prop | — | **new** "prop" concept, modeled directly on the companion-portrait pattern |

Nothing here requires touching the core turn loop (`advance_turn`, `_gen_image_impl`,
choice generation). Camp is a **side pocket**, exactly like a conversation: it doesn't
increment `turn_count`, doesn't rewrite `history`, and exiting it doesn't cost a
generation. That's the property that makes it safe to ship without destabilizing the
survival loop.

---

## 3. UX flow

```
Idle world (image or realtime mode, no Moment active)
        │  press CAMP  (button, or a shortcut key, e.g. "C")
        ▼
Moments.push("camp", {})            same choreography as TALK: glitch, letterbox in,
        │                           HUD hidden, underlay paused (frozen, not destroyed)
        ▼
"camp" Moment.enter():
  - show establishing-shot chrome (developing shimmer, full-bleed not portrait-sized)
  - GET /api/companions  → who's known
  - POST /api/camp/enter → composite scene (fire + jeep + up to N companions)
  - reveal the shot (crossfade), place tap-targets over each recognizable figure
        ▼
Player taps a companion silhouette
        ▼
Talk.start(subject)  — IDENTICAL to today's conversation flow, just entered from camp
  (portrait fetch passes the camp frame as `reference_image` so the close-up is lit
   by the fire, not by wherever they were first met)
        │  Esc / end conversation
        ▼
Back at the wide camp shot (conversation was a nested Moment; popping it returns here)
        │  Esc / "LEAVE CAMP" button
        ▼
Moments.pop() → underlay resumes exactly where the player left it, no regeneration
```

Nested Moments already work today (`moments.js`'s stack keeps the layer below active and
re-tags `moment-active`/`moment-<type>` on pop), so "talk to someone while at camp" is
just `Moments.push("conversation", {subject})` **on top of** the already-pushed `"camp"`
Moment — no new stacking logic needed, just registering `"camp"` as a normal type.

---

## 4. The CAMP button

Added to `#action-wheel` in `templates/standalone.html`, alongside `free-will-btn` /
`realtime-btn` / `scan-btn`. Suggested placement: mirrored opposite `SCAN` (which sits at
`translateX(calc(-100% - 34px))`), so CAMP doesn't crowd the ACT/PHOTO/SCAN cluster —
e.g. a slight upward offset above the wheel, since CAMP is a *deliberate* full stop, not
a fast per-turn tool. Icon: a small flame (🔥) or campfire glyph, label `CAMP`.

**Availability (mirrors `updateScanButton`'s gating):**

```js
function updateCampButton() {
  const turnActive = el.actionWheel.classList.contains("turn-active");
  const ok = !state.gameOver && !turnActive && !state.processing &&
    !state.awaitingResolution && !(Talk && Talk.isOpen()) && !Moments.isActive();
  el.campBtn.disabled = !ok;
}
```

Explicitly **disabled during**: an active turn, an open conversation, an already-open
Moment (no stacking CAMP under CAMP), game over. It should behave like a "pause and step
outside the story" action — always reachable when the player has agency, never mid-turn.

---

## 5. A new Moment chrome: full-scene, not portrait

`#moment-overlay` today has exactly one visual surface: `#moment-portrait`, a small,
centered, circular/framed shot meant for a close-up face. Camp needs a **full-bleed
establishing shot** — the whole frame, like the game's own scene images. Rather than
overload `#moment-portrait`'s CSS for two incompatible shapes, add a sibling layer:

```html
<!-- alongside #moment-portrait inside #moment-overlay -->
<div id="moment-scene" class="hidden" aria-hidden="true">
  <div id="moment-scene-shimmer" aria-hidden="true"></div>
  <img id="moment-scene-img" alt="" />
  <div id="moment-scene-grain" aria-hidden="true"></div>
  <!-- tap targets laid out over recognizable figures, populated by JS -->
  <div id="moment-scene-hotspots" aria-hidden="true"></div>
</div>
```

`moments.js` gains `Moments.setScene(url)` / `Moments.clearScene()` mirroring
`setPortrait`/`clearPortrait` (same crossfade-on-load pattern), and
`showOverlayChrome()`/`hideOverlayChrome()` learn to toggle `#moment-scene` vs.
`#moment-portrait` based on the entering type (or the type handler just calls whichever
one it needs — conversation keeps using `setPortrait`, camp uses `setScene`). This is a
small, additive change to `docs/MOMENTS.md`'s "shared chrome" contract, not a rewrite —
the pause/resume/HUD-hide/letterbox machinery in `push`/`pop` doesn't change at all.

**Companion hotspots:** `POST /api/camp/enter`'s response includes each attendee's
`label` and an approximate `{x_pct, y_pct}` seat position (server picks fixed seat slots
around the fire based on attendee count — see §7) so the client can drop a simple
tap-target over each figure without needing live object detection. This is deliberately
low-tech compared to the SCAN system's vision-based hotspot detection: camp's cast is
already known ahead of generation, so we don't need to *detect* who's in the shot, only
*place* them.

---

## 6. Backend: `/api/camp/*`

New Flask routes in `engine.py`, registered in `api.py` next to the companion routes:

```python
app.add_url_rule('/api/camp/enter', 'standalone_api_camp_enter', engine.api_camp_enter, methods=['POST'])
```

### `POST /api/camp/enter`

Request: `{"session_id"?: str}`

1. Load state; read `state.companions` (the roster).
2. Select attendees — see §7 for the cap/selection rule.
3. Build (or reuse — see §8 caching) the establishing shot:
   - References passed to `generate_gemini_img2img` (max 6 slots): the **jeep prop
     reference** (§7) + up to **5 companion portraits**. If the roster is empty, this is
     a text2img "quiet fire, jeep alone" shot instead — camp always works, even on turn 1.
   - Prompt: night camp exterior, campfire glow, the game's own `world_prompt` /
     `time_of_day` styling swapped to night, explicit instruction to seat each
     referenced person around the fire, and to include the referenced jeep parked at
     the edge of the firelight. (Full prompt strategy in §7.)
   - `strength` tuned higher than `companion_place`'s 0.5 (this is a full new location,
     not "insert into the current frame") — closer to a fresh composition anchored by
     the reference *people*, not by the current scene's background.
4. Persist nothing to `history`/`feed_log` that would perturb the next turn's continuity
   — see §10 for the one additive, non-mutating feed entry we *do* want.
5. Response:
   ```json
   {
     "image_url": "/images/...",
     "attendees": [
       {"label": "Marisol", "portrait_url": "...", "seat": {"x_pct": 28, "y_pct": 62}},
       {"label": "Doc Reyes", "portrait_url": "...", "seat": {"x_pct": 71, "y_pct": 58}}
     ],
     "jeep_included": true
   }
   ```

### Talking to a companion at camp

**No new endpoint.** The client calls `Talk.start(subject)` exactly as it does from a
SCAN tag today, where `subject = {label, kind: "companion", speaks: true}` built from the
attendee entry. `fetchPortrait()` already accepts a `reference_image` (a captured frame)
so it can img2img the close-up off the camp shot instead of the live scene — the
"establishing wide shot → close-up of the person you tapped" cut is exactly what a
director would do, and it's free: the existing `api_talk_portrait` code path already
supports this, no backend change needed.

### Leaving camp

**No backend call.** `Moments.pop()` alone — same "exit = instant resume" contract as
conversation (`docs/MOMENTS.md` §"Exit = instant resume"). Camp never touched the
underlay's state, so there's nothing to roll back.

---

## 7. The jeep — a persistent prop, not a one-off generation

Today the companion pipeline has a clean pattern for "a specific, recognizable thing that
must look the same across every future scene it appears in": generate once, copy to a
**stable filename**, always pass that file as an img2img reference from then on
(`_persist_companion_image` → `companion_<slug>.png`). The jeep needs exactly this, just
for a **prop** instead of a **character**. Concretely, add the prop-equivalent of the
companion helpers:

- `_persist_prop_image(image_path, session_id, slug) -> web_url` — same copy-to-stable-
  file logic as `_persist_companion_image`, writing `prop_<slug>.png` (e.g. `prop_jeep.png`).
- `state.props["jeep"] = {"label": "your red jeep", "portrait_url": ..., "prompt": ...,
  "first_seen_turn": ..., "updated_at": ...}` — a `state.companions`-shaped sibling dict,
  so the same `_load_state`/`_save_state` round-trip and the same "durable, sweep-
  protected" guarantee applies.
- First time the jeep is needed (the player's first CAMP visit, if it's never appeared
  before) generate it **once** with a dedicated text2img prompt (red 1990s Jeep, dusty,
  parked, consistent with the world's "1990s trucks, jeeps, helicopters" setting
  constraint already in `prompts/simulation_prompts.json`) and persist it. Every later
  scene that wants the jeep (camp, and later the mission-transition moment in §9) reuses
  `state.props["jeep"]["portrait_url"]` as a reference image — never regenerates it from
  scratch, so it stays visually the same jeep run after run.
- The brief says the jeep "should probably always be in the scene" — this plan makes
  that possible (one durable reference, reusable everywhere) without committing to
  *actually* forcing it into every turn's image yet. That's a `build_image_prompt` /
  world-evolution change with its own blast radius on the main loop and belongs in a
  follow-up once camp (and the jeep prop pipeline) is proven out — flagged in §9.

**Camp always includes the jeep** in its reference set (parked at the edge of the
firelight) — it's the visual anchor that primes the player for "this is your vehicle,
you'll be leaving in it," setting up §9 without committing to the transition mechanic yet.

---

## 8. Multi-companion compositing: selection, caching, cost

- **Reference budget:** `generate_gemini_img2img` accepts up to 6 reference images. Camp
  spends 1 on the jeep prop, leaving **5 companion slots**. Rank the roster by
  `last_seen_turn` desc (most recently met/talked-to first) and take the top 5. A roster
  of 6+ companions still shows *a* full camp — just the 5 most current relationships,
  which is also the more narratively relevant set ("who matters to you right now").
- **Seat layout:** a small fixed table keyed by attendee count (1 → center-ish by the
  fire; 2 → either side; 3–5 → an arc), so hotspot placement doesn't need per-image
  vision analysis. Good enough for tap-targets; not meant to be pixel-perfect.
- **Caching:** key on `(session_id, sorted attendee labels, jeep prop hash)`, mirroring
  `_portrait_cache_key`'s `(session, label, scene_hash)` pattern. Revisiting camp with
  the *same* roster reuses the cached shot instantly (no re-generation, no cost); a newly
  met companion since the last visit invalidates the cache and regenerates. Store this
  cache the same way `_PORTRAIT_CACHE` works today (in-process dict, session-scoped).
- **Budget guard:** reuse the existing `_rate_limited(bucket, min_interval)` pattern
  (`companion_place` uses `1.2`s) on the new `camp_enter` bucket, and consider a
  `CONVERSATION_PORTRAIT_BUDGET`-style per-session cap if camp turns out to be
  regenerated often (unlikely, given caching, but cheap insurance).

---

## 9. Solo case & empty roster

Turn 1, before the player has met anyone: CAMP still works — jeep alone at the fire,
maybe a beat of narration/ambience ("no one's out here with you yet"). This matters
because it means the button can ship enabled from the very first turn instead of needing
an unlock condition, and it previews the payoff ("this is where your companions will
show up") from the first press.

---

## 10. Story Log — additive, non-mutating

Per §6, camp never touches `turn_count`/`history` (the state the choice/consequence loop
reasons over). It *should* still leave a trace in the Story Log for flavor and pacing
memory, the same way companion "place" beats don't advance the turn but are still
memorable. Add one lightweight, additive `feed_log` entry on first entry per visit (not
per companion talked to) — something like `{"type": "camp", "content": "You made camp
for the night.", "image_url": <the establishing shot>}` via the existing
`create_feed_item`/`_feed_append` helpers, exactly the pattern already used for
ambient/notification-style feed items elsewhere in `engine.py`. This is display-only; it
must not feed back into `_generate_combined_dispatches` or choice generation.

---

## 11. What this phase deliberately does NOT do

- Does not change `world_prompt`, `turn_count`, or the choice-generation pipeline.
- Does not force the jeep into every regular turn's image (that's a separate,
  higher-blast-radius change to `build_image_prompt`/world evolution — §12).
- Does not yet define how you *leave* camp into a new story beat beyond "resume exactly
  where you were" (the same safe default conversation already uses). That's the open
  question in §12, explicitly deferred per the brief.
- Does not add a day/night cycle to the main loop — camp's "night" is local to the Moment
  (like a memory/dream beat), not a change to the live scene's time of day.

---

## 12. Open question (deferred): transitioning out of camp into a new mission

The brief explicitly asks to solve this *after* the arrival moment is validated. Recording
the candidates now so the jeep prop (§7) is built with this in mind:

1. **Default for this phase — plain resume.** `Moments.pop()` back to the exact scene
   the player left, same as ending a conversation. Zero risk, ships with the rest of
   this plan, and is indistinguishable from "camp is just a nice pause" until a real
   transition mechanic lands.
2. **Jeep drive-off cinematic (likely direction).** A "DRIVE OUT" action inside the camp
   Moment that, instead of a plain pop, triggers a `hard_transition` establishing shot
   (reusing `is_hard_transition`'s existing hard-cut visual grammar) framed from/beside
   the jeep — reusing the same `prop_jeep.png` reference — and hands off into
   `generate_directive`/`_generate_dispatch` to seed a fresh objective, i.e. camp becomes
   a literal chapter break. This is the biggest change of the three: it touches the main
   turn loop's re-anchoring logic and deserves its own follow-up plan once §1–§11 are
   built and playtested.
3. **Camp-triggered choice injection.** Leaving camp normally (option 1) but appending a
   one-time special choice to the *next* turn's choice list ("Pack up and drive toward
   the next lead") that, when picked, plays a jeep-anchored hard transition through the
   existing `advance_turn`/`_gen_image_impl` path with no new state machine at all —
   lower risk than (2) because it rides the existing choice/consequence pipeline instead
   of adding a second one.

Recommendation: ship options in §1–§11 with behavior (1), get it in front of the player,
then pick between (2) and (3) for the mission-transition follow-up plan — (3) is the
lower-risk starting point since it reuses the existing turn loop verbatim; (2) is the
more cinematic payoff once the pattern is proven.

---

## 13. File-by-file change list

- **`engine.py`**
  - `_persist_prop_image`, `state.props["jeep"]` read/write helpers (mirrors
    `_persist_companion_image`/`_record_companion`).
  - `api_camp_enter()` — builds/caches the composite establishing shot (§6, §8).
  - One additive `create_feed_item("camp", ...)` call on first entry per visit (§10).
- **`api.py`**
  - `app.add_url_rule('/api/camp/enter', ..., engine.api_camp_enter, methods=['POST'])`.
- **`static/js/moments.js`**
  - `setScene(url)` / `clearScene()` alongside `setPortrait`/`clearPortrait` (§5).
  - `showOverlayChrome`/`hideOverlayChrome` learn about `#moment-scene`.
- **`static/js/standalone.js`**
  - `updateCampButton()` (§4) wired into the same render/gating pass as
    `updateScanButton()`.
  - `window.Moments.register("camp", { enter, exit, onEsc })` — `enter` calls
    `/api/camp/enter`, renders hotspots, wires each to `Talk.start(...)`; `exit` is a
    no-op (nothing to tear down beyond shared chrome, which `pop()` already clears).
  - Click handler on the new `#camp-btn` → `Moments.push("camp", {})`.
- **`templates/standalone.html`**
  - `#camp-btn` inside `#action-wheel` (§4).
  - `#moment-scene` + children inside `#moment-overlay` (§5).
- **`static/css/standalone.css`**
  - `#camp-btn` positioning/disabled states (mirrors `#scan-btn`'s block).
  - `#moment-scene` full-bleed layout + shimmer/grain treatment (mirrors
    `#moment-portrait`'s existing crossfade CSS, just full-frame instead of framed).
- **`docs/MOMENTS.md`**
  - Document the `#moment-scene` chrome alongside `#moment-portrait` and the new
    "props" concept alongside the existing "Companions (persistent roster)" section.

---

## 14. Testing plan

1. **Empty roster:** fresh session, press CAMP → jeep-alone shot, no attendee hotspots,
   leaves cleanly.
2. **Populated roster:** talk to 2–3 companions across a run, then CAMP → all of them
   appear, hotspots land roughly on each figure, tapping one opens a normal conversation
   lit by the camp reference.
3. **Nested Moment:** CAMP → talk to someone → Esc (closes conversation, returns to the
   wide camp shot, not straight to the world) → Esc again (leaves camp, resumes the
   world exactly where it was).
4. **Cache behavior:** re-enter CAMP twice in a row with no new companions met → second
   entry is instant (cache hit, confirm via a log line / no new image request).
5. **New companion since last visit:** meet someone new, re-enter CAMP → cache miss,
   regenerates with the new attendee included.
6. **Gating:** CAMP button is disabled mid-turn, during an open conversation, on game
   over; re-enables once the world is idle again.
7. **Roster >5:** synthetic session with 6+ companions → camp shows exactly the 5 most
   recently seen, no server error, no silently-dropped reference budget overflow.

---

## 15. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Gemini compositing 5 companions + a jeep into one coherent shot degrades likeness | Cap at 5 + jeep (6 total, the API's own hard limit); if quality suffers in testing, drop the cap further before adding complexity elsewhere. |
| New "full-scene" Moment chrome duplicates portrait CSS/logic | Explicitly designed as a sibling surface reusing the same crossfade pattern, not a fork of the whole Moments stack — only `setScene`/`clearScene` are new. |
| Player expects CAMP to "count" as a story beat (save/consequence) | Explicitly scoped as a non-mutating side pocket in this phase (§6, §11); documented plainly so no future code path is tempted to hang consequence logic off it. |
| Jeep prop generation drifts from later mission-transition needs before that design lands | Keep the prop pipeline generic (`state.props[<slug>]`, not jeep-specific fields) so §9's follow-up can add a second prop without reworking this one. |
| Cost: regenerating the composite shot too often | Roster-signature cache (§8) makes repeat visits free; only a genuinely new companion (or first visit) triggers a real generation. |

---

## 16. Summary

CAMP is a new `Moments` type, not a new subsystem: it reuses the pause/resume underlay,
the companion roster, and the entire TALK pipeline verbatim, and only adds two genuinely
new pieces — a full-bleed "scene" chrome (vs. today's portrait-only chrome) and a
persistent-prop pattern (the jeep) modeled directly on the existing persistent-companion
pattern. That keeps the change additive and low-risk to the core survival loop while
delivering the actual point of the feature: seeing everyone you've met, together, by the
fire. The jeep is deliberately present from day one as a Chekhov's gun for the
mission-transition mechanic the brief asks to design next, once this moment is in
players' hands.
