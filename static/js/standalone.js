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
  // A turn's narrative + choices resolve fast (~1-2s) but its scene image
  // streams in several seconds later. To keep text and imagery IN SYNC we hold
  // the turn's text/choices until the frame arrives, reveal them together, then
  // let auto-play dwell on that synced beat before advancing.
  const AUTOPLAY_DWELL_MS = 3000;        // read/watch beat after a synced reveal before auto-advancing
  const REVEAL_IMAGE_TIMEOUT_MS = 10000; // if the frame never arrives (slow/failed), reveal text anyway

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
    prose: document.getElementById("prose-feed"),
    choices: document.getElementById("choices-container"),
    customForm: document.getElementById("custom-form"),
    customInput: document.getElementById("custom-input"),
    freeWillBtn: document.getElementById("free-will-btn"),
    forwardBtn: document.getElementById("forward-btn"),
    actionWheel: document.getElementById("action-wheel"),
    veil: document.getElementById("processing-veil"),
    veilMessage: document.getElementById("veil-message"),
    hudTurn: document.getElementById("hud-turn"),
    hudPhase: document.getElementById("hud-phase"),
    hudChaos: document.getElementById("hud-chaos"),
    hudTime: document.getElementById("hud-time"),
    hudTimeWrap: document.getElementById("hud-time-wrap"),
    ejectBtn: document.getElementById("eject-btn"),
    btnVhs: document.getElementById("btn-vhs"),
    btnSnd: document.getElementById("btn-snd"),
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
    turnBuffer: [],            // this turn's text/choices, held until the frame renders (sync)
    revealTimer: null,         // fallback: reveal the buffer even if no frame arrives
    resumeAutoAfterAct: false, // ACT paused auto-play; resume it after the action
    lastPromptItem: null,      // last live choice prompt, so ACT can revert cleanly
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
    // A filtered noise burst — percussive/textural (clicks, hits, tape, static).
    function noise(dur, vol, filterType, freq, delay, q) {
      if (!state.soundEnabled) return;
      const c = ensure();
      if (!c) return;
      const t0 = c.currentTime + (delay || 0);
      const frames = Math.max(1, Math.floor(c.sampleRate * dur));
      const buf = c.createBuffer(1, frames, c.sampleRate);
      const data = buf.getChannelData(0);
      for (let i = 0; i < frames; i++) data[i] = Math.random() * 2 - 1;
      const src = c.createBufferSource();
      src.buffer = buf;
      const filt = c.createBiquadFilter();
      filt.type = filterType || "bandpass";
      filt.frequency.value = freq || 1200;
      filt.Q.value = q || 0.9;
      const gain = c.createGain();
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.exponentialRampToValueAtTime(vol || 0.05, t0 + 0.008);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
      src.connect(filt); filt.connect(gain); gain.connect(c.destination);
      src.start(t0);
      src.stop(t0 + dur + 0.02);
    }
    return {
      resume() { ensure(); },

      // ── ambient text / world beats ──────────────────────────────
      narrative() { tone(430, 0.09, "sine", 0.045); noise(0.05, 0.018, "highpass", 2600); }, // a dispatch lands
      text() { tone(430, 0.09, "sine", 0.045); },              // legacy alias
      act() { tone(300, 0.05, "sine", 0.05); noise(0.06, 0.05, "lowpass", 420); },           // you commit an action
      worldShift() { tone([250, 400], 0.26, "sine", 0.045); tone([520, 720], 0.22, "triangle", 0.022, 0.06); }, // world update swells in

      // ── world reactions: luck / danger / harm ───────────────────
      lucky() { tone([720, 1180], 0.14, "triangle", 0.05, 0.02); tone(1560, 0.12, "sine", 0.035, 0.12); },   // fortune breaks your way
      unlucky() { tone([260, 150], 0.34, "sawtooth", 0.06); noise(0.2, 0.03, "lowpass", 240, 0.02); },       // fate turns against you
      hurt() { noise(0.16, 0.09, "lowpass", 300); tone([200, 85], 0.22, "sawtooth", 0.06, 0.01); },          // you take a wound
      escalate() { tone([300, 620], 0.35, "sawtooth", 0.06); tone([620, 900], 0.3, "square", 0.03, 0.12); }, // phase escalates

      // ── items ────────────────────────────────────────────────────
      pickup() { tone([620, 940], 0.15, "triangle", 0.06); tone(1320, 0.1, "sine", 0.035, 0.1); }, // got an item
      packFull() { tone([300, 220], 0.13, "square", 0.05); tone([300, 200], 0.13, "square", 0.045, 0.12); }, // inventory full (denied)

      // ── choices / commit ────────────────────────────────────────
      choices() { tone(680, 0.07, "triangle", 0.05); tone(920, 0.09, "triangle", 0.045, 0.07); noise(0.05, 0.018, "highpass", 3200); }, // options ready
      select() { tone(520, 0.05, "square", 0.05); tone(790, 0.10, "square", 0.05, 0.055); },       // pick a numbered choice
      forward() { tone([420, 900], 0.16, "triangle", 0.055); noise(0.12, 0.03, "bandpass", 1400, 0.02); }, // "move forward" advance
      hover() { tone(1180, 0.028, "sine", 0.018); },           // button hover tick

      // ── HUD / status ─────────────────────────────────────────────
      status() { tone(320, 0.05, "sine", 0.03); },             // subtle tick

      // ── lifecycle ────────────────────────────────────────────────
      death() { tone([180, 60], 0.7, "sawtooth", 0.09); noise(0.6, 0.05, "lowpass", 200, 0.05); }, // game over
      error() { tone([200, 120], 0.18, "sawtooth", 0.05); noise(0.14, 0.03, "bandpass", 800); },
      scene() { noise(0.09, 0.04, "bandpass", 2200); tone(180, 0.05, "sine", 0.045); tone([520, 380], 0.14, "sine", 0.035, 0.03); }, // shutter/whir
      start() { tone([160, 520], 0.28, "sawtooth", 0.05); tone(880, 0.12, "triangle", 0.04, 0.18); noise(0.3, 0.03, "bandpass", 1600); }, // tape spins up

      // ── free-will gate ───────────────────────────────────────────
      submit() { tone(700, 0.05, "square", 0.05); tone(1050, 0.11, "square", 0.045, 0.05); }, // custom action sent
      open() { tone([420, 760], 0.14, "triangle", 0.05); },    // gate opens
      close() { tone([760, 380], 0.12, "triangle", 0.04); },   // gate closes

      // ── UI toggles ───────────────────────────────────────────────
      toggle() { tone(300, 0.04, "square", 0.04); noise(0.03, 0.02, "highpass", 4000); }, // generic click
      autoplayOn() { tone([360, 720], 0.16, "triangle", 0.05); tone(960, 0.1, "sine", 0.035, 0.1); }, // world starts advancing
      autoplayOff() { tone([720, 320], 0.18, "triangle", 0.045); }, // paused

      // ── tape transport ───────────────────────────────────────────
      tapeStep() { noise(0.05, 0.06, "bandpass", 1000, 0, 1.4); tone(260, 0.03, "square", 0.03); }, // mechanical chk
      eject() { tone([300, 120], 0.22, "sawtooth", 0.05); noise(0.18, 0.04, "lowpass", 500, 0.02); }, // clunk
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

  function showVeil(message) {
    state.processing = true;
    el.veilMessage.textContent = message || INTERIM_MESSAGES[0];
    el.veil.classList.remove("hidden");
  }

  function hideVeil() {
    state.processing = false;
    el.veil.classList.add("hidden");
  }

  function cycleVeilMessages() {
    let i = 0;
    return setInterval(() => {
      if (!state.processing) return;
      i = (i + 1) % INTERIM_MESSAGES.length;
      el.veilMessage.textContent = INTERIM_MESSAGES[i];
    }, 2200);
  }

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

  function setScene(imageUrl) {
    if (!imageUrl) return;
    const incoming = state.activeScene === "A" ? el.sceneB : el.sceneA;
    const outgoing = state.activeScene === "A" ? el.sceneA : el.sceneB;
    incoming.style.backgroundImage = `url('${imageUrl}')`;
    incoming.classList.add("scene-active");
    outgoing.classList.remove("scene-active");
    state.activeScene = state.activeScene === "A" ? "B" : "A";
    flashScene();
  }

  // Wipe both scene layers back to the pre-game backdrop (used on restart so
  // the previous run's frame doesn't linger behind the intro).
  function clearScene() {
    el.sceneA.style.backgroundImage = "";
    el.sceneB.style.backgroundImage = "";
    el.sceneB.classList.remove("scene-active");
    el.sceneA.classList.add("scene-active"); // sceneA is the base layer again
    state.activeScene = "A";
  }

  function flashScene() {
    if (!el.sceneFlash) return;
    el.sceneFlash.classList.remove("flash");
    // Force reflow so the animation can re-trigger on consecutive scene swaps.
    void el.sceneFlash.offsetWidth;
    el.sceneFlash.classList.add("flash");
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
    world_update: "world-update",
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

  // Keep the transmission log minimal: only the newest few dispatches stay on
  // screen. The latest is the focal line; older ones dim/clamp and the oldest
  // dissolve out — punchy, never a growing wall of prose.
  const MAX_PROSE_ENTRIES = 3;
  // Only the story beat is the focal line; the action echo and the short
  // world-update ride beneath it as accents so the narrative stays the star.
  const FOCAL_TYPES = new Set(["narrative_event", "consequence_event", "game_over"]);

  function appendProse(item) {
    const div = document.createElement("div");
    div.className = `prose-entry glow-pop ${classForType(item.type)}`;
    div.dataset.itemId = item.id;
    div.innerHTML = renderInline(item.content || "");

    // Promote story beats to the focal "latest"; demote the previous focal line.
    if (FOCAL_TYPES.has(item.type)) {
      const prevLatest = el.prose.querySelector(".prose-entry.latest");
      if (prevLatest) prevLatest.classList.remove("latest");
      div.classList.add("latest");
    }

    el.prose.appendChild(div);

    // Fade out anything beyond the cap so the feed stays tight.
    const entries = el.prose.querySelectorAll(".prose-entry:not(.fading)");
    if (entries.length > MAX_PROSE_ENTRIES) {
      for (let i = 0; i < entries.length - MAX_PROSE_ENTRIES; i++) {
        const old = entries[i];
        old.classList.add("fading");
        setTimeout(() => old.remove(), 480);
      }
    }
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
      btn.dataset.choiceText = choice.text; // used by "move forward" (random pick)
      btn.innerHTML = `<span class="choice-num">${idx + 1}</span><span>${renderInline(choice.text)}</span>`;
      btn.addEventListener("mouseenter", () => Sound.hover());
      btn.addEventListener("click", () => {
        if (state.processing || state.gameOver) return;
        Sound.select(); // deliberate numbered pick
        commitChoice(btn, choice.text, promptItem.id);
      });
      el.choices.appendChild(btn);
    });
  }

  // Commit a choice (flash + send). The caller owns the sound so a deliberate
  // pick and a "move forward" advance can feel different.
  function commitChoice(btn, text, promptId) {
    if (state.processing || state.gameOver) return;
    if (btn) btn.classList.add("picked");
    makeChoice(text, promptId);
  }

  function enterGameOver(message) {
    state.gameOver = true;
    state.awaitingResolution = false;
    hideVeil();
    el.choices.innerHTML = "";
    if (message) el.deathMessage.innerHTML = renderInline(message);
    el.deathOverlay.classList.remove("hidden");
  }

  function exitGameOver() {
    state.gameOver = false;
    el.deathOverlay.classList.add("hidden");
  }

  // Reveal everything held for the in-flight turn. Called when the turn's scene
  // frame arrives (so text + imagery appear together) or, as a safety net, when
  // the frame is too slow / failed to arrive.
  function flushTurnBuffer() {
    clearTimeout(state.revealTimer);
    const buffered = state.turnBuffer;
    state.turnBuffer = [];
    buffered.forEach(renderResolved);
    hideVeil();
    state.awaitingResolution = false;
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

    // The scene frame is the SYNC POINT for a turn: swap it in and immediately
    // reveal the turn's buffered narrative + choices, so text never appears
    // ahead of (or behind) the imagery it describes. NOTE: only scene_image
    // sets the scene — choice prompts carry a STALE image_url (the previous
    // turn's frame) that would otherwise flip the backdrop backwards.
    if (item.type === "scene_image") {
      if (item.image_url) setScene(item.image_url);
      Sound.scene();
      flushTurnBuffer();
      return;
    }

    // The player's own action echoes back instantly (it's their input, not a
    // world-state beat), so it never waits on the frame.
    if (item.type === "player_action") {
      renderResolved(item);
      return;
    }

    // Terminal states must never be trapped behind a pending frame: reveal
    // anything buffered first, then handle them.
    if (item.type === "error_event" || item.type === "game_over") {
      flushTurnBuffer();
      renderResolved(item);
      return;
    }

    // Mid-turn: hold narrative / choices / inventory until the frame arrives.
    if (state.awaitingResolution) {
      state.turnBuffer.push(item);
      return;
    }
    renderResolved(item);
  }

  // Actually paint a resolved feed item (buffering decisions happen upstream in
  // renderItem / flushTurnBuffer).
  function renderResolved(item) {
    switch (item.type) {
      case "scene_image":
        if (item.image_url) setScene(item.image_url);
        Sound.scene();
        return;

      case "game_over":
        appendProse(item);
        Sound.death();
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
        // The choices themselves are the prompt — don't dump the generic
        // "What do you do next?" line into the log (it was pure clutter).
        renderChoices(item);
        Sound.choices();
        hideVeil();
        state.awaitingResolution = false;
        // New decision point: these are now the live/latest choices. Auto-play
        // will advance against THIS prompt (and only once), after a readable
        // dwell — the frame and choices are already in sync at this moment.
        state.currentPromptId = item.id;
        state.lastPromptItem = item; // cached so ACT can revert to this decision point
        refreshStatus(); // reflect turn/chaos/inventory promptly, not on the 4s tick
        scheduleAutoAdvance(AUTOPLAY_DWELL_MS);
        return;

      case "error_event":
        appendProse(item);
        Sound.error();
        hideVeil();
        state.awaitingResolution = false;
        return;

      case "player_action":
        // The player's own move committing — a soft, satisfying thunk.
        appendProse(item);
        Sound.act();
        return;

      case "narrative_event":
      case "consequence_event": {
        // The world's reaction to the last move: the dispatch lands, then a
        // sting layered on top reflects HOW it went — a wound, a lucky break,
        // or fate turning against you. This is the punchiest per-turn feedback.
        appendProse(item);
        Sound.narrative();
        const meta = item.metadata || {};
        if (meta.injured) Sound.hurt();
        else if (meta.fate === "LUCKY") Sound.lucky();
        else if (meta.fate === "UNLUCKY") Sound.unlucky();
        return;
      }

      case "inventory_pickup":
        appendProse(item);
        Sound.pickup();
        refreshStatus(); // update the inventory HUD right away
        return;

      case "inventory_full":
        appendProse(item);
        Sound.packFull(); // distinct "denied" buzz
        refreshStatus();
        return;

      case "world_update":
        // Short, punchy atmospheric reaction (like the Discord world updates).
        appendProse(item);
        Sound.worldShift();
        return;

      default:
        // Any other world-building text.
        appendProse(item);
        Sound.text();
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

  // EJECT — one elegant action that tears everything down and reloads a fresh
  // run: stop the world, cancel any in-flight generation, wipe the frame + the
  // text feed, and stream in a brand-new intro. Safe to call at any point
  // (mid-turn, mid-auto-play, on the death screen).
  async function resetGame() {
    try {
      stopPolling();          // avoid a mid-reset poll racing the rebuilt feed
      setAutoPlay(false);     // stop the world advancing
      state.resumeAutoAfterAct = false;
      closeFreeWill(true);
      exitGameOver();
      Sound.eject();          // tape ejects...
      Sound.start();          // ...and a fresh one spins up
      showVeil("Reawakening the tape...");

      // Cancel anything still generating so it can't land on the fresh feed.
      clearTimeout(state.autoTimer);
      clearTimeout(state.revealTimer);
      state.turnBuffer = [];
      state.awaitingResolution = false;

      // Wipe the visuals + text back to a clean slate.
      clearScene();
      el.prose.innerHTML = "";
      el.choices.innerHTML = "";
      renderInventory([]);

      // Reset all run state.
      state.lastId = 0;
      state.renderedIds = new Set();
      state.gameOver = false;
      state.currentPromptId = null;
      state.lastAdvancedPromptId = null;
      state.lastPromptItem = null;
      state.lastStatus = {};

      startTimecode();
      // /api/reset also bumps the server's turn token, superseding in-flight work.
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

  // Stop whatever the world is currently generating: tell the server to drop
  // the in-flight turn, clear client timers/veil, and revert to the last real
  // decision point so the player can act cleanly (no orphaned half-turn).
  function cancelInFlight() {
    postJSON("/api/cancel", {}).catch(() => {}); // supersede the server-side turn
    clearTimeout(state.revealTimer);
    clearTimeout(state.autoTimer);
    state.turnBuffer = [];
    state.awaitingResolution = false;
    hideVeil();
    // Drop the orphaned "» ..." echo from the auto-advance we just cancelled.
    const echoes = el.prose.querySelectorAll(".prose-entry.player-action");
    const lastEcho = echoes[echoes.length - 1];
    if (lastEcho) lastEcho.remove();
    // Revert to the last live choices so the decision point is on screen again
    // and auto-play (if resumed) can advance from here.
    if (state.lastPromptItem) {
      renderChoices(state.lastPromptItem);
      state.currentPromptId = state.lastPromptItem.id;
      state.lastAdvancedPromptId = null; // allow advancing from this prompt again
    }
  }

  async function makeChoice(choiceText, contextItemId) {
    if (state.processing || state.gameOver) return;
    closeFreeWill(true); // picking any action closes the free-will gate
    // If ACT paused auto-play to let us intervene, resume it after this action.
    if (state.resumeAutoAfterAct) {
      state.resumeAutoAfterAct = false;
      setAutoPlay(true);
    }
    el.choices.innerHTML = "";
    showVeil(INTERIM_MESSAGES[0]);
    // Start a fresh turn: hold its text/choices until the frame renders so
    // they reveal in sync. The fallback reveals them even if the frame is
    // slow or fails, so the game never freezes waiting on an image.
    state.turnBuffer = [];
    state.awaitingResolution = true;
    clearTimeout(state.revealTimer);
    state.revealTimer = setTimeout(flushTurnBuffer, REVEAL_IMAGE_TIMEOUT_MS);
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
    if (state.gameOver || state.freeWillOpen) return;
    // ACT is an interruption: pause auto-play and cancel any in-flight
    // generation so your own action lands cleanly. Remember whether to resume
    // auto-play once you've acted (or bailed).
    state.resumeAutoAfterAct = state.autoPlay;
    if (state.autoPlay) setAutoPlay(false);
    if (state.processing || state.awaitingResolution) cancelInFlight();
    state.freeWillOpen = true;
    el.actionWheel.classList.add("fw-open");
    Sound.open();
    // Focus after the expand animation starts so the caret lands cleanly.
    setTimeout(() => el.customInput.focus(), 60);
  }

  // Dismiss the gate without acting (Esc / cancel). If ACT had paused
  // auto-play, resume it — the decision point is back on screen.
  function dismissFreeWill() {
    const wasOpen = state.freeWillOpen;
    if (wasOpen) Sound.close();
    closeFreeWill(true);
    if (state.resumeAutoAfterAct) {
      state.resumeAutoAfterAct = false;
      setAutoPlay(true);
    }
  }

  function closeFreeWill(clear) {
    if (!state.freeWillOpen) return;
    state.freeWillOpen = false;
    el.actionWheel.classList.remove("fw-open");
    if (clear) el.customInput.value = "";
    if (document.activeElement === el.customInput) el.customInput.blur();
    if (el.actionWheel) el.actionWheel.style.bottom = ""; // drop any keyboard offset
  }

  // "Move forward" — commit to one of the generated actions at random. Uses a
  // distinct "advance" whoosh instead of the numbered-pick sound so it feels
  // like the world surging forward.
  function moveForward() {
    if (state.processing || state.gameOver || state.freeWillOpen) return;
    const btns = Array.from(el.choices.children);
    if (!btns.length) return;
    const pick = btns[Math.floor(Math.random() * btns.length)];
    Sound.forward();
    commitChoice(pick, pick.dataset.choiceText || "", state.currentPromptId);
  }

  // Pressing FORWARD starts the world advancing on its own: it flips auto-play
  // on (if not already) and takes the first step now. From there the world
  // keeps advancing until the player pauses (ACT) or stops auto-play.
  function forwardPressed() {
    if (state.gameOver) return;
    if (!state.autoPlay) { Sound.autoplayOn(); setAutoPlay(true); }
    moveForward();
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
    }, delay == null ? AUTOPLAY_DWELL_MS : delay);
  }

  function setAutoPlay(on) {
    state.autoPlay = on;
    el.autoplayBtn.classList.toggle("on", on);
    el.autoplayLabel.textContent = on ? "STOP" : "AUTO-PLAY";
    el.autoplayBtn.title = on ? "Stop auto-play (P)" : "Auto-play — advance on its own (P)";
    if (on) scheduleAutoAdvance(AUTOPLAY_DWELL_MS); // start advancing after a dwell
    else clearTimeout(state.autoTimer);  // pause
  }

  function toggleAutoPlay() {
    const next = !state.autoPlay;
    if (next) Sound.autoplayOn(); else Sound.autoplayOff();
    setAutoPlay(next);
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
    Sound.tapeStep(); // mechanical frame-advance chk
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
    Sound.eject(); // clunk on eject
  }

  function toggleSound() {
    state.soundEnabled = !state.soundEnabled;
    el.btnSnd.classList.toggle("off", !state.soundEnabled);
    el.btnSnd.innerHTML = state.soundEnabled ? "\u266A SND" : "\u2715 SND";
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
      if (e.key === "Escape") dismissFreeWill(); // Esc closes the gate (resumes auto if it was on)
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
    } else if (e.key === "ArrowUp" || e.key === " " || e.key === "Spacebar") {
      e.preventDefault();
      forwardPressed(); // forward also starts the world auto-advancing
    } else if (e.key === "Escape") {
      dismissFreeWill();
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
    el.ejectBtn.addEventListener("click", resetGame);
    el.btnVhs.addEventListener("click", toggleVhs);
    el.btnSnd.addEventListener("click", toggleSound);
    el.deathRestart.addEventListener("click", resetGame);
    el.freeWillBtn.addEventListener("click", openFreeWill);
    el.forwardBtn.addEventListener("click", forwardPressed);
    el.tapeBtn.addEventListener("click", openTape);
    el.tapePlayPause.addEventListener("click", toggleTapePlay);
    el.tapePrev.addEventListener("click", () => tapeStep(-1));
    el.tapeNext.addEventListener("click", () => tapeStep(1));
    el.tapeEject.addEventListener("click", closeTape);
    el.autoplayBtn.addEventListener("click", toggleAutoPlay);
    el.customForm.addEventListener("submit", submitCustomAction);
    document.addEventListener("keydown", onKeydown);

    // A subtle hover tick on every control makes the whole UI feel tactile.
    [
      el.ejectBtn, el.btnVhs, el.btnSnd, el.deathRestart, el.freeWillBtn,
      el.forwardBtn, el.tapeBtn, el.autoplayBtn, el.tapePlayPause,
      el.tapePrev, el.tapeNext, el.tapeEject,
    ].forEach((b) => { if (b) b.addEventListener("mouseenter", () => Sound.hover()); });

    // Browsers block audio until a user gesture; unlock the context on the
    // first interaction so feedback sounds work for the rest of the session.
    const unlockAudio = () => { Sound.resume(); };
    document.addEventListener("pointerdown", unlockAudio, { once: true });
    document.addEventListener("keydown", unlockAudio, { once: true });

    initVhsGrain();
    initKeyboardInset();
    cycleVeilMessages();
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
