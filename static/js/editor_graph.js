/* ============================================================
   SOMEWHERE // THE ORGANISM — the World Editor as a recursive graph

   The editor's data is a tree: one game, four layers, and inside each layer
   a handful of objects (spec sheets, prompts, saved levels, runtime knobs).
   The old panel flattened all of it onto one 100vw column of tabs, forms and
   15,000-character textareas, so the only way to see the shape of what you
   were editing was to scroll past every part of it.

   Here the hierarchy IS the navigation. One circle is THE GAME. Double-tap it
   and it becomes the screen — you're inside the cell, and its children are
   the circles arranged within. Double-tap one of those and the same thing
   happens, as deep as the tree goes. The parent's membrane stays visible as a
   halo at the periphery; tapping it surfaces you back up a level. At the
   vertices — prompts, spec sheets, saved levels and builds, where there is
   nothing left to dive into — a tap animates a window up with that object's
   editable options.

   Geometry, not physics: children are packed in symmetric rings (see pack()),
   zoom is one interpolated viewBox, and depth costs one order of magnitude of
   scale per level, which is what makes entering a cell read as *entering* it.

   This file owns no state of its own. Everything is read and written through
   WorldEditor's bridge (see the `bridge` object in standalone.js), so the
   graph and the flat list are two renderings of one truth, sharing one client
   for /api/admin/studio/*.

   window.EditorGraph facade:
       init(bridge)   wire up once, at startup
       sync()         re-read the state and redraw (after any save/load)
       onEscape()     close the window, else surface a level; false at the root
       focusId()      current cell id (tests / debugging)
   ============================================================ */
(function () {
  "use strict";

  const SVG = "http://www.w3.org/2000/svg";

  // The root cell's radius in world units. Everything else is a fraction of
  // it, so one number sets the whole coordinate space.
  const ROOT_R = 1000;
  // The focused cell spans this much of the view's SHORTER axis. The rest is
  // the parent's interior, showing as a halo of membrane around the cell —
  // which is both the depth cue and the target you tap to come back up. On a
  // phone the longer axis gets much more of it, which is why entering a cell
  // never feels like a screen swap.
  const FRAME_FILL = 0.87;
  // The root has no parent to reveal, so it opens edge to edge instead of
  // floating inside a margin of nothing.
  const ROOT_FILL = 0.98;
  // Breathing room between packed siblings, as a fraction of the tight fit.
  const PACK_GAP = 0.9;
  const ZOOM_MS = 620;
  // Below this projected size a cell isn't worth drawing (and its label
  // certainly isn't legible), so it's culled from the frame.
  const CULL_PX = 1.4;
  const TAP_SLOP_PX = 26;
  const DBL_TAP_MS = 340;

  let B = null;                 // the WorldEditor bridge
  let els = {};
  let root = null;              // tree root
  let nodesById = Object.create(null);
  let focusId = "game";
  let selectedId = null;
  let sheetId = null;           // which vertex's window is open
  let view = null;              // {cx, cy, r} currently on screen
  let anim = null;              // in-flight zoom
  let lastTap = { t: 0, x: 0, y: 0, id: null };
  let hoverId = null;           // cell under the pointer, on desktop
  let hintTimer = null;

  function reduceMotion() {
    try { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; }
    catch (_) { return false; }
  }

  // ══════════════════════════════════════════════════════════════════
  // GEOMETRY — symmetric ring packing inside a unit circle
  //
  // Deliberately not a physics pack: the point is that it looks composed.
  // Returns n frames {x, y, r} in a unit circle centred on the origin, with
  // the first child at 12 o'clock so the layout is stable between renders.
  // ══════════════════════════════════════════════════════════════════
  // The classic result: m equal circles on one ring inside a unit circle are
  // largest when neighbour-tangency and shell-tangency coincide, at
  // r = sin(π/m) / (1 + sin(π/m)), centres at 1 − r.
  function bestRing(m) {
    if (m === 1) return { rho: 0, r: 0.54 };
    const s = Math.sin(Math.PI / m);
    const r = (s / (1 + s)) * PACK_GAP;
    return { rho: 1 - r / PACK_GAP, r: r };
  }

  function ring(m, rho, r, phase, out) {
    for (let i = 0; i < m; i++) {
      const a = phase + (i / m) * Math.PI * 2;
      out.push({ x: Math.cos(a) * rho, y: Math.sin(a) * rho, r: r });
    }
  }

  function pack(n) {
    const out = [];
    if (n <= 0) return out;
    const top = -Math.PI / 2;                 // first child at 12 o'clock
    if (n === 1) return [{ x: 0, y: 0, r: 0.54 }];
    if (n <= 6) {
      const b = bestRing(n);
      ring(n, b.rho, b.r, top, out);
      return out;
    }
    if (n <= 12) {
      // A nucleus plus a ring: keeps the middle of the cell alive instead of
      // leaving a hole, and holds every sibling at one size.
      const b = bestRing(n - 1);
      out.push({ x: 0, y: 0, r: Math.min(b.r, b.rho - b.r * 1.1) });
      ring(n - 1, b.rho, b.r, top, out);
      return out;
    }
    // Two concentric rings for a crowded cell (a game with many saved levels).
    const outerN = Math.ceil(n * 0.62);
    const innerN = n - outerN;
    const b = bestRing(outerN);
    const rhoIn = Math.max(b.r * 1.1, b.rho - b.r * 2.1);
    const rIn = Math.min(b.r, rhoIn * Math.sin(Math.PI / Math.max(2, innerN)) * PACK_GAP);
    ring(innerN, rhoIn, rIn, top + Math.PI / innerN, out);
    ring(outerN, b.rho, b.r, top, out);
    return out;
  }

  // Absolute frames, computed once per data change: a child's world frame is
  // its unit frame scaled into its parent's.
  function layout(node, cx, cy, r) {
    node.cx = cx; node.cy = cy; node.r = r;
    const kids = node.children || [];
    const frames = pack(kids.length);
    kids.forEach((kid, i) => {
      const f = frames[i];
      layout(kid, cx + f.x * r, cy + f.y * r, f.r * r);
    });
  }

  // ══════════════════════════════════════════════════════════════════
  // THE TREE — derived from the bridge, never stored
  // ══════════════════════════════════════════════════════════════════
  function node(spec) {
    spec.children = spec.children || [];
    return spec;
  }

  function promptNode(field, accent) {
    const key = field.id;
    const text = String(B.valOf(key) || "");
    return node({
      id: "prompt:" + key,
      kind: "prompt",
      label: field.label || key,
      glyph: "\u2261",
      accent: accent,
      meta: text.length > 999
        ? (text.length / 1000).toFixed(1) + "k chars"
        : text.length + " chars",
      dirty: B.isDirty(key),
      modified: text !== String(B.defOf(key) || ""),
      data: { key: key, field: field },
    });
  }

  function specNode(block, accent) {
    const spec = (B.identity() || {})[block.id] || {};
    const count = (block.fields || []).length;
    return node({
      id: "spec:" + block.id,
      kind: "spec",
      label: block.label || block.id,
      // The schema's icons are emoji (🧍 🗺️ 🎥); a coloured pictogram in a
      // line-art diagram looks like a sticker, so the graph keeps one
      // geometric glyph for "a sheet you fill in".
      glyph: "\u25A6",
      accent: accent,
      meta: spec.enabled === false ? "off" : count + " fields",
      off: spec.enabled === false,
      data: { block: block },
    });
  }

  function buildTree() {
    const layers = B.layers() || [];
    const schema = B.schema() || [];
    const kids = [];

    layers.forEach((layer) => {
      const accent = layer.accent || "#7aa2ff";
      const inner = [];

      // The engine owns the runtime knobs: nine live <button> elements the
      // graph proxies taps to (see bridge.engineControls).
      if (layer.id === "engine") {
        const controls = [];
        (B.engineControls() || []).forEach((group) => {
          group.buttons.forEach((btn) => {
            const lbl = btn.querySelector(".rail-lbl");
            const desc = btn.querySelector(".we-sys-desc");
            controls.push(node({
              id: "control:" + btn.id,
              kind: "control",
              label: (lbl && lbl.textContent) || btn.id,
              glyph: (btn.querySelector(".rail-ico") || {}).textContent || "\u25CB",
              accent: accent,
              meta: group.title,
              sub: (desc && desc.textContent) || btn.title || "",
              // The buttons own their state; the graph only reflects it. Some
              // are switches ("on"/"active"/"off"), others just open a panel
              // and carry no state at all.
              on: btn.classList.contains("on") || btn.classList.contains("active"),
              off: btn.classList.contains("off"),
              data: { button: btn },
            }));
          });
        });
        if (controls.length) {
          inner.push(node({
            id: "group:system",
            kind: "group",
            label: "System",
            glyph: "\u25A4",
            accent: accent,
            sub: "The engine's runtime knobs \u2014 renderer, models, sound.",
            children: controls,
          }));
        }
      }

      // The level layer keeps a gallery: a level is a place you can hold
      // several of, so they're cells in their own right.
      if (layer.id === "level") {
        const levels = (B.levels() || []).map((lv) => node({
          id: "level:" + lv.slug,
          kind: "level",
          label: lv.name || lv.slug,
          glyph: "\u25F0",
          accent: accent,
          // A saved level is never "off" — that flag belongs to its plate, and
          // reading OFF on a place you saved is just alarming.
          meta: lv.era || (lv.plate_count ? lv.plate_count + " plates" : "no art"),
          art: (lv.plates && lv.plates[0]) || null,
          data: { level: lv },
        }));
        levels.push(node({
          id: "new:level",
          kind: "new-level",
          label: "Save this level",
          glyph: "+",
          accent: accent,
          meta: "new",
        }));
        inner.push(node({
          id: "group:levels",
          kind: "group",
          label: "Levels",
          glyph: "\u25A4",
          accent: accent,
          sub: "Every place you've saved. Opening one leaves the rest of the game alone.",
          children: levels,
        }));
      }

      // Spec sheets first — a form is easier to answer than a blank page.
      (layer.spec_blocks || []).forEach((id) => {
        const block = B.identityBlock(id);
        if (block) inner.push(specNode(block, accent));
      });

      // Prompt bodies: the essential ones as cells, the mechanical rulebooks
      // behind one more dive so a layer never opens as twelve equal walls.
      const fields = (layer.fields || [])
        .map((id) => schema.find((f) => f.id === id))
        .filter(Boolean);
      fields.filter((f) => !B.isAdvanced(f)).forEach((f) => inner.push(promptNode(f, accent)));
      const advanced = fields.filter((f) => B.isAdvanced(f));
      if (advanced.length) {
        inner.push(node({
          id: "group:advanced:" + layer.id,
          kind: "group",
          label: "Advanced",
          glyph: "\u25B8",
          accent: accent,
          sub: "The mechanical rulebooks underneath this layer. A careless edit here stops turns resolving.",
          children: advanced.map((f) => promptNode(f, accent)),
        }));
      }

      kids.push(node({
        id: "layer:" + layer.id,
        kind: "layer",
        label: layer.label || layer.id,
        glyph: layer.icon || "\u25C8",
        accent: accent,
        sub: layer.question || "",
        tag: layer.tagline || "",
        risk: layer.risk || "content",
        volatility: layer.volatility || "",
        blurb: layer.blurb || "",
        children: inner,
      }));
    });

    // Builds sit beside the layers, not inside one: a build is a snapshot of
    // all of them at once.
    const buildAccent = "#cfd6e6";
    const builds = (B.worlds() || []).map((w) => node({
      id: "build:" + w.slug,
      kind: "build",
      label: w.name || w.slug,
      glyph: "\u25A3",
      accent: buildAccent,
      meta: (w.field_count || 0) + " prompts",
      data: { world: w },
    }));
    builds.push(node({
      id: "new:build",
      kind: "new-build",
      label: "Save a build",
      glyph: "+",
      accent: buildAccent,
      meta: "new",
    }));
    kids.push(node({
      id: "group:builds",
      kind: "group",
      label: "Builds",
      glyph: "\u25A3",
      accent: buildAccent,
      sub: "A snapshot of everything: engine, game, level and character.",
      children: builds,
    }));

    const tree = node({
      id: "game",
      kind: "root",
      label: "The Game",
      glyph: "\u25C9",
      accent: "#eafff2",
      sub: "Everything the simulation reads, one cell at a time.",
      children: kids,
    });

    // Index + parent links, then place it all in world space.
    nodesById = Object.create(null);
    (function index(n, parent, depth) {
      n.parent = parent;
      n.depth = depth;
      nodesById[n.id] = n;
      (n.children || []).forEach((k) => index(k, n, depth + 1));
    })(tree, null, 0);
    layout(tree, 0, 0, ROOT_R);
    return tree;
  }

  // ══════════════════════════════════════════════════════════════════
  // DRAWING
  // ══════════════════════════════════════════════════════════════════
  function mk(name, attrs, cls) {
    const n = document.createElementNS(SVG, name);
    if (attrs) for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (cls) n.setAttribute("class", cls);
    return n;
  }

  // Two lines of ~14 characters is what fits inside a child cell at the label
  // size below; past that the name is clipped with an ellipsis.
  function wrapLabel(text, maxChars, maxLines) {
    const words = String(text || "").trim().split(/\s+/);
    const lines = [];
    let line = "";
    words.forEach((w) => {
      const next = line ? line + " " + w : w;
      if (next.length <= maxChars) { line = next; return; }
      if (line) lines.push(line);
      line = w.length > maxChars ? w.slice(0, maxChars - 1) + "\u2026" : w;
    });
    if (line) lines.push(line);
    if (lines.length > maxLines) {
      const cut = lines.slice(0, maxLines);
      cut[maxLines - 1] = cut[maxLines - 1].slice(0, maxChars - 1) + "\u2026";
      return cut;
    }
    return lines;
  }

  function drawNode(n) {
    const g = mk("g", { "data-id": n.id }, "eg-node eg-kind-" + n.kind);
    g.style.setProperty("--eg-accent", n.accent || "#7aa2ff");
    const container = (n.children || []).length > 0;
    if (container) g.classList.add("eg-container");
    if (n.risk === "contract") g.classList.add("is-contract");
    if (n.dirty) g.classList.add("is-dirty");
    if (n.modified) g.classList.add("is-modified");
    if (n.on) g.classList.add("is-on");
    if (n.off) g.classList.add("is-off");

    // The membrane. Containers get a second, inset ring so they read as
    // something you can be *inside* rather than a filled dot.
    g.appendChild(mk("circle", { cx: n.cx, cy: n.cy, r: n.r }, "eg-cell"));
    if (container) {
      g.appendChild(mk("circle", { cx: n.cx, cy: n.cy, r: n.r * 0.955 }, "eg-cell-in"));
      g.appendChild(mk("circle", { cx: n.cx, cy: n.cy, r: Math.max(1, n.r * 0.018) }, "eg-nucleus"));
    }

    // Label block, sized off the node's own radius so its apparent size stays
    // constant: a child of the focus is always ~a third of the view.
    const lab = mk("g", null, "eg-label");
    const glyph = mk("text", {
      x: n.cx, y: n.cy - n.r * (container ? 0.30 : 0.26),
      "font-size": n.r * (container ? 0.30 : 0.34),
    }, "eg-glyph");
    glyph.textContent = n.glyph || "";
    lab.appendChild(glyph);

    const lines = wrapLabel(n.label, 14, 2);
    // Shrink a long name until it fits the cell rather than letting it spill
    // over the membrane.
    const longest = lines.reduce((m, l) => Math.max(m, l.length), 0);
    const nameSize = n.r * 0.19 * Math.min(1, 12 / Math.max(9, longest));
    const name = mk("text", {
      x: n.cx, y: n.cy + n.r * (lines.length > 1 ? 0.02 : 0.08),
      "font-size": nameSize,
    }, "eg-name");
    lines.forEach((ln, i) => {
      const t = mk("tspan", { x: n.cx, dy: i === 0 ? 0 : nameSize * 1.12 });
      t.textContent = ln;
      name.appendChild(t);
    });
    lab.appendChild(name);

    const metaText = container
      ? (n.children.length + " inside")
      : (n.meta || "");
    if (metaText) {
      const meta = mk("text", {
        x: n.cx,
        y: n.cy + n.r * (lines.length > 1 ? 0.46 : 0.36),
        "font-size": n.r * 0.135,
      }, "eg-meta");
      meta.textContent = metaText;
      lab.appendChild(meta);
    }
    g.appendChild(lab);
    return g;
  }

  function build() {
    if (!els.world) return;
    root = buildTree();
    els.world.innerHTML = "";
    hoverId = null;   // the highlighted node just stopped existing

    // Tethers first (under everything): a container's nucleus reaching out to
    // each child, so this is honestly a graph and not just nested bubbles.
    const tethers = mk("g", null, "eg-tethers");
    (function walk(n) {
      (n.children || []).forEach((k) => {
        const line = mk("line", {
          x1: n.cx, y1: n.cy, x2: k.cx, y2: k.cy,
          "data-parent": n.id,
        }, "eg-tether");
        tethers.appendChild(line);
        walk(k);
      });
    })(root);
    els.world.appendChild(tethers);

    const layer = mk("g", null, "eg-nodes");
    (function walk(n) {
      layer.appendChild(drawNode(n));
      (n.children || []).forEach(walk);
    })(root);
    els.world.appendChild(layer);
  }

  // ── Guide furniture for the focused cell: concentric rings + radial
  // spokes. Faint, slowly rotating, and purely to give the geometry a
  // mathematical register rather than a bubbly one.
  function drawField(f) {
    if (!els.field) return;
    els.field.innerHTML = "";
    [0.34, 0.67, 1].forEach((k) => {
      els.field.appendChild(mk("circle", {
        cx: f.cx, cy: f.cy, r: f.r * k,
      }, "eg-guide"));
    });
    const spokes = mk("g", null, "eg-spokes");
    for (let i = 0; i < 12; i++) {
      const a = (i / 12) * Math.PI * 2;
      spokes.appendChild(mk("line", {
        x1: f.cx + Math.cos(a) * f.r * 0.1,
        y1: f.cy + Math.sin(a) * f.r * 0.1,
        x2: f.cx + Math.cos(a) * f.r,
        y2: f.cy + Math.sin(a) * f.r,
      }, "eg-spoke"));
    }
    els.field.appendChild(spokes);
  }

  // ── Per-node state relative to the focus. Recomputed on focus change (not
  // per frame): who's interactive, who shows a label, who's just texture.
  function paint() {
    const f = nodesById[focusId] || root;
    if (!f) return;
    const childIds = Object.create(null);
    (f.children || []).forEach((k) => { childIds[k.id] = true; });
    // Projected pixels per world unit, along the axis the cell is inscribed in.
    const scale = viewportPx() / (2 * ((view && view.half) || f.r));

    els.world.querySelectorAll(".eg-node").forEach((g) => {
      const n = nodesById[g.getAttribute("data-id")];
      if (!n) return;
      const isFocus = n.id === f.id;
      const isChild = !!childIds[n.id];
      const isInside = !isFocus && within(n, f);
      g.classList.toggle("is-focus", isFocus);
      g.classList.toggle("is-child", isChild);
      g.classList.toggle("is-deep", isInside && !isChild);
      g.classList.toggle("is-outside", !isFocus && !isInside);
      // The cell you're standing in sits inside another one: its membrane is
      // the halo at the periphery, so it stays readable as a wall.
      g.classList.toggle("is-parent", !!f.parent && n.id === f.parent.id);
      g.classList.toggle("is-selected", n.id === selectedId);
      g.classList.toggle("is-open", n.id === sheetId);
      // Cull anything too small to read or to matter.
      g.classList.toggle("is-tiny", n.r * scale < CULL_PX * 8);
      g.style.display = (n.r * scale < CULL_PX) ? "none" : "";
    });
    els.world.querySelectorAll(".eg-tether").forEach((line) => {
      line.classList.toggle("is-live", line.getAttribute("data-parent") === f.id);
    });
    // You are inside this: light the periphery in the parent's colour.
    if (els.rim) {
      els.rim.classList.toggle("is-on", !!f.parent);
      if (f.parent) els.rim.style.setProperty("--eg-accent", f.parent.accent || "#7aa2ff");
    }
    drawField(f);
    renderHud(f);
  }

  function within(n, ancestor) {
    let p = n.parent;
    while (p) { if (p.id === ancestor.id) return true; p = p.parent; }
    return false;
  }

  function viewportPx() {
    if (!els.canvas) return 600;
    const r = els.canvas.getBoundingClientRect();
    return Math.max(120, Math.min(r.width || 600, r.height || 600));
  }

  // ══════════════════════════════════════════════════════════════════
  // ZOOM — one interpolated viewBox. Radius is interpolated in log space so
  // the descent feels even rather than lurching at the end.
  // ══════════════════════════════════════════════════════════════════
  // `view.half` is the world half-extent along the view's shorter axis. The
  // viewBox matches the canvas's aspect exactly, so the cell is inscribed in
  // that axis and the longer one fills with the parent it sits in, rather than
  // with letterboxed nothing.
  function applyView(v) {
    view = v;
    const box = els.canvas.getBoundingClientRect();
    const w = box.width || 1, h = box.height || 1;
    const hx = w >= h ? v.half * (w / h) : v.half;
    const hy = h >= w ? v.half * (h / w) : v.half;
    els.canvas.setAttribute("viewBox",
      (v.cx - hx) + " " + (v.cy - hy) + " " + (hx * 2) + " " + (hy * 2));
  }

  function frameOf(n) {
    return { cx: n.cx, cy: n.cy, half: n.r / (n.parent ? FRAME_FILL : ROOT_FILL) };
  }

  // Labels are sized in world units, so during a zoom they'd scale with the
  // diagram — a name arriving at four times its final size, shrinking into
  // place. They ride out the motion hidden and fade in once it lands.
  function setZooming(on) {
    if (els.graph) els.graph.classList.toggle("is-zooming", !!on);
  }

  function frame(n, animate) {
    const to = frameOf(n);
    if (!view || !animate || reduceMotion()) {
      if (anim) { cancelAnimationFrame(anim.raf); anim = null; }
      setZooming(false);
      applyView(to);
      return;
    }
    const from = view;
    const t0 = performance.now();
    if (anim) cancelAnimationFrame(anim.raf);
    setZooming(true);
    const step = (now) => {
      const p = Math.min(1, (now - t0) / ZOOM_MS);
      // cubic in-out
      const e = p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2;
      applyView({
        cx: from.cx + (to.cx - from.cx) * e,
        cy: from.cy + (to.cy - from.cy) * e,
        // Scale interpolates in log space, or the descent lurches at the end.
        half: Math.exp(Math.log(from.half) + (Math.log(to.half) - Math.log(from.half)) * e),
      });
      if (p < 1) anim = { raf: requestAnimationFrame(step) };
      else { anim = null; setZooming(false); paint(); }
    };
    anim = { raf: requestAnimationFrame(step) };
  }

  function setFocus(id, animate) {
    const n = nodesById[id];
    if (!n) return;
    focusId = id;
    selectedId = null;
    frame(n, animate !== false);
    paint();
  }

  // ══════════════════════════════════════════════════════════════════
  // HUD — the trail you came down, and what's under your finger
  // ══════════════════════════════════════════════════════════════════
  function renderHud(f) {
    if (els.trail) {
      els.trail.innerHTML = "";
      const chain = [];
      for (let n = f; n; n = n.parent) chain.unshift(n);
      chain.forEach((n, i) => {
        const li = document.createElement("li");
        li.className = "eg-crumb" + (i === chain.length - 1 ? " is-here" : "");
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = n.label;
        b.style.setProperty("--eg-accent", n.accent || "#7aa2ff");
        if (i === chain.length - 1) b.setAttribute("aria-current", "true");
        else b.addEventListener("click", () => setFocus(n.id, true));
        li.appendChild(b);
        els.trail.appendChild(li);
      });
    }
    const shown = (selectedId && nodesById[selectedId]) || f;
    if (els.captionName) {
      els.captionName.textContent = shown.label;
      els.captionName.style.setProperty("--eg-accent", shown.accent || "#7aa2ff");
    }
    if (els.captionSub) {
      els.captionSub.textContent = shown.sub || shown.tag || shown.meta || "";
    }
    if (els.hint && !hintTimer) {
      els.hint.textContent = defaultHint(shown, f);
    }
  }

  function defaultHint(shown, f) {
    const container = (shown.children || []).length > 0;
    if (shown !== f && container) return "double-tap to go inside";
    if (shown !== f) return shown.kind === "control" ? "tap to toggle" : "tap to edit";
    if (f.parent) return "tap the edge to come back up";
    return "double-tap a cell to go inside";
  }

  function say(msg) {
    if (!els.hint) return;
    els.hint.textContent = msg;
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => { hintTimer = null; renderHud(nodesById[focusId] || root); }, 2200);
  }

  // ══════════════════════════════════════════════════════════════════
  // INTERACTION
  //
  // Only the focused cell's children are live: you act on what you can see,
  // and going deeper is an explicit dive. Everything outside the focus — the
  // parent's halo and the crowded edges of your siblings — surfaces you up.
  // ══════════════════════════════════════════════════════════════════
  // Client pixels → world units, straight off the viewBox. (getScreenCTM would
  // do this too, but its notion of "screen" drifts from client coordinates
  // under device emulation and page zoom — enough to miss a small cell while
  // still landing inside a big one.)
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

  function hitAt(clientX, clientY) {
    const p = toWorld(clientX, clientY);
    if (!p) return null;
    const f = nodesById[focusId] || root;
    let found = null;
    (f.children || []).forEach((k) => {
      const d = Math.hypot(p.x - k.cx, p.y - k.cy);
      if (d <= k.r) found = k;
    });
    if (found) return { node: found, where: "child" };
    const df = Math.hypot(p.x - f.cx, p.y - f.cy);
    return { node: f, where: df <= f.r ? "focus" : "outside" };
  }

  function activate(n) {
    if ((n.children || []).length) { setFocus(n.id, true); return; }
    // A control has no window to open — double-tapping one is still just the
    // switch, not an empty sheet.
    if (n.kind === "control") { toggleControl(n); return; }
    openSheet(n);
  }

  function onTap(evt) {
    const h = hitAt(evt.clientX, evt.clientY);
    if (!h) return;
    const now = performance.now();
    const isDouble = (now - lastTap.t) < DBL_TAP_MS &&
      Math.hypot(evt.clientX - lastTap.x, evt.clientY - lastTap.y) < TAP_SLOP_PX &&
      lastTap.id === h.node.id;
    lastTap = { t: now, x: evt.clientX, y: evt.clientY, id: h.node.id };

    if (h.where === "outside") {
      const f = nodesById[focusId] || root;
      if (f.parent) setFocus(f.parent.id, true);
      else say("this is the whole game \u2014 double-tap a cell");
      return;
    }
    if (h.where === "focus") { selectedId = null; paint(); return; }

    const n = h.node;
    const container = (n.children || []).length > 0;
    if (isDouble) { activate(n); return; }
    if (container) {
      // One tap selects, so the HUD can tell you what this is before you commit
      // to it; the next tap dives in. A double-tap does both at once, and a
      // slow second tap still works — a gesture that only counts under 340ms
      // reads as a broken control.
      if (selectedId === n.id) { activate(n); return; }
      selectedId = n.id;
      paint();
      return;
    }
    // A vertex: the tap IS the edit. Controls have nothing to configure, so
    // for them the tap is the toggle itself.
    if (n.kind === "control") { toggleControl(n); return; }
    openSheet(n);
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
    if (!h) return setHover(null);
    if (h.where === "outside") return setHover(null, nodesById[focusId].parent ? "zoom-out" : "default");
    if (h.where === "focus") return setHover(null);
    setHover(h.node.id, (h.node.children || []).length ? "zoom-in" : "pointer");
  }

  function toggleControl(n) {
    const btn = n.data && n.data.button;
    if (!btn) return;
    try { btn.click(); } catch (_) {}
    // The button owns its state classes, so re-read them rather than guess.
    // Twice: some switches settle immediately, others (the live renderer)
    // land on their state a connection later.
    setTimeout(() => { sync(); say(n.label + (btn.classList.contains("on") ? " on" : "")); }, 40);
    setTimeout(sync, 600);
  }

  // ══════════════════════════════════════════════════════════════════
  // THE WINDOW — animates up at a vertex with that object's options
  // ══════════════════════════════════════════════════════════════════
  function openSheet(n) {
    sheetId = n.id;
    els.sheet.classList.add("is-open");
    els.sheet.setAttribute("aria-hidden", "false");
    els.scrim.classList.add("is-open");
    els.graph.classList.add("has-sheet");
    els.sheetGlyph.textContent = n.glyph || "";
    els.sheetGlyph.style.setProperty("--eg-accent", n.accent || "#7aa2ff");
    els.sheetTitle.textContent = n.label;
    els.sheet.style.setProperty("--eg-accent", n.accent || "#7aa2ff");
    els.sheetBody.innerHTML = "";
    els.sheetBody.scrollTop = 0;
    const fill = {
      prompt: sheetPrompt,
      spec: sheetSpec,
      level: sheetLevel,
      build: sheetBuild,
      "new-level": sheetNewLevel,
      "new-build": sheetNewBuild,
    }[n.kind];
    els.sheetSub.textContent = sheetSub(n);
    if (fill) fill(n, els.sheetBody);
    paint();
  }

  // The header's second line is set in small caps, so it carries short
  // metadata only — anything prose-shaped goes in the body as a note.
  function sheetSub(n) {
    if (n.kind === "prompt") {
      return (B.isRestartKey(n.data.key) ? "restart to take effect" : "live next turn") +
        " \u00B7 " + n.meta;
    }
    if (n.kind === "spec") {
      const inLayer = n.parent && n.parent.kind === "layer" ? n.parent.label + " layer \u00B7 " : "";
      return inLayer + n.meta;
    }
    if (n.kind === "level") return "saved level";
    if (n.kind === "build") return "saved build \u00B7 " + (n.meta || "");
    return "";
  }

  function closeSheet() {
    if (!sheetId) return false;
    sheetId = null;
    els.sheet.classList.remove("is-open");
    els.sheet.setAttribute("aria-hidden", "true");
    els.scrim.classList.remove("is-open");
    els.graph.classList.remove("has-sheet");
    els.sheetBody.innerHTML = "";
    paint();
    return true;
  }

  function row(parent, cls) {
    const d = document.createElement("div");
    d.className = cls || "eg-row";
    parent.appendChild(d);
    return d;
  }

  function button(parent, label, cls, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "we-btn " + (cls || "");
    b.textContent = label;
    b.addEventListener("click", onClick);
    parent.appendChild(b);
    return b;
  }

  function note(parent, text, cls) {
    if (!text) return null;
    const p = document.createElement("p");
    p.className = cls || "eg-note";
    p.textContent = text;
    parent.appendChild(p);
    return p;
  }

  // A prompt vertex: the text itself, editable in place, sharing the very
  // `edits` buffer the list view uses — so Apply Live in the footer picks it
  // up, and Expand hands off to the full-screen editor unchanged.
  function sheetPrompt(n, body) {
    const key = n.data.key;
    const field = n.data.field;
    note(body, field.description);
    const wiring = B.wiringNote(key);
    if (wiring) note(body, wiring, "eg-note" + (wiring.indexOf("\u26a0") === 0 ? " is-warn" : ""));

    const ta = document.createElement("textarea");
    ta.className = "eg-text";
    ta.spellcheck = false;
    ta.value = String(B.valOf(key) || "");
    ta.setAttribute("aria-label", field.label || key);
    body.appendChild(ta);

    const foot = row(body, "eg-row eg-row-foot");
    const count = document.createElement("span");
    count.className = "eg-count";
    const setCount = () => {
      count.textContent = ta.value.length.toLocaleString("en-US") + " chars" +
        (B.isDirty(key) ? " \u00B7 unsaved" : "");
    };
    setCount();
    ta.addEventListener("input", () => { B.setEdit(key, ta.value); setCount(); });
    foot.appendChild(count);

    const acts = row(body, "eg-row eg-acts");
    button(acts, "Save", "we-btn-primary", async () => {
      const res = await B.saveField(key, ta.value);
      if (res && res.ok) { say("saved \u2014 live next turn"); sync(); }
    });
    button(acts, "Full editor", "", () => { closeSheet(); B.openPrompt(key); });
    if (n.modified) {
      button(acts, "Reset to default", "we-btn-ghost", async () => {
        await B.resetField(key);
        sync();
        const fresh = nodesById[n.id];
        if (fresh) openSheet(fresh);
      });
    }
  }

  // A spec sheet: the real cast form, mounted here by WorldEditor, saving on
  // blur through the same endpoint as before.
  function sheetSpec(n, body) {
    note(body, n.data.block.description);
    const host = document.createElement("div");
    host.className = "eg-spec";
    body.appendChild(host);
    B.renderSpecInto(host, n.data.block.id);
  }

  function sheetLevel(n, body) {
    const lv = n.data.level;
    if (lv.plates && lv.plates[0]) {
      const art = document.createElement("div");
      art.className = "eg-art";
      art.style.backgroundImage = "url('" + lv.plates[0] + "')";
      body.appendChild(art);
    }
    note(body, lv.summary || "No description yet.");
    const bits = [];
    if (lv.era) bits.push(lv.era);
    if (lv.has_opening_shot) bits.push("opening shot");
    if (lv.plate_count) bits.push(lv.plate_count + " plates");
    if (!lv.enabled) bits.push("plate off");
    note(body, bits.join(" \u00B7 "), "eg-note is-meta");
    const acts = row(body, "eg-row eg-acts");
    button(acts, "Open", "we-btn-primary", async () => {
      if (await B.loadLevel(lv.slug, lv.name)) { closeSheet(); say("now editing " + lv.name); }
    });
    button(acts, "Delete", "we-btn-ghost", async () => {
      if (await B.deleteLevel(lv.slug)) { closeSheet(); setFocus("group:levels", false); }
    });
  }

  function sheetBuild(n, body) {
    const w = n.data.world;
    note(body, "A build snapshots every layer at once: engine, game, level and character.");
    note(body, (w.field_count || 0) + " prompts" + (w.note ? " \u00B7 " + w.note : ""), "eg-note is-meta");
    const acts = row(body, "eg-row eg-acts");
    button(acts, "Load", "we-btn-primary", async () => { await B.loadWorld(w.slug, false); closeSheet(); });
    button(acts, "Play", "we-btn-accent", async () => { await B.loadWorld(w.slug, true); });
    button(acts, "Delete", "we-btn-ghost", async () => {
      await B.deleteWorld(w.slug, w.name);
      closeSheet();
      setFocus("group:builds", false);
    });
  }

  function nameForm(body, placeholder, label, onSave) {
    const form = document.createElement("form");
    form.className = "eg-name-form";
    form.autocomplete = "off";
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = placeholder;
    input.maxLength = 64;
    form.appendChild(input);
    const save = document.createElement("button");
    save.type = "submit";
    save.className = "we-btn we-btn-primary";
    save.textContent = label;
    form.appendChild(save);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const name = input.value.trim();
      if (!name) { input.focus(); return; }
      if (await onSave(name)) { closeSheet(); }
    });
    body.appendChild(form);
    setTimeout(() => input.focus(), 320);
  }

  function sheetNewLevel(n, body) {
    note(body, "Saves this place \u2014 its brief and its setting plate \u2014 as a level you " +
      "can come back to. The engine, the game and your character are left alone.");
    nameForm(body, "Name this level\u2026", "SAVE LEVEL", async (name) => {
      const ok = await B.saveLevel(name);
      if (ok) { sync(); say("saved level \u201C" + name + "\u201D"); }
      return ok;
    });
  }

  function sheetNewBuild(n, body) {
    note(body, "Saves everything at once: engine, game, level and character, as one " +
      "build you can load or play later.");
    nameForm(body, "Name this build\u2026", "SAVE BUILD", async (name) => {
      const ok = await B.saveWorld(name);
      if (ok) { sync(); say("saved build \u201C" + name + "\u201D"); }
      return ok;
    });
  }

  // ══════════════════════════════════════════════════════════════════
  // LIFECYCLE
  // ══════════════════════════════════════════════════════════════════
  function sync() {
    if (!B || !els.world) return;
    if (!B.isGraphMode()) return;
    const openId = sheetId;
    const active = document.activeElement;
    const typing = active && (active.tagName === "TEXTAREA" || active.tagName === "INPUT") &&
      els.sheetBody.contains(active);
    // Where you were, deepest first — if the cell you're standing in has just
    // been deleted, you surface to the nearest ancestor that still exists.
    const wasChain = [];
    for (let n = nodesById[focusId]; n; n = n.parent) wasChain.push(n.id);
    build();
    focusId = wasChain.find((id) => nodesById[id]) || root.id;
    frame(nodesById[focusId], false);
    paint();
    // Re-mount the open window against the fresh tree, unless the player is
    // mid-sentence in it.
    if (openId && nodesById[openId] && !typing) openSheet(nodesById[openId]);
    else if (openId && !nodesById[openId]) closeSheet();
  }

  function onEscape() {
    if (closeSheet()) return true;
    const f = nodesById[focusId];
    if (f && f.parent) { setFocus(f.parent.id, true); return true; }
    return false;
  }

  function init(bridge) {
    B = bridge;
    els = {
      graph: document.getElementById("we-graph"),
      canvas: document.getElementById("eg-canvas"),
      world: document.getElementById("eg-world"),
      field: document.getElementById("eg-field"),
      trail: document.getElementById("eg-trail"),
      captionName: document.getElementById("eg-caption-name"),
      captionSub: document.getElementById("eg-caption-sub"),
      rim: document.getElementById("eg-rim"),
      hint: document.getElementById("eg-hint"),
      sheet: document.getElementById("eg-sheet"),
      scrim: document.getElementById("eg-scrim"),
      sheetGlyph: document.getElementById("eg-sheet-glyph"),
      sheetTitle: document.getElementById("eg-sheet-title"),
      sheetSub: document.getElementById("eg-sheet-sub"),
      sheetBody: document.getElementById("eg-sheet-body"),
      sheetClose: document.getElementById("eg-sheet-close"),
    };
    if (!els.canvas || !els.world) return;

    els.canvas.addEventListener("click", onTap);
    // Our tap handler resolves single vs double itself, so the browser's own
    // double-click behaviour (selecting the label text) is just noise.
    els.canvas.addEventListener("dblclick", (e) => e.preventDefault());
    // With a mouse, say what a click will do before it happens: the cursor
    // names the gesture and the cell under the pointer lifts.
    els.canvas.addEventListener("mousemove", onHover);
    els.canvas.addEventListener("mouseleave", () => setHover(null));
    if (els.sheetClose) els.sheetClose.addEventListener("click", closeSheet);
    if (els.scrim) els.scrim.addEventListener("click", closeSheet);
    // The viewBox encodes the canvas aspect, so a resize has to re-frame.
    window.addEventListener("resize", () => { if (view) { applyView(view); paint(); } });
  }

  window.EditorGraph = {
    init: init,
    sync: sync,
    onEscape: onEscape,
    focusId: () => focusId,
    isSheetOpen: () => !!sheetId,
    // What a tap at these client coordinates would land on — the graph's hit
    // test, exposed so tests and the console can ask without clicking.
    probe: (x, y) => {
      const h = hitAt(x, y);
      return h ? { id: h.node.id, where: h.where } : null;
    },
  };
})();
