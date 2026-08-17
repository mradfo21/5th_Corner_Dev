/* ============================================================
   SOMEWHERE // THE ORGANISM — the editor as four dots and a nucleus

   The first version of this graph mirrored the prompt file: every key in
   storage became a vertex, which is how it ended up with 37 nodes, 18 of them
   three levels down. Navigating your own game meant spelunking, and the things
   you actually art-direct were buried next to the engine's rulebook.

   So this is the whole editor now:

       ONE red dot — the game.
       Double-tap it and four dots bloom around it:
       Level · Character · Game · Controls.
       Tap one and its window comes up with the few fields that matter.

   Everything else — the engine's contract prompts, the runtime knobs, saved
   levels and builds — is still there, still editable, in the flat List behind
   the header toggle. It just stopped being the first thing you meet.

   The dots are sized to their own labels (a dot is a word with a circle round
   it, nothing more) and they never sit still: each is sprung to its slot on the
   ring, drifts on its own slow Lissajous figure, and pushes off any neighbour
   that comes too close. The composition holds; the surface breathes.

   This file owns no state of its own. Everything is read and written through
   WorldEditor's bridge (see `bridge` in standalone.js), so the dots and the
   flat list are two renderings of one truth.

   window.EditorGraph facade:
       init(bridge)   wire up once, at startup
       sync()         re-read the state and redraw (after any save/load)
       onEscape()     close the window, else collapse; false at the top
   ============================================================ */
(function () {
  "use strict";

  const SVG = "http://www.w3.org/2000/svg";

  // ── World units. One number sets the coordinate space; everything below is
  // relative to it, so the whole diagram scales with the viewBox and nothing
  // needs to know about pixels.
  const UNIT = 1000;
  const LABEL = UNIT * 0.052;        // label type size
  const PAD = UNIT * 0.030;          // ink between the text and its membrane
  const MIN_R = UNIT * 0.072;        // a one-word dot is still a dot
  // Average glyph width as a fraction of type size, for the UI font at these
  // sizes. Measuring in the DOM would be exact but forces layout on every
  // rebuild; being 5% out just changes the padding, which nobody can see.
  const GLYPH_W = 0.56;

  // How much room the view leaves around what it is framing. The collapsed
  // root has to read as "a small dot on a big sheet", so it gets a lot.
  const COLLAPSED_ZOOM = 4.6;
  const EXPANDED_PAD = 1.1;
  const ZOOM_MS = 620;
  const TAP_SLOP_PX = 26;
  const DBL_TAP_MS = 340;

  // ── Wobble. Small numbers on purpose: this should read as something alive
  // rather than something loose.
  const DRIFT = 0.055;               // Lissajous amplitude, × ring radius
  const DRIFT_HZ = 0.055;            // base frequency; each dot offsets from it
  const SPRING = 0.006;              // pull back to the slot
  const DAMP = 0.90;
  const REPEL_GAP = 1.22;            // start pushing at this multiple of r1+r2
  const REACH_GAP = 1.9;             // start pulling past this multiple
  const REPEL = 0.030;
  const ATTRACT = 0.012;

  let B = null;                      // the WorldEditor bridge
  let els = {};
  let root = null;
  let nodesById = Object.create(null);
  // The node whose children are on screen, or null for the collapsed root —
  // one red dot, nothing else.
  let openId = null;
  let sheetId = null;
  let view = null;                   // {cx, cy, half} currently on screen
  let anim = null;                   // in-flight zoom
  let raf = null;                    // wobble loop
  let t0 = 0;
  let lastTap = { t: 0, x: 0, y: 0, id: null };
  let hoverId = null;
  let hintTimer = null;

  function reduceMotion() {
    try { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; }
    catch (_) { return false; }
  }

  // ══════════════════════════════════════════════════════════════════
  // THE TREE — four things you can direct, and the game they belong to
  // ══════════════════════════════════════════════════════════════════
  // Which identity block each dot edits, in the order they ring the nucleus.
  // One short line each, and only when you are pointing at one. The editor is
  // not the place to explain the simulator; it is the place to direct it.
  const DOTS = [
    { id: "level", block: "setting_reference", label: "Level",
      sub: "Where this happens." },
    { id: "character", block: "player_character", label: "Character",
      sub: "Who you are in it." },
    { id: "game", block: "game_design", label: "Game",
      sub: "The kind of story this is." },
    { id: "controls", block: null, label: "Controls",
      sub: "How you see it and how you move." },
  ];

  // Does this sheet actually say anything yet? An empty sheet reaches no model,
  // so "has content" is the one piece of state a dot needs to show.
  function blockIsSet(blockId) {
    const spec = (B.identity() || {})[blockId] || {};
    const schema = B.identityBlock(blockId);
    if (!schema) return false;
    return (schema.fields || []).some((f) => {
      if (f.type === "toggle" || f.type === "mode") return false;
      const v = spec[f.id];
      return typeof v === "string" && v.trim() !== "";
    });
  }

  function dotRadius(label) {
    const w = String(label || "").length * GLYPH_W * LABEL;
    return Math.max(MIN_R, w / 2 + PAD);
  }

  function buildTree() {
    const kids = DOTS.map((d) => ({
      id: "dot:" + d.id,
      kind: d.block ? "spec" : "controls",
      label: d.label,
      sub: d.sub,
      block: d.block,
      set: d.block ? blockIsSet(d.block) : false,
      children: [],
    }));
    const tree = {
      id: "game",
      kind: "root",
      label: "Game",
      // No sub: the dot is already named, and "everything the simulation reads"
      // was the machinery introducing itself.
      sub: "",
      children: kids,
    };
    layout(tree);
    nodesById = Object.create(null);
    (function walk(n, parent) {
      n.parent = parent || null;
      nodesById[n.id] = n;
      (n.children || []).forEach((k) => walk(k, n));
    })(tree, null);
    return tree;
  }

  // ══════════════════════════════════════════════════════════════════
  // GEOMETRY — a nucleus, and its children on one ring
  //
  // Each dot is only as big as its own word. The ring is then sized so the
  // widest pair of neighbours still clears, which means the layout adapts if
  // the labels ever change without anyone tuning a constant.
  // ══════════════════════════════════════════════════════════════════
  function layout(node) {
    node.r = dotRadius(node.label);
    node.cx = 0;
    node.cy = 0;
    const kids = node.children || [];
    if (!kids.length) return;
    kids.forEach((k) => { k.r = dotRadius(k.label); });

    const maxR = kids.reduce((m, k) => Math.max(m, k.r), 0);
    // Two constraints: neighbours must not touch each other, and none may
    // swallow the nucleus. Take whichever ring is larger.
    const bySpan = kids.length > 1
      ? (maxR * REPEL_GAP) / Math.sin(Math.PI / kids.length)
      : maxR * 2;
    const byCore = node.r + maxR * 1.9;
    const ring = Math.max(bySpan, byCore);
    node.ring = ring;
    const top = -Math.PI / 2;                       // first dot at 12 o'clock
    kids.forEach((k, i) => {
      const a = top + (i / kids.length) * Math.PI * 2;
      k.slotX = Math.cos(a) * ring;
      k.slotY = Math.sin(a) * ring;
      k.cx = k.slotX;
      k.cy = k.slotY;
      k.vx = 0; k.vy = 0;
      // Each dot drifts on its own figure, so they never pulse in unison.
      k.phase = i * 1.7;
      k.wob = 0.82 + (i % 3) * 0.14;
    });
    node.slotX = 0; node.slotY = 0; node.vx = 0; node.vy = 0;
    node.phase = 0.4; node.wob = 0.7;
  }

  // ══════════════════════════════════════════════════════════════════
  // WOBBLE — spring to slot, drift, repel, lean together
  // ══════════════════════════════════════════════════════════════════
  function visibleNodes() {
    if (!root) return [];
    if (openId === null) return [root];
    const f = nodesById[openId] || root;
    return [f].concat(f.children || []);
  }

  function stepWobble(now) {
    const f = nodesById[openId] || root;
    const ring = f.ring || f.r * 3;
    const amp = ring * DRIFT;
    const t = (now - t0) / 1000;
    const live = visibleNodes();

    live.forEach((n) => {
      // The nucleus breathes a quarter as far as its satellites: enough that a
      // single dot alone on the sheet is alive, not enough to look adrift.
      const a = n === f ? amp * 0.25 : amp;
      const ax = n.slotX + Math.cos(t * DRIFT_HZ * 6.283 * n.wob + n.phase) * a;
      const ay = n.slotY + Math.sin(t * DRIFT_HZ * 6.283 * (n.wob * 1.37) + n.phase * 1.9) * a;
      n.vx += (ax - n.cx) * SPRING;
      n.vy += (ay - n.cy) * SPRING;
    });

    // Pairwise, and only pairwise: a dot pushes off a neighbour that comes
    // inside REPEL_GAP and reaches for one that has drifted past REACH_GAP.
    // The first version pulled every dot toward the group's centre of mass,
    // which is a force with nothing opposing it — the ring quietly collapsed
    // inward until the satellites were sitting on the nucleus.
    const orbit = live.filter((n) => n !== f);
    for (let i = 0; i < orbit.length; i++) {
      for (let j = i + 1; j < orbit.length; j++) {
        const a = orbit[i], b = orbit[j];
        let dx = b.cx - a.cx, dy = b.cy - a.cy;
        const d = Math.hypot(dx, dy) || 0.001;
        dx /= d; dy /= d;
        const near = (a.r + b.r) * REPEL_GAP;
        const far = (a.r + b.r) * REACH_GAP;
        let f2 = 0;
        if (d < near) f2 = -(near - d) * REPEL;
        else if (d > far) f2 = Math.min(d - far, far) * ATTRACT;
        else continue;
        a.vx += dx * f2; a.vy += dy * f2;
        b.vx -= dx * f2; b.vy -= dy * f2;
      }
    }
    // Nothing may sit on the nucleus. It doesn't move, so the shove is
    // one-sided.
    orbit.forEach((n) => {
      let dx = n.cx - f.cx, dy = n.cy - f.cy;
      const d = Math.hypot(dx, dy) || 0.001;
      const want = (n.r + f.r) * REPEL_GAP;
      if (d >= want) return;
      n.vx += (dx / d) * (want - d) * REPEL * 2;
      n.vy += (dy / d) * (want - d) * REPEL * 2;
    });

    live.forEach((n) => {
      n.vx *= DAMP; n.vy *= DAMP;
      n.cx += n.vx; n.cy += n.vy;
    });
  }

  function place() {
    visibleNodes().forEach((n) => {
      if (n.g) n.g.setAttribute("transform",
        "translate(" + n.cx.toFixed(2) + " " + n.cy.toFixed(2) + ")");
    });
    const f = nodesById[openId] || root;
    (f.children || []).forEach((k) => {
      if (!k.line) return;
      k.line.setAttribute("x1", f.cx.toFixed(2));
      k.line.setAttribute("y1", f.cy.toFixed(2));
      k.line.setAttribute("x2", k.cx.toFixed(2));
      k.line.setAttribute("y2", k.cy.toFixed(2));
    });
  }

  function loop(now) {
    if (!t0) t0 = now;
    stepWobble(now);
    place();
    raf = requestAnimationFrame(loop);
  }

  function startWobble() {
    if (raf || reduceMotion()) { place(); return; }
    t0 = 0;
    raf = requestAnimationFrame(loop);
  }

  function stopWobble() {
    if (raf) cancelAnimationFrame(raf);
    raf = null;
  }

  // ══════════════════════════════════════════════════════════════════
  // DRAWING — every dot is drawn at the origin and moved by a transform, so
  // the wobble costs one attribute per dot per frame.
  // ══════════════════════════════════════════════════════════════════
  function mk(name, attrs, cls) {
    const n = document.createElementNS(SVG, name);
    if (attrs) for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (cls) n.setAttribute("class", cls);
    return n;
  }

  function drawNode(n) {
    const g = mk("g", { "data-id": n.id }, "eg-node eg-kind-" + n.kind);
    g.appendChild(mk("circle", { cx: 0, cy: 0, r: n.r }, "eg-cell"));
    const t = mk("text", { x: 0, y: 0, "font-size": LABEL }, "eg-name");
    t.textContent = n.label;
    g.appendChild(t);
    n.g = g;
    return g;
  }

  function build() {
    if (!els.world) return;
    root = buildTree();
    els.world.innerHTML = "";
    hoverId = null;

    // Tethers under everything: the nucleus reaching out to each dot, which is
    // what makes this a graph rather than a menu in a circle.
    const wires = mk("g", null, "eg-tethers");
    (root.children || []).forEach((k) => {
      k.line = mk("line", { x1: 0, y1: 0, x2: 0, y2: 0 }, "eg-tether");
      wires.appendChild(k.line);
    });
    els.world.appendChild(wires);

    const layer = mk("g", null, "eg-nodes");
    layer.appendChild(drawNode(root));
    (root.children || []).forEach((k) => layer.appendChild(drawNode(k)));
    els.world.appendChild(layer);
  }

  // Per-node state relative to where you are. Recomputed on change, not per
  // frame — the frame loop only moves things.
  function paint() {
    const collapsed = openId === null;
    const f = nodesById[openId] || root;
    els.world.querySelectorAll(".eg-node").forEach((g) => {
      const n = nodesById[g.getAttribute("data-id")];
      if (!n) return;
      const isCore = n === f;
      g.classList.toggle("is-core", isCore && !collapsed);
      g.classList.toggle("is-alone", isCore && collapsed);
      g.classList.toggle("is-orbit", !isCore);
      g.classList.toggle("is-set", !!n.set);
      g.classList.toggle("is-open", n.id === sheetId);
      // Collapsed, the ring does not exist yet.
      g.style.display = (collapsed && !isCore) ? "none" : "";
    });
    els.world.querySelectorAll(".eg-tether").forEach((line) => {
      line.classList.toggle("is-live", !collapsed);
    });
    if (els.graph) els.graph.classList.toggle("is-collapsed", collapsed);
    renderHud();
    place();
  }

  // ══════════════════════════════════════════════════════════════════
  // ZOOM — one interpolated viewBox, scale in log space so the descent is even
  // ══════════════════════════════════════════════════════════════════
  function applyView(v) {
    view = v;
    const box = els.canvas.getBoundingClientRect();
    const w = box.width || 1, h = box.height || 1;
    const hx = w >= h ? v.half * (w / h) : v.half;
    const hy = h >= w ? v.half * (h / w) : v.half;
    els.canvas.setAttribute("viewBox",
      (v.cx - hx) + " " + (v.cy - hy) + " " + (hx * 2) + " " + (hy * 2));
  }

  function frameFor() {
    if (openId === null) {
      return { cx: 0, cy: 0, half: root.r * COLLAPSED_ZOOM };
    }
    const f = nodesById[openId] || root;
    const maxR = (f.children || []).reduce((m, k) => Math.max(m, k.r), f.r);
    return { cx: 0, cy: 0, half: ((f.ring || f.r * 3) + maxR) * EXPANDED_PAD };
  }

  function setZooming(on) {
    if (els.graph) els.graph.classList.toggle("is-zooming", !!on);
  }

  function frame(animate) {
    const to = frameFor();
    if (!view || !animate || reduceMotion()) {
      if (anim) { cancelAnimationFrame(anim.raf); anim = null; }
      setZooming(false);
      applyView(to);
      return;
    }
    const from = view;
    const start = performance.now();
    if (anim) cancelAnimationFrame(anim.raf);
    setZooming(true);
    const step = (now) => {
      const p = Math.min(1, (now - start) / ZOOM_MS);
      const e = p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2;
      applyView({
        cx: from.cx + (to.cx - from.cx) * e,
        cy: from.cy + (to.cy - from.cy) * e,
        half: Math.exp(Math.log(from.half) + (Math.log(to.half) - Math.log(from.half)) * e),
      });
      if (p < 1) anim = { raf: requestAnimationFrame(step) };
      else { anim = null; setZooming(false); }
    };
    anim = { raf: requestAnimationFrame(step) };
  }

  function setOpen(id, animate) {
    openId = id;
    frame(animate !== false);
    paint();
  }

  // ══════════════════════════════════════════════════════════════════
  // HUD
  // ══════════════════════════════════════════════════════════════════
  // Two lines: what you are looking at, and what it is for. A breadcrumb of one
  // item was the third thing saying "Game" on a screen with one dot on it.
  function renderHud() {
    const collapsed = openId === null;
    const f = nodesById[openId] || root;
    const shown = (hoverId && nodesById[hoverId]) || f;
    if (els.captionName) els.captionName.textContent = shown.label;
    if (els.captionSub) els.captionSub.textContent = shown.sub || "";
    // The one instruction worth printing is the one that isn't obvious: that
    // the dot opens. Four labelled dots explain themselves.
    if (els.hint && !hintTimer) {
      els.hint.textContent = collapsed ? "double-tap the dot" : "";
    }
  }

  function say(msg) {
    if (!els.hint) return;
    els.hint.textContent = msg;
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => { hintTimer = null; renderHud(); }, 2200);
  }

  // ══════════════════════════════════════════════════════════════════
  // INTERACTION
  // ══════════════════════════════════════════════════════════════════
  function toWorld(clientX, clientY) {
    const box = els.canvas.getBoundingClientRect();
    if (!box.width || !box.height) return null;
    const vb = els.canvas.viewBox.baseVal;
    if (!vb || !vb.width) return null;
    return {
      x: vb.x + (clientX - box.left) * (vb.width / box.width),
      y: vb.y + (clientY - box.top) * (vb.height / box.height),
    };
  }

  // Hit tested against LIVE positions, not the slots — the dots are moving, so
  // anything else would mean aiming at where a dot used to be.
  function hitAt(clientX, clientY) {
    const p = toWorld(clientX, clientY);
    if (!p) return null;
    let found = null;
    visibleNodes().forEach((n) => {
      if (Math.hypot(p.x - n.cx, p.y - n.cy) <= n.r) found = n;
    });
    if (found) return { node: found, where: found.id === (openId || root.id) ? "core" : "orbit" };
    return { node: null, where: "empty" };
  }

  function activate(n) {
    if ((n.children || []).length) { setOpen(n.id, true); return; }
    openSheet(n);
  }

  function onTap(evt) {
    const h = hitAt(evt.clientX, evt.clientY);
    if (!h) return;
    const id = h.node ? h.node.id : "";
    const now = performance.now();
    const isDouble = (now - lastTap.t) < DBL_TAP_MS &&
      Math.hypot(evt.clientX - lastTap.x, evt.clientY - lastTap.y) < TAP_SLOP_PX &&
      lastTap.id === id;
    lastTap = { t: now, x: evt.clientX, y: evt.clientY, id: id };

    // Empty paper is the way back out. With dots this small there is far more
    // of it than there is of them, which makes leaving easier than arriving —
    // the right way round.
    if (h.where === "empty") {
      if (openId !== null) setOpen(null, true);
      return;
    }
    if (h.where === "core") {
      // The nucleus toggles the ring. A double-tap is ONE intent, so the second
      // half of it is dropped — otherwise double-tapping opens the ring and
      // then instantly closes it again, which is what it did the first time.
      if (isDouble) return;
      setOpen(openId === null ? root.id : null, true);
      return;
    }
    // A dot is a window. The second half of a double-tap would just re-open the
    // one that is already up.
    if (isDouble && sheetId === h.node.id) return;
    activate(h.node);
  }

  function setHover(id, cursor) {
    els.canvas.style.cursor = cursor || "default";
    if (id === hoverId) return;
    hoverId = id;
    els.world.querySelectorAll(".eg-node.is-hover")
      .forEach((g) => g.classList.remove("is-hover"));
    if (!id) return;
    const g = els.world.querySelector('.eg-node[data-id="' + id + '"]');
    if (g) g.classList.add("is-hover");
  }

  function onHover(evt) {
    const h = hitAt(evt.clientX, evt.clientY);
    if (!h || h.where === "empty") {
      return setHover(null, openId === null ? "default" : "zoom-out");
    }
    if (h.where === "core") return setHover(h.node.id, openId === null ? "zoom-in" : "default");
    setHover(h.node.id, "pointer");
  }

  // ══════════════════════════════════════════════════════════════════
  // THE WINDOW — the few fields that steer this dot, and nothing else
  // ══════════════════════════════════════════════════════════════════
  function openSheet(n) {
    sheetId = n.id;
    // The window names itself; the HUD behind it should go back to saying where
    // you are rather than echoing the title through the scrim.
    hoverId = null;
    els.sheet.classList.add("is-open");
    els.sheet.setAttribute("aria-hidden", "false");
    els.scrim.classList.add("is-open");
    els.graph.classList.add("has-sheet");
    els.sheetTitle.textContent = n.label;
    els.sheetBody.innerHTML = "";
    els.sheetBody.scrollTop = 0;
    if (n.kind === "controls") sheetControls(els.sheetBody);
    else sheetSpec(n, els.sheetBody);
    paint();
  }

  function closeSheet() {
    if (!sheetId) return false;
    sheetId = null;
    els.sheet.classList.remove("is-open");
    els.sheet.setAttribute("aria-hidden", "true");
    els.scrim.classList.remove("is-open");
    els.graph.classList.remove("has-sheet");
    // Both strips are real, wired elements on loan from the panel; they have to
    // go home or their listeners leave with the innerHTML.
    try { B.unmountInputControls(); } catch (_) {}
    try { B.unmountPanelControls(); } catch (_) {}
    els.sheetBody.innerHTML = "";
    paint();
    return true;
  }

  // One sheet is the identity form itself, mounted minimal: the essential
  // fields, their placeholders, and nothing to read.
  function sheetSpec(n, body) {
    const host = document.createElement("div");
    host.className = "eg-spec";
    body.appendChild(host);
    B.renderSpecInto(host, n.block, { minimal: true });
  }

  // The other is every knob that isn't the world: how the camera sees, how you
  // move, and how this panel reads. Three quiet groups rather than three
  // windows — and the last two are the panel's own wired elements on loan, so
  // the header above is left with nothing but the way out.
  function sheetControls(body) {
    const host = document.createElement("div");
    host.className = "eg-spec";
    body.appendChild(host);
    B.renderSpecInto(host, "camera_perspective", { minimal: true });
    try { B.mountInputControls(group(body, "Movement")); } catch (_) {}
    try { B.mountPanelControls(group(body, "Panel")); } catch (_) {}
  }

  function group(body, label) {
    const wrap = document.createElement("div");
    wrap.className = "eg-group";
    const h = document.createElement("div");
    h.className = "we-cast-label";
    h.textContent = label;
    wrap.appendChild(h);
    body.appendChild(wrap);
    return wrap;
  }

  // ══════════════════════════════════════════════════════════════════
  // LIFECYCLE
  // ══════════════════════════════════════════════════════════════════
  function sync() {
    if (!B || !els.world) return;
    // Switching to the flat list has to give the CONTROLS strip back, or it
    // leaves with the window it was borrowed into.
    if (!B.isGraphMode()) { stopWobble(); closeSheet(); return; }
    const openSheetId = sheetId;
    const active = document.activeElement;
    const typing = active && (active.tagName === "TEXTAREA" || active.tagName === "INPUT") &&
      els.sheetBody.contains(active);
    // Don't tear down a window the player is typing in; the dots behind it can
    // wait for the next save.
    if (openSheetId && typing) return;
    if (openSheetId) {
      try { B.unmountInputControls(); } catch (_) {}
      try { B.unmountPanelControls(); } catch (_) {}
    }
    build();
    if (openId !== null && !nodesById[openId]) openId = root.id;
    frame(false);
    paint();
    startWobble();
    if (openSheetId && nodesById[openSheetId]) openSheet(nodesById[openSheetId]);
    else if (openSheetId) closeSheet();
  }

  function onEscape() {
    if (closeSheet()) return true;
    if (openId !== null) { setOpen(null, true); return true; }
    return false;
  }

  function init(bridge) {
    B = bridge;
    els = {
      graph: document.getElementById("we-graph"),
      canvas: document.getElementById("eg-canvas"),
      world: document.getElementById("eg-world"),
      captionName: document.getElementById("eg-caption-name"),
      captionSub: document.getElementById("eg-caption-sub"),
      hint: document.getElementById("eg-hint"),
      sheet: document.getElementById("eg-sheet"),
      scrim: document.getElementById("eg-scrim"),
      sheetTitle: document.getElementById("eg-sheet-title"),
      sheetBody: document.getElementById("eg-sheet-body"),
      sheetClose: document.getElementById("eg-sheet-close"),
    };
    if (!els.canvas || !els.world) return;

    els.canvas.addEventListener("click", onTap);
    els.canvas.addEventListener("dblclick", (e) => e.preventDefault());
    els.canvas.addEventListener("mousemove", onHover);
    els.canvas.addEventListener("mouseleave", () => setHover(null));
    if (els.sheetClose) els.sheetClose.addEventListener("click", closeSheet);
    if (els.scrim) els.scrim.addEventListener("click", closeSheet);
    window.addEventListener("resize", () => { if (view) applyView(view); });
    // A hidden panel doesn't need a frame loop running behind it.
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stopWobble();
      else if (B.isGraphMode()) startWobble();
    });
  }

  window.EditorGraph = {
    init: init,
    sync: sync,
    onEscape: onEscape,
    isSheetOpen: () => !!sheetId,
    // Where you are: null while collapsed, else the open node's id.
    openId: () => openId,
    // What a tap at these client coordinates would land on, without clicking.
    probe: (x, y) => {
      const h = hitAt(x, y);
      return h ? { id: h.node ? h.node.id : null, where: h.where } : null;
    },
    // Live centre of a dot in client pixels — the dots move, so tests and the
    // console need to ask where one is right now.
    dotAt: (id) => {
      const n = nodesById[id];
      const box = els.canvas && els.canvas.getBoundingClientRect();
      const vb = els.canvas && els.canvas.viewBox.baseVal;
      if (!n || !box || !vb || !vb.width) return null;
      return {
        x: box.left + (n.cx - vb.x) * (box.width / vb.width),
        y: box.top + (n.cy - vb.y) * (box.height / vb.height),
        r: n.r * (box.width / vb.width),
      };
    },
  };
})();
