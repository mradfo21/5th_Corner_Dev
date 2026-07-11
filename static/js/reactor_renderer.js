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
    showSuppressed: false, // keep the video hidden during reset gaps
    frameWatch: false,
    frameWatchTimer: null,
    applying: false,       // flush() re-entrancy guard (establishing is async)
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
    video.play().catch(() => {});
    // Keep the video hidden until it actually produces decoded frames, so the
    // still image stays on screen until the world model is genuinely "ready to
    // go" — no black or old-frame takeover, no flashing between still and video.
    startFrameWatch(video);
    log("main_video attached (awaiting first frame)");
  }

  // Reveal the video the moment it has real decoded frames (videoWidth > 0),
  // unless we're mid-reset. This is the single hand-off from still -> video.
  function revealIfFrames(video) {
    if (rstate.showSuppressed) return;
    if (video.videoWidth > 0) {
      if (video.classList.contains("hidden")) {
        video.classList.remove("hidden");
        emitEvent("video_showing", {}); // first real frame is on screen
      }
      if (rstate.status !== "live") setStatus("live");
    }
  }

  function startFrameWatch(video) {
    if (rstate.frameWatch) return;
    rstate.frameWatch = true;
    if (typeof video.requestVideoFrameCallback === "function") {
      const cb = () => {
        if (!rstate.reactor) { rstate.frameWatch = false; return; }
        revealIfFrames(video);
        try { video.requestVideoFrameCallback(cb); } catch (_) { rstate.frameWatch = false; }
      };
      try { video.requestVideoFrameCallback(cb); } catch (_) { rstate.frameWatch = false; }
    } else {
      const onp = () => revealIfFrames(video);
      video.addEventListener("playing", onp);
      video.addEventListener("timeupdate", onp);
      rstate.frameWatchTimer = setInterval(() => {
        if (!rstate.reactor) { clearInterval(rstate.frameWatchTimer); rstate.frameWatch = false; return; }
        revealIfFrames(video);
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
    const ref = await uploadStill(s.imageUrl);
    if (!ref) {
      // LingBot World 2 cannot start without a reference image. Keep the still
      // fallback on screen and retry when a scene with an image arrives.
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
    rstate.showSuppressed = false; // allow the video to reveal once frames flow
    await cmd("start", {});
    rstate.started = true;
    rstate.lastPrompt = s.prompt;
    emitEvent("stage_started", { prompt: s.prompt });
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
        // onto a new frame means a fresh stage: reset, hide the stale video
        // during the gap, and re-establish from the new still + prompt. This is
        // the "vignette to vignette" blend — each run is anchored (strength 1)
        // on the exact guide image the engine drew, so the video can't drift.
        try { await cmd("reset", {}); } catch (err) { log("reset failed", err); }
        rstate.started = false;
        rstate.lastPrompt = null;
        rstate.lastRef = null;
        rstate.lastImageUrl = null;
        rstate.showSuppressed = true;
        const v = getVideo();
        if (v) v.classList.add("hidden");
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
    rstate.showSuppressed = false;
    rstate.frameWatch = false;
    if (rstate.frameWatchTimer) { clearInterval(rstate.frameWatchTimer); rstate.frameWatchTimer = null; }
    const video = getVideo();
    if (video) { video.classList.add("hidden"); try { video.srcObject = null; } catch (_) {} }
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
    // Hide the (now-stale) video during the reset gap so the fresh still shows
    // until the new run's first frame is ready — no old-scene bleed-through.
    rstate.showSuppressed = true;
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
    if (!v || !v.videoWidth || v.classList.contains("hidden")) return null;
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
    // True only when the video is actually on-screen with decoded frames — the
    // signal the client uses to stop repainting the still behind it.
    isShowing: () => {
      const v = rstate.video || document.getElementById("reactor-video");
      return !!(v && !v.classList.contains("hidden") && v.videoWidth > 0);
    },
    onStatus: null,
    onEvent: null,
    // fn(imageUrl, data) — fired when a guide image is integrated into the world.
    onGuideImage: null,
  };
})();
