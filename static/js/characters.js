/* CHARACTERS — the screen PLAY opens on: who you are, before where you go.
 *
 * Designed on the canvas "GOD — Characters" (2026-09-23), after three rounds
 * of Matt's notes: "they're not developers … pure visual, classic video game
 * character concepts", "more atmospheric, more cinematic, simple", and then
 * "this is cheesy … get back to the black background … smokey backdrop …
 * character framed on the right with controls on the left". The flow he
 * asked for: PLAY → this screen (select, or create) → create is one prompt →
 * back here with them selected → PLAY → the normal start (the World picker).
 *
 * The server owns the characters (characters.py, /api/characters*). This file
 * draws them, lets the player make one, and hands the chosen id to whoever
 * opened it — StartMenu (→ the picker, → /api/reset {character_id}) or, in a
 * run, the editors' "Change character" (→ /api/character/bind).
 *
 * No turnarounds and no slot lists on anything a player sees: the turnaround
 * is what the SIMULATION is shown; the player is shown the idle hero.
 */
(function () {
  "use strict";
  if (window.Characters) return;

  const LS_KEY = "somewhere.character";
  const POLL_MS = 1400;
  const sid = () => window.__SOMEWHERE_SESSION__ || "default";

  async function api(method, url, body) {
    const opt = { method, headers: { "X-Session-Id": sid() } };
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(Object.assign({ session_id: sid() }, body));
    }
    const sep = url.includes("?") ? "&" : "?";
    const res = await fetch(method === "GET" ? `${url}${sep}session_id=${encodeURIComponent(sid())}` : url, opt);
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (!res.ok || data.ok === false) {
      const err = new Error((data && data.error) || `HTTP ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return data;
  }
  function remembered() { try { return localStorage.getItem(LS_KEY) || ""; } catch (_) { return ""; } }
  function remember(id) { try { if (id) localStorage.setItem(LS_KEY, id); } catch (_) {} }
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function snd(name) { try { if (window.Sound && typeof Sound[name] === "function") Sound[name](); } catch (_) {} }
  const reduceMotion = () => {
    try { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (_) { return false; }
  };

  // ── the shared art: the backdrop, the figure ────────────────────────────
  // A studio, not weather. It was drawn smoke (fractal noise through a colour
  // matrix, drifting) until Matt: "the smokey background is extremely cheesy
  // … a much better background of a simpler gradient" (2026-09-24). Now: a
  // near-black field, one soft pool of key light behind where the figure
  // stands, a faint pool on the floor under their feet, the left side kept
  // darker for the words, and a fine static grain so the gradients never
  // band. Nothing moves. Pure CSS (characters.css, .cs-bg), no image.
  function backdrop() {
    return '<div class="cs-bg" aria-hidden="true"><i class="cs-bg-key"></i>' +
      '<i class="cs-bg-floor"></i><i class="cs-bg-grain"></i></div>';
  }
  // There is no stand-in figure. A drawn A-pose outline stood where the
  // character would be while the model worked, lit from the feet up with a
  // scan line: "get rid of this stupid stick figure … it looks cheesy" (Matt,
  // 2026-09-24). Now a first drawing has an empty stage — the bar and the
  // words carry the wait — and a redraw or a fitting shows the pose they had
  // as a black shape with a faint rim of light (.cs-fig.cs-ghost), nothing
  // invented.
  window.CharacterArt = { backdrop };

  // ── state ────────────────────────────────────────────────────────────────
  const S = {
    open: false,
    mode: "select",          // select | create | developing
    list: [],
    sel: 0,
    fresh: "",               // the id just made: CHARACTER · NEW on the reveal
    opts: {},
    pics: [],                // data urls the player added to the create line
    watching: "",            // the id whose drawing the screen is following
    pollT: null,
    changing: false,         // the CHANGE SOMETHING line is open
    saidFor: "",             // "id|voice url" of the line already spoken on this arrival
    busy: false,
    offline: false,
    t0: 0,
    finishing: "",
    styleOpen: false,        // the STYLE prompt is open under the name
    styleDraft: "",          // what the create screen will draw them in ("" = the game's)
    defaultStyle: "",        // characters.DEFAULT_STYLE, from the roster answer
    entered: false,          // the entrance has played since this open
    dir: 0,                  // which way the roster last moved (the figure drifts with it)
  };
  let root = null;
  const $ = (sel) => root && root.querySelector(sel);

  function current() { return S.list[S.sel] || null; }
  function selectedId() {
    const c = current();
    if (c && c.status === "ready") return c.id;
    return remembered();
  }

  // ── the figure: two slots that cross-fade ───────────────────────────────
  // Filmed 2026-09-24 at a player's speed: the figure was ONE <img> whose src
  // was swapped, so a new picture replaced the old in a single frame (the
  // stand-in pose becoming the hero pose), and the developing picture was a
  // DIFFERENT <img> from the revealed one — at the reveal it vanished and
  // the idle faded up from nothing. Now there are two slots: the new picture
  // loads into the back one, fades in over the front, and they trade places.
  // A picture is never replaced by nothing unless nothing is asked for.
  // Shared (CharacterArt.Figure): the pack in a run draws its figure with the
  // same two slots, so wearing something feels like the main menu does.
  // host gets cs-fig-shown while a picture is up; alive() says the screen is.
  function makeFigure(a, b, host, alive) {
   return {
    front: a, back: b, want: "",
    cidOf(src) { const m = /\/api\/characters\/([^/]+)\//.exec(src || ""); return m ? m[1] : ""; },
    reset() {
      [this.front, this.back].forEach((im) => { if (im) { im.classList.remove("on", "cs-leaving", "cs-dev-in", "cs-ghost", "cs-strip"); im.removeAttribute("src"); } });
      this.want = "";
      this.mark();
    },
    treat(img, kind) {
      img.classList.toggle("cs-ghost", kind === "ghost");
      img.classList.toggle("cs-strip", kind === "strip");
    },
    shown() { return !!(this.front && this.front.classList.contains("on") && this.front.getAttribute("src")); },
    mark() { if (host) host.classList.toggle("cs-fig-shown", this.shown() || !!this.want); },
    leave(img, move) {
      if (!img.classList.contains("on")) return;
      img.style.setProperty("--cs-move", `${(move || 0) * -18}px`);
      img.classList.add("cs-leaving");
      img.classList.remove("on");
      const done = () => { img.classList.remove("cs-leaving"); img.style.removeProperty("--cs-move"); };
      setTimeout(done, 1500);
    },
    // kind: idle | ghost | strip; opts.develop = the picture develops in
    // (light and blur settling) rather than fading; opts.move = -1/1 drifts
    // with the roster; opts.slow = the same person in a better picture.
    set(src, kind, opts) {
      if (!this.front) return;
      opts = opts || {};
      kind = kind || "idle";
      if (src && src === this.front.getAttribute("src") && this.front.classList.contains("on")) {
        this.want = src;
        this.treat(this.front, kind);   // the treatment eases (CSS), the picture stays
        this.mark();
        return;
      }
      if (src === this.want) return;    // already on its way in
      this.want = src;
      if (!src) { this.leave(this.front, opts.move); this.mark(); return; }
      // The same person in a better picture — but the four-view strip is
      // framed nothing like a pose, so leaving it is an ordinary cross-fade.
      // Out of a shadow, the new picture develops in (light settling), whether
      // or not a poll caught the in-between state.
      if (this.front && this.front.classList.contains("cs-ghost") && kind !== "ghost") opts = Object.assign({}, opts, { develop: true });
      const sameOne = this.shown() && !this.front.classList.contains("cs-strip") &&
        kind !== "strip" && this.cidOf(this.front.getAttribute("src")) === this.cidOf(src);
      const held = warmed.get(src);
      const pre = held && held.complete && held.naturalWidth ? null : new Image();
      const go = () => {
        if (this.want !== src || !alive()) return;
        const inc = this.back, out = this.front;
        inc.classList.remove("on", "cs-leaving", "cs-dev-in");
        inc.style.setProperty("--cs-move", `${(opts.move || 0) * 18}px`);
        inc.style.setProperty("--cs-delay", "0s");
        inc.style.setProperty("--cs-fade", sameOne || opts.slow ? "1.4s" : "0.8s");
        out.style.setProperty("--cs-fade", sameOne || opts.slow ? "1.4s" : "0.8s");
        this.treat(inc, kind);
        inc.src = src;
        void inc.offsetWidth;
        if (opts.develop && !reduceMotion()) {
          inc.classList.add("cs-dev-in");
          const off = (e) => { if (e.animationName === "cs-develop") { inc.classList.remove("cs-dev-in"); inc.removeEventListener("animationend", off); } };
          inc.addEventListener("animationend", off);
        }
        inc.style.setProperty("--cs-move", "0px");
        inc.classList.add("on");
        if (sameOne) {
          // The same person in a better picture: the new one comes up over
          // the old, and the old only starts to go once it is well covered
          // (a two-way fade dipped the figure to half and let the backdrop
          // through; a cut once covered left the old A-pose's arms showing
          // past the new pose for a second — both filmed 2026-09-24).
          out.style.setProperty("--cs-fade", "0.9s");
          out.style.setProperty("--cs-delay", "0.5s");
          out.style.zIndex = "0"; inc.style.zIndex = "1";
          out.classList.remove("on");
          setTimeout(() => out.style.removeProperty("--cs-delay"), 1500);
        } else {
          // Somebody else: the one leaving goes quickly, the one arriving
          // takes its time, so the two bodies barely overlap.
          out.style.setProperty("--cs-fade", "0.45s");
          inc.style.setProperty("--cs-delay", "0.1s");
          out.style.zIndex = "0"; inc.style.zIndex = "1";
          this.leave(out, opts.move);
          setTimeout(() => inc.style.removeProperty("--cs-delay"), 1200);
        }
        this.front = inc; this.back = out;
        this.mark();
      };
      if (!pre) { go(); return; }
      pre.onload = go;
      pre.onerror = () => { if (this.want === src) { this.want = ""; this.mark(); } };
      pre.src = src;
      if (pre.complete && pre.naturalWidth) { pre.onload = null; go(); }
      this.mark();
    },
   };
  }
  window.CharacterArt.Figure = makeFigure;
  let Fig = null;

  // Mode changes (the roster → the create line → developing → the reveal)
  // used to be hard cuts of the whole left column. The column and the verbs
  // now dip out, change, and come back up; a second change asked for while
  // the first is still dipping lands at once instead of queueing behind it.
  let swapT = null, swapFn = null;
  function swapTo(fn) {
    if (swapFn) { const f = swapFn; swapFn = null; clearTimeout(swapT); try { f(); } catch (_) {} }
    if (!root || reduceMotion() || !S.open) { fn(); return; }
    swapFn = fn;
    root.classList.add("cs-swapping");
    swapT = setTimeout(() => {
      const f = swapFn; swapFn = null;
      if (f) f();
      void root.offsetWidth;
      root.classList.remove("cs-swapping");
      flipHead();
    }, 180);
  }
  // The heading and the line under it set in again when they change.
  function flipHead() {
    const h = $(".cs-head");
    if (!h || reduceMotion()) return;
    h.classList.remove("cs-textin");
    void h.offsetWidth;
    h.classList.add("cs-textin");
  }

  // ── build ────────────────────────────────────────────────────────────────
  function build() {
    if (root) return root;
    root = el("div", "char-screen");
    root.id = "char-screen";
    root.hidden = true;
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-label", "Characters");
    root.innerHTML =
      backdrop() +
      '<div class="cs-figure"><div class="cs-figure-in">' +
      '<img class="cs-source" alt="" draggable="false">' +
      '<img class="cs-fig cs-fig-a" alt="" draggable="false">' +
      '<img class="cs-fig cs-fig-b" alt="" draggable="false"></div></div>' +
      '<div class="cs-fade"></div>' +
      '<button type="button" class="cs-back">BACK</button>' +
      '<div class="cs-left">' +
      '  <div class="cs-head"><div class="cs-eyebrow"></div><div class="cs-name"></div><div class="cs-tag"></div>' +
      '  <button type="button" class="cs-style-line" aria-expanded="false"><span class="cs-style-k">STYLE</span>' +
      '<span class="cs-style-v"></span><i class="cs-style-caret" aria-hidden="true"></i></button>' +
      '  <button type="button" class="cs-voice-line" hidden><span class="cs-style-k">VOICE</span>' +
      '<span class="cs-voice-v"></span><i class="cs-voice-mark" aria-hidden="true"></i></button>' +
      '  <div class="cs-note" aria-live="polite"></div>' +
      '  <div class="cs-error" role="alert"></div></div>' +
      '  <div class="cs-roster" role="listbox" aria-label="Your characters"></div>' +
      '  <div class="cs-create">' +
      '    <label class="cs-sr" for="cs-who">Describe your character</label>' +
      '    <textarea id="cs-who" class="cs-who" rows="3" maxlength="600" spellcheck="true" ' +
      '      placeholder="A salvage diver in her forties. Patched olive canvas suit, brass helmet under one arm, rope and hook at the hip."></textarea>' +
      '    <div class="cs-pics"></div>' +
      '    <div class="cs-tools"><button type="button" class="cs-tool cs-add">+ ADD A PICTURE</button>' +
      '      <button type="button" class="cs-tool cs-surprise">SURPRISE ME</button></div>' +
      '    <input type="file" class="cs-file" accept="image/png,image/jpeg,image/webp" multiple hidden>' +
      '  </div>' +
      '  <div class="cs-style">' +
      '    <label class="cs-sr" for="cs-style-in">Art style</label>' +
      '    <textarea id="cs-style-in" class="cs-style-in" rows="3" maxlength="800" spellcheck="true"></textarea>' +
      '    <div class="cs-tools"><button type="button" class="cs-tool cs-style-reset">BACK TO DEFAULT</button>' +
      '      <span class="cs-style-hint"></span></div>' +
      '  </div>' +
      '  <div class="cs-dev" aria-live="polite"><div class="cs-dev-prompt"></div>' +
      '    <div class="cs-dev-line"></div><div class="cs-bar"><i></i></div><div class="cs-dev-eta"></div></div>' +
      '</div>' +
      '<div class="cs-actions">' +
      '  <div class="cs-change"><label class="cs-sr" for="cs-change-in">What should change?</label>' +
      '    <input id="cs-change-in" class="cs-change-in" maxlength="300" placeholder="What should change? — e.g. make the jacket a long waxed duster"></div>' +
      '  <div class="cs-buttons"><button type="button" class="cs-primary"></button>' +
      '    <button type="button" class="cs-second cs-again">TRY AGAIN</button>' +
      '    <button type="button" class="cs-second cs-change-btn">CHANGE SOMETHING</button></div>' +
      '</div>';
    document.body.appendChild(root);
    Fig = makeFigure($(".cs-fig-a"), $(".cs-fig-b"), root, () => S.open);
    ["pointerdown", "click", "wheel"].forEach((t) => root.addEventListener(t, (e) => e.stopPropagation()));
    $(".cs-back").addEventListener("click", back);
    $(".cs-primary").addEventListener("click", primary);
    $(".cs-again").addEventListener("click", tryAgain);
    $(".cs-change-btn").addEventListener("click", openChange);
    $(".cs-add").addEventListener("click", () => $(".cs-file").click());
    $(".cs-file").addEventListener("change", addPictures);
    $(".cs-surprise").addEventListener("click", surpriseMe);
    const who = $(".cs-who");
    who.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); primary(); }
      if (e.key === "Escape") { e.preventDefault(); back(); }
      e.stopPropagation();
    });
    who.addEventListener("input", () => paintActions());
    $(".cs-style-line").addEventListener("click", toggleStyle);
    $(".cs-voice-line").addEventListener("click", voiceClick);
    $(".cs-style-reset").addEventListener("click", () => {
      $(".cs-style-in").value = S.defaultStyle || "";
      styleInput();
      snd("select");
    });
    const sty = $(".cs-style-in");
    sty.addEventListener("input", styleInput);
    sty.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); primary(); }
      if (e.key === "Escape") { e.preventDefault(); closeStyle(); }
      e.stopPropagation();
    });
    const ch = $(".cs-change-in");
    ch.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); submitChange(); }
      if (e.key === "Escape") { e.preventDefault(); closeChange(); }
      e.stopPropagation();
    });
    // Drop pictures anywhere on the create screen.
    root.addEventListener("dragover", (e) => { if (S.mode === "create") { e.preventDefault(); root.classList.add("cs-drop"); } });
    root.addEventListener("dragleave", () => root.classList.remove("cs-drop"));
    root.addEventListener("drop", (e) => {
      root.classList.remove("cs-drop");
      if (S.mode !== "create") return;
      e.preventDefault();
      readFiles(e.dataTransfer && e.dataTransfer.files);
    });
    // The figure follows the pointer by a degree or two — alive, not animated.
    root.addEventListener("pointermove", (e) => {
      if (reduceMotion()) return;
      const f = $(".cs-figure-in");
      if (!f) return;
      const dx = (e.clientX / window.innerWidth - 0.5) * 2;
      const dy = (e.clientY / window.innerHeight - 0.5) * 2;
      f.style.setProperty("--cs-tilt-x", `${(dx * 1.4).toFixed(2)}deg`);
      f.style.setProperty("--cs-tilt-y", `${(-dy * 0.8).toFixed(2)}deg`);
    });
    return root;
  }

  // ── open / close ─────────────────────────────────────────────────────────
  // PLAY calls this as the veil starts to cover (StartMenu.openCharacters):
  // the roster and the selected figure are fetched and decoded during the
  // 1.3 s of black, so the screen comes up whole. Filmed before it: the veil
  // lifted on an empty "+ Create new", the name arrived a beat later, the
  // figure a beat after that.
  let prefetched = null;
  function prefetch() {
    prefetched = api("GET", "/api/characters").then((data) => {
      const list = data.characters || [];
      const want = remembered() || data.last;
      const c = list.find((x) => x.id === want) || list.find((x) => x.status === "ready") || list[0];
      const src = c && (c.idle || c.source);
      if (!src) return data;
      const im = new Image();
      im.src = src;
      return (im.decode ? im.decode().catch(() => {}) : Promise.resolve()).then(() => data);
    }).catch(() => null);
    return prefetched;
  }
  function take(data, first) {
    S.offline = !!data.offline;
    if (data.default_style) S.defaultStyle = data.default_style;
    const before = current() && current().id;
    S.list = data.characters || [];
    let want = before || (first ? (remembered() || data.last) : "");
    if (S.watching) want = S.watching;
    let i = S.list.findIndex((c) => c.id === want);
    if (i < 0) i = S.list.findIndex((c) => c.status === "ready");
    S.sel = Math.max(0, i);
    warm();
  }
  // Every figure on the roster, fetched and decoded before it is asked for:
  // moving down the list, the name used to change a third of a second before
  // the picture could start to cross-fade.
  // Held, not just requested: an <img> nobody keeps is free to lose its
  // decoded pixels, and decoding a 2K hero pose is the delay being removed.
  const warmed = new Map();
  function warm() {
    S.list.forEach((c) => {
      const src = c.idle;
      if (!src || warmed.has(src)) return;
      const im = new Image();
      im.src = src;
      if (im.decode) im.decode().catch(() => {});
      warmed.set(src, im);
    });
  }
  // The screen sets in: the backdrop is already there, the figure rises a few
  // pixels as it fades up, the words follow it down the column.
  function enter() {
    if (S.entered || !root) return;
    S.entered = true;
    if (reduceMotion()) return;
    root.classList.remove("cs-enter");
    void root.offsetWidth;
    root.classList.add("cs-enter");
    setTimeout(() => root && root.classList.remove("cs-enter"), 2200);
  }
  async function show(opts) {
    build();
    S.opts = opts || {};
    S.open = true;
    S.changing = false;
    S.styleOpen = false;
    S.fresh = "";
    S.entered = false;
    S.saidFor = "";
    root.hidden = false;
    root.classList.toggle("cs-in-run", !!S.opts.inRun);
    // Under the menu's veil the veil is the fade: the screen is simply there
    // when it lifts. Filmed with the screen's own 0.35 s fade on top, the
    // title's background film showed through it for a few frames on the way
    // in (2026-09-24).
    const veiled = document.body.classList.contains("buck-on");
    if (veiled) root.style.transition = "none";
    void root.offsetWidth;
    root.classList.add("on");
    if (veiled) { void root.offsetWidth; root.style.transition = ""; }
    document.body.classList.add("char-open");
    S.mode = "select";
    root.dataset.mode = "select";
    // Back from the picker, the roster from a moment ago is still here:
    // up with it at once (the fetch below only freshens it). Filmed without
    // this, the veil lifted on an empty backdrop for a quarter of a second.
    if (S.list.length) {
      S.sel = Math.max(0, S.list.findIndex((c) => c.id === (remembered() || "")));
      paint();
      enter();
    }
    let got = null;
    if (prefetched) {
      // Usually settled by now (it ran under the veil); if not, wait for it
      // rather than paint an empty screen and fill it in piece by piece.
      got = await Promise.race([prefetched, new Promise((r) => setTimeout(() => r(null), 1500))]);
      prefetched = null;
    }
    if (got) {
      take(got, !S.entered);
      if (!S.list.length) S.mode = root.dataset.mode = "create";
      paint();
      enter();
      if (anyBusy()) startPoll();
      refresh(false);
    } else {
      await refresh(true);
      if (!S.list.length) { S.mode = root.dataset.mode = "create"; paint(); }
      enter();
    }
    sayCurrentSoon(700);
    return true;
  }
  function hide() {
    if (!root) return;
    S.open = false;
    stopVoice();
    stopPoll();
    stopDev();
    root.classList.remove("on", "cs-enter", "cs-swapping");
    document.body.classList.remove("char-open");
    root.hidden = true;
    // Next open starts clean: no half-faded slot left over.
    if (Fig) Fig.reset();
  }
  function isOpen() { return S.open; }
  function anyBusy() {
    return S.list.some((c) => c.status === "drawing" || (c.job && c.job.kind) ||
      (c.voice && c.voice.status === "designing"));
  }

  async function refresh(first) {
    let data;
    try { data = await api("GET", "/api/characters"); } catch (e) {
      showError("The characters would not load. " + (e.message || ""));
      return;
    }
    if (!S.open) return;
    take(data, first);
    paint();
    // Anyone still being drawn, or given a voice: keep following them.
    if (anyBusy()) startPoll();
  }

  // ── painting ─────────────────────────────────────────────────────────────
  function setMode(m) {
    S.mode = m;
    if (!root) return;
    root.dataset.mode = m;
    if (m === "developing") startDev(); else stopDev();
    paint();
  }
  function showError(msg) {
    const e = $(".cs-error");
    if (e) e.textContent = msg || "";
  }
  function spaced(name) {
    // The wordmark: first name over last, tracked out, bone on black.
    const parts = String(name || "").trim().toUpperCase().split(/\s+/).filter(Boolean);
    if (parts.length <= 1) return parts.join("");
    const first = parts.shift();
    return `${first}\n${parts.join(" ")}`;
  }
  // The wordmark keeps to two lines: a long name is set smaller, not wrapped.
  function fitName(n) {
    const lines = String(n.textContent || "").split("\n");
    const longest = Math.max(...lines.map((l) => l.length), 1);
    n.classList.toggle("cs-long", longest > 9);
    n.classList.toggle("cs-longer", longest > 13);
  }
  function metaOf(c) {
    if (c.id === S.fresh && !c.finishing) return "JUST MADE";
    if (c.status === "drawing") return "DRAWING…";
    if (c.status === "new") return "NOT DRAWN YET";
    if (c.status === "failed") return "COULD NOT BE DRAWN";
    if (c.finishing) return "FINISHING…";
    if (c.fitting || (c.job && c.job.kind)) return "CHANGING…";
    if (!c.runs) return "NEW";
    return `${c.runs} RUN${c.runs === 1 ? "" : "S"}`;
  }
  function setText(node, text) { if (node && node.textContent !== text) node.textContent = text; }
  let headWas = "";
  function paint() {
    if (!root) return;
    const c = current();
    const m = S.mode;
    setText($(".cs-back"), m === "create" ? (S.list.length ? "CHARACTERS" : "BACK")
      : m === "developing" ? "CHARACTERS" : (S.opts.inRun ? "CLOSE" : "BACK"));
    const eyebrow = $(".cs-eyebrow");
    const nameEl = $(".cs-name");
    const tag = $(".cs-tag");
    let eb = "", nm = "", tg = "", mint = false, pending = false;
    if (m === "create") {
      eb = "NEW CHARACTER"; nm = "DESCRIBE\nTHEM";
    } else if (m === "developing") {
      const w = S.list.find((x) => x.id === S.watching) || c || {};
      eb = w.base_look || w.status === "ready" ? "CHARACTER" : "NEW CHARACTER";
      // Who they are lands first (the brief, ~5 s in), what they look like
      // after: the name sets in over "PICTURING THEM" while the figure is
      // still an outline.
      if (w.name) { nm = spaced(w.name); tg = w.tagline || ""; }
      else { nm = "PICTURING\nTHEM"; pending = true; }
    } else if (c) {
      const isNew = c.id === S.fresh;
      eb = isNew ? "CHARACTER · NEW" : "CHARACTER";
      mint = isNew;
      nm = spaced(c.name || "Unnamed");
      tg = c.status === "new" ? "Carried over from your old character sheet. Draw them to play." : (c.tagline || "");
    }
    setText(eyebrow, eb);
    eyebrow.classList.toggle("cs-mint", mint);
    setText(nameEl, nm);
    nameEl.classList.toggle("cs-pending", pending);
    fitName(nameEl);
    setText(tag, tg);
    // A new name or line in the same mode (the roster moved, the brief
    // landed) sets in; a mode change does its own (swapTo).
    const head = `${nm}|${tg}`;
    if (headWas && head !== headWas && !root.classList.contains("cs-swapping")) flipHead();
    headWas = head;
    setText($(".cs-note"), m === "select" && c && c.finishing
      ? "FINISHING THE PORTRAIT — PLAY WHENEVER YOU'RE READY" : "");
    if (m !== "developing") showError(c && c.status === "failed" ? `Could not be drawn: ${c.error || "unknown"}` :
      (c && c.error && m === "select" ? `The last change could not be drawn: ${c.error}` : ""));
    paintRoster();
    paintFigure();
    paintDev();
    paintStyle();
    paintVoice();
    paintActions();
  }
  // The roster is built once per list and then updated in place: rebuilt on
  // every poll, the thumbnails were re-created every 1.4 s while anyone was
  // drawing.
  let rosterKey = "";
  function paintRoster() {
    const box = $(".cs-roster");
    const key = S.list.map((c) => c.id).join("|");
    if (key !== rosterKey || box.childElementCount !== S.list.length + 1) {
      rosterKey = key;
      box.innerHTML = "";
      S.list.forEach((c, i) => {
        const b = el("button", "cs-item");
        b.type = "button";
        b.setAttribute("role", "option");
        b.dataset.id = c.id;
        const th = el("span", "cs-thumb");
        b.appendChild(th);
        const tx = el("span", "cs-item-tx");
        const nm = el("span", "cs-item-name");
        nm.appendChild(el("span", "cs-item-nm"));
        nm.appendChild(el("span", "cs-new", "NEW"));
        tx.appendChild(nm);
        tx.appendChild(el("span", "cs-item-meta"));
        b.appendChild(tx);
        b.addEventListener("click", () => { pick(i); });
        b.addEventListener("dblclick", () => { pick(i); primary(); });
        box.appendChild(b);
      });
      const add = el("button", "cs-item cs-add-new");
      add.type = "button";
      add.innerHTML = '<span class="cs-thumb cs-plus">+</span><span class="cs-item-tx"><span class="cs-item-name">Create new</span></span>';
      add.addEventListener("click", startCreate);
      box.appendChild(add);
    }
    S.list.forEach((c, i) => {
      const b = box.children[i];
      b.classList.toggle("sel", i === S.sel);
      b.setAttribute("aria-selected", i === S.sel ? "true" : "false");
      b.setAttribute("aria-label", c.name || "Unnamed");
      setText(b.querySelector(".cs-item-nm"), c.name || "Unnamed");
      b.querySelector(".cs-new").hidden = c.id !== S.fresh;
      setText(b.querySelector(".cs-item-meta"), metaOf(c));
      const th = b.querySelector(".cs-thumb");
      const src = c.thumb || c.source || "";
      const im = th.querySelector("img");
      if (src) {
        if (!im) {
          th.innerHTML = "";
          const n = new Image(); n.alt = ""; n.draggable = false;
          n.onload = () => n.classList.add("in");
          n.src = src; th.appendChild(n);
        } else if (im.getAttribute("src") !== src) {
          const pre = new Image();
          pre.onload = () => { im.src = src; };
          pre.src = src;
        }
      } else if (th.firstChild) {
        th.innerHTML = "";
      }
    });
    const sel = box.children[S.sel];
    if (sel && sel.scrollIntoView && S.mode === "select") {
      try { sel.scrollIntoView({ block: "nearest", behavior: reduceMotion() ? "auto" : "smooth" }); } catch (_) {}
    }
  }
  // The cast-sheet picture of a character not drawn yet (dim, framed).
  function setSource(img, src) {
    if (!src) { img.classList.remove("on"); return; }
    if (img.getAttribute("src") === src) { img.classList.add("on"); return; }
    img.classList.remove("on");
    img.onload = () => img.classList.add("on");
    img.src = src;
  }
  function paintFigure() {
    const c = current();
    const m = S.mode;
    const w = m === "developing" ? (S.list.find((x) => x.id === S.watching) || {}) : c || {};
    root.classList.toggle("cs-drawing", m === "developing" || m === "create" || w.status === "drawing" ||
      (!!w.finishing && !w.idle));
    root.classList.toggle("cs-empty-figure", !w.idle && !w.source);
    setSource($(".cs-source"), !w.idle && w.source && m === "select" ? w.source : "");
    if (m === "create") { Fig.set("", "idle", { move: 0 }); return; }
    if (m === "developing") {
      const d = w.dev || {};
      // The first pose develops in; before that, a character being redrawn
      // stands behind the outline as a shadow of who they were. (The
      // four-view strip is not shown: it is on disk a moment before the pose
      // cut from it, and filmed, a poll that landed in that moment flashed
      // the strip for a second before the pose developed over it.)
      // Filmed on the PC: the poll that found the drawing finished (job gone,
      // so no dev) set the NEW portrait as a shadow for a second before the
      // reveal turned it to colour. A finished drawing develops in, always.
      const busy = !!(w.job && w.job.kind);
      if (d.idle) Fig.set(d.idle, "idle", { develop: true });
      else if (!busy && w.idle) Fig.set(w.idle, "idle", { develop: true });
      else Fig.set(w.idle || "", "ghost");
      return;
    }
    Fig.set(w.status === "drawing" ? "" : (w.idle || ""), "idle", { move: S.dir });
    S.dir = 0;
  }

  // ── progress, while they are drawn ───────────────────────────────────────
  // The bar used to be a CSS width set on each 1.4 s poll — a staircase —
  // against a fixed 30 s estimate, so it sat at two thirds when the character
  // arrived and jumped. It is driven every frame now: an ease toward a curve
  // of the time this machine's drawings actually take (job.eta, learned
  // server-side from the last few), never backwards, and at the reveal it
  // runs out to the end before the screen changes. The same number lights
  // the outline from the feet up, with a scan line at the edge.
  const DEV = { raf: 0, p: 0, target: 0, last: 0, eta: "", done: null };
  function devJob() {
    const w = S.list.find((x) => x.id === S.watching) || {};
    return w.job || {};
  }
  function startDev() {
    if (DEV.raf) return;
    DEV.p = 0; DEV.last = performance.now(); DEV.done = null; DEV.eta = "";
    const tick = (t) => {
      DEV.raf = requestAnimationFrame(tick);
      const dt = Math.min(0.1, (t - DEV.last) / 1000);
      DEV.last = t;
      const job = devJob();
      const eta = Math.max(6, Number(job.eta) || 30);
      const total = Math.max(eta + 6, Number(job.eta_total) || eta * 1.9);
      const now = Date.now() / 1000;
      const el_ = Math.max(0, now - (Number(job.started) || now));
      const playable = job.stage === "finishing";
      // One bar, to the portrait; being playable is its middle.
      let target = Math.min(0.94, Math.max(1 - Math.exp(-2.3 * el_ / total), Number(job.progress) || 0));
      if (DEV.done) target = 1;
      DEV.target = Math.max(DEV.target, target);
      const k = DEV.done ? 9 : 2.2;
      DEV.p = Math.min(1, Math.max(DEV.p, DEV.p + (DEV.target - DEV.p) * Math.min(1, dt * k)));
      if (root) {
        root.style.setProperty("--cs-p", DEV.p.toFixed(4));
        const to = playable ? total : eta;
        const left = to - el_;
        const when = left > 7 ? `ABOUT ${Math.ceil(left / 5) * 5} SECONDS`
          : left > 2 ? "A FEW SECONDS"
          : el_ > to * 1.6 ? "TAKING LONGER THAN USUAL" : "ANY MOMENT";
        const txt = S.offline ? "OFFLINE — A STAND-IN, NO KEY"
          : DEV.done ? "HERE THEY ARE"
          : playable ? `YOU CAN PLAY NOW · THE PORTRAIT IN ${when}`
          : `PLAYABLE IN ${when}`;
        if (txt !== DEV.eta) { DEV.eta = txt; setText($(".cs-dev-eta"), txt); }
      }
      if (DEV.done && DEV.p > 0.995) { const f = DEV.done; DEV.done = null; f(); }
    };
    DEV.raf = requestAnimationFrame(tick);
  }
  function stopDev() {
    if (DEV.raf) cancelAnimationFrame(DEV.raf);
    DEV.raf = 0; DEV.target = 0;
  }
  // Run the bar out, then do fn (the reveal). Straight away with no bar.
  function finishDev(fn) {
    if (!DEV.raf || reduceMotion()) { fn(); return; }
    DEV.done = fn;
    setTimeout(() => { if (DEV.done === fn) { DEV.done = null; fn(); } }, 900);
  }
  function paintDev() {
    const w = S.list.find((x) => x.id === S.watching) || {};
    const job = w.job || {};
    setText($(".cs-dev-prompt"), w.concept || "");
    const line = $(".cs-dev-line");
    const txt = DEV.done ? "Here they are." : (job.line || (w.status === "failed" ? "" : "Picturing them…"));
    if (line && line.textContent !== txt) {
      const had = !!line.textContent;
      line.textContent = txt;
      if (had && !reduceMotion()) { line.classList.remove("cs-textin"); void line.offsetWidth; line.classList.add("cs-textin"); }
    }
  }
  // ── STYLE: the art style, under the name ─────────────────────────────────
  // Photoreal unless they say otherwise: "i think having a consistent art
  // style at first is key, but if players want we break it, we'll let them"
  // (Matt, 2026-09-24). Closed it is one quiet line under the name — STYLE ·
  // PHOTOREAL — and open it is the prompt itself, the game's default in it,
  // to rewrite. On a character: REDRAW IN THIS STYLE (the same person, only
  // the rendering changes — characters.restyle). On the create screen: what
  // the new one will be drawn in.
  // ── VOICE ────────────────────────────────────────────────────────────────
  // Who you are sounds like someone, and it is the voice the run is narrated
  // in (the narrator is you, speaking into a tape). Designed from words when
  // the brief lands (characters.design_voice); heard for the first time at
  // the reveal, once, by itself; after that on a click. Only where a voice
  // can be designed (a Gemini key): elsewhere the line is not drawn at all.
  let voiceAudio = null;
  function stopVoice() {
    if (voiceAudio) { try { voiceAudio.pause(); } catch (_) {} voiceAudio = null; }
    if (root) root.classList.remove("cs-voice-playing");
  }
  function playVoice(c) {
    const v = c && c.voice;
    if (!v || !v.url) return;
    stopVoice();
    const a = new Audio(v.url);
    voiceAudio = a;
    const end = () => { if (voiceAudio === a) stopVoice(); };
    a.addEventListener("ended", end);
    a.addEventListener("error", end);
    a.addEventListener("playing", () => { if (voiceAudio === a && root) root.classList.add("cs-voice-playing"); });
    a.play().catch(end);
  }
  // The character you are looking at says their line: when the screen
  // opens, and each time you move to another one — once per arrival, and not
  // again while you stay. Arrowing through the roster speaks only for the one
  // you stop on (sayT). A voice still being designed speaks when it lands, if
  // they are still the one chosen (pollOnce calls this).
  let sayT = 0;
  function sayCurrent() {
    const c = current();
    if (!S.open || S.mode !== "select" || !c || !c.voice || !c.voice.url) return;
    const key = c.id + "|" + c.voice.url;
    if (S.saidFor === key) return;
    S.saidFor = key;
    playVoice(c);
  }
  function sayCurrentSoon(ms) {
    clearTimeout(sayT);
    sayT = setTimeout(sayCurrent, ms);
  }
  function voiceLabel(v) {
    if (!v) return "";
    if (v.status === "designing") return "FINDING IT\u2026";
    if (v.status === "failed") return "COULD NOT FIND IT \u2014 TRY AGAIN";
    if (v.status !== "ready") return "GIVE THEM ONE";
    const first = String(v.description || "").split(/[.:;]/)[0].replace(/^(a|an|the)\s+/i, "");
    const words = first.trim().split(/\s+/);
    return (words.slice(0, 5).join(" ") + (words.length > 5 ? "\u2026" : "")).toUpperCase();
  }
  function paintVoice() {
    if (!root) return;
    const c = current();
    const line = $(".cs-voice-line");
    const show = S.mode === "select" && c && c.voice && c.voice.can && c.status === "ready";
    line.hidden = !show;
    if (!show) { stopVoice(); return; }
    setText($(".cs-voice-v"), voiceLabel(c.voice));
    line.classList.toggle("cs-voice-wait", c.voice.status === "designing");
    line.title = c.voice.status === "ready" ? (c.voice.line ? `\u201c${c.voice.line}\u201d` : "Hear them") : "";
  }
  async function voiceClick() {
    const c = current();
    if (!c || !c.voice) return;
    if (c.voice.status === "ready") {
      if (voiceAudio) { stopVoice(); return; }
      snd("select");
      playVoice(c);
      return;
    }
    if (c.voice.status === "designing") return;
    changeVoice(c, "");
  }
  // A line in CHANGE SOMETHING that is about how they sound.
  function aboutTheVoice(t) {
    return /\b(voice|voices|accent|sounds?|sounding|speaks?|speaking|talks?|pitch|deeper|higher|raspier|husk(y|ier)|drawl|lisp|whisper(y|s)?)\b/i.test(t);
  }
  async function changeVoice(c, change) {
    if (S.busy) return;
    S.busy = true;
    stopVoice();
    try {
      const res = await api("POST", `/api/characters/${c.id}/voice`, { change });
      const i = S.list.findIndex((x) => x.id === c.id);
      if (i >= 0) S.list[i] = Object.assign({}, S.list[i], res.character);
      closeChange();
      // Heard by itself once it lands, like a new character's.
      S.saidFor = "";
      snd("submit");
      paint();
      startPoll();
    } catch (e) {
      showError("Could not change their voice: " + (e.message || ""));
    } finally {
      S.busy = false;
    }
  }

  function isDefault(txt) {
    const t = String(txt || "").trim();
    return !t || t === String(S.defaultStyle || "").trim();
  }
  function styleNow() {
    if (S.mode === "create") return S.styleDraft || "";
    const c = current();
    return (c && c.style) || "";
  }
  function styleLabel(txt) {
    if (isDefault(txt)) return "PHOTOREAL";
    const words = String(txt).trim().replace(/[.,;:—-]+$/, "").split(/\s+/).slice(0, 4).join(" ");
    return words.toUpperCase() + (String(txt).trim().split(/\s+/).length > 4 ? "\u2026" : "");
  }
  // The prompt shows whole — a fixed six rows cut the default's last line.
  function fitStyle() {
    const t = $(".cs-style-in");
    if (!t) return;
    t.style.height = "auto";
    t.style.height = `${Math.min(t.scrollHeight + 2, Math.round(window.innerHeight * 0.3))}px`;
  }
  function styleInput() {
    fitStyle();
    const v = $(".cs-style-in").value;
    if (S.mode === "create") S.styleDraft = isDefault(v) ? "" : v.trim();
    paintStyle();
    paintActions();
  }
  function paintStyle() {
    if (!root) return;
    const c = current();
    const can = S.mode === "create" || (S.mode === "select" && c && (c.status === "ready" || c.status === "new") &&
      !(c.job && c.job.kind));
    const line = $(".cs-style-line");
    line.hidden = !can;
    setText($(".cs-style-v"), styleLabel(S.styleOpen ? $(".cs-style-in").value : styleNow()));
    line.setAttribute("aria-expanded", S.styleOpen ? "true" : "false");
    root.classList.toggle("cs-style-open", S.styleOpen && can);
    const v = $(".cs-style-in").value;
    setText($(".cs-style-hint"), isDefault(v) ? "THE GAME'S OWN STYLE" : "YOUR OWN STYLE");
    $(".cs-style-reset").hidden = isDefault(v);
    if (S.styleOpen && !can) S.styleOpen = false;
  }
  function openStyle() {
    if (S.styleOpen) return;
    const go = () => {
      S.styleOpen = true;
      S.changing = false;
      const t = $(".cs-style-in");
      t.value = styleNow() || S.defaultStyle || "";
      paint();
      fitStyle();
      setTimeout(() => { try { t.focus({ preventScroll: true }); t.setSelectionRange(t.value.length, t.value.length); } catch (_) {} }, 60);
    };
    snd("open");
    if (S.mode === "create") go(); else swapTo(go);
  }
  function closeStyle() {
    if (!S.styleOpen) return;
    snd("menuClose");
    const go = () => { S.styleOpen = false; paint(); };
    if (S.mode === "create") go(); else swapTo(go);
  }
  function toggleStyle() { if (S.styleOpen) closeStyle(); else openStyle(); }
  async function submitStyle() {
    const c = current();
    const v = $(".cs-style-in").value.trim();
    if (!c || S.busy) return;
    const same = c.style_known !== false && (isDefault(v) ? isDefault(c.style) : v === String(c.style || "").trim());
    if (same || !v) { closeStyle(); return; }
    S.busy = true;
    paintActions();
    showError("");
    try {
      const res = await api("POST", `/api/characters/${c.id}/style`, { style: isDefault(v) ? "" : v });
      const i = S.list.findIndex((x) => x.id === c.id);
      if (i >= 0) S.list[i] = res.character;
      S.styleOpen = false;
      S.watching = c.id;
      S.fresh = "";
      snd("submit");
      swapTo(() => setMode("developing"));
      startPoll();
    } catch (e) {
      showError("Could not redraw them: " + (e.message || ""));
      snd("error");
    } finally {
      S.busy = false;
      paintActions();
    }
  }

  function paintActions() {
    const c = current();
    const p = $(".cs-primary");
    const again = $(".cs-again"), change = $(".cs-change-btn");
    let label = "", show2 = false;
    if (S.mode === "create") {
      label = "CREATE ⏎";
      p.disabled = !($(".cs-who").value.trim() || S.pics.length) || S.busy;
    } else if (S.mode === "developing") {
      // Playable while the portrait is still being posed: PLAY is here.
      const w = S.list.find((x) => x.id === S.watching) || {};
      label = w.status === "ready" && !S.revealing ? (S.opts.inRun ? "BE THEM ⏎" : "PLAY NOW ⏎") : "";
      p.disabled = S.busy;
    } else if (c && S.styleOpen) {
      const v = $(".cs-style-in").value.trim();
      const same = c.style_known !== false && (isDefault(v) ? isDefault(c.style) : v === String(c.style || "").trim());
      label = c.status === "new" ? "DRAW IN THIS STYLE ⏎" : "REDRAW IN THIS STYLE ⏎";
      p.disabled = S.busy || same || !v;
    } else if (c) {
      p.disabled = S.busy;
      if (c.status === "ready") { label = S.opts.inRun ? "BE THEM ⏎" : "PLAY ⏎"; show2 = c.id === S.fresh; }
      else if (c.status === "new") label = "DRAW ⏎";
      else if (c.status === "failed") label = "TRY AGAIN ⏎";
      else if (c.status === "drawing") { label = "WATCH ⏎"; }
    }
    p.textContent = label;
    p.hidden = !label;
    again.hidden = !show2;
    change.hidden = !show2;
    const ch = $(".cs-change");
    ch.classList.toggle("on", S.changing);
  }

  // ── the poll: following a drawing ────────────────────────────────────────
  function startPoll() {
    if (S.pollT) return;
    S.pollT = setInterval(pollOnce, POLL_MS);
  }
  function stopPoll() { if (S.pollT) { clearInterval(S.pollT); S.pollT = null; } }
  let polling = false;
  async function pollOnce() {
    if (!S.open || polling) return;
    polling = true;
    try {
      const busyOnes = S.list.filter((c) => c.status === "drawing" || (c.job && c.job.kind) || c.id === S.watching ||
        (c.voice && c.voice.status === "designing"));
      if (!busyOnes.length) { stopPoll(); return; }
      for (const c of busyOnes) {
        let got;
        try { got = (await api("GET", `/api/characters/${c.id}`)).character; } catch (_) { continue; }
        const i = S.list.findIndex((x) => x.id === c.id);
        if (i >= 0) S.list[i] = Object.assign({}, S.list[i], got);
        warm();
        // The reveal is the HERO POSE: playable comes ~25 s earlier (the
        // turnaround, characters._draw_look's on_ready) and PLAY works from
        // then, on this screen, but the portrait is only ever a posed one —
        // a stand-in cut off the sheet read as an A-pose (2026-09-24).
        const posed = !!(got.dev && got.dev.idle) || !!got.idle;
        const doneNow = got.status !== "drawing" &&
          (!(got.job && got.job.kind) || (got.job.stage === "finishing" && posed));
        if (c.id === S.watching && doneNow && !S.revealing) {
          // The bar runs out, "Here they are.", then the column turns over to
          // their name while the figure that developed in stays where it is.
          S.revealing = true;
          const ok = got.status === "ready";
          const reveal = () => {
            S.revealing = false;
            if (!S.open || S.watching !== got.id) return;
            S.watching = "";
            S.finishing = got.job && got.job.stage === "finishing" ? got.id : "";
            if (ok) { S.fresh = got.id; snd("itemReveal"); } else snd("error");
            S.sel = Math.max(0, S.list.findIndex((x) => x.id === got.id));
            swapTo(() => setMode("select"));
            S.saidFor = "";
            sayCurrentSoon(700);
          };
          if (ok) { finishDev(reveal); paintDev(); } else reveal();
          continue;
        }
      }
      paint();
      sayCurrent();
    } finally {
      polling = false;
    }
  }

  // ── actions ──────────────────────────────────────────────────────────────
  function pick(i) {
    const n = S.list.length;
    if (!n) return;
    const to = (i + n) % n;
    if (S.mode !== "select") { S.sel = to; swapTo(() => setMode("select")); return; }
    if (to === S.sel) return;
    S.dir = to > S.sel ? 1 : -1;
    S.sel = to;
    S.styleOpen = false;
    S.changing = false;
    stopVoice();
    S.saidFor = "";
    snd("focusTick");
    paint();
    sayCurrentSoon(350);
    freshVoice(S.list[to]);
  }
  // Switching to someone whose voice this screen last saw as not ready: read
  // their card again. The roster retries a failed voice on its own (the
  // server's ensure_voice), and the screen, which only follows voices it
  // knows are being designed, kept showing "COULD NOT FIND IT" over a voice
  // that had since landed — Jason, on the first opening of Matt's own roster
  // after a Google 500 (2026-09-25).
  async function freshVoice(c) {
    if (!c || (c.voice && c.voice.url) || !(c.voice && c.voice.can)) return;
    let got;
    try { got = (await api("GET", `/api/characters/${c.id}`)).character; } catch (_) { return; }
    const i = S.list.findIndex((x) => x.id === c.id);
    if (i < 0 || !got) return;
    S.list[i] = Object.assign({}, S.list[i], got);
    paint();
    if (anyBusy()) startPoll();
    sayCurrent();
  }
  function startCreate() {
    if (S.mode === "create") return;
    S.pics = [];
    S.changing = false;
    S.styleOpen = false;
    S.styleDraft = "";
    snd("open");
    swapTo(() => {
      paintPics();
      const who = $(".cs-who");
      who.value = "";
      setMode("create");
      showError("");
      setTimeout(() => { try { who.focus({ preventScroll: true }); } catch (_) {} }, 40);
    });
  }
  async function primary() {
    if (S.busy) return;
    const c = current();
    if (S.mode === "create") return submitCreate();
    if (S.mode === "select" && S.styleOpen) return submitStyle();
    if (S.mode === "developing") {
      const w = S.list.find((x) => x.id === S.watching);
      if (w && w.status === "ready") return play(w);
      return;
    }
    if (!c) return;
    if (c.status === "ready") return play(c);
    if (c.status === "new" || c.status === "failed") return draw(c);
    if (c.status === "drawing") { S.watching = c.id; swapTo(() => setMode("developing")); startPoll(); }
  }
  function play(c) {
    remember(c.id);
    snd("start");
    if (typeof S.opts.onPlay === "function") S.opts.onPlay(c.id, c);
  }
  async function submitCreate() {
    const concept = $(".cs-who").value.trim();
    if (!concept && !S.pics.length) return;
    S.busy = true;
    paintActions();
    showError("");
    try {
      const res = await api("POST", "/api/characters", { concept, pictures: S.pics, style: S.styleDraft || "" });
      const c = res.character;
      S.list.unshift(c);
      S.sel = 0;
      S.watching = c.id;
      snd("submit");
      swapTo(() => setMode("developing"));
      startPoll();
    } catch (e) {
      showError("Could not start drawing them: " + (e.message || ""));
      snd("error");
    } finally {
      S.busy = false;
      paintActions();
    }
  }
  async function draw(c, concept) {
    S.busy = true;
    try {
      const res = await api("POST", `/api/characters/${c.id}/draw`, concept ? { concept } : {});
      const i = S.list.findIndex((x) => x.id === c.id);
      if (i >= 0) S.list[i] = res.character;
      S.watching = c.id;
      snd("submit");
      swapTo(() => setMode("developing"));
      startPoll();
    } catch (e) {
      showError("Could not draw them: " + (e.message || ""));
    } finally {
      S.busy = false;
    }
  }
  function tryAgain() {
    const c = current();
    if (!c || S.busy) return;
    S.fresh = "";
    draw(c);
  }
  function openChange() {
    S.changing = true;
    S.styleOpen = false;
    paintStyle();
    paintActions();
    setTimeout(() => { try { $(".cs-change-in").focus(); } catch (_) {} }, 40);
  }
  function closeChange() {
    S.changing = false;
    $(".cs-change-in").value = "";
    paintActions();
  }
  async function submitChange() {
    const c = current();
    const change = $(".cs-change-in").value.trim();
    if (!c || !change || S.busy) return;
    if (aboutTheVoice(change) && c.voice && c.voice.can) { changeVoice(c, change); return; }
    S.busy = true;
    try {
      const res = await api("POST", `/api/characters/${c.id}/revise`, { change });
      const i = S.list.findIndex((x) => x.id === c.id);
      if (i >= 0) S.list[i] = res.character;
      closeChange();
      S.watching = c.id;
      S.fresh = "";
      snd("submit");
      swapTo(() => setMode("developing"));
      startPoll();
    } catch (e) {
      showError("Could not change that: " + (e.message || ""));
    } finally {
      S.busy = false;
    }
  }
  async function surpriseMe() {
    const b = $(".cs-surprise");
    b.disabled = true;
    try {
      const res = await api("POST", "/api/characters/surprise", {});
      $(".cs-who").value = res.concept || "";
      paintActions();
      snd("select");
    } catch (e) {
      showError("No one came to mind: " + (e.message || ""));
    } finally {
      b.disabled = false;
    }
  }
  function addPictures(e) { readFiles(e.target.files); e.target.value = ""; }
  function readFiles(files) {
    Array.from(files || []).slice(0, 3 - S.pics.length).forEach((f) => {
      if (!/^image\//.test(f.type) || f.size > 8 * 1024 * 1024) return;
      const r = new FileReader();
      r.onload = () => { if (S.pics.length < 3) { S.pics.push(String(r.result)); paintPics(); paintActions(); } };
      r.readAsDataURL(f);
    });
  }
  function paintPics() {
    const box = $(".cs-pics");
    if (!box) return;
    box.innerHTML = "";
    S.pics.forEach((url, i) => {
      const f = el("figure", "cs-pic");
      const im = new Image(); im.src = url; im.alt = "";
      const x = el("button", "cs-pic-x", "×");
      x.type = "button";
      x.setAttribute("aria-label", "Remove picture");
      x.addEventListener("click", () => { S.pics.splice(i, 1); paintPics(); paintActions(); });
      f.appendChild(im); f.appendChild(x);
      box.appendChild(f);
    });
  }
  function back() {
    snd("menuClose");
    if (S.changing) { closeChange(); return; }
    if (S.styleOpen) { closeStyle(); return; }
    if (S.mode === "create" && S.list.length) { swapTo(() => setMode("select")); return; }
    if (S.mode === "developing") { S.watching = ""; S.revealing = false; swapTo(() => setMode("select")); return; }
    if (typeof S.opts.onBack === "function") S.opts.onBack();
    else hide();
  }

  // ── keys (StartMenu routes them here while the screen is up) ────────────
  function onKey(e) {
    if (!S.open) return false;
    const t = e.target;
    const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA");
    const k = e.key;
    if (typing && k !== "Escape") return true;
    if (k === "Escape") { e.preventDefault(); back(); return true; }
    if (S.mode === "select") {
      if (k === "ArrowDown" || k === "ArrowRight") { e.preventDefault(); pick(S.sel + 1); return true; }
      if (k === "ArrowUp" || k === "ArrowLeft") { e.preventDefault(); pick(S.sel - 1); return true; }
      if (k === "Enter") { e.preventDefault(); primary(); return true; }
      if (k === "n" || k === "N") { e.preventDefault(); startCreate(); return true; }
      if (k === "s" || k === "S") { e.preventDefault(); toggleStyle(); return true; }
      if ((k === "r" || k === "R") && current() && current().id === S.fresh) { e.preventDefault(); tryAgain(); return true; }
    } else if (S.mode === "create" || S.mode === "developing") {
      if (k === "Enter") { e.preventDefault(); primary(); return true; }
    }
    return true;
  }
  window.addEventListener("keydown", (e) => {
    // In a run (the editors' Change character) nothing else routes keys here.
    if (!S.open || !S.opts.inRun) return;
    if (onKey(e)) e.stopImmediatePropagation();
  }, true);

  function debug() {
    return {
      open: S.open, mode: S.mode, sel: S.sel, fresh: S.fresh, watching: S.watching,
      list: S.list.map((c) => ({ id: c.id, name: c.name, status: c.status, idle: !!c.idle,
                                 job: (c.job && c.job.stage) || "", finishing: !!c.finishing })),
      idleSrc: Fig && Fig.front ? (Fig.front.getAttribute("src") || "") : "",
      note: root && $(".cs-note") ? $(".cs-note").textContent : "",
      idlePainted: !!(Fig && Fig.front && Fig.front.classList.contains("on") && Fig.front.naturalWidth > 0),
      swapping: !!(root && root.classList.contains("cs-swapping")),
      primary: root ? $(".cs-primary").textContent : "",
      styleOpen: S.styleOpen, styleLine: root ? $(".cs-style-line").textContent : "",
    };
  }

  window.Characters = {
    show, hide, isOpen, onKey, selectedId, remember, prefetch,
    refresh: () => refresh(false),
    debug,
  };
})();
