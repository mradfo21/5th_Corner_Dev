# Moments — cinematic set-piece framework

A **Moment** is a full-screen cinematic interaction layered on top of the live
world. The underlay scene is **paused, not destroyed**, so exiting a Moment
restores the exact place the player left — instantly.

Conversation (TALK → cinematic dialogue) and Camp (CAMP → night campsite with
companions) are the shipped Moment types. This doc describes how to register more.

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
    // re-assert nameplate / choices / hotspots (camp uses this).
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
- `Moments.setScene(url)` / `Moments.clearScene()` — **full-bleed establishing shot**
  (camp). Sibling to the portrait chrome: conversation keeps using `setPortrait`,
  camp uses `setScene`. Nesting conversation on top of camp leaves the scene
  underneath; popping conversation restores the camp layer via `resume`.
- `Moments.notify({ text, icon? })` — RPG-style toast along the letterbox
- `Moments.setChoices(items, onPick)` / `clearChoices()` — dialogue options list
- `Moments.setNameplate(name, sub)`

## Exit = instant resume

Leaving a conversation (or camp) resumes the paused world (see above) rather than
generating anything — the player lands back on the exact frame they left with
the world moving again. This is the fast, seamless feel; no "load" on exit.

## Camp Moment

Press **CAMP** on the action wheel (`#camp-btn`) to push a `"camp"` Moment:

1. Enter/exit use a **fade-to-black** veil (`#moment-fade`, `transition: "fade"`) —
   not the VCR glitch cut conversation uses.
2. `POST /api/camp/enter` builds (or reuses a cached) **4:3** night campsite plate
   from the durable **jeep prop** + up to **5** companion portraits
   (ranked by `last_seen_turn`), via `generate_gemini_img2img(..., ensemble_mode=True)`.
   The red jeep is required in the composition (text + reference). Response includes
   `realtime_prompt` for the live world-model.
3. The client stages the plate with `Moments.setScene`, then **re-anchors the
   Reactor underlay** onto that campsite (`hard_transition`) so the fire is a
   living world-model. `Moments.setSceneLive(true)` makes the scene shell
   transparent so the stream shows through; the explore pad stays available.
4. Tap-target hotspots + a compact **LEAVE CAMP** pill. Tapping a companion
   nests `Talk.start` (firelit `reference_image` from the live frame when possible).
5. **LEAVE CAMP** (and Esc) does **not** restore the campsite. It fades to black
   and fires a hard-transition turn — *"Leave camp and drive the red jeep into
   a new location…"* — so the engine builds a **brand-new level**. The camp
   world is cleared from `Renderer.lastScene` so a late world-model rebuild
   can't resurrect it.

Empty roster still works (quiet fire + jeep). Camp appends one additive
`feed_log` item (`type: "camp"`) for Story Log flavor only — it does **not**
touch `turn_count` / `history` / choice generation.

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

Each companion also stores its **ElevenLabs voice data** (recorded by
`api_talk_session`) under `companion.voice`:

```
voice: {
  voice_id,      # reuse this exact ElevenLabs voice
  description,   # the Voice Design brief — the seed to REGENERATE the voice
  model,         # the TTV model that produced it (e.g. eleven_ttv_v3)
  source,        # designed / cache / fallback / override
  status, cache_key, settings, updated_at
}
```

`voice_id` lets a later scene reuse the exact voice; `description` + `model`
are everything Voice Design needs to regenerate it from scratch if the voice
slot was evicted. The regen description is preserved even if the player later
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

## Portrait animation (world-model) + fast return

The character is **animated by the world model** using the single Reactor
session, with a fast, resume-like exit — best of both:

- **Enter:** show the cinematic img2img still immediately, then
  `animateCharacter()` saves the current world's id (`getWorldId()`) and scene,
  **re-anchors the session onto the portrait** (`applyScene({prompt, imageUrl})`
  via the facade, so `Renderer.lastScene` is untouched), and mirrors the live
  feed into `#moment-portrait-video` (`Moments.setPortraitStream`, revealed when
  `isShowing()`), crossfading over the still. The character moves/breathes with
  the world model.
- **Exit:** `restoreWorldAfterConversation()` reopens the ORIGINAL world by id
  with **`attach_world`** — which paints the env still into the freeze buffer
  instantly (feels like a resume) then reveals the live world **without a
  rebuild**. Falls back to a scene re-apply only when the id is unknown.

So the character animates live during the conversation, and returning to the
world is a fast freeze-still-then-live reveal rather than a slow regeneration.
The CSS **living-portrait** treatment (breathing + grain + orb-linked rim light)
is the always-on baseline for the still and the fallback under reduced-motion /
still-image mode / if the character feed never goes live. Opt out of world-model
animation with `window.__CONVERSATION_ANIMATE__ = false`.
