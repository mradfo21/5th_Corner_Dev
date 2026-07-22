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
       setScene(url) / clearScene()   // full-bleed establishing shot (camp)
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
        : (subj.label || "—").toString();
    }
    if (nameplateSub()) nameplateSub().textContent = type === "camp" ? "making camp…" : "establishing…";
    if (nameplate()) {
      nameplate().classList.remove("hidden");
      nameplate().classList.add("moment-nameplate-in");
    }
    // Conversation Moments use the portrait chrome; camp uses the full-bleed
    // scene chrome. When nesting conversation ON TOP of camp, leave the scene
    // in place underneath — the portrait covers it while Talk is open.
    if (type === "camp") {
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
      } else {
        fireGlitch();
      }
      playSound("convoEnter");
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
      playSound("convoExit");
      const transition = (entry.handlers && entry.handlers.transition) || "glitch";
      if (transition === "fade") {
        // Release busy before awaiting the fade so nested work can proceed.
        choreographyBusy = false;
        await fadeDown();
        choreographyBusy = true;
      } else {
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
  function setScene(url, onReady) {
    const sc = sceneEl();
    const img = sceneImg();
    if (!sc || !img || !url) return;
    sc.classList.remove("hidden");
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
    clearScene,
    setSceneLive,
    setPortraitStream,
    notify,
    setChoices,
    clearChoices,
    onEscape,
    fadeDown,
    fadeUp,
    revealFromFade,
    // Exposed for tests / future Moment types that need shared chrome.
    _setHudHidden: setHudHidden,
  };
})();
