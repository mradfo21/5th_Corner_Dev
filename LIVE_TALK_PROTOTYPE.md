# TALK refactor — vision-grounded persona + Gemini Live audio prototype

**Status:**
- **Flavor A (vision-grounded persona)** — shipped, on by default. Every TALK session, ElevenLabs or text, now includes the current on-screen frame as part of the character's persona.
- **Flavor B (Gemini Live native audio replacing ElevenLabs)** — opt-in probe behind `TALK_LIVE_API=1`. Not wired into the default `/api/talk/session` path. Not manually validated (no mic/speaker in the cloud env).

## Why this exists (significance)

Before this PR, the TALK character was **blind**. `build_talk_context` composed the persona from:

- The subject's SCAN `label` + `kind`
- The story premise, phase, chaos, turn, location, time-of-day
- The current scene's *text* description (`world_prompt`)
- The last 4 narrative dispatch beats

None of that told the character what pixels were on screen. If you SCAN a figure holding a lantern and try to talk to them, they know they're a "figure" and know the scene text says "a dim corridor" — they can't see the lantern, can't see your posture, can't see the door behind them. This is a real, visible immersion break.

The refactor's core idea: **give the character eyes.** Two flavors, both landed together so we can move independently on them.

## Flavor A — vision-grounded persona (default-on)

Adds two helpers to `engine.py`:

- `_talk_vision_snapshot(session_id)` — reads the current on-disk scene image (from `state['current_image_url']`) and runs both `_vision_analyze_all` (already cached per image path) and `_detect_objects` (with the PR #89 scene-prompt prior). Returns `{visible, description, time_of_day, image_url}`.
- `_format_vision_for_persona(subject_label, snapshot)` — turns that into a short block for the persona prompt. Deliberately narrates positions in plain English ("off to your left / in front of you / off to your right") instead of dumping coordinates. Filters out any detected object whose label matches the subject, so the character doesn't say "I see a figure over there" when *they* are the figure. Explicitly instructs the model: **use these to react, never invent objects that aren't listed.**

`build_talk_context` now folds that block into `persona_prompt` and adds two new dynamic variables — `visible_now` and `visible_description` — that flow into the ElevenLabs agent's dynamic-variable map alongside the existing ones. An ElevenLabs agent template can reference `{{visible_now}}` and `{{visible_description}}` to stay grounded without any Cursor-side agent config.

**Cost model:** at most one extra vision call per TALK session start. In the common case (TALK opened during a scene the engine already analyzed for the turn dispatch), `_vision_analyze_all` is a cache hit, so the only extra work is the `_detect_objects` call — the same call the SCAN overlay already makes on this frame.

**Failure mode:** vision disabled or no scene image → empty snapshot, no persona block, no dynamic variables, exact same behavior as before. TALK is never broken by a perception failure.

## Flavor B — Gemini Live native audio (opt-in probe)

New module `gemini_live_talk.py`. Opens a Gemini Live session per browser WebSocket with `response_modalities=["AUDIO"]` and the (already-vision-grounded) persona as the system instruction. The browser talks to it directly through `/ws/talk/live`:

```
Browser → server (JSON handshake)
    {"type":"start","subject":{"label":..,"kind":..},"session_id":"default"}
Browser → server (streaming)
    binary  — 16-bit PCM 16 kHz mono mic capture
    {"type":"frame","data":"data:image/jpeg;base64,..."}   (throttled to ≤ 1 FPS server-side)
    {"type":"text","text":"..."}                           (typed messages)
    {"type":"end"}                                         (clean close)
Server → browser (streaming)
    {"type":"start",  ...}         acknowledgement + opening line + vision flag
    binary  — 16-bit PCM 24 kHz mono model audio
    {"type":"transcript","role":"user"|"assistant","text":"..."}
    {"type":"turn_complete"}
    {"type":"end"}                 session closed / rotated
    {"type":"error","message":..}  on any failure
```

`LiveTalkBridge` runs the whole shuttle inside a single asyncio loop (spawned on the WS worker thread, since flask-sock's `ws.receive` is blocking) with three concurrent tasks: producer (browser → Gemini), consumer (Gemini → browser), and a session-rotation deadline. First task to complete cancels the others and closes the session cleanly.

### What Live-audio unlocks that ElevenLabs alone doesn't

1. **Vision INSIDE the conversation.** The character doesn't just have a *snapshot* of the frame taken when TALK opened — they can *watch* the scene through the session (still ≤ 1 FPS, but that's enough to follow major visible changes). "Look, I'm holding it up now" works.
2. **One vendor for the whole TALK experience.** Persona, voice, and vision all live in one Gemini session. No signed-URL exchange, no agent template config, no override matrix. New characters "just work."
3. **Native audio quality.** Gemini's native audio is preview but very good; the pitch shift toward specific characters (a machine's radio warble, a creature's low rasp) is intrinsic to the model, no per-character voice ID needed.

### What Live-audio costs / risks

- **Live sessions are billed for wall-clock**, not per request. `MAX_SESSION_SECONDS = 100` forces rotation before Gemini's ~2-min ceiling; `IDLE_CLOSE_SECONDS = 30` closes on client silence. Still, a lively TALK session costs more per second than an idle ElevenLabs one.
- **Preview model names change.** Default is `gemini-2.5-flash-native-audio-preview-09-2025`; override via `GEMINI_LIVE_TALK_MODEL`. Expect to bump this occasionally.
- **Voice fidelity is different.** ElevenLabs voices are a product asset with 30+ character voices in `voices.json`. Gemini native audio picks a voice per session; live-switching between named voices is a lot rougher than the ElevenLabs `changeVoice()` flow the current UI supports. That's why this ships opt-in, not as a replacement.
- **No manual validation from this environment.** I built the transport, the parsing helpers are unit-tested, the route registration is verified. I could not test a real mic + speaker roundtrip in the cloud env. **Enable it in a browser + real key + real audio hardware to validate.**

### Client-side (not in this PR)

The default TALK UI in `standalone.js` still uses ElevenLabs. Wiring the browser to `/ws/talk/live` needs:

- WebSocket open + JSON handshake
- `AudioWorklet` capturing mic input, converting to 16-bit PCM 16 kHz mono, sending as binary WS frames
- Second `AudioWorklet` decoding incoming 16-bit PCM 24 kHz binary frames into an `AudioBufferSourceNode` chain
- Transcript events → the existing `addLine("assistant"|"user", text)` path in the `Talk` module
- Optional: throttled `<video>`/still frame grabs → `{"type":"frame"}` messages so vision rides the session

That's a self-contained frontend PR that will land after we've validated the transport with a hand-driven test tool. Server side is ready for it.

## How to try it locally

```bash
# Flavor A is on by default — nothing to enable.
python api.py

# Flavor B (opt-in probe):
export TALK_LIVE_API=1
export GEMINI_API_KEY=…    # existing key, no new secret
python api.py
```

You'll see `[LIVE TALK] /ws/talk/live registered (TALK_LIVE_API=1)` in the startup log if the WS route wired up.

Verify server-side wiring with any WS client (e.g. `websocat` or a browser dev console):

```js
const ws = new WebSocket("ws://localhost:5001/ws/talk/live");
ws.onopen = () => ws.send(JSON.stringify({
  type: "start",
  subject: { label: "figure", kind: "person", speaks: true },
  session_id: "default"
}));
ws.onmessage = (e) => console.log(e.data);
```

You should see back a `{"type":"start", subject:..., opening_line:..., voice_source:"gemini-live", vision_available:true|false}` handshake, then either audio bytes + transcript events (with a valid `GEMINI_API_KEY` and mic input) or a clean close.

## When to promote Flavor B to default

Not now. Promote it if:

- The manual audio-roundtrip validation is done and the voice quality passes the same bar `voices.json` sets today.
- The client-side WS + AudioWorklet transport (out of scope here) is written and shipped behind the same flag.
- We're prepared to either replace or duplicate the live voice-picker UX on top of Gemini's session voice model.

Until then Flavor A is the win we can ship: the character has eyes today.
