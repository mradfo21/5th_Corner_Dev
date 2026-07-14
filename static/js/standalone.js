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
  const AUTOPLAY_FRAME_DELAY_MS = 350;  // (image mode) advance almost immediately once the new still renders
  const AUTOPLAY_FALLBACK_MS = 6000;    // if no image arrives, advance anyway after this
  // Realtime auto-play: the "frame" is the live video, not the scene_image feed
  // item. Advance only after the NEW scene's video is actually on screen, then
  // let it PLAY for this watch window so the world visibly evolves. Advancing off
  // the scene_image feed item (before the re-anchor established) stacked resets
  // faster than the model could re-stage and blacked out the stream.
  const AUTOPLAY_REALTIME_WATCH_MS = (typeof window !== "undefined" && window.__AUTOPLAY_WATCH_MS__) || 7000;   // let the live video play this long before advancing
  const AUTOPLAY_REALTIME_MAX_WAIT_MS = 20000; // never wait longer than this for the video to appear
  const REALTIME_MAX_RETRIES = 3; // transient realtime errors retry before falling back to stills

  // Show a small thumbnail preview of each guide image as it's integrated into
  // the realtime world model. Flip to false (or set localStorage
  // "guide_thumbnail" = "off", toggled by the thumbnail's own hide button) to
  // hide it — the notification + re-anchor still happen either way.
  const GUIDE_THUMBNAIL_ENABLED = true;

  // Debug/verification preview: show the EXACT realtime frame captured at
  // act-time and sent to the server as the primary img2img reference, so it's
  // visually verifiable that the live world-model texture (not a stale still)
  // is what actually drives the next guide image. Toggle off via build flag or
  // localStorage "capture_thumbnail" = "off" (the preview's own ✕ button).
  const CAPTURE_THUMBNAIL_ENABLED = true;

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
    reactorVideo: document.getElementById("reactor-video"),
    reactorFreeze: document.getElementById("reactor-freeze"),
    sceneFlash: document.getElementById("scene-flash"),
    sceneGlitch: document.getElementById("scene-glitch"),
    prose: document.getElementById("prose-feed"),
    choices: document.getElementById("choices-container"),
    customForm: document.getElementById("custom-form"),
    customInput: document.getElementById("custom-input"),
    freeWillBtn: document.getElementById("free-will-btn"),
    realtimeBtn: document.getElementById("realtime-btn"),
    touchLayer: document.getElementById("touch-layer"),
    touchReticle: document.getElementById("touch-reticle"),
    touchForm: document.getElementById("touch-form"),
    touchInput: document.getElementById("touch-input"),
    scanLayer: document.getElementById("scan-layer"),
    scanTags: document.getElementById("scan-tags"),
    scanHint: document.getElementById("scan-hint"),
    touchCaptureFrame: document.getElementById("touch-capture-frame"),
    touchHint: document.getElementById("touch-hint"),
    touchZoom: document.getElementById("touch-zoom"),
    touchTargets: document.getElementById("touch-targets"),
    touchLock: document.getElementById("touch-lock"),
    evidenceCard: document.getElementById("evidence-card"),
    evidenceHud: document.getElementById("evidence-hud"),
    photoReceipt: document.getElementById("photo-receipt"),
    caseOverlay: document.getElementById("case-overlay"),
    caseRankLetter: document.getElementById("case-rank-letter"),
    caseSubjects: document.getElementById("case-subjects"),
    caseEvidence: document.getElementById("case-evidence"),
    caseShots: document.getElementById("case-shots"),
    caseFlavor: document.getElementById("case-flavor"),
    caseContinue: document.getElementById("case-continue"),
    caseRestart: document.getElementById("case-restart"),
    investigationsTray: document.getElementById("investigations-tray"),
    investigationsStrip: document.getElementById("investigations-strip"),
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
    menuToggle: document.getElementById("menu-toggle"),
    controlRail: document.getElementById("control-rail"),
    btnModel: document.getElementById("btn-model"),
    rtModelAdd: document.getElementById("rt-model-add"),
    rtModelInput: document.getElementById("rt-model-input"),
    vhsOverlay: document.getElementById("vhs-overlay"),
    backendName: document.getElementById("backend-name"),
    timecodeText: document.getElementById("timecode-text"),
    inventoryHud: document.getElementById("inventory-hud"),
    inventoryList: document.getElementById("inventory-list"),
    rtLog: document.getElementById("rt-log"),
    rtLogList: document.getElementById("rt-log-list"),
    rtLogHide: document.getElementById("rt-log-hide"),
    rtLogModels: document.getElementById("rt-log-models"),
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
    inputMode: "act",           // custom input intent: "act" (full turn) | "steer" (realtime nudge)
    touchMode: null,            // TOUCH tool state: null | "aim" (reticle tracks cursor) | "prompt" (spot locked, field open)
    touchPoint: null,           // {x, y} viewport coords of the reticle / locked spot
    photoZoom: 1,               // optical zoom magnification while the camera is armed (1..PHOTO_ZOOM_MAX)
    photoPointers: new Map(),   // active pointers on the camera layer, for pinch-to-zoom
    pinchBase: null,            // {dist, zoom} anchor captured when a two-finger pinch begins
    pinchActive: false,         // true once 2 fingers are down (suppresses the shot until release)
    receiptTimers: [],          // pending setTimeouts driving the sequential receipt reveal
    receiptToken: 0,            // bumped per capture so a newer shot cancels an older reveal
    caseWon: false,             // the dossier census hit its target this run (win fired)
    photoTargets: [],           // detected photographable subjects while the camera is armed
    photoDetected: false,       // at least one detection has returned this arming session
    photoDetectBusy: false,     // a /api/detect for photo targeting is in flight
    photoDetectTimer: null,     // idle re-detect loop while armed
    photoDetectLast: 0,         // last time we hit /api/detect for photo targeting
    photoLockedLabel: null,     // label of the subject currently framed (locked)
    pendingInvestigation: null, // {screen, region, texture} captured at TOUCH lock, finalized on submit
    selectedInvestigation: null,// a specimen chosen from the tray to inform the next action
    scanOn: false,              // ambient hotspot overlay live (object tags over the scene)
    scanBusy: false,            // a detection request is in flight
    scanObjects: [],            // last detected objects (normalized coords + labels)
    scanTagActing: null,        // tag element with its inline action bar open
    scanMoveTimer: null,        // debounced re-detect after the cursor settles (realtime)
    scanSrcSize: null,          // {w,h} of the last scanned source (video or still), for cover-mapping tags
    scanPrewarm: { objects: [], size: null, ts: 0 }, // last detection cached (renders hotspots instantly)
    scanPrewarmTimer: null,     // debounce for detection passes
    lastTurnTs: 0,              // when the last turn was committed/active (pre-warm defers around it)
    turnWatchdog: null,         // safety timer: recover the UI if a turn never resolves
    currentStillUrl: null,      // last still shown via setScene (stills-mode SCAN source)
    scanStillImg: null,         // cached <img> of the current still, for canvas capture
    evidenceTimer: null,        // evidence flourish hold-then-file timer
    autoPlay: false,
    autoTimer: null,
    autoDeadline: 0,            // realtime: latest time we'll wait for the new video before advancing anyway
    currentPromptId: null,     // id of the latest choice prompt (the live decision point)
    lastAdvancedPromptId: null, // guard: auto-advance at most once per prompt
    observeTimer: null,         // debounce for feeding the video frame to the sim
    observedPromptId: null,     // guard: observe at most once per decision point
    turnResolved: false,        // the turn's pipeline finished (choices are live)
    turnImageLoaded: false,     // this turn's new frame has arrived on screen
    imagesEnabled: true,        // server has image gen on (from /api/status) — else skip the guide-image wait
    finishTimer: null,          // fallback: fade the progress bar back to play
    sceneVisible: false,        // has the first realtime feed / still appeared this run? (gates the prose + SNAP tool on boot)
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
      // Shared, gesture-unlocked AudioContext so the ambient scene score
      // (SceneAudio) rides the same mute + first-gesture gating as the SFX.
      context() { return ensure(); },
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
      // ---- Universal tactile feedback: a faint detent as the pointer crosses a
      // control, a crisp mechanical click on press, and a little servo for the
      // menu — so every surface of the game feels physical to operate. ----
      hover() { tone(2050, 0.014, "sine", 0.012); },           // pointer enters a control — soft detent
      focusTick() { tone(1500, 0.02, "sine", 0.018); },        // keyboard focus lands on a control
      press() { tone(1650, 0.012, "square", 0.03); noise(0.028, 0.02); }, // button press — mechanical click
      menuOpen() { tone([440, 980], 0.13, "triangle", 0.05); tone(1320, 0.08, "sine", 0.03, 0.05); }, // menu slides open
      menuClose() { tone([940, 380], 0.13, "triangle", 0.045); }, // menu tucks away
      scan() { tone([320, 1180], 0.34, "sine", 0.028); tone(1180, 0.12, "sine", 0.02, 0.24); }, // SCAN armed — radar sweep
      ping() { tone([1300, 1850], 0.10, "sine", 0.03); tone(2500, 0.07, "sine", 0.018, 0.05); }, // tags land — starfield shimmer
      grab() { tone(900, 0.03, "square", 0.045); tone([700, 340], 0.10, "triangle", 0.04, 0.02); }, // TOUCH specimen captured
      shutter() { tone(1500, 0.015, "square", 0.055); noise(0.05, 0.035); tone(760, 0.03, "square", 0.05, 0.03); }, // camera shutter
      // ---- Photo receipt: printing, per-item reveals, score rolls, stamp ----
      receiptOpen() { noise(0.09, 0.03); tone(240, 0.05, "square", 0.03); tone(360, 0.06, "triangle", 0.03, 0.04); }, // paper feeds out
      // Each revealed item chimes a little HIGHER than the last — a rising combo.
      itemReveal(step) { const f = 680 + (step || 0) * 110; tone(f, 0.045, "square", 0.05); tone(f * 1.5, 0.07, "sine", 0.03, 0.03); },
      scoreTick() { tone(1500, 0.02, "square", 0.028); },        // rolling score counter blip
      stamp() { tone([170, 80], 0.16, "sawtooth", 0.07); noise(0.06, 0.05); tone(90, 0.12, "sine", 0.05, 0.02); }, // rating stamp thunk
      zoom(t) { const f = 420 + Math.max(0, Math.min(1, t || 0)) * 900; tone(f, 0.03, "sine", 0.025); }, // lens zoom tick
      lock() { tone([900, 1350], 0.05, "sine", 0.03); }, // a subject snaps into frame (worthy)
      miss() { tone([300, 150], 0.14, "sine", 0.045); noise(0.05, 0.02); }, // empty frame — no subject
      newSubject() { tone([700, 1150], 0.10, "triangle", 0.05); tone(1500, 0.10, "sine", 0.04, 0.08); tone(1950, 0.12, "sine", 0.03, 0.16); }, // NEW subject filed to the case
      caseSolved() { // dossier complete — a rising, triumphant fanfare
        tone([300, 620], 0.18, "triangle", 0.06); tone([620, 930], 0.2, "triangle", 0.055, 0.14);
        tone([930, 1400], 0.28, "sine", 0.05, 0.3); tone(1860, 0.5, "sine", 0.045, 0.5); noise(0.12, 0.03);
      },
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
  // SceneAudio — generated ambient score for the current guide image.
  //
  // Each new scene carries a text descriptor (metadata.prompt). We POST it to
  // /api/scene_audio, which renders a short scene-matched instrumental clip with
  // Google Lyria RealTime and returns a URL. We loop that clip as an ambient bed
  // and crossfade to a fresh one whenever the world re-scores. Shares the Sound
  // synth's AudioContext so it inherits the same mute + first-gesture gating and
  // silently no-ops when audio is unavailable (no key / offline / stream fail).
  // ------------------------------------------------------------------
  const SceneAudio = (function () {
    let currentUrl = null;      // audio_url currently playing (guards re-triggers)
    let requestedKey = null;    // last descriptor we requested (dedupe re-renders)
    let src = null;             // active looping AudioBufferSourceNode
    let gain = null;            // its GainNode
    const bufferCache = new Map(); // url -> decoded AudioBuffer
    const TARGET_VOL = 0.26;    // sit UNDER the UI SFX — it's a bed, not a lead
    const FADE = 1.4;           // crossfade seconds between scene scores

    function ctx() {
      try { return Sound.context ? Sound.context() : null; } catch (_) { return null; }
    }

    async function fetchBuffer(url) {
      if (bufferCache.has(url)) return bufferCache.get(url);
      const resp = await fetch(url);
      if (!resp.ok) throw new Error("audio HTTP " + resp.status);
      const arr = await resp.arrayBuffer();
      const c = ctx();
      if (!c) throw new Error("no audio context");
      const buf = await c.decodeAudioData(arr);
      bufferCache.set(url, buf);
      return buf;
    }

    function stop(fadeOut) {
      const oldSrc = src, oldGain = gain;
      src = null; gain = null;
      if (!oldSrc) return;
      const c = ctx();
      try {
        if (c && oldGain) {
          const t = c.currentTime;
          const d = fadeOut == null ? FADE : fadeOut;
          oldGain.gain.cancelScheduledValues(t);
          oldGain.gain.setValueAtTime(oldGain.gain.value, t);
          oldGain.gain.linearRampToValueAtTime(0.0001, t + d);
          oldSrc.stop(t + d + 0.05);
        } else {
          oldSrc.stop();
        }
      } catch (_) {}
    }

    function playBuffer(buf) {
      const c = ctx();
      if (!c || !buf) return;
      const s = c.createBufferSource();
      s.buffer = buf;
      s.loop = true;
      const g = c.createGain();
      const t = c.currentTime;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.linearRampToValueAtTime(TARGET_VOL, t + FADE);
      s.connect(g); g.connect(c.destination);
      try { s.start(); } catch (_) { return; }
      src = s; gain = g;
    }

    async function crossfadeTo(url) {
      try {
        const buf = await fetchBuffer(url);
        if (!state.soundEnabled) return;   // muted while we were fetching
        stop(FADE);
        playBuffer(buf);
        currentUrl = url;
      } catch (_) { /* stay silent on any audio failure */ }
    }

    // ── Increment 2: realtime streaming backend (opt-in via ?music=stream) ──
    // Continuously plays PCM pushed from /ws/scene_music, re-steering on each
    // scene instead of looping a clip. Falls back to nothing (silent) on any
    // socket/decoder error; the clip path stays the default.
    const STREAM_MODE = (function () {
      try { return new URLSearchParams(location.search).get("music") === "stream"; }
      catch (_) { return false; }
    })();
    let ws = null;             // active WebSocket
    let streamGain = null;     // gain for streamed PCM
    let streamNextTime = 0;    // scheduling clock (AudioContext time)
    let wsOpening = false;

    function schedulePCM(arrayBuffer) {
      const c = ctx();
      if (!c || !streamGain || !arrayBuffer || arrayBuffer.byteLength < 4) return;
      const view = new DataView(arrayBuffer);
      const frames = (arrayBuffer.byteLength / 4) | 0; // 2ch * 16-bit
      if (frames <= 0) return;
      const buf = c.createBuffer(2, frames, 48000);
      const chL = buf.getChannelData(0), chR = buf.getChannelData(1);
      let o = 0;
      for (let i = 0; i < frames; i++) {
        chL[i] = view.getInt16(o, true) / 32768; o += 2;
        chR[i] = view.getInt16(o, true) / 32768; o += 2;
      }
      const s = c.createBufferSource();
      s.buffer = buf;
      s.connect(streamGain);
      const now = c.currentTime;
      // Keep a small latency cushion; re-prime if we've underrun.
      if (streamNextTime < now + 0.05) streamNextTime = now + 0.15;
      try { s.start(streamNextTime); } catch (_) { return; }
      streamNextTime += buf.duration;
    }

    function openStream(prompt) {
      const c = ctx();
      if (!c || wsOpening || (ws && ws.readyState <= 1)) return;
      wsOpening = true;
      streamGain = c.createGain();
      streamGain.gain.value = TARGET_VOL;
      streamGain.connect(c.destination);
      streamNextTime = 0;
      let sock;
      try {
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        sock = new WebSocket(proto + "//" + location.host + "/ws/scene_music");
        sock.binaryType = "arraybuffer";
      } catch (_) { wsOpening = false; return; }
      ws = sock;
      sock.onopen = () => {
        wsOpening = false;
        try { sock.send(JSON.stringify({ prompt: prompt || "" })); } catch (_) {}
      };
      sock.onmessage = (ev) => {
        if (!state.soundEnabled) return;
        if (ev.data instanceof ArrayBuffer) schedulePCM(ev.data);
      };
      sock.onerror = () => { wsOpening = false; };
      sock.onclose = () => { wsOpening = false; if (ws === sock) ws = null; };
    }

    function steerStream(prompt) {
      if (ws && ws.readyState === 1) {
        try { ws.send(JSON.stringify({ prompt: prompt || "" })); } catch (_) {}
      } else {
        openStream(prompt);
      }
    }

    function closeStream() {
      try { if (ws) ws.close(); } catch (_) {}
      ws = null; wsOpening = false;
      if (streamGain) { try { streamGain.disconnect(); } catch (_) {} streamGain = null; }
      streamNextTime = 0;
    }

    return {
      // Ask for a scene score, then crossfade (clip) or re-steer (stream).
      // Deduped so repeated renders of the same scene don't re-request.
      async score(prompt) {
        if (!state.soundEnabled) return;
        const key = (prompt == null ? "" : String(prompt)).trim().slice(0, 240);
        if (!key || key === requestedKey) return;
        requestedKey = key;
        if (STREAM_MODE) { steerStream(key); return; }
        let res;
        try {
          res = await postJSON("/api/scene_audio", { prompt: key, session: "default" });
        } catch (_) { return; }
        if (!res || !res.audio_url) return;      // unavailable — stay silent
        if (res.audio_url === currentUrl) return; // same bed already playing
        crossfadeTo(res.audio_url);
      },
      // Follow the global sound toggle: fade out on mute, resume on unmute.
      setEnabled(on) {
        if (STREAM_MODE) {
          if (!on) closeStream();
          else if (requestedKey) openStream(requestedKey);
          return;
        }
        if (!on) stop(0.4);
        else if (currentUrl) crossfadeTo(currentUrl);
      },
      // Full reset (new game): silence and forget so the next scene re-scores.
      reset() {
        stop(0.4); closeStream();
        currentUrl = null; requestedKey = null;
      },
    };
  })();
  // Expose for debugging + e2e (mirrors window.ReactorRenderer).
  try { window.SceneAudio = SceneAudio; } catch (_) {}

  // ------------------------------------------------------------------
  // Haptics — physical vibration feedback on devices that support it (mobile).
  // Independent of the sound mute so the game still feels tactile when silenced.
  // Respects prefers-reduced-motion and silently no-ops on desktop.
  // ------------------------------------------------------------------
  const Haptics = (function () {
    let enabled = true;
    try {
      enabled = !(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    } catch (_) {}
    function buzz(pattern) {
      if (!enabled) return;
      try { if (navigator && typeof navigator.vibrate === "function") navigator.vibrate(pattern); } catch (_) {}
    }
    return {
      tap: () => buzz(8),          // light press
      select: () => buzz(14),      // committing an action / choice
      strong: () => buzz([16, 24, 16]), // big moment (death / new game)
      soft: () => buzz(5),         // subtle nudge
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
    // Safety net: never leave the prose + SNAP tool stuck hidden once the boot
    // veil is gone (covers text-only mode and any path where no frame lands).
    markSceneVisible();
    Ceremony.reset();
  }

  // Boot gating: at the start of a new run we hide the narrative text and the
  // SNAP camera tool until the world is actually on screen (a still lands or the
  // realtime feed goes live), so a fresh instance doesn't show floating text and
  // a lone SNAP button over a black void while the first scene renders.
  function markSceneAwaiting() {
    state.sceneVisible = false;
    document.body.classList.add("awaiting-first-scene");
  }

  function markSceneVisible() {
    if (state.sceneVisible) return;
    state.sceneVisible = true;
    document.body.classList.remove("awaiting-first-scene");
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
      // The guide image is the slowest stage and lands AFTER choices, so it gets
      // its own step that stays "rendering" (spinning) until the still actually
      // arrives — otherwise the app looks frozen while it generates.
      { key: "guide_image",   label: "Guide Image\nRendering", glyph: "\u25A6", sound: "cereWorldUpdate" },   // ▦
    ];
    const IDX = {};
    STEPS.forEach((s, i) => { IDX[s.key] = i; });
    const IMG_STEP = STEPS.length - 1;      // the guide-image step (last)
    const DWELL_MS = 460;      // minimum time each step is shown (so it registers)
    // After the turn resolves we keep the (green) progress bar in the play
    // button's spot until the new frame actually loads, then fade back to play.
    const FADE_AFTER_IMAGE_MS = 520;   // brief hold once the image is on screen
    // The guide-image step spins until the still lands. Image gen can be slow —
    // that's the whole point of showing it — so give it a generous window before
    // we resolve anyway (so a failed/absent image can't spin forever).
    const GUIDE_IMAGE_FALLBACK_MS = 30000;

    let built = false;
    let active = false;
    let cur = -1;        // index of the currently active step
    let target = -1;     // furthest step requested
    let dwellTimer = null;
    let doneTimer = null;
    let noteTimer = null;
    let completing = false; // animating through the logic steps toward the guide-image wait
    let awaitingImage = false; // parked on the guide-image step, waiting for the still

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
      if (cur >= target) {                    // caught up on the logic steps
        if (completing) enterGuideImageWait(); // …now park on the guide-image step
        return;
      }
      enter(cur + 1);
      dwellTimer = setTimeout(() => { dwellTimer = null; pump(); }, DWELL_MS);
    }

    // Once the logic steps (…Actions Generating) have animated through, PARK on
    // the guide-image step as an active spinner — so the wait for the (slow)
    // still reads as live progress, not a frozen app. Resolves when the image
    // arrives (imageLoaded) or the fallback fires.
    function enterGuideImageWait() {
      completing = false;
      awaitingImage = true;
      enter(IMG_STEP); // guide-image dot goes active/pulsing
      api.note("\u25A6 Rendering the guide image\u2026", { tick: false });
      // If the still is already here (or there's no image to wait for), resolve now.
      if (state.turnImageLoaded || state.imagesEnabled === false) resolveGuideImage();
    }

    // The guide image landed (or the fallback fired): mark everything done, flash
    // green, then fade the bar back to the play button.
    function resolveGuideImage() {
      if (!awaitingImage && cur >= IMG_STEP && el.ceremony && el.ceremony.classList.contains("resolved")) return;
      awaitingImage = false;
      completing = false;
      clearTimeout(dwellTimer); dwellTimer = null;
      if (el.ceremonySteps) {
        Array.from(el.ceremonySteps.children).forEach((n) => { n.classList.remove("active", "beat"); n.classList.add("done"); });
      }
      cur = STEPS.length - 1;
      if (el.ceremony) el.ceremony.classList.add("resolved");
      api.note("\u2713 Guide image ready", { tick: false });
      Sound.cereDone();
      active = false;
      clearTimeout(doneTimer);
      clearTimeout(state.finishTimer);
      doneTimer = setTimeout(hideVeil, FADE_AFTER_IMAGE_MS);
    }

    const api = {
      // Show the tracker fresh and enter the first step (action selected).
      begin() {
        build();
        clearTimeout(doneTimer); clearTimeout(dwellTimer); dwellTimer = null;
        clearTimeout(state.finishTimer);
        active = true;
        completing = false;
        awaitingImage = false;
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
        // ANIMATE through the logic steps (World Responding, Actions Generating)
        // in sequence, THEN park on the guide-image step as a live spinner until
        // the still actually renders (enterGuideImageWait / resolveGuideImage) —
        // so the final image-gen wait shows progress instead of a frozen app.
        // Choices are already live, so release input right away.
        target = IMG_STEP - 1; // animate up to "Actions Generating"
        completing = true;
        state.processing = false; // choices are live — let the player act
        state.turnResolved = true;
        // Never spin forever: if the guide image never lands, resolve anyway
        // after a generous window (image gen is legitimately slow).
        clearTimeout(doneTimer);
        clearTimeout(state.finishTimer);
        state.finishTimer = setTimeout(() => resolveGuideImage(), GUIDE_IMAGE_FALLBACK_MS);
        pump();
      },

      // The new frame for this turn is on screen. If the pipeline has already
      // resolved, fade the progress bar back to the play button.
      imageLoaded() {
        state.turnImageLoaded = true;
        // If we're parked on the guide-image step waiting for the still, this is
        // the signal to complete + fade. (If it arrives before we've parked, the
        // flag above lets enterGuideImageWait resolve immediately.)
        if (awaitingImage) resolveGuideImage();
        else this._tryFinish();
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
        completing = false;
        awaitingImage = false;
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
    return api;
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

  // Honor the OS "reduce motion" setting so the receipt/score flourishes fall
  // back to instant reveals instead of animating.
  function prefersReducedMotion() {
    try {
      return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch (_) { return false; }
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
    state.currentStillUrl = imageUrl; // remember for stills-mode SCAN capture
    schedulePrewarm(); // a new scene is on screen — detect its interaction hotspots
    updateAmbientScan(); // keep the ambient hotspot overlay live for this scene
    const silent = !!(opts && opts.silent);
    const instant = !!(opts && opts.instant);
    const incoming = state.activeScene === "A" ? el.sceneB : el.sceneA;
    const outgoing = state.activeScene === "A" ? el.sceneA : el.sceneB;
    // `instant` swaps with NO crossfade — used to keep a silent still "floor"
    // under the realtime video without any visible transition/flash.
    if (instant) {
      const prevT = incoming.style.transition;
      incoming.style.transition = "none";
      incoming.style.backgroundImage = `url('${imageUrl}')`;
      incoming.classList.add("scene-active");
      outgoing.classList.remove("scene-active");
      void incoming.offsetWidth; // flush before restoring the transition
      incoming.style.transition = prevT || "";
      state.activeScene = state.activeScene === "A" ? "B" : "A";
      return;
    }
    incoming.style.backgroundImage = `url('${imageUrl}')`;
    incoming.classList.add("scene-active");
    outgoing.classList.remove("scene-active");
    state.activeScene = state.activeScene === "A" ? "B" : "A";
    // Skip the white scene flash AND the VCR glitch when we're staging a still
    // *behind* the live video (silent): both overlays sit above the video, so
    // firing them here would strobe over the running stream. The re-anchor's own
    // glitch (on the reactor 'reset' command) masks that hand-off instead.
    if (!silent) {
      flashScene();
      glitchTransition();
      markSceneVisible(); // a still is now genuinely on screen
    }
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
          RtLog.push("status", "status \u00B7 " + s);
          if (s === "error" && Renderer.mode === "reactor") {
            // A realtime error is often TRANSIENT (a first-attempt WebRTC/ICE or
            // session hiccup, common on mobile/iOS). Don't permanently drop to
            // stills on the first failure — that left the player stuck on
            // "Realtime unavailable" forever. Retry a couple times before giving
            // up, so realtime self-heals.
            Renderer._rtRetries = (Renderer._rtRetries || 0) + 1;
            if (Renderer._rtRetries <= REALTIME_MAX_RETRIES) {
              console.warn("[standalone] realtime error — retry", Renderer._rtRetries);
              showRendererToast("Realtime reconnecting…");
              try { window.ReactorRenderer.disable(); } catch (_) {}
              clearTimeout(Renderer._rtRetryTimer);
              Renderer._rtRetryTimer = setTimeout(() => {
                if (Renderer.mode !== "reactor" || !Renderer.reactorAvailable()) return;
                window.ReactorRenderer.enable().then((ok) => {
                  if (ok && Renderer.lastScene) window.ReactorRenderer.applyScene(Renderer.lastScene);
                });
              }, 1600 * Renderer._rtRetries);
              updateRendererButton();
              return;
            }
            console.warn("[standalone] realtime unavailable after retries — falling back to stills");
            Renderer.mode = "image"; // reflect reality; keep stored pref intact
            showRendererToast("Realtime unavailable — showing stills");
            clearScanTags(); // re-map hotspots onto the still that replaces the video
            hideGuideThumbnail();
            // Tear down the realtime layers (video + freeze) and paint the last
            // known still so the fallback isn't a blank/black screen.
            try { window.ReactorRenderer.disable(); } catch (_) {}
            if (Renderer.lastScene && Renderer.lastScene.imageUrl) setScene(Renderer.lastScene.imageUrl);
          } else if (s === "live" && Renderer.mode === "reactor") {
            Renderer._rtRetries = 0; // healthy again — reset the retry budget
            showRendererToast("Realtime video — live");
          } else if (s === "ready" || s === "connecting") {
            // Progress means the session is alive; don't hold a stale error count.
            if (s === "ready") Renderer._rtRetries = 0;
          }
          updateRendererButton();
        };
        // Surface the realtime world model's lifecycle on the ceremony sub-line
        // so the player sees the video pipeline working too: prompts submitted,
        // seed accepted, stream live, state/chunks updating.
        window.ReactorRenderer.onEvent = (name, data) => {
          const d = data || {};
          // World-model inspector log (subtle right-side console) — sequential
          // record of what we SEND (prompts) and what the model REPORTS, so the
          // black box is legible (incl. stalls / errors / "just drawing black").
          switch (name) {
            case "command_sent":
              if (d.command === "set_prompt" || d.command === "schedule_prompt" ||
                  d.command === "set_shot" || d.command === "scene_cut")
                RtLog.push("prompt", "\u2192 " + d.command, RtLog.clip(d.prompt, 160));
              else if (d.command === "set_image") RtLog.push("prompt", "\u2192 set_image", d.hasImage ? "[seed image]" : "");
              else RtLog.push(null, "\u2192 " + d.command);
              break;
            case "capabilities": RtLog.push("status", "\u25C6 model commands", RtLog.clip((d.commands || []).join(", "), 140)); break;
            case "tracks": {
              RtLog.push("status", "\u25C6 video out: " + ((d.outputs && d.outputs.length ? d.outputs.join(", ") : "?")) + " \u2192 " + (d.chosen || "main_video"));
              if (d.inputs && d.inputs.length) RtLog.push("error", "\u26A0 model needs input track: " + d.inputs.join(", ") + " (not fed \u2014 may not render)");
              break;
            }
            case "track_received": RtLog.push("dim", "\u25C9 track \u00B7 " + (d.name || "?") + (d.kind ? " (" + d.kind + ")" : "")); break;
            case "command_skipped": RtLog.push("dim", "\u2298 skip \u00B7 " + (d.command || "") + " (unsupported)"); break;
            case "prompt_accepted": RtLog.push("ok", "\u2713 prompt accepted"); break;
            case "image_accepted": RtLog.push("ok", "\u2713 image accepted (seed decoded)"); break;
            case "generation_started":
            case "stage_started": RtLog.push("ok", "\u25C8 generation started"); break;
            case "video_showing": RtLog.push("ok", "\u25C9 video live \u2014 frames on screen"); break;
            case "video_stalled": RtLog.push("error", "\u26A0 stalled \u2014 no video after " + (d.afterMs || "?") + "ms"); break;
            case "chunk_complete": RtLog.push("dim", "chunk " + ((d.chunk_index != null ? d.chunk_index : 0) + 1), "", { throttleMs: 1200 }); break;
            case "state": {
              const act = d.current_action && d.current_action !== "still" ? d.current_action : null;
              RtLog.push("dim", act ? "state \u00B7 " + act : "state", "", { throttleMs: 1200 });
              break;
            }
            case "generation_reset": RtLog.push("status", "\u21BA world reset (re-staging)"); break;
            case "command_error": RtLog.push("error", "\u26A0 error \u00B7 " + (d.command || ""), RtLog.clip(d.reason, 140)); break;
            default: break;
          }
          // VCR static over the ONE visible realtime hand-off: the freeze→video
          // reveal (video_showing). We deliberately do NOT burst on the 'reset'
          // command — at teardown the freeze buffer is already covering an
          // unchanging frame, so a burst there masks nothing and fires seconds
          // before the actual switch, leaving a naked hold then an abrupt jump.
          // Timing the static to the reveal makes it mask the real transition.
          if (Renderer.mode === "reactor") {
            if (name === "video_showing") {
              glitchTransition();
              markSceneVisible(); // the realtime feed is now live on screen
              schedulePrewarm(); // live scene on screen — detect its hotspots
              updateAmbientScan(); // surface ambient hotspots over the live video
            }
            // Realtime auto-play advances off the LIVE video, not the scene_image
            // feed item: once the new scene is actually on screen, let it play for
            // the watch window, then advance. This is what stops auto-play from
            // stacking re-anchors and blacking out the stream.
            if (name === "video_showing" && state.autoPlay) {
              scheduleAutoAdvance(AUTOPLAY_REALTIME_WATCH_MS);
            }
          }
          if (Renderer.mode !== "reactor" || !Ceremony.isActive()) return;
          switch (name) {
            case "command_sent": {
              const cmdNote = {
                set_prompt: "\u25B8 Prompt submitted",
                schedule_prompt: "\u25B8 Prompt submitted",
                set_shot: "\u25B8 Shot submitted",
                scene_cut: "\u25B8 Cutting to new scene",
                set_image: "\u25B8 Seed image sent",
                start: "\u25B8 Starting stream",
                reset: "\u25B8 Re-staging world",
                pause: "\u25B8 Pausing stream",
                resume: "\u25B8 Resuming stream",
              }[d.command];
              if (cmdNote) {
                Ceremony.note(cmdNote);
                if (d.command === "set_prompt" || d.command === "schedule_prompt" ||
                    d.command === "set_shot" || d.command === "scene_cut" || d.command === "set_image")
                  Ceremony.reach("world_update");
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
          RtLog.push("img", "\u25C8 guide image integrated");
          showRendererToast("Guide image integrated");
          if (Ceremony.isActive()) Ceremony.note("\u25C8 Guide image integrated");
          try { Sound.scene(); } catch (_) {}
          // Re-assert the ambient score on re-anchor (deduped, so it only acts
          // if the scene descriptor actually changed since the last request).
          try { SceneAudio.score(state.lastScenePrompt); } catch (_) {}
          showGuideThumbnail(imageUrl);
        };
        // The active world model changed (mid-game swap) — reflect it in the
        // switcher, the log, and a transient toast.
        window.ReactorRenderer.onModel = (id, label) => {
          RtLog.push("status", "\u21C4 world model \u00B7 " + (label || id));
          showRendererToast("World model: " + (label || id));
          if (Ceremony.isActive()) Ceremony.note("\u21C4 World model \u2192 " + (label || id));
          updateModelSwitcher();
        };
        // The available-model list changed (e.g. a custom model was added) —
        // rebuild the switcher so the new entry appears.
        window.ReactorRenderer.onModelsChanged = () => { buildModelSwitcher(); };
        // The log + switcher stay reachable whenever realtime is available so you
        // can flip world models even from still mode.
        document.body.classList.add("reactor-available");
      }
      RtLog.init();
      buildModelSwitcher();
      // In realtime mode, connect eagerly so the GPU session is warming while
      // the intro scene generates — the video then starts as soon as the first
      // scene prompt arrives. (Falls back to stills if it can't connect.)
      if (this.mode === "reactor" && this.reactorAvailable()) {
        window.ReactorRenderer.enable().then((ok) => {
          buildModelSwitcher(); // config may refine the model list/labels
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
        // Realtime mode: the reactor renderer OWNS the screen (live video + a
        // freeze back-buffer). But we ALSO paint the Gemini still as a SILENT,
        // instant floor on the scene layer beneath the video/freeze. That floor
        // is invisible during healthy playback (the opaque video covers it) but
        // becomes the safety net whenever the live video can't present frames —
        // warming up, stalled, or autoplay-blocked (e.g. iOS Low Power Mode) —
        // so realtime is never "just black". Instant + silent = no crossfade,
        // so it never flashes between guide images (the reason it was omitted
        // before; the freeze buffer covers re-anchors, so the floor stays hidden
        // during them).
        if (scene.imageUrl) setScene(scene.imageUrl, { silent: true, instant: true });
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
          buildModelSwitcher(); // config may refine the model list/labels
          // Steer the current scene immediately so switching mid-game shows
          // something without waiting for the next turn.
          if (ok && Renderer.lastScene) window.ReactorRenderer.applyScene(Renderer.lastScene);
        });
      } else if (this.reactorAvailable()) {
        showRendererToast("Still images");
        try { window.ReactorRenderer.disable(); } catch (_) {}
        hideGuideThumbnail();
        hideCaptureThumbnail();
      }
      // Ambient hotspots work in BOTH renderers — drop the tags so they re-map
      // to the new source (video vs still cover the viewport differently), then
      // re-detect for the switched-in source.
      state.scanSrcSize = null;
      clearScanTags();
      updateAmbientScan();
      updateRendererButton();
    },

    toggle() {
      this.setMode(this.mode === "reactor" ? "image" : "reactor");
    },

    // Switch to a specific world model live, mid-game (from the switcher UI).
    // Ensures we're in realtime mode, then swaps the model on the running
    // session (or picks it up on connect if realtime was just enabled).
    setWorldModel(id) {
      if (!this.reactorAvailable()) { showRendererToast("Realtime unavailable"); return; }
      if (this.mode !== "reactor") {
        // Enabling realtime connects with the chosen model directly (no swap).
        try { window.ReactorRenderer.setModel(id); } catch (_) {}
        this.setMode("reactor");
      } else {
        window.ReactorRenderer.setModel(id);
      }
      updateModelSwitcher();
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

    // SHAPE tool: submit a REALTIME prompt that steers the CURRENT live video
    // INSTANTLY — a prompt hot-swap on the running stream (no new guide image,
    // no backend turn), so the world reacts now for fast feedback. This is
    // deliberately separate from ACT/choices, which resolve a full turn and
    // change the scene. Returns true if it steered, false if realtime isn't
    // ready (no live scene to build on yet).
    steerRealtime(text, where) {
      if (this.mode !== "reactor" || !this.reactorAvailable()) return false;
      const a = (text || "").trim().replace(/\.+$/, "");
      if (!a) return false;
      // Build on the stable scene bible (style + physical scene, no action beat)
      // so the nudge blends with the current shot instead of resetting it.
      const base = this.lastBase || (this.lastScene && this.lastScene.prompt) || "";
      if (!base) return false;
      const act = a.charAt(0).toLowerCase() + a.slice(1);
      // Anchor the nudge to the spot the player touched, when one was given, so
      // the change lands where they aimed instead of across the whole frame.
      const beat = (where && where.phrase)
        ? "Motion: " + where.phrase + ", " + act + "."
        : "Motion: the view shifts as you " + act + ".";
      window.ReactorRenderer.applyScene({
        prompt: base + " " + beat,
        imageUrl: null,           // same scene — just re-steer, no image swap
        hardTransition: false,
      });
      if (Ceremony.isActive()) Ceremony.note("\u25B8 Live nudge injected");
      return true;
    },
  };

  function updateRendererButton() {
    // Reveal the realtime SHAPE tool only when the realtime renderer is active.
    document.body.classList.toggle(
      "realtime-on",
      Renderer.mode === "reactor" && Renderer.reactorAvailable()
    );
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
    updateModelSwitcher();
  }

  // ------------------------------------------------------------------
  // World-model switcher — a simple segmented control in the log head to flip
  // between the still renderer and each Reactor world model live, mid-game.
  // ------------------------------------------------------------------
  function makeModelBtn(id, label, custom) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "rt-model-btn" + (custom ? " custom" : "");
    b.dataset.model = id;
    const dot = document.createElement("span");
    dot.className = "rt-model-dot";
    dot.textContent = id === "__image__" ? "\u25CE" : "\u25C9"; // ◎ still / ◉ live
    const txt = document.createElement("span");
    txt.textContent = label;
    b.appendChild(dot);
    b.appendChild(txt);
    b.title = id === "__image__"
      ? "Still images (Gemini)"
      : custom
        ? "Experimental world model: " + label
        : "Realtime world model: " + label;
    b.addEventListener("click", () => {
      if (id === "__image__") Renderer.setMode("image");
      else Renderer.setWorldModel(id);
      updateModelSwitcher();
    });
    return b;
  }

  function buildModelSwitcher() {
    const wrap = el.rtLogModels;
    if (!wrap) return;
    wrap.innerHTML = "";
    wrap.appendChild(makeModelBtn("__image__", "Stills"));
    const models = (Renderer.reactorAvailable() && window.ReactorRenderer.getModels)
      ? window.ReactorRenderer.getModels()
      : [];
    models.forEach((m) => wrap.appendChild(makeModelBtn(m.id, m.label, m.custom)));
    // Reveal the "try any model" field only when the server allows connecting to
    // unadvertised models — so we can use a brand-new Reactor model instantly.
    if (el.rtModelAdd) {
      const allowCustom = Renderer.reactorAvailable() &&
        window.ReactorRenderer.allowsCustom && window.ReactorRenderer.allowsCustom();
      el.rtModelAdd.classList.toggle("hidden", !allowCustom);
    }
    updateModelSwitcher();
  }

  // Submit handler for the custom-model field: register + switch to any model id
  // the tester types in (e.g. a model Reactor just shipped), live.
  function addCustomModel(e) {
    if (e) e.preventDefault();
    if (!el.rtModelInput) return;
    const raw = (el.rtModelInput.value || "").trim();
    if (!raw) return;
    if (!Renderer.reactorAvailable() || !window.ReactorRenderer.addModel) {
      showRendererToast("Realtime unavailable");
      return;
    }
    const id = window.ReactorRenderer.addModel(raw);
    if (!id) return;
    el.rtModelInput.value = "";
    buildModelSwitcher();
    Renderer.setWorldModel(id);
  }

  function updateModelSwitcher() {
    const wrap = el.rtLogModels;
    if (!wrap) return;
    const inReactor = Renderer.mode === "reactor" && Renderer.reactorAvailable();
    const activeModel = (inReactor && window.ReactorRenderer.getModel)
      ? window.ReactorRenderer.getModel()
      : null;
    const status = (inReactor && window.ReactorRenderer.getStatus)
      ? window.ReactorRenderer.getStatus()
      : "off";
    Array.prototype.forEach.call(wrap.children, (b) => {
      const id = b.dataset.model;
      const isActive = id === "__image__" ? !inReactor : (inReactor && id === activeModel);
      b.classList.toggle("active", isActive);
      b.classList.toggle("pending", isActive && id !== "__image__" && status === "connecting");
    });
  }

  // ------------------------------------------------------------------
  // World-model inspector log — a subtle, sequential console of what we send to
  // the realtime world model (prompts) and what it reports back (accepted /
  // started / chunks / stalls / errors), so the black box is legible.
  // ------------------------------------------------------------------
  const RtLog = (function () {
    const MAX = 220;
    const throttleAt = {};           // per-kind throttle timestamps
    // Collapsed by default: the world-model log/selector opens from the MODEL
    // button (or L), rather than cluttering the screen on load.
    function visible() {
      try { return localStorage.getItem("rt_log") === "on"; } catch (_) { return false; }
    }
    function applyVisibility() {
      document.body.classList.toggle("rt-log-on", visible());
    }
    function stamp() {
      const d = new Date();
      const mm = String(d.getMinutes()).padStart(2, "0");
      const ss = String(d.getSeconds()).padStart(2, "0");
      return `${mm}:${ss}`;
    }
    // kind: prompt | img | ok | error | status | dim | (default)
    function push(kind, label, detail, opts) {
      if (!el.rtLogList) return;
      if (opts && opts.throttleMs) {
        const now = Date.now();
        const key = kind + "|" + label.slice(0, 8);
        if (now - (throttleAt[key] || 0) < opts.throttleMs) {
          // Update the most recent matching line in place instead of appending.
          const last = el.rtLogList.lastElementChild;
          if (last && last.dataset.key === key) {
            const m = last.querySelector(".rt-m"); if (m) m.textContent = label;
            const dd = last.querySelector(".rt-d"); if (dd) dd.textContent = detail ? " " + detail : "";
            return;
          }
        }
        throttleAt[key] = now;
        var _key = key;
      }
      const li = document.createElement("li");
      li.className = "rt-e" + (kind ? " rt-" + kind : "");
      if (typeof _key === "string") li.dataset.key = _key;
      const t = document.createElement("span"); t.className = "rt-t"; t.textContent = stamp();
      const m = document.createElement("span"); m.className = "rt-m"; m.textContent = label;
      li.appendChild(t); li.appendChild(m);
      if (detail) {
        const dd = document.createElement("span"); dd.className = "rt-d"; dd.textContent = " " + detail;
        li.appendChild(dd);
      }
      const atBottom = el.rtLogList.scrollTop + el.rtLogList.clientHeight >= el.rtLogList.scrollHeight - 24;
      el.rtLogList.appendChild(li);
      while (el.rtLogList.children.length > MAX) el.rtLogList.removeChild(el.rtLogList.firstChild);
      if (atBottom) el.rtLogList.scrollTop = el.rtLogList.scrollHeight;
    }
    function clip(s, n) {
      s = (s || "").toString().replace(/\s+/g, " ").trim();
      return s.length > (n || 120) ? s.slice(0, (n || 120) - 1) + "\u2026" : s;
    }
    function toggle() {
      const on = !visible();
      try { localStorage.setItem("rt_log", on ? "on" : "off"); } catch (_) {}
      applyVisibility();
    }
    function init() {
      applyVisibility();
      if (el.rtLogHide) el.rtLogHide.addEventListener("click", () => { try { localStorage.setItem("rt_log", "off"); } catch (_) {} applyVisibility(); });
    }
    return { push, clip, toggle, init };
  })();

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
  // Menu — the collapsible control rail (top-right). Starts COLLAPSED every
  // load so the scene is unobstructed; the corner toggle opens/closes it. The
  // keyboard shortcuts (T/V/M/…) still work while collapsed, so power users
  // aren't slowed down — the menu is just the visual surface.
  // ------------------------------------------------------------------
  const Menu = (function () {
    let open = false;
    function apply() {
      document.body.classList.toggle("menu-open", open);
      if (el.menuToggle) {
        el.menuToggle.setAttribute("aria-expanded", open ? "true" : "false");
        el.menuToggle.title = open ? "Close menu" : "Menu";
      }
    }
    function set(next) {
      if (next === open) return;
      open = next;
      apply();
      try { Sound[open ? "menuOpen" : "menuClose"](); } catch (_) {}
      try { Haptics.tap(); } catch (_) {}
    }
    return {
      isOpen: () => open,
      toggle: () => set(!open),
      open: () => set(true),
      close: () => set(false),
      init: apply,
    };
  })();

  // ------------------------------------------------------------------
  // Tactile — makes EVERY interactive surface feel physical: a faint detent as
  // the pointer crosses a control, a crisp mechanical click + haptic buzz on
  // press, and a tick as keyboard focus lands. Implemented once via event
  // delegation so it automatically covers current AND future controls (buttons,
  // choices, model switcher, tape controls, scan tags, evidence thumbnails…),
  // layered UNDER the existing semantic cues (select/submit/scene…) for depth.
  // ------------------------------------------------------------------
  const Tactile = (function () {
    // Everything the player can click/tap. Buttons/links cover most; the couple
    // of non-button clickables (scan tags, evidence thumbnails) are named too.
    const SELECTOR = "button, a[href], [role='button'], .scan-tag, .inv-thumb";
    let lastHover = null;
    let keyboardModality = false;

    function control(node) {
      if (!node || typeof node.closest !== "function") return null;
      const c = node.closest(SELECTOR);
      if (!c || c.disabled || c.getAttribute("aria-disabled") === "true") return null;
      return c;
    }

    function onOver(e) {
      // Hover has no meaning on touch (pointerover fires on tap → would double
      // with the press cue), so only detent for a real mouse.
      if (e.pointerType && e.pointerType !== "mouse") return;
      const c = control(e.target);
      if (!c || c === lastHover) return;
      lastHover = c;
      try { Sound.hover(); } catch (_) {}
    }
    function onOut(e) {
      const c = control(e.target);
      if (c && c === lastHover) lastHover = null;
    }
    function onDown(e) {
      keyboardModality = false;
      const c = control(e.target);
      if (!c) return;
      try { Sound.press(); } catch (_) {}
      try { Haptics.tap(); } catch (_) {}
    }
    function onKeyModality(e) {
      if (e.key === "Tab" || e.key === "ArrowUp" || e.key === "ArrowDown" ||
          e.key === "ArrowLeft" || e.key === "ArrowRight") {
        keyboardModality = true;
      }
    }
    function onFocusIn(e) {
      if (!keyboardModality) return; // pointer focus already got hover+press
      if (!control(e.target)) return;
      try { Sound.focusTick(); } catch (_) {}
    }

    function init() {
      // Capture phase so we still fire even if a handler stops propagation.
      document.addEventListener("pointerover", onOver, true);
      document.addEventListener("pointerout", onOut, true);
      document.addEventListener("pointerdown", onDown, true);
      document.addEventListener("keydown", onKeyModality, true);
      document.addEventListener("focusin", onFocusIn, true);
    }
    return { init };
  })();

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
  // Captured-texture preview — shows the EXACT frame grabbed from the live
  // world-model video at act-time and handed to the backend as the primary
  // img2img reference (act_frame → _ingest_realtime_frame). This is the
  // "captured texture" verification view: if it shows the current melty live
  // frame (not the crisp Gemini still), you can confirm the realtime state is
  // what's being passed to img2img.
  // ------------------------------------------------------------------
  function captureThumbHidden() {
    if (!CAPTURE_THUMBNAIL_ENABLED) return true;
    try { return localStorage.getItem("capture_thumbnail") === "off"; } catch (_) { return false; }
  }

  function ensureCaptureThumb() {
    let wrap = document.getElementById("capture-thumb");
    if (wrap) return wrap;
    wrap = document.createElement("div");
    wrap.id = "capture-thumb";
    wrap.className = "guide-thumb capture-thumb";
    const img = document.createElement("img");
    img.className = "guide-thumb-img";
    img.alt = "Realtime frame captured and passed to img2img";
    const label = document.createElement("div");
    label.className = "guide-thumb-label";
    label.textContent = "CAPTURED \u2192 IMG2IMG";
    const hide = document.createElement("button");
    hide.type = "button";
    hide.className = "guide-thumb-hide";
    hide.title = "Hide captured-frame preview";
    hide.textContent = "\u2715";
    hide.addEventListener("click", () => {
      try { localStorage.setItem("capture_thumbnail", "off"); } catch (_) {}
      hideCaptureThumbnail();
    });
    wrap.appendChild(img);
    wrap.appendChild(label);
    wrap.appendChild(hide);
    document.body.appendChild(wrap);
    return wrap;
  }

  // dataUrl is the exact JPEG data URL sent to /api/choose as act_frame.
  function showCaptureThumbnail(dataUrl) {
    if (captureThumbHidden() || !dataUrl) return;
    const wrap = ensureCaptureThumb();
    const img = wrap.querySelector(".guide-thumb-img");
    if (img) img.setAttribute("src", dataUrl);
    wrap.classList.add("show");
    wrap.classList.remove("pulse");
    void wrap.offsetWidth; // reflow to restart the animation
    wrap.classList.add("pulse");
  }

  function hideCaptureThumbnail() {
    const wrap = document.getElementById("capture-thumb");
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
        try { Haptics.select(); } catch (_) {}
        btn.classList.add("picked");
        makeChoice(choice.text, promptItem.id);
      });
      el.choices.appendChild(btn);
    });
  }

  function enterGameOver(message) {
    state.gameOver = true;
    state.awaitingResolution = false;
    clearTurnWatchdog();
    closeScan(); // no scanning over the death screen
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
      // Re-score the ambient bed from this scene's descriptor. Works for both
      // the still (image) and realtime (reactor) renderers since both flow the
      // guide image + prompt through here.
      const scenePrompt = (item.metadata && item.metadata.prompt) || item.content || "";
      state.lastScenePrompt = scenePrompt;
      try { SceneAudio.score(scenePrompt); } catch (_) {}
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
        // Auto-play (IMAGE mode only): the still just rendered — advance soon.
        // In REALTIME the scene_image feed item is NOT the on-screen frame (the
        // video re-anchor is still establishing), so realtime auto-advance is
        // driven by the reactor 'video_showing' event instead — see Renderer.init.
        if (state.autoPlay && Renderer.mode !== "reactor") scheduleAutoAdvance(AUTOPLAY_FRAME_DELAY_MS);
        return;

      case "game_over":
        appendProse(item);
        Sound.death();
        try { Haptics.strong(); } catch (_) {}
        Ceremony.abort();
        setAutoPlay(false); // stop the world advancing once you're dead
        enterGameOver(item.content);
        return;

      case "player_choice_prompt":
        // The engine pairs a death with a "GAME OVER" restart prompt; when
        // we're in the death state we let the overlay own restart instead.
        clearTurnWatchdog();
        state.lastTurnTs = Date.now(); // post-turn cooldown for pre-warm counts from here
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
        // Realtime: cap how long auto-play waits for this turn's video to show
        // before advancing anyway (so a stalled stream can't freeze the loop).
        state.autoDeadline = Date.now() + AUTOPLAY_REALTIME_MAX_WAIT_MS;
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
        clearTurnWatchdog();
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
      clearTurnWatchdog(); // don't let a stale turn timer fire into the new run
      exitGameOver();
      closeScan(); // drop any scan tags/overlay from the dead run
      closeTouch(); // drop any camera overlay
      try { Photo.hide(); Photo.clearTimers(); } catch (_) {} // kill any in-flight receipt
      try { hideCaseWin(); } catch (_) {}    // drop the win screen from the prior run
      state.caseWon = false;
      try { Evidence.reset(); } catch (_) {} // the EVIDENCE score + case file are per-run
      state.selectedInvestigation = null;
      try { Investigations.clear(); } catch (_) {} // the case file is per-run
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
      state.lastScenePrompt = null;
      try { SceneAudio.reset(); } catch (_) {} // silence the prior run's bed
      Sound.start(); // new tape / game begins
      try { Haptics.strong(); } catch (_) {}
      Ceremony.abort(); // cancel any mid-turn pipeline from the prior run
      // Restart runs the SAME gamified generation pipeline as a normal turn: the
      // progress bar takes over the play button's spot at the bottom and parks on
      // "Guide Image Rendering" until the fresh run's first scene is on screen —
      // instead of a bare spinner over a black screen.
      Ceremony.begin();
      el.prose.innerHTML = "";
      el.choices.innerHTML = "";
      state.lastId = 0;
      state.renderedIds = new Set();
      // Let the fresh run's feed items (consequence → world updating → scene) drive
      // and resolve the ceremony, exactly like a normal generation.
      state.awaitingResolution = true;
      state.gameOver = false;
      state.currentPromptId = null;
      state.lastAdvancedPromptId = null;
      markSceneAwaiting(); // hide prose + SNAP until this run's first frame lands
      clearTimeout(state.autoTimer);
      closeFreeWill(true);
      renderInventory([]);
      startTimecode();
      const items = await postJSON("/api/reset", {});
      // Do NOT hide the veil here: the ceremony now owns the progress bar and
      // fades itself once the first scene lands (player_choice_prompt →
      // Ceremony.complete, then the guide-image step resolves on scene_image).
      // The guide-image fallback timer guarantees it can never spin forever.
      renderItems(items);
      refreshStatus();
    } catch (err) {
      console.error("[standalone] resetGame failed:", err);
      hideVeil();
      appendProse({ id: -1, type: "error_event", content: `Could not reach the server: ${err.message}` });
    } finally {
      startPolling(); // resume normal polling once the fresh feed is in
    }
  }

  // Turn watchdog: a committed turn must produce a player_choice_prompt within a
  // bounded time. If the server turn stalls (LLM error / rate-limit / lost feed
  // item) the ceremony would otherwise sit on the progress bar forever with no
  // way to act. This guarantees the player is never permanently stuck: we do one
  // forced feed catch-up, then — if still unresolved — release the UI with
  // recovery choices so the game can continue.
  const TURN_WATCHDOG_MS = (typeof window !== "undefined" && window.__TURN_WATCHDOG_MS__) || 26000;
  function clearTurnWatchdog() {
    if (state.turnWatchdog) { clearTimeout(state.turnWatchdog); state.turnWatchdog = null; }
  }
  function armTurnWatchdog() {
    clearTurnWatchdog();
    state.turnWatchdog = setTimeout(async () => {
      state.turnWatchdog = null;
      if (!state.awaitingResolution) return; // already resolved
      try { await pollOnce(); } catch (_) {} // maybe a poll was just missed
      if (!state.awaitingResolution) return; // catch-up delivered the prompt
      console.error("[standalone] turn watchdog fired — no resolution; recovering UI");
      Ceremony.abort();
      hideVeil();
      state.awaitingResolution = false;
      state.lastTurnTs = Date.now();
      appendProse({ id: -1, type: "error_event", content: "The world hesitated. Choose again." });
      renderChoices({
        id: state.currentPromptId || -1,
        choices: [
          { text: "Look around." },
          { text: "Move forward." },
          { text: "Wait and listen." },
        ],
      });
    }, TURN_WATCHDOG_MS);
  }

  async function makeChoice(choiceText, contextItemId, opts) {
    if (state.processing || state.gameOver) return;
    // `opts.source` marks HOW the action was issued (e.g. a SCAN object
    // interaction) so the backend can drive the story-escalation systems harder
    // for deliberate meddling — see _process_turn_background (engine.py).
    const actionSource = (opts && opts.source) || null;
    closeFreeWill(true); // picking any action closes the free-will gate
    clearScanTags();      // the scene is about to change — drop stale scan tags
    el.choices.innerHTML = "";
    Ceremony.begin(); // light up the turn pipeline — starting with "action selected"
    state.awaitingResolution = true;
    state.lastTurnTs = Date.now(); // pre-warm defers around the turn
    armTurnWatchdog(choiceText, contextItemId);
    // NOTE: we deliberately do NOT steer the CURRENT live video with the action
    // here. Injecting the action into the video the instant a choice is made
    // meant the ORIGINAL scene's video started playing the action before its new
    // guide image had formed — which looked wrong. The action is now applied
    // only when the new guide image has been generated and its video is running:
    // the re-anchor (see Renderer.applyScene / ReactorRenderer) starts the new
    // guide-image stream with the render prompt, which already carries this
    // action beat (build_realtime_prompt, server-side).
    //
    // ACT-TIME FRAME CAPTURE: grab the frame the player is ACTUALLY looking at
    // in the live world model at the instant they commit an action, and hand it
    // to the turn. The server uses it as the PRIMARY img2img reference (so the
    // next guide image evolves from the realtime state on screen, not from a
    // stale still) while still keeping the original high-fidelity guide still as
    // a secondary quality anchor. Captured only when the realtime video is truly
    // showing; null (still mode / not yet live) simply skips this and behaves as
    // before.
    let actFrame = null;
    try {
      if (
        Renderer.mode === "reactor" &&
        Renderer.reactorAvailable() &&
        window.ReactorRenderer.isShowing &&
        window.ReactorRenderer.isShowing() &&
        window.ReactorRenderer.captureFrame
      ) {
        actFrame = window.ReactorRenderer.captureFrame();
      }
    } catch (_) {
      actFrame = null;
    }
    // Verification: surface the exact texture we're about to hand img2img (or
    // note when nothing was captured, so a silent miss is obvious).
    if (actFrame) {
      showCaptureThumbnail(actFrame);
      RtLog.push("img", "\u25C8 captured live frame \u2192 img2img");
    } else if (Renderer.mode === "reactor") {
      RtLog.push("img", "\u26A0 no live frame captured (still mode / not yet showing)");
    }
    // If a captured specimen is armed, ride its id along with the action so the
    // backend can (in future) ground the turn on what the player examined. The
    // engine ignores unknown fields today — this is forward infrastructure.
    const investigationId = state.selectedInvestigation ? state.selectedInvestigation.id : null;
    state.selectedInvestigation = null;
    try { Investigations.render(); } catch (_) {} // drop the selection highlight
    try {
      const items = await postJSON("/api/choose", {
        choice: choiceText,
        context_item_id: contextItemId,
        act_frame: actFrame,
        investigation_id: investigationId,
        source: actionSource,
      });
      renderItems(items); // immediately shows the player_action echo
      beginFastPolling();
    } catch (err) {
      console.error("[standalone] makeChoice failed:", err);
      clearTurnWatchdog();
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
    state.inputMode = "act";
    el.actionWheel.classList.add("fw-open");
    if (el.customInput) el.customInput.setAttribute("placeholder", "type your own action...");
    Sound.open();
    // Focus after the expand animation starts so the caret lands cleanly.
    setTimeout(() => el.customInput.focus(), 60);
  }

  // ------------------------------------------------------------------
  // Scene-region capture — the shared plumbing behind "investigation" textures.
  // Crops a piece of the CURRENT scene (live video frame OR the still) into a
  // small square JPEG the player collects as a specimen. This is the raw
  // material for scene-driven prompt mechanics: interacting with the world by
  // referencing what you looked at closely.
  // ------------------------------------------------------------------

  // Intrinsic size of whatever is currently on screen (video or still), for
  // mapping between screen space and the source frame.
  function currentSourceSize() {
    if (scanInRealtime()) {
      return (window.ReactorRenderer.getVideoSize && window.ReactorRenderer.getVideoSize()) || null;
    }
    const img = getStillImage();
    return img ? { w: img.naturalWidth, h: img.naturalHeight } : null;
  }

  // Inverse of mapNormToScreen: screen px -> normalized (0..1) source coords,
  // accounting for the object-fit/background-size: cover crop AND any optical
  // zoom transform on the scene (so the capture crop matches the framed view).
  function screenToNorm(x, y) {
    const W = window.innerWidth, H = window.innerHeight;
    // Undo the scene's zoom transform first: a screen point maps back to the
    // pre-transform layer point p = O + (screen - O) / scale, where O is the
    // transform origin (viewport center).
    const t = getSceneTransform();
    if (t && t.scale !== 1) {
      x = t.ox + (x - t.ox) / t.scale;
      y = t.oy + (y - t.oy) / t.scale;
    }
    const size = currentSourceSize();
    if (!size || !size.w || !size.h) return { x: x / W, y: y / H };
    const scale = Math.max(W / size.w, H / size.h);
    const dw = size.w * scale, dh = size.h * scale;
    const ox = (W - dw) / 2, oy = (H - dh) / 2;
    return { x: (x - ox) / dw, y: (y - oy) / dh };
  }

  // A screen-space square (center + size in px) as a normalized source box.
  function screenBoxToNorm(cx, cy, sizePx) {
    const a = screenToNorm(cx - sizePx / 2, cy - sizePx / 2);
    const b = screenToNorm(cx + sizePx / 2, cy + sizePx / 2);
    const x = Math.max(0, Math.min(a.x, b.x));
    const y = Math.max(0, Math.min(a.y, b.y));
    const w = Math.min(1 - x, Math.abs(b.x - a.x));
    const h = Math.min(1 - y, Math.abs(b.y - a.y));
    return { x, y, w: Math.max(0.02, w), h: Math.max(0.02, h) };
  }

  // Default on-screen size of the capture box (px), tuned to the viewport.
  function investBoxPx() {
    // 2x the original framing — a bigger, more forgiving capture region (this
    // also scales the on-screen frame and the "in-frame" worthiness tolerance).
    return Math.round(Math.min(640, Math.max(280, Math.min(window.innerWidth, window.innerHeight) * 0.52)));
  }

  // Crop a normalized region of the current scene to a square JPEG data URL.
  // Uses the live video in realtime mode, or the current still in image mode.
  function captureSceneRegion(normBox, outSize) {
    const out = outSize || 256;
    if (scanInRealtime()) {
      return window.ReactorRenderer.captureRegion
        ? window.ReactorRenderer.captureRegion(normBox, out) : null;
    }
    const img = getStillImage();
    if (!img) return null;
    try {
      const vw = img.naturalWidth, vh = img.naturalHeight;
      let sx = Math.max(0, Math.min(1, normBox.x)) * vw;
      let sy = Math.max(0, Math.min(1, normBox.y)) * vh;
      let sw = Math.max(1, Math.min(1, normBox.w) * vw);
      let sh = Math.max(1, Math.min(1, normBox.h) * vh);
      if (sx + sw > vw) sw = vw - sx;
      if (sy + sh > vh) sh = vh - sy;
      const c = document.createElement("canvas");
      c.width = out; c.height = out;
      c.getContext("2d").drawImage(img, sx, sy, sw, sh, 0, 0, out, out);
      return c.toDataURL("image/jpeg", 0.82);
    } catch (e) {
      console.warn("[standalone] region capture failed:", e);
      return null;
    }
  }

  // ------------------------------------------------------------------
  // Investigations — the "case file" of captured specimen textures.
  // In-memory + localStorage, mirrored to the server (/api/investigate). This
  // is deliberately front-loaded infrastructure: the textures are collected now
  // so future mechanics can build prompts from them (examine an object, compose
  // a photo-journalist dispatch, seed img2img from a close-up, etc.).
  // Exposed as window.Investigations for iteration.
  // ------------------------------------------------------------------
  const Investigations = (function () {
    const KEY = "investigations_v1";
    const MAX = 60;
    let items = [];
    try {
      const raw = localStorage.getItem(KEY);
      if (raw) items = JSON.parse(raw) || [];
    } catch (_) { items = []; }

    function persist() {
      try { localStorage.setItem(KEY, JSON.stringify(items.slice(-MAX))); } catch (_) {}
    }

    function render() {
      if (!el.investigationsStrip || !el.investigationsTray) return;
      el.investigationsTray.classList.toggle("hidden", items.length === 0);
      const recent = items.slice(-12).reverse();
      el.investigationsStrip.innerHTML = "";
      recent.forEach((it) => {
        const t = document.createElement("div");
        t.className = "inv-thumb kind-" + (it.kind || "touch");
        if (state.selectedInvestigation && state.selectedInvestigation.id === it.id) t.classList.add("selected");
        const src = it.texture || it.imageUrl;
        if (src) t.style.backgroundImage = `url('${src}')`;
        t.title = (it.note || it.label || (it.kind === "photo" ? "photograph" : "specimen")) + " — click to use in an action";
        const k = document.createElement("span");
        k.className = "inv-kind";
        k.textContent = it.kind === "photo" ? "\uD83D\uDCF7" : "\u270B"; // 📷 / ✋
        t.appendChild(k);
        t.addEventListener("click", () => use(it));
        el.investigationsStrip.appendChild(t);
      });
    }

    // Store an already-captured specimen (texture + region), mirror to server.
    function store(spec) {
      if (!spec || !spec.texture) return null;
      const entry = {
        id: Date.now(),
        kind: spec.kind || "touch",
        note: spec.note || "",
        label: spec.label || "",
        region: spec.region || null,
        texture: spec.texture,
        ts: Date.now(),
        imageUrl: null,
      };
      items.push(entry);
      if (items.length > MAX) items = items.slice(-MAX);
      persist();
      render();
      postJSON("/api/investigate", {
        texture: entry.texture, region: entry.region,
        kind: entry.kind, note: entry.note, label: entry.label,
      }).then((res) => {
        if (res && res.image_url) { entry.imageUrl = res.image_url; entry.id = res.id || entry.id; persist(); }
      }).catch((err) => console.warn("[standalone] investigate save failed:", err));
      return entry;
    }

    // Capture a fresh specimen from the scene around a screen point, then store.
    function capture(opts) {
      opts = opts || {};
      const boxPx = opts.boxPx || investBoxPx();
      const sp = opts.screen || { x: window.innerWidth / 2, y: window.innerHeight / 2 };
      const region = screenBoxToNorm(sp.x, sp.y, boxPx);
      const texture = captureSceneRegion(region, opts.outSize || 256);
      if (!texture) return null;
      return store({ texture, region, kind: opts.kind, note: opts.note, label: opts.label });
    }

    // First "prompt driven by a thumbnail" hook: selecting a specimen arms it as
    // context for the next action (sent to the backend as investigation_id) and
    // opens the ACT input so you can act on what you examined.
    function use(entry) {
      state.selectedInvestigation = entry;
      try { document.dispatchEvent(new CustomEvent("investigation:select", { detail: entry })); } catch (_) {}
      render(); // reflect the selection highlight
      if (!state.gameOver && !state.processing) openFreeWill();
      Sound.select();
      showRendererToast((entry.kind === "photo" ? "Photo" : "Specimen") + " loaded — describe your action");
    }

    return {
      capture, store, render, use,
      all: () => items.slice(),
      clear() { items = []; persist(); render(); },
    };
  })();
  window.Investigations = Investigations;

  // ------------------------------------------------------------------
  // Evidence — the run's photography SCORE + the CASE FILE census that gives
  // the whole loop a GOAL (a win condition). Drawing on the greats: a
  // Beyond-Good-&-Evil-style census (document N distinct subjects to close the
  // case), a Pokémon-Snap-style grade (RANK D→S from your evidence), and
  // Umurangi-ish legible bonuses so points never feel arbitrary. Every photo is
  // appraised; new/rare/well-framed subjects pay more and fill the dossier.
  // Persistent per-run + exposed as window.Evidence for future engine hooks.
  // ------------------------------------------------------------------
  const CASE_TARGET = 8;   // distinct subjects to document to CLOSE THE CASE (win)
  const Evidence = (function () {
    const KEY = "evidence_v1";
    let total = 0, shots = 0;
    let seen = new Set();
    try {
      const raw = JSON.parse(localStorage.getItem(KEY) || "null");
      if (raw && typeof raw === "object") {
        total = Number(raw.total) || 0;
        shots = Number(raw.shots) || 0;
        seen = new Set(Array.isArray(raw.seen) ? raw.seen : []);
      }
    } catch (_) {}

    function persist() {
      try { localStorage.setItem(KEY, JSON.stringify({ total, shots, seen: [...seen] })); } catch (_) {}
    }

    function fmt(n) { return Math.round(n).toLocaleString("en-US"); }

    // Photographer's grade from the evidence banked (shown live + on the win
    // screen). Thresholds are tuned so a completed case lands around A/S.
    function rankFor(t) {
      if (t >= 3200) return "S";
      if (t >= 2200) return "A";
      if (t >= 1300) return "B";
      if (t >= 650) return "C";
      return "D";
    }

    // Paint the RANK badge + the CASE FILE census bar.
    function renderCase() {
      if (!el.evidenceHud) return;
      const rankEl = el.evidenceHud.querySelector(".ev-rank");
      if (rankEl) {
        const r = rankFor(total);
        rankEl.textContent = "RANK " + r;
        rankEl.className = "ev-rank rank-" + r;
      }
      const count = Math.min(seen.size, CASE_TARGET);
      const fill = el.evidenceHud.querySelector(".ev-bar-fill");
      if (fill) fill.style.width = Math.round((count / CASE_TARGET) * 100) + "%";
      const countEl = el.evidenceHud.querySelector(".ev-case-count");
      if (countEl) countEl.textContent = seen.size + "/" + CASE_TARGET;
      el.evidenceHud.classList.toggle("case-complete", seen.size >= CASE_TARGET);
    }

    // Roll the HUD number from its current display value up to `total`, ticking
    // as it climbs — so points visibly accrue rather than snapping.
    function renderHud(animate) {
      if (!el.evidenceHud) return;
      el.evidenceHud.classList.remove("hidden");
      renderCase();
      const totalEl = el.evidenceHud.querySelector(".ev-total");
      if (!totalEl) return;
      const from = Number(totalEl.getAttribute("data-val")) || 0;
      const to = total;
      totalEl.setAttribute("data-val", String(to));
      if (!animate || from === to || prefersReducedMotion()) {
        totalEl.textContent = fmt(to);
        return;
      }
      el.evidenceHud.classList.remove("bump");
      void el.evidenceHud.offsetWidth;
      el.evidenceHud.classList.add("bump");
      const t0 = performance.now();
      const dur = Math.min(900, 220 + Math.abs(to - from) * 1.4);
      let lastTick = 0;
      function step(now) {
        const p = Math.min(1, (now - t0) / dur);
        const eased = 1 - Math.pow(1 - p, 3);
        const v = from + (to - from) * eased;
        totalEl.textContent = fmt(v);
        if (now - lastTick > 55) { try { Sound.scoreTick(); } catch (_) {} lastTick = now; }
        if (p < 1) requestAnimationFrame(step);
        else totalEl.textContent = fmt(to);
      }
      requestAnimationFrame(step);
    }

    return {
      total: () => total,
      shots: () => shots,
      uniqueCount: () => seen.size,
      target: () => CASE_TARGET,
      rank: () => rankFor(total),
      isNew: (label) => !!label && !seen.has(String(label).toLowerCase()),
      markSeen: (label) => { if (label) seen.add(String(label).toLowerCase()); },
      addShot: () => { shots += 1; },
      add(points) { total = Math.max(0, total + (Number(points) || 0)); persist(); renderHud(true); },
      // Flash the HUD when a brand-new subject joins the case file.
      pulseSubject() {
        if (!el.evidenceHud) return;
        el.evidenceHud.classList.remove("subject");
        void el.evidenceHud.offsetWidth;
        el.evidenceHud.classList.add("subject");
      },
      // Show the dossier HUD even before the first point (so the goal is known).
      reveal() { if (el.evidenceHud) { el.evidenceHud.classList.remove("hidden"); renderCase(); } },
      renderHud,
      reset() {
        total = 0; shots = 0; seen = new Set(); persist();
        if (el.evidenceHud) {
          el.evidenceHud.classList.add("hidden");
          el.evidenceHud.classList.remove("case-complete");
          const t = el.evidenceHud.querySelector(".ev-total");
          if (t) { t.textContent = "0"; t.setAttribute("data-val", "0"); }
          renderCase();
        }
      },
    };
  })();
  window.Evidence = Evidence;

  // ------------------------------------------------------------------
  // Photo — the reward loop that ties a capture to feedback + score. On capture
  // it files the specimen to the CASE FILE (as before), then prints a "receipt"
  // in the top-right: the shot develops, its contents are named and revealed
  // one at a time (each with a rising chime + points), and a rating stamp lands.
  // A newer capture cancels an in-flight reveal so receipts never stack.
  // ------------------------------------------------------------------
  const Photo = (function () {
    const STAGGER_MS = 380;       // gap between item reveals
    const HOLD_MS = 3200;         // how long the finished receipt lingers
    const BASE_PER_INTEREST = 40; // points per interest point (1..5)
    const NOVELTY_BONUS = 60;     // first time a subject is photographed this run
    const RARE_BONUS = 80;        // a striking 5-star "rare find" premium
    const CONSOLATION = 10;       // an "undeveloped" shot still pays a little

    const TIERS = [
      { min: 0,   tier: 0, label: "UNDEVELOPED" },
      { min: 1,   tier: 1, label: "SNAPSHOT" },
      { min: 150, tier: 2, label: "EVIDENCE" },
      { min: 350, tier: 3, label: "KEY EVIDENCE" },
      { min: 600, tier: 4, label: "SMOKING GUN" },
    ];
    function ratingFor(shotTotal) {
      let r = TIERS[0];
      for (const t of TIERS) if (shotTotal >= t.min) r = t;
      return r;
    }

    function clearTimers() {
      state.receiptTimers.forEach((t) => clearTimeout(t));
      state.receiptTimers = [];
    }
    function later(fn, ms) { const t = setTimeout(fn, ms); state.receiptTimers.push(t); return t; }

    function els() {
      const r = el.photoReceipt;
      if (!r) return null;
      return {
        root: r,
        photo: r.querySelector(".receipt-photo"),
        status: r.querySelector(".receipt-status"),
        caption: r.querySelector(".receipt-caption"),
        items: r.querySelector(".receipt-items"),
        subtotal: r.querySelector(".receipt-subtotal"),
        subVal: r.querySelector(".receipt-subtotal .rs-val"),
        stamp: r.querySelector(".receipt-stamp"),
      };
    }

    function hide() {
      const r = el.photoReceipt;
      if (!r) return;
      clearTimers();
      r.classList.add("leaving");
      r.classList.remove("show");
      later(() => {
        r.classList.add("hidden");
        r.classList.remove("leaving", "developing", "tallied");
      }, 320);
    }

    // Entry point from the two capture paths.
    // `spec` = {texture, region, kind, label, zoom}.
    function capture(spec) {
      if (!spec || !spec.texture) return;
      // File the specimen exactly as before (case file + server mirror).
      try { Investigations.store(spec); } catch (_) {}
      reveal(spec.texture, spec.zoom || 1, !!spec.centered);
    }

    function reveal(texture, zoom, centered) {
      const parts = els();
      if (!parts) return;
      const token = ++state.receiptToken; // invalidate any older reveal
      clearTimers();

      // Reset + open the receipt in its "developing" state.
      parts.items.innerHTML = "";
      parts.caption.textContent = "";
      parts.stamp.textContent = "";
      parts.stamp.className = "receipt-stamp";
      if (parts.subVal) parts.subVal.textContent = "+0";
      parts.status.textContent = "DEVELOPING\u2026";
      if (parts.photo && texture) parts.photo.style.backgroundImage = `url('${texture}')`;
      parts.root.classList.remove("hidden", "leaving", "tallied");
      parts.root.classList.add("developing");
      void parts.root.offsetWidth;
      parts.root.classList.add("show");
      try { Sound.receiptOpen(); } catch (_) {}

      postJSON("/api/photo", { frame: texture })
        .then((res) => { if (token === state.receiptToken) printReceipt(token, res || {}, zoom, centered); })
        .catch((err) => {
          console.warn("[standalone] photo appraise failed:", err);
          if (token === state.receiptToken) printReceipt(token, { items: [] }, zoom, centered);
        });
    }

    function printReceipt(token, appraisal, zoom, centered) {
      const parts = els();
      if (!parts || token !== state.receiptToken) return;
      parts.root.classList.remove("developing");
      const items = Array.isArray(appraisal.items) ? appraisal.items : [];
      Evidence.addShot();

      if (!items.length) {
        // Nothing legible — a gentle consolation so it never feels punishing.
        parts.status.textContent = "NO CLEAR EVIDENCE";
        if (appraisal.caption) parts.caption.textContent = appraisal.caption;
        Evidence.add(CONSOLATION);   // a small pity payout so it never feels punishing
        finishStamp(token, 0);       // ...but the shot itself rates UNDEVELOPED
        scheduleDismiss(token);
        return;
      }

      parts.status.textContent = "EVIDENCE LOGGED";
      if (appraisal.caption) parts.caption.textContent = "\u201C" + appraisal.caption + "\u201D";

      let shotTotal = 0;
      let newCount = 0;
      const reduced = prefersReducedMotion();
      items.forEach((it, i) => {
        const delay = reduced ? 0 : i * STAGGER_MS;
        later(() => {
          if (token !== state.receiptToken) return;
          const label = String(it.label || "?");
          const interest = Math.max(1, Math.min(5, Number(it.interest) || 2));
          const isNew = Evidence.isNew(label);
          const isRare = interest >= 5;
          // New subjects fill the case file (novelty bonus); rare (5-star) finds
          // pay a premium. Both are called out so the points read as earned.
          let pts = interest * BASE_PER_INTEREST + (isNew ? NOVELTY_BONUS : 0) + (isRare ? RARE_BONUS : 0);
          Evidence.markSeen(label);
          if (isNew) newCount += 1;
          shotTotal += pts;
          appendItemRow(parts, { label, interest, note: it.note, pts, isNew, isRare });
          if (parts.subVal) parts.subVal.textContent = "+" + Math.round(shotTotal);
          if (isNew) { try { Sound.newSubject(); } catch (_) {} Evidence.pulseSubject(); }
          else { try { Sound.itemReveal(i); } catch (_) {} }
          Evidence.add(pts); // each find visibly bumps the TOP score + case bar
        }, delay);
      });

      // After the last item: composition + tight-framing bonuses, then stamp.
      const afterItems = reduced ? 10 : items.length * STAGGER_MS + 220;
      later(() => {
        if (token !== state.receiptToken) return;
        parts.root.classList.add("tallied");
        // Busier, well-composed shots earn an escalating combo on top.
        if (items.length >= 2) {
          const combo = Math.round(shotTotal * 0.15 * (items.length - 1));
          shotTotal += combo;
          appendBonusRow(parts, "composition \u00d7" + items.length, combo);
          Evidence.add(combo);
        }
        // A tight, zoomed-in frame is a "detail shot" — rewards using the zoom.
        if (zoom && zoom > 1.3) {
          const framing = Math.round(shotTotal * 0.12 * Math.min(1.5, zoom - 1));
          if (framing > 0) {
            shotTotal += framing;
            appendBonusRow(parts, "tight framing " + zoom.toFixed(1) + "\u00d7", framing);
            Evidence.add(framing);
          }
        }
        // Nailing the detected subject dead-center is the money shot.
        if (centered) {
          const bonus = Math.round(shotTotal * 0.2);
          if (bonus > 0) {
            shotTotal += bonus;
            appendBonusRow(parts, "subject centered", bonus);
            Evidence.add(bonus);
          }
        }
        if (parts.subVal) parts.subVal.textContent = "+" + Math.round(shotTotal);
        finishStamp(token, shotTotal);
        scheduleDismiss(token);
        // The win condition: enough distinct subjects closes the case.
        maybeCloseCase();
      }, afterItems);
    }

    function appendItemRow(parts, o) {
      const li = document.createElement("li");
      li.className = "receipt-item";
      const stars = "\u2605".repeat(o.interest) + "\u2606".repeat(5 - o.interest);
      const label = document.createElement("span");
      label.className = "ri-label";
      label.textContent = o.label;
      if (o.isNew) {
        const nw = document.createElement("span");
        nw.className = "ri-new";
        nw.textContent = "NEW";
        label.appendChild(nw);
      }
      if (o.isRare) {
        const rr = document.createElement("span");
        rr.className = "ri-rare";
        rr.textContent = "RARE";
        label.appendChild(rr);
      }
      const pts = document.createElement("span");
      pts.className = "ri-pts";
      pts.textContent = "+" + Math.round(o.pts);
      const note = document.createElement("span");
      note.className = "ri-note";
      note.innerHTML = `<span class="ri-stars">${stars}</span>` + (o.note ? "  " + escapeHtml(o.note) : "");
      li.appendChild(label);
      li.appendChild(pts);
      li.appendChild(note);
      parts.items.appendChild(li);
    }

    function appendBonusRow(parts, labelText, pts) {
      const li = document.createElement("li");
      li.className = "receipt-item";
      const label = document.createElement("span");
      label.className = "ri-label";
      label.textContent = labelText;
      const p = document.createElement("span");
      p.className = "ri-pts";
      p.textContent = "+" + Math.round(pts);
      li.appendChild(label);
      li.appendChild(p);
      parts.items.appendChild(li);
    }

    function finishStamp(token, shotTotal) {
      const parts = els();
      if (!parts || token !== state.receiptToken) return;
      const r = ratingFor(shotTotal);
      parts.stamp.textContent = r.label;
      parts.stamp.className = "receipt-stamp tier-" + r.tier;
      void parts.stamp.offsetWidth;
      parts.stamp.classList.add("stamped");
      try { Sound.stamp(); } catch (_) {}
    }

    function scheduleDismiss(token) {
      later(() => { if (token === state.receiptToken) hide(); }, HOLD_MS);
    }

    return { capture, reveal, hide, clearTimers };
  })();
  window.Photo = Photo;

  // ------------------------------------------------------------------
  // Win condition — CLOSE THE CASE. Documenting enough DISTINCT subjects
  // (the dossier census) completes the assignment and grades the run with a
  // photographer's RANK. Fires once per run; the player can start a new case
  // or dismiss and keep shooting the same world.
  // ------------------------------------------------------------------
  const CASE_FLAVORS = {
    S: "A flawless dossier. Every subject catalogued, the whole picture developed.",
    A: "A sharp, thorough case. The story's in the details you caught.",
    B: "A solid file — enough evidence to make the pattern undeniable.",
    C: "The case holds together. A few more angles and it would sing.",
    D: "Barely a case, but the shutter never lies. It's on the record now.",
  };

  function maybeCloseCase() {
    if (state.caseWon) return;
    if (Evidence.uniqueCount() < Evidence.target()) return;
    state.caseWon = true;
    // Let the receipt's stamp land first, then celebrate.
    setTimeout(showCaseWin, 700);
  }

  function showCaseWin() {
    if (!el.caseOverlay || !state.caseWon) return; // a reset may have cancelled it
    try { Photo.hide(); } catch (_) {} // clear the receipt so the win screen is clean
    const rank = Evidence.rank();
    if (el.caseRankLetter) el.caseRankLetter.textContent = rank;
    if (el.caseSubjects) el.caseSubjects.textContent = String(Evidence.uniqueCount());
    if (el.caseEvidence) el.caseEvidence.textContent = Evidence.total().toLocaleString("en-US");
    if (el.caseShots) el.caseShots.textContent = String(Evidence.shots());
    if (el.caseFlavor) el.caseFlavor.textContent = CASE_FLAVORS[rank] || CASE_FLAVORS.C;
    el.caseOverlay.classList.remove("hidden");
    try { Sound.caseSolved(); } catch (_) {}
  }

  function hideCaseWin() {
    if (el.caseOverlay) el.caseOverlay.classList.add("hidden");
  }

  // ------------------------------------------------------------------
  // CAMERA (SNAP) tool — arming it turns the whole scene into a capture surface:
  // a CAMERA reticle follows the pointer/finger, and a tap/click shoots a photo
  // of the region under it — collected as "evidence" in the case file with a
  // satisfying flourish. Pointer-driven so it works on iOS (tap = capture).
  // Realtime mode only (captures the live world-model frame).
  // ------------------------------------------------------------------
  function openTouch() {
    if (state.gameOver || state.freeWillOpen) return;
    if (state.touchMode) { closeTouch(); return; } // toggle off if already armed
    if (Renderer.mode !== "reactor" || !Renderer.reactorAvailable()) return;
    closeScan(); // the two realtime instruments are mutually exclusive
    state.touchMode = "aim";
    if (el.realtimeBtn) el.realtimeBtn.classList.add("aiming");
    document.body.classList.add("touch-aiming");
    // Raising the "camera" pushes gently into the scene (a viewfinder feel).
    state.photoPointers.clear();
    state.pinchBase = null;
    state.pinchActive = false;
    setPhotoZoom(PHOTO_ZOOM_ARMED, { silent: true, force: true });
    try { Evidence.reveal(); } catch (_) {} // surface the CASE FILE goal on pickup
    // Start the reticle where it was last, else at the center of the view.
    const start = state.touchPoint ||
      { x: window.innerWidth / 2, y: window.innerHeight / 2 };
    moveReticle(start.x, start.y);
    if (el.touchLayer) el.touchLayer.classList.remove("hidden");
    startPhotoTargeting(); // begin surfacing photographable subjects
    Sound.open();
  }

  // ------------------------------------------------------------------
  // Optical zoom — while the camera is armed, scroll (mouse) or pinch (touch)
  // magnifies the scene AROUND THE RETICLE (where you're aiming), not the
  // viewport center. The magnification is anchored to the aim point and the
  // scene layers carry a CSS transform transition, so as you move the mouse the
  // zoomed view smoothly glides to follow it — an FPS-scope feel. The capture
  // crop is derived from the framed (magnified) view, so zooming in genuinely
  // captures a tighter, telephoto slice of exactly what you're looking at.
  // ------------------------------------------------------------------
  const PHOTO_ZOOM_MIN = 1.0;    // full wide
  const PHOTO_ZOOM_MAX = 3.0;    // max telephoto (reasonable bound)
  const PHOTO_ZOOM_ARMED = 1.12; // gentle push-in the instant the camera is raised

  function clampZoom(z) { return Math.max(PHOTO_ZOOM_MIN, Math.min(PHOTO_ZOOM_MAX, z)); }

  // The scene transform currently applied (identity unless the camera is armed).
  // Centralized so the capture crop math can invert it. The origin is the
  // RETICLE point: scaling about it keeps whatever is under the reticle locked
  // under the reticle while everything else magnifies around it.
  function getSceneTransform() {
    const scale = state.touchMode ? (state.photoZoom || 1) : 1;
    const p = state.touchPoint || { x: window.innerWidth / 2, y: window.innerHeight / 2 };
    return { scale, ox: p.x, oy: p.y };
  }

  // Apply scale-about-reticle as `translate(t) scale(z)` with transform-origin
  // 0 0, where t = (1 - z) * reticle. Expressing it this way (instead of moving
  // transform-origin) means only the translate changes as the reticle moves, so
  // the CSS transform transition can smoothly interpolate the pan — the view
  // glides to follow the cursor instead of snapping.
  function applySceneTransform() {
    const z = state.touchMode ? (state.photoZoom || 1) : 1;
    let val = "";
    if (z !== 1) {
      const t = getSceneTransform();
      const tx = ((1 - z) * t.ox).toFixed(2);
      const ty = ((1 - z) * t.oy).toFixed(2);
      val = `translate(${tx}px, ${ty}px) scale(${z.toFixed(4)})`;
    }
    [el.sceneA, el.sceneB, el.reactorVideo, el.reactorFreeze].forEach((n) => {
      if (n) n.style.transform = val;
    });
    if (el.touchZoom) el.touchZoom.innerHTML = (state.photoZoom || 1).toFixed(1) + "&times;";
  }

  function setPhotoZoom(z, opts) {
    opts = opts || {};
    const clamped = clampZoom(z);
    if (!opts.force && Math.abs(clamped - state.photoZoom) < 0.004) return;
    state.photoZoom = clamped;
    applySceneTransform();
    if (el.touchZoom) { // pop the readout only on an actual zoom change
      el.touchZoom.classList.remove("bump");
      void el.touchZoom.offsetWidth;
      el.touchZoom.classList.add("bump");
    }
    if (!opts.silent) {
      try { Sound.zoom((clamped - PHOTO_ZOOM_MIN) / (PHOTO_ZOOM_MAX - PHOTO_ZOOM_MIN)); } catch (_) {}
    }
  }

  function clearSceneZoom() {
    state.photoZoom = 1;
    [el.sceneA, el.sceneB, el.reactorVideo, el.reactorFreeze].forEach((n) => {
      if (n) n.style.transform = "";
    });
  }

  function photoPointerDist() {
    const pts = [...state.photoPointers.values()];
    if (pts.length < 2) return 0;
    return Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
  }

  // Mouse wheel = smooth multiplicative zoom (down = out, up = in).
  function onTouchWheel(e) {
    if (state.touchMode !== "aim") return;
    e.preventDefault();
    setPhotoZoom(state.photoZoom * Math.exp(-e.deltaY * 0.0015));
  }

  // ------------------------------------------------------------------
  // Photographable TARGETS — the "is this a worthy shot?" system. While the
  // camera is armed we run the same realtime object detection the SCAN tool
  // uses (/api/detect) and float targeting brackets over each thing the world
  // model can actually see. Framing one (its center inside the capture box)
  // LOCKS it as a subject; only then does a shot gather evidence. This shows
  // players what they can photograph and confirms a capture is real, using
  // genuine in-game perception data.
  // ------------------------------------------------------------------
  const PHOTO_DETECT_INTERVAL_MS = 2600; // idle re-detect cadence while armed
  const PHOTO_DETECT_MIN_MS = 2100;      // floor between detection calls (LLM latency)

  // Map a normalized source point (0..1) to its on-screen position, accounting
  // for the object-fit:cover crop AND the current optical-zoom transform — the
  // inverse of screenToNorm, so markers sit exactly on the displayed subject.
  function normToPhotoScreen(cx, cy) {
    const W = window.innerWidth, H = window.innerHeight;
    const size = currentSourceSize();
    let x, y;
    if (!size || !size.w || !size.h) { x = cx * W; y = cy * H; }
    else {
      const scale = Math.max(W / size.w, H / size.h);
      const dw = size.w * scale, dh = size.h * scale;
      const ox = (W - dw) / 2, oy = (H - dh) / 2;
      x = ox + cx * dw; y = oy + cy * dh;
    }
    const t = getSceneTransform();
    if (t && t.scale !== 1) {
      x = t.ox + (x - t.ox) * t.scale;
      y = t.oy + (y - t.oy) * t.scale;
    }
    return { x, y };
  }

  // Detected subjects whose center falls inside a capture box at (cx,cy).
  function framedTargets(cx, cy, boxPx) {
    const half = boxPx / 2;
    return (state.photoTargets || []).filter((o) => {
      const p = normToPhotoScreen(o.cx, o.cy);
      return Math.abs(p.x - cx) <= half && Math.abs(p.y - cy) <= half;
    });
  }

  function startPhotoTargeting() {
    state.photoTargets = [];
    state.photoDetected = false;
    state.photoLockedLabel = null;
    if (el.touchTargets) el.touchTargets.innerHTML = "";
    runPhotoDetect(true);
  }

  function stopPhotoTargeting() {
    clearTimeout(state.photoDetectTimer);
    state.photoDetectTimer = null;
    state.photoTargets = [];
    state.photoDetected = false;
    state.photoLockedLabel = null;
    if (el.touchTargets) el.touchTargets.innerHTML = "";
    if (el.touchLock) { el.touchLock.classList.remove("show"); el.touchLock.textContent = ""; }
    if (el.touchCaptureFrame) el.touchCaptureFrame.classList.remove("locked");
  }

  function schedulePhotoDetect() {
    clearTimeout(state.photoDetectTimer);
    state.photoDetectTimer = setTimeout(() => {
      if (state.touchMode === "aim") runPhotoDetect(false);
    }, PHOTO_DETECT_INTERVAL_MS);
  }

  function runPhotoDetect(force) {
    if (state.touchMode !== "aim" || state.photoDetectBusy) return;
    const now = Date.now();
    if (!force && now - state.photoDetectLast < PHOTO_DETECT_MIN_MS) { schedulePhotoDetect(); return; }
    const cap = captureScanFrame();
    if (!cap || !cap.frame) { schedulePhotoDetect(); return; }
    state.photoDetectBusy = true;
    state.photoDetectLast = now;
    postJSON("/api/detect", { frame: cap.frame })
      .then((res) => {
        if (state.touchMode !== "aim") return;
        state.photoDetected = true;
        state.photoTargets = (res && Array.isArray(res.objects)) ? res.objects : [];
        renderPhotoTargets();
      })
      .catch((err) => { console.warn("[standalone] photo detect failed:", err); })
      .finally(() => {
        state.photoDetectBusy = false;
        if (state.touchMode === "aim") schedulePhotoDetect();
      });
  }

  // Rebuild the marker elements to match the latest detection, then lay them out.
  function renderPhotoTargets() {
    if (!el.touchTargets) return;
    el.touchTargets.innerHTML = "";
    (state.photoTargets || []).forEach((o) => {
      const m = document.createElement("div");
      m.className = "photo-target";
      m._obj = o;
      const tr = document.createElement("span"); tr.className = "pt-tr";
      const bl = document.createElement("span"); bl.className = "pt-bl";
      const label = document.createElement("span");
      label.className = "pt-label";
      label.textContent = o.label || "";
      m.appendChild(tr); m.appendChild(bl); m.appendChild(label);
      el.touchTargets.appendChild(m);
    });
    layoutPhotoTargets();
  }

  // Position every marker for the current zoom/pan and light up the ones that
  // are framed; name the nearest framed subject as the locked target.
  function layoutPhotoTargets() {
    if (!el.touchTargets) return;
    const box = investBoxPx();
    const half = box / 2;
    const rx = state.touchPoint ? state.touchPoint.x : window.innerWidth / 2;
    const ry = state.touchPoint ? state.touchPoint.y : window.innerHeight / 2;
    const W = window.innerWidth, H = window.innerHeight;
    let lockedLabel = null, lockedDist = Infinity;
    Array.from(el.touchTargets.children).forEach((m) => {
      const o = m._obj;
      if (!o) return;
      const p = normToPhotoScreen(o.cx, o.cy);
      m.style.left = p.x + "px";
      m.style.top = p.y + "px";
      m.classList.toggle("off", p.x < -60 || p.x > W + 60 || p.y < -60 || p.y > H + 60);
      const inFrame = Math.abs(p.x - rx) <= half && Math.abs(p.y - ry) <= half;
      m.classList.toggle("in-frame", inFrame);
      if (inFrame) {
        const d = Math.hypot(p.x - rx, p.y - ry);
        if (d < lockedDist) { lockedDist = d; lockedLabel = o.label; }
      }
    });
    const hadLock = !!state.photoLockedLabel;
    state.photoLockedLabel = lockedLabel;
    if (el.touchCaptureFrame) el.touchCaptureFrame.classList.toggle("locked", !!lockedLabel);
    if (el.touchLock) {
      el.touchLock.classList.toggle("show", !!lockedLabel);
      el.touchLock.textContent = lockedLabel ? ("\u25CE SUBJECT: " + lockedLabel) : "";
    }
    if (lockedLabel && !hadLock) { try { Sound.lock(); } catch (_) {} } // a fresh lock chirps
  }

  // Empty-frame feedback: a quick red shake + soft tone + a plain-language nudge.
  function photoMiss(msg) {
    showRendererToast(msg);
    try { Sound.miss(); } catch (_) {}
    if (el.touchCaptureFrame) {
      el.touchCaptureFrame.classList.remove("miss");
      void el.touchCaptureFrame.offsetWidth;
      el.touchCaptureFrame.classList.add("miss");
    }
  }

  function moveReticle(x, y) {
    state.touchPoint = { x, y };
    if (el.touchReticle) {
      el.touchReticle.style.left = x + "px";
      el.touchReticle.style.top = y + "px";
    }
    // The capture frame tracks the camera so you see exactly what will be shot.
    if (el.touchCaptureFrame) {
      const b = investBoxPx();
      el.touchCaptureFrame.style.width = b + "px";
      el.touchCaptureFrame.style.height = b + "px";
      el.touchCaptureFrame.style.left = x + "px";
      el.touchCaptureFrame.style.top = y + "px";
    }
    // Re-anchor the zoom to the new aim point so the magnified view smoothly
    // follows the reticle (the CSS transform transition does the gliding).
    if (state.photoZoom && state.photoZoom !== 1) applySceneTransform();
    // Re-evaluate which subject is framed as we aim (and reposition markers,
    // since the zoom origin moved with the reticle).
    if (state.touchMode === "aim") layoutPhotoTargets();
  }

  // Pointer-driven so it works with mouse (hover) AND touch (drag) alike.
  // Two fingers down pinch-to-zoom instead of aiming; a lone pointer aims.
  function onTouchMove(e) {
    if (state.touchMode !== "aim") return;
    if (state.photoPointers.has(e.pointerId)) {
      state.photoPointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    }
    if (state.photoPointers.size >= 2 && state.pinchBase) {
      const d = photoPointerDist();
      if (d > 0 && state.pinchBase.dist > 0) {
        setPhotoZoom(state.pinchBase.zoom * (d / state.pinchBase.dist));
      }
      return; // don't move the reticle mid-pinch
    }
    moveReticle(e.clientX, e.clientY);
  }

  // Press to aim; a clean single-pointer release shoots. A second finger turns
  // the gesture into a pinch (and suppresses the shot) so zoom never fires a
  // stray capture. On desktop, hover aims and a click (down+up) shoots.
  // Right/middle click is a quick "exit the camera" gesture.
  function onTouchDown(e) {
    if (state.touchMode !== "aim") return;
    if (e.button && e.button !== 0) { e.preventDefault(); closeTouch(); return; }
    e.preventDefault();
    state.photoPointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (state.photoPointers.size === 1) {
      moveReticle(e.clientX, e.clientY);
    } else if (state.photoPointers.size === 2) {
      state.pinchActive = true;
      state.pinchBase = { dist: photoPointerDist(), zoom: state.photoZoom };
    }
  }

  function onTouchUp(e) {
    if (!state.photoPointers.has(e.pointerId)) return;
    const wasSingle = state.photoPointers.size === 1;
    state.photoPointers.delete(e.pointerId);
    if (state.touchMode === "aim" && wasSingle && !state.pinchActive && e.type === "pointerup" &&
        (!e.button || e.button === 0)) {
      captureAt(e.clientX, e.clientY); // shoot on a clean single-finger release
    }
    if (state.photoPointers.size < 2) state.pinchBase = null;
    if (state.photoPointers.size === 0) state.pinchActive = false;
  }

  // Right-click anywhere on the aiming surface exits the camera (and never shows
  // the browser context menu).
  function onTouchContextMenu(e) {
    if (state.touchMode !== "aim") return;
    e.preventDefault();
    closeTouch();
  }

  // A pointer can lift OFF the aiming surface (e.g. over the raised PHOTO/rail
  // controls). Clean it up globally so a tap/pinch can never get "stuck" — this
  // never captures (the layer's own pointerup handles that).
  function onTouchPointerCleanup(e) {
    if (!state.photoPointers.has(e.pointerId)) return;
    state.photoPointers.delete(e.pointerId);
    if (state.photoPointers.size < 2) state.pinchBase = null;
    if (state.photoPointers.size === 0) state.pinchActive = false;
  }

  // Take the shot: crop the region under the reticle, flash, sound, file it to
  // the case file, and pop the satisfying evidence flourish. Stays armed so you
  // can keep gathering evidence tap after tap.
  function captureAt(x, y) {
    moveReticle(x, y);
    const boxPx = investBoxPx();
    const framed = framedTargets(x, y, boxPx);
    // WORTHY-SHOT GATE: once detection is live, the frame must contain a real
    // detected subject to count as evidence. (Before the first detection
    // returns we give the benefit of the doubt so latency never eats a shot.)
    if (state.photoDetected && !framed.length) {
      photoMiss(state.photoTargets.length
        ? "No subject in frame \u2014 line up a target"
        : "Nothing to photograph here \u2014 explore to find evidence");
      return;
    }
    // Which subject did we catch, and is it well-centered (a great shot)?
    let subject = null, centered = false, best = Infinity;
    framed.forEach((o) => {
      const p = normToPhotoScreen(o.cx, o.cy);
      const d = Math.hypot(p.x - x, p.y - y);
      if (d < best) { best = d; subject = o.label; }
    });
    if (framed.length) centered = best <= boxPx * 0.3;
    const region = screenBoxToNorm(x, y, boxPx);
    const texture = captureSceneRegion(region, 512); // larger region → keep detail
    if (!texture) { showRendererToast("Couldn't capture \u2014 hold steady"); return; }
    // Frame flash + camera flash + shutter for a tactile "snap".
    if (el.touchCaptureFrame) {
      el.touchCaptureFrame.classList.remove("grab");
      void el.touchCaptureFrame.offsetWidth;
      el.touchCaptureFrame.classList.add("grab");
    }
    flashScene();
    try { Sound.shutter(); } catch (_) {}
    Photo.capture({
      texture, region, kind: "photo", label: describeTouchRegion({ x, y }).label,
      zoom: state.photoZoom || 1, subject, centered,
    });
  }

  function closeTouch() {
    if (!state.touchMode) return;
    state.touchMode = null;
    // Release the viewfinder magnification back to full wide.
    state.photoPointers.clear();
    state.pinchBase = null;
    state.pinchActive = false;
    clearSceneZoom();
    stopPhotoTargeting();
    if (el.touchLayer) el.touchLayer.classList.add("hidden");
    if (el.touchReticle) el.touchReticle.classList.remove("holding");
    if (el.touchCaptureFrame) el.touchCaptureFrame.classList.remove("grab");
    if (el.realtimeBtn) el.realtimeBtn.classList.remove("aiming");
    document.body.classList.remove("touch-aiming");
    updateAmbientScan(); // hotspots return once the camera is put away
  }

  // Turn a viewport position into a human region phrase (used to label evidence).
  function describeTouchRegion(pt) {
    if (!pt) return { label: "the scene", phrase: "" };
    const fx = pt.x / Math.max(1, window.innerWidth);
    const fy = pt.y / Math.max(1, window.innerHeight);
    const h = fx < 0.34 ? "left" : fx > 0.66 ? "right" : "center";
    const v = fy < 0.34 ? "top" : fy > 0.66 ? "bottom" : "middle";
    let label;
    if (h === "center" && v === "middle") label = "the center of the view";
    else if (h === "center") label = "the " + v + " of the view";
    else if (v === "middle") label = "the " + h + " of the view";
    else label = "the " + v + "-" + h + " of the view";
    return { label, phrase: "at " + label };
  }

  // The satisfying "gathered evidence" flourish: the freshly captured photo pops
  // up big for a beat, then files itself down into the CASE FILE tray and fades.
  function showEvidence(texture) {
    if (!el.evidenceCard || !texture) return;
    const photo = el.evidenceCard.querySelector(".evidence-photo");
    if (photo) photo.style.backgroundImage = `url('${texture}')`;
    clearTimeout(state.evidenceTimer);
    el.evidenceCard.classList.remove("hidden", "filing", "show");
    void el.evidenceCard.offsetWidth; // restart the animation
    el.evidenceCard.classList.add("show");
    state.evidenceTimer = setTimeout(() => {
      el.evidenceCard.classList.add("filing"); // fly down into the case file + fade
      state.evidenceTimer = setTimeout(() => {
        el.evidenceCard.classList.remove("show", "filing");
        el.evidenceCard.classList.add("hidden");
      }, 680);
    }, 1000);
  }

  // ------------------------------------------------------------------
  // PHOTOGRAPH (C key) — a quick CENTERED snapshot using the same camera +
  // evidence plumbing as the SNAP tool (which captures where you tap).
  // ------------------------------------------------------------------
  function capturePhoto() {
    if (state.gameOver) return;
    if (!currentSourceSize()) { showRendererToast("Nothing to photograph yet"); return; }
    const center = { x: window.innerWidth / 2, y: window.innerHeight / 2 };
    const boxPx = Math.round(Math.min(window.innerWidth, window.innerHeight) * 0.5);
    const region = screenBoxToNorm(center.x, center.y, boxPx);
    const texture = captureSceneRegion(region, 512);
    if (!texture) { showRendererToast("Couldn't capture the frame"); return; }
    flashScene();
    try { Sound.shutter(); } catch (_) {}
    Photo.capture({ texture, region, kind: "photo", label: "the center of the view", zoom: state.photoZoom || 1 });
  }

  // ------------------------------------------------------------------
  // Ambient interaction hotspots — object recognition with NO button. Whenever a
  // scene is on screen, the objects the model recognizes surface as floating
  // "starfield" tags anchored where they actually sit. Hovering near a tag
  // highlights it; clicking it opens an inline action bar to ACT on THAT exact
  // thing — a full turn (consequence + a freshly generated scene). Works in BOTH
  // renderers: it reads the live video frame in realtime mode, or the current
  // still in image mode.
  //
  // Engineering notes:
  //  • Detection is an LLM round-trip, so it's throttled (a floor between hits)
  //    and always DEFERS around a turn (never competes with the turn's own LLM
  //    calls). It runs once per scene and, in realtime, on hover-settle.
  //  • Tags are RECONCILED by label between passes (kept + repositioned, added
  //    with a twinkle, removed with a fade) so a refresh never churns the whole
  //    field or yanks a tag out from under the cursor.
  //  • A detection pass NEVER runs while a tag's action bar is open.
  //  • Works with mouse (hover) and touch (tap the tag); the overlay is
  //    non-modal so the game's choices/controls stay live underneath.
  // ------------------------------------------------------------------
  const SCAN_MOVE_SETTLE_MS = 600;    // re-detect this long after the cursor settles (realtime)
  const SCAN_NEAR_RADIUS = 150;       // px: how close the cursor "finds" a hotspot

  // SCAN works in BOTH renderers:
  //  • realtime (reactor): scans the live video frame.
  //  • stills (image): scans the current scene still.
  function scanInRealtime() {
    return Renderer.mode === "reactor" && Renderer.reactorAvailable() &&
      window.ReactorRenderer.isShowing && window.ReactorRenderer.isShowing();
  }

  // A loaded <img> of the current still (stills mode), cached per URL, or null
  // while it hasn't decoded yet. Served same-origin (/images/…) so it can be
  // drawn to a canvas without tainting it.
  function getStillImage() {
    const url = state.currentStillUrl ||
      (Renderer.lastScene && Renderer.lastScene.imageUrl) || null;
    if (!url) return null;
    if (!state.scanStillImg || state.scanStillImg.getAttribute("data-src") !== url) {
      const img = new Image();
      img.setAttribute("data-src", url);
      img.src = url;
      state.scanStillImg = img;
    }
    const img = state.scanStillImg;
    return (img && img.complete && img.naturalWidth > 0) ? img : null;
  }

  function scanAvailable() {
    if (scanInRealtime()) return true;
    // Stills mode: scannable once the current scene still has decoded.
    return Renderer.mode !== "reactor" && !!getStillImage();
  }

  // Grab the current scene as a JPEG data URL + its intrinsic size (for
  // cover-mapping tags), from whichever renderer is live.
  function captureScanFrame() {
    if (scanInRealtime()) {
      const frame = window.ReactorRenderer.captureFrame
        ? window.ReactorRenderer.captureFrame(640) : null;
      const size = (window.ReactorRenderer.getVideoSize && window.ReactorRenderer.getVideoSize()) || null;
      return frame ? { frame, size } : null;
    }
    const img = getStillImage();
    if (!img) return null;
    try {
      const cap = 640;
      const scale = Math.min(1, cap / img.naturalWidth);
      const w = Math.max(1, Math.round(img.naturalWidth * scale));
      const h = Math.max(1, Math.round(img.naturalHeight * scale));
      const c = document.createElement("canvas");
      c.width = w; c.height = h;
      c.getContext("2d").drawImage(img, 0, 0, w, h);
      return { frame: c.toDataURL("image/jpeg", 0.72), size: { w: img.naturalWidth, h: img.naturalHeight } };
    } catch (e) {
      console.warn("[standalone] still capture failed:", e);
      return null;
    }
  }

  // Ambient scan is allowed whenever there's a scene to read and no full-screen
  // instrument (camera, tape, free-will input) or end state is claiming the view.
  function ambientScanAllowed() {
    if (state.gameOver || state.touchMode || state.freeWillOpen) return false;
    if (typeof tapeIsOpen === "function" && tapeIsOpen()) return false;
    return scanAvailable();
  }

  // Ambient object hotspots: there's no SCAN button anymore. Whenever a scene is
  // on screen we quietly surface what's interactable as starfield tags; hovering
  // near one highlights it and a click opens its actions. This just ensures the
  // overlay is live and shows whatever we've already detected for this scene
  // (instantly, from cache) — detection itself runs via prewarmScan (per scene +
  // on hover-settle in realtime), always deferring around turns.
  function updateAmbientScan() {
    if (!ambientScanAllowed()) { closeScan(); return; }
    state.scanOn = true;
    if (el.scanLayer) el.scanLayer.classList.remove("hidden");
    const pw = state.scanPrewarm;
    if (pw && pw.objects && pw.objects.length) {
      if (pw.size) state.scanSrcSize = pw.size;
      reconcileScanTags(pw.objects);
      setScanHint("");
    } else if (!el.scanTags || !el.scanTags.children.length) {
      schedulePrewarm(150); // nothing detected yet — kick a detection now
    }
  }

  // Detection pass: read the current scene and cache + render the hotspots.
  // Runs per scene (via schedulePrewarm on scene settle) and on hover-settle in
  // realtime (the live video drifts). Throttled, single-flight, and it always
  // defers around a turn so it can't compete with the turn's own LLM calls.
  const SCAN_PREWARM_MIN_MS = 4000;
  const SCAN_PREWARM_TURN_COOLDOWN_MS = 9000; // stay off the wire around a turn
  function prewarmScan() {
    if (state.gameOver || state.scanBusy) return;
    if (state.scanTagActing) return; // don't reshuffle tags while a bar is open
    const now = Date.now();
    // Defer to gameplay: never add a background detection call while a turn is
    // resolving (or just did) — that competes with the turn's own LLM calls and
    // can rate-limit/slow them. Retry once the game is idle again.
    if (state.processing || state.awaitingResolution ||
        (now - (state.lastTurnTs || 0) < SCAN_PREWARM_TURN_COOLDOWN_MS)) {
      schedulePrewarm(2500);
      return;
    }
    if (!scanAvailable()) {
      // A still may just not have decoded yet — retry shortly so hotspots still
      // appear on their own. (Realtime is driven by the video_showing event.)
      if (Renderer.mode !== "reactor" && state.currentStillUrl) schedulePrewarm(600);
      return;
    }
    if (now - (state.scanPrewarm.ts || 0) < SCAN_PREWARM_MIN_MS) return;
    const cap = captureScanFrame();
    if (!cap || !cap.frame) return;
    state.scanBusy = true;
    state.scanPrewarm.ts = now;
    postJSON("/api/detect", { frame: cap.frame })
      .then((res) => {
        const objs = (res && Array.isArray(res.objects)) ? res.objects : [];
        state.scanPrewarm = { objects: objs, size: cap.size || null, ts: now };
        // Render straight onto the ambient overlay so hotspots appear/refresh
        // without any button press — turning the overlay on if needed.
        if (ambientScanAllowed() && !state.scanTagActing) {
          state.scanOn = true;
          if (el.scanLayer) el.scanLayer.classList.remove("hidden");
          if (cap.size) state.scanSrcSize = cap.size;
          reconcileScanTags(objs);
          setScanHint("");
        }
      })
      .catch((err) => { console.warn("[standalone] scan detect failed:", err); })
      .finally(() => { state.scanBusy = false; });
  }

  function schedulePrewarm(delay) {
    clearTimeout(state.scanPrewarmTimer);
    state.scanPrewarmTimer = setTimeout(prewarmScan, delay == null ? 1200 : delay);
  }

  // Suspend the ambient overlay: hide it and drop its tags. Used when a
  // full-screen instrument (camera/tape/free-will) or an end state takes over;
  // updateAmbientScan() brings it back (re-rendering from cache) when they close.
  function closeScan() {
    if (!state.scanOn && (!el.scanLayer || el.scanLayer.classList.contains("hidden"))) return;
    state.scanOn = false;
    state.scanTagActing = null;
    clearTimeout(state.scanMoveTimer); state.scanMoveTimer = null;
    document.body.classList.remove("scan-busy");
    if (el.scanLayer) el.scanLayer.classList.add("hidden");
    if (el.scanTags) el.scanTags.innerHTML = "";
    state.scanObjects = [];
  }

  function setScanHint(text) {
    if (!el.scanHint) return;
    el.scanHint.textContent = text || "";
    el.scanHint.classList.toggle("hidden", !text);
  }

  // Map normalized (0..1) frame coordinates onto the on-screen scene's
  // object-fit/background-size: cover display rect (video OR still) so a tag
  // lands exactly over its object on screen.
  function mapNormToScreen(nx, ny) {
    const W = window.innerWidth, H = window.innerHeight;
    const size = state.scanSrcSize ||
      (window.ReactorRenderer.getVideoSize && window.ReactorRenderer.getVideoSize()) || null;
    if (!size || !size.w || !size.h) return { x: nx * W, y: ny * H };
    const scale = Math.max(W / size.w, H / size.h);
    const dw = size.w * scale, dh = size.h * scale;
    const ox = (W - dw) / 2, oy = (H - dh) / 2;
    return { x: ox + nx * dw, y: oy + ny * dh };
  }

  // Hover: highlight the interaction possibility nearest the cursor so moving
  // over the scene "finds" the things in it. In realtime the live video drifts,
  // so a settle also refreshes detection (throttled + deferred around turns).
  function onScanMove(e) {
    if (!state.scanOn) return;
    highlightNearestTag(e.clientX, e.clientY);
    if (scanInRealtime() && !state.scanTagActing) {
      clearTimeout(state.scanMoveTimer);
      state.scanMoveTimer = setTimeout(() => { if (state.scanOn) prewarmScan(); }, SCAN_MOVE_SETTLE_MS);
    }
  }

  // Tapping empty scene (not a tag/control) dismisses an open action bar. On
  // mobile the tags themselves are tapped directly (their own click handler).
  function onScanTap(e) {
    if (!state.scanOn) return;
    const t = e.target;
    if (t && t.closest && t.closest(
      "#action-wheel, #control-rail, #scan-tags, #tape-overlay, " +
      "#death-overlay, #rt-log, button, a, input, form"
    )) return;
    if (state.scanTagActing) closeTagPrompt(state.scanTagActing);
  }

  function highlightNearestTag(x, y) {
    if (!el.scanTags) return;
    const tags = Array.from(el.scanTags.children);
    if (!tags.length) return;
    let best = null, bestD = Infinity;
    for (const t of tags) {
      if (t.classList.contains("leaving")) continue;
      const tx = parseFloat(t.dataset.sx || "0"), ty = parseFloat(t.dataset.sy || "0");
      const d = (tx - x) * (tx - x) + (ty - y) * (ty - y);
      if (d < bestD) { bestD = d; best = t; }
    }
    const near = best && bestD < (SCAN_NEAR_RADIUS * SCAN_NEAR_RADIUS);
    for (const t of tags) t.classList.toggle("near", near && t === best);
  }

  // Reconcile the on-screen tags against a fresh detection: keep + reposition
  // ones that persist, twinkle in new ones, fade out the gone — so a refresh
  // reads as the field breathing, not a hard redraw.
  function reconcileScanTags(objects) {
    if (!el.scanTags) return;
    const existing = new Map();
    Array.from(el.scanTags.children).forEach((t) => {
      if (t._label) existing.set(t._label, t);
    });
    const nextLabels = new Set();
    objects.forEach((obj, i) => {
      nextLabels.add(obj.label);
      let tag = existing.get(obj.label);
      if (tag) {
        tag._obj = obj;
        tag.classList.remove("leaving");
        positionScanTag(tag); // CSS transitions the move
      } else {
        tag = buildScanTag(obj);
        tag.style.setProperty("--twk", (i * 70) + "ms");
        el.scanTags.appendChild(tag);
        positionScanTag(tag);
        // Enable position transitions only AFTER first placement so a new tag
        // twinkles in at its spot instead of sliding in from the corner; on
        // later scans the persistent tag then glides to its new position.
        requestAnimationFrame(() => tag.classList.add("scan-live"));
      }
    });
    // Retire tags no longer detected (but keep one being actively poked).
    existing.forEach((tag, label) => {
      if (nextLabels.has(label)) return;
      if (tag === state.scanTagActing) return;
      tag.classList.add("leaving");
      setTimeout(() => {
        if (tag.parentNode && tag.classList.contains("leaving")) tag.remove();
      }, 420);
    });
    state.scanObjects = objects.slice();
  }

  function positionScanTag(tag) {
    const obj = tag._obj;
    if (!obj) return;
    const p = mapNormToScreen(obj.cx, obj.cy);
    // Keep tags on screen and OUT of the bottom control zone (action wheel +
    // hint) so labels near an edge stay readable and never sit on the buttons.
    // The bottom margin tracks the actual wheel height so it's right on phones
    // (taller wheel) and desktop alike.
    const wheelH = (el.actionWheel && el.actionWheel.offsetHeight) || 110;
    const bottomSafe = Math.max(150, wheelH + 56);
    const x = Math.min(Math.max(p.x, 62), window.innerWidth - 62);
    const y = Math.min(Math.max(p.y, 48), Math.max(80, window.innerHeight - bottomSafe));
    tag.style.left = x + "px";
    tag.style.top = y + "px";
    tag.dataset.sx = x;
    tag.dataset.sy = y;
  }

  // The actions a player can take on a detected object. Each composes a clean,
  // natural prompt from the verb + the object's own name — no typing. The
  // consequence LLM (server-side) already turns that intent into an in-world,
  // exciting outcome + a fresh scene, so there's no need for a separate
  // "action-writing" LLM. Add more verbs here to grow the vocabulary.
  // Objects you can go INSIDE / through — a passage, opening, vehicle, or
  // structure. When "MOVE TO" targets one of these, we phrase it as an ENTRY so
  // the engine cuts to a fresh interior scene (is_hard_transition fires on
  // "enter …"); otherwise it's an APPROACH that advances the camera closer
  // (the movement classifier keys on "approach", so the scene actually changes
  // instead of drifting in place — which is why plain "move to the X" sometimes
  // looked static).
  const ENTERABLE_RE = /\b(door|doorway|gate|gateway|entrance|entry|hatch|portal|threshold|arch|archway|opening|mouth|maw|tunnel|pipe|duct|corridor|hallway|hall|passage|passageway|stair|stairs|stairway|stairwell|room|building|house|cabin|shack|shed|garage|barn|cave|cavern|vault|chamber|window|breach|gap|hole|vent|shaft|elevator|lift|airlock|tent|bunker|silo|structure|ruin|ruins|store|shop|church|warehouse|facility|lab|laboratory|booth|trailer|van|truck|car|bus|train|carriage|wagon|boat|ship|cockpit|rig|derrick)\b/i;

  function moveActionPhrase(o) {
    if (ENTERABLE_RE.test(o)) {
      // "Enter …" -> hard transition -> a genuinely new interior scene.
      return "Enter the " + o + ", moving inside into the space beyond.";
    }
    // "approach" -> forward_movement -> the camera advances, so the scene visibly
    // changes as you close in.
    return "Move toward the " + o + ", approaching until it fills the view.";
  }

  const SCAN_ACTIONS = [
    {
      id: "move", label: "MOVE TO", title: "Move to",
      phrase: moveActionPhrase,
      icon: '<svg class="scan-action-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2v20M2 12h20"/><path d="M9 5l3-3 3 3M9 19l3 3 3-3M5 9l-3 3 3 3M19 9l3 3-3 3"/></svg>',
    },
    {
      id: "interact", label: "INTERACT", title: "Interact with",
      phrase: (o) => "Interact with the " + o + ".",
      icon: '<svg class="scan-action-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 13V5a2 2 0 0 1 4 0v6"/><path d="M12 11V4a2 2 0 0 1 4 0v7"/><path d="M16 11V7a2 2 0 0 1 4 0v8a6 6 0 0 1-6 6h-2a6 6 0 0 1-5-2.7l-2.8-4a2 2 0 0 1 3.1-2.5L9 14"/></svg>',
    },
  ];

  function buildScanTag(obj) {
    const tag = document.createElement("div");
    tag.className = "scan-tag";
    tag._obj = obj;
    tag._label = obj.label;
    // Once the twinkle-in finishes, hand off to a static "shown" state so the
    // finished animation's fill can't override hover/near/acting transforms.
    tag.addEventListener("animationend", (e) => {
      if (e.target === tag && e.animationName === "scan-twinkle-in") tag.classList.add("shown");
    });

    const star = document.createElement("span");
    star.className = "scan-star";
    star.textContent = "\u2726"; // ✦

    const label = document.createElement("span");
    label.className = "scan-tag-label";
    label.textContent = obj.label;

    // The "act" affordance: a small marker shown on hover/near. The WHOLE tag is
    // clickable to reveal its actions (see below), so no button press is needed —
    // hover to highlight, click to act.
    const act = document.createElement("span");
    act.className = "scan-tag-act";
    act.setAttribute("aria-hidden", "true");
    act.textContent = "+";

    // Click anywhere on the tag (star/label/marker) to open its actions. The
    // action buttons themselves stopPropagation, so this only fires from the
    // tag body. Works for hover-then-click on desktop and a direct tap on mobile.
    tag.addEventListener("click", (e) => {
      if (e.target.closest && e.target.closest(".scan-action")) return;
      e.preventDefault();
      e.stopPropagation();
      openTagPrompt(tag);
    });

    // Inline action bar (hidden until the + is pressed): one sleek icon per
    // action. No typing — tapping an icon composes the prompt and commits a turn.
    const actions = document.createElement("div");
    actions.className = "scan-tag-actions";
    SCAN_ACTIONS.forEach((a) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "scan-action scan-action-" + a.id;
      b.title = a.title + " the " + obj.label;
      b.setAttribute("aria-label", a.title + " the " + obj.label);
      b.innerHTML = a.icon + '<span class="scan-action-lbl">' + a.label + "</span>";
      b.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        commitScanAction(tag, a);
      });
      actions.appendChild(b);
    });

    tag.appendChild(star);
    tag.appendChild(label);
    tag.appendChild(act);
    tag.appendChild(actions);
    return tag;
  }

  // Reveal a tag's action icons (only one tag's bar open at a time).
  function openTagPrompt(tag) {
    if (state.scanTagActing && state.scanTagActing !== tag) {
      state.scanTagActing.classList.remove("acting");
    }
    state.scanTagActing = tag;
    tag.classList.add("acting");
    tag.classList.remove("near");
    Sound.open();
    // The expanded action bar is wider than the tag — clamp its center so the
    // whole box stays on screen even when the object sits near a viewport edge
    // (otherwise the action icons clip off the side). Use offsetWidth (stable
    // layout width) + a buffer (font-swap can widen the label after measuring,
    // and the acting scale adds a hair). The clamp only moves EDGE tags.
    const fitOnScreen = () => {
      if (state.scanTagActing !== tag) return;
      const m = 12;
      const half = tag.offsetWidth / 2 + 26;
      const W = window.innerWidth;
      const cx = parseFloat(tag.style.left || "0");
      const clamped = Math.max(m + half, Math.min(W - m - half, cx));
      if (Math.abs(clamped - cx) > 0.5) {
        tag.style.left = clamped + "px";
        tag.dataset.sx = String(clamped);
      }
    };
    requestAnimationFrame(fitOnScreen);
    setTimeout(fitOnScreen, 260); // re-clamp after the font/animation settles
  }

  function closeTagPrompt(tag) {
    if (!tag) return;
    tag.classList.remove("acting");
    if (state.scanTagActing === tag) state.scanTagActing = null;
  }

  // Commit a chosen action on a detected object: compose the prompt from the
  // verb + the object's name and resolve a FULL turn (consequence + fresh
  // scene) via the existing pipeline. No typing, no separate action LLM.
  function commitScanAction(tag, action) {
    const obj = tag._obj || { label: "it" };
    if (state.gameOver) { closeTagPrompt(tag); return; }
    if (state.processing) { showRendererToast("The world is still resolving…"); return; }
    const phrase = action.phrase(obj.label);
    Sound.submit();
    closeTagPrompt(tag);
    showRendererToast(action.title + " the " + obj.label + "\u2026");
    // makeChoice clears the tags (the scene is about to change); the ambient
    // overlay stays live and repopulates hotspots once the new scene settles.
    // Tag the turn as a SCAN object interaction so the story backend escalates
    // risk and forces a consequential, plot-moving outcome (not an inert poke).
    const source = action.id === "move" ? "scan_move" : "scan_interact";
    makeChoice(phrase, null, { source });
  }

  // Drop the current tags (e.g. when a turn changes the scene) so stale labels
  // don't hover over a shot they no longer describe; the next scan repopulates.
  function clearScanTags() {
    if (!el.scanTags) return;
    el.scanTags.innerHTML = "";
    state.scanObjects = [];
    state.scanTagActing = null;
    // The scene is changing — drop the stale detection so hotspots never linger
    // from the previous shot; the new scene re-detects once it settles.
    state.scanPrewarm = { objects: [], size: null, ts: 0 };
    setScanHint("");
  }

  function repositionScanTags() {
    if (!state.scanOn || !el.scanTags) return;
    Array.from(el.scanTags.children).forEach((tag) => positionScanTag(tag));
  }

  function closeFreeWill(clear) {
    if (!state.freeWillOpen) return;
    state.freeWillOpen = false;
    state.inputMode = "act";
    el.actionWheel.classList.remove("fw-open", "steer-open");
    if (el.customInput) el.customInput.setAttribute("placeholder", "type your own action...");
    if (clear) el.customInput.value = "";
    if (document.activeElement === el.customInput) el.customInput.blur();
    if (el.actionWheel) el.actionWheel.style.bottom = ""; // drop any keyboard offset
    updateAmbientScan(); // hotspots return once the input closes
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
      if (!(state.autoPlay && !state.processing && !state.gameOver &&
            !state.freeWillOpen && !tapeIsOpen() &&
            el.choices.children.length &&
            state.currentPromptId != null &&
            state.currentPromptId !== state.lastAdvancedPromptId)) return;
      // Realtime: never advance until the NEW scene's video is genuinely on
      // screen. Advancing while it's still re-staging stacks resets faster than
      // the world model can keep up and blacks out the stream. Wait and retry,
      // but honor the deadline so a stalled stream can't freeze the loop.
      if (Renderer.mode === "reactor" && Renderer.reactorAvailable() &&
          !window.ReactorRenderer.isShowing() &&
          Date.now() < (state.autoDeadline || 0)) {
        scheduleAutoAdvance(1200);
        return;
      }
      state.lastAdvancedPromptId = state.currentPromptId;
      moveForward();
    }, delay == null ? AUTOPLAY_FALLBACK_MS : delay);
  }

  function setAutoPlay(on) {
    state.autoPlay = on;
    el.autoplayBtn.classList.toggle("on", on);
    el.autoplayLabel.textContent = on ? "STOP" : "AUTO";
    el.autoplayBtn.title = on ? "Stop auto-play (P)" : "Auto-play — advance on its own (P)";
    if (on) {
      // In realtime, let the current video play a watch window (and never advance
      // before it's on screen); in image mode, advance almost immediately.
      state.autoDeadline = Date.now() + AUTOPLAY_REALTIME_MAX_WAIT_MS;
      const initial = (Renderer.mode === "reactor" && Renderer.reactorAvailable())
        ? AUTOPLAY_REALTIME_WATCH_MS : AUTOPLAY_FRAME_DELAY_MS;
      scheduleAutoAdvance(initial);
    } else {
      clearTimeout(state.autoTimer);  // pause
    }
  }

  function toggleAutoPlay() {
    Sound.toggle();
    setAutoPlay(!state.autoPlay);
  }

  function submitCustomAction(e) {
    e.preventDefault();
    const text = el.customInput.value.trim();
    if (!text || state.gameOver) return;
    // Realtime SHAPE: steer the live video NOW (works even while a turn resolves).
    if (state.inputMode === "steer") {
      el.customInput.value = "";
      const ok = Renderer.steerRealtime(text);
      Sound.submit();
      closeFreeWill(true);
      showRendererToast(ok ? "Live nudge sent" : "Realtime not ready yet");
      return;
    }
    // ACT (full turn) stays gated on the pipeline being idle.
    if (state.processing) return;
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
      if (typeof s.image_enabled === "boolean") state.imagesEnabled = s.image_enabled;
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
      // Log the image-generation prompt (the text we sent Gemini to draw the
      // guide still) whenever it changes — the other half of "what did we send".
      const ip = (s.current_image_prompt || "").trim();
      if (ip && ip !== state._lastImagePrompt) {
        state._lastImagePrompt = ip;
        RtLog.push("img", "image prompt", RtLog.clip(ip, 180));
      }
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
    closeScan(); // no scan overlay behind the tape player
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
    updateAmbientScan(); // hotspots return once the tape deck closes
  }

  function toggleSound() {
    state.soundEnabled = !state.soundEnabled;
    el.btnSnd.classList.toggle("off", !state.soundEnabled);
    const ico = el.btnSnd.querySelector(".rail-ico");
    if (ico) ico.textContent = state.soundEnabled ? "\u266A" : "\u2715"; // ♪ / ✕
    if (state.soundEnabled) { Sound.resume(); Sound.select(); }
    try { SceneAudio.setEnabled(state.soundEnabled); } catch (_) {}
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
    // A scan tag's action bar is open: Esc collapses it (keeps scanning).
    if (state.scanTagActing && e.key === "Escape") {
      closeTagPrompt(state.scanTagActing);
      return;
    }
    // Camera (SNAP) tool owns the keyboard while armed: Esc or H closes it.
    if (state.touchMode) {
      if (e.key === "Escape" || e.key.toLowerCase() === "h") closeTouch();
      return;
    }
    // Case-closed win screen: R starts a new case, Esc dismisses to keep shooting.
    if (el.caseOverlay && !el.caseOverlay.classList.contains("hidden")) {
      if (e.key.toLowerCase() === "r") resetGame();
      else if (e.key === "Escape") hideCaseWin();
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
    } else if (e.key.toLowerCase() === "h") {
      openTouch(); // camera (SNAP) tool — tap to capture evidence
    } else if (e.key.toLowerCase() === "c") {
      capturePhoto(); // journalist photograph — file a specimen to the case file
    } else if (e.key.toLowerCase() === "l") {
      RtLog.toggle(); // show/hide the world-model inspector log
    } else if (e.key.toLowerCase() === "g") {
      Renderer.toggle();
      Sound.toggle();
    } else if (e.key === "ArrowUp" || e.key === " " || e.key === "Spacebar") {
      e.preventDefault();
      moveForward();
    } else if (e.key === "Escape") {
      if (state.scanTagActing) closeTagPrompt(state.scanTagActing); // dismiss an open action bar
      closeFreeWill(true);
    }
  }

  // ------------------------------------------------------------------
  // Bootstrap — every visit / reload starts a fresh run from scratch
  // ------------------------------------------------------------------

  /**
   * On load (including a plain page reload of /standalone or /realtime) we
   * always restart the game from scratch rather than resuming the in-progress
   * session. Visiting the URL is treated as "begin a new run" — the intro and
   * choices (and, in realtime, a fresh world-model stage) come up immediately.
   */
  async function bootstrap() {
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
    if (el.menuToggle) el.menuToggle.addEventListener("click", () => Menu.toggle());
    if (el.btnModel) el.btnModel.addEventListener("click", () => { RtLog.toggle(); });
    if (el.rtModelAdd) el.rtModelAdd.addEventListener("submit", addCustomModel);
    Menu.init();
    Tactile.init();
    el.deathRestart.addEventListener("click", resetGame);
    if (el.caseRestart) el.caseRestart.addEventListener("click", resetGame);
    if (el.caseContinue) el.caseContinue.addEventListener("click", hideCaseWin);
    el.freeWillBtn.addEventListener("click", openFreeWill);
    if (el.realtimeBtn) el.realtimeBtn.addEventListener("click", openTouch);
    if (el.touchLayer) {
      // Pointer events cover mouse (hover to aim) AND touch (drag to aim, tap to
      // shoot) — so the camera works on iOS where mousemove never fires.
      el.touchLayer.addEventListener("pointermove", onTouchMove);
      el.touchLayer.addEventListener("pointerdown", onTouchDown);
      el.touchLayer.addEventListener("pointerup", onTouchUp);
      el.touchLayer.addEventListener("pointercancel", onTouchUp);
      // Right-click on the scene = exit the camera (suppress the browser menu).
      el.touchLayer.addEventListener("contextmenu", onTouchContextMenu);
      // Scroll to zoom (needs passive:false so we can preventDefault the page).
      el.touchLayer.addEventListener("wheel", onTouchWheel, { passive: false });
    }
    // Global cleanup so a pointer lifting over a raised control (e.g. the PHOTO
    // button) can't leave the tap/pinch state stuck.
    window.addEventListener("pointerup", onTouchPointerCleanup);
    window.addEventListener("pointercancel", onTouchPointerCleanup);
    // Ambient hotspots are non-modal: the overlay doesn't capture the pointer
    // (so choices and controls stay live). We watch pointer moves to highlight
    // the nearest interaction possibility and taps to dismiss an open bar.
    window.addEventListener("pointermove", onScanMove);
    window.addEventListener("pointerdown", onScanTap);
    window.addEventListener("resize", repositionScanTags);
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
    try { Investigations.render(); } catch (_) {} // show any persisted case file
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
