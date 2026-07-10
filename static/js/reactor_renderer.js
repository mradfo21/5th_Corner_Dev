/* ============================================================
   SOMEWHERE // Reactor realtime renderer (experimental)

   Drives Reactor's Helios world model as an alternative scene renderer.
   We steer the live video with a clean, video-model-appropriate scene prompt
   (built server-side by build_realtime_prompt) and — following Helios's
   image-to-video guidance — condition the world model on the SAME still the
   game generated so the video matches our intended composition.

   Wire protocol (per Helios schema reference; verified against the live model):
     • establishing shot / new game:
         set_prompt({prompt}) -> uploadFile(still) -> set_image_strength(S)
           -> set_image({image}) -> start
       (prompt MUST be registered before start; set_conditioning + immediate
        start races and the model rejects start with "No prompt set")
     • location change (hard_transition):
         uploadFile(still) -> set_image_strength(S) -> set_image({image}) -> set_prompt({prompt})
     • same-location turn:
         set_prompt({prompt})   (let the video evolve continuously)

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
  // How strongly the reference still anchors the video (0..1). Moderate-high so
  // the scene matches our composition but the model can still animate/breathe.
  const IMAGE_STRENGTH = 0.6;

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
    video.classList.remove("hidden");
    video.play().catch(() => {});
    setStatus("live");
    log("main_video attached");
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

    // Dedupe pure re-sends of the same prompt while already running.
    if (rstate.started && s.prompt === rstate.lastPrompt && !s.hardTransition) return;

    try {
      if (!rstate.started) {
        // Establishing shot. IMPORTANT: register the prompt BEFORE start —
        // set_conditioning + immediate start races (the model rejects start
        // with "No prompt set" while the image/prompt are still decoding). So
        // we set_prompt first, attach the still as an image-to-video reference,
        // then start. Chunk 0 is text-driven; the still conditions subsequent
        // chunks once image_accepted lands (a beat later), which is exactly how
        // Helios image-to-video is meant to run.
        await cmd("set_prompt", { prompt: s.prompt });
        const ref = await uploadStill(s.imageUrl);
        if (ref) {
          await cmd("set_image_strength", { image_strength: IMAGE_STRENGTH });
          await cmd("set_image", { image: ref });
        }
        await cmd("start", {});
        rstate.started = true;
        rstate.lastPrompt = s.prompt;
        log("generation started", ref ? "(image-conditioned)" : "(text-only)");
      } else if (s.hardTransition) {
        // Location change: swap the reference still, then re-steer.
        const ref = await uploadStill(s.imageUrl);
        if (ref) {
          await cmd("set_image_strength", { image_strength: IMAGE_STRENGTH });
          await cmd("set_image", { image: ref });
        }
        await cmd("set_prompt", { prompt: s.prompt });
        rstate.lastPrompt = s.prompt;
        log("hard transition re-steer", ref ? "(reseeded image)" : "");
      } else {
        // Same location: let the video evolve continuously via prompt only.
        await cmd("set_prompt", { prompt: s.prompt });
        rstate.lastPrompt = s.prompt;
        log("re-steered:", s.prompt.slice(0, 80));
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

  window.ReactorRenderer = {
    enable, disable, applyScene, setPrompt, reset, pause, resume,
    getStatus: () => rstate.status,
    isActive: () => rstate.active,
    isReady: () => rstate.ready,
    onStatus: null,
  };
})();
