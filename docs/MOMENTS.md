# Moments — cinematic set-piece framework

A **Moment** is a full-screen cinematic interaction layered on top of the live
world. The underlay scene is **paused, not destroyed**, so exiting a Moment
restores the exact place the player left — instantly.

Conversation (TALK → cinematic dialogue) is a shipped Moment type. Encounter
is the other: a generated character + danger interrupt that restages this
place as a confrontation plate, offers three actions, and **does not** restore
the paused world — if you survive, the aftermath is the scene you walk back
into. Camp is a **playable level** (hard-cut via `/api/camp/enter` +
`Renderer.applyScene`), not a Moment — so PHOTO / SCAN / ACT stay live around
the fire.

## Architecture

| Piece | Role |
|-------|------|
| [`static/js/moments.js`](../static/js/moments.js) | Stack controller: `register` / `push` / `pop`, shared letterbox/HUD/glitch choreography, portrait + scene + notify + choices chrome |
| [`#moment-overlay`](../templates/standalone.html) | Shared markup (letterbox, portrait, **scene**, nameplate, notify tray, choices) |
| `Renderer.pauseUnderlay()` / `resumeUnderlay()` | Freeze the image/Reactor underlay without tearing down the world-model session |
| Type handlers | Per-Moment `enter` / `exit` / optional `resume` / `onEsc` |

Conversation networking (ElevenLabs / `/api/talk/*`) stays inside the existing
`Talk` module in `standalone.js`. Moments only own **presentation**.

## Registering a new Moment type

```js
window.Moments.register("interrogation", {
  async enter(payload, entry) {
    // Chrome (letterbox, HUD hide, underlay pause) is already up.
    window.Moments.setNameplate(payload.subject.label, "under questioning");
    window.Moments.notify({ icon: "⏱", text: "You have 60 seconds" });
    // …start your minigame / timer / UI…
  },
  async exit(result, entry) {
    // Tear down type-specific resources. Shared chrome is cleared by pop().
  },
  async resume(entry) {
    // Optional: called when a nested Moment above this one pops, so you can
    // re-assert nameplate / choices / chrome.
  },
  onEsc(entry) {
    // Return true if you handled Esc. Default pops the Moment.
    window.Moments.pop({ aborted: true });
    return true;
  },
});

// Later:
await window.Moments.push("interrogation", { subject, stakes: "…" });
```

## Shared chrome helpers

- `Moments.setPortrait(url)` — still image + CSS living-portrait animation
- `Moments.setPortraitStream(mediaStream)` — Phase-2 world-model animated portrait
- `Moments.setScene(url)` / `Moments.clearScene()` — optional full-bleed establishing
  shot chrome (legacy / future Moment types). Conversation uses `setPortrait`.
  Camp does **not** use this — it goes through `Renderer.applyScene`.
- `Moments.notify({ text, icon? })` — RPG-style toast along the letterbox
- `Moments.setChoices(items, onPick)` / `clearChoices()` — dialogue options list
- `Moments.setNameplate(name, sub)`

## Exit = instant resume

Leaving a conversation resumes the paused world (see above) rather than
generating anything — the player lands back on the exact frame they left with
the world moving again. This is the fast, seamless feel; no "load" on exit.

## Encounter (interrupt → aftermath)

## Cutscene (4-shot montage)

`Moments.register("cutscene", { transition: "fade" })` — a revived flipbook:
one 2×2 Gemini restage of the current plate (or an optical 4-crop fallback),
played as a letterboxed montage. A **Cutscene** is also a first-class
Experience-graph node, so a run can stitch World A → Cutscene → World B.

- Graph hop stamps `pending_cutscene` and a `cutscene` feed item. The client
  POSTs `/api/cutscene/play` to generate shots, then `/api/cutscene/complete`
  to follow the outgoing `immediate` edge.
- Programmatic: `Cutscene.play({ mood, offline })` or `?cutscene_demo=1` /
  **Shift+K**. Moods: threshold, aftermath, arrival, departure, encounter.
- Esc / space / click skip or advance. Reduced motion holds the last shot.

Server: `cutscene.py`, `/api/cutscene/play`, `/api/cutscene/complete`.
Prototype: `python prototype_cutscene.py --offline [plate.png]`.

`Moments.register("encounter", { transition: "develop" })` — **not** Talk's
VCR glitch. Hitch the live frame, slam a full-screen **ENCOUNTER** flare,
then **hold black** while the confrontation plate generates (never paint
the live-frame grab — that read as a screen capture). Develop the plate of
**this** place (outdoor stays outdoor) with the **authored player** locked
from the character sheet plus a new challenger. Slam three interrupt bars
tagged `confront` / `evade` / `use` — every bar is about **the figure in
the plate**, not the landscape.

Resolve is ceremonial. A pick **holds black**, slams **COMMIT**, then
`POST /api/encounter/resolve` returns a **new generated still of the verb**
(bodies in motion, same two people). That still develops from black. A
**SURVIVED / HURT / CLEAR / DEAD** verdict flares over it, then the
consequence line. **Every pick** then runs the same
`_process_turn_background` pipeline as MOVE TO or a typed `/api/choose`
(`source: "encounter"`, skip image): `advance_story_dynamics` +
`advance_turn_image_fast` write the consequence. Survive / wounded stay
locked and build the next slate from that engine dispatch. Escape / die
release and run the turn in the background. There is no parallel
encounter narrator. Do not also POST `/api/choose`.

Lanes are data. The server owns survive / escape / wounded / die from
lane + stance + kind + `player_state.condition`. The consequence LLM writes
that beat; it does not flip `player_alive`. Escape is getting clear **in
this place** — it is not Talk's restore of the paused explore frame, and
Esc during the Moment attempts the evade bar. Wounded is a condition flag,
not HP (`DAMAGE_SYSTEM_ENABLED` stays off).

Exit applies the escape still with `hard_transition` **before** the overlay
comes down, then `resumeUnderlay()` reveals the modified world. Death pops
without restaging (`survived: false`). Demo: `Shift+N` or `?encounter_demo=1`.
Server: `/api/encounter/begin`, `/api/encounter/resolve`, `/api/encounter/travel`
(walking time counts down a distance clock; looking around does not).

## Encounter sound design

Three layers, in priority order. Missing files fall through; the synth
always plays last so a silent key still has a ceremony.

| Layer | Where | What |
|-------|--------|------|
| Designer WAV / MP3 | `static/audio/encounter/<stem>.wav` | Authored one-shots. Stems listed in `static/audio/encounter/cues.json`. |
| Stock stinger | `assets/music/stock/sting_encounter_*.mp3` served as `/audio/…` | ElevenLabs one-shots, warmed in the background. Catalog in `scene_audio.STOCK_STINGERS`. |
| Synth + pulse | `Sound` mixer family `encounter` | Built-in Web Audio cues + heartbeat. Mute the family from the sound panel. |

**Music + ambience.** Hitch starts a generic confrontation bed
(`SceneAudio.scoreEncounter`, `/api/scene_audio` `mode: "encounter"`) so
the wait for the plate is not silent. When the brief lands the client
re-scores with `music_prompt` (`stance — kind — label — danger — stakes`).
Hostile / desperate / opportunistic / creature change BPM and mood.
Survive leaves that bed off so the aftermath scene can `SceneAudio.score()`
the new walkable world. Abort restores the paused explore bed.

**Timeline**

| Beat | Cue | Also |
|------|-----|------|
| Hitch (~520ms) | `encounterHitch` | heartbeat 76, generic Lyria + tense ambience, duck 0.22 |
| Title flare (~840ms) | `encounterTitle` | the stinger + haptics |
| Letterbox | `encounterEnter` | Moments `enterSound` |
| Plate + choices | `encounterLock` + `encounterStance(stance)` | pulse 84 / 96 / 68 / 62 (creature), stance-colored bed |
| Hover / pick | `encounterChoiceHover` / `encounterChoiceSelect` | remapped from Moments `choiceHover` |
| Commit | `encounterResolve` + **COMMIT** flare | hold black, pulse 108 |
| Action plate | develop from black | the verb, same two people |
| Verdict | `encounterSurvive` / `encounterDie` | **SURVIVED / HURT / CLEAR / DEAD** over the still |
| Survive / wounded | stay locked | same turn pipeline as choose + new slate |
| Escape | `encounterSurvive` | `endEncounter({ restore: false })` + aftermath score |
| Die | `encounterDie` | heartbeat stop |
| Abort / pop | `encounterExit` | restore explore bed |

## Camp (playable level)

Press **CAMP** on the action wheel (`#camp-btn`) to hard-cut into a **playable
campsite level** — not a Moments cinematic. Full HUD stays live (PHOTO / SCAN /
ACT / explore pad); there is no letterbox / nameplate chrome.

1. Enter uses the same **fade-to-black** contract as MOVE TO
   (`ReactorRenderer.beginSceneFade` + hard re-anchor), not the VCR glitch.
2. `POST /api/camp/enter` builds (or reuses a cached) **4:3** night campsite plate
   via `generate_gemini_img2img(..., ensemble_mode=True)` with **every** available
   companion screenshot as an img2img reference **plus** the durable **jeep prop**
   (Gemini hard-cap: jeep + up to 5 companions; extras stay named in the prompt).
   Portraits resolve from `sessions/<id>/images/companion_*.png`. A numbered
   REFERENCE IMAGE MAP tells the model which ref is the jeep vs each person.
   Response includes `realtime_prompt` for the live world-model.
3. The client applies the plate through `Renderer.applyScene(..., { hard_transition: true })`
   — the same path as any other level — so SCAN / PHOTO / Talk work normally.
   Companions are reached via **SCAN → TALK**, not Moment hotspots.
4. A compact `#leave-camp-btn` (and Esc) fires a hard-transition turn —
   *"Leave camp and walk into a new outdoor location…"* (`source: "camp_leave"`)
   so the engine builds a **brand-new on-foot level**. The choice is normalized
   server-side to forbid cab/dashboard/driving POVs (those break the walkable
   world model). Camp is cleared from `Renderer.lastScene` so a late rebuild
   can't resurrect it.

Empty roster still works (quiet fire + jeep). Camp enter appends one additive
`feed_log` item (`type: "camp"`) for Story Log flavor only — it does **not**
touch `turn_count` / `history` / choice generation. Leave is a real choose.

## Companions (persistent roster)

Every character the player has a conversation with is saved as a **companion**:
their generated cinematic portrait is copied to a stable, sweep-protected file
(`companion_<slug>.png`) and a roster record is written to
`state.companions[label]` (`{label, kind, portrait_url, first_seen_turn,
last_seen_turn, seen_count, prompt, scene}`), recorded by `api_talk_portrait`.

- `GET /api/companions` — list the roster (most recently seen first), joined
  with the character-memory notes/trust.
- `POST /api/companions/place` — the primitive for continuing-story beats:
  given `{label, reference_image?, prompt?}`, it img2img's the companion INTO
  the current scene using `[current frame + companion portrait]` as references,
  so the same character reappears standing in the present place. Returns the
  new scene `image_url`.
- `POST /api/companions/regenerate_voice` — force a new ElevenLabs Voice Design
  from the companion's stored `voice.description` seed. Body:
  `{label, session_id?, wait?}`. Returns `{label, voice:{voice_id, status,
  cache_key, description, model, source}}`. When `status` is `generating`,
  poll `/api/talk/voice/status?cache_key=…` (the roster updates when ready).

Each companion also stores its **ElevenLabs voice data** (recorded by
`api_talk_session`) under `companion.voice`:

```
voice: {
  voice_id,      # reuse this exact ElevenLabs voice
  description,   # the Voice Design brief — the seed to REGENERATE the voice
  model,         # the TTV model that produced it (e.g. eleven_ttv_v3)
  source,        # designed / cache / fallback / override / companion
  status, cache_key, settings, updated_at
}
```

`voice_id` is reused on later TALK sessions (`resolve_voice_for_subject`
prefers the companion roster before designing a new voice). `description` +
`model` are everything Voice Design needs to regenerate from scratch via
`/api/companions/regenerate_voice` (or automatically when the stored id was
evicted). The regen description is preserved even if the player later
switches to a preset voice. Surfaced in `GET /api/companions`.

The client shows a "{label} added to your companions" notification the first
time each character is met.

## Props (persistent objects)

Same pattern as companions, for durable recognizable **objects** (vehicles,
set pieces) that must look identical across visits:

- `_persist_prop_image` → `prop_<slug>.png` (sweep-protected)
- `state.props[slug]` → `{label, slug, portrait_url, prompt, first_seen_turn, updated_at}`

The **jeep** (`state.props["jeep"]`, file `prop_jeep.png`) is generated once on
first CAMP visit and reused forever as an img2img reference. Camp always
includes it at the edge of the firelight. Future mission-transition beats can
reuse the same prop without regenerating it.

## Character memory hook

`/api/talk/end` accepts an optional `subject` and upserts
`state.characters[label]` with `{ first_met_turn, last_talk_turn, talk_count,
notes[], trust }`. This is additive metadata — it does **not** mutate
`history` / `feed_log`. Future trust / relationship Moments can read and
extend this record.

## Portrait (crop still) + optional world-model animation

The speak-screen likeness is the **SCAN bounding-box crop** of the person
you clicked, pinned immediately and persisted as their companion plate
(no Gemini img2img — that path invented the player). The CSS
**living-portrait** treatment (breathing + grain + orb-linked rim light)
is the default animation.

World-model re-anchor is **off** by default. The live session is a
third-person player follow-cam; `animateCharacter()` used to `applyScene`
the portrait and then `setPortraitStream(#reactor-video)` the moment
`isShowing()` was true — which it already was for the env world — so the
conversation frame showed the **player**, not the tagged subject. Opt in
with `window.__CONVERSATION_ANIMATE__ = true` (then enter still saves the
env world id + a live frame grab, re-anchors, and exit restores via
`attach_world` / rebuild).
