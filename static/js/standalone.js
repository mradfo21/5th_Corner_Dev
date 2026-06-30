/* ============================================================
   SOMEWHERE // Standalone — game controller
   Talks to: POST /api/reset, POST /api/choose, POST /api/regenerate_choices,
             GET /api/feed?since_id=N, GET /api/status
   ============================================================ */
(function () {
  "use strict";

  const POLL_INTERVAL_MS = 1500;
  const STATUS_INTERVAL_MS = 4000;
  const FAST_POLL_INTERVAL_MS = 800; // used right after a choice, while waiting for the turn to resolve
  const FAST_POLL_TIMEOUT_MS = 45000; // give up waiting fast after this long and fall back to normal polling

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
    prose: document.getElementById("prose-feed"),
    choices: document.getElementById("choices-container"),
    customForm: document.getElementById("custom-form"),
    customInput: document.getElementById("custom-input"),
    veil: document.getElementById("processing-veil"),
    veilMessage: document.getElementById("veil-message"),
    hudTurn: document.getElementById("hud-turn"),
    hudPhase: document.getElementById("hud-phase"),
    hudChaos: document.getElementById("hud-chaos"),
    btnReset: document.getElementById("btn-reset"),
    btnRegen: document.getElementById("btn-regen"),
    btnVhs: document.getElementById("btn-vhs"),
    vhsOverlay: document.getElementById("vhs-overlay"),
    backendName: document.getElementById("backend-name"),
    timecodeText: document.getElementById("timecode-text"),
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
  };

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

  /**
   * Normalize the response of POST /api/regenerate_choices, which may come
   * back as a bare list of feed items OR (in older/alternate server builds)
   * as a single object. Always returns the most relevant
   * `player_choice_prompt` item, or null.
   */
  function pickChoicePrompt(resp) {
    if (Array.isArray(resp)) {
      for (let i = resp.length - 1; i >= 0; i--) {
        if (resp[i] && resp[i].type === "player_choice_prompt") return resp[i];
      }
      return resp.length ? resp[resp.length - 1] : null;
    }
    return resp || null;
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
  };

  function classForType(type) {
    if (TYPE_CLASS[type]) return TYPE_CLASS[type];
    if (type && type.indexOf("combat") === 0) return "combat-event";
    return "narrative-event";
  }

  function appendProse(item) {
    const div = document.createElement("div");
    div.className = `prose-entry ${classForType(item.type)}`;
    div.dataset.itemId = item.id;
    div.textContent = item.content || "";
    el.prose.appendChild(div);
    el.prose.scrollTop = el.prose.scrollHeight + 200;
    return div;
  }

  function renderChoices(promptItem) {
    el.choices.innerHTML = "";
    if (!promptItem || !Array.isArray(promptItem.choices)) return;
    promptItem.choices.forEach((choice, idx) => {
      const btn = document.createElement("button");
      btn.className = "choice-btn";
      btn.innerHTML = `<span class="choice-num">${idx + 1}</span><span>${escapeHtml(choice.text)}</span>`;
      btn.addEventListener("click", () => makeChoice(choice.text, promptItem.id));
      el.choices.appendChild(btn);
    });
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
  }

  function renderItem(item) {
    if (!item || typeof item.id !== "number") return;
    if (item.id > state.lastId) state.lastId = item.id;

    if (item.image_url) {
      setScene(item.image_url);
    }

    if (item.type === "player_choice_prompt") {
      appendProse(item);
      renderChoices(item);
      hideVeil();
      state.awaitingResolution = false;
      return;
    }

    if (item.type === "error_event") {
      appendProse(item);
      hideVeil();
      state.awaitingResolution = false;
      return;
    }

    appendProse(item);
  }

  function renderItems(items) {
    (items || []).forEach(renderItem);
  }

  // ------------------------------------------------------------------
  // Game actions
  // ------------------------------------------------------------------

  async function resetGame() {
    try {
      showVeil("Initializing simulation...");
      el.prose.innerHTML = "";
      el.choices.innerHTML = "";
      state.lastId = 0;
      state.awaitingResolution = false;
      startTimecode();
      const items = await postJSON("/api/reset", {});
      renderItems(items);
      hideVeil();
      refreshStatus();
    } catch (err) {
      console.error("[standalone] resetGame failed:", err);
      hideVeil();
      appendProse({ id: -1, type: "error_event", content: `Could not reach the server: ${err.message}` });
    }
  }

  async function makeChoice(choiceText, contextItemId) {
    if (state.processing) return;
    el.choices.innerHTML = "";
    showVeil(INTERIM_MESSAGES[0]);
    state.awaitingResolution = true;
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

  async function regenChoices() {
    if (state.processing) return;
    showVeil("Reconsidering the options...");
    try {
      const resp = await postJSON("/api/regenerate_choices", {});
      const promptItem = pickChoicePrompt(resp);
      if (promptItem) {
        renderItem(promptItem);
      }
    } catch (err) {
      console.error("[standalone] regenChoices failed:", err);
      appendProse({ id: -1, type: "error_event", content: `Could not regenerate choices: ${err.message}` });
    } finally {
      hideVeil();
    }
  }

  function submitCustomAction(e) {
    e.preventDefault();
    const text = el.customInput.value.trim();
    if (!text || state.processing) return;
    el.customInput.value = "";
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

  async function refreshStatus() {
    try {
      const s = await getJSON("/api/status");
      el.hudTurn.textContent = s.turn ?? 0;
      el.hudPhase.textContent = s.phase ?? "normal";
      el.hudChaos.textContent = s.chaos ?? 0;
      el.backendName.textContent = s.backend ?? "unknown";
      if (s.alive === false) {
        el.hudPhase.textContent = "deceased";
      }
    } catch (err) {
      el.backendName.textContent = "offline";
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
  }

  function initVhsGrain() {
    const canvas = document.getElementById("vhs-grain");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    function resize() {
      canvas.width = window.innerWidth;
      canvas.height = window.innerHeight;
    }
    resize();
    window.addEventListener("resize", resize);

    function drawNoise() {
      const w = canvas.width, h = canvas.height;
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

  // ------------------------------------------------------------------
  // Keyboard shortcuts
  // ------------------------------------------------------------------

  function onKeydown(e) {
    if (document.activeElement === el.customInput) {
      if (e.key === "Escape") el.customInput.value = "";
      return;
    }
    if (e.key === "1" || e.key === "2" || e.key === "3") {
      const idx = Number(e.key) - 1;
      const btn = el.choices.children[idx];
      if (btn) btn.click();
    } else if (e.key.toLowerCase() === "r") {
      resetGame();
    } else if (e.key.toLowerCase() === "g") {
      regenChoices();
    } else if (e.key.toLowerCase() === "v") {
      toggleVhs();
    } else if (e.key === "Escape") {
      el.customInput.value = "";
    }
  }

  // ------------------------------------------------------------------
  // Init
  // ------------------------------------------------------------------

  function init() {
    el.btnReset.addEventListener("click", resetGame);
    el.btnRegen.addEventListener("click", regenChoices);
    el.btnVhs.addEventListener("click", toggleVhs);
    el.customForm.addEventListener("submit", submitCustomAction);
    document.addEventListener("keydown", onKeydown);

    initVhsGrain();
    cycleVeilMessages();
    startTimecode();
    startPolling();
    startStatusPolling();
    refreshStatus();

    // Does NOT auto-reset on load, per spec: pick up wherever the session
    // left off by polling from id 0 (returns the full existing feed_log).
    pollOnce();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
