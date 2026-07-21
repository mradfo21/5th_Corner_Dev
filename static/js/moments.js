/* ============================================================
   SOMEWHERE // Moments — reusable cinematic interaction stack

   A Moment is a full-screen set-piece layered on top of the live world
   (conversation, interrogation, flashback, trade…). Pushing a Moment:
     1. pauses the underlay world (does NOT tear it down)
     2. runs shared enter choreography (glitch + letterbox + HUD hide)
     3. hands off to the registered type's enter()/render()
   Popping reverses that and restores the exact scene the player left.

   Types register with Moments.register(type, { enter, exit, render?, onEsc? }).
   Conversation is the first type; future set-pieces plug into the same stack.

   window.Moments facade:
       register(type, handlers)
       push(type, payload) / pop(result?)
       isActive() / current() / topType()
       setPortrait(url) / clearPortrait()
       notify({ text, icon? })
       setChoices(items) / clearChoices()
   ============================================================ */
(function () {
  "use strict";

  const registry = Object.create(null);
  const stack = [];
  let choreographyBusy = false;

  const HUD_SELECTORS = [
    "#action-wheel",
    "#control-rail",
    "#verb-bar",
    "#move-pad",
    "#scan-layer",
    "#objectives-hud",
    "#evidence-hud",
    "#inventory-hud",
    "#menu-toggle",
    "#narrator-bar",
  ];

  function $(id) { return document.getElementById(id); }

  function overlay() { return $("moment-overlay"); }
  function portraitEl() { return $("moment-portrait"); }
  function portraitImg() { return $("moment-portrait-img"); }
  function portraitVideo() { return $("moment-portrait-video"); }
  function notifyTray() { return $("moment-notify"); }
  function choicesEl() { return $("moment-choices"); }
  function nameplate() { return $("moment-nameplate"); }
  function nameplateName() { return $("moment-nameplate-name"); }
  function nameplateSub() { return $("moment-nameplate-sub"); }

  function prefersReducedMotion() {
    try {
      return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    } catch (_) { return false; }
  }

  function setHudHidden(hidden) {
    for (const sel of HUD_SELECTORS) {
      const node = document.querySelector(sel);
      if (!node) continue;
      if (hidden) {
        if (!node.dataset.momentWasHidden) {
          node.dataset.momentWasHidden = node.classList.contains("hidden") ? "1" : "0";
        }
        node.classList.add("hidden");
        node.classList.add("moment-hud-hidden");
      } else {
        node.classList.remove("moment-hud-hidden");
        if (node.dataset.momentWasHidden === "0") node.classList.remove("hidden");
        delete node.dataset.momentWasHidden;
      }
    }
  }

  function fireGlitch() {
    try {
      if (typeof window.__MOMENT_GLITCH__ === "function") window.__MOMENT_GLITCH__();
    } catch (_) {}
  }

  function playSound(name) {
    try {
      const S = window.Sound || (window.__SOMEWHERE_SOUND__);
      if (S && typeof S[name] === "function") S[name]();
    } catch (_) {}
  }

  function pauseUnderlay() {
    try {
      if (window.Renderer && typeof window.Renderer.pauseUnderlay === "function") {
        window.Renderer.pauseUnderlay();
      }
    } catch (_) {}
  }

  function resumeUnderlay() {
    try {
      if (window.Renderer && typeof window.Renderer.resumeUnderlay === "function") {
        window.Renderer.resumeUnderlay();
      }
    } catch (_) {}
  }

  function showOverlayChrome(payload) {
    const ov = overlay();
    if (!ov) return;
    ov.classList.remove("hidden");
    ov.setAttribute("aria-hidden", "false");
    document.body.classList.add("moment-active");
    document.body.classList.add("moment-" + (payload && payload.type ? payload.type : "generic"));
    // Force reflow then animate letterbox in.
    void ov.offsetWidth;
    ov.classList.add("moment-in");
    const subj = (payload && payload.subject) || {};
    if (nameplateName()) nameplateName().textContent = (subj.label || "—").toString();
    if (nameplateSub()) nameplateSub().textContent = "establishing…";
    if (nameplate()) {
      nameplate().classList.remove("hidden");
      nameplate().classList.add("moment-nameplate-in");
    }
    const p = portraitEl();
    if (p) {
      p.classList.remove("ready", "living", "animated");
      p.classList.add("developing");
    }
    const img = portraitImg();
    if (img) { img.removeAttribute("src"); img.classList.add("hidden"); }
    const vid = portraitVideo();
    if (vid) {
      try { vid.pause(); } catch (_) {}
      vid.removeAttribute("src");
      vid.classList.add("hidden");
    }
  }

  function hideOverlayChrome() {
    const ov = overlay();
    if (!ov) return;
    ov.classList.remove("moment-in");
    document.body.classList.remove("moment-active");
    // Strip type class (moment-conversation, etc.)
    Array.from(document.body.classList).forEach((c) => {
      if (c.indexOf("moment-") === 0 && c !== "moment-active") document.body.classList.remove(c);
    });
    if (nameplate()) {
      nameplate().classList.remove("moment-nameplate-in");
      nameplate().classList.add("hidden");
    }
    clearPortrait();
    clearChoices();
    const tray = notifyTray();
    if (tray) tray.innerHTML = "";
    setTimeout(() => {
      if (!stack.length && ov) {
        ov.classList.add("hidden");
        ov.setAttribute("aria-hidden", "true");
      }
    }, prefersReducedMotion() ? 0 : 320);
  }

  function register(type, handlers) {
    if (!type || !handlers) return;
    registry[String(type)] = handlers;
  }

  async function push(type, payload) {
    const handlers = registry[type];
    if (!handlers || typeof handlers.enter !== "function") {
      console.warn("[moments] unknown type:", type);
      return null;
    }
    if (choreographyBusy) return null;
    choreographyBusy = true;
    const entry = { type: String(type), payload: payload || {}, handlers };
    try {
      pauseUnderlay();
      setHudHidden(true);
      fireGlitch();
      playSound("convoEnter");
      showOverlayChrome({ type: entry.type, subject: (payload && payload.subject) || payload });
      stack.push(entry);
      const result = await handlers.enter(payload || {}, entry);
      return result;
    } catch (err) {
      console.warn("[moments] enter failed:", err);
      // Roll back this push if enter blew up before the type settled.
      if (stack[stack.length - 1] === entry) stack.pop();
      if (!stack.length) {
        hideOverlayChrome();
        setHudHidden(false);
        resumeUnderlay();
      }
      return null;
    } finally {
      choreographyBusy = false;
    }
  }

  async function pop(result) {
    if (!stack.length || choreographyBusy) return null;
    choreographyBusy = true;
    const entry = stack[stack.length - 1];
    try {
      playSound("convoExit");
      fireGlitch();
      if (entry.handlers && typeof entry.handlers.exit === "function") {
        try { await entry.handlers.exit(result, entry); } catch (e) {
          console.warn("[moments] exit handler failed:", e);
        }
      }
      stack.pop();
      hideOverlayChrome();
      if (!stack.length) {
        setHudHidden(false);
        resumeUnderlay();
      } else {
        // A Moment remains underneath — keep HUD hidden / underlay paused.
        const below = stack[stack.length - 1];
        document.body.classList.add("moment-active");
        document.body.classList.add("moment-" + below.type);
      }
      return result;
    } finally {
      choreographyBusy = false;
    }
  }

  function isActive() { return stack.length > 0; }
  function current() { return stack.length ? stack[stack.length - 1] : null; }
  function topType() { const c = current(); return c ? c.type : null; }

  function setNameplate(name, sub) {
    if (nameplateName() && name != null) nameplateName().textContent = String(name);
    if (nameplateSub() && sub != null) nameplateSub().textContent = String(sub);
  }

  function setPortrait(url) {
    const p = portraitEl();
    const img = portraitImg();
    if (!p || !img || !url) return;
    img.onload = () => {
      img.classList.remove("hidden");
      p.classList.remove("developing");
      p.classList.add("ready", "living");
      playSound("portraitReveal");
    };
    img.onerror = () => {
      p.classList.remove("developing");
      p.classList.add("ready");
    };
    img.src = url;
  }

  function clearPortrait() {
    const p = portraitEl();
    const img = portraitImg();
    const vid = portraitVideo();
    if (img) { img.removeAttribute("src"); img.classList.add("hidden"); }
    if (vid) {
      try { vid.pause(); } catch (_) {}
      try { vid.srcObject = null; } catch (_) {}
      vid.removeAttribute("src");
      vid.classList.add("hidden");
    }
    if (p) p.classList.remove("ready", "living", "developing", "animated");
  }

  // Phase-2 scaffold: attach a live world-model video element over the still
  // portrait once a second Reactor session reports frames. Callers own the
  // stream lifecycle; Moments only handles the reveal crossfade.
  function setPortraitStream(mediaStream) {
    const p = portraitEl();
    const vid = portraitVideo();
    if (!p || !vid || !mediaStream) return;
    try {
      vid.srcObject = mediaStream;
      vid.classList.remove("hidden");
      const playP = vid.play();
      if (playP && typeof playP.catch === "function") playP.catch(() => {});
      p.classList.add("animated");
      p.classList.remove("developing");
    } catch (_) {}
  }

  function notify(opts) {
    const tray = notifyTray();
    if (!tray) return;
    const text = (opts && (opts.text || opts.message)) || "";
    if (!text) return;
    const chip = document.createElement("div");
    chip.className = "moment-notify-chip";
    if (opts && opts.icon) {
      const ic = document.createElement("span");
      ic.className = "moment-notify-icon";
      ic.textContent = opts.icon;
      chip.appendChild(ic);
    }
    const body = document.createElement("span");
    body.className = "moment-notify-text";
    body.textContent = text;
    chip.appendChild(body);
    tray.appendChild(chip);
    playSound("notify");
    requestAnimationFrame(() => chip.classList.add("in"));
    const dwell = Math.min(7000, 2800 + text.length * 35);
    setTimeout(() => {
      chip.classList.remove("in");
      chip.classList.add("out");
      setTimeout(() => { if (chip.parentNode) chip.parentNode.removeChild(chip); }, 320);
    }, dwell);
  }

  function setChoices(items, onPick) {
    const box = choicesEl();
    if (!box) return;
    box.innerHTML = "";
    const list = Array.isArray(items) ? items : [];
    if (!list.length) {
      box.classList.add("hidden");
      return;
    }
    box.classList.remove("hidden");
    list.forEach((item, idx) => {
      const label = typeof item === "string" ? item : (item && item.label) || "";
      if (!label) return;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "moment-choice";
      btn.textContent = label;
      btn.addEventListener("mouseenter", () => playSound("choiceHover"));
      btn.addEventListener("focus", () => playSound("choiceHover"));
      btn.addEventListener("click", () => {
        playSound("choiceSelect");
        if (typeof onPick === "function") onPick(item, idx);
      });
      box.appendChild(btn);
    });
  }

  function clearChoices() {
    const box = choicesEl();
    if (!box) return;
    box.innerHTML = "";
    box.classList.add("hidden");
  }

  function onEscape() {
    const entry = current();
    if (!entry) return false;
    if (entry.handlers && typeof entry.handlers.onEsc === "function") {
      return !!entry.handlers.onEsc(entry);
    }
    pop();
    return true;
  }

  window.Moments = {
    register,
    push,
    pop,
    isActive,
    current,
    topType,
    setNameplate,
    setPortrait,
    clearPortrait,
    setPortraitStream,
    notify,
    setChoices,
    clearChoices,
    onEscape,
    // Exposed for tests / future Moment types that need shared chrome.
    _setHudHidden: setHudHidden,
  };
})();
