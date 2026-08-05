# Moments — cinematic set-piece framework

A **Moment** is a full-screen cinematic interaction layered on top of the live
world. The underlay scene is **paused, not destroyed**, so exiting a Moment
restores the exact place the player left — instantly.

Conversation (TALK → cinematic dialogue) is the shipped Moment type. Camp is a
**playable level** (hard-cut via `/api/camp/enter` + `Renderer.applyScene`), not
a Moment — so PHOTO / SCAN / ACT stay live around the fire.

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

## Portrait animation (world-model) + fast return

The character is **animated by the world model** using the single Reactor
session, with a fast, resume-like exit — best of both:

- **Enter:** show the cinematic img2img still immediately, then
  `animateCharacter()` saves the current world's id (`getWorldId()`), scene, and
  a **live env frame grab** (guide PNGs get swept and 404 later), **re-anchors
  the session onto the portrait** (`applyScene({prompt, imageUrl})` via the
  facade, so `Renderer.lastScene` is untouched), and mirrors the live feed into
  `#moment-portrait-video` (`Moments.setPortraitStream`, revealed when
  `isShowing()`), crossfading over the still. The character moves/breathes with
  the world model.
- **Exit:** `restoreWorldAfterConversation()` fades over the character, then
  reopens the ORIGINAL world by id with **`attach_world`** (which **stops** the
  character travel first — attaching while still travelling left the stream
  stuck on the person with the HUD back). Prefers the captured env frame over a
  swept guide URL. If attach fails, falls back to a hard-transition rebuild
  (prompt + frame, or prompt-only). Dead guide URLs no longer retry forever
  every 1.5s.

So the character animates live during the conversation, and returning to the
world is a fast freeze-still-then-live reveal rather than a slow regeneration.
The CSS **living-portrait** treatment (breathing + grain + orb-linked rim light)
is the always-on baseline for the still and the fallback under reduced-motion /
still-image mode / if the character feed never goes live. Opt out of world-model
animation with `window.__CONVERSATION_ANIMATE__ = false`.
