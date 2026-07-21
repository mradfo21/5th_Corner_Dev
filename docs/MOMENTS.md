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

## Exit = instant resume

Leaving a conversation resumes the paused world (see above) rather than
generating anything — the player lands back on the exact frame they left with
the world moving again. This is the fast, seamless feel; no "load" on exit.

## Character memory hook

`/api/talk/end` accepts an optional `subject` and upserts
`state.characters[label]` with `{ first_met_turn, last_talk_turn, talk_count,
notes[], trust }`. This is additive metadata — it does **not** mutate
`history` / `feed_log`. Future trust / relationship Moments can read and
extend this record.

## Portrait animation & the pause/resume world

The character is the cinematic img2img still with the **CSS living-portrait**
treatment: a slow breathing scale, film grain, and a rim light that pulses with
the speaking/listening orb state. It's alive without a second GPU session.

Crucially, the world session is **not** re-anchored onto the character. An
earlier version did that to animate the character with the world model, but it
destroyed the environment world and forced a slow rebuild on exit (a jarring
"load"). Instead:

- **Enter:** `Renderer.pauseUnderlay()` (via `Moments.push`) pauses the world
  session and freezes it on the exact frame the player was standing on.
- **Exit:** `Renderer.resumeUnderlay()` (via `Moments.pop`) resumes it — an
  instant, seamless return to where they stood, world moving again. No image
  generation, no re-anchor, no rewind.

Live world-model motion for the *character* would require a genuinely separate
Reactor session (a larger renderer refactor); `Moments.setPortraitStream` is
kept as the hook for that future path.
