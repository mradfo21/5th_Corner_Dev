/* ============================================================
   SOMEWHERE // Reactor realtime renderer (LingBot World 2)

   Drives Reactor's LingBot World 2 real-time world model as an alternative
   scene renderer. We steer the live video with a clean, video-model-appropriate
   scene prompt (built server-side by build_realtime_prompt) and condition the
   world model on the SAME still the game generated so the video matches our
   intended composition.

   Wire protocol (per the LingBot World 2 schema reference):
     • establishing shot / new game:
         uploadFile(still) -> set_image({image}) -> [await image_accepted]
           -> set_prompt({prompt}) -> start
       (start requires BOTH a prompt and a reference image; waiting for
        image_accepted means chunk 0 renders from the seed still)
     • NEW guide image (a fresh still — every turn that draws one, or a
       location change / hard_transition):
         reset  -> uploadFile(new still) -> set_image -> set_prompt -> start
       LingBot World 2's reference image is LOCKED once a run starts — per the
       schema, "changes during generation have no effect until reset is issued
       and start is called again." So the ONLY way to force the live video onto
       a new guide image at full strength (strength 1 — chunk 0 renders directly
       from that frame) is a fresh stage. We re-anchor on EVERY new guide image,
       not just location changes, so the video keeps blending from vignette to
       vignette instead of drifting off the still the engine actually drew.
     • same scene, prompt-only re-steer (e.g. instant action injection, no new
       still): set_prompt({prompt})   (hot-swap; lands on the next chunk)

   The Reactor API key never touches the browser: we mint a short-lived JWT via
   our own POST /api/reactor/token proxy. The SDK loads from an ESM CDN (pinned)
   so no build step is required. If anything fails, standalone.js falls back to
   the still image.

   window.ReactorRenderer facade:
       enable() / disable()
       applyScene({prompt, imageUrl, hardTransition})
       setPrompt(prompt, imageUrl)   // thin back-compat wrapper
       reset() / pause() / resume()
       getStatus() -> "off"|"connecting"|"live"|"error"
       isActive() / isReady()
   Set window.ReactorRenderer.onStatus = fn to observe status changes.
   Set window.ReactorRenderer.onEvent  = fn(name, data) to observe the model's
       lifecycle events (prompt_accepted, image_accepted, generation_started,
       chunk_complete, generation_reset, command_error, …) — the standalone UI
       uses these to surface the realtime pipeline in the gamified ceremony.
   ============================================================ */
(function () {
  "use strict";

  const SDK_URL = "https://esm.sh/@reactor-team/js-sdk@2.12.0";
  // World models are DATA, not hard-wired code paths. Each model maps to a
  // DRIVER *family* (its wire protocol) — not a per-id driver — so ANY Reactor
  // model, including one that ships tomorrow, works with no code change:
  //   • "seed_locked" (LingBot): reference image locked once a run starts, so a
  //     new guide image forces a fresh stage (reset + re-establish).
  //   • "blend" (Helios, and the DEFAULT for anything new/unknown): text/image
  //     to video; a new guide image blends in-stream with no reset, prompts
  //     re-steer live.
  // The model list + protocols come from /api/reactor/config (which itself is
  // env-driven and open to custom models); these are just pre-config defaults.
  const FALLBACK_MODEL_ID = "lingbot-world-2";
  const FALLBACK_MODEL = "reactor/lingbot-world-2";
  const FALLBACK_FAMILY = "blend"; // the flexible family unknown models default to
  const SDK_PREFIX = "reactor/";   // how a bare model id becomes an SDK name
  const DEFAULT_MODELS = [
    { id: "lingbot-world-2", label: "LingBot World 2", sdk_name: "reactor/lingbot-world-2", requiresSeedImage: true, protocol: "seed_locked" },
    { id: "helios", label: "Helios", sdk_name: "reactor/helios", requiresSeedImage: false, protocol: "blend" },
    { id: "lingbot", label: "LingBot", sdk_name: "reactor/lingbot", requiresSeedImage: true, protocol: "seed_locked" },
    { id: "longlive-v2", label: "LongLive V2", sdk_name: "reactor/longlive-v2", requiresSeedImage: false, protocol: "blend" },
    { id: "sana-streaming", label: "Sana Streaming", sdk_name: "reactor/sana-streaming", requiresSeedImage: false, protocol: "blend" },
  ];
  // How long to wait for the seed image to decode before starting anyway.
  const IMAGE_ACCEPT_TIMEOUT_MS = 6000;
  // After we issue `start`, how long to wait for real decoded video frames
  // before declaring the stream stalled. If this fires, the model accepted our
  // commands but no `main_video` frames arrived — a server/session/model issue,
  // NOT a client bug. The still fallback stays on screen; we log + emit a
  // diagnostic so the exact stall point is visible in the browser console.
  // Overridable (e.g. from tests) via a window global.
  const REVEAL_WATCHDOG_MS = (typeof window !== "undefined" && window.__REACTOR_REVEAL_WATCHDOG_MS__) || 12000;
  // Helios image-to-video conditioning strength (0..1). Helios is prompt-primary
  // by design, so we push this high to make it hew to our guide still as closely
  // as possible. Overridable at runtime via window.__HELIOS_IMAGE_STRENGTH__.
  const HELIOS_IMAGE_STRENGTH = (typeof window !== "undefined" && window.__HELIOS_IMAGE_STRENGTH__) || 0.9;
  // Scene fade-down beat (matches the still renderer's crossfade-to-dark feel):
  //   • MIN_HOLD — guarantee the scene stays dark at least this long before the
  //     new stream is revealed, so a fast re-anchor still reads as a deliberate
  //     "moment of pause" and never a black flicker.
  //   • SAFETY — never leave the scene stuck dark if the reveal never fires
  //     (stalled stream / lost frames); lift the veil regardless after this.
  //   • BLEND_REVEAL — blend-family models (Helios) stream continuously with no
  //     discrete "new frame" boundary, so hold the veil this long after the new
  //     guide image is sent, then reveal the blended-in scene.
  const SCENE_FADE_MIN_HOLD_MS = 650;
  const SCENE_FADE_SAFETY_MS = 9000;
  const SCENE_FADE_BLEND_REVEAL_MS = 1200;
  // Absolute maximum a fade veil may stay down before we give up and lift it,
  // regardless of what the stream is doing. This is the "video generation
  // legitimately died" ceiling — reasonable turns finish well under this. The
  // per-fade `safetyMs` controls the FIRST sample; while a fresh stream is
  // ARMED and still trying to present frames, we resample instead of lifting.
  const SCENE_FADE_HARD_CAP_MS = 90000;
  const SCENE_FADE_RESAMPLE_MS = 1500;

  // Blackout detection + recovery. Some world models, when steered toward
  // content their OWN safety refuses (a graphic scene), don't error — they
  // "give up" and stream SOLID BLACK frames. Those are still real frames
  // (videoWidth>0), so the reveal watchdog — which only catches the ABSENCE of
  // frames — never fires, and the scene is stuck black forever with no recovery
  // (the reported "reactor/lingbot freaks out, refusing to draw anything but
  // black"). We sample presented frames and, if they stay essentially black for
  // a sustained beat after the stream revealed, treat it as a soft failure: hide
  // the black video so the still floor painted beneath shows through, and
  // self-heal the instant real (non-black) frames return.
  const BLACK_SAMPLE_INTERVAL_MS = 700;   // how often to sample the live frame
  const BLACK_MEAN_LUMA_MAX = 10;         // 0..255 mean luma below this = "black"
  const BLACK_PEAK_LUMA_MAX = 24;         // brightest sampled pixel below = "black"
  const BLACKOUT_AFTER_MS = 2400;         // continuous black this long => blackout

  const rstate = {
    reactor: null,
    active: false,
    ready: false,
    started: false,
    paused: false,
    pending: null,       // latest scene awaiting apply: {prompt,imageUrl,hardTransition}
    lastPrompt: null,
    lastImageUrl: null,  // avoid re-uploading the same still
    lastRef: null,
    stagingGuideUrl: null, // still URL currently being staged (awaiting image_accepted)
    guideImageUrl: null,   // last guide image actually integrated into the world
    supportsUpload: true,
    video: null,
    modelId: null,                 // active world-model id (resolved in enable())
    models: DEFAULT_MODELS.slice(), // available world models (from /api/reactor/config)
    allowCustom: true,             // may we connect to an unadvertised model id? (from config)
    sdkPrefix: SDK_PREFIX,         // how a bare id becomes an SDK name (from config)
    caps: null,                    // model Capabilities (tracks/commands) from the SDK
    commandSet: null,              // Set of command names the model advertises (or null = unknown)
    currentChunk: 0,               // last current_chunk seen in a state message (for look-ahead)
    videoTrackName: null,          // the model's output video track name (from caps; default main_video)
    videoAttached: false,          // have we attached a video output track this session?
    lastSceneApplied: null,        // last scene handed to applyScene, for model-swap re-apply
    swapping: false,               // a live model swap is in flight
    // Live camera-drive state (LingBot World 2 native movement axes). These are
    // PERSISTENT: the model holds each value across chunks until we send a new
    // one, so we track the desired value here, push it on change, and re-assert
    // it after a (re)start (a re-anchor issues reset, which clears the model's
    // copy). See setAxis() / applyMoveState().
    move: { longitudinal: "idle", lateral: "idle", lookH: "idle", lookV: "idle", rotationDeg: null },
    cfg: { model_name: FALLBACK_MODEL, enabled: false },
    connecting: false,
    status: "off",
    frameWatch: false,
    frameWatchTimer: null,
    applying: false,       // flush() re-entrancy guard (establishing is async)
    revealWatchdog: null,  // timer: warns if start never yields video frames
    // Freeze back-buffer (single-session double buffer): a canvas that covers
    // the video with the seed image / last live frame during warmup + re-anchor
    // so the switch never exposes black or the underlying still.
    freeze: null,
    freezeActive: false,   // freeze canvas is currently covering the video
    freezeArmed: false,    // waiting for the first NEW frame to fade it out
    freezeArmTs: 0,
    freezeFallbackTimer: null,
    // Scene fade veil (the "fade down → pause → reveal" beat between scenes,
    // mirroring the still renderer). Distinct from the freeze back-buffer: the
    // freeze HOLDS a frame to avoid a gap; the fade deliberately goes dark.
    fade: null,
    fadeDownActive: false, // the fade veil is currently down (scene darkened)
    fadeDownTs: 0,
    fadeUpTimer: null,     // pending reveal (honoring the minimum dark hold)
    fadeSafetyTimer: null, // failsafe: lift the veil even if no reveal fires
    fadeHardDeadline: 0,   // absolute ceiling for holding the veil (ms since epoch) — a re-arm CANNOT shorten it, so a pre-fade's long window survives the re-anchor's default one
    awaitingReanchor: false, // caller promised a re-anchor is coming; hold the veil (up to fadeHardDeadline) until the apply actually happens even if nothing is currently in-flight
    seedToken: 0,          // bumps every reveal/reset so a slow seed decode that
                           // lands AFTER the stream revealed can't re-cover it
    // Blackout state: the live stream is rendering solid black (model refused
    // the scene). We hide the black video so the still floor shows, and recover
    // the moment real frames return.
    blackout: false,
    blackSince: 0,         // ts the current run of black frames began (0 = none)
    lastBlackSampleTs: 0,  // throttle for the luma sample
    blackCanvas: null,     // tiny offscreen canvas reused for luma sampling
  };

  // Event waiters keyed by model message type, so command flows can await a
  // specific model reply (e.g. image_accepted) with a timeout fallback.
  const waiters = Object.create(null);

  function log() { try { console.log.apply(console, ["[reactor]", ...arguments]); } catch (_) {} }

  function setStatus(s) {
    if (rstate.status === s) return;
    rstate.status = s;
    try {
      if (typeof window.ReactorRenderer.onStatus === "function") window.ReactorRenderer.onStatus(s);
    } catch (_) {}
  }

  // Surface a model lifecycle event to any observer (the ceremony UI).
  function emitEvent(name, data) {
    try {
      if (typeof window.ReactorRenderer.onEvent === "function") window.ReactorRenderer.onEvent(name, data || {});
    } catch (_) {}
  }

  // Fire when a guide image is actually integrated into the world model (the
  // model has accepted + decoded the seed still and the next chunk will render
  // from it). The UI uses this to notify the player and show a thumbnail.
  function emitGuideImage(imageUrl, data) {
    try {
      if (typeof window.ReactorRenderer.onGuideImage === "function") window.ReactorRenderer.onGuideImage(imageUrl, data || {});
    } catch (_) {}
  }

  function waitForEvent(name, timeoutMs) {
    return new Promise((resolve) => {
      const entry = { resolve };
      (waiters[name] = waiters[name] || []).push(entry);
      entry.timer = setTimeout(() => {
        const arr = waiters[name] || [];
        const i = arr.indexOf(entry);
        if (i >= 0) arr.splice(i, 1);
        resolve(null); // timed out — let the caller proceed regardless
      }, timeoutMs || IMAGE_ACCEPT_TIMEOUT_MS);
    });
  }

  function resolveWaiters(name, data) {
    const arr = waiters[name];
    if (!arr || !arr.length) return;
    waiters[name] = [];
    arr.forEach((e) => { try { clearTimeout(e.timer); } catch (_) {} e.resolve(data); });
  }

  function getVideo() {
    if (!rstate.video) rstate.video = document.getElementById("reactor-video");
    return rstate.video;
  }

  function getFreeze() {
    if (!rstate.freeze) rstate.freeze = document.getElementById("reactor-freeze");
    return rstate.freeze;
  }

  // Show the freeze buffer (already painted) — covering the video. `instant`
  // snaps it on with no fade (used when grabbing the last frame as we tear the
  // old stream down, so there's no flicker before it covers).
  function showFreeze(instant) {
    const f = getFreeze();
    if (!f) return;
    rstate.freezeActive = true;
    if (instant) { f.classList.add("instant"); void f.offsetWidth; }
    f.classList.add("show");
    if (instant) { void f.offsetWidth; f.classList.remove("instant"); }
  }

  // Crossfade the freeze buffer out, revealing the live video underneath.
  function hideFreeze() {
    const f = getFreeze();
    if (!f) return;
    rstate.freezeActive = false;
    f.classList.remove("show");
  }

  function getFadeEl() {
    if (!rstate.fade) rstate.fade = document.getElementById("reactor-fade");
    return rstate.fade;
  }

  // Fade the whole scene down to near-black — the deliberate "moment of pause"
  // beat at the start of a re-anchor (a new guide image / hard transition), so
  // advancing time in video mode reads as a clean fade-down → pause → reveal
  // instead of a hot cut over the live stream. Mirrors the still renderer,
  // where the outgoing scene crossfades to its dark background before the next
  // one loads. endSceneFade() lifts it once the fresh scene is on screen.
  //
  // `opts.safetyMs` overrides the FIRST failsafe check — used by callers who
  // kick the fade off BEFORE the re-anchor arrives (e.g. a MOVE TO pre-fade so
  // the live world stops drifting the instant the player commits to a trip).
  // The fresh scene's own applyRunning* call later invokes beginSceneFade()
  // again with the default window, so this only extends the pre-fade grace
  // period for as long as we're waiting for the guide image to land.
  //
  // The safety check is video-aware: while a fresh stream is ARMED (established
  // but no new frames presented yet), we RESAMPLE instead of lifting — "wait
  // till everything is ready" for a video-to-video transition, so we never
  // reveal the freeze back-buffer's static still while the new video is still
  // warming up. Only give up (and lift) if the resample keeps failing past the
  // SCENE_FADE_HARD_CAP_MS ceiling, or the stream was never started at all.
  function beginSceneFade(opts) {
    const f = getFadeEl();
    if (!f) return;
    const safetyMs = (opts && typeof opts.safetyMs === "number" && opts.safetyMs > 0)
      ? opts.safetyMs
      : SCENE_FADE_SAFETY_MS;
    // `awaitReanchor` — the caller (typically the standalone layer on MOVE TO)
    // guarantees a re-anchor is coming from the server; hold the veil until
    // that apply actually kicks off, so the pre-fade doesn't lift into the OLD
    // drifting video during the "gap" before /api/choose comes back with a
    // new scene_image. Sticky through re-arms (only cleared on genuine reveal
    // / teardown / cap).
    if (opts && opts.awaitReanchor) rstate.awaitingReanchor = true;
    rstate.fadeDownActive = true;
    rstate.fadeDownTs = Date.now();
    // The absolute deadline never moves once set for a fade — a subsequent
    // beginSceneFade() call (e.g. the re-anchor re-arming) MUST NOT reset the
    // ceiling back down, or a pre-fade with a long safety would collapse to
    // the default 9 s the instant the guide image arrives.
    rstate.fadeHardDeadline = Math.max(
      rstate.fadeHardDeadline || 0,
      rstate.fadeDownTs + Math.max(safetyMs, SCENE_FADE_HARD_CAP_MS)
    );
    if (rstate.fadeUpTimer) { clearTimeout(rstate.fadeUpTimer); rstate.fadeUpTimer = null; }
    f.classList.add("down");
    scheduleFadeSafetyTick(safetyMs);
  }

  // The video-aware failsafe: sample the stream state after `ms`, and either
  // give up (lift the veil) or resample. Only lifts when the fade has genuinely
  // hit its ceiling OR there's no live stream we could be waiting on. We hold
  // the veil while ANY of these is true:
  //   • a driver is mid-apply (reset → establish → start is running; covers the
  //     brief window between the re-anchor's beginSceneFade() and armFreezeReveal
  //     where freezeArmed would otherwise be a false negative)
  //   • the freeze reveal is armed (stream is started but its FIRST new frame
  //     hasn't been presented yet — the "video to video" wait)
  //   • a scene is queued for apply (rstate.pending)
  //   • a blend-model reveal is scheduled (fadeUpTimer will lift on its own)
  function scheduleFadeSafetyTick(ms) {
    if (rstate.fadeSafetyTimer) clearTimeout(rstate.fadeSafetyTimer);
    rstate.fadeSafetyTimer = setTimeout(() => {
      rstate.fadeSafetyTimer = null;
      if (!rstate.fadeDownActive) return;
      const now = Date.now();
      const overCap = rstate.fadeHardDeadline && now >= rstate.fadeHardDeadline;
      const waitingForVideo = !!(
        rstate.applying ||
        rstate.pending ||
        rstate.fadeUpTimer ||
        rstate.awaitingReanchor ||
        (rstate.started && rstate.freezeArmed)
      );
      if (waitingForVideo && !overCap) {
        scheduleFadeSafetyTick(SCENE_FADE_RESAMPLE_MS);
        return;
      }
      endSceneFade();
    }, Math.max(0, ms));
  }

  // Lift the fade veil to reveal the freshly generated scene, then run the
  // optional `onLifted` callback (used to emit video_showing) — but ONLY once
  // the veil is genuinely gone, never while it's still opaque. Honors a minimum
  // dark hold so a fast re-anchor still shows the pause (never a black blink):
  // if we haven't been dark long enough, defer BOTH the lift and the callback
  // to the remainder. If no veil is down, the callback runs immediately.
  function endSceneFade(onLifted) {
    if (!rstate.fadeDownActive) { if (onLifted) onLifted(); return; }
    const f = getFadeEl();
    if (!f) { rstate.fadeDownActive = false; if (onLifted) onLifted(); return; }
    const elapsed = Date.now() - rstate.fadeDownTs;
    if (elapsed < SCENE_FADE_MIN_HOLD_MS) {
      // A lift is already scheduled — let it carry its own callback (this dedupes
      // racing reveal triggers so video_showing fires once per transition).
      if (rstate.fadeUpTimer) return;
      rstate.fadeUpTimer = setTimeout(() => {
        rstate.fadeUpTimer = null;
        endSceneFade(onLifted);
      }, SCENE_FADE_MIN_HOLD_MS - elapsed);
      return;
    }
    rstate.fadeDownActive = false;
    rstate.fadeHardDeadline = 0;
    rstate.awaitingReanchor = false;
    if (rstate.fadeSafetyTimer) { clearTimeout(rstate.fadeSafetyTimer); rstate.fadeSafetyTimer = null; }
    if (rstate.fadeUpTimer) { clearTimeout(rstate.fadeUpTimer); rstate.fadeUpTimer = null; }
    f.classList.remove("down");
    if (onLifted) onLifted();
  }

  // Schedule the reveal after `ms` — used by blend-family models (Helios) that
  // stream continuously and have no discrete "new frame" boundary to reveal on.
  function scheduleSceneReveal(ms) {
    if (!rstate.fadeDownActive) return;
    // Handoff: the scheduled reveal is the blend-family equivalent of an armed
    // freeze reveal — the pre-fade's awaitReanchor promise is fulfilled here.
    rstate.awaitingReanchor = false;
    if (rstate.fadeUpTimer) clearTimeout(rstate.fadeUpTimer);
    rstate.fadeUpTimer = setTimeout(() => {
      rstate.fadeUpTimer = null;
      // A freeze reveal may have already lifted the veil + announced the scene;
      // don't fire a second video_showing for the same transition.
      if (!rstate.fadeDownActive) return;
      // Blend models don't fire the freeze reveal, so announce the new scene
      // here (glitch mask + ceremony progression + autoplay), matching how the
      // seed-locked path reveals via video_showing.
      endSceneFade(() => emitEvent("video_showing", {}));
    }, ms);
  }

  // Drop the fade veil immediately (no min-hold, no reveal event) — used on
  // teardown / reset / disable so a mid-transition fade never sticks on screen.
  function clearSceneFade() {
    rstate.fadeDownActive = false;
    rstate.fadeHardDeadline = 0;
    rstate.awaitingReanchor = false;
    if (rstate.fadeUpTimer) { clearTimeout(rstate.fadeUpTimer); rstate.fadeUpTimer = null; }
    if (rstate.fadeSafetyTimer) { clearTimeout(rstate.fadeSafetyTimer); rstate.fadeSafetyTimer = null; }
    const f = getFadeEl();
    if (f) f.classList.remove("down");
  }

  // Snapshot the current live video frame onto the freeze canvas and show it
  // instantly, so we can re-stage the stream beneath without a black gap.
  // Returns true if a frame was captured.
  function captureVideoToFreeze() {
    const v = getVideo(), f = getFreeze();
    if (!v || !f || !v.videoWidth) return false;
    try {
      f.width = v.videoWidth;
      f.height = v.videoHeight;
      f.getContext("2d").drawImage(v, 0, 0, f.width, f.height);
      showFreeze(true);
      return true;
    } catch (e) { log("freeze capture failed", e); return false; }
  }

  // Paint the seed guide image onto the freeze canvas (async image load) so the
  // very first scene shows the intended composition instead of a black video
  // while the stream warms up. The image is EASED in (soft fade) rather than
  // snapped on: the seed is a deliberate "here's the destination" beat, so a
  // hard pop before the (later) video reveal reads as a glitch.
  function paintSeedToFreeze(imageUrl) {
    const f = getFreeze();
    if (!f || !imageUrl) return;
    // Capture the staging token now. If a reveal fires (or a new stage begins)
    // before this image decodes, the token advances and we drop this paint —
    // otherwise a slow decode could slam the seed back over the live video that
    // already revealed underneath.
    const token = rstate.seedToken;
    const img = new Image();
    img.onload = () => {
      if (token !== rstate.seedToken) return; // stale — the stream already moved on
      try {
        f.width = img.naturalWidth || 1280;
        f.height = img.naturalHeight || 720;
        f.getContext("2d").drawImage(img, 0, 0, f.width, f.height);
        showFreeze(false); // fade in — no instant snap
      } catch (e) { log("seed paint failed", e); }
    };
    img.onerror = () => {};
    img.src = imageUrl;
  }

  // Arm the freeze reveal: once a genuinely NEW video frame is presented after
  // `start`, crossfade the freeze out. A frozen stream presents no new frames,
  // so requestVideoFrameCallback stays silent until real frames flow — that's
  // our precise "the new scene is actually on screen now" trigger.
  function armFreezeReveal() {
    // A fresh stage is establishing — drop any prior black run so the new
    // stream's frames are judged on their own (and un-hide happens on reveal).
    clearBlackout();
    // Handoff: from here on, the "hold the fade" duty belongs to the freeze
    // reveal (freezeArmed). The pre-fade's awaitReanchor promise is fulfilled.
    rstate.awaitingReanchor = false;
    rstate.freezeArmed = true;
    rstate.freezeArmTs = Date.now();
    if (rstate.freezeFallbackTimer) clearTimeout(rstate.freezeFallbackTimer);
    // Fallback for browsers without requestVideoFrameCallback: reveal after a
    // short grace period. (Modern Safari/Chrome use the frame callback path.)
    if (getVideo() && typeof getVideo().requestVideoFrameCallback !== "function") {
      rstate.freezeFallbackTimer = setTimeout(() => {
        if (rstate.freezeArmed) {
          rstate.freezeArmed = false;
          hideFreeze();
          endSceneFade(() => emitEvent("video_showing", {})); // lift the veil too
        }
      }, 1800);
    }
  }

  async function loadConfig() {
    try {
      const r = await fetch("/api/reactor/config");
      if (r.ok) {
        rstate.cfg = await r.json();
        if (typeof rstate.cfg.allow_custom_models === "boolean") rstate.allowCustom = rstate.cfg.allow_custom_models;
        if (rstate.cfg.sdk_name_prefix) rstate.sdkPrefix = rstate.cfg.sdk_name_prefix;
        if (Array.isArray(rstate.cfg.available_models) && rstate.cfg.available_models.length) {
          rstate.models = rstate.cfg.available_models.map((m) => ({
            id: m.id,
            label: m.label || m.id,
            sdk_name: m.sdk_name || m.model_name || m.id,
            requiresSeedImage: !!m.requires_seed_image,
            // Protocol picks the driver family; default the flexible "blend"
            // family so a model advertised without one still works.
            protocol: m.protocol || (m.requires_seed_image ? "seed_locked" : FALLBACK_FAMILY),
            custom: !!m.custom,
          }));
        }
      }
    } catch (err) { log("config fetch failed, using defaults", err); }
    return rstate.cfg;
  }

  // ── World-model helpers ─────────────────────────────────────────────────────
  function modelById(id) { return (rstate.models || []).find((m) => m.id === id) || null; }
  function knownModel(id) { return !!modelById(id); }
  function modelLabel(id) { const m = modelById(id); return m ? m.label : id; }
  function sdkNameFor(id) {
    if (!id) return rstate.cfg.model_name || FALLBACK_MODEL;
    // A caller can pass a fully-qualified SDK name directly (contains "/").
    return id.indexOf("/") >= 0 ? id : (rstate.sdkPrefix || SDK_PREFIX) + id;
  }
  function modelNameFor(id) {
    const m = modelById(id);
    return (m && m.sdk_name) || (id ? sdkNameFor(id) : (rstate.cfg.model_name || FALLBACK_MODEL));
  }
  function familyFor(id) {
    const m = modelById(id);
    return (m && m.protocol) || FALLBACK_FAMILY;
  }
  // May we use this model id? Anything advertised, plus any id at all when the
  // server allows custom models — so a brand-new model is usable immediately.
  function canUseModel(id) { return !!id && (knownModel(id) || rstate.allowCustom); }
  // Register a not-yet-advertised model on the fly (used for ?model= / typed-in
  // custom ids) so the rest of the pipeline treats it like any other model.
  function ensureModel(id, label, opts) {
    if (!id) return null;
    let m = modelById(id);
    if (m) return m;
    opts = opts || {};
    m = {
      id: id,
      label: label || id,
      sdk_name: opts.sdk_name || sdkNameFor(id),
      requiresSeedImage: !!opts.requiresSeedImage,
      protocol: opts.protocol || FALLBACK_FAMILY,
      custom: true,
    };
    rstate.models = (rstate.models || []).concat([m]);
    if (typeof window.ReactorRenderer === "object" && typeof window.ReactorRenderer.onModelsChanged === "function") {
      try { window.ReactorRenderer.onModelsChanged(); } catch (_) {}
    }
    return m;
  }
  // Resolve the active world model: ?model= > localStorage > server default >
  // fallback. Custom ids (not advertised) are accepted + registered when the
  // server permits, so ?model=<anything-new> just works.
  function resolveModelId() {
    if (rstate.modelId && canUseModel(rstate.modelId)) return rstate.modelId;
    let q = null, stored = null;
    try { q = new URLSearchParams(location.search).get("model"); } catch (_) {}
    try { stored = localStorage.getItem("world_model"); } catch (_) {}
    const pick = (id) => {
      if (!id) return null;
      if (knownModel(id)) return id;
      if (rstate.allowCustom) { ensureModel(id); return id; }
      return null;
    };
    rstate.modelId = pick(q) || pick(stored) || pick(rstate.cfg.world_model) || FALLBACK_MODEL_ID;
    return rstate.modelId;
  }

  async function fetchToken() {
    const r = await fetch("/api/reactor/token", { method: "POST" });
    if (!r.ok) {
      let detail = "";
      try { detail = (await r.json()).error || ""; } catch (_) {}
      throw new Error(`token exchange failed (HTTP ${r.status}) ${detail}`);
    }
    const data = await r.json();
    if (!data || !data.jwt) throw new Error("token response missing jwt");
    return data.jwt;
  }

  function attachTrack(name, track, stream) {
    const kind = track && track.kind;
    emitEvent("track_received", { name: name, kind: kind });
    // Never route an audio track into the <video> scene layer.
    if (kind === "audio") { log("skip audio track:", name); return; }
    // Attach the model's declared output video track. Not every model uses
    // "main_video", so: accept the expected track name, OR — if we haven't
    // attached any video yet — accept the FIRST video track we receive. This
    // stops a differently-named output track from reading as a stall (commands
    // accepted + state flowing, but nothing ever shown).
    const expected = rstate.videoTrackName || "main_video";
    if (name !== expected && rstate.videoAttached) { log("ignoring extra track:", name, kind); return; }
    const video = getVideo();
    if (!video) return;
    video.srcObject = stream || new MediaStream([track]);
    rstate.videoAttached = true;
    rstate.videoTrackName = name; // remember what actually carried the video
    // The video is visible immediately; the freeze back-buffer (the seed still)
    // sits ABOVE it and covers warmup, so there's no black gap, while keeping
    // the <video> visible lets iOS Safari actually decode/play the WebRTC track
    // (a video held at opacity:0 can be refused decode on iOS, which stalled the
    // stream). The freeze fades out once real frames arrive.
    video.classList.remove("hidden");
    video.play().catch(() => {});
    startFrameWatch(video);
    log("video track attached:", name, "(freeze covers until first frame)");
  }

  // Sample the current live frame's brightness (downscaled) — true when the
  // frame is essentially solid black. Cheap: a 32x18 draw + one getImageData.
  // Returns false on any failure (decode not ready / tainted canvas) so we never
  // false-trip a blackout.
  function frameIsBlack(video) {
    try {
      let c = rstate.blackCanvas;
      if (!c) { c = rstate.blackCanvas = document.createElement("canvas"); c.width = 32; c.height = 18; }
      const ctx = c.getContext("2d", { willReadFrequently: true });
      if (!ctx) return false;
      ctx.drawImage(video, 0, 0, c.width, c.height);
      const data = ctx.getImageData(0, 0, c.width, c.height).data;
      let sum = 0, peak = 0;
      for (let i = 0; i < data.length; i += 4) {
        // Rec.601 luma approximation.
        const y = (data[i] * 299 + data[i + 1] * 587 + data[i + 2] * 114) / 1000;
        sum += y;
        if (y > peak) peak = y;
      }
      const mean = sum / (data.length / 4);
      return mean <= BLACK_MEAN_LUMA_MAX && peak <= BLACK_PEAK_LUMA_MAX;
    } catch (_) { return false; }
  }

  // Throttled blackout monitor, driven off presented frames. Only meaningful
  // once the LIVE stream is what's actually on screen (freeze not covering, not
  // mid fade-down, run started).
  function monitorBlackout(video) {
    if (rstate.freezeActive || rstate.fadeDownActive || !rstate.started) return;
    const now = Date.now();
    if (now - rstate.lastBlackSampleTs < BLACK_SAMPLE_INTERVAL_MS) return;
    rstate.lastBlackSampleTs = now;
    if (frameIsBlack(video)) {
      if (!rstate.blackSince) rstate.blackSince = now;
      if (!rstate.blackout && now - rstate.blackSince >= BLACKOUT_AFTER_MS) enterBlackout();
    } else {
      rstate.blackSince = 0;
      if (rstate.blackout) exitBlackout();
    }
  }

  function enterBlackout() {
    rstate.blackout = true;
    // Reveal the still floor beneath by hiding the black video. Frames keep
    // decoding at opacity:0, so we still sample and can self-heal.
    const v = getVideo();
    if (v) v.classList.add("hidden");
    log("WARNING: live stream is rendering solid black (model refused the scene?) —",
        "hiding the video so the still fallback shows; will restore on recovery.");
    emitEvent("video_black", {});
  }

  function exitBlackout() {
    rstate.blackout = false;
    const v = getVideo();
    if (v && rstate.started) v.classList.remove("hidden");
    log("live stream recovered from black — restoring video");
    emitEvent("video_recovered", {});
  }

  // Reset blackout tracking (new stage / teardown / reset). Does NOT un-hide the
  // video — callers manage visibility for their own transition.
  function clearBlackout() {
    rstate.blackout = false;
    rstate.blackSince = 0;
    rstate.lastBlackSampleTs = 0;
  }

  // Called on every presented video frame. Marks the stream live, and — when a
  // reveal is armed — treats the frame as the new scene actually being on
  // screen, so it crossfades the freeze buffer out.
  function onPresentedFrame(video) {
    if (video.videoWidth <= 0) return;
    clearRevealWatchdog(); // frames are flowing — the stream is healthy
    // A full reset hides the video for a clean wipe; genuine frames un-hide it
    // (a re-anchor keeps it visible under the freeze, so this is a no-op there).
    // During a blackout we deliberately KEEP it hidden (the still floor is
    // showing) — exitBlackout restores it once real frames return.
    if (!rstate.blackout && video.classList.contains("hidden")) video.classList.remove("hidden");
    if (rstate.status !== "live") setStatus("live");
    if (rstate.freezeArmed) {
      // Ignore stray callbacks in the first beat after arming so we don't reveal
      // the (still-old) frame before the new scene has actually rendered.
      if (Date.now() - rstate.freezeArmTs < 200) return;
      rstate.freezeArmed = false;
      rstate.seedToken++; // invalidate any still-decoding seed so it can't re-cover us
      if (rstate.freezeFallbackTimer) { clearTimeout(rstate.freezeFallbackTimer); rstate.freezeFallbackTimer = null; }
      hideFreeze();
      // Lift the fade-down veil and announce the fresh scene ONLY once it's
      // actually visible (endSceneFade honors the minimum dark hold and fires
      // this immediately when no veil is down).
      endSceneFade(() => emitEvent("video_showing", {}));
    }
    // Watch for the model streaming solid black (its own safety refusing the
    // scene) so we can fall back to the still floor instead of holding black.
    monitorBlackout(video);
  }

  // After `start`, warn (once) if no decoded video frames show up in time. This
  // is the diagnostic that turns a silent "realtime never starts" into a clear,
  // reportable signal: commands were accepted but the model produced no video.
  function armRevealWatchdog() {
    clearRevealWatchdog();
    rstate.revealWatchdog = setTimeout(() => {
      rstate.revealWatchdog = null;
      const v = getVideo();
      const showing = !!(v && v.videoWidth > 0 && !rstate.freezeActive);
      if (showing) return;
      log(
        "WARNING: `start` was issued but no video frames arrived after",
        REVEAL_WATCHDOG_MS + "ms.",
        "Realtime is stalled (model/session produced no main_video) — the still",
        "fallback stays on screen. Check for command_error events / the Reactor",
        "session, this is not a client rendering bug."
      );
      // Purely diagnostic: the still fallback is already on screen and status
      // stays "connecting" (we never reached "live"), so realtime can still
      // recover on late frames or the next guide image — we don't permanently
      // disable it on a single stall.
      emitEvent("video_stalled", { afterMs: REVEAL_WATCHDOG_MS });
    }, REVEAL_WATCHDOG_MS);
  }

  function clearRevealWatchdog() {
    if (rstate.revealWatchdog) { clearTimeout(rstate.revealWatchdog); rstate.revealWatchdog = null; }
  }

  function startFrameWatch(video) {
    if (rstate.frameWatch) return;
    rstate.frameWatch = true;
    if (typeof video.requestVideoFrameCallback === "function") {
      const cb = () => {
        if (!rstate.reactor) { rstate.frameWatch = false; return; }
        onPresentedFrame(video);
        try { video.requestVideoFrameCallback(cb); } catch (_) { rstate.frameWatch = false; }
      };
      try { video.requestVideoFrameCallback(cb); } catch (_) { rstate.frameWatch = false; }
    } else {
      const onp = () => onPresentedFrame(video);
      video.addEventListener("playing", onp);
      video.addEventListener("timeupdate", onp);
      rstate.frameWatchTimer = setInterval(() => {
        if (!rstate.reactor) { clearInterval(rstate.frameWatchTimer); rstate.frameWatch = false; return; }
        onPresentedFrame(video);
      }, 400);
    }
  }

  // Fetch our own generated still and upload it to Reactor, returning a FileRef
  // (or null on failure). Uploads only work while connection status is ready.
  async function uploadStill(imageUrl) {
    if (!rstate.supportsUpload || !imageUrl || !rstate.reactor) return null;
    if (imageUrl === rstate.lastImageUrl && rstate.lastRef) return rstate.lastRef;
    if (typeof rstate.reactor.uploadFile !== "function") { rstate.supportsUpload = false; return null; }
    try {
      const resp = await fetch(imageUrl);
      if (!resp.ok) throw new Error("still fetch HTTP " + resp.status);
      const blob = await resp.blob();
      let fileArg = blob;
      try { fileArg = new File([blob], "scene.png", { type: blob.type || "image/png" }); } catch (_) {}
      const ref = await rstate.reactor.uploadFile(fileArg);
      rstate.lastImageUrl = imageUrl;
      rstate.lastRef = ref;
      return ref;
    } catch (err) {
      log("still upload failed (continuing text-only)", err);
      return null;
    }
  }

  // ── Capability awareness ────────────────────────────────────────────────────
  // Reactor's SDK advertises each model's command schema (Capabilities.commands)
  // at connect time. We read it so the generic driver adapts to WHATEVER a model
  // supports — a brand-new model with a slightly different command set still
  // works, instead of us blindly sending commands it rejects.
  function knownCaps() { return rstate.commandSet instanceof Set; }
  function supportsCmd(name) { return !knownCaps() || rstate.commandSet.has(name); }
  // First supported command name from a preference list (or the first candidate
  // when caps are unknown, so behavior is unchanged before capabilities arrive).
  function pickCmd(cands, fallback) {
    if (!knownCaps()) return cands[0];
    for (let i = 0; i < cands.length; i++) if (rstate.commandSet.has(cands[i])) return cands[i];
    return fallback || null;
  }
  function captureCaps(caps) {
    if (!caps) return;
    rstate.caps = caps;
    // Commands the model accepts (so the driver only sends supported ones).
    const cmds = Array.isArray(caps.commands) ? caps.commands : null;
    if (cmds) {
      const names = cmds.map((c) => (typeof c === "string" ? c : (c && c.name))).filter(Boolean);
      if (names.length) {
        rstate.commandSet = new Set(names);
        log("model commands:", names.join(", "));
        emitEvent("capabilities", { commands: names });
      }
    }
    // Tracks the model exposes. Models don't all stream video on "main_video";
    // read the declared output video track so we attach the RIGHT one (a
    // mismatch here = "generation started" but no frames = a false stall).
    const tracks = Array.isArray(caps.tracks) ? caps.tracks : null;
    if (tracks) {
      const isVid = (t) => t && t.kind === "video";
      const outs = tracks.filter((t) => isVid(t) && (t.direction === "recvonly" || !t.direction)).map((t) => t.name).filter(Boolean);
      const ins = tracks.filter((t) => isVid(t) && t.direction === "sendonly").map((t) => t.name).filter(Boolean);
      if (outs.length) {
        rstate.videoTrackName = outs.indexOf("main_video") >= 0 ? "main_video" : outs[0];
        log("model video output tracks:", outs.join(", "), "-> using", rstate.videoTrackName);
      }
      // Surface tracks (esp. any REQUIRED input track) so a model that needs a
      // fed-in video to produce output is diagnosable instead of silently stalling.
      emitEvent("tracks", { outputs: outs, inputs: ins, chosen: rstate.videoTrackName || "main_video" });
    }
  }
  function clearCaps() {
    rstate.caps = null; rstate.commandSet = null; rstate.currentChunk = 0;
    rstate.videoTrackName = null; rstate.videoAttached = false;
  }
  // Wait (briefly) for the model's command schema before the OPENING command, so
  // the driver sends the right prompt command (set_shot vs schedule_prompt vs
  // set_prompt) and skips commands the model lacks (e.g. set_image on LongLive).
  // Capabilities normally arrive around session creation; poll getCapabilities()
  // in case the event landed before/after we subscribed. Resolves on a timeout so
  // a model that never advertises a schema still starts (best-effort defaults).
  function ensureCaps(timeoutMs) {
    if (knownCaps()) return Promise.resolve();
    return new Promise((resolve) => {
      const t0 = Date.now();
      const tick = () => {
        if (knownCaps() || !rstate.reactor) return resolve();
        try { captureCaps(rstate.reactor.getCapabilities && rstate.reactor.getCapabilities()); } catch (_) {}
        if (knownCaps()) return resolve();
        if (Date.now() - t0 >= (timeoutMs || 3500)) return resolve();
        setTimeout(tick, 150);
      };
      tick();
    });
  }

  // Commands we KNOW the LingBot (seed_locked) family accepts per the model
  // docs, even when a given capabilities payload doesn't enumerate them. LingBot
  // and LingBot World 2 are navigable-video models built for exactly these, so
  // we must never let a short/omitted caps list suppress movement — that's what
  // silently broke "I can't move" in production (the SDK advertised caps without
  // the movement axes, so cmd() skipped every one).
  const MOVE_COMMANDS = new Set([
    "set_move_longitudinal", "set_move_lateral",
    "set_look_horizontal", "set_look_vertical",
    "set_rotation_speed_deg", "set_camera_pose",
  ]);
  function familyDrivesCamera() { return familyFor(rstate.modelId) === "seed_locked"; }

  async function cmd(name, data) {
    // Never send a command the model doesn't advertise — skip it cleanly (and
    // announce the skip) instead of triggering a command_error round-trip. But
    // the LingBot movement axes are always allowed for the LingBot family even
    // if capabilities didn't list them (the docs guarantee them).
    if (!supportsCmd(name) && !(MOVE_COMMANDS.has(name) && familyDrivesCamera())) {
      log("skip unsupported command:", name);
      emitEvent("command_skipped", { command: name });
      return;
    }
    // Surface the payload (prompt text / whether an image seed rides along, and
    // the salient scalar for movement/look commands) so the world-model
    // inspector can show EXACTLY what we send to the model.
    let value = null;
    if (data) {
      if (typeof data.move_longitudinal === "string") value = data.move_longitudinal;
      else if (typeof data.move_lateral === "string") value = data.move_lateral;
      else if (typeof data.look_horizontal === "string") value = data.look_horizontal;
      else if (typeof data.look_vertical === "string") value = data.look_vertical;
      else if (typeof data.rotation_speed_deg === "number") value = data.rotation_speed_deg + "\u00B0/f";
    }
    emitEvent("command_sent", {
      command: name,
      prompt: (data && typeof data.prompt === "string") ? data.prompt : null,
      hasImage: !!(data && data.image),
      value: value,
    });
    return rstate.reactor.sendCommand(name, data || {});
  }

  // Deliver a scene prompt using whatever prompt command the model actually
  // supports — this is what lets one driver steer materially different models:
  //   • Helios:   schedule_prompt {prompt,chunk:0} to open, set_prompt to re-steer.
  //   • LingBot:  set_prompt.
  //   • LongLive: set_shot {prompt} to open / soft-change; scene_cut {prompt} for
  //               a hard transition. (LongLive has NO set_prompt/schedule_prompt.)
  // We only use the shot/cut commands when the model ACTUALLY advertises them
  // (never guessed), so Helios/LingBot are unaffected.
  async function sendScenePrompt(prompt, opts) {
    opts = opts || {};
    const has = (c) => knownCaps() && rstate.commandSet.has(c);
    // LongLive-family shot/cut protocol.
    if (has("set_shot") || has("scene_cut")) {
      if (opts.hardTransition && has("scene_cut")) return cmd("scene_cut", { prompt: prompt });
      if (has("set_shot")) return cmd("set_shot", { prompt: prompt });
      return cmd("scene_cut", { prompt: prompt });
    }
    // Helios / LingBot prompt commands.
    if (opts.establish) {
      const c = pickCmd(["schedule_prompt", "set_prompt"], "schedule_prompt");
      if (c === "set_prompt") return cmd("set_prompt", { prompt: prompt });
      return cmd("schedule_prompt", { prompt: prompt, chunk: 0 });
    }
    const c = pickCmd(["set_prompt", "schedule_prompt"], "set_prompt");
    if (c === "schedule_prompt") return cmd("schedule_prompt", { prompt: prompt, chunk: (rstate.currentChunk || 0) + 1 });
    return cmd("set_prompt", { prompt: prompt });
  }

  // ── Per-model drivers ───────────────────────────────────────────────────────
  // Each world model has a materially different wire protocol, so the "how to
  // realize a scene" logic lives in a driver. `establish(s)` stages a fresh run;
  // `applyRunning(s, ctx)` handles a per-turn update while already streaming.
  // Both return true when applied, false when they had to defer (retry later).

  // LingBot World 2: image-conditioned; the reference image is LOCKED once a run
  // starts, so a new guide image forces a fresh stage (reset + re-establish).
  async function establishLingbot(s) {
    // New stage boundary: invalidate any seed still decoding from a prior stage
    // so it can't paint over this one.
    rstate.seedToken++;
    if (!rstate.freezeActive && s.imageUrl) paintSeedToFreeze(s.imageUrl);
    const ref = await uploadStill(s.imageUrl);
    if (!ref) {
      // LingBot World 2 cannot start without a reference image. Keep the freeze
      // (seed) on screen and retry when a scene with an image arrives.
      log("no reference image yet — deferring start (LingBot requires one)");
      return false;
    }
    rstate.stagingGuideUrl = s.imageUrl || null;
    const imageReady = waitForEvent("image_accepted", IMAGE_ACCEPT_TIMEOUT_MS);
    await cmd("set_image", { image: ref });
    await cmd("set_prompt", { prompt: s.prompt });
    await imageReady; // let the seed decode so the first chunk starts from it
    await cmd("start", {});
    rstate.started = true;
    rstate.lastPrompt = s.prompt;
    emitEvent("stage_started", { prompt: s.prompt });
    applyMoveState(); // re-assert any held camera drive across the re-stage
    armFreezeReveal();
    armRevealWatchdog();
    log("lingbot: generation started (image-conditioned)");
    return true;
  }

  async function applyRunningLingbot(s, ctx) {
    if (s.hardTransition || ctx.newGuideImage) {
      // A new guide image means a fresh stage (reference image is locked). Fade
      // the scene down for the deliberate "moment of pause" beat, then freeze
      // the last live frame beneath it as the safety floor (revealed only if the
      // fresh stream never comes). The freeze reveal lifts the fade.
      beginSceneFade();
      captureVideoToFreeze();
      try { await cmd("reset", {}); } catch (err) { log("reset failed", err); }
      rstate.started = false;
      rstate.lastPrompt = null;
      rstate.lastRef = null;
      rstate.lastImageUrl = null;
      const ok = await establishLingbot(s);
      if (ok) log(s.hardTransition ? "hard transition re-staged" : "re-anchored on new guide image");
      // Couldn't re-establish (no reference image / upload failed): lift the fade
      // so the frozen last frame stays visible during the retry instead of
      // holding a black veil until the safety timer.
      else clearSceneFade();
      return ok;
    }
    await cmd("set_prompt", { prompt: s.prompt });
    rstate.lastPrompt = s.prompt;
    log("re-steered:", s.prompt.slice(0, 80));
    return true;
  }

  // Helios: text/image-to-video with infinite streaming. Starts text-only (no
  // seed image required); a new guide image blends IN-STREAM (set_image) with no
  // reset, and prompts re-steer live via set_prompt.
  async function establishHelios(s) {
    // New stage boundary: invalidate any seed still decoding from a prior stage.
    rstate.seedToken++;
    if (!rstate.freezeActive && s.imageUrl) paintSeedToFreeze(s.imageUrl);
    // Feed the guide image in the way Helios's docs prescribe: upload it and pass
    // the FileRef to set_image (NOT base64), crank the image-conditioning strength
    // up so it hews to our composition, set the prompt at chunk 0, and WAIT for
    // image_accepted before start — so the FIRST chunk is image-conditioned
    // instead of hitting the documented race where `start` sails past the upload
    // and chunk 0 renders from the prompt alone (which reads as "disconnected").
    // Only upload the guide still if the model can actually condition on an image
    // (Helios can; LongLive is text/shot-only, so skip the wasted upload).
    const ref = supportsCmd("set_image") ? await uploadStill(s.imageUrl) : null;
    await sendScenePrompt(s.prompt, { establish: true, hardTransition: s.hardTransition });
    if (ref && supportsCmd("set_image")) {
      rstate.stagingGuideUrl = s.imageUrl || null;
      try { await cmd("set_image_strength", { image_strength: HELIOS_IMAGE_STRENGTH }); } catch (_) {}
      const imageReady = waitForEvent("image_accepted", IMAGE_ACCEPT_TIMEOUT_MS);
      await cmd("set_image", { image: ref, transition: "cut" });
      rstate.lastImageUrl = s.imageUrl;
      await imageReady; // let the seed decode so chunk 0 renders from it
    }
    await cmd("start", {});
    rstate.started = true;
    rstate.lastPrompt = s.prompt;
    emitEvent("stage_started", { prompt: s.prompt });
    applyMoveState(); // re-assert any held camera drive across the re-stage
    armFreezeReveal();
    armRevealWatchdog();
    log("helios: generation started (image-conditioned)");
    return true;
  }

  async function applyRunningHelios(s, ctx) {
    const reanchor = ctx.newGuideImage || s.hardTransition;
    if (reanchor && supportsCmd("set_image")) {
      // Fade the scene down for the "moment of pause" beat before the new guide
      // image blends in — blend models stream continuously (no reset), so the
      // fade is what gives advancing time the same deliberate feel stills have.
      beginSceneFade();
      // Swap the guide image in-stream as a FileRef (same path as establish), at
      // full conditioning strength, so the live video re-anchors on the new still
      // instead of drifting. Blend keeps continuity; a hard transition cuts.
      const ref = await uploadStill(s.imageUrl);
      if (ref) {
        rstate.stagingGuideUrl = s.imageUrl;
        try { await cmd("set_image_strength", { image_strength: HELIOS_IMAGE_STRENGTH }); } catch (_) {}
        await cmd("set_image", { image: ref, transition: s.hardTransition ? "cut" : "blend" });
        rstate.lastImageUrl = s.imageUrl;
      }
    }
    await sendScenePrompt(s.prompt, { hardTransition: s.hardTransition });
    rstate.lastPrompt = s.prompt;
    // If we faded down for a re-anchor, hold the dark beat, then reveal the
    // blended-in scene (blend models have no freeze reveal to lift the fade).
    if (reanchor && rstate.fadeDownActive) scheduleSceneReveal(SCENE_FADE_BLEND_REVEAL_MS);
    log("helios/blend: re-steered", reanchor ? "(image re-anchored)" : "");
    return true;
  }

  // Drivers are keyed by PROTOCOL FAMILY, not by model id, so every current and
  // future Reactor model maps onto one of these without a bespoke driver. The
  // LingBot pair is the "seed_locked" family; the Helios pair is "blend" and is
  // also the default any unknown/new model falls back to.
  const DRIVERS = {
    "seed_locked": { establish: establishLingbot, applyRunning: applyRunningLingbot },
    "blend": { establish: establishHelios, applyRunning: applyRunningHelios },
  };
  function activeDriver() { return DRIVERS[familyFor(rstate.modelId)] || DRIVERS[FALLBACK_FAMILY]; }

  // Apply the most recent pending scene through the active model's driver.
  async function flush() {
    if (rstate.applying) return;
    if (!rstate.reactor || !rstate.ready || rstate.pending == null) return;
    const s = rstate.pending;
    rstate.pending = null;
    if (!s.prompt) return;

    // A still we haven't staged yet is a NEW guide image. How that's handled is
    // model-specific (LingBot re-stages; Helios blends it in), so the driver
    // decides. Steering-only updates (instant action beats) carry no image.
    const newGuideImage = !!(s.imageUrl && s.imageUrl !== rstate.lastImageUrl);

    // Dedupe pure re-sends of the same prompt while already running — but never
    // skip a new guide image or an explicit location change.
    if (rstate.started && s.prompt === rstate.lastPrompt && !s.hardTransition && !newGuideImage) return;

    const driver = activeDriver();
    rstate.applying = true;
    let deferred = false;
    try {
      if (!rstate.started) {
        deferred = !(await driver.establish(s));
      } else {
        deferred = !(await driver.applyRunning(s, { newGuideImage }));
      }
    } catch (err) {
      log("apply scene failed", err);
      if (rstate.pending == null) rstate.pending = s; // retry on next ready/flush
    } finally {
      rstate.applying = false;
    }

    if (deferred) {
      // Couldn't start (no reference image yet). Retry the SAME scene after a
      // short delay — unless a newer scene has already been queued — so we
      // don't spin in a tight failing loop.
      if (rstate.pending == null) {
        rstate.pending = s;
        setTimeout(() => { if (!rstate.started) flush(); }, 1500);
      } else {
        flush(); // a newer scene arrived; process it now
      }
      return;
    }
    // A newer scene may have arrived while we were applying — drain it.
    if (rstate.pending != null) flush();
  }

  function applyScene(scene) {
    if (!scene || !scene.prompt) return;
    rstate.pending = {
      prompt: scene.prompt,
      imageUrl: scene.imageUrl || null,
      hardTransition: !!scene.hardTransition,
    };
    // Remember the latest complete scene so a mid-game model swap can re-apply it
    // on the new model without waiting for the next turn.
    rstate.lastSceneApplied = rstate.pending;
    if (!rstate.active) { enable().then(() => flush()); return; }
    flush();
  }

  // Back-compat thin wrapper.
  function setPrompt(prompt, imageUrl) { applyScene({ prompt, imageUrl, hardTransition: false }); }

  // Route the model's messages: resolve any awaiting command flows and surface
  // every lifecycle event to the ceremony UI.
  function handleMessage(msg) {
    if (!msg || !msg.type) return;
    const t = msg.type;
    const d = msg.data || {};
    // Track the model's chunk cursor so a look-ahead re-steer (schedule_prompt)
    // can target a FUTURE chunk on models that lack a live set_prompt.
    if (t === "state" && typeof d.current_chunk === "number") rstate.currentChunk = d.current_chunk;
    // LingBot reports image_accepted/prompt_accepted; Helios reports
    // image_set/prompt_scheduled/prompt_switched. Map both onto the same
    // internal waiters so the command flows are model-agnostic.
    if (t === "image_accepted" || t === "image_set") {
      resolveWaiters("image_accepted", d);
      // The guide image is now decoded and anchoring the world — announce it.
      const url = rstate.stagingGuideUrl || rstate.lastImageUrl || null;
      if (url) {
        rstate.guideImageUrl = url;
        rstate.stagingGuideUrl = null;
        emitGuideImage(url, d);
      }
    }
    else if (t === "prompt_accepted" || t === "prompt_scheduled" || t === "prompt_switched" ||
             t === "shot_set" || t === "shot_scheduled" || t === "scene_cut") {
      // LongLive acknowledges a prompt with shot_set / scene_cut / shot_scheduled;
      // map them onto the same internal waiter so the flow is model-agnostic.
      resolveWaiters("prompt_accepted", d);
    }
    else if (t === "generation_started") resolveWaiters("generation_started", d);
    emitEvent(t, d);
  }

  async function enable() {
    if (rstate.active || rstate.connecting) return true;
    rstate.connecting = true;
    setStatus("connecting");
    try {
      await loadConfig();
      if (!rstate.cfg.enabled) { log("disabled: no REACTOR_API_KEY on server"); rstate.connecting = false; setStatus("error"); return false; }
      let sdk;
      try { sdk = await import(/* @vite-ignore */ SDK_URL); }
      catch (err) { log("SDK import failed", err); rstate.connecting = false; setStatus("error"); return false; }
      const Reactor = sdk.Reactor || (sdk.default && sdk.default.Reactor);
      if (!Reactor) { log("SDK missing Reactor export"); rstate.connecting = false; setStatus("error"); return false; }

      const modelId = resolveModelId();
      const modelName = modelNameFor(modelId);
      log("connecting to world model:", modelId, "(" + modelName + ")");
      const reactor = new Reactor({ modelName: modelName });
      rstate.reactor = reactor;
      reactor.on("trackReceived", attachTrack);
      reactor.on("statusChanged", async (status) => {
        log("status:", status);
        if (status === "ready") {
          rstate.ready = true;
          // Load the model's command schema BEFORE the first (opening) command so
          // we send the right prompt command and skip unsupported ones.
          await ensureCaps(3500);
          await flush();
        }
        else if (status === "disconnected") { rstate.ready = false; }
      });
      // Read the model's command schema so the driver adapts to what it supports.
      try { reactor.on("capabilitiesReceived", captureCaps); } catch (_) {}
      // Lifecycle messages (state, *_accepted, generation_*, command_error…).
      try { reactor.on("message", handleMessage); } catch (_) {}
      reactor.on("error", (e) => {
        log("error", e && e.code, e && e.message);
        emitEvent("command_error", { command: (e && e.command) || "", reason: (e && e.message) || (e && e.code) || "error" });
        if (e && e.recoverable === false) setStatus("error");
      });

      const jwt = await fetchToken();
      rstate.active = true;
      await reactor.connect(jwt);
      rstate.connecting = false;
      return true;
    } catch (err) {
      log("enable failed", err);
      rstate.connecting = false;
      setStatus("error");
      await disable();
      return false;
    }
  }

  // Tear down the current Reactor session WITHOUT clearing the freeze cover or
  // the chosen model — used by a live model swap so the last frame stays on
  // screen while we reconnect to the other world model.
  async function teardownSession() {
    rstate.ready = false;
    rstate.started = false;
    rstate.paused = false;
    rstate.lastPrompt = null;
    rstate.lastImageUrl = null;
    rstate.lastRef = null;
    rstate.stagingGuideUrl = null;
    rstate.frameWatch = false;
    rstate.freezeArmed = false;
    clearBlackout();
    clearSceneFade(); // the freeze cover holds the last frame across the swap
    clearCaps(); // the next model advertises its own command schema
    clearRevealWatchdog();
    if (rstate.freezeFallbackTimer) { clearTimeout(rstate.freezeFallbackTimer); rstate.freezeFallbackTimer = null; }
    if (rstate.frameWatchTimer) { clearInterval(rstate.frameWatchTimer); rstate.frameWatchTimer = null; }
    const video = getVideo();
    if (video) { try { video.srcObject = null; } catch (_) {} } // freeze cover stays up
    const r = rstate.reactor;
    rstate.reactor = null;
    rstate.active = false;
    rstate.connecting = false;
    if (rstate.status !== "error") setStatus("connecting");
    if (r) { try { await r.disconnect(); } catch (_) {} }
  }

  // Switch world models LIVE, mid-game. Reactor is one model per session, so we
  // freeze the current frame, tear the session down, and reconnect to the other
  // model — then re-apply the current scene on it. The freeze buffer keeps the
  // last frame on screen throughout, so the swap reads as a VCR-style hand-off
  // with no black gap. Returns true if the switch was accepted.
  async function setModel(id) {
    if (!id) return false;
    if (!knownModel(id)) {
      if (!rstate.allowCustom) { log("unknown world model (custom disabled):", id); return false; }
      ensureModel(id); // brand-new / typed-in model — register + use it live
    }
    if (id === rstate.modelId) return true;
    if (rstate.swapping) return false;
    rstate.swapping = true;
    try {
      const scene = rstate.lastSceneApplied || rstate.pending || null;
      // Cover the switch: hold the last live frame (or the seed still) so the
      // reconnect never flashes black or the underlying image.
      if (!captureVideoToFreeze() && scene && scene.imageUrl) paintSeedToFreeze(scene.imageUrl);
      const wasActive = rstate.active || rstate.connecting;
      rstate.modelId = id;
      try { localStorage.setItem("world_model", id); } catch (_) {}
      log("switching world model ->", id, "(" + modelNameFor(id) + ")");
      emitEvent("model_switching", { model: id, label: modelLabel(id) });
      if (typeof window.ReactorRenderer.onModel === "function") {
        try { window.ReactorRenderer.onModel(id, modelLabel(id)); } catch (_) {}
      }
      if (wasActive) {
        await teardownSession();
        // Queue the current scene so the new session applies it once ready.
        if (scene) rstate.pending = scene;
        const ok = await enable(); // reconnects with the new modelId
        if (ok && rstate.ready) await flush();
        return ok;
      }
      return true;
    } finally {
      rstate.swapping = false;
    }
  }

  async function disable() {
    rstate.ready = false;
    rstate.started = false;
    rstate.paused = false;
    rstate.pending = null;
    rstate.lastPrompt = null;
    rstate.lastImageUrl = null;
    rstate.lastRef = null;
    rstate.stagingGuideUrl = null;
    rstate.frameWatch = false;
    rstate.freezeArmed = false;
    clearCaps();
    clearBlackout();
    resetMoveState(); // camera returns to rest for a fresh run
    rstate.seedToken++; // drop any in-flight seed decode from the dead run
    clearRevealWatchdog();
    if (rstate.freezeFallbackTimer) { clearTimeout(rstate.freezeFallbackTimer); rstate.freezeFallbackTimer = null; }
    if (rstate.frameWatchTimer) { clearInterval(rstate.frameWatchTimer); rstate.frameWatchTimer = null; }
    const video = getVideo();
    if (video) { video.classList.add("hidden"); try { video.srcObject = null; } catch (_) {} }
    // Drop the freeze cover so the still fallback (image mode) shows cleanly.
    const f = getFreeze();
    if (f) { rstate.freezeActive = false; f.classList.remove("show"); }
    clearSceneFade(); // don't leave a fade veil over the still fallback
    const r = rstate.reactor;
    rstate.reactor = null;
    rstate.active = false;
    if (rstate.status !== "error") setStatus("off");
    if (r) { try { await r.disconnect(); } catch (_) {} }
  }

  async function reset() {
    rstate.started = false;
    rstate.lastPrompt = null;
    rstate.lastImageUrl = null;
    rstate.lastRef = null;
    rstate.paused = false;
    // Drop anything still queued/staged from the previous run so a stale scene
    // (e.g. the dead run's opening image) can't flush in after the reset. Reset
    // MUST fully clear the queue — otherwise the old image reappears/regenerates
    // and throws the fresh run off.
    rstate.pending = null;
    rstate.stagingGuideUrl = null;
    rstate.guideImageUrl = null;
    rstate.freezeArmed = false;
    clearBlackout();
    resetMoveState(); // camera returns to rest for a fresh run
    rstate.seedToken++; // drop any in-flight seed decode from the dead run
    clearRevealWatchdog();
    if (rstate.freezeFallbackTimer) { clearTimeout(rstate.freezeFallbackTimer); rstate.freezeFallbackTimer = null; }
    // A game reset is a CLEAN WIPE (unlike a per-turn re-anchor, which freezes
    // the last frame for a seamless switch): drop the freeze cover and hide the
    // video so the dead run's scene is gone immediately. The fresh run paints
    // its seed onto the freeze and reveals its own video when it establishes.
    const f = getFreeze();
    if (f) { rstate.freezeActive = false; f.classList.remove("show"); }
    clearSceneFade(); // a fresh run wipes clean — no lingering fade veil
    const v = getVideo();
    if (v) v.classList.add("hidden");
    if (rstate.status === "live") setStatus("connecting");
    if (!rstate.reactor || !rstate.ready) return;
    try { await cmd("reset", {}); } catch (err) { log("reset failed", err); }
  }

  async function pause() {
    if (!rstate.reactor || !rstate.ready || !rstate.started || rstate.paused) return;
    rstate.paused = true;
    try { await cmd("pause", {}); } catch (err) { log("pause failed", err); }
  }

  async function resume() {
    if (!rstate.reactor || !rstate.ready || !rstate.paused) return;
    rstate.paused = false;
    try { await cmd("resume", {}); } catch (err) { log("resume failed", err); }
  }

  // Grab the current on-screen video frame as a JPEG data URL (downscaled to
  // keep the payload small). Used to feed the world simulator what the player
  // actually sees. Returns null if the video isn't showing real frames.
  function captureFrame(maxW) {
    const v = rstate.video || document.getElementById("reactor-video");
    // Only read the LIVE video — not while the freeze buffer is covering it
    // (that frame isn't the current scene the model is actually rendering), and
    // not during a blackout (the frame is solid black; feeding it to the sim
    // would derail the story with a "you see nothing" grounding).
    if (!v || !v.videoWidth || rstate.freezeActive || rstate.blackout) return null;
    const cap = maxW || 512;
    const scale = Math.min(1, cap / v.videoWidth);
    const w = Math.max(1, Math.round(v.videoWidth * scale));
    const h = Math.max(1, Math.round(v.videoHeight * scale));
    try {
      const c = document.createElement("canvas");
      c.width = w; c.height = h;
      c.getContext("2d").drawImage(v, 0, 0, w, h);
      return c.toDataURL("image/jpeg", 0.72);
    } catch (err) {
      log("captureFrame failed", err);
      return null;
    }
  }

  // Crop a NORMALIZED sub-rect {x,y,w,h} (0..1) of the current live video frame
  // to a square JPEG data URL — the "investigation texture" the TOUCH tool grabs
  // from around/under the reticle. Returns null unless real frames are showing.
  function captureRegion(box, outSize) {
    const v = rstate.video || document.getElementById("reactor-video");
    if (!v || !v.videoWidth || rstate.freezeActive || rstate.blackout || !box) return null;
    const vw = v.videoWidth, vh = v.videoHeight;
    let sx = Math.max(0, Math.min(1, box.x || 0)) * vw;
    let sy = Math.max(0, Math.min(1, box.y || 0)) * vh;
    let sw = Math.max(1, Math.min(1, box.w || 0) * vw);
    let sh = Math.max(1, Math.min(1, box.h || 0) * vh);
    if (sx + sw > vw) sw = vw - sx;
    if (sy + sh > vh) sh = vh - sy;
    const out = outSize || 256;
    // Preserve the captured region's aspect ratio (the capture frame is 16:9,
    // not a square) — `out` is the longest side.
    const aspect = sw / sh;
    let ow = out, oh = out;
    if (aspect >= 1) oh = Math.max(1, Math.round(out / aspect));
    else ow = Math.max(1, Math.round(out * aspect));
    try {
      const c = document.createElement("canvas");
      c.width = ow; c.height = oh;
      c.getContext("2d").drawImage(v, sx, sy, sw, sh, 0, 0, ow, oh);
      return c.toDataURL("image/jpeg", 0.82);
    } catch (e) { log("captureRegion failed", e); return null; }
  }

  // ── Live camera drive (LingBot World 2 native movement) ─────────────────────
  // The world model is navigable in real time through persistent-state command
  // axes — this is the channel that actually MOVES the camera (a set_prompt
  // "camera moves" beat does not). Each axis holds its value across chunks until
  // changed, so we drive it exactly like a game: set on keydown / stick push,
  // idle on release. See the LingBot World 2 schema reference.
  const AXIS_CMD = {
    longitudinal: { cmd: "set_move_longitudinal", param: "move_longitudinal" },
    lateral:      { cmd: "set_move_lateral",      param: "move_lateral" },
    lookH:        { cmd: "set_look_horizontal",   param: "look_horizontal" },
    lookV:        { cmd: "set_look_vertical",     param: "look_vertical" },
  };

  // Does the active world model expose the navigable movement axes? LingBot /
  // LingBot World 2 do; blend-family models (Helios, LongLive, SANA) generally
  // don't, so callers fall back to a prompt nudge there. When capabilities
  // haven't arrived yet we optimistically say yes (they load before the first
  // command, and cmd() skips anything genuinely unsupported).
  function motionSupported() {
    // LingBot (seed_locked) family always drives the camera natively; other
    // families only if they actually advertise the axes.
    return familyDrivesCamera() || !knownCaps() || rstate.commandSet.has("set_move_longitudinal");
  }

  // Re-assert the currently-held axes after a (re)start. A per-turn re-anchor
  // issues `reset`, which clears the model's movement state, so if the player is
  // still holding a direction we must send it again for the fresh stage.
  function applyMoveState() {
    if (!rstate.reactor || !rstate.ready || !rstate.started) return;
    const m = rstate.move;
    if (typeof m.rotationDeg === "number") cmd("set_rotation_speed_deg", { rotation_speed_deg: m.rotationDeg });
    Object.keys(AXIS_CMD).forEach((k) => {
      if (m[k] && m[k] !== "idle") {
        const a = AXIS_CMD[k]; const d = {}; d[a.param] = m[k];
        cmd(a.cmd, d);
      }
    });
  }

  // Set one movement/look axis. Deduped (persistent state — no point resending
  // the same value) and deferred until generation is running (the value is
  // remembered and re-applied by applyMoveState() once it starts).
  function setAxis(axis, value) {
    const a = AXIS_CMD[axis];
    if (!a) return false;
    value = value || "idle";
    if (rstate.move[axis] === value) return true;
    rstate.move[axis] = value;
    if (!rstate.reactor || !rstate.ready || !rstate.started) return false;
    const d = {}; d[a.param] = value;
    cmd(a.cmd, d);
    return true;
  }

  // Rotation speed (deg/latent-frame, 0..30) — how fast a held look axis turns.
  function setRotationSpeed(deg) {
    deg = Math.max(0, Math.min(30, Number(deg) || 0));
    // Quantize a little so tiny analog jitters don't spam identical commands.
    deg = Math.round(deg * 2) / 2;
    if (rstate.move.rotationDeg === deg) return;
    rstate.move.rotationDeg = deg;
    if (!rstate.reactor || !rstate.ready || !rstate.started) return;
    cmd("set_rotation_speed_deg", { rotation_speed_deg: deg });
  }

  // Idle every axis — the camera comes to rest (persistent state means we MUST
  // send idle or it keeps moving after the player lets go).
  function stopMotion() {
    setAxis("longitudinal", "idle");
    setAxis("lateral", "idle");
    setAxis("lookH", "idle");
    setAxis("lookV", "idle");
  }

  // Drop the tracked drive state (a fresh run / teardown starts from rest).
  function resetMoveState() {
    rstate.move = { longitudinal: "idle", lateral: "idle", lookH: "idle", lookV: "idle", rotationDeg: null };
  }

  window.ReactorRenderer = {
    enable, disable, applyScene, setPrompt, reset, pause, resume, captureFrame, captureRegion,
    setModel,
    // Scene-fade controls, exposed so the standalone layer can preemptively
    // fade the live world to black the instant a hard transition is COMMITTED
    // (e.g. MOVE TO) — instead of waiting for the new guide image to arrive.
    // Otherwise the current stream keeps drifting for seconds while the next
    // scene generates, which reads as ridiculous. The reactor's own re-anchor
    // path calls beginSceneFade() again with its default safety window when the
    // fresh scene lands, and armFreezeReveal / scheduleSceneReveal lift the
    // veil once the new frame is on screen — so all we need to expose is the
    // early "start the fade now" hook.
    beginSceneFade, endSceneFade,
    // Live camera drive (see above): the navigable-video control surface.
    motionSupported, setAxis, setRotationSpeed, stopMotion,
    // Register a custom / brand-new Reactor model at runtime (from the UI's
    // "add model" field), then it's selectable like any other. Returns the id.
    addModel: (id, label, opts) => {
      id = (id || "").trim();
      if (!id) return null;
      const m = ensureModel(id, label, opts);
      return m ? m.id : null;
    },
    // Whether the server permits connecting to unadvertised model names.
    allowsCustom: () => !!rstate.allowCustom,
    getStatus: () => rstate.status,
    isActive: () => rstate.active,
    isReady: () => rstate.ready,
    // World-model selection API (for the mid-game switcher UI).
    getModel: () => rstate.modelId,
    getModels: () => (rstate.models || []).map((m) => ({
      id: m.id, label: m.label, active: m.id === rstate.modelId,
      protocol: m.protocol, custom: !!m.custom,
    })),
    // The last guide image actually integrated into the live world model.
    getGuideImage: () => rstate.guideImageUrl || null,
    // The prompt the live stream is currently running (the last one applied to
    // the model). Lets callers re-steer ON TOP of the running scene even when the
    // standalone layer never saw a feed scene_image for it (e.g. a stream that
    // was established directly by native movement/exploration mode).
    getPrompt: () => rstate.lastPrompt ||
      (rstate.lastSceneApplied && rstate.lastSceneApplied.prompt) || null,
    // True only when the LIVE video is actually on-screen (decoded frames and
    // the freeze back-buffer is not covering it).
    isShowing: () => {
      const v = rstate.video || document.getElementById("reactor-video");
      return !!(v && v.videoWidth > 0 && !rstate.freezeActive && !rstate.blackout);
    },
    // Intrinsic size of the live video track, so callers can map normalized
    // frame coordinates (e.g. object-detection boxes) onto the object-fit:cover
    // display rect. Returns null when no real frames are flowing yet.
    getVideoSize: () => {
      const v = rstate.video || document.getElementById("reactor-video");
      if (!v || !v.videoWidth || !v.videoHeight) return null;
      return { w: v.videoWidth, h: v.videoHeight };
    },
    onStatus: null,
    onEvent: null,
    // fn(imageUrl, data) — fired when a guide image is integrated into the world.
    onGuideImage: null,
    // fn(id, label) — fired when the active world model changes.
    onModel: null,
    // fn() — fired when the available-model list changes (e.g. a custom model
    // was added), so the switcher UI can rebuild.
    onModelsChanged: null,
  };
})();
