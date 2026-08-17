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
  const LABEL = UNIT * 0.038;        // label type size — the SAME everywhere
  // ONE radius for every dot at every depth. Sizing each dot to its own word
  // was honest to "just big enough to cover the text" and looked like a bag of
  // different coins: LEVEL a marble, CHARACTER a saucer. A set of identical
  // circles is the thing that reads as designed, so the radius is fixed to fit
  // the longest label we allow (nine characters, upper case) and every dot gets
  // it — satellites and nucleus alike.
  const DOT_R = LABEL * 4.2;
  // Upper-case glyphs in the UI font, as a fraction of type size, including
  // tracking. Measuring in the DOM would be exact but forces layout on every
  // rebuild; being a few percent out only changes the breathing room.
  const GLYPH_W = 0.75;              // includes the tracking in .eg-name
  const MAX_CHARS = 9;               // past this a label is too long to be a dot

  // How much room the view leaves around what it is framing. The collapsed
  // root has to read as "a small dot on a big sheet", so it gets a lot.
  const COLLAPSED_ZOOM = 4.6;
  const EXPANDED_PAD = 1.1;
  const ZOOM_MS = 620;

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
  // The tree. `block` means "this dot is that identity sheet"; `kind` picks the
  // window builder; children make a dot a container you dive into.
  //
  // One short line of sub each, shown only when you are pointing at it. The
  // editor is not the place to explain the simulator; it is the place to direct
  // it. Every label is at most nine characters, because a dot is a word.
  const DOTS = [
    { id: "level", kind: "spec", block: "setting_reference", label: "Level",
      sub: "Where this happens." },
    { id: "character", kind: "spec", block: "player_character", label: "Character",
      sub: "Who you are in it." },
    {
      id: "game", kind: "group", label: "Game", sub: "The game itself.",
      children: [
        { id: "story", kind: "spec", block: "game_design", label: "Story",
          sub: "Genre, tone, what threatens you." },
        {
          id: "mechanics", kind: "group", label: "Mechanics",
          sub: "The things the game can do.",
          children: [
            { id: "camera", kind: "spec", block: "camera_perspective", label: "Camera",
              sub: "Where the camera stands." },
            { id: "scan", kind: "scan", label: "Scan",
              sub: "Finding objects in the frame." },
            { id: "camp", kind: "camp", label: "Camp",
              sub: "Who is at the fire with you." },
            { id: "npc", kind: "npc", label: "NPC",
              sub: "Talking to the people in it." },
          ],
        },
        {
          id: "models", kind: "group", label: "Models",
          sub: "What generates the world.",
          children: [
            { id: "world", kind: "world", label: "World", sub: "The live world model." },
            { id: "image", kind: "image", label: "Image", sub: "What draws the stills." },
            { id: "voice", kind: "voice", label: "Voice", sub: "Who speaks, and how." },
          ],
        },
        { id: "controls", kind: "controls", label: "Controls",
          sub: "How you see it and how you move." },
      ],
    },
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

  // Every dot is the same size; the only question is whether the word fits, and
  // a label that doesn't is a naming problem, not a layout one.
  function dotRadius() { return DOT_R; }

  function buildTree() {
    const spec = (d) => ({
      id: "dot:" + d.id,
      kind: d.kind || "spec",
      label: d.label,
      sub: d.sub,
      block: d.block || null,
      // The one piece of state a dot carries: is this steering the game? Only
      // sheets can answer it; a container inherits it from what's inside.
      set: d.block ? blockIsSet(d.block) : false,
      children: (d.children || []).map(spec),
    });
    const kids = DOTS.map(spec);
    (function inherit(n) {
      (n.children || []).forEach(inherit);
      if ((n.children || []).length) n.set = n.children.some((k) => k.set);
    })({ children: kids });
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
  // Every container lays its children out on a ring around the ORIGIN, because
  // whichever node is open is always the one in the middle. So a node has two
  // possible homes — (0,0) when it is the nucleus, and its slot in its parent's
  // ring when it is a satellite — and stepWobble picks by role. Laying the whole
  // tree out in one absolute space is what the first version did, and it left
  // everything below the second level with no coordinates at all.
  function layout(node) {
    node.r = dotRadius();
    node.cx = 0;
    node.cy = 0;
    node.slotX = 0;
    node.slotY = 0;
    node.vx = 0;
    node.vy = 0;
    node.phase = node.phase || 0.4;
    node.wob = node.wob || 0.7;
    const kids = node.children || [];
    kids.forEach(layout);
    if (!kids.length) return;

    const maxR = DOT_R;
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
      // Each dot drifts on its own figure, so they never pulse in unison.
      k.phase = i * 1.7;
      k.wob = 0.82 + (i % 3) * 0.14;
    });
  }

  // Where a node belongs right now: the middle if it is the one you are inside,
  // otherwise its slot in that node's ring.
  function homeOf(n, focus) {
    return n === focus ? { x: 0, y: 0 } : { x: n.slotX, y: n.slotY };
  }

  // Put the stage where it belongs before it blooms, so dots don't slide in
  // from wherever they were last time they were on screen.
  function seat() {
    const f = nodesById[openId] || root;
    visibleNodes().forEach((n) => {
      const h = homeOf(n, f);
      n.cx = h.x; n.cy = h.y; n.vx = 0; n.vy = 0;
    });
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
      const h = homeOf(n, f);
      const ax = h.x + Math.cos(t * DRIFT_HZ * 6.283 * n.wob + n.phase) * a;
      const ay = h.y + Math.sin(t * DRIFT_HZ * 6.283 * (n.wob * 1.37) + n.phase * 1.9) * a;
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

  // Two nested groups per dot, because two different things want to write to
  // `transform`: the outer one is moved by the wobble loop every frame, the
  // inner one is scaled by CSS for the bloom, the wilt and the idle breath. One
  // group for both meant whichever ran last won, and the dots either stopped
  // drifting or never arrived.
  function drawNode(n, index) {
    const g = mk("g", { "data-id": n.id }, "eg-node eg-kind-" + n.kind);
    const inner = mk("g", null, "eg-node-in");
    // Stagger, so the ring blooms as a sequence rather than a flashbulb.
    inner.style.setProperty("--i", String(index || 0));
    inner.appendChild(mk("circle", { cx: 0, cy: 0, r: n.r }, "eg-cell"));
    const t = mk("text", { x: 0, y: 0, "font-size": LABEL }, "eg-name");
    t.textContent = n.label;
    inner.appendChild(t);
    g.appendChild(inner);
    n.g = g;
    n.gi = inner;
    return g;
  }

  function build() {
    if (!els.world) return;
    root = buildTree();
    els.world.innerHTML = "";
    hoverId = null;
    // No tethers. Spokes from the middle to every satellite drew the one
    // relationship you can already see (these things are inside that thing) and
    // turned a constellation into a wheel.
    const layer = mk("g", null, "eg-nodes");
    (function walk(n, index) {
      layer.appendChild(drawNode(n, index));
      (n.children || []).forEach(walk);
    })(root, 0);
    els.world.appendChild(layer);
  }

  // Per-node state relative to where you are. Recomputed on change, not per
  // frame — the frame loop only moves things.
  //
  // A dot that is leaving is still on screen: it has to shrink back into the
  // nucleus first. So visibility is three states, not two, and `leaving` holds
  // the outgoing set until its animation is done.
  let leaving = Object.create(null);
  let leaveTimer = null;

  function paint() {
    const collapsed = openId === null;
    const f = nodesById[openId] || root;
    const here = Object.create(null);
    visibleNodes().forEach((n) => { here[n.id] = true; });

    els.world.querySelectorAll(".eg-node").forEach((g) => {
      const n = nodesById[g.getAttribute("data-id")];
      if (!n) return;
      const isCore = n === f;
      const onStage = !!here[n.id];
      const isLeaving = !onStage && !!leaving[n.id];
      g.classList.toggle("is-core", isCore && !collapsed);
      g.classList.toggle("is-alone", isCore && collapsed);
      g.classList.toggle("is-orbit", onStage && !isCore);
      g.classList.toggle("is-set", !!n.set);
      g.classList.toggle("is-open", n.id === sheetId);
      g.classList.toggle("is-leaving", isLeaving);
      g.style.display = (onStage || isLeaving) ? "" : "none";
    });
    if (els.graph) els.graph.classList.toggle("is-collapsed", collapsed);
    renderHud();
    place();
  }

  // Whatever was on stage and isn't any more gets to wilt, then disappear.
  function beginLeave(previous) {
    const keep = Object.create(null);
    visibleNodes().forEach((n) => { keep[n.id] = true; });
    leaving = Object.create(null);
    previous.forEach((n) => { if (!keep[n.id]) leaving[n.id] = true; });
    clearTimeout(leaveTimer);
    leaveTimer = setTimeout(() => {
      leaving = Object.create(null);
      paint();
    }, reduceMotion() ? 0 : 380);
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

  function depthOf(id) {
    let d = 0;
    for (let n = nodesById[id]; n && n.parent; n = n.parent) d++;
    return id === null ? -1 : d;
  }

  function setOpen(id, animate) {
    const before = visibleNodes();
    const up = depthOf(id) < depthOf(openId);
    openId = id;
    beginLeave(before);
    seat();
    frame(animate !== false);
    paint();
    // Arriving dots replay their bloom. Going DOWN, the old ring is already
    // gone and they can come straight in; going UP, the outgoing ring is still
    // shrinking through the same space, and both at once reads as a scramble —
    // so the arrival waits for the departure to clear.
    bloom(visibleNodes().filter((n) => before.indexOf(n) === -1), up ? 210 : 0);
  }

  // One level up: the nucleus is where you came from, and so is the paper.
  function surface() {
    if (openId === null) return false;
    const f = nodesById[openId];
    setOpen(f && f.parent ? f.parent.id : null, true);
    return true;
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

  // Two clicks inside this window are ONE gesture. Everything here moves the
  // stage — opening a ring re-seats every dot on screen — so the second half of
  // a double-tap lands on whatever has slid under the cursor since. That is how
  // double-tapping used to open a ring and instantly close it again, and then
  // how diving two levels bounced straight back to the top.
  const ACT_DEBOUNCE_MS = 300;
  let lastAct = 0;

  function acted() { lastAct = performance.now(); }

  function onTap(evt) {
    if (performance.now() - lastAct < ACT_DEBOUNCE_MS) return;
    const h = hitAt(evt.clientX, evt.clientY);
    if (!h) return;

    // Empty paper is the way back out, one level at a time. With dots this
    // small there is far more of it than there is of them, which makes leaving
    // easier than arriving — the right way round.
    if (h.where === "empty") { acted(); surface(); return; }
    // The nucleus opens the ring, and once open it is the way back up.
    if (h.where === "core") {
      acted();
      if (openId === null) setOpen(root.id, true);
      else surface();
      return;
    }
    acted();
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
    // Named `fill`, not `build` — the module already has a build() that draws
    // the dots, and shadowing it here is a trap for the next reader.
    const fill = {
      spec: sheetSpec,
      controls: sheetControls,
      scan: sheetScan,
      camp: sheetCamp,
      npc: sheetNpc,
      world: sheetWorld,
      image: sheetImage,
      voice: sheetVoice,
    }[n.kind] || sheetSpec;
    fill(n, els.sheetBody);
    paint();
  }

  // ── Window furniture ──────────────────────────────────────────────────
  // Status panels are not settings, and shouldn't pretend to be: a row is a
  // label and a value, and a value that means "broken" says so in red.
  function statusRow(host, label, value, bad) {
    const row = document.createElement("div");
    row.className = "eg-stat" + (bad ? " is-bad" : "");
    const k = document.createElement("span");
    k.className = "eg-stat-k";
    k.textContent = label;
    const v = document.createElement("span");
    v.className = "eg-stat-v";
    v.textContent = value == null || value === "" ? "—" : String(value);
    row.appendChild(k);
    row.appendChild(v);
    host.appendChild(row);
    return row;
  }

  function note(host, text) {
    if (!text) return null;
    const p = document.createElement("p");
    p.className = "eg-note";
    p.textContent = text;
    host.appendChild(p);
    return p;
  }

  // A row of choices, one of them current. Used for every model picker, so they
  // all behave the same way and none of them invents a new idiom.
  function picker(host, options, currentId, onPick) {
    const grid = document.createElement("div");
    grid.className = "we-mode-grid";
    options.forEach((o) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "we-mode" + (o.id === currentId ? " active" : "");
      b.dataset.value = o.id;
      const nm = document.createElement("span");
      nm.className = "we-mode-name";
      nm.textContent = o.label;
      b.appendChild(nm);
      if (o.tag) {
        const t = document.createElement("span");
        t.className = "we-mode-tag";
        t.textContent = o.tag;
        b.appendChild(t);
      }
      b.addEventListener("click", () => onPick(o.id, b));
      grid.appendChild(b);
    });
    host.appendChild(grid);
    return grid;
  }

  async function getJson(url) {
    try {
      const r = await fetch(url, { headers: { Accept: "application/json" } });
      if (!r.ok) return null;
      const body = await r.json();
      return (body && body.data) || body;
    } catch (_) { return null; }
  }

  // ── Mechanics ─────────────────────────────────────────────────────────
  // SCAN's backend is chosen by an environment variable on the server, so there
  // is nothing here to set. What there IS to know is which detector answered and
  // why the fast one didn't load — which until now only existed in a boot log.
  function sheetScan(n, body) {
    const host = group(body, "Detector");
    statusRow(host, "Backend", "…");
    getJson("/api/health").then((h) => {
      host.innerHTML = "";
      const d = (h && h.detect) || {};
      const local = d.local || {};
      const onDevice = d.backend === "local" || d.backend === "auto";
      statusRow(host, "Backend", d.backend || "unknown");
      statusRow(host, "On device", local.available ? "ready" : "unavailable",
                onDevice && !local.available);
      if (local.min_score != null) statusRow(host, "Min score", local.min_score);
      if (local.timeouts) statusRow(host, "Timeouts", local.timeouts, true);
      if (local.breaker_open) statusRow(host, "Breaker", "open", true);
      // A stack-trace fragment is not a value in a two-column row; it needs its
      // own block, in the typeface it came from.
      if (local.error) {
        const pre = document.createElement("pre");
        pre.className = "eg-err";
        pre.textContent = local.error;
        host.appendChild(pre);
      }
      note(body, onDevice && !local.available
        ? "The on-device detector didn't load, so SCAN is asking the image model instead. Slower, and it costs a call per scan."
        : "SCAN reads the frame on the box. Nothing to tune from here — the backend is set on the server.");
    });
  }

  function sheetCamp(n, body) {
    const host = group(body, "At the fire");
    getJson("/api/companions").then((c) => {
      host.innerHTML = "";
      const roster = (c && (c.companions || c.roster)) || [];
      if (!roster.length) {
        note(body, "Nobody yet. Companions join at a camp moment, and each one " +
                   "keeps the portrait and voice it was given.");
        return;
      }
      roster.forEach((p) => statusRow(host, p.name || p.slug || "someone",
                                      p.voice_id ? "has a voice" : "no voice yet",
                                      !p.voice_id));
    });
  }

  function sheetNpc(n, body) {
    const host = group(body, "Conversation");
    getJson("/api/health").then((h) => {
      host.innerHTML = "";
      const t = (h && h.talk) || {};
      statusRow(host, "Voice", t.voice ? "ready" : "text only", !t.voice);
      statusRow(host, "Agent", t.agent ? "set" : "missing", !t.agent);
      statusRow(host, "API key", t.api_key ? "set" : "not set", false);
      statusRow(host, "Overrides", t.overrides ? "on" : "off", false);
      statusRow(host, "Designed voices", t.designed_voices || 0);
      if (t.reason && t.reason !== "ready") note(body, t.reason);
    });
    // The narrator is a live button; the graph proxies to it rather than owning
    // a second copy of its state.
    const btn = (B.engineControls() || [])
      .reduce((all, g) => all.concat(g.buttons), [])
      .find((b) => b.id === "narrator-btn");
    if (btn) {
      const acts = group(body, "Narrator");
      const b = document.createElement("button");
      b.type = "button";
      b.className = "we-btn";
      b.textContent = "Speak the last line";
      b.addEventListener("click", () => { try { btn.click(); } catch (_) {} });
      acts.appendChild(b);
    }
  }

  // ── Models ────────────────────────────────────────────────────────────
  function sheetWorld(n, body) {
    const host = group(body, "World model");
    getJson("/api/reactor/config").then((cfg) => {
      host.innerHTML = "";
      const models = (cfg && cfg.available_models) || [];
      const live = (function () {
        try { return window.ReactorRenderer.currentModel(); } catch (_) { return null; }
      })() || (cfg && cfg.world_model);
      if (!models.length) { note(host, "No world models advertised by the server."); return; }
      picker(host, models.map((m) => ({
        id: m.id || m,
        label: m.label || m.name || m.id || m,
        tag: m.note || "",
      })), live, (id) => {
        try { window.ReactorRenderer.setModel(id); B.toast("Switching world model…"); }
        catch (_) { B.toast("Couldn't switch the world model.", "warn"); }
        setTimeout(() => { if (sheetId === n.id) openSheet(n); }, 900);
      });
      // A model that shipped after this build. The server says whether it will
      // accept a name it hasn't advertised.
      if (cfg && cfg.allow_custom_models) {
        const form = document.createElement("form");
        form.className = "eg-name-form";
        form.autocomplete = "off";
        const input = document.createElement("input");
        input.type = "text";
        input.placeholder = "another model id\u2026";
        input.maxLength = 64;
        const go = document.createElement("button");
        go.type = "submit";
        go.className = "we-btn we-btn-primary";
        go.textContent = "Use";
        form.appendChild(input);
        form.appendChild(go);
        form.addEventListener("submit", (e) => {
          e.preventDefault();
          const raw = input.value.trim();
          if (!raw) { input.focus(); return; }
          try {
            window.ReactorRenderer.addModel(raw, raw);
            window.ReactorRenderer.setModel(raw);
            B.toast("Trying " + raw + "\u2026");
          } catch (_) { B.toast("Couldn't use that model.", "warn"); }
          setTimeout(() => { if (sheetId === n.id) openSheet(n); }, 900);
        });
        host.appendChild(form);
      }
    });
    // Adventure or Director — only while the model that has it is live.
    const seg = B.experienceSeg && B.experienceSeg();
    if (seg) { try { B.borrow(seg, group(body, "Experience")); } catch (_) {} }
    const health = group(body, "Right now");
    getJson("/api/reactor/health").then((h) => {
      health.innerHTML = "";
      if (!h) { statusRow(health, "Realtime", "unknown", true); return; }
      statusRow(health, "Realtime", h.ok ? "ready" : REASONS[h.reason] || h.reason, !h.ok);
      if (!h.ok && h.detail) note(health, h.detail);
    });
  }

  // The endpoint answers in machine tokens. `no_api_key` on screen is the server
  // talking to itself.
  const REASONS = {
    no_api_key: "not configured",
    bad_api_key: "key rejected",
    rate_limited: "rate limited",
    token_exchange_failed: "handshake failed",
    unreachable: "can't reach it",
    ready: "ready",
  };

  function sheetImage(n, body) {
    const host = group(body, "Image model");
    getJson("/api/ai/config").then((cfg) => {
      host.innerHTML = "";
      const presets = (cfg && cfg.presets) || [];
      const active = cfg && cfg.active_preset;
      if (!presets.length) { note(host, "No image presets configured."); return; }
      picker(host, presets.map((p) => ({
        id: p.name || p.id,
        label: p.label || p.name || p.id,
        tag: p.image_model || p.note || "",
      })), active, async (id) => {
        try {
          const r = await fetch("/api/ai/switch", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ preset: id }),
          });
          B.toast(r.ok ? "Image model switched." : "Couldn't switch that.", r.ok ? "" : "warn");
        } catch (_) { B.toast("Couldn't switch that.", "warn"); }
        if (sheetId === n.id) openSheet(n);
      });
    });
  }

  function sheetVoice(n, body) {
    const host = group(body, "Conversation");
    getJson("/api/health").then((h) => {
      host.innerHTML = "";
      const t = (h && h.talk) || {};
      statusRow(host, "Agent", t.agent ? "set" : "missing", !t.agent);
      statusRow(host, "Designed voices", t.designed_voices || 0);
      if (!t.agent) note(host, "Without an ElevenLabs agent, everyone is on text.");
    });
    // The actual catalogue, not a count of it: voices.json is what a character
    // can be cast from, and the default and narrator are picked out of it.
    const cast = group(body, "Cast");
    getJson("/api/talk/voices").then((v) => {
      cast.innerHTML = "";
      const list = (v && v.voices) || [];
      if (!list.length) { note(cast, "No voices in the registry."); return; }
      list.forEach((entry) => {
        const marks = [];
        if (entry.id === (v && v.default)) marks.push("default");
        if (entry.id === (v && v.narrator)) marks.push("narrator");
        const row = statusRow(cast, entry.name || entry.id,
                              marks.length ? marks.join(" \u00B7 ") : (entry.tag || entry.gender || ""));
        if (marks.length) row.classList.add("is-now");
      });
      note(cast, "A character is given one the first time they speak, and keeps it.");
    });
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
    try { B.giveBack(); } catch (_) {}
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
  function sheetControls(n, body) {
    try { B.mountInputControls(group(body, "Movement")); } catch (_) {}
    // The bindings themselves. They're fixed per scheme rather than remappable,
    // so this is a reference card — but "which key does what" was information
    // you could only get by reading PROFILES in the source.
    const keys = group(body, "Keys");
    const bindings = B.inputBindings ? B.inputBindings() : null;
    if (bindings) {
      bindings.list.forEach((p) => {
        statusRow(keys, p.label || p.id, p.hint || "")
          .classList.toggle("is-now", p.id === bindings.current);
      });
      statusRow(keys, "Look", "arrow keys, or drag the world in FPS");
      statusRow(keys, "Editor", "` opens it \u00B7 Esc goes back");
    }
    try { B.mountPanelControls(group(body, "Panel")); } catch (_) {}
  }

  // Returns the CONTENT of the group, not the group: every async panel in here
  // clears itself when the fetch lands, and returning the wrapper meant they all
  // deleted their own heading on the way in.
  function group(body, label) {
    const wrap = document.createElement("div");
    wrap.className = "eg-group";
    const h = document.createElement("div");
    h.className = "we-cast-label";
    h.textContent = label;
    wrap.appendChild(h);
    const inner = document.createElement("div");
    inner.className = "eg-group-body";
    wrap.appendChild(inner);
    body.appendChild(wrap);
    return inner;
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
      try { B.giveBack(); } catch (_) {}
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
    return surface();
  }

  // Opening the panel is an arrival too: the dot blooms in rather than being
  // there already. Sync doesn't do this, or every save would replay it.
  function onOpen() {
    if (!B || !B.isGraphMode() || !root) return;
    bloom(visibleNodes());
    startWobble();
  }

  // Restarting a CSS animation needs the class off for a frame, or the browser
  // sees no change and does nothing.
  function bloom(nodes, waitMs) {
    if (!nodes.length) return;
    nodes.forEach((n) => { if (n.gi) n.gi.classList.remove("is-blooming"); });
    const go = () => nodes.forEach((n) => { if (n.gi) n.gi.classList.add("is-blooming"); });
    if (waitMs && !reduceMotion()) setTimeout(go, waitMs);
    else requestAnimationFrame(go);
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
    onOpen: onOpen,
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
