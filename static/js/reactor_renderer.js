/* ============================================================
   SOMEWHERE // Reactor realtime renderer (experimental)

   Drives Reactor's Helios world model as an alternative scene renderer.
   We steer the live video with a clean, video-model-appropriate scene prompt
   (built server-side by build_realtime_prompt) and — following Helios's
   image-to-video guidance — condition the world model on the SAME still the
   game generated so the video matches our intended composition.

   Steering model: IMAGE-TO-IMAGE ON EVERY TURN. Rather than letting Helios
   free-run on text (which drifts into incoherent, out-of-universe video), we
   re-anchor the live stream to the SAME coherent Gemini keyframe the game
   generated each turn, at a high image_strength. The video then only adds
   motion/continuity around our composition instead of inventing its own world.
   Clarity (staying in-universe) is favored over free motion.

   Wire protocol (per Helios schema reference; verified against the live model):
     • establishing shot / new game:
         uploadFile(still) -> set_image_strength(S)
           -> set_conditioning({prompt, image})   (atomic; avoids the first-chunk
              race where start ships before the image lands and the scene
              "corrects itself" a chunk later) -> start
         (falls back to set_prompt -> set_image -> start if set_conditioning is
          unsupported, and to text-only set_prompt -> start if the upload fails)
     • every subsequent turn (same location AND hard_transition):
         uploadFile(still) -> set_image_strength(S) -> set_image({image})
           -> set_prompt({prompt})
         (re-anchor to the fresh keyframe so the video tracks our universe; the
          Helios image swap is an immediate switch at the next chunk boundary)

   image_strength note: Helios snapshots image_strength together with the
   reference image, so a new strength value only takes effect on the NEXT
   set_image. We therefore always send set_image_strength immediately before
   set_image / set_conditioning. Tune live with ?strength=0..1.

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
   ============================================================ */
(function () {
  "use strict";

  const SDK_URL = "https://esm.sh/@reactor-team/js-sdk@2.12.0";
  const FALLBACK_MODEL = "helios";
  // How strongly the reference keyframe anchors the video (0..1). High by
  // default: we want the live video to stay clearly in the SAME universe as our
  // Gemini still (clarity/coherence) and just breathe/animate around it, rather
  // than drift off on its own. Override live for tuning with ?strength=0..1.
  const DEFAULT_IMAGE_STRENGTH = 0.85;
  function resolveImageStrength() {
    try {
      const q = new URLSearchParams(location.search).get("strength");
      if (q != null && q !== "") {
        const v = parseFloat(q);
        if (!isNaN(v)) return Math.max(0, Math.min(1, v));
      }
    } catch (_) {}
    return DEFAULT_IMAGE_STRENGTH;
  }
  const IMAGE_STRENGTH = resolveImageStrength();

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
    supportsUpload: true,
    video: null,
    cfg: { model_name: FALLBACK_MODEL, enabled: false },
    connecting: false,
    status: "off",
    showSuppressed: false, // keep the video hidden during reset gaps
    frameWatch: false,
    frameWatchTimer: null,
  };

  function log() { try { console.log.apply(console, ["[reactor]", ...arguments]); } catch (_) {} }

  function setStatus(s) {
    if (rstate.status === s) return;
    rstate.status = s;
    try {
      if (typeof window.ReactorRenderer.onStatus === "function") window.ReactorRenderer.onStatus(s);
    } catch (_) {}
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
      if (video.classList.contains("hidden")) video.classList.remove("hidden");
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
    return rstate.reactor.sendCommand(name, data || {});
  }

  // Apply the most recent pending scene, honoring Helios's command ordering.
  async function flush() {
    if (!rstate.reactor || !rstate.ready || rstate.pending == null) return;
    const s = rstate.pending;
    rstate.pending = null;
    if (!s.prompt) return;

    // Dedupe true no-ops only: same prompt AND no fresh keyframe to re-anchor on
    // while already running. A new still (s.imageUrl) always re-anchors.
    if (rstate.started && !s.hardTransition && s.prompt === rstate.lastPrompt &&
        (!s.imageUrl || s.imageUrl === rstate.lastImageUrl)) return;

    try {
      if (!rstate.started) {
        // Establishing shot. Register the prompt and reference image ATOMICALLY
        // via set_conditioning so the very first chunk is generated from our
        // keyframe (not text-only, then "correcting itself" a chunk later once
        // the image lands). set_image_strength must precede it — Helios
        // snapshots strength with the image. Falls back gracefully if
        // set_conditioning isn't available, and to text-only if upload fails.
        const ref = await uploadStill(s.imageUrl);
        if (ref) {
          await cmd("set_image_strength", { image_strength: IMAGE_STRENGTH });
          let conditioned = false;
          try {
            await cmd("set_conditioning", { prompt: s.prompt, image: ref });
            conditioned = true;
          } catch (e) {
            log("set_conditioning unsupported, falling back", e);
          }
          if (!conditioned) {
            await cmd("set_prompt", { prompt: s.prompt });
            await cmd("set_image", { image: ref });
          }
        } else {
          // No keyframe yet — start text-only; the very next turn re-anchors on
          // the still via set_image. standalone.js keeps the still painted
          // underneath until real frames flow, so we never show a blank scene.
          await cmd("set_prompt", { prompt: s.prompt });
        }
        rstate.showSuppressed = false; // allow the video to reveal once frames flow
        await cmd("start", {});
        rstate.started = true;
        rstate.lastPrompt = s.prompt;
        log("generation started", ref ? "(image-conditioned)" : "(text-only fallback)");
      } else {
        // EVERY subsequent turn — same location and hard transitions alike:
        // re-anchor the live video to the fresh Gemini keyframe (image-to-image)
        // so it stays in our universe instead of drifting on text. The swap is
        // an immediate switch at the next Helios chunk boundary. When there's no
        // new still (e.g. instant action re-steer before the turn resolves) we
        // just re-prompt and let the current anchor ride.
        const ref = s.imageUrl ? await uploadStill(s.imageUrl) : null;
        if (ref) {
          await cmd("set_image_strength", { image_strength: IMAGE_STRENGTH });
          await cmd("set_image", { image: ref });
        }
        await cmd("set_prompt", { prompt: s.prompt });
        rstate.lastPrompt = s.prompt;
        log(ref ? "re-anchored (img2img)" : "re-steered (text)", s.prompt.slice(0, 80));
      }
    } catch (err) {
      log("apply scene failed", err);
      rstate.pending = s; // retry on next ready/flush
    }
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
      reactor.on("error", (e) => { log("error", e && e.code, e && e.message); if (e && e.recoverable === false) setStatus("error"); });

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
    // True only when the video is actually on-screen with decoded frames — the
    // signal the client uses to stop repainting the still behind it.
    isShowing: () => {
      const v = rstate.video || document.getElementById("reactor-video");
      return !!(v && !v.classList.contains("hidden") && v.videoWidth > 0);
    },
    onStatus: null,
  };
})();
