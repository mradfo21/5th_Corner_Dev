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
    sceneFlash: document.getElementById("scene-flash"),
    prose: document.getElementById("prose-feed"),
    choices: document.getElementById("choices-container"),
    customForm: document.getElementById("custom-form"),
    customInput: document.getElementById("custom-input"),
    veil: document.getElementById("processing-veil"),
    veilMessage: document.getElementById("veil-message"),
    hudTurn: document.getElementById("hud-turn"),
    hudPhase: document.getElementById("hud-phase"),
    hudChaos: document.getElementById("hud-chaos"),
    hudTime: document.getElementById("hud-time"),
    hudTimeWrap: document.getElementById("hud-time-wrap"),
    btnReset: document.getElementById("btn-reset"),
    btnRegen: document.getElementById("btn-regen"),
    btnVhs: document.getElementById("btn-vhs"),
    vhsOverlay: document.getElementById("vhs-overlay"),
    backendName: document.getElementById("backend-name"),
    timecodeText: document.getElementById("timecode-text"),
    inventoryHud: document.getElementById("inventory-hud"),
    inventoryList: document.getElementById("inventory-list"),
    deathOverlay: document.getElementById("death-overlay"),
    deathMessage: document.getElementById("death-message"),
    deathRestart: document.getElementById("death-restart"),
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
    flashScene();
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

  function appendProse(item) {
    const div = document.createElement("div");
    div.className = `prose-entry ${classForType(item.type)}`;
    div.dataset.itemId = item.id;
    div.innerHTML = renderInline(item.content || "");
    el.prose.appendChild(div);
    el.prose.scrollTop = el.prose.scrollHeight + 200;
    return div;
  }

  function renderChoices(promptItem) {
    el.choices.innerHTML = "";
    if (state.gameOver) return; // death overlay owns the restart action
    if (!promptItem || !Array.isArray(promptItem.choices)) return;
    promptItem.choices.forEach((choice, idx) => {
      const btn = document.createElement("button");
      btn.className = "choice-btn";
      btn.innerHTML = `<span class="choice-num">${idx + 1}</span><span>${renderInline(choice.text)}</span>`;
      btn.addEventListener("click", () => makeChoice(choice.text, promptItem.id));
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
  }

  function exitGameOver() {
    state.gameOver = false;
    el.deathOverlay.classList.add("hidden");
  }

  function renderItem(item) {
    if (!item || typeof item.id !== "number") return;
    if (item.id > state.lastId) state.lastId = item.id;

    if (item.image_url) {
      setScene(item.image_url);
    }

    switch (item.type) {
      case "scene_image":
        // The image itself is the payload (handled above by setScene). Its
        // placeholder content ("The scene shifts...") is intentionally NOT
        // added to the prose feed — it would just be noise over the art.
        return;

      case "game_over":
        appendProse(item);
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
        hideVeil();
        state.awaitingResolution = false;
        refreshStatus(); // reflect turn/chaos/inventory promptly, not on the 4s tick
        return;

      case "error_event":
        appendProse(item);
        hideVeil();
        state.awaitingResolution = false;
        return;

      case "inventory_pickup":
      case "inventory_full":
        appendProse(item);
        refreshStatus(); // update the inventory HUD right away
        return;

      default:
        appendProse(item);
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
      exitGameOver();
      showVeil("Initializing simulation...");
      el.prose.innerHTML = "";
      el.choices.innerHTML = "";
      state.lastId = 0;
      state.awaitingResolution = false;
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
    }
  }

  async function makeChoice(choiceText, contextItemId) {
    if (state.processing || state.gameOver) return;
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
    if (state.processing || state.gameOver) return;
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
    if (!text || state.processing || state.gameOver) return;
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
      renderInventory(s.inventory);
      if (s.time_of_day) {
        el.hudTime.textContent = s.time_of_day;
        el.hudTimeWrap.classList.remove("hidden");
      } else {
        el.hudTimeWrap.classList.add("hidden");
      }
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

  // ------------------------------------------------------------------
  // Keyboard shortcuts
  // ------------------------------------------------------------------

  function onKeydown(e) {
    if (document.activeElement === el.customInput) {
      if (e.key === "Escape") el.customInput.value = "";
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
    } else if (e.key.toLowerCase() === "g") {
      regenChoices();
    } else if (e.key.toLowerCase() === "v") {
      toggleVhs();
    } else if (e.key === "Escape") {
      el.customInput.value = "";
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
    el.btnRegen.addEventListener("click", regenChoices);
    el.btnVhs.addEventListener("click", toggleVhs);
    el.deathRestart.addEventListener("click", resetGame);
    el.customForm.addEventListener("submit", submitCustomAction);
    document.addEventListener("keydown", onKeydown);

    initVhsGrain();
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
