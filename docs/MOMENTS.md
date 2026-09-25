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

Conversation networking (`/api/talk/*`, each answer spoken through
`/api/narrator/say` by `VoiceOut`) stays inside the existing `Talk` module in
`standalone.js`. Moments only own **presentation**.

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

**A fight is played by tabletop rules** (`combat.py` — read its docstring;
`Battle` in standalone.js draws it). A d20 against a target: ATTACK rolls
against his armour class and a hit rolls damage dice off his HP bar; FLEE
and REASON are skill checks against his DCs. He swings every round with his
named move (`character.move`, written by the brief). Initiative is rolled
once when the fight opens — or decided by detection: hidden is a SURPRISE
round for you, hunted an AMBUSH for him. Natural 20 always succeeds (a
critical doubles the damage dice), natural 1 always fails (a fumble gives
him advantage). A bloodied foe may break on MORALE; the boss never does. At
0 HP: armour from the pack takes the first killing blow of a fight, then a
DEATH SAVE (d20 ≥ 10, harder each time). A typed action that uses the scene
well earns ADVANTAGE (`encounter.judge_custom_action`). The bars ride the
top letterbox — you on the left, him on the right — and the slate reads
ATTACK / FLEE / REASON with each word's exact odds (`combat.lane_odds`).
`tools/encounter_length_probe.py` plays thousands of fights through the
rules and prints how long they run and how often they kill.

A pick throws the dice FIRST: `POST /api/encounter/exchange` plays the
round (`encounter.roll_exchange` → `combat.play_round`), stores it on the
fight as `pending`, and returns its **beats** in milliseconds. The client
plays them (each d20 spinning onto its face, the bars draining, the picture
shaking) while `POST /api/encounter/resolve` draws the **new generated
still of the verb** from exactly that result; a pending round is never
rolled again. When the still lands it plays as the payoff. A won fight ends
on **SURVIVED** / **TALKED DOWN** / **DRIVEN OFF** (rounds, HP left, the
spoil it dropped); getting clear gets the **CLEAR** card.

The fight wears the game's own type, not a battle screen's: names in the
goal HUD's tracked mono, bone hairline bars (red ink only when nearly out),
the battle line in Manrope 300 and in the second person like the prose
("You drop your camera at him…" / "It lands. 10 damage."), the rolls under
it in OSD mono, the endings set like the GOD wordmark. No bold, no
exclamation marks, no "used X!" — the first cut had all three and read as a
cartoon beside the rest of the app.

**Dying is a scene.** The dice know you are dead before any picture exists,
so the client holds the moment (`Battle.dying`: the colour drains, the
heartbeat slows, one line stays) while the resolve draws a **death
flipbook** — outcome `die` has its own direction in
`build_encounter_resolve_prompt`, all of it on the player's death, and its
panels animate the killing blow (`_death_beat`). The reel plays slowly
(`DEATH_FRAME_MS`), pushing in, the dark closing; its last frame becomes the
world; only then does YOU DIED come up over it, naming what did it ("Killed
by the FREELANCER · HIDDEN BLADE · round 2"). The engine's `game_over` item
is held until the reel has played. **Every pick** then runs the same
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
Esc during the Moment attempts the evade bar. Player HP (30) outlives the
fight in `player_state.hp` and mends 8 between fights; `condition` is
derived from it (wounded at or under half) for the rest of the game to read.
`DAMAGE_SYSTEM_ENABLED` (turn-by-turn damage outside a fight) stays off.

Exit applies the escape still with `hard_transition` **before** the overlay
comes down, then `resumeUnderlay()` reveals the modified world. Death pops
onto the death reel's last frame (see above). Demo: `Shift+N` or `?encounter_demo=1`.
Server: `/api/encounter/begin`, `/api/encounter/exchange`, `/api/encounter/resolve`, `/api/encounter/travel`
(walking time counts down a distance clock; looking around does not).

## Encounter sound design

Three layers, in priority order. Missing files fall through; the synth
always plays last so a silent key still has a ceremony.

| Layer | Where | What |
|-------|--------|------|
| Designer WAV / MP3 | `static/audio/encounter/<stem>.wav` | Authored one-shots. Stems listed in `static/audio/encounter/cues.json`. |
| Stock stinger | `assets/music/stock/sting_encounter_*.mp3` served as `/audio/…` | One-shots already on disk (generated by ElevenLabs before 2026-09-25; nothing on the player's key makes new ones — `docs/plans/ONE_KEY_AUDIO_PLAN.md`). Catalog in `scene_audio.STOCK_STINGERS`. |
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
| Commit | `encounterResolve` | pulse 108; the dice are thrown (`/api/encounter/exchange`) |
| Each die | `encounterRoll` ticks, then `encounterLand` / `encounterMissed` — by what it means for YOU (his hit is `Missed`) | the face slams down on the track (`.bt-die.slam`), the line it had to beat pulses or cracks |
| A natural | `encounterNat20` (your 20, his 1) / `encounterNat1` (his 20, your 1) | `impact("nat20")` a clean flare / `impact("nat1")` the colour heads slip + red tears |
| A blow lands | `encounterStrike` (you) / `encounterHurt` (him) / `encounterCrit` | `impact()`: yours punches the frame in, his jolts it and drains the colour, a crit splits the colour heads (SVG `#bt-rgb-a/-b`) and tears the picture; shake, flash; hit-stop (80ms, 190ms on a crit) before the bar drains and the number lands |
| A swing misses | `encounterWhiff` (attack or strike) | `impact("whiff")` the frame smears sideways and drifts back |
| They go down | `encounterKo` | `impact("ko")`, hard shake, their bar dims |
| Action plate | the standoff is held until it decodes | the verb, same two people — the payoff |
| Won | `encounterVictory` | **SURVIVED** (or TALKED DOWN / DRIVEN OFF) · rounds · HP left · the spoil |
| Verdict | `encounterSurvive` / `encounterDie` | **CLEAR / DEAD** when a fight ends any other way |
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
- `POST /api/companions/regenerate_voice` — design the voice again (Gemini
  voice design) from the companion's stored `voice.description` seed. Body:
  `{label, session_id?, wait?}`. Returns `{label, voice:{voice_id, status,
  cache_key, description, model, source}}`. When `status` is `generating`,
  poll `/api/talk/voice/status?cache_key=…` (the roster updates when ready).

Each companion also stores its **voice data** (recorded by
`api_talk_session`) under `companion.voice`:

```
voice: {
  voice_id,      # a Gemini voice name ("Algenib") or a designed "voice_..."
  description,   # the design brief — the seed to REGENERATE the voice
  model,         # the model that designed it (gemini-3.8-flash-tts)
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
