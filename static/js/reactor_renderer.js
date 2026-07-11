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
  const FALLBACK_MODEL = "reactor/lingbot-world-2";
  // How long to wait for the seed image to decode before starting anyway.
  const IMAGE_ACCEPT_TIMEOUT_MS = 6000;
  // After we issue `start`, how long to wait for real decoded video frames
  // before declaring the stream stalled. If this fires, the model accepted our
  // commands but no `main_video` frames arrived — a server/session/model issue,
  // NOT a client bug. The still fallback stays on screen; we log + emit a
  // diagnostic so the exact stall point is visible in the browser console.
  // Overridable (e.g. from tests) via a window global.
  const REVEAL_WATCHDOG_MS = (typeof window !== "undefined" && window.__REACTOR_REVEAL_WATCHDOG_MS__) || 12000;

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
  // very first scene shows the intended composition immediately instead of a
  // black video while the stream warms up.
  function paintSeedToFreeze(imageUrl) {
    const f = getFreeze();
    if (!f || !imageUrl) return;
    const img = new Image();
    img.onload = () => {
      try {
        f.width = img.naturalWidth || 1280;
        f.height = img.naturalHeight || 720;
        f.getContext("2d").drawImage(img, 0, 0, f.width, f.height);
        showFreeze(true);
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
    rstate.freezeArmed = true;
    rstate.freezeArmTs = Date.now();
    if (rstate.freezeFallbackTimer) clearTimeout(rstate.freezeFallbackTimer);
    // Fallback for browsers without requestVideoFrameCallback: reveal after a
    // short grace period. (Modern Safari/Chrome use the frame callback path.)
    if (getVideo() && typeof getVideo().requestVideoFrameCallback !== "function") {
      rstate.freezeFallbackTimer = setTimeout(() => {
        if (rstate.freezeArmed) { rstate.freezeArmed = false; hideFreeze(); emitEvent("video_showing", {}); }
      }, 1800);
    }
  }

  async function loadConfig() {
    try {
      const r = await fetch("/api/reactor/config");
      if (r.ok) rstate.cfg = await r.json();
    } catch (err) { log("config fetch failed, using defaults", err); }
    return rstate.cfg;
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
    if (name !== "main_video") return;
    const video = getVideo();
    if (!video) return;
    video.srcObject = stream || new MediaStream([track]);
    // The video is ALWAYS visible; the freeze back-buffer (canvas) covers it
    // during warmup + re-anchor so there's never a black gap or an underlying
    // still showing through. The freeze fades out once real frames arrive.
    video.classList.remove("hidden");
    video.play().catch(() => {});
    startFrameWatch(video);
    log("main_video attached (freeze covers until first frame)");
  }

  // Called on every presented video frame. Marks the stream live, and — when a
  // reveal is armed — treats the frame as the new scene actually being on
  // screen, so it crossfades the freeze buffer out.
  function onPresentedFrame(video) {
    if (video.videoWidth <= 0) return;
    clearRevealWatchdog(); // frames are flowing — the stream is healthy
    // A full reset hides the video for a clean wipe; genuine frames un-hide it
    // (a re-anchor keeps it visible under the freeze, so this is a no-op there).
    if (video.classList.contains("hidden")) video.classList.remove("hidden");
    if (rstate.status !== "live") setStatus("live");
    if (rstate.freezeArmed) {
      // Ignore stray callbacks in the first beat after arming so we don't reveal
      // the (still-old) frame before the new scene has actually rendered.
      if (Date.now() - rstate.freezeArmTs < 200) return;
      rstate.freezeArmed = false;
      if (rstate.freezeFallbackTimer) { clearTimeout(rstate.freezeFallbackTimer); rstate.freezeFallbackTimer = null; }
      hideFreeze();
      emitEvent("video_showing", {}); // the fresh scene is now on screen
    }
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

  async function cmd(name, data) {
    emitEvent("command_sent", { command: name });
    return rstate.reactor.sendCommand(name, data || {});
  }

  // Stage a fresh run: reference image first (LingBot needs an image before
  // start), then the prompt, then start once the seed has decoded. Chunk 0
  // renders from the still, subsequent chunks are steered by the prompt.
  // Returns true once generation has started, false if it had to defer (no
  // reference image available yet).
  async function establish(s) {
    // Make sure SOMETHING intended is on screen while we stage: if the freeze
    // buffer isn't already covering (i.e. this isn't a re-anchor that grabbed
    // the last live frame), paint the seed guide image so we never show a black
    // video during warmup.
    if (!rstate.freezeActive && s.imageUrl) paintSeedToFreeze(s.imageUrl);

    const ref = await uploadStill(s.imageUrl);
    if (!ref) {
      // LingBot World 2 cannot start without a reference image. Keep the freeze
      // (seed) on screen and retry when a scene with an image arrives.
      log("no reference image yet — deferring start (LingBot requires one)");
      return false;
    }
    // Remember which still we're seeding so the guide-image notification can
    // carry the exact URL once the model reports image_accepted.
    rstate.stagingGuideUrl = s.imageUrl || null;
    const imageReady = waitForEvent("image_accepted", IMAGE_ACCEPT_TIMEOUT_MS);
    await cmd("set_image", { image: ref });
    await cmd("set_prompt", { prompt: s.prompt });
    await imageReady; // let the seed decode so the first chunk starts from it
    await cmd("start", {});
    rstate.started = true;
    rstate.lastPrompt = s.prompt;
    emitEvent("stage_started", { prompt: s.prompt });
    armFreezeReveal();   // crossfade the freeze out on the first NEW frame
    armRevealWatchdog(); // surface it loudly if no frames ever arrive
    log("generation started (image-conditioned)");
    return true;
  }

  // Apply the most recent pending scene, honoring LingBot's command ordering.
  async function flush() {
    if (rstate.applying) return;
    if (!rstate.reactor || !rstate.ready || rstate.pending == null) return;
    const s = rstate.pending;
    rstate.pending = null;
    if (!s.prompt) return;

    // A still we haven't staged yet is a NEW guide image — a re-anchor boundary.
    // LingBot's reference image is locked once a run starts (set_image mid-run is
    // ignored), so the only way to force the video onto this exact frame at full
    // strength is a fresh stage. Steering-only updates (instant action beats)
    // carry no image and fall through to a prompt hot-swap.
    const newGuideImage = !!(s.imageUrl && s.imageUrl !== rstate.lastImageUrl);

    // Dedupe pure re-sends of the same prompt while already running — but never
    // skip a new guide image or an explicit location change.
    if (rstate.started && s.prompt === rstate.lastPrompt && !s.hardTransition && !newGuideImage) return;

    rstate.applying = true;
    let deferred = false;
    try {
      if (!rstate.started) {
        deferred = !(await establish(s));
      } else if (s.hardTransition || newGuideImage) {
        // New guide image (every turn that draws one) or a location change.
        // LingBot's reference image is locked for the life of a run, so moving
        // onto a new frame means a fresh stage: reset, then re-establish from
        // the new still + prompt. To make the switch seamless we FREEZE the last
        // live frame onto the back-buffer canvas first (a single-session double
        // buffer) and keep the video visible underneath — so the tear-down never
        // exposes black or the underlying still, and we crossfade to the fresh
        // stream the instant its first new frame arrives.
        captureVideoToFreeze();
        try { await cmd("reset", {}); } catch (err) { log("reset failed", err); }
        rstate.started = false;
        rstate.lastPrompt = null;
        rstate.lastRef = null;
        rstate.lastImageUrl = null;
        deferred = !(await establish(s));
        if (!deferred) log(s.hardTransition ? "hard transition re-staged" : "re-anchored on new guide image");
      } else {
        // Same scene, no new still: hot-swap the prompt; the video evolves
        // continuously (used for instant action injection between turns).
        await cmd("set_prompt", { prompt: s.prompt });
        rstate.lastPrompt = s.prompt;
        log("re-steered:", s.prompt.slice(0, 80));
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
    if (t === "image_accepted") {
      resolveWaiters("image_accepted", d);
      // The guide image is now decoded and anchoring the world — announce it.
      const url = rstate.stagingGuideUrl || rstate.lastImageUrl || null;
      if (url) {
        rstate.guideImageUrl = url;
        rstate.stagingGuideUrl = null;
        emitGuideImage(url, d);
      }
    }
    else if (t === "prompt_accepted") resolveWaiters("prompt_accepted", d);
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

      const reactor = new Reactor({ modelName: rstate.cfg.model_name || FALLBACK_MODEL });
      rstate.reactor = reactor;
      reactor.on("trackReceived", attachTrack);
      reactor.on("statusChanged", async (status) => {
        log("status:", status);
        if (status === "ready") { rstate.ready = true; await flush(); }
        else if (status === "disconnected") { rstate.ready = false; }
      });
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
    clearRevealWatchdog();
    if (rstate.freezeFallbackTimer) { clearTimeout(rstate.freezeFallbackTimer); rstate.freezeFallbackTimer = null; }
    if (rstate.frameWatchTimer) { clearInterval(rstate.frameWatchTimer); rstate.frameWatchTimer = null; }
    const video = getVideo();
    if (video) { video.classList.add("hidden"); try { video.srcObject = null; } catch (_) {} }
    // Drop the freeze cover so the still fallback (image mode) shows cleanly.
    const f = getFreeze();
    if (f) { rstate.freezeActive = false; f.classList.remove("show"); }
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
    clearRevealWatchdog();
    if (rstate.freezeFallbackTimer) { clearTimeout(rstate.freezeFallbackTimer); rstate.freezeFallbackTimer = null; }
    // A game reset is a CLEAN WIPE (unlike a per-turn re-anchor, which freezes
    // the last frame for a seamless switch): drop the freeze cover and hide the
    // video so the dead run's scene is gone immediately. The fresh run paints
    // its seed onto the freeze and reveals its own video when it establishes.
    const f = getFreeze();
    if (f) { rstate.freezeActive = false; f.classList.remove("show"); }
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
    // (that frame isn't the current scene the model is actually rendering).
    if (!v || !v.videoWidth || rstate.freezeActive) return null;
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

  window.ReactorRenderer = {
    enable, disable, applyScene, setPrompt, reset, pause, resume, captureFrame,
    getStatus: () => rstate.status,
    isActive: () => rstate.active,
    isReady: () => rstate.ready,
    // The last guide image actually integrated into the live world model.
    getGuideImage: () => rstate.guideImageUrl || null,
    // True only when the LIVE video is actually on-screen (decoded frames and
    // the freeze back-buffer is not covering it).
    isShowing: () => {
      const v = rstate.video || document.getElementById("reactor-video");
      return !!(v && v.videoWidth > 0 && !rstate.freezeActive);
    },
    onStatus: null,
    onEvent: null,
    // fn(imageUrl, data) — fired when a guide image is integrated into the world.
    onGuideImage: null,
  };
})();
