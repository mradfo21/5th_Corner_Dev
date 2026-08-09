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
  //
  // The watch window is deliberately LONG for the seed-locked models (LingBot):
  // measured, the live world takes ~14s just to START streaming after a
  // re-anchor, and needs several more seconds before it has visibly evolved.
  // A short window (the old 7s) advanced — i.e. hard-reset the world — before it
  // ever settled, which read as constant "flashing"/black re-staging and meant
  // the player never actually got to SEE the live world. Give it real time to
  // render and breathe before the next turn resets it.
  const AUTOPLAY_REALTIME_WATCH_MS = (typeof window !== "undefined" && window.__AUTOPLAY_WATCH_MS__) || 22000;  // let the live video play this long before advancing
  const AUTOPLAY_REALTIME_MAX_WAIT_MS = 30000; // never wait longer than this for the video to appear
  // DAMAGE / danger system toggle. The vision-driven danger vignette + health
  // drain looks good but currently hurts the player arbitrarily (there's no
  // real combat context yet), so it's DISABLED for now. Flip to true to bring
  // it back for a future COMBAT MODE — all the machinery (DangerSystem, the
  // /api/danger grader, the HEALTH HUD) stays intact and gated behind this one
  // flag. `window.__DAMAGE_ENABLED__` can force it on for testing.
  const DAMAGE_SYSTEM_ENABLED = (typeof window !== "undefined" && window.__DAMAGE_ENABLED__ === true) || false;
  // AMBIENT WORLD DRIFT: how often we ASK the server for a text-only simulation
  // step while the player is idle at a decision point (see the WorldDrift module
  // and engine.world_drift_tick). The server owns the real pacing and refuses
  // early asks with {skipped: "too_soon"}, so this only has to be frequent
  // enough that a drift lands promptly once one is due.
  // How long the boot will wait for /api/reset before giving up and saying so.
  // Generous — reset does real model work — but finite, because the loader
  // cannot draw anything until this resolves.
  const RESET_TIMEOUT_MS = (typeof window !== "undefined" && window.__RESET_TIMEOUT_MS__) || 45000;
  // How long a TALK voice channel gets to actually connect before we give up on
  // it and hold the conversation in text instead.
  const TALK_CONNECT_TIMEOUT_MS = (typeof window !== "undefined" && window.__TALK_CONNECT_TIMEOUT_MS__) || 12000;
  const WORLD_DRIFT_ASK_MS = (typeof window !== "undefined" && window.__WORLD_DRIFT_ASK_MS__) || 6000;
  // Hard release for the in-flight guard, so a request that never settles can't
  // silently stop the world drifting for the rest of the page.
  const WORLD_DRIFT_ASK_TIMEOUT_MS = 15000;
  const REALTIME_MAX_RETRIES = 3; // transient realtime errors retry before falling back to stills
  // Reactor occasionally has no free server for the model ("no available
  // capacity" 429s) — an upstream availability shortage, not a local WebRTC
  // hiccup. Those can take longer to clear than an ICE renegotiation, so we
  // give capacity errors a longer, more patient retry budget, then keep
  // quietly checking in the background so the player doesn't have to
  // remember to flip back to realtime once Reactor frees a slot.
  const REALTIME_CAPACITY_MAX_RETRIES = 5;
  const REALTIME_CAPACITY_RETRY_BASE_MS = 3000;
  const REALTIME_BACKGROUND_RETRY_MS = 25000; // how often to quietly re-check after falling back
  const REALTIME_BACKGROUND_RETRY_MAX_ATTEMPTS = 8; // ~3.5 min of quiet background checks, then give up

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
    storyLog: document.getElementById("story-log"),
    storyLogHide: document.getElementById("story-log-hide"),
    choices: document.getElementById("choices-container"),
    customForm: document.getElementById("custom-form"),
    customInput: document.getElementById("custom-input"),
    freeWillBtn: document.getElementById("free-will-btn"),
    realtimeBtn: document.getElementById("realtime-btn"),
    scanBtn: document.getElementById("scan-btn"),
    scanTutorial: document.getElementById("scan-tutorial"),
    tutDismiss: document.getElementById("tut-dismiss"),
    campBtn: document.getElementById("camp-btn"),
    leaveCampBtn: document.getElementById("leave-camp-btn"),
    movePad: document.getElementById("move-pad"),
    moveNub: document.getElementById("move-nub"),
    verbBar: document.getElementById("verb-bar"),
    moveArrow: document.querySelector("#move-nub .move-arrow"),
    moveReadout: document.querySelector("#move-pad .move-readout .move-head"),
    touchLayer: document.getElementById("touch-layer"),
    touchReticle: document.getElementById("touch-reticle"),
    touchForm: document.getElementById("touch-form"),
    touchInput: document.getElementById("touch-input"),
    scanLayer: document.getElementById("scan-layer"),
    scanTags: document.getElementById("scan-tags"),
    scanHint: document.getElementById("scan-hint"),
    talkOverlay: document.getElementById("talk-overlay"),
    talkScrim: document.getElementById("talk-scrim"),
    talkPanel: document.getElementById("talk-panel"),
    talkName: document.getElementById("talk-name"),
    talkSub: document.getElementById("talk-sub"),
    talkOrb: document.getElementById("talk-orb"),
    talkLog: document.getElementById("talk-log"),
    talkForm: document.getElementById("talk-form"),
    talkInput: document.getElementById("talk-input"),
    talkSend: document.getElementById("talk-send"),
    talkClose: document.getElementById("talk-close"),
    talkFloat: document.getElementById("talk-float"),
    talkFloatWho: document.getElementById("talk-float-who"),
    talkFloatBody: document.getElementById("talk-float-body"),
    talkModeToggle: document.getElementById("talk-mode-toggle"),
    talkVoiceBtn: document.getElementById("talk-voice-btn"),
    talkVoiceName: document.getElementById("talk-voice-name"),
    talkVoiceMenu: document.getElementById("talk-voice-menu"),
    narratorBtn: document.getElementById("narrator-btn"),
    narratorBar: document.getElementById("narrator-bar"),
    narratorSpeaker: document.getElementById("narrator-speaker"),
    narratorLine: document.getElementById("narrator-line"),
    narratorStop: document.getElementById("narrator-stop"),
    agentDebugBtn: document.getElementById("agent-debug-btn"),
    agentLogList: document.getElementById("agent-log-list"),
    agentLogHide: document.getElementById("agent-log-hide"),
    agentLogClear: document.getElementById("agent-log-clear"),
    touchCaptureFrame: document.getElementById("touch-capture-frame"),
    touchHint: document.getElementById("touch-hint"),
    touchZoom: document.getElementById("touch-zoom"),
    touchTargets: document.getElementById("touch-targets"),
    touchLock: document.getElementById("touch-lock"),
    touchDof: document.getElementById("touch-dof"),
    evidenceCard: document.getElementById("evidence-card"),
    captureCinema: document.getElementById("capture-cinema"),
    shotTally: document.getElementById("shot-tally"),
    tallyCount: document.getElementById("tally-count"),
    objectivesHud: document.getElementById("objectives-hud"),
    objHead: document.getElementById("obj-head"),
    objCount: document.getElementById("obj-count"),
    objList: document.getElementById("obj-list"),
    objCollapse: document.getElementById("obj-collapse"),
    btnObjectives: document.getElementById("btn-objectives"),
    photoFiled: document.getElementById("photo-filed"),
    caseOverlay: document.getElementById("case-overlay"),
    caseRankLetter: document.getElementById("case-rank-letter"),
    caseSubjects: document.getElementById("case-subjects"),
    caseObjectives: document.getElementById("case-objectives"),
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
    btnImgModel: document.getElementById("btn-imgmodel"),
    imgModel: document.getElementById("img-model"),
    imgModelList: document.getElementById("img-model-list"),
    imgModelHide: document.getElementById("img-model-hide"),
    btnStory: document.getElementById("btn-story"),
    lobbyCount: document.getElementById("lobby-count"),
    btnEditor: document.getElementById("btn-editor"),
    worldEditor: document.getElementById("world-editor"),
    weClose: document.getElementById("we-close"),
    weTabs: document.getElementById("we-tabs"),
    weFields: document.getElementById("we-fields"),
    weLayerHead: document.getElementById("we-layer-head"),
    weCast: document.getElementById("we-cast"),
    weWide: document.getElementById("we-wide"),
    weFontDown: document.getElementById("we-font-down"),
    weFontUp: document.getElementById("we-font-up"),
    weResize: document.getElementById("we-resize"),
    weWorlds: document.getElementById("we-worlds"),
    weWorldsList: document.getElementById("we-worlds-list"),
    weWorldName: document.getElementById("we-world-name"),
    weWorldSave: document.getElementById("we-world-save"),
    weApply: document.getElementById("we-apply"),
    weRestart: document.getElementById("we-restart"),
    weRevert: document.getElementById("we-revert"),
    weDirty: document.getElementById("we-dirty"),
    weToast: document.getElementById("we-toast"),
    // Pop-out prompt editor
    wem: document.getElementById("we-modal"),
    wemTitle: document.getElementById("wem-title"),
    wemKey: document.getElementById("wem-key"),
    wemChip: document.getElementById("wem-chip"),
    wemDesc: document.getElementById("wem-desc"),
    wemClose: document.getElementById("wem-close"),
    wemFontDown: document.getElementById("wem-font-down"),
    wemFontUp: document.getElementById("wem-font-up"),
    wemWrap: document.getElementById("wem-wrap"),
    wemLines: document.getElementById("wem-lines"),
    wemDiffBtn: document.getElementById("wem-diff"),
    wemCounts: document.getElementById("wem-counts"),
    wemVars: document.getElementById("wem-vars"),
    wemEditor: document.getElementById("wem-editor"),
    wemGutter: document.getElementById("wem-gutter"),
    wemText: document.getElementById("wem-text"),
    wemDiffPane: document.getElementById("wem-diff-pane"),
    wemDiffBody: document.getElementById("wem-diff-body"),
    wemReset: document.getElementById("wem-reset"),
    wemWarn: document.getElementById("wem-warn"),
    wemCancel: document.getElementById("wem-cancel"),
    wemSave: document.getElementById("wem-save"),
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
    rtMusic: document.getElementById("rt-music"),
    rtMusicOpts: document.getElementById("rt-music-opts"),
    deathOverlay: document.getElementById("death-overlay"),
    deathMessage: document.getElementById("death-message"),
    deathRestart: document.getElementById("death-restart"),
    deathContinue: document.getElementById("death-continue"),
    deathContinuePrice: document.getElementById("death-continue-price"),
    deathContinueStatus: document.getElementById("death-continue-status"),
    coinopBlock: document.getElementById("coinop-block"),
    coinopCountdown: document.getElementById("coinop-countdown"),
    coinopCeremony: document.getElementById("coinop-ceremony"),
    continuesHud: document.getElementById("continues-hud"),
    continuesHudCount: document.getElementById("continues-hud-count"),
    // Arcade credit meter + pause overlay (see CoinOp.Meter / CoinOp.Pause
    // in the CoinOp module below). Always resolved even when gating is
    // off — we just leave them .hidden. Cheap to hold refs.
    creditMeter: document.getElementById("credit-meter"),
    creditMeterCount: document.getElementById("credit-meter-count"),
    creditMeterSpent: document.getElementById("credit-meter-spent"),
    pauseOverlay: document.getElementById("coinop-pause-overlay"),
    pauseContinue: document.getElementById("pause-continue"),
    pauseContinuePrice: document.getElementById("pause-continue-price"),
    pauseContinueStatus: document.getElementById("pause-continue-status"),
    pauseCountdown: document.getElementById("pause-countdown"),
    pauseCeremony: document.getElementById("pause-ceremony"),
    pauseRestart: document.getElementById("pause-restart"),
    pausePackBlurb: document.getElementById("pause-pack-blurb"),
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
    audioUnlocked: false,       // true after the first user gesture (autoplay ok)
    renderedIds: new Set(), // guard against rendering the same feed item twice
    lastStatus: {},
    freeWillOpen: false,
    inputMode: "act",           // custom input intent: "act" (full turn) | "steer" (realtime nudge)
    touchMode: null,            // TOUCH tool state: null | "aim" (reticle tracks cursor) | "prompt" (spot locked, field open)
    touchPoint: null,           // {x, y} viewport coords of the reticle / locked spot
    photoZoom: 1,               // optical zoom magnification while the camera is armed (1..PHOTO_ZOOM_MAX)
    panFocus: null,             // {x, y} scene point (untransformed screen coords) shown at frame center — driven by mouselook / touch-drag while zoomed
    photoPointers: new Map(),   // active pointers on the camera layer, for pinch-to-zoom
    pinchBase: null,            // {dist, zoom} anchor captured when a two-finger pinch begins
    pinchActive: false,         // true once 2 fingers are down (suppresses the shot until release)
    touchGesture: null,         // {id, x0, y0, t0, moved} — tracks a single-finger press so a TAP shoots but a DRAG only re-frames (never auto-fires on release)
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
    scanOn: false,              // hotspot overlay live (object tags over the scene)
    scanBusy: false,            // a detection request is in flight
    scanObjects: [],            // last detected objects (normalized coords + labels)
    scanTagActing: null,        // tag element with its inline action bar open
    scanMoveTimer: null,        // debounced re-detect after the cursor settles (realtime)
    scanSrcSize: null,          // {w,h} of the last scanned source (video or still), for cover-mapping tags
    scanPrewarm: { objects: [], size: null, ts: 0 }, // last detection cached (for tag positioning / re-scan diffing)
    scanFadeTimer: null,        // TTL timer: fade the hotspots out a few seconds after a manual scan
    scanFadeOutTimer: null,     // the fade animation -> teardown timer (after tags start leaving)
    moving: false,              // camera is being driven (joystick / WASD) — OCR hotspots are hidden + detection paused while moving; they regenerate once you stop
    moveSettleTimer: null,      // after movement stops, wait for the view to settle before re-detecting hotspots
    moveFadeTimer: null,        // MOVE TO: delayed fade-to-black kickoff so the live world stops drifting during the trip
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
    objDirectiveTurn: null,     // turn number the objectives LEAD was last refreshed for (one refresh/turn)
    objDirectiveBusy: false,    // a /api/objectives directive fetch is in flight
    inCamp: false,              // playable campsite level is live (full HUD; not a Moment)
    campEntering: false,        // /api/camp/enter in flight
    campLeaving: false,         // LEAVE CAMP → new-level choose in flight
  };

  // ------------------------------------------------------------------
  // Device — one source of truth for "is this a phone or a desktop?".
  //
  // The game is full-bleed and the generated media (4:3 stills, 16:9 realtime
  // video) is authored for a wide desktop canvas. On a desktop we WANT that
  // cinematic full-bleed crop; on a portrait phone the same `cover` fit throws
  // away most of the frame. Rather than sprinkle `window.innerWidth` checks
  // everywhere, we resolve the device once and stamp the <html> element with
  // classes the CSS keys off of:
  //   .is-mobile / .is-desktop   — coarse pointer + phone-sized viewport (or UA)
  //   .is-touch  / .is-pointer   — whether touch is the primary input
  //   .is-portrait / .is-landscape
  // JS can also read Device.isMobile() etc. Everything stays reactive to
  // resize / orientation changes and to a phone being rotated mid-run.
  // ------------------------------------------------------------------
  const Device = (function () {
    const root = document.documentElement;
    let listeners = [];
    const mm = (q) => (window.matchMedia ? window.matchMedia(q) : { matches: false, addEventListener: () => {}, addListener: () => {} });
    const state = { mobile: false, touch: false, portrait: false };

    function detectTouch() {
      try {
        if (mm("(pointer: coarse)").matches) return true;
        if (navigator.maxTouchPoints > 0) return true;
        if ("ontouchstart" in window) return true;
      } catch (_) {}
      return false;
    }

    function detectMobile(touch) {
      // A phone/tablet is: a touch-primary device whose *shortest* side is
      // small enough that a desktop-cinematic crop would hurt, OR a device
      // that reports a mobile user-agent. Desktops with touchscreens (large
      // viewport, fine pointer available) stay in "desktop" mode.
      let uaMobile = false;
      try {
        const ua = navigator.userAgent || "";
        uaMobile = /Android|iPhone|iPad|iPod|Windows Phone|webOS|BlackBerry|Mobile/i.test(ua);
        // Modern iPadOS reports as desktop Safari; catch it via touch + Mac.
        if (!uaMobile && /Macintosh/.test(ua) && navigator.maxTouchPoints > 1) uaMobile = true;
      } catch (_) {}
      const shortSide = Math.min(window.innerWidth || 0, window.innerHeight || 0);
      const finePointer = (function () { try { return mm("(pointer: fine)").matches; } catch (_) { return false; } })();
      // Small viewport + touch-primary (no fine pointer) => phone-class.
      const smallTouch = touch && !finePointer && shortSide > 0 && shortSide <= 820;
      return uaMobile || smallTouch;
    }

    function apply() {
      const touch = detectTouch();
      const mobile = detectMobile(touch);
      const portrait = (window.innerHeight || 0) >= (window.innerWidth || 0);
      const changed = mobile !== state.mobile || touch !== state.touch || portrait !== state.portrait;
      state.mobile = mobile; state.touch = touch; state.portrait = portrait;
      root.classList.toggle("is-mobile", mobile);
      root.classList.toggle("is-desktop", !mobile);
      root.classList.toggle("is-touch", touch);
      root.classList.toggle("is-pointer", !touch);
      root.classList.toggle("is-portrait", portrait);
      root.classList.toggle("is-landscape", !portrait);
      if (changed) listeners.forEach((fn) => { try { fn(state); } catch (_) {} });
      return state;
    }

    let _raf = 0;
    function onChange() {
      // Coalesce bursts of resize events (mobile URL-bar show/hide fires many).
      if (_raf) cancelAnimationFrame(_raf);
      _raf = requestAnimationFrame(apply);
    }

    function init() {
      apply();
      window.addEventListener("resize", onChange, { passive: true });
      window.addEventListener("orientationchange", onChange, { passive: true });
      try {
        // React the instant the primary pointer type changes (e.g. a tablet
        // docking a mouse) without waiting for a resize.
        mm("(pointer: coarse)").addEventListener("change", onChange);
      } catch (_) {
        try { mm("(pointer: coarse)").addListener(onChange); } catch (_) {}
      }
      return state;
    }

    return {
      init,
      refresh: apply,
      onChange(fn) { if (typeof fn === "function") listeners.push(fn); },
      isMobile() { return state.mobile; },
      isDesktop() { return !state.mobile; },
      isTouch() { return state.touch; },
      isPortrait() { return state.portrait; },
    };
  })();
  // Expose for debugging / other scripts (e.g. the reactor renderer).
  try { window.__DEVICE__ = Device; } catch (_) {}

  // The scene media (still background / realtime video) is displayed `cover` on
  // desktops and landscape phones, but `contain` on PORTRAIT phones (see the
  // "MOBILE MEDIA FIT" CSS). The SCAN hotspots and PHOTO markers map model-space
  // (0..1) coordinates onto the *displayed* media rect, so they must use the
  // exact same fit the CSS uses — otherwise every tag/marker drifts on a phone.
  function isContainFit() {
    return Device.isMobile() && Device.isPortrait();
  }
  // Scale that maps a source (sw x sh) into the viewport (W x H) under the
  // current fit: max() = cover (fill + crop), min() = contain (fit + letterbox).
  function mediaFitScale(W, H, sw, sh) {
    return isContainFit() ? Math.min(W / sw, H / sh) : Math.max(W / sw, H / sh);
  }

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

    // ── Heartbeat loop ────────────────────────────────────────────────
    // A muffled "lub-dub" repeated at BPM. Uses a shared low-pass so the
    // heartbeat sits UNDER the mix (subwoofer territory) and never masks
    // the hit / warning ticks. State kept in closure so start() while
    // already running just re-tempos, and stop() is idempotent.
    let hbTimer = null;
    let hbBpm = 80;
    function hbTick() {
      if (!state.soundEnabled) return;
      const c = ensure();
      if (!c) return;
      // "Lub" — deeper, heavier
      const t0 = c.currentTime;
      const lub = c.createOscillator(); const lubGain = c.createGain();
      lub.type = "sine"; lub.frequency.setValueAtTime(70, t0);
      lub.frequency.exponentialRampToValueAtTime(45, t0 + 0.12);
      lubGain.gain.setValueAtTime(0.0001, t0);
      lubGain.gain.exponentialRampToValueAtTime(0.14, t0 + 0.015);
      lubGain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.14);
      const lubLp = c.createBiquadFilter();
      lubLp.type = "lowpass"; lubLp.frequency.value = 160;
      lub.connect(lubLp); lubLp.connect(lubGain); lubGain.connect(c.destination);
      lub.start(t0); lub.stop(t0 + 0.16);
      // "Dub" — 90ms later, slightly brighter accent
      const dt = 0.09;
      const dub = c.createOscillator(); const dubGain = c.createGain();
      dub.type = "sine"; dub.frequency.setValueAtTime(90, t0 + dt);
      dub.frequency.exponentialRampToValueAtTime(60, t0 + dt + 0.10);
      dubGain.gain.setValueAtTime(0.0001, t0 + dt);
      dubGain.gain.exponentialRampToValueAtTime(0.10, t0 + dt + 0.012);
      dubGain.gain.exponentialRampToValueAtTime(0.0001, t0 + dt + 0.12);
      const dubLp = c.createBiquadFilter();
      dubLp.type = "lowpass"; dubLp.frequency.value = 200;
      dub.connect(dubLp); dubLp.connect(dubGain); dubGain.connect(c.destination);
      dub.start(t0 + dt); dub.stop(t0 + dt + 0.14);
    }
    function hbSchedule() {
      if (hbTimer) clearTimeout(hbTimer);
      const period = Math.max(300, 60000 / Math.max(30, Math.min(220, hbBpm)));
      hbTimer = setTimeout(() => { hbTick(); hbSchedule(); }, period);
    }
    function hbStart(bpm) {
      hbBpm = bpm;
      if (hbTimer) return; // already running — tempo updated
      // Fire the first beat immediately so state entry feels responsive.
      hbTick();
      hbSchedule();
    }
    function hbSetBpm(bpm) {
      hbBpm = bpm;
      // Live-tempo change is just letting the next scheduled tick pick up
      // the new period — no restart needed.
    }
    function hbStop() {
      if (hbTimer) { clearTimeout(hbTimer); hbTimer = null; }
    }

    // ── Tinnitus tone ────────────────────────────────────────────────
    // Sustained high sine (~3.7kHz) with slight FM wobble. Long fade-in +
    // fade-out so it eases into your ears rather than beeping.
    let tinNodes = null; // { osc, lfo, lfoGain, gain, ctx }
    function tinStart() {
      if (tinNodes) return;
      if (!state.soundEnabled) return;
      const c = ensure();
      if (!c) return;
      const t0 = c.currentTime;
      const osc = c.createOscillator();
      const gain = c.createGain();
      const lfo = c.createOscillator();
      const lfoGain = c.createGain();
      osc.type = "sine"; osc.frequency.setValueAtTime(3720, t0);
      lfo.type = "sine"; lfo.frequency.setValueAtTime(6, t0);
      lfoGain.gain.setValueAtTime(24, t0); // ±24 Hz wobble around 3720
      lfo.connect(lfoGain); lfoGain.connect(osc.frequency);
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.exponentialRampToValueAtTime(0.024, t0 + 0.7);
      osc.connect(gain); gain.connect(c.destination);
      osc.start(t0); lfo.start(t0);
      tinNodes = { osc, lfo, lfoGain, gain, ctx: c };
    }
    function tinStop() {
      if (!tinNodes) return;
      const { osc, lfo, gain, ctx } = tinNodes;
      const t0 = ctx.currentTime;
      try {
        gain.gain.cancelScheduledValues(t0);
        gain.gain.setValueAtTime(gain.gain.value, t0);
        gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.5);
        osc.stop(t0 + 0.55);
        lfo.stop(t0 + 0.55);
      } catch (_) {}
      tinNodes = null;
    }

    return {
      resume() { ensure(); },
      // Shared, gesture-unlocked AudioContext so the ambient scene score
      // (SceneAudio) rides the same mute + first-gesture gating as the SFX.
      context() { return ensure(); },
      glitch() { noise(0.24, 0.055); },                        // VCR transition static burst
      text() { tone(430, 0.09, "sine", 0.045); },              // narrative / world text lands
      pickup() { tone([600, 900], 0.16, "triangle", 0.06); },  // item pickup
      // ---- Coin-op ----
      // coin: two bright metallic pings 40ms apart — the "kaCHING" of a quarter
      //       hitting a slot's ramp then dropping into the hopper. Tighter and
      //       higher than pickup so it never blurs into an item cue.
      // coinReady: an ascending three-tone chime — "CONTINUE READY" arcade
      //       fanfare played on return-from-checkout, right before the death
      //       overlay dissolves and the run resumes.
      coin() {
        tone([1760, 1320], 0.06, "square", 0.06);
        tone([1180, 780], 0.14, "triangle", 0.05, 0.04);
      },
      coinReady() {
        tone(660, 0.09, "triangle", 0.05);
        tone(990, 0.09, "triangle", 0.05, 0.07);
        tone(1320, 0.14, "triangle", 0.055, 0.14);
      },
      choices() { tone(680, 0.07, "triangle", 0.05); tone(920, 0.09, "triangle", 0.045, 0.07); }, // choices ready
      select() { tone(520, 0.05, "square", 0.05); tone(790, 0.10, "square", 0.05, 0.055); },       // confirm choice
      status() { tone(320, 0.05, "sine", 0.03); },             // HUD tick
      death() { tone([180, 60], 0.7, "sawtooth", 0.09); },     // game over
      error() { tone([200, 120], 0.18, "sawtooth", 0.05); },
      scene() { tone(180, 0.05, "sine", 0.05); tone([520, 380], 0.14, "sine", 0.04, 0.03); }, // new scene image streams in (shutter/whir)
      start() { tone([160, 520], 0.28, "sawtooth", 0.05); tone(880, 0.12, "triangle", 0.04, 0.18); }, // new tape / game start
      escalate() { tone([300, 620], 0.35, "sawtooth", 0.06); tone([620, 900], 0.3, "square", 0.03, 0.12); }, // phase escalates — tension rises
      // ---- Danger vignette / damage / health ----
      // WARNING is a taut two-note alert (heads up, back off); HURTING is a
      // lower, uglier throb (you're bleeding); HIT is a short percussive tick
      // that lands on each damage tick, so the steady drain feels like
      // discrete impacts, not a numeric ticker. SAFE-CHIME is the "you're
      // clear" descending mini-arpeggio when the red drops away. REGEN-DONE
      // is the bright ascending sparkle when HP returns to full — the audio
      // that closes the recover loop.
      warning() { tone([880, 660], 0.14, "square", 0.055); tone(1240, 0.08, "square", 0.04, 0.12); },
      hurting() { tone([320, 180], 0.30, "sawtooth", 0.08); tone([540, 300], 0.22, "sawtooth", 0.055, 0.10); },
      hit()     { tone([520, 200], 0.10, "sawtooth", 0.08); tone([1100, 700], 0.06, "square", 0.04); },
      safeChime()    { tone([740, 560], 0.16, "sine", 0.045); tone([560, 420], 0.20, "sine", 0.04, 0.10); },
      regenComplete(){ tone([560, 880], 0.12, "triangle", 0.05); tone([880, 1320], 0.14, "triangle", 0.045, 0.08); tone(1760, 0.10, "triangle", 0.035, 0.16); },
      // ---- Heartbeat loop (HURTING) ----
      // A muffled two-thump per beat, tempo driven by heartbeatSetBpm(). The
      // "lub-dub" is a low sine with a slightly higher accent 90ms later,
      // filtered dark so it sits under the mix. Playing state persists
      // across ensure() calls so mute/unmute doesn't stack loops.
      heartbeatStart(bpm) { hbStart(bpm || 80); },
      heartbeatSetBpm(bpm) { hbSetBpm(bpm || 80); },
      heartbeatStop() { hbStop(); },
      // ---- Tinnitus (critical HP) ----
      // A sustained ~3.7kHz sine with a tiny FM wobble, faded in over ~700ms
      // and out over ~500ms. Sits under everything else at low gain so it
      // reads as "your ears are ringing", not "there's a beep".
      tinnitusStart() { tinStart(); },
      tinnitusStop() { tinStop(); },
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
      // ---- TALK: opening a channel to a subject, and a reply landing ----
      talkOpen() { tone([260, 620], 0.22, "sine", 0.045); tone(880, 0.14, "triangle", 0.035, 0.14); noise(0.05, 0.02); }, // channel opens — a warm carrier tone
      talkLine() { tone(560, 0.05, "triangle", 0.04); tone(760, 0.10, "sine", 0.03, 0.05); }, // a spoken reply arrives
      talkClose() { tone([620, 200], 0.2, "sine", 0.04); }, // channel closes
      // ---- Conversation Moment (cinematic takeover) ----
      // Rising swell into the letterboxed dialogue screen; resolving chord on
      // exit; soft photographic flash when the portrait lands; UI ticks for
      // dialogue choices; a quiet chime for in-Moment notifications.
      convoEnter() {
        tone([180, 420], 0.28, "sine", 0.05);
        tone([420, 780], 0.22, "triangle", 0.04, 0.12);
        tone(1180, 0.12, "sine", 0.03, 0.28);
        noise(0.06, 0.018);
        // A quiet sustained "channel open" drone bridges the gap between the
        // entrance stinger and whatever lands next (portrait / voice connect /
        // conversation music) — so the wait never reads as dead air.
        tone(300, 1.7, "sine", 0.011, 0.16);
      },
      convoExit() {
        tone([780, 360], 0.22, "triangle", 0.04);
        tone([360, 180], 0.26, "sine", 0.035, 0.1);
      },
      portraitReveal() {
        noise(0.04, 0.03);
        tone([880, 1320], 0.1, "sine", 0.04);
        tone(660, 0.08, "triangle", 0.03, 0.06);
      },
      choiceHover() { tone(1500, 0.018, "sine", 0.014); },
      choiceSelect() { tone(720, 0.05, "triangle", 0.045); tone(1080, 0.09, "sine", 0.035, 0.04); },
      notify() { tone(990, 0.06, "triangle", 0.04); tone(1320, 0.1, "sine", 0.03, 0.05); },
      grab() { tone(900, 0.03, "square", 0.045); tone([700, 340], 0.10, "triangle", 0.04, 0.02); }, // TOUCH specimen captured
      shutter() { tone(1500, 0.015, "square", 0.055); noise(0.05, 0.035); tone(760, 0.03, "square", 0.05, 0.03); }, // camera shutter
      // Raising / lowering the camera: a little servo whir that racks up and
      // locks ready, then powers back down — so the tool feels mechanical.
      cameraOn() { tone([170, 540], 0.16, "sawtooth", 0.035); tone([720, 1280], 0.08, "square", 0.03, 0.02); noise(0.05, 0.022); tone(1560, 0.012, "square", 0.05, 0.15); },
      cameraOff() { tone([620, 170], 0.18, "sawtooth", 0.035); tone(300, 0.04, "square", 0.03, 0.02); },
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
  // Expose Sound so moments.js (and e2e) can fire conversation cues without
  // reaching into this IIFE. Harmless if Moments isn't loaded.
  try { window.Sound = Sound; } catch (_) {}

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
    const FADE = 1.4;           // crossfade seconds between scene scores
    let duckFactor = 1;         // 0..1 multiplier applied on top of musicVol (Conversation Moments)
    let preConvoUrl = null;     // scene bed to restore after a conversation score
    let preConvoKey = null;

    // Music bed volume — an ambient bed that should sit UNDER the UI SFX, not
    // compete with it. It's now adjustable live from the debug panel (WORLD
    // MODEL / L) and persisted per browser. We expose a short list of preset
    // "options" (Off…Max) rather than a fiddly slider, and default lower than
    // before (the old 0.26 read as too loud for a background bed).
    const VOL_KEY = "music_vol";
    const VOL_PRESETS = [
      { id: "off",  label: "Off",  value: 0.0  },
      { id: "low",  label: "Low",  value: 0.06 },
      { id: "med",  label: "Med",  value: 0.12 },
      { id: "high", label: "High", value: 0.20 },
      { id: "max",  label: "Max",  value: 0.30 },
    ];
    const DEFAULT_VOL = 0.12;
    function loadVol() {
      try {
        const raw = localStorage.getItem(VOL_KEY);
        if (raw == null) return DEFAULT_VOL;
        const v = parseFloat(raw);
        return (isFinite(v) && v >= 0 && v <= 1) ? v : DEFAULT_VOL;
      } catch (_) { return DEFAULT_VOL; }
    }
    let musicVol = loadVol();   // current bed volume (0..1), the live target

    // Push the current volume onto whatever is playing right now (clip gain
    // and/or streamed PCM gain) so debug-panel changes take effect instantly,
    // without waiting for the next scene to re-score. Honors the global mute
    // and the Conversation Moment duck factor.
    function applyLiveVolume() {
      const c = ctx();
      const target = state.soundEnabled ? (musicVol * Math.max(0, Math.min(1, duckFactor))) : 0;
      if (gain && c) {
        try {
          const t = c.currentTime;
          gain.gain.cancelScheduledValues(t);
          gain.gain.setValueAtTime(gain.gain.value, t);
          gain.gain.linearRampToValueAtTime(target, t + 0.25);
        } catch (_) {}
      }
      if (streamGain) {
        try { streamGain.gain.value = target; } catch (_) {}
      }
    }

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
      g.gain.linearRampToValueAtTime(musicVol * Math.max(0, Math.min(1, duckFactor)), t + FADE);
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
      streamGain.gain.value = state.soundEnabled ? musicVol : 0;
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
      // ── Music volume (debug-panel controlled) ──
      // The preset "options" shown in the debug panel (Off…Max).
      volumePresets() { return VOL_PRESETS.map((p) => ({ id: p.id, label: p.label, value: p.value })); },
      // Current bed volume (0..1).
      getVolume() { return musicVol; },
      // Set + persist the bed volume, applying it live to whatever's playing.
      setVolume(v) {
        v = Number(v);
        if (!isFinite(v)) return;
        musicVol = Math.max(0, Math.min(1, v));
        try { localStorage.setItem(VOL_KEY, String(musicVol)); } catch (_) {}
        applyLiveVolume();
      },
      // Duck (not silence) the ambient bed under a Conversation Moment.
      // factor 0.35 ≈ present but under dialogue; 1.0 = full bed.
      duck(factor) {
        duckFactor = (factor == null) ? 0.35 : Math.max(0, Math.min(1, Number(factor) || 0));
        applyLiveVolume();
      },
      unduck() {
        duckFactor = 1;
        applyLiveVolume();
      },
      // Score an intimate conversation bed for the subject/scene, remembering
      // the exploration bed so exit can restore it. Best-effort / fire-and-forget.
      async scoreConversation(prompt) {
        if (!state.soundEnabled) return;
        const key = (prompt == null ? "" : String(prompt)).trim().slice(0, 240);
        if (!key) return;
        // Remember the exploration bed once; nested calls shouldn't overwrite.
        if (preConvoUrl == null) {
          preConvoUrl = currentUrl;
          preConvoKey = requestedKey;
        }
        this.duck(0.35);
        let res;
        try {
          res = await postJSON("/api/scene_audio", {
            prompt: key,
            session: (typeof SESSION_ID !== "undefined" && SESSION_ID) ? SESSION_ID : "default",
            mode: "conversation",
          });
        } catch (_) { return; }
        if (!res || !res.audio_url) return;
        if (res.audio_url === currentUrl) return;
        // Don't stamp requestedKey with the convo key — that would block the
        // next exploration score() of the same scene after we restore.
        crossfadeTo(res.audio_url);
      },
      // Restore the pre-conversation exploration bed (or just unduck).
      async endConversation() {
        this.unduck();
        const restoreUrl = preConvoUrl;
        const restoreKey = preConvoKey;
        preConvoUrl = null;
        preConvoKey = null;
        if (restoreUrl && restoreUrl !== currentUrl) {
          try { await crossfadeTo(restoreUrl); } catch (_) {}
          if (restoreKey) requestedKey = restoreKey;
        }
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
    // Chrome blocks navigator.vibrate() (and logs an [Intervention] warning) until
    // the page has received a genuine user gesture. Track that so we never call
    // vibrate prematurely — e.g. haptics fired during boot or programmatic events.
    let userEngaged = false;
    try {
      const markEngaged = () => {
        userEngaged = true;
        ["pointerdown", "touchstart", "keydown", "mousedown", "click"].forEach((evt) => {
          try { window.removeEventListener(evt, markEngaged, true); } catch (_) {}
        });
      };
      ["pointerdown", "touchstart", "keydown", "mousedown", "click"].forEach((evt) => {
        window.addEventListener(evt, markEngaged, { capture: true, passive: true });
      });
    } catch (_) {}
    function buzz(pattern) {
      if (!enabled || !userEngaged) return;
      try { if (navigator && typeof navigator.vibrate === "function") navigator.vibrate(pattern); } catch (_) {}
    }
    return {
      tap: () => buzz(8),          // light press
      select: () => buzz(14),      // committing an action / choice
      strong: () => buzz([16, 24, 16]), // big moment (death / new game)
      soft: () => buzz(5),         // subtle nudge
      // ---- Danger vignette / damage ----
      warn: () => buzz([10, 60, 10]),  // entering the red — a heads-up shake
      tick: () => buzz(18),            // each damage tick — a single firm punch
      // ---- Camera feel ----
      camera: () => buzz([10, 24, 18]), // raising the camera — a two-stage clunk
      shutter: () => buzz(24),          // the shot fires — one firm snap
      lock: () => buzz(7),              // a subject snaps into frame
      miss: () => buzz([14, 34, 14]),   // empty / out-of-focus frame
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
    try { updateScanButton(); } catch (_) {} // turn's over — SCAN is live again
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
    // There is now something to photograph, which is what reveals the camera
    // (see updateRendererButton) — in stills that's this call, not a renderer
    // status change, so nothing else would refresh it.
    try { updateRendererButton(); } catch (_) {}
    // The world is genuinely on screen now — surface the objectives tracker
    // (and derive this run's opening lead). Held back until here so it never
    // floats over the black boot / "rendering" void.
    try { Objectives.reveal(); } catch (_) {}
    try { Objectives.syncCase(); } catch (_) {}
    // Surface the CASE FILE goal (EVIDENCE + census + stakes) the moment the
    // first scene is readable — not only when the camera is first raised — so
    // the player knows what the game is FOR from turn one (the "directionless"
    // complaint: the win condition was effectively hidden until you happened to
    // pick up the camera).
    try { Evidence.reveal(); } catch (_) {}
    // First-timers get the tutorial card (which already states the goal); after
    // they've dismissed it once, returning runs get the lighter goal toast.
    try { if (!showScanTutorialOnce()) showIntroGoalOnce(); } catch (_) {}
    try { refreshDirective(true); } catch (_) {}
    try { updateScanButton(); } catch (_) {} // a scene is readable — SCAN is live
  }

  // A single, once-per-run nudge that states the whole game in one line the
  // first time a scene is up: you are here to DOCUMENT, and documenting N
  // distinct subjects closes the case. Uses the existing renderer toast so it's
  // non-intrusive and self-dismisses.
  function showIntroGoalOnce() {
    if (state._introGoalShown) return;
    state._introGoalShown = true;
    const target = (window.Evidence && Evidence.target && Evidence.target()) || 8;
    try {
      showRendererToast("Raise your camera. " + target +
        " good photographs closes the case.", 5000);
    } catch (_) {}
  }

  // ── First-run tutorial: the ONE thing a new player must learn — TAP TO SCAN.
  // Shown once per browser (localStorage-gated) the first time a scene is
  // readable. Dismissed by the button, a tap anywhere on it, or Escape.
  // Returns true if it was shown this call (so the caller can suppress the
  // redundant goal toast on the very first run).
  const TUTORIAL_SEEN_KEY = "scan_tutorial_seen_v1";
  function showScanTutorialOnce() {
    if (!el.scanTutorial) return false;
    if (state._tutorialShown) return false; // already up this session
    let seen = false;
    try { seen = localStorage.getItem(TUTORIAL_SEEN_KEY) === "1"; } catch (_) {}
    if (seen) return false;
    state._tutorialShown = true;
    const tgt = el.scanTutorial.querySelector("#tut-target");
    const target = (window.Evidence && Evidence.target && Evidence.target()) || 8;
    if (tgt) tgt.textContent = String(target);
    el.scanTutorial.classList.remove("hidden");
    // rAF so the .show transition actually plays from the hidden state.
    requestAnimationFrame(() => el.scanTutorial.classList.add("show"));
    try { Sound.talkOpen && Sound.talkOpen(); } catch (_) {}
    return true;
  }

  function dismissScanTutorial() {
    if (!el.scanTutorial || el.scanTutorial.classList.contains("hidden")) return;
    state._tutorialShown = false;
    try { localStorage.setItem(TUTORIAL_SEEN_KEY, "1"); } catch (_) {}
    el.scanTutorial.classList.remove("show");
    const hide = () => el.scanTutorial.classList.add("hidden");
    if (prefersReducedMotion()) hide(); else setTimeout(hide, 300);
    try { Sound.press && Sound.press(); } catch (_) {}
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
        try { updateScanButton(); } catch (_) {} // dim SCAN while the turn runs
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

  // Multi-user session framework: every request tells the server which
  // instance of the experience it's talking to. The id is read once from
  // ?session=<id> (or ?session_id=<id>) on the URL and stashed here so
  // every /api/* call automatically threads it via the X-Session-Id header
  // (see engine.set_active_session / api._session_scoped). If no id is on
  // the URL, we fall back to 'default' — legacy /standalone bookmarks and
  // embed links still work the way they always did.
  const SESSION_ID = (function () {
    try {
      const u = new URL(window.location.href);
      const raw = (u.searchParams.get("session") || u.searchParams.get("session_id") || "").trim();
      if (raw && /^[A-Za-z0-9_\-]{1,40}$/.test(raw)) {
        try { localStorage.setItem("somewhere.lobby.last_session", raw); } catch (_) {}
        return raw;
      }
    } catch (_) {}
    return "default";
  })();

  // Expose for debugging + for any tooling that wants to know which instance
  // this page is bound to. Read-only; the framework does not support hot-swap.
  try { window.__SOMEWHERE_SESSION__ = SESSION_ID; } catch (_) {}

  /**
   * Reject after `ms` if `promise` hasn't settled. The underlying request is
   * left to finish or die on its own — the point is that the UI stops waiting,
   * not that the socket is torn down.
   */
  function withTimeout(promise, ms, label) {
    let timer = null;
    const bail = new Promise((_resolve, reject) => {
      timer = setTimeout(
        () => reject(new Error(`${label || "request"} (timed out after ${Math.round(ms / 1000)}s)`)),
        ms,
      );
    });
    return Promise.race([promise, bail]).finally(() => clearTimeout(timer));
  }

  async function postJSON(url, body) {
    const payload = Object.assign({}, body || {});
    // Also embed it in the JSON body for endpoints (like /api/choose) that
    // read the id off the body — belt-and-suspenders with the header.
    if (payload.session_id === undefined) payload.session_id = SESSION_ID;
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Session-Id": SESSION_ID,
      },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      // Attach the response body + status onto the thrown error so callers
      // that care about a structured failure (specifically /api/choose
      // returning 402 with `needs_coin: true` when the credit meter is
      // empty) can inspect it without needing a parallel fetch. Best-
      // effort JSON parse — a non-JSON error body (e.g. a proxy's HTML
      // 502) just leaves body=null, no throw.
      let errBody = null;
      try { errBody = await resp.json(); } catch (_) {}
      const err = new Error(`${url} -> HTTP ${resp.status}`);
      err.status = resp.status;
      err.body = errBody;
      throw err;
    }
    return resp.json();
  }

  async function getJSON(url) {
    // Append session_id as a query param so it also survives to endpoints
    // that don't read the header. Do not clobber an existing session_id.
    let target = url;
    try {
      const abs = new URL(url, window.location.origin);
      if (!abs.searchParams.has("session_id")) {
        abs.searchParams.set("session_id", SESSION_ID);
      }
      target = abs.pathname + (abs.search ? abs.search : "") + (abs.hash || "");
    } catch (_) {
      target = url + (url.includes("?") ? "&" : "?") + "session_id=" + encodeURIComponent(SESSION_ID);
    }
    const resp = await fetch(target, {
      headers: { "X-Session-Id": SESSION_ID },
    });
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
    // A new scene is on screen: any hotspots from the previous shot are now
    // stale, so drop them. Scanning is manual (behind the SCAN button) — we
    // don't auto-detect the new scene; the player taps SCAN for a fresh read.
    closeScan();
    updateScanButton();
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

  // Punchier, photographic capture flash: a fast white pop with a shutter-blink
  // dip, distinct from the slow scene-change flash used on turns.
  function flashShutter() {
    if (!el.sceneFlash) return;
    el.sceneFlash.classList.remove("flash", "shutter");
    void el.sceneFlash.offsetWidth;
    el.sceneFlash.classList.add("shutter");
  }

  // Handheld "camera" jolts — a brief, one-shot shake of the whole view (a class
  // on <body>, so it composes on top of the scene's optical-zoom transform and a
  // telephoto framing never snaps to center mid-jolt). `photoShake` is the bigger
  // bump when the camera is raised; `photoKick` is a smaller recoil the instant a
  // shot fires. Both respect reduced motion and only ever fire as momentary
  // one-shots — never during a sustained aim/drag — so aiming stays steady.
  const _scenePunchTimers = {};
  function scenePunch(cls, ms) {
    if (prefersReducedMotion()) return;
    document.body.classList.remove(cls);
    void document.body.offsetWidth; // reflow so it re-triggers back-to-back
    document.body.classList.add(cls);
    clearTimeout(_scenePunchTimers[cls]);
    _scenePunchTimers[cls] = setTimeout(() => document.body.classList.remove(cls), ms);
  }
  function photoShake() { scenePunch("photo-shake", 470); }
  function photoKick() { scenePunch("photo-kick", 240); }

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
  // Moments controller (moments.js) reuses the same VCR burst for enter/exit.
  try { window.__MOMENT_GLITCH__ = glitchTransition; } catch (_) {}

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
  // Camera — the authored cast-sheet camera, on the client.
  //
  // The browser is not a passive display in realtime mode: it builds the world
  // (create_world takes its own `perspective`) and re-steers it on every
  // movement, nudge and idle drift between turns. All of that text used to be
  // hardcoded first person — "the view shifts as you…", "Smooth continuous
  // first-person motion" — so selecting a third-person camera in the editor
  // changed every still frame and every server prompt, then lost the argument
  // to the client on the very next step the player took.
  //
  // The server compiles the phrasing once (game_identity.live_camera_contract)
  // and hands it over through /api/camera; everything here is a lookup with a
  // first-person fallback identical to the strings this replaced, so a failed
  // fetch degrades to the shipped behavior instead of an empty prompt.
  // ------------------------------------------------------------------
  const Camera = {
    contract: null,

    async load() {
      try {
        const data = await getJSON("/api/camera");
        const payload = (data && (data.data || data)) || {};
        if (payload.camera) this.apply(payload.camera);
      } catch (err) {
        console.warn("[standalone] camera fetch failed; assuming first person", err);
      }
      return this.contract;
    },

    // Adopt a camera (boot fetch, or an editor save pushing the new one in).
    // Rebuilds the live world when the perspective it was BUILT with changes —
    // Happy Oyster fixes that at creation, so without the rebuild the switch
    // only takes effect at the next hard cut.
    apply(camera) {
      if (!camera) return false;
      this.contract = camera;
      let changed = false;
      try {
        if (Renderer.reactorAvailable() && window.ReactorRenderer.setAuthoredCamera) {
          changed = window.ReactorRenderer.setAuthoredCamera(camera);
        }
      } catch (_) {}
      try { HappyOysterOptions.update(); } catch (_) {}
      if (changed) {
        try { window.ReactorRenderer.rebuildWorld(); } catch (_) {}
      }
      return changed;
    },

    showsCharacter() { return !!(this.contract && this.contract.shows_character); },
    // "the camera follows as Wren Alvarez" / "the view shifts as you"
    motionClause() {
      return (this.contract && this.contract.motion_clause) || "the view shifts as you";
    },
    movementClause() {
      return (this.contract && this.contract.movement_clause) ||
        "Smooth continuous first-person motion, the environment flowing past.";
    },
    sceneFloor() {
      return (this.contract && this.contract.scene_floor) ||
        "First-person cinematic view of the current scene.";
    },
    vantage() {
      return (this.contract && this.contract.vantage) ||
        "first-person eye-level walking vantage";
    },
  };
  try { window.__Camera = Camera; } catch (_) {}

  // ------------------------------------------------------------------
  // Renderer facade — swap between the classic still-image renderer and the
  // Reactor realtime world-model renderer without the rest of the game caring.
  // "image"   -> Gemini still per turn (default; existing behavior).
  // "reactor" -> steer Reactor's Happy Oyster navigable world with the SAME
  //              per-turn scene prompt the engine used to build the still (passed
  //              as the world's first frame). The still is painted underneath as
  //              a graceful fallback if Reactor drops.
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
            //
            // Reactor's OWN infra sometimes has no free server for the model at
            // all ("Failed to create session: 429 no available capacity") — an
            // upstream availability shortage, not a local hiccup. That needs a
            // longer, more patient retry budget than an ICE renegotiation, and
            // deserves an honest message instead of a generic "reconnecting".
            const lastErr = (window.ReactorRenderer.getLastError && window.ReactorRenderer.getLastError()) || null;
            const isCapacity = !!(lastErr && lastErr.capacity);
            const maxRetries = isCapacity ? REALTIME_CAPACITY_MAX_RETRIES : REALTIME_MAX_RETRIES;
            const retryBaseMs = isCapacity ? REALTIME_CAPACITY_RETRY_BASE_MS : 1600;
            Renderer._rtRetries = (Renderer._rtRetries || 0) + 1;
            if (Renderer._rtRetries <= maxRetries) {
              console.warn("[standalone] realtime error — retry", Renderer._rtRetries, isCapacity ? "(capacity)" : "");
              showRendererToast(isCapacity ? "Reactor is at capacity \u2014 retrying\u2026" : "Realtime reconnecting\u2026");
              try { window.ReactorRenderer.disable(); } catch (_) {}
              clearTimeout(Renderer._rtRetryTimer);
              Renderer._rtRetryTimer = setTimeout(() => {
                if (Renderer.mode !== "reactor" || !Renderer.reactorAvailable()) return;
                window.ReactorRenderer.enable().then((ok) => {
                  if (ok && Renderer.lastScene) window.ReactorRenderer.applyScene(Renderer.lastScene);
                });
              }, retryBaseMs * Renderer._rtRetries);
              updateRendererButton();
              return;
            }
            console.warn("[standalone] realtime unavailable after retries — falling back to stills",
                         (lastErr && lastErr.reason) || "unknown");
            // Every non-capacity failure used to read as the same shrug. The
            // classifier names the cause now, and each cause has a different
            // fix, so say which one happened.
            Renderer.fallbackToStills(
              (lastErr && lastErr.hint) ||
              (isCapacity
                ? "Reactor is full right now \u2014 showing stills (retrying quietly)"
                : "Realtime unavailable \u2014 showing stills"));
            // Capacity is Reactor's problem to resolve, not the player's — keep
            // quietly checking in the background so realtime comes back on its
            // own the moment a server frees up, instead of leaving the player
            // stuck on stills until they remember to flip the toggle.
            if (isCapacity) Renderer._armBackgroundResume();
            else Renderer._cancelBackgroundResume();
          } else if (s === "live" && Renderer.mode === "reactor") {
            Renderer._rtRetries = 0; // healthy again — reset the retry budget
            Renderer._cancelBackgroundResume();
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
                  d.command === "set_shot" || d.command === "scene_cut" || d.command === "create_world")
                RtLog.push("prompt", "\u2192 " + d.command, RtLog.clip(d.prompt, 160) + (d.hasImage ? " [first frame]" : ""));
              else if (d.command === "set_image") RtLog.push("prompt", "\u2192 set_image", d.hasImage ? "[seed image]" : "");
              else if (d.command === "start_travel") RtLog.push("status", "\u25B8 start travel");
              else if (d.command === "move" || d.command === "look" ||
                       d.command === "interact" || d.command === "stop") {
                const nice = { move: "move", look: "look", interact: "interact", stop: "stop" }[d.command];
                RtLog.push("prompt", "\u25B8 " + nice, d.value != null ? String(d.value) : "");
              }
              else if (d.command === "set_move_longitudinal" || d.command === "set_move_lateral" ||
                       d.command === "set_look_horizontal" || d.command === "set_look_vertical" ||
                       d.command === "set_rotation_speed_deg") {
                const nice = {
                  set_move_longitudinal: "move", set_move_lateral: "strafe",
                  set_look_horizontal: "turn", set_look_vertical: "look",
                  set_rotation_speed_deg: "turn speed",
                }[d.command];
                RtLog.push("prompt", "\u25B8 " + nice, d.value != null ? String(d.value) : "");
              }
              else RtLog.push(null, "\u2192 " + d.command);
              break;
            case "world_state": RtLog.push("status", "\u25C6 world \u00B7 " + (d.phase || "?")); break;
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
            case "video_black": RtLog.push("error", "\u26A0 stream went black (scene refused) \u2014 showing still"); break;
            case "video_recovered": RtLog.push("ok", "\u25C9 stream recovered \u2014 video restored"); break;
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
              // The live video for this turn is genuinely on screen now — this,
              // not the async scene_image beat, is when the turn's guide-image
              // step resolves and the interaction layer (veil/FORWARD/ACT/SCAN)
              // is released. Keeps interaction options gated until the world is
              // actually playing (see the scene_image handler's realtime note).
              Ceremony.imageLoaded();
              // A fresh live scene is on screen — drop the previous location's
              // hotspots so they can't linger. Scanning is manual now (behind
              // the SCAN button), so we just re-enable the button; the player
              // taps SCAN to read this scene.
              closeScan();
              updateScanButton();
              try { VerbBar.update(); } catch (_) {} // world's interaction verbs
            }
            // The world model started streaming solid black (its safety refused
            // the scene). The renderer has already hidden the black video to
            // reveal the still floor; make sure a real still is actually painted
            // there (the floor can be stale/empty), glitch over the swap, and
            // surface hotspots on the still so the game stays fully playable.
            if (name === "video_black") {
              if (Renderer.lastScene && Renderer.lastScene.imageUrl) {
                setScene(Renderer.lastScene.imageUrl);
              } else {
                glitchTransition();
                markSceneVisible();
              }
              // A still fallback is now the on-screen frame for this turn, so
              // resolve the ceremony over the still instead of leaving the
              // interaction layer gated until the fallback timer fires.
              Ceremony.imageLoaded();
              closeScan();
              updateScanButton();
              showRendererToast("Scene refused \u2014 showing still");
            }
            // The live world can NEVER start for this scene: the model needs a
            // seed still and none is coming (generation was content-filtered or
            // failed). Nothing is on screen and nothing ever will be, so stop
            // waiting on black and hand the scene to the still renderer. Without
            // this the player just sits looking at black — the whole "it's
            // completely broken" report.
            if (name === "needs_seed_image") {
              const shown = Renderer.lastScene && Renderer.lastScene.imageUrl;
              if (shown) setScene(Renderer.lastScene.imageUrl);
              else glitchTransition();
              markSceneVisible();
              Ceremony.imageLoaded();
              closeScan();
              updateScanButton();
              // Drop to stills for real: realtime has nothing to render, and
              // leaving the toggle on "LIVE" over a dead stream reads as broken.
              Renderer.fallbackToStills(
                shown ? "No live scene \u2014 showing stills"
                      : "Scene unavailable \u2014 showing stills",
              );
            }
            // Real frames returned — the live video is back on screen.
            if (name === "video_recovered") {
              glitchTransition();
              markSceneVisible();
              // Live video is back on screen — resolve any pending turn so the
              // interaction layer is released against the recovered stream.
              Ceremony.imageLoaded();
              closeScan();
              updateScanButton();
              showRendererToast("Realtime video \u2014 recovered");
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
                create_world: "\u25B8 Building world",
                start_travel: "\u25B8 Entering world",
                start: "\u25B8 Starting stream",
                reset: "\u25B8 Re-staging world",
                pause: "\u25B8 Pausing stream",
                resume: "\u25B8 Resuming stream",
              }[d.command];
              if (cmdNote) {
                Ceremony.note(cmdNote);
                if (d.command === "set_prompt" || d.command === "schedule_prompt" ||
                    d.command === "set_shot" || d.command === "scene_cut" ||
                    d.command === "set_image" || d.command === "create_world")
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
              // A rendered chunk means frames are arriving, which is the same
              // proof of life as `video_showing`. Some models emit chunks and
              // never that event, and the ceremony then sat on "World Updating"
              // over a world that was already running.
              Ceremony.reach("world_respond");
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
            case "video_black":
              Ceremony.note("\u26A0 Scene refused \u00B7 stream went black \u2014 showing still", { tick: false });
              return;
            case "video_recovered":
              Ceremony.note("\u25C9 Stream recovered", { tick: false });
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
        // Happy Oyster reported this world's interaction verbs (travel_state) —
        // rebuild the verb bar with the world's real, live actions.
        window.ReactorRenderer.onInteractVerbs = () => { try { VerbBar.onVerbs(); } catch (_) {} };
        // The renderer caches world ids per scene and reopens them with
        // attach_world on revisit; just surface it in the log for visibility.
        window.ReactorRenderer.onWorldId = (id) => { RtLog.push("dim", "\u25C7 world id \u00B7 saved"); };
        // The log + switcher stay reachable whenever realtime is available so you
        // can flip world models even from still mode.
        document.body.classList.add("reactor-available");
      }
      RtLog.init();
      buildModelSwitcher();
      buildMusicVolume();
      // In realtime mode, connect eagerly so the GPU session is warming while
      // the intro scene generates — the video then starts as soon as the first
      // scene prompt arrives. (Falls back to stills if it can't connect.)
      if (this.mode === "reactor" && this.reactorAvailable()) {
        window.ReactorRenderer.enable().then((ok) => {
          buildModelSwitcher(); // config may refine the model list/labels
          if (ok && Renderer.lastScene) window.ReactorRenderer.applyScene(Renderer.lastScene);
          // Reactor is up — kick off the vision-driven danger loop so the
          // player has a live threat readout the moment the video renders.
          try { DangerSystem.start(); } catch (_) {}
        });
      }
      updateRendererButton();
    },

    reactorAvailable() {
      return !!window.ReactorRenderer;
    },

    // Pause the underlay world for a cinematic Moment WITHOUT tearing it down.
    // Image mode: just dim the still layers (CSS via body.moment-active).
    // Reactor mode: stop held movement/look, mute the live video element, and
    // ask the model to pause if it supports it — the Happy Oyster / Reactor
    // session stays connected so exit is instant (no rebuild).
    _underlayPaused: false,
    _underlayWasMuted: null,
    pauseUnderlay() {
      if (this._underlayPaused) return;
      this._underlayPaused = true;
      document.body.classList.add("moment-underlay-paused");
      // Stop player locomotion so the world doesn't keep drifting under the
      // letterbox while they talk.
      try { if (typeof onMovementStop === "function") onMovementStop(); } catch (_) {}
      try {
        if (window.ReactorRenderer && typeof window.ReactorRenderer.stopMotion === "function") {
          window.ReactorRenderer.stopMotion();
        }
      } catch (_) {}
      try {
        if (window.ReactorRenderer && typeof window.ReactorRenderer.setHeldVerb === "function") {
          window.ReactorRenderer.setHeldVerb(null);
        }
      } catch (_) {}
      try {
        const v = document.getElementById("reactor-video");
        if (v) {
          this._underlayWasMuted = !!v.muted;
          v.muted = true;
        }
      } catch (_) {}
      try {
        if (window.ReactorRenderer && typeof window.ReactorRenderer.pause === "function") {
          window.ReactorRenderer.pause();
        }
      } catch (_) {}
    },

    resumeUnderlay() {
      if (!this._underlayPaused) return;
      this._underlayPaused = false;
      document.body.classList.remove("moment-underlay-paused");
      try {
        const v = document.getElementById("reactor-video");
        if (v && this._underlayWasMuted != null) {
          v.muted = this._underlayWasMuted;
        }
      } catch (_) {}
      this._underlayWasMuted = null;
      try {
        if (window.ReactorRenderer && typeof window.ReactorRenderer.resume === "function") {
          window.ReactorRenderer.resume();
        }
      } catch (_) {}
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

    // Drop out of realtime and show the still renderer instead. Used whenever
    // the live world cannot present frames and won't recover on its own
    // (connect failed, or the model needs a seed still that never arrived).
    // Tears the realtime layers down and paints the last known still, so a
    // fallback is never just a black screen. The STORED preference is left
    // alone — this reflects reality, it isn't the player changing their mind.
    fallbackToStills(message) {
      this.mode = "image";
      if (message) showRendererToast(message);
      clearScanTags();   // re-map hotspots onto the still that replaces the video
      hideGuideThumbnail();
      try { window.ReactorRenderer.disable(); } catch (_) {}
      if (this.lastScene && this.lastScene.imageUrl) setScene(this.lastScene.imageUrl);
      // Danger grading reads the live frame; there isn't one anymore.
      try { DangerSystem.stop(); } catch (_) {}
      updateRendererButton();
    },

    // Quietly keep retrying realtime in the background after an automatic
    // capacity fallback, so the player doesn't have to remember to flip back
    // to "LIVE" once Reactor frees a server. Only fires while the player
    // hasn't manually touched the renderer toggle since the fallback (see
    // _cancelBackgroundResume, called from any explicit setMode) and gives up
    // silently after REALTIME_BACKGROUND_RETRY_MAX_ATTEMPTS so a persistent
    // outage doesn't retry forever.
    _armBackgroundResume() {
      this._cancelBackgroundResume();
      this._bgResumeAttempts = 0;
      const tick = () => {
        this._bgResumeTimer = null;
        // The player took over (manual toggle/model pick) or is already back
        // on realtime — nothing left for the background loop to do.
        if (this.mode !== "image" || !this.reactorAvailable()) return;
        this._bgResumeAttempts++;
        window.ReactorRenderer.enable().then((ok) => {
          if (ok) {
            if (this.mode !== "image") return; // player switched away while we connected
            this.mode = "reactor";
            if (this.lastScene) window.ReactorRenderer.applyScene(this.lastScene);
            try { DangerSystem.start(); } catch (_) {}
            showRendererToast("Realtime video is back \u2014 capacity freed up");
            updateRendererButton();
            return;
          }
          // Still no capacity (Renderer.mode is "image" here, so the onStatus
          // "error" handler above is a no-op for this attempt — it only acts
          // while mode === "reactor"). Reschedule ourselves until the attempt
          // budget runs out, then stop trying quietly.
          if (this._bgResumeAttempts < REALTIME_BACKGROUND_RETRY_MAX_ATTEMPTS) {
            this._bgResumeTimer = setTimeout(tick, REALTIME_BACKGROUND_RETRY_MS);
          }
        });
      };
      this._bgResumeTimer = setTimeout(tick, REALTIME_BACKGROUND_RETRY_MS);
    },

    _cancelBackgroundResume() {
      if (this._bgResumeTimer) { clearTimeout(this._bgResumeTimer); this._bgResumeTimer = null; }
      this._bgResumeAttempts = 0;
    },

    setMode(mode) {
      // A manual toggle/model pick always wins over the quiet background
      // capacity retry — the player is taking explicit control.
      this._cancelBackgroundResume();
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
          // Reactor came up — spin up the danger vignette + health loop so
          // the vision-driven threat readout tracks the live video.
          try { DangerSystem.start(); } catch (_) {}
        });
      } else if (this.reactorAvailable()) {
        showRendererToast("Still images");
        try { window.ReactorRenderer.disable(); } catch (_) {}
        hideGuideThumbnail();
        hideCaptureThumbnail();
        // Danger is a REALTIME-only mechanic (still images have no live
        // frame to grade), so tear the loop down and clear the vignette
        // whenever we drop back to stills.
        try { DangerSystem.stop(); } catch (_) {}
      }
      // Hotspots work in BOTH renderers, but a scan reads one specific source
      // (video vs still cover the viewport differently) — switching renderers
      // invalidates them, so drop the overlay. The player re-scans the new
      // source with the SCAN button.
      state.scanSrcSize = null;
      closeScan();
      updateScanButton();
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
        // Don't re-ground choices from a frame while the camera is travelling —
        // it's mid-motion and unrepresentative. Retry once movement stops.
        if (state.moving) {
          if (tries++ < 20) state.observeTimer = setTimeout(attempt, 1200);
          return;
        }
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
    // Resolve the "scene bible" (style + physical scene) a live re-steer builds
    // on. Prefers the stable base, then the last scene prompt the feed carried,
    // then the prompt the reactor stream is ACTUALLY running (which native
    // movement/exploration mode sets without a feed scene_image), and finally a
    // neutral first-person floor — so a re-steer can ALWAYS fire while the world
    // model is live, instead of silently failing back to a full turn.
    steerBase() {
      const fromReactor = (this.reactorAvailable() && window.ReactorRenderer.getPrompt)
        ? window.ReactorRenderer.getPrompt() : null;
      return this.lastBase
        || (this.lastScene && this.lastScene.prompt)
        || (typeof state !== "undefined" && state.lastScenePrompt)
        || fromReactor
        || Camera.sceneFloor();
    },

    steerRealtime(text, where) {
      if (this.mode !== "reactor" || !this.reactorAvailable()) return false;
      const a = (text || "").trim().replace(/\.+$/, "");
      if (!a) return false;
      // Build on the stable scene bible (style + physical scene, no action beat)
      // so the nudge blends with the current shot instead of resetting it.
      const base = this.steerBase();
      const act = a.charAt(0).toLowerCase() + a.slice(1);
      // Anchor the nudge to the spot the player touched, when one was given, so
      // the change lands where they aimed instead of across the whole frame.
      const anchor = (where && where.phrase) ? where.phrase : null;
      // Two framings for the injected beat:
      //   • "event" (INTERACT): a world EVENT overlay for prompt-steered models
      //     (LingBot/Helios). Happy Oyster instead takes a real interact({action})
      //     verb (see commitScanAction), so this path is the fallback. Firing an
      //     event via a prompt IS just a set_prompt with the event described — but
      //     it only renders if it follows the model's prompt rules, which the old
      //     beat broke and is why INTERACT looked dead:
      //       - Room-budget rule: "a two-word mention tucked into an otherwise
      //         dense prompt usually gets ignored." The event must be a FULL
      //         sentence-anchor with concrete physical detail (the caller's
      //         `text` already is), not a short tag drowned by the scene base.
      //       - No camera-motion verbs ("Motion:"), which fight the live look
      //         axes and get ignored; no meta ("clearly visible on screen") and
      //         no frame coordinates — the model renders physical description of
      //         the world, not instructions about itself.
      //     So we append the concrete event sentence as-is, anchored in physical
      //     space (see objectAnchorPhrase), and let it carry its own weight.
      //   • default (freeform SHAPE tool): a camera / POV nudge, phrased for
      //     the authored camera — the same clause the server uses for the
      //     turn's own action beat (game_identity.motion_clause).
      let beat;
      if (where && where.kind === "event") {
        const sentence = anchor ? anchor + ", " + act : act;
        beat = sentence.charAt(0).toUpperCase() + sentence.slice(1) + ".";
      } else {
        beat = anchor
          ? "Motion: " + anchor + ", " + act + "."
          : "Motion: " + Camera.motionClause() + " " + act + ".";
      }
      window.ReactorRenderer.applyScene({
        prompt: base + " " + beat,
        imageUrl: null,           // same scene — just re-steer, no image swap
        hardTransition: false,
      });
      if (Ceremony.isActive()) Ceremony.note("\u25B8 Live nudge injected");
      return true;
    },

    // AMBIENT DRIFT: apply a `world_drift` beat (see WorldDrift) as a prompt-only
    // hot-swap on the running stream. Deliberately NOT routed through
    // applyScene: a drift carries no new guide image, and on a seed-locked model
    // an apply that looks like a new scene re-stages the whole world. We keep
    // lastBase/lastScene coherent so a later steer, model swap, or reconnect
    // builds on the DRIFTED world rather than snapping back to the last choice.
    applyDrift(meta) {
      const prompt = (meta && meta.prompt) || null;
      if (!prompt) return false;
      if (this.mode !== "reactor" || !this.reactorAvailable()) return false;
      const RR = window.ReactorRenderer;
      if (RR.supportsLiveSteer && !RR.supportsLiveSteer()) return false;
      if (meta.base) this.lastBase = meta.base;
      const prev = this.lastScene || {};
      this.lastScene = {
        prompt: prompt,
        imageUrl: prev.imageUrl || null,
        hardTransition: false,
      };
      RR.applyScene({ prompt: prompt, imageUrl: null, hardTransition: false });
      return true;
    },

    // MOVEMENT (joystick / WASD): steer the live video as a first-person CAMERA.
    // Like steerRealtime, this is a prompt hot-swap on the running stream — no
    // new guide image, no backend turn — but the beat is a camera-motion clause
    // (`camera` describes where the viewpoint travels) so the world reads as a
    // place you can walk around in. `beat` is the movement clause built by the
    // Movement module; returns true if it steered, false if realtime isn't ready.
    steerMovement(beat) {
      if (this.mode !== "reactor" || !this.reactorAvailable()) return false;
      const b = (beat || "").trim();
      if (!b) return false;
      // Build on the stable scene bible so the move blends with the current shot
      // (same anchor steerRealtime uses) instead of regenerating the scene.
      const base = this.steerBase();
      window.ReactorRenderer.applyScene({
        prompt: base + " " + b,
        imageUrl: null,           // same scene — pure camera re-steer, no image swap
        hardTransition: false,
      });
      return true;
    },
  };
  // Expose for debugging + e2e (so tests can seed a scene base for movement).
  // Moments.js also reads window.Renderer.pauseUnderlay / resumeUnderlay.
  try { window.__Renderer = Renderer; window.Renderer = Renderer; } catch (_) {}

  // ═══════════════════════════════════════════════════════════════════════
  // WorldDrift — keep the world EVOLVING while the player deliberates.
  //
  // The turn loop is the only thing that ever spoke to the world model, so
  // between choices the live stream ran forever on the prompt from the last
  // action: the video kept moving but the world was frozen at that moment, which
  // is what makes a long look around feel stale. This asks the server for a
  // cheap text-only simulation step (POST /api/world_tick); the server decides
  // whether one is due, and when it grants one it appends a `world_drift` feed
  // item that the normal feed poll picks up and applies as a prompt-only
  // re-steer (see Renderer.applyDrift).
  //
  // Everything expensive is gated here, on the client, because the client is the
  // only place that knows whether anyone is actually WATCHING:
  //   • realtime renderer live and genuinely showing frames (a still has nothing
  //     to re-steer, and steering black wastes the call)
  //   • the model can take a live prompt edit — a Happy Oyster adventure world is
  //     fixed once built, so drifting it would rebuild the world on a timer
  //   • idle: no turn in flight, not dead, not mid-camera-move, no full-screen
  //     instrument or conversation claiming the view
  //   • tab visible — a backgrounded tab must not keep billing
  const WorldDrift = {
    timer: null,
    inFlight: false,

    start() {
      if (this.timer) return;
      this.timer = setInterval(() => this.tick(), WORLD_DRIFT_ASK_MS);
    },

    stop() {
      if (this.timer) clearInterval(this.timer);
      this.timer = null;
    },

    idle() {
      if (this.inFlight) return false;
      if (typeof document !== "undefined" && document.hidden) return false;
      if (!window.Renderer || Renderer.mode !== "reactor" || !Renderer.reactorAvailable()) return false;
      const RR = window.ReactorRenderer;
      if (!RR) return false;
      if (RR.supportsLiveSteer && !RR.supportsLiveSteer()) return false;
      if (!RR.isShowing || !RR.isShowing()) return false;
      if (state.processing || state.awaitingResolution) return false;
      return ambientContextAllowed();
    },

    // Ambient polish must never be able to break the game: any throw in the
    // gate (a renderer swapped out mid-check, an instrument module not loaded
    // yet) is swallowed, and a failed ask just waits for the next interval.
    tick() {
      try {
        if (!this.idle()) return;
      } catch (_) {
        return;
      }
      this.inFlight = true;
      // Always release the in-flight guard, even if the request never settles.
      // A fetch that hangs forever would otherwise pin inFlight=true and stop
      // the world drifting for the rest of the page with no error anywhere.
      const release = () => { this.inFlight = false; };
      const guard = setTimeout(release, WORLD_DRIFT_ASK_TIMEOUT_MS);
      postJSON("/api/world_tick", {})
        .catch((err) => console.warn("[standalone] world tick failed:", err))
        .then(() => { clearTimeout(guard); release(); });
    },
  };
  try { window.__WorldDrift = WorldDrift; } catch (_) {}

  // ═══════════════════════════════════════════════════════════════════════
  // DangerSystem — realtime vision-driven danger vignette + health.
  //
  // Loop: every ~1s while the realtime renderer is showing frames, sample the
  // on-screen video (captureFrame) and POST /api/danger to grade it. Vision
  // returns one of three ordinal levels — 0 safe / 1 threatened / 2 attacking
  // — which drives a tiny three-state machine on the client:
  //
  //   SAFE     → nothing hostile in view. Vignette off. Health regens.
  //   WARNING  → level ≥ 1. Red vignette pulses at 900ms. Health HOLDS.
  //              After WARNING_GRACE_MS of continuous WARNING → HURTING.
  //   HURTING  → damage tick every second while in state. Vignette throbs
  //              at 500ms (faster + hotter). Only level=0 held for
  //              SAFE_CONFIRM_MS drops back to SAFE.
  //
  // Level 2 fast-tracks straight to HURTING with no grace — an "attacking"
  // reading is defined as danger already committed, so the visual and the
  // mechanic both snap on the same tick. Predictable: warning ALWAYS precedes
  // damage unless the picture itself already shows the hit landing.
  //
  // Every tuning knob lives in one CONFIG object at the top so playtest can
  // live-edit via window.__DANGER_CONFIG__. Nothing else in the file touches
  // these numbers.
  // ═══════════════════════════════════════════════════════════════════════
  const DangerSystem = (function () {
    const CONFIG = Object.assign({
      SAMPLE_MS: 1000,             // how often we grade the frame
      SAMPLE_MIN_MS: 900,          // minimum gap between calls (in-flight guard)
      REQUEST_TIMEOUT_MS: 6000,    // client-side abort if the server hangs
      BACKOFF_MS: 2500,            // after 2 consecutive failures
      WARNING_GRACE_MS: 3000,      // WARNING → HURTING after this much red
      SAFE_CONFIRM_MS: 2000,       // clean vision needed to fully de-escalate
      DAMAGE_TICK_MS: 1000,        // one hit per tick while HURTING
      DAMAGE_PER_TICK: 8,          // → 12.5s from full health to death
      REGEN_PER_SEC: 5,            // → 20s from empty back to full
      HEALTH_MAX: 100,
      HEALTH_CRITICAL: 25,         // bar starts pulsing critical below this
      BOOST_LUMA_DELTA: 0.30,      // brightness spike → extra out-of-band call
      CAPTURE_WIDTH: 384,          // downscale for cheap POST payloads
      DEATH_MSG: "Your body gave out.\nThe last thing you saw was the light.",
    }, (typeof window !== "undefined" && window.__DANGER_CONFIG__) || {});

    let el = {
      vignette: null, inner: null, hitFlash: null,
      spatter: null, chroma: null,
      arrows: {}, // {top, right, bottom, left}
      health: null, healthFill: null, healthNum: null, healthShimmer: null,
    };
    let running = false;
    let sampleTimer = null;
    let inFlight = false;
    let lastPostMs = 0;
    let consecutiveErrors = 0;
    let lastLumaSample = null;   // most recent captureFrame luma (0..1)
    let lastRegenerating = false; // did the previous rAF frame regen?
    let heartbeatOn = false;      // is the looping heartbeat currently playing?
    let tinnitusOn = false;       // is the tinnitus tone currently playing?

    // Danger state machine. `mode` is one of "safe" | "warning" | "hurting".
    // `stateSince` is the ms timestamp we entered the current mode; used to
    // gate WARNING → HURTING (must hold for WARNING_GRACE_MS).
    // `cleanSince` is the ms timestamp of the first level=0 reading in the
    // current de-escalation streak; used to gate WARNING/HURTING → SAFE
    // (must stay clean for SAFE_CONFIRM_MS).
    let mode = "safe";
    let stateSince = 0;
    let cleanSince = 0;
    let lastLevel = 0;
    let lastReason = "";
    let lastDirection = null;
    let lastThreatCx = null; // 0..1, from last reading (for spatter placement)
    let lastThreatCy = null;

    // Health lives here as the single source of truth; ticks on rAF via a
    // simple wall-clock delta so drain feels steady even if the sample loop
    // stumbles. Never mutated from outside — call takeDamage() / regen()
    // instead.
    let health = CONFIG.HEALTH_MAX;
    let healthTickTs = 0;
    let nextDamageTick = 0;
    let hpLoopId = null;
    let dead = false;

    function log(...args) {
      if (typeof window !== "undefined" && window.__DEBUG_DANGER__) {
        console.log("[danger]", ...args);
      }
    }

    function now() { return performance.now(); }

    function bindDom() {
      if (el.vignette) return;
      el.vignette     = document.getElementById("danger-vignette");
      el.inner        = el.vignette && el.vignette.querySelector(".danger-vignette-inner");
      el.hitFlash     = document.getElementById("danger-hit-flash");
      el.spatter      = document.getElementById("danger-blood-spatter");
      el.chroma       = document.getElementById("danger-chroma");
      el.arrows = {
        top:    el.vignette && el.vignette.querySelector(".danger-arrow-top"),
        right:  el.vignette && el.vignette.querySelector(".danger-arrow-right"),
        bottom: el.vignette && el.vignette.querySelector(".danger-arrow-bottom"),
        left:   el.vignette && el.vignette.querySelector(".danger-arrow-left"),
      };
      el.health        = document.getElementById("danger-health");
      el.healthFill    = document.getElementById("danger-health-fill");
      el.healthNum     = document.getElementById("danger-health-num");
      el.healthShimmer = document.getElementById("danger-health-shimmer");
    }

    function applyDirectionCss(direction) {
      if (!el.vignette) return;
      // Move the radial-gradient origin toward the edge the threat is on so
      // the vignette pushes IN from that side. Center-biased for "center"
      // and no-direction so it stays symmetric when we don't know.
      let cx = "50%", cy = "50%";
      switch (direction) {
        case "left":   cx = "12%"; cy = "50%"; break;
        case "right":  cx = "88%"; cy = "50%"; break;
        case "top":    cx = "50%"; cy = "12%"; break;
        case "bottom": cx = "50%"; cy = "88%"; break;
        // "center" | null → symmetric
      }
      el.vignette.style.setProperty("--danger-cx", cx);
      el.vignette.style.setProperty("--danger-cy", cy);
    }

    function setMode(next, reason) {
      if (next === mode) return;
      const prev = mode;
      mode = next;
      stateSince = now();
      cleanSince = 0;
      // Entering HURTING → deliver the first damage tick on the very next
      // rAF frame so the punch (flash + audio hit) lands ON the same beat
      // the pulse speeds up. Otherwise the "safe" branch of the health
      // loop had been continuously pushing nextDamageTick forward, so the
      // first drain would land a full second after entering HURTING.
      if (next === "hurting") nextDamageTick = now();
      log("mode", prev, "→", next, reason || "");
      applyVisualForMode();
      // Audio + haptic beats on state entry — WARNING is a soft ping (heads-
      // up), HURTING is a heavier tone (you're in it now). Death has its own
      // sound via the existing gameover flow.
      try {
        if (next === "warning" && prev === "safe") {
          Sound.status(); // brief HUD tick
          if (Sound.warning) Sound.warning();
          try { Haptics.select(); } catch (_) {}
        } else if (next === "hurting") {
          if (Sound.hurting) Sound.hurting();
          else Sound.error();
          try { Haptics.warn && Haptics.warn(); } catch (_) {}
        }
        // De-escalation: the moment red drops off (any → safe) we play a
        // short chime so the player audibly hears "you're clear". Missing
        // this beat was the biggest gap — silence for exiting danger felt
        // like an unresolved chord.
        if (next === "safe" && prev !== "safe") {
          if (Sound.safeChime) Sound.safeChime();
        }
        // Heartbeat management — running iff HURTING and alive. Started
        // here on state entry; stopped here on state exit. Tempo is set
        // per-frame from the health loop below so it accelerates as HP falls.
        if (next === "hurting" && !heartbeatOn) {
          if (Sound.heartbeatStart) Sound.heartbeatStart(computeHeartbeatBpm());
          heartbeatOn = true;
        } else if (next !== "hurting" && heartbeatOn) {
          if (Sound.heartbeatStop) Sound.heartbeatStop();
          heartbeatOn = false;
        }
      } catch (_) {}
    }

    // Heartbeat tempo maps HP → BPM. At full HP (100) beats at 78 (resting
    // "you're stressed"); at 0 HP crescendos to 160 (adrenal spike). Called
    // both on state entry and on every rAF frame while HURTING so tempo
    // tracks live health changes.
    function computeHeartbeatBpm() {
      const hp = Math.max(0, Math.min(CONFIG.HEALTH_MAX, health));
      const frac = 1 - (hp / CONFIG.HEALTH_MAX); // 0 → 1 as HP falls
      return Math.round(78 + frac * 82);
    }

    function applyVisualForMode() {
      if (!el.vignette) return;
      const on = mode !== "safe";
      el.vignette.classList.toggle("on", on);
      el.vignette.classList.toggle("hurting", mode === "hurting");
      // Body-level classes drive the chromatic-aberration wash + any
      // future full-screen effects that need to overlay on top of tools.
      try {
        document.body.classList.toggle("danger-hurting", mode === "hurting");
        document.body.classList.toggle("danger-warning", mode === "warning");
      } catch (_) {}
    }

    function applyCriticalBodyClass() {
      // Separate from applyVisualForMode because critical is a HEALTH threshold,
      // not a mode. Drives the intensified chroma flicker at low HP.
      try {
        document.body.classList.toggle("danger-critical",
          !dead && running && health <= CONFIG.HEALTH_CRITICAL);
      } catch (_) {}
    }

    // ── Hit-effect stack ────────────────────────────────────────────────
    // Screen shake — brief camera-jolt on <body>. Amplitude scales with the
    // proportion of health you just lost, so bigger hits feel harder.
    let shakeClearTimer = null;
    function shakeScreen(amplitudePx) {
      const px = Math.max(2, Math.min(12, amplitudePx || 5));
      try {
        document.body.style.setProperty("--shake", px + "px");
        // Re-trigger the animation by pulling the class off and forcing reflow.
        document.body.classList.remove("danger-shaking");
        void document.body.offsetWidth;
        document.body.classList.add("danger-shaking");
        if (shakeClearTimer) clearTimeout(shakeClearTimer);
        shakeClearTimer = setTimeout(() => {
          document.body.classList.remove("danger-shaking");
        }, 220);
      } catch (_) {}
    }

    // Directional damage arrow — pulses the chevron on the edge the threat
    // is on. Falls back to a random cardinal edge when the server didn't
    // return a direction hint, so the player still gets a "hit came from
    // *somewhere*" signal on every damage tick.
    let arrowClearTimers = { top: null, right: null, bottom: null, left: null };
    function flashArrow(direction) {
      const dir = ({ top: "top", right: "right", bottom: "bottom", left: "left" })[direction]
        || ["top", "right", "bottom", "left"][Math.floor(Math.random() * 4)];
      const node = el.arrows && el.arrows[dir];
      if (!node) return;
      node.classList.remove("show");
      void node.offsetWidth;
      node.classList.add("show");
      if (arrowClearTimers[dir]) clearTimeout(arrowClearTimers[dir]);
      arrowClearTimers[dir] = setTimeout(() => node.classList.remove("show"), 720);
    }

    // Blood spatter — places the primary blob at the threat's on-screen
    // point when we have coordinates from the server, else at a nudge
    // toward the given cardinal direction (or random on the edges).
    function flashSpatter(direction, threatCx, threatCy) {
      if (!el.spatter) return;
      let x, y;
      if (typeof threatCx === "number" && typeof threatCy === "number") {
        x = threatCx * 100;
        y = threatCy * 100;
      } else {
        const bias = { top: [50, 25], right: [78, 50], bottom: [50, 78],
                       left: [22, 50], center: [50, 50] }[direction || "center"];
        x = bias[0]; y = bias[1];
      }
      el.spatter.style.setProperty("--spatter-x", x + "%");
      el.spatter.style.setProperty("--spatter-y", y + "%");
      el.spatter.classList.remove("hit");
      void el.spatter.offsetWidth;
      el.spatter.classList.add("hit");
    }

    function ingest(reading) {
      if (dead || !running) return;
      const level = Math.max(0, Math.min(2, Number(reading && reading.level) || 0));
      lastLevel = level;
      lastReason = (reading && reading.reason) || "";
      lastDirection = (reading && reading.direction) || null;
      lastThreatCx = (reading && typeof reading.threat_cx === "number")
                     ? reading.threat_cx : null;
      lastThreatCy = (reading && typeof reading.threat_cy === "number")
                     ? reading.threat_cy : null;
      applyDirectionCss(lastDirection);

      const t = now();

      // Fast-path: attacking-level danger. The picture ALREADY shows the hit
      // being committed, so we snap straight to HURTING — no grace. This is
      // still predictable because the tell is on screen; the vignette flips
      // to the fast throb on the same tick and the player sees why.
      if (level >= 2) {
        cleanSince = 0;
        if (mode !== "hurting") setMode("hurting", "level=2");
        return;
      }

      if (level >= 1) {
        cleanSince = 0;
        if (mode === "safe") setMode("warning", "level=1");
        else if (mode === "warning" &&
                 t - stateSince >= CONFIG.WARNING_GRACE_MS) {
          setMode("hurting", "warning grace exhausted");
        }
        // If we're already hurting, level=1 sustains it — no timer reset.
        return;
      }

      // level === 0: begin / continue the SAFE_CONFIRM_MS de-escalation clock.
      if (mode === "safe") { cleanSince = 0; return; }
      if (!cleanSince) cleanSince = t;
      if (t - cleanSince >= CONFIG.SAFE_CONFIRM_MS) {
        setMode("safe", "clean vision confirmed");
      }
    }

    // ── Health / damage ──────────────────────────────────────────────────
    function showHealthBar(show) {
      if (!el.health) return;
      el.health.classList.toggle("hidden", !show);
    }

    function updateHealthBar() {
      if (!el.healthFill) return;
      const pct = Math.max(0, Math.min(100,
                 (health / CONFIG.HEALTH_MAX) * 100));
      el.healthFill.style.width = pct.toFixed(1) + "%";
      if (el.healthNum) el.healthNum.textContent = Math.max(0, Math.round(health));
      const critical = health <= CONFIG.HEALTH_CRITICAL;
      if (el.health) el.health.classList.toggle("critical", critical && !dead);
      showHealthBar(health < CONFIG.HEALTH_MAX);
      applyCriticalBodyClass();
    }

    // The number readout kicks up briefly on damage so each hit lands
    // visually on the meter itself. Retriggered by removing + re-adding
    // the class after a forced reflow.
    function bumpHealthNum() {
      if (!el.healthNum) return;
      el.healthNum.classList.remove("bump");
      void el.healthNum.offsetWidth;
      el.healthNum.classList.add("bump");
    }

    function setRegenerating(on) {
      if (!el.health) return;
      if (on === lastRegenerating) return;
      lastRegenerating = on;
      el.health.classList.toggle("regenerating", !!on);
    }

    function flashHit() {
      if (!el.hitFlash) return;
      // Red hit flash + spatter + directional arrow + screen shake + number
      // bump + audio hit + haptic tick — all on the same beat so each
      // damage tick lands as one felt "impact".
      el.hitFlash.classList.remove("on");
      void el.hitFlash.offsetWidth;
      el.hitFlash.classList.add("on");
      flashSpatter(lastDirection, lastThreatCx, lastThreatCy);
      flashArrow(lastDirection);
      // Shake harder as HP falls — a hit at 10% HP is more visceral than at 90%.
      const hpFrac = Math.max(0, health / CONFIG.HEALTH_MAX);
      const shakePx = 4 + (1 - hpFrac) * 6;
      shakeScreen(shakePx);
      bumpHealthNum();
      try { if (Sound.hit) Sound.hit(); else Sound.error(); } catch (_) {}
      try { Haptics.tick && Haptics.tick(); } catch (_) {}
    }

    function stopHealthLoop() {
      if (hpLoopId) { cancelAnimationFrame(hpLoopId); hpLoopId = null; }
    }

    function startHealthLoop() {
      stopHealthLoop();
      healthTickTs = now();
      nextDamageTick = healthTickTs + CONFIG.DAMAGE_TICK_MS;
      const step = () => {
        if (!running || dead) return;
        const t = now();
        const dt = Math.max(0, (t - healthTickTs) / 1000); // seconds
        healthTickTs = t;

        if (mode === "hurting") {
          // Fairness gate: don't drain while a turn is being committed
          // (state.processing) or the freeze buffer is up (isShowing=false).
          // The picture the state machine is grading is stale during those
          // windows, so a player who just made a good escape choice should
          // not eat 40 HP waiting on server latency.
          const R = window.ReactorRenderer;
          // Fairness gate: skip drain during turn-processing / non-showing
          // frames — EXCEPT during demoActive, where the mode was set by
          // scripted code, not by grading a live frame, so no fairness
          // gate applies. Without this, running the demo in still-image
          // mode (or with reactor warming) would show the vignette + audio
          // but never tick down health, which reads as broken.
          const stale = !demoActive && (
            state.processing ||
            (R && R.isShowing && !R.isShowing())
          );
          if (!stale && t >= nextDamageTick) {
            // Discrete hit: the drain is felt as a punch, not a smooth
            // decrement, which reads better in an action loop.
            const before = health;
            health = Math.max(0, health - CONFIG.DAMAGE_PER_TICK);
            if (health < before) flashHit();
            nextDamageTick = t + CONFIG.DAMAGE_TICK_MS;
            if (health <= 0) {
              die();
              return;
            }
          } else if (stale) {
            // Keep the cadence rolling forward so the FIRST tick after the
            // world settles doesn't land a "banked" hit from the pause.
            nextDamageTick = t + CONFIG.DAMAGE_TICK_MS;
          }
        } else if (mode === "safe") {
          // Smooth regen — feels less like an "advantage granted" and more
          // like getting your breath back.
          if (health < CONFIG.HEALTH_MAX) {
            const before = health;
            health = Math.min(CONFIG.HEALTH_MAX,
                              health + CONFIG.REGEN_PER_SEC * dt);
            setRegenerating(true);
            // Regen-complete chime: fired once when HP crosses back up to
            // full. Closes the audio loop of danger → damage → recover.
            if (before < CONFIG.HEALTH_MAX && health >= CONFIG.HEALTH_MAX) {
              try { if (Sound.regenComplete) Sound.regenComplete(); } catch (_) {}
            }
          } else {
            setRegenerating(false);
          }
          nextDamageTick = t + CONFIG.DAMAGE_TICK_MS; // reset the drain cadence
        } else {
          // WARNING: hold at current value. The reset avoids a stale
          // damage-tick landing the instant we escalate to HURTING.
          nextDamageTick = t + CONFIG.DAMAGE_TICK_MS;
          setRegenerating(false);
        }

        // Live heartbeat tempo tracking — tempo climbs as HP falls so the
        // heartbeat itself telegraphs how close to death you are.
        if (heartbeatOn) {
          try { Sound.heartbeatSetBpm && Sound.heartbeatSetBpm(computeHeartbeatBpm()); } catch (_) {}
        }

        // Tinnitus tone: sustained ringing while at critical HP. Fires ONCE
        // when we cross the threshold down; stopped as soon as HP recovers
        // above it (so a brief dip doesn't leave a lingering hum).
        try {
          const isCritical = !dead && health <= CONFIG.HEALTH_CRITICAL;
          if (isCritical && !tinnitusOn) {
            if (Sound.tinnitusStart) Sound.tinnitusStart();
            tinnitusOn = true;
          } else if (!isCritical && tinnitusOn) {
            if (Sound.tinnitusStop) Sound.tinnitusStop();
            tinnitusOn = false;
          }
        } catch (_) {}

        updateHealthBar();
        hpLoopId = requestAnimationFrame(step);
      };
      hpLoopId = requestAnimationFrame(step);
    }

    function die() {
      if (dead) return;
      dead = true;
      running = false;
      stopHealthLoop();
      if (sampleTimer) { clearTimeout(sampleTimer); sampleTimer = null; }
      // Cut the sustained audio the moment death lands — the death SFX
      // that enterGameOver plays should stand alone, not layer over a
      // still-running heartbeat/tinnitus.
      try { if (heartbeatOn) { Sound.heartbeatStop && Sound.heartbeatStop(); heartbeatOn = false; } } catch (_) {}
      try { if (tinnitusOn) { Sound.tinnitusStop && Sound.tinnitusStop(); tinnitusOn = false; } } catch (_) {}
      // Collapse mode BEFORE applyVisualForMode — otherwise mode is still
      // "hurting" and applyVisualForMode would helpfully re-add the
      // body.danger-hurting class we're about to remove, leaving the
      // chromatic-aberration flicker running through the death overlay.
      mode = "safe";
      setRegenerating(false);
      applyVisualForMode();
      // Redundant with applyVisualForMode's toggle-off, but explicit —
      // also clears danger-critical (health-driven, not mode-driven) and
      // any in-flight shake class so the game-over screen isn't jittering.
      try { document.body.classList.remove("danger-critical", "danger-hurting", "danger-warning", "danger-shaking"); } catch (_) {}
      updateHealthBar();
      log("DEATH — health hit 0");
      try {
        // Route through the existing game-over flow so the death overlay,
        // narrator epitaph, tape archival, and reactor pause all fire the
        // same way as a story-driven death.
        enterGameOver(CONFIG.DEATH_MSG);
      } catch (e) { console.warn("[danger] enterGameOver failed", e); }
    }

    // ── Sampling loop ────────────────────────────────────────────────────
    function shouldSample() {
      if (!running || dead) return false;
      if (state.gameOver) return false;
      if (Renderer.mode !== "reactor") return false;
      if (!Renderer.reactorAvailable()) return false;
      const R = window.ReactorRenderer;
      if (!R.isShowing || !R.isShowing()) return false;
      // Camera is being driven — the frame we'd grade is mid-motion and
      // often mostly blur / partial. Coast on the last mode until the view
      // settles; the state machine will de-escalate on its own via clean
      // readings when sampling resumes.
      if (state.moving) return false;
      return true;
    }

    function readLuma(dataUrl) {
      // Fast luma proxy from a tiny 8x8 downsample of the captured frame.
      // Used to catch brightness spikes (muzzle flash / explosion) that a
      // 1 Hz vision loop would miss. Async → we don't block sampling on it.
      return new Promise((resolve) => {
        try {
          const img = new Image();
          img.onload = () => {
            try {
              const c = document.createElement("canvas");
              c.width = 8; c.height = 8;
              const g = c.getContext("2d");
              g.drawImage(img, 0, 0, 8, 8);
              const d = g.getImageData(0, 0, 8, 8).data;
              let s = 0;
              for (let i = 0; i < d.length; i += 4) {
                s += (0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2]);
              }
              // 8×8 = 64 pixels, each contributing 0..255 → normalize to 0..1
              // so BOOST_LUMA_DELTA is a legible "fraction of full range".
              resolve(s / (64 * 255));
            } catch (_) { resolve(null); }
          };
          img.onerror = () => resolve(null);
          img.src = dataUrl;
        } catch (_) { resolve(null); }
      });
    }

    async function sampleOnce(reason) {
      if (inFlight) return;
      const R = window.ReactorRenderer;
      if (!R || !R.captureFrame) return;
      const frame = R.captureFrame(CONFIG.CAPTURE_WIDTH);
      if (!frame) return;

      // Cheap luma-delta boost: if the picture just got dramatically
      // brighter, that's almost certainly a muzzle flash / explosion. We
      // fire the vision call NOW rather than waiting for the next tick, so
      // sudden violence has sub-second reaction time.
      // (Sampling this AFTER we've already decided to do a call is fine —
      // it seeds the next comparison.)
      readLuma(frame).then((lum) => {
        if (lum == null) return;
        if (lastLumaSample != null) {
          const delta = Math.abs(lum - lastLumaSample);
          if (delta >= CONFIG.BOOST_LUMA_DELTA && !inFlight) {
            log("luma spike", delta.toFixed(2), "→ boost");
            // Trigger a fresh sample almost immediately, out of band.
            setTimeout(() => { if (running) sampleOnce("luma-boost"); }, 60);
          }
        }
        lastLumaSample = lum;
      });

      inFlight = true;
      lastPostMs = now();
      const controller = ("AbortController" in window) ? new AbortController() : null;
      const timeout = controller ? setTimeout(() => {
        try { controller.abort(); } catch (_) {}
      }, CONFIG.REQUEST_TIMEOUT_MS) : null;

      try {
        const resp = await fetch("/api/danger", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ frame }),
          signal: controller ? controller.signal : undefined,
        });
        if (timeout) clearTimeout(timeout);
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        const reading = await resp.json();
        consecutiveErrors = 0;
        log("reading", reading, reason || "");
        ingest(reading);
      } catch (err) {
        if (timeout) clearTimeout(timeout);
        consecutiveErrors += 1;
        log("call failed", err && err.message);
        // Don't ingest ANYTHING on failure — the state machine coasts on
        // its last known level, which for a persistent outage means it
        // will slowly de-escalate via clean-reading absence (see below).
        // But we do NOT want to be stuck HURTING forever if the server is
        // gone, so on repeated failures we synthesize a level=0 read to
        // ease back toward SAFE.
        if (consecutiveErrors >= 3) ingest({ level: 0, reason: "vision-offline" });
      } finally {
        inFlight = false;
      }
    }

    function scheduleNext() {
      if (sampleTimer) { clearTimeout(sampleTimer); sampleTimer = null; }
      if (!running) return;
      const gap = (consecutiveErrors >= 2) ? CONFIG.BACKOFF_MS : CONFIG.SAMPLE_MS;
      sampleTimer = setTimeout(async () => {
        try {
          if (shouldSample()) await sampleOnce("tick");
        } finally {
          scheduleNext();
        }
      }, gap);
    }

    // ── Public control ──────────────────────────────────────────────────
    function start() {
      // Damage/danger system is disabled until a real combat mode exists — see
      // DAMAGE_SYSTEM_ENABLED. No frame grading, no health drain, no vignette.
      if (!DAMAGE_SYSTEM_ENABLED) return;
      if (running) return;
      bindDom();
      if (!el.vignette) return; // DOM missing — abort silently
      running = true;
      dead = false;
      consecutiveErrors = 0;
      lastLumaSample = null;
      health = CONFIG.HEALTH_MAX;
      mode = "safe";
      stateSince = now();
      cleanSince = 0;
      applyDirectionCss(null);
      applyVisualForMode();
      updateHealthBar();
      showHealthBar(false);
      startHealthLoop();
      scheduleNext();
      log("start");
    }

    function stop() {
      if (!running) return;
      running = false;
      if (sampleTimer) { clearTimeout(sampleTimer); sampleTimer = null; }
      stopHealthLoop();
      mode = "safe";
      applyVisualForMode();
      // Tear down the sustained audio loops so leaving reactor mode (or
      // pausing the game) doesn't leave a heartbeat / tinnitus running.
      try { if (heartbeatOn) { Sound.heartbeatStop && Sound.heartbeatStop(); heartbeatOn = false; } } catch (_) {}
      try { if (tinnitusOn) { Sound.tinnitusStop && Sound.tinnitusStop(); tinnitusOn = false; } } catch (_) {}
      try { document.body.classList.remove("danger-critical", "danger-hurting", "danger-warning", "danger-shaking"); } catch (_) {}
      setRegenerating(false);
      log("stop");
    }

    function reset() {
      // Clean slate — used by /api/reset & the death-overlay restart. Wipes
      // health + mode + death flag, then re-arms the loop if realtime is
      // active (a die() during the prior run set running=false, so without
      // this the danger meter would be dark forever after a restart).
      const wasRunning = running;
      running = false;
      if (sampleTimer) { clearTimeout(sampleTimer); sampleTimer = null; }
      stopHealthLoop();
      // Any lingering sustained audio from the prior run needs to end
      // before we zero everything out.
      try { if (heartbeatOn) { Sound.heartbeatStop && Sound.heartbeatStop(); heartbeatOn = false; } } catch (_) {}
      try { if (tinnitusOn) { Sound.tinnitusStop && Sound.tinnitusStop(); tinnitusOn = false; } } catch (_) {}
      try { document.body.classList.remove("danger-critical", "danger-hurting", "danger-warning", "danger-shaking"); } catch (_) {}
      // End any in-flight demo/manual override so a game restart returns
      // to normal vision-driven behavior.
      try { if (demoActive) { demoActive = false; demoTimers.forEach((t) => clearTimeout(t)); demoTimers = []; } } catch (_) {}
      dead = false;
      consecutiveErrors = 0;
      lastLumaSample = null;
      lastThreatCx = lastThreatCy = null;
      lastDirection = null;
      health = CONFIG.HEALTH_MAX;
      mode = "safe";
      stateSince = now();
      cleanSince = 0;
      applyDirectionCss(null);
      applyVisualForMode();
      setRegenerating(false);
      updateHealthBar();
      showHealthBar(false);
      const reactorLive = (typeof Renderer !== "undefined") &&
                          Renderer.mode === "reactor" &&
                          Renderer.reactorAvailable();
      if (wasRunning || reactorLive) start();
      log("reset");
    }

    function getState() {
      return {
        running, mode, health,
        level: lastLevel, reason: lastReason, direction: lastDirection,
        dead,
      };
    }

    // On sound-toggle: the parent toggleSound() has already cut the
    // sustained audio when muting. When re-enabling, we clear our own
    // flags so the next health-loop frame notices "no heartbeat but we're
    // HURTING" and restarts the tone, and same for the tinnitus threshold
    // check. Muting side just clears the flags so the check is honest.
    function onSoundToggled(on) {
      heartbeatOn = false;
      tinnitusOn = false;
      if (on && mode === "hurting" && !dead) {
        try { Sound.heartbeatStart && Sound.heartbeatStart(computeHeartbeatBpm()); heartbeatOn = true; } catch (_) {}
      }
      if (on && !dead && health <= CONFIG.HEALTH_CRITICAL) {
        try { Sound.tinnitusStart && Sound.tinnitusStart(); tinnitusOn = true; } catch (_) {}
      }
    }

    // ── Demo / manual test mode ──────────────────────────────────────
    // A scripted safe → warning → hurting → warning → safe sequence that
    // ignores the vision loop and just drives the state machine directly.
    // Trigger with Shift+D (see keyboard handler far below) or the query
    // string `?danger_demo=1`, or programmatically via DangerSystem.demo().
    // Purpose: let anyone experience the full polish stack instantly on
    // any scene, without hunting for one that trips the vision rubric.
    // Suspends the sampling loop for the duration so live readings can't
    // stomp the scripted mode.
    // `demoActive` is the shared "vision loop is temporarily suspended in
    // favour of a scripted / manual state" flag. Both demo() and
    // forceMode() set it, and the shouldSample() override below gates on
    // it so real readings can't stomp the scripted mode.
    let demoActive = false;
    let demoTimers = [];
    function clearDemoTimers() {
      demoTimers.forEach((t) => clearTimeout(t));
      demoTimers = [];
    }
    function demoStep(delayMs, fn) {
      demoTimers.push(setTimeout(fn, delayMs));
    }
    function endDemo() {
      demoActive = false;
      clearDemoTimers();
      log("demo", "end");
    }
    function ensureLocallyRunning() {
      // If start()'s reactor-gated flow hasn't already spun us up (e.g. we
      // are in still-image mode or reactor is still warming), pull up the
      // system locally so demo/forceMode still work. Live samples remain
      // gated by shouldSample() so this doesn't add spurious API traffic.
      if (!DAMAGE_SYSTEM_ENABLED) return false; // disabled until combat mode
      if (running) return true;
      bindDom();
      if (!el.vignette) { log("abort — no DOM"); return false; }
      running = true;
      dead = false;
      health = CONFIG.HEALTH_MAX;
      mode = "safe";
      stateSince = now();
      applyDirectionCss(null);
      applyVisualForMode();
      updateHealthBar();
      showHealthBar(false);
      startHealthLoop();
      return true;
    }
    function demo(opts) {
      // Force the DangerSystem to visibly cycle through its states so a
      // player (or a QA session) can experience the polish without needing
      // vision to actually escalate. Health drops during the HURTING phase
      // and regenerates during the recovery phase — same code path as a
      // real run.
      if (!DAMAGE_SYSTEM_ENABLED) return; // disabled until combat mode
      if (!ensureLocallyRunning()) return;
      if (demoActive) endDemo();
      demoActive = true;
      clearDemoTimers();
      const dir = (opts && opts.direction) || "right";
      log("demo", "start", dir);
      lastDirection = dir; lastThreatCx = 0.85; lastThreatCy = 0.5;
      applyDirectionCss(dir);
      setMode("warning", "demo");
      demoStep(2400, () => { if (demoActive) setMode("hurting", "demo"); });
      demoStep(6400, () => { if (demoActive) setMode("warning", "demo"); });
      demoStep(8000, () => { if (demoActive) setMode("safe", "demo"); });
      demoStep(9200, () => { if (demoActive) endDemo(); });
    }

    // Test-only setter: force a specific mode. Useful for taking
    // screenshots or tuning individual states. Suspends live sampling
    // until the caller calls forceMode("safe") or reset().
    function forceMode(next, direction) {
      if (!ensureLocallyRunning()) return;
      demoActive = true;
      if (direction) {
        lastDirection = direction;
        applyDirectionCss(direction);
      }
      setMode(next, "manual");
      if (next === "safe") {
        // "safe" through the manual gate is treated as "resume live sampling"
        // so a tester can flip out of a forced state without calling reset().
        demoActive = false;
      }
    }

    // shouldSample() gate — extended to also suspend during demo/manual
    // states so live readings don't overwrite the scripted mode.
    const _baseShouldSample = shouldSample;
    shouldSample = function () {
      if (demoActive) return false;
      return _baseShouldSample();
    };

    return { start, stop, reset, getState, onSoundToggled, demo, forceMode };
  })();
  try { window.__DangerSystem = DangerSystem; } catch (_) {}

  function updateRendererButton() {
    const realtime = Renderer.mode === "reactor" && Renderer.reactorAvailable();
    document.body.classList.toggle("realtime-on", realtime);
    // The camera works on a still frame just as well as on live video, so it's
    // revealed by "there is a scene", not "realtime is up". Gating it on
    // realtime hid the whole photography loop — the dossier and the case win
    // with it — in exactly the situation where realtime is unavailable.
    document.body.classList.toggle(
      "camera-on",
      realtime || !!(state.currentStillUrl || (Renderer.lastScene && Renderer.lastScene.imageUrl))
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
    try { VerbBar.update(); } catch (_) {}
    try { HappyOysterOptions.update(); } catch (_) {}
    try { InputProfileUi.update(); } catch (_) {}
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
      ? "Still images (current image provider)"
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
  // Music volume control — preset options in the debug panel (WORLD MODEL / L)
  // so the ambient bed can be tuned or muted live. Persists via SceneAudio.
  // ------------------------------------------------------------------
  function makeMusicBtn(preset) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "rt-music-btn";
    b.dataset.vol = String(preset.value);
    b.textContent = preset.label;
    b.title = "Music volume: " + preset.label;
    b.addEventListener("click", () => {
      try { SceneAudio.setVolume(preset.value); } catch (_) {}
      try { Haptics.tap(); } catch (_) {}
      updateMusicVolume();
    });
    return b;
  }

  function buildMusicVolume() {
    const wrap = el.rtMusicOpts;
    if (!wrap) return;
    let presets = [];
    try { presets = SceneAudio.volumePresets ? SceneAudio.volumePresets() : []; } catch (_) {}
    wrap.innerHTML = "";
    presets.forEach((p) => wrap.appendChild(makeMusicBtn(p)));
    updateMusicVolume();
  }

  function updateMusicVolume() {
    const wrap = el.rtMusicOpts;
    if (!wrap || !wrap.children.length) return;
    let cur = 0;
    try { cur = SceneAudio.getVolume ? SceneAudio.getVolume() : 0; } catch (_) {}
    // Highlight the preset closest to the current volume (values are distinct).
    let best = null, bestD = Infinity;
    Array.prototype.forEach.call(wrap.children, (b) => {
      const d = Math.abs(parseFloat(b.dataset.vol) - cur);
      if (d < bestD) { bestD = d; best = b; }
    });
    Array.prototype.forEach.call(wrap.children, (b) => {
      b.classList.toggle("active", b === best);
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

  // ------------------------------------------------------------------
  // Story log — the run's narrative chronicle. Formerly floating text over the
  // scene; now a docked, toggleable panel (STORY button / J), mirroring the
  // world-model log, so the art is never obstructed. Collapsed by default.
  // ------------------------------------------------------------------
  const StoryLog = (function () {
    function visible() {
      try { return localStorage.getItem("story_log") === "on"; } catch (_) { return false; }
    }
    function applyVisibility() {
      const on = visible();
      document.body.classList.toggle("story-log-on", on);
      if (el.btnStory) el.btnStory.classList.toggle("active", on);
      // When opened, jump to the latest beat so it reads like a live tail.
      if (on && el.prose) el.prose.scrollTop = el.prose.scrollHeight + 400;
    }
    function toggle() {
      const on = !visible();
      try { localStorage.setItem("story_log", on ? "on" : "off"); } catch (_) {}
      applyVisibility();
    }
    function init() {
      applyVisibility();
      if (el.storyLogHide) el.storyLogHide.addEventListener("click", () => {
        try { localStorage.setItem("story_log", "off"); } catch (_) {}
        applyVisibility();
      });
    }
    return { visible, toggle, init };
  })();

  // ------------------------------------------------------------------
  // WORLD EDITOR — tweak the prompts that drive the simulation, live, without
  // leaving the game. Prompts hot-reload server-side (prompts_store), so an
  // edit either takes effect on the NEXT TURN (Apply Live) or seeds a fresh
  // run (Save & Restart). The Worlds tab saves/loads named prompt-sets. Reads
  // the existing /api/admin/studio/* endpoints (open in local dev).
  // ------------------------------------------------------------------
  const WorldEditor = (function () {
    // Fields whose effect only really shows on a FRESH run (they seed the
    // world/intro); everything else re-steers live on the next turn.
    const RESTART_KEYS = new Set(["world_initial_state", "gemini_text_to_image_instructions"]);
    let content = null;            // {prompts, prompts_defaults, schema, groups}
    let sessionSnapshot = null;    // editable values captured on first load (Revert target)
    let edits = {};                // key -> current textarea value (unsaved)
    let tabs = [];                 // [{id,label,layer}] — one per layer, plus Worlds
    let activeTab = null;
    let levels = [];               // saved LEVEL-layer snapshots (the gallery)
    let loaded = false;
    let loading = false;
    // Cast & Camera: the structured spec, its form definition, and what it
    // compiles to. Unlike prompts these save on change (no draft buffer) —
    // they're small typed values and every save recompiles the directive
    // server-side, which is what the preview pane renders.
    let identity = {};
    let identitySchema = [];
    let identityPreview = null;
    // One switch for the whole editor. Off (the default) shows the four
    // prompts and the dozen cast fields that actually redirect the game; on
    // reveals the mechanical rulebooks underneath them. Twelve equal-looking
    // paragraphs is how you end up editing the wrong one, so the hierarchy is
    // in the UI rather than only in the descriptions.
    let showAdvanced = false;
    try { showAdvanced = localStorage.getItem("we_advanced") === "1"; } catch (_) {}

    async function weFetch(method, path, body) {
      const opts = { method, headers: {} };
      if (body !== undefined) {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
      }
      const r = await fetch(path, opts);
      let data = null;
      try { data = await r.json(); } catch (_) {}
      return { ok: r.ok, status: r.status, data };
    }

    function toast(msg, kind) {
      if (!el.weToast) return;
      el.weToast.textContent = msg;
      el.weToast.style.borderColor = kind === "warn"
        ? "rgba(255, 180, 120, 0.6)" : "rgba(134, 200, 255, 0.4)";
      el.weToast.classList.add("show");
      clearTimeout(toast._t);
      toast._t = setTimeout(() => el.weToast.classList.remove("show"), 2600);
    }

    function schemaFields() { return (content && content.schema) || []; }
    function groupLabels() { return (content && content.groups) || {}; }
    function groupBlurbs() { return (content && content.group_blurbs) || {}; }
    function isAdvanced(f) { return (f && f.tier) === "advanced"; }

    function setAdvanced(on) {
      showAdvanced = !!on;
      try { localStorage.setItem("we_advanced", showAdvanced ? "1" : "0"); } catch (_) {}
      render();
    }

    // The "N more" reveal shared by the prompt tabs and the cast cards, so
    // both use one mental model: essentials in front, machinery behind a
    // single labelled line you can click.
    function makeAdvancedToggle(count, noun) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "we-more";
      const label = count + " advanced " + noun + (count === 1 ? "" : "s");
      b.textContent = showAdvanced ? "▾ Hide " + label : "▸ " + label;
      b.title = showAdvanced
        ? "Hide the mechanical rulebooks"
        : "Show the mechanical rulebooks underneath these";
      b.addEventListener("click", () => setAdvanced(!showAdvanced));
      return b;
    }
    function valOf(key) {
      if (key in edits) return edits[key];
      return (content && content.prompts && content.prompts[key] != null) ? content.prompts[key] : "";
    }
    function defOf(key) {
      const d = content && content.prompts_defaults;
      return d && d[key] != null ? d[key] : "";
    }
    function anyDirty() { return Object.keys(edits).length > 0; }

    // The two image templates pull the shared Art Direction / Camera Rules
    // blocks in through {art_direction} / {camera_rules}. Deleting a
    // placeholder is legal — you might want a fully bespoke template — but it
    // means edits to the shared field stop reaching this render path, which is
    // invisible from the resulting image. Say so.
    //
    // Derived from the field's CURRENT text rather than a value fetched at
    // load, so it can't go stale the moment you save a template.
    const SHARED_IMAGE_VARS = ["art_direction", "camera_rules"];

    function setHint(node, key) {
      const note = sharedWiringNote(key);
      node.textContent = note || "Double-click the text (or hit Expand) to edit full-screen.";
      node.classList.toggle("warn", note.startsWith("\u26a0"));
    }

    function sharedWiringNote(key) {
      const f = schemaFields().find((x) => x.id === key);
      const shared = ((f && f.format_vars) || []).filter((v) => SHARED_IMAGE_VARS.includes(v));
      if (!shared.length) return "";
      const text = String(valOf(key) || "");
      const missing = shared.filter((v) => !text.includes("{" + v + "}"));
      if (!missing.length) {
        return "Pulls in the shared Art Direction + Camera Rules. Edit those to change the world.";
      }
      return "⚠ This template no longer includes " + missing.map((v) => "{" + v + "}").join(" or ") +
             ", so edits to that shared field won't reach this render path.";
    }

    // ── INFO POPOVERS ──────────────────────────────────────────────────────
    // Every long explanation in this panel goes through here. Inline prose was
    // the whole problem: a layer paragraph, a description under every block, a
    // help line under every input and a "what this does" note on every prompt
    // meant the editor had to be READ before it could be used. The text still
    // exists — it's one click away, on the thing it describes, and it closes
    // when you look away.
    let popNode = null;
    let popOwner = null;

    function popEl() {
      if (popNode && popNode.isConnected) return popNode;
      popNode = document.createElement("div");
      popNode.className = "we-pop";
      popNode.setAttribute("role", "dialog");
      popNode.innerHTML =
        '<div class="we-pop-title"></div><div class="we-pop-body"></div>';
      (el.worldEditor || document.body).appendChild(popNode);
      return popNode;
    }

    function closePop() {
      if (popNode) popNode.classList.remove("show");
      if (popOwner) popOwner.setAttribute("aria-expanded", "false");
      popOwner = null;
    }

    function openPop(btn, title, body) {
      const node = popEl();
      node.querySelector(".we-pop-title").textContent = title || "";
      node.querySelector(".we-pop-body").textContent = body || "";
      popOwner = btn;
      btn.setAttribute("aria-expanded", "true");
      node.classList.add("show");
      // Anchor under the button, then pull back inside the panel if it would
      // hang off either edge.
      const host = (el.worldEditor || document.body).getBoundingClientRect();
      const r = btn.getBoundingClientRect();
      const w = node.offsetWidth;
      let left = r.left - host.left + r.width / 2 - w / 2;
      left = Math.max(10, Math.min(left, host.width - w - 10));
      node.style.left = left + "px";
      let top = r.bottom - host.top + 8;
      if (top + node.offsetHeight > host.height - 10) {
        top = Math.max(10, r.top - host.top - node.offsetHeight - 8);
      }
      node.style.top = top + "px";
    }

    // A small ⓘ that carries the long version. Returns null when there's
    // nothing to say, so callers can append unconditionally.
    function infoBtn(title, body) {
      if (!body) return null;
      const b = document.createElement("button");
      b.type = "button";
      b.className = "we-info";
      b.setAttribute("aria-label", "About " + (title || "this"));
      b.setAttribute("aria-expanded", "false");
      b.textContent = "\u24D8";
      b.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (popOwner === b) { closePop(); return; }
        openPop(b, title || "", body);
      });
      return b;
    }

    function refreshDirtyBadge() {
      if (el.weDirty) el.weDirty.classList.toggle("hidden", !anyDirty());
    }

    function layerList() { return (content && content.layers) || []; }
    function layerById(id) { return layerList().find((l) => l.id === id) || null; }

    // One tab per LAYER — the four questions a game developer actually asks, in
    // the order they depend on each other. LEVEL opens first: it's the layer you
    // live in, and opening on the engine's JSON contracts is exactly what made
    // this feel like someone else's codebase instead of your level editor.
    function buildTabs() {
      const layers = layerList();
      tabs = layers.map((l) => ({ id: l.id, label: l.label, layer: l }));
      tabs.push({ id: "worlds", label: "Builds" });
      if (!activeTab || !tabs.find((t) => t.id === activeTab)) {
        activeTab = layers.some((l) => l.id === "level") ? "level"
                  : (tabs[0] && tabs[0].id);
      }
    }

    // The layer STACK, not a tab strip: each rail shows what the layer answers,
    // how volatile it is, and whether editing it is a contract change. Reading
    // it top to bottom is meant to teach the model in one glance.
    function renderTabs() {
      if (!el.weTabs) return;
      el.weTabs.innerHTML = "";
      tabs.forEach((t) => {
        const b = document.createElement("button");
        const isLayer = !!t.layer;
        b.className = "we-layer" + (t.id === activeTab ? " active" : "") +
                      (isLayer ? "" : " we-layer-aux");
        b.type = "button";
        b.dataset.tab = t.id;
        if (isLayer) {
          b.style.setProperty("--layer-accent", t.layer.accent || "#ffd27a");
          b.title = t.layer.question + " — " + t.layer.scope;
          b.innerHTML =
            '<span class="we-layer-glyph" aria-hidden="true">' + (t.layer.icon || "") + "</span>" +
            '<span class="we-layer-text">' +
              '<span class="we-layer-name">' + esc(t.layer.label) + "</span>" +
              '<span class="we-layer-q">' + esc(t.layer.question) + "</span>" +
            "</span>" +
            '<span class="we-layer-risk we-risk-' + esc(t.layer.risk || "content") + '">' +
              (t.layer.risk === "contract" ? "contract" : "content") + "</span>";
        } else {
          b.innerHTML = '<span class="we-layer-glyph" aria-hidden="true">\u25A6</span>' +
            '<span class="we-layer-text"><span class="we-layer-name">Builds</span>' +
            '<span class="we-layer-q">Snapshot everything</span></span>';
        }
        b.addEventListener("click", () => { activeTab = t.id; render(); });
        el.weTabs.appendChild(b);
      });
    }

    function esc(s) {
      return String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    // The header a layer opens with. One line by default — the long version is
    // behind a disclosure, because a paragraph of explanation on every panel is
    // what made this read as documentation rather than a tool.
    function layerIntro(layer) {
      const wrap = document.createElement("div");
      wrap.className = "we-lhead we-risk-" + (layer.risk || "content");
      wrap.style.setProperty("--layer-accent", layer.accent || "#7aa2ff");

      const row = document.createElement("div");
      row.className = "we-lhead-row";
      row.innerHTML =
        '<span class="we-lhead-dot" aria-hidden="true"></span>' +
        '<span class="we-lhead-title">' + esc(layer.label) + "</span>" +
        '<span class="we-lhead-tag">' + esc(layer.tagline || layer.question || "") + "</span>";

      const info = infoBtn(layer.label, (layer.blurb || "") +
        (layer.risk === "contract"
          ? " These values are parsed by code, so a careless edit can stop turns resolving."
          : " " + (layer.scope || "")));
      if (info) row.appendChild(info);

      wrap.appendChild(row);
      return wrap;
    }

    // A prompt is a 2,000+ character document. Rendering a dozen of them as
    // open 320px textareas turned every layer into a wall of prose you had to
    // scroll past to find anything. Each is now a CARD showing its first couple
    // of lines; the full writing surface is the pop-out editor, which already
    // has line numbers, a diff against factory, and validation.
    function makeField(f) {
      const key = f.id;
      const wrap = document.createElement("div");
      const modified = valOf(key) !== defOf(key);
      wrap.className = "we-card" + (modified ? " modified" : "");
      wrap.dataset.key = key;
      wrap.tabIndex = 0;
      wrap.setAttribute("role", "button");
      wrap.setAttribute("aria-label", "Edit " + (f.label || key));

      const head = document.createElement("div");
      head.className = "we-card-head";
      const label = document.createElement("span");
      label.className = "we-card-label";
      label.textContent = f.label || key;
      const isRestart = RESTART_KEYS.has(key);
      const chip = document.createElement("span");
      chip.className = "we-tag " + (isRestart ? "we-tag-restart" : "we-tag-live");
      chip.textContent = isRestart ? "restart" : "live";
      chip.title = isRestart
        ? "Seeds the world — start a fresh run to see it"
        : "Re-steers the sim on the next turn";
      head.appendChild(label);
      const wiring = sharedWiringNote(key);
      const about = infoBtn(f.label || key,
        [f.description, wiring].filter(Boolean).join("\n\n"));
      if (about) head.appendChild(about);
      head.appendChild(chip);

      const value = String(valOf(key) || "");
      const preview = document.createElement("div");
      preview.className = "we-card-preview";
      // Truncate in JS, not just with a CSS line clamp. A clamp still puts the
      // whole 15,000-character contract in the DOM to render two lines of it.
      const flat = value.replace(/\s+/g, " ").trim();
      preview.textContent = flat ? flat.slice(0, 240) : "Empty";

      const meta = document.createElement("div");
      meta.className = "we-card-meta";
      const size = document.createElement("span");
      size.textContent = value.length.toLocaleString("en-US") + " chars";
      meta.appendChild(size);
      if (Array.isArray(f.format_vars) && f.format_vars.length) {
        const vars = document.createElement("span");
        vars.className = "we-card-vars";
        vars.textContent = f.format_vars.length + " variables";
        vars.title = f.format_vars.map((v) => "{" + v + "}").join("  ");
        meta.appendChild(vars);
      }
      if (modified) {
        const edited = document.createElement("span");
        edited.className = "we-card-edited";
        edited.textContent = "edited";
        meta.appendChild(edited);
      }

      const open = () => openPromptModal(key);
      wrap.addEventListener("click", (e) => {
        if (e.target.closest(".we-info, .we-pop, .we-card-reset")) return;
        open();
      });
      wrap.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
      });

      wrap.appendChild(head);
      wrap.appendChild(preview);
      wrap.appendChild(meta);

      if (modified) {
        const reset = document.createElement("button");
        reset.className = "we-card-reset";
        reset.type = "button";
        reset.textContent = "Reset";
        reset.title = "Restore the factory default";
        reset.addEventListener("click", (e) => { e.stopPropagation(); resetField(key); });
        wrap.appendChild(reset);
      }

      const warn = document.createElement("div");
      warn.className = "we-warn hidden";
      wrap.appendChild(warn);
      return wrap;
    }

    function render() {
      closePop();
      renderTabs();
      const showWorlds = activeTab === "worlds";
      const layer = layerById(activeTab);
      // The cast form and the prompt column are both driven per LAYER now: a
      // layer may own structured spec blocks, prompt bodies, or both.
      const showCast = !!(layer && (layer.spec_blocks || []).length);
      if (el.weFields) el.weFields.classList.toggle("hidden", showWorlds);
      if (el.weWorlds) el.weWorlds.classList.toggle("hidden", !showWorlds);
      if (el.weCast) {
        el.weCast.classList.toggle("hidden", !showCast);
        // Drop the forms outright on a layer that owns none, rather than
        // leaving the previous layer's sheet parked behind this one.
        if (!showCast) el.weCast.innerHTML = "";
      }
      // Drop the prompt column's DOM when it isn't in play: dozens of textareas
      // holding tens of thousands of characters have no business sitting behind
      // another panel.
      if (el.weLayerHead) {
        el.weLayerHead.innerHTML = "";
        el.weLayerHead.classList.toggle("hidden", showWorlds);
      }
      if (showWorlds && el.weFields) el.weFields.innerHTML = "";
      if (showWorlds) { renderWorlds(); refreshDirtyBadge(); return; }
      if (!el.weFields || !layer) return;
      el.weFields.innerHTML = "";

      // The active layer's accent drives the whole panel — focus rings, the
      // primary button, the plate hover — so which layer you're in is legible
      // from any corner without a badge repeating it.
      if (el.worldEditor) {
        el.worldEditor.style.setProperty("--layer-accent", layer.accent || "#7aa2ff");
        el.worldEditor.style.setProperty("--we-accent", layer.accent || "#7aa2ff");
      }
      if (el.weLayerHead) {
        el.weLayerHead.appendChild(layerIntro(layer));
        // The Level layer leads with its gallery — what makes a level a level
        // rather than "the current settings" is that you can keep several.
        if (activeTab === "level") el.weLayerHead.appendChild(makeLevelGallery());
      }

      // Structured forms (spec blocks) come before prompt bodies: answering
      // "which place is this" with a form is easier than writing prose, and the
      // prose field below it is then an elaboration rather than a blank page.
      if (showCast) renderCast(layer.spec_blocks);

      const ids = layer.fields || [];
      const fields = schemaFields().filter((f) => ids.indexOf(f.id) !== -1);
      const primary = fields.filter((f) => !isAdvanced(f));
      const advanced = fields.filter(isAdvanced);
      primary.forEach((f) => el.weFields.appendChild(makeField(f)));
      if (advanced.length) {
        el.weFields.appendChild(makeAdvancedToggle(advanced.length, "prompt"));
        if (showAdvanced) advanced.forEach((f) => el.weFields.appendChild(makeField(f)));
      }
      refreshDirtyBadge();
    }

    // ---- LEVEL gallery: save the current level, switch between saved ones ----
    function makeLevelGallery() {
      const wrap = document.createElement("div");
      wrap.className = "we-levels";

      const head = document.createElement("div");
      head.className = "we-levels-head";
      head.innerHTML = '<span class="we-levels-title">LEVELS</span>';
      const about = infoBtn("Levels",
        "A level is one place: its brief and its setting plate. Saving snapshots " +
        "only those, so opening a different level leaves the engine, the game and " +
        "your character exactly as they were \u2014 which is what lets you build a " +
        "set of levels for one game and switch between them.");
      if (about) head.appendChild(about);
      wrap.appendChild(head);

      const row = document.createElement("div");
      row.className = "we-levels-save";
      const input = document.createElement("input");
      input.type = "text";
      input.placeholder = "Name this level\u2026";
      input.maxLength = 64;
      const save = document.createElement("button");
      save.type = "button";
      save.className = "we-btn we-btn-primary";
      save.textContent = "SAVE LEVEL";
      const commit = async () => {
        const name = input.value.trim();
        if (!name) { input.focus(); return; }
        // Flush pending prompt edits so the snapshot is what's on screen.
        const dirty = dirtyFields();
        if (Object.keys(dirty).length) await saveFields(dirty);
        const { ok, data } = await weFetch("POST", "/api/admin/studio/levels", { name });
        const payload = (data && (data.data || data)) || {};
        if (!ok) { toast("Couldn't save that level", "warn"); return; }
        levels = payload.levels || levels;
        edits = {};
        input.value = "";
        toast("Saved level \u201C" + name + "\u201D");
        render();
      };
      save.addEventListener("click", commit);
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); commit(); }
      });
      row.appendChild(input);
      row.appendChild(save);
      wrap.appendChild(row);

      const list = document.createElement("div");
      list.className = "we-level-cards";
      if (!levels.length) {
        const empty = document.createElement("div");
        empty.className = "we-levels-empty";
        empty.textContent = "No levels yet. Describe a place below, then name and save it.";
        list.appendChild(empty);
      }
      levels.forEach((lv) => list.appendChild(makeLevelCard(lv)));
      wrap.appendChild(list);
      return wrap;
    }

    function makeLevelCard(lv) {
      const card = document.createElement("div");
      card.className = "we-level-card" + (lv.enabled ? "" : " off");
      const bits = [];
      if (lv.era) bits.push(lv.era);
      if (lv.has_opening_shot) bits.push("opening shot");
      // A level you can SEE beats a level you have to read. When the level has
      // reference art, the first plate becomes the card.
      const art = (lv.plates && lv.plates[0])
        ? '<div class="we-level-art" style="background-image:url(\'' + lv.plates[0] + '\')"></div>'
        : '<div class="we-level-art we-level-art-empty" aria-hidden="true"></div>';
      card.innerHTML = art +
        '<div class="we-level-body">' +
          '<div class="we-level-card-top">' +
            '<span class="we-level-card-name">' + esc(lv.name) + "</span>" +
            (lv.enabled ? "" : '<span class="we-level-card-off">off</span>') +
          "</div>" +
          '<div class="we-level-card-sum">' + esc(lv.summary || "No description yet.") + "</div>" +
          (bits.length ? '<div class="we-level-card-meta">' + esc(bits.join(" \u00B7 ")) + "</div>" : "") +
        "</div>";

      const acts = document.createElement("div");
      acts.className = "we-level-card-acts";
      const load = document.createElement("button");
      load.type = "button";
      load.className = "we-btn we-btn-primary";
      load.textContent = "OPEN";
      load.addEventListener("click", async () => {
        const { ok, data } = await weFetch("POST", "/api/admin/studio/levels/load", { slug: lv.slug });
        if (!ok) { toast("Couldn't open that level", "warn"); return; }
        const payload = (data && (data.data || data)) || {};
        if (payload.prompts && content) content.prompts = payload.prompts;
        if (payload.identity) identity = payload.identity;
        if (payload.identity_preview) identityPreview = payload.identity_preview;
        edits = {};
        toast("Now editing \u201C" + lv.name + "\u201D \u2014 restart to play it");
        render();
      });
      const del = document.createElement("button");
      del.type = "button";
      del.className = "we-btn we-btn-ghost";
      del.textContent = "Delete";
      del.addEventListener("click", async () => {
        const { ok, data } = await weFetch("DELETE", "/api/admin/studio/levels", { slug: lv.slug });
        if (!ok) { toast("Couldn't delete that level", "warn"); return; }
        const payload = (data && (data.data || data)) || {};
        levels = payload.levels || [];
        render();
      });
      acts.appendChild(load);
      acts.appendChild(del);
      (card.querySelector(".we-level-body") || card).appendChild(acts);
      return card;
    }

    async function loadContent(force) {
      if (loaded && !force) return true;
      if (loading) return false;
      loading = true;
      try {
        const { ok, data } = await weFetch("GET", "/api/admin/studio/content");
        const payload = data && (data.data || data);
        if (!ok || !payload || !payload.schema) {
          toast("Couldn't load prompts (is this a local/admin session?)", "warn");
          return false;
        }
        content = {
          prompts: payload.prompts || {},
          prompts_defaults: payload.prompts_defaults || {},
          schema: payload.schema || [],
          groups: payload.groups || {},
          group_blurbs: payload.group_blurbs || {},
          layers: payload.layers || [],
        };
        levels = payload.levels || [];
        identity = payload.identity || {};
        identitySchema = payload.identity_schema || [];
        identityPreview = payload.identity_preview || null;
        // Loading a world or a level can swap the camera out from under a
        // running session; the renderer has to hear about that too.
        if (identityPreview && identityPreview.camera) Camera.apply(identityPreview.camera);
        // Capture the run's starting prompts ONCE, as the Revert target. The
        // cast sheet lives in the same prompt file, so Revert has to restore it
        // too or you'd revert the writing and keep a mismatched protagonist.
        if (!sessionSnapshot) {
          sessionSnapshot = {};
          for (const f of content.schema) sessionSnapshot[f.id] = content.prompts[f.id];
          for (const b of identitySchema) sessionSnapshot[b.id] = content.prompts[b.id];
        }
        loaded = true;
        buildTabs();
        return true;
      } finally {
        loading = false;
      }
    }

    // Persist the given fields; returns {ok, warnings}. Surfaces per-field
    // placeholder warnings inline (never force — protects the running turn).
    async function saveFields(fields) {
      clearWarns();
      if (!Object.keys(fields).length) return { ok: true, warnings: {} };
      const { ok, data } = await weFetch("PUT", "/api/admin/studio/prompts", { data: fields });
      const warnings = (data && data.warnings) || (data && data.data && data.data.warnings) || {};
      if (!ok) {
        showWarns(warnings);
        toast("Fix the highlighted placeholder issue to save.", "warn");
        return { ok: false, warnings };
      }
      // Commit locally: saved values become the new baseline.
      for (const k of Object.keys(fields)) {
        content.prompts[k] = fields[k];
        delete edits[k];
      }
      if (warnings && Object.keys(warnings).length) showWarns(warnings);
      return { ok: true, warnings };
    }

    function dirtyFields() {
      const out = {};
      for (const k of Object.keys(edits)) out[k] = edits[k];
      return out;
    }

    async function applyLive() {
      const fields = dirtyFields();
      if (!Object.keys(fields).length) { toast("No edits to apply."); return; }
      const { ok } = await saveFields(fields);
      if (!ok) return;
      render();
      try { refreshDirective(true); } catch (_) {}
      toast("Applied — live on your next turn.");
    }

    async function saveAndRestart() {
      const fields = dirtyFields();
      const { ok } = await saveFields(fields); // ok even if nothing dirty
      if (!ok) return;
      toast("Saved — restarting the world…");
      close();
      setTimeout(() => { try { resetGame(); } catch (_) {} }, 260);
    }

    async function revertToStart() {
      if (!sessionSnapshot) return;
      const { ok } = await saveFields(Object.assign({}, sessionSnapshot));
      if (!ok) return;
      edits = {};
      await loadContent(true);   // pick the cast sheet back up too
      toast("Reverted to how this run started.");
      render();
    }

    async function resetField(key) {
      const { ok, data } = await weFetch("POST", "/api/admin/studio/prompts/reset", { key });
      const payload = data && (data.data || data);
      if (!ok || !payload || !payload.prompts) { toast("Reset failed.", "warn"); return; }
      content.prompts = payload.prompts;
      delete edits[key];
      render();
      toast("Reset to default.");
    }

    // ── Pop-out prompt editor ─────────────────────────────────────────
    // The side panel is a window onto a prompt; this is the writing surface.
    // Deliberately dev-tool shaped: line numbers, a font-size stepper and
    // soft-wrap toggle (both persisted), live counts, a diff against the
    // factory default, and Ctrl/Cmd+S to commit.

    const WEM_FONT_KEY = "we.editorFontPx";
    const WEM_WRAP_KEY = "we.editorWrap";
    const WEM_LINES_KEY = "we.editorLines";
    const WEM_MIN_FONT = 12, WEM_MAX_FONT = 34;
    const WEM_DEFAULT_FONT = 18;

    let modalKey = null;        // which prompt is open, or null
    let modalOpenValue = "";    // value when it opened (Cancel target)
    let modalFont = WEM_DEFAULT_FONT;
    let modalWrap = true;
    let modalLines = true;
    let modalDiff = false;

    function lsGet(key, fallback) {
      try { const v = localStorage.getItem(key); return v == null ? fallback : v; }
      catch (_) { return fallback; }
    }
    function lsSet(key, value) { try { localStorage.setItem(key, String(value)); } catch (_) {} }

    function loadModalPrefs() {
      modalFont = Math.min(WEM_MAX_FONT, Math.max(WEM_MIN_FONT,
        parseInt(lsGet(WEM_FONT_KEY, String(WEM_DEFAULT_FONT)), 10) || WEM_DEFAULT_FONT));
      modalWrap = lsGet(WEM_WRAP_KEY, "1") !== "0";
      modalLines = lsGet(WEM_LINES_KEY, "1") !== "0";
    }

    function applyModalPrefs() {
      if (!el.wemText) return;
      // The gutter has to match the textarea's metrics exactly or the numbers
      // drift out of alignment as you scroll.
      const px = modalFont + "px";
      const lh = (Math.round(modalFont * 1.65 * 100) / 100) + "px";
      el.wemText.style.fontSize = px;
      el.wemText.style.lineHeight = lh;
      if (el.wemGutter) {
        el.wemGutter.style.fontSize = px;
        el.wemGutter.style.lineHeight = lh;
        el.wemGutter.classList.toggle("hidden", !modalLines);
      }
      el.wemText.classList.toggle("nowrap", !modalWrap);
      if (el.wemWrap) el.wemWrap.classList.toggle("active", modalWrap);
      if (el.wemLines) el.wemLines.classList.toggle("active", modalLines);
      if (el.wemFontDown) el.wemFontDown.disabled = modalFont <= WEM_MIN_FONT;
      if (el.wemFontUp) el.wemFontUp.disabled = modalFont >= WEM_MAX_FONT;
      renderGutter();
    }

    // With soft wrapping on, one logical line can occupy several visual rows, so
    // a naive 1..n gutter would desync. Measure each line's rendered height and
    // pad the gutter to match.
    function renderGutter() {
      if (!el.wemGutter || !el.wemText || !modalLines) return;
      const lines = el.wemText.value.split("\n");
      const lineH = parseFloat(getComputedStyle(el.wemText).lineHeight) || (modalFont * 1.65);
      const probe = gutterProbe();
      let out = "";
      for (let i = 0; i < lines.length; i++) {
        let rows = 1;
        if (modalWrap) {
          probe.textContent = lines[i] || " ";
          rows = Math.max(1, Math.round(probe.getBoundingClientRect().height / lineH));
        }
        out += (i + 1) + "\n".repeat(rows);
      }
      el.wemGutter.textContent = out;
      el.wemGutter.scrollTop = el.wemText.scrollTop;
    }

    // Offscreen mirror of the textarea's text box, used to measure wrapped height.
    let _probe = null;
    function gutterProbe() {
      if (!_probe) {
        _probe = document.createElement("div");
        _probe.setAttribute("aria-hidden", "true");
        _probe.style.cssText =
          "position:absolute;visibility:hidden;pointer-events:none;left:-9999px;top:0;" +
          "white-space:pre-wrap;overflow-wrap:break-word;";
        document.body.appendChild(_probe);
      }
      const cs = getComputedStyle(el.wemText);
      _probe.style.font = cs.font;
      _probe.style.fontSize = cs.fontSize;
      _probe.style.lineHeight = cs.lineHeight;
      _probe.style.letterSpacing = cs.letterSpacing;
      _probe.style.width = (el.wemText.clientWidth
        - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)) + "px";
      return _probe;
    }

    function modalCounts() {
      if (!el.wemCounts || !el.wemText) return;
      const v = el.wemText.value;
      const words = (v.trim().match(/\S+/g) || []).length;
      const dirty = v !== ((content.prompts && content.prompts[modalKey]) || "");
      el.wemCounts.textContent =
        `${v.length.toLocaleString()} chars · ${words.toLocaleString()} words · ` +
        `${v.split("\n").length} lines${dirty ? " · unsaved" : ""}`;
    }

    function modalValidate() {
      if (!el.wemWarn) return;
      const f = schemaFields().find((x) => x.id === modalKey);
      const allowed = new Set((f && f.format_vars) || []);
      const msgs = [];
      if (f && f.format_vars) {
        const found = new Set((el.wemText.value.match(/\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g) || [])
          .map((m) => m.slice(1, -1)));
        const unknown = [...found].filter((v) => !allowed.has(v));
        if (unknown.length) {
          msgs.push("⚠ Unknown placeholder(s) " + unknown.map((u) => "{" + u + "}").join(", ") +
                    " — only " + [...allowed].map((v) => "{" + v + "}").join(", ") +
                    " are recognized here, and saving this will break the prompt at runtime.");
        }
        const scrubbed = el.wemText.value
          .replace(/\{[a-zA-Z_][a-zA-Z0-9_]*\}/g, "").replace(/\{\{/g, "").replace(/\}\}/g, "");
        if (scrubbed.includes("{") || scrubbed.includes("}")) {
          msgs.push("⚠ Stray '{' or '}' that isn't a recognized placeholder — double it ({{ or }}) to keep it literal.");
        }
      }
      el.wemWarn.textContent = msgs.join(" ");
      el.wemWarn.classList.toggle("hidden", !msgs.length);
    }

    function renderModalDiff() {
      if (!el.wemDiffBody) return;
      const a = String(defOf(modalKey) || "").split("\n");
      const b = String(el.wemText.value || "").split("\n");
      const aSet = new Set(a), bSet = new Set(b);
      let html = "";
      if (a.join("\n") === b.join("\n")) {
        html = '<span class="wem-dl wem-dl-same">(identical to the factory default)</span>';
      } else {
        for (let i = 0; i < Math.max(a.length, b.length); i++) {
          const al = a[i], bl = b[i];
          if (al === bl) {
            if (al !== undefined) html += `<span class="wem-dl wem-dl-same">${escapeHtml(al || " ")}</span>`;
          } else {
            if (al !== undefined && !bSet.has(al)) html += `<span class="wem-dl wem-dl-del">− ${escapeHtml(al || " ")}</span>`;
            if (bl !== undefined && !aSet.has(bl)) html += `<span class="wem-dl wem-dl-add">+ ${escapeHtml(bl || " ")}</span>`;
          }
        }
      }
      el.wemDiffBody.innerHTML = html;
    }

    function setModalDiff(on) {
      modalDiff = !!on;
      if (el.wemDiffPane) el.wemDiffPane.classList.toggle("hidden", !modalDiff);
      if (el.wemEditor) el.wemEditor.classList.toggle("with-diff", modalDiff);
      if (el.wemDiffBtn) el.wemDiffBtn.classList.toggle("active", modalDiff);
      if (modalDiff) renderModalDiff();
      renderGutter();
    }

    function openPromptModal(key) {
      const f = schemaFields().find((x) => x.id === key);
      if (!f || !el.wem) return;
      modalKey = key;
      modalOpenValue = valOf(key);

      el.wemTitle.textContent = f.label || key;
      el.wemKey.textContent = key;
      const isRestart = RESTART_KEYS.has(key);
      el.wemChip.className = "we-chip " + (isRestart ? "we-chip-restart" : "we-chip-live");
      el.wemChip.textContent = isRestart ? "restart" : "live";
      el.wemChip.title = isRestart
        ? "Seeds the world — start a fresh run to see it"
        : "Re-steers the sim on the next turn";
      el.wemDesc.textContent = f.description || "";

      // Placeholder chips, for the fields that are run through str.format().
      const vars = (f.format_vars || []);
      el.wemVars.classList.toggle("hidden", !vars.length);
      el.wemVars.querySelectorAll(".wem-var").forEach((n) => n.remove());
      vars.forEach((v) => {
        const b = document.createElement("button");
        b.type = "button"; b.className = "wem-var"; b.textContent = "{" + v + "}";
        b.title = "Insert at the cursor";
        b.addEventListener("click", () => insertAtCursor(el.wemText, "{" + v + "}"));
        el.wemVars.appendChild(b);
      });

      el.wemText.value = modalOpenValue;
      el.wem.classList.remove("hidden");
      requestAnimationFrame(() => el.wem.classList.add("open"));
      applyModalPrefs();
      setModalDiff(false);
      modalCounts();
      modalValidate();
      el.wemText.focus();
      el.wemText.setSelectionRange(0, 0);
      el.wemText.scrollTop = 0;
    }

    function insertAtCursor(ta, text) {
      const s = ta.selectionStart, e = ta.selectionEnd;
      ta.value = ta.value.slice(0, s) + text + ta.value.slice(e);
      ta.selectionStart = ta.selectionEnd = s + text.length;
      ta.focus();
      onModalInput();
    }

    function onModalInput() {
      if (!modalKey) return;
      const base = (content.prompts && content.prompts[modalKey] != null) ? content.prompts[modalKey] : "";
      if (el.wemText.value === base) delete edits[modalKey]; else edits[modalKey] = el.wemText.value;
      modalCounts();
      modalValidate();
      renderGutter();
      if (modalDiff) renderModalDiff();
      refreshDirtyBadge();
    }

    function modalIsOpen() { return !!modalKey; }

    function closePromptModal(restore) {
      if (!modalKey) return;
      const key = modalKey;
      if (restore) {
        // Cancel: put the value back to what it was when the modal opened.
        const base = (content.prompts && content.prompts[key] != null) ? content.prompts[key] : "";
        if (modalOpenValue === base) delete edits[key]; else edits[key] = modalOpenValue;
      }
      modalKey = null;
      el.wem.classList.remove("open");
      setTimeout(() => { if (!modalKey) el.wem.classList.add("hidden"); }, 200);
      render();   // reflect the new value back into the panel's textarea
    }

    async function saveFromModal() {
      if (!modalKey) return;
      const key = modalKey;
      const value = el.wemText.value;
      const { ok, warnings } = await saveFields({ [key]: value });
      if (!ok) {
        // Keep the modal open so the reported problem can be fixed in place.
        const msgs = (warnings && warnings[key]) || [];
        if (msgs.length && el.wemWarn) {
          el.wemWarn.textContent = "⚠ " + msgs.join(" ");
          el.wemWarn.classList.remove("hidden");
        }
        toast("Fix the highlighted placeholder issue to save.", "warn");
        return;
      }
      modalOpenValue = value;
      closePromptModal(false);
      toast("Saved — live on your next turn.");
    }

    async function resetFromModal() {
      if (!modalKey) return;
      const key = modalKey;
      const { ok, data } = await weFetch("POST", "/api/admin/studio/prompts/reset", { key });
      const payload = data && (data.data || data);
      if (!ok || !payload || !payload.prompts) { toast("Reset failed.", "warn"); return; }
      content.prompts = payload.prompts;
      delete edits[key];
      el.wemText.value = valOf(key);
      modalOpenValue = el.wemText.value;
      onModalInput();
      toast("Reset to factory default.");
    }

    function onModalKeydown(e) {
      const meta = e.ctrlKey || e.metaKey;
      if (e.key === "Escape") {
        e.preventDefault(); e.stopPropagation();
        if (el.wemText.value !== modalOpenValue &&
            !confirm("Discard your changes to this prompt?")) return;
        closePromptModal(true);
        return;
      }
      if (meta && (e.key === "s" || e.key === "S")) { e.preventDefault(); saveFromModal(); return; }
      if (meta && e.key === "Enter") { e.preventDefault(); saveFromModal(); return; }
      // Tab indents instead of escaping the field — this is a text editor.
      if (e.key === "Tab") {
        e.preventDefault();
        insertAtCursor(el.wemText, "  ");
      }
    }

    function initModal() {
      if (!el.wem) return;
      loadModalPrefs();
      el.wemText.addEventListener("input", onModalInput);
      el.wemText.addEventListener("scroll", () => {
        if (el.wemGutter) el.wemGutter.scrollTop = el.wemText.scrollTop;
      });
      el.wem.addEventListener("keydown", onModalKeydown);
      // Click the backdrop to cancel, but never a click inside the box.
      el.wem.addEventListener("mousedown", (e) => {
        if (e.target === el.wem) closePromptModal(true);
      });
      el.wemClose.addEventListener("click", () => closePromptModal(true));
      el.wemCancel.addEventListener("click", () => closePromptModal(true));
      el.wemSave.addEventListener("click", saveFromModal);
      el.wemReset.addEventListener("click", () => {
        if (confirm("Restore this prompt's factory default? This overwrites the saved value immediately.")) resetFromModal();
      });
      el.wemWrap.addEventListener("click", () => {
        modalWrap = !modalWrap; lsSet(WEM_WRAP_KEY, modalWrap ? "1" : "0"); applyModalPrefs();
      });
      el.wemLines.addEventListener("click", () => {
        modalLines = !modalLines; lsSet(WEM_LINES_KEY, modalLines ? "1" : "0"); applyModalPrefs();
      });
      el.wemDiffBtn.addEventListener("click", () => setModalDiff(!modalDiff));
      el.wemFontUp.addEventListener("click", () => {
        modalFont = Math.min(WEM_MAX_FONT, modalFont + 1); lsSet(WEM_FONT_KEY, modalFont); applyModalPrefs();
      });
      el.wemFontDown.addEventListener("click", () => {
        modalFont = Math.max(WEM_MIN_FONT, modalFont - 1); lsSet(WEM_FONT_KEY, modalFont); applyModalPrefs();
      });
      window.addEventListener("resize", () => { if (modalIsOpen()) renderGutter(); });
    }

    // ── Editor text size ──────────────────────────────────────────────
    // Every type size in the panel and the pop-out is a multiple of --we-fs, so
    // this one control scales the whole surface. Persisted, because the right
    // size depends on the display and the eyes in front of it.
    const WE_FS_KEY = "we.textScale";
    const WE_FS_MIN = 0.9, WE_FS_MAX = 1.8, WE_FS_STEP = 0.1, WE_FS_DEFAULT = 1.15;
    let textScale = WE_FS_DEFAULT;

    function applyTextScale() {
      document.documentElement.style.setProperty("--we-fs", String(textScale));
      if (el.weFontDown) el.weFontDown.disabled = textScale <= WE_FS_MIN + 1e-9;
      if (el.weFontUp) el.weFontUp.disabled = textScale >= WE_FS_MAX - 1e-9;
      // The pop-out's line-number gutter is measured in pixels, so it has to be
      // re-laid-out whenever surrounding metrics change.
      if (modalIsOpen()) renderGutter();
    }

    function stepTextScale(delta) {
      const next = Math.min(WE_FS_MAX, Math.max(WE_FS_MIN, textScale + delta));
      textScale = Math.round(next * 100) / 100;
      lsSet(WE_FS_KEY, textScale);
      applyTextScale();
      toast("Editor text " + Math.round(textScale * 100) + "%");
    }

    function initTextScale() {
      const stored = parseFloat(lsGet(WE_FS_KEY, ""));
      if (stored >= WE_FS_MIN && stored <= WE_FS_MAX) textScale = stored;
      applyTextScale();
      if (el.weFontUp) el.weFontUp.addEventListener("click", () => stepTextScale(WE_FS_STEP));
      if (el.weFontDown) el.weFontDown.addEventListener("click", () => stepTextScale(-WE_FS_STEP));
      // Double-click either half to get back to the default.
      [el.weFontUp, el.weFontDown].forEach((b) => {
        if (!b) return;
        b.addEventListener("dblclick", (e) => {
          e.preventDefault();
          textScale = WE_FS_DEFAULT;
          lsSet(WE_FS_KEY, textScale);
          applyTextScale();
          toast("Editor text reset");
        });
      });
    }

    // ── Panel width: drag to resize, remembered across sessions ───────
    const WE_WIDTH_KEY = "we.panelWidth";
    const WE_WIDE_KEY = "we.panelWide";

    function applyPanelWidth() {
      const stored = parseInt(lsGet(WE_WIDTH_KEY, ""), 10);
      if (stored && el.worldEditor) {
        el.worldEditor.style.setProperty("--we-width", stored + "px");
      }
      const wide = lsGet(WE_WIDE_KEY, "0") === "1";
      if (el.worldEditor) el.worldEditor.classList.toggle("we-wide", wide);
      if (el.weWide) el.weWide.classList.toggle("active", wide);
    }

    function initResize() {
      if (!el.weResize || !el.worldEditor) return;
      applyPanelWidth();

      if (el.weWide) {
        el.weWide.addEventListener("click", () => {
          const wide = !el.worldEditor.classList.contains("we-wide");
          el.worldEditor.classList.toggle("we-wide", wide);
          el.weWide.classList.toggle("active", wide);
          lsSet(WE_WIDE_KEY, wide ? "1" : "0");
        });
      }

      const onMove = (e) => {
        const w = Math.max(380, Math.min(window.innerWidth * 0.98, e.clientX));
        el.worldEditor.style.setProperty("--we-width", w + "px");
      };
      const onUp = () => {
        document.body.classList.remove("we-resizing");
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        const w = parseInt(el.worldEditor.style.getPropertyValue("--we-width"), 10);
        if (w) lsSet(WE_WIDTH_KEY, w);
      };
      el.weResize.addEventListener("mousedown", (e) => {
        e.preventDefault();
        // Dragging the edge is an explicit width choice; drop WIDE so the two
        // don't fight over the same property.
        el.worldEditor.classList.remove("we-wide");
        if (el.weWide) el.weWide.classList.remove("active");
        lsSet(WE_WIDE_KEY, "0");
        document.body.classList.add("we-resizing");
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
      });
      // Double-click the handle to snap back to the default width.
      el.weResize.addEventListener("dblclick", () => {
        el.worldEditor.style.removeProperty("--we-width");
        try { localStorage.removeItem(WE_WIDTH_KEY); } catch (_) {}
      });
    }

    // ── Cast & Camera tab ─────────────────────────────────────────────
    // Direct your own game: name your character, describe the level, attach
    // reference art, and pick where the camera sits. Everything here compiles
    // into the image and story prompts (see game_identity.py), and the pane at
    // the bottom of each block shows the literal text that gets injected.

    function perspectiveOptions() {
      const block = identitySchema.find((b) => b.id === "camera_perspective");
      const field = block && block.fields.find((f) => f.type === "mode");
      return (field && field.options) || [];
    }

    function plateThumbs(blockId) {
      if (!identityPreview || !identityPreview.reference_images) return [];
      const slot = blockId === "player_character" ? "character"
                 : blockId === "setting_reference" ? "setting" : null;
      return slot ? (identityPreview.reference_images[slot] || []) : [];
    }

    // What a single card compiles to, as ONE block of text with the
    // destinations marked inside it. The server sends a per-block breakdown
    // (see game_identity.block_preview) because the old shared blob made half
    // the fields look dead — appearance, wardrobe, era and palette compile
    // into the IMAGE blocks and never appear in the director's sheet, so
    // typing into them changed nothing on screen. But three separate scrolling
    // panes per card was its own wall of text, so they're joined here.
    function compiledFor(blockId) {
      const p = identityPreview || {};
      const b = (p.blocks || {})[blockId] || {};
      const parts = [];
      if (b.image) parts.push("→ TO THE IMAGE MODEL\n" + b.image);
      if (b.negative) parts.push("→ NEGATIVE PROMPT (recomputed for this camera)\n" + b.negative);
      if (b.narrative) parts.push("→ TO THE WRITER\n" + b.narrative);
      if (!parts.length && p.image_directive && blockId === "camera_perspective") {
        parts.push("→ TO THE IMAGE MODEL\n" + p.image_directive);
      }
      return parts.join("\n\n");
    }

    function notesFor(blockId) {
      const p = identityPreview || {};
      return (p.notes || {})[blockId] || [];
    }

    function castRow(children) {
      const row = document.createElement("div");
      row.className = "we-cast-row";
      children.forEach((c) => row.appendChild(c));
      return row;
    }

    // A field label, with the long help behind an ⓘ rather than printed under
    // every input.
    function castLabelWithHelp(text, help) {
      const row = document.createElement("div");
      row.className = "we-cast-labelrow";
      row.appendChild(castLabel(text));
      const info = infoBtn(text, help);
      if (info) row.appendChild(info);
      return row;
    }

    function castLabel(text) {
      const s = document.createElement("span");
      s.className = "we-cast-label";
      s.textContent = text;
      return s;
    }

    function makeCastField(blockId, field) {
      const value = (identity[blockId] || {})[field.id];

      if (field.type === "toggle") {
        const label = document.createElement("label");
        label.className = "we-cast-toggle";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = !!value;
        cb.addEventListener("change", () => saveIdentity(blockId, { [field.id]: cb.checked }));
        const name = document.createElement("span");
        name.className = "we-cast-toggle-label";
        name.textContent = field.label;
        label.appendChild(cb);
        label.appendChild(name);
        const row = castRow([label]);
        // The ⓘ sits outside the <label> so clicking it can't flip the switch.
        const info = infoBtn(field.label, field.help);
        if (info) row.appendChild(info);
        row.classList.add("we-cast-togglerow");
        return row;
      }

      if (field.type === "mode") {
        const grid = document.createElement("div");
        grid.className = "we-mode-grid";
        perspectiveOptions().forEach((o) => {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "we-mode" + (o.id === (value || "first_person") ? " active" : "");
          const nm = document.createElement("span");
          nm.className = "we-mode-name"; nm.textContent = o.label;
          const tg = document.createElement("span");
          tg.className = "we-mode-tag"; tg.textContent = o.tagline;
          b.appendChild(nm); b.appendChild(tg);
          b.addEventListener("click", () => saveIdentity(blockId, { mode: o.id }));
          grid.appendChild(b);
        });
        return castRow([castLabelWithHelp(field.label, field.help), grid]);
      }

      const isLong = field.type === "longtext";
      const input = document.createElement(isLong ? "textarea" : "input");
      if (!isLong) input.type = "text";
      input.spellcheck = false;
      if (field.placeholder) input.placeholder = field.placeholder;
      input.value = value == null ? "" : String(value);
      // Save on blur, not per-keystroke: each save recompiles the directive
      // server-side and re-renders this panel.
      input.addEventListener("blur", () => {
        const current = (identity[blockId] || {})[field.id] || "";
        if (input.value === current) return;
        saveIdentity(blockId, { [field.id]: input.value });
      });
      if (!isLong) {
        input.addEventListener("keydown", (e) => { if (e.key === "Enter") input.blur(); });
      }
      return castRow([castLabelWithHelp(field.label, field.help), input]);
    }

    function makeCastBlock(block) {
      const wrap = document.createElement("div");
      wrap.className = "we-block";
      wrap.dataset.block = block.id;

      const top = document.createElement("div");
      top.className = "we-block-head";
      const label = document.createElement("span");
      label.className = "we-block-label";
      label.textContent = block.label;
      top.appendChild(label);

      const about = infoBtn(block.label, block.description);
      if (about) top.appendChild(about);
      wrap.appendChild(top);

      const essential = block.fields.filter((f) => f.tier !== "advanced");
      const advanced = block.fields.filter((f) => f.tier === "advanced");
      essential.forEach((f) => wrap.appendChild(makeCastField(block.id, f)));
      if (advanced.length) {
        wrap.appendChild(makeAdvancedToggle(advanced.length, "field"));
        if (showAdvanced) advanced.forEach((f) => wrap.appendChild(makeCastField(block.id, f)));
      }

      if (block.supports_images) wrap.appendChild(makePlateZone(block));

      notesFor(block.id).forEach((note) => {
        const warn = document.createElement("div");
        warn.className = "we-warn";
        warn.textContent = note;
        wrap.appendChild(warn);
      });

      // The exact text this compiles to. Worth being able to check; not worth
      // reading every time you open the panel.
      const compiled = compiledFor(block.id);
      if (compiled) {
        const more = document.createElement("details");
        more.className = "we-more-info we-more-compiled";
        const sum = document.createElement("summary");
        sum.textContent = "What the model receives";
        const pane = document.createElement("div");
        pane.className = "we-compiled";
        pane.textContent = compiled;
        more.appendChild(sum);
        more.appendChild(pane);
        wrap.appendChild(more);
      }
      return wrap;
    }

    function makePlateZone(block) {
      const slot = block.id === "player_character" ? "character" : "setting";
      const holder = document.createElement("div");
      holder.className = "we-plate-zone";

      const head = document.createElement("div");
      head.className = "we-plate-head";
      head.innerHTML = '<span class="we-plate-title">' +
        esc(block.images_label || "Reference") + "</span>";
      const hint = infoBtn(block.images_label || "Reference", block.images_hint);
      if (hint) head.appendChild(hint);
      holder.appendChild(head);

      const file = document.createElement("input");
      file.type = "file"; file.accept = "image/*"; file.style.display = "none";
      file.addEventListener("change", () => {
        if (file.files[0]) uploadPlate(file.files[0], slot);
        file.value = "";
      });

      // One grid: the plates you have, then a tile to add another. The old
      // version put a full-width "Drop an image here, or tap to choose one"
      // sentence under every block, which read as instructions rather than a
      // place to put a picture.
      const grid = document.createElement("div");
      grid.className = "we-plates";
      plateThumbs(block.id).forEach((pl) => {
        const cell = document.createElement("figure");
        cell.className = "we-plate";
        const img = document.createElement("img");
        img.src = pl.url; img.alt = pl.label || "Reference image";
        img.loading = "lazy";
        const rm = document.createElement("button");
        rm.type = "button"; rm.className = "we-plate-remove";
        rm.innerHTML = "&#10005;";
        rm.title = "Remove this reference";
        rm.addEventListener("click", () => deletePlate(pl.id));
        cell.appendChild(img); cell.appendChild(rm);
        grid.appendChild(cell);
      });

      const add = document.createElement("button");
      add.type = "button";
      add.className = "we-plate-add";
      add.innerHTML = '<span class="we-plate-add-mark" aria-hidden="true">+</span>' +
                      '<span class="we-plate-add-text">Add reference</span>';
      add.title = "Choose an image, or drop one here";
      add.addEventListener("click", () => file.click());
      ["dragenter", "dragover"].forEach((ev) => grid.addEventListener(ev, (e) => {
        e.preventDefault(); grid.classList.add("dragover");
      }));
      ["dragleave", "drop"].forEach((ev) => grid.addEventListener(ev, (e) => {
        e.preventDefault(); grid.classList.remove("dragover");
      }));
      grid.addEventListener("drop", (e) => {
        const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
        if (f) uploadPlate(f, slot);
      });
      grid.appendChild(add);

      holder.appendChild(grid);
      holder.appendChild(file);
      return holder;
    }

    // The short forms of the cast sheet that go to the surfaces which can't
    // take the full directive — the live world model, the vision loop that
    // decides what the NEXT frame looks like, camp, and anyone you talk to.
    // Behind the advanced switch: it answers "does this reach anything beyond
    // the still image?", which is worth being able to check and not worth
    // reading every time.
    function makeReachBlock() {
      const c = (identityPreview || {}).compact || {};
      const rows = [
        ["Live world model & video", [c.vantage, c.place_line].filter(Boolean).join(" — ")],
        ["Vision analysis & scans", c.scene_grounding],
        ["Characters you talk to", c.protagonist_line],
      ].filter((r) => r[1]);
      if (!rows.length) return null;

      const wrap = document.createElement("div");
      wrap.className = "we-block";
      const top = document.createElement("div");
      top.className = "we-block-head";
      const label = document.createElement("span");
      label.className = "we-block-label";
      label.textContent = "Where else this reaches";
      top.appendChild(label);
      const about = infoBtn("Where else this reaches",
        "The same sheet, compressed for the surfaces that only get a sentence or " +
        "two: the live world model, the vision loop that decides what the next " +
        "frame looks like, camp, and anyone you talk to.");
      if (about) top.appendChild(about);
      wrap.appendChild(top);

      rows.forEach(([name, text]) => {
        wrap.appendChild(castLabel(name));
        const pane = document.createElement("div");
        pane.className = "we-compiled";
        pane.textContent = text;
        wrap.appendChild(pane);
      });
      return wrap;
    }

    function makeCastReset() {
      const row = document.createElement("div");
      row.className = "we-cast-row";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "we-btn";
      btn.textContent = "Reset Cast & Camera";
      btn.title = "Back to first person, nobody, nowhere";
      btn.addEventListener("click", async () => {
        if (!window.confirm("Clear your character, level, and camera back to the shipped defaults?")) return;
        const { ok, data } = await weFetch("POST", "/api/admin/studio/identity/reset", {});
        const payload = data && (data.data || data);
        if (!ok || !payload) { toast("Couldn't reset.", "warn"); return; }
        applyIdentityPayload(payload);
        toast("Cast & Camera reset.");
      });
      row.appendChild(btn);
      return row;
    }

    // `only` limits rendering to the spec blocks the active layer owns, so the
    // camera lands in Game, the level plate in Level, and the character sheet in
    // Character, instead of all three stacked in one "Cast & Camera" scroll.
    function renderCast(only) {
      if (!el.weCast) return;
      el.weCast.innerHTML = "";
      const wanted = only && only.length
        ? identitySchema.filter((b) => only.indexOf(b.id) !== -1)
        : identitySchema;
      wanted.forEach((block) => el.weCast.appendChild(makeCastBlock(block)));
      if (showAdvanced) {
        const reach = makeReachBlock();
        if (reach) el.weCast.appendChild(reach);
      }
      el.weCast.appendChild(makeCastReset());
    }

    function applyIdentityPayload(data) {
      if (!data) return;
      if (data.identity) identity = data.identity;
      if (data.preview) identityPreview = data.preview;
      // The camera is not just prompt text: the live world is BUILT with a
      // perspective and re-steered with camera-specific language, so a save
      // has to reach the renderer too or picking third person mid-run changes
      // the stills and leaves the video exactly as it was.
      if (data.preview && data.preview.camera) Camera.apply(data.preview.camera);
      render();
      try { refreshDirective(true); } catch (_) {}
    }

    async function saveIdentity(blockId, patch) {
      const { ok, data } = await weFetch("PUT", "/api/admin/studio/identity", { [blockId]: patch });
      const payload = data && (data.data || data);
      if (!ok || !payload) { toast("Couldn't save that.", "warn"); return; }
      applyIdentityPayload(payload);
      toast("Saved — live on your next turn.");
    }

    function uploadPlate(file, slot) {
      const reader = new FileReader();
      reader.onload = async () => {
        toast("Uploading reference…");
        const { ok, data } = await weFetch("POST", "/api/admin/studio/reference", {
          image: reader.result, kind: slot, label: file.name,
        });
        const payload = data && (data.data || data);
        if (!ok || !payload) {
          toast((data && data.error) || "Upload failed.", "warn");
          return;
        }
        applyIdentityPayload(payload);
        toast("Reference added.");
      };
      reader.readAsDataURL(file);
    }

    async function deletePlate(refId) {
      const { ok, data } = await weFetch("DELETE", "/api/admin/studio/reference", { id: refId });
      const payload = data && (data.data || data);
      if (!ok || !payload) { toast("Delete failed.", "warn"); return; }
      applyIdentityPayload(payload);
      toast("Reference removed.");
    }

    // ── Worlds tab ────────────────────────────────────────────────────
    let worlds = [];
    async function loadWorlds() {
      const { ok, data } = await weFetch("GET", "/api/admin/studio/worlds");
      const payload = data && (data.data || data);
      worlds = (ok && payload && payload.worlds) ? payload.worlds : [];
    }
    function renderWorlds() {
      if (!el.weWorldsList) return;
      el.weWorldsList.innerHTML = "";
      if (!worlds.length) {
        const li = document.createElement("li");
        li.className = "we-empty";
        li.textContent = "No saved worlds yet. Tweak the prompts, then save this one.";
        el.weWorldsList.appendChild(li);
        return;
      }
      worlds.forEach((w) => {
        const li = document.createElement("li");
        li.className = "we-world";
        const info = document.createElement("div");
        info.className = "we-world-main";
        const nm = document.createElement("div");
        nm.className = "we-world-name"; nm.textContent = w.name || w.slug;
        const meta = document.createElement("div");
        meta.className = "we-world-meta";
        meta.textContent = (w.field_count || 0) + " prompts" + (w.note ? " · " + w.note : "");
        info.appendChild(nm); info.appendChild(meta);
        const actions = document.createElement("div");
        actions.className = "we-world-acts";
        const load = document.createElement("button");
        load.className = "we-btn we-btn-primary"; load.type = "button"; load.textContent = "Load";
        load.title = "Apply this world's prompts (live, next turn)";
        load.addEventListener("click", () => loadWorld(w.slug, false));
        const play = document.createElement("button");
        play.className = "we-btn we-btn-accent"; play.type = "button"; play.textContent = "Play";
        play.title = "Load this world and start a fresh run";
        play.addEventListener("click", () => loadWorld(w.slug, true));
        const del = document.createElement("button");
        del.className = "we-btn we-btn-ghost"; del.type = "button"; del.textContent = "✕";
        del.title = "Delete this world";
        del.addEventListener("click", () => deleteWorld(w.slug, w.name));
        actions.appendChild(load); actions.appendChild(play); actions.appendChild(del);
        li.appendChild(info); li.appendChild(actions);
        el.weWorldsList.appendChild(li);
      });
    }
    async function saveWorld() {
      const name = (el.weWorldName && el.weWorldName.value || "").trim();
      if (!name) { toast("Name your world first.", "warn"); return; }
      // Persist any unsaved edits first so the snapshot reflects what's on screen.
      if (anyDirty()) { const { ok } = await saveFields(dirtyFields()); if (!ok) return; }
      const { ok } = await weFetch("POST", "/api/admin/studio/worlds", { name });
      if (!ok) { toast("Couldn't save world.", "warn"); return; }
      if (el.weWorldName) el.weWorldName.value = "";
      await loadWorlds(); renderWorlds();
      toast("World saved: " + name);
    }
    async function loadWorld(slug, restart) {
      const { ok, data } = await weFetch("POST", "/api/admin/studio/worlds/load", { slug });
      const payload = data && (data.data || data);
      if (!ok || !payload) { toast("Couldn't load world.", "warn"); return; }
      edits = {};
      // Refetch rather than patching from the response: a world carries the
      // cast sheet as well as the prompts, and the compiled directive preview
      // has to be recomputed server-side from the newly applied spec.
      await loadContent(true);
      render();
      if (restart) {
        toast("Loaded — launching…");
        close();
        setTimeout(() => { try { resetGame(); } catch (_) {} }, 260);
      } else {
        toast("Loaded — live on your next turn.");
        try { refreshDirective(true); } catch (_) {}
      }
    }
    async function deleteWorld(slug, name) {
      const { ok } = await weFetch("DELETE", "/api/admin/studio/worlds", { slug });
      if (!ok) { toast("Delete failed.", "warn"); return; }
      await loadWorlds(); renderWorlds();
      toast("Deleted " + (name || slug));
    }

    // ── Inline validation warnings ────────────────────────────────────
    function clearWarns() {
      if (!el.weFields) return;
      el.weFields.querySelectorAll(".we-warn").forEach((w) => { w.classList.add("hidden"); w.textContent = ""; });
      el.weFields.querySelectorAll(".we-field").forEach((f) => f.classList.remove("has-warn"));
    }
    function showWarns(warnings) {
      if (!warnings || !el.weFields) return;
      Object.keys(warnings).forEach((key) => {
        const card = el.weFields.querySelector('.we-field[data-key="' + key + '"]');
        if (!card) return;
        const w = card.querySelector(".we-warn");
        if (w) { w.textContent = (warnings[key] || []).join(" "); w.classList.remove("hidden"); }
      });
    }

    // ── Open / close / toggle ─────────────────────────────────────────
    let open_ = false;
    async function open() {
      if (open_) return;
      open_ = true;
      document.body.classList.add("world-editor-on");
      if (el.worldEditor) { el.worldEditor.classList.remove("hidden"); el.worldEditor.setAttribute("aria-hidden", "false"); }
      if (el.btnEditor) el.btnEditor.classList.add("active");
      // The CONTROLS switch is local (no server content needed), so paint it
      // before the prompt fetch — it must work even if that request fails.
      try { InputProfileUi.paint(); } catch (_) {}
      const ok = await loadContent(false);
      if (ok) { await loadWorlds(); render(); }
    }
    function close() {
      if (!open_) return;
      closePop();
      open_ = false;
      document.body.classList.remove("world-editor-on");
      if (el.btnEditor) el.btnEditor.classList.remove("active");
      if (el.worldEditor) {
        el.worldEditor.setAttribute("aria-hidden", "true");
        setTimeout(() => { if (!open_) el.worldEditor.classList.add("hidden"); }, 360);
      }
    }
    function toggle() { open_ ? close() : open(); }
    function isOpen() { return open_; }

    function init() {
      // Any click that isn't the popover or its own ⓘ dismisses it.
      if (el.worldEditor) {
        el.worldEditor.addEventListener("click", (e) => {
          if (!popOwner) return;
          if (e.target.closest(".we-pop") || e.target.closest(".we-info")) return;
          closePop();
        });
      }
      if (el.weClose) el.weClose.addEventListener("click", close);
      if (el.weApply) el.weApply.addEventListener("click", applyLive);
      if (el.weRestart) el.weRestart.addEventListener("click", saveAndRestart);
      if (el.weRevert) el.weRevert.addEventListener("click", revertToStart);
      initTextScale();
      initResize();
      initModal();
      if (el.weWorldSave) el.weWorldSave.addEventListener("click", saveWorld);
      if (el.weWorldName) el.weWorldName.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); saveWorld(); }
      });
    }

    return { init, open, close, toggle, isOpen, modalIsOpen, openPrompt: openPromptModal };
  })();

  // ------------------------------------------------------------------
  // Image-generator switcher — a player-facing menu (IMAGE button / I key)
  // to pick the model that DRAWS the world. Mirrors the live world-model
  // switcher, but for still frames. Reads/writes the same preset system the
  // admin dashboard + Discord /ai_switch use, so a change is live on the next
  // generated frame. A failed/expensive provider auto-falls back to Gemini in
  // engine._gen_image, so switching can never blank the world.
  // ------------------------------------------------------------------
  const ImageModel = (function () {
    let presets = [];
    let activePreset = null;
    let loaded = false;
    let switching = false;

    function visible() {
      return document.body.classList.contains("img-model-on");
    }
    function applyVisibility(on) {
      document.body.classList.toggle("img-model-on", on);
      if (el.btnImgModel) el.btnImgModel.classList.toggle("active", on);
    }
    async function toggle() {
      const on = !visible();
      applyVisibility(on);
      if (on) {
        // Refresh on open so it always reflects the live server config
        // (another player, the admin dashboard, or Discord may have changed it).
        await load();
      }
    }
    function hide() { applyVisibility(false); }

    function speedPips(speed) {
      let out = "";
      for (let i = 1; i <= 5; i++) out += `<span class="img-model-pip ${i <= (speed || 0) ? "on" : ""}"></span>`;
      return out;
    }

    async function load() {
      try {
        const r = await fetch("/api/ai/config");
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        presets = data.presets || [];
        activePreset = data.active_preset || null;
        loaded = true;
        render();
      } catch (err) {
        console.warn("[IMG MODEL] load failed:", err);
        if (el.imgModelList) {
          el.imgModelList.innerHTML =
            '<div class="img-model-opt-blurb" style="padding:4px 2px;">Couldn\u2019t load models. Try again.</div>';
        }
      }
    }

    function render() {
      const wrap = el.imgModelList;
      if (!wrap) return;
      if (!presets.length) {
        wrap.innerHTML = '<div class="img-model-opt-blurb" style="padding:4px 2px;">No models available.</div>';
        return;
      }
      wrap.innerHTML = "";
      presets.forEach((p) => {
        const isActive = p.name === activePreset;
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "img-model-opt" + (isActive ? " active" : "");
        btn.dataset.preset = p.name;
        btn.disabled = isActive;
        btn.innerHTML =
          `<span class="img-model-opt-top">` +
            `<span class="img-model-opt-name">${esc(p.label)}</span>` +
            (isActive ? `<span class="img-model-opt-active-flag">On</span>` :
              (p.latency ? `<span class="img-model-opt-latency">${esc(p.latency)}</span>` : "")) +
          `</span>` +
          `<span class="img-model-opt-sub">${esc(p.image_provider)} / ${esc(p.image_model)}</span>` +
          (p.blurb ? `<span class="img-model-opt-blurb">${esc(p.blurb)}</span>` : "") +
          `<span class="img-model-speed" title="Relative speed">${speedPips(p.speed)}</span>`;
        if (!isActive) btn.addEventListener("click", () => switchTo(p.name));
        wrap.appendChild(btn);
      });
    }

    function esc(s) {
      return String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    async function switchTo(name) {
      if (switching || !name || name === activePreset) return;
      switching = true;
      const btn = el.imgModelList && el.imgModelList.querySelector(`[data-preset="${name}"]`);
      if (btn) btn.classList.add("switching");
      try { Haptics.tap(); } catch (_) {}
      try {
        const r = await fetch("/api/ai/switch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ preset: name }),
        });
        const data = await r.json();
        if (!r.ok || data.status !== "ok") throw new Error(data.error || "HTTP " + r.status);
        activePreset = name;
        const label = (presets.find((p) => p.name === name) || {}).label || name;
        render();
        showRendererToast("IMAGE MODEL → " + label);
        try { RtLog.push("ok", "image model", label + " (" + data.image_provider + "/" + data.image_model + ")"); } catch (_) {}
      } catch (err) {
        console.warn("[IMG MODEL] switch failed:", err);
        showRendererToast("Switch failed");
        if (btn) btn.classList.remove("switching");
      } finally {
        switching = false;
      }
    }

    function init() {
      if (el.imgModelHide) el.imgModelHide.addEventListener("click", hide);
      // Lazy: presets load the first time the menu is opened.
    }

    return { toggle, hide, load, init, visible };
  })();

  // Small transient on-screen note so it's obvious which renderer is active
  // (useful while testing / toggling with the G key).
  let _rendererToastTimer = null;
  function showRendererToast(text, dur) {
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
    _rendererToastTimer = setTimeout(() => toast.classList.remove("show"), dur || 2200);
  }

  // ------------------------------------------------------------------
  // InputBindings — the CONTROL MODES for the realtime EXPLORE instrument.
  //
  // Physical inputs map to SEMANTIC drive tokens the Movement layer already
  // understands (fwd / back / strafeL / strafeR / lookL / lookR / pitchUp /
  // pitchDown). Two modes ship, toggled live in the WORLD EDITOR:
  //
  //   DOOM  W forward · S back · A look left · D look right   (no mouse look)
  //   FPS   W forward · S back · A strafe left · D strafe right + MOUSE look
  //
  // DOOM is the default because it needs no pointer capture and works on any
  // setup. Adding a mode = one entry in PROFILES; nothing in the drive loop
  // changes. The choice persists per browser in localStorage.
  // ------------------------------------------------------------------
  const InputBindings = (function () {
    const LS_KEY = "input_profile";
    // Mouse-look tuning. The world model only takes a HELD look direction, which
    // keeps rotating until stopped — so "turn while the mouse is moving" is the
    // wrong contract: hand tremor alone sustains it and the camera spins forever.
    // Instead every mouse delta deposits a finite BUDGET of turn (in px) that
    // bleeds off over time. Rotation is therefore proportional to how far you
    // actually moved the mouse, always winds down on its own, and jitter (which
    // nets ~zero and drains away) can never hold the camera.
    const MOUSE_SUBTLE = {
      sensitivity: 3.0,    // mouse px -> queued turn px (tunable in the editor)
      holdPx: 12,          // queued turn under this = camera at rest
      drainPxPerMs: 0.4,   // bleed-off rate
      // The ceiling is expressed in TIME, not pixels: however sensitive the
      // mouse is, one wild flick can never buy more than this much rotation.
      // Raising sensitivity therefore reaches a given turn SOONER rather than
      // spinning for longer — which is what "more sensitive" should mean.
      maxHoldMs: 600,
      maxIntensity: 0.5,   // caps the turn-rate contribution (subtle)
      invertY: false,
    };
    const SENS_KEY = "input_look_sens";
    const SENS_MIN = 0.5, SENS_MAX = 12;
    let sensitivity = MOUSE_SUBTLE.sensitivity;
    const PROFILES = {
      // Doom: the tank-style scheme — A/D swing the view, no mouse capture.
      doom: {
        label: "DOOM",
        hint: "W/S move · A/D turn · Q/E strafe",
        mouseLook: false,
        mouse: null,
        keys: {
          w: "fwd", s: "back",
          a: "lookL", d: "lookR",
          q: "strafeL", e: "strafeR",
          arrowleft: "lookL", arrowright: "lookR",
          arrowup: "pitchUp", arrowdown: "pitchDown",
        },
      },
      // FPS: WASD is pure locomotion, the MOUSE steers the camera.
      fps: {
        label: "FPS",
        hint: "WASD move · drag the world to look (double-click to capture, Esc frees)",
        mouseLook: true,
        mouse: MOUSE_SUBTLE,
        keys: {
          w: "fwd", s: "back",
          a: "strafeL", d: "strafeR",
          q: "strafeL", e: "strafeR",
          arrowleft: "lookL", arrowright: "lookR",
          arrowup: "pitchUp", arrowdown: "pitchDown",
        },
      },
    };
    const ORDER = ["doom", "fps"];
    let name = "doom";

    function load() {
      try {
        const v = localStorage.getItem(LS_KEY);
        if (v && PROFILES[v]) name = v;
      } catch (_) {}
      try {
        const s = parseFloat(localStorage.getItem(SENS_KEY));
        if (isFinite(s)) sensitivity = clampSens(s);
      } catch (_) {}
    }
    function clampSens(v) {
      v = Number(v);
      if (!isFinite(v)) return MOUSE_SUBTLE.sensitivity;
      return Math.min(SENS_MAX, Math.max(SENS_MIN, Math.round(v * 10) / 10));
    }
    function setSensitivity(v) {
      const next = clampSens(v);
      if (next === sensitivity) return sensitivity;
      sensitivity = next;
      try { localStorage.setItem(SENS_KEY, String(sensitivity)); } catch (_) {}
      return sensitivity;
    }
    function profile() { return PROFILES[name] || PROFILES.doom; }
    function setProfile(id) {
      if (!PROFILES[id] || id === name) return false;
      name = id;
      try { localStorage.setItem(LS_KEY, name); } catch (_) {}
      // Leaving a mouse-look mode must drop any pointer capture, and any keys
      // held under the OLD map have to be released or they stay stuck down.
      try { MouseLook.onModeChanged(); } catch (_) {}
      try { Movement.releaseAll(); } catch (_) {}
      try { Movement.refreshHints(); } catch (_) {}
      try { InputProfileUi.paint(); } catch (_) {}
      return true;
    }
    function keyFor(key) {
      const map = profile().keys || {};
      return map[(key || "").toLowerCase()] || null;
    }

    load();
    return {
      current: () => name,
      profile: profile,
      list: () => ORDER.map((id) => ({ id: id, label: PROFILES[id].label, hint: PROFILES[id].hint })),
      setProfile: setProfile,
      keyFor: keyFor,
      mouseLookEnabled: () => !!profile().mouseLook,
      // The live mouse config: profile defaults, the player's sensitivity, and
      // the pixel ceiling derived from the time ceiling.
      mouseConfig: () => {
        const base = profile().mouse || MOUSE_SUBTLE;
        const drain = base.drainPxPerMs || 0.4;
        return Object.assign({}, base, {
          sensitivity: sensitivity,
          maxBudgetPx: (base.maxHoldMs || 600) * drain,
        });
      },
      sensitivity: () => sensitivity,
      setSensitivity: setSensitivity,
      sensitivityRange: () => ({ min: SENS_MIN, max: SENS_MAX }),
      hint: () => profile().hint || "",
    };
  })();
  try { window.__InputBindings = InputBindings; } catch (_) {}

  // ------------------------------------------------------------------
  // MouseLook — the FPS camera steer (FPS mode only).
  //
  // World models take DISCRETE held look directions (keep turning until told to
  // stop), not angles. So mouse motion deposits a finite TURN BUDGET that drains
  // with time: while budget remains we hold that direction, and when it runs out
  // we stop. Moving the mouse further turns further, holding still winds the turn
  // down within a few hundred ms, and flicking back cancels the queued turn
  // instead of fighting it. Two ways in, because a browser can always refuse
  // pointer capture:
  //
  //   • Drag-look (primary) — hold the left button on the world and move. Always
  //     works, never touches the cursor, and a click with no movement does
  //     nothing, so it can't swallow a click the game needed.
  //   • Pointer lock (opt-in) — DOUBLE-click the world to capture the cursor and
  //     steer continuously (true FPS); Esc frees it. Deliberately NOT bound to a
  //     single click: that silently stole the cursor from ordinary clicks, which
  //     reads as the game freezing.
  //
  // Everything here no-ops outside FPS mode / outside live video.
  // ------------------------------------------------------------------
  const MouseLook = (function () {
    // Real UI that must keep its own clicks/drags. Anything NOT matching this
    // counts as "the world" (the video, the still layers, the page backdrop).
    const UI = "button, a, input, textarea, select, option, label, " +
      "[role='button'], [role='dialog'], [contenteditable='true'], " +
      "#move-pad, #verb-bar, #control-rail, #menu-toggle, #rt-log, #story-log, " +
      "#img-model, #agent-log, #world-editor, #we-modal, #talk-overlay, " +
      "#touch-layer, #scan-layer, #action-wheel, #objectives-hud, #shot-tally, " +
      "#investigations-tray, #case-overlay, #ceremony, #processing-veil, " +
      "#scan-tutorial, #narrator-bar, #guide-thumb, #capture-thumb, " +
      "#moment-overlay, .renderer-toast, #tape-overlay";

    // Movement past this makes a gesture a LOOK, not a tap.
    const DRAG_SLOP_PX = 8;

    let locked = false;         // pointer lock held
    let dragging = false;       // left button held on the world
    let budgetX = 0;            // signed px of turn still owed to the player
    let budgetY = 0;
    let drainedAt = 0;          // wall clock of the last budget bleed
    let lastClient = null;      // previous cursor pos (drag-look delta source)
    let downAt = null;          // where the button went down (drag vs click)
    let draggedFar = false;     // this gesture moved -> it steered, it didn't tap
    let hinted = false;
    let reticle = null;

    function modeOn() { return InputBindings.mouseLookEnabled(); }
    function allowed() {
      if (!modeOn()) return false;
      try { if (!Movement.enabled()) return false; } catch (_) { return false; }
      return true;
    }
    // Another instrument owns the pointer right now — never capture over it.
    function uiBusy() {
      try {
        if (state.touchMode) return true;                       // photo aiming
        if (typeof tapeIsOpen === "function" && tapeIsOpen()) return true;
        if (el.actionWheel && el.actionWheel.classList.contains("fw-open")) return true;
        if (el.worldEditor && !el.worldEditor.classList.contains("hidden")) return true;
        if (el.talkOverlay && !el.talkOverlay.classList.contains("hidden")) return true;
        if (document.body.classList.contains("awaiting-first-scene")) return true;
      } catch (_) {}
      return false;
    }
    function isUi(t) {
      try { return !!(t && t.closest && t.closest(UI)); } catch (_) { return true; }
    }

    function ensureReticle() {
      if (reticle && reticle.isConnected) return reticle;
      reticle = document.getElementById("mouse-look-reticle");
      if (!reticle) {
        reticle = document.createElement("div");
        reticle.id = "mouse-look-reticle";
        reticle.className = "mouse-look-reticle";
        reticle.setAttribute("aria-hidden", "true");
        reticle.innerHTML = '<span class="mlr-h"></span><span class="mlr-v"></span><span class="mlr-dot"></span>';
        document.body.appendChild(reticle);
      }
      return reticle;
    }
    function paint() {
      const on = locked || dragging;
      document.body.classList.toggle("mouse-looking", on);
      document.body.classList.toggle("mouse-look-locked", locked);
      ensureReticle().classList.toggle("on", on);
    }

    function reset() {
      budgetX = 0;
      budgetY = 0;
      drainedAt = 0;
      lastClient = null;
    }

    function clamp(v, lim) { return v < -lim ? -lim : v > lim ? lim : v; }
    // Bleed the queued turn toward zero, never past it.
    function bleed(v, amount) {
      if (v > 0) return Math.max(0, v - amount);
      if (v < 0) return Math.min(0, v + amount);
      return 0;
    }

    // Bank the turn this mouse movement earned. Opposite motion SUBTRACTS, so
    // flicking back cancels a queued turn rather than queueing a fight.
    function feed(dx, dy) {
      if (!dx && !dy) return;
      const cfg = InputBindings.mouseConfig();
      const s = cfg.sensitivity || 1;
      const lim = cfg.maxBudgetPx || 140;
      drain(); // charge elapsed time before depositing, so rate stays honest
      budgetX = clamp(budgetX + dx * s, lim);
      budgetY = clamp(budgetY + (cfg.invertY ? -dy : dy) * s, lim);
      // Keep the drive loop warm so the look lands on the very next tick.
      try { Movement.noteActivity(); } catch (_) {}
    }

    // Time-based, so it behaves identically however often it's called.
    function drain() {
      const now = Date.now();
      if (!drainedAt) { drainedAt = now; return; }
      const dt = now - drainedAt;
      if (dt <= 0) return;
      drainedAt = now;
      const cfg = InputBindings.mouseConfig();
      const amount = (cfg.drainPxPerMs || 0.4) * dt;
      budgetX = bleed(budgetX, amount);
      budgetY = bleed(budgetY, amount);
    }

    // The current look intent, or null once the budget is spent. BOTH axes can be
    // owed at once (up AND left), so we report both plus how much each is owed —
    // the drive layer decides whether the live model can hold a real diagonal or
    // has to time-slice one.
    function intent() {
      if (!modeOn() || (!locked && !dragging)) return null;
      drain();
      const cfg = InputBindings.mouseConfig();
      const hold = cfg.holdPx || 12;
      const lim = cfg.maxBudgetPx || 240;
      const ax = Math.abs(budgetX), ay = Math.abs(budgetY);
      if (Math.max(ax, ay) < hold) return null;
      const hOn = ax >= hold, vOn = ay >= hold;
      const strength = Math.min(cfg.maxIntensity || 0.5, Math.max(ax, ay) / lim);
      return {
        lookH: hOn ? (budgetX < 0 ? "left" : "right") : "idle",
        lookV: vOn ? (budgetY < 0 ? "up" : "down") : "idle",
        hMag: hOn ? Math.min(1, ax / lim) : 0,
        vMag: vOn ? Math.min(1, ay / lim) : 0,
        intensity: strength,
      };
    }
    function isActive() { return !!intent(); }
    function isEngaged() { return locked || dragging; }

    // ---- pointer lock ----
    function requestLock() {
      if (locked || !allowed() || uiBusy()) return;
      // Capturing the cursor over a world that hasn't revealed yet is how a slow
      // first scene turns into "the game froze on black".
      try {
        if (!window.ReactorRenderer.isShowing || !window.ReactorRenderer.isShowing()) return;
      } catch (_) { return; }
      const req = document.body.requestPointerLock || document.body.mozRequestPointerLock;
      if (!req) return;
      try { req.call(document.body); } catch (_) {}
    }
    function releaseLock() {
      dragging = false;
      reset();
      if (locked) {
        const exit = document.exitPointerLock || document.mozExitPointerLock;
        if (exit) { try { exit.call(document); } catch (_) {} }
      }
      paint();
      try { Movement.onLookReleased(); } catch (_) {}
    }
    function onLockChange() {
      locked = !!(document.pointerLockElement || document.mozPointerLockElement);
      reset();
      paint();
      if (locked) {
        RtLog.push("dim", "\u25CE mouse look \u00B7 captured");
      } else {
        try { Movement.onLookReleased(); } catch (_) {}
      }
    }

    // ---- events ----
    function onMouseDown(e) {
      if (!allowed() || uiBusy()) return;
      if (e.button !== 0 || isUi(e.target)) return;
      if (locked) return;
      // Drag-look starts immediately; the click that follows asks for the lock,
      // so a plain click upgrades to full capture and a drag still steers if
      // the browser refuses.
      dragging = true;
      lastClient = { x: e.clientX, y: e.clientY };
      downAt = { x: e.clientX, y: e.clientY };
      draggedFar = false;
      paint();
      if (!hinted) {
        hinted = true;
        showRendererToast("Mouse look \u2014 drag to steer \u00B7 double-click to capture the cursor", 3200);
      }
    }
    function onMouseUp() {
      if (!dragging) return;
      dragging = false;
      reset();
      paint();
      try { Movement.onLookReleased(); } catch (_) {}
    }
    function onMouseMove(e) {
      if (!modeOn()) return;
      if (locked) { feed(e.movementX || 0, e.movementY || 0); return; }
      if (!dragging) { lastClient = null; return; }
      if (!allowed()) return;
      if (lastClient) feed(e.clientX - lastClient.x, e.clientY - lastClient.y);
      lastClient = { x: e.clientX, y: e.clientY };
      if (downAt && Math.hypot(e.clientX - downAt.x, e.clientY - downAt.y) > DRAG_SLOP_PX) {
        draggedFar = true;
      }
    }

    // Tapping the world fires a PAID detection pass (onWorldTap -> triggerScan),
    // and the mouseup ending a look-drag produces a real click on the scene — so
    // steering the camera was buying a scan every time you let go. A gesture that
    // MOVED now eats its own click. Also swallowed: the second click of the
    // double-click that takes pointer capture, and clicks while captured (their
    // coordinates are frozen at the lock point, so they'd scan a stale spot).
    // A stationary tap on the world is untouched and still scans.
    function onClickCapture(e) {
      if (!modeOn()) return;
      const steering = draggedFar || locked || (e.detail || 0) >= 2;
      draggedFar = false;
      if (!steering) return;
      e.stopImmediatePropagation();
      e.stopPropagation();
      e.preventDefault();
    }
    // Explicit opt-in for full capture. A single click is left alone: the game
    // uses clicks, and stealing the cursor from one looked like a freeze.
    function onDblClick(e) {
      if (locked || !allowed() || uiBusy()) return;
      if (e.button != null && e.button !== 0) return;
      if (isUi(e.target)) return;
      requestLock();
    }
    function onBlur() { if (locked || dragging) releaseLock(); }

    function onModeChanged() {
      if (!modeOn()) releaseLock();
      else paint();
    }

    function init() {
      ensureReticle();
      document.addEventListener("pointerlockchange", onLockChange);
      document.addEventListener("mozpointerlockchange", onLockChange);
      document.addEventListener("mousedown", onMouseDown, true);
      document.addEventListener("mouseup", onMouseUp, true);
      document.addEventListener("mousemove", onMouseMove);
      document.addEventListener("dblclick", onDblClick, true);
      // Capture phase on window: runs before the bubble-phase world-tap scan
      // handler, which is the only way to cancel it.
      window.addEventListener("click", onClickCapture, true);
      window.addEventListener("blur", onBlur);
      paint();
    }

    return {
      init, intent, isActive, isEngaged, requestLock, releaseLock,
      onModeChanged, allowed,
      // Debug/e2e: push a synthetic delta as if the mouse moved while captured.
      __feed: (dx, dy) => { dragging = true; paint(); feed(dx, dy); },
    };
  })();
  try { window.__MouseLook = MouseLook; } catch (_) {}

  // ------------------------------------------------------------------
  // InputProfileUi — the CONTROLS switch (DOOM / FPS). Mounted in the WORLD
  // EDITOR (the authoring surface) and mirrored in the WORLD MODEL panel, both
  // driven from one place so a new mode shows up in both automatically.
  // ------------------------------------------------------------------
  const InputProfileUi = (function () {
    const MOUNTS = [
      { seg: "we-input-profile", wrap: "we-input-opts", hint: "we-input-hint", always: true },
      { seg: "rt-input-profile", wrap: "rt-input-opts", hint: null, always: false },
    ];

    function apply(id) {
      if (!InputBindings.setProfile(id)) return;
      const p = InputBindings.profile();
      showRendererToast("Controls: " + (p.label || id) + " \u2014 " + (p.hint || ""), 2800);
      try { RtLog.push("ok", "controls", (p.label || id) + " \u00B7 " + (p.hint || "")); } catch (_) {}
    }

    function build(m) {
      const host = document.getElementById(m.seg);
      if (!host || host.childElementCount) return;
      InputBindings.list().forEach((opt) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "rt-ho-btn";
        b.dataset.value = opt.id;
        b.textContent = opt.label;
        b.title = opt.hint || opt.label;
        b.addEventListener("click", () => apply(opt.id));
        host.appendChild(b);
      });
    }

    // The sensitivity slider (editor only). Only meaningful in a mouse-look mode,
    // so it dims out in DOOM rather than vanishing — the control stays findable.
    function buildSens() {
      const slider = document.getElementById("we-input-sens");
      if (!slider || slider.dataset.wired) return;
      const r = InputBindings.sensitivityRange();
      slider.min = String(r.min);
      slider.max = String(r.max);
      slider.dataset.wired = "1";
      slider.addEventListener("input", () => {
        InputBindings.setSensitivity(slider.value);
        paintSens();
      });
    }
    function paintSens() {
      const slider = document.getElementById("we-input-sens");
      const out = document.getElementById("we-input-sens-val");
      const wrap = document.getElementById("we-input-sens-wrap");
      const on = InputBindings.mouseLookEnabled();
      const s = InputBindings.sensitivity();
      if (slider) { slider.value = String(s); slider.disabled = !on; }
      if (out) out.textContent = s.toFixed(1) + "\u00D7";
      if (wrap) {
        wrap.classList.toggle("off", !on);
        wrap.title = on ? "How far the camera turns per mouse movement"
                        : "Mouse look is only used in FPS mode";
      }
    }

    function paint() {
      const cur = InputBindings.current();
      MOUNTS.forEach((m) => {
        const host = document.getElementById(m.seg);
        if (host) {
          build(m);
          Array.from(host.children).forEach((b) =>
            b.classList.toggle("on", b.dataset.value === cur));
        }
        if (m.hint) {
          const h = document.getElementById(m.hint);
          if (h) h.textContent = InputBindings.hint();
        }
      });
      buildSens();
      paintSens();
    }

    // The WORLD MODEL mirror only makes sense while a navigable model is live;
    // the editor mount is always available (that's where you set up a rig).
    function update() {
      let live = false;
      try {
        live = Renderer.mode === "reactor" && Renderer.reactorAvailable();
      } catch (_) {}
      MOUNTS.forEach((m) => {
        const w = document.getElementById(m.wrap);
        if (w && !m.always) w.classList.toggle("hidden", !live);
      });
      paint();
    }

    function init() { update(); }
    return { init, update, paint };
  })();
  try { window.__InputProfileUi = InputProfileUi; } catch (_) {}

  // ------------------------------------------------------------------
  // Drive joystick — the realtime "EXPLORE" instrument (drive + steer).
  //
  // First-person drive over the live world model's NATIVE navigation. The
  // renderer facade (setAxis/setRotationSpeed/stopMotion) presents one stable
  // surface and translates to whichever model is live:
  //
  //   • forward / back        (W / S · stick y)           -> Happy Oyster move
  //                             Front/Back  (LingBot: set_move_longitudinal)
  //   • strafe left / right   (A / D in FPS · Q / E)      -> Happy Oyster move
  //                             Left/Right  (LingBot: set_move_lateral)
  //   • yaw / pitch look      (mouse · arrows · stick x)  -> Happy Oyster look
  //                             Mouse_*     (LingBot: set_look_*)
  //   • turn speed             a slow CONSTANT rate proportional to push
  //                             (LingBot set_rotation_speed_deg; Happy Oyster has
  //                             no turn-rate knob, so it's ignored there)
  //
  // Key → action mapping lives in InputBindings (fps / classic profiles).
  // Movement is HELD state: the model keeps applying each direction until
  // released. Models without native navigation (Helios / blend-family) fall
  // back to a prompt nudge. Only active in realtime video mode.
  // ------------------------------------------------------------------
  const Movement = (function () {
    const RAMP_MS = 2600;            // (visual thrust ramp only; turn speed is constant)
    const DEADZONE = 0.22;           // ignore tiny stick wiggle near the center
    const TICK_MS = 90;              // visual + drive loop cadence
    // Turn speed is deg/latent-frame and it COMPOUNDS every chunk, so small
    // numbers pan fast. Keep it slow + CONSTANT (no hold-time acceleration) so
    // it's easy to aim and never disorients — well under the model default of 5.
    const ROT_MIN = 1.875;           // deg/latent-frame at a gentle push (2.5x the prior 0.75)
    const ROT_MAX = 5;               // deg/latent-frame at a full push (2.5x the prior 2; 0..30 allowed)
    // Mouse look uses a gentler band — latent lag punishes fast turns.
    const MOUSE_ROT_MIN = 1.0;
    const MOUSE_ROT_MAX = 2.75;
    // Full H+V cycle when interleaving a diagonal on a one-look-axis model. Long
    // enough that each slice actually moves the latent picture, short enough to
    // read as one diagonal sweep rather than two separate turns.
    const DIAGONAL_CYCLE_MS = 320;
    const KEY_INTENSITY = 0.5;       // fixed push level for keyboard turning (no analog)
    const FALLBACK_SEND_MS = 950;    // prompt-fallback (non-native-nav) re-steer cadence

    // Keyboard → semantic drive tokens via the active InputBindings profile.
    function keyFor(key) { return InputBindings.keyFor(key); }

    const vec = { x: 0, y: 0 };      // stick vector, SCREEN space: x right+, y down+
    let mag = 0;                     // stick magnitude 0..1
    let radius = 44;                 // px travel radius of the nub
    let pointerActive = false;
    let pointerId = null;
    const keys = new Set();          // held drive tokens
    let engaged = false;
    let rampStart = 0;
    let loopTimer = null;
    let warnedNotReady = false;
    // Last axis values pushed to the model, so we only send on change.
    const sent = { longitudinal: "idle", lateral: "idle", lookH: "idle", lookV: "idle", rot: null };
    let lastFallbackTs = 0;
    let lastFallbackKey = null;

    function enabled() {
      if (Renderer.mode !== "reactor" || !Renderer.reactorAvailable()) return false;
      // The Director experience has no movement/look — steering is text only.
      try {
        if (window.ReactorRenderer.getExperience &&
            window.ReactorRenderer.getExperience() === "director") return false;
      } catch (_) {}
      return true;
    }
    function nativeMotion() {
      return enabled() && window.ReactorRenderer.motionSupported &&
        window.ReactorRenderer.motionSupported();
    }

    function setVar(node, name, val) { if (node) node.style.setProperty(name, val); }

    // Can the live model hold only ONE look direction at a time? (Happy Oyster.)
    function oneLookAxisOnly() {
      try {
        return !!(window.ReactorRenderer.looksOneAxisAtATime &&
                  window.ReactorRenderer.looksOneAxisAtATime());
      } catch (_) { return false; }
    }
    // ...and only ONE move direction? Then forward+strafe needs slicing too.
    function oneMoveAxisOnly() {
      try {
        return !!(window.ReactorRenderer.movesOneAxisAtATime &&
                  window.ReactorRenderer.movesOneAxisAtATime());
      } catch (_) { return false; }
    }
    // Which half of the interleave cycle we're in. `hShare` (0..1) is horizontal's
    // slice of the cycle, so a mostly-sideways sweep spends most of it turning.
    function diagonalPhase(hShare, offsetMs) {
      const share = Math.min(0.85, Math.max(0.15, hShare)); // always give V a turn
      const t = (Date.now() + (offsetMs || 0)) % DIAGONAL_CYCLE_MS;
      return (t / DIAGONAL_CYCLE_MS) < share;
    }

    // Compose the desired DRIVE state from keyboard + stick + mouse-look.
    // Precedence per axis: keys > stick > mouse (keys win where non-idle).
    function compose() {
      // Keyboard contribution — mapped through the active InputBindings profile.
      let lon = keys.has("fwd") && !keys.has("back") ? "forward"
              : keys.has("back") && !keys.has("fwd") ? "back" : "idle";
      let lat = keys.has("strafeL") && !keys.has("strafeR") ? "left"
              : keys.has("strafeR") && !keys.has("strafeL") ? "right" : "idle";
      let lh  = keys.has("lookL") && !keys.has("lookR") ? "left"
              : keys.has("lookR") && !keys.has("lookL") ? "right" : "idle";
      let lv  = keys.has("pitchUp") && !keys.has("pitchDown") ? "up"
              : keys.has("pitchDown") && !keys.has("pitchUp") ? "down" : "idle";
      let keyLooking = lh !== "idle" || lv !== "idle";
      // Stick contribution: y = forward/back, x = yaw.
      let ptrTurnMag = 0;
      if (pointerActive) {
        if (lon === "idle") lon = vec.y < -DEADZONE ? "forward" : vec.y > DEADZONE ? "back" : "idle";
        if (lh === "idle") {
          lh = vec.x < -DEADZONE ? "left" : vec.x > DEADZONE ? "right" : "idle";
        }
        if (lh !== "idle" && !keyLooking) {
          ptrTurnMag = Math.min(1, Math.max(0, (Math.abs(vec.x) - DEADZONE) / (1 - DEADZONE)));
        }
      }
      // Mouse-look contribution (FPS profile). Never overrides an explicit look
      // key / stick yaw. Both axes can be owed at once, so a diagonal (up AND
      // left) is honoured: models with independent look axes hold both, while
      // Happy Oyster — which can hold only ONE held look verb — gets the two
      // interleaved in time slices weighted by how far the mouse went on each
      // axis, which reads as a diagonal sweep.
      let mouseIntensity = 0;
      let fromMouse = false;
      if (lh === "idle" && lv === "idle") {
        let ml = null;
        try { ml = MouseLook.intent(); } catch (_) {}
        if (ml) {
          lh = ml.lookH || "idle";
          lv = ml.lookV || "idle";
          mouseIntensity = ml.intensity || 0;
          fromMouse = lh !== "idle" || lv !== "idle";
          if (lh !== "idle" && lv !== "idle" && oneLookAxisOnly()) {
            const hShare = (ml.hMag || 0) / ((ml.hMag || 0) + (ml.vMag || 0) || 1);
            if (diagonalPhase(hShare)) lv = "idle";
            else lh = "idle";
          }
        }
      }
      // Turn speed (deg/frame): CONSTANT, predictable — no hold-time acceleration.
      // Mouse uses a gentler band than stick/keys because latent video lags.
      let rot = null;
      if (lh !== "idle" || lv !== "idle") {
        if (fromMouse) {
          rot = MOUSE_ROT_MIN + (MOUSE_ROT_MAX - MOUSE_ROT_MIN) * mouseIntensity;
        } else {
          const intensity = (pointerActive && !keyLooking) ? ptrTurnMag : KEY_INTENSITY;
          rot = ROT_MIN + (ROT_MAX - ROT_MIN) * intensity;
        }
      }
      // What the player is ASKING for, before any per-model slicing. The readout
      // and the joystick show this, so the HUD never flickers mid-interleave.
      const raw = { longitudinal: lon, lateral: lat, lookH: lh, lookV: lv };
      // Forward + strafe (W+A) on a model that holds only ONE move direction:
      // interleave them, or the renderer picks longitudinal and the strafe is
      // silently dropped. Offset half a cycle from the look interleave so the
      // move and look slices don't stall in lockstep.
      if (lon !== "idle" && lat !== "idle" && oneMoveAxisOnly()) {
        if (diagonalPhase(0.5, DIAGONAL_CYCLE_MS / 2)) lat = "idle";
        else lon = "idle";
      }
      return { longitudinal: lon, lateral: lat, lookH: lh, lookV: lv, rot: rot, raw: raw };
    }

    // Push changed drive axes to the world model via the renderer facade, which
    // translates to the live model's native navigation (Happy Oyster / LingBot).
    function driveNative(st) {
      const R = window.ReactorRenderer;
      const AXES = ["longitudinal", "lateral", "lookH", "lookV"];
      // Diff the whole drive state and push it in ONE reconcile per tick (batched
      // so a diagonal change never emits a transient stop→re-assert flurry).
      const changed = AXES.some((k) => sent[k] !== st[k]);
      if (changed) {
        const axes = {};
        AXES.forEach((k) => { sent[k] = st[k]; axes[k] = st[k]; });
        if (R.setAxes) R.setAxes(axes);
        else AXES.forEach((k) => R.setAxis(k, st[k])); // fallback for older renderer
      }
      const moved = AXES.some((k) => st[k] !== "idle");
      if (st.rot != null && Math.round(st.rot) !== Math.round(sent.rot == null ? -1 : sent.rot)) {
        sent.rot = st.rot;
        R.setRotationSpeed(st.rot);
      }
      if (moved && !window.ReactorRenderer.isShowing() && !warnedNotReady) {
        warnedNotReady = true;
        showRendererToast("Exploring \u2014 the live world catches up in a moment");
      }
    }

    // Fallback for models without native look axes: nudge with a prompt.
    function driveFallback(st, label) {
      const now = Date.now();
      if (label === "still") return;
      if (label === lastFallbackKey && now - lastFallbackTs < FALLBACK_SEND_MS) return;
      lastFallbackKey = label; lastFallbackTs = now;
      const phrase = fallbackPhrase(st);
      if (!phrase) return;
      const ok = Renderer.steerMovement("Camera: " + phrase + ". " + Camera.movementClause());
      if (ok) RtLog.push("prompt", "\u25B8 camera \u00B7 " + label.toLowerCase());
      else if (!warnedNotReady) { warnedNotReady = true; showRendererToast("Live video is warming up \u2014 explore in a moment"); }
    }
    function fallbackPhrase(st) {
      const p = [];
      if (st.longitudinal === "forward") p.push("the camera pushes forward, deeper into the scene");
      else if (st.longitudinal === "back") p.push("the camera pulls backward, retreating");
      if (st.lateral === "left") p.push("strafing to the left");
      else if (st.lateral === "right") p.push("strafing to the right");
      if (st.lookH === "left") p.push("turning to look to the left");
      else if (st.lookH === "right") p.push("turning to look to the right");
      if (st.lookV === "up") p.push("tilting the view upward");
      else if (st.lookV === "down") p.push("tilting the view downward");
      return p.join(", ");
    }

    // A short human label for the readout + log from the composed drive state.
    function actionLabel(st) {
      const p = [];
      if (st.longitudinal === "forward") p.push("FWD");
      else if (st.longitudinal === "back") p.push("BACK");
      if (st.lateral === "left") p.push("STRAFE L");
      else if (st.lateral === "right") p.push("STRAFE R");
      if (st.lookH === "left") p.push("LOOK L");
      else if (st.lookH === "right") p.push("LOOK R");
      if (st.lookV === "up") p.push("LOOK UP");
      else if (st.lookV === "down") p.push("LOOK DN");
      return p.length ? p.join(" + ") : "still";
    }

    function updateVisual(state_) {
      if (!el.movePad) return;
      // Show the player's actual intent, not the current interleave slice.
      const st = state_.raw || state_;
      // Nub position: pointer uses its real vector; keys synthesize one from the
      // active axes so the knob shows the drive direction.
      let nx = vec.x, ny = vec.y;
      if (!pointerActive) {
        // Prefer look direction on the nub; fall back to strafe so FPS A/D still
        // shows on the pad when you're sliding without turning.
        nx = st.lookH === "left" ? -0.85 : st.lookH === "right" ? 0.85
           : st.lateral === "left" ? -0.85 : st.lateral === "right" ? 0.85 : 0;
        ny = st.longitudinal === "forward" ? -0.85 : st.longitudinal === "back" ? 0.85 : 0;
      }
      setVar(el.moveNub, "--mx", (nx * radius).toFixed(1) + "px");
      setVar(el.moveNub, "--my", (ny * radius).toFixed(1) + "px");
      const moving = st.longitudinal !== "idle" || st.lookH !== "idle" ||
                     st.lateral !== "idle" || st.lookV !== "idle";
      const thrust = pointerActive ? mag : (moving ? Math.min(1, (Date.now() - rampStart) / RAMP_MS * 0.6 + 0.4) : 0);
      setVar(el.movePad, "--thrust", (engaged && moving ? thrust : 0).toFixed(3));
      let deg = 0;
      if (moving) deg = (Math.atan2(nx, -ny) * 180 / Math.PI + 360) % 360;
      setVar(el.moveArrow, "--dir", deg.toFixed(0) + "deg");
      const ticks = el.movePad.querySelectorAll(".move-tick");
      ticks.forEach((t) => t.classList.remove("lit"));
      const lit = (cls) => { const n = el.movePad.querySelector(".move-tick-" + cls); if (n) n.classList.add("lit"); };
      if (st.longitudinal === "forward") lit("n");
      if (st.longitudinal === "back") lit("s");
      if (st.lookH === "left" || st.lateral === "left") lit("w");
      if (st.lookH === "right" || st.lateral === "right") lit("e");
      const label = actionLabel(st);
      if (el.moveReadout) el.moveReadout.textContent = moving ? label : "EXPLORE";
      if (el.moveNub) el.moveNub.setAttribute("aria-valuetext", moving ? label.toLowerCase() : "centered");
    }

    function tick() {
      if (!engaged) return;
      const st = compose();
      updateVisual(st);
      const label = actionLabel(st.raw || st);
      // OCR hotspots follow REAL motion, not merely an engaged instrument —
      // holding the mouse captured without moving must not keep SCAN disabled.
      const intent = st.raw || st;
      const moving = intent.longitudinal !== "idle" || intent.lateral !== "idle" ||
                     intent.lookH !== "idle" || intent.lookV !== "idle";
      if (moving) { try { onMovementStart(); } catch (_) {} }
      else { try { onMovementStop(); } catch (_) {} }
      if (nativeMotion()) driveNative(st);
      else if (moving) driveFallback(st, label);
    }

    function startLoop() { if (!loopTimer) loopTimer = setInterval(tick, TICK_MS); }
    function stopLoop() { if (loopTimer) { clearInterval(loopTimer); loopTimer = null; } }

    function engage() {
      if (engaged) return;
      engaged = true;
      rampStart = Date.now();
      warnedNotReady = false;
      if (el.movePad) el.movePad.classList.add("engaged");
      try { Sound.press(); } catch (_) {}
      startLoop();
      tick(); // respond immediately (also hides the OCR overlay if we're moving)
    }

    function stopAll() {
      // Idle every axis so the camera comes to rest (persistent state!). We call
      // stopMotion (idles ALL axes incl. any translation) so nothing lingers.
      if (nativeMotion() && window.ReactorRenderer.stopMotion) window.ReactorRenderer.stopMotion();
      else if (enabled() && (sent.longitudinal !== "idle" || sent.lookH !== "idle" ||
                             sent.lateral !== "idle" || sent.lookV !== "idle")) {
        Renderer.steerMovement("Camera: the viewpoint eases to a halt and holds steady, the scene settling into a calm, stable shot.");
      }
      sent.longitudinal = "idle"; sent.lateral = "idle"; sent.lookH = "idle"; sent.lookV = "idle";
      lastFallbackKey = null;
    }

    function disengage() {
      if (!engaged) return;
      engaged = false;
      pointerActive = false;
      pointerId = null;
      keys.clear();
      vec.x = 0; vec.y = 0; mag = 0;
      stopLoop();
      if (el.movePad) el.movePad.classList.remove("engaged");
      if (el.moveNub) el.moveNub.classList.remove("dragging");
      updateVisual({ longitudinal: "idle", lateral: "idle", lookH: "idle", lookV: "idle" });
      stopAll();
      RtLog.push("dim", "\u25A0 camera \u00B7 rest");
      // Regenerate + reveal the OCR hotspots once the view settles.
      try { onMovementStop(); } catch (_) {}
    }

    // ---- Pointer (mouse / touch) ----
    function measure() {
      if (!el.movePad) return;
      const r = el.movePad.getBoundingClientRect();
      radius = Math.max(24, r.width / 2 - (el.moveNub ? el.moveNub.offsetWidth / 2 : 22) + 6);
    }
    function updateFromPointer(clientX, clientY) {
      const r = el.movePad.getBoundingClientRect();
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      let dx = clientX - cx, dy = clientY - cy;
      const dist = Math.hypot(dx, dy);
      if (dist > radius) { dx = dx / dist * radius; dy = dy / dist * radius; }
      vec.x = dx / radius; vec.y = dy / radius;
      mag = Math.min(1, dist / radius);
    }
    function onPointerDown(e) {
      if (!enabled()) { showRendererToast("Switch to LIVE video to explore (G)"); return; }
      measure();
      pointerActive = true;
      pointerId = e.pointerId;
      try { el.movePad.setPointerCapture(e.pointerId); } catch (_) {}
      if (el.moveNub) el.moveNub.classList.add("dragging");
      engage();
      updateFromPointer(e.clientX, e.clientY);
      tick();
      e.preventDefault();
    }
    function onPointerMove(e) {
      if (!pointerActive || e.pointerId !== pointerId) return;
      updateFromPointer(e.clientX, e.clientY);
      e.preventDefault();
    }
    // Something is still holding the instrument: a key, the stick, or the mouse
    // (captured / dragging). Mouse capture keeps the loop warm without implying
    // motion — tick() decides whether the camera is actually moving.
    function anyDriver() {
      let mouse = false;
      try { mouse = MouseLook.isEngaged(); } catch (_) {}
      return keys.size > 0 || pointerActive || mouse;
    }

    function onPointerUp(e) {
      if (!pointerActive || e.pointerId !== pointerId) return;
      try { el.movePad.releasePointerCapture(e.pointerId); } catch (_) {}
      pointerActive = false;
      if (el.moveNub) el.moveNub.classList.remove("dragging");
      if (!anyDriver()) disengage();
      else { vec.x = 0; vec.y = 0; mag = 0; tick(); }
    }

    // ---- Keyboard ----
    function pressKey(tok) {
      if (!enabled() || !tok) return false;
      if (!keys.has(tok)) {
        keys.add(tok);
        engage();
        tick();
      }
      return true;
    }
    function releaseKey(tok) {
      if (!tok || !keys.has(tok)) return;
      keys.delete(tok);
      if (!anyDriver()) disengage();
      else tick();
    }
    function releaseAll() {
      try { MouseLook.releaseLock(); } catch (_) {}
      keys.clear();
      if (engaged) disengage();
    }

    // MouseLook calls these so look-driven motion engages the same OCR-hide /
    // drive loop path as a key press, and releases cleanly when the lock drops.
    function noteActivity() {
      if (!enabled()) return;
      engage();
      tick();
    }
    // Pointer capture / drag ended: stop the camera unless a key still holds it.
    function onLookReleased() {
      if (!engaged) return;
      if (!anyDriver()) disengage();
      else tick();
    }
    function retick() { if (engaged) tick(); }

    function refreshHints() {
      if (!el.moveNub) return;
      const hint = InputBindings.hint();
      el.moveNub.setAttribute("aria-label", "Drive joystick \u2014 " + hint + ", or drag the stick");
      el.moveNub.title = "Explore \u2014 " + hint;
    }

    function init() {
      if (!el.movePad) return;
      el.movePad.addEventListener("pointerdown", onPointerDown);
      el.movePad.addEventListener("pointermove", onPointerMove);
      el.movePad.addEventListener("pointerup", onPointerUp);
      el.movePad.addEventListener("pointercancel", onPointerUp);
      el.movePad.addEventListener("lostpointercapture", () => {
        if (pointerActive) {
          pointerActive = false;
          if (!anyDriver()) disengage();
        }
      });
      el.movePad.addEventListener("contextmenu", (e) => e.preventDefault());
      window.addEventListener("resize", measure);
      window.addEventListener("blur", releaseAll);
      measure();
      refreshHints();
      updateVisual({ longitudinal: "idle", lateral: "idle", lookH: "idle", lookV: "idle" });
    }

    return {
      init, enabled, keyFor, pressKey, releaseKey, releaseAll,
      noteActivity, onLookReleased, retick, refreshHints,
    };
  })();
  // Expose for debugging + e2e.
  try { window.__Movement = Movement; } catch (_) {}

  // ------------------------------------------------------------------
  // VerbBar — the interaction-verb instrument for Happy Oyster Adventure.
  //
  // Happy Oyster worlds accept interaction VERBS: a built-in survival set
  // (Sprint / Crouch / Jump / Attack) plus verbs the specific world advertises
  // live via travel_state (character_actions + environment_actions). This surfaces
  // them as tappable buttons so the player can actually DO them, and the set
  // updates itself as the world reports new verbs. Momentary verbs (Jump / Attack
  // / advertised) fire a one-shot interact({action}); held verbs (Sprint /
  // Crouch) stay engaged while pressed (press = setHeldVerb, release = clear) and
  // compose with movement. Only visible in realtime video mode when the live
  // model takes verbs (Happy Oyster). Keyboard: hold Shift to Sprint.
  // ------------------------------------------------------------------
  const VerbBar = (function () {
    // Verbs that are HELD (engaged while pressed) vs momentary (fire once).
    const HELD = { sprint: true, crouch: true };
    // Small glyphs for the built-in verbs; advertised verbs render as text chips.
    const ICON = {
      sprint: "\u00BB", crouch: "\u02C5", jump: "\u2191", attack: "\u2694",
    };
    let built = "";           // signature of the verb set currently rendered
    let heldBtn = null;       // the button whose held verb is currently engaged

    function available() {
      return Renderer.mode === "reactor" && Renderer.reactorAvailable() &&
        window.ReactorRenderer && window.ReactorRenderer.canInteract &&
        window.ReactorRenderer.canInteract();
    }

    function verbs() {
      try {
        const v = window.ReactorRenderer.getInteractVerbs && window.ReactorRenderer.getInteractVerbs();
        if (v && v.length) return v;
      } catch (_) {}
      return ["Sprint", "Crouch", "Jump", "Attack"];
    }

    function fire(verb) {
      try { window.ReactorRenderer.interact(verb); } catch (_) {}
      try { Sound.submit(); } catch (_) {}
      try { Haptics.soft(); } catch (_) {}
    }
    function hold(verb, on) {
      try { window.ReactorRenderer.setHeldVerb(on ? verb : null); } catch (_) {}
      if (on) { try { Haptics.soft(); } catch (_) {} }
    }

    function makeBtn(verb) {
      const key = verb.toLowerCase();
      const held = !!HELD[key];
      const b = document.createElement("button");
      b.type = "button";
      b.className = "verb-btn" + (held ? " verb-held" : "");
      b.dataset.verb = verb;
      b.title = (held ? "Hold to " : "") + verb;
      const ico = ICON[key];
      b.innerHTML = (ico ? '<span class="verb-ico" aria-hidden="true">' + ico + "</span>" : "") +
        '<span class="verb-label">' + verb.toUpperCase() + "</span>";
      if (held) {
        const down = (e) => { e.preventDefault(); b.classList.add("active"); heldBtn = b; hold(verb, true); };
        const up = (e) => { if (e) e.preventDefault(); if (!b.classList.contains("active")) return; b.classList.remove("active"); if (heldBtn === b) heldBtn = null; hold(verb, false); };
        b.addEventListener("pointerdown", down);
        b.addEventListener("pointerup", up);
        b.addEventListener("pointerleave", up);
        b.addEventListener("pointercancel", up);
      } else {
        b.addEventListener("click", (e) => {
          e.preventDefault();
          b.classList.remove("poke"); void b.offsetWidth; b.classList.add("poke");
          fire(verb);
        });
      }
      return b;
    }

    function render() {
      if (!el.verbBar) return;
      const list = verbs();
      const sig = list.join("|");
      if (sig === built) return; // nothing changed
      built = sig;
      // Releasing any engaged held verb before we rebuild the buttons.
      if (heldBtn) { hold(heldBtn.dataset.verb, false); heldBtn = null; }
      el.verbBar.innerHTML = "";
      list.forEach((v) => el.verbBar.appendChild(makeBtn(v)));
    }

    // Show/hide with the realtime instrument set; rebuild when verbs change.
    function update() {
      if (!el.verbBar) return;
      const on = available();
      el.verbBar.classList.toggle("hidden", !on);
      el.verbBar.setAttribute("aria-hidden", on ? "false" : "true");
      if (on) render();
      else {
        built = "";
        el.verbBar.innerHTML = "";
        heldBtn = null;
        shiftHeld = false;
        // Releasing the bar (Director mode, leaving realtime, etc.) must also
        // release any verb still held in the renderer, or it stays engaged with
        // no UI to clear it.
        try { window.ReactorRenderer.setHeldVerb && window.ReactorRenderer.setHeldVerb(null); } catch (_) {}
      }
    }

    // The live model reported a new verb set (travel_state) — rebuild.
    function onVerbs() { built = ""; update(); }

    // Keyboard Sprint (hold Shift while exploring the live world).
    let shiftHeld = false;
    function onShift(down) {
      if (down === shiftHeld) return;
      if (down && !available()) return;
      shiftHeld = down;
      hold("Sprint", down);
      const sb = el.verbBar && el.verbBar.querySelector('.verb-btn[data-verb="Sprint"]');
      if (sb) sb.classList.toggle("active", down);
      if (!down && heldBtn && heldBtn.dataset.verb === "Sprint") heldBtn = null;
    }

    function init() {
      update();
    }

    return { init, update, onVerbs, onShift };
  })();
  try { window.__VerbBar = VerbBar; } catch (_) {}

  // ------------------------------------------------------------------
  // HappyOysterOptions — the two session-fixed knobs Happy Oyster exposes at
  // world creation, surfaced in the WORLD MODEL panel:
  //   • VIEW — camera perspective: first-person (default) or third-person.
  //   • MODE — the EXPERIENCE: Adventure (walk/look/interact — the game) or
  //     Director (steer the scene with text + pause/resume/rewind).
  // Both are fixed for a world's lifetime, so changing one persists the choice
  // and rebuilds the current world to apply it. Only shown when the live model
  // is Happy Oyster.
  // ------------------------------------------------------------------
  const HappyOysterOptions = (function () {
    const wrap = () => document.getElementById("rt-ho-opts");
    const SEGS = {
      perspective: {
        el: () => document.getElementById("rt-ho-perspective"),
        opts: [["first_person", "1ST"], ["third_person", "3RD"]],
        get: () => { try { return window.ReactorRenderer.getPerspective(); } catch (_) { return null; } },
        set: (v) => { try { window.ReactorRenderer.setPerspective(v); } catch (_) {} },
      },
      experience: {
        el: () => document.getElementById("rt-ho-experience"),
        opts: [["adventure", "ADVENTURE"], ["director", "DIRECTOR"]],
        get: () => { try { return window.ReactorRenderer.getExperience(); } catch (_) { return null; } },
        set: (v) => { try { window.ReactorRenderer.setExperience(v); } catch (_) {} },
      },
    };

    function isHappyOyster() {
      try { return Renderer.mode === "reactor" && Renderer.reactorAvailable() &&
        window.ReactorRenderer.getExperience && window.ReactorRenderer.getExperience() !== null; }
      catch (_) { return false; }
    }

    function apply(seg, value) {
      const cur = SEGS[seg].get();
      if (value === cur) return;
      SEGS[seg].set(value);
      update(); // repaint + reflect the experience on <body> (ho-director) now
      // Rebuild so the new session-fixed setting takes effect on the live world.
      let rebuilt = false;
      try { rebuilt = window.ReactorRenderer.rebuildWorld(); } catch (_) {}
      showRendererToast((seg === "perspective" ? "View" : "Mode") + ": " +
        (SEGS[seg].opts.find((o) => o[0] === value) || ["", value])[1].toLowerCase() +
        (rebuilt ? " — rebuilding world…" : ""));
    }

    function buildSeg(seg) {
      const host = SEGS[seg].el();
      if (!host || host.childElementCount) return;
      SEGS[seg].opts.forEach(([value, label]) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "rt-ho-btn";
        b.dataset.value = value;
        b.textContent = label;
        b.addEventListener("click", () => apply(seg, value));
        host.appendChild(b);
      });
    }

    function paint() {
      Object.keys(SEGS).forEach((seg) => {
        const host = SEGS[seg].el();
        if (!host) return;
        const cur = SEGS[seg].get();
        Array.from(host.children).forEach((b) =>
          b.classList.toggle("on", b.dataset.value === cur));
      });
    }

    function update() {
      const w = wrap();
      // The Director experience swaps the whole control scheme (text-steer +
      // playback instead of walk/look/interact) — flag it on <body> so the
      // Adventure-only instruments (joystick, verb bar) recede in CSS.
      const director = isHappyOyster() && SEGS.experience.get() === "director";
      document.body.classList.toggle("ho-director", director);
      if (!w) return;
      const on = isHappyOyster();
      w.classList.toggle("hidden", !on);
      if (on) { buildSeg("perspective"); buildSeg("experience"); paint(); }
    }

    function init() { update(); }

    return { init, update };
  })();
  try { window.__HappyOysterOptions = HappyOysterOptions; } catch (_) {}

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
    objective_new: "objective-event objective-new",
    objective_done: "objective-event objective-done",
    camp: "narrative-event",
    world_drift: "world-drift-event",
  };

  function classForType(type) {
    if (TYPE_CLASS[type]) return TYPE_CLASS[type];
    if (type && type.indexOf("combat") === 0) return "combat-event";
    if (type && type.indexOf("threat") === 0) return "threat-event";
    return "narrative-event";
  }

  // Short, human tag shown per log entry so the chronicle is legible at a glance.
  const LOG_LABEL = {
    player_action: "ACT",
    narrative_event: "SCENE",
    consequence_event: "RESULT",
    vision_analysis: "VISION",
    error_event: "ERROR",
    player_choice_prompt: "CHOICE",
    inventory_pickup: "PICKUP",
    inventory_full: "PACK",
    suspense_event: "TENSION",
    threat_escalation: "THREAT",
    risky_action_outcome: "RISK",
    combat_action: "COMBAT",
    combat_resolution: "COMBAT",
    game_over: "END",
    objective_new: "OBJECTIVE",
    objective_done: "COMPLETE",
    camp: "CAMP",
    world_drift: "WORLD",
  };

  function labelForType(type) {
    if (LOG_LABEL[type]) return LOG_LABEL[type];
    if (type && type.indexOf("combat") === 0) return "COMBAT";
    if (type && type.indexOf("threat") === 0) return "THREAT";
    return "LOG";
  }

  // Timestamp for a story-log entry (mm:ss into the session-visible clock).
  function logStamp() {
    const d = new Date();
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ss = String(d.getSeconds()).padStart(2, "0");
    return `${mm}:${ss}`;
  }

  function appendProse(item) {
    const div = document.createElement("div");
    div.className = `prose-entry glow-pop ${classForType(item.type)}`;
    div.dataset.itemId = item.id;
    const raw = item.content || "";

    // It's a detailed log now (not ambient overlay text), so each entry carries
    // a timestamp + type tag and shows the FULL beat rather than a condensed
    // one-liner.
    const t = document.createElement("span");
    t.className = "prose-time";
    t.textContent = logStamp();
    const tag = document.createElement("span");
    tag.className = "prose-tag";
    tag.textContent = labelForType(item.type);
    const body = document.createElement("span");
    body.className = "prose-body";
    body.innerHTML = renderInline(raw);

    div.appendChild(t);
    div.appendChild(tag);
    div.appendChild(body);
    el.prose.appendChild(div);
    // Tail to the newest entry when the log is open.
    if (StoryLog.visible()) el.prose.scrollTop = el.prose.scrollHeight + 400;
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
        // The boot-failure recovery button restarts the run rather than taking
        // a turn — there is no run yet to take one in.
        if (choice.action_id === "__retry_boot") { resetGame(); return; }
        makeChoice(choice.text, promptItem.id);
      });
      el.choices.appendChild(btn);
    });
  }

  function enterGameOver(message) {
    state.gameOver = true;
    state.awaitingResolution = false;
    Talk.close(); // end any conversation — the run is over
    Narrator.stop(); // silence any in-progress narration
    clearTurnWatchdog();
    // A closing narrated line over the death screen (the player has interacted,
    // so audio is unlocked). Fires once, after the death overlay settles.
    if (state.audioUnlocked) setTimeout(() => { if (state.gameOver) Narrator.epitaph(); }, 1600);
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
    // Kick off the arcade "CONTINUE? 10…9…8…" countdown + autofocus the
    // continue button. CoinOp is a no-op when the feature isn't enabled,
    // so this is safe unconditionally.
    try { CoinOp.onGameOverShown(); } catch (_) {}
  }

  function exitGameOver() {
    state.gameOver = false;
    el.deathOverlay.classList.add("hidden");
  }

  /**
   * Dismiss the death overlay AND undo the side effects enterGameOver
   * applied — used by the coin-op continue flow, which is a REVIVE on
   * the current run rather than a fresh restart.
   *
   * Two things enterGameOver does that a plain exitGameOver leaves
   * broken for revive:
   *   1. In realtime mode, it pauses the reactor's live video stream
   *      (see enterGameOver's ReactorRenderer.pause() call). Without
   *      the matching resume() the world is frozen after the revive.
   *   2. In realtime mode the DangerSystem may have already fired its
   *      client-side die() (peripheral-vignette damage → HP zero),
   *      which set its internal `dead=true` flag and cut its sampling
   *      loop. DangerSystem.reset() is the same primitive resetGame()
   *      uses for a fresh run: refills HP, clears mode/vignette, and
   *      restarts monitoring if reactor is live. It's a no-op beyond
   *      internal-state reset in stills mode, so calling it
   *      unconditionally is safe.
   *
   * Explicitly does NOT touch scene layers, history, inventory, or
   * timers — this is a revive on the current run, not a restart. The
   * server has already appended the `continue_used` narrative beat
   * and a fresh `player_choice_prompt`; a manual pollOnce right after
   * this call surfaces them without waiting for the poll tick.
   */
  function exitGameOverAndResume() {
    exitGameOver();
    try { DangerSystem.reset(); } catch (_) {}
    if (Renderer.mode === "reactor" && Renderer.reactorAvailable()) {
      try { window.ReactorRenderer.resume(); } catch (_) {}
    }
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

    // Ambient world drift (see WorldDrift): a text-only simulation step between
    // turns. It carries a steer prompt but NO new guide image, so it must not go
    // through the generic scene path below — that treats a prompt as a new scene
    // (ceremony beats, scene sound, autoplay, ambient re-score) and on a
    // seed-locked model re-stages the whole world for an atmospheric beat.
    if (item.type === "world_drift") {
      Renderer.applyDrift(item.metadata || {});
      appendProse(item);
      Sound.text();
      return;
    }

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
        // A scene beat with NO image_url means the still was content-filtered or
        // generation failed (the server still emits the beat so the turn
        // resolves and realtime keeps steering — see _generate_and_append_scene_image).
        // In stills mode there's no new frame to show, so give a visible "signal
        // lost" glitch instead of leaving the scene silently unchanged (which
        // read as "selecting an action didn't change scenes"). Realtime mode is
        // unaffected — the live video keeps steering off the prompt.
        if ((!item.image_url || (item.metadata && item.metadata.blocked)) && Renderer.mode !== "reactor") {
          glitchTransition();
          markSceneVisible(); // never leave the UI gated on a blocked first frame
          showRendererToast("Signal lost \u2014 image filtered");
        }
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
        //
        // REALTIME (reactor) mode: the scene_image feed item is NOT the frame
        // that's actually on screen — the live video re-anchor is still
        // establishing, so the world can be black/frozen for several seconds
        // after this beat. Resolving the ceremony here fades the turn veil and
        // unlocks the interaction layer (FORWARD / ACT / SCAN / move pad), which
        // made those options appear while no video was playing yet — very
        // visible right after switching to a slow/failing image provider.
        // Defer the resolve to the reactor's video events (video_showing /
        // video_black / video_recovered) so input is released only once a frame
        // is genuinely on screen. Ceremony's guide-image fallback timer still
        // guarantees the UI can never spin forever if the stream stalls.
        if (!(Renderer.mode === "reactor" && Renderer.reactorAvailable())) {
          Ceremony.imageLoaded();
        }
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
        // Evolve the generative LEAD to match where the story now stands. The
        // tracker itself is only REVEALED once the scene is on screen (see
        // markSceneVisible) — so it never floats over the boot void — but we
        // keep its data current here regardless. refreshStatus() is async, so
        // give it a beat to land the new turn/phase before deriving the lead.
        if (state.sceneVisible) {
          try { Objectives.syncCase(); } catch (_) {}
          setTimeout(() => { try { refreshDirective(); } catch (_) {} }, 400);
        }
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
      // Reset coin-op run state (countdown, credits HUD, button busy flag)
      // before we tear down anything else. Safe no-op when coin-op is off.
      try { CoinOp.onRunReset(); } catch (_) {}
      exitGameOver();
      Talk.close(); // end any conversation from the prior run
      Narrator.stop(); // silence any narration from the prior run
      closeScan(); // drop any scan tags/overlay from the dead run
      closeTouch(); // drop any camera overlay
      state.inCamp = false;
      state.campEntering = false;
      state.campLeaving = false;
      document.body.classList.remove("in-camp");
      try { updateLeaveCampButton(); } catch (_) {}
      try { Photo.hide(); Photo.clearTimers(); } catch (_) {} // kill any in-flight receipt
      try { hideCaseWin(); } catch (_) {}    // drop the win screen from the prior run
      state.caseWon = false;
      state._introGoalShown = false; // re-show the one-line goal on a fresh case
      try { Evidence.reset(); } catch (_) {} // the EVIDENCE score + case file are per-run
      try { Objectives.reset(); } catch (_) {} // objectives are per-run; reseed the spine + challenges
      state.objDirectiveTurn = null;
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
      // Fresh run → full health, safe state, vignette cleared. If we're in
      // realtime mode the loop keeps sampling; in stills mode reset() is a
      // no-op beyond zeroing the meter.
      try { DangerSystem.reset(); } catch (_) {}
      Renderer.lastScene = null;
      Renderer.lastBase = null;
      Renderer.observedPromptId = null;
      clearTimeout(state.observeTimer);
      state.lastScenePrompt = null;
      try { SceneAudio.reset(); } catch (_) {} // silence the prior run's bed
      Sound.start(); // new tape / game begins
      try { Haptics.strong(); } catch (_) {}
      Ceremony.abort(); // cancel any mid-turn pipeline from the prior run
      cancelMoveTransition(); // drop any pending MOVE TO fade so a restart during a trip doesn't dim the fresh run
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
      // Bound the wait. resetGame() awaits this before it can draw ANYTHING, so
      // a server that accepts the request and never answers (a wedged worker,
      // a proxy holding the socket) leaves the loader parked on its first step
      // over a black screen with no error, forever — indistinguishable from the
      // game being broken. Fail loudly instead, and let the player retry.
      const items = await withTimeout(
        postJSON("/api/reset", {}),
        RESET_TIMEOUT_MS,
        "the server did not respond",
      );
      // Do NOT hide the veil here: the ceremony now owns the progress bar and
      // fades itself once the first scene lands (player_choice_prompt →
      // Ceremony.complete, then the guide-image step resolves on scene_image).
      // The guide-image fallback timer guarantees it can never spin forever.
      renderItems(items);
      refreshStatus();
      // A narrated cold open once the first scene has had a moment to land —
      // only if audio is already unlocked (a real gesture happened), so it
      // speaks rather than silently failing autoplay.
      setTimeout(() => Narrator.coldOpen(), 4200);
    } catch (err) {
      console.error("[standalone] resetGame failed:", err);
      hideVeil();
      appendProse({ id: -1, type: "error_event", content: `Could not start the run: ${err.message}` });
      // Leave the player somewhere they can act from. Without this the boot
      // failure is a dead black screen: prose is the only thing on it, there
      // are no choices, and the only way out is knowing to reload.
      renderChoices({
        id: -1,
        choices: [{ text: "Try again", action_id: "__retry_boot" }],
      });
      state.awaitingResolution = false;
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
    const moveTarget = (opts && opts.moveTarget) || null;
    closeFreeWill(true); // picking any action closes the free-will gate
    clearScanTags();      // the scene is about to change — drop stale scan tags
    Narrator.stop();      // stop narration about the scene we're leaving
    // MOVE TO always resolves as a hard transition (moveActionPhrase emits
    // "enter"/"cross over", the engine's is_hard_transition triggers). Without
    // help, the LIVE video keeps drifting for however long the next guide image
    // takes to generate — the player commits to a trip and then watches the
    // world they're leaving flail (ridiculous). Kick off a bridging narrator
    // line NOW so a voice lands over the pause, and schedule a fade-to-black a
    // beat later so the departure reads as deliberate. The fresh scene's own
    // re-anchor path lifts the fade once the new frame is on screen.
    // Hard location changes (MOVE TO from SCAN, or LEAVE CAMP → new level)
    // hold a fade-to-black until the next guide image re-anchors — never steer
    // the world we're abandoning.
    if (actionSource === "scan_move" || actionSource === "camp_leave") {
      beginMoveTransition(moveTarget);
    }
    el.choices.innerHTML = "";
    Ceremony.begin(); // light up the turn pipeline — starting with "action selected"
    state.awaitingResolution = true;
    state.lastTurnTs = Date.now(); // pre-warm defers around the turn
    armTurnWatchdog(choiceText, contextItemId);
    // IMMEDIATE WORLD STEER: the live world model starts reacting to the chosen
    // action right away (see the steer block just below the act-frame capture),
    // so the world "travels" without waiting on the consequence + choice LLM
    // calls; the narrative and the guide-image re-anchor then catch up behind
    // the already-moving video. (Historically we did NOT steer here — with the
    // old seed-locked model, injecting the action before the new guide image
    // formed looked wrong. The default is now Happy Oyster, whose interact() /
    // travel controls are built to be driven directly, so immediate steering is
    // the natural fit.)
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
    // ── IMMEDIATE WORLD STEER (after the act-frame is captured, so img2img still
    // sees the pre-action frame the player decided on) ──
    // Start the live world reacting to the action NOW, so travel/interaction is
    // felt instantly instead of after the text pipeline resolves.
    //   • Happy Oyster (navigable world): interact({action}) drives the CURRENT
    //     traveling world in place — the intended primitive. The turn's later
    //     re-anchor rebuilds the world on the new guide image + full prompt.
    //   • Prompt-steered models (LingBot/Helios): steerRealtime composes the
    //     stable base + this action beat; the scene_image re-anchor arrives with
    //     the same full prompt and de-dupes (see ReactorRenderer.flush).
    // MOVE TO (scan_move) is a hard location change that fades to black and
    // re-anchors, so we let that path own the transition rather than steer the
    // world we're leaving.
    if (
      Renderer.mode === "reactor" &&
      Renderer.reactorAvailable() &&
      actionSource !== "scan_move" &&
      actionSource !== "camp_leave"
    ) {
      const steerVerb = (choiceText || "").trim();
      if (steerVerb) {
        try {
          if (window.ReactorRenderer.canInteract && window.ReactorRenderer.canInteract()) {
            window.ReactorRenderer.interact(steerVerb);
            try { RtLog.push("prompt", "\u25B8 interact", steerVerb); } catch (_) {}
          } else {
            Renderer.steerRealtime(steerVerb);
          }
        } catch (_) {}
      }
    }
    // If a captured specimen is armed, send the CAPTURE ITSELF, not just its id.
    // The id only ever meant something in this tab — nothing was uploaded — so
    // the engine had no way to resolve it and dropped it, and "loaded — describe
    // your action" was a promise the turn never kept. The texture rides along as
    // an img2img reference so the next frame is actually grounded on the thing
    // the player examined.
    const armed = state.selectedInvestigation;
    const investigationId = armed ? armed.id : null;
    const investigationFrame = (armed && armed.texture) || null;
    state.selectedInvestigation = null;
    try { Investigations.render(); } catch (_) {} // drop the selection highlight
    // Committing an action is what separates "watching" from "steering" in
    // the presence readout.
    try { Lobby.markActive(); } catch (_) {}
    try {
      const items = await postJSON("/api/choose", {
        choice: choiceText,
        context_item_id: contextItemId,
        act_frame: actFrame,
        investigation_id: investigationId,
        investigation_frame: investigationFrame,
        source: actionSource,
      });
      renderItems(items); // immediately shows the player_action echo
      beginFastPolling();
      // The server debits one credit on a successful turn. Poll the
      // balance so the HUD chip reflects the new number right away —
      // otherwise the count would drift until the next background poll.
      try { CoinOp.onTurnCompleted(); } catch (_) {}
    } catch (err) {
      // 402 + {needs_coin: true} = credit meter emptied out on the
      // server side. Pop the "INSERT COIN" pause overlay instead of
      // surfacing a raw error, and don't advance any turn state — the
      // engine didn't process the turn, so nothing was consumed and
      // there's nothing to "undo".
      if (err && err.status === 402 && err.body && err.body.needs_coin) {
        clearTurnWatchdog();
        cancelMoveTransition();
        hideVeil();
        state.awaitingResolution = false;
        try { CoinOp.pausePrompt(err.body); } catch (_) {}
        return;
      }
      console.error("[standalone] makeChoice failed:", err);
      clearTurnWatchdog();
      cancelMoveTransition(); // never leave the pre-fade hanging on a failed send
      hideVeil();
      state.awaitingResolution = false;
      appendProse({ id: -1, type: "error_event", content: `Action failed to send: ${err.message}` });
    }
  }

  // ---- MOVE TO transition -------------------------------------------
  // Bridge the gap between "player commits to a trip" and "the new scene lands"
  // so the leaving world doesn't drift ridiculously under a live camera. Fires
  // a short narrator line immediately, then fades the scene to black a beat
  // later — long enough for the click's press ceremony (pulse + toast + tag
  // pop) to register, short enough that the video isn't allowed to wander far.
  //   • DELAY  — hold the world visible this long after the press so the beat
  //              of departure reads before the fade kicks in.
  //   • SAFETY — the reactor's failsafe would otherwise lift the veil after
  //              only 9 s; a slow turn (LLM + guide image) legitimately runs
  //              longer, so stretch it well past a typical turn. The fresh
  //              scene's own beginSceneFade() call resets the safety back to
  //              the normal window when it arrives.
  const MOVE_TRANSITION_FADE_DELAY_MS = 900;
  const MOVE_TRANSITION_FADE_SAFETY_MS = 60000;
  function beginMoveTransition(destinationLabel) {
    // Fire the narrator BEFORE the fade so a voice lands as fast as possible
    // over the black. Silent when audio isn't unlocked (transition() no-ops).
    let narrated = false;
    try {
      narrated = Narrator.transition(destinationLabel) === true;
    } catch (err) {
      console.warn("[standalone] Narrator.transition failed", err);
    }
    try {
      RtLog.push("narrator", "\u25B8 MOVE TO \u00B7 bridge + dark truth" + (destinationLabel ? " \u2192 " + destinationLabel : "") +
                 (narrated ? "" : " (silent \u2014 audio not unlocked / no agent)"));
    } catch (_) {}
    // The fade only makes sense when the LIVE video would otherwise drift
    // ridiculously during the trip. In still mode the scene is already static
    // during a load, and the reactor's fade-lift machinery (freeze reveal /
    // scheduleSceneReveal) never fires there — a fade would just sit stuck
    // dark until the safety cap. Deliberately no isShowing() gate: freeze /
    // blackout can transiently flip it to false during the very moment we want
    // to fade (a recent re-anchor / stream sample), and silently dropping the
    // fade there is exactly the "world keeps going" symptom this fixes.
    if (Renderer.mode !== "reactor") {
      try { RtLog.push("dim", "\u2298 MOVE TO fade skipped (still mode \u2014 no live drift)"); } catch (_) {}
      return;
    }
    const RR = window.ReactorRenderer;
    if (!RR || typeof RR.beginSceneFade !== "function") {
      try { RtLog.push("error", "\u26A0 MOVE TO fade unavailable (renderer facade too old)"); } catch (_) {}
      return;
    }
    clearTimeout(state.moveFadeTimer);
    state.moveFadeTimer = setTimeout(() => {
      state.moveFadeTimer = null;
      try {
        // awaitReanchor: hold the veil past the initial safety window until
        // the fresh scene's re-anchor actually starts (armFreezeReveal or
        // scheduleSceneReveal). Prevents the "fade up onto the freeze-buffer
        // still, then video snaps in 10 s later" bug — video-to-video means
        // we wait for the real video before revealing anything.
        RR.beginSceneFade({
          safetyMs: MOVE_TRANSITION_FADE_SAFETY_MS,
          awaitReanchor: true,
        });
        RtLog.push("status", "\u25CF MOVE TO \u00B7 scene faded to black");
      } catch (err) {
        console.warn("[standalone] beginSceneFade failed", err);
      }
    }, MOVE_TRANSITION_FADE_DELAY_MS);
  }
  function cancelMoveTransition() {
    if (state.moveFadeTimer) { clearTimeout(state.moveFadeTimer); state.moveFadeTimer = null; }
    // If the pre-fade already committed and no scene is coming (send error /
    // reset), lift the veil ourselves so the player isn't stuck staring at
    // black waiting on the hard cap.
    try {
      const RR = window.ReactorRenderer;
      if (RR && typeof RR.endSceneFade === "function") RR.endSceneFade();
    } catch (_) {}
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
      x = t.px + (x - t.cx) / t.scale;
      y = t.py + (y - t.cy) / t.scale;
    }
    const size = currentSourceSize();
    if (!size || !size.w || !size.h) return { x: x / W, y: y / H };
    const scale = mediaFitScale(W, H, size.w, size.h);
    const dw = size.w * scale, dh = size.h * scale;
    const ox = (W - dw) / 2, oy = (H - dh) / 2;
    return { x: (x - ox) / dw, y: (y - oy) / dh };
  }

  // A screen-space rectangle (center + width/height in px) as a normalized
  // source box. If height is omitted the box is square (back-compat).
  function screenBoxToNorm(cx, cy, boxW, boxH) {
    if (boxH == null) boxH = boxW;
    const a = screenToNorm(cx - boxW / 2, cy - boxH / 2);
    const b = screenToNorm(cx + boxW / 2, cy + boxH / 2);
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

  // Crop a normalized region of the current scene to a JPEG data URL, preserving
  // the region's aspect ratio (the capture frame is 16:9). `outSize` is the
  // longest side. Uses the live video in realtime mode, or the still otherwise.
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
      // Preserve the region's aspect ratio (the capture frame is 16:9, not a
      // square) — `out` is the longest side.
      const aspect = sw / sh;
      let ow = out, oh = out;
      if (aspect >= 1) oh = Math.max(1, Math.round(out / aspect));
      else ow = Math.max(1, Math.round(out * aspect));
      const c = document.createElement("canvas");
      c.width = ow; c.height = oh;
      c.getContext("2d").drawImage(img, sx, sy, sw, sh, 0, 0, ow, oh);
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
                           // Kept intentionally attainable: a tight, satisfying
                           // dossier you can actually finish in a session beats a
                           // 12-slot grind the regenerating world made a slog.
  const FILM_START = 36;   // exposures per run — a real, depleting resource
  const Evidence = (function () {
    const KEY = "evidence_v1";
    let total = 0, shots = 0;
    let film = FILM_START;      // exposures remaining this run (0 => out of film)
    let seen = new Set();       // appraised item labels — drives scoring dedup + the census
    let spent = new Set();      // detected POIs already photographed — drives the "document once" gate
    try {
      const raw = JSON.parse(localStorage.getItem(KEY) || "null");
      if (raw && typeof raw === "object") {
        total = Number(raw.total) || 0;
        shots = Number(raw.shots) || 0;
        film = (typeof raw.film === "number") ? raw.film : FILM_START;
        seen = new Set(Array.isArray(raw.seen) ? raw.seen : []);
        // Migrate saves from before "document once": if there's no spent set yet,
        // treat everything already in the dossier as spent so old subjects don't
        // resurface as fresh, lockable targets worth zero points.
        spent = new Set(Array.isArray(raw.spent) ? raw.spent
          : (Array.isArray(raw.seen) ? raw.seen : []));
      }
    } catch (_) {}

    function persist() {
      try { localStorage.setItem(KEY, JSON.stringify({ total, shots, film, seen: [...seen], spent: [...spent] })); } catch (_) {}
    }

    function fmt(n) { return Math.round(n).toLocaleString("en-US"); }

    // Photographer's grade from the evidence banked. Still tracked every shot —
    // it's what grades the run on the case-closed screen — but no longer painted
    // live: a rank you can't act on mid-run is decoration, and it was sitting in
    // the corner of every frame competing with the world.
    function rankFor(t) {
      if (t >= 4800) return "S";
      if (t >= 3400) return "A";
      if (t >= 2100) return "B";
      if (t >= 1000) return "C";
      return "D";
    }

    // The ONLY ambient readout: how many keepers you have, and how many close
    // the case. Replaces a score, a rank badge, a progress bar and a film gauge.
    function renderTally() {
      if (!el.shotTally) return;
      if (el.tallyCount) el.tallyCount.textContent = seen.size + "/" + CASE_TARGET;
      el.shotTally.classList.toggle("case-complete", seen.size >= CASE_TARGET);
      // Film only speaks up when it's actually a problem. A gauge that reads
      // 36/36 for most of a run is telling you nothing.
      el.shotTally.classList.toggle("film-low", film <= 8 && film > 0);
      el.shotTally.classList.toggle("film-out", film <= 0);
    }

    return {
      total: () => total,
      shots: () => shots,
      uniqueCount: () => seen.size,
      target: () => CASE_TARGET,
      rank: () => rankFor(total),
      isNew: (label) => !!label && !seen.has(String(label).toLowerCase()),
      markSeen: (label) => { if (label) seen.add(String(label).toLowerCase()); },
      // "Document once": a photographed POI is spent and can't be re-farmed.
      isSpent: (label) => !!label && spent.has(String(label).toLowerCase()),
      spend: (label) => { if (label) { spent.add(String(label).toLowerCase()); persist(); } },
      // FILM — a real depleting resource. addShot spends one exposure.
      film: () => film,
      filmCap: () => FILM_START,
      hasFilm: () => film > 0,
      addShot: () => {
        shots += 1; film = Math.max(0, film - 1); persist(); renderTally();
        if (film === 8) { try { showRendererToast("8 exposures left \u2014 make them count.", 3000); } catch (_) {} }
        else if (film === 0) { try { showRendererToast("That was your last frame.", 3000); } catch (_) {} }
      },
      add(points) { total = Math.max(0, total + (Number(points) || 0)); persist(); renderTally(); },
      // A new keeper joined the file — one small pulse on the tally.
      pulseSubject() {
        if (!el.shotTally) return;
        el.shotTally.classList.remove("subject");
        void el.shotTally.offsetWidth;
        el.shotTally.classList.add("subject");
      },
      // Show the tally as soon as the goal is knowable.
      reveal() { if (el.shotTally) { el.shotTally.classList.remove("hidden"); renderTally(); } },
      renderHud: renderTally,
      renderFilm: renderTally,
      reset() {
        total = 0; shots = 0; film = FILM_START; seen = new Set(); spent = new Set(); persist();
        if (el.shotTally) {
          el.shotTally.classList.add("hidden");
          el.shotTally.classList.remove("case-complete", "film-low", "film-out");
          renderTally();
        }
      },
    };
  })();
  window.Evidence = Evidence;

  // ------------------------------------------------------------------
  // Objectives — a AAA-style objective tracker / "case board" in the top-right,
  // driven by BOTH the generative world and the photo loop so the run always
  // has a legible sense of purpose. It braids together:
  //
  //   • PRIMARY  — Close the Case: mirror the dossier census (document N
  //                distinct subjects). The spine of the run.
  //   • LEAD     — a model-authored "current directive" that EVOLVES with the
  //                story (fetched from /api/objectives, grounded in the world
  //                state; falls back to a scene/phase-derived line so it always
  //                reads well). This is the generative heart of the system.
  //   • FIELD    — bounties to DOCUMENT specific subjects the world model
  //                surfaces in the live scene (from /api/detect + /api/photo).
  //                Photographing that subject completes the bounty.
  //   • BONUS    — standing challenge goals (a rare ★★★★★ find; a PERFECT-focus
  //                shot) that reward mastery of the camera.
  //
  // Every objective animates in, ticks its progress, and lands a cinematic
  // "OBJECTIVE COMPLETE" beat — a deliberately game-y, satisfying loop.
  // Persistent per-run + exposed as window.Objectives for engine hooks / tests.
  // ------------------------------------------------------------------
  const Objectives = (function () {
    const KEY = "objectives_v1";
    const OPEN_KEY = "objectives_open";
    const MAX_FIELD = 3;             // simultaneous field bounties (avoid clutter)
    const STALE_MISSES = 4;          // detection passes a subject can be absent before its bounty retires
    const ARCHIVE_MS = 4600;         // how long a completed side-goal lingers before filing away
    const CHECK_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12.5l5 5L20 6"/></svg>';

    // kind → sort weight (lower = higher in the list) + tag label.
    const KIND_META = {
      primary: { order: 0, tag: "PRIMARY" },
      lead:    { order: 1, tag: "LEAD" },
      field:   { order: 2, tag: "FIELD" },
      bonus:   { order: 3, tag: "BONUS" },
    };

    let items = [];                  // active + recently-completed objectives
    let nodes = new Map();           // id -> <li>
    let seq = 0;                     // creation order tiebreak
    let revealed = false;
    let open_ = false;
    let bannerTimer = null;
    const archiveTimers = new Map(); // id -> timeout

    try { open_ = localStorage.getItem(OPEN_KEY) === "1"; } catch (_) {}

    function persist() {
      try {
        localStorage.setItem(KEY, JSON.stringify({ items }));
        localStorage.setItem(OPEN_KEY, open_ ? "1" : "0");
      } catch (_) {}
    }

    function norm(s) { return String(s == null ? "" : s).trim().toLowerCase(); }
    function get(id) { return items.find((o) => o.id === id) || null; }
    function has(id) { return !!get(id); }
    function activeFieldCount() {
      return items.filter((o) => o.kind === "field" && o.status === "active").length;
    }

    // ---- Cinematic banner ("NEW OBJECTIVE" / "OBJECTIVE COMPLETE") ----
    // The cinematic "NEW OBJECTIVE" / "BOUNTY SECURED" / "CHALLENGE COMPLETE"
    // banner is gone. A sweeping title card every time the world noticed an
    // object is what made a quiet documentary game feel like a slot machine;
    // objective changes are recorded in the story log instead, where you can
    // read them if you care and ignore them if you don't.
    function banner() {}

    function makeNode(o) {
      const li = document.createElement("li");
      li.className = "obj-item kind-" + o.kind;
      li.dataset.id = o.id;
      const check = document.createElement("span");
      check.className = "obj-check";
      check.innerHTML = CHECK_SVG;
      const main = document.createElement("div");
      main.className = "obj-main";
      const row = document.createElement("div");
      row.className = "obj-row";
      const tag = document.createElement("span");
      tag.className = "obj-tag";
      const title = document.createElement("span");
      title.className = "obj-title";
      row.appendChild(tag);
      row.appendChild(title);
      const detail = document.createElement("div");
      detail.className = "obj-detail";
      const prog = document.createElement("div");
      prog.className = "obj-progress";
      const track = document.createElement("div");
      track.className = "obj-progress-track";
      const fill = document.createElement("div");
      fill.className = "obj-progress-fill";
      const num = document.createElement("span");
      num.className = "obj-progress-num";
      track.appendChild(fill);
      prog.appendChild(track);
      prog.appendChild(num);
      main.appendChild(row);
      main.appendChild(detail);
      main.appendChild(prog);
      li.appendChild(check);
      li.appendChild(main);
      li._parts = { tag, title, detail, prog, fill, num };
      return li;
    }

    function paintNode(li, o) {
      const p = li._parts;
      li.className = "obj-item kind-" + o.kind + " " + o.status;
      p.tag.textContent = (KIND_META[o.kind] || {}).tag || o.kind.toUpperCase();
      p.title.textContent = o.title || "";
      // Keep the tracker clean: the flavor/detail lives as a hover tooltip
      // rather than a second line of text under every objective.
      li.title = o.detail ? (o.title + " \u2014 " + o.detail) : (o.title || "");
      if (typeof o.goal === "number" && o.goal > 0) {
        p.prog.style.display = "";
        const count = Math.max(0, Math.min(o.goal, o.count || 0));
        const pct = Math.round((count / o.goal) * 100);
        const prev = p.fill.dataset.pct;
        p.fill.style.width = pct + "%";
        p.fill.dataset.pct = String(pct);
        p.num.textContent = count + "/" + o.goal;
        if (prev !== undefined && prev !== String(pct)) {
          li.classList.remove("ticked"); void li.offsetWidth; li.classList.add("ticked");
        }
      } else {
        p.prog.style.display = "none";
      }
    }

    function displayList() {
      return items.slice().sort((a, b) => {
        const oa = (KIND_META[a.kind] || {}).order ?? 9;
        const ob = (KIND_META[b.kind] || {}).order ?? 9;
        // Completed side-goals sink below active ones within a kind.
        const sa = a.status === "active" ? 0 : 1;
        const sb = b.status === "active" ? 0 : 1;
        if (oa !== ob) return oa - ob;
        if (sa !== sb) return sa - sb;
        return a.seq - b.seq;
      });
    }

    function render() {
      if (!el.objList) return;
      const list = displayList();
      const wanted = new Set(list.map((o) => o.id));
      for (const [id, node] of nodes) {
        if (!wanted.has(id)) { node.remove(); nodes.delete(id); }
      }
      list.forEach((o) => {
        let node = nodes.get(o.id);
        const fresh = !node;
        if (fresh) { node = makeNode(o); nodes.set(o.id, node); }
        paintNode(node, o);
        el.objList.appendChild(node); // reorder
        if (fresh && revealed && !prefersReducedMotion()) {
          node.classList.remove("entering"); void node.offsetWidth; node.classList.add("entering");
        }
      });
      if (el.objCount) {
        const done = items.filter((o) => o.status === "complete").length;
        el.objCount.textContent = done ? done + " done" : "";
      }
    }

    // "Revealed" now means the case EXISTS and the tally can show, not that a
    // panel is open over the scene. The sheet itself only appears when asked for
    // (O / the CASE rail button), so being told your goal costs one line.
    function reveal() {
      revealed = true;
      try { Evidence.reveal(); } catch (_) {}
      if (!open_) render();
      syncRailButton();
    }

    // Open / close the case sheet.
    function setOpen(next) {
      open_ = !!next;
      if (!el.objectivesHud) return;
      el.objectivesHud.classList.toggle("hidden", !open_);
      if (open_) {
        render();
        if (!prefersReducedMotion()) {
          el.objectivesHud.classList.remove("obj-in"); void el.objectivesHud.offsetWidth;
          el.objectivesHud.classList.add("obj-in");
        }
      }
      persist();
      syncRailButton();
    }

    function pulseHud() {
      if (!el.objectivesHud || prefersReducedMotion()) return;
      el.objectivesHud.classList.remove("pulse"); void el.objectivesHud.offsetWidth;
      el.objectivesHud.classList.add("pulse");
    }

    function syncRailButton() {
      if (el.btnObjectives) el.btnObjectives.classList.toggle("active", open_);
    }

    // Mirror an objective moment into the STORY LOG (the run chronicle), so the
    // objectives live in the same tech stack as every other beat and the log
    // reads like a real case record: "OBJECTIVE ▸ …" / "COMPLETE ✓ …".
    let beatId = -100000;
    function logBeat(type, mark, title) {
      try { appendProse({ id: beatId--, type, content: mark + " **" + title + "**" }); } catch (_) {}
    }

    // ---- Mutations ----
    // spec: { id, kind, title, detail, count, goal, quiet }
    function add(spec) {
      if (!spec || !spec.id) return null;
      if (has(spec.id)) { update(spec.id, spec); return get(spec.id); }
      const o = {
        id: spec.id,
        kind: spec.kind || "field",
        title: spec.title || "",
        detail: spec.detail || "",
        status: "active",
        count: spec.count || 0,
        goal: typeof spec.goal === "number" ? spec.goal : null,
        seq: seq++,
      };
      items.push(o);
      persist();
      if (revealed) render();
      if (!spec.quiet) {
        try { Sound.select(); } catch (_) {}
        try { Haptics.soft(); } catch (_) {}
        banner("New Objective", o.title, o.kind === "bonus" ? "bonus" : "");
        logBeat("objective_new", "\u25B8", o.title);
      }
      return o;
    }

    function update(id, patch) {
      const o = get(id);
      if (!o) return;
      if (patch.title != null) o.title = patch.title;
      if (patch.detail != null) o.detail = patch.detail;
      if (patch.count != null) o.count = patch.count;
      if (patch.goal != null) o.goal = patch.goal;
      persist();
      if (revealed) render();
    }

    function setProgress(id, count, goal) {
      const o = get(id);
      if (!o) return;
      const before = o.count;
      o.count = count;
      if (typeof goal === "number") o.goal = goal;
      persist();
      if (revealed) render();
      if (count > before) { try { Sound.scoreTick(); } catch (_) {} }
    }

    function complete(id, opts) {
      const o = get(id);
      if (!o || o.status === "complete") return;
      opts = opts || {};
      o.status = "complete";
      if (typeof o.goal === "number") o.count = o.goal;
      persist();
      if (revealed) { render(); pulseHud(); }
      if (!opts.quiet) {
        try { Sound.newSubject(); } catch (_) {}
        try { Haptics.select(); } catch (_) {}
        const cls = o.kind === "bonus" ? "bonus" : "complete";
        const KICKER = { field: "Photographed", bonus: "Done", lead: "Lead closed", primary: "Case closed" };
        banner(KICKER[o.kind] || "Objective Complete", o.title, cls);
        logBeat("objective_done", "\u2713", o.title);
      }
      // Side-goals file themselves away after a beat; the PRIMARY stays put
      // (its completion is the win, handled by the case overlay).
      if (o.kind !== "primary" && opts.archive !== false) scheduleArchive(id);
    }

    function fail(id) {
      const o = get(id);
      if (!o || o.status !== "active") return;
      o.status = "failed";
      persist();
      if (revealed) render();
      try { Sound.miss(); } catch (_) {}
      scheduleArchive(id);
    }

    function scheduleArchive(id) {
      if (archiveTimers.has(id)) clearTimeout(archiveTimers.get(id));
      archiveTimers.set(id, setTimeout(() => {
        archiveTimers.delete(id);
        const node = nodes.get(id);
        const finish = () => { items = items.filter((o) => o.id !== id); persist(); render(); };
        if (node && !prefersReducedMotion()) {
          node.classList.add("leaving");
          setTimeout(finish, 520);
        } else { finish(); }
      }, ARCHIVE_MS));
    }

    function remove(id) {
      items = items.filter((o) => o.id !== id);
      const node = nodes.get(id);
      if (node) { node.remove(); nodes.delete(id); }
      persist();
      if (revealed) render();
    }

    // ---- Collapse / toggle (O key + GOALS rail button) ----
    function toggleCollapsed() {
      if (!revealed) reveal();
      setOpen(!open_);
    }

    // ---- Generative LEAD (the evolving directive) ----
    const LEAD_ID = "lead";
    function setLead(title, detail) {
      title = (title || "").trim();
      if (!title) return;
      if (has(LEAD_ID)) {
        const o = get(LEAD_ID);
        const changed = norm(o.title) !== norm(title);
        update(LEAD_ID, { title, detail: detail || o.detail });
        if (changed && revealed) {
          const node = nodes.get(LEAD_ID);
          if (node && !prefersReducedMotion()) {
            node.classList.remove("entering"); void node.offsetWidth; node.classList.add("entering");
          }
          try { Sound.status(); } catch (_) {}
        }
      } else {
        add({ id: LEAD_ID, kind: "lead", title, detail: detail || "", quiet: true });
      }
    }

    // ---- Case primary — mirror the dossier census ----
    function syncCase() {
      const goal = (window.Evidence && Evidence.target && Evidence.target()) || 8;
      const count = (window.Evidence && Evidence.uniqueCount && Evidence.uniqueCount()) || 0;
      if (!has("case")) {
        add({
          id: "case", kind: "primary",
          title: "Take " + goal + " good photographs",
          detail: "Each has to be something new \u2014 the same subject twice only counts once",
          count, goal, quiet: true,
        });
      } else {
        setProgress("case", count, goal);
      }
      const o = get("case");
      // Completing the PRIMARY objective IS the win — let it land as a real,
      // visible objective completion (banner + chime + the tracker check) so
      // the case-closed screen reads as the payoff of the objective, not a
      // disconnected pop-up. maybeCloseCase() then shows the reward on a beat.
      if (o && o.status === "active" && count >= goal) complete("case");
    }

    // ---- Photo loop hooks ----
    // A subject the world model surfaced in the live scene → a field bounty.
    function offerField(label) {
      const l = norm(label);
      if (!l) return;
      const id = "field:" + l;
      if (has(id)) return;                                   // already tracked
      if (window.Evidence && Evidence.isSpent && Evidence.isSpent(l)) return; // already documented
      if (window.Evidence && Evidence.isNew && !Evidence.isNew(l)) return;    // already on file
      if (activeFieldCount() >= MAX_FIELD) return;           // keep the board tidy
      add({ id, kind: "field", title: fieldTitle(l, label), detail: "" });
    }

    // A little procedural variety in the phrasing so the board doesn't read as
    // a wall of identical "Document the …" lines. Stable per-label hash so a
    // subject always draws the same verb (no flicker if it's re-offered) while
    // different subjects vary.
    const FIELD_VERBS = ["Document", "Photograph", "Capture", "Get a shot of"];
    function hashStr(s) {
      let h = 0;
      for (let i = 0; i < s.length; i++) { h = (h * 31 + s.charCodeAt(i)) | 0; }
      return h;
    }
    function fieldTitle(normLabel, rawLabel) {
      const verb = FIELD_VERBS[Math.abs(hashStr(normLabel)) % FIELD_VERBS.length];
      return verb + " the " + rawLabel;
    }

    // Drop the longest-standing active field bounty to make room for one the
    // player just deliberately tapped on — a full board should never be the
    // reason a targeted objective silently fails to appear.
    function evictOldestField(excludeId) {
      const candidates = items.filter((o) => o.kind === "field" && o.status === "active" && o.id !== excludeId);
      if (!candidates.length) return false;
      candidates.sort((a, b) => a.seq - b.seq);
      remove(candidates[0].id);
      return true;
    }

    // A subject the player TAPPED ON — the "investigation" path. Unlike
    // offerField (a passive, ambient offer that can be silently skipped),
    // this ALWAYS resolves to a clear outcome: a fresh/re-affirmed objective,
    // or an explicit "already documented" signal the caller can surface as a
    // scan hint. Never silently a no-op — that silence is what made tapping a
    // subject feel like a coin flip.
    function offerFieldTargeted(label) {
      const l = norm(label);
      if (!l) return "none";
      if (window.Evidence && Evidence.isSpent && Evidence.isSpent(l)) return "documented";
      const id = "field:" + l;
      if (has(id)) {
        const o = get(id);
        if (o.status === "active") {
          // Re-affirm rather than doing nothing: replay the item's entrance
          // flourish + a whole-board pulse so re-tapping the same subject
          // always reads as "yes, that's still the objective."
          const node = nodes.get(id);
          if (node && !prefersReducedMotion()) {
            node.classList.remove("entering"); void node.offsetWidth; node.classList.add("entering");
          }
          pulseHud();
          try { Sound.select(); } catch (_) {}
          try { Haptics.soft && Haptics.soft(); } catch (_) {}
        }
        return "targeted";
      }
      if (activeFieldCount() >= MAX_FIELD) evictOldestField(id);
      add({ id, kind: "field", title: fieldTitle(l, label), detail: "" });
      pulseHud();
      return "targeted";
    }

    // objects: the latest /api/detect results. opts.target (optional): the
    // raw label of whatever the player TAPPED ON this pass (resolved by the
    // caller against the tap point) — it always gets offerFieldTargeted's
    // guaranteed treatment instead of competing with everything else for a
    // spot by box size. Returns offerFieldTargeted's result ("targeted" /
    // "documented") when a target was given, else null.
    function onDetect(objects, opts) {
      if (!revealed || !Array.isArray(objects)) return null;
      opts = opts || {};
      const targetLabel = opts.target || null;
      const targetNorm = targetLabel ? norm(targetLabel) : null;
      const present = new Set(objects.map((o) => norm(o.label)).filter(Boolean));
      // Retire field bounties whose subject has left the frame for several
      // passes, so the board tracks what's actually in view and never clogs its
      // slots with stale goals you've already moved past.
      items.filter((o) => o.kind === "field" && o.status === "active").forEach((o) => {
        const l = o.id.slice(6); // strip "field:"
        if (present.has(l)) { o.missPasses = 0; }
        else if ((o.missPasses = (o.missPasses || 0) + 1) >= STALE_MISSES) remove(o.id);
      });
      // Offer the most prominent NEW subjects first (larger boxes read as
      // closer) — this ambient sweep is unchanged; the tapped target (if any)
      // is excluded here and handled explicitly below so it never has to
      // compete for one of the MAX_FIELD slots.
      objects.slice()
        .sort((a, b) => ((b.w || 0) * (b.h || 0)) - ((a.w || 0) * (a.h || 0)))
        .forEach((o) => { if (!targetNorm || norm(o.label) !== targetNorm) offerField(o.label); });
      return targetLabel ? offerFieldTargeted(targetLabel) : null;
    }

    // Called from the photo receipt as each subject is appraised.
    function onSubjectDocumented(label, opts) {
      opts = opts || {};
      const id = "field:" + norm(label);
      if (has(id)) complete(id);
      if (opts.rare && has("bonus:rare")) complete("bonus:rare");
      syncCase();
    }

    function onFocusGrade(grade) {
      if (grade === "PERFECT" && has("bonus:perfect")) complete("bonus:perfect");
    }

    // ---- Lifecycle ----
    function reset() {
      items = [];
      nodes.forEach((n) => n.remove());
      nodes = new Map();
      archiveTimers.forEach((t) => clearTimeout(t));
      archiveTimers.clear();
      clearTimeout(bannerTimer);
      if (el.objBanner) { el.objBanner.classList.add("hidden"); el.objBanner.classList.remove("show"); }
      seq = 0;
      revealed = false;
      if (el.objectivesHud) { el.objectivesHud.classList.add("hidden"); el.objectivesHud.classList.remove("obj-in", "pulse"); }
      // Seed the run's spine + standing challenges. The LEAD is filled in by the
      // first directive refresh; the CASE mirrors the dossier.
      syncCase();
      // Seed the LEAD so the board reads complete the instant it's revealed;
      // refreshDirective() upgrades it with a world-grounded directive shortly.
      add({ id: LEAD_ID, kind: "lead", title: "Look around",
            detail: "Find something worth photographing.", quiet: true });
      add({ id: "bonus:rare", kind: "bonus", title: "Photograph something rare",
            detail: "Something you won't get a second chance at", quiet: true });
      add({ id: "bonus:perfect", kind: "bonus", title: "Get one perfectly sharp",
            detail: "Centred and in focus", quiet: true });
      persist();
      syncRailButton();
    }

    return {
      reveal, reset, render,
      add, update, remove, complete, fail, setProgress,
      setLead, syncCase, onDetect, onSubjectDocumented, onFocusGrade,
      toggle: toggleCollapsed,
      has, get, list: () => items.slice(),
      completedCount: () => items.filter((o) => o.status === "complete").length,
      isRevealed: () => revealed,
    };
  })();
  window.Objectives = Objectives;

  // ------------------------------------------------------------------
  // Directive — fetch the GENERATIVE "current lead" for the objectives tracker.
  // Best-effort: the client always synthesizes a solid fallback from the live
  // scene + phase so the LEAD reads well even offline, then upgrades it with a
  // model-authored directive from /api/objectives when the server can provide
  // one. Throttled to at most once per turn.
  // ------------------------------------------------------------------
  const LEAD_FALLBACKS = {
    normal: [
      { t: "Look around", d: "Find something worth photographing." },
      { t: "Document what you find", d: "Photograph anything that tells the story." },
      { t: "Press deeper", d: "Follow the scene to whatever it's hiding." },
    ],
    escalating: [
      { t: "Keep the camera close", d: "Something is shifting — capture it before it turns." },
      { t: "Track the disturbance", d: "Get evidence of what's changing around you." },
    ],
    critical: [
      { t: "Get what you can and move", d: "It's turning against you — shoot and stay alive." },
      { t: "Find a way out", d: "Document the threat, then break contact." },
    ],
    deceased: [
      { t: "The transmission ends", d: "Start a new case to keep documenting." },
    ],
  };

  function synthLead() {
    const phase = String((state.lastStatus && state.lastStatus.phase) || "normal").trim().toLowerCase();
    const pool = LEAD_FALLBACKS[phase] || LEAD_FALLBACKS.normal;
    // Bias by turn so it drifts through the pool as the story advances.
    const turn = (state.lastStatus && state.lastStatus.turn) || 0;
    const pick = pool[turn % pool.length];
    return { title: pick.t, detail: pick.d };
  }

  async function refreshDirective(force) {
    if (state.gameOver) return;
    const turn = (state.lastStatus && state.lastStatus.turn) || 0;
    if (!force && state.objDirectiveTurn === turn) return; // one refresh per turn
    state.objDirectiveTurn = turn;
    // Always give the tracker a good line immediately.
    const fb = synthLead();
    Objectives.setLead(fb.title, fb.detail);
    if (state.objDirectiveBusy) return;
    state.objDirectiveBusy = true;
    try {
      const res = await getJSON("/api/objectives");
      if (res && res.lead && String(res.lead).trim()) {
        Objectives.setLead(String(res.lead), res.detail ? String(res.detail) : "");
      }
    } catch (_) {
      /* keep the fallback — the tracker never depends on the server */
    } finally {
      state.objDirectiveBusy = false;
    }
  }

  // ------------------------------------------------------------------
  // Photo — what happens after you press the shutter.
  //
  // This used to print a scored receipt: every object in frame itemised with
  // points, a star rating, NEW / RARE / ON FILE badges, bonus rows for framing
  // and focus, a running subtotal, and a rubber stamp that graded the shot
  // SNAPSHOT through SMOKING GUN. All of it landed on top of the one moment in
  // the game that should feel like looking at a photograph you just took.
  //
  // Now the picture holds full-screen on its own (see presentCapture), and when
  // it fades we say one line: what you kept. The scoring still happens — it's
  // what ranks the run at the end — it just stops narrating itself shot by shot.
  // ------------------------------------------------------------------
  const Photo = (function () {
    const HOLD_MS = 2100;         // how long the "filed" line lingers
    const BASE_PER_INTEREST = 40; // points per interest point (1..5)
    const NOVELTY_BONUS = 60;     // first time a subject is photographed this run
    const RARE_BONUS = 80;        // a striking 5-star "rare find" premium
    const CONSOLATION = 10;       // an unreadable shot still pays a little
    // FOCUS grade → payout multiplier. A crisp, centered frame is worth far more
    // than a soft one; this is still where shooting skill turns into rank.
    const FOCUS_MULT = { PERFECT: 1.6, SHARP: 1.15, SOFT: 0.6 };

    function clearTimers() {
      state.receiptTimers.forEach((t) => clearTimeout(t));
      state.receiptTimers = [];
    }
    function later(fn, ms) { const t = setTimeout(fn, ms); state.receiptTimers.push(t); return t; }

    function els() {
      const r = el.photoFiled;
      if (!r) return null;
      return { root: r, text: r.querySelector(".filed-text") };
    }

    function hide() {
      const r = el.photoFiled;
      if (!r) return;
      clearTimers();
      r.classList.remove("show");
      later(() => r.classList.add("hidden"), 320);
    }

    // Entry point from the two capture paths.
    // `spec` = {texture, region, kind, label, zoom, focus, focusGrade}.
    function capture(spec) {
      if (!spec || !spec.texture) return;
      // OUT OF FILM: exposures are a finite resource. When the roll is spent you
      // can't shoot — a hard stop that makes every earlier shot a real decision.
      if (window.Evidence && Evidence.hasFilm && !Evidence.hasFilm()) {
        try { Sound.miss && Sound.miss(); } catch (_) {}
        const closed = (window.Evidence && Evidence.uniqueCount && Evidence.target)
          ? Evidence.uniqueCount() >= Evidence.target() : false;
        try {
          showRendererToast(closed
            ? "Out of film \u2014 the case is already closed."
            : "Out of film \u2014 no shots left this roll. Press R for a new case.", 3600);
        } catch (_) {}
        return;
      }
      try { Investigations.store(spec); } catch (_) {}
      appraise(spec.texture, {
        zoom: spec.zoom || 1, focus: spec.focus,
        grade: spec.focusGrade, subject: spec.subject,
      });
    }

    // Score the shot silently, then say one line about it. No UI opens until the
    // answer is back, so nothing competes with the photo while it's on screen.
    function appraise(texture, shot) {
      shot = shot || {};
      const token = ++state.receiptToken; // invalidate any older appraisal
      clearTimers();
      postJSON("/api/photo", { frame: texture })
        .then((res) => { if (token === state.receiptToken) settle(token, res || {}, shot); })
        .catch((err) => {
          console.warn("[standalone] photo appraise failed:", err);
          if (token === state.receiptToken) settle(token, { items: [] }, shot);
        });
    }

    // One line, and the scoring behind it.
    function settle(token, appraisal, shot) {
      shot = shot || {};
      if (token !== state.receiptToken) return;
      const items = Array.isArray(appraisal.items) ? appraisal.items : [];
      const subject = shot.subject || null;
      Evidence.addShot();

      if (!items.length) {
        // Nothing legible. The POI is NOT spent, so you can line the shot up
        // again — and the copy says that rather than grading you for it.
        Evidence.add(CONSOLATION);
        say("Nothing came out \u2014 try again, closer.", { faint: true });
        return;
      }

      // Real evidence developed, so NOW the subject counts as documented — the
      // credit and the document-once lockout stay in lockstep.
      if (subject) {
        Evidence.spend(subject);
        try { Objectives.onSubjectDocumented(subject, { rare: false }); } catch (_) {}
        if (state.touchMode === "aim") layoutPhotoTargets(); // dim its target to a check
      }

      let shotTotal = 0;
      let newCount = 0;
      const kept = [];
      items.forEach((it) => {
        const label = String(it.label || "?");
        const interest = Math.max(1, Math.min(5, Number(it.interest) || 2));
        const isNew = Evidence.isNew(label);
        const isRare = interest >= 5;
        // DOCUMENT ONCE: a subject already in the dossier is worth nothing, so
        // there's no farming the same evidence.
        const pts = isNew
          ? interest * BASE_PER_INTEREST + NOVELTY_BONUS + (isRare ? RARE_BONUS : 0)
          : 0;
        Evidence.markSeen(label);
        if (isNew) {
          newCount += 1;
          shotTotal += pts;
          kept.push(label);
          try { Evidence.pulseSubject(); } catch (_) {}
          if (isRare) { try { Objectives.complete("bonus:rare"); } catch (_) {} }
        }
      });

      // Composition and framing still pay, quietly.
      if (newCount >= 2) shotTotal += Math.round(shotTotal * 0.15 * (newCount - 1));
      const zoom = shot.zoom || 1;
      if (zoom > 1.3) shotTotal += Math.round(shotTotal * 0.12 * Math.min(1.5, zoom - 1));
      const mult = FOCUS_MULT[shot.grade];
      if (mult) shotTotal += Math.round(shotTotal * (mult - 1));
      if (shotTotal > 0) Evidence.add(shotTotal);
      if (shot.grade) { try { Objectives.onFocusGrade(shot.grade); } catch (_) {} }
      try { Objectives.syncCase(); } catch (_) {}

      if (!kept.length) {
        // Everything in frame is already in the file. Say so plainly instead of
        // stamping ALREADY ON FILE across the picture.
        say("Already in the file.", { faint: true });
        return;
      }
      // Name what you actually kept — the photograph is the reward, this is the
      // receipt for it. Two subjects read as a list; more than that would turn
      // back into the inventory this replaced.
      const named = kept.length <= 2
        ? kept.join(" and ")
        : kept.slice(0, 2).join(", ") + " +" + (kept.length - 2);
      say("Filed \u2014 " + named);
      try { maybeCloseCase(); } catch (_) {}
    }

    function say(text, opts) {
      opts = opts || {};
      const parts = els();
      if (!parts) return;
      parts.text.textContent = text;
      parts.root.classList.toggle("faint", !!opts.faint);
      parts.root.classList.remove("hidden");
      void parts.root.offsetWidth;
      parts.root.classList.add("show");
      try { Sound.receiptOpen(); } catch (_) {}
      later(() => hide(), HOLD_MS);
    }

    return { capture, hide };
  })();
  window.Photo = Photo;

  // ------------------------------------------------------------------
  // Win condition — CLOSE THE CASE. Documenting enough DISTINCT subjects
  // (the dossier census) completes the assignment and grades the run with a
  // photographer's RANK. Fires once per run; the player can start a new case
  // or dismiss and keep shooting the same world.
  // ------------------------------------------------------------------
  // One short line. These used to be full sentences of self-congratulation
  // ("A flawless dossier. Every subject catalogued, the whole picture
  // developed.") stacked under four stat readouts.
  const CASE_FLAVORS = {
    S: "Not a frame wasted.",
    A: "Sharp work. The story's in the details you caught.",
    B: "Enough here to make the pattern undeniable.",
    C: "It holds together. A few more angles and it would sing.",
    D: "Thin, but the shutter never lies.",
  };

  // The reward screen is the payoff of COMPLETING THE PRIMARY OBJECTIVE (Close
  // the Case), not a standalone subject-count check — so it stays in lockstep
  // with the objectives tracker. We wait for the "Case File Complete" objective
  // beat (banner + chime + the tracker check) to land first, THEN reveal the
  // case-closed screen, so the win reads as earned rather than abrupt.
  const CASE_WIN_DELAY_MS = 1800;
  function primaryComplete() {
    const o = (window.Objectives && Objectives.get) ? Objectives.get("case") : null;
    if (o) return o.status === "complete";
    return Evidence.uniqueCount() >= Evidence.target(); // fallback if the tracker is unavailable
  }

  function maybeCloseCase() {
    if (state.caseWon) return;
    if (!primaryComplete()) return;
    state.caseWon = true;
    // Let the objective completion + its beat register before the reward.
    setTimeout(showCaseWin, CASE_WIN_DELAY_MS);
  }

  function showCaseWin() {
    if (!el.caseOverlay || !state.caseWon) return; // a reset may have cancelled it
    try { Photo.hide(); } catch (_) {} // clear the receipt so the win screen is clean
    const rank = Evidence.rank();
    if (el.caseRankLetter) el.caseRankLetter.textContent = rank;
    if (el.caseSubjects) el.caseSubjects.textContent = String(Evidence.uniqueCount());
    if (el.caseEvidence) el.caseEvidence.textContent = Evidence.total().toLocaleString("en-US");
    if (el.caseShots) el.caseShots.textContent = String(Evidence.shots());
    // Tie the win explicitly to the objectives cleared this run.
    if (el.caseObjectives) {
      let done = 0;
      try { done = Objectives.completedCount(); } catch (_) {}
      el.caseObjectives.textContent = String(done);
    }
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
    // Gate on "is there anything to photograph", not "is realtime on". Capture
    // has worked on stills for a while (captureSceneRegion falls back to the
    // scene <img>, and the C hotkey already used it), but the PHOTO tool itself
    // was still locked behind the reactor check — so the whole camera loop, the
    // dossier, and the case win were unreachable whenever realtime dropped to
    // stills, which is exactly when Reactor is full or unconfigured.
    if (!currentSourceSize()) {
      showRendererToast("Nothing to photograph yet");
      return;
    }
    closeScan(); // the two scene instruments are mutually exclusive
    state.touchMode = "aim";
    if (el.realtimeBtn) el.realtimeBtn.classList.add("aiming");
    document.body.classList.add("touch-aiming");
    // Open on the full 16:9 frame — the whole scene IS the shot. Pinch / scroll
    // crops the capture region tighter; the scene is never magnified.
    state.photoPointers.clear();
    state.pinchBase = null;
    state.pinchActive = false;
    // Open dead wide, then immediately + smoothly PUSH IN to a tighter FOV so
    // raising the camera feels like snapping to a scope in an FPS — not a flat,
    // fully-wide viewfinder you then have to fiddle with (see pushInToArmed).
    setPhotoZoom(PHOTO_ZOOM_MIN, { silent: true, force: true });
    try { Evidence.reveal(); } catch (_) {} // surface the CASE FILE goal on pickup
    // The viewfinder is centered; seed the aim point at center for tap/drag math.
    moveReticle(window.innerWidth / 2, window.innerHeight / 2);
    if (el.touchLayer) el.touchLayer.classList.remove("hidden");
    pushInToArmed(); // the cinematic push-in
    startPhotoTargeting(); // begin surfacing photographable subjects
    // Raising the camera: a servo whir + haptic clunk so activating photo mode
    // lands with weight. NO screen shake on entry — the shake is reserved for the
    // moment a shot fires (see photoKick in the capture path).
    try { Sound.cameraOn(); } catch (_) {}
    try { Haptics.camera(); } catch (_) {}
  }

  // ------------------------------------------------------------------
  // Immersive viewfinder framing — scroll (mouse) or pinch (touch) ZOOM does NOT
  // magnify the scene. The scene always shows true-to-capture (1:1); zooming
  // simply ADJUSTS THE CAPTURE REGION: a centered 16:9 window that crops tighter
  // into the live view. A letterbox mask dims everything outside that window, so
  // what you see IS the photo you'll get.
  // ------------------------------------------------------------------
  const PHOTO_ZOOM_MIN = 1.0;    // widest: the capture region = the whole 16:9 frame
  const PHOTO_ZOOM_MAX = 3.0;    // tightest crop
  const PHOTO_ZOOM_ARMED = 1.6;  // raising the camera pushes in to this tighter FOV
  const PHOTO_PUSHIN_MS = 620;   // duration of the cinematic push-in on arming

  function clampZoom(z) { return Math.max(PHOTO_ZOOM_MIN, Math.min(PHOTO_ZOOM_MAX, z)); }

  const FRAME_ASPECT = 16 / 9;   // capture frame aspect ratio

  // Viewport center — the capture region is a FIXED, centered viewfinder now, so
  // framing is always about the middle of the screen (no roaming reticle).
  function captureCenter() {
    return { x: window.innerWidth / 2, y: window.innerHeight / 2 };
  }

  // The largest 16:9 rectangle that fits INSIDE the viewport (contain), centered.
  // This is the full-frame capture region at 1×: it lines up with the letterboxed
  // scene on portrait phones and fills the screen on 16:9 displays.
  function frameFitPx() {
    const W = window.innerWidth, H = window.innerHeight;
    let w = W, h = w / FRAME_ASPECT;
    if (h > H) { h = H; w = h * FRAME_ASPECT; }
    return { w, h };
  }

  // The on-screen capture frame is a CONSTANT centered 16:9 window — pushing in
  // does NOT shrink it; it magnifies the scene inside (see sceneScale), so the
  // whole frame shows the cropped-in view. The captured crop is recovered by
  // inverting that magnification in screenToNorm. Returns { w, h } px.
  function frameBoxPx() {
    const fit = frameFitPx();
    return { w: Math.round(fit.w), h: Math.round(fit.h) };
  }

  // Pushing IN magnifies the scene about the center so the cropped area fills the
  // frame — it genuinely feels like you're looking at (and can explore) the
  // cropped-in image, not a shrinking window. The capture-crop math inverts this
  // magnification in screenToNorm, so the shot matches exactly what's framed.
  function sceneScale() {
    const z = state.touchMode ? (state.photoZoom || 1) : 1;
    return Math.max(1, z);
  }

  // The scene transform currently applied (identity unless the camera is armed).
  // Centralized so the capture crop math can invert it. The transform magnifies
  // the scene by `scale` and pins a FOCAL scene point (px,py — the point you've
  // panned to) at the frame CENTER (cx,cy). Forward map of an untransformed
  // screen point s: s' = scale*(s - focal) + center. Inverse: s = focal +
  // (s' - center)/scale. With no pan the focal IS the center (scale-about-middle).
  function getSceneTransform() {
    const scale = sceneScale();
    const c = captureCenter();
    const p = (scale > 1 && state.panFocus) ? state.panFocus : c;
    return { scale, px: p.x, py: p.y, cx: c.x, cy: c.y };
  }

  // Express the magnify+pan as `translate(t) scale(z)` (transform-origin 0 0),
  // where t = center - z*focal. Only the translate changes as you pan, so the
  // CSS transform transition can smoothly glide the view.
  function applySceneTransform() {
    const t = getSceneTransform();
    const z = t.scale;
    let val = "";
    if (z !== 1) {
      const tx = (t.cx - z * t.px).toFixed(2);
      const ty = (t.cy - z * t.py).toFixed(2);
      val = `translate(${tx}px, ${ty}px) scale(${z.toFixed(4)})`;
    }
    [el.sceneA, el.sceneB, el.reactorVideo, el.reactorFreeze].forEach((n) => {
      if (n) n.style.transform = val;
    });
    if (el.touchZoom) el.touchZoom.innerHTML = (state.photoZoom || 1).toFixed(1) + "&times;";
  }

  // Displayed media rect (px) under the current object-fit — where the scene
  // pixels actually sit on screen. Used to clamp panning to the scene bounds.
  function displayedMediaRect() {
    const W = window.innerWidth, H = window.innerHeight;
    const size = currentSourceSize();
    if (!size || !size.w || !size.h) return { ox: 0, oy: 0, dw: W, dh: H };
    const scale = mediaFitScale(W, H, size.w, size.h);
    const dw = size.w * scale, dh = size.h * scale;
    return { ox: (W - dw) / 2, oy: (H - dh) / 2, dw, dh };
  }

  // Keep the panned focal point inside the scene so the (fixed, centered) 16:9
  // frame never reveals a black edge. At 1x the focal is pinned to center.
  function clampPanFocus() {
    const z = sceneScale();
    const c = captureCenter();
    if (z <= 1) { state.panFocus = { x: c.x, y: c.y }; return; }
    const fit = frameFitPx();
    const m = displayedMediaRect();
    const halfW = (fit.w / 2) / z, halfH = (fit.h / 2) / z;
    const minX = m.ox + halfW, maxX = m.ox + m.dw - halfW;
    const minY = m.oy + halfH, maxY = m.oy + m.dh - halfH;
    const p = state.panFocus || { x: c.x, y: c.y };
    state.panFocus = {
      x: (minX <= maxX) ? Math.max(minX, Math.min(maxX, p.x)) : (m.ox + m.dw / 2),
      y: (minY <= maxY) ? Math.max(minY, Math.min(maxY, p.y)) : (m.oy + m.dh / 2),
    };
  }

  // Drag-to-pan the magnified view (mobile touch drag + desktop mouse drag).
  // Content follows the finger: dragging right reveals content to the left.
  function panBy(dx, dy) {
    const z = sceneScale();
    if (z <= 1) return; // nothing to explore at full frame
    const c = captureCenter();
    const p = state.panFocus || { x: c.x, y: c.y };
    state.panFocus = { x: p.x - dx / z, y: p.y - dy / z };
    clampPanFocus();
    applySceneTransform();
    updateDofMask();
    if (state.touchMode === "aim") layoutPhotoTargets();
  }

  // FPS-style mouselook: while pushed in, the framed region simply FOLLOWS the
  // cursor — no clicking, no dragging. The pointer's position within the frame
  // maps absolutely onto the pan range (cursor at the left edge looks left, at
  // the top looks up, dead-center recenters), and the eased CSS transform makes
  // the view glide there smoothly. This is what makes aiming feel like sweeping
  // a scope instead of hauling the scene around by hand. Caller re-lays the
  // markers/reticle (via moveReticle) so we skip that here.
  function lookAt(x, y) {
    const z = sceneScale();
    if (z <= 1) return; // nothing hidden to look toward at the full frame
    const fit = frameFitPx();
    const c = captureCenter();
    const m = displayedMediaRect();
    const halfW = (fit.w / 2) / z, halfH = (fit.h / 2) / z;
    const minX = m.ox + halfW, maxX = m.ox + m.dw - halfW;
    const minY = m.oy + halfH, maxY = m.oy + m.dh - halfH;
    // Cursor position as a 0..1 fraction across the centered capture frame.
    const fx = Math.max(0, Math.min(1, (x - (c.x - fit.w / 2)) / fit.w));
    const fy = Math.max(0, Math.min(1, (y - (c.y - fit.h / 2)) / fit.h));
    state.panFocus = {
      x: (minX <= maxX) ? (minX + fx * (maxX - minX)) : (m.ox + m.dw / 2),
      y: (minY <= maxY) ? (minY + fy * (maxY - minY)) : (m.oy + m.dh / 2),
    };
    applySceneTransform();
    updateDofMask();
  }

  // Drive the LETTERBOX MASK: a centered 16:9 window sized to the current capture
  // region. #touch-dof is styled as a transparent rectangle with a huge dimming
  // box-shadow, so everything OUTSIDE the window darkens — the bright area you
  // see is exactly the photo you'll take. Only width/height change (it's centered
  // in CSS), so this is a cheap compositor update on every zoom frame.
  function updateDofMask() {
    if (!el.touchDof || !state.touchMode) return;
    const b = frameBoxPx();
    const s = el.touchDof.style;
    s.width = b.w + "px";
    s.height = b.h + "px";
  }

  function setPhotoZoom(z, opts) {
    opts = opts || {};
    const clamped = clampZoom(z);
    if (!opts.force && Math.abs(clamped - state.photoZoom) < 0.004) return;
    state.photoZoom = clamped;
    // Re-clamp the pan focal to the new zoom (a lower zoom shrinks the pan range
    // and eases the view back toward center; 1x re-centers).
    clampPanFocus();
    applySceneTransform();
    layoutCaptureFrame();
    updateDofMask();
    // While CONTINUOUS gestures (pinch, wheel spin) fire, skip the per-frame
    // marker re-layout, the ".bump" reflow, and the audio tick — they were the
    // source of the pinch stutter on phones. The pinch handler calls back with
    // `continuous: true`; discrete zoom steps get the full flourish.
    if (!opts.continuous && state.touchMode === "aim") layoutPhotoTargets();
    if (!opts.continuous && el.touchZoom) { // pop the readout only on discrete zoom steps
      el.touchZoom.classList.remove("bump");
      void el.touchZoom.offsetWidth;
      el.touchZoom.classList.add("bump");
    }
    if (!opts.silent && !opts.continuous) {
      try { Sound.zoom((clamped - PHOTO_ZOOM_MIN) / (PHOTO_ZOOM_MAX - PHOTO_ZOOM_MIN)); } catch (_) {}
    }
  }

  // The cinematic push-in. Raising the camera opens on the full wide frame, then
  // this glides the zoom into PHOTO_ZOOM_ARMED over a long, eased transform so it
  // reads as "snapping to a scope" — immediate, but smooth. A dedicated
  // `photo-pushing` class swaps in the longer easing for just this beat; normal
  // zoom (wheel/pinch) keeps its snappy 0.16s chase. Under reduced-motion we just
  // land on the armed FOV with no animation.
  function pushInToArmed() {
    if (prefersReducedMotion()) {
      setPhotoZoom(PHOTO_ZOOM_ARMED, { silent: true, force: true });
      return;
    }
    document.body.classList.add("photo-pushing");
    // Commit the wide (1×) frame first, THEN kick the zoom, so the transform has
    // a real starting value to ease from (a two-rAF settle avoids the browser
    // collapsing both into one non-animated jump).
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (state.touchMode !== "aim") { document.body.classList.remove("photo-pushing"); return; }
        setPhotoZoom(PHOTO_ZOOM_ARMED, { silent: true, force: true });
      });
    });
    setTimeout(() => document.body.classList.remove("photo-pushing"), PHOTO_PUSHIN_MS + 40);
  }

  function clearSceneZoom() {
    state.photoZoom = 1;
    state.panFocus = null; // drop any pan so the next arming opens centered
    [el.sceneA, el.sceneB, el.reactorVideo, el.reactorFreeze].forEach((n) => {
      if (n) n.style.transform = "";
    });
    // Reset the letterbox mask so it doesn't flash the OLD crop on re-open
    // (opacity is CSS-driven off `.touch-aiming`, but the geometry sticks).
    if (el.touchDof) {
      el.touchDof.style.removeProperty("width");
      el.touchDof.style.removeProperty("height");
    }
  }

  function photoPointerDist() {
    const pts = [...state.photoPointers.values()];
    if (pts.length < 2) return 0;
    return Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
  }

  // Midpoint of the two active pinch fingers (in viewport coords). Returns
  // null unless there are 2 pointers down — pinch handling always guards on
  // pinchBase, so this only gets called when the check passed.
  function photoPointerCentroid() {
    const pts = [...state.photoPointers.values()];
    if (pts.length < 2) return null;
    return { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 };
  }

  // Mouse wheel = smooth multiplicative zoom (down = out, up = in). Wheel is
  // a continuous stream of small deltas, so mark it that way — the zoom sound
  // and marker re-layout only fire on the FINAL tick (below), keeping the
  // scroll buttery.
  let _wheelTailTimer = 0;
  function onTouchWheel(e) {
    if (state.touchMode !== "aim") return;
    e.preventDefault();
    setPhotoZoom(state.photoZoom * Math.exp(-e.deltaY * 0.0015), { continuous: true });
    if (_wheelTailTimer) clearTimeout(_wheelTailTimer);
    _wheelTailTimer = setTimeout(() => {
      _wheelTailTimer = 0;
      // If the camera was closed between the last wheel tick and this tail,
      // don't fire a phantom zoom sound / marker relayout — just drop it.
      if (state.touchMode !== "aim") return;
      // Tail: fire the discrete-tick flourish (audio + marker layout + bump)
      // once the user has stopped spinning the wheel.
      setPhotoZoom(state.photoZoom, { force: true });
    }, 140);
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

  // ------------------------------------------------------------------
  // FOCUS — the skill layer. A detected subject in the frame is not enough; you
  // must actually FOCUS on it (center it under the reticle) for the shot to
  // count, and how well you center it grades the exposure. `focus` is 0..1: 1.0
  // is dead-center, 0 is at the very edge of the capture box. Two rules give the
  // photography teeth without being punishing:
  //   1) DOCUMENT-ONCE — a subject already in the case file can't be milked for
  //      points again; its target dims to a ✓ and no longer locks.
  //   2) A shot below FOCUS_MIN is "out of focus" and misses, so you can't just
  //      spray the shutter — you have to line the subject up.
  // Focus quality then multiplies the payout, so nailing a crisp, centered frame
  // of a NEW subject is the money shot.
  // ------------------------------------------------------------------
  const FOCUS_MIN = 0.16;      // below this the subject is too far off-center: a miss
  const FOCUS_SHARP = 0.42;    // SHARP focus threshold (a clean, well-aimed shot)
  const FOCUS_PERFECT = 0.70;  // PERFECT focus threshold (dead-center, crisp)
  function focusGrade(f) {
    if (f >= FOCUS_PERFECT) return "PERFECT";
    if (f >= FOCUS_SHARP) return "SHARP";
    return "SOFT";
  }

  // Map a normalized source point (0..1) to its on-screen position, accounting
  // for the object-fit:cover crop AND the current optical-zoom transform — the
  // inverse of screenToNorm, so markers sit exactly on the displayed subject.
  function normToPhotoScreen(cx, cy) {
    const W = window.innerWidth, H = window.innerHeight;
    const size = currentSourceSize();
    let x, y;
    if (!size || !size.w || !size.h) { x = cx * W; y = cy * H; }
    else {
      const scale = mediaFitScale(W, H, size.w, size.h);
      const dw = size.w * scale, dh = size.h * scale;
      const ox = (W - dw) / 2, oy = (H - dh) / 2;
      x = ox + cx * dw; y = oy + cy * dh;
    }
    const t = getSceneTransform();
    if (t && t.scale !== 1) {
      x = t.scale * (x - t.px) + t.cx;
      y = t.scale * (y - t.py) + t.cy;
    }
    return { x, y };
  }

  // Whether a subject has already been documented this run (case file). A
  // documented subject is "spent": it no longer locks, no longer scores.
  function isDocumented(label) {
    return Evidence.isSpent(label);
  }

  // Evaluate a would-be shot centered at (cx,cy) with a boxPx capture window.
  // Returns the "verdict" the capture path acts on:
  //   { ok, subject, focus, grade, centered, framedCount, reason, kind }
  // A shot is only worthy when a NEW (undocumented) subject is FRAMED and in
  // FOCUS. `focus` (0..1) is 1 at dead-center, 0 at the box edge.
  function evaluateShot(cx, cy, box) {
    const halfW = box.w / 2, halfH = box.h / 2;
    const targets = state.photoTargets || [];
    // Every detected subject whose center sits inside the capture box.
    const framed = [];
    targets.forEach((o) => {
      const p = normToPhotoScreen(o.cx, o.cy);
      const nx = Math.abs(p.x - cx) / halfW, ny = Math.abs(p.y - cy) / halfH;
      if (nx <= 1 && ny <= 1) {
        framed.push({ o, d: Math.hypot(p.x - cx, p.y - cy), off: Math.max(nx, ny) });
      }
    });
    if (!framed.length) {
      return { ok: false, framedCount: 0, kind: "empty",
        reason: targets.length
          ? "No subject in frame \u2014 line up a target"
          : "Nothing to photograph here \u2014 explore to find evidence" };
    }
    // Only undocumented subjects are worth points; drop anything already spent.
    const fresh = framed.filter((t) => !isDocumented(t.o.label));
    if (!fresh.length) {
      return { ok: false, framedCount: framed.length, kind: "documented",
        reason: "Already documented \u2014 find a new subject" };
    }
    // The nearest fresh subject is the one you're focusing on.
    fresh.sort((a, b) => a.d - b.d);
    const best = fresh[0];
    const focus = Math.max(0, Math.min(1, 1 - best.off));
    if (focus < FOCUS_MIN) {
      return { ok: false, framedCount: framed.length, kind: "blurry", focus,
        reason: "Out of focus \u2014 center your subject" };
    }
    return {
      ok: true, subject: best.o.label, focus, grade: focusGrade(focus),
      centered: focus >= FOCUS_SHARP, framedCount: framed.length, kind: "worthy",
    };
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
    // Never rebuild the target markers while a finger is down — a mid-drag
    // re-detect makes the brackets flicker/jump under the moving reticle.
    if (state.photoPointers.size > 0) { schedulePhotoDetect(); return; }
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
        try { Objectives.onDetect(state.photoTargets); } catch (_) {} // generative subjects → field bounties
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
      const check = document.createElement("span"); check.className = "pt-check"; check.textContent = "\u2713";
      const label = document.createElement("span");
      label.className = "pt-label";
      label.textContent = o.label || "";
      m.appendChild(tr); m.appendChild(bl); m.appendChild(check); m.appendChild(label);
      el.touchTargets.appendChild(m);
    });
    layoutPhotoTargets();
  }

  // Position every marker for the current zoom/pan. Documented subjects dim to a
  // ✓ and never lock; a NEW subject you center enough locks on — and the lock
  // reads its live FOCUS grade so you can feel the shot sharpen as you aim.
  function layoutPhotoTargets() {
    if (!el.touchTargets) return;
    const box = frameBoxPx();
    const halfW = box.w / 2, halfH = box.h / 2;
    const c = captureCenter();
    const rx = c.x, ry = c.y;
    const W = window.innerWidth, H = window.innerHeight;
    let lockedLabel = null, lockedFocus = 0, lockedDist = Infinity;
    Array.from(el.touchTargets.children).forEach((m) => {
      const o = m._obj;
      if (!o) return;
      const p = normToPhotoScreen(o.cx, o.cy);
      m.style.left = p.x + "px";
      m.style.top = p.y + "px";
      m.classList.toggle("off", p.x < -60 || p.x > W + 60 || p.y < -60 || p.y > H + 60);
      const spent = isDocumented(o.label);
      m.classList.toggle("documented", spent);
      const d = Math.hypot(p.x - rx, p.y - ry);
      const nx = Math.abs(p.x - rx) / halfW, ny = Math.abs(p.y - ry) / halfH;
      // A spent subject can be in the box but it never counts as "framed".
      const inFrame = !spent && nx <= 1 && ny <= 1;
      m.classList.toggle("in-frame", inFrame);
      if (inFrame && d < lockedDist) { lockedDist = d; lockedLabel = o.label; lockedFocus = Math.max(0, Math.min(1, 1 - Math.max(nx, ny))); }
    });
    const hadLock = !!state.photoLockedLabel;
    // A lock only counts once the subject is at least minimally in focus.
    const locked = lockedLabel && lockedFocus >= FOCUS_MIN;
    const grade = locked ? focusGrade(lockedFocus) : null;
    state.photoLockedLabel = locked ? lockedLabel : null;
    if (el.touchCaptureFrame) {
      el.touchCaptureFrame.classList.toggle("locked", !!locked);
      el.touchCaptureFrame.classList.remove("focus-soft", "focus-sharp", "focus-perfect");
      if (locked) el.touchCaptureFrame.classList.add("focus-" + grade.toLowerCase());
    }
    if (el.touchLock) {
      el.touchLock.classList.toggle("show", !!locked);
      el.touchLock.classList.remove("focus-soft", "focus-sharp", "focus-perfect");
      if (locked) {
        el.touchLock.classList.add("focus-" + grade.toLowerCase());
        const dot = grade === "PERFECT" ? "\u25C9" : grade === "SHARP" ? "\u25CE" : "\u25CB";
        el.touchLock.textContent = dot + " " + lockedLabel + " \u00b7 " + grade + " FOCUS";
      } else {
        el.touchLock.textContent = "";
      }
    }
    if (locked && !hadLock) { try { Sound.lock(); } catch (_) {} try { Haptics.lock(); } catch (_) {} } // a fresh lock chirps
  }

  // Empty-frame feedback: a quick red shake + soft tone + a plain-language nudge.
  function photoMiss(msg) {
    showRendererToast(msg);
    try { Sound.miss(); } catch (_) {}
    try { Haptics.miss(); } catch (_) {}
    if (el.touchCaptureFrame) {
      el.touchCaptureFrame.classList.remove("miss");
      void el.touchCaptureFrame.offsetWidth;
      el.touchCaptureFrame.classList.add("miss");
    }
  }

  // The capture frame is a FIXED, centered viewfinder — sizing it just means
  // resizing the letterbox mask for the current zoom (crop). No reticle to move.
  function layoutCaptureFrame() {
    updateDofMask();
  }

  // Track the pointer only so a TAP can be told from a DRAG (and to feed the
  // pinch centroid). Framing itself is centered + fixed, so moving the pointer
  // no longer roams a reticle or pans the scene — the viewfinder stays put.
  function moveReticle(x, y) {
    state.touchPoint = { x, y };
    layoutCaptureFrame();
    // Re-evaluate which subject is framed — skip mid-pinch (the per-frame walk of
    // every marker was a big source of pinch stutter on phones); onTouchUp
    // catches markers back up once the pinch ends.
    const pinching = state.photoPointers.size >= 2 && state.pinchBase;
    if (state.touchMode === "aim" && !pinching) layoutPhotoTargets();
  }

  // A press counts as a TAP (→ shoot) only if the finger barely moved and lifted
  // quickly. Anything more is a DRAG — the player is re-framing / panning and
  // must NOT trigger a capture on release (the old behavior that "felt bad").
  const TAP_MOVE_PX = 16;   // total travel under this reads as a tap, not a drag
  const TAP_MAX_MS = 700;   // and only if released within this window (touch only)
  // Movement past this begins an active drag: we switch the scene pan to 1:1
  // (no eased chase) so framing feels locked to the finger instead of shaky.
  const DRAG_START_PX = 6;

  function isTouchPointer(e) {
    return e && e.pointerType && e.pointerType !== "mouse";
  }

  // Pointer-driven so it works with mouse (hover) AND touch (drag) alike.
  // Two fingers down pinch-to-zoom instead of aiming; a lone pointer aims.
  function onTouchMove(e) {
    if (state.touchMode !== "aim") return;
    if (state.photoPointers.has(e.pointerId)) {
      state.photoPointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    }
    if (state.photoPointers.size >= 2 && state.pinchBase) {
      // Real pinch feel: the zoom rides the CENTROID of the two fingers, not
      // whichever finger touched down first. We move the aim point to the
      // centroid (so the transform origin, capture frame, and DOF halo all
      // track the pinch) and treat the update as a continuous gesture so we
      // don't rerun marker layout / play a zoom tick every frame — those were
      // the two biggest sources of pinch jank on phones.
      const c = photoPointerCentroid();
      if (c) moveReticle(c.x, c.y);
      const d = photoPointerDist();
      if (d > 0 && state.pinchBase.dist > 0) {
        setPhotoZoom(state.pinchBase.zoom * (d / state.pinchBase.dist), { continuous: true });
      }
      return; // don't fall through into the single-finger drag path
    }
    // DESKTOP FPS-LOOK: a mouse doesn't need to grab-and-drag to reframe. While
    // pushed in, the view simply follows the cursor (hover OR click-held alike),
    // so aiming feels like sweeping a scope. A click still shoots (onTouchUp),
    // it just no longer has to double as a pan handle. Touch keeps drag-to-pan
    // below since it has no hover.
    if (!isTouchPointer(e)) {
      lookAt(e.clientX, e.clientY);
      const gm = state.touchGesture;
      if (gm && gm.id === e.pointerId) { gm.lastX = e.clientX; gm.lastY = e.clientY; }
      moveReticle(e.clientX, e.clientY);
      return;
    }
    // Track travel for the active single-finger gesture so release can tell a
    // tap from a drag, and flip on 1:1 tracking the moment it becomes a drag.
    const g = state.touchGesture;
    if (g && g.id === e.pointerId) {
      g.moved = Math.max(g.moved, Math.hypot(e.clientX - g.x0, e.clientY - g.y0));
      if (g.moved > DRAG_START_PX && !g.dragging) {
        g.dragging = true;
        document.body.classList.add("photo-dragging");
      }
      if (g.dragging) {
        // Once dragging, PAN the magnified view so you can explore the scene
        // (mobile touch-drag + desktop mouse-drag). panBy no-ops at 1x (nothing
        // is hidden to pan to) and does its own re-layout, so return here.
        panBy(e.clientX - g.lastX, e.clientY - g.lastY);
        g.lastX = e.clientX;
        g.lastY = e.clientY;
        return;
      }
      g.lastX = e.clientX;
      g.lastY = e.clientY;
    }
    moveReticle(e.clientX, e.clientY);
  }

  // Press to aim; a clean single-finger TAP shoots, a DRAG only re-frames. A
  // second finger turns the gesture into a pinch (and suppresses the shot) so
  // zoom never fires a stray capture. On desktop, hover aims and a click shoots.
  // Right/middle click is a quick "exit the camera" gesture.
  function onTouchDown(e) {
    if (state.touchMode !== "aim") return;
    if (e.button && e.button !== 0) { e.preventDefault(); closeTouch(); return; }
    e.preventDefault();
    try { if (el.touchLayer && el.touchLayer.setPointerCapture) el.touchLayer.setPointerCapture(e.pointerId); } catch (_) {}
    state.photoPointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (state.photoPointers.size === 1) {
      state.touchGesture = { id: e.pointerId, x0: e.clientX, y0: e.clientY, t0: Date.now(), moved: 0, dragging: false, touch: isTouchPointer(e), lastX: e.clientX, lastY: e.clientY };
      moveReticle(e.clientX, e.clientY);
    } else if (state.photoPointers.size === 2) {
      // Second finger → pinch. Abandon the single-finger gesture so lifting
      // after a pinch never fires a capture.
      state.touchGesture = null;
      document.body.classList.remove("photo-dragging");
      state.pinchActive = true;
      state.pinchBase = { dist: photoPointerDist(), zoom: state.photoZoom };
      // Snap the scene/frame/DOF transitions off for the duration of the pinch
      // so everything tracks the fingers 1:1 — the eased CSS chase read as a
      // smeary, laggy pinch on touch.
      document.body.classList.add("photo-pinching");
      // Anchor the aim point (transform origin, capture frame, DOF halo) at
      // the pinch centroid from the START, so the first pinch-move doesn't
      // snap the world to a new spot.
      const c = photoPointerCentroid();
      if (c) moveReticle(c.x, c.y);
    }
  }

  function onTouchUp(e) {
    if (!state.photoPointers.has(e.pointerId)) return;
    const wasSingle = state.photoPointers.size === 1;
    state.photoPointers.delete(e.pointerId);
    const g = state.touchGesture;
    const cleanRelease = state.touchMode === "aim" && wasSingle && !state.pinchActive &&
      e.type === "pointerup" && (!e.button || e.button === 0);
    // Only a genuine TAP shoots. For a touch pointer a drag re-frames silently;
    // a mouse click (essentially zero travel) still shoots as before.
    if (cleanRelease && g && g.id === e.pointerId) {
      const elapsed = Date.now() - g.t0;
      // On DESKTOP the mouse looks around by hover, so a click never doubles as a
      // pan — any clean left-click shoots, even if the cursor was gliding. On
      // TOUCH a tap must stay put and be quick (small travel, released fast);
      // a real drag past TAP_MOVE_PX only re-frames and a long press never fires.
      const isTap = g.touch ? (g.moved <= TAP_MOVE_PX && elapsed <= TAP_MAX_MS) : true;
      if (isTap) captureAt();
    }
    if (g && g.id === e.pointerId) state.touchGesture = null;
    document.body.classList.remove("photo-dragging");
    if (state.photoPointers.size < 2) {
      const wasPinching = !!state.pinchBase;
      state.pinchBase = null;
      document.body.classList.remove("photo-pinching");
      // Once the pinch releases, catch markers up (they were skipped every
      // frame during the pinch to keep the gesture buttery) and fire the
      // discrete-zoom flourish (audio tick + bump) once as a settle beat.
      // Forcing setPhotoZoom with the current value runs both the marker
      // relayout and the flourish in one pass.
      if (wasPinching && state.touchMode === "aim") {
        setPhotoZoom(state.photoZoom, { force: true });
      }
    }
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
    if (state.touchGesture && state.touchGesture.id === e.pointerId) state.touchGesture = null;
    if (state.photoPointers.size === 0) document.body.classList.remove("photo-dragging");
    if (state.photoPointers.size < 2) {
      state.pinchBase = null;
      document.body.classList.remove("photo-pinching");
    }
    if (state.photoPointers.size === 0) state.pinchActive = false;
  }

  // Take the shot: crop the region under the reticle, flash, sound, file it to
  // the case file, and pop the satisfying evidence flourish. Stays armed so you
  // can keep gathering evidence tap after tap.
  function captureAt() {
    // The viewfinder is centered + fixed, so a shot always captures the centered
    // 16:9 region — exactly the bright area framed by the letterbox mask.
    const c = captureCenter();
    const boxPx = frameBoxPx();
    const shot = evaluateShot(c.x, c.y, boxPx);
    // WORTHY-SHOT GATE: once detection is live, a shot must FRAME a new subject
    // and hold it in FOCUS. Documented subjects and out-of-focus/empty frames
    // miss (no score). Before the first detection returns we give the benefit of
    // the doubt so latency never eats a shot.
    if (state.photoDetected && !shot.ok) {
      photoMiss(shot.reason);
      return;
    }
    const subject = shot.ok ? shot.subject : null;
    const region = screenBoxToNorm(c.x, c.y, boxPx.w, boxPx.h);
    const texture = captureSceneRegion(region, 512); // larger region → keep detail
    if (!texture) { showRendererToast("Couldn't capture \u2014 hold steady"); return; }
    flashShutter();
    photoKick();
    try { Sound.shutter(); } catch (_) {}
    try { Haptics.shutter(); } catch (_) {}
    presentCapture(texture); // full-screen cinematic hold on the shot
    // NOTE: the subject is "spent" (document-once) only once the appraisal is
    // actually credited in printReceipt — never eagerly here, so a cancelled or
    // empty shot never burns a POI without banking its evidence.
    Photo.capture({
      texture, region, kind: "photo", label: "the center of the view",
      zoom: state.photoZoom || 1, subject,
      focus: shot.ok ? shot.focus : null,
      focusGrade: shot.ok ? shot.grade : null,
    });
  }

  function closeTouch() {
    if (!state.touchMode) return;
    state.touchMode = null;
    // Kill any in-flight wheel-tail so a phantom zoom sound can't land after
    // the camera has already been put away.
    if (_wheelTailTimer) { clearTimeout(_wheelTailTimer); _wheelTailTimer = 0; }
    // Release the viewfinder magnification back to full wide.
    state.photoPointers.clear();
    state.touchGesture = null;
    state.pinchBase = null;
    state.pinchActive = false;
    document.body.classList.remove("photo-dragging");
    clearSceneZoom();
    stopPhotoTargeting();
    if (el.touchLayer) el.touchLayer.classList.add("hidden");
    if (el.touchReticle) el.touchReticle.classList.remove("holding");
    if (el.touchCaptureFrame) el.touchCaptureFrame.classList.remove("grab");
    if (el.realtimeBtn) el.realtimeBtn.classList.remove("aiming");
    document.body.classList.remove("touch-aiming", "photo-shake", "photo-kick", "photo-pinching");
    try { Sound.cameraOff(); } catch (_) {}
    try { Haptics.soft(); } catch (_) {}
    updateScanButton(); // the SCAN button is available again once the camera is put away
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

  // Cinematic full-screen hold on the shot you just took: the photo fills the
  // screen on pure black for ~2s — a beat to admire your creation, like a
  // classic war-photography reveal in a film — then fades, handing off to the
  // scoring receipt (which is developing underneath). Tap anywhere to skip.
  let _cinemaHoldTimer = 0, _cinemaOutTimer = 0;
  function presentCapture(texture) {
    const c = el.captureCinema;
    if (!c || !texture) return;
    if (prefersReducedMotion()) return; // no full-screen takeover under reduced motion
    const photo = c.querySelector(".capture-cinema-photo");
    if (photo) photo.style.backgroundImage = `url('${texture}')`;
    clearTimeout(_cinemaHoldTimer);
    clearTimeout(_cinemaOutTimer);
    c.classList.remove("hidden", "out");
    void c.offsetWidth; // restart the entrance animation on rapid re-shoots
    c.classList.add("show");
    let dismissed = false;
    const dismiss = () => {
      if (dismissed) return;
      dismissed = true;
      clearTimeout(_cinemaHoldTimer);
      c.removeEventListener("pointerdown", onTap);
      c.classList.remove("show");
      c.classList.add("out");
      _cinemaOutTimer = setTimeout(() => {
        c.classList.remove("out");
        c.classList.add("hidden");
      }, 520);
    };
    function onTap(e) { e.preventDefault(); e.stopPropagation(); dismiss(); }
    // Tap anywhere on the takeover to skip ahead to the score.
    c.addEventListener("pointerdown", onTap);
    // Hold the creation on screen for ~2 seconds, then fade out.
    _cinemaHoldTimer = setTimeout(dismiss, 2000);
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
    const box = frameBoxPx();
    // Same worthy-shot + document-once + focus rules as tap-to-shoot, judged at
    // the center of the frame (this is a centered snapshot).
    const shot = evaluateShot(center.x, center.y, box);
    if (state.photoDetected && !shot.ok) { photoMiss(shot.reason); return; }
    const subject = shot.ok ? shot.subject : null;
    const region = screenBoxToNorm(center.x, center.y, box.w, box.h);
    const texture = captureSceneRegion(region, 512);
    if (!texture) { showRendererToast("Couldn't capture the frame"); return; }
    flashShutter();
    photoKick();
    try { Sound.shutter(); } catch (_) {}
    try { Haptics.shutter(); } catch (_) {}
    presentCapture(texture); // full-screen cinematic hold on the shot
    // Spent only when the appraisal is credited (see printReceipt), not here.
    Photo.capture({
      texture, region, kind: "photo", label: "the center of the view",
      zoom: state.photoZoom || 1, subject,
      focus: shot.ok ? shot.focus : null,
      focusGrade: shot.ok ? shot.grade : null,
    });
  }

  // ------------------------------------------------------------------
  // Interaction hotspots — object recognition GATED BEHIND THE SCAN BUTTON.
  // Gemini image recognition is our biggest cost, so nothing scans on its own:
  // the player taps SCAN to fire ONE detection pass and the objects the model
  // recognizes surface as floating "starfield" tags anchored where they sit.
  // Hovering near a tag highlights it; clicking it opens an inline action bar to
  // ACT on THAT exact thing — a full turn (consequence + a freshly generated
  // scene). The tags then FADE OUT on their own after SCAN_TTL_MS so they can
  // never go stale (and so we don't need more calls to keep them fresh) — the
  // player taps SCAN again for a new read. Works in BOTH renderers: it reads the
  // live video frame in realtime mode, or the current still in image mode.
  //
  // Engineering notes:
  //  • ONE detection round-trip per SCAN tap (triggerScan) — no continuous
  //    polling, no per-scene auto-detect, no hover/move re-detect. This is the
  //    whole point of the button (cost control). It won't fire during a turn.
  //  • Tags are RECONCILED by label within a pass (kept + repositioned, added
  //    with a twinkle, removed with a fade) so re-scanning never churns the whole
  //    field or yanks a tag out from under the cursor.
  //  • The auto fade-out holds if a tag's action bar is open (mid-interaction).
  //  • Works with mouse (hover) and touch (tap the tag); the overlay is
  //    non-modal so the game's choices/controls stay live underneath.
  // ------------------------------------------------------------------
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
      // This Image exists ONLY to draw the still onto a canvas for detection
      // (the visible scene is a separate background-image). Request it CORS-clean
      // so a cross-origin still (e.g. an S3-hosted turn image) doesn't taint the
      // canvas and silently break capture. Same-origin stills ignore this. If a
      // cross-origin host lacks CORS headers the capture Image simply won't load
      // (naturalWidth 0 -> no detection), which never affects the visible scene.
      img.crossOrigin = "anonymous";
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
  // CONTEXT gate: is a full-screen instrument / end state / conversation
  // claiming the view? (Independent of whether a scene is currently readable.)
  function ambientContextAllowed() {
    if (state.gameOver || state.touchMode || state.freeWillOpen) return false;
    // Frozen behind the coin-op "out of credits" pause — the world isn't live.
    if (typeof CoinOp !== "undefined" && CoinOp.isPaused && CoinOp.isPaused()) return false;
    // While the camera is being driven the scene is in motion, so the OCR
    // hotspots (detected against a drifting frame) are stale/inaccurate — keep
    // them down. They regenerate when movement stops (see onMovementStop).
    if (state.moving) return false;
    if (typeof tapeIsOpen === "function" && tapeIsOpen()) return false;
    // Don't surface hotspots behind an open conversation — they'd sit under the
    // TALK panel (or behind a cinematic Moment). Re-armed when it closes.
    if (typeof Talk !== "undefined" && Talk.isOpen && Talk.isOpen()) return false;
    if (typeof window !== "undefined" && window.Moments && window.Moments.isActive && window.Moments.isActive()) return false;
    return true;
  }

  // Interaction hotspots are gated behind the SCAN button. Gemini image
  // recognition is our biggest cost, so instead of scanning continuously we
  // fire ONE detection pass only when the player taps SCAN. The resulting tags
  // then fade out on their own after a few seconds so they can never go stale;
  // the player taps SCAN again for a fresh read. This is the single entry point
  // for hitting /api/detect from the hotspot overlay — nothing scans on its own.
  const SCAN_TTL_MS =
    (typeof window !== "undefined" && window.__SCAN_TTL_MS__) || 5000;

  // origin (optional): {x, y} viewport point of a world tap — when present we
  // bloom a tactile ripple right there the instant the scan starts. The SCAN
  // button passes none (its own ring pulse is the feedback).
  function triggerScan(origin) {
    if (state.gameOver) return;
    // A full-screen instrument (camera/tape/free-will/conversation) or a turn
    // in flight owns the view — don't scan into it.
    if (!ambientContextAllowed()) return;
    if (state.processing || state.awaitingResolution) return;
    if (state.scanBusy) return; // a pass is already in flight — ignore re-taps
    if (!scanAvailable()) return; // nothing readable on screen yet
    const cap = captureScanFrame();
    if (!cap || !cap.frame) return;
    // A fresh pass restarts the fade clock and cancels any pending teardown.
    clearTimeout(state.scanFadeTimer); state.scanFadeTimer = null;
    clearTimeout(state.scanFadeOutTimer); state.scanFadeOutTimer = null;
    state.scanBusy = true;
    document.body.classList.add("scan-busy");
    if (el.scanBtn) el.scanBtn.classList.add("scanning");
    // Tactile feedback the instant you tap the world — ripple at the tap point,
    // a radar-sweep sound, a soft haptic — then the OCR runs (below).
    if (origin && typeof origin.x === "number") spawnTapRipple(origin.x, origin.y);
    try { Sound.scan(); } catch (_) {} // radar sweep on the press
    try { Haptics.soft && Haptics.soft(); } catch (_) {}
    postJSON("/api/detect", { frame: cap.frame })
      .then((res) => {
        const objs = (res && Array.isArray(res.objects)) ? res.objects : [];
        // The view may have been claimed while the pass was in flight.
        if (!ambientContextAllowed() || !scanAvailable()) return;
        state.scanOn = true;
        if (el.scanLayer) el.scanLayer.classList.remove("hidden");
        if (cap.size) state.scanSrcSize = cap.size;
        state.scanPrewarm = { objects: objs, size: cap.size || null, ts: Date.now() };
        // A world tap AIMS the scan: whatever detection sits under the tap
        // point becomes the objective's target, so the board reacts to what
        // you actually pointed at rather than whatever the detector ranks
        // biggest. The SCAN button (no origin) keeps the old ambient sweep.
        const tapped = origin ? nearestDetectionToPoint(objs, origin) : null;
        reconcileScanTags(objs, tapped);
        setScanHint(scanHintFor(objs, origin, tapped));
        if (objs.length) { try { Sound.ping(); } catch (_) {} } // starfield shimmer as tags land
        scheduleScanFade();
      })
      .catch((err) => { console.warn("[standalone] scan detect failed:", err); })
      .finally(() => {
        state.scanBusy = false;
        document.body.classList.remove("scan-busy");
        if (el.scanBtn) el.scanBtn.classList.remove("scanning");
      });
  }

  // Start the "fade out" clock: a few seconds after a scan lands, the hotspots
  // fade away so they can't linger and go stale (and so we're not tempted to
  // keep them fresh with more detection calls). Re-armed on each scan.
  function scheduleScanFade() {
    clearTimeout(state.scanFadeTimer);
    state.scanFadeTimer = setTimeout(fadeOutScan, SCAN_TTL_MS);
  }

  function fadeOutScan() {
    clearTimeout(state.scanFadeTimer); state.scanFadeTimer = null;
    // Don't yank a tag out from under an open action bar — the player is
    // mid-interaction. Hold the fade and retry once it closes.
    if (state.scanTagActing) { scheduleScanFade(); return; }
    if (el.scanTags && el.scanTags.children.length) {
      Array.from(el.scanTags.children).forEach((t) => t.classList.add("leaving"));
      clearTimeout(state.scanFadeOutTimer);
      state.scanFadeOutTimer = setTimeout(() => {
        state.scanFadeOutTimer = null;
        closeScan();
      }, 460); // matches the .leaving transition (see .scan-tag.leaving)
    } else {
      closeScan();
    }
  }

  // Enable/disable + label the SCAN button for the current context. It's inert
  // whenever there's nothing readable to scan (still decoding / live video not
  // up yet) or an instrument/turn owns the view.
  function updateScanButton() {
    if (!el.scanBtn) return;
    const turnActive = !!(el.actionWheel && el.actionWheel.classList.contains("turn-active"));
    const ok = !state.gameOver && ambientContextAllowed() && !turnActive &&
      !state.processing && !state.awaitingResolution && scanAvailable();
    el.scanBtn.disabled = !ok;
    try { updateCampButton(); } catch (_) {}
  }

  // CAMP mirrors SCAN's agency gate: reachable when the player can act, never
  // mid-turn / mid-conversation / inside another Moment / already camping /
  // game over / paused. While at camp the wheel button stays disabled; LEAVE
  // CAMP is the exit.
  function updateCampButton() {
    if (!el.campBtn) return;
    const turnActive = !!(el.actionWheel && el.actionWheel.classList.contains("turn-active"));
    const talkOpen = !!(Talk && typeof Talk.isOpen === "function" && Talk.isOpen());
    const momentActive = !!(window.Moments && typeof window.Moments.isActive === "function" &&
      window.Moments.isActive());
    const coinPaused = !!(typeof CoinOp !== "undefined" && CoinOp.isPaused && CoinOp.isPaused());
    const tapeOpen = !!(typeof tapeIsOpen === "function" && tapeIsOpen());
    const ok = !state.gameOver && !turnActive && !state.processing &&
      !state.awaitingResolution && !talkOpen && !momentActive &&
      !coinPaused && !tapeOpen && !state.touchMode && !state.freeWillOpen &&
      !state.inCamp && !state.campEntering && !state.campLeaving;
    el.campBtn.disabled = !ok;
    try { updateLeaveCampButton(); } catch (_) {}
  }

  function updateLeaveCampButton() {
    if (!el.leaveCampBtn) return;
    const show = !!(state.inCamp && !state.campEntering);
    el.leaveCampBtn.classList.toggle("hidden", !show);
    el.leaveCampBtn.setAttribute("aria-hidden", show ? "false" : "true");
    const talkOpen = !!(Talk && typeof Talk.isOpen === "function" && Talk.isOpen());
    const momentActive = !!(window.Moments && typeof window.Moments.isActive === "function" &&
      window.Moments.isActive());
    const turnActive = !!(el.actionWheel && el.actionWheel.classList.contains("turn-active"));
    el.leaveCampBtn.disabled = !show || state.campLeaving || state.processing ||
      state.gameOver || talkOpen || momentActive || turnActive || !!state.touchMode;
  }

  // CAMP is a normal playable level: hard-cut to the ensemble campsite plate,
  // re-anchor the live world model, keep the full action wheel (PHOTO / SCAN /
  // ACT / pad). Not a Moments cinematic — letterbox / HUD-hide would block
  // instruments. LEAVE CAMP fires a hard-transition choose into a new mission.
  // Hard-transition keywords ("Leave ", "new location") must stay so the
  // engine builds a fresh level — but NEVER say drive/jeep/truck. Cab / dashboard
  // POVs break the walkable world model after camp.
  const CAMP_LEAVE_CHOICE =
    "Leave camp and walk into a new outdoor location across the desert — " +
    "arrive on foot at eye level with open ground ahead, not inside any vehicle.";

  function clearCampFromRenderer() {
    try {
      if (Renderer) {
        Renderer.lastScene = {
          prompt: null,
          imageUrl: null,
          hardTransition: true,
        };
      }
    } catch (_) {}
  }

  function activateCampAsLevel(imageUrl, prompt) {
    if (!imageUrl || !Renderer || typeof Renderer.applyScene !== "function") return false;
    const rtPrompt = prompt ||
      "Night campsite in high-desert scrub. Campfire burns, embers drift, " +
      "a dusty red 1990s jeep parked at the edge of the firelight. " +
      Camera.vantage().charAt(0).toUpperCase() + Camera.vantage().slice(1) +
      ", handheld. Firelight flickers.";
    try {
      Renderer.lastScene = {
        prompt: rtPrompt,
        imageUrl: imageUrl,
        hardTransition: true,
      };
      if (Renderer.mode !== "reactor" && typeof Renderer.setMode === "function" &&
          Renderer.reactorAvailable && Renderer.reactorAvailable()) {
        // Prefer the live world-model at camp (same as any other level).
        Renderer.setMode("reactor");
      } else {
        Renderer.applyScene(imageUrl, rtPrompt, { hard_transition: true });
      }
      if (typeof Renderer.resumeUnderlay === "function") Renderer.resumeUnderlay();
      return true;
    } catch (err) {
      console.warn("[camp] activate as level failed:", err);
      return false;
    }
  }

  async function openCamp() {
    if (!el.campBtn || el.campBtn.disabled) return;
    if (state.inCamp || state.campEntering || state.campLeaving) return;
    if (window.Moments && window.Moments.isActive && window.Moments.isActive()) return;
    try { closeScan(); } catch (_) {}
    try { closeFreeWill(true); } catch (_) {}

    state.campEntering = true;
    updateCampButton();

    // Same fade contract as MOVE TO / hard location changes — hold black until
    // the campsite re-anchors. No Moment letterbox, no HUD hide.
    let faded = false;
    try {
      const RR = window.ReactorRenderer;
      if (Renderer.mode === "reactor" && RR && typeof RR.beginSceneFade === "function") {
        RR.beginSceneFade({
          safetyMs: MOVE_TRANSITION_FADE_SAFETY_MS,
          awaitReanchor: true,
        });
        faded = true;
      }
    } catch (err) {
      console.warn("[camp] beginSceneFade failed:", err);
    }

    let res = null;
    try {
      res = await postJSON("/api/camp/enter", {});
    } catch (err) {
      console.warn("[camp] enter failed:", err);
      if (err && err.status === 429) {
        res = Object.assign({ image_url: null, reason: "slow_down" }, err.body || {});
      } else {
        res = null;
      }
    }

    if (!res || !res.image_url) {
      if (faded) {
        try {
          const RR = window.ReactorRenderer;
          if (RR && typeof RR.endSceneFade === "function") RR.endSceneFade();
        } catch (_) {}
      }
      try {
        showRendererToast(
          (res && res.reason === "slow_down")
            ? "Camp is still settling — try again"
            : "Couldn't make camp right now"
        );
      } catch (_) {}
      state.campEntering = false;
      updateCampButton();
      return;
    }

    const ok = activateCampAsLevel(res.image_url, res.realtime_prompt || "");
    if (!ok && faded) {
      try {
        const RR = window.ReactorRenderer;
        if (RR && typeof RR.endSceneFade === "function") RR.endSceneFade();
      } catch (_) {}
    }

    state.campEntering = false;
    state.inCamp = true;
    document.body.classList.add("in-camp");
    updateCampButton();

    const n = Array.isArray(res.attendees) ? res.attendees.length : 0;
    try {
      showRendererToast(
        n
          ? ("Camp — " + n + " companion" + (n === 1 ? "" : "s") + " by the fire")
          : "Camp — jeep by the fire"
      );
      RtLog.push("status", "\u25CF CAMP \u00B7 playable level (" + n + " companions)");
    } catch (_) {}
  }

  async function leaveCamp() {
    if (!state.inCamp || state.campLeaving || state.processing || state.gameOver) return;
    if (Talk && typeof Talk.isOpen === "function" && Talk.isOpen()) {
      try { Talk.close(); } catch (_) {}
    }
    state.campLeaving = true;
    state.inCamp = false;
    document.body.classList.remove("in-camp");
    updateCampButton();

    // Drop camp from lastScene before the choose so a late reconnect can't
    // resurrect the campsite over the new mission.
    clearCampFromRenderer();

    try {
      await makeChoice(CAMP_LEAVE_CHOICE, null, {
        source: "camp_leave",
        moveTarget: "the next lead",
      });
    } catch (err) {
      console.warn("[camp] leave → new level failed:", err);
      try { cancelMoveTransition(); } catch (_) {}
    } finally {
      state.campLeaving = false;
      updateCampButton();
    }
  }

  // Tear the hotspot overlay down: hide it, drop its tags, and cancel any
  // pending fade timers. Used by the fade-out and whenever an instrument
  // (camera/tape/free-will/conversation) or a scene change takes over.
  function closeScan() {
    clearTimeout(state.scanFadeTimer); state.scanFadeTimer = null;
    clearTimeout(state.scanFadeOutTimer); state.scanFadeOutTimer = null;
    if (!state.scanOn && (!el.scanLayer || el.scanLayer.classList.contains("hidden"))) {
      updateScanButton();
      return;
    }
    state.scanOn = false;
    state.scanTagActing = null;
    clearTimeout(state.scanMoveTimer); state.scanMoveTimer = null;
    document.body.classList.remove("scan-busy");
    if (el.scanLayer) el.scanLayer.classList.add("hidden");
    if (el.scanTags) el.scanTags.innerHTML = "";
    state.scanObjects = [];
    updateScanButton();
  }

  function setScanHint(text) {
    if (!el.scanHint) return;
    el.scanHint.textContent = text || "";
    el.scanHint.classList.toggle("hidden", !text);
  }

  // What the scan hint should say for this pass. A world tap always resolves
  // to a legible outcome now — found something new (silence is fine, the
  // objective banner already said it), found something already on file
  // (say so), or found nothing near the tap at all (say so) — so tapping the
  // world never again reads as a coin flip between "it worked" and nothing.
  // The SCAN button (no tap origin) keeps its original ambient message.
  function scanHintFor(objects, origin, tapped) {
    if (tapped) {
      if (window.Evidence && Evidence.isSpent && Evidence.isSpent(tapped.label)) {
        return "already documented \u2014 " + tapped.label;
      }
      return ""; // the fresh/re-affirmed objective banner already said it
    }
    if (origin) return "nothing worth investigating here";
    return objects.length ? "" : "nothing to interact with here";
  }

  // A quick ripple at a world-tap point (viewport coords). Lives on <body> so it
  // shows even while the hotspot overlay is still hidden, and self-cleans. Purely
  // cosmetic feedback for "I tapped the world" — the scan/OCR runs separately.
  function spawnTapRipple(x, y) {
    const r = document.createElement("div");
    r.className = "world-tap-ripple";
    r.style.left = x + "px";
    r.style.top = y + "px";
    document.body.appendChild(r);
    const kill = () => { if (r.parentNode) r.parentNode.removeChild(r); };
    r.addEventListener("animationend", kill, { once: true });
    setTimeout(kill, 900); // safety net (reduced-motion / missed animationend)
  }

  // ---- Movement ↔ ambient hotspots ----------------------------------------
  // The OCR hotspots (and the choices grounded on them) are detected against the
  // current frame, so they're wrong the instant the camera starts travelling.
  // Hide them the moment movement begins; regenerate + reveal once it stops and
  // the view has settled on the new vantage.
  const MOVE_SETTLE_MS = (typeof window !== "undefined" && window.__MOVE_SETTLE_MS__) || 900;

  function onMovementStart() {
    if (state.moving) return;
    state.moving = true;
    // Tear the hotspot overlay down the instant the camera starts travelling —
    // the tags are pinned to the frame we're leaving. (closeScan cancels the
    // fade timers too.) The player re-scans once movement settles.
    clearTimeout(state.scanMoveTimer); state.scanMoveTimer = null;
    clearTimeout(state.moveSettleTimer); state.moveSettleTimer = null;
    document.body.classList.add("moving");
    closeScan();
  }

  function onMovementStop() {
    if (!state.moving) return;
    state.moving = false;
    document.body.classList.remove("moving");
    // Hotspots are gated behind SCAN now — we don't auto-re-detect after a move.
    // Just re-enable the button once the view has settled so a fresh scan reads
    // the new vantage rather than the frame we started travelling from.
    clearTimeout(state.moveSettleTimer);
    state.moveSettleTimer = setTimeout(() => {
      if (state.moving) return; // started moving again
      updateScanButton();
    }, MOVE_SETTLE_MS);
  }

  // Map normalized (0..1) frame coordinates onto the on-screen scene's
  // object-fit/background-size: cover display rect (video OR still) so a tag
  // lands exactly over its object on screen.
  function mapNormToScreen(nx, ny) {
    const W = window.innerWidth, H = window.innerHeight;
    const size = state.scanSrcSize ||
      (window.ReactorRenderer.getVideoSize && window.ReactorRenderer.getVideoSize()) || null;
    if (!size || !size.w || !size.h) return { x: nx * W, y: ny * H };
    const scale = mediaFitScale(W, H, size.w, size.h);
    const dw = size.w * scale, dh = size.h * scale;
    const ox = (W - dw) / 2, oy = (H - dh) / 2;
    return { x: ox + nx * dw, y: oy + ny * dh };
  }

  // A fingertip on a phone is a lot less precise than a mouse cursor, and the
  // beacon dots on mobile sit collapsed into a narrower letterboxed band (see
  // the mobile scan-tag CSS), so tap-targeting gets a bit more forgiveness
  // than the desktop hover radius (SCAN_NEAR_RADIUS) it's otherwise based on.
  function scanTapRadius() {
    let mobile = false;
    try { mobile = !!(window.__DEVICE__ && window.__DEVICE__.isMobile()); } catch (_) {}
    return mobile ? SCAN_NEAR_RADIUS * 1.35 : SCAN_NEAR_RADIUS;
  }

  // Which detection (if any) sits under a tap point — this is what turns SCAN
  // into a targeted "investigate this thing" action instead of a background
  // lucky-dip over whatever the detector ranks biggest. Works identically for
  // a mouse click or a touch tap — both land here as the same synthetic
  // "click" (see onWorldTap) with real clientX/clientY — but touch gets the
  // wider scanTapRadius() tolerance above.
  function nearestDetectionToPoint(objects, point) {
    if (!point || typeof point.x !== "number" || !Array.isArray(objects) || !objects.length) return null;
    let best = null, bestD = Infinity;
    objects.forEach((o) => {
      const p = mapNormToScreen(o.cx, o.cy);
      const dx = p.x - point.x, dy = p.y - point.y;
      const d = dx * dx + dy * dy;
      if (d < bestD) { bestD = d; best = o; }
    });
    const r = scanTapRadius();
    if (!best || bestD > r * r) return null;
    return best;
  }

  // Hover: highlight the interaction possibility nearest the cursor so moving
  // over the scene "finds" the things in it. Purely visual — no detection fires
  // on hover anymore (scanning is manual, behind the SCAN button).
  function onScanMove(e) {
    if (!state.scanOn) return;
    highlightNearestTag(e.clientX, e.clientY);
  }

  // Tapping the WORLD itself scans it — the same on-demand OCR the SCAN button
  // fires, but it makes you feel like you're reaching out and touching the
  // scene. We ALLOWLIST the scene surfaces (the live video, the still layers,
  // the freeze buffer) rather than denylisting controls: a stray click on any
  // HUD/control/overlay/tag must never burn a paid detection call. A tap while
  // an action bar is open just dismisses the bar (no re-scan). triggerScan still
  // gates on context (camera/tape/talk/turn/paused/…) and only ripples if it
  // actually starts. The cinematic Moment scrim sits above the scene and has
  // its own pointer-events, so a Moment tap never reaches these surfaces.
  const WORLD_TAP_SURFACES = ".scene, #reactor-video, #reactor-freeze";
  function onWorldTap(e) {
    // Only primary (left) button / touch taps scan the world.
    if (typeof e.button === "number" && e.button !== 0) return;
    const t = e.target;
    const onWorld = t === document.body ||
      !!(t && t.closest && t.closest(WORLD_TAP_SURFACES));
    if (!onWorld) return; // a control/overlay/HUD/tag owns this click
    if (state.scanTagActing) { closeTagPrompt(state.scanTagActing); return; }
    triggerScan({ x: e.clientX, y: e.clientY });
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
  // Phones show these as small green "beacon" dots collapsed into a narrow
  // letterboxed band, so a long detection list piles up into an unreadable,
  // never-clearing clump. Keep only the few most CENTRAL subjects on mobile so
  // the field stays clean; desktop (roomy canvas, hover labels) shows them all.
  const MOBILE_SCAN_TAG_CAP = 6;
  // keepLabel (optional): a label the device cap must never drop even if it
  // isn't among the most-central subjects — used so the exact thing the
  // player just tapped on always gets a visible tag, even near a screen edge.
  function capScanObjectsForDevice(objects, keepLabel) {
    if (!Array.isArray(objects) || objects.length <= MOBILE_SCAN_TAG_CAP) return objects;
    let mobile = false;
    try { mobile = !!(window.__DEVICE__ && window.__DEVICE__.isMobile()); } catch (_) {}
    if (!mobile) return objects;
    const ranked = objects
      .map((o) => {
        const cx = typeof o.cx === "number" ? o.cx : 0.5;
        const cy = typeof o.cy === "number" ? o.cy : 0.5;
        return { o, d: (cx - 0.5) * (cx - 0.5) + (cy - 0.5) * (cy - 0.5) };
      })
      .sort((a, b) => a.d - b.d);
    const kept = ranked.slice(0, MOBILE_SCAN_TAG_CAP).map((x) => x.o);
    if (keepLabel && !kept.some((o) => o.label === keepLabel)) {
      const extra = ranked.find((x) => x.o.label === keepLabel);
      if (extra) kept[kept.length - 1] = extra.o;
    }
    return kept;
  }

  // tapped (optional): the detection object (from nearestDetectionToPoint)
  // that sits under the player's tap this pass — gets a `.targeted` lock-on
  // treatment and is guaranteed a field objective via Objectives.onDetect's
  // opts.target, independent of the ambient MAX_FIELD-capped sweep.
  function reconcileScanTags(objects, tapped) {
    if (!el.scanTags) return;
    objects = capScanObjectsForDevice(objects, tapped && tapped.label);
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
      tag.classList.toggle("targeted", !!tapped && obj.label === tapped.label);
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
    // Scanned subjects → field bounties. The tapped target (if any) is
    // guaranteed an objective; everything else still runs the ambient sweep.
    try { Objectives.onDetect(objects, tapped ? { target: tapped.label } : null); } catch (_) {}
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

  // The actions a player can take on a detected object. They split into two
  // distinct kinds:
  //   • MOVE — resolves a FULL turn that RELOCATES you: a full change of scenery
  //     (hard transition) to a fresh scene composed around the object.
  //   • INTERACT — injects a LIVE realtime event into the running world model (a
  //     prompt hot-swap) so the world reacts in place, without changing scene.
  //   • TALK — opens a live conversation overlay (unchanged).
  // MOVE composes a clean, natural prompt from the verb + the object's own name;
  // the consequence LLM (server-side) turns that intent into an in-world outcome
  // + a fresh scene, so there's no need for a separate "action-writing" LLM.
  // Objects you can go INSIDE / through — a passage, opening, vehicle, or
  // structure. When "MOVE TO" targets one of these, we phrase it as an ENTRY
  // ("enter …"); every other object is phrased as a relocation ("cross over").
  // Both are hard transitions (is_hard_transition fires on "enter"/"cross
  // over"), so MOVE always yields a genuinely new scene rather than drifting in
  // place — which is why plain "move to the X" used to look static.
  const ENTERABLE_RE = /\b(door|doorway|gate|gateway|entrance|entry|hatch|portal|threshold|arch|archway|opening|mouth|maw|tunnel|pipe|duct|corridor|hallway|hall|passage|passageway|stair|stairs|stairway|stairwell|room|building|house|cabin|shack|shed|garage|barn|cave|cavern|vault|chamber|window|breach|gap|hole|vent|shaft|elevator|lift|airlock|tent|bunker|silo|structure|ruin|ruins|store|shop|church|warehouse|facility|lab|laboratory|booth|trailer|van|truck|car|bus|train|carriage|wagon|boat|ship|cockpit|rig|derrick)\b/i;

  function moveActionPhrase(o) {
    if (ENTERABLE_RE.test(o)) {
      // "Enter …" -> hard transition -> a genuinely new interior scene.
      return "Enter the " + o + ", moving inside into the space beyond.";
    }
    // MOVE always RELOCATES you — a full change of scenery, not a camera drift
    // in place. "Cross over" is one of the engine's hard-transition triggers
    // (is_hard_transition matches the literal phrase), so the turn cuts to a
    // fresh scene composed around the object at your new vantage instead of
    // merely advancing the camera. The wording MUST contain "cross over"
    // verbatim or the transition won't fire (that's why plain "move to the X"
    // used to look static).
    return "Travel to the " + o + ", cross over to it, and arrive at a new vantage where the surroundings have completely changed.";
  }

  // A short spatial anchor for a detected object, derived from its normalized
  // center — used to aim a realtime INTERACT event at the spot on screen where
  // the thing actually is (so the world reacts THERE, not across the whole frame).
  // Where the object sits, in PHYSICAL first-person space (not "upper-left of
  // the frame" screen coordinates — the world model renders a described world,
  // not instructions about the picture). Feeds the realtime event overlay so the
  // reaction reads as happening at a real place in the scene.
  function objectAnchorPhrase(obj) {
    const cx = typeof obj.cx === "number" ? obj.cx : 0.5;
    const cy = typeof obj.cy === "number" ? obj.cy : 0.5;
    const h = cx < 0.34 ? "to your left" : cx > 0.66 ? "to your right" : "directly ahead";
    const v = cy < 0.34 ? ", up high" : cy > 0.66 ? ", down low" : "";
    return h + v;
  }

  // Does this detected thing hold a conversation? The perception layer
  // (/api/detect) flags `speaks` for people, characters, sentient creatures,
  // and voice-carrying machines; keep a light client-side backstop so TALK
  // still appears if an older/edge detection omitted the flag.
  const TALKABLE_LABEL_RE = /\b(person|people|man|men|woman|women|boy|girl|child|kid|guy|lady|figure|stranger|survivor|soldier|guard|worker|scientist|doctor|nurse|officer|cop|ranger|pilot|driver|operator|technician|villager|prisoner|captive|hostage|patient|civilian|face|ghost|spirit|creature|beast|monster|alien|humanoid|android|robot|droid|cyborg|hologram|radio|intercom|speaker|phone|telephone|walkie|transceiver|terminal|console|loudspeaker|megaphone)\b/i;
  function objectSpeaks(obj) {
    if (!obj) return false;
    if (obj.speaks === true) return true;
    if (obj.speaks === false) return false; // trust an explicit negative
    const kind = (obj.kind || "").toLowerCase();
    if (kind === "person" || kind === "character" || kind === "creature") return true;
    return TALKABLE_LABEL_RE.test(obj.label || "");
  }

  const SCAN_ACTIONS = [
    {
      id: "move", label: "MOVE TO", title: "Move to",
      phrase: moveActionPhrase,
      icon: '<svg class="scan-action-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2v20M2 12h20"/><path d="M9 5l3-3 3 3M9 19l3 3 3-3M5 9l-3 3 3 3M19 9l3 3-3 3"/></svg>',
    },
    {
      // INTERACT injects a LIVE interaction into the running world model — on
      // Happy Oyster a real interact({action}) verb command (the world reacts in
      // place, no rebuild); on prompt-steered models a realtime prompt-event
      // hot-swap. Either way: no backend turn, no scene change, the world reacts
      // to the poke NOW where the object sits. Falls back to a full turn when
      // realtime isn't live (still mode).
      id: "interact", label: "INTERACT", title: "Interact with",
      realtime: true,
      phrase: (o) => "Interact with the " + o + ".",
      // Happy Oyster interaction verb — a concise action string handed to
      // interact({action}). "Any verb string is accepted" (the built-ins are
      // Jump/Attack/Crouch/Sprint), so we name the object being worked.
      interactVerb: (o) => "interact with the " + o,
      // Prompt-steered models (LingBot/Helios) instead need a full, concrete
      // PHYSICAL event as a sentence-anchor, not "interact with the X" (too
      // abstract to draw) and not a short tag (a brief mention gets drowned out
      // by the dense scene base and never renders). Hands enter frame and
      // physically work the object, and the object responds with detailed,
      // additive motion — enough weight to actually show up. Used only for the
      // realtime prompt-event steer; the plain `phrase` above is the clean intent
      // handed to the consequence LLM on the full-turn fallback.
      realtimePhrase: (o) => "your hands reach into view and take hold of the " + o + ", and it responds with an unmistakable physical change \u2014 the " + o + " shifts and moves, swinging, opening, sliding, or activating, its surfaces catching the light as loose dust and small debris stir into the air around it",
      icon: '<svg class="scan-action-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 13V5a2 2 0 0 1 4 0v6"/><path d="M12 11V4a2 2 0 0 1 4 0v7"/><path d="M16 11V7a2 2 0 0 1 4 0v8a6 6 0 0 1-6 6h-2a6 6 0 0 1-5-2.7l-2.8-4a2 2 0 0 1 3.1-2.5L9 14"/></svg>',
    },
    {
      // TALK — only surfaces for things that can speak. It doesn't resolve a
      // turn; it opens a live, story-aware conversation overlay (voice via
      // ElevenLabs when configured, else text). A warm-accented speech bubble
      // sets it apart from the two cool "world action" verbs above.
      id: "talk", label: "TALK", title: "Talk to",
      when: objectSpeaks,
      conversational: true,
      icon: '<svg class="scan-action-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5a8.38 8.38 0 0 1-8.5 8.5 9 9 0 0 1-3.9-.9L3 21l1.9-5.6A8.38 8.38 0 0 1 4 11.5 8.5 8.5 0 0 1 12.5 3 8.38 8.38 0 0 1 21 11.5z"/><path d="M8.5 11.5h.01M12 11.5h.01M15.5 11.5h.01"/></svg>',
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
    // action. No typing — tapping an icon composes the prompt and commits a turn
    // (or, for TALK, opens the conversation overlay). Actions with a `when`
    // predicate only render when this specific object qualifies, so TALK shows
    // up solely on things that can actually speak.
    const actions = document.createElement("div");
    actions.className = "scan-tag-actions";
    const applicable = SCAN_ACTIONS.filter((a) => !a.when || a.when(obj));
    if (applicable.some((a) => a.conversational)) tag.classList.add("can-talk");
    applicable.forEach((a) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "scan-action scan-action-" + a.id;
      b.title = a.title + " the " + obj.label;
      b.setAttribute("aria-label", a.title + " the " + obj.label);
      b.innerHTML = a.icon + '<span class="scan-action-lbl">' + a.label + "</span>";
      b.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (a.conversational) { startTalk(tag, obj); return; }
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
  // Instant press-ceremony: a bright ring pulse that blooms from the exact spot
  // on screen the player poked. It's spawned SYNCHRONOUSLY the instant a scan
  // action button fires — before any network / world-model work — so the press
  // is ALWAYS acknowledged at the object, even if the world model is slow, off,
  // or underwhelms. Purely cosmetic and self-cleaning; lives on the (fixed,
  // full-viewport) scan overlay so it shares the tag coordinate space and never
  // interferes with tag bookkeeping (which iterates #scan-tags children).
  function spawnScanPulse(x, y, variant) {
    if (!el.scanLayer) return;
    const p = document.createElement("div");
    p.className = "scan-pulse" + (variant ? " scan-pulse-" + variant : "");
    p.style.left = x + "px";
    p.style.top = y + "px";
    el.scanLayer.appendChild(p);
    const kill = () => { if (p.parentNode) p.parentNode.removeChild(p); };
    p.addEventListener("animationend", kill, { once: true });
    setTimeout(kill, 1100); // safety net (reduced-motion / missed animationend)
  }

  // A quick, satisfying pop on the tag itself — restarts cleanly on repeat taps.
  // Only visible on tags that PERSIST after the press (INTERACT); MOVE clears its
  // tags immediately, and the ring pulse carries the confirmation there.
  function pokeTag(tag) {
    if (!tag) return;
    tag.classList.remove("poked");
    void tag.offsetWidth; // reflow so the animation replays on rapid taps
    tag.classList.add("poked");
    tag.addEventListener("animationend", (e) => {
      if (e.animationName === "scan-poked") tag.classList.remove("poked");
    }, { once: true });
  }

  // The screen anchor of a tag (its object's mapped position), for the pulse.
  function tagAnchor(tag) {
    return {
      x: parseFloat(tag.dataset.sx || tag.style.left || "0"),
      y: parseFloat(tag.dataset.sy || tag.style.top || "0"),
    };
  }

  function commitScanAction(tag, action) {
    const obj = tag._obj || { label: "it" };
    if (state.gameOver) { closeTagPrompt(tag); return; }
    const phrase = action.phrase(obj.label);
    const a = tagAnchor(tag);

    // INTERACT is a LIVE poke: inject a realtime event into the running world
    // model (a set_prompt hot-swap) so the world reacts in place, right where the
    // object sits — no backend turn, no scene change. This works even while a
    // turn is resolving, so it isn't gated on the pipeline being idle. If
    // realtime isn't live (still mode), fall through to a full turn so INTERACT
    // still does something.
    if (action.realtime) {
      // Prefer a REAL interaction verb command when the live model takes them
      // (Happy Oyster): the world reacts in place with no rebuild. Otherwise
      // steer with the CONCRETE, renderable reaction phrase (not the abstract
      // "interact with the X"), framed as a world EVENT so a prompt-steered model
      // (LingBot/Helios) draws the object responding right where it sits.
      let steered = false;
      const canVerb = Renderer.reactorAvailable() &&
        window.ReactorRenderer.canInteract && window.ReactorRenderer.canInteract();
      if (canVerb && action.interactVerb) {
        steered = window.ReactorRenderer.interact(action.interactVerb(obj.label));
      }
      if (!steered) {
        const rtText = action.realtimePhrase ? action.realtimePhrase(obj.label) : phrase;
        steered = Renderer.steerRealtime(rtText, { phrase: objectAnchorPhrase(obj), kind: "event" });
      }
      if (steered) {
        // Prove the press INSTANTLY — a double-ring "event" pulse + tag pop —
        // regardless of when (or whether) the world model actually reacts.
        Sound.submit();
        spawnScanPulse(a.x, a.y, "interact");
        closeTagPrompt(tag);
        pokeTag(tag);
        showRendererToast(action.title + " the " + obj.label + "\u2026");
        return;
      }
    }

    if (state.processing) { showRendererToast("The world is still resolving…"); return; }
    Sound.submit();
    spawnScanPulse(a.x, a.y, action.id);
    closeTagPrompt(tag);
    showRendererToast(action.title + " the " + obj.label + "\u2026");
    // makeChoice clears the tags (the scene is about to change); the ambient
    // overlay stays live and repopulates hotspots once the new scene settles.
    // Tag the turn as a SCAN object interaction so the story backend escalates
    // risk and forces a consequential, plot-moving outcome (not an inert poke);
    // `moveTarget` is the object's own name, carried through to the MOVE TO
    // transition (fade-to-black + narrator bridging line) inside makeChoice.
    const source = action.id === "move" ? "scan_move" : "scan_interact";
    const moveTarget = action.id === "move" ? obj.label : null;
    makeChoice(phrase, null, { source, moveTarget });
  }

  // ------------------------------------------------------------------
  // Shared ElevenLabs client-SDK loader — used by BOTH the TALK conversation
  // and the NARRATOR. Loaded lazily and pinned to the 1.x line so a future
  // breaking release can't change the API out from under us.
  // ------------------------------------------------------------------
  const ElevenSDK = (function () {
    const URL = "https://esm.sh/@elevenlabs/client@1";
    let p = null;
    function load() {
      if (!p) {
        AgentLog.push("sdk", "loading ElevenLabs SDK\u2026");
        p = import(/* webpackIgnore: true */ URL)
          .then((m) => {
            const C = m.Conversation || (m.default && m.default.Conversation);
            if (!C) throw new Error("Conversation export missing");
            AgentLog.push("ok", "SDK loaded");
            return C;
          })
          .catch((e) => { p = null; AgentLog.push("error", "SDK load failed", String(e).slice(0, 120)); throw e; });
      }
      return p;
    }
    return { load };
  })();

  // ------------------------------------------------------------------
  // AGENT DEBUG LOG — a dedicated, toggleable inspector for the voice agents
  // (TALK + narrator). It records what the agents are doing — session opens,
  // SDK/connection status, mode changes, transcript lines, voice switches,
  // narrator segments, and errors — so issues (e.g. "no audio on live") are
  // diagnosable in the field. Toggle with the DEBUG rail button or the "D" key;
  // add ?agentdebug to the URL to open it on load.
  // ------------------------------------------------------------------
  const AgentLog = (function () {
    const MAX = 300;
    function visible() {
      try { return localStorage.getItem("agent_log") === "on"; } catch (_) { return false; }
    }
    function apply() { document.body.classList.toggle("agent-log-on", visible()); }
    function stamp() {
      const d = new Date();
      return String(d.getMinutes()).padStart(2, "0") + ":" + String(d.getSeconds()).padStart(2, "0") +
        "." + String(d.getMilliseconds()).padStart(3, "0").slice(0, 2);
    }
    // kind: ok | error | warn | sdk | voice | narrator | talk | net | dim
    function push(kind, label, detail) {
      // Mirror to the console for remote debugging (visible in devtools on live).
      try { console.log("[agent]", kind || "", label || "", detail || ""); } catch (_) {}
      const list = el.agentLogList;
      if (!list) return;
      const li = document.createElement("li");
      li.className = "al-e" + (kind ? " al-" + kind : "");
      const t = document.createElement("span"); t.className = "al-t"; t.textContent = stamp();
      const m = document.createElement("span"); m.className = "al-m"; m.textContent = label || "";
      li.appendChild(t); li.appendChild(m);
      if (detail != null && detail !== "") {
        const dd = document.createElement("span"); dd.className = "al-d"; dd.textContent = " " + detail;
        li.appendChild(dd);
      }
      const atBottom = list.scrollTop + list.clientHeight >= list.scrollHeight - 24;
      list.appendChild(li);
      while (list.children.length > MAX) list.removeChild(list.firstChild);
      if (atBottom) list.scrollTop = list.scrollHeight;
    }
    function toggle() {
      const on = !visible();
      try { localStorage.setItem("agent_log", on ? "on" : "off"); } catch (_) {}
      apply();
      if (el.narratorBtn) {} // no-op; keeps lints calm
      push("dim", on ? "\u2014 debug log opened \u2014" : "");
    }
    function clip(s, n) {
      s = (s || "").toString().replace(/\s+/g, " ").trim();
      return s.length > (n || 140) ? s.slice(0, (n || 140) - 1) + "\u2026" : s;
    }
    function init() {
      try { if (/(?:\?|&)agentdebug\b/.test(location.search)) localStorage.setItem("agent_log", "on"); } catch (_) {}
      apply();
      if (el.agentLogHide) el.agentLogHide.addEventListener("click", () => { try { localStorage.setItem("agent_log", "off"); } catch (_) {} apply(); });
      if (el.agentLogClear) el.agentLogClear.addEventListener("click", () => { if (el.agentLogList) el.agentLogList.innerHTML = ""; });
    }
    return { push, toggle, clip, init };
  })();

  // ------------------------------------------------------------------
  // TALK — a live, story-aware conversation with a SCAN subject that speaks.
  //
  // Kicked off from a tag's TALK action (only shown on things that can speak).
  // It asks the server for a session: when ElevenLabs is configured the server
  // returns voice-agent config (agent id / signed url + the story briefing as
  // dynamic variables + prompt overrides) and we run a LIVE VOICE conversation
  // via the ElevenLabs client SDK — real mic in, the character's voice out —
  // rendered inside this same sleek panel (its transcript is our transcript).
  // With no agent configured we fall back to a text conversation via
  // /api/talk/message. Either way the subject is aware of the current scene and
  // recent beats. TALK never resolves a turn — it's a parallel layer over the
  // world. You can also TYPE at any time during a voice call.
  // ------------------------------------------------------------------
  const Talk = (function () {
    let open = false;
    let subject = null;         // {label, kind, speaks}
    let messages = [];          // text-mode transcript sent to /api/talk/message
    let busy = false;           // text-mode request in flight
    let mode = "text";          // "text" | "voice"
    let convo = null;           // ElevenLabs SDK Conversation instance (voice)
    let micMuted = false;
    // Live voice switching: the registry (from the session) + the player's
    // chosen voice (persisted). Changing it live reconnects the voice channel.
    let voices = null;
    let selectedVoiceId = "";
    try { selectedVoiceId = localStorage.getItem("talk_voice_id") || ""; } catch (_) {}
    let lastSession = null;
    let switching = false;
    let wasAutoPlay = false;     // restore auto-play on close if we paused it
    let lastFocus = null;        // restore focus on close (a11y)
    // Dynamic per-character voice bookkeeping. `voiceInUse` is the voice_id
    // actually attached to the live Convai session — so /api/talk/end can
    // drop its refcount on close and session-cleanup can then reap it.
    // `designPollTimer` polls /api/talk/voice/status for a still-designing
    // voice so we can hot-swap it into the live call when it lands.
    let voiceInUse = "";
    let voiceConnectedAt = 0;   // Date.now() the live Convai channel actually went live (cost reporting)
    let designPollTimer = null;
    let designPollTries = 0;
    let designCacheKey = "";
    // When a designed voice becomes ready while the AI is still speaking, we
    // queue it and swap the moment the AI transitions back to listening —
    // otherwise we'd cut the character off mid-word. Also tracks whether the
    // opening line has finished so a swap doesn't force it to be re-spoken.
    let pendingDesignedVoiceId = "";
    let aiIsSpeaking = false;
    let openingSpoken = false;

    function stopDesignPoll() {
      if (designPollTimer) { clearInterval(designPollTimer); designPollTimer = null; }
      designPollTries = 0;
      designCacheKey = "";
    }

    // Poll the server every ~1.5s for a background-designed voice; when it
    // flips to ready, hot-swap the live Convai session onto it. Caps at ~21s
    // of polling so a failed design falls silently back to the preset voice.
    function startDesignPoll(cacheKey) {
      stopDesignPoll();
      if (!cacheKey) return;
      designCacheKey = cacheKey;
      designPollTries = 0;
      designPollTimer = setInterval(async () => {
        designPollTries++;
        if (!open || designPollTries > 14 || designCacheKey !== cacheKey) {
          stopDesignPoll();
          return;
        }
        try {
          const res = await fetch("/api/talk/voice/status?cache_key=" + encodeURIComponent(cacheKey));
          if (!res.ok) return;
          const data = await res.json();
          if (!open || mode !== "voice" || designCacheKey !== cacheKey) return;
          if (data.status === "ready" && data.voice_id) {
            stopDesignPoll();
            // Don't override a manual pick the player made mid-generation.
            if (selectedVoiceId && selectedVoiceId !== voiceInUse) return;
            // Defer the actual swap until (a) the opening line is done AND
            // (b) the AI isn't mid-speech, so a hot-swap can never truncate
            // the character. If either condition is missing we stash the
            // voice id; the onModeChange -> "listening" path picks it up.
            if (!openingSpoken || aiIsSpeaking) {
              pendingDesignedVoiceId = data.voice_id;
              AgentLog.push("dim", "designed voice ready, queued until pause");
              return;
            }
            hotSwapDesignedVoice(data.voice_id);
          } else if (data.status === "failed" || data.status === "unknown") {
            stopDesignPoll();
          }
        } catch (e) { /* transient; keep polling */ }
      }, 1500);
    }

    // Rebuild the Convai session with the newly-designed voice id.
    // Distinct from user-driven changeVoice() in three ways so the swap
    // stays invisible-feeling to the player:
    //   1. Does NOT persist to localStorage (the human picker's contract).
    //   2. Does NOT print a chat line (too meta / draws attention).
    //   3. Passes __suppressFirstMessage so the character doesn't
    //      re-introduce themselves — the opening was already said in the
    //      fallback voice; the new voice takes over from the NEXT turn.
    // Callers must gate on !aiIsSpeaking + openingSpoken so the reconnect
    // never truncates the character mid-word.
    async function hotSwapDesignedVoice(newVoiceId) {
      if (!newVoiceId || switching || !open || mode !== "voice") return;
      if (newVoiceId === voiceInUse) return;
      pendingDesignedVoiceId = "";
      switching = true;
      setSub("channel live \u00b7 listening");
      if (convo) { try { await convo.endSession(); } catch (_) {} convo = null; }
      const reuseOpening = (lastSession && lastSession.context && lastSession.context.opening_line) || "";
      let session = null;
      try {
        session = await postJSON("/api/talk/session",
          { subject, voice_id: newVoiceId, opening_line: reuseOpening });
      } catch (e) { console.warn("[talk] hot-swap fetch failed:", e); }
      if (!open) { switching = false; return; }
      if (!session || session.mode !== "voice") { switching = false; return; }
      AgentLog.push("talk", "voice hot-swapped", "designed \u00b7 " + newVoiceId);
      beginVoice(session, "", { suppressFirstMessage: true });
    }

    // Fire-and-forget notify so server can drop the voice refcount and
    // reclaim the ElevenLabs voice slot at session end. sendBeacon survives
    // pagehide/close; fetch with keepalive is the fallback. `durationSeconds`
    // (how long the Convai channel was actually live) lets the server log a
    // cost-usage row for the conversational-agent minutes — otherwise
    // ElevenLabs TALK is invisible to the cost tracker (it's a client<->agent
    // websocket the server never proxies).
    function releaseVoiceOnClose(voiceId, durationSeconds, subj) {
      // Notify /api/talk/end to drop the voice refcount. Pass `subj` ONLY on
      // final hang-up so character memory isn't incremented on mid-call voice
      // hot-swaps (those call this with just a voice id).
      try {
        const body = JSON.stringify({
          voice_id: voiceId || "",
          session_id: SESSION_ID,
          duration_seconds: durationSeconds || 0,
          subject: subj || undefined,
        });
        if (navigator.sendBeacon) {
          const blob = new Blob([body], { type: "application/json" });
          navigator.sendBeacon("/api/talk/end", blob);
        } else {
          fetch("/api/talk/end", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: body,
            keepalive: true,
          }).catch(() => {});
        }
      } catch (_) {}
    }

    let floatTimer = 0;
    let inMoment = false; // true while a Conversation Moment chrome is up
    // Live character animation via the ONE world-model session: on enter we
    // re-anchor the session onto the character portrait (so the world model
    // animates them) and mirror that live feed into the full-screen portrait;
    // on exit we reopen the ORIGINAL world by id (attach_world — skips the
    // rebuild, paints the env still instantly via the freeze buffer, then
    // reveals the live world) so returning still feels like a resume.
    let worldSwapped = false;      // did we re-anchor the session onto the character?
    let savedWorldScene = null;    // the env scene to return to (still + prompt)
    let savedWorldId = null;       // the env world's encrypted id, for attach_world
    let savedEnvFrameDataUrl = null; // live env frame grabbed BEFORE character re-anchor
    let portraitPollTimer = null;  // waits for the character feed to go live
    let restoreInFlight = null;    // promise for the async exit restore

    function isOpen() { return open; }
    function isCinematic() { return !!(inMoment && open); }

    function setSub(text) {
      if (el.talkSub) el.talkSub.textContent = text || "";
      try {
        if (window.Moments && typeof window.Moments.setNameplate === "function" && subject) {
          window.Moments.setNameplate(subject.label, text || "");
        }
      } catch (_) {}
    }

    function setOrbState(s) {
      if (el.talkOrb) el.talkOrb.dataset.state = s || "idle";
      // Drive the living-portrait rim light via a body data attribute that
      // the Moments CSS keys off (speaking / listening / idle).
      try {
        if (inMoment) document.body.setAttribute("data-talk-orb", s || "idle");
        else document.body.removeAttribute("data-talk-orb");
      } catch (_) {}
    }

    // Fetch the cinematic portrait in parallel with the talk session. Best-
    // effort: a missing/failed portrait just leaves the developing shimmer.
    // `referenceFrame` (a data-URL grab of the CURRENT scene) lets the server
    // img2img the character INTO the same environment so it reads as the next
    // shot in the same place, not a brand-new location.
    async function fetchPortrait(subj, referenceFrame) {
      try {
        const res = await postJSON("/api/talk/portrait", {
          subject: subj,
          reference_image: referenceFrame || undefined,
        });
        if (!open || !inMoment) return;
        if (res && res.image_url && window.Moments) {
          // Show the cinematic still immediately, then animate the character
          // with the world model (re-anchor + mirror the live feed) so they
          // move. The still is the instant content + graceful fallback.
          window.Moments.setPortrait(res.image_url);
          try { animateCharacter(res.image_url, res.prompt, subj); } catch (_) {}
          // The server stored this character (with their portrait) as a
          // COMPANION for the roster — surface it the first time we meet them
          // so the player feels the world remembering people.
          try {
            if (res.companion && res.companion.first_seen && window.Moments.notify) {
              window.Moments.notify({
                icon: "\u2726",
                text: (res.companion.label || subj.label || "They") + " added to your companions",
              });
            }
          } catch (_) {}
        } else if (window.Moments) {
          const p = document.getElementById("moment-portrait");
          if (p) { p.classList.remove("developing"); p.classList.add("ready"); }
        }
      } catch (err) {
        console.warn("[talk] portrait failed:", err);
        try {
          const p = document.getElementById("moment-portrait");
          if (p) { p.classList.remove("developing"); p.classList.add("ready"); }
        } catch (_) {}
      }
    }

    // Is the realtime world model actually live right now?
    function reactorLive() {
      try {
        return Renderer.mode === "reactor" && window.ReactorRenderer &&
          window.ReactorRenderer.isActive && window.ReactorRenderer.isActive();
      } catch (_) { return false; }
    }

    // Idle-motion world prompt: no movement commands are sent, so Happy Oyster
    // just breathes the character to life (blink, sway, ambient light) around
    // the portrait first-frame.
    function buildPortraitWorldPrompt(worldPrompt, subj) {
      const who = (subj && subj.label) ? subj.label : "the figure";
      const base = (worldPrompt || "").toString().slice(0, 320);
      return (base ? base + " " : "") +
        "Cinematic medium shot of " + who + " facing the camera, standing and " +
        "breathing with subtle idle motion \u2014 a slight sway, a blink, weight " +
        "shifting \u2014 gentle ambient movement. Camera holds steady, minimal drift.";
    }

    // Animate the character with the world model: re-anchor the single session
    // onto the portrait and mirror its live feed into the full-screen portrait.
    // The env world's id (+ a live frame grab) is saved first so exit can reopen
    // it with attach_world even when the original guide PNG was swept (404).
    // Opt out with window.__CONVERSATION_ANIMATE__ === false; reduced-motion /
    // still mode keep the CSS living portrait.
    function animateCharacter(imageUrl, worldPrompt, subj) {
      if (typeof window !== "undefined" && window.__CONVERSATION_ANIMATE__ === false) return;
      if (prefersReducedMotion && prefersReducedMotion()) return;
      if (!imageUrl || !reactorLive()) return;
      const RR = window.ReactorRenderer;
      // Save the world we're leaving BEFORE re-anchoring (getWorldId returns the
      // CURRENT world, which becomes the character's after applyScene).
      try { savedWorldId = (RR.getWorldId && RR.getWorldId()) || null; } catch (_) { savedWorldId = null; }
      savedWorldScene = Renderer.lastScene ? Object.assign({}, Renderer.lastScene) : null;
      // Guide PNGs get swept from disk; a captured live frame survives so exit
      // can freeze/rebuild without hammering a 404 URL.
      savedEnvFrameDataUrl = null;
      try {
        if (typeof RR.captureFrame === "function") {
          savedEnvFrameDataUrl = RR.captureFrame(960) || null;
        }
      } catch (_) { savedEnvFrameDataUrl = null; }
      worldSwapped = true;
      // Moments.push paused the session for the takeover; resume so frames flow
      // for the character world (audio stays muted — the voice is ElevenLabs).
      try { RR.resume && RR.resume(); } catch (_) {}
      // Re-anchor DIRECTLY via the facade (NOT Renderer.applyScene) so
      // Renderer.lastScene keeps pointing at the env world we'll return to.
      try {
        RR.applyScene({
          prompt: buildPortraitWorldPrompt(worldPrompt, subj),
          imageUrl: imageUrl,
          hardTransition: true,
        });
      } catch (e) {
        worldSwapped = false;
        savedEnvFrameDataUrl = null;
        return;
      }
      try { AgentLog.push("talk", "portrait \u2192 world model", (subj && subj.label) || ""); } catch (_) {}
      startPortraitPoll(subj);
    }

    // Probe whether a guide URL still resolves (swept session images 404).
    function probeImageUrl(url, timeoutMs) {
      return new Promise((resolve) => {
        if (!url) { resolve(false); return; }
        if (/^data:/i.test(url)) { resolve(true); return; }
        const img = new Image();
        const t = setTimeout(() => { try { img.src = ""; } catch (_) {} resolve(false); }, timeoutMs || 1800);
        img.onload = () => { clearTimeout(t); resolve(true); };
        img.onerror = () => { clearTimeout(t); resolve(false); };
        img.src = url;
      });
    }

    // Wait for the re-anchored character feed to present frames, then mirror its
    // MediaStream into the portrait video and crossfade. Bounded so a stalled
    // build silently leaves the still CSS living portrait up.
    function startPortraitPoll(subj) {
      stopPortraitPoll();
      let tries = 0;
      portraitPollTimer = setInterval(() => {
        tries++;
        if (!open || !worldSwapped || tries > 50) { stopPortraitPoll(); return; } // ~10s cap
        let showing = false;
        try { showing = window.ReactorRenderer.isShowing && window.ReactorRenderer.isShowing(); } catch (_) {}
        if (!showing) return;
        const rv = document.getElementById("reactor-video");
        if (rv && rv.srcObject && window.Moments && window.Moments.setPortraitStream) {
          window.Moments.setPortraitStream(rv.srcObject);
          try { AgentLog.push("ok", "character animated (world model)", (subj && subj.label) || ""); } catch (_) {}
        }
        stopPortraitPoll();
      }, 200);
    }
    function stopPortraitPoll() {
      if (portraitPollTimer) { clearInterval(portraitPollTimer); portraitPollTimer = null; }
    }

    // Return to the env world on exit. Prefer attach_world (same world by id);
    // if that fails or the guide PNG 404s, rebuild from the prompt using a
    // captured env frame so we never leave the player frozen on the character
    // with the HUD restored. No-op if we never swapped.
    function restoreWorldAfterConversation() {
      stopPortraitPoll();
      if (!worldSwapped) return;
      worldSwapped = false;
      const RR = window.ReactorRenderer;
      const scene = savedWorldScene;
      const wid = savedWorldId;
      const envFrame = savedEnvFrameDataUrl;
      savedWorldScene = null;
      savedWorldId = null;
      savedEnvFrameDataUrl = null;
      if (!RR) return;

      // Cover the character feed immediately so Moments.pop (HUD back) doesn't
      // read as "stuck talking to them" while attach/rebuild runs.
      try {
        if (typeof RR.beginSceneFade === "function") RR.beginSceneFade();
      } catch (_) {}

      const restorePromise = (async () => {
        let guideUrl = (scene && scene.imageUrl) || null;
        // Prefer a live-captured env frame over a possibly-swept guide PNG.
        if (envFrame) guideUrl = envFrame;
        else if (guideUrl) {
          const ok = await probeImageUrl(guideUrl, 1600);
          if (!ok) guideUrl = null;
        }
        const restoreScene = scene && scene.prompt ? {
          prompt: scene.prompt,
          imageUrl: guideUrl,
          hardTransition: true,
        } : (guideUrl ? { prompt: (Renderer.lastScene && Renderer.lastScene.prompt) || "", imageUrl: guideUrl, hardTransition: true } : null);

        let restored = false;
        if (wid && typeof RR.attachWorld === "function") {
          try {
            restored = (await RR.attachWorld(wid, restoreScene || scene || undefined)) === true;
          } catch (e) {
            console.warn("[talk] attachWorld restore failed:", e);
            restored = false;
          }
        }
        if (!restored && restoreScene && restoreScene.prompt && typeof RR.applyScene === "function") {
          try {
            RR.applyScene(restoreScene);
            restored = true;
            try { AgentLog.push("talk", "exit restore via rebuild", restoreScene.imageUrl ? "with frame" : "prompt-only"); } catch (_) {}
          } catch (e) {
            console.warn("[talk] applyScene restore failed:", e);
          }
        }
        if (!restored) {
          try { if (typeof RR.endSceneFade === "function") RR.endSceneFade(); } catch (_) {}
          try { AgentLog.push("img", "conversation exit restore failed — left character world"); } catch (_) {}
        }
        return restored;
      })();
      restoreInFlight = restorePromise;
      restorePromise.finally(() => { if (restoreInFlight === restorePromise) restoreInFlight = null; });
    }

    function ensureSdk() { return ElevenSDK.load(); }

    // Mirror the latest line (yours or theirs) as a soft speech caption
    // floating over the scene — Coffee-Talk-style dialog that "hangs in the
    // air" so a conversation can happen in your peripheral vision while you
    // keep playing. This is the ONLY on-screen dialogue surface now (there is
    // no separate "expanded" transcript card) — it auto-fades on its own.
    function showFloat(content, who) {
      if (!el.talkFloat || !el.talkFloatBody || !content) return;
      if (el.talkFloatWho) el.talkFloatWho.textContent = who || (subject ? subject.label.toUpperCase() : "—");
      el.talkFloatBody.textContent = content;
      el.talkFloat.classList.remove("hidden");
      // Re-trigger the entrance animation even if it's already showing.
      el.talkFloat.classList.remove("talk-float-in");
      void el.talkFloat.offsetWidth;
      el.talkFloat.classList.add("talk-float-in");
      if (floatTimer) { clearTimeout(floatTimer); floatTimer = 0; }
      // Longer lines linger longer, up to a comfortable ceiling.
      const dwell = Math.min(9000, 3200 + content.length * 42);
      floatTimer = setTimeout(hideFloat, dwell);
    }
    function hideFloat() {
      if (floatTimer) { clearTimeout(floatTimer); floatTimer = 0; }
      if (!el.talkFloat) return;
      el.talkFloat.classList.remove("talk-float-in");
      // After the fade, hide entirely so it never blocks a click while offscreen.
      setTimeout(() => { if (el.talkFloat && !el.talkFloat.classList.contains("talk-float-in")) el.talkFloat.classList.add("hidden"); }, 320);
    }

    function addLine(role, content) {
      const line = document.createElement("div");
      line.className = "talk-line talk-" + (role === "user" ? "you" : "them");
      const who = document.createElement("span");
      who.className = "talk-who";
      who.textContent = role === "user" ? "YOU" : (subject ? subject.label.toUpperCase() : "—");
      const body = document.createElement("span");
      body.className = "talk-body";
      body.textContent = content;
      line.appendChild(who);
      line.appendChild(body);
      el.talkLog.appendChild(line);
      el.talkLog.scrollTop = el.talkLog.scrollHeight;
      // Every line — yours or theirs — also floats out as an ephemeral
      // caption over the scene, coffee-talk-style, since the floating
      // caption is the only on-screen dialogue surface now.
      showFloat(content, who.textContent);
      return line;
    }

    function typingLine() {
      const line = document.createElement("div");
      line.className = "talk-line talk-them talk-typing";
      line.innerHTML = '<span class="talk-who">' +
        (subject ? subject.label.toUpperCase() : "—") +
        '</span><span class="talk-body"><i></i><i></i><i></i></span>';
      el.talkLog.appendChild(line);
      el.talkLog.scrollTop = el.talkLog.scrollHeight;
      return line;
    }

    async function start(subj) {
      if (open) return;
      subject = { label: (subj.label || "figure"), kind: subj.kind || "", speaks: true };
      messages = [];
      busy = false;
      mode = "text";
      convo = null;
      micMuted = false;
      open = true;
      // Reset the dynamic-voice bookkeeping so a new TALK never inherits
      // a queued swap or stale "opening already spoken" state from a prior
      // conversation (which would let a hot-swap fire mid-greeting).
      pendingDesignedVoiceId = "";
      aiIsSpeaking = false;
      openingSpoken = false;
      lastFocus = document.activeElement;
      Narrator.stop(); // a two-way conversation takes over from ambient narration
      // Auto-play shouldn't advance the world mid-conversation (restore on close).
      wasAutoPlay = state.autoPlay;
      if (state.autoPlay) setAutoPlay(false);
      el.talkLog.innerHTML = "";
      el.talkModeToggle.classList.add("hidden");
      el.talkModeToggle.classList.remove("muted");
      el.talkForm.classList.remove("hidden");
      el.talkInput.value = "";
      el.talkInput.setAttribute("placeholder", "say something…");
      el.talkName.textContent = subject.label;
      setSub("establishing channel…");
      setOrbState("connecting");
      // Clear any leftover floating caption from a prior conversation.
      hideFloat();
      el.talkOverlay.classList.remove("hidden");
      el.talkOverlay.setAttribute("aria-hidden", "false");
      document.body.classList.add("talking");
      requestAnimationFrame(() => el.talkOverlay.classList.add("talk-in"));
      Haptics.select();

      // Grab the CURRENT scene frame NOW — before the letterbox/dim covers it —
      // so the portrait can be img2img'd into this exact environment (the
      // character as the next shot in the same place). Captured from whichever
      // renderer is live; null in text-only mode (portrait falls back to
      // text2img). captureScanFrame() returns { frame, size }.
      // Camp hotspots may pass a pre-captured firelit frame via
      // subject.reference_image so the close-up stays lit by the campfire.
      let referenceFrame = (subj && subj.reference_image) || null;
      if (!referenceFrame) {
        try {
          const cap = (typeof captureScanFrame === "function") ? captureScanFrame() : null;
          referenceFrame = cap && cap.frame ? cap.frame : null;
        } catch (_) { referenceFrame = null; }
      }

      // Push the Conversation Moment chrome (letterbox + portrait frame + HUD
      // hide + underlay pause). Networking below is unchanged — Moments only
      // owns presentation. Falls back to the slim overlay if Moments.js is
      // missing so TALK never hard-depends on the cinematic layer.
      //
      // Sound sequencing: Moments.push() itself fires the glitch-cut +
      // convoEnter swell as ONE entrance beat. Playing the legacy talkOpen
      // carrier tone on TOP of that would stack three overlapping cues into a
      // muddy instant, so it's reserved for the non-cinematic fallback below.
      const cinematicAvailable = !!(window.Moments && typeof window.Moments.push === "function");
      inMoment = false;
      if (cinematicAvailable) {
        try {
          await window.Moments.push("conversation", { subject: subject });
          inMoment = !!(window.Moments.isActive && window.Moments.isActive() &&
            window.Moments.topType && window.Moments.topType() === "conversation");
        } catch (e) {
          console.warn("[talk] Moments.push failed:", e);
          inMoment = false;
        }
      }
      if (!inMoment) Sound.talkOpen();

      // Fire session + portrait in parallel so time-to-content is the slower
      // of the two, not their sum.
      const sessionP = postJSON("/api/talk/session", {
        subject, voice_id: selectedVoiceId || undefined,
      }).catch((err) => { console.warn("[talk] session failed:", err); return null; });
      // Fire-and-forget alongside session; img2img off the captured frame when
      // we have one so the character lands in the same environment.
      if (inMoment) fetchPortrait(subject, referenceFrame);

      // Intimate conversation bed (ducked). Best-effort; silence is fine.
      try {
        const scoreKey = [
          subject.label,
          subject.kind || "",
          (state && state.lastScenePrompt) || "",
        ].filter(Boolean).join(" — ").slice(0, 240);
        if (scoreKey && SceneAudio && SceneAudio.scoreConversation) {
          SceneAudio.scoreConversation(scoreKey);
        }
      } catch (_) {}

      let session = null;
      try { session = await sessionP; } catch (_) { session = null; }
      if (!open) return; // closed while awaiting

      if (session && session.voices) voices = session.voices;
      const opening = (session && session.context && session.context.opening_line) || "";
      if (inMoment && window.Moments) {
        try {
          window.Moments.notify({
            icon: "✦",
            text: "Speaking with " + (subject.label || "someone"),
          });
        } catch (_) {}
      }
      if (session && session.voice_error) {
        // The server already knows voice can't work (no key, a malformed key,
        // a rejected signing request). Don't open a channel that will never
        // connect — go straight to the text conversation, which does work, and
        // say why once so it's diagnosable instead of mysterious.
        console.warn("[talk] voice unavailable:", session.voice_error);
        try { AgentLog.push("error", "voice unavailable", session.voice_error); } catch (_) {}
        beginText(opening, "text transmission \u00b7 voice unavailable");
      } else if (session && session.mode === "voice" && (session.agent_id || session.signed_url)) {
        beginVoice(session, opening);
      } else {
        beginText(opening);
      }
    }

    function beginText(opening, note) {
      mode = "text";
      switching = false; // terminal state — clear any in-flight voice-switch lock
      setSub(note || "text transmission");
      setOrbState("idle");
      el.talkModeToggle.classList.add("hidden");
      el.talkInput.setAttribute("placeholder", "say something…");
      if (opening) {
        messages.push({ role: "assistant", content: opening });
        addLine("assistant", opening);
        Sound.talkLine();
        pulseOrb();
      }
      setTimeout(() => { if (open) el.talkInput.focus(); }, 220);
    }

    // Live voice via the ElevenLabs client SDK, rendered into THIS panel: the
    // agent's spoken lines + our voice transcriptions stream in as bubbles, the
    // orb reflects listening/speaking, and typing still works (sendUserMessage).
    // Any failure (SDK blocked, mic denied, connect error) degrades to the
    // server text conversation so TALK always works.
    async function beginVoice(session, opening, opts_ext) {
      mode = "voice";
      lastSession = session;
      if (session && session.voices) voices = session.voices;
      setSub(switching ? "switching voice\u2026" : "opening channel\u2026");
      setOrbState("connecting");
      el.talkInput.setAttribute("placeholder", "speak, or type\u2026");
      // Hot-swap path passes { suppressFirstMessage: true } so the character
      // doesn't re-greet in the new voice — the opening was already said in
      // the fallback voice. Also resets the opening-spoken gate so any
      // NEXT designed-voice swap defers again until the (new) opening is done.
      var suppressFirst = !!(opts_ext && opts_ext.suppressFirstMessage);
      if (!suppressFirst) openingSpoken = false;

      let Conversation;
      try {
        Conversation = await ensureSdk();
      } catch (e) {
        console.warn("[talk] SDK load failed, falling back to text:", e);
        return beginText(opening, "text transmission (voice unavailable)");
      }
      if (!open || mode !== "voice") return;

      // A channel that never opens must not look like one that is still
      // opening. startSession() can resolve happily and then simply never
      // connect — an unauthorised private agent does exactly that — which left
      // the player staring at "opening channel…" with a dead mic and no way to
      // tell it had failed. Give it a bounded window, then use text.
      let connected = false;
      let connectTimer = null;
      const clearConnectTimer = () => { if (connectTimer) { clearTimeout(connectTimer); connectTimer = null; } };

      const opts = {
        connectionType: "websocket",
        dynamicVariables: session.dynamic_variables || {},
        onConnect: () => {
          if (!open) return;
          connected = true;
          clearConnectTimer();
          switching = false;
          AgentLog.push("ok", "talk connected", subject && subject.label);
          setSub("channel live \u00b7 listening"); setOrbState("listening");
          el.talkModeToggle.classList.remove("hidden");
          el.talkModeToggle.textContent = micMuted ? "UNMUTE" : "MUTE";
          showVoiceControl(session.voice_id);
          // Remember the voice we actually handed to Convai so /api/talk/end
          // can release its refcount at close time. Every /api/talk/session
          // call bumps the refcount, so on a re-connect (voice switch /
          // hot-swap / user pick) we release the PREVIOUS voice first —
          // otherwise its refcount would stay >0 across the whole session
          // and block reclaim.
          if (voiceInUse && voiceInUse !== session.voice_id) {
            releaseVoiceOnClose(voiceInUse, voiceConnectedAt ? (Date.now() - voiceConnectedAt) / 1000 : 0);
          }
          voiceInUse = session.voice_id || "";
          voiceConnectedAt = Date.now();
          // If a per-character voice is being designed in the background,
          // start polling so we can hot-swap it in once it lands.
          if (session.voice_status === "generating" && session.voice_cache_key) {
            AgentLog.push("dim", "casting character voice", session.voice_cache_key);
            startDesignPoll(session.voice_cache_key);
          } else {
            stopDesignPoll();
          }
        },
        onDisconnect: () => { AgentLog.push("dim", "talk disconnected"); if (open && mode === "voice") { setSub("channel closed"); setOrbState("idle"); } },
        onError: (e) => { AgentLog.push("error", "talk error", AgentLog.clip(e && (e.message || e), 120)); console.warn("[talk] voice error:", e); },
        onStatusChange: (s) => { AgentLog.push("dim", "talk status", (s && (s.status || s)) || ""); },
        onModeChange: (m) => {
          if (!open) return;
          const md = (m && (m.mode || m)) || "";
          if (md === "speaking") {
            aiIsSpeaking = true;
            setSub("channel live \u00b7 speaking"); setOrbState("speaking");
          } else if (md === "listening") {
            aiIsSpeaking = false;
            // The AI just finished a turn. Mark opening-spoken (the first
            // speaking->listening transition is when the greeting ends),
            // and if a designed voice landed while we were speaking, apply
            // it NOW so the swap never truncates the character.
            openingSpoken = true;
            setSub("channel live \u00b7 listening"); setOrbState("listening");
            if (pendingDesignedVoiceId && !switching) {
              const vid = pendingDesignedVoiceId;
              pendingDesignedVoiceId = "";
              hotSwapDesignedVoice(vid);
            }
          }
        },
        onMessage: (m) => {
          if (!open || !m) return;
          const src = m.source || m.role;
          const text = (m.message || m.text || "").trim();
          if (!text) return;
          if (src === "ai" || src === "agent") { addLine("assistant", text); Sound.talkLine(); pulseOrb(); AgentLog.push("talk", subject.label.toUpperCase() + ":", AgentLog.clip(text, 100)); }
          else if (src === "user") { addLine("user", text); AgentLog.push("dim", "YOU:", AgentLog.clip(text, 100)); }
        },
      };
      // Persona overrides (server sends them only when the agent allows it).
      const a = session.overrides && session.overrides.agent;
      const tts = session.overrides && session.overrides.tts;
      if (a || tts) {
        opts.overrides = {};
        if (a) {
          opts.overrides.agent = {};
          if (a.prompt && a.prompt.prompt) opts.overrides.agent.prompt = { prompt: a.prompt.prompt };
          // Hot-swap explicitly suppresses the first message so the character
          // doesn't re-greet in the new voice. A single space keeps the agent
          // from falling back to its DASHBOARD-configured first message
          // (which would defeat the purpose) while producing essentially no
          // audio the player would notice.
          if (suppressFirst) {
            opts.overrides.agent.firstMessage = " ";
          } else if (a.first_message) {
            opts.overrides.agent.firstMessage = a.first_message;
          }
          opts.overrides.agent.language = "en";
        }
        // Voice override — THIS is what actually makes a live voice switch change
        // the sound (the agent has tts.voice_id override enabled).
        if (tts && tts.voice_id) opts.overrides.tts = { voiceId: tts.voice_id };
      }
      // A signed URL authorizes private agents (and works for public ones);
      // otherwise connect to a public agent by id.
      if (session.signed_url) opts.signedUrl = session.signed_url;
      else opts.agentId = session.agent_id;
      AgentLog.push("talk", "opening voice", (session.signed_url ? "signed-url" : "agent " + (session.agent_id || "?")) + " \u00b7 voice " + (session.voice_id || "default"));

      try {
        convo = await Conversation.startSession(opts);
      } catch (e) {
        console.warn("[talk] voice start failed, falling back to text:", e);
        convo = null;
        clearConnectTimer();
        return beginText(opening, "text transmission (mic unavailable)");
      }
      if (!open) { try { convo.endSession(); } catch (_) {} convo = null; clearConnectTimer(); return; }
      // Armed only after startSession resolves, so a slow SDK handshake isn't
      // counted against the connect budget.
      connectTimer = setTimeout(() => {
        connectTimer = null;
        if (!open || connected || mode !== "voice") return;
        console.warn("[talk] voice channel never connected — falling back to text");
        try { AgentLog.push("error", "voice channel never opened", "falling back to text"); } catch (_) {}
        try { if (convo) convo.endSession(); } catch (_) {}
        convo = null;
        beginText(opening, "text transmission \u00b7 voice didn't connect");
      }, TALK_CONNECT_TIMEOUT_MS);
      setTimeout(() => { if (open) el.talkInput.focus(); }, 200);
    }

    // Mic mute toggle (voice mode only) — repurposes the header pill.
    function micToggle() {
      if (mode !== "voice" || !convo) return;
      micMuted = !micMuted;
      try { convo.setMicMuted(micMuted); } catch (e) { console.warn("[talk] mute failed:", e); }
      el.talkModeToggle.textContent = micMuted ? "UNMUTE" : "MUTE";
      el.talkModeToggle.classList.toggle("muted", micMuted);
      Sound.toggle();
    }

    // ---- Live voice switching -------------------------------------------
    function voiceName(id) {
      const list = (voices && voices.voices) || [];
      const v = list.find((x) => x.id === id);
      return v ? v.name : "Voice";
    }

    function showVoiceControl(currentId) {
      if (!el.talkVoiceBtn) return;
      const list = (voices && voices.voices) || [];
      if (!list.length) { el.talkVoiceBtn.classList.add("hidden"); return; }
      el.talkVoiceBtn.classList.remove("hidden");
      if (el.talkVoiceName) el.talkVoiceName.textContent = voiceName(selectedVoiceId || currentId);
      buildVoiceMenu(currentId);
    }

    function buildVoiceMenu(currentId) {
      if (!el.talkVoiceMenu) return;
      const list = (voices && voices.voices) || [];
      const active = selectedVoiceId || currentId;
      el.talkVoiceMenu.innerHTML = "";
      list.forEach((v) => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "talk-voice-item" + (v.id === active ? " active" : "");
        item.setAttribute("role", "option");
        item.innerHTML = '<span class="tv-name">' + v.name + "</span>" +
          (v.tag ? '<span class="tv-tag">' + v.tag + "</span>" : "");
        item.addEventListener("click", (e) => { e.stopPropagation(); changeVoice(v.id); });
        el.talkVoiceMenu.appendChild(item);
      });
    }

    function toggleVoiceMenu() {
      if (!el.talkVoiceMenu) return;
      const hidden = el.talkVoiceMenu.classList.toggle("hidden");
      el.talkVoiceBtn.setAttribute("aria-expanded", String(!hidden));
      if (!hidden) Sound.open();
    }

    function closeVoiceMenu() {
      if (el.talkVoiceMenu) el.talkVoiceMenu.classList.add("hidden");
      if (el.talkVoiceBtn) el.talkVoiceBtn.setAttribute("aria-expanded", "false");
    }

    // Change the active voice on the fly. Persists the choice and, if a voice
    // call is live, reconnects the channel with the new voice (the character
    // re-greets you in the new voice). The typed transcript is preserved.
    async function changeVoice(voiceId) {
      if (!voiceId || voiceId === selectedVoiceId && switching) return;
      if (switching) return; // a switch is already reconnecting — ignore rapid clicks
      const prev = selectedVoiceId;
      selectedVoiceId = voiceId;
      try { localStorage.setItem("talk_voice_id", voiceId); } catch (_) {}
      if (el.talkVoiceName) el.talkVoiceName.textContent = voiceName(voiceId);
      buildVoiceMenu(voiceId);
      closeVoiceMenu();
      Sound.toggle();
      if (mode !== "voice" || !open) return;
      if (voiceId === prev) return; // no change to a live call
      switching = true;
      setSub("switching voice…"); setOrbState("connecting");
      el.talkVoiceBtn && el.talkVoiceBtn.classList.add("switching");
      if (convo) { try { await convo.endSession(); } catch (_) {} convo = null; }
      let session = null;
      // Reuse the current opening line so the reconnect doesn't burn an LLM call
      // just to regenerate an identical greeting.
      const reuseOpening = (lastSession && lastSession.context && lastSession.context.opening_line) || "";
      try { session = await postJSON("/api/talk/session", { subject, voice_id: voiceId, opening_line: reuseOpening }); }
      catch (e) { console.warn("[talk] reconnect failed:", e); }
      el.talkVoiceBtn && el.talkVoiceBtn.classList.remove("switching");
      if (!open) return;
      if (!session || session.mode !== "voice") { switching = false; setSub("voice unavailable"); return; }
      addLine("assistant", "\u2014 now speaking as " + voiceName(voiceId) + " \u2014");
      beginVoice(session, (session.context && session.context.opening_line) || "");
    }

    async function send(text) {
      text = (text || "").trim();
      if (!text || !open) return;

      // VOICE: hand the typed line to the live agent (it takes its turn).
      if (mode === "voice") {
        if (!convo) return;
        addLine("user", text);
        el.talkInput.value = "";
        Sound.submit();
        try { convo.sendUserMessage(text); } catch (e) { console.warn("[talk] sendUserMessage failed:", e); }
        return;
      }

      // TEXT: resolve a reply from the server (story-aware LLM roleplay).
      if (busy) return;
      busy = true;
      messages.push({ role: "user", content: text });
      addLine("user", text);
      el.talkInput.value = "";
      Sound.submit();
      const typing = typingLine();
      try {
        const res = await postJSON("/api/talk/message", { subject, messages });
        const reply = (res && res.reply) || "…";
        if (!open) return;
        typing.remove();
        messages.push({ role: "assistant", content: reply });
        addLine("assistant", reply);
        Sound.talkLine();
        pulseOrb();
      } catch (err) {
        console.warn("[talk] message failed:", err);
        if (open) { typing.remove(); addLine("assistant", "[the signal drops — try again]"); }
      } finally {
        busy = false;
        if (open) el.talkInput.focus();
      }
    }

    function pulseOrb() {
      // Pulse the presence orb so a new line reads as a "presence beat".
      if (!el.talkOrb) return;
      el.talkOrb.classList.remove("talk-orb-pulse");
      void el.talkOrb.offsetWidth;
      el.talkOrb.classList.add("talk-orb-pulse");
    }

    function close() {
      if (!open) return;
      open = false;
      switching = false;
      hideFloat();
      stopDesignPoll();
      pendingDesignedVoiceId = "";
      aiIsSpeaking = false;
      openingSpoken = false;
      // Notify the server so it drops the refcount on the voice we've been
      // using AND records a lightweight per-character memory entry. Capture
      // subject before we null it out.
      const releasedVoice = voiceInUse;
      const releasedDuration = voiceConnectedAt ? (Date.now() - voiceConnectedAt) / 1000 : 0;
      const closedSubject = subject;
      voiceInUse = "";
      voiceConnectedAt = 0;
      releaseVoiceOnClose(releasedVoice, releasedDuration, closedSubject);
      // Sound sequencing: Moments.pop() below fires its own glitch-cut +
      // convoExit chord as ONE exit beat. Only play the legacy talkClose tone
      // when there's no cinematic chrome to hand off to, so exit doesn't stack
      // two overlapping "hang up" cues.
      if (!inMoment) Sound.talkClose();
      if (convo) { try { convo.endSession(); } catch (_) {} convo = null; }
      closeVoiceMenu();
      if (el.talkVoiceBtn) el.talkVoiceBtn.classList.add("hidden");
      el.talkOverlay.classList.remove("talk-in");
      el.talkOverlay.setAttribute("aria-hidden", "true");
      document.body.classList.remove("talking");
      document.body.removeAttribute("data-talk-orb");

      // If we animated the character (re-anchored the session onto them), reopen
      // the ORIGINAL world by id NOW — attach_world paints the env still
      // instantly (freeze buffer) then reveals the live world, hidden by the
      // exit glitch + letterbox retract. If we never swapped, this is a no-op
      // and Moments.pop -> resumeUnderlay simply resumes the paused world.
      restoreWorldAfterConversation();

      // Pop the Conversation Moment: retract the letterbox, restore the HUD, and
      // resume the world (Moments.pop -> Renderer.resumeUnderlay). Guarded so a
      // double-close or missing Moments.js never throws.
      if (inMoment && window.Moments && typeof window.Moments.pop === "function") {
        try {
          if (window.Moments.topType && window.Moments.topType() === "conversation") {
            window.Moments.pop({ subject: closedSubject });
          }
        } catch (e) { console.warn("[talk] Moments.pop failed:", e); }
      }
      inMoment = false;
      try {
        if (SceneAudio && typeof SceneAudio.endConversation === "function") {
          SceneAudio.endConversation();
        }
      } catch (_) {}

      setTimeout(() => {
        el.talkOverlay.classList.add("hidden");
        el.talkLog.innerHTML = "";
        // The conversation replaced the hotspot overlay — the SCAN button is
        // available again once it closes (mirrors camera/tape/free-will close).
        updateScanButton();
        try { updateCampButton(); } catch (_) {}
      }, 260);
      subject = null;
      messages = [];
      mode = "text";
      // Restore what we paused/where focus was.
      if (wasAutoPlay && !state.gameOver) { wasAutoPlay = false; setAutoPlay(true); }
      if (lastFocus && typeof lastFocus.focus === "function") { try { lastFocus.focus(); } catch (_) {} }
      lastFocus = null;
    }

    // Escape handler: collapse the voice menu first if it's open, otherwise
    // end the conversation. There's no "fold to a chip" step anymore — the
    // control strip is already minimal — so a single Esc just hangs up.
    function onEscape() {
      if (el.talkVoiceMenu && !el.talkVoiceMenu.classList.contains("hidden")) { closeVoiceMenu(); return; }
      close();
    }

    return {
      start, close, isOpen, isCinematic, micToggle, send,
      toggleVoiceMenu, closeVoiceMenu, onEscape,
    };
  })();

  // ------------------------------------------------------------------
  // Conversation Moment type — presentation only. Talk owns networking.
  // Registered against the Moments stack so future set-pieces (interrogation,
  // flashback, trade) can reuse the same letterbox / pause / notify chrome.
  // ------------------------------------------------------------------
  (function registerConversationMoment() {
    if (!window.Moments || typeof window.Moments.register !== "function") return;
    window.Moments.register("conversation", {
      // enter: chrome is already shown by Moments.push; Talk.start drives the
      // session/portrait fetches. We only confirm the nameplate here.
      async enter(payload) {
        const subj = (payload && payload.subject) || {};
        try {
          window.Moments.setNameplate(subj.label || "—", "establishing…");
        } catch (_) {}
        return true;
      },
      async exit(/* result */) {
        // Talk.close already restored SceneAudio / cleared portrait via pop's
        // hideOverlayChrome. Nothing else to tear down for MVP.
        return true;
      },
      onEsc() {
        // Defer to Talk's Escape handler so voice-menu collapse still works.
        try { if (Talk && Talk.onEscape) Talk.onEscape(); } catch (_) {}
        return true;
      },
    });
  })();

  // QA hook: expose the real Talk controller ONLY when explicitly requested via
  // ?talkdev in the URL, so automated/manual tests can open a conversation
  // without a live SCAN detection. No-op in normal play.
  try {
    if (typeof location !== "undefined" && /(?:\?|&)talkdev\b/.test(location.search)) {
      window.__TALK__ = Talk;
      window.__STARTTALK__ = (obj) => startTalk(null, obj); // real entry (closes scan first)
      // Scan/OCR observability for QA (dev-gated): inspect the ambient overlay
      // state and force a still so hotspot behavior is testable without a live
      // image backend.
      const _primeStill = (url) => new Promise((res) => {
        const img = new Image();
        img.setAttribute("data-src", url);
        img.onload = () => { state.currentStillUrl = url; state.scanStillImg = img; try { Renderer.mode = "image"; } catch (_) {} res(true); };
        img.onerror = () => res(false);
        img.src = url;
      });
      window.__SCAN__ = {
        on: () => state.scanOn,
        hidden: () => !!(el.scanLayer && el.scanLayer.classList.contains("hidden")),
        available: () => scanAvailable(),
        // Fire a manual scan pass (the SCAN button's action) for QA.
        trigger: () => triggerScan(),
        // Prime a decoded still + force image mode, then fire a scan pass (QA).
        forceStill: (url) => _primeStill(url).then((ok) => { triggerScan(); return ok; }),
        // Prime a still WITHOUT scanning — lets a test drive the scan itself.
        primeStill: _primeStill,
        // Make the scene un-scannable (simulates the realtime video not showing).
        dropStill: () => { state.currentStillUrl = null; state.scanStillImg = null; },
      };
    }
  } catch (_) {}

  // Entry point from a SCAN tag's TALK action: close the scan overlay (the
  // conversation takes over) and open the talk layer for this subject.
  function startTalk(tag, obj) {
    if (state.gameOver) { closeTagPrompt(tag); return; }
    const subj = obj || (tag && tag._obj) || { label: "figure" };
    closeTagPrompt(tag);
    closeScan();
    Talk.start(subj);
  }

  // ------------------------------------------------------------------
  // NARRATOR — a disembodied voice that frames the world (and can be many).
  //
  // A one-way voice OVER the scene (vs. TALK, which is two-way with a subject
  // IN it). It asks the server to GENERATE a short, story-aware world-building
  // narration — optionally a radio-play script that hands off between a cast of
  // voices (narrator / man / woman / elder / creature / machine / warden) — then
  // plays each line's ElevenLabs audio in sequence with lower-third subtitles.
  // Built to expand: point it at a focus, auto-narrate scene changes, etc.
  // ------------------------------------------------------------------
  const Narrator = (function () {
    let playing = false;
    let busy = false;
    let gen = 0;                 // bumped by stop() to abort in-flight work
    let convo = null;            // the active per-segment SDK session
    let agentCfg = null;         // {agent_id, signed_url} — the narrator's agent
    let agentAvailable = null;   // did the server advertise a usable agent?
    let silentInput = null;      // synthetic silent mic (see startSessionNoMic)

    function isBusy() { return busy || playing; }

    // The narrator is ONE-WAY (a voice OVER the scene, never listening), but the
    // ElevenLabs VoiceConversation SDK always grabs a microphone on connect —
    // so on any device with no mic, no mic permission, or a locked-down browser,
    // startSession() throws "Requested device not found" and narration silently
    // dies. Since we never actually listen, we hand the SDK a SYNTHETIC SILENT
    // audio track instead of a real mic: voice OUT still plays, and there is zero
    // microphone dependency. makeSilentMicStream() builds that track from a muted
    // WebAudio graph; closeSilentInput() tears it down.
    function makeSilentMicStream() {
      try {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return null;
        const ctx = new Ctx();
        const dest = ctx.createMediaStreamDestination();
        // A gain-0 oscillator keeps the output track "live" (unended) without
        // ever emitting audible sound.
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        gain.gain.value = 0;
        osc.connect(gain).connect(dest);
        osc.start();
        return { stream: dest.stream, ctx, osc };
      } catch (_) { return null; }
    }

    function closeSilentInput() {
      if (!silentInput) return;
      const s = silentInput; silentInput = null;
      try { s.osc && s.osc.stop(); } catch (_) {}
      try { s.stream && s.stream.getTracks().forEach((t) => t.stop()); } catch (_) {}
      try { s.ctx && s.ctx.close(); } catch (_) {}
    }

    // Start a narrator SDK session WITHOUT requiring a real microphone. We
    // briefly shim navigator.mediaDevices.getUserMedia so the SDK's connect-time
    // audio-capture request resolves to a clone of our silent track, then restore
    // the original immediately (the narrator never switches input devices, so
    // getUserMedia is only called once, during startSession). Any failure falls
    // back to the real getUserMedia — and the caller still degrades to timed
    // subtitles if that throws too.
    async function startSessionNoMic(Conversation, opts) {
      const md = (navigator && navigator.mediaDevices) || null;
      const orig = md && md.getUserMedia ? md.getUserMedia.bind(md) : null;
      closeSilentInput();
      silentInput = orig ? makeSilentMicStream() : null;
      if (orig && silentInput && silentInput.stream) {
        md.getUserMedia = (constraints) => {
          try {
            if (constraints && constraints.audio && silentInput && silentInput.stream) {
              // Hand back a fresh clone so the SDK can stop "its" tracks at
              // endSession without ending our keep-alive source.
              return Promise.resolve(silentInput.stream.clone());
            }
          } catch (_) {}
          return orig(constraints);
        };
      }
      try {
        return await Conversation.startSession(opts);
      } finally {
        if (orig) { try { md.getUserMedia = orig; } catch (_) {} }
      }
    }

    function show(speaker, text) {
      if (el.narratorSpeaker) el.narratorSpeaker.textContent = speaker ? speaker.toUpperCase() : "";
      if (el.narratorLine) el.narratorLine.textContent = text || "";
      if (!el.narratorBar) return;
      // Sit the caption ABOVE the action wheel so it never covers the controls.
      const wheelH = (el.actionWheel && el.actionWheel.offsetHeight) || 120;
      el.narratorBar.style.bottom = "calc(env(safe-area-inset-bottom, 0px) + " + (wheelH + 22) + "px)";
      el.narratorBar.classList.remove("hidden");
      el.narratorBar.setAttribute("aria-hidden", "false");
      requestAnimationFrame(() => el.narratorBar.classList.add("narrator-in"));
    }

    function hide() {
      if (!el.narratorBar) return;
      el.narratorBar.classList.remove("narrator-in");
      el.narratorBar.setAttribute("aria-hidden", "true");
      setTimeout(() => { if (!playing) el.narratorBar.classList.add("hidden"); }, 320);
    }

    async function endConvo() {
      if (convo) { const c = convo; convo = null; try { await c.endSession(); } catch (_) {} }
      closeSilentInput();
    }

    // Speak ONE segment through the generative agent: a short SDK session whose
    // FIRST MESSAGE is the exact narration line, in the segment's voice. The
    // agent utters it, we detect it finished (mode → listening, or a hard
    // timeout), then tear the session down and move on. Mic is muted (one-way).
    function speakSegment(seg, myGen) {
      return new Promise(async (resolve) => {
        show(seg.character, seg.text);
        Sound.talkLine();
        AgentLog.push("narrator", (seg.character || "narrator").toUpperCase() + ":", AgentLog.clip(seg.text, 100));
        // No voice channel possible → timed subtitle.
        const timed = () => setTimeout(resolve, Math.max(2600, (seg.text || "").length * 60));
        if (!agentCfg || !agentCfg.agent_id && !agentCfg.signed_url || state.soundEnabled === false) {
          if (state.soundEnabled === false) AgentLog.push("dim", "muted \u2014 subtitle only");
          else AgentLog.push("warn", "no narrator agent \u2014 subtitle only");
          timed();
          return;
        }
        let Conversation, done = false, spoke = false, hardTimer = null;
        const finish = async (why) => {
          if (done) return; done = true;
          clearTimeout(hardTimer);
          await endConvo();
          resolve();
        };
        try { Conversation = await ElevenSDK.load(); }
        catch (e) { AgentLog.push("error", "narrator SDK failed \u2014 subtitle", String(e).slice(0, 80)); timed(); return; }
        if (myGen !== gen) { resolve(); return; }
        const opts = {
          connectionType: "websocket",
          overrides: {
            agent: {
              prompt: { prompt: "You are a disembodied narrator. Utter the first message EXACTLY as written, once, as narration. Then say nothing further and do not ask questions." },
              firstMessage: seg.text,
              language: "en",
            },
            tts: { voiceId: seg.voice_id },
          },
          onConnect: () => { AgentLog.push("ok", "narrator connected", seg.voice_id); },
          onModeChange: (m) => {
            const md = (m && (m.mode || m)) || "";
            if (md === "speaking") spoke = true;
            // Finished uttering the line → wrap this segment.
            else if (md === "listening" && spoke) finish("spoke");
          },
          onError: (e) => { AgentLog.push("error", "narrator seg error", AgentLog.clip(e && (e.message || e), 100)); finish("error"); },
          onDisconnect: () => { if (spoke) finish("disconnect"); },
        };
        if (agentCfg.signed_url) opts.signedUrl = agentCfg.signed_url; else opts.agentId = agentCfg.agent_id;
        try {
          // One-way: feed a synthetic silent mic so no real microphone (or mic
          // permission) is ever required just to HEAR the narration.
          convo = await startSessionNoMic(Conversation, opts);
          try { convo.setMicMuted(true); } catch (_) {} // one-way — never listen
        } catch (e) {
          AgentLog.push("error", "narrator start failed \u2014 subtitle", AgentLog.clip(e && (e.message || e), 100));
          convo = null; timed(); return;
        }
        if (myGen !== gen) { finish("aborted"); return; }
        // Safety net: never hang on a segment (long line ≈ read time + buffer).
        hardTimer = setTimeout(() => finish("timeout"), Math.max(9000, (seg.text || "").length * 90));
      });
    }

    async function play(segments, myGen) {
      playing = true;
      if (el.narratorBtn) el.narratorBtn.classList.add("on");
      for (const seg of segments) {
        if (myGen !== gen || !seg || !(seg.text || "").trim()) continue;
        // Optional inter-line pause — used by transition() to hold a beat of
        // silence between the bridging line and the "dark truth" that follows.
        const pause = seg && typeof seg._preDelayMs === "number" ? seg._preDelayMs : 0;
        if (pause > 0) {
          await new Promise((r) => setTimeout(r, pause));
          if (myGen !== gen) break;
        }
        await speakSegment(seg, myGen);
        if (myGen !== gen) break;
      }
      playing = false;
      if (el.narratorBtn) el.narratorBtn.classList.remove("on");
      if (myGen === gen) hide();
    }

    // Generate a story-aware narration server-side (LLM), then SPEAK it live via
    // the generative agent in the browser (no server TTS key required).
    async function narrate(opts) {
      opts = opts || {};
      if (Talk.isOpen()) { showRendererToast("End the conversation to hear the narrator"); return; }
      if (busy || playing) { stop(); return; }
      const myGen = ++gen;
      busy = true;
      if (el.narratorBtn) el.narratorBtn.classList.add("on");
      show("narrator", "\u2026");
      Sound.talkOpen();
      AgentLog.push("narrator", "worldbuild\u2026", opts.focus ? AgentLog.clip(opts.focus, 60) : (opts.multi !== false ? "multi" : "single"));
      try {
        // speak:false → server returns TEXT + per-line voice + agent config; the
        // browser voices it as a generative agent.
        const res = await postJSON("/api/narrator/worldbuild", {
          multi: opts.multi !== false, speak: false, focus: opts.focus || "",
        });
        if (myGen !== gen) return;
        agentCfg = (res && res.agent) || agentCfg;
        agentAvailable = !!(res && res.agent_available);
        const segs = (res && res.segments) || [];
        AgentLog.push("dim", "worldbuild \u2192 " + segs.length + " line(s)", agentAvailable ? "agent voice" : "subtitles");
        if (!segs.length) { show("narrator", "The channel is silent."); setTimeout(() => { if (myGen === gen) hide(); }, 1800); return; }
        busy = false;
        await play(segs, myGen);
      } catch (e) {
        AgentLog.push("error", "worldbuild failed", AgentLog.clip(e && (e.message || e), 100));
        if (myGen === gen) { show("narrator", "The signal breaks up\u2026"); setTimeout(() => { if (myGen === gen) hide(); }, 1800); }
      } finally {
        busy = false;
        if (!playing && el.narratorBtn) el.narratorBtn.classList.remove("on");
      }
    }

    function stop() {
      gen++; // abort in-flight worldbuild / segment loop / timers
      playing = false;
      busy = false;
      endConvo();
      if (el.narratorBtn) el.narratorBtn.classList.remove("on");
      hide();
    }

    // Learn whether the narrator can SPEAK (a generative agent is configured)
    // and cache its agent config; reflect it on the control's tooltip.
    async function preflight() {
      try {
        const c = await getJSON("/api/narrator/cast");
        agentAvailable = !!(c && c.agent_available);
        agentCfg = (c && c.agent) || null;
        AgentLog.push("narrator", "preflight", "agent " + (agentAvailable ? "ready (" + ((agentCfg && agentCfg.agent_id) || "?") + ")" : "NOT configured") + " \u00b7 tts key " + ((c && c.voice_available) ? "yes" : "no"));
      } catch (e) { agentAvailable = null; AgentLog.push("warn", "preflight failed", AgentLog.clip(e && (e.message || e), 80)); }
      if (el.narratorBtn) {
        el.narratorBtn.title = agentAvailable === false
          ? "Narrator — world-building subtitles (voice agent not configured) (N)"
          : "Narrator — a voice frames the world (N)";
      }
    }

    // A cold open at the start of a run. Browser autoplay policy forbids audio
    // before a user gesture, so if audio isn't unlocked yet we DEFER the cold
    // open (arm a pending flag) instead of dropping it silently — otherwise the
    // very first playthrough is mute and narration only ever appears on a second
    // run (after a Reset click unlocks audio). onAudioUnlocked() fires the
    // pending cold open on the first real gesture.
    let pendingColdOpen = false;
    function coldOpen() {
      if (state.gameOver || Talk.isOpen() || isBusy()) return;
      if (!state.audioUnlocked) { pendingColdOpen = true; return; }
      pendingColdOpen = false;
      narrate({ multi: false, focus: "You have just woken up here. Say how uneasy you feel and that you need to find out what happened." });
    }

    // Called from the global audio-unlock gesture handler. If a cold open was
    // deferred because audio wasn't unlocked at start, play it now that the
    // user's first gesture has satisfied autoplay policy.
    function onAudioUnlocked() {
      if (!pendingColdOpen) return;
      pendingColdOpen = false;
      // Small delay so the unlock gesture (and any scene it triggered) settles
      // before the narrator opens; still guards on gameOver/Talk/busy inside.
      setTimeout(() => coldOpen(), 300);
    }

    // A final, funereal line over the death screen.
    function epitaph() {
      narrate({ multi: false, focus: "You have just died here. One short, final line about your end and what you never found out." });
    }

    // TWO narrator lines fired when the player commits to a MOVE TO, back-to-
    // back with a 1 s beat of silence between them, so the narrator carries
    // the black loading beat while the next scene generates instead of one
    // short line and a lot of silence.
    //   1. BRIDGING LINE — the trip in motion, world closing behind them,
    //      next place looming (matches the fade-to-black feel).
    //   2. DARK TRUTH — one buried confession about why the REPORTER is
    //      really out here: the private motive they haven't admitted, the
    //      thing pulling them into this on purpose. Different every trip —
    //      the LLM keeps it story-aware from focus + state.
    // Both scripts are fetched in PARALLEL so the second line is ready by the
    // time we need it, then played sequentially with a 1 s inter-line pause.
    // Runs the AJAX + agent handshake even if state.audioUnlocked was still
    // false at call time: MOVE TO's own click IS a user gesture (pointerdown
    // unlocks audio first, then the button's click fires and lands here), so
    // a stale audioUnlocked flag mustn't silently swallow the narration.
    const TRANSITION_INTERLINE_PAUSE_MS = 1000;
    function transition(destination) {
      if (state.gameOver || Talk.isOpen()) return false;
      // Drop any in-flight narration so this run starts NOW, not after the
      // previous one finishes reading itself out.
      stop();
      // Fire the async two-line sequence; don't block the caller.
      _runTransition(destination);
      return true;
    }
    async function _runTransition(destination) {
      const myGen = ++gen;
      busy = true;
      if (el.narratorBtn) el.narratorBtn.classList.add("on");
      show("narrator", "\u2026");
      Sound.talkOpen();
      const dest = (destination || "").toString().trim().slice(0, 80);
      const bridgeFocus = dest
        ? "The player has just committed to travel to the " + dest + ". Speak ONE short, tense bridging line \u2014 the trip in motion, the world closing behind them, the next place looming \u2014 as the scene fades to black."
        : "The player has just committed to travel to a new location. Speak ONE short, tense bridging line \u2014 the trip in motion, the world closing behind them, the next place looming \u2014 as the scene fades to black.";
      const truthFocus =
        "REVEAL A DARK TRUTH. Speak ONE short first-person confession from the reporter \u2014 the BURIED reason they're really out here. Not the assignment, not the cover story: the private motive they haven't admitted to themselves. A guilt, a debt, a person they lost, a thing they did, a thing they're chasing that will destroy them. Concrete and specific to this world's premise + recent events. Ominous, quiet, honest. First person, one short sentence, no meta.";
      AgentLog.push("narrator", "transition\u2026", "bridge + dark truth");
      try {
        // One request carries BOTH focuses (follow_focus is appended server-
        // side to the primary script's segments) so we never hit the per-IP
        // rate limit on the second line.
        const res = await postJSON("/api/narrator/worldbuild", {
          multi: false, speak: false,
          focus: bridgeFocus,
          follow_focus: truthFocus,
        });
        if (myGen !== gen) return;
        agentCfg = (res && res.agent) || agentCfg;
        agentAvailable = !!(res && res.agent_available);
        const raw = (res && res.segments) || [];
        // First segment is the BRIDGE, second is the DARK TRUTH; only the
        // dark-truth line carries the inter-line pause so there's a beat of
        // silence between the two.
        const segs = raw.slice(0, 2).map((s, i) => (
          i === 0 ? s : Object.assign({}, s, { _preDelayMs: TRANSITION_INTERLINE_PAUSE_MS })
        ));
        AgentLog.push("dim", "transition \u2192 " + segs.length + " line(s)", agentAvailable ? "agent voice" : "subtitles");
        if (!segs.length) {
          show("narrator", "The channel is silent.");
          setTimeout(() => { if (myGen === gen) hide(); }, 1800);
          return;
        }
        busy = false;
        await play(segs, myGen);
      } catch (e) {
        AgentLog.push("error", "transition failed", AgentLog.clip(e && (e.message || e), 100));
        if (myGen === gen) {
          show("narrator", "The signal breaks up\u2026");
          setTimeout(() => { if (myGen === gen) hide(); }, 1800);
        }
      } finally {
        busy = false;
        if (!playing && el.narratorBtn) el.narratorBtn.classList.remove("on");
      }
    }

    return { narrate, stop, isBusy, preflight, coldOpen, onAudioUnlocked, epitaph, transition };
  })();

  function toggleNarrator() {
    if (state.gameOver) return;
    if (Narrator.isBusy()) { Narrator.stop(); return; }
    Narrator.narrate({ multi: false });
  }

  // Drop the current tags (e.g. when a turn changes the scene) so stale labels
  // don't hover over a shot they no longer describe; the next SCAN repopulates.
  function clearScanTags() {
    if (!el.scanTags) return;
    // A scene change makes the auto fade-out moot — cancel it so a pending
    // teardown can't fire against the next scan.
    clearTimeout(state.scanFadeTimer); state.scanFadeTimer = null;
    clearTimeout(state.scanFadeOutTimer); state.scanFadeOutTimer = null;
    el.scanTags.innerHTML = "";
    state.scanObjects = [];
    state.scanTagActing = null;
    // The scene is changing — drop the stale detection so hotspots never linger
    // from the previous shot; the next SCAN reads the new scene fresh.
    state.scanPrewarm = { objects: [], size: null, ts: 0 };
    setScanHint("");
    updateScanButton();
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
    updateScanButton(); // the SCAN button is available again once the input closes
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
    // Director experience (Happy Oyster): typed text is a live `instruct` that
    // steers the unfolding scene — the Directing steering verb — not a full turn.
    try {
      if (Renderer.mode === "reactor" && Renderer.reactorAvailable() &&
          window.ReactorRenderer.getExperience &&
          window.ReactorRenderer.getExperience() === "director") {
        el.customInput.value = "";
        const ok = window.ReactorRenderer.instruct(text);
        Sound.submit();
        closeFreeWill(true);
        showRendererToast(ok ? "Directing: instruction sent" : "World not ready yet");
        if (ok) RtLog.push("prompt", "\u25B8 instruct", RtLog.clip(text, 160));
        return;
      }
    } catch (_) {}
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
      // Bottom-left HUD shows the IMAGE provider (what actually draws the
      // world), not the text/chat backend. Krea shows its tier (medium/large).
      if (el.backendName) {
        const prov = s.image_provider || s.backend || "unknown";
        const model = s.image_model || "";
        let label = prov;
        if (prov === "krea" && model) {
          const tier = model.includes("large") ? "large" : (model.includes("medium") ? "medium" : "");
          label = tier ? ("krea " + tier) : "krea";
        }
        el.backendName.textContent = label;
      }
      if (typeof s.image_enabled === "boolean") state.imagesEnabled = s.image_enabled;
      // HEALTH has no readout. It used to sit in the dossier HUD next to the
      // score, which meant carrying that whole panel for a number that never
      // moves while DAMAGE_SYSTEM_ENABLED is false. When combat returns, health
      // wants its own surface tied to the danger vignette that actually drains
      // it — not a digit parked beside the photo tally.
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
    updateScanButton(); // the SCAN button is available again once the tape deck closes
  }

  function toggleSound() {
    state.soundEnabled = !state.soundEnabled;
    el.btnSnd.classList.toggle("off", !state.soundEnabled);
    const ico = el.btnSnd.querySelector(".rail-ico");
    if (ico) ico.textContent = state.soundEnabled ? "\u266A" : "\u2715"; // ♪ / ✕
    if (state.soundEnabled) { Sound.resume(); Sound.select(); }
    try { SceneAudio.setEnabled(state.soundEnabled); } catch (_) {}
    // Muting silences the narrator's ambient voice too (it plays real audio).
    if (!state.soundEnabled) { try { Narrator.stop(); } catch (_) {} }
    // Danger's sustained tones (heartbeat, tinnitus) are actual live audio
    // nodes, not one-shots, so a mute mid-HURTING has to explicitly cut
    // them; the DangerSystem will restart them on the next state-machine
    // tick once sound is enabled again.
    if (!state.soundEnabled) {
      try { Sound.heartbeatStop && Sound.heartbeatStop(); } catch (_) {}
      try { Sound.tinnitusStop && Sound.tinnitusStop(); } catch (_) {}
      try { DangerSystem && DangerSystem.onSoundToggled && DangerSystem.onSoundToggled(false); } catch (_) {}
    } else {
      try { DangerSystem && DangerSystem.onSoundToggled && DangerSystem.onSoundToggled(true); } catch (_) {}
    }
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

  // Backtick toggles the World Editor. NOT E — E is strafe-right in movement
  // mode (see the Q/E pair below), which is why the "press E" the docs used to
  // promise could never have worked.
  function _isEditorToggleKey(e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return false;
    return e.key === "`";
  }

  function onKeydown(e) {
    // First-run tutorial is a modal takeover: any key dismisses it and is
    // swallowed so a world shortcut (S=scan, C=photo…) doesn't fire behind it.
    if (el.scanTutorial && !el.scanTutorial.classList.contains("hidden")) {
      e.preventDefault();
      dismissScanTutorial();
      return;
    }
    // WORLD EDITOR — backtick (`) toggles it from anywhere; Esc closes it.
    // While it's open, keystrokes stay inside its text fields (so typing a
    // prompt never fires a world/movement shortcut behind the panel).
    {
      const _ae = document.activeElement;
      const _typing = _ae && (_ae.tagName === "TEXTAREA" || _ae.tagName === "INPUT");
      // The pop-out prompt editor is a modal takeover and handles its own keys
      // (Esc cancels, Ctrl/Cmd+S saves) on the dialog element.
      if (WorldEditor.modalIsOpen && WorldEditor.modalIsOpen()) return;
      if (WorldEditor.isOpen()) {
        // Esc while typing gets you OUT OF THE FIELD, not out of the editor —
        // closing the whole panel mid-sentence loses the thread of the edit.
        if (e.key === "Escape") {
          e.preventDefault();
          if (_typing) _ae.blur(); else WorldEditor.close();
          return;
        }
        if (_isEditorToggleKey(e) && !_typing) { e.preventDefault(); WorldEditor.close(); return; }
        return; // typing passes through; all other shortcuts are blocked behind the editor
      } else if (_isEditorToggleKey(e) && !_typing) {
        e.preventDefault(); WorldEditor.open(); return;
      }
    }
    // Conversation Moments are a cinematic takeover — Esc hangs up, typing
    // goes to the composer, and world shortcuts (choices / ACT / PHOTO / SCAN)
    // stay blocked until the Moment pops. The legacy non-cinematic TALK strip
    // (Moments.js missing) still allows fall-through so the world stays
    // playable under a companion HUD.
    if (Talk.isOpen()) {
      const typing = document.activeElement === el.talkInput;
      const cinematic = !!(Talk.isCinematic && Talk.isCinematic());
      if (e.key === "Escape") { e.preventDefault(); Talk.onEscape(); return; }
      if (typing) return; // let the composer handle the rest
      if (cinematic) return; // block world shortcuts during the Moment
      // Non-cinematic companion HUD: fall through so global shortcuts fire.
    }
    if (window.Moments && window.Moments.isActive && window.Moments.isActive()) {
      if (e.key === "Escape") {
        e.preventDefault();
        try { window.Moments.onEscape(); } catch (_) {}
        return;
      }
      return; // any active Moment owns the keyboard
    }
    // Playable camp level: Esc drives out (same as LEAVE CAMP). Instruments
    // above (Talk / Moments / tape / camera) already returned.
    if (state.inCamp && e.key === "Escape") {
      e.preventDefault();
      leaveCamp();
      return;
    }
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
    // While dead, R restarts and C inserts a coin to continue. C is a
    // no-op when the coin-op feature is disabled, so no per-mode branching
    // is needed here.
    if (state.gameOver) {
      const k = e.key.toLowerCase();
      if (k === "r") resetGame();
      else if (k === "c") { try { CoinOp.insertCoin(); } catch (_) {} }
      return;
    }
    // While the "OUT OF COINS" pause overlay is up, the world is frozen
    // behind it — same keyboard contract as game-over. Also prevents the
    // regular 'C = capturePhoto' shortcut from firing invisibly under
    // the modal.
    if (CoinOp.isPaused && CoinOp.isPaused()) {
      const k = e.key.toLowerCase();
      if (k === "r") { resetGame(); return; }
      if (k === "c") { try { CoinOp.insertCoin(); } catch (_) {} return; }
      if (e.key === "Escape") { return; }
    }
    // Drive joystick owns the drive keys while realtime video is on — hold to
    // go, release to stop. Mapping comes from InputBindings (default FPS:
    // WASD move, arrows look; CLASSIC: A/D look). Mouse look is separate
    // (pointer-lock). This reassigns those keys in LIVE mode only (D no longer
    // toggles the debug log — use the DEBUG button). In still mode the keys
    // keep their old meaning.
    if (Movement.enabled() && !e.ctrlKey && !e.metaKey && !e.altKey) {
      // Esc releases mouse-look pointer-lock first (browser also exits lock).
      if (e.key === "Escape") {
        try { if (MouseLook.isLocked()) { MouseLook.releaseLock(); e.preventDefault(); return; } } catch (_) {}
      }
      const mk = Movement.keyFor(e.key);
      if (mk) {
        e.preventDefault();
        if (!e.repeat) Movement.pressKey(mk);
        return;
      }
      // Hold Shift to Sprint (a held interaction verb) while exploring the live
      // world — composes with movement. Released on keyup.
      if (e.key === "Shift") { if (!e.repeat) { try { VerbBar.onShift(true); } catch (_) {} } return; }
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
    } else if (e.key.toLowerCase() === "n") {
      toggleNarrator(); // narrator — a voice frames the world
    } else if (e.shiftKey && e.key === "D") {
      // Danger demo — scripted safe → warning → hurting → safe sequence so
      // the vignette / HP bar / shake / heartbeat can be experienced on any
      // scene, even when the vision loop hasn't tripped. See DangerSystem.demo.
      try { DangerSystem.demo(); } catch (_) {}
    } else if (e.key.toLowerCase() === "d") {
      AgentLog.toggle(); // voice-agent debug log
    } else if (e.key.toLowerCase() === "p") {
      toggleAutoPlay();
    } else if (e.key.toLowerCase() === "f") {
      openFreeWill();
    } else if (e.key.toLowerCase() === "h") {
      openTouch(); // camera (SNAP) tool — tap to capture evidence
    } else if (e.key.toLowerCase() === "s") {
      triggerScan(); // SCAN — one recognition pass; tags fade after a few seconds
    } else if (e.key.toLowerCase() === "c") {
      capturePhoto(); // journalist photograph — file a specimen to the case file
    } else if (e.key.toLowerCase() === "l") {
      RtLog.toggle(); // show/hide the world-model inspector log
    } else if (e.key.toLowerCase() === "i") {
      ImageModel.toggle(); // show/hide the image-generator (still-frame) model menu
    } else if (e.key.toLowerCase() === "j") {
      StoryLog.toggle(); // show/hide the story log (the run chronicle)
    } else if (e.key.toLowerCase() === "o") {
      Objectives.toggle(); // collapse/expand the objectives tracker
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
  // Coin-op — "insert coin to continue" (MVP)
  //
  // Fetches /api/coinop/config on init; if enabled, reveals the continue
  // button in the death overlay and, on click, redirects to Stripe Checkout
  // with a success URL back to /play. On page load, if the URL carries
  // ?coinop=success&cs=<checkout_session_id>, we POST /api/coinop/redeem
  // (server-side verifies the payment with Stripe, then internally invokes
  // engine.api_revive) and clear the death overlay so the run continues.
  //
  // Fully dark-shippable: without server-side config, /api/coinop/config
  // returns {enabled:false} and none of this code changes any UI.
  // ------------------------------------------------------------------

  const CoinOp = (function () {
    let cfg = { enabled: false };
    // Comp code = the "free-play token" a tester or influencer was given.
    // We accept it from ?comp=<code> once, persist it in sessionStorage
    // for the tab's lifetime (so it survives the death overlay → redeem
    // → new run cycle without needing to keep it in the URL), and strip
    // it from the URL immediately for tidiness + screen-recording privacy.
    let compCode = null;

    // Run-scoped continue counter shown in the corner HUD. Separate
    // concept from the credit meter (credit balance is server-owned;
    // continuesUsed is a purely client-side lifetime revive count).
    let continuesUsed = 0;

    // Last-known snapshot from /api/coinop/balance. Used by the meter
    // (paint) and by pausePrompt() to decide when to auto-dismiss the
    // pause overlay after a top-up. Missing/zeroed shape until the
    // first successful fetchBalance() lands.
    let bal = { balance: 0, spent_cents: 0, gating_enabled: false };
    // Poll timer for the meter — used only in realtime sessions where
    // "turn boundaries" don't cleanly correspond to a client action
    // (the danger system can spend credits via server-side hooks in
    // the future). Cheap: one HEAD-ish call every N seconds when the
    // meter is visible.
    let balancePollTimer = null;
    // Pause-overlay countdown state (mirrors the death overlay's
    // countdownTimer/countdownValue but kept separate so the two can
    // coexist without stomping on each other in weird timing edge cases).
    let pauseCountdownTimer = null;
    let pauseCountdownValue = 10;
    // Guard so a second 402 while the pause overlay is opening doesn't
    // stack multiple "insert coin" prompts.
    let pauseOpen = false;

    // CONTINUE? countdown state. Purely visual urgency — we never actually
    // block the button on countdown expiry, since the arcade cabinet's
    // "10 seconds to insert coin" pattern is nostalgic but user-hostile
    // when applied to a real payment flow.
    const COUNTDOWN_START = 10;
    let countdownTimer = null;
    let countdownValue = COUNTDOWN_START;

    function readCompFromUrlOrStorage() {
      try {
        const q = new URLSearchParams(location.search);
        const fromUrl = (q.get("comp") || "").trim();
        if (fromUrl) {
          try { sessionStorage.setItem("coinop_comp_code", fromUrl); } catch (_) {}
          // Strip it from the URL so refreshes/screenshots don't leak the code.
          try {
            q.delete("comp");
            const rest = q.toString();
            const clean = location.pathname + (rest ? `?${rest}` : "") + location.hash;
            history.replaceState(null, "", clean);
          } catch (_) {}
          return fromUrl;
        }
        try {
          const fromStore = sessionStorage.getItem("coinop_comp_code");
          if (fromStore) return fromStore.trim();
        } catch (_) {}
      } catch (_) {}
      return null;
    }

    function setStatus(msg, isError) {
      if (!el.deathContinueStatus) return;
      if (!msg) {
        el.deathContinueStatus.classList.add("hidden");
        el.deathContinueStatus.textContent = "";
        return;
      }
      el.deathContinueStatus.textContent = msg;
      el.deathContinueStatus.classList.remove("hidden");
      el.deathContinueStatus.classList.toggle("error", !!isError);
    }

    async function fetchConfig() {
      try {
        const url = compCode
          ? `/api/coinop/config?comp=${encodeURIComponent(compCode)}`
          : "/api/coinop/config";
        const resp = await fetch(url, { method: "GET" });
        if (!resp.ok) return { enabled: false };
        return await resp.json();
      } catch (_) {
        return { enabled: false };
      }
    }

    function paintButton() {
      if (!el.deathContinue) return;
      // The wrapping .coinop-block owns visibility now — the button itself
      // stays laid out inside so the coin-drop animation has a stable
      // relative-positioned parent.
      if (!cfg.enabled) {
        if (el.coinopBlock) el.coinopBlock.classList.add("hidden");
        return;
      }
      if (el.coinopBlock) el.coinopBlock.classList.remove("hidden");
      const labelEl = el.deathContinue.querySelector(".continue-label");
      const slotEl = el.deathContinue.querySelector(".coin-slot");
      const compActive = !!(cfg.comp && cfg.comp.active);
      if (compActive) {
        // Free-play mode: relabel so the tester / influencer sees the same
        // coin-op ceremony without any dollar figure — and so anyone
        // watching a screen recording knows this isn't a real charge. The
        // button keeps its shape/animation for demo footage.
        el.deathContinue.classList.add("comp");
        if (labelEl) labelEl.textContent = cfg.comp.label || "Free Continue";
        if (slotEl) slotEl.textContent = "\u26A1"; // lightning bolt
        if (el.deathContinuePrice) {
          el.deathContinuePrice.textContent = (cfg.comp.remaining != null)
            ? `(${cfg.comp.remaining} left)`
            : "";
        }
      } else {
        el.deathContinue.classList.remove("comp");
        if (labelEl && cfg.label) labelEl.textContent = cfg.label;
        if (slotEl) slotEl.textContent = "\u25C9"; // filled circle (coin)
        if (el.deathContinuePrice) {
          el.deathContinuePrice.textContent = cfg.display_price ? `(${cfg.display_price})` : "";
        }
      }
    }

    // ── CONTINUE? countdown ────────────────────────────────────────────
    //
    // Fires when the death overlay opens (see enterGameOver → startCountdown
    // hook below). Ticks once per second, pulses the number, turns red
    // below 4s. On zero we DON'T disable the button — we just soften the
    // number and stop pulsing, so the arcade urgency lands without ever
    // actually blocking a paying player. Any click, keypress, or restart
    // cancels the countdown early via stopCountdown().

    function startCountdown() {
      stopCountdown();
      if (!cfg.enabled || !el.coinopCountdown) return;
      countdownValue = COUNTDOWN_START;
      el.coinopCountdown.textContent = String(countdownValue);
      el.coinopCountdown.classList.remove("urgent", "finished");
      countdownTimer = setInterval(() => {
        countdownValue -= 1;
        if (countdownValue < 0) {
          stopCountdown(/* finished */ true);
          return;
        }
        el.coinopCountdown.textContent = String(countdownValue);
        // A brief scale pop each tick so the number feels alive.
        el.coinopCountdown.classList.remove("tick");
        void el.coinopCountdown.offsetWidth; // reflow so animation restarts
        el.coinopCountdown.classList.add("tick");
        if (countdownValue <= 3) {
          el.coinopCountdown.classList.add("urgent");
          try { Sound.status(); } catch (_) {} // faint tick sound in the red zone
        }
      }, 1000);
    }

    function stopCountdown(finished) {
      if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
      if (!el.coinopCountdown) return;
      if (finished) {
        el.coinopCountdown.textContent = "\u2014";
        el.coinopCountdown.classList.add("finished");
        el.coinopCountdown.classList.remove("urgent", "tick");
      } else {
        el.coinopCountdown.classList.remove("tick");
      }
    }

    // ── Coin-drop micro-animation ──────────────────────────────────────
    //
    // Adds .dropping to the button for the duration of the CSS keyframes
    // (see #death-continue.dropping .coin-drop in standalone.css). Returns
    // a promise so callers can await the physical moment before doing the
    // network hop — that way the redirect never beats the animation.

    function playCoinDrop() {
      return new Promise((resolve) => {
        if (!el.deathContinue) return resolve();
        el.deathContinue.classList.remove("dropping");
        void el.deathContinue.offsetWidth; // reflow so anim restarts on rapid clicks
        el.deathContinue.classList.add("dropping");
        try { Sound.coin(); } catch (_) {}
        try { Haptics.strong(); } catch (_) {}
        setTimeout(() => {
          el.deathContinue.classList.remove("dropping");
          resolve();
        }, 460); // matches the 0.44s keyframe + a hair of slack
      });
    }

    // ── Return ceremony ────────────────────────────────────────────────
    //
    // Right before we dismiss the death overlay on a successful revive:
    // flash a phosphor-green "CONTINUE" over the overlay for ~700ms. This
    // gives the return moment weight — the run doesn't just "un-die," it
    // reboots.

    function playReturnCeremony() {
      return new Promise((resolve) => {
        if (!el.coinopCeremony) return resolve();
        el.coinopCeremony.classList.remove("playing", "hidden");
        void el.coinopCeremony.offsetWidth;
        el.coinopCeremony.classList.add("playing");
        try { Sound.coinReady(); } catch (_) {}
        try { Sound.glitch(); } catch (_) {}
        setTimeout(() => {
          el.coinopCeremony.classList.remove("playing");
          el.coinopCeremony.classList.add("hidden");
          resolve();
        }, 720);
      });
    }

    // ── Continues-used HUD ─────────────────────────────────────────────
    //
    // A small "CREDITS: N" chip in the corner that fades in the first
    // time a continue lands and stays put for the rest of the run. Ticks
    // (glowing pulse) on each increment so the counter feels like a
    // physical cabinet indicator.

    function bumpContinuesHud() {
      continuesUsed += 1;
      if (!el.continuesHud || !el.continuesHudCount) return;
      el.continuesHudCount.textContent = String(continuesUsed);
      el.continuesHud.classList.remove("hidden");
      el.continuesHud.classList.remove("tick");
      void el.continuesHud.offsetWidth;
      el.continuesHud.classList.add("tick");
    }

    function resetContinuesHud() {
      continuesUsed = 0;
      if (!el.continuesHud || !el.continuesHudCount) return;
      el.continuesHudCount.textContent = "0";
      el.continuesHud.classList.add("hidden");
      el.continuesHud.classList.remove("tick");
    }

    async function startCheckout() {
      if (!cfg.enabled) return;
      // The click has been received — kill the urgency countdown; the
      // player is committing to the flow either way now.
      stopCountdown();
      const compActive = !!(cfg.comp && cfg.comp.active);
      // Play the coin-drop animation FIRST, then kick off the network
      // call in parallel with its remaining frames — perceived latency
      // becomes "how long the coin takes to fall," not "how long the API
      // takes to answer." Same feel as an arcade cabinet's mechanical
      // ingest.
      el.deathContinue.classList.add("busy");
      const coinAnim = playCoinDrop();
      setStatus(compActive ? "Coin registered…" : "Opening Stripe Checkout…", false);
      try {
        const [_, resp] = await Promise.all([
          coinAnim,
          fetch("/api/coinop/checkout", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Session-Id": SESSION_ID,
            },
            body: JSON.stringify({
              session_id: SESSION_ID,
              comp: compCode || undefined,
            }),
          }),
        ]);
        if (!resp.ok) {
          throw new Error(`checkout HTTP ${resp.status}`);
        }
        const data = await resp.json();

        // COMP path: no Stripe session was created — the server returned a
        // comp voucher instead. Skip the redirect entirely and go straight
        // to redeem, so the whole flow completes in one round trip without
        // ever leaving the game.
        if (data.comp && data.checkout_session_id) {
          await redeem(data.checkout_session_id, /* isComp */ true);
          return;
        }

        // Paid path: hand off to Stripe Checkout via a top-window redirect
        // (works even when the game is embedded in an iframe on the main
        // site, since Stripe refuses to render inside a third-party frame).
        // A quick VCR glitch cue makes the navigation feel intentional
        // rather than jarring — same primitive used for scene transitions.
        if (!data.url) throw new Error("no checkout url returned");
        try { Sound.glitch(); } catch (_) {}
        try { glitchTransition && glitchTransition(280); } catch (_) {}
        try {
          window.top.location.href = data.url;
        } catch (_) {
          window.location.href = data.url;
        }
      } catch (err) {
        console.error("[coinop] checkout failed:", err);
        setStatus("Could not open checkout. Try again in a moment.", true);
        el.deathContinue.classList.remove("busy");
      }
    }

    async function redeem(checkoutSessionId, isComp) {
      try {
        const resp = await fetch("/api/coinop/redeem", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Session-Id": SESSION_ID,
          },
          body: JSON.stringify({
            session_id: SESSION_ID,
            checkout_session_id: checkoutSessionId,
          }),
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) {
          const reason = (data && data.reason) || `HTTP ${resp.status}`;
          setStatus(
            isComp
              ? `Comp failed: ${reason}.`
              : `Continue failed: ${reason}. Contact support if you were charged.`,
            true,
          );
          el.deathContinue.classList.remove("busy");
          return false;
        }
        setStatus("");
        el.deathContinue.classList.remove("busy");
        stopCountdown();
        // Bump the run HUD BEFORE the ceremony so the counter is already
        // ticking in the corner as the phosphor flash lifts.
        bumpContinuesHud();
        // Return ceremony: a phosphor "CONTINUE" flash over the death
        // overlay, then dismiss + resume. Awaiting the ceremony means the
        // overlay stays put for its ~700ms; the revive feels earned, not
        // like the modal blinked out. In realtime mode the reactor's
        // resume + DangerSystem.reset happen inside exitGameOverAndResume.
        try { await playReturnCeremony(); } catch (_) {}
        try { exitGameOverAndResume(); } catch (_) {}
        try { if (typeof pollOnce === "function") pollOnce(); } catch (_) {}
        // Refresh config so the comp counter in the button reflects the
        // new "remaining" figure for this tester.
        try {
          cfg = await fetchConfig();
          paintButton();
          paintPauseButton();
        } catch (_) {}
        // The revive endpoint also grants a fresh pack of credits — pull
        // the new balance so the arcade meter chip glows on the way up.
        try { await refreshBalance(); } catch (_) {}
        return true;
      } catch (err) {
        console.error("[coinop] redeem failed:", err);
        setStatus(
          isComp
            ? "Could not apply comp — the server rejected the voucher."
            : "Could not verify payment. Contact support if you were charged.",
          true,
        );
        el.deathContinue.classList.remove("busy");
        return false;
      }
    }

    async function handleReturnIfPresent() {
      let q;
      try { q = new URLSearchParams(location.search); } catch (_) { return; }
      const status = q.get("coinop");
      if (!status) return;
      const csId = q.get("cs") || "";

      // Clean the URL immediately so a refresh doesn't re-trigger anything
      // and so a copy/paste of the return URL doesn't leak the session id.
      try {
        q.delete("coinop");
        q.delete("cs");
        const rest = q.toString();
        const clean = location.pathname + (rest ? `?${rest}` : "") + location.hash;
        history.replaceState(null, "", clean);
      } catch (_) {}

      if (status === "cancel") {
        setStatus("Checkout cancelled. You can still restart.", false);
        return;
      }
      if (status !== "success" || !csId) return;

      if (!cfg.enabled) cfg = await fetchConfig();
      if (!cfg.enabled) {
        setStatus("Continue is not available on this server.", true);
        return;
      }
      setStatus("Verifying payment…", false);
      await redeem(csId, /* isComp */ false);
    }

    // Public helper used by the C keyboard shortcut and any other caller
    // that wants to "click" the continue button without a real DOM click
    // (e.g. onboarding tour, e2e test, remote-play). Routes to whichever
    // overlay is currently active — pause takes precedence over death
    // (a dead-and-broke player who just paid a top-up on the pause
    // overlay would be unnervingly odd, so pause never coexists with
    // gameOver in practice, but the ordering here matches user intent
    // in the edge case).
    function insertCoin() {
      if (!cfg.enabled) return;
      if (pauseOpen && el.pauseContinue) {
        if (el.pauseContinue.classList.contains("busy")) return;
        pauseCheckout();
        return;
      }
      if (!el.deathContinue) return;
      if (el.deathContinue.classList.contains("busy")) return;
      if (el.coinopBlock && el.coinopBlock.classList.contains("hidden")) return;
      startCheckout();
    }

    // ==================================================================
    // ARCADE CREDIT METER (always-visible HUD chip)
    // ==================================================================
    //
    // A live readout of the session's remaining credits + total spent.
    // Reads from /api/coinop/balance, which is server-authoritative — the
    // client never mutates the balance directly. Refresh cadence:
    //
    //   * Once at init(), so the HUD lands before the first turn.
    //   * After every successful /api/choose (see onTurnCompleted()).
    //   * After every successful redeem (see redeem() → refresh block).
    //   * A slow background poll (10s) in realtime sessions as a safety
    //     net — the server can theoretically spend credits via other
    //     future paths (auto-turn, danger-driven costs) that don't
    //     round-trip through /api/choose.
    //
    // Silent no-op when gating is off: fetchBalance still runs (so the
    // client can log dev telemetry), but paintMeter leaves the chip
    // .hidden.

    async function fetchBalance() {
      try {
        const url = `/api/coinop/balance?session_id=${encodeURIComponent(SESSION_ID)}`;
        const resp = await fetch(url, { headers: { "X-Session-Id": SESSION_ID } });
        if (!resp.ok) return null;
        return await resp.json();
      } catch (_) {
        return null;
      }
    }

    function paintMeter() {
      if (!el.creditMeter || !el.creditMeterCount) return;
      if (!bal || !bal.gating_enabled) {
        el.creditMeter.classList.add("hidden");
        try { document.body.classList.remove("credit-meter-on"); } catch (_) {}
        return;
      }
      el.creditMeter.classList.remove("hidden");
      try { document.body.classList.add("credit-meter-on"); } catch (_) {}
      const n = Math.max(0, Number(bal.balance || 0));
      el.creditMeterCount.textContent = String(n);
      if (el.creditMeterSpent) {
        const cents = Number(bal.spent_cents || 0);
        // $1.98 for 198 cents; $0 stays "$0.00" for consistent width.
        el.creditMeterSpent.textContent = `$${(cents / 100).toFixed(2)}`;
      }
      el.creditMeter.classList.toggle("low", n > 0 && n <= 2);
      el.creditMeter.classList.toggle("empty", n === 0);
    }

    function flashMeterTick() {
      if (!el.creditMeter) return;
      el.creditMeter.classList.remove("tick");
      void el.creditMeter.offsetWidth;
      el.creditMeter.classList.add("tick");
    }

    function flashMeterRefilled() {
      if (!el.creditMeter) return;
      el.creditMeter.classList.remove("refilled");
      void el.creditMeter.offsetWidth;
      el.creditMeter.classList.add("refilled");
    }

    async function refreshBalance(opts) {
      const next = await fetchBalance();
      if (!next) return null;
      const wasBalance = Number(bal.balance || 0);
      bal = next;
      paintMeter();
      // Visual feedback: a tick when the count went DOWN, a green
      // refilled flash when it went UP. If nothing changed (or this is
      // the very first paint after config), stay quiet.
      if (opts && opts.silent) return next;
      if (typeof wasBalance === "number" && Number(next.balance) < wasBalance) flashMeterTick();
      if (typeof wasBalance === "number" && Number(next.balance) > wasBalance) flashMeterRefilled();
      return next;
    }

    function startBalancePolling() {
      stopBalancePolling();
      // Only run the safety poll in realtime mode — image mode is
      // strictly turn-driven, so /api/choose is a perfect signal.
      if (!bal || !bal.gating_enabled) return;
      if (Renderer && Renderer.mode !== "reactor") return;
      balancePollTimer = setInterval(() => {
        // If the pause overlay is up we're already polling on its own
        // cadence; skip the background poll to avoid a double-fetch.
        if (pauseOpen) return;
        refreshBalance({ silent: true }).catch(() => {});
      }, 10000);
    }

    function stopBalancePolling() {
      if (balancePollTimer) { clearInterval(balancePollTimer); balancePollTimer = null; }
    }

    // ==================================================================
    // OUT-OF-CREDITS PAUSE OVERLAY
    // ==================================================================
    //
    // Symmetrical with the death overlay — same coin-op button, same
    // C-to-continue keyboard shortcut, same coin-drop / return
    // ceremony — but framed as "INSERT COIN TO KEEP PLAYING" instead
    // of "YOU DIED". Fires when /api/choose returns 402 with
    // {needs_coin: true}, and dismisses itself the moment a successful
    // top-up lands. The world freezes visually behind it via
    // body.coinop-paused (see standalone.css).

    function paintPauseButton() {
      if (!el.pauseContinue) return;
      const labelEl = el.pauseContinue.querySelector(".continue-label");
      const slotEl = el.pauseContinue.querySelector(".coin-slot");
      const compActive = !!(cfg.comp && cfg.comp.active);
      const pack = Number(cfg.credits_per_coin || 20);
      if (compActive) {
        el.pauseContinue.classList.add("comp");
        if (labelEl) labelEl.textContent = cfg.comp.label || `Free · +${pack} credits`;
        if (slotEl) slotEl.textContent = "\u26A1";
        if (el.pauseContinuePrice) {
          el.pauseContinuePrice.textContent = (cfg.comp.remaining != null)
            ? `(${cfg.comp.remaining} left)`
            : "";
        }
      } else {
        el.pauseContinue.classList.remove("comp");
        if (labelEl) labelEl.textContent = `Insert Coin · +${pack} credits`;
        if (slotEl) slotEl.textContent = "\u25C9";
        if (el.pauseContinuePrice) {
          el.pauseContinuePrice.textContent = cfg.display_price ? `(${cfg.display_price})` : "";
        }
      }
      if (el.pausePackBlurb) {
        el.pausePackBlurb.textContent = compActive
          ? `${pack} credits · comp, on the house.`
          : `${pack} credits per coin.`;
      }
    }

    function setPauseStatus(msg, isError) {
      if (!el.pauseContinueStatus) return;
      if (!msg) {
        el.pauseContinueStatus.classList.add("hidden");
        el.pauseContinueStatus.textContent = "";
        return;
      }
      el.pauseContinueStatus.textContent = msg;
      el.pauseContinueStatus.classList.remove("hidden");
      el.pauseContinueStatus.classList.toggle("error", !!isError);
    }

    function startPauseCountdown() {
      stopPauseCountdown();
      if (!el.pauseCountdown) return;
      pauseCountdownValue = 10;
      el.pauseCountdown.textContent = String(pauseCountdownValue);
      el.pauseCountdown.classList.remove("urgent", "finished");
      pauseCountdownTimer = setInterval(() => {
        pauseCountdownValue -= 1;
        if (pauseCountdownValue < 0) { stopPauseCountdown(true); return; }
        el.pauseCountdown.textContent = String(pauseCountdownValue);
        el.pauseCountdown.classList.remove("tick");
        void el.pauseCountdown.offsetWidth;
        el.pauseCountdown.classList.add("tick");
        if (pauseCountdownValue <= 3) {
          el.pauseCountdown.classList.add("urgent");
          try { Sound.status(); } catch (_) {}
        }
      }, 1000);
    }

    function stopPauseCountdown(finished) {
      if (pauseCountdownTimer) { clearInterval(pauseCountdownTimer); pauseCountdownTimer = null; }
      if (el.pauseCountdown && finished) {
        el.pauseCountdown.classList.add("finished");
        el.pauseCountdown.textContent = "0";
      }
    }

    function playPauseCoinDrop() {
      return new Promise((resolve) => {
        if (!el.pauseContinue) return resolve();
        el.pauseContinue.classList.remove("dropping");
        void el.pauseContinue.offsetWidth;
        el.pauseContinue.classList.add("dropping");
        try { Sound.coin(); } catch (_) {}
        setTimeout(() => {
          el.pauseContinue.classList.remove("dropping");
          resolve();
        }, 440);
      });
    }

    function playPauseCeremony() {
      return new Promise((resolve) => {
        if (!el.pauseCeremony) return resolve();
        el.pauseCeremony.classList.remove("playing", "hidden");
        void el.pauseCeremony.offsetWidth;
        el.pauseCeremony.classList.add("playing");
        try { Sound.coinReady && Sound.coinReady(); } catch (_) {}
        try { Sound.glitch(); } catch (_) {}
        setTimeout(() => {
          el.pauseCeremony.classList.remove("playing");
          el.pauseCeremony.classList.add("hidden");
          resolve();
        }, 720);
      });
    }

    function openPauseOverlay(errBody) {
      if (!el.pauseOverlay) return;
      pauseOpen = true;
      paintPauseButton();
      el.pauseOverlay.classList.remove("hidden");
      el.pauseOverlay.setAttribute("aria-hidden", "false");
      document.body.classList.add("coinop-paused");
      startPauseCountdown();
      // Copy any server-provided message into the subtitle for context.
      if (errBody && errBody.message && el.pauseContinueStatus) {
        setPauseStatus(errBody.message, false);
      } else {
        setPauseStatus("");
      }
      if (el.pauseContinue) {
        setTimeout(() => { try { el.pauseContinue.focus({ preventScroll: true }); } catch (_) {} }, 60);
      }
    }

    function closePauseOverlay() {
      if (!el.pauseOverlay) return;
      pauseOpen = false;
      stopPauseCountdown();
      el.pauseOverlay.classList.add("hidden");
      el.pauseOverlay.setAttribute("aria-hidden", "true");
      document.body.classList.remove("coinop-paused");
      if (el.pauseContinue) el.pauseContinue.classList.remove("busy");
      setPauseStatus("");
    }

    // Public: open the "OUT OF COINS" overlay. Called from makeChoice()
    // when /api/choose returned 402 (see the catch-block near the top
    // of this file), and reachable via a direct click on the credit
    // meter chip (so a player can proactively top up before hitting 0).
    function pausePrompt(errBody) {
      if (!cfg.enabled) return;
      // Refresh the config in case the comp status flipped (e.g. code
      // exhausted) since init(). This determines the button copy.
      // Non-blocking: paint now, upgrade later if the config changes.
      openPauseOverlay(errBody || null);
      fetchConfig().then((c) => {
        cfg = c || cfg;
        paintPauseButton();
      }).catch(() => {});
      refreshBalance({ silent: true }).catch(() => {});
    }

    async function pauseCheckout() {
      if (!cfg.enabled || !el.pauseContinue) return;
      stopPauseCountdown();
      const compActive = !!(cfg.comp && cfg.comp.active);
      el.pauseContinue.classList.add("busy");
      const coinAnim = playPauseCoinDrop();
      setPauseStatus(compActive ? "Coin registered…" : "Opening Stripe Checkout…", false);
      try {
        const [_, resp] = await Promise.all([
          coinAnim,
          fetch("/api/coinop/checkout", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Session-Id": SESSION_ID,
            },
            body: JSON.stringify({
              session_id: SESSION_ID,
              comp: compCode || undefined,
            }),
          }),
        ]);
        if (!resp.ok) throw new Error(`checkout HTTP ${resp.status}`);
        const data = await resp.json();

        // COMP path: mint a comp voucher server-side, then redeem it
        // inline without ever leaving the page. Same shape as the
        // death overlay's comp path.
        if (data.comp && data.checkout_session_id) {
          await pauseRedeem(data.checkout_session_id, /* isComp */ true);
          return;
        }
        // Paid path: hand off to Stripe. On successful return the
        // top-level handleReturnIfPresent() → redeem() flow will
        // credit the account and pollOnce.
        if (!data.url) throw new Error("no checkout url returned");
        try { Sound.glitch(); } catch (_) {}
        try { glitchTransition && glitchTransition(280); } catch (_) {}
        try { window.top.location.href = data.url; }
        catch (_) { window.location.href = data.url; }
      } catch (err) {
        console.error("[coinop] pauseCheckout failed:", err);
        setPauseStatus("Could not open checkout. Try again in a moment.", true);
        el.pauseContinue.classList.remove("busy");
      }
    }

    async function pauseRedeem(checkoutSessionId, isComp) {
      try {
        const resp = await fetch("/api/coinop/redeem", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Session-Id": SESSION_ID,
          },
          body: JSON.stringify({
            session_id: SESSION_ID,
            checkout_session_id: checkoutSessionId,
          }),
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) {
          const reason = (data && data.reason) || `HTTP ${resp.status}`;
          setPauseStatus(
            isComp ? `Comp failed: ${reason}.`
                   : `Top-up failed: ${reason}. Contact support if you were charged.`,
            true,
          );
          el.pauseContinue.classList.remove("busy");
          return false;
        }
        setPauseStatus("");
        el.pauseContinue.classList.remove("busy");
        // Refresh balance so the meter chip glows green on the way up.
        await refreshBalance();
        // Return ceremony THEN dismiss — order matters so the phosphor
        // flash lifts over the pause overlay, not after it's gone.
        try { await playPauseCeremony(); } catch (_) {}
        closePauseOverlay();
        // Latest feed tick so any incidental narration lands promptly.
        try { if (typeof pollOnce === "function") pollOnce(); } catch (_) {}
        // Comp counter in the config may have decremented; refresh so a
        // second Insert-Coin on the same overlay reflects the new state.
        try { cfg = await fetchConfig() || cfg; paintPauseButton(); paintButton(); } catch (_) {}
        return true;
      } catch (err) {
        console.error("[coinop] pauseRedeem failed:", err);
        setPauseStatus(
          isComp ? "Could not apply comp — the server rejected the voucher."
                 : "Could not verify payment. Contact support if you were charged.",
          true,
        );
        el.pauseContinue.classList.remove("busy");
        return false;
      }
    }

    // ── Public hook: called from makeChoice() after a turn succeeds.
    // Refreshes the meter so the count reflects the server-authoritative
    // debit. Cheap: one GET, no side effects if gating is off.
    function onTurnCompleted() {
      if (!cfg.enabled) return;
      refreshBalance({ silent: false }).catch(() => {});
    }

    async function init() {
      compCode = readCompFromUrlOrStorage();
      cfg = await fetchConfig();
      paintButton();
      paintPauseButton();
      if (el.deathContinue) {
        el.deathContinue.addEventListener("click", startCheckout);
      }
      if (el.pauseContinue) {
        el.pauseContinue.addEventListener("click", pauseCheckout);
      }
      if (el.pauseRestart) {
        el.pauseRestart.addEventListener("click", () => {
          try { closePauseOverlay(); } catch (_) {}
          try { resetGame(); } catch (_) {}
        });
      }
      if (el.creditMeter) {
        // Clicking the meter proactively opens the pause overlay so a
        // player can top up before hitting zero.
        el.creditMeter.addEventListener("click", () => {
          if (!cfg.enabled) return;
          pausePrompt(null);
        });
      }
      // Initial balance paint — establishes the HUD chip if gating is on.
      await refreshBalance({ silent: true });
      startBalancePolling();
      // Handle the redirect back from Stripe (if that's how we got here).
      handleReturnIfPresent();
    }

    return {
      init,
      insertCoin,
      // Public balance hooks — see JSDoc on each.
      onTurnCompleted,
      pausePrompt,
      refreshBalance: () => refreshBalance({ silent: false }),
      isPaused: () => pauseOpen,
      // Lifecycle hooks called from enterGameOver / resetGame so the
      // countdown + HUD + button focus all follow the game's own state
      // machine without CoinOp having to observe it.
      onGameOverShown() {
        if (!cfg.enabled) return;
        startCountdown();
        // Autofocus so keyboard players can tap Enter/Space or C right
        // away — no mouse hunt required. Small delay so the overlay's
        // fade-in doesn't fight the focus scroll.
        if (el.deathContinue) {
          setTimeout(() => { try { el.deathContinue.focus({ preventScroll: true }); } catch (_) {} }, 60);
        }
      },
      onRunReset() {
        stopCountdown();
        stopPauseCountdown();
        closePauseOverlay();
        resetContinuesHud();
        if (el.deathContinue) el.deathContinue.classList.remove("busy");
        if (el.pauseContinue) el.pauseContinue.classList.remove("busy");
        setStatus("");
        setPauseStatus("");
        // Balance may have changed (fresh session) — repaint next tick.
        setTimeout(() => refreshBalance({ silent: true }).catch(() => {}), 200);
      },
      isEnabled() { return !!cfg.enabled; },
    };
  })();

  // ------------------------------------------------------------------
  // Presence — who else is on THIS run. Scoped to the session, so two tabs
  // only see each other when they're pointed at the same ?session= id; a
  // private run correctly reads as one person. Entirely decorative: every
  // call is best-effort and a failure is swallowed.
  // ------------------------------------------------------------------
  const Lobby = (function () {
    const HEARTBEAT_MS = 8000;
    let viewerId = null;
    let timer = null;
    let last = { count: 0, active_count: 0 };

    function id() {
      if (viewerId) return viewerId;
      try {
        viewerId = sessionStorage.getItem("somewhere.viewer_id");
        if (!viewerId) {
          viewerId = "v_" + Math.random().toString(36).slice(2, 12);
          sessionStorage.setItem("somewhere.viewer_id", viewerId);
        }
      } catch (_) {
        viewerId = "v_" + Math.random().toString(36).slice(2, 12);
      }
      return viewerId;
    }

    async function beat(active) {
      try {
        const snap = await postJSON("/api/lobby/heartbeat", { viewer_id: id(), active: !!active });
        if (snap && typeof snap.count === "number") { last = snap; render(); }
      } catch (_) {}
    }

    function render() {
      const node = el.lobbyCount;
      if (!node) return;
      // One person on their own run is the normal case and doesn't need a
      // headcount shouting at them — only show it once somebody else is here.
      if (!last.count || last.count < 2) { node.classList.add("hidden"); return; }
      node.classList.remove("hidden");
      node.textContent = last.active_count > 0
        ? `${last.count} watching \u00B7 ${last.active_count} steering`
        : `${last.count} watching`;
    }

    function markActive() { beat(true); }

    function init() {
      beat(false);
      timer = setInterval(() => beat(false), HEARTBEAT_MS);
      // sendBeacon survives the page teardown that a fetch would not, so a
      // closed tab drops out now instead of lingering for the TTL.
      const bail = () => {
        try {
          const blob = new Blob([JSON.stringify({ viewer_id: id(), session_id: SESSION_ID })],
                                { type: "application/json" });
          navigator.sendBeacon("/api/lobby/leave", blob);
        } catch (_) {}
      };
      window.addEventListener("pagehide", bail);
      window.addEventListener("beforeunload", bail);
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") beat(false);
      });
    }

    return { init, markActive, get: () => last };
  })();
  window.Lobby = Lobby;

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
    // If we're returning from Stripe (?coinop=success), skip the automatic
    // reset — a reset would blow away the run the player just paid to
    // continue. CoinOp.init() will handle the redeem+revive itself.
    let returningFromCoinop = false;
    try {
      const q = new URLSearchParams(location.search);
      returningFromCoinop = q.get("coinop") === "success";
    } catch (_) {}
    if (!returningFromCoinop) {
      await resetGame();
    }
  }

  // ------------------------------------------------------------------
  // Init
  // ------------------------------------------------------------------

  function init() {
    // Resolve mobile vs desktop first so every later init (and the CSS) can
    // adapt: phones get a "truly mobile" layout + media fit, desktops keep the
    // cinematic full-bleed experience.
    Device.init();
    el.btnReset.addEventListener("click", resetGame);
    el.btnVhs.addEventListener("click", toggleVhs);
    el.btnSnd.addEventListener("click", toggleSound);
    if (el.rendererBtn) {
      el.rendererBtn.addEventListener("click", () => { Renderer.toggle(); Sound.toggle(); });
    }
    if (el.menuToggle) el.menuToggle.addEventListener("click", () => Menu.toggle());
    if (el.btnModel) el.btnModel.addEventListener("click", () => { RtLog.toggle(); });
    if (el.btnImgModel) el.btnImgModel.addEventListener("click", () => { ImageModel.toggle(); });
    if (el.btnStory) el.btnStory.addEventListener("click", () => { StoryLog.toggle(); });
    if (el.btnObjectives) el.btnObjectives.addEventListener("click", () => { Objectives.toggle(); });
    if (el.btnEditor) el.btnEditor.addEventListener("click", () => { WorldEditor.toggle(); });
    if (el.objHead) el.objHead.addEventListener("click", (ev) => {
      // The header is the collapse handle, but let the ✕/▾ button own its click.
      if (el.objCollapse && el.objCollapse.contains(ev.target)) return;
      Objectives.toggle();
    });
    if (el.objCollapse) el.objCollapse.addEventListener("click", (ev) => { ev.stopPropagation(); Objectives.toggle(); });
    if (el.rtModelAdd) el.rtModelAdd.addEventListener("submit", addCustomModel);
    StoryLog.init();
    Lobby.init();
    WorldEditor.init();
    ImageModel.init();
    Menu.init();
    Tactile.init();
    el.deathRestart.addEventListener("click", resetGame);
    CoinOp.init();
    if (el.caseRestart) el.caseRestart.addEventListener("click", resetGame);
    if (el.caseContinue) el.caseContinue.addEventListener("click", hideCaseWin);
    el.freeWillBtn.addEventListener("click", openFreeWill);
    if (el.realtimeBtn) el.realtimeBtn.addEventListener("click", openTouch);
    if (el.scanBtn) el.scanBtn.addEventListener("click", () => triggerScan());
    // First-run tutorial: dismiss on the button OR a tap anywhere on the card.
    if (el.tutDismiss) el.tutDismiss.addEventListener("click", (e) => { e.stopPropagation(); dismissScanTutorial(); });
    if (el.scanTutorial) el.scanTutorial.addEventListener("click", () => dismissScanTutorial());
    if (el.campBtn) el.campBtn.addEventListener("click", () => openCamp());
    if (el.leaveCampBtn) el.leaveCampBtn.addEventListener("click", () => leaveCamp());
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
    // Hotspots are non-modal: the overlay doesn't capture the pointer (so
    // choices and controls stay live). We watch pointer moves to highlight the
    // nearest interaction possibility, and a click on the scene taps the world
    // to scan it (see onWorldTap). Click (not pointerdown) keeps it a real
    // "tap" — it won't fire mid-drag on the joystick or a swipe.
    window.addEventListener("pointermove", onScanMove);
    window.addEventListener("click", onWorldTap);
    // Reflow overlays on resize AND orientation change (portrait ⇄ landscape):
    // the scan dots and, if the camera is up, its target markers must re-map to
    // the newly-shaped scene rect so nothing drifts when the phone is rotated.
    window.addEventListener("resize", () => {
      repositionScanTags();
      if (state.touchMode === "aim") layoutPhotoTargets();
    });
    el.forwardBtn.addEventListener("click", moveForward);
    el.tapeBtn.addEventListener("click", openTape);
    el.tapePlayPause.addEventListener("click", toggleTapePlay);
    el.tapePrev.addEventListener("click", () => tapeStep(-1));
    el.tapeNext.addEventListener("click", () => tapeStep(1));
    el.tapeEject.addEventListener("click", closeTape);
    el.autoplayBtn.addEventListener("click", toggleAutoPlay);
    el.customForm.addEventListener("submit", submitCustomAction);
    // TALK overlay wiring.
    if (el.talkForm) {
      el.talkForm.addEventListener("submit", (e) => { e.preventDefault(); Talk.send(el.talkInput.value); });
    }
    if (el.talkClose) el.talkClose.addEventListener("click", () => Talk.close());
    // The scrim closes the (non-cinematic) companion overlay when you click
    // away from it. In a cinematic Moment the scrim's job is the OPPOSITE —
    // it blocks the world underneath so a stray tap (e.g. on the portrait)
    // can't poke the paused scene; hanging up is reserved for the explicit
    // ✕ / Esc so you don't lose a conversation by clicking the character.
    if (el.talkScrim) {
      el.talkScrim.addEventListener("click", () => {
        if (Talk.isCinematic && Talk.isCinematic()) return;
        Talk.close();
      });
    }
    if (el.talkModeToggle) el.talkModeToggle.addEventListener("click", () => Talk.micToggle());
    if (el.talkVoiceBtn) el.talkVoiceBtn.addEventListener("click", (e) => { e.stopPropagation(); Talk.toggleVoiceMenu(); });
    // Click anywhere else in the panel closes the voice menu.
    if (el.talkPanel) el.talkPanel.addEventListener("click", (e) => {
      if (el.talkVoiceMenu && !el.talkVoiceMenu.contains(e.target) &&
          el.talkVoiceBtn && !el.talkVoiceBtn.contains(e.target)) Talk.closeVoiceMenu();
    });
    if (el.narratorBtn) el.narratorBtn.addEventListener("click", toggleNarrator);
    if (el.narratorStop) el.narratorStop.addEventListener("click", () => Narrator.stop());
    if (el.agentDebugBtn) el.agentDebugBtn.addEventListener("click", () => AgentLog.toggle());
    AgentLog.init();
    Movement.init();
    // Independent of the joystick element: mouse look and the CONTROLS switch
    // must come up even on layouts without a #move-pad.
    MouseLook.init();
    InputProfileUi.init();
    Movement.refreshHints();
    VerbBar.init();
    HappyOysterOptions.init();
    // Learn which camera the game was authored with before the first scene
    // lands, so the world is BUILT with it rather than corrected afterwards.
    Camera.load();
    document.addEventListener("keydown", onKeydown);
    // Release joystick directions on keyup so held W/A/S/D/Q/E/arrows stop the
    // moment the key lifts (movement is a "hold to travel" control). Shift ends
    // a held Sprint.
    document.addEventListener("keyup", (e) => {
      if (e.key === "Shift") { try { VerbBar.onShift(false); } catch (_) {} }
      if (!Movement.enabled()) return;
      const mk = Movement.keyFor(e.key);
      if (mk) Movement.releaseKey(mk);
    });

    // Browsers block audio until a user gesture; unlock the context on the
    // first interaction so feedback sounds work for the rest of the session.
    // We also remember that audio is now unlocked so the narrator can auto-play
    // (e.g. a cold open on a new run) without a broken/silent autoplay attempt.
    const unlockAudio = () => {
      Sound.resume();
      state.audioUnlocked = true;
      // Fire a cold open that was deferred because audio wasn't unlocked yet,
      // so the first playthrough gets narration rather than only the second run.
      try { Narrator.onAudioUnlocked(); } catch (_) {}
    };
    document.addEventListener("pointerdown", unlockAudio, { once: true });
    document.addEventListener("keydown", unlockAudio, { once: true });
    // Learn whether narrator VOICE is available so the control can say so.
    Narrator.preflight();

    Renderer.init(); // async: resolves default renderer + warms realtime session
    try { Investigations.render(); } catch (_) {} // show any persisted case file
    initVhsGrain();
    initKeyboardInset();
    startTimecode();
    startPolling();
    // Ask for a text-only simulation step whenever the player is idle in front
    // of a live world model, so the world keeps evolving between choices. The
    // loop self-gates (see WorldDrift.idle) and the server owns the pacing.
    WorldDrift.start();
    startStatusPolling();
    refreshStatus();

    // Danger-system dev affordances, all off by default:
    //   ?danger_demo=1  — auto-runs the safe/warning/hurting/safe sequence
    //                     shortly after boot, so QA / stakeholders can see
    //                     the full polish stack without hunting for a scene
    //                     that trips the vision rubric. (Same as Shift+D.)
    //   ?danger_debug=1 — mounts a small on-screen readout showing the live
    //                     danger mode, last level, last reason, and current
    //                     health. Invaluable when tuning the rubric.
    try {
      const q = new URLSearchParams(location.search);
      if (q.get("danger_demo") === "1") {
        // Small delay so the reactor has a moment to warm; the demo will
        // still fire on stills if the reactor is unavailable.
        setTimeout(() => { try { DangerSystem.demo(); } catch (_) {} }, 2600);
      }
      if (q.get("danger_debug") === "1") mountDangerDebugHud();
    } catch (_) {}

    // Resume an in-progress run if one exists, otherwise auto-start a fresh
    // game so a first-time visitor is never greeted by a blank screen.
    bootstrap();
  }

  // Debug HUD for the danger system. Off by default; enable with
  // `?danger_debug=1`. Polls DangerSystem.getState() a few times per second
  // and renders MODE / LEVEL / REASON / HP in a small bottom-right pill so
  // you can watch the vision loop's readings in real time. Purposefully
  // ugly + prefixed with __ so nobody mistakes it for a shipped HUD.
  function mountDangerDebugHud() {
    if (document.getElementById("__danger-debug-hud")) return;
    const hud = document.createElement("div");
    hud.id = "__danger-debug-hud";
    hud.style.cssText = [
      "position:fixed", "right:12px", "bottom:12px", "z-index:9999",
      "font-family:ui-monospace,Menlo,Consolas,monospace", "font-size:11px",
      "line-height:1.45", "color:#fff",
      "background:rgba(20,4,4,0.86)",
      "border:1px solid rgba(255,80,80,0.55)",
      "border-radius:4px", "padding:6px 10px",
      "box-shadow:0 4px 18px rgba(0,0,0,0.55)",
      "pointer-events:none",
      "min-width:180px", "letter-spacing:0.02em",
    ].join(";");
    hud.innerHTML =
      "<div style='opacity:0.75;font-size:9px;letter-spacing:0.16em'>DANGER DEBUG</div>" +
      "<div id='__ddb-mode'>mode: safe</div>" +
      "<div id='__ddb-lvl'>lvl:  0</div>" +
      "<div id='__ddb-hp'>hp:   100</div>" +
      "<div id='__ddb-reason' style='opacity:0.7'>—</div>";
    document.body.appendChild(hud);
    setInterval(() => {
      try {
        const s = DangerSystem.getState();
        const m = document.getElementById("__ddb-mode");
        const l = document.getElementById("__ddb-lvl");
        const h = document.getElementById("__ddb-hp");
        const r = document.getElementById("__ddb-reason");
        if (m) m.textContent = "mode: " + s.mode + (s.dead ? " [DEAD]" : "");
        if (l) l.textContent = "lvl:  " + s.level + (s.direction ? " (" + s.direction + ")" : "");
        if (h) h.textContent = "hp:   " + Math.round(s.health);
        if (r) r.textContent = (s.reason || "—").slice(0, 40);
      } catch (_) {}
    }, 250);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
