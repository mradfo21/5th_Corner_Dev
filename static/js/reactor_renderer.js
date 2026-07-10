/* ============================================================
   SOMEWHERE // Reactor realtime renderer (experimental)

   Drives Reactor's Helios world model as an alternative scene renderer.
   Instead of painting a Gemini still each turn, we stream live WebRTC video
   and STEER it with the same per-turn scene prompt the engine already builds.

   - The Reactor API key never touches the browser: we mint a short-lived JWT
     via our own POST /api/reactor/token proxy.
   - The SDK is loaded from an ESM CDN so no build step is required. If the
     import or connection fails, standalone.js falls back to the still image.

   Exposes a tiny facade on window.ReactorRenderer so standalone.js can stay
   renderer-agnostic:
       enable()              -> connect + start streaming
       disable()             -> stop + hide the video layer
       setPrompt(prompt,url) -> steer the live model with a new scene prompt
       isActive()            -> currently connected/streaming
       isReady()             -> connection reached "ready"
   ============================================================ */
(function () {
  "use strict";

  // Pinned SDK version (Reactor is beta; pin to avoid surprise breakage).
  const SDK_URL = "https://esm.sh/@reactor-team/js-sdk@0";
  const FALLBACK_MODEL = "helios";

  const rstate = {
    reactor: null,
    active: false,       // connect() has been kicked off and not torn down
    ready: false,        // connection status === "ready"
    started: false,      // generation has been started at least once
    pendingPrompt: null, // most recent prompt awaiting injection
    lastPrompt: null,    // last prompt actually sent (dedup)
    video: null,
    cfg: { model_name: FALLBACK_MODEL, enabled: false },
    connecting: false,
  };

  function log() {
    try { console.log.apply(console, ["[reactor]", ...arguments]); } catch (_) {}
  }

  function getVideo() {
    if (!rstate.video) rstate.video = document.getElementById("reactor-video");
    return rstate.video;
  }

  async function loadConfig() {
    try {
      const r = await fetch("/api/reactor/config");
      if (r.ok) rstate.cfg = await r.json();
    } catch (err) {
      log("config fetch failed, using defaults", err);
    }
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
    video.play().catch(() => {}); // autoplay may need muted; element is muted
    log("main_video attached");
  }

  // Send whatever prompt is pending, honoring Helios's "must set_prompt at
  // chunk 0 before start" rule. Safe to call repeatedly.
  async function flushPrompt() {
    const r = rstate.reactor;
    if (!r || !rstate.ready) return;
    if (rstate.pendingPrompt == null) return;
    const prompt = rstate.pendingPrompt;
    if (prompt === rstate.lastPrompt && rstate.started) {
      rstate.pendingPrompt = null;
      return;
    }
    rstate.pendingPrompt = null;
    try {
      await r.sendCommand("set_prompt", { prompt });
      rstate.lastPrompt = prompt;
      if (!rstate.started) {
        await r.sendCommand("start", {});
        rstate.started = true;
        log("generation started");
      } else {
        log("re-steered:", prompt.slice(0, 80));
      }
    } catch (err) {
      log("prompt send failed", err);
      // Keep the prompt so a later ready/reconnect can retry.
      rstate.pendingPrompt = prompt;
    }
  }

  async function enable() {
    if (rstate.active || rstate.connecting) return true;
    rstate.connecting = true;
    try {
      await loadConfig();
      if (!rstate.cfg.enabled) {
        log("disabled: server has no REACTOR_API_KEY configured");
        rstate.connecting = false;
        return false;
      }
      let sdk;
      try {
        sdk = await import(/* @vite-ignore */ SDK_URL);
      } catch (err) {
        log("SDK import failed", err);
        rstate.connecting = false;
        return false;
      }
      const Reactor = sdk.Reactor || (sdk.default && sdk.default.Reactor);
      if (!Reactor) {
        log("SDK missing Reactor export");
        rstate.connecting = false;
        return false;
      }

      const reactor = new Reactor({ modelName: rstate.cfg.model_name || FALLBACK_MODEL });
      rstate.reactor = reactor;

      reactor.on("trackReceived", attachTrack);
      reactor.on("statusChanged", async (status) => {
        log("status:", status);
        if (status === "ready") {
          rstate.ready = true;
          await flushPrompt();
        } else if (status === "disconnected") {
          rstate.ready = false;
        }
      });
      reactor.on("error", (e) => log("error", e && e.code, e && e.message));

      const jwt = await fetchToken();
      rstate.active = true;
      await reactor.connect(jwt);
      log("connect() resolved");
      rstate.connecting = false;
      return true;
    } catch (err) {
      log("enable failed", err);
      rstate.connecting = false;
      await disable();
      return false;
    }
  }

  async function disable() {
    rstate.ready = false;
    rstate.started = false;
    rstate.pendingPrompt = null;
    rstate.lastPrompt = null;
    const video = getVideo();
    if (video) {
      video.classList.add("hidden");
      try { video.srcObject = null; } catch (_) {}
    }
    const r = rstate.reactor;
    rstate.reactor = null;
    rstate.active = false;
    if (r) {
      try { await r.disconnect(); } catch (_) {}
    }
  }

  // Queue a new scene prompt to steer the live model. `imageUrl` is accepted
  // for parity with the still renderer / future set_image seeding.
  function setPrompt(prompt, imageUrl) {
    if (!prompt) return;
    rstate.pendingPrompt = prompt;
    if (!rstate.active) {
      // Lazy-connect on first prompt if enabled.
      enable().then(() => flushPrompt());
      return;
    }
    flushPrompt();
  }

  window.ReactorRenderer = {
    enable,
    disable,
    setPrompt,
    isActive: () => rstate.active,
    isReady: () => rstate.ready,
  };
})();
