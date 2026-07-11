/* ============================================================
   SOMEWHERE // Standalone — game controller
   Talks to: POST /api/reset, POST /api/choose,
             GET /api/feed?since_id=N, GET /api/status
   ============================================================ */
(function () {
  "use strict";

  const POLL_INTERVAL_MS = 1500;
  const STATUS_INTERVAL_MS = 4000;
  const FAST_POLL_INTERVAL_MS = 800; // used right after a choice, while waiting for the turn to resolve
  const FAST_POLL_TIMEOUT_MS = 45000; // give up waiting fast after this long and fall back to normal polling
  const AUTOPLAY_FRAME_DELAY_MS = 350;  // advance almost immediately once the new frame renders
  const AUTOPLAY_FALLBACK_MS = 6000;    // if no image arrives, advance anyway after this

  // Show a small thumbnail preview of each guide image as it's integrated into
  // the realtime world model. Flip to false (or set localStorage
  // "guide_thumbnail" = "off", toggled by the thumbnail's own hide button) to
  // hide it — the notification + re-anchor still happen either way.
  const GUIDE_THUMBNAIL_ENABLED = true;

  const INTERIM_MESSAGES = [
    "Transmitting...",
    "Static crackles across the channel...",
    "Reading the scene...",
    "Calculating consequence...",
    "Signal stabilizing...",
    "The world responds...",
    "Rendering what comes next...",
  ];

  const el = {
    sceneA: document.getElementById("sceneA"),
    sceneB: document.getElementById("sceneB"),
    sceneFlash: document.getElementById("scene-flash"),
    sceneGlitch: document.getElementById("scene-glitch"),
    prose: document.getElementById("prose-feed"),
    choices: document.getElementById("choices-container"),
    customForm: document.getElementById("custom-form"),
    customInput: document.getElementById("custom-input"),
    freeWillBtn: document.getElementById("free-will-btn"),
    forwardBtn: document.getElementById("forward-btn"),
    actionWheel: document.getElementById("action-wheel"),
    veil: document.getElementById("processing-veil"),
    veilMessage: document.getElementById("veil-message"),
    veilSimple: document.getElementById("veil-simple"),
    ceremony: document.getElementById("ceremony"),
    ceremonySteps: document.getElementById("ceremony-steps"),
    ceremonyNote: document.getElementById("ceremony-note"),
    hudTurn: document.getElementById("hud-turn"),
    hudPhase: document.getElementById("hud-phase"),
    hudChaos: document.getElementById("hud-chaos"),
    hudTime: document.getElementById("hud-time"),
    hudTimeWrap: document.getElementById("hud-time-wrap"),
    btnReset: document.getElementById("btn-reset"),
    btnVhs: document.getElementById("btn-vhs"),
    btnSnd: document.getElementById("btn-snd"),
    rendererBtn: document.getElementById("btn-renderer"),
    vhsOverlay: document.getElementById("vhs-overlay"),
    backendName: document.getElementById("backend-name"),
    timecodeText: document.getElementById("timecode-text"),
    inventoryHud: document.getElementById("inventory-hud"),
    inventoryList: document.getElementById("inventory-list"),
    deathOverlay: document.getElementById("death-overlay"),
    deathMessage: document.getElementById("death-message"),
    deathRestart: document.getElementById("death-restart"),
    tapeBtn: document.getElementById("tape-btn"),
    tapeOverlay: document.getElementById("tape-overlay"),
    tapeFrameA: document.getElementById("tape-frameA"),
    tapeFrameB: document.getElementById("tape-frameB"),
    tapeHud: document.getElementById("tape-hud"),
    tapeRec: document.getElementById("tape-rec"),
    tapeCounter: document.getElementById("tape-counter"),
    tapeTime: document.getElementById("tape-time"),
    tapeEmpty: document.getElementById("tape-empty"),
    tapePrev: document.getElementById("tape-prev"),
    tapePlayPause: document.getElementById("tape-playpause"),
    tapeNext: document.getElementById("tape-next"),
    tapeEject: document.getElementById("tape-eject"),
    autoplayBtn: document.getElementById("autoplay-btn"),
    autoplayLabel: document.getElementById("autoplay-label"),
  };

  const state = {
    lastId: 0,
    polling: false,
    pollTimer: null,
    statusTimer: null,
    vhsEnabled: true,
    processing: false,
    activeScene: "A", // which of sceneA/sceneB is currently visible
    secondsElapsed: 0,
    timecodeTimer: null,
    awaitingResolution: false,
    gameOver: false,
    soundEnabled: true,
    renderedIds: new Set(), // guard against rendering the same feed item twice
    lastStatus: {},
    freeWillOpen: false,
    autoPlay: false,
    autoTimer: null,
    currentPromptId: null,     // id of the latest choice prompt (the live decision point)
    lastAdvancedPromptId: null, // guard: auto-advance at most once per prompt
    observeTimer: null,         // debounce for feeding the video frame to the sim
    observedPromptId: null,     // guard: observe at most once per decision point
    turnResolved: false,        // the turn's pipeline finished (choices are live)
    turnImageLoaded: false,     // this turn's new frame has arrived on screen
    finishTimer: null,          // fallback: fade the progress bar back to play
  };

  // ------------------------------------------------------------------
  // Sound — tiny WebAudio synth (no assets; gated on first user gesture)
  // ------------------------------------------------------------------
  const Sound = (function () {
    let ctx = null;
    function ensure() {
      if (!state.soundEnabled) return null;
      if (!ctx) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return null;
        ctx = new AC();
      }
      if (ctx.state === "suspended") ctx.resume();
      return ctx;
    }
    // A short shaped tone. Freqs can be a single value or a [from,to] glide.
    function tone(freq, dur, type, vol, delay) {
      if (!state.soundEnabled) return;
      const c = ensure();
      if (!c) return;
      const t0 = c.currentTime + (delay || 0);
      const osc = c.createOscillator();
      const gain = c.createGain();
      osc.type = type || "sine";
      if (Array.isArray(freq)) {
        osc.frequency.setValueAtTime(freq[0], t0);
        osc.frequency.exponentialRampToValueAtTime(Math.max(1, freq[1]), t0 + dur);
      } else {
        osc.frequency.setValueAtTime(freq, t0);
      }
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.exponentialRampToValueAtTime(vol || 0.06, t0 + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
      osc.connect(gain);
      gain.connect(c.destination);
      osc.start(t0);
      osc.stop(t0 + dur + 0.02);
    }
    // A short burst of filtered white noise — the audible "shhk" of a VCR
    // jumping between tape segments, played under the transition glitch.
    function noise(dur, vol) {
      if (!state.soundEnabled) return;
      const c = ensure();
      if (!c) return;
      const t0 = c.currentTime;
      const len = Math.max(1, Math.floor(c.sampleRate * (dur || 0.22)));
      const buf = c.createBuffer(1, len, c.sampleRate);
      const data = buf.getChannelData(0);
      for (let i = 0; i < len; i++) data[i] = (Math.random() * 2 - 1);
      const src = c.createBufferSource();
      src.buffer = buf;
      const bp = c.createBiquadFilter();
      bp.type = "bandpass";
      bp.frequency.value = 2600;
      bp.Q.value = 0.6;
      const gain = c.createGain();
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.exponentialRampToValueAtTime(vol || 0.05, t0 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + (dur || 0.22));
      src.connect(bp); bp.connect(gain); gain.connect(c.destination);
      src.start(t0);
      src.stop(t0 + (dur || 0.22) + 0.02);
    }
    return {
      resume() { ensure(); },
      glitch() { noise(0.24, 0.055); },                        // VCR transition static burst
      text() { tone(430, 0.09, "sine", 0.045); },              // narrative / world text lands
      pickup() { tone([600, 900], 0.16, "triangle", 0.06); },  // item pickup
      choices() { tone(680, 0.07, "triangle", 0.05); tone(920, 0.09, "triangle", 0.045, 0.07); }, // choices ready
      select() { tone(520, 0.05, "square", 0.05); tone(790, 0.10, "square", 0.05, 0.055); },       // confirm choice
      status() { tone(320, 0.05, "sine", 0.03); },             // HUD tick
      death() { tone([180, 60], 0.7, "sawtooth", 0.09); },     // game over
      error() { tone([200, 120], 0.18, "sawtooth", 0.05); },
      scene() { tone(180, 0.05, "sine", 0.05); tone([520, 380], 0.14, "sine", 0.04, 0.03); }, // new scene image streams in (shutter/whir)
      start() { tone([160, 520], 0.28, "sawtooth", 0.05); tone(880, 0.12, "triangle", 0.04, 0.18); }, // new tape / game start
      escalate() { tone([300, 620], 0.35, "sawtooth", 0.06); tone([620, 900], 0.3, "square", 0.03, 0.12); }, // phase escalates — tension rises
      submit() { tone(700, 0.05, "square", 0.05); tone(1050, 0.11, "square", 0.045, 0.05); }, // custom action sent
      open() { tone([420, 760], 0.14, "triangle", 0.05); },    // free-will input reveal
      toggle() { tone(300, 0.04, "square", 0.04); },           // UI toggle click
      // ---- Ceremony: one distinct cue per pipeline step, so the player HEARS
      // the world working through each stage. ----
      cereAction() { tone(300, 0.05, "square", 0.06); tone([300, 620], 0.14, "square", 0.05, 0.05); },   // action selected — decisive commit
      cereConsequence() { tone(210, 0.09, "sine", 0.05); tone([420, 300], 0.2, "triangle", 0.045, 0.08); }, // consequence generated — a heavy reveal
      cereWorldUpdate() { tone([260, 700], 0.32, "sawtooth", 0.045); },                                   // world updating — rising machine sweep
      cereWorldRespond() { tone(520, 0.06, "triangle", 0.05); tone(780, 0.1, "triangle", 0.05, 0.06); tone(1040, 0.14, "sine", 0.04, 0.13); }, // world responding — it materialises
      cereActions() { tone(660, 0.06, "triangle", 0.045); tone(880, 0.06, "triangle", 0.045, 0.06); tone(1180, 0.12, "sine", 0.04, 0.12); },  // actions generating — options shimmer in
      cereDone() { tone(720, 0.06, "sine", 0.05); tone(1080, 0.18, "sine", 0.05, 0.06); },                // turn resolved — clean affirmation
      cereNote() { tone(1500, 0.03, "square", 0.028); },       // realtime sub-event tick (prompt sent, chunk rendered…)
    };
  })();

  // ------------------------------------------------------------------
  // Utilities
  // ------------------------------------------------------------------

  function fmtTimecode(totalSeconds) {
    const h = Math.floor(totalSeconds / 3600).toString().padStart(2, "0");
    const m = Math.floor((totalSeconds % 3600) / 60).toString().padStart(2, "0");
    const s = Math.floor(totalSeconds % 60).toString().padStart(2, "0");
    return `${h}:${m}:${s}`;
  }

  function startTimecode() {
    stopTimecode();
    state.secondsElapsed = 0;
    el.timecodeText.textContent = fmtTimecode(0);
    state.timecodeTimer = setInterval(() => {
      state.secondsElapsed += 1;
      el.timecodeText.textContent = fmtTimecode(state.secondsElapsed);
    }, 1000);
  }

  function stopTimecode() {
    if (state.timecodeTimer) clearInterval(state.timecodeTimer);
    state.timecodeTimer = null;
  }

  // Simple spinner veil — used for boot / reset, not for a turn (the turn uses
  // the ceremony stepper below). Gates input via state.processing.
  function showVeil(message) {
    state.processing = true;
    Ceremony.hideUI();
    if (el.veilSimple) el.veilSimple.classList.remove("hidden");
    el.veilMessage.textContent = message || INTERIM_MESSAGES[0];
    el.veil.classList.remove("hidden");
  }

  function hideVeil() {
    state.processing = false;
    state.turnResolved = false;
    state.turnImageLoaded = false;
    clearTimeout(state.finishTimer);
    el.veil.classList.add("hidden");
    // Fade the play button back in — the progress bar occupied its spot.
    if (el.actionWheel) el.actionWheel.classList.remove("turn-active");
    Ceremony.reset();
  }

  // ------------------------------------------------------------------
  // Ceremony — the gamified turn pipeline. Each turn the world runs a clear
  // sequence of steps; we light them up one at a time (with sound + a beat)
  // so the player can FEEL the game working, instead of a vague spinner.
  // The same tracker drives both still-image and realtime-video renderers;
  // in video mode the realtime sub-events (prompt submitted, seed accepted,
  // stream live, chunk rendered) show on the sub-line.
  // ------------------------------------------------------------------
  const Ceremony = (function () {
    const STEPS = [
      { key: "action",        label: "Action\nSelected",       glyph: "\u25C9", sound: "cereAction" },       // ◉
      { key: "consequence",   label: "Consequence\nGenerated", glyph: "\u2726", sound: "cereConsequence" },  // ✦
      { key: "world_update",  label: "World\nUpdating",        glyph: "\u27F3", sound: "cereWorldUpdate" },   // ⟳
      { key: "world_respond", label: "World\nResponding",      glyph: "\u25C8", sound: "cereWorldRespond" },  // ◈
      { key: "actions",       label: "Actions\nGenerating",    glyph: "\u22D4", sound: "cereActions" },       // ⋔
    ];
    const IDX = {};
    STEPS.forEach((s, i) => { IDX[s.key] = i; });
    const DWELL_MS = 460;      // minimum time each step is shown (so it registers)
    // After the turn resolves we keep the (green) progress bar in the play
    // button's spot until the new frame actually loads, then fade back to play.
    const FADE_AFTER_IMAGE_MS = 520;   // brief hold once the image is on screen
    const IMAGE_WAIT_FALLBACK_MS = 6000; // never strand the bar (no_images / gen fail)

    let built = false;
    let active = false;
    let cur = -1;        // index of the currently active step
    let target = -1;     // furthest step requested
    let dwellTimer = null;
    let doneTimer = null;
    let noteTimer = null;

    function build() {
      if (built || !el.ceremonySteps) return;
      el.ceremonySteps.innerHTML = "";
      STEPS.forEach((s) => {
        const li = document.createElement("li");
        li.className = "cere-step";
        li.dataset.key = s.key;
        li.innerHTML =
          `<span class="cere-dot">${s.glyph}</span>` +
          `<span class="cere-label">${s.label.replace(/\n/g, "<br>")}</span>`;
        el.ceremonySteps.appendChild(li);
      });
      built = true;
    }

    function stepEl(i) {
      return el.ceremonySteps ? el.ceremonySteps.children[i] : null;
    }

    function enter(i) {
      const node = stepEl(i);
      if (!node) return;
      for (let k = 0; k < i; k++) {
        const p = stepEl(k);
        if (p) { p.classList.remove("active", "beat"); p.classList.add("done"); }
      }
      node.classList.remove("done");
      node.classList.add("active", "beat");
      node.addEventListener("animationend", () => node.classList.remove("beat"), { once: true });
      cur = i;
      const s = STEPS[i];
      if (s && Sound[s.sound]) Sound[s.sound]();
    }

    function pump() {
      if (dwellTimer) return;                 // still dwelling on the current step
      if (cur >= target) return;              // caught up
      enter(cur + 1);
      dwellTimer = setTimeout(() => { dwellTimer = null; pump(); }, DWELL_MS);
    }

    return {
      // Show the tracker fresh and enter the first step (action selected).
      begin() {
        build();
        clearTimeout(doneTimer); clearTimeout(dwellTimer); dwellTimer = null;
        clearTimeout(state.finishTimer);
        active = true;
        cur = -1; target = -1;
        state.processing = true;
        state.turnResolved = false;
        state.turnImageLoaded = false;
        // The progress bar takes over the play button's spot for the turn.
        if (el.actionWheel) el.actionWheel.classList.add("turn-active");
        // Reset all chips to pending.
        if (el.ceremonySteps) {
          Array.from(el.ceremonySteps.children).forEach((n) => n.classList.remove("active", "done", "beat"));
        }
        if (el.ceremony) el.ceremony.classList.remove("hidden", "resolved");
        if (el.veilSimple) el.veilSimple.classList.add("hidden");
        this.note("");
        el.veil.classList.remove("hidden");
        this.reach("action");
      },

      // Monotonically advance to (at least) the named step. Never goes back, so
      // out-of-order feed items and duplicate signals are safe.
      reach(key) {
        if (!active) return;
        const i = typeof key === "number" ? key : IDX[key];
        if (i == null || i < 0) return;
        if (i > target) target = i;
        pump();
      },

      // A realtime sub-event line (prompt submitted, seed accepted, stream live,
      // chunk N rendered…). Doesn't advance the main step; just informs + ticks.
      note(text, opts) {
        if (!el.ceremonyNote) return;
        const t = (text || "").trim();
        el.ceremonyNote.textContent = t;
        el.ceremonyNote.classList.toggle("show", !!t);
        if (t && active) {
          el.ceremonyNote.classList.remove("tick");
          void el.ceremonyNote.offsetWidth;
          el.ceremonyNote.classList.add("tick");
          if (!opts || opts.tick !== false) Sound.cereNote();
          clearTimeout(noteTimer);
        }
      },

      // The turn fully resolved: march to the last step, mark everything done,
      // flash green, sound the affirmation, then fade. Releases input gating.
      complete() {
        if (!active) { hideVeil(); return; }
        target = STEPS.length - 1;
        // Snap through any remaining steps immediately for the finish.
        clearTimeout(dwellTimer); dwellTimer = null;
        while (cur < target) enter(cur + 1);
        if (el.ceremonySteps) {
          Array.from(el.ceremonySteps.children).forEach((n) => {
            n.classList.remove("active", "beat"); n.classList.add("done");
          });
        }
        if (el.ceremony) el.ceremony.classList.add("resolved");
        this.note("\u2713 World updated", { tick: false });
        Sound.cereDone();
        state.processing = false; // choices are live — let the player act
        active = false;
        state.turnResolved = true;
        // Hold the resolved bar until this turn's frame loads, then fade it back
        // to the play button. Fallback so a missing image never strands the bar.
        clearTimeout(doneTimer);
        clearTimeout(state.finishTimer);
        state.finishTimer = setTimeout(() => this.finish(), IMAGE_WAIT_FALLBACK_MS);
        this._tryFinish();
      },

      // The new frame for this turn is on screen. If the pipeline has already
      // resolved, fade the progress bar back to the play button.
      imageLoaded() {
        state.turnImageLoaded = true;
        this._tryFinish();
      },

      _tryFinish() {
        if (state.turnResolved && state.turnImageLoaded) this.finish();
      },

      // Fade the resolved progress bar out, restoring the play button.
      finish() {
        if (!state.turnResolved) return;
        state.turnResolved = false;
        clearTimeout(state.finishTimer);
        clearTimeout(doneTimer);
        doneTimer = setTimeout(hideVeil, FADE_AFTER_IMAGE_MS);
      },

      // Tear down without the resolve flourish (error / game over / reset).
      abort() {
        active = false;
        clearTimeout(dwellTimer); dwellTimer = null;
        clearTimeout(doneTimer); doneTimer = null;
      },

      // Hide just the ceremony chrome (keep the veil for the simple spinner).
      hideUI() {
        if (el.ceremony) el.ceremony.classList.add("hidden");
      },

      // Full reset of tracker visuals/state.
      reset() {
        this.abort();
        cur = -1; target = -1;
        if (el.ceremony) el.ceremony.classList.remove("resolved");
        this.hideUI();
        this.note("");
      },

      isActive() { return active; },
    };
  })();

  async function postJSON(url, body) {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!resp.ok) {
      throw new Error(`${url} -> HTTP ${resp.status}`);
    }
    return resp.json();
  }

  async function getJSON(url) {
    const resp = await fetch(url);
    if (!resp.ok) {
      throw new Error(`${url} -> HTTP ${resp.status}`);
    }
    return resp.json();
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
  }

  // Render a small subset of markdown (the engine emits **bold** in some
  // feed content, e.g. inventory pickups). Everything is HTML-escaped first,
  // so this is safe against injection from model-generated text.
  function renderInline(text) {
    return escapeHtml(text).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  }

  // ------------------------------------------------------------------
  // Scene rendering
  // ------------------------------------------------------------------

  function setScene(imageUrl, opts) {
    if (!imageUrl) return;
    const silent = !!(opts && opts.silent);
    const incoming = state.activeScene === "A" ? el.sceneB : el.sceneA;
    const outgoing = state.activeScene === "A" ? el.sceneA : el.sceneB;
    incoming.style.backgroundImage = `url('${imageUrl}')`;
    incoming.classList.add("scene-active");
    outgoing.classList.remove("scene-active");
    state.activeScene = state.activeScene === "A" ? "B" : "A";
    // Skip the white scene flash AND the VCR glitch when we're staging a still
    // *behind* the live video (silent): both overlays sit above the video, so
    // firing them here would strobe over the running stream. The re-anchor's own
    // glitch (on the reactor 'reset' command) masks that hand-off instead.
    if (!silent) { flashScene(); glitchTransition(); }
  }

  function flashScene() {
    if (!el.sceneFlash) return;
    el.sceneFlash.classList.remove("flash");
    // Force reflow so the animation can re-trigger on consecutive scene swaps.
    void el.sceneFlash.offsetWidth;
    el.sceneFlash.classList.add("flash");
  }

  // Fire the VCR distortion burst over the current frame to mask a transition
  // (image swap, realtime re-anchor/reveal, or reset). Re-triggering restarts
  // the burst and extends it, so back-to-back transitions read as one longer
  // patch of static rather than a stutter.
  let _glitchTimer = null;
  function glitchTransition(ms) {
    const g = el.sceneGlitch;
    if (!g) return;
    g.classList.remove("burst");
    void g.offsetWidth; // reflow so the animation restarts on rapid re-triggers
    g.classList.add("burst");
    clearTimeout(_glitchTimer);
    _glitchTimer = setTimeout(() => g.classList.remove("burst"), ms || 640);
    try { Sound.glitch(); } catch (_) {}
  }

  // Wipe BOTH still layers immediately (no crossfade) so the current scene image
  // vanishes the instant it's called — used on reset so the dead run's image
  // can't linger or bleed back through under a hidden/fading video.
  function clearSceneLayers() {
    [el.sceneA, el.sceneB].forEach((s) => {
      if (!s) return;
      const prev = s.style.transition;
      s.style.transition = "none"; // kill the fade — this must be instant
      s.classList.remove("scene-active");
      s.style.backgroundImage = "";
      void s.offsetWidth; // flush the change before restoring the transition
      s.style.transition = prev || "";
    });
    state.activeScene = "A";
  }

  // ------------------------------------------------------------------
  // Renderer facade — swap between the classic still-image renderer and the
  // Reactor realtime world-model renderer without the rest of the game caring.
  // "image"   -> Gemini still per turn (default; existing behavior).
  // "reactor" -> steer Reactor's LingBot World 2 video with the SAME per-turn
  //              scene prompt the engine used to build the still. The still is
  //              painted underneath as a graceful fallback if Reactor drops.
  // Selection: ?renderer= query param > localStorage > "image".
  // ------------------------------------------------------------------
  const Renderer = {
    mode: "image",
    explicit: false,  // did the user/URL explicitly pick a renderer?
    lastScene: null,  // latest {prompt,imageUrl,hardTransition}, for mid-game toggle
    lastBase: null,   // stable style+scene text, for instant action re-steer

    resolveInitial() {
      // A dedicated route (e.g. /realtime) can force the renderer regardless of
      // any saved preference. This wins over everything.
      const forced = window.__FORCED_RENDERER__;
      const q = new URLSearchParams(location.search).get("renderer");
      const stored = (function () {
        try { return localStorage.getItem("scene_renderer"); } catch (_) { return null; }
      })();
      if (forced === "image" || forced === "reactor") {
        this.mode = forced; this.explicit = true;
      } else if (q === "image" || q === "reactor") {
        this.mode = q; this.explicit = true;
      } else if (stored === "image" || stored === "reactor") {
        this.mode = stored; this.explicit = true;
      } else {
        this.mode = "image"; this.explicit = false; // provisional; server sets default
      }
    },

    async init() {
      this.resolveInitial();
      // When the player hasn't explicitly chosen, follow the server's default
      // renderer (SCENE_RENDERER — "reactor" out of the box).
      if (!this.explicit) {
        try {
          const r = await fetch("/api/reactor/config");
          if (r.ok) {
            const c = await r.json();
            if (c.renderer === "image" || c.renderer === "reactor") this.mode = c.renderer;
          }
        } catch (_) {}
      }
      // Report real connection state on the toggle button, and fall back to
      // stills automatically if the realtime renderer can't start (no key,
      // CDN/import failure, GPU conflict) so a player is never stuck on a
      // "LIVE" button that isn't actually live.
      if (this.reactorAvailable()) {
        window.ReactorRenderer.onStatus = (s) => {
          if (s === "error" && Renderer.mode === "reactor") {
            console.warn("[standalone] realtime renderer unavailable — falling back to stills");
            Renderer.mode = "image"; // reflect reality; keep stored pref intact
            showRendererToast("Realtime unavailable — showing stills");
            hideGuideThumbnail();
            // Tear down the realtime layers (video + freeze) and paint the last
            // known still so the fallback isn't a blank/black screen.
            try { window.ReactorRenderer.disable(); } catch (_) {}
            if (Renderer.lastScene && Renderer.lastScene.imageUrl) setScene(Renderer.lastScene.imageUrl);
          } else if (s === "live" && Renderer.mode === "reactor") {
            showRendererToast("Realtime video — live");
          }
          updateRendererButton();
        };
        // Surface the realtime world model's lifecycle on the ceremony sub-line
        // so the player sees the video pipeline working too: prompts submitted,
        // seed accepted, stream live, state/chunks updating.
        window.ReactorRenderer.onEvent = (name, data) => {
          const d = data || {};
          // VCR static over realtime transitions, independent of the ceremony
          // overlay: the re-anchor (world 'reset' before re-staging on a new
          // guide image) and the still→video reveal are the visible hand-offs.
          if (Renderer.mode === "reactor") {
            if (name === "video_showing" ||
                (name === "command_sent" && d.command === "reset")) {
              glitchTransition();
            }
          }
          if (Renderer.mode !== "reactor" || !Ceremony.isActive()) return;
          switch (name) {
            case "command_sent": {
              const cmdNote = {
                set_prompt: "\u25B8 Prompt submitted",
                set_image: "\u25B8 Seed image sent",
                start: "\u25B8 Starting stream",
                reset: "\u25B8 Re-staging world",
                pause: "\u25B8 Pausing stream",
                resume: "\u25B8 Resuming stream",
              }[d.command];
              if (cmdNote) {
                Ceremony.note(cmdNote);
                if (d.command === "set_prompt" || d.command === "set_image") Ceremony.reach("world_update");
              }
              return;
            }
            case "prompt_accepted": Ceremony.note("\u2713 Prompt accepted"); return;
            case "image_accepted": Ceremony.note("\u2713 Seed image accepted"); return;
            case "generation_started":
            case "stage_started":
              Ceremony.note("\u25C8 Stream generating");
              Ceremony.reach("world_update");
              return;
            case "video_showing":
              Ceremony.note("\u25C9 Stream live");
              Ceremony.reach("world_respond");
              return;
            case "chunk_complete":
              Ceremony.note("\u25A3 Chunk " + ((d.chunk_index != null ? d.chunk_index : 0) + 1) + " rendered");
              return;
            case "state": {
              const now = Date.now();
              if (now - (state._lastStateNote || 0) < 700) return; // throttle
              state._lastStateNote = now;
              const act = d.current_action && d.current_action !== "still" ? d.current_action : null;
              Ceremony.note(act ? "\u2261 State \u00B7 " + act : "\u2261 State updated");
              return;
            }
            case "generation_reset": Ceremony.note("\u25CC World cleared"); return;
            case "video_stalled":
              Ceremony.note("\u26A0 Stream stalled \u00B7 no video \u2014 showing stills", { tick: false });
              return;
            case "command_error":
              Ceremony.note("\u26A0 " + (d.reason || d.command || "error"), { tick: false });
              return;
            default: return;
          }
        };
        // When a guide image is integrated into the world model, notify the
        // player and (optionally) show a thumbnail preview of the exact frame
        // the live video just re-anchored on.
        window.ReactorRenderer.onGuideImage = (imageUrl) => {
          if (Renderer.mode !== "reactor" || !imageUrl) return;
          showRendererToast("Guide image integrated");
          if (Ceremony.isActive()) Ceremony.note("\u25C8 Guide image integrated");
          try { Sound.scene(); } catch (_) {}
          showGuideThumbnail(imageUrl);
        };
      }
      // In realtime mode, connect eagerly so the GPU session is warming while
      // the intro scene generates — the video then starts as soon as the first
      // scene prompt arrives. (Falls back to stills if it can't connect.)
      if (this.mode === "reactor" && this.reactorAvailable()) {
        window.ReactorRenderer.enable().then((ok) => {
          if (ok && Renderer.lastScene) window.ReactorRenderer.applyScene(Renderer.lastScene);
        });
      }
      updateRendererButton();
    },

    reactorAvailable() {
      return !!window.ReactorRenderer;
    },

    // Apply a scene coming off the feed. `prompt` is the engine's realtime
    // scene prompt (feed item metadata.prompt); `imageUrl` is the generated
    // still; `meta` carries flags like hard_transition (location change).
    applyScene(imageUrl, prompt, meta) {
      const scene = {
        prompt: prompt || null,
        imageUrl: imageUrl || null,
        hardTransition: !!(meta && meta.hard_transition),
      };
      // Remember the latest scene we can (re)start realtime from. A steer needs
      // BOTH a prompt and an image, but not every feed item carries both — a
      // player_choice_prompt, for instance, rides the current still with no
      // steer prompt. MERGE rather than replace, so a partial update never wipes
      // a known-good prompt/image (which used to silently lock the realtime
      // toggle out with "Realtime starts once a scene is ready").
      if (scene.prompt || scene.imageUrl) {
        const prev = this.lastScene || {};
        this.lastScene = {
          prompt: scene.prompt || prev.prompt || null,
          imageUrl: scene.imageUrl || prev.imageUrl || null,
          hardTransition: scene.hardTransition,
        };
      }
      if (meta && meta.base) this.lastBase = meta.base;
      if (this.mode === "reactor" && this.reactorAvailable()) {
        // Realtime mode: the reactor renderer OWNS the screen. It shows the live
        // video plus a freeze back-buffer (the seed guide image on the first
        // scene, or the last live frame while re-anchoring onto a new guide
        // image) so switches are seamless. We deliberately do NOT paint the
        // Gemini still here — painting it under the video is exactly what made
        // the ORIGINAL image flash between guide images.
        if (scene.prompt) window.ReactorRenderer.applyScene(scene);
        return;
      }
      if (imageUrl) setScene(imageUrl);
    },

    setMode(mode) {
      if (mode === this.mode) return;
      // Enabling realtime just needs the renderer available. It connects now and
      // starts as soon as a scene is ready to steer from — we must NOT hard-block
      // here on lastScene, or a partial/incomplete scene permanently locks the
      // toggle (the old "starts once a scene is ready" dead end).
      if (mode === "reactor" && !this.reactorAvailable()) {
        showRendererToast("Realtime unavailable");
        return;
      }
      this.mode = mode;
      this.explicit = true;
      try { localStorage.setItem("scene_renderer", mode); } catch (_) {}
      if (mode === "reactor" && this.reactorAvailable()) {
        showRendererToast("Realtime video — connecting…");
        window.ReactorRenderer.enable().then((ok) => {
          // Steer the current scene immediately so switching mid-game shows
          // something without waiting for the next turn.
          if (ok && Renderer.lastScene) window.ReactorRenderer.applyScene(Renderer.lastScene);
        });
      } else if (this.reactorAvailable()) {
        showRendererToast("Still images");
        try { window.ReactorRenderer.disable(); } catch (_) {}
        hideGuideThumbnail();
      }
      updateRendererButton();
    },

    toggle() {
      this.setMode(this.mode === "reactor" ? "image" : "reactor");
    },

    // Vision loop: once the realtime video has settled on a scene, feed the
    // ACTUAL frame the player is looking at back into the simulation so the
    // narrative/choices/next-scene track the video instead of drifting from the
    // still. Debounced; runs at most once per decision point.
    observeScene(promptId) {
      if (this.mode !== "reactor" || !this.reactorAvailable()) return;
      if (this.observedPromptId === promptId) return;
      clearTimeout(state.observeTimer);
      let tries = 0;
      const attempt = () => {
        if (state.processing || state.gameOver) return;
        if (state.currentPromptId !== promptId) return; // moved on already
        // Wait until the video is actually showing real frames before reading it.
        if (!window.ReactorRenderer.isShowing()) {
          if (tries++ < 10) state.observeTimer = setTimeout(attempt, 1500);
          return;
        }
        const frame = window.ReactorRenderer.captureFrame
          ? window.ReactorRenderer.captureFrame()
          : null;
        if (!frame) {
          if (tries++ < 10) state.observeTimer = setTimeout(attempt, 1500);
          return;
        }
        this.observedPromptId = promptId;
        // Fire and forget: the backend re-grounds the sim and delivers revised
        // choices via a 'choices_revised' feed item (handled in renderItem).
        postJSON("/api/observe", { frame, prompt_id: promptId })
          .catch((err) => console.warn("[standalone] observe failed:", err));
      };
      state.observeTimer = setTimeout(attempt, 2600); // let the video settle first
    },

    // Steer the CURRENT live video with an action as a motion beat, against the
    // scene already on screen. This is NO LONGER called on every choice: doing
    // so made the original scene's video play the action before its new guide
    // image had formed, which looked wrong. The action now rides the new guide
    // image's stream instead (its render prompt already carries the beat). Kept
    // as a facade capability for callers that want an explicit instant steer.
    // No-op unless realtime is live and we have a scene to build on.
    steerAction(action) {
      if (this.mode !== "reactor" || !this.reactorAvailable() || !this.lastBase) return;
      const a = (action || "").trim().replace(/\.+$/, "");
      if (!a) return;
      const beat = "Motion: the view shifts as you " + a.charAt(0).toLowerCase() + a.slice(1) + ".";
      window.ReactorRenderer.applyScene({
        prompt: this.lastBase + " " + beat,
        imageUrl: null,           // same scene — just re-steer, no image swap
        hardTransition: false,
      });
      Ceremony.note("\u25B8 Action injected into stream");
    },
  };

  function updateRendererButton() {
    if (!el.rendererBtn) return;
    const reactorMode = Renderer.mode === "reactor";
    const status = (reactorMode && Renderer.reactorAvailable())
      ? window.ReactorRenderer.getStatus()
      : "off";
    const ico = el.rendererBtn.querySelector(".rail-ico");
    const lbl = el.rendererBtn.querySelector(".rail-lbl");
    if (ico) ico.textContent = reactorMode ? "\u25C9" : "\u25CE"; // ◉ live / ◎ still
    if (lbl) {
      lbl.textContent = !reactorMode ? "STILL"
        : status === "connecting" ? "\u00B7\u00B7\u00B7"
        : "LIVE";
    }
    el.rendererBtn.classList.toggle("on", reactorMode && status === "live");
    el.rendererBtn.classList.toggle("pending", reactorMode && status === "connecting");
    el.rendererBtn.title = !reactorMode
      ? "Renderer: still images — click for realtime (G)"
      : status === "live"
        ? "Renderer: realtime world model (live) — click for stills (G)"
        : status === "connecting"
          ? "Renderer: realtime — connecting…"
          : "Renderer: realtime — showing stills until it connects (G)";
  }

  // Small transient on-screen note so it's obvious which renderer is active
  // (useful while testing / toggling with the G key).
  let _rendererToastTimer = null;
  function showRendererToast(text) {
    let toast = document.getElementById("renderer-toast");
    if (!toast) {
      toast = document.createElement("div");
      toast.id = "renderer-toast";
      toast.className = "renderer-toast";
      document.body.appendChild(toast);
    }
    toast.textContent = text;
    toast.classList.add("show");
    clearTimeout(_rendererToastTimer);
    _rendererToastTimer = setTimeout(() => toast.classList.remove("show"), 2200);
  }

  // ------------------------------------------------------------------
  // Guide-image thumbnail preview — a small, dismissible corner preview of the
  // still that was just integrated into the realtime world model, so it's
  // obvious the guide image is actually driving the video (not silently
  // dropped). Built lazily; easy to hide (build-time flag GUIDE_THUMBNAIL_ENABLED
  // or the player's own ✕ button, persisted in localStorage).
  // ------------------------------------------------------------------
  function guideThumbHidden() {
    if (!GUIDE_THUMBNAIL_ENABLED) return true;
    try { return localStorage.getItem("guide_thumbnail") === "off"; } catch (_) { return false; }
  }

  function ensureGuideThumb() {
    let wrap = document.getElementById("guide-thumb");
    if (wrap) return wrap;
    wrap = document.createElement("div");
    wrap.id = "guide-thumb";
    wrap.className = "guide-thumb";
    const label = document.createElement("div");
    label.className = "guide-thumb-label";
    label.textContent = "GUIDE";
    const img = document.createElement("img");
    img.className = "guide-thumb-img";
    img.alt = "Guide image integrated into the realtime world";
    const hide = document.createElement("button");
    hide.type = "button";
    hide.className = "guide-thumb-hide";
    hide.title = "Hide guide preview";
    hide.textContent = "\u2715";
    hide.addEventListener("click", () => {
      try { localStorage.setItem("guide_thumbnail", "off"); } catch (_) {}
      hideGuideThumbnail();
    });
    wrap.appendChild(img);
    wrap.appendChild(label);
    wrap.appendChild(hide);
    document.body.appendChild(wrap);
    return wrap;
  }

  function showGuideThumbnail(imageUrl) {
    if (guideThumbHidden() || !imageUrl) return;
    const wrap = ensureGuideThumb();
    const img = wrap.querySelector(".guide-thumb-img");
    if (img && img.getAttribute("src") !== imageUrl) img.setAttribute("src", imageUrl);
    wrap.classList.add("show");
    // Brief "just updated" pulse so a re-anchor on a new frame is noticeable.
    wrap.classList.remove("pulse");
    void wrap.offsetWidth; // reflow to restart the animation
    wrap.classList.add("pulse");
  }

  function hideGuideThumbnail() {
    const wrap = document.getElementById("guide-thumb");
    if (wrap) wrap.classList.remove("show");
  }

  // ------------------------------------------------------------------
  // Feed rendering
  // ------------------------------------------------------------------

  const TYPE_CLASS = {
    player_action: "player-action",
    narrative_event: "narrative-event",
    consequence_event: "consequence-event",
    vision_analysis: "vision-analysis",
    error_event: "error-event",
    player_choice_prompt: "player-choice-prompt",
    inventory_pickup: "inventory-pickup",
    inventory_full: "inventory-full",
    suspense_event: "suspense-event",
    threat_escalation: "threat-event",
    risky_action_outcome: "risky-event",
    combat_action: "combat-event",
    combat_resolution: "combat-event",
    game_over: "game-over",
  };

  function classForType(type) {
    if (TYPE_CLASS[type]) return TYPE_CLASS[type];
    if (type && type.indexOf("combat") === 0) return "combat-event";
    if (type && type.indexOf("threat") === 0) return "threat-event";
    return "narrative-event";
  }

  // Condense a long field-note paragraph down to a short, punchy beat — the
  // scene (video) is the star; the feed should read like terse dispatches, not
  // walls of text.
  function shortBeat(text) {
    const t = String(text == null ? "" : text).trim().replace(/\s+/g, " ");
    if (t.length <= 160) return t;
    // Accumulate whole sentences up to a coherent ~1–2 line beat (avoids cutting
    // on tiny leading fragments like "1993.").
    const parts = t.split(/(?<=[.!?])\s+/);
    let out = "";
    for (const s of parts) {
      if (out && out.length + 1 + s.length > 180) break;
      out = out ? out + " " + s : s;
      if (out.length >= 130) break;
    }
    if (!out) out = t.slice(0, 160);
    if (out.length > 200) out = out.slice(0, 197).trim() + "\u2026";
    return out;
  }

  // Feed lines that carry generated prose we want to keep short.
  const SHORTEN_TYPES = {
    narrative_event: 1, consequence_event: 1, vision_analysis: 1,
    suspense_event: 1, threat_escalation: 1, risky_action_outcome: 1,
  };

  function appendProse(item) {
    const div = document.createElement("div");
    div.className = `prose-entry glow-pop ${classForType(item.type)}`;
    div.dataset.itemId = item.id;
    const raw = item.content || "";
    div.innerHTML = renderInline(SHORTEN_TYPES[item.type] ? shortBeat(raw) : raw);
    el.prose.appendChild(div);
    el.prose.scrollTop = el.prose.scrollHeight + 400;
    return div;
  }

  function renderChoices(promptItem) {
    el.choices.innerHTML = "";
    if (state.gameOver) return; // death overlay owns the restart action
    if (!promptItem || !Array.isArray(promptItem.choices)) return;
    promptItem.choices.forEach((choice, idx) => {
      const btn = document.createElement("button");
      btn.className = "choice-btn";
      btn.style.animationDelay = `${idx * 70}ms`; // staggered pop-in cascade
      btn.innerHTML = `<span class="choice-num">${idx + 1}</span><span>${renderInline(choice.text)}</span>`;
      btn.addEventListener("click", () => {
        if (state.processing || state.gameOver) return;
        Sound.select();
        btn.classList.add("picked");
        makeChoice(choice.text, promptItem.id);
      });
      el.choices.appendChild(btn);
    });
  }

  function enterGameOver(message) {
    state.gameOver = true;
    state.awaitingResolution = false;
    hideVeil();
    el.choices.innerHTML = "";
    if (message) el.deathMessage.innerHTML = renderInline(message);
    el.deathOverlay.classList.remove("hidden");
    // Stop the realtime model generating behind the death overlay (cost + it
    // would keep drifting the world while the run is over).
    if (Renderer.mode === "reactor" && Renderer.reactorAvailable()) {
      try { window.ReactorRenderer.pause(); } catch (_) {}
    }
  }

  function exitGameOver() {
    state.gameOver = false;
    el.deathOverlay.classList.add("hidden");
  }

  function renderItem(item) {
    if (!item || typeof item.id !== "number") return;
    // Dedup: never render the same server feed item twice (guards against
    // overlapping polls / bootstrap races that caused doubled lines).
    if (item.id >= 0) {
      if (state.renderedIds.has(item.id)) return;
      state.renderedIds.add(item.id);
    }
    if (item.id > state.lastId) state.lastId = item.id;

    if (item.image_url || (item.metadata && item.metadata.prompt)) {
      // The world's new composition is being submitted to the renderer — this
      // IS the "world updating" beat (prompt + seed pushed to the model).
      if (state.awaitingResolution && item.metadata && item.metadata.prompt) {
        Ceremony.reach("world_update");
      }
      Renderer.applyScene(item.image_url, item.metadata && item.metadata.prompt, item.metadata);
    }

    switch (item.type) {
      case "scene_image":
        // The image itself is the payload (handled above by setScene). Its
        // placeholder content ("The scene shifts...") is intentionally NOT
        // added to the prose feed — it would just be noise over the art.
        Sound.scene(); // audible cue that the scene has materialised
        // The world has responded (a new composition is on screen); the game
        // is now generating the next set of actions.
        if (state.awaitingResolution) {
          Ceremony.reach("world_respond");
          Ceremony.reach("actions");
        }
        // The new frame is on screen — fade the progress bar back to the play
        // button (once the pipeline has also resolved).
        Ceremony.imageLoaded();
        // Auto-play: the new frame just rendered — submit the next turn now
        // (minus a tiny beat), so advancement is gated only by generation lag.
        if (state.autoPlay) scheduleAutoAdvance(AUTOPLAY_FRAME_DELAY_MS);
        return;

      case "game_over":
        appendProse(item);
        Sound.death();
        Ceremony.abort();
        setAutoPlay(false); // stop the world advancing once you're dead
        enterGameOver(item.content);
        return;

      case "player_choice_prompt":
        // The engine pairs a death with a "GAME OVER" restart prompt; when
        // we're in the death state we let the overlay own restart instead.
        if (state.gameOver || (item.content || "").toUpperCase() === "GAME OVER") {
          state.gameOver = true;
          el.deathOverlay.classList.remove("hidden");
          hideVeil();
          return;
        }
        appendProse(item);
        renderChoices(item);
        Sound.choices();
        state.awaitingResolution = false;
        // Turn fully resolved: march the ceremony to its finish, flash green,
        // then clear it — choices are live so input is released.
        Ceremony.complete();
        // New decision point: these are now the live/latest choices. Auto-play
        // will advance against THIS prompt (and only once).
        state.currentPromptId = item.id;
        refreshStatus(); // reflect turn/chaos/inventory promptly, not on the 4s tick
        // Feed the settled video frame back into the sim so choices match what's
        // actually on screen (realtime "vision"). No-op outside realtime mode.
        Renderer.observeScene(item.id);
        // Prefer advancing when this turn's frame renders; fall back if none.
        scheduleAutoAdvance();
        return;

      case "choices_revised":
        // Realtime "vision" result: choices regenerated from the actual video
        // frame. Swap them in (no prose) only if we're still on that decision
        // point and idle, so they now match what's on screen.
        if (!state.gameOver && !state.processing &&
            item.metadata && item.metadata.prompt_id === state.currentPromptId &&
            Array.isArray(item.choices) && item.choices.length) {
          renderChoices({ id: state.currentPromptId, choices: item.choices });
        }
        return;

      case "error_event":
        appendProse(item);
        Sound.error();
        Ceremony.abort();
        hideVeil();
        state.awaitingResolution = false;
        return;

      case "inventory_pickup":
      case "inventory_full":
        appendProse(item);
        Sound.pickup();
        refreshStatus(); // update the inventory HUD right away
        return;

      default:
        // Narrative / world-building text lands with a soft blip.
        appendProse(item);
        Sound.text();
        // The consequence prose for this turn just landed → mark the
        // "consequence generated" beat. (The player_action echo is not a
        // consequence, so it doesn't advance the pipeline.)
        if (state.awaitingResolution && item.type !== "player_action") {
          Ceremony.reach("consequence");
        }
        return;
    }
  }

  function renderItems(items) {
    (items || []).forEach(renderItem);
  }

  // ------------------------------------------------------------------
  // Inventory HUD
  // ------------------------------------------------------------------

  function renderInventory(items) {
    const inv = Array.isArray(items) ? items : [];
    el.inventoryList.innerHTML = "";
    if (!inv.length) {
      el.inventoryHud.classList.add("hidden");
      return;
    }
    inv.forEach((it) => {
      const li = document.createElement("li");
      const emoji = it.emoji ? `${it.emoji} ` : "";
      li.textContent = `${emoji}${it.display || it.id || ""}`;
      el.inventoryList.appendChild(li);
    });
    el.inventoryHud.classList.remove("hidden");
  }

  // ------------------------------------------------------------------
  // Game actions
  // ------------------------------------------------------------------

  async function resetGame() {
    try {
      stopPolling(); // avoid a mid-reset poll racing the rebuilt feed
      exitGameOver();
      // Wipe the current visuals IMMEDIATELY and permanently: blank both still
      // layers and reset the realtime world model (which hides + suppresses the
      // live video and drains its queue). This runs regardless of the active
      // renderer so nothing from the dead run — image or video — can linger or
      // come back after the restart.
      clearSceneLayers();
      glitchTransition(820); // VCR static over the wipe so the cut isn't abrupt
      hideGuideThumbnail();
      if (Renderer.reactorAvailable()) {
        try { window.ReactorRenderer.reset(); } catch (_) {}
      }
      Renderer.lastScene = null;
      Renderer.lastBase = null;
      Renderer.observedPromptId = null;
      clearTimeout(state.observeTimer);
      Sound.start(); // new tape / game begins
      Ceremony.abort(); // cancel any mid-turn pipeline from the prior run
      showVeil("Reawakening the tape...");
      el.prose.innerHTML = "";
      el.choices.innerHTML = "";
      state.lastId = 0;
      state.renderedIds = new Set();
      state.awaitingResolution = false;
      state.gameOver = false;
      state.currentPromptId = null;
      state.lastAdvancedPromptId = null;
      clearTimeout(state.autoTimer);
      closeFreeWill(true);
      renderInventory([]);
      startTimecode();
      const items = await postJSON("/api/reset", {});
      renderItems(items);
      hideVeil();
      refreshStatus();
    } catch (err) {
      console.error("[standalone] resetGame failed:", err);
      hideVeil();
      appendProse({ id: -1, type: "error_event", content: `Could not reach the server: ${err.message}` });
    } finally {
      startPolling(); // resume normal polling once the fresh feed is in
    }
  }

  async function makeChoice(choiceText, contextItemId) {
    if (state.processing || state.gameOver) return;
    closeFreeWill(true); // picking any action closes the free-will gate
    el.choices.innerHTML = "";
    Ceremony.begin(); // light up the turn pipeline — starting with "action selected"
    state.awaitingResolution = true;
    // NOTE: we deliberately do NOT steer the CURRENT live video with the action
    // here. Injecting the action into the video the instant a choice is made
    // meant the ORIGINAL scene's video started playing the action before its new
    // guide image had formed — which looked wrong. The action is now applied
    // only when the new guide image has been generated and its video is running:
    // the re-anchor (see Renderer.applyScene / ReactorRenderer) starts the new
    // guide-image stream with the render prompt, which already carries this
    // action beat (build_realtime_prompt, server-side).
    try {
      const items = await postJSON("/api/choose", {
        choice: choiceText,
        context_item_id: contextItemId,
      });
      renderItems(items); // immediately shows the player_action echo
      beginFastPolling();
    } catch (err) {
      console.error("[standalone] makeChoice failed:", err);
      hideVeil();
      state.awaitingResolution = false;
      appendProse({ id: -1, type: "error_event", content: `Action failed to send: ${err.message}` });
    }
  }

  // ------------------------------------------------------------------
  // Free-will gate — the custom action input hides behind a button, so
  // typing your own action is a deliberate, satisfying choice.
  // ------------------------------------------------------------------
  function openFreeWill() {
    if (state.processing || state.gameOver || state.freeWillOpen) return;
    state.freeWillOpen = true;
    el.actionWheel.classList.add("fw-open");
    Sound.open();
    // Focus after the expand animation starts so the caret lands cleanly.
    setTimeout(() => el.customInput.focus(), 60);
  }

  function closeFreeWill(clear) {
    if (!state.freeWillOpen) return;
    state.freeWillOpen = false;
    el.actionWheel.classList.remove("fw-open");
    if (clear) el.customInput.value = "";
    if (document.activeElement === el.customInput) el.customInput.blur();
    if (el.actionWheel) el.actionWheel.style.bottom = ""; // drop any keyboard offset
  }

  // "Move forward" — commit to one of the generated actions at random.
  function moveForward() {
    if (state.processing || state.gameOver || state.freeWillOpen) return;
    const btns = Array.from(el.choices.children);
    if (!btns.length) return;
    const pick = btns[Math.floor(Math.random() * btns.length)];
    pick.click(); // reuses the choice flow (select sound, pick flash, makeChoice)
  }

  // ------------------------------------------------------------------
  // Auto-play — the world advances on its own via the forward hub
  // ------------------------------------------------------------------
  function scheduleAutoAdvance(delay) {
    clearTimeout(state.autoTimer);
    if (!state.autoPlay) return;
    state.autoTimer = setTimeout(() => {
      // Advance only when idle AND we haven't already advanced this decision
      // point — so a late/stale frame can't fire against newer choices, and we
      // always commit one of the CURRENT prompt's choices (frame + choices in
      // lockstep).
      if (state.autoPlay && !state.processing && !state.gameOver &&
          !state.freeWillOpen && !tapeIsOpen() &&
          el.choices.children.length &&
          state.currentPromptId != null &&
          state.currentPromptId !== state.lastAdvancedPromptId) {
        state.lastAdvancedPromptId = state.currentPromptId;
        moveForward();
      }
    }, delay == null ? AUTOPLAY_FALLBACK_MS : delay);
  }

  function setAutoPlay(on) {
    state.autoPlay = on;
    el.autoplayBtn.classList.toggle("on", on);
    el.autoplayLabel.textContent = on ? "STOP" : "AUTO";
    el.autoplayBtn.title = on ? "Stop auto-play (P)" : "Auto-play — advance on its own (P)";
    if (on) scheduleAutoAdvance(AUTOPLAY_FRAME_DELAY_MS); // start advancing right away
    else clearTimeout(state.autoTimer);  // pause
  }

  function toggleAutoPlay() {
    Sound.toggle();
    setAutoPlay(!state.autoPlay);
  }

  function submitCustomAction(e) {
    e.preventDefault();
    const text = el.customInput.value.trim();
    if (!text || state.processing || state.gameOver) return;
    el.customInput.value = "";
    Sound.submit(); // custom free-will action sent
    closeFreeWill(true); // gate closes on submit
    makeChoice(text, null);
  }

  // ------------------------------------------------------------------
  // Polling
  // ------------------------------------------------------------------

  async function pollOnce() {
    if (state.polling) return;
    state.polling = true;
    try {
      const items = await getJSON(`/api/feed?since_id=${state.lastId}`);
      if (Array.isArray(items) && items.length) {
        renderItems(items);
      }
    } catch (err) {
      console.error("[standalone] pollFeed failed:", err);
    } finally {
      state.polling = false;
    }
  }

  function startPolling() {
    stopPolling();
    state.pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
  }

  function stopPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = null;
  }

  /**
   * After a player choice, the engine resolves the turn on a background
   * thread, so we poll faster for a while to feel responsive, then fall
   * back to the normal cadence whether or not the prompt arrived (the
   * normal poller will pick up anything we missed).
   */
  function beginFastPolling() {
    stopPolling();
    const startedAt = Date.now();
    state.pollTimer = setInterval(async () => {
      await pollOnce();
      const timedOut = Date.now() - startedAt > FAST_POLL_TIMEOUT_MS;
      if (!state.awaitingResolution || timedOut) {
        if (timedOut) hideVeil();
        startPolling();
      }
    }, FAST_POLL_INTERVAL_MS);
  }

  // Update a HUD value element, and glow-pop it if the value actually changed.
  function setHud(node, key, value) {
    if (!node) return false; // top status bar was removed; no-op
    const str = String(value);
    if (node.textContent !== str) {
      node.textContent = str;
      if (state.lastStatus[key] !== undefined && state.lastStatus[key] !== str) {
        node.classList.remove("bumped");
        void node.offsetWidth; // restart the animation
        node.classList.add("bumped");
        return true; // changed
      }
    }
    return false;
  }

  async function refreshStatus() {
    try {
      const s = await getJSON("/api/status");
      let changed = false;
      changed = setHud(el.hudTurn, "turn", s.turn ?? 0) || changed;
      changed = setHud(el.hudChaos, "chaos", s.chaos ?? 0) || changed;
      const phaseText = s.alive === false ? "deceased" : (s.phase ?? "normal");
      changed = setHud(el.hudPhase, "phase", phaseText) || changed;
      if (el.backendName) el.backendName.textContent = s.backend ?? "unknown";
      renderInventory(s.inventory);
      if (el.hudTimeWrap && el.hudTime) {
        if (s.time_of_day) {
          el.hudTime.textContent = s.time_of_day;
          el.hudTimeWrap.classList.remove("hidden");
        } else {
          el.hudTimeWrap.classList.add("hidden");
        }
      }
      // Detect world-state changes independent of the (removed) top HUD and
      // give them audible feedback: a rising sting when the phase escalates,
      // a subtle tick when chaos climbs.
      const prev = state.lastStatus || {};
      const PHASE_RANK = { normal: 0, escalating: 1, critical: 2, deceased: 3 };
      const escalated = prev.phase && phaseText !== prev.phase &&
        (PHASE_RANK[phaseText] ?? 0) > (PHASE_RANK[prev.phase] ?? 0) && phaseText !== "deceased";
      const chaosUp = prev.chaos !== undefined && (s.chaos ?? 0) > Number(prev.chaos);
      state.lastStatus = { turn: String(s.turn ?? 0), chaos: String(s.chaos ?? 0), phase: phaseText };
      if (escalated) Sound.escalate();
      else if (chaosUp || changed) Sound.status();
    } catch (err) {
      if (el.backendName) el.backendName.textContent = "offline";
    }
  }

  function startStatusPolling() {
    if (state.statusTimer) clearInterval(state.statusTimer);
    state.statusTimer = setInterval(refreshStatus, STATUS_INTERVAL_MS);
  }

  // ------------------------------------------------------------------
  // VHS toggle
  // ------------------------------------------------------------------

  function toggleVhs() {
    state.vhsEnabled = !state.vhsEnabled;
    el.vhsOverlay.classList.toggle("vhs-on", state.vhsEnabled);
    Sound.toggle();
  }

  // ------------------------------------------------------------------
  // VHS tape playback — replay this run's frames in sequence
  // ------------------------------------------------------------------
  const tape = { frames: [], idx: 0, playing: false, timer: null, clock: null, seconds: 0, active: "A" };

  function tapeIsOpen() { return el.tapeOverlay && !el.tapeOverlay.classList.contains("hidden"); }

  async function openTape() {
    if (tapeIsOpen()) return;
    Sound.start();
    try {
      const data = await getJSON("/api/tape");
      tape.frames = (data && Array.isArray(data.frames)) ? data.frames : [];
    } catch (err) {
      tape.frames = [];
    }
    el.tapeOverlay.classList.remove("hidden");
    if (!tape.frames.length) {
      el.tapeEmpty.classList.remove("hidden");
      el.tapeCounter.textContent = "FRAME 0 / 0";
      el.tapeRec.textContent = "\u25A0 NO TAPE";
      return;
    }
    el.tapeEmpty.classList.add("hidden");
    tape.idx = 0; tape.active = "A";
    el.tapeFrameA.style.backgroundImage = "";
    el.tapeFrameB.style.backgroundImage = "";
    el.tapeFrameA.classList.add("tape-frame-active");
    el.tapeFrameB.classList.remove("tape-frame-active");
    showTapeFrame(0);
    startTapeClock();
    startTapePlay();
  }

  function showTapeFrame(i) {
    tape.idx = Math.max(0, Math.min(i, tape.frames.length - 1));
    const url = tape.frames[tape.idx];
    const incoming = tape.active === "A" ? el.tapeFrameB : el.tapeFrameA;
    const outgoing = tape.active === "A" ? el.tapeFrameA : el.tapeFrameB;
    incoming.style.backgroundImage = `url('${url}')`;
    incoming.classList.add("tape-frame-active");
    outgoing.classList.remove("tape-frame-active");
    tape.active = tape.active === "A" ? "B" : "A";
    el.tapeCounter.textContent = `FRAME ${tape.idx + 1} / ${tape.frames.length}`;
    Sound.scene();
  }

  function startTapePlay() {
    tape.playing = true;
    el.tapePlayPause.textContent = "\u23F8"; // pause glyph
    el.tapeRec.textContent = "\u25B6 PLAY";
    clearInterval(tape.timer);
    tape.timer = setInterval(() => {
      if (tape.idx >= tape.frames.length - 1) { pauseTape(true); return; }
      showTapeFrame(tape.idx + 1);
    }, 1300);
  }

  function pauseTape(ended) {
    tape.playing = false;
    clearInterval(tape.timer); tape.timer = null;
    el.tapePlayPause.textContent = "\u23F5"; // play glyph
    el.tapeRec.textContent = ended ? "\u23F9 END" : "\u23F8 PAUSE";
  }

  function toggleTapePlay() {
    if (!tape.frames.length) return;
    if (tape.playing) { pauseTape(false); Sound.toggle(); return; }
    if (tape.idx >= tape.frames.length - 1) showTapeFrame(0); // replay from the top
    Sound.toggle();
    startTapePlay();
  }

  function tapeStep(delta) {
    if (!tape.frames.length) return;
    pauseTape(false);
    showTapeFrame(tape.idx + delta);
  }

  function startTapeClock() {
    clearInterval(tape.clock);
    tape.seconds = 0;
    el.tapeTime.textContent = fmtTimecode(0);
    tape.clock = setInterval(() => {
      tape.seconds += 1;
      el.tapeTime.textContent = fmtTimecode(tape.seconds);
    }, 1000);
  }

  function closeTape() {
    clearInterval(tape.timer); tape.timer = null;
    clearInterval(tape.clock); tape.clock = null;
    tape.playing = false;
    el.tapeOverlay.classList.add("hidden");
    Sound.toggle();
  }

  function toggleSound() {
    state.soundEnabled = !state.soundEnabled;
    el.btnSnd.classList.toggle("off", !state.soundEnabled);
    const ico = el.btnSnd.querySelector(".rail-ico");
    if (ico) ico.textContent = state.soundEnabled ? "\u266A" : "\u2715"; // ♪ / ✕
    if (state.soundEnabled) { Sound.resume(); Sound.select(); }
  }

  function initVhsGrain() {
    const canvas = document.getElementById("vhs-grain");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    // Render grain at a low resolution and let CSS stretch it to full screen.
    // Grain noise looks identical scaled up, and this keeps the per-frame
    // pixel fill cheap — critical for smoothness on phones.
    const MAX_DIM = 360;
    function resize() {
      const w = window.innerWidth, h = window.innerHeight;
      const scale = Math.min(1, MAX_DIM / Math.max(w, h));
      canvas.width = Math.max(1, Math.round(w * scale));
      canvas.height = Math.max(1, Math.round(h * scale));
    }
    resize();
    window.addEventListener("resize", resize);

    function drawNoise() {
      if (!state.vhsEnabled) return; // no work when the overlay is off
      const w = canvas.width, h = canvas.height;
      if (!w || !h) return;
      const imgData = ctx.createImageData(w, h);
      const buf = new Uint32Array(imgData.data.buffer);
      for (let i = 0; i < buf.length; i++) {
        const shade = (Math.random() * 255) | 0;
        buf[i] = (255 << 24) | (shade << 16) | (shade << 8) | shade;
      }
      ctx.putImageData(imgData, 0, 0);
    }
    setInterval(drawNoise, 90);
  }

  // Keep the free-will input visible above the on-screen keyboard on mobile.
  function initKeyboardInset() {
    const vv = window.visualViewport;
    if (!vv) return;
    const adjust = () => {
      if (!el.actionWheel) return;
      if (!state.freeWillOpen) {
        el.actionWheel.style.bottom = "";
        return;
      }
      const keyboard = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
      el.actionWheel.style.bottom = keyboard > 80 ? `${keyboard + 8}px` : "";
    };
    vv.addEventListener("resize", adjust);
    vv.addEventListener("scroll", adjust);
  }

  // ------------------------------------------------------------------
  // Keyboard shortcuts
  // ------------------------------------------------------------------

  function onKeydown(e) {
    // Tape playback owns the keyboard while open.
    if (tapeIsOpen()) {
      if (e.key === "Escape" || e.key.toLowerCase() === "t") closeTape();
      else if (e.key === " " || e.key === "Spacebar") { e.preventDefault(); toggleTapePlay(); }
      else if (e.key === "ArrowLeft") tapeStep(-1);
      else if (e.key === "ArrowRight") tapeStep(1);
      return;
    }
    if (document.activeElement === el.customInput) {
      if (e.key === "Escape") closeFreeWill(true); // Esc closes the gate
      return;
    }
    // While dead, only R (restart) is meaningful.
    if (state.gameOver) {
      if (e.key.toLowerCase() === "r") resetGame();
      return;
    }
    if (e.key === "1" || e.key === "2" || e.key === "3") {
      const idx = Number(e.key) - 1;
      const btn = el.choices.children[idx];
      if (btn) btn.click();
    } else if (e.key.toLowerCase() === "r") {
      resetGame();
    } else if (e.key.toLowerCase() === "v") {
      toggleVhs();
    } else if (e.key.toLowerCase() === "m") {
      toggleSound();
    } else if (e.key.toLowerCase() === "t") {
      openTape();
    } else if (e.key.toLowerCase() === "p") {
      toggleAutoPlay();
    } else if (e.key.toLowerCase() === "f") {
      openFreeWill();
    } else if (e.key.toLowerCase() === "g") {
      Renderer.toggle();
      Sound.toggle();
    } else if (e.key === "ArrowUp" || e.key === " " || e.key === "Spacebar") {
      e.preventDefault();
      moveForward();
    } else if (e.key === "Escape") {
      closeFreeWill(true);
    }
  }

  // ------------------------------------------------------------------
  // Bootstrap — decide between resuming an existing session and starting fresh
  // ------------------------------------------------------------------

  /**
   * On load, look at the existing feed. If a session is already in progress
   * (has content, including an active choice prompt or a game-over), resume it
   * so a page refresh doesn't wipe the player's run. If the session is empty
   * or has no actionable prompt (a cold visit or a stale/half-written state),
   * start a fresh game automatically so a first-time visitor immediately sees
   * the intro and choices instead of a blank screen.
   */
  async function bootstrap() {
    try {
      const items = await getJSON(`/api/feed?since_id=0`);
      if (Array.isArray(items) && items.length) {
        renderItems(items);
        const hasPrompt = items.some((i) => i.type === "player_choice_prompt");
        const isDead = items.some((i) => i.type === "game_over");
        if (hasPrompt || isDead) {
          hideVeil();
          return; // resume the in-progress run
        }
      }
    } catch (err) {
      console.error("[standalone] bootstrap feed check failed:", err);
    }
    // Cold start (or unusable session) → begin a new game.
    await resetGame();
  }

  // ------------------------------------------------------------------
  // Init
  // ------------------------------------------------------------------

  function init() {
    el.btnReset.addEventListener("click", resetGame);
    el.btnVhs.addEventListener("click", toggleVhs);
    el.btnSnd.addEventListener("click", toggleSound);
    if (el.rendererBtn) {
      el.rendererBtn.addEventListener("click", () => { Renderer.toggle(); Sound.toggle(); });
    }
    el.deathRestart.addEventListener("click", resetGame);
    el.freeWillBtn.addEventListener("click", openFreeWill);
    el.forwardBtn.addEventListener("click", moveForward);
    el.tapeBtn.addEventListener("click", openTape);
    el.tapePlayPause.addEventListener("click", toggleTapePlay);
    el.tapePrev.addEventListener("click", () => tapeStep(-1));
    el.tapeNext.addEventListener("click", () => tapeStep(1));
    el.tapeEject.addEventListener("click", closeTape);
    el.autoplayBtn.addEventListener("click", toggleAutoPlay);
    el.customForm.addEventListener("submit", submitCustomAction);
    document.addEventListener("keydown", onKeydown);

    // Browsers block audio until a user gesture; unlock the context on the
    // first interaction so feedback sounds work for the rest of the session.
    const unlockAudio = () => { Sound.resume(); };
    document.addEventListener("pointerdown", unlockAudio, { once: true });
    document.addEventListener("keydown", unlockAudio, { once: true });

    Renderer.init(); // async: resolves default renderer + warms realtime session
    initVhsGrain();
    initKeyboardInset();
    startTimecode();
    startPolling();
    startStatusPolling();
    refreshStatus();

    // Resume an in-progress run if one exists, otherwise auto-start a fresh
    // game so a first-time visitor is never greeted by a blank screen.
    bootstrap();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
