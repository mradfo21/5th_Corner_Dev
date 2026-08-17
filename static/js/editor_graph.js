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
  const LABEL = UNIT * 0.038;        // label type size at the top level
  // ONE radius per level, for every dot on it. Sizing each dot to its own word
  // looked like a bag of different coins: LEVEL a marble, CHARACTER a saucer.
  const DOT_R = LABEL * 4.2;
  // ...and each level down is a step smaller, type and all. The view no longer
  // rescales to fit the ring (see VIEW_HALF), so this is visible as depth: the
  // game is the biggest thing there is, and a knob three levels inside it is a
  // detail. Without it, every level rendered at exactly the same size and the
  // whole tree read as flat.
  const DEPTH_SHRINK = 0.9;
  // Upper-case glyphs in the UI font, as a fraction of type size, including
  // tracking. Measuring in the DOM would be exact but forces layout on every
  // rebuild; being a few percent out only changes the breathing room.
  const GLYPH_W = 0.75;              // includes the tracking in .eg-name
  const MAX_CHARS = 9;               // past this a label is too long to be a dot

  // How much room the view leaves around what it is framing. The collapsed
  // root has to read as "a small dot on a big sheet", so it gets a lot.
  const COLLAPSED_ZOOM = 4.6;
  // The expanded view is a FIXED window on the world, the same at every depth.
  // It used to be fitted to whichever ring was open, which silently cancelled
  // the depth shrink: smaller dots in a proportionally smaller frame render at
  // exactly the same size on screen.
  const VIEW_HALF = DOT_R * 3.9;
  // The enclosure you are inside. WIDER than the frame's short axis on purpose:
  // its left and right run off the edge and only the top and bottom arcs curve
  // through the view, which is what makes it read as a wall you are inside
  // rather than a ring drawn around the dots.
  const SHELL_R = VIEW_HALF * 1.3;
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

  // One size per level, a step smaller each level down.
  function scaleAt(depth) { return Math.pow(DEPTH_SHRINK, depth); }
  function dotRadius(depth) { return DOT_R * scaleAt(depth); }

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
  function layout(node, depth) {
    depth = depth || 0;
    node.depth = depth;
    // A node's own size is the size of the level it LIVES on; its children are
    // one step smaller, and its ring is sized for them.
    node.r = dotRadius(depth);
    node.label_size = LABEL * scaleAt(depth);
    node.cx = 0;
    node.cy = 0;
    node.slotX = 0;
    node.slotY = 0;
    node.vx = 0;
    node.vy = 0;
    node.phase = node.phase || 0.4;
    node.wob = node.wob || 0.7;
    const kids = node.children || [];
    kids.forEach((k) => layout(k, depth + 1));
    if (!kids.length) return;

    const maxR = dotRadius(depth + 1);
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
    const t = mk("text", { x: 0, y: 0, "font-size": n.label_size || LABEL }, "eg-name");
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
    // The enclosure. A single faint circle wider than the frame, so on a tall
    // panel you see its top and bottom arcs and read the whole view as the
    // inside of something. Without it the dots floated on an unbounded sheet and
    // there was no hierarchy to feel, only labels to read.
    els.world.appendChild(mk("circle", { cx: 0, cy: 0, r: SHELL_R }, "eg-shell"));

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
      g.classList.toggle("is-leaving", isLeaving);
      g.style.display = (onStage || isLeaving) ? "" : "none";
      // A dot on its way out keeps the face it had. Stripping is-core the
      // instant you dive turned the outgoing nucleus from red to white WHILE it
      // was shrinking, which read as the glow popping rather than receding.
      if (isLeaving) return;
      g.classList.toggle("is-core", isCore && !collapsed);
      g.classList.toggle("is-alone", isCore && collapsed);
      g.classList.toggle("is-orbit", onStage && !isCore);
      g.classList.toggle("is-set", !!n.set);
      g.classList.toggle("is-open", n.id === sheetId);
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
      // Long enough for the last dot's staggered wilt (0.22s + 4 × 26ms).
    }, reduceMotion() ? 0 : 340);
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

  // One window, every depth. Fitting it to the open ring is what made the depth
  // shrink invisible: a smaller ring in a smaller frame is the same picture.
  function frameFor() {
    if (openId === null) return { cx: 0, cy: 0, half: root.r * COLLAPSED_ZOOM };
    return { cx: 0, cy: 0, half: VIEW_HALF };
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
    bloom(visibleNodes().filter((n) => before.indexOf(n) === -1), up ? 160 : 0);
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

  function button(host, label, cls, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "we-btn " + (cls || "");
    b.textContent = label;
    b.addEventListener("click", onClick);
    host.appendChild(b);
    return b;
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

  async function putJson(url, payload) {
    try {
      const r = await fetch(url, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await r.json().catch(() => null);
      if (!r.ok) return { ok: false, error: (body && body.message) || "failed" };
      return { ok: true, data: (body && body.data) || body };
    } catch (_) { return { ok: false, error: "failed" }; }
  }

  // ── Real controls ─────────────────────────────────────────────────────
  // A row that LOOKS like the status rows above it but can actually be changed.
  // The panels were all facts and no handles: correct information, presented as
  // dead text, which is indistinguishable from a broken control.
  function fieldRow(host, label, help) {
    const row = document.createElement("label");
    row.className = "eg-field";
    const k = document.createElement("span");
    k.className = "eg-field-k";
    k.textContent = label;
    row.appendChild(k);
    host.appendChild(row);
    if (help) {
      const h = document.createElement("span");
      h.className = "eg-field-help";
      h.textContent = help;
      host.appendChild(h);
    }
    return row;
  }

  function selectRow(host, label, options, value, help, onPick) {
    const row = fieldRow(host, label, help);
    const sel = document.createElement("select");
    sel.className = "eg-select";
    options.forEach((o) => {
      const opt = document.createElement("option");
      opt.value = o.id;
      opt.textContent = o.label;
      if (String(o.id) === String(value)) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.addEventListener("change", () => onPick(sel.value));
    row.appendChild(sel);
    return sel;
  }

  function numberRow(host, label, spec, value, help, onSet) {
    const row = fieldRow(host, label, help);
    const wrap = document.createElement("span");
    wrap.className = "eg-range";
    const input = document.createElement("input");
    input.type = "range";
    input.min = spec.min;
    input.max = spec.max;
    input.step = spec.step || 1;
    input.value = value;
    const out = document.createElement("span");
    out.className = "eg-range-v";
    out.textContent = value;
    input.addEventListener("input", () => { out.textContent = input.value; });
    input.addEventListener("change", () => onSet(input.value));
    wrap.appendChild(input);
    wrap.appendChild(out);
    row.appendChild(wrap);
    return input;
  }

  function switchRow(host, label, value, help, onSet) {
    const row = fieldRow(host, label, help);
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "eg-switch";
    box.checked = !!value;
    box.addEventListener("change", () => onSet(box.checked));
    row.appendChild(box);
    return box;
  }

  // A prompt, editable in place, sharing the very edits buffer the flat list
  // uses — so the footer's Apply Live picks it up and the full-screen editor
  // opens on the same text. Used where a look is authored rather than set.
  function promptEditor(body, key, label) {
    const field = B.fieldById(key);
    if (!field) return null;
    const host = group(body, label || field.label);
    const ta = document.createElement("textarea");
    ta.className = "eg-prompt";
    ta.spellcheck = false;
    ta.value = String(B.valOf(key) || "");
    ta.setAttribute("aria-label", field.label || key);
    host.appendChild(ta);
    if (field.description) note(host, field.description);

    const acts = document.createElement("div");
    acts.className = "eg-row eg-acts";
    host.appendChild(acts);
    ta.addEventListener("input", () => B.setEdit(key, ta.value));
    button(acts, "Save", "we-btn-primary", async () => {
      const res = await B.saveField(key, ta.value);
      if (res && res.ok) { B.toast("Saved."); sync(); }
      else B.toast("Couldn't save that.", "warn");
    });
    button(acts, "Full editor", "", () => { closeSheet(); B.openPrompt(key); });
    if (String(B.valOf(key) || "") !== String(B.defOf(key) || "")) {
      button(acts, "Reset", "we-btn-ghost", async () => {
        await B.resetField(key);
        ta.value = String(B.valOf(key) || "");
        B.toast("Back to the shipped shot.");
      });
    }
    return ta;
  }

  // Every knob goes through one endpoint, so every panel saves the same way and
  // reports failure the same way.
  let tunables = null;
  async function loadTunables(force) {
    if (tunables && !force) return tunables;
    tunables = await getJson("/api/admin/studio/tunables");
    return tunables;
  }

  async function setTunable(name, value) {
    const res = await putJson("/api/admin/studio/tunables", { [name]: value });
    if (!res.ok) { B.toast(res.error || "Couldn't save that.", "warn"); return false; }
    tunables = res.data;
    B.toast("Saved.");
    return true;
  }

  // ── Mechanics ─────────────────────────────────────────────────────────
  function sheetScan(n, body) {
    const knobs = group(body, "Detector");
    const state = group(body, "Right now");
    Promise.all([loadTunables(true), getJson("/api/health")]).then(([t, h]) => {
      knobs.innerHTML = "";
      state.innerHTML = "";
      const spec = (t && t.schema) || {};
      const vals = (t && t.values) || {};
      if (spec.detect_backend) {
        selectRow(knobs, spec.detect_backend.label,
          spec.detect_backend.options.map((o) => ({
            id: o,
            label: { gemini: "Ask the image model", local: "On the box",
                     auto: "On the box, fall back" }[o] || o,
          })),
          vals.detect_backend, spec.detect_backend.help,
          (v) => setTunable("detect_backend", v).then((ok) => {
            if (ok && sheetId === n.id) openSheet(n);
          }));
      }
      if (spec.detect_min_score) {
        numberRow(knobs, spec.detect_min_score.label, spec.detect_min_score,
          vals.detect_min_score, spec.detect_min_score.help,
          (v) => setTunable("detect_min_score", v));
      }
      const d = (h && h.detect) || {};
      const local = d.local || {};
      const onDevice = d.backend === "local" || d.backend === "auto";
      statusRow(state, "Answering", d.backend || "unknown");
      statusRow(state, "On device", local.available ? "ready" : "unavailable",
                onDevice && !local.available);
      if (local.timeouts) statusRow(state, "Timeouts", local.timeouts, true);
      if (local.breaker_open) statusRow(state, "Breaker", "open", true);
      // A stack-trace fragment is not a value in a two-column row.
      if (local.error && onDevice) {
        const pre = document.createElement("pre");
        pre.className = "eg-err";
        pre.textContent = local.error;
        state.appendChild(pre);
      }
      if (onDevice && !local.available) {
        note(state, "The on-device detector didn't load, so SCAN is asking the " +
                    "image model instead. Slower, and it costs a call per scan.");
      }
    });
  }

  function sheetCamp(n, body) {
    const knobs = group(body, "The fire");
    loadTunables(true).then((t) => {
      knobs.innerHTML = "";
      const spec = (t && t.schema) || {};
      const vals = (t && t.values) || {};
      if (spec.camp_companion_cap) {
        numberRow(knobs, spec.camp_companion_cap.label, spec.camp_companion_cap,
          vals.camp_companion_cap, spec.camp_companion_cap.help,
          (v) => setTunable("camp_companion_cap", v));
      }
      if (spec.camp_include_jeep) {
        switchRow(knobs, spec.camp_include_jeep.label, vals.camp_include_jeep,
          spec.camp_include_jeep.help,
          (v) => setTunable("camp_include_jeep", v));
      }
    });
    const who = group(body, "Who comes");
    getJson("/api/companions").then((c) => {
      who.innerHTML = "";
      const roster = (c && (c.companions || c.roster)) || [];
      if (!roster.length) {
        note(who, "Nobody yet. Companions join as you meet them, and each keeps " +
                  "the portrait and the voice it was given.");
        return;
      }
      roster.forEach((p) => statusRow(who, p.name || p.label || p.slug || "someone",
                                      p.voice_id ? "voiced" : "silent", !p.voice_id));
    });
    // How camp LOOKS. This was a wall of hardcoded strings in engine.py: the one
    // scene the game composes for you was the one scene you couldn't direct.
    promptEditor(body, "camp_scene_prompt", "The shot");

    // Camp caches its establishing shot against the roster, so changing the
    // seats does nothing until the shot is rebuilt. This is that button.
    const acts = group(body, "Image");
    button(acts, "Rebuild the camp shot", "", async () => {
      B.toast("Rebuilding camp\u2026");
      try {
        await fetch("/api/camp/enter", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: "default", force: true }),
        });
        B.toast("Camp redrawn.");
      } catch (_) { B.toast("Couldn't redraw camp.", "warn"); }
    });
    note(acts, "The establishing shot is cached against who is there, so it only " +
               "changes when the roster does — or when you ask.");
  }

  function sheetNpc(n, body) {
    const state = group(body, "Conversation");
    getJson("/api/health").then((h) => {
      state.innerHTML = "";
      const t = (h && h.talk) || {};
      statusRow(state, "Voice", t.voice ? "ready" : "text only", !t.voice);
      statusRow(state, "Agent", t.agent ? "set" : "missing", !t.agent);
      statusRow(state, "API key", t.api_key ? "set" : "not set", false);
      if (t.reason && t.reason !== "ready") note(state, t.reason);
    });
    // The party. /api/companions is the roster; place() puts one into the scene
    // you are standing in, and regenerate_voice recasts them.
    const party = group(body, "The party");
    getJson("/api/companions").then((c) => {
      party.innerHTML = "";
      const roster = (c && (c.companions || c.roster)) || [];
      if (!roster.length) {
        note(party, "Nobody travelling with you yet. Talk to someone and they " +
                    "join the roster.");
        return;
      }
      roster.forEach((p) => {
        const label = p.name || p.label || p.slug || "someone";
        const row = document.createElement("div");
        row.className = "eg-person";
        const nm = document.createElement("span");
        nm.className = "eg-person-n";
        nm.textContent = label;
        row.appendChild(nm);
        const acts = document.createElement("span");
        acts.className = "eg-person-a";
        button(acts, "Bring in", "", async () => {
          B.toast("Bringing " + label + " in\u2026");
          try {
            const r = await fetch("/api/companions/place", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ label: label, session_id: "default" }),
            });
            const d = await r.json().catch(() => null);
            B.toast(d && d.image_url ? label + " is here."
                                     : "Couldn't place them right now.",
                    d && d.image_url ? "" : "warn");
          } catch (_) { B.toast("Couldn't place them.", "warn"); }
        });
        button(acts, "Recast voice", "we-btn-ghost", async () => {
          B.toast("Designing a voice for " + label + "\u2026");
          try {
            const r = await fetch("/api/companions/regenerate_voice", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ label: label, session_id: "default" }),
            });
            const d = await r.json().catch(() => null);
            B.toast(d && (d.voice_id || d.status === "queued")
              ? "Recasting " + label + "." : "Couldn't recast that voice.",
              d && (d.voice_id || d.status === "queued") ? "" : "warn");
          } catch (_) { B.toast("Couldn't recast that voice.", "warn"); }
        });
        row.appendChild(acts);
        party.appendChild(row);
      });
    });
    // The narrator is a live button; the graph proxies to it rather than owning
    // a second copy of its state.
    const btn = (B.engineControls() || [])
      .reduce((all, g) => all.concat(g.buttons), [])
      .find((b) => b.id === "narrator-btn");
    if (btn) {
      const acts = group(body, "Narrator");
      button(acts, "Speak the last line", "", () => {
        try { btn.click(); } catch (_) {}
      });
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

  // Why your own voices might not be in the list. Worth spelling out: the most
  // likely cause is a key without the voices_read permission, which from the
  // outside is indistinguishable from having no custom voices at all.
  const LIBRARY_REASONS = {
    no_api_key: "No ElevenLabs key on the server, so there is no library to read.",
    key_cannot_read_voices:
      "The ElevenLabs key can't list voices — it needs the voices_read " +
      "permission. Showing the shipped voices until it does.",
    rate_limited: "ElevenLabs is rate-limiting us. Try again in a moment.",
    unreachable: "Couldn't reach ElevenLabs from the server.",
    empty: "The ElevenLabs account has no voices on it yet.",
    bad_key: "The ElevenLabs key on the server looks wrong, so the library " +
             "can't be read. Showing the shipped voices until it's fixed.",
    http_400: "ElevenLabs rejected the request for the voice list. Usually the " +
              "key: it needs to be an API key (sk_\u2026), not an agent id.",
  };

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
    const picks = group(body, "Casting");
    // The WHOLE account, not the eleven stock ids baked into voices.json —
    // custom voices designed in ElevenLabs were invisible from in here, which
    // made the panel look like it was showing someone else's voices.
    Promise.all([
      loadTunables(true),
      getJson("/api/talk/voices/library"),
      getJson("/api/talk/voices"),
    ]).then(([t, lib, reg]) => {
      picks.innerHTML = "";
      const vals = (t && t.values) || {};
      const spec = (t && t.schema) || {};
      const library = (lib && lib.voices) || [];
      // Fall back to the shipped registry if the account can't be reached, so
      // the menu is never empty.
      const options = (library.length ? library : ((reg && reg.voices) || []))
        .map((v) => ({
          id: v.id,
          label: v.name + (v.category && v.category !== "premade" ? "  \u2022 yours" : ""),
        }));
      if (!options.length) {
        note(picks, LIBRARY_REASONS[lib && lib.reason] || "Couldn't read the voice library.");
      } else {
        selectRow(picks, spec.default_voice_id ? spec.default_voice_id.label : "Default voice",
          options, vals.default_voice_id,
          spec.default_voice_id && spec.default_voice_id.help,
          (v) => setTunable("default_voice_id", v));
        selectRow(picks, spec.narrator_voice_id ? spec.narrator_voice_id.label : "Narrator",
          options, vals.narrator_voice_id,
          spec.narrator_voice_id && spec.narrator_voice_id.help,
          (v) => setTunable("narrator_voice_id", v));
      }

      // The stock list still fills the menus, but say WHY your own voices aren't
      // in it — an empty library and a key that can't read one look identical.
      if (!library.length && options.length) {
        note(picks, LIBRARY_REASONS[lib && lib.reason] ||
                    "Showing the shipped voices only.");
        if (lib && lib.detail) {
          const pre = document.createElement("pre");
          pre.className = "eg-err";
          pre.textContent = lib.detail;
          picks.appendChild(pre);
        }
      }

      const yours = library.filter((v) => v.category && v.category !== "premade");
      const all = group(body, library.length
        ? ("Library \u00B7 " + library.length + (yours.length ? " (" + yours.length + " yours)" : ""))
        : "Library");
      library.forEach((v) => {
        const row = statusRow(all, v.name, v.description || v.category || "");
        if (v.id === vals.default_voice_id || v.id === vals.narrator_voice_id) {
          row.classList.add("is-now");
        }
      });
      if (library.length) {
        note(all, "A character is cast from this list the first time they speak, " +
                  "and keeps that voice.");
      }
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

    // One button that puts the whole thing back to how it shipped: the four
    // sheets AND every runtime knob. Getting out of a mess used to mean
    // emptying a dozen fields by hand.
    const reset = group(body, "Start over");
    let armed = false;
    const b = button(reset, "Clear everything", "we-btn-ghost", async () => {
      if (!armed) {
        armed = true;
        b.classList.add("we-btn-primary");
        b.textContent = "Sure? This clears every setting";
        setTimeout(() => {
          if (!armed) return;
          armed = false;
          b.classList.remove("we-btn-primary");
          b.textContent = "Clear everything";
        }, 4000);
        return;
      }
      armed = false;
      b.disabled = true;
      try {
        await fetch("/api/admin/studio/identity/reset", { method: "POST" });
        await putJson("/api/admin/studio/tunables", { _clear: true });
        tunables = null;
        B.toast("Cleared.");
        closeSheet();
        setOpen(null, true);
        sync();
      } catch (_) {
        B.toast("Couldn't clear that.", "warn");
      }
      b.disabled = false;
    });
    note(reset, "Empties the game, level, character and camera sheets, and puts " +
                "every setting back to how it shipped.");
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
