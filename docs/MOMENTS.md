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

## Phased portrait animation

1. **Shipped:** CSS living portrait (breathing scale, grain, orb-linked rim)
2. **Scaffold:** `window.__CONVERSATION_ANIMATE__ = true` (or `?convo_animate=1`)
   logs intent for a second Reactor idle session; `Moments.setPortraitStream`
   is ready for the reveal-when-ready crossfade once that path is wired.
