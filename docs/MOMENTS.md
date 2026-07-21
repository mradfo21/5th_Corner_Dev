# Moments — cinematic set-piece framework

A **Moment** is a full-screen cinematic interaction layered on top of the live
world. The underlay scene is **paused, not destroyed**, so exiting a Moment
restores the exact place the player left — instantly.

Conversation (TALK → cinematic dialogue) is the first Moment type. This doc
describes how to register more.

## Architecture

| Piece | Role |
|-------|------|
| [`static/js/moments.js`](../static/js/moments.js) | Stack controller: `register` / `push` / `pop`, shared letterbox/HUD/glitch choreography, portrait + notify + choices chrome |
| [`#moment-overlay`](../templates/standalone.html) | Shared markup (letterbox, portrait, nameplate, notify tray, choices) |
| `Renderer.pauseUnderlay()` / `resumeUnderlay()` | Freeze the image/Reactor underlay without tearing down the world-model session |
| Type handlers | Per-Moment `enter` / `exit` / optional `onEsc` |

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
- `Moments.notify({ text, icon? })` — RPG-style toast along the letterbox
- `Moments.setChoices(items, onPick)` / `clearChoices()` — dialogue options list
- `Moments.setNameplate(name, sub)`

## Character memory hook

`/api/talk/end` accepts an optional `subject` and upserts
`state.characters[label]` with `{ first_met_turn, last_talk_turn, talk_count,
notes[], trust }`. This is additive metadata — it does **not** mutate
`history` / `feed_log`. Future trust / relationship Moments can read and
extend this record.

## Portrait animation

Two layers, both shipped:

1. **CSS living portrait (always-on baseline):** breathing scale, film grain,
   and an orb-linked rim light on the still img2img portrait. Used in
   still-image mode, under reduced-motion, or until the world feed goes live.
2. **World-model animation (realtime mode):** the single Reactor session is
   **re-anchored** onto the character render (`applyScene({prompt, imageUrl})`)
   so the character moves/breathes with the world model. Its live MediaStream
   is mirrored into `#moment-portrait-video` via `Moments.setPortraitStream`
   and crossfaded over the still (reveal-when-ready via `isShowing()` polling).
   On exit the session is re-anchored back to the pre-conversation scene, hidden
   by the glitch + letterbox-retract + reactor freeze buffer — so the world is
   always moving and the swap stays invisible. One session, swapped both ways
   (no second GPU session). Opt out with `window.__CONVERSATION_ANIMATE__ = false`.
