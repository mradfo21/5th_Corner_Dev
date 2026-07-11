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
  // The base pair of Reactor world models we can switch between live, mid-game
  // (see WORLD_MODEL_SWITCHING_PLAN.md). Each id maps to a DRIVER (its wire
  // protocol) below and to SDK metadata advertised by /api/reactor/config.
  const FALLBACK_MODEL_ID = "lingbot-world-2";
  const FALLBACK_MODEL = "reactor/lingbot-world-2";
  const DEFAULT_MODELS = [
    { id: "lingbot-world-2", label: "LingBot World 2", sdk_name: "reactor/lingbot-world-2", requiresSeedImage: true },
    { id: "helios", label: "Helios", sdk_name: "reactor/helios", requiresSeedImage: false },
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
    lastSceneApplied: null,        // last scene handed to applyScene, for model-swap re-apply
    swapping: false,               // a live model swap is in flight
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
    seedToken: 0,          // bumps every reveal/reset so a slow seed decode that
                           // lands AFTER the stream revealed can't re-cover it
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
      if (r.ok) {
        rstate.cfg = await r.json();
        if (Array.isArray(rstate.cfg.available_models) && rstate.cfg.available_models.length) {
          rstate.models = rstate.cfg.available_models.map((m) => ({
            id: m.id,
            label: m.label || m.id,
            sdk_name: m.sdk_name || m.model_name || m.id,
            requiresSeedImage: !!m.requires_seed_image,
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
  function modelNameFor(id) {
    const m = modelById(id);
    return (m && m.sdk_name) || rstate.cfg.model_name || FALLBACK_MODEL;
  }
  // Resolve the active world model: ?model= > localStorage > server default > fallback.
  function resolveModelId() {
    if (rstate.modelId && knownModel(rstate.modelId)) return rstate.modelId;
    let q = null, stored = null;
    try { q = new URLSearchParams(location.search).get("model"); } catch (_) {}
    try { stored = localStorage.getItem("world_model"); } catch (_) {}
    const pick = (id) => (id && knownModel(id) ? id : null);
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
      rstate.seedToken++; // invalidate any still-decoding seed so it can't re-cover us
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

  // Fetch our own generated still as raw base64 (no data: prefix) for models
  // whose set_image takes an inline image (Helios `image_b64`). Returns null on
  // failure so the caller can proceed text-only.
  async function fetchImageBase64(imageUrl) {
    if (!imageUrl) return null;
    try {
      const resp = await fetch(imageUrl);
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const blob = await resp.blob();
      return await new Promise((resolve) => {
        const fr = new FileReader();
        fr.onload = () => {
          const s = String(fr.result || "");
          const comma = s.indexOf(",");
          resolve(comma >= 0 ? s.slice(comma + 1) : s || null);
        };
        fr.onerror = () => resolve(null);
        fr.readAsDataURL(blob);
      });
    } catch (err) { log("image base64 fetch failed", err); return null; }
  }

  async function cmd(name, data) {
    // Surface the payload (prompt text / whether an image seed rides along) so
    // the world-model inspector can show EXACTLY what we send to the model.
    emitEvent("command_sent", {
      command: name,
      prompt: (data && typeof data.prompt === "string") ? data.prompt : null,
      hasImage: !!(data && data.image),
    });
    return rstate.reactor.sendCommand(name, data || {});
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
    armFreezeReveal();
    armRevealWatchdog();
    log("lingbot: generation started (image-conditioned)");
    return true;
  }

  async function applyRunningLingbot(s, ctx) {
    if (s.hardTransition || ctx.newGuideImage) {
      // A new guide image means a fresh stage (reference image is locked). Freeze
      // the last live frame first so the tear-down never exposes black/the still.
      captureVideoToFreeze();
      try { await cmd("reset", {}); } catch (err) { log("reset failed", err); }
      rstate.started = false;
      rstate.lastPrompt = null;
      rstate.lastRef = null;
      rstate.lastImageUrl = null;
      const ok = await establishLingbot(s);
      if (ok) log(s.hardTransition ? "hard transition re-staged" : "re-anchored on new guide image");
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
    await cmd("schedule_prompt", { prompt: s.prompt, chunk: 0 });
    if (s.imageUrl) {
      const b64 = await fetchImageBase64(s.imageUrl);
      if (b64) {
        rstate.stagingGuideUrl = s.imageUrl;
        const imageReady = waitForEvent("image_accepted", IMAGE_ACCEPT_TIMEOUT_MS);
        await cmd("set_image", { image_b64: b64, transition: "cut" });
        rstate.lastImageUrl = s.imageUrl;
        await imageReady;
      }
    }
    await cmd("start", {});
    rstate.started = true;
    rstate.lastPrompt = s.prompt;
    emitEvent("stage_started", { prompt: s.prompt });
    armFreezeReveal();
    armRevealWatchdog();
    log("helios: generation started");
    return true;
  }

  async function applyRunningHelios(s, ctx) {
    if (ctx.newGuideImage || s.hardTransition) {
      const b64 = await fetchImageBase64(s.imageUrl);
      if (b64) {
        rstate.stagingGuideUrl = s.imageUrl;
        // Blend the new frame in continuously; a hard transition cuts decisively.
        await cmd("set_image", { image_b64: b64, transition: s.hardTransition ? "cut" : "blend" });
        rstate.lastImageUrl = s.imageUrl;
      }
    }
    await cmd("set_prompt", { prompt: s.prompt });
    rstate.lastPrompt = s.prompt;
    log("helios: re-steered", (ctx.newGuideImage || s.hardTransition) ? "(image blended)" : "");
    return true;
  }

  const DRIVERS = {
    "lingbot-world-2": { establish: establishLingbot, applyRunning: applyRunningLingbot },
    "helios": { establish: establishHelios, applyRunning: applyRunningHelios },
  };
  function activeDriver() { return DRIVERS[rstate.modelId] || DRIVERS[FALLBACK_MODEL_ID]; }

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
    else if (t === "prompt_accepted" || t === "prompt_scheduled" || t === "prompt_switched") {
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
    if (!knownModel(id)) { log("unknown world model:", id); return false; }
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
    rstate.seedToken++; // drop any in-flight seed decode from the dead run
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
    rstate.seedToken++; // drop any in-flight seed decode from the dead run
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
    setModel,
    getStatus: () => rstate.status,
    isActive: () => rstate.active,
    isReady: () => rstate.ready,
    // World-model selection API (for the mid-game switcher UI).
    getModel: () => rstate.modelId,
    getModels: () => (rstate.models || []).map((m) => ({
      id: m.id, label: m.label, active: m.id === rstate.modelId,
    })),
    // The last guide image actually integrated into the live world model.
    getGuideImage: () => rstate.guideImageUrl || null,
    // True only when the LIVE video is actually on-screen (decoded frames and
    // the freeze back-buffer is not covering it).
    isShowing: () => {
      const v = rstate.video || document.getElementById("reactor-video");
      return !!(v && v.videoWidth > 0 && !rstate.freezeActive);
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
  };
})();
