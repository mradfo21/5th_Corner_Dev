# Gemini Live API for realtime object recognition — prototype notes

**Status:** opt-in experiment behind `DETECT_LIVE_API=1`. Not wired into the default `/api/detect` path, not enabled in production, safe to merge as a probe.

> **Superseded for the latency problem this was chasing.** `/api/detect` now defaults to on-device detection (`local_vision.py`, ~20 ms, no network, no key) — see `LOCAL_OBJECT_DETECTION.md`. This prototype was an attempt to cut the round-trip cost of a *remote* detector; removing the round trip entirely beats shortening it. The notes below stay relevant if the Gemini path is ever re-selected via `DETECT_BACKEND=gemini`, since it remains the only open-vocabulary option.

## What this is

An alternate implementation of the SCAN detection call that keeps a persistent Gemini Live-API WebSocket session open per game session and streams frames into it, instead of doing one-shot HTTP round-trips against `gemini-3.1-flash-lite:generateContent` (the current default in `engine._detect_objects`).

Wire contract with the browser is **identical** — the new endpoint `POST /api/detect/live` returns the same `{objects: [...]}` shape as `/api/detect`, so the client can A/B by swapping the URL and changing nothing else.

## Why we tried it

Item 14 on the image-recognition improvement list: the pitch was "Gemini's Live API would let us watch the Reactor video track continuously and cut request overhead to near zero, matching the 'realtime' branding better."

## What actually shipped in the prototype

- `gemini_live_vision.py` — self-contained module: feature flag, per-session background asyncio worker, bounded frame queue (drops stale frames), lazy session start on first push, idle-close after 45 s, forced session rotation before Gemini's ~2-min ceiling.
- `POST /api/detect/live` in `api.py` — thin proxy that pushes the frame into the per-session worker and returns whatever detections the worker has cached most recently. Registered only when the feature flag + API key + SDK are all present; otherwise the route simply doesn't exist and callers fall through to the default `/api/detect`.
- Response parsing (`_parse_detection_payload`) mirrors `engine._detect_objects` post-processing: 0–1000 box normalization, IoU-free label-key dedupe, `_classify_speaker` reused for `kind`/`speaks` so the TALK affordance behaves identically.

## What we learned that the pitch missed

1. **Live API caps video input at ≤ 1 FPS.** Our current polling cadence is ~2.5 s (SCAN prewarm) / ~2.1 s (photo-target), which is already below 1 FPS. So Live API's upstream throughput isn't actually a win — it's the same information rate, just via a persistent socket instead of fresh HTTPS.

2. **Real wins are elsewhere.** The persistent session buys us:
   - **Cross-frame memory.** Gemini remembers the last few frames, so labels stabilize ("figure" doesn't flip to "person" on identical pixels). The quick-wins PR #89 gets us most of that stability by pinning `temperature: 0`, and it does it without a long-lived socket.
   - **Text streamed alongside the next frame upload.** The next detection can start emitting while the next frame is still being uploaded. For our cadence this saves at most a few hundred ms.

3. **Cost model is different.** The default `/api/detect` is billed per request; a Live session is billed for wall-clock time regardless of how many frames you push. For an idle SCAN overlay this is *worse*, not better — hence `IDLE_CLOSE_SECONDS = 45`.

4. **Session limits are real.** Audio + video sessions time out at ~2 minutes without context compression. Video-only tends to last longer but we don't get a contractual guarantee, so `MAX_SESSION_SECONDS = 100` forces rotation before Gemini can `GoAway` us mid-frame.

5. **`response_schema` on Live-preview models is inconsistent.** The HTTP path in PR #89 uses `responseSchema` for guaranteed structure; on `gemini-live-2.5-flash-preview` this isn't uniformly enforced yet, so this module falls back to a strict system-instruction + defensive parsing (the same code path the pre-schema HTTP call used).

## Where the Live API is actually a big win for this codebase

Detection *alone* doesn't need Live. The unlock is **fusing vision + audio conversation in one session** — which is exactly what the TALK affordance does today via ElevenLabs. A single Live-API session per game session could:

- Watch the Reactor video track continuously (this module's mechanism)
- Detect and label objects (this module's parsing)
- Route the TALK button into the same session so the character being talked to is grounded in the *current visible frame* instead of a stale still + text briefing
- Emit audio replies natively, cutting one round trip and one API vendor

That's a full re-architecture of `engine.api_talk_session` / `engine.api_talk_message` and out of scope for this prototype. But this module is the plumbing that path would need, so shipping it (even disabled) de-risks that future work.

## How to try it locally

```bash
export DETECT_LIVE_API=1
export GEMINI_API_KEY=…   # existing key, no new secret
python api.py             # or bash start_production.sh
```

You'll see `[LIVE VISION] /api/detect/live registered (DETECT_LIVE_API=1)` in the startup log if it wired up. Point the client's SCAN prewarm at `/api/detect/live` instead of `/api/detect` to A/B:

```js
// static/js/standalone.js (local hack for A/B)
postJSON(window.__DETECT_LIVE__ ? "/api/detect/live" : "/api/detect", { frame: cap.frame })
```

To disable, unset `DETECT_LIVE_API` — the route drops out of the URL map on the next restart.

## When to promote this to default

Not now. Promote it if any of these become true:

- Live API's video FPS cap rises above the equivalent of a ~2 s polling cadence (would need to check the docs — as of 2026-07 it's still 1 FPS).
- We ship voice + vision TALK using the same session (this module becomes the shared foundation).
- Cross-frame tag stability regressions appear in production despite `temperature: 0` from PR #89.
