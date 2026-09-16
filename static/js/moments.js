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
       setScene(url) / clearScene()   // full-bleed establishing shot (optional)
       notify({ text, icon? })
       setChoices(items, onPick, { custom }) / clearChoices()
       openCustomChoice() / closeCustomChoice(clear) / customChoiceOpen()
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
  function sceneEl() { return $("moment-scene"); }
  function sceneImg() { return $("moment-scene-img"); }
  function sceneHotspots() { return $("moment-scene-hotspots"); }
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

  // Soft cinematic fade (black veil). Used by CAMP — conversation keeps the
  // VCR glitch cut. Resolves after the CSS opacity transition settles.
  function fadeMs() { return prefersReducedMotion() ? 40 : 420; }
  function fadeEl() { return $("moment-fade"); }

  function fadeDown() {
    const f = fadeEl();
    if (!f) return Promise.resolve();
    f.classList.remove("hidden");
    void f.offsetWidth;
    f.classList.add("down");
    return new Promise((resolve) => setTimeout(resolve, fadeMs()));
  }

  function fadeUp() {
    const f = fadeEl();
    if (!f) return Promise.resolve();
    f.classList.remove("down");
    return new Promise((resolve) => {
      setTimeout(() => {
        if (!f.classList.contains("down")) f.classList.add("hidden");
        resolve();
      }, fadeMs());
    });
  }

  function playSound(name) {
    try {
      if (name === "choiceHover" && topType() === "encounter") name = "encounterChoiceHover";
      if (name === "choiceSelect" && topType() === "encounter") name = "encounterChoiceSelect";
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
    if (!ov) return false;
    ov.classList.remove("hidden");
    ov.setAttribute("aria-hidden", "false");
    document.body.classList.add("moment-active");
    document.body.classList.add("moment-" + (payload && payload.type ? payload.type : "generic"));
    // Force reflow then animate letterbox in.
    void ov.offsetWidth;
    ov.classList.add("moment-in");
    const subj = (payload && payload.subject) || {};
    const type = (payload && payload.type) || "";
    // Camp uses a nameplate of "CAMP"; conversation uses the subject label.
    if (nameplateName()) {
      nameplateName().textContent = type === "camp"
        ? "CAMP"
        : type === "encounter"
          ? (subj.label || "…")
          : type === "cutscene"
            ? (subj.label || subj.name || "CUTSCENE")
            : (subj.label || "—").toString();
    }
    if (nameplateSub()) {
      nameplateSub().textContent = type === "camp"
        ? "making camp…"
        : type === "encounter"
          ? "something is here"
          : type === "cutscene"
            ? (subj.sub || subj.mood || "montage")
            : "establishing…";
    }
    if (nameplate()) {
      nameplate().classList.remove("hidden");
      nameplate().classList.add("moment-nameplate-in");
    }
    // Conversation Moments use the portrait chrome; camp and encounter use the
    // full-bleed scene chrome. When nesting conversation ON TOP of camp, leave
    // the scene in place underneath — the portrait covers it while Talk is open.
    if (type === "camp" || type === "encounter" || type === "cutscene") {
      const sc = sceneEl();
      if (sc) {
        sc.classList.remove("hidden", "ready");
        sc.classList.add("developing");
        sc.setAttribute("aria-hidden", "false");
      }
      const simg = sceneImg();
      if (simg) simg.removeAttribute("src");
      // Hide idle portrait chrome so it doesn't cover the establishing shot.
      const p = portraitEl();
      if (p) {
        p.classList.remove("ready", "living", "animated", "developing");
        p.classList.add("hidden");
      }
    } else {
      const p = portraitEl();
      if (p) {
        p.classList.remove("hidden", "ready", "living", "animated");
        p.classList.add("developing");
      }
      // NOTE: the image is opacity-driven (see CSS), not display-toggled, so the
      // eventual reveal in setPortrait() can crossfade smoothly out of this
      // "developing" shimmer instead of popping in on a hard display:none swap.
      const img = portraitImg();
      if (img) img.removeAttribute("src");
      const vid = portraitVideo();
      if (vid) {
        try { vid.pause(); } catch (_) {}
        vid.removeAttribute("src");
        vid.classList.add("hidden");
      }
    }
    return true;
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
    clearScene();
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
    // `aborted` lets long-running enter() handlers (e.g. CAMP image gen) ignore
    // late results after the player Esc / LEAVE mid-load. choreographyBusy only
    // covers the sync chrome setup so pop() stays available during await.
    const entry = { type: String(type), payload: payload || {}, handlers, aborted: false };
    try {
      // If the shared chrome markup is missing (e.g. a stale cached template),
      // abort BEFORE touching the world so the caller can fall back to its own
      // non-cinematic presentation instead of a broken half-state.
      const chromeOk = showOverlayChrome({
        type: entry.type,
        subject: (payload && payload.subject) || payload,
      });
      if (!chromeOk) {
        console.warn("[moments] overlay markup missing — skipping cinematic chrome");
        return null;
      }
      // Camp keeps the underlay available so it can re-anchor onto a live
      // world-model campsite; conversation still freezes the mission world.
      const shouldPause = handlers.pauseUnderlay !== false;
      if (shouldPause) pauseUnderlay();
      setHudHidden(true);
      const transition = handlers.transition || "glitch";
      if (transition === "fade") {
        // Awaited below (after busy is released) so Esc still works mid-fade.
        entry._fadeEnter = true;
      } else if (transition === "develop") {
        // Encounter: photo-chemistry reveal, not Talk's VCR glitch.
      } else {
        fireGlitch();
      }
      const enterCue = handlers.enterSound || (transition === "develop" ? "encounterEnter" : "convoEnter");
      playSound(enterCue);
      stack.push(entry);
    } catch (err) {
      console.warn("[moments] push chrome failed:", err);
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

    try {
      if (entry._fadeEnter) await fadeDown();
      if (entry.aborted) return null;
      const result = await handlers.enter(payload || {}, entry);
      if (entry.aborted) return null;
      // Camp (fade) lifts the veil once the plate / world is ready; if enter
      // already faded up itself, this is a no-op (veil already hidden).
      if (entry._fadeEnter && !entry._fadedUp) await fadeUp();
      return result;
    } catch (err) {
      console.warn("[moments] enter failed:", err);
      // Roll back this push if enter blew up before the type settled.
      if (!entry.aborted && stack[stack.length - 1] === entry) {
        stack.pop();
        if (!stack.length) {
          hideOverlayChrome();
          setHudHidden(false);
          resumeUnderlay();
          fadeUp();
        }
      }
      return null;
    }
  }

  async function pop(result) {
    if (!stack.length || choreographyBusy) return null;
    choreographyBusy = true;
    const entry = stack[stack.length - 1];
    // Signal any in-flight enter() to ignore its late network/image result.
    entry.aborted = true;
    try {
      const exitCue = (entry.handlers && "exitSound" in entry.handlers)
        ? entry.handlers.exitSound
        : ((entry.handlers && entry.handlers.transition) === "develop" ? "encounterExit" : "convoExit");
      if (exitCue) playSound(exitCue);
      const transition = (entry.handlers && entry.handlers.transition) || "glitch";
      if (transition === "fade") {
        // Release busy before awaiting the fade so nested work can proceed.
        choreographyBusy = false;
        await fadeDown();
        choreographyBusy = true;
      } else if (transition !== "develop") {
        fireGlitch();
      }
      if (entry.handlers && typeof entry.handlers.exit === "function") {
        try { await entry.handlers.exit(result, entry); } catch (e) {
          console.warn("[moments] exit handler failed:", e);
        }
      }
      stack.pop();
      if (!stack.length) {
        // Fully leaving the Moments stack — tear down shared chrome + resume.
        hideOverlayChrome();
        setHudHidden(false);
        resumeUnderlay();
        if (transition === "fade") {
          choreographyBusy = false;
          await fadeUp();
          choreographyBusy = true;
        }
      } else {
        // Nested pop (e.g. conversation on top of camp): clear only the top
        // Moment's portrait/choices, keep letterbox + scene chrome for the
        // Moment underneath, then let it re-assert its nameplate/choices.
        clearPortrait();
        clearChoices();
        const p = portraitEl();
        if (p) {
          p.classList.remove("ready", "living", "animated", "developing");
          p.classList.add("hidden");
        }
        Array.from(document.body.classList).forEach((c) => {
          if (c.indexOf("moment-") === 0 && c !== "moment-active") {
            document.body.classList.remove(c);
          }
        });
        const below = stack[stack.length - 1];
        document.body.classList.add("moment-active");
        document.body.classList.add("moment-" + below.type);
        const ov = overlay();
        if (ov) {
          ov.classList.remove("hidden");
          ov.classList.add("moment-in");
          ov.setAttribute("aria-hidden", "false");
        }
        if (below.handlers && typeof below.handlers.resume === "function") {
          try { await below.handlers.resume(below); } catch (e) {
            console.warn("[moments] resume handler failed:", e);
          }
        }
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
      // Crossfade: the shimmer fades OUT (opacity transition on .developing
      // removal) at the same time the photo fades IN (opacity transition on
      // .ready) — a dissolve, not a hard pop. See the CSS transitions on
      // #moment-portrait-shimmer / #moment-portrait-img.
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
    if (img) img.removeAttribute("src");
    if (vid) {
      try { vid.pause(); } catch (_) {}
      try { vid.srcObject = null; } catch (_) {}
      vid.removeAttribute("src");
      vid.classList.add("hidden");
    }
    if (p) p.classList.remove("ready", "living", "developing", "animated");
  }

  // Full-bleed establishing shot (camp). Mirrors setPortrait's crossfade, but
  // fills the viewport instead of a framed close-up. Optional onReady fires
  // after the image decodes (or errors) so callers can place hotspots once
  // campSceneAsDataUrl() can actually sample pixels.
  function holdBlack() {
    const sc = sceneEl();
    const img = sceneImg();
    if (img) {
      img.onload = null;
      img.onerror = null;
      img.removeAttribute("src");
    }
    if (sc) {
      sc.classList.remove("hidden", "ready", "live-world");
      sc.classList.add("developing");
      sc.setAttribute("aria-hidden", "false");
    }
  }

  function setScene(url, onReady) {
    const sc = sceneEl();
    const img = sceneImg();
    if (!sc || !img || !url) return;
    sc.classList.remove("hidden", "live-world");
    sc.setAttribute("aria-hidden", "false");
    const done = () => {
      sc.classList.remove("developing");
      sc.classList.add("ready");
      if (typeof onReady === "function") {
        try { onReady(); } catch (_) {}
      }
    };
    img.onload = () => {
      done();
      playSound("portraitReveal");
    };
    img.onerror = () => { done(); };
    img.src = url;
  }

  function clearScene() {
    const sc = sceneEl();
    const img = sceneImg();
    const hs = sceneHotspots();
    if (img) img.removeAttribute("src");
    if (hs) hs.innerHTML = "";
    if (sc) {
      sc.classList.remove("ready", "developing", "live-world");
      sc.classList.add("hidden");
      sc.setAttribute("aria-hidden", "true");
    }
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

  // opts.custom (optional): { label, placeholder, maxLength, onSubmit } — adds
  // a final row that is not one of the authored choices but a way to write your
  // own action, and swaps itself for a prompt bar when taken. A Moment that
  // offers a slate is offering the only things the player may do, and for a
  // confrontation that is the wrong shape: the interesting move is usually the
  // one nobody wrote down. onSubmit(text) receives the typed line.
  function setChoices(items, onPick, opts) {
    const box = choicesEl();
    if (!box) return;
    box.innerHTML = "";
    customState = null;
    const list = Array.isArray(items) ? items : [];
    const custom = (opts && opts.custom) || null;
    if (!list.length && !custom) {
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
      const num = document.createElement("span");
      num.className = "moment-choice-num";
      num.textContent = String(idx + 1);
      const body = document.createElement("span");
      body.className = "moment-choice-text";
      body.textContent = label;
      btn.appendChild(num);
      btn.appendChild(body);
      btn.addEventListener("mouseenter", () => playSound("choiceHover"));
      btn.addEventListener("focus", () => playSound("choiceHover"));
      btn.addEventListener("click", () => {
        playSound("choiceSelect");
        if (typeof onPick === "function") onPick(item, idx);
      });
      box.appendChild(btn);
    });
    if (custom) buildCustomRow(box, custom);
  }

  // The typed-action row: a button until it is taken, then an inline prompt bar.
  // Both live in the choices box so the slate reads as one list.
  let customState = null;

  function buildCustomRow(box, custom) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "moment-choice moment-choice-custom";
    const num = document.createElement("span");
    num.className = "moment-choice-num";
    num.textContent = custom.key || "";
    const body = document.createElement("span");
    body.className = "moment-choice-text";
    body.textContent = custom.label || "Do something else";
    row.appendChild(num);
    row.appendChild(body);

    const form = document.createElement("form");
    form.className = "moment-custom-form hidden";
    form.setAttribute("autocomplete", "off");
    const caret = document.createElement("span");
    caret.className = "moment-custom-caret";
    caret.textContent = ">";
    const input = document.createElement("input");
    input.type = "text";
    input.className = "moment-custom-input";
    input.setAttribute("placeholder", custom.placeholder || "type what you do...");
    input.setAttribute("maxlength", String(custom.maxLength || 200));
    const send = document.createElement("button");
    send.type = "submit";
    send.className = "moment-custom-send";
    send.textContent = "DO IT";
    form.appendChild(caret);
    form.appendChild(input);
    form.appendChild(send);

    function open() {
      if (!customState) return;
      customState.open = true;
      row.classList.add("hidden");
      form.classList.remove("hidden");
      box.classList.add("has-open-custom");
      playSound("open");
      setTimeout(() => { try { input.focus(); } catch (_) {} }, 40);
    }

    function close(clear) {
      if (!customState) return;
      customState.open = false;
      form.classList.add("hidden");
      row.classList.remove("hidden");
      box.classList.remove("has-open-custom");
      if (clear) input.value = "";
      try { if (document.activeElement === input) input.blur(); } catch (_) {}
    }

    row.addEventListener("mouseenter", () => playSound("choiceHover"));
    row.addEventListener("click", () => { playSound("choiceSelect"); open(); });
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      playSound("choiceSelect");
      if (typeof custom.onSubmit === "function") custom.onSubmit(text);
    });

    box.appendChild(row);
    box.appendChild(form);
    customState = { open: false, openFn: open, closeFn: close, input };
  }

  // For key handlers: open / close the typed-action bar from outside, and ask
  // whether it currently owns the keyboard.
  function openCustomChoice() {
    if (!customState || customState.open) return false;
    customState.openFn();
    return true;
  }

  function closeCustomChoice(clear) {
    if (!customState || !customState.open) return false;
    customState.closeFn(clear);
    return true;
  }

  function customChoiceOpen() {
    return !!(customState && customState.open);
  }

  function clearChoices() {
    const box = choicesEl();
    if (!box) return;
    box.innerHTML = "";
    box.classList.add("hidden");
    box.classList.remove("has-open-custom");
    customState = null;
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

  // Mark the scene layer as a transparent hotspot shell over a live underlay
  // world-model (camp). The still is kept as a soft floor until the video is
  // up, then callers can call setSceneLive(true) to let the stream show through.
  function setSceneLive(live) {
    const sc = sceneEl();
    if (!sc) return;
    if (live) sc.classList.add("live-world");
    else sc.classList.remove("live-world");
  }

  async function revealFromFade(entry) {
    if (entry) entry._fadedUp = true;
    await fadeUp();
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
    setScene,
    holdBlack,
    clearScene,
    setSceneLive,
    setPortraitStream,
    notify,
    setChoices,
    clearChoices,
    openCustomChoice,
    closeCustomChoice,
    customChoiceOpen,
    onEscape,
    fadeDown,
    fadeUp,
    revealFromFade,
    // Exposed for tests / future Moment types that need shared chrome.
    _setHudHidden: setHudHidden,
  };
})();
