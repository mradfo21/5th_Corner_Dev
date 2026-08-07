/* ============================================================
   SOMEWHERE // Reactor realtime renderer (Happy Oyster + legacy models)

   Drives Reactor's real-time world models as an alternative scene renderer. The
   DEFAULT model is Happy Oyster — a prompt-to-world model: a paragraph of text
   (built server-side by build_realtime_prompt), optionally anchored by a
   first-frame image (the SAME still the game generated), becomes a navigable
   place you then TRAVEL through in first person. Older models (LingBot World 2,
   Helios, …) remain selectable; each protocol family has its own driver.

   Wire protocol by family:
     • "happy_oyster" (Happy Oyster — DEFAULT): two-phase, build then travel.
         create_world({prompt, first_frame_image_url?, perspective}) ->
           [await world_state ready] -> start_travel
       The world is FIXED once built — you steer the live stream with held
       movement (move: Front/Back/Left/Right), look (look: Mouse_Up/Down/Left/
       Right) and interaction verbs (interact: {action}); `stop` releases every
       held control. There is NO live prompt edit in the Adventure experience,
       so a NEW scene (a fresh still, a location change / hard_transition, or a
       materially new prompt) is a NEW WORLD: we rebuild via create_world +
       start_travel, re-anchoring on the new first-frame image every turn.
     • "seed_locked" (LingBot World 2 / LingBot): image-conditioned; the
       reference image is LOCKED once a run starts, so a new guide image forces a
       fresh stage. uploadFile(still) -> set_image -> [await image_accepted] ->
       set_prompt -> start; a new guide image re-stages via reset. A same-scene
       prompt-only re-steer hot-swaps with set_prompt.
     • "blend" (Helios / LongLive / SANA, and the default for anything new):
       text/image-to-video; a new guide image blends in-stream (set_image, no
       reset) and prompts re-steer live (set_prompt / schedule_prompt).

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
  //   • "happy_oyster" (Happy Oyster — DEFAULT): prompt-to-world; build a world
  //     (create_world) then travel it (start_travel). Steered by held movement/
  //     look + interaction verbs; a new scene rebuilds the world.
  //   • "seed_locked" (LingBot): reference image locked once a run starts, so a
  //     new guide image forces a fresh stage (reset + re-establish).
  //   • "blend" (Helios, and the flexible fallback for anything new/unknown):
  //     text/image to video; a new guide image blends in-stream with no reset,
  //     prompts re-steer live.
  // The model list + protocols come from /api/reactor/config (which itself is
  // env-driven and open to custom models); these are just pre-config defaults.
  const FALLBACK_MODEL_ID = "happy-oyster";
  const FALLBACK_MODEL = "reactor/happy-oyster";
  const FALLBACK_FAMILY = "blend"; // the flexible family unknown models default to
  const SDK_PREFIX = "reactor/";   // how a bare model id becomes an SDK name
  const DEFAULT_MODELS = [
    { id: "happy-oyster", label: "Happy Oyster", sdk_name: "reactor/happy-oyster", requiresSeedImage: false, protocol: "happy_oyster" },
    { id: "lingbot-world-2", label: "LingBot World 2", sdk_name: "reactor/lingbot-world-2", requiresSeedImage: true, protocol: "seed_locked" },
    { id: "helios", label: "Helios", sdk_name: "reactor/helios", requiresSeedImage: false, protocol: "blend" },
    { id: "lingbot", label: "LingBot", sdk_name: "reactor/lingbot", requiresSeedImage: true, protocol: "seed_locked" },
    { id: "longlive-v2", label: "LongLive V2", sdk_name: "reactor/longlive-v2", requiresSeedImage: false, protocol: "blend" },
    { id: "sana-streaming", label: "Sana Streaming", sdk_name: "reactor/sana-streaming", requiresSeedImage: false, protocol: "blend" },
  ];
  // Happy Oyster session shape (fixed for a session's lifetime). Two experiences
  // — the FULL Happy Oyster surface — chosen at connect and honored by the
  // driver:
  //   • "adventure" (default) — walk/look/interact in first (or third) person.
  //     Our survival game lives here. create_world takes `perspective`.
  //   • "director" — steer the unfolding scene with text instructions and control
  //     playback (pause/resume/rewind). create_world takes resolution/layout/
  //     narrative. There are no movement/interact verbs; steering is `instruct`.
  // Resolved from ?experience= / localStorage / window global so a session can be
  // opened in either experience. Built-in interaction verbs the Adventure world
  // always accepts (worlds advertise more via travel_state — see hoVerbs).
  const HAPPY_OYSTER_BUILTIN_VERBS = ["Jump", "Attack", "Crouch", "Sprint"];
  function readOpt(qsKey, lsKey, winKey) {
    let v = null;
    try { v = new URLSearchParams(location.search).get(qsKey); } catch (_) {}
    if (!v) { try { v = localStorage.getItem(lsKey); } catch (_) {} }
    if (!v && typeof window !== "undefined" && window[winKey]) v = window[winKey];
    return v ? String(v).toLowerCase() : null;
  }
  function happyOysterExperience() {
    const v = rstate.hoExperience || readOpt("experience", "happy_oyster_experience", "__HAPPY_OYSTER_MODE__") || "adventure";
    return (v === "director" || v === "directing") ? "director" : "adventure";
  }
  function happyOysterPerspective() {
    const v = rstate.hoPerspective || readOpt("perspective", "happy_oyster_perspective", "__HAPPY_OYSTER_PERSPECTIVE__") || "first_person";
    return v === "third_person" ? "third_person" : "first_person";
  }
  // Director-experience create_world knobs (Directing worlds only). Overridable
  // via window globals; sane Happy Oyster defaults otherwise.
  function directorParams() {
    const g = (k, d) => (typeof window !== "undefined" && window[k]) || d;
    return {
      resolution: String(g("__HAPPY_OYSTER_RESOLUTION__", "720p")),
      layout: String(g("__HAPPY_OYSTER_LAYOUT__", "Stable")),
      narrative: String(g("__HAPPY_OYSTER_NARRATIVE__", "Normal")),
    };
  }
  // How long to wait for create_world to report a ready world_state before we
  // start travelling anyway (build is a short generation step).
  const WORLD_BUILD_TIMEOUT_MS = (typeof window !== "undefined" && window.__HAPPY_OYSTER_BUILD_TIMEOUT_MS__) || 15000;
  // How long to wait for the seed image to decode before starting anyway.
  const IMAGE_ACCEPT_TIMEOUT_MS = 6000;
  // Guide-image deferral budget (see flush). A seed-locked model can't start
  // without a still, so we retry — but bounded, or a scene whose still never
  // arrives leaves the stream black forever with no way out.
  const GUIDE_RETRY_MS = (typeof window !== "undefined" && window.__GUIDE_RETRY_MS__) || 1500;
  // Still present but unusable (swept / 404): broken now, won't heal — bail fast.
  const GUIDE_UPLOAD_MAX_ATTEMPTS =
    (typeof window !== "undefined" && window.__GUIDE_UPLOAD_MAX_ATTEMPTS__) || 3;
  // No still yet: the render is probably still in flight (the intro image takes
  // tens of seconds), so wait generously — but not forever.
  const GUIDE_WAIT_MAX_ATTEMPTS =
    (typeof window !== "undefined" && window.__GUIDE_WAIT_MAX_ATTEMPTS__) || 40;
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
    // Happy Oyster travel state. Its move/look are HELD single-direction
    // commands (only `stop` releases them), so we track the last direction we
    // actually sent per channel and diff against the desired axes.
    hoTraveling: false,        // start_travel issued and the world is streaming
    hoSentMove: null,          // last move {direction} sent (Front/Back/Left/Right)
    hoSentLook: null,          // last look {direction} sent (Mouse_*)
    hoHeldVerb: null,          // desired held interaction verb (Sprint/Crouch/…)
    hoSentVerb: null,          // last held interaction verb actually asserted
    hoVerbs: [],               // interaction verbs the current world advertises
    hoWorldId: null,           // encrypted_world_id of the current world (attach_world)
    hoWorldsByImage: Object.create(null), // scene guide-image URL -> { id, prompt } (revisit cache)
    hoStagingPrompt: null,     // prompt of the world currently being built (for the cache key)
    hoExperience: null,        // resolved experience for this session ("adventure"|"director")
    hoPerspective: null,       // resolved perspective ("first_person"|"third_person")
    cfg: { model_name: FALLBACK_MODEL, enabled: false },
    connecting: false,
    connectedAt: 0,        // Date.now() a session actually went live (for cost-usage reporting)
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
    lastError: null,       // most recent connect failure: { message, capacity } (see classifyConnectError)
  };

  // Event waiters keyed by model message type, so command flows can await a
  // specific model reply (e.g. image_accepted) with a timeout fallback.
  const waiters = Object.create(null);

  function log() { try { console.log.apply(console, ["[reactor]", ...arguments]); } catch (_) {} }

  // Reactor's own infra occasionally has no free GPU/session slot for a model
  // ("Failed to create session: 429 {"error":"no available capacity: ..."}")
  // — a transient upstream availability issue, distinct from a local WebRTC/
  // ICE hiccup. Those self-heal in a couple seconds; capacity errors can take
  // longer (Reactor needs to free or spin up a server), so callers use this
  // flag to be more patient before giving up and to explain WHY to the player
  // instead of a generic "reconnecting" message.
  function classifyConnectError(err) {
    const message = String((err && err.message) || err || "unknown error");
    const capacity = /\b429\b/.test(message) ||
      /no available (capacity|servers?)/i.test(message) ||
      /at capacity|no capacity|no servers? available/i.test(message);
    // Capacity was the only failure that got a name, so every other cause —
    // an unconfigured server, a blocked SDK, a rejected key — showed the same
    // "realtime unavailable" with nothing actionable in it. These are the
    // causes worth telling apart, because the fix for each is different.
    let reason = "unknown";
    let hint = "Realtime unavailable \u2014 showing stills";
    if (capacity) {
      reason = "capacity";
      hint = "Reactor is full right now \u2014 showing stills (retrying quietly)";
    } else if (/not configured|REACTOR_API_KEY|\b503\b/i.test(message)) {
      reason = "not_configured";
      hint = "Realtime isn't configured on this server \u2014 showing stills";
    } else if (/\b401\b|\b403\b|unauthor|forbidden/i.test(message)) {
      reason = "bad_key";
      hint = "Realtime rejected this server's key \u2014 showing stills";
    } else if (/import|module|failed to fetch|network|load/i.test(message)) {
      reason = "sdk_blocked";
      hint = "Couldn't load the realtime SDK \u2014 showing stills";
    } else if (/token/i.test(message)) {
      reason = "token_exchange_failed";
      hint = "Realtime sign-in failed \u2014 showing stills";
    }
    return { message: message, capacity: capacity, reason: reason, hint: hint };
  }

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
    tickTelemetry();
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

  // ── Telemetry ────────────────────────────────────────────────────────────
  // "Is the stream actually alive?" was previously answered by whether the
  // status string said "live", which stays true through a total stall — the
  // last decoded frame just sits there. Counting presented frames is the only
  // way to tell a running world from a frozen picture of one.
  const tele = { frames: 0, fps: 0, lastFrameTs: 0, _windowStart: 0, _windowFrames: 0 };

  function tickTelemetry() {
    const now = Date.now();
    tele.frames++;
    tele.lastFrameTs = now;
    if (!tele._windowStart) { tele._windowStart = now; tele._windowFrames = 0; }
    tele._windowFrames++;
    const span = now - tele._windowStart;
    if (span >= 1000) {
      tele.fps = Math.round((tele._windowFrames * 1000) / span);
      tele._windowStart = now;
      tele._windowFrames = 0;
    }
  }

  function resetTelemetry() {
    tele.frames = 0; tele.fps = 0; tele.lastFrameTs = 0;
    tele._windowStart = 0; tele._windowFrames = 0;
  }

  function getTelemetry() {
    const since = tele.lastFrameTs ? Date.now() - tele.lastFrameTs : null;
    return {
      frames: tele.frames,
      fps: tele.fps,
      msSinceLastFrame: since,
      // Frames stopped arriving but we still claim to be live — the picture is
      // frozen. Two seconds is well past any normal inter-frame gap.
      stalled: rstate.status === "live" && since != null && since > 2000,
      status: rstate.status,
      blackout: !!rstate.blackout,
    };
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
      // Remember the handler so teardown can detach it. The <video> element is
      // persistent, but startFrameWatch runs again on every new track (model
      // swap, capacity retry, reconnect) — without removal each reconnect left
      // another pair of listeners bound, so onPresentedFrame ran N times per
      // timeupdate, doing N freeze-reveal checks and N seedToken bumps.
      stopFrameWatchListeners();
      const onp = () => onPresentedFrame(video);
      rstate.frameWatchEl = video;
      rstate.frameWatchHandler = onp;
      video.addEventListener("playing", onp);
      video.addEventListener("timeupdate", onp);
      rstate.frameWatchTimer = setInterval(() => {
        if (!rstate.reactor) { clearInterval(rstate.frameWatchTimer); rstate.frameWatch = false; return; }
        onPresentedFrame(video);
      }, 400);
    }
  }

  // Detach the fallback frame-watch listeners (no-op on the
  // requestVideoFrameCallback path, which self-cancels via rstate.frameWatch).
  function stopFrameWatchListeners() {
    const v = rstate.frameWatchEl;
    const h = rstate.frameWatchHandler;
    if (v && h) {
      try {
        v.removeEventListener("playing", h);
        v.removeEventListener("timeupdate", h);
      } catch (_) {}
    }
    rstate.frameWatchEl = null;
    rstate.frameWatchHandler = null;
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
    // The SDK re-emits "capabilitiesReceived" repeatedly (once per state
    // message on some models), so everything here is CHANGE-AWARE: we only
    // reassign, log, and emit when the command set / track selection actually
    // changes. Otherwise a live session floods the console (the observed
    // "model video output tracks ..." x26) and churns the ceremony UI with
    // identical events every ~chunk.
    // Commands the model accepts (so the driver only sends supported ones).
    const cmds = Array.isArray(caps.commands) ? caps.commands : null;
    if (cmds) {
      const names = cmds.map((c) => (typeof c === "string" ? c : (c && c.name))).filter(Boolean);
      if (names.length) {
        const key = names.join(",");
        if (key !== rstate._capsCmdKey) {
          rstate._capsCmdKey = key;
          rstate.commandSet = new Set(names);
          log("model commands:", names.join(", "));
          emitEvent("capabilities", { commands: names });
        }
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
        const chosen = outs.indexOf("main_video") >= 0 ? "main_video" : outs[0];
        const key = outs.join(",") + "|" + ins.join(",") + "|" + chosen;
        if (key !== rstate._capsTrackKey) {
          rstate._capsTrackKey = key;
          rstate.videoTrackName = chosen;
          log("model video output tracks:", outs.join(", "), "-> using", rstate.videoTrackName);
          // Surface tracks (esp. any REQUIRED input track) so a model that
          // needs a fed-in video to produce output is diagnosable instead of
          // silently stalling.
          emitEvent("tracks", { outputs: outs, inputs: ins, chosen: rstate.videoTrackName || "main_video" });
        }
      }
    }
  }
  function clearCaps() {
    rstate.caps = null; rstate.commandSet = null; rstate.currentChunk = 0;
    rstate.videoTrackName = null; rstate.videoAttached = false;
    rstate._capsCmdKey = null; rstate._capsTrackKey = null;
    rstate._appliedPrompt = null; rstate._appliedImageUrl = null;
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
  // Happy Oyster's typed command surface. These are always allowed for the
  // happy_oyster family — a build/travel/steer command must never be suppressed
  // by a short or omitted capabilities payload.
  const HAPPY_OYSTER_COMMANDS = new Set([
    "create_world", "attach_world", "start_travel", "disconnect",
    "move", "look", "interact", "stop",
    "instruct", "pause", "resume", "rewind",
  ]);
  function isHappyOyster() { return familyFor(rstate.modelId) === "happy_oyster"; }
  // Families that navigate the camera natively (vs a prompt-nudge fallback):
  // LingBot's movement axes and Happy Oyster's held move/look both qualify.
  function familyDrivesCamera() {
    const f = familyFor(rstate.modelId);
    return f === "seed_locked" || f === "happy_oyster";
  }

  async function cmd(name, data) {
    // Never send a command the model doesn't advertise — skip it cleanly (and
    // announce the skip) instead of triggering a command_error round-trip. But
    // a family's own native commands are always allowed even if capabilities
    // didn't list them (the LingBot movement axes; Happy Oyster's build/travel/
    // steer commands) — the docs guarantee them for those models.
    const familyNative =
      (MOVE_COMMANDS.has(name) && familyDrivesCamera()) ||
      (HAPPY_OYSTER_COMMANDS.has(name) && isHappyOyster());
    if (!supportsCmd(name) && !familyNative) {
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
      // Happy Oyster held move/look carry a `direction`; interact carries an
      // `action` verb. Surface those so the WORLD MODEL log reads cleanly.
      else if (typeof data.direction === "string") value = data.direction;
      else if (typeof data.action === "string") value = data.action;
    }
    emitEvent("command_sent", {
      command: name,
      prompt: (data && typeof data.prompt === "string") ? data.prompt : null,
      // Happy Oyster anchors on first_frame_image_url (a URL, not an uploaded
      // FileRef); count either as "carries an image" for the inspector.
      hasImage: !!(data && (data.image || data.first_frame_image_url)),
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

  // Resolve a guide still to an absolute, publicly reachable URL for Happy
  // Oyster's first_frame_image_url (the model fetches it server-side, so a
  // relative path won't do). data: URLs (used by tests/mocks) pass through.
  function absoluteImageUrl(url) {
    if (!url) return "";
    if (/^(https?:|data:)/i.test(url)) return url;
    try { return new URL(url, location.href).href; } catch (_) { return url; }
  }

  // Enter a world once it's built: wait for a ready world_state, start travelling,
  // and hand off to the reveal machinery. Shared by create_world + attach_world.
  async function enterHappyOysterWorld(s) {
    rstate.started = true;
    rstate.hoTraveling = true;
    rstate.lastPrompt = s ? s.prompt : rstate.lastPrompt;
    if (s && s.imageUrl) rstate.lastImageUrl = s.imageUrl;
    emitEvent("stage_started", { prompt: rstate.lastPrompt });
    applyMoveState(); // re-assert any held camera drive / verb onto the fresh world
    armFreezeReveal();
    armRevealWatchdog();
  }

  // Happy Oyster: prompt-to-world. Build a world from the scene prompt (anchored
  // by our still as first_frame_image_url), wait for it to be ready, then travel
  // it. Adventure worlds are then driven by held movement/look + interaction
  // verbs; Directing worlds by `instruct` + playback (pause/resume/rewind).
  async function buildHappyOysterWorld(s) {
    // Revisit: reopen with attach_world instead of regenerating ONLY when we've
    // already built a world for this EXACT scene (same guide image AND same
    // prompt) this session — faster and identical (worlds are permanent). Keying
    // on the prompt too is essential: the same image URL with a NEW prompt is a
    // narrative update at the same spot and MUST rebuild, not reopen the old world.
    const cached = s.imageUrl ? rstate.hoWorldsByImage[s.imageUrl] : null;
    if (cached && cached.id && cached.prompt === s.prompt) {
      log("happy-oyster: revisiting a built world -> attach_world");
      return attachHappyOysterWorld(cached.id, s);
    }
    // New stage boundary: invalidate any seed still decoding from a prior world.
    rstate.seedToken++;
    if (!rstate.freezeActive && s.imageUrl) paintSeedToFreeze(s.imageUrl);
    rstate.stagingGuideUrl = s.imageUrl || null;
    rstate.hoStagingPrompt = s.prompt || null;
    rstate.hoTraveling = false;
    const experience = happyOysterExperience();
    const payload = { prompt: s.prompt };
    const ff = absoluteImageUrl(s.imageUrl);
    if (ff) payload.first_frame_image_url = ff;
    if (experience === "director") {
      // Directing worlds: resolution / layout / narrative shape the scene.
      const dp = directorParams();
      payload.resolution = dp.resolution;
      payload.layout = dp.layout;
      payload.narrative = dp.narrative;
    } else {
      payload.perspective = happyOysterPerspective();
    }
    // create_world resolves (per the docs) once the world is ready to enter,
    // reported by a world_state message — wait for it so start_travel enters a
    // built world instead of racing an empty one.
    const worldReady = waitForEvent("world_ready", WORLD_BUILD_TIMEOUT_MS);
    await cmd("create_world", payload);
    await worldReady;
    await cmd("start_travel", {});
    await enterHappyOysterWorld(s);
    log("happy-oyster: world built + travelling (" + experience + "/" +
        (experience === "director" ? directorParams().narrative : happyOysterPerspective()) + ")");
    return true;
  }

  async function establishHappyOyster(s) { return buildHappyOysterWorld(s); }

  // Reopen a previously-built world by its encrypted id (skips the build step).
  // Used when we revisit a place we've already generated — continuity + speed.
  // Also used when leaving a Conversation Moment that temporarily re-anchored
  // the session onto a character portrait.
  async function attachHappyOysterWorld(worldId, s) {
    if (!worldId) return false;
    if (!isHappyOyster()) return false;
    try {
      // attach_world while still travelling another world (e.g. the character
      // portrait world from TALK) leaves the stream stuck on that prior world
      // with the HUD back — the "frozen on the person after conversation" bug.
      // Mirror applyRunningHappyOyster's rebuild teardown before attaching.
      if (rstate.started || rstate.hoTraveling) {
        beginSceneFade();
        captureVideoToFreeze();
        try { await cmd("stop", {}); } catch (_) {}
        rstate.hoSentMove = null;
        rstate.hoSentLook = null;
        rstate.hoSentVerb = null;
        rstate.started = false;
        rstate.hoTraveling = false;
        rstate.lastPrompt = null;
      }
      rstate.seedToken++;
      if (!rstate.freezeActive && s && s.imageUrl) paintSeedToFreeze(s.imageUrl);
      rstate.stagingGuideUrl = (s && s.imageUrl) || null;
      rstate.hoStagingPrompt = (s && s.prompt) || null;
      rstate.hoTraveling = false;
      const worldReady = waitForEvent("world_ready", WORLD_BUILD_TIMEOUT_MS);
      await cmd("attach_world", { encrypted_world_id: worldId });
      // world_ready may time out (resolve null) on a slow reopen — still try
      // start_travel; enterHappyOysterWorld arms the reveal either way.
      await worldReady;
      await cmd("start_travel", {});
      await enterHappyOysterWorld(s);
      log("happy-oyster: attached saved world + travelling");
      return true;
    } catch (err) {
      log("happy-oyster: attach_world failed", err);
      try { clearSceneFade(); } catch (_) {}
      return false;
    }
  }

  async function applyRunningHappyOyster(s, ctx) {
    // Directing experience: the world stays live and text `instruct` steers the
    // unfolding scene — a prompt-only change re-steers in place (NO rebuild),
    // exactly like a live prompt edit. A new guide image / hard transition still
    // rebuilds (it's a new place to compose the story around).
    if (happyOysterExperience() === "director" && !ctx.newGuideImage && !s.hardTransition) {
      if (s.prompt === rstate.lastPrompt) return true;
      await cmd("instruct", { content: s.prompt });
      rstate.lastPrompt = s.prompt;
      log("happy-oyster/director: instructed", s.prompt.slice(0, 80));
      return true;
    }
    // Adventure worlds are FIXED once built (no live prompt edit), so any new
    // scene — a fresh guide image, a hard transition, or a materially changed
    // prompt — is a NEW WORLD. Fade the scene down for the deliberate "moment of
    // pause" beat, freeze the last live frame beneath as the safety floor,
    // release held controls, and rebuild.
    const rebuild = ctx.newGuideImage || s.hardTransition || (s.prompt !== rstate.lastPrompt);
    if (!rebuild) return true; // nothing materially changed — keep travelling
    beginSceneFade();
    captureVideoToFreeze();
    try { await cmd("stop", {}); } catch (_) {}
    rstate.hoSentMove = null;
    rstate.hoSentLook = null;
    rstate.hoSentVerb = null; // the fresh world holds nothing (hoHeldVerb re-asserts)
    rstate.started = false;
    rstate.hoTraveling = false;
    rstate.lastPrompt = null;
    const ok = await buildHappyOysterWorld(s);
    if (ok) log(s.hardTransition ? "happy-oyster: hard transition -> new world" : "happy-oyster: rebuilt world on new scene");
    // Couldn't rebuild — lift the fade so the frozen last frame stays visible.
    else clearSceneFade();
    return ok;
  }

  // Drivers are keyed by PROTOCOL FAMILY, not by model id, so every current and
  // future Reactor model maps onto one of these without a bespoke driver. Happy
  // Oyster is its own build/travel family; the LingBot pair is "seed_locked";
  // the Helios pair is "blend" and is also the default any unknown/new model
  // falls back to.
  const DRIVERS = {
    "happy_oyster": { establish: establishHappyOyster, applyRunning: applyRunningHappyOyster },
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
      // Re-queue for a LATER retry, never an immediate one. Re-queuing without
      // also marking this deferred used to fall straight through to the
      // "drain the queue" call at the end of this function, which re-entered
      // flush() with no delay and no attempt counter: one repeatedly-throwing
      // sendCommand became unbounded microtask recursion that starved the
      // timer/render/input queues and wedged the whole tab. Route throws
      // through the SAME bounded deferral path as a refusal.
      if (rstate.pending == null) {
        deferred = true;
      }
    } finally {
      rstate.applying = false;
    }

    if (deferred) {
      // Couldn't start. Retry after a short delay — unless a newer scene has
      // already been queued — so we don't spin in a tight failing loop.
      //
      // EVERY deferral counts, including ones for a scene that carried no image
      // at all. Only counting image-bearing attempts meant a scene whose still
      // was content-filtered or failed to generate (the engine emits that beat
      // with a prompt and NO image_url) retried forever on a seed-locked model:
      // ~1 attempt/second, no cap, no message, and a permanently black stream
      // because LingBot cannot start without a seed. That is the "it just draws
      // black" failure — the retry has to be able to give up and say so.
      if (rstate.pending == null) {
        const fails = (s._guideFails || 0) + 1;
        const fam = familyFor(rstate.modelId);
        // Two different waits. A still we HAVE but can't upload (swept/404 PNG)
        // is broken now and won't heal, so bail fast. NO still yet just means
        // the render is still in flight — the intro image legitimately takes
        // tens of seconds — so wait generously before declaring it lost.
        const budget = s.imageUrl ? GUIDE_UPLOAD_MAX_ATTEMPTS : GUIDE_WAIT_MAX_ATTEMPTS;
        let next = Object.assign({}, s, { _guideFails: fails });
        if (fails >= budget) {
          if (fam === "seed_locked") {
            log("no usable guide image after " + fails + " attempts — giving up (seed-locked needs a still)");
            try { clearSceneFade(); } catch (_) {}
            // Tell the app the live world can NEVER start for this scene, so it
            // can fall back to stills instead of holding a black frame forever.
            emitEvent("needs_seed_image", { attempts: fails, hadImage: !!s.imageUrl });
            return;
          }
          log("guide image unavailable — retrying prompt-only");
          next = Object.assign({}, s, { imageUrl: null, _guideFails: 0 });
        }
        rstate.pending = next;
        setTimeout(() => { if (!rstate.started) flush(); }, GUIDE_RETRY_MS);
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
    const imageUrl = scene.imageUrl || null;
    const hard = !!scene.hardTransition;
    // DEDUPE redundant re-applies. The standalone layer re-applies the CURRENT
    // scene on assorted polls/updates, and on a seed-locked model (LingBot)
    // every apply carrying a "new" guide image is a full world RESET. Re-applying
    // an UNCHANGED scene while the world is already live therefore caused endless
    // re-staging — the flashing / "never settles" / black re-anchor loop. So if
    // the incoming scene is identical to the one already applied AND the live
    // video is genuinely on screen, do nothing. This is gated on isShowing() so a
    // reconnect / model-swap re-apply (which runs while NOT showing) still
    // re-establishes the world.
    const v = rstate.video || document.getElementById("reactor-video");
    const showing = !!(v && v.videoWidth > 0 && !rstate.freezeActive && !rstate.blackout);
    if (!hard && rstate.started && showing &&
        rstate._appliedPrompt === scene.prompt && rstate._appliedImageUrl === imageUrl) {
      return;
    }
    rstate._appliedPrompt = scene.prompt;
    rstate._appliedImageUrl = imageUrl;
    rstate.pending = {
      prompt: scene.prompt,
      imageUrl: imageUrl,
      hardTransition: hard,
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
    // Happy Oyster: world_state is the authoritative snapshot. create_world
    // resolves once the world is ready to enter — anything past the build phases
    // ("no_world"/"creating"/"building") means we can start_travel. When ready,
    // announce the anchoring first-frame image the same way seed models announce
    // an accepted guide image.
    if (t === "world_state") {
      const phase = d && d.phase;
      // Save the world's id so we can reopen it later with attach_world — both as
      // the current id and keyed by the scene image that built it (revisit cache).
      if (d && d.encrypted_world_id && d.encrypted_world_id !== rstate.hoWorldId) {
        rstate.hoWorldId = d.encrypted_world_id;
        const img = rstate.stagingGuideUrl || rstate.lastImageUrl || null;
        if (img) rstate.hoWorldsByImage[img] = {
          id: d.encrypted_world_id,
          prompt: rstate.hoStagingPrompt || rstate.lastPrompt || "",
        };
        try {
          if (typeof window.ReactorRenderer.onWorldId === "function")
            window.ReactorRenderer.onWorldId(d.encrypted_world_id, d);
        } catch (_) {}
      }
      const stillBuilding = phase === "no_world" || phase === "creating" || phase === "building";
      if (!stillBuilding) {
        resolveWaiters("world_ready", d);
        const url = rstate.stagingGuideUrl || rstate.lastImageUrl || null;
        if (url) {
          rstate.guideImageUrl = url;
          rstate.stagingGuideUrl = null;
          emitGuideImage(url, d);
        }
      }
    }
    // Happy Oyster: travel_state advertises this world's interaction verbs
    // (character + environment actions) for interact({action}). Combine them with
    // the built-in verbs and surface the set so the UI can offer real actions.
    else if (t === "travel_state") {
      const advertised = []
        .concat(Array.isArray(d.character_actions) ? d.character_actions : [])
        .concat(Array.isArray(d.environment_actions) ? d.environment_actions : []);
      const verbs = advertised.map(String).filter(Boolean);
      const changed = verbs.join("|") !== (rstate.hoVerbs || []).join("|");
      rstate.hoVerbs = verbs;
      if (changed) {
        try {
          if (typeof window.ReactorRenderer.onInteractVerbs === "function")
            window.ReactorRenderer.onInteractVerbs(getAllInteractVerbs(), d);
        } catch (_) {}
      }
    }
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
      rstate.connectedAt = Date.now();
      rstate.lastError = null;
      resetTelemetry(); // a fresh connection starts a fresh frame count
      return true;
    } catch (err) {
      const classified = classifyConnectError(err);
      log("enable failed", classified.capacity ? "(capacity)" : "", err);
      rstate.lastError = classified;
      emitEvent("connect_error", classified);
      rstate.connecting = false;
      setStatus("error");
      await disable();
      return false;
    }
  }

  // Report connected seconds to the cost ledger (fire-and-forget) whenever a
  // live session ends — model swap, disable, error cleanup, or page unload.
  // Reads rstate.modelId/connectedAt BEFORE the caller clears them, so this
  // must run first thing in any teardown path (see teardownSession/disable).
  function reportUsage() {
    const connectedAt = rstate.connectedAt;
    rstate.connectedAt = 0;
    if (!connectedAt) return;
    const seconds = (Date.now() - connectedAt) / 1000;
    if (seconds < 1) return; // not worth a row
    const sessionId = window.__SOMEWHERE_SESSION__ || "default";
    const model = rstate.modelId || "default";
    try {
      const body = JSON.stringify({ session_id: sessionId, model: model, duration_seconds: seconds });
      if (navigator.sendBeacon) {
        navigator.sendBeacon("/api/reactor/usage", new Blob([body], { type: "application/json" }));
      } else {
        fetch("/api/reactor/usage", { method: "POST", headers: { "Content-Type": "application/json" }, body: body, keepalive: true }).catch(() => {});
      }
    } catch (_) {}
  }

  // Tear down the current Reactor session WITHOUT clearing the freeze cover or
  // the chosen model — used by a live model swap so the last frame stays on
  // screen while we reconnect to the other world model.
  async function teardownSession() {
    reportUsage();
    rstate.ready = false;
    rstate.started = false;
    rstate.paused = false;
    rstate.hoTraveling = false;
    rstate.hoSentMove = null;
    rstate.hoSentLook = null;
    rstate.lastPrompt = null;
    rstate.lastImageUrl = null;
    rstate.lastRef = null;
    rstate.stagingGuideUrl = null;
    rstate.frameWatch = false;
    stopFrameWatchListeners();
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
      // NOTE: rstate.modelId is intentionally NOT updated to `id` yet —
      // teardownSession() below reports usage for the model that's actually
      // been connected this whole time, so it must still see the OLD id.
      try { localStorage.setItem("world_model", id); } catch (_) {}
      log("switching world model ->", id, "(" + modelNameFor(id) + ")");
      emitEvent("model_switching", { model: id, label: modelLabel(id) });
      if (typeof window.ReactorRenderer.onModel === "function") {
        try { window.ReactorRenderer.onModel(id, modelLabel(id)); } catch (_) {}
      }
      if (wasActive) {
        await teardownSession();
        rstate.modelId = id; // now safe to switch — enable() below reads it
        // Queue the current scene so the new session applies it once ready.
        if (scene) rstate.pending = scene;
        const ok = await enable(); // reconnects with the new modelId
        if (ok && rstate.ready) await flush();
        return ok;
      }
      rstate.modelId = id; // not connected yet — just remember the choice
      return true;
    } finally {
      rstate.swapping = false;
    }
  }

  async function disable() {
    reportUsage();
    rstate.ready = false;
    rstate.started = false;
    rstate.paused = false;
    rstate.hoTraveling = false;
    rstate.pending = null;
    rstate.lastPrompt = null;
    rstate.lastImageUrl = null;
    rstate.lastRef = null;
    rstate.stagingGuideUrl = null;
    rstate.frameWatch = false;
    stopFrameWatchListeners();
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
    rstate.hoTraveling = false;
    // A fresh run builds brand-new worlds — drop the revisit cache + saved id.
    rstate.hoWorldsByImage = Object.create(null);
    rstate.hoWorldId = null;
    if (rstate.status === "live") setStatus("connecting");
    if (!rstate.reactor || !rstate.ready) return;
    // Happy Oyster has no `reset`; releasing held controls (`stop`) is the clean
    // wipe — the fresh run builds a brand-new world. Other families reset.
    try { await cmd(isHappyOyster() ? "stop" : "reset", {}); } catch (err) { log("reset failed", err); }
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

  // ── Live camera drive (native world-model navigation) ───────────────────────
  // The world model is navigable in real time — this is the channel that
  // actually MOVES the camera (a prompt "camera moves" beat does not). Two
  // native schemes exist, and setAxis()/stopMotion() present ONE stable surface
  // over both so the standalone joystick/WSAD code never has to care which model
  // is live:
  //   • LingBot (seed_locked): persistent-state AXES. Each axis holds its value
  //     across chunks until changed — set on press, "idle" on release.
  //     set_move_longitudinal / set_move_lateral / set_look_horizontal /
  //     set_look_vertical, plus set_rotation_speed_deg for turn rate.
  //   • Happy Oyster (happy_oyster): HELD single-direction move + look commands
  //     (move: Front/Back/Left/Right; look: Mouse_Up/Down/Left/Right). They keep
  //     applying until released, and only a global `stop` releases them — so we
  //     diff the desired axes into at-most-one move + one look direction and
  //     re-issue `stop` when a held channel goes idle. (No turn-rate knob.)
  const AXIS_CMD = {
    longitudinal: { cmd: "set_move_longitudinal", param: "move_longitudinal" },
    lateral:      { cmd: "set_move_lateral",      param: "move_lateral" },
    lookH:        { cmd: "set_look_horizontal",   param: "look_horizontal" },
    lookV:        { cmd: "set_look_vertical",     param: "look_vertical" },
  };

  // Map the abstract axis values the standalone layer sends onto Happy Oyster's
  // held move / look directions. Only one move and one look direction can be
  // held at a time; longitudinal wins over strafe if both are somehow set.
  function hoMoveDirection() {
    const m = rstate.move;
    if (m.longitudinal === "forward") return "Front";
    if (m.longitudinal === "back") return "Back";
    if (m.lateral === "left") return "Left";
    if (m.lateral === "right") return "Right";
    return null;
  }
  function hoLookDirection() {
    const m = rstate.move;
    if (m.lookH === "left") return "Mouse_Left";
    if (m.lookH === "right") return "Mouse_Right";
    if (m.lookV === "up") return "Mouse_Up";
    if (m.lookV === "down") return "Mouse_Down";
    return null;
  }

  // Reconcile the desired held controls (move + look + one held interaction verb,
  // e.g. Sprint/Crouch) with what Happy Oyster is currently holding. Because
  // `stop` releases EVERY held control, when any held channel drops we stop once
  // and re-assert whatever is still held.
  function pushHappyOysterMotion() {
    if (!rstate.reactor || !rstate.ready || !rstate.started) return;
    const mv = hoMoveDirection();
    const lk = hoLookDirection();
    const vb = rstate.hoHeldVerb || null;
    if (mv === rstate.hoSentMove && lk === rstate.hoSentLook && vb === rstate.hoSentVerb) return;
    if (!mv && !lk && !vb) {
      if (rstate.hoSentMove || rstate.hoSentLook || rstate.hoSentVerb) cmd("stop", {});
      rstate.hoSentMove = null; rstate.hoSentLook = null; rstate.hoSentVerb = null;
      return;
    }
    // A previously-held channel just went idle OR a held verb SWITCHED to a
    // different one (but not everything released): `stop` clears them all — the
    // only way Happy Oyster releases a held control — then we re-assert the
    // survivor(s) below. (Switching Sprint->Crouch without a stop would leave
    // Sprint engaged server-side alongside Crouch.)
    if ((rstate.hoSentMove && !mv) || (rstate.hoSentLook && !lk) ||
        (rstate.hoSentVerb && rstate.hoSentVerb !== vb)) {
      cmd("stop", {});
      rstate.hoSentMove = null; rstate.hoSentLook = null; rstate.hoSentVerb = null;
    }
    if (mv && mv !== rstate.hoSentMove) { cmd("move", { direction: mv }); rstate.hoSentMove = mv; }
    if (lk && lk !== rstate.hoSentLook) { cmd("look", { direction: lk }); rstate.hoSentLook = lk; }
    if (vb && vb !== rstate.hoSentVerb) { cmd("interact", { action: vb }); rstate.hoSentVerb = vb; }
  }

  // Does the active world model navigate the camera natively? Happy Oyster and
  // LingBot / LingBot World 2 do; blend-family models (Helios, LongLive, SANA)
  // generally don't, so callers fall back to a prompt nudge there. When
  // capabilities haven't arrived yet we optimistically say yes (they load before
  // the first command, and cmd() skips anything genuinely unsupported).
  function motionSupported() {
    // Directing worlds have no movement/look — steering is text `instruct`.
    if (isHappyOyster() && happyOysterExperience() === "director") return false;
    return familyDrivesCamera() || !knownCaps() || rstate.commandSet.has("set_move_longitudinal");
  }

  // Re-assert the currently-held drive after a (re)start / world rebuild, which
  // clears the model's movement state — so if the player is still holding a
  // direction we send it again for the fresh stage/world.
  function applyMoveState() {
    if (!rstate.reactor || !rstate.ready || !rstate.started) return;
    if (isHappyOyster()) {
      rstate.hoSentMove = null; rstate.hoSentLook = null; rstate.hoSentVerb = null; // fresh world holds nothing
      // Director worlds have no move/look/interact — never re-assert Adventure
      // controls onto them (residual held keys/verbs would be sent as commands
      // the world rejects). Steering there is text `instruct` + playback.
      if (happyOysterExperience() === "director") { rstate.hoHeldVerb = null; return; }
      pushHappyOysterMotion();
      return;
    }
    const m = rstate.move;
    if (typeof m.rotationDeg === "number") cmd("set_rotation_speed_deg", { rotation_speed_deg: m.rotationDeg });
    Object.keys(AXIS_CMD).forEach((k) => {
      if (m[k] && m[k] !== "idle") {
        const a = AXIS_CMD[k]; const d = {}; d[a.param] = m[k];
        cmd(a.cmd, d);
      }
    });
  }

  // Set one movement/look axis. Deduped (no point resending the same value) and
  // deferred until generation is running (the value is remembered and re-applied
  // by applyMoveState() once it starts). Translates to the active model's native
  // scheme — LingBot axes or Happy Oyster held move/look.
  function setAxis(axis, value) {
    const a = AXIS_CMD[axis];
    if (!a) return false;
    value = value || "idle";
    if (rstate.move[axis] === value) return true;
    rstate.move[axis] = value;
    if (isHappyOyster()) { pushHappyOysterMotion(); return true; }
    if (!rstate.reactor || !rstate.ready || !rstate.started) return false;
    const d = {}; d[a.param] = value;
    cmd(a.cmd, d);
    return true;
  }

  // Batch-set several movement/look axes and reconcile ONCE. For Happy Oyster
  // this collapses a whole joystick tick into a single held-control reconcile
  // (instead of one per axis), so a diagonal change never emits a transient
  // stop→re-assert flurry — smoother, fewer commands. Legacy models keep their
  // per-axis command behavior. `axes` = { longitudinal?, lateral?, lookH?, lookV? }.
  function setAxes(axes) {
    if (!axes) return;
    if (isHappyOyster()) {
      let changed = false;
      Object.keys(AXIS_CMD).forEach((k) => {
        if (typeof axes[k] === "string" && rstate.move[k] !== axes[k]) { rstate.move[k] = (axes[k] || "idle"); changed = true; }
      });
      if (changed) pushHappyOysterMotion();
      return;
    }
    Object.keys(AXIS_CMD).forEach((k) => { if (typeof axes[k] === "string") setAxis(k, axes[k]); });
  }

  // Rotation speed (deg/latent-frame, 0..30) — how fast a held look axis turns.
  // Happy Oyster has no turn-rate knob (its look is a plain held direction), so
  // this is a no-op there.
  function setRotationSpeed(deg) {
    if (isHappyOyster()) return;
    deg = Math.max(0, Math.min(30, Number(deg) || 0));
    // Quantize a little so tiny analog jitters don't spam identical commands.
    deg = Math.round(deg * 2) / 2;
    if (rstate.move.rotationDeg === deg) return;
    rstate.move.rotationDeg = deg;
    if (!rstate.reactor || !rstate.ready || !rstate.started) return;
    cmd("set_rotation_speed_deg", { rotation_speed_deg: deg });
  }

  // Idle every axis — the camera comes to rest (held state means we MUST release
  // or it keeps moving after the player lets go).
  function stopMotion() {
    if (isHappyOyster()) {
      rstate.move.longitudinal = "idle"; rstate.move.lateral = "idle";
      rstate.move.lookH = "idle"; rstate.move.lookV = "idle";
      pushHappyOysterMotion(); // no held direction remains -> emits a single stop
      return;
    }
    setAxis("longitudinal", "idle");
    setAxis("lateral", "idle");
    setAxis("lookH", "idle");
    setAxis("lookV", "idle");
  }

  // Perform a MOMENTARY interaction verb (Happy Oyster) — e.g. Jump, Attack, or a
  // world-advertised verb. Any verb string is accepted. Fire-and-forget: it holds
  // until the next `stop` (which the movement reconciliation issues on the next
  // input change). Returns true if sent.
  function interact(action) {
    action = (action == null ? "" : String(action)).trim();
    if (!action) return false;
    if (!isHappyOyster()) return false;
    if (!rstate.reactor || !rstate.ready || !rstate.started) return false;
    cmd("interact", { action: action });
    return true;
  }

  // Set (or clear, with null) the HELD interaction verb — e.g. Sprint or Crouch,
  // which stay engaged while a key/button is held. It composes with move+look and
  // is re-asserted after any `stop`/rebuild via the motion reconciliation.
  function setHeldVerb(action) {
    if (!isHappyOyster()) return false;
    const v = action ? String(action).trim() : null;
    if (rstate.hoHeldVerb === v) return true;
    rstate.hoHeldVerb = v || null;
    pushHappyOysterMotion();
    return true;
  }

  // The full interaction-verb set for the current world: the built-ins Happy
  // Oyster always accepts, plus whatever this world advertised via travel_state
  // (deduped, built-ins first).
  function getAllInteractVerbs() {
    const seen = Object.create(null);
    const out = [];
    HAPPY_OYSTER_BUILTIN_VERBS.concat(rstate.hoVerbs || []).forEach((v) => {
      const s = (v == null ? "" : String(v)).trim();
      const key = s.toLowerCase();
      if (s && !seen[key]) { seen[key] = 1; out.push(s); }
    });
    return out;
  }

  // ── Directing experience: text-steer + playback ─────────────────────────────
  // Directing worlds have no movement/interact; you steer the unfolding scene
  // with text and control playback. These are no-ops unless the live model is a
  // Happy Oyster session opened in the "director" experience.
  function instruct(content) {
    content = (content == null ? "" : String(content)).trim();
    if (!content || !isHappyOyster()) return false;
    if (!rstate.reactor || !rstate.ready || !rstate.started) return false;
    cmd("instruct", { content: content });
    rstate.lastPrompt = content;
    return true;
  }
  function rewind(seconds) {
    if (!isHappyOyster()) return false;
    if (!rstate.reactor || !rstate.ready) return false;
    // Docs: rewind_to_sec is rounded down to a multiple of 4; playback resumes.
    const sec = Math.max(0, Math.floor((Number(seconds) || 0) / 4) * 4);
    cmd("rewind", { rewind_to_sec: sec });
    rstate.paused = false;
    return true;
  }

  // Drop the tracked drive state (a fresh run / teardown starts from rest).
  function resetMoveState() {
    rstate.move = { longitudinal: "idle", lateral: "idle", lookH: "idle", lookV: "idle", rotationDeg: null };
    rstate.hoSentMove = null;
    rstate.hoSentLook = null;
    rstate.hoSentVerb = null;
    rstate.hoHeldVerb = null;
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
    motionSupported, setAxis, setAxes, setRotationSpeed, stopMotion,
    // Happy Oyster can only HOLD ONE look direction at a time (its look is a
    // held Mouse_* verb, and hoLookDirection picks a single winner), so a
    // diagonal has to be time-sliced by the caller. LingBot has independent
    // horizontal/vertical axes and can hold a true diagonal.
    looksOneAxisAtATime: () => isHappyOyster(),
    // Interaction verbs (Happy Oyster): interact(action) fires a momentary verb
    // (Jump/Attack or a world-advertised verb); setHeldVerb(action|null) holds a
    // verb (Sprint/Crouch) that composes with movement. canInteract() tells the
    // standalone layer whether the live model takes verbs (so it can route
    // INTERACT to a real command instead of a prompt nudge). getInteractVerbs()
    // returns the full verb set this world accepts (built-ins + advertised), and
    // getAdvertisedVerbs() just the ones travel_state advertised.
    interact, setHeldVerb,
    canInteract: () => isHappyOyster() && rstate.started && happyOysterExperience() !== "director",
    getInteractVerbs: () => getAllInteractVerbs(),
    getAdvertisedVerbs: () => (rstate.hoVerbs || []).slice(),
    // Directing experience: steer with text + control playback. No-ops unless the
    // session was opened in the "director" experience.
    instruct, rewind,
    // Reopen a previously-built world by id (skips the build). Returns a promise.
    attachWorld: (id, scene) => attachHappyOysterWorld(id, scene || rstate.lastSceneApplied || null),
    getWorldId: () => rstate.hoWorldId || null,
    // Force the current Happy Oyster world to rebuild — used after changing a
    // session-fixed knob (perspective / experience) so the new setting takes
    // effect. Drops the revisit cache for this scene so it genuinely regenerates.
    rebuildWorld: () => {
      if (!isHappyOyster() || !rstate.started) return false;
      const s = rstate.lastSceneApplied;
      if (!s || !s.prompt) return false;
      if (s.imageUrl && rstate.hoWorldsByImage[s.imageUrl]) delete rstate.hoWorldsByImage[s.imageUrl];
      rstate.lastImageUrl = null; // force a new-guide-image rebuild via applyRunning
      rstate.pending = { prompt: s.prompt, imageUrl: s.imageUrl || null, hardTransition: true };
      flush();
      return true;
    },
    // Happy Oyster session shape (experience + perspective). Setting persists to
    // localStorage and takes effect on the NEXT world build (fixed per session).
    getExperience: () => (isHappyOyster() ? happyOysterExperience() : null),
    getPerspective: () => (isHappyOyster() ? happyOysterPerspective() : null),
    setExperience: (v) => {
      v = (v === "director" || v === "directing") ? "director" : "adventure";
      rstate.hoExperience = v;
      try { localStorage.setItem("happy_oyster_experience", v); } catch (_) {}
      return v;
    },
    setPerspective: (v) => {
      v = (v === "third_person") ? "third_person" : "first_person";
      rstate.hoPerspective = v;
      try { localStorage.setItem("happy_oyster_perspective", v); } catch (_) {}
      return v;
    },
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
    // The reason the last connect attempt failed (see classifyConnectError),
    // or null if the last attempt succeeded / none has happened yet. Lets the
    // UI distinguish a transient upstream capacity shortage (Reactor has no
    // free server for this model right now) from other failures so it can be
    // more patient and explain what's happening instead of a generic error.
    getLastError: () => rstate.lastError,
    // Measured stream health: presented frames, fps, and whether the picture
    // has frozen while still reporting "live". Status alone can't tell those
    // apart — a stalled stream keeps showing its last decoded frame.
    getTelemetry: getTelemetry,
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
    // Can the ACTIVE model take a prompt edit on the RUNNING stream? The prompt
    // families can (set_prompt / schedule_prompt / set_shot), and so can Happy
    // Oyster's Directing experience (instruct). A Happy Oyster ADVENTURE world
    // is FIXED once built, so a new prompt there means tearing the world down
    // and rebuilding it — the right cost for a turn, the wrong cost for an
    // atmospheric beat. Anything that steers CONTINUOUSLY (see WorldDrift in
    // standalone.js) must check this first or it rebuilds the world on a timer.
    supportsLiveSteer: () =>
      familyFor(rstate.modelId) !== "happy_oyster" || happyOysterExperience() === "director",
    // True only when the LIVE video is actually on-screen and RUNNING — decoded
    // frames are flowing AND nothing is covering/darkening the scene. Besides the
    // freeze back-buffer and a blackout, this also excludes the two "not revealed
    // yet" windows that are visually black but where the <video> already reports
    // videoWidth>0 (from a held/stale frame):
    //   • fadeDownActive — the scene-fade veil is deliberately down (the "moment
    //     of pause" beat of a transition, incl. blend-model re-anchors that lift
    //     via scheduleSceneReveal and never touch the freeze).
    //   • freezeArmed — the stream started but its first genuinely-new frame has
    //     not been presented; we're still showing the old/held frame.
    // This keeps isShowing() in lockstep with the `video_showing` reveal event,
    // so consumers gated on it (e.g. the OCR object-detection hotspots) never
    // trigger over black before the video is actually playing.
    isShowing: () => {
      const v = rstate.video || document.getElementById("reactor-video");
      return !!(
        v && v.videoWidth > 0 &&
        !rstate.freezeActive && !rstate.blackout &&
        !rstate.fadeDownActive && !rstate.freezeArmed
      );
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
    // fn(verbs, data) — fired when the current Happy Oyster world's interaction
    // verbs change (built-ins + travel_state-advertised), so the UI can rebuild
    // the verb bar with real, world-specific actions.
    onInteractVerbs: null,
    // fn(worldId, data) — fired when the current world's encrypted id is known,
    // so the game can save it and reopen the world later with attachWorld.
    onWorldId: null,
  };
})();
