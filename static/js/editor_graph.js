/* ============================================================
   SOMEWHERE // THE ORGANISM — Experience as a state machine of Worlds

   Collapsed: one coin, the Experience — a real node, not a marker.
   Open it and a small origin nucleus sits at the centre. The Experience
   stays a placeable coin beside its Worlds (later Experiences can link
   the same way Worlds already do). Each World is a playable identity.
   Drag + WORLD onto the sheet to drop a cell under the cursor.
   Every World has a small satellite that tracks the mouse; drag that out
   to noodle a directed transition. An × on the cell removes it. Select a
   World and a card lets you name the place; double-tap (or Edit)
   dives into Level · Character · Gameplay. Lore is a coin on this same
   sheet: drop background notes and pictures there; the fragments on the
   coin update as they land, and Play reads them as shared history.

   Harness is the engine tab: a readout of how a turn actually runs. Open it
   and the engine lays out as one loop, in order — Choices, Actions,
   Picture (the wait), State, then back to Choices. Dive a node to see
   the knobs inside it. Sound is the third tab: the UI synth that used to
   be a pile of arcade chimes. Scroll to zoom, drag empty paper to pan.

   This file owns no identity state of its own. Everything is read and written
   through WorldEditor's bridge (see `bridge` in standalone.js).

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
  // Experience is a real coin; this is breathing room, not a second shrink.
  const COLLAPSED_ZOOM = 4.6;
  // The expanded view is a FIXED window on the world, the same at every depth.
  // Experience (a free layout of Worlds) gets a wider pane so noodles have
  // room and three cells don't sit on the clip edge.
  const VIEW_HALF = DOT_R * 3.9;
  const WORLD_VIEW_HALF = DOT_R * 5.8;
  const HARNESS_VIEW_HALF = DOT_R * 8.4;
  // The editor is a wash on the left of a full-bleed still. Framing the
  // nucleus at the centre of the SVG put PLAY on the actor. STAGE_LEFT is
  // where a ring's origin sits in the frame; STAGE_INSET is the left edge
  // of the harness machine, so the cluster starts in the wash without clipping.
  const STAGE_LEFT = 0.27;
  const STAGE_INSET = 0.10;
  // The enclosure you are inside. WIDER than the frame's short axis on purpose:
  // its left and right run off the edge and only the top and bottom arcs curve
  // through the view, which is what makes it read as a wall you are inside
  // rather than a ring drawn around the dots.
  const SHELL_R = VIEW_HALF * 1.3;
  const ZOOM_MS = 620;

  // ── Orbit. The open level is a rigid ring around the nucleus. One shared
  // spin, one radius — no per-dot Lissajous, no pairwise shove. Those were
  // what made leaving a menu wobble one time and not the next.
  const ORBIT_REV_S = 48;            // seconds per full revolution
  const RAD_EASE = 0.14;             // how fast a dot finds its radius
  const REPEL_GAP = 1.22;            // ring sizing only (neighbours must clear)
  const LEAVE_MS = 480;              // collapse-into-nucleus, then hide

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
  let raf = null;                    // orbit loop
  let lastTick = 0;
  let spin = 0;                      // shared angle of the open ring
  let hoverId = null;
  let selectId = null;               // selected World on the Experience graph
  let tool = "select";               // select | add-world | connect | delete
  let connectFrom = null;            // world node id while linking
  let hoverEdgeId = null;
  let openEdgeId = null;
  let openWorldId = null;
  let openLore = false;
  let ptr = null;                    // pointer gesture in flight
  let pointerWorld = null;           // last mouse in graph space (ports chase it)
  const DRAG_PX = 8;
  const PORT_R = DOT_R * 0.09;       // tiny satellite; hit area is larger than this
  const PORT_GAP = DOT_R * 0.12;
  const KILL_R = DOT_R * 0.11;       // × on the top-right of a World
  // Transition Type enum. The inspector and the sheet both render this as a
  // <select> so a new hook is one row, not a new pair of buttons. Keep the
  // ids in lockstep with experience_store.CONDITION_CATALOG.
  const TRANSITION_TYPES = [
    {
      id: "turn_count",
      label: "After N turns",
      short: "",
      hint: "Leave after this many turns in the current World.",
      fields: ["turns"],
    },
    {
      id: "game_over",
      label: "On death",
      short: "death",
      hint: "When the run ends here, continue in the next World.",
      fields: [],
    },
    {
      id: "immediate",
      label: "Immediately",
      short: "now",
      hint: "Fire as soon as this node is left. Used for Cutscene → World.",
      fields: [],
    },
  ];
  function transitionTypes() {
    try {
      const live = B && B.transitionTypes && B.transitionTypes();
      if (Array.isArray(live) && live.length) return live;
    } catch (_) {}
    return TRANSITION_TYPES;
  }
  function transitionType(id) {
    const list = transitionTypes();
    return list.find((row) => row && row.id === id) || list[0] || TRANSITION_TYPES[0];
  }
  function typeHasField(spec, name) {
    return !!spec && (spec.fields || []).indexOf(name) >= 0;
  }
  // Experience, Harness, and Sound are three trees on one canvas. Remember
  // where you were in each so the tab switch is a swap, not a reset — and
  // not a dive into the other tree's leftover openId.
  let surfaceKind = "experience";
  const openBySurface = { experience: null, harness: null, sound: null };
  const camBySurface = { experience: null, harness: null, sound: null };
  let userCam = null;                 // {cx, cy, half} after wheel / pan
  let harnessFrame = { cx: 0, cy: 0, half: HARNESS_VIEW_HALF };

  function currentSurface() {
    const s = B && B.editorSurface && B.editorSurface();
    return (s === "harness" || s === "sound") ? s : "experience";
  }
  function isHarness() { return currentSurface() === "harness"; }
  function isSound() { return currentSurface() === "sound"; }
  function isExperience() { return currentSurface() === "experience"; }

  function isHarnessOverview() {
    return isHarness() && !!root && openId === root.id;
  }

  function flattenKids(n) {
    const out = [];
    (n.children || []).forEach((k) => {
      out.push(k);
      flattenKids(k).forEach((x) => out.push(x));
    });
    return out;
  }

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
      id: "world", kind: "group", label: "Gameplay", sub: "How the game plays.",
      children: [
        {
          id: "mechanics", kind: "group", label: "Mechanics",
          sub: "The things the game can do.",
          children: [
            { id: "camera", kind: "spec", block: "camera_perspective", label: "Camera",
              sub: "Where the camera stands." },
            { id: "pacing", kind: "pacing", label: "Pacing",
              sub: "When the story tightens, and what choices hear." },
            { id: "scan", kind: "scan", label: "Scan",
              sub: "Finding objects in the frame." },
            { id: "camp", kind: "camp", prompt: "camp_scene_prompt", label: "Camp",
              sub: "Who is at the fire with you." },
            { id: "narrator", kind: "narrator", prompt: "narrator_direction", label: "Narrator",
              sub: "The voice that talks to you." },
            { id: "music", kind: "music", label: "Music",
              sub: "Score, ambience, and the prompts that write them." },
          ],
        },
        {
          id: "models", kind: "group", label: "Models",
          sub: "What generates the world.",
          children: [
            { id: "live", kind: "world", label: "World", sub: "The live world model." },
            { id: "text", kind: "text", label: "Text", sub: "Who writes the world." },
            { id: "image", kind: "image", label: "Image", sub: "What draws the stills." },
            { id: "voice", kind: "voice", label: "Voice", sub: "Who speaks, and how." },
          ],
        },
        { id: "controls", kind: "controls", label: "Controls",
          sub: "How you see it and how you move." },
      ],
    },
  ];

  // The machine room, as one loop, in play order. Picture is the wait —
  // the turn does not come back until a frame lands — so it is the
  // biggest bubble. Labels stay ≤9 characters because a dot is a word.
  const HARNESS_DOTS = [
    { id: "choices", kind: "prompt", prompt: "player_choice_generation_instructions",
      label: "Choices", beat: 1, weight: 1.08, sub: "What you are offered." },
    { id: "actions", kind: "prompt", prompt: "action_consequence_instructions",
      label: "Actions", beat: 2, weight: 1.18, sub: "You pick. The world answers." },
    {
      id: "picture", kind: "group", label: "Picture", beat: 3, wait: true, weight: 1.42,
      sub: "The frame has to land. This is the wait.",
      children: [
        { id: "system", kind: "system", label: "System", weight: 0.7,
          sub: "How the picture is drawn." },
        { id: "framing", kind: "prompt", prompt: "image_camera_rules",
          label: "Framing", weight: 0.66, sub: "Camera physics for the picture." },
        { id: "still", kind: "prompt", prompt: "gemini_text_to_image_instructions",
          label: "Still", weight: 0.8, sub: "Text to a still." },
        { id: "edit", kind: "prompt", prompt: "gemini_image_to_image_instructions",
          label: "Edit", weight: 0.8, sub: "Image to image." },
        { id: "film", kind: "prompt", prompt: "gemini_flipbook_4panel_prefix",
          label: "Film", weight: 0.62, sub: "The flipbook prefix." },
      ],
    },
    {
      id: "state", kind: "group", label: "State", beat: 4, weight: 1.08,
      sub: "What changed. Then new offers.",
      children: [
        { id: "evolve", kind: "prompt", prompt: "world_evolution_instructions",
          label: "Evolve", weight: 0.78, sub: "What may change between turns." },
        { id: "scene", kind: "prompt", prompt: "situation_summary_instructions",
          label: "Scene", weight: 0.7, sub: "The between-turn bulletin." },
      ],
    },
  ];

  // The machine's voice. Palette is how it speaks; the families are what
  // you can mute. Labels stay ≤9 characters because a dot is a word.
  const SOUND_DOTS = [
    { id: "palette", kind: "sound-palette", label: "Palette",
      sub: "How the machine speaks." },
    { id: "clicks", kind: "sound-family", family: "clicks", label: "Clicks",
      sub: "Pointer and keys." },
    { id: "chrome", kind: "sound-family", family: "chrome", label: "Chrome",
      sub: "Menus and confirms." },
    { id: "turn", kind: "sound-family", family: "turn", label: "Turn",
      sub: "The loop speaking." },
    { id: "lens", kind: "sound-family", family: "lens", label: "Lens",
      sub: "Camera, scan, case." },
    { id: "body", kind: "sound-family", family: "body", label: "Body",
      sub: "Hurt, pulse, death." },
    { id: "voice", kind: "sound-family", family: "voice", label: "Voice",
      sub: "Talk and moments." },
  ];

  // One clockwise cycle. No side branch, no faint return that hides
  // Picture. Dive a node and the green hub is the start: out through
  // its children in order, then back to the hub — the way up.
  const HARNESS_FLOW = [
    { from: "choices", to: "actions", kind: "loop", route: "spine" },
    { from: "actions", to: "picture", kind: "loop", route: "spine" },
    { from: "picture", to: "state", kind: "loop", route: "spine" },
    { from: "state", to: "choices", kind: "loop", route: "spine" },
  ];

  // Have YOU changed this from how it shipped? That is the one piece of state a
  // dot carries, and it used to be "does this have any content", which is a
  // different question with a misleading answer: the shipped character sheet has
  // a name and a look in it, so Character glowed on a game nobody had touched.
  function blockIsChanged(blockId) {
    const spec = (B.identity() || {})[blockId] || {};
    const base = (B.identityDefaults() || {})[blockId] || {};
    const schema = B.identityBlock(blockId);
    if (!schema) return false;
    return (schema.fields || []).some((f) => {
      const now = spec[f.id];
      const was = base[f.id];
      if (f.type === "toggle") return !!now !== !!was;
      // A blank field and a missing one are the same thing to the compiler, so
      // they must not read as a change here either.
      return String(now == null ? "" : now).trim() !==
             String(was == null ? "" : was).trim();
    });
  }

  // Same question for a dot backed by a prompt rather than a sheet.
  function promptIsChanged(key) {
    if (!key || !B.fieldById(key)) return false;
    return String(B.valOf(key) || "").trim() !== String(B.defOf(key) || "").trim();
  }

  // One size per level, a step smaller each level down.
  function scaleAt(depth) { return Math.pow(DEPTH_SHRINK, depth); }
  function dotRadius(depth) { return DOT_R * scaleAt(depth); }

  function clipLabel(s) {
    const t = String(s || "").trim() || "World";
    return t.length <= MAX_CHARS ? t : t.slice(0, MAX_CHARS).trim();
  }

  function isPlaced(n) {
    return !!(n && (n.kind === "world-node" || n.kind === "cutscene-node"
      || n.kind === "experience-node" || n.kind === "lore-node")
      && n.homeX != null && n.homeY != null);
  }

  function isLinkable(n) {
    return !!(n && (n.kind === "world-node" || n.kind === "cutscene-node"));
  }

  function graphId(n) {
    if (!n) return null;
    return n.graphId || n.worldId || n.cutsceneId || null;
  }

  function graphNodeOf(id) {
    return nodesById["world:" + id] || nodesById["cutscene:" + id] || null;
  }

  function defaultExperienceHome() {
    return { x: -DOT_R * 2.45, y: DOT_R * 1.35 };
  }

  function defaultLoreHome(xpHome) {
    const base = xpHome || defaultExperienceHome();
    return { x: base.x - DOT_R * 2.15, y: base.y - DOT_R * 2.05 };
  }

  function loreState() {
    const exp = experienceState();
    return (exp && exp.lore) || { enabled: true, notes: "", documents: [] };
  }

  function loreRichness(lore) {
    const data = lore || loreState();
    const notes = String(data.notes || "");
    const docs = data.documents || [];
    const chars = notes.length + docs.reduce((n, d) => n + (d.chars || 0), 0);
    return {
      notes: notes,
      docs: docs,
      chars: chars,
      count: docs.length + (notes.trim() ? 1 : 0),
      rich: !!(notes.trim() || docs.length),
    };
  }

  function isGenericWorldName(s) {
    return /^(world|new level)( \d+)?$/i.test(String(s || "").trim());
  }

  function inheritChanged(n) {
    (n.children || []).forEach(inheritChanged);
    if ((n.children || []).length) n.changed = n.children.some((k) => k.changed);
  }

  function soundIsChanged(d) {
    const cfg = (B && B.sound && B.sound()) || {};
    if (d.kind === "sound-palette") return String(cfg.palette || "tape") !== "tape";
    if (d.family) return (cfg.muted || []).indexOf(d.family) >= 0;
    return false;
  }

  const DEFAULT_BEAT_NORMAL = "BEAT: put a character or a direct threat in view early — a patrol, a figure, a voice — don't let the opening stay empty.";
  const DEFAULT_BEAT_ESCALATING = "BEAT: pressure is rising. Push the situation forward.";
  const DEFAULT_BEAT_CRITICAL = "BEAT: the situation is critical. Offer a way through or a last stand.";

  function pacingState() {
    const exp = (B && B.experience && B.experience()) || {};
    const t = exp.threat || {};
    return {
      escalate_at: t.escalate_at == null ? 3 : Number(t.escalate_at),
      critical_at: t.critical_at == null ? 6 : Number(t.critical_at),
      beat_normal: String(t.beat_normal || DEFAULT_BEAT_NORMAL),
      beat_escalating: String(t.beat_escalating || DEFAULT_BEAT_ESCALATING),
      beat_critical: String(t.beat_critical || DEFAULT_BEAT_CRITICAL),
    };
  }

  function pacingIsChanged() {
    const t = pacingState();
    return Number(t.escalate_at) !== 3
      || Number(t.critical_at) !== 6
      || String(t.beat_normal || "").trim() !== DEFAULT_BEAT_NORMAL
      || String(t.beat_escalating || "").trim() !== DEFAULT_BEAT_ESCALATING
      || String(t.beat_critical || "").trim() !== DEFAULT_BEAT_CRITICAL;
  }

  function specDots(prefix, defs) {
    const spec = (d) => ({
      id: prefix + "dot:" + d.id,
      kind: d.kind || "spec",
      label: d.label,
      sub: d.sub,
      weight: d.weight || 1,
      beat: d.beat || 0,
      wait: !!d.wait,
      block: d.block || null,
      prompt: d.prompt || null,
      family: d.family || null,
      changed: d.block ? blockIsChanged(d.block)
        : (d.kind === "sound-palette" || d.kind === "sound-family")
          ? soundIsChanged(d)
          : (d.kind === "pacing")
            ? pacingIsChanged()
            : promptIsChanged(d.prompt),
      children: (d.children || []).map(spec),
    });
    const kids = (defs || DOTS).map(spec);
    inheritChanged({ children: kids });
    return kids;
  }

  function experienceState() {
    return (B && B.experience && B.experience()) || { worlds: [], transitions: [] };
  }

  function currentWorldId(exp) {
    const worlds = (exp && exp.worlds) || [];
    const edit = B && B.editingWorldId && B.editingWorldId();
    if (edit && worlds.some((w) => w.id === edit)) return edit;
    return (exp && exp.start_world) || (worlds[0] && worlds[0].id) || null;
  }

  function liveWorldId() {
    return (B && B.liveWorldId && B.liveWorldId()) || null;
  }

  function adoptTree(tree) {
    layout(tree);
    nodesById = Object.create(null);
    (function walk(n, parent) {
      n.parent = parent || null;
      nodesById[n.id] = n;
      (n.children || []).forEach((k) => walk(k, n));
    })(tree, null);
    return tree;
  }

  function buildExperienceTree() {
    const exp = experienceState();
    const worlds = exp.worlds || [];
    const curId = currentWorldId(exp);
    const hereId = liveWorldId();
    // Experience is a placeable coin. Worlds sit beside it. The root is
    // only the origin you open — a nucleus, not the Experience itself.
    // The interior (Level / Character / Gameplay) lives on the World you
    // have entered, so diving in is a real second level.
    const kids = worlds.map((w) => {
      const editing = w.id === curId;
      const node = {
        id: "world:" + w.id,
        kind: "world-node",
        label: clipLabel(w.name),
        fullName: w.name,
        blurb: w.blurb || "",
        sub: w.blurb
          || (w.id === exp.start_world ? "Start." : "Name this place, then edit it."),
        worldId: w.id,
        graphId: w.id,
        slug: w.slug || "",
        frameUrl: ((w.frame_source === "plate" || w.frame_source === "placeholder")
          ? "" : (w.frame_url || "")),
        frameStatus: w.frame_status || "",
        frameGenerating: !!w.frame_generating,
        frameSource: w.frame_source || "",
        isStart: w.id === exp.start_world,
        isEditing: editing,
        isHere: !!(hereId && w.id === hereId),
        changed: false,
        children: editing ? specDots("") : [],
      };
      if (typeof w.x === "number" && typeof w.y === "number") {
        node.homeX = w.x;
        node.homeY = w.y;
      }
      return node;
    });
    const cuts = (exp.cutscenes || []).map((c) => {
      const node = {
        id: "cutscene:" + c.id,
        kind: "cutscene-node",
        label: clipLabel(c.name),
        fullName: c.name,
        blurb: c.blurb || "",
        sub: c.blurb
          || (c.id === exp.start_world ? "Start." : ((c.mood || "threshold") + " montage.")),
        cutsceneId: c.id,
        graphId: c.id,
        mood: c.mood || "threshold",
        source: c.source || "incoming",
        shotBrief: c.shot_brief || "",
        isStart: c.id === exp.start_world,
        changed: false,
        children: [],
      };
      if (typeof c.x === "number" && typeof c.y === "number") {
        node.homeX = c.x;
        node.homeY = c.y;
      }
      return node;
    });
    inheritChanged({ children: kids });
    const title = String((exp && exp.name) || "").trim() || "Experience";
    const pinned = typeof exp.x === "number" && typeof exp.y === "number";
    const home = pinned ? { x: exp.x, y: exp.y } : defaultExperienceHome();
    const xp = {
      id: "xp",
      kind: "experience-node",
      label: clipLabel(title),
      fullName: title,
      sub: title,
      homeX: home.x,
      homeY: home.y,
      children: [],
    };
    const lore = loreState();
    const rich = loreRichness(lore);
    const lorePinned = typeof lore.x === "number" && typeof lore.y === "number";
    const loreHome = lorePinned ? { x: lore.x, y: lore.y } : defaultLoreHome(home);
    const loreNode = {
      id: "lore",
      kind: "lore-node",
      label: "Lore",
      fullName: "Lore",
      sub: rich.rich
        ? (rich.count === 1 ? "1 piece of background." : rich.count + " pieces of background.")
        : "Drop the history of this place.",
      homeX: loreHome.x,
      homeY: loreHome.y,
      loreChars: rich.chars,
      loreCount: rich.count,
      loreDocs: rich.docs,
      loreNotes: rich.notes,
      isRich: rich.rich,
      children: [],
    };
    return adoptTree({
      id: "experience",
      kind: "root",
      label: clipLabel(title),
      fullName: title,
      sub: title,
      children: [xp, loreNode].concat(kids, cuts),
    });
  }

  function buildHarnessTree() {
    return adoptTree({
      id: "harness",
      kind: "root",
      label: "Harness",
      sub: "The engine's contract.",
      children: specDots("h:", HARNESS_DOTS),
    });
  }

  function buildSoundTree() {
    return adoptTree({
      id: "sound",
      kind: "root",
      label: "Sound",
      sub: "The machine's voice.",
      children: specDots("s:", SOUND_DOTS),
    });
  }

  function buildTree() {
    const next = currentSurface();
    if (next !== surfaceKind) {
      openBySurface[surfaceKind] = openId;
      camBySurface[surfaceKind] = userCam;
      surfaceKind = next;
      openId = Object.prototype.hasOwnProperty.call(openBySurface, next)
        ? openBySurface[next] : null;
      userCam = Object.prototype.hasOwnProperty.call(camBySurface, next)
        ? camBySurface[next] : null;
      if (sheetId) closeSheet();
      setTool("select");
    }
    if (next === "harness") return buildHarnessTree();
    if (next === "sound") return buildSoundTree();
    return buildExperienceTree();
  }

  // ══════════════════════════════════════════════════════════════════
  // GEOMETRY — a nucleus, and its children on one ring
  //
  // Each dot is only as big as its own word. The ring is then sized so the
  // widest pair of neighbours still clears, which means the layout adapts if
  // the labels ever change without anyone tuning a constant.
  // ══════════════════════════════════════════════════════════════════
  // Every container lays its children out on a ring around the ORIGIN, because
  // whichever node is open is always the one in the middle. A satellite's home
  // is an angle on that ring; the frame loop turns the whole ring together.
  function layout(node, depth) {
    depth = depth || 0;
    node.depth = depth;
    // A node's own size is the size of the level it LIVES on; its children are
    // one step smaller, and its ring is sized for them.
    // Weight is for the four overview coins (depth 1). Once you dive, the
    // open node is the nucleus — inflating it shoved its knobs off a phone.
    const beat = isHarness() && depth === 1 && node.id !== openId;
    node.r = dotRadius(depth) * (beat && node.weight ? node.weight : 1);
    node.label_size = LABEL * scaleAt(depth);
    node.cx = 0;
    node.cy = 0;
    node.slotAngle = node.slotAngle || 0;
    node.rad = node.rad == null ? 0 : node.rad;
    node.targetRad = 0;
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
      k.slotAngle = top + (i / kids.length) * Math.PI * 2;
      k.orbitR = ring;
      if (isPlaced(k)) {
        k.cx = k.homeX;
        k.cy = k.homeY;
        k.rad = Math.hypot(k.cx, k.cy);
        k.targetRad = k.rad;
        return;
      }
      k.rad = ring;
      k.targetRad = ring;
      k.cx = Math.cos(k.slotAngle) * ring;
      k.cy = Math.sin(k.slotAngle) * ring;
    });
    if (node.id === "harness") rememberHarnessHomes(node);
  }

  // The turn as one clock, in play order, sitting in the left wash.
  //   Choices → Actions
  //      ↑         ↓
  //    State  ← Picture
  // Children stay inside those nodes until you dive.
  function pinHome(n, x, y) {
    n.homeX = x;
    n.homeY = y;
  }

  function harnessDot(h, id) {
    const want = "h:dot:" + id;
    return flattenKids(h).find((n) => n.id === want) || null;
  }

  function rememberHarnessHomes(h) {
    const COL = DOT_R * 2.55;
    const ROW = DOT_R * 2.45;
    const choices = harnessDot(h, "choices");
    const actions = harnessDot(h, "actions");
    const picture = harnessDot(h, "picture");
    const state = harnessDot(h, "state");
    if (choices) pinHome(choices, -COL, -ROW);
    if (actions) pinHome(actions, COL, -ROW);
    if (picture) pinHome(picture, COL, ROW);
    if (state) pinHome(state, -COL, ROW);
    relaxHarness(h);
    fitHarness(h);
  }

  function harnessLoop(h) {
    return (h && h.children) || [];
  }

  function relaxHarness(h) {
    const nodes = harnessLoop(h);
    const byShort = Object.create(null);
    nodes.forEach((n) => {
      byShort[n.id.replace(/^h:dot:/, "")] = n;
      n._vx = 0;
      n._vy = 0;
    });
    const pins = Object.create(null);
    ["choices", "actions", "picture", "state"].forEach((id) => {
      const n = byShort[id];
      if (n) pins[id] = { x: n.homeX, y: n.homeY };
    });
    const REPEL = DOT_R * DOT_R * 48;
    const PIN_K = 0.055;
    const PARENT_K = 0.18;
    const DAMP = 0.68;
    function restFor(a, b, e) {
      const clear = a.r + b.r;
      if (e.kind === "cluster") return clear + DOT_R * 0.4;
      if (e.route === "return") return clear + DOT_R * 5.4;
      if (e.route === "branch") return clear + DOT_R * 2.55;
      return clear + DOT_R * 2.2;
    }
    function stiffness(e) {
      if (e.kind === "cluster") return 0.14;
      if (e.route === "return") return 0.018;
      if (e.route === "branch") return 0.05;
      return 0.07;
    }
    for (let step = 0; step < 90; step++) {
      nodes.forEach((n) => { n._fx = 0; n._fy = 0; });
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const a = nodes[i], b = nodes[j];
          let dx = b.homeX - a.homeX, dy = b.homeY - a.homeY;
          let dist = Math.hypot(dx, dy) || 0.01;
          const minD = (a.r + b.r) * 1.2;
          const f = REPEL / (dist * dist) + (dist < minD ? (minD - dist) * 0.42 : 0);
          dx /= dist;
          dy /= dist;
          a._fx -= f * dx;
          a._fy -= f * dy;
          b._fx += f * dx;
          b._fy += f * dy;
        }
      }
      nodes.forEach((n) => {
        const dist = Math.hypot(n.homeX, n.homeY) || 0.01;
        const minR = DOT_R * 1.45 + n.r;
        if (dist < minR) {
          const f = (minR - dist) * 0.45;
          n._fx += (n.homeX / dist) * f;
          n._fy += (n.homeY / dist) * f;
        }
      });
      HARNESS_FLOW.forEach((e) => {
        const a = byShort[e.from], b = byShort[e.to];
        if (!a || !b) return;
        let dx = b.homeX - a.homeX, dy = b.homeY - a.homeY;
        const dist = Math.hypot(dx, dy) || 0.01;
        const f = (dist - restFor(a, b, e)) * stiffness(e);
        dx /= dist;
        dy /= dist;
        a._fx += f * dx;
        a._fy += f * dy;
        b._fx -= f * dx;
        b._fy -= f * dy;
      });
      nodes.forEach((n) => {
        (n.children || []).forEach((k) => {
          if (k.homeX == null || k.homeY == null) return;
          let dx = k.homeX - n.homeX, dy = k.homeY - n.homeY;
          const dist = Math.hypot(dx, dy) || 0.01;
          const rest = n.r + k.r + DOT_R * 0.38;
          const f = (dist - rest) * PARENT_K;
          dx /= dist;
          dy /= dist;
          n._fx += f * dx;
          n._fy += f * dy;
          k._fx -= f * dx;
          k._fy -= f * dy;
        });
      });
      Object.keys(pins).forEach((id) => {
        const n = byShort[id];
        if (!n) return;
        n._fx += (pins[id].x - n.homeX) * PIN_K;
        n._fy += (pins[id].y - n.homeY) * PIN_K;
      });
      nodes.forEach((n) => {
        n._vx = (n._vx + n._fx) * DAMP;
        n._vy = (n._vy + n._fy) * DAMP;
        pinHome(n, n.homeX + n._vx, n.homeY + n._vy);
      });
    }
    nodes.forEach((n) => {
      delete n._fx;
      delete n._fy;
      delete n._vx;
      delete n._vy;
    });
  }

  function fitHarness(h) {
    const nodes = harnessLoop(h);
    if (!nodes.length) {
      harnessFrame = { cx: 0, cy: 0, half: HARNESS_VIEW_HALF };
      return;
    }
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    nodes.forEach((n) => {
      minX = Math.min(minX, n.homeX - n.r);
      minY = Math.min(minY, n.homeY - n.r);
      maxX = Math.max(maxX, n.homeX + n.r);
      maxY = Math.max(maxY, n.homeY + n.r);
    });
    harnessFrame = {
      cx: (minX + maxX) / 2,
      cy: (minY + maxY) / 2,
      minX: minX,
      half: Math.max(maxX - minX, maxY - minY) / 2 + DOT_R * 1.65,
    };
  }

  function applyHarnessHomes() {
    if (!isHarnessOverview() || !root) return;
    harnessLoop(root).forEach((n) => {
      if (n.homeX == null) return;
      n.cx = n.homeX;
      n.cy = n.homeY;
      n.rad = Math.hypot(n.homeX, n.homeY);
      n.targetRad = n.rad;
      n.holdAngle = null;
    });
  }

  // ══════════════════════════════════════════════════════════════════
  // ORBIT — one ring, one spin, nucleus at the origin
  // ══════════════════════════════════════════════════════════════════
  function visibleNodes() {
    if (!root) return [];
    if (openId === null) return [root];
    const f = nodesById[openId] || root;
    // Overview is the turn itself. Drawing the Harness nucleus in the
    // middle of that square made Picture look like a hole, not a beat.
    if (isHarnessOverview()) return f.children || [];
    return [f].concat(f.children || []);
  }

  function stagedNodes() {
    const live = visibleNodes();
    const extra = [];
    Object.keys(leaving).forEach((id) => {
      const n = nodesById[id];
      if (n && live.indexOf(n) === -1) extra.push(n);
    });
    return live.concat(extra);
  }

  function polar(n, angle, rad) {
    n.cx = Math.cos(angle) * rad;
    n.cy = Math.sin(angle) * rad;
  }

  function stepOrbit(now) {
    if (!lastTick) lastTick = now;
    const dt = Math.min(0.05, (now - lastTick) / 1000);
    lastTick = now;

    const f = nodesById[openId] || root;
    const ring = (openId === null) ? 0 : (f.ring || 0);
    const ease = reduceMotion() ? 1 : RAD_EASE;
    const freezeSpin = isHarness() || (f.children || []).some((k) => k.kind === "world-node");
    if (openId !== null && !reduceMotion() && !freezeSpin) {
      spin += (Math.PI * 2 / ORBIT_REV_S) * dt;
    }

    // Nucleus sits at the origin. If it just arrived from the ring (you dove
    // into it), it keeps the angle it had and eases its radius in — that is
    // the hierarchy, not a teleport.
    f.targetRad = 0;
    if (f.holdAngle == null && (f.rad || 0) > 1) {
      f.holdAngle = Math.atan2(f.cy, f.cx);
    }
    f.rad += (f.targetRad - f.rad) * ease;
    if (Math.abs(f.rad) < 0.4) { f.rad = 0; f.holdAngle = null; }
    polar(f, f.holdAngle || 0, f.rad);

    if (openId !== null) {
      const kids = f.children || [];
      kids.forEach((n) => {
        if (isHarnessOverview() && n.homeX != null && n.homeY != null) {
          n.cx = n.homeX;
          n.cy = n.homeY;
          n.rad = Math.hypot(n.cx, n.cy);
          n.targetRad = n.rad;
          n.holdAngle = null;
          return;
        }
        if (isPlaced(n)) {
          n.cx = n.homeX;
          n.cy = n.homeY;
          n.rad = Math.hypot(n.cx, n.cy);
          n.targetRad = n.rad;
          n.holdAngle = null;
          return;
        }
        n.targetRad = ring;
        n.holdAngle = null;
        n.rad += (n.targetRad - n.rad) * ease;
        polar(n, n.slotAngle + spin, n.rad);
      });
    }

    // Dots that are leaving fall into the nucleus along the angle they had,
    // so going up or down is the same motion every time — no leftover shove.
    Object.keys(leaving).forEach((id) => {
      const n = nodesById[id];
      if (!n || n === f) return;
      n.targetRad = 0;
      if (n.holdAngle == null) n.holdAngle = Math.atan2(n.cy, n.cx);
      n.rad += (0 - n.rad) * ease;
      polar(n, n.holdAngle, n.rad);
    });
  }

  function place() {
    stagedNodes().forEach((n) => {
      if (n.g) n.g.setAttribute("transform",
        "translate(" + n.cx.toFixed(2) + " " + n.cy.toFixed(2) + ")");
    });
    stepPorts();
    placePorts();
    placeHulls();
    placeEdges();
  }

  function loop(now) {
    stepOrbit(now);
    place();
    raf = requestAnimationFrame(loop);
  }

  function startWobble() {
    // Always tick: ports chase the mouse even when the ring itself is still.
    if (raf) { place(); return; }
    lastTick = 0;
    raf = requestAnimationFrame(loop);
  }

  function stopWobble() {
    if (raf) cancelAnimationFrame(raf);
    raf = null;
  }

  // ══════════════════════════════════════════════════════════════════
  // DRAWING — every dot is drawn at the origin and moved by a transform, so
  // the orbit costs one attribute per dot per frame.
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
  function frameBusy(n) {
    if (!n || n.kind !== "world-node") return false;
    if (n.frameHoldUntil && Date.now() < n.frameHoldUntil) return true;
    const st = n.frameStatus || "";
    return st === "generating" || st === "dirty" || !!n.frameGenerating;
  }

  function applyFrameStatus(n) {
    if (!n || !n.g || n.kind !== "world-node") return;
    const busy = frameBusy(n);
    n.g.classList.toggle("is-rendering", busy);
    n.g.classList.toggle("has-frame", !!n.frameUrl);
    let mark = n.gi && n.gi.querySelector(".eg-render-mark");
    if (!busy) {
      if (mark) mark.remove();
      return;
    }
    const r = n.r || DOT_R;
    if (!mark && n.gi) {
      mark = mk("g", null, "eg-render-mark");
      mark.appendChild(mk("circle", {
        cx: 0, cy: 0, r: r * 0.92, fill: "none",
        "stroke-width": Math.max(1.6, r * 0.05),
      }, "eg-render-ring"));
      mark.appendChild(mk("circle", { cx: 0, cy: 0, r: r * 0.55 }, "eg-render-glow"));
      const label = mk("text", {
        x: 0, y: r * 0.06, "font-size": Math.max(7.5, r * 0.17),
      }, "eg-render-label");
      label.textContent = "RENDERING";
      mark.appendChild(label);
      n.gi.appendChild(mark);
    }
  }

  function applyWorldFrame(n) {
    if (!n || !n.gi || n.kind !== "world-node") return;
    const url = n.frameUrl || "";
    let img = n.gi.querySelector("image.eg-world-frame");
    const cell = n.gi.querySelector("circle.eg-cell");
    if (!url) {
      if (img) img.remove();
      if (n.g) n.g.classList.remove("has-frame");
      applyFrameStatus(n);
      return;
    }
    const r = n.r || DOT_R;
    const d = r * 2;
    if (!img) {
      img = mk("image", {
        x: -r,
        y: -r,
        width: d,
        height: d,
        preserveAspectRatio: "xMidYMid slice",
      }, "eg-world-frame");
      if (n.clipId) img.setAttribute("clip-path", "url(#" + n.clipId + ")");
      if (cell) n.gi.insertBefore(img, cell);
      else n.gi.insertBefore(img, n.gi.firstChild);
    }
    const prev = img.getAttribute("href")
      || img.getAttributeNS("http://www.w3.org/1999/xlink", "href")
      || "";
    if (prev !== url) {
      img.classList.add("is-swapping");
      const land = () => {
        img.classList.remove("is-swapping");
        if (!n.g || frameBusy(n)) return;
        n.g.classList.add("is-frame-fresh");
        clearTimeout(n._freshT);
        n._freshT = setTimeout(() => {
          if (n.g) n.g.classList.remove("is-frame-fresh");
        }, 980);
      };
      img.addEventListener("load", land, { once: true });
      setTimeout(land, 700);
      img.setAttribute("href", url);
      img.setAttributeNS("http://www.w3.org/1999/xlink", "href", url);
    }
    if (n.g) n.g.classList.add("has-frame");
    applyFrameStatus(n);
  }

  function syncFrames() {
    const worlds = ((experienceState().worlds) || []);
    const byId = Object.create(null);
    worlds.forEach((w) => { byId[w.id] = w; });
    Object.keys(nodesById).forEach((k) => {
      const n = nodesById[k];
      if (!n || n.kind !== "world-node") return;
      const w = byId[n.worldId];
      const source = (w && w.frame_source) || "";
      const url = (source === "plate" || source === "placeholder")
        ? ""
        : ((w && w.frame_url) || "");
      const status = (w && w.frame_status) || "";
      const generating = !!(w && w.frame_generating);
      if (url === (n.frameUrl || "")
          && status === (n.frameStatus || "")
          && generating === !!n.frameGenerating
          && source === (n.frameSource || "")) {
        applyFrameStatus(n);
        return;
      }
      n.frameUrl = url;
      n.frameStatus = status;
      n.frameGenerating = generating;
      n.frameSource = source;
      applyWorldFrame(n);
    });
  }

  function markRendering(worldId) {
    const exp = experienceState();
    const target = worldId || currentWorldId(exp);
    if (!target) return;
    (exp.worlds || []).forEach((w) => {
      if (w.id !== target) return;
      w.frame_status = "generating";
      w.frame_generating = true;
    });
    Object.keys(nodesById).forEach((k) => {
      const n = nodesById[k];
      if (!n || n.kind !== "world-node" || n.worldId !== target) return;
      n.frameStatus = "generating";
      n.frameGenerating = true;
      n.frameHoldUntil = Date.now() + 640;
      clearTimeout(n._holdT);
      n._holdT = setTimeout(() => applyFrameStatus(n), 680);
      applyFrameStatus(n);
    });
    paint();
  }

  function drawNode(n, index) {
    const g = mk("g", { "data-id": n.id }, "eg-node eg-kind-" + n.kind);
    const inner = mk("g", null, "eg-node-in");
    // Stagger, so the ring blooms as a sequence rather than a flashbulb.
    inner.style.setProperty("--i", String(index || 0));
    if (n.kind === "world-node") {
      const clipId = "eg-wclip-" + String(n.worldId || n.id).replace(/[^A-Za-z0-9_-]/g, "");
      n.clipId = clipId;
      const defs = mk("defs");
      const clip = mk("clipPath", { id: clipId });
      // Image sits inside the cell stroke so the mint ring surrounds
      // the still instead of cutting through it.
      clip.appendChild(mk("circle", { cx: 0, cy: 0, r: n.r * 0.97 }));
      defs.appendChild(clip);
      inner.appendChild(defs);
    }
    inner.appendChild(mk("circle", {
      cx: 0, cy: 0, r: (n.r || DOT_R) * 1.32,
    }, "eg-pick"));
    inner.appendChild(mk("circle", { cx: 0, cy: 0, r: n.r }, "eg-cell"));
    if (n.beat) {
      const beat = mk("text", {
        x: 0,
        y: -((n.r || DOT_R) + LABEL * 0.42),
        "font-size": LABEL * 0.42,
      }, "eg-beat");
      beat.textContent = String(n.beat);
      inner.appendChild(beat);
    }
    const t = mk("text", { x: 0, y: 0, "font-size": n.label_size || LABEL }, "eg-name");
    t.textContent = n.label;
    inner.appendChild(t);
    if (n.wait) {
      const wait = mk("text", {
        x: 0,
        y: LABEL * 0.78,
        "font-size": LABEL * 0.32,
      }, "eg-wait");
      wait.textContent = "WAIT";
      inner.appendChild(wait);
    }
    if (n.kind === "lore-node") {
      inner.appendChild(mk("circle", { cx: 0, cy: 0, r: n.r * 0.78 }, "eg-lore-well"));
      const ring = mk("circle", { cx: 0, cy: 0, r: n.r * 0.92 }, "eg-lore-ring");
      inner.appendChild(ring);
      const bits = mk("g", null, "eg-lore-bits");
      inner.appendChild(bits);
      const cap = mk("text", {
        x: 0,
        y: (n.r || DOT_R) + LABEL * 1.15,
        "font-size": LABEL * 0.84,
      }, "eg-world-cap");
      cap.textContent = "LORE";
      inner.appendChild(cap);
    }
    if (n.kind === "world-node") {
      const cap = mk("text", {
        x: 0,
        y: (n.r || DOT_R) + LABEL * 1.15,
        "font-size": LABEL * 0.84,
      }, "eg-world-cap");
      cap.textContent = n.fullName || n.label;
      inner.appendChild(cap);
      const here = mk("text", {
        x: 0,
        y: -((n.r || DOT_R) + LABEL * 0.28),
        "font-size": LABEL * 0.32,
      }, "eg-here");
      here.textContent = "HERE";
      inner.appendChild(here);
      const start = mk("text", {
        x: 0,
        y: (n.r || DOT_R) + LABEL * 1.58,
        "font-size": LABEL * 0.32,
      }, "eg-start");
      start.textContent = "START";
      inner.appendChild(start);
    }
    if (n.kind === "cutscene-node") {
      const cap = mk("text", {
        x: 0,
        y: (n.r || DOT_R) + LABEL * 1.15,
        "font-size": LABEL * 0.84,
      }, "eg-world-cap");
      cap.textContent = n.fullName || n.label;
      inner.appendChild(cap);
      const face = mk("g", null, "eg-cut-face");
      const s = n.r * 0.78;
      face.appendChild(mk("rect", {
        x: -s, y: -s * 0.72, width: s * 2, height: s * 1.44, rx: s * 0.08,
      }, "eg-cut-face"));
      const gap = s * 0.08;
      const cell = (s * 2 - gap * 3) / 2;
      for (let r = 0; r < 2; r++) {
        for (let c = 0; c < 2; c++) {
          face.appendChild(mk("rect", {
            x: -s + gap + c * (cell + gap),
            y: -s * 0.72 + gap + r * (cell * 0.72 + gap),
            width: cell,
            height: cell * 0.72,
            rx: 1.2,
          }, "eg-cut-cell"));
        }
      }
      inner.appendChild(face);
      const tag = mk("text", {
        x: 0,
        y: (n.r || DOT_R) + LABEL * 1.58,
        "font-size": LABEL * 0.32,
      }, "eg-start");
      tag.textContent = n.isStart ? "START" : "CUT";
      tag.style.opacity = "1";
      inner.appendChild(tag);
    }
    g.appendChild(inner);
    n.g = g;
    n.gi = inner;
    if (n.kind === "world-node") applyWorldFrame(n);
    if (n.kind === "lore-node") applyLoreFace(n);
    return g;
  }

  function applyLoreFace(n) {
    if (!n || !n.gi || n.kind !== "lore-node") return;
    const bits = n.gi.querySelector(".eg-lore-bits");
    const ring = n.gi.querySelector(".eg-lore-ring");
    if (bits) {
      bits.innerHTML = "";
      const docs = n.loreDocs || [];
      const notes = String(n.loreNotes || "").trim();
      const pieces = docs.slice();
      if (notes) pieces.unshift({ id: "notes", kind: "notes", name: "notes" });
      const max = Math.min(pieces.length, 10);
      for (let i = 0; i < max; i++) {
        const ang = -Math.PI / 2 + (Math.PI * 2 * i) / Math.max(max, 3);
        const rad = n.r * (0.42 + (i % 2) * 0.12);
        const dot = mk("circle", {
          cx: Math.cos(ang) * rad,
          cy: Math.sin(ang) * rad,
          r: n.r * (pieces[i].kind === "image" ? 0.11 : 0.08),
        }, "eg-lore-bit" + (pieces[i].kind === "image" ? " is-image" : "")
          + (pieces[i].kind === "notes" ? " is-notes" : ""));
        bits.appendChild(dot);
      }
    }
    if (ring) {
      const circ = 2 * Math.PI * (n.r * 0.92);
      const fill = Math.min(1, (n.loreChars || 0) / 4000);
      ring.style.strokeDasharray = circ.toFixed(1);
      ring.style.strokeDashoffset = (circ * (1 - fill)).toFixed(1);
    }
    if (n.g) n.g.classList.toggle("is-rich", !!n.isRich);
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
    let shellR = SHELL_R;
    if (isHarness() && root) {
      let max = 0;
      harnessLoop(root).forEach((n) => {
        const d = Math.hypot(n.homeX || 0, n.homeY || 0) + (n.r || 0);
        if (d > max) max = d;
      });
      shellR = Math.max(SHELL_R, max * 1.22);
    }
    els.world.appendChild(mk("circle", { cx: 0, cy: 0, r: shellR }, "eg-shell"));
    hullHost();

    // No tethers. Spokes from the middle to every satellite drew the one
    // relationship you can already see (these things are inside that thing) and
    // turned a constellation into a wheel.
    const layer = mk("g", null, "eg-nodes");
    (function walk(n, index) {
      layer.appendChild(drawNode(n, index));
      (n.children || []).forEach(walk);
    })(root, 0);
    els.world.appendChild(layer);
    rebuildEdges();
    applyHarnessHomes();
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
      g.classList.toggle("is-changed", !!n.changed);
      g.classList.toggle("is-open", n.id === sheetId);
      g.classList.toggle("is-start", !!n.isStart);
      g.classList.toggle("is-here", !!n.isHere);
      g.classList.toggle("has-frame", !!(n.kind === "world-node" && n.frameUrl));
      g.classList.toggle("is-rich", !!(n.kind === "lore-node" && n.isRich));
      g.classList.toggle("is-rendering", frameBusy(n));
      g.classList.toggle("is-selected", n.id === selectId);
      g.classList.toggle("is-from", !!(connectFrom && graphId(n) === connectFrom));
      g.classList.toggle("is-drop", !!(ptr && ptr.mode === "connect" &&
        ptr.dropId && graphId(n) === ptr.dropId));
      paintPort(n);
      paintKill(n);
    });
    if (els.graph) {
      els.graph.classList.toggle("is-collapsed", collapsed);
      els.graph.classList.toggle("is-worlds", onExperienceRing());
      els.graph.classList.toggle("is-flow", isHarnessOverview());
      els.graph.classList.toggle("can-surface", paperSurfaces());
      if (!paperSurfaces()) els.graph.classList.remove("is-paper-hot");
    }
    renderHud();
    place();
  }

  function markWorldDrop(worldId) {
    if (!els.world) return;
    els.world.querySelectorAll(".eg-kind-world-node, .eg-kind-cutscene-node").forEach((g) => {
      const n = nodesById[g.getAttribute("data-id")];
      g.classList.toggle("is-drop", !!(worldId && n && graphId(n) === worldId));
    });
  }

  // Whatever was on stage and isn't any more gets to wilt, then disappear.
  function beginLeave(previous) {
    const keep = Object.create(null);
    visibleNodes().forEach((n) => { keep[n.id] = true; });
    leaving = Object.create(null);
    previous.forEach((n) => { if (!keep[n.id]) leaving[n.id] = true; });
    clearTimeout(leaveTimer);
    leaveTimer = setTimeout(() => {
      Object.keys(leaving).forEach((id) => {
        const n = nodesById[id];
        if (n) n.holdAngle = null;
      });
      leaving = Object.create(null);
      paint();
    }, reduceMotion() ? 0 : LEAVE_MS);
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

  function rememberCam() {
    if (!view) return;
    if (isHarnessOverview() || onExperienceRing()) {
      userCam = { cx: view.cx, cy: view.cy, half: view.half };
    }
  }

  function stopViewAnim() {
    if (anim) { cancelAnimationFrame(anim.raf); anim = null; }
    setZooming(false);
  }

  function clamp(n, lo, hi) {
    return Math.max(lo, Math.min(hi, n));
  }

  function zoomAt(clientX, clientY, factor) {
    if (!view || openId === null) return;
    const world = toWorld(clientX, clientY);
    if (!world) return;
    stopViewAnim();
    const nextHalf = clamp(view.half * factor, DOT_R * 1.55, DOT_R * 18);
    if (Math.abs(nextHalf - view.half) < 0.01) return;
    const t = 1 - nextHalf / view.half;
    applyView({
      cx: view.cx + (world.x - view.cx) * t,
      cy: view.cy + (world.y - view.cy) * t,
      half: nextHalf,
    });
    rememberCam();
  }

  function onWheel(evt) {
    if (!B || !B.isGraphMode || !B.isGraphMode()) return;
    if (openId === null) return;
    evt.preventDefault();
    zoomAt(evt.clientX, evt.clientY, evt.deltaY > 0 ? 1.14 : 1 / 1.14);
  }

  // Place a world point at `frac` of the frame width (0 = left, 0.5 = centre).
  // The offset is in view space so a wide still keeps the graph in the wash.
  function lookAt(worldX, worldY, half, frac) {
    const box = els.canvas ? els.canvas.getBoundingClientRect() : { width: 1, height: 1 };
    const w = box.width || 1, h = box.height || 1;
    const hx = w >= h ? half * (w / h) : half;
    const f = (frac == null) ? STAGE_LEFT : frac;
    return {
      cx: worldX + hx * (1 - 2 * f),
      cy: worldY,
      half: half,
    };
  }

  // One window, every depth. Fitting it to the open ring is what made the depth
  // shrink invisible: a smaller ring in a smaller frame is the same picture.
  function frameFor() {
    if (openId === null) return lookAt(0, 0, root.r * COLLAPSED_ZOOM);
    if (isHarnessOverview()) {
      if (userCam) return { cx: userCam.cx, cy: userCam.cy, half: userCam.half };
      if (harnessFrame.minX != null) {
        return lookAt(harnessFrame.minX, harnessFrame.cy, harnessFrame.half, STAGE_INSET);
      }
      return lookAt(harnessFrame.cx, harnessFrame.cy, harnessFrame.half);
    }
    if (onExperienceRing()) {
      if (userCam) return { cx: userCam.cx, cy: userCam.cy, half: userCam.half };
      return lookAt(0, 0, WORLD_VIEW_HALF);
    }
    return lookAt(0, 0, VIEW_HALF);
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
    if (id === null) {
      userCam = null;
      camBySurface[surfaceKind] = null;
    }
    const before = visibleNodes();
    openId = id;
    const f = nodesById[openId] || root;
    const ring = (openId === null) ? 0 : (f.ring || 0);
    beginLeave(before);
    // New satellites grow out of the nucleus. Ones that were already on
    // stage (the node you dove into, now the core) keep their current
    // radius so they can slide in instead of jumping.
    visibleNodes().forEach((n) => {
      if (n === f) {
        n.targetRad = 0;
        return;
      }
      if (isHarnessOverview() && n.homeX != null) {
        n.targetRad = Math.hypot(n.homeX, n.homeY);
        if (before.indexOf(n) === -1) {
          n.rad = 0;
          n.cx = 0;
          n.cy = 0;
        }
        return;
      }
      if (isPlaced(n)) {
        n.targetRad = Math.hypot(n.homeX, n.homeY);
        if (before.indexOf(n) === -1) {
          n.rad = 0;
          n.cx = 0;
          n.cy = 0;
        }
        return;
      }
      n.targetRad = ring;
      if (before.indexOf(n) === -1) {
        n.rad = 0;
        n.cx = 0;
        n.cy = 0;
      }
    });
    if (isHarnessOverview()) applyHarnessHomes();
    spin = 0;
    frame(animate !== false);
    paint();
    bloom(visibleNodes().filter((n) => before.indexOf(n) === -1), 0);
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
    const f = nodesById[openId] || root;
    const shown = (hoverId && nodesById[hoverId]) || f;
    if (!els.captionName) return;
    if (ptr && ptr.mode === "spawn-tool") {
      els.captionName.textContent = "Drop to plant a World.";
      return;
    }
    if (tool === "add-world") {
      els.captionName.textContent = "Click the graph to place a World.";
      return;
    }
    if (selectId && nodesById[selectId]) {
      const n = nodesById[selectId];
      els.captionName.textContent = n.fullName || n.label;
      return;
    }
    if (onExperienceRing()) {
      const here = Object.keys(nodesById).map((k) => nodesById[k])
        .find((n) => n && n.isHere);
      const name = here && (here.fullName || here.label);
      els.captionName.textContent = name
        ? ("You are in " + name + ".")
        : "Drag a satellite to draw a transition.";
      return;
    }
    if (isHarnessOverview()) {
      els.captionName.textContent = shown && shown !== root
        ? (shown.sub || shown.label)
        : "1 Choices → 2 Actions → 3 Picture → 4 State";
      return;
    }
    if (isSound() && root && openId === root.id) {
      els.captionName.textContent = shown && shown !== root
        ? (shown.sub || shown.label)
        : "The machine's voice.";
      return;
    }
    els.captionName.textContent = shown ? (shown.fullName || shown.label) : "";
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

  function portOrbit(n) {
    return (n && n.r ? n.r : DOT_R) + PORT_GAP;
  }

  function portWorldPos(n) {
    if (!n) return { x: 0, y: 0, r: PORT_R };
    if (n.portFollow) {
      return { x: n.cx + n.portFollow.x, y: n.cy + n.portFollow.y, r: PORT_R };
    }
    const a = n.portAngle || 0;
    const d = portOrbit(n);
    return { x: n.cx + Math.cos(a) * d, y: n.cy + Math.sin(a) * d, r: PORT_R };
  }

  function shortestDelta(from, to) {
    let d = to - from;
    while (d > Math.PI) d -= Math.PI * 2;
    while (d < -Math.PI) d += Math.PI * 2;
    return d;
  }

  function portIsHot(n) {
    if (!isLinkable(n)) return false;
    if (ptr && ptr.mode === "connect" && ptr.fromId === graphId(n)) return true;
    if (n.id === hoverId) return true;
    if (pointerWorld) {
      const d = Math.hypot(pointerWorld.x - n.cx, pointerWorld.y - n.cy);
      if (d <= n.r + PORT_GAP + 18 * worldPerPx()) return true;
    }
    return false;
  }

  function stepPorts() {
    if (!onExperienceRing()) return;
    const ease = reduceMotion() ? 1 : 0.2;
    visibleNodes().forEach((n) => {
      if (!isLinkable(n)) return;
      const pulling = ptr && ptr.mode === "connect" && ptr.fromId === graphId(n);
      if (pulling && pointerWorld) {
        n.portFollow = { x: pointerWorld.x - n.cx, y: pointerWorld.y - n.cy };
        n.portAngle = Math.atan2(n.portFollow.y, n.portFollow.x);
        return;
      }
      n.portFollow = null;
      const want = pointerWorld
        ? Math.atan2(pointerWorld.y - n.cy, pointerWorld.x - n.cx)
        : 0;
      if (n.portAngle == null) n.portAngle = want;
      n.portAngle += shortestDelta(n.portAngle, want) * ease;
    });
  }

  function paintPort(n) {
    if (!n || !n.g) return;
    const show = onExperienceRing() && isLinkable(n) && portIsHot(n);
    let port = n.g.querySelector(".eg-port");
    if (!show) {
      if (port) port.remove();
      return;
    }
    if (!port) {
      port = mk("g", null, "eg-port");
      port.appendChild(mk("line", { x1: 0, y1: 0, x2: 0, y2: 0 }, "eg-port-tether"));
      port.appendChild(mk("circle", { cx: 0, cy: 0, r: PORT_R }, "eg-port-dot"));
      n.g.appendChild(port);
    }
    placeOnePort(n, port);
  }

  function killLocal(n) {
    const d = (n && n.r ? n.r : DOT_R) * 0.72;
    return { x: d, y: -d };
  }

  function killWorldPos(n) {
    const L = killLocal(n);
    return { x: n.cx + L.x, y: n.cy + L.y, r: KILL_R };
  }

  function paintKill(n) {
    if (!n || !n.g) return;
    const worlds = ((experienceState().worlds) || []).length;
    const show = onExperienceRing() && (
      (n.kind === "world-node" && worlds > 1) || n.kind === "cutscene-node"
    );
    let k = n.g.querySelector(".eg-kill");
    if (!show) {
      if (k) k.remove();
      return;
    }
    if (!k) {
      k = mk("g", null, "eg-kill");
      k.appendChild(mk("circle", { cx: 0, cy: 0, r: KILL_R }, "eg-kill-disk"));
      const arm = KILL_R * 0.42;
      k.appendChild(mk("path", {
        d: "M " + (-arm) + " " + (-arm) + " L " + arm + " " + arm +
          " M " + arm + " " + (-arm) + " L " + (-arm) + " " + arm,
        fill: "none",
      }, "eg-kill-x"));
      n.g.appendChild(k);
    }
    const L = killLocal(n);
    k.setAttribute("transform", "translate(" + L.x.toFixed(1) + " " + L.y.toFixed(1) + ")");
  }

  function placeOnePort(n, port) {
    port = port || (n.g && n.g.querySelector(".eg-port"));
    if (!n || !port) return;
    const pos = portWorldPos(n);
    const lx = pos.x - n.cx, ly = pos.y - n.cy;
    port.setAttribute("transform",
      "translate(" + lx.toFixed(2) + " " + ly.toFixed(2) + ")");
    const pulling = ptr && ptr.mode === "connect" && ptr.fromId === graphId(n);
    port.classList.toggle("is-active", !!pulling);
    port.classList.toggle("is-near", n.id === hoverId || n.id === selectId);
    const tether = port.querySelector(".eg-port-tether");
    if (tether) {
      const dist = Math.hypot(lx, ly) || 1;
      const back = Math.max(0, dist - n.r);
      tether.setAttribute("x1", ((-lx / dist) * back).toFixed(2));
      tether.setAttribute("y1", ((-ly / dist) * back).toFixed(2));
      tether.setAttribute("x2", "0");
      tether.setAttribute("y2", "0");
    }
  }

  function placePorts() {
    if (!onExperienceRing()) return;
    visibleNodes().forEach((n) => {
      if (isLinkable(n)) placeOnePort(n);
    });
  }

  function fxHost() {
    if (els.fx && els.fx.isConnected) return els.fx;
    els.fx = document.getElementById("eg-fx");
    if (!els.fx && els.canvas) {
      els.fx = mk("g", { id: "eg-fx" }, "eg-fx");
      els.canvas.appendChild(els.fx);
    }
    return els.fx;
  }

  function hullHost() {
    if (els.hulls && els.hulls.isConnected) return els.hulls;
    els.hulls = document.getElementById("eg-hulls");
    if (!els.hulls && els.canvas) {
      els.hulls = mk("g", { id: "eg-hulls" }, "eg-hulls");
      const before = els.edges || els.canvas.firstChild;
      els.canvas.insertBefore(els.hulls, before);
    }
    return els.hulls;
  }

  function clusterBlob(nodes) {
    const live = (nodes || []).filter((n) => n && Number.isFinite(n.cx) && Number.isFinite(n.r));
    if (!live.length) return null;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    live.forEach((n) => {
      minX = Math.min(minX, n.cx - n.r);
      minY = Math.min(minY, n.cy - n.r);
      maxX = Math.max(maxX, n.cx + n.r);
      maxY = Math.max(maxY, n.cy + n.r);
    });
    const pad = DOT_R * 0.55;
    return {
      cx: (minX + maxX) / 2,
      cy: (minY + maxY) / 2,
      rx: (maxX - minX) / 2 + pad,
      ry: (maxY - minY) / 2 + pad,
    };
  }

  function placeHulls() {
    const host = hullHost();
    if (!host) return;
    host.innerHTML = "";
    // Overview is the turn loop only. Hulls grouped children that now
    // appear only after you dive.
  }

  function setRubber(from, to) {
    const host = fxHost();
    if (!host) return;
    let p = host.querySelector(".eg-rubber");
    if (!from || !to) {
      if (p) p.remove();
      return;
    }
    if (!p) {
      p = mk("path", null, "eg-rubber");
      host.appendChild(p);
    }
    const a = { cx: from.x, cy: from.y, r: from.r || 0 };
    const b = { cx: to.x, cy: to.y, r: to.r || 0 };
    p.setAttribute("d", edgePath(a, b));
  }

  function setGhost(pos) {
    const host = fxHost();
    if (!host) return;
    let g = host.querySelector(".eg-ghost");
    if (!pos || !Number.isFinite(pos.x)) {
      if (g) g.remove();
      return;
    }
    if (!g) {
      g = mk("g", null, "eg-ghost");
      g.appendChild(mk("circle", { cx: 0, cy: 0, r: DOT_R }, "eg-ghost-cell"));
      host.appendChild(g);
    }
    g.setAttribute("transform",
      "translate(" + pos.x.toFixed(2) + " " + pos.y.toFixed(2) + ")");
  }

  function selectNode(id) {
    selectId = id || null;
    paint();
    renderHud();
  }

  function onExperienceRing() {
    return isExperience() && !!root && openId === root.id;
  }

  function shellRadius() {
    const shell = els.world && els.world.querySelector(".eg-shell");
    const r = shell && parseFloat(shell.getAttribute("r"));
    return Number.isFinite(r) && r > 0 ? r : SHELL_R;
  }

  function outsideShellAt(clientX, clientY) {
    const p = toWorld(clientX, clientY);
    if (!p) return false;
    return Math.hypot(p.x, p.y) > shellRadius();
  }

  // Empty paper is the way up, one level at a time. On the Experience
  // canvas the enclosure is the room: a miss inside it deselects, a miss
  // outside it closes the Worlds back into the Experience coin (the
  // origin nucleus stays behind). Harness keeps its overview — that
  // ring is the machine, not a World.
  function paperSurfaces(evt) {
    if (openId === null || isHarnessOverview()) return false;
    if (!onExperienceRing()) return true;
    if (!evt) return true;
    return outsideShellAt(evt.clientX, evt.clientY);
  }

  // Hit tested against LIVE positions, not the slots — the dots are moving, so
  // anything else would mean aiming at where a dot used to be.
  function worldPerPx() {
    if (!els.canvas) return 1;
    const box = els.canvas.getBoundingClientRect();
    const vb = els.canvas.viewBox.baseVal;
    if (!box.width || !vb || !vb.width) return 1;
    return vb.width / box.width;
  }

  function worldDropAt(p, skipId) {
    if (!p) return null;
    const px = worldPerPx();
    const floor = 28 * px;
    let found = null, best = Infinity;
    visibleNodes().forEach((n) => {
      if (!isLinkable(n)) return;
      if (skipId && graphId(n) === skipId) return;
      const d = Math.hypot(p.x - n.cx, p.y - n.cy);
      const max = Math.max(n.r * 2.8, floor);
      if (d <= max && d < best) { best = d; found = n; }
    });
    return found;
  }

  function hitAt(clientX, clientY) {
    const p = toWorld(clientX, clientY);
    if (!p) return null;
    const pulling = ptr && ptr.mode === "connect";
    const skipId = pulling ? ptr.fromId : null;
    const px = worldPerPx();

    if (pulling) {
      const dst = worldDropAt(p, skipId);
      if (dst) return { node: dst, where: "orbit" };
      const edge = hitEdgeAt(p, px);
      if (edge) return { node: null, where: "edge", edge: edge };
      return { node: null, where: "empty" };
    }

    const killSlop = Math.max(KILL_R * 1.7, 13 * px);
    let killHit = null, killD = killSlop;
    if (onExperienceRing()) {
      visibleNodes().forEach((n) => {
        if (!isLinkable(n) || !n.g || !n.g.querySelector(".eg-kill")) return;
        const k = killWorldPos(n);
        const d = Math.hypot(p.x - k.x, p.y - k.y);
        if (d <= killD) { killD = d; killHit = n; }
      });
    }
    if (killHit) return { node: killHit, where: "kill" };

    const portSlop = Math.max(PORT_R * 3.2, 16 * px);
    let portHit = null, portD = portSlop;
    if (onExperienceRing()) {
      visibleNodes().forEach((n) => {
        if (!isLinkable(n) || !portIsHot(n)) return;
        const g = portWorldPos(n);
        const d = Math.hypot(p.x - g.x, p.y - g.y);
        if (d <= portD) { portD = d; portHit = n; }
      });
    }

    let found = null, best = Infinity;
    visibleNodes().forEach((n) => {
      const d = Math.hypot(p.x - n.cx, p.y - n.cy);
      const hitR = (n.kind === "world-node" || n.kind === "cutscene-node"
        || n.kind === "lore-node")
        ? n.r + LABEL * 0.75 : n.r;
      if (d <= hitR && d < best) { best = d; found = n; }
    });

    // The satellite sits on the rim. A centre click still moves the World;
    // a click on the handle, or the outer band toward it, pulls a noodle.
    if (portHit) {
      const inward = Math.hypot(p.x - portHit.cx, p.y - portHit.cy);
      if (inward > portHit.r * 0.62) return { node: portHit, where: "gizmo" };
    }
    if (found && isLinkable(found) && portIsHot(found)) {
      const d = Math.hypot(p.x - found.cx, p.y - found.cy);
      if (d > found.r * 0.72) return { node: found, where: "gizmo" };
    }

    if (found) {
      const isCore = found.id === (openId || (root && root.id));
      if (isCore && onExperienceRing()) {
        if (Math.hypot(p.x - found.cx, p.y - found.cy) > found.r * 0.28) {
          found = null;
        }
      }
      if (found) {
        return { node: found, where: isCore ? "core" : "orbit" };
      }
    }

    const edge = hitEdgeAt(p, px);
    if (edge) return { node: null, where: "edge", edge: edge };
    return { node: null, where: "empty" };
  }

  function activate(n) {
    if (n.kind === "experience-node") {
      if (openId === null) setOpen(root.id, true);
      else selectNode(n.id);
      return;
    }
    if (n.kind === "lore-node") {
      openLoreInspector(n, { focus: true });
      return;
    }
    if (n.kind === "world-node") {
      diveWorld(n);
      return;
    }
    if (n.kind === "cutscene-node") {
      openCutsceneInspector(n, { focus: true });
      return;
    }
    if ((n.children || []).length) { setOpen(n.id, true); return; }
    openSheet(n);
  }

  function diveWorld(n) {
    if (!n || n.kind !== "world-node" || !n.worldId) return;
    closeInspector();
    closeSheet();
    if (selectId) selectNode(null);
    const go = () => {
      sync();
      const id = "world:" + n.worldId;
      if (nodesById[id]) setOpen(id, true);
    };
    const cur = B && B.editingWorldId && B.editingWorldId();
    if (cur === n.worldId) { go(); return; }
    Promise.resolve(B.enterWorld && B.enterWorld(n.worldId))
      .then(go)
      .catch(() => { if (B.toast) B.toast("Couldn't open that World.", "warn"); });
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

    if (tool === "add-world") {
      acted();
      const p = toWorld(evt.clientX, evt.clientY);
      placeNewWorld(p);
      return;
    }

    if (h.where === "kill") {
      acted();
      if (h.node.kind === "cutscene-node") removeCutscene(h.node.cutsceneId);
      else removeWorld(h.node.worldId);
      return;
    }

    if (h.where === "gizmo") {
      acted();
      return;
    }

    if (h.where === "edge") {
      acted();
      selectNode(null);
      openLinkInspector(h.edge);
      return;
    }

    // Empty paper is the way back out, one level at a time. With dots this
    // small there is far more of it than there is of them, which makes leaving
    // easier than arriving — the right way round.
    if (h.where === "empty") {
      acted();
      closeInspector();
      if (isHarnessOverview()) {
        if (selectId) selectNode(null);
        return;
      }
      if (onExperienceRing()) {
        if (outsideShellAt(evt.clientX, evt.clientY)) {
          surface();
          return;
        }
        if (selectId) selectNode(null);
        return;
      }
      surface();
      return;
    }
    // The nucleus opens the ring, and once open it is the way back up.
    if (h.where === "core") {
      acted();
      selectNode(null);
      closeInspector();
      if (openId === null) setOpen(root.id, true);
      else surface();
      return;
    }
    if (h.node && h.node.kind === "experience-node" && onExperienceRing()) {
      acted();
      selectNode(h.node.id);
      return;
    }
    if (h.node && h.node.kind === "lore-node" && onExperienceRing()) {
      acted();
      const already = selectId === h.node.id;
      selectNode(h.node.id);
      const cardOpen = !!(els.inspector && els.inspector.classList.contains("is-open") &&
        els.inspector.dataset.kind === "lore");
      if (already && cardOpen) {
        if (document.activeElement && els.inspector.contains(document.activeElement)) {
          document.activeElement.blur();
        }
        return;
      }
      openLoreInspector(h.node, { focus: true });
      return;
    }
    if (h.node && h.node.kind === "world-node" && onExperienceRing()) {
      acted();
      const already = selectId === h.node.id;
      selectNode(h.node.id);
      const cardOpen = !!(els.inspector && els.inspector.classList.contains("is-open") &&
        els.inspector.dataset.kind === "world" &&
        els.inspector.dataset.worldId === h.node.worldId);
      if (already && cardOpen) {
        if (document.activeElement && els.inspector.contains(document.activeElement)) {
          document.activeElement.blur();
        }
        return;
      }
      openWorldInspector(h.node, { focus: true });
      return;
    }
    if (h.node && h.node.kind === "cutscene-node" && onExperienceRing()) {
      acted();
      const already = selectId === h.node.id;
      selectNode(h.node.id);
      const cardOpen = !!(els.inspector && els.inspector.classList.contains("is-open") &&
        els.inspector.dataset.kind === "cutscene" &&
        els.inspector.dataset.cutsceneId === h.node.cutsceneId);
      if (already && cardOpen) {
        if (document.activeElement && els.inspector.contains(document.activeElement)) {
          document.activeElement.blur();
        }
        return;
      }
      openCutsceneInspector(h.node, { focus: true });
      return;
    }
    acted();
    selectNode(h.node.id);
    activate(h.node);
  }

  function onDblClick(evt) {
    evt.preventDefault();
    const h = hitAt(evt.clientX, evt.clientY);
    if (!h || !h.node) return;
    if (h.node.kind === "experience-node") {
      acted();
      if (openId === null) setOpen(root.id, true);
      else selectNode(h.node.id);
      return;
    }
    if (h.node.kind === "lore-node") {
      acted();
      selectNode(h.node.id);
      openLoreInspector(h.node, { focus: true });
      return;
    }
    if (h.node.kind === "world-node") {
      acted();
      diveWorld(h.node);
    }
  }

  function setHover(id, cursor) {
    els.canvas.style.cursor = cursor || "default";
    const changed = id !== hoverId;
    hoverId = id;
    els.world.querySelectorAll(".eg-node.is-hover")
      .forEach((g) => g.classList.remove("is-hover"));
    if (id) {
      const g = els.world.querySelector('.eg-node[data-id="' + id + '"]');
      if (g) g.classList.add("is-hover");
    }
    if (changed && onExperienceRing()) {
      visibleNodes().forEach((n) => { if (isLinkable(n)) paintPort(n); });
    }
  }

  function onHover(evt) {
    const at = toWorld(evt.clientX, evt.clientY);
    if (at) pointerWorld = at;
    const h = hitAt(evt.clientX, evt.clientY);
    hoverEdgeId = (h && h.where === "edge" && h.edge) ? h.edge.id : null;
    paintEdges();
    if (!h || h.where === "empty") {
      if (tool === "add-world" || (ptr && ptr.mode === "spawn-tool")) {
        setHover(null, "crosshair");
      } else {
        setHover(null, openId === null ? "default"
          : (isHarnessOverview() ? "grab"
            : (onExperienceRing() && !outsideShellAt(evt.clientX, evt.clientY)
              ? "grab" : "zoom-out")));
      }
    } else if (h.where === "gizmo") {
      setHover(h.node.id, "grab");
    } else if (h.where === "kill") {
      setHover(h.node.id, "pointer");
    } else if (h.where === "edge") {
      setHover(null, "pointer");
    } else if (h.where === "core") {
      setHover(h.node.id, openId === null ? "zoom-in" : "default");
    } else {
      setHover(h.node.id, tool === "add-world" ? "crosshair" : "pointer");
    }
    if (onExperienceRing()) {
      visibleNodes().forEach((n) => { if (isLinkable(n)) paintPort(n); });
    }
    if (els.graph) {
      const onPaper = !h || h.where === "empty";
      const placing = tool === "add-world" || (ptr && ptr.mode === "spawn-tool");
      els.graph.classList.toggle("is-paper-hot",
        !!(paperSurfaces(evt) && onPaper && !placing));
    }
  }

  // ══════════════════════════════════════════════════════════════════
  // EXPERIENCE LAYER — Worlds as bubbles, transitions as edges
  // ══════════════════════════════════════════════════════════════════
  function worldNodeOf(worldId) {
    return nodesById["world:" + worldId] || graphNodeOf(worldId);
  }

  function showingWorldRing() {
    return onExperienceRing();
  }

  function worldIdFromHit(h) {
    if (!h || !h.node) return null;
    if (isLinkable(h.node)) return graphId(h.node);
    return null;
  }

  function rebuildEdges() {
    if (!els.edges) return;
    els.edges.innerHTML = "";
    if (!els.edges.querySelector("defs")) {
      const defs = mk("defs");
      const marker = mk("marker", {
        id: "eg-arrow", viewBox: "0 0 10 10",
        refX: "9", refY: "5", markerWidth: "7", markerHeight: "7",
        orient: "auto-start-reverse",
      });
      marker.appendChild(mk("path", { d: "M 0 0 L 10 5 L 0 10 z" }, "eg-arrow-head"));
      defs.appendChild(marker);
      els.edges.appendChild(defs);
    }
    placeEdges();
  }

  // Worlds sit on the enclosure ring. A right-out / left-in noodle (the
  // ComfyUI socket rule) forced every return to leave the far side of the
  // disk, U-turn, and cross the rest of the cycle. Tangents now follow the
  // ring: leave along the circular travel direction, enter the same way,
  // handle length is the cubic that approximates that arc.
  // Harness edges still face the node they are going to.
  function rimPin(n, ang) {
    const r = n.r || DOT_R;
    return {
      x: n.cx + Math.cos(ang) * r,
      y: n.cy + Math.sin(ang) * r,
      ang: ang,
    };
  }

  function chordClearance(a, b, others) {
    const dx = b.cx - a.cx, dy = b.cy - a.cy;
    const len = Math.hypot(dx, dy) || 1;
    let nx = -dy / len, ny = dx / len;
    if (ny > 0) { nx = -nx; ny = -ny; }
    let need = 0;
    (others || []).forEach((n) => {
      if (!n || n === a || n === b) return;
      if (n.id !== "h:dot:choices" && n.id !== "h:dot:actions"
        && n.id !== "h:dot:picture" && n.id !== "h:dot:state") return;
      const d = distToSegment({ x: n.cx, y: n.cy }, a, b);
      const pad = (n.r || DOT_R) + DOT_R * 1.25;
      if (d < pad) need = Math.max(need, pad - d + DOT_R * 0.55);
    });
    if (need < 1) return null;
    return {
      nx: nx, ny: ny, d: need,
      x: (a.cx + b.cx) / 2 + nx * need,
      y: (a.cy + b.cy) / 2 + ny * need,
    };
  }

  function packPinAngles(list, gap) {
    if (!list || list.length < 2) return;
    list.sort((p, q) => p.want - q.want);
    for (let i = 1; i < list.length; i++) {
      if (list[i].want - list[i - 1].want < gap) {
        list[i].want = list[i - 1].want + gap;
      }
    }
    const wrap = (list[0].want + Math.PI * 2) - list[list.length - 1].want;
    if (wrap < gap) list[0].want = list[list.length - 1].want + gap - Math.PI * 2;
  }

  function assignHarnessPins(onStage) {
    const others = [];
    Object.keys(onStage).forEach((id) => { others.push(onStage[id]); });
    const items = [];
    HARNESS_FLOW.forEach((e, i) => {
      const a = onStage["h:dot:" + e.from];
      const b = onStage["h:dot:" + e.to];
      if (!a || !b) return;
      items.push({ i: i, e: e, a: a, b: b });
    });
    const outs = Object.create(null);
    const inns = Object.create(null);
    items.forEach((it) => {
      const lane = it.e.kind === "cluster" ? null : chordClearance(it.a, it.b, others);
      it.lane = lane;
      const ax = lane ? lane.x : it.b.cx;
      const ay = lane ? lane.y : it.b.cy;
      const bx = lane ? lane.x : it.a.cx;
      const by = lane ? lane.y : it.a.cy;
      (outs[it.a.id] || (outs[it.a.id] = [])).push({
        it: it, want: Math.atan2(ay - it.a.cy, ax - it.a.cx),
      });
      (inns[it.b.id] || (inns[it.b.id] = [])).push({
        it: it, want: Math.atan2(by - it.b.cy, bx - it.b.cx),
      });
    });
    Object.keys(outs).forEach((id) => packPinAngles(outs[id], 0.48));
    Object.keys(inns).forEach((id) => packPinAngles(inns[id], 0.48));
    const pins = [];
    items.forEach((it) => {
      const o = (outs[it.a.id] || []).find((p) => p.it === it);
      const n = (inns[it.b.id] || []).find((p) => p.it === it);
      pins[it.i] = {
        from: rimPin(it.a, o ? o.want : Math.atan2(it.b.cy - it.a.cy, it.b.cx - it.a.cx)),
        to: rimPin(it.b, n ? n.want : Math.atan2(it.a.cy - it.b.cy, it.a.cx - it.b.cx)),
        lane: it.lane,
      };
    });
    return pins;
  }

  function wrapPi(d) {
    while (d <= -Math.PI) d += Math.PI * 2;
    while (d > Math.PI) d -= Math.PI * 2;
    return d;
  }

  function facingHandles(a, b, opts) {
    const from = opts.fromPin || rimPin(a, Math.atan2(b.cy - a.cy, b.cx - a.cx));
    const to = opts.toPin || rimPin(b, Math.atan2(a.cy - b.cy, a.cx - b.cx));
    if (opts.lane) {
      return {
        x1: from.x, y1: from.y, x2: to.x, y2: to.y,
        c1x: opts.lane.x, c1y: opts.lane.y,
        c2x: opts.lane.x, c2y: opts.lane.y,
      };
    }
    const dist = Math.hypot(to.x - from.x, to.y - from.y) || 1;
    const k = opts.bulge != null ? opts.bulge : 0.24;
    const hlen = Math.max(dist * k, DOT_R * 0.4);
    return {
      x1: from.x, y1: from.y, x2: to.x, y2: to.y,
      c1x: from.x + Math.cos(from.ang) * hlen,
      c1y: from.y + Math.sin(from.ang) * hlen,
      c2x: to.x + Math.cos(to.ang) * hlen,
      c2y: to.y + Math.sin(to.ang) * hlen,
    };
  }

  function ringCenter() {
    let sx = 0, sy = 0, n = 0;
    Object.keys(nodesById).forEach((id) => {
      const w = nodesById[id];
      if (!w || w.kind !== "world-node") return;
      if (!Number.isFinite(w.cx) || !Number.isFinite(w.cy)) return;
      sx += w.cx;
      sy += w.cy;
      n += 1;
    });
    return n > 1 ? { x: sx / n, y: sy / n } : { x: 0, y: 0 };
  }

  // Cubic that rides the worlds' circle: tangent = perpendicular to the
  // radius from the cluster centre, same rotational sense at both ends,
  // length = the 4/3 tan(θ/4) handle for a circular arc of sweep θ.
  // Minor arc, so a 3-world cycle is a rounded triangle, not a pretzel.
  function ringHandles(a, b) {
    const o = ringCenter();
    const ax = (a.cx || 0) - o.x, ay = (a.cy || 0) - o.y;
    const bx = (b.cx || 0) - o.x, by = (b.cy || 0) - o.y;
    const radA = Math.hypot(ax, ay);
    const radB = Math.hypot(bx, by);
    const minRad = Math.max(a.r || 0, b.r || 0, DOT_R) * 0.45;
    if (radA < minRad || radB < minRad) {
      return facingHandles(a, b, { bulge: 0.28 });
    }
    const angA = Math.atan2(ay, ax);
    const angB = Math.atan2(by, bx);
    const sweep = wrapPi(angB - angA);
    let dir = sweep >= 0 ? 1 : -1;
    const theta = Math.max(Math.abs(sweep), 0.12);
    if (theta > Math.PI * 0.85) {
      const r = (radA + radB) / 2;
      const p = Math.hypot(o.x + Math.cos(angA + dir * theta / 2) * r,
                           o.y + Math.sin(angA + dir * theta / 2) * r);
      const q = Math.hypot(o.x + Math.cos(angA - dir * theta / 2) * r,
                           o.y + Math.sin(angA - dir * theta / 2) * r);
      if (q > p) dir = -dir;
    }
    const tAx = -dir * Math.sin(angA), tAy = dir * Math.cos(angA);
    const tBx = -dir * Math.sin(angB), tBy = dir * Math.cos(angB);
    const from = rimPin(a, Math.atan2(tAy, tAx));
    const to = rimPin(b, Math.atan2(-tBy, -tBx));
    const chord = Math.hypot(to.x - from.x, to.y - from.y) || 1;
    const sinHalf = Math.sin(theta / 2) || 1e-6;
    let hlen = chord * (4 / 3) * Math.tan(theta / 4) / (2 * sinHalf);
    hlen = Math.max(DOT_R * 0.35, Math.min(hlen, chord * 0.85));
    return {
      x1: from.x, y1: from.y, x2: to.x, y2: to.y,
      c1x: from.x + tAx * hlen,
      c1y: from.y + tAy * hlen,
      c2x: to.x - tBx * hlen,
      c2y: to.y - tBy * hlen,
    };
  }

  function edgeHandles(a, b, opts) {
    opts = opts || {};
    if (opts.mode === "facing") return facingHandles(a, b, opts);
    return ringHandles(a, b);
  }

  function edgePath(a, b, opts) {
    const h = edgeHandles(a, b, opts);
    return "M " + h.x1.toFixed(1) + " " + h.y1.toFixed(1) +
      " C " + h.c1x.toFixed(1) + " " + h.c1y.toFixed(1) +
      " " + h.c2x.toFixed(1) + " " + h.c2y.toFixed(1) +
      " " + h.x2.toFixed(1) + " " + h.y2.toFixed(1);
  }

  function curveMid(a, b, opts) {
    const h = edgeHandles(a, b, opts);
    return {
      x: 0.125 * h.x1 + 0.375 * h.c1x + 0.375 * h.c2x + 0.125 * h.x2,
      y: 0.125 * h.y1 + 0.375 * h.c1y + 0.375 * h.c2y + 0.125 * h.y2,
    };
  }

  function condLabel(t) {
    const c = (t && t.condition) || {};
    const spec = transitionType(c.type);
    if (typeHasField(spec, "turns")) return String(c.turns || 8);
    return spec.short || spec.label || spec.id;
  }

  function placeEdges() {
    if (!els.edges) return;
    const keep = [];
    els.edges.querySelectorAll("defs, .eg-rubber, .eg-ghost").forEach((el) => keep.push(el));
    els.edges.innerHTML = "";
    keep.forEach((el) => els.edges.appendChild(el));
    if (showingWorldRing()) {
      placeWorldEdges();
      return;
    }
    if (isHarness()) placeHarnessFlow();
  }

  function placeWorldEdges() {
    const exp = experienceState();
    const startId = exp.start_world;
    (exp.transitions || []).forEach((t) => {
      const a = worldNodeOf(t.from), b = worldNodeOf(t.to);
      if (!a || !b) return;
      const g = mk("g", { "data-id": t.id }, "eg-edge-g");
      const hit = mk("path", { d: edgePath(a, b) }, "eg-edge-hit");
      let cls = "eg-edge";
      if (graphId(a) === startId) cls += " is-from-start";
      if (graphId(b) === startId) cls += " is-to-start";
      if (t.id === hoverEdgeId || t.id === openEdgeId) cls += " is-hover";
      const vis = mk("path", { d: edgePath(a, b) }, cls);
      g.appendChild(hit);
      g.appendChild(vis);
      const mid = curveMid(a, b);
      const bead = mk("g", {
        transform: "translate(" + mid.x.toFixed(1) + " " + mid.y.toFixed(1) + ")",
      }, "eg-bead" + (t.id === openEdgeId || t.id === hoverEdgeId ? " is-hot" : ""));
      bead.appendChild(mk("circle", { cx: 0, cy: 0, r: DOT_R * 0.22 }, "eg-bead-disk"));
      const lab = mk("text", { x: 0, y: 0, "font-size": LABEL * 0.42 }, "eg-bead-lab");
      lab.textContent = condLabel(t);
      bead.appendChild(lab);
      g.appendChild(bead);
      g._transition = t;
      els.edges.appendChild(g);
    });
  }

  function drawHarnessPath(a, b, route, opts) {
    opts = opts || {};
    const g = mk("g", { "data-id": opts.id || ("flow:" + a.id + ":" + b.id) }, "eg-edge-g");
    const vis = mk("path", {
      d: edgePath(a, b, {
        mode: "facing",
        fromPin: opts.fromPin,
        toPin: opts.toPin,
        lane: opts.lane,
        bulge: opts.bulge != null ? opts.bulge : 0.2,
      }),
    }, "eg-edge eg-flow eg-flow-" + route);
    vis.setAttribute("marker-end", "url(#eg-arrow)");
    g.appendChild(vis);
    if (opts.pin && opts.fromPin) {
      g.appendChild(mk("circle", {
        cx: opts.fromPin.x.toFixed(1),
        cy: opts.fromPin.y.toFixed(1),
        r: PORT_R,
      }, "eg-flow-pin"));
    }
    els.edges.appendChild(g);
  }

  function outwardLane(a, b) {
    const mx = (a.cx + b.cx) / 2;
    const my = (a.cy + b.cy) / 2;
    const r = Math.hypot(mx, my) || 1;
    const lift = Math.hypot(a.cx - b.cx, a.cy - b.cy) * 0.32 + DOT_R * 0.22;
    return { x: mx + (mx / r) * lift, y: my + (my / r) * lift };
  }

  // Hub ↔ satellite along the radius. A twist splits an out-and-back on
  // the same child so the two arrows don't sit on one line.
  function drawHubHop(hub, sat, outbound, route, id, twist) {
    const ang = Math.atan2(sat.cy - hub.cy, sat.cx - hub.cx) + (twist || 0);
    if (outbound) {
      drawHarnessPath(hub, sat, route, {
        id: id,
        fromPin: rimPin(hub, ang),
        toPin: rimPin(sat, ang + Math.PI),
        bulge: 0.06,
      });
      return;
    }
    drawHarnessPath(sat, hub, route, {
      id: id,
      fromPin: rimPin(sat, ang + Math.PI),
      toPin: rimPin(hub, ang),
      bulge: 0.06,
    });
  }

  function placeHarnessRing(parent) {
    const kids = (parent && parent.children) || [];
    if (!kids.length) return;
    // Author order is execution order, and also the clock (12 o'clock
    // first, then clockwise). The nucleus starts the loop and the last
    // child returns to it — that's how you leave this level.
    const split = kids.length === 1 ? 0.28 : 0;
    drawHubHop(parent, kids[0], true, "spine", "ring:start", -split);
    for (let i = 0; i < kids.length - 1; i++) {
      const a = kids[i];
      const b = kids[i + 1];
      const lane = outwardLane(a, b);
      drawHarnessPath(a, b, "cluster", {
        id: "ring:" + i,
        fromPin: rimPin(a, Math.atan2(lane.y - a.cy, lane.x - a.cx)),
        toPin: rimPin(b, Math.atan2(lane.y - b.cy, lane.x - b.cx)),
        lane: lane,
        bulge: 0.16,
      });
    }
    drawHubHop(parent, kids[kids.length - 1], false, "return", "ring:back", split);
  }

  function placeHarnessFlow() {
    if (!isHarnessOverview()) {
      const f = nodesById[openId];
      if (f) placeHarnessRing(f);
      return;
    }
    const onStage = Object.create(null);
    visibleNodes().forEach((n) => { onStage[n.id] = n; });
    const pins = assignHarnessPins(onStage);
    HARNESS_FLOW.forEach((e, i) => {
      const a = onStage["h:dot:" + e.from];
      const b = onStage["h:dot:" + e.to];
      if (!a || !b) return;
      const pair = pins[i];
      drawHarnessPath(a, b, e.route || "spine", {
        id: "flow:" + i,
        fromPin: pair && pair.from,
        toPin: pair && pair.to,
        lane: pair && pair.lane,
        pin: true,
      });
    });
  }

  function paintEdges() {
    if (!els.edges) return;
    els.edges.querySelectorAll(".eg-edge").forEach((p) => {
      const id = p.parentNode && p.parentNode.getAttribute("data-id");
      p.classList.toggle("is-hover", id === hoverEdgeId || id === openEdgeId);
      p.classList.toggle("is-open", id === openEdgeId);
    });
  }

  function hitEdgeAt(p, px) {
    if (!showingWorldRing()) return null;
    px = px || worldPerPx();
    let found = null, best = 16 * px;
    const exp = experienceState();
    (exp.transitions || []).forEach((t) => {
      const a = worldNodeOf(t.from), b = worldNodeOf(t.to);
      if (!a || !b) return;
      const mid = curveMid(a, b);
      const bead = Math.hypot(p.x - mid.x, p.y - mid.y);
      if (bead < DOT_R * 0.32 && bead < best) { best = bead; found = t; return; }
      const d = distToSegment(p, a, b);
      if (d < best) { best = d; found = t; }
    });
    return found;
  }

  function distToSegment(p, a, b) {
    const x1 = a.cx, y1 = a.cy, x2 = b.cx, y2 = b.cy;
    const dx = x2 - x1, dy = y2 - y1;
    const len2 = dx * dx + dy * dy || 1;
    let t = ((p.x - x1) * dx + (p.y - y1) * dy) / len2;
    t = Math.max(0, Math.min(1, t));
    return Math.hypot(p.x - (x1 + t * dx), p.y - (y1 + t * dy));
  }

  function setTool(next) {
    tool = next || "select";
    connectFrom = null;
    setRubber(null);
    if (els.toolkit) {
      els.toolkit.querySelectorAll(".eg-tool").forEach((b) => {
        b.classList.toggle("is-on", b.getAttribute("data-tool") === tool);
      });
    }
    if (els.graph) {
      els.graph.classList.toggle("is-placing", tool === "add-world");
    }
    renderHud();
    paint();
  }

  function finishLink(fromId, toId) {
    connectFrom = null;
    setRubber(null);
    markWorldDrop(null);
    const fromNode = graphNodeOf(fromId);
    const cond = (fromNode && fromNode.kind === "cutscene-node")
      ? { type: "immediate" }
      : { type: "turn_count", turns: 8 };
    Promise.resolve(B.addTransition && B.addTransition(fromId, toId, cond))
      .then((exp) => {
        sync();
        setTool("select");
        const created = ((exp && exp.transitions) || []).slice().reverse()
          .find((t) => t.from === fromId && t.to === toId);
        if (created) openLinkInspector(created);
      })
      .catch(() => { if (B.toast) B.toast("Couldn't link those nodes.", "warn"); });
  }

  function removeTransition(id) {
    if (!id) return;
    closeInspector();
    Promise.resolve(B.removeTransition && B.removeTransition(id))
      .then(() => sync())
      .catch(() => { if (B.toast) B.toast("Couldn't remove that link.", "warn"); });
  }

  function removeCutscene(cutsceneId) {
    if (!cutsceneId) return;
    closeSheet();
    closeInspector();
    Promise.resolve(B.removeExperienceCutscene && B.removeExperienceCutscene(cutsceneId))
      .then(() => { selectId = null; setTool("select"); sync(); })
      .catch((err) => {
        if (B.toast) B.toast((err && err.message) || "Couldn't remove that Cutscene.", "warn");
      });
  }

  function removeWorld(worldId) {
    if (!worldId) return;
    closeSheet();
    closeInspector();
    Promise.resolve(B.removeExperienceWorld && B.removeExperienceWorld(worldId))
      .then(() => { selectId = null; setTool("select"); sync(); })
      .catch((err) => {
        if (B.toast) B.toast((err && err.message) || "Couldn't remove that World.", "warn");
      });
  }

  function typingInField() {
    const ae = document.activeElement;
    if (!ae) return false;
    const tag = ae.tagName;
    if (tag === "TEXTAREA" || tag === "INPUT" || tag === "SELECT") return true;
    return !!ae.isContentEditable;
  }

  function deleteSelection() {
    if (!B || !B.isGraphMode || !B.isGraphMode()) return false;
    if (!onExperienceRing()) return false;
    if (typingInField()) return false;
    if (openEdgeId) {
      removeTransition(openEdgeId);
      return true;
    }
    const n = selectId && nodesById[selectId];
    if (n && n.kind === "world-node" && n.worldId) {
      removeWorld(n.worldId);
      return true;
    }
    if (n && n.kind === "cutscene-node" && n.cutsceneId) {
      removeCutscene(n.cutsceneId);
      return true;
    }
    return false;
  }

  function onGraphKey(evt) {
    if (evt.key !== "Delete" && evt.key !== "Backspace") return;
    if (!document.body.classList.contains("world-editor-on")) return;
    if (typingInField()) return;
    if (!B || !B.isGraphMode || !B.isGraphMode()) return;
    evt.preventDefault();
    deleteSelection();
  }

  function addWorldFromToolkit() {
    placeNewWorld(null);
  }

  function addCutsceneFromToolkit() {
    placeNewCutscene(null);
  }

  function nextWorldSlot(pos) {
    if (pos && Number.isFinite(pos.x) && Number.isFinite(pos.y)) return pos;
    const exp = experienceState();
    const n = ((exp.worlds || []).length) + ((exp.cutscenes || []).length) + 1;
    const f = nodesById[openId] || root;
    const ring = (f && f.ring) || (DOT_R * 3.2);
    const top = -Math.PI / 2;
    const a = top + ((n - 1) / Math.max(n, 1)) * Math.PI * 2;
    return { x: Math.cos(a) * ring, y: Math.sin(a) * ring };
  }

  function defaultWorldHome(i, n) {
    const count = Math.max(n, 1);
    const r = DOT_R * (2.05 + Math.max(0, count - 3) * 0.18);
    const a = -Math.PI / 2 + (i / count) * Math.PI * 2;
    return { x: Math.cos(a) * r, y: Math.sin(a) * r };
  }

  function seedWorldHomes() {
    if (!isExperience()) return;
    const worlds = (experienceState().worlds || []);
    worlds.forEach((w, i) => {
      const n = worldNodeOf(w.id);
      if (!n) return;
      if (n.homeX != null && n.homeY != null) return;
      const pos = defaultWorldHome(i, worlds.length);
      n.homeX = pos.x;
      n.homeY = pos.y;
      n.cx = pos.x;
      n.cy = pos.y;
      w.x = pos.x;
      w.y = pos.y;
      if (B.moveExperienceWorld) {
        Promise.resolve(B.moveExperienceWorld(w.id, pos.x, pos.y)).catch(() => {});
      }
    });
  }

  function seedExperienceHome() {
    if (!isExperience()) return;
    const exp = experienceState();
    const n = nodesById["xp"];
    if (!n) return;
    if (typeof exp.x === "number" && typeof exp.y === "number") return;
    const pos = defaultExperienceHome();
    n.homeX = pos.x;
    n.homeY = pos.y;
    n.cx = pos.x;
    n.cy = pos.y;
    exp.x = pos.x;
    exp.y = pos.y;
    if (B.moveExperience) {
      Promise.resolve(B.moveExperience(pos.x, pos.y)).catch(() => {});
    }
  }

  function seedLoreHome() {
    if (!isExperience()) return;
    const lore = loreState();
    const n = nodesById["lore"];
    if (!n) return;
    if (typeof lore.x === "number" && typeof lore.y === "number") return;
    const xp = nodesById["xp"];
    const pos = defaultLoreHome(xp ? { x: xp.homeX, y: xp.homeY } : null);
    n.homeX = pos.x;
    n.homeY = pos.y;
    n.cx = pos.x;
    n.cy = pos.y;
    lore.x = pos.x;
    lore.y = pos.y;
    if (B.moveLore) {
      Promise.resolve(B.moveLore(pos.x, pos.y)).catch(() => {});
    }
  }

  function placeNewWorld(pos) {
    if (!isExperience()) return;
    if (openId === null && root) setOpen(root.id, false);
    const slot = nextWorldSlot(pos);
    Promise.resolve(B.addExperienceWorld && B.addExperienceWorld("", slot))
      .then((exp) => {
        const worlds = (exp && exp.worlds) || experienceState().worlds || [];
        const last = worlds[worlds.length - 1];
        sync();
        if (root) setOpen(root.id, true);
        setTool("select");
        if (last) {
          const id = "world:" + last.id;
          selectNode(id);
          const n = nodesById[id];
          if (n) {
            popNode(n);
            openWorldInspector(n, { focus: isGenericWorldName(n.fullName || n.label) });
          }
        }
      })
      .catch(() => { if (B.toast) B.toast("Couldn't add a World.", "warn"); });
  }

  function placeNewCutscene(pos) {
    if (!isExperience()) return;
    if (openId === null && root) setOpen(root.id, false);
    const slot = nextWorldSlot(pos);
    Promise.resolve(B.addExperienceCutscene && B.addExperienceCutscene("", slot))
      .then((exp) => {
        const cuts = (exp && exp.cutscenes) || experienceState().cutscenes || [];
        const last = cuts[cuts.length - 1];
        sync();
        if (root) setOpen(root.id, true);
        setTool("select");
        if (last) {
          const id = "cutscene:" + last.id;
          selectNode(id);
          const n = nodesById[id];
          if (n) {
            popNode(n);
            openCutsceneInspector(n, { focus: true });
          }
        }
      })
      .catch(() => { if (B.toast) B.toast("Couldn't add a Cutscene.", "warn"); });
  }

  function openTransitionSheet(t) {
    openLinkInspector(t);
  }

  function ensureInspector() {
    if (els.inspector) return els.inspector;
    if (!els.graph) return null;
    const d = document.createElement("div");
    d.id = "eg-inspector";
    d.className = "eg-inspector";
    d.setAttribute("aria-hidden", "true");
    els.graph.appendChild(d);
    els.inspector = d;
    return d;
  }

  function syncMenuStack() {
    // Menus live inside #world-editor's isolated stacking context. Play chrome
    // is raised above that pane, so a body class is what lets CSS lift the
    // whole editor over the live view while a menu is up.
    const menu = !!(
      (els.inspector && els.inspector.classList.contains("is-open")) ||
      (els.sheet && els.sheet.classList.contains("is-open"))
    );
    try { document.body.classList.toggle("we-menu-open", menu); } catch (_) {}
  }

  function closeInspector() {
    if (!els.inspector || !els.inspector.classList.contains("is-open")) return false;
    els.inspector.classList.remove("is-open", "is-world", "is-lore");
    els.inspector.setAttribute("aria-hidden", "true");
    els.inspector.innerHTML = "";
    delete els.inspector.dataset.kind;
    delete els.inspector.dataset.worldId;
    openEdgeId = null;
    openWorldId = null;
    openLore = false;
    paintEdges();
    syncMenuStack();
    return true;
  }

  function openLinkInspector(t) {
    if (!t) return;
    closeSheet();
    const host = ensureInspector();
    if (!host) return;
    const exp = experienceState();
    const src = (exp.worlds || []).find((w) => w.id === t.from);
    const dst = (exp.worlds || []).find((w) => w.id === t.to);
    const cond = Object.assign({ type: "turn_count", turns: 8 }, t.condition || {});
    openEdgeId = t.id;
    openWorldId = null;
    openLore = false;
    host.innerHTML = "";
    host.classList.add("is-open");
    host.classList.remove("is-world");
    host.dataset.kind = "link";
    delete host.dataset.worldId;
    host.setAttribute("aria-hidden", "false");

    const path = document.createElement("div");
    path.className = "eg-insp-path";
    path.innerHTML = "<b></b><span>→</span><b></b>";
    path.querySelectorAll("b")[0].textContent = (src && src.name) || "World";
    path.querySelectorAll("b")[1].textContent = (dst && dst.name) || "World";
    host.appendChild(path);

    const typeRow = document.createElement("label");
    typeRow.className = "eg-insp-type";
    const typeK = document.createElement("span");
    typeK.textContent = "Type";
    const typeSel = document.createElement("select");
    typeSel.className = "eg-select";
    typeSel.setAttribute("aria-label", "Transition type");
    transitionTypes().forEach((row) => {
      const opt = document.createElement("option");
      opt.value = row.id;
      opt.textContent = row.label;
      if (row.id === cond.type) opt.selected = true;
      typeSel.appendChild(opt);
    });
    if (cond.type && !transitionTypes().some((row) => row.id === cond.type)) {
      const opt = document.createElement("option");
      opt.value = cond.type;
      opt.textContent = cond.type;
      opt.selected = true;
      typeSel.appendChild(opt);
    }
    typeSel.addEventListener("change", () => {
      cond.type = typeSel.value;
      save();
      openLinkInspector(t);
    });
    typeRow.appendChild(typeK);
    typeRow.appendChild(typeSel);
    host.appendChild(typeRow);

    const spec = transitionType(cond.type);
    if (typeHasField(spec, "turns")) {
      const stepper = document.createElement("div");
      stepper.className = "eg-insp-step";
      const minus = document.createElement("button");
      minus.type = "button";
      minus.textContent = "–";
      const val = document.createElement("span");
      val.textContent = String(cond.turns || 8);
      const plus = document.createElement("button");
      plus.type = "button";
      plus.textContent = "+";
      function bump(d) {
        cond.turns = Math.max(1, Math.min(99, (parseInt(cond.turns, 10) || 8) + d));
        val.textContent = String(cond.turns);
        save();
        paintEdges();
      }
      minus.addEventListener("click", () => bump(-1));
      plus.addEventListener("click", () => bump(1));
      stepper.appendChild(minus);
      stepper.appendChild(val);
      stepper.appendChild(plus);
      const unit = document.createElement("span");
      unit.className = "eg-insp-unit";
      unit.textContent = "turns in this World, then go.";
      host.appendChild(stepper);
      host.appendChild(unit);
    } else {
      const hint = document.createElement("p");
      hint.className = "eg-insp-unit";
      hint.textContent = spec.hint || "This Type has no extra fields yet.";
      host.appendChild(hint);
    }

    const foot = document.createElement("div");
    foot.className = "eg-insp-foot";
    const rm = document.createElement("button");
    rm.type = "button";
    rm.className = "eg-insp-remove";
    rm.textContent = "Remove";
    rm.addEventListener("click", async () => {
      if (B.removeTransition) await B.removeTransition(t.id);
      closeInspector();
      sync();
    });
    const done = document.createElement("button");
    done.type = "button";
    done.className = "eg-insp-done";
    done.textContent = "Done";
    done.addEventListener("click", closeInspector);
    foot.appendChild(rm);
    foot.appendChild(done);
    host.appendChild(foot);

    function save() {
      t.condition = { type: cond.type, turns: cond.turns };
      if (B.updateTransition) B.updateTransition(t.id, { condition: t.condition });
    }
    paintEdges();
    syncMenuStack();
  }

  function stampWorldName(n, name, blurb) {
    if (!n) return;
    n.fullName = name;
    n.label = clipLabel(name);
    if (blurb !== undefined) {
      n.blurb = blurb;
      n.sub = blurb || (n.isStart ? "Start." : "Name this place, then edit it.");
    }
    if (n.g) {
      const t = n.g.querySelector(".eg-name");
      if (t) t.textContent = n.label;
      const cap = n.g.querySelector(".eg-world-cap");
      if (cap) cap.textContent = name;
    }
    renderHud();
  }

  function persistWorldCard(n, name, blurb) {
    const label = String(name || "").trim() || (n.fullName || "New Level");
    const line = blurb == null ? (n.blurb || "") : String(blurb);
    stampWorldName(n, label, line);
    if (!B.renameExperienceWorld) return;
    Promise.resolve(B.renameExperienceWorld(n.worldId, label, line)).catch(() => {
      if (B.toast) B.toast("Couldn't rename that World.", "warn");
    });
  }

  function openWorldInspector(n, opts) {
    if (!n || n.kind !== "world-node") return;
    closeSheet();
    const host = ensureInspector();
    if (!host) return;
    const focus = !!(opts && opts.focus);
    const exp = experienceState();
    const world = (exp.worlds || []).find((w) => w.id === n.worldId) || {};
    openWorldId = n.worldId;
    openEdgeId = null;
    openLore = false;
    host.innerHTML = "";
    host.classList.add("is-open", "is-world");
    host.dataset.kind = "world";
    host.dataset.worldId = n.worldId;
    host.setAttribute("aria-hidden", "false");

    const name = document.createElement("input");
    name.type = "text";
    name.className = "eg-insp-name";
    name.value = world.name || n.fullName || n.label || "";
    name.placeholder = "Name this level";
    name.maxLength = 32;
    name.autocomplete = "off";
    name.spellcheck = false;
    name.setAttribute("aria-label", "Level name");
    host.appendChild(name);
    if (n.isStart) {
      const flag = document.createElement("div");
      flag.className = "eg-insp-flag";
      flag.textContent = "Start";
      host.appendChild(flag);
    }

    let saveTimer = null;
    function queueSave() {
      stampWorldName(n, name.value, n.blurb);
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => persistWorldCard(n, name.value, n.blurb), 280);
    }
    name.addEventListener("input", queueSave);
    function flush() {
      clearTimeout(saveTimer);
      persistWorldCard(n, name.value, n.blurb);
    }
    name.addEventListener("change", flush);
    name.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); flush(); name.blur(); }
    });

    const design = document.createElement("button");
    design.type = "button";
    design.className = "eg-insp-design";
    design.textContent = "Edit";
    design.addEventListener("click", () => {
      flush();
      diveWorld(n);
    });
    host.appendChild(design);

    if (!n.isStart) {
      const start = document.createElement("button");
      start.type = "button";
      start.className = "eg-insp-start";
      start.textContent = "Start here";
      start.addEventListener("click", async () => {
        flush();
        if (document.activeElement) document.activeElement.blur();
        if (B.setStartWorld) await B.setStartWorld(n.worldId);
        sync();
        const now = nodesById["world:" + n.worldId];
        if (now) openWorldInspector(now, { focus: false });
      });
      host.appendChild(start);
    }

    paintEdges();
    syncMenuStack();
    if (focus) {
      requestAnimationFrame(() => {
        name.focus();
        name.select();
      });
    }
  }

  function persistCutsceneCard(n, name, patch) {
    const label = String(name || "").trim() || (n.fullName || "Cutscene");
    stampWorldName(n, label, (patch && patch.blurb) || n.blurb);
    if (!B.renameExperienceCutscene) return;
    Promise.resolve(B.renameExperienceCutscene(n.cutsceneId, label, patch || {})).catch(() => {
      if (B.toast) B.toast("Couldn't rename that Cutscene.", "warn");
    });
  }

  function openCutsceneInspector(n, opts) {
    if (!n || n.kind !== "cutscene-node") return;
    closeSheet();
    const host = ensureInspector();
    if (!host) return;
    const focus = !!(opts && opts.focus);
    const exp = experienceState();
    const rec = ((exp.cutscenes) || []).find((c) => c.id === n.cutsceneId) || {};
    openWorldId = null;
    openEdgeId = null;
    openLore = false;
    host.innerHTML = "";
    host.classList.add("is-open", "is-world");
    host.dataset.kind = "cutscene";
    host.dataset.cutsceneId = n.cutsceneId;
    host.setAttribute("aria-hidden", "false");

    const name = document.createElement("input");
    name.type = "text";
    name.className = "eg-insp-name";
    name.value = rec.name || n.fullName || n.label || "";
    name.placeholder = "Name this cutscene";
    name.maxLength = 32;
    name.autocomplete = "off";
    name.spellcheck = false;
    name.setAttribute("aria-label", "Cutscene name");
    host.appendChild(name);

    const flag = document.createElement("div");
    flag.className = "eg-insp-flag";
    flag.textContent = "Cutscene";
    host.appendChild(flag);

    let saveTimer = null;
    function flush() {
      clearTimeout(saveTimer);
      persistCutsceneCard(n, name.value, {
        mood: rec.mood || n.mood,
        source: rec.source || n.source,
        shot_brief: rec.shot_brief || n.shotBrief,
        blurb: rec.blurb || n.blurb,
      });
    }
    name.addEventListener("input", () => {
      stampWorldName(n, name.value, n.blurb);
      clearTimeout(saveTimer);
      saveTimer = setTimeout(flush, 280);
    });
    name.addEventListener("change", flush);
    name.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); flush(); name.blur(); }
    });

    const open = document.createElement("button");
    open.type = "button";
    open.className = "eg-insp-design";
    open.textContent = "Edit";
    open.addEventListener("click", () => {
      flush();
      openSheet(n);
    });
    host.appendChild(open);

    if (!n.isStart) {
      const start = document.createElement("button");
      start.type = "button";
      start.className = "eg-insp-start";
      start.textContent = "Start here";
      start.addEventListener("click", async () => {
        flush();
        if (document.activeElement) document.activeElement.blur();
        if (B.setStartWorld) await B.setStartWorld(n.cutsceneId);
        sync();
        const now = nodesById["cutscene:" + n.cutsceneId];
        if (now) openCutsceneInspector(now, { focus: false });
      });
      host.appendChild(start);
    } else {
      const flagStart = document.createElement("div");
      flagStart.className = "eg-insp-flag";
      flagStart.textContent = "PLAY starts here";
      host.appendChild(flagStart);
    }

    paintEdges();
    syncMenuStack();
    if (focus) {
      requestAnimationFrame(() => {
        name.focus();
        name.select();
      });
    }
  }

  function stampLoreNode(lore) {
    const exp = experienceState();
    if (exp) exp.lore = lore || loreState();
    const n = nodesById["lore"];
    if (!n) return;
    const rich = loreRichness(exp.lore);
    n.loreChars = rich.chars;
    n.loreCount = rich.count;
    n.loreDocs = rich.docs;
    n.loreNotes = rich.notes;
    n.isRich = rich.rich;
    n.sub = rich.rich
      ? (rich.count === 1 ? "1 piece of background." : rich.count + " pieces of background.")
      : "Drop the history of this place.";
    applyLoreFace(n);
    if (n.g) n.g.classList.toggle("is-rich", !!n.isRich);
    paintLoreInspectorViz();
  }

  function paintLoreInspectorViz() {
    const host = els.inspector && els.inspector.querySelector(".eg-lore-viz");
    if (!host) return;
    const rich = loreRichness();
    host.innerHTML = "";
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("viewBox", "-80 -80 160 160");
    svg.setAttribute("class", "eg-lore-viz-svg");
    const well = document.createElementNS(SVG, "circle");
    well.setAttribute("r", "36");
    well.setAttribute("class", "eg-lore-viz-well");
    svg.appendChild(well);
    const pieces = (rich.docs || []).slice();
    if (String(rich.notes || "").trim()) {
      pieces.unshift({ id: "notes", kind: "notes", name: "Notes" });
    }
    const max = Math.min(pieces.length, 12);
    for (let i = 0; i < max; i++) {
      const ang = -Math.PI / 2 + (Math.PI * 2 * i) / Math.max(max, 3);
      const rad = 52 + (i % 2) * 8;
      const c = document.createElementNS(SVG, "circle");
      c.setAttribute("cx", String(Math.cos(ang) * rad));
      c.setAttribute("cy", String(Math.sin(ang) * rad));
      c.setAttribute("r", pieces[i].kind === "image" ? "6.5" : "5");
      c.setAttribute("class", "eg-lore-viz-bit"
        + (pieces[i].kind === "image" ? " is-image" : "")
        + (pieces[i].kind === "notes" ? " is-notes" : ""));
      svg.appendChild(c);
    }
    host.appendChild(svg);
    const meta = document.createElement("div");
    meta.className = "eg-lore-viz-meta";
    if (!rich.rich) {
      meta.textContent = "Empty. The Worlds share whatever you drop here.";
    } else {
      const words = Math.max(1, Math.round(rich.chars / 5));
      meta.textContent = words + (words === 1 ? " word" : " words")
        + (rich.docs.length ? " · " + rich.docs.length + (rich.docs.length === 1 ? " file" : " files") : "");
    }
    host.appendChild(meta);
  }

  function paintLoreDocList(list) {
    if (!list) return;
    list.innerHTML = "";
    const docs = loreState().documents || [];
    if (!docs.length) {
      const empty = document.createElement("div");
      empty.className = "eg-lore-empty";
      empty.textContent = "No files yet.";
      list.appendChild(empty);
      return;
    }
    docs.forEach((doc) => {
      const row = document.createElement("div");
      row.className = "eg-lore-doc" + (doc.kind === "image" ? " is-image" : "");
      if (doc.kind === "image" && doc.url) {
        const thumb = document.createElement("img");
        thumb.className = "eg-lore-thumb";
        thumb.src = doc.url;
        thumb.alt = "";
        row.appendChild(thumb);
      } else {
        const mark = document.createElement("span");
        mark.className = "eg-lore-mark";
        mark.textContent = "Aa";
        row.appendChild(mark);
      }
      const label = document.createElement("span");
      label.className = "eg-lore-doc-name";
      label.textContent = doc.name || doc.id;
      row.appendChild(label);
      const kill = document.createElement("button");
      kill.type = "button";
      kill.className = "eg-lore-doc-x";
      kill.setAttribute("aria-label", "Remove " + (doc.name || "file"));
      kill.textContent = "×";
      kill.addEventListener("click", async () => {
        if (!B.removeLoreDocument) return;
        try {
          const lore = await B.removeLoreDocument(doc.id);
          stampLoreNode(lore);
          paintLoreDocList(list);
        } catch (_) {
          if (B.toast) B.toast("Couldn't remove that.", "warn");
        }
      });
      row.appendChild(kill);
      list.appendChild(row);
    });
  }

  function readDroppedFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(new Error("read failed"));
      const isImage = /^image\//.test(file.type) || /\.(png|jpe?g|webp|gif)$/i.test(file.name || "");
      reader.onload = () => resolve({
        name: file.name || (isImage ? "plate.png" : "note.md"),
        image: isImage ? String(reader.result || "") : "",
        text: isImage ? "" : String(reader.result || ""),
      });
      if (isImage) reader.readAsDataURL(file);
      else reader.readAsText(file);
    });
  }

  function openLoreInspector(n, opts) {
    if (!n || n.kind !== "lore-node") return;
    closeSheet();
    const host = ensureInspector();
    if (!host) return;
    const focus = !!(opts && opts.focus);
    openLore = true;
    openWorldId = null;
    openEdgeId = null;
    host.innerHTML = "";
    host.classList.add("is-open", "is-lore");
    host.dataset.kind = "lore";
    host.setAttribute("aria-hidden", "false");

    const title = document.createElement("div");
    title.className = "eg-insp-flag";
    title.textContent = "Lore";
    host.appendChild(title);

    if (loreState().source === "world") {
      const hint = document.createElement("p");
      hint.className = "eg-insp-unit";
      hint.textContent = "From the start World. Edit to keep it on this Experience.";
      host.appendChild(hint);
    }

    const viz = document.createElement("div");
    viz.className = "eg-lore-viz";
    host.appendChild(viz);

    const notes = document.createElement("textarea");
    notes.className = "eg-lore-notes";
    notes.placeholder = "The history of this place. Who built it. What they buried. The player discovers this — they are not told it.";
    notes.value = loreState().notes || "";
    notes.setAttribute("aria-label", "Background notes");
    notes.rows = 5;
    host.appendChild(notes);

    const drop = document.createElement("label");
    drop.className = "eg-lore-drop";
    drop.textContent = "Drop .md / .txt / pictures";
    const file = document.createElement("input");
    file.type = "file";
    file.accept = ".md,.txt,image/png,image/jpeg,image/webp,image/gif";
    file.multiple = true;
    file.hidden = true;
    drop.appendChild(file);
    host.appendChild(drop);

    const list = document.createElement("div");
    list.className = "eg-lore-docs";
    host.appendChild(list);

    let saveTimer = null;
    function queueNotes() {
      const exp = experienceState();
      if (!exp.lore) exp.lore = { enabled: true, notes: "", documents: [] };
      exp.lore.notes = notes.value;
      stampLoreNode(exp.lore);
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => {
        if (B.saveLoreNotes) B.saveLoreNotes(notes.value).catch(() => {});
      }, 280);
    }
    notes.addEventListener("input", queueNotes);

    async function ingest(files) {
      const batch = Array.from(files || []);
      if (!batch.length || !B.addLoreDocument) return;
      drop.classList.add("is-busy");
      try {
        for (const item of batch) {
          const packed = await readDroppedFile(item);
          const lore = await B.addLoreDocument(packed);
          stampLoreNode(lore);
        }
        paintLoreDocList(list);
      } catch (_) {
        if (B.toast) B.toast("Couldn't add that file.", "warn");
      }
      drop.classList.remove("is-busy");
    }
    file.addEventListener("change", () => {
      ingest(file.files);
      file.value = "";
    });
    drop.addEventListener("dragover", (e) => {
      e.preventDefault();
      drop.classList.add("is-hot");
    });
    drop.addEventListener("dragleave", () => drop.classList.remove("is-hot"));
    drop.addEventListener("drop", (e) => {
      e.preventDefault();
      drop.classList.remove("is-hot");
      ingest(e.dataTransfer && e.dataTransfer.files);
    });

    paintLoreInspectorViz();
    paintLoreDocList(list);
    paintEdges();
    syncMenuStack();
    if (focus) {
      requestAnimationFrame(() => notes.focus());
    }
  }

  function sheetPrompt(n, body) {
    if (!promptEditor(body, n.prompt)) {
      note(body, "This prompt isn't wired yet.");
    }
  }

  function sheetSystem(n, body) {
    note(body, "How this session draws. These take effect on the live run \u2014 they are not story text.");
    fillPicturePicker(group(body, "Picture"), n);
    fillWorldModelPicker(group(body, "World model"), n, { hideIfEmpty: true });
    fillImageModelPicker(group(body, "Image model"), n, { hideIfEmpty: true });
  }

  function sheetWorldNode(n, body) {
    note(body, n.isStart
      ? "This is the start."
      : "Name the place, then open it to edit Level, Character, and the rest.");
    const name = document.createElement("input");
    name.type = "text";
    name.className = "eg-select";
    name.value = (n.fullName || n.label || "World");
    name.placeholder = "Name this level…";
    name.maxLength = 32;
    const row = fieldRow(body, "Name");
    row.appendChild(name);
    const blurb = document.createElement("input");
    blurb.type = "text";
    blurb.className = "eg-select";
    blurb.value = n.blurb || "";
    blurb.placeholder = "What is this place?";
    blurb.maxLength = 80;
    const brief = fieldRow(body, "What is this place?");
    brief.appendChild(blurb);
    async function saveCard() {
      if (!B.renameExperienceWorld) return;
      await B.renameExperienceWorld(n.worldId, name.value, blurb.value);
      sync();
    }
    name.addEventListener("change", saveCard);
    blurb.addEventListener("change", saveCard);
    if (!n.isStart) {
      button(body, "Start here", "", async () => {
        await saveCard();
        if (document.activeElement) document.activeElement.blur();
        if (B.setStartWorld) await B.setStartWorld(n.worldId);
        sync();
      });
    }
    button(body, "Edit", "we-btn-primary", () => {
      closeSheet();
      diveWorld(n);
    });
    // The look book lives with the World, where the author is looking — not
    // only on HARNESS > Image, which nobody opening a World ever sees.
    fillLookBook(group(body, "Look book"), n);
    if (B.resetWorldPicture) {
      button(body, "Reset this World", "", async () => {
        await saveCard();
        await B.resetWorldPicture(n.worldId);
        sync();
      });
    }
  }

  function sheetCutscene(n, body) {
    note(body, n.isStart
      ? "PLAY opens on this montage, then follows the outgoing link."
      : "A 4-shot cinematic montage. Link a World into this, then out to the next World. Mark Start here to open PLAY on it.");
    const name = document.createElement("input");
    name.type = "text";
    name.className = "eg-select";
    name.value = n.fullName || n.label || "Cutscene";
    name.maxLength = 32;
    fieldRow(body, "Name").appendChild(name);
    const moods = [
      { id: "threshold", label: "Threshold" },
      { id: "aftermath", label: "Aftermath" },
      { id: "arrival", label: "Arrival" },
      { id: "departure", label: "Departure" },
      { id: "encounter", label: "Encounter" },
    ];
    let mood = n.mood || "threshold";
    selectRow(body, "Mood", moods, mood, "", (id) => { mood = id; save(); });
    const sources = [
      { id: "incoming", label: "Incoming plate" },
      { id: "dest_world", label: "Destination World frame" },
    ];
    let source = n.source || "incoming";
    selectRow(body, "Source", sources, source, "", (id) => { source = id; save(); });
    const brief = document.createElement("input");
    brief.type = "text";
    brief.className = "eg-select";
    brief.value = n.shotBrief || "";
    brief.placeholder = "Optional camera direction…";
    brief.maxLength = 160;
    fieldRow(body, "Shot brief").appendChild(brief);
    async function save() {
      if (!B.renameExperienceCutscene) return;
      await B.renameExperienceCutscene(n.cutsceneId, name.value, {
        mood: mood,
        source: source,
        shot_brief: brief.value,
      });
      n.mood = mood;
      n.source = source;
      n.shotBrief = brief.value;
      sync();
    }
    name.addEventListener("change", save);
    brief.addEventListener("change", save);
    if (!n.isStart) {
      button(body, "Start here", "", async () => {
        await save();
        if (document.activeElement) document.activeElement.blur();
        if (B.setStartWorld) await B.setStartWorld(n.cutsceneId);
        sync();
      });
    } else {
      note(body, "PLAY starts on this Cutscene.");
    }
  }

  function sheetTransition(n, body) {
    const t = n.transition || {};
    const cond = t.condition || { type: "turn_count", turns: 8 };
    note(body, (n.fromName || "World") + " → " + (n.toName || "World"));
    selectRow(body, "Type", transitionTypes().map((row) => ({
      id: row.id, label: row.label,
    })), cond.type, "", (id) => {
      cond.type = id;
      saveCond();
    });
    const turnsHost = document.createElement("div");
    body.appendChild(turnsHost);
    function paintTurns() {
      turnsHost.innerHTML = "";
      if (!typeHasField(transitionType(cond.type), "turns")) return;
      numberRow(turnsHost, "Turns", { min: 1, max: 99, step: 1 }, cond.turns || 8, "", (v) => {
        cond.turns = parseInt(v, 10) || 8;
        saveCond();
      });
    }
    function saveCond() {
      paintTurns();
      if (B.updateTransition) B.updateTransition(t.id, { condition: cond });
    }
    paintTurns();
    button(body, "Remove this link", "we-btn-ghost", async () => {
      if (B.removeTransition) await B.removeTransition(t.id);
      closeSheet();
      sync();
    });
  }

  function bindToolkit() {
    if (!els.toolkit) return;
    let spawnConsumed = false;
    els.toolkit.addEventListener("click", (evt) => {
      const b = evt.target.closest(".eg-tool");
      if (!b) return;
      const next = b.getAttribute("data-tool");
      if (next === "add-world") {
        if (spawnConsumed) { spawnConsumed = false; return; }
        addWorldFromToolkit();
        return;
      }
      if (next === "add-cutscene") {
        addCutsceneFromToolkit();
        return;
      }
      setTool(next);
    });
    const addBtn = els.toolkit.querySelector('[data-tool="add-world"]');
    if (!addBtn) return;

    function onSpawnMove(evt) {
      if (!ptr || ptr.mode !== "spawn-tool") return;
      const p = toWorld(evt.clientX, evt.clientY);
      if (p) pointerWorld = p;
      if (Math.hypot(evt.clientX - ptr.x0, evt.clientY - ptr.y0) >= DRAG_PX) {
        ptr.moved = true;
        if (els.graph) els.graph.classList.add("is-placing");
        setGhost(p);
        renderHud();
      }
    }
    function onSpawnUp(evt) {
      if (!ptr || ptr.mode !== "spawn-tool") return;
      const moved = !!ptr.moved;
      const p = toWorld(evt.clientX, evt.clientY);
      window.removeEventListener("pointermove", onSpawnMove);
      window.removeEventListener("pointerup", onSpawnUp, true);
      window.removeEventListener("pointercancel", onSpawnUp, true);
      ptr = null;
      setGhost(null);
      if (els.graph) els.graph.classList.remove("is-placing");
      setTool("select");
      if (!moved) return;
      spawnConsumed = true;
      const box = els.canvas.getBoundingClientRect();
      const over = evt.clientX >= box.left && evt.clientX <= box.right &&
        evt.clientY >= box.top && evt.clientY <= box.bottom;
      if (over && p) placeNewWorld(p);
    }
    addBtn.addEventListener("pointerdown", (evt) => {
      if (evt.button) return;
      if (!isExperience()) return;
      if (openId === null && root) setOpen(root.id, true);
      ptr = {
        mode: "spawn-tool",
        x0: evt.clientX,
        y0: evt.clientY,
        pointerId: evt.pointerId,
        moved: false,
      };
      window.addEventListener("pointermove", onSpawnMove);
      window.addEventListener("pointerup", onSpawnUp, true);
      window.addEventListener("pointercancel", onSpawnUp, true);
    });
  }

  function onPointerDown(evt) {
    if (evt.button != null && evt.button !== 0) return;
    if (!els.canvas) return;
    const at = toWorld(evt.clientX, evt.clientY);
    if (at) pointerWorld = at;
    const h = hitAt(evt.clientX, evt.clientY);
    if (h && h.where === "kill") {
      ptr = { mode: "kill", worldId: graphId(h.node), kind: h.node.kind, pointerId: evt.pointerId };
      try { els.canvas.setPointerCapture(evt.pointerId); } catch (_) {}
      evt.preventDefault();
      return;
    }
    if (h && h.where === "gizmo") {
      closeInspector();
      ptr = { mode: "connect", fromId: graphId(h.node), pointerId: evt.pointerId, dropId: null };
      connectFrom = graphId(h.node);
      selectNode(h.node.id);
      try { els.canvas.setPointerCapture(evt.pointerId); } catch (_) {}
      evt.preventDefault();
      renderHud();
      return;
    }
    if (tool === "select" && onExperienceRing() && h && h.node &&
        (h.node.kind === "world-node" || h.node.kind === "cutscene-node"
          || h.node.kind === "experience-node" || h.node.kind === "lore-node") &&
        h.where === "orbit") {
      ptr = {
        mode: "maybe-drag",
        id: h.node.id,
        worldId: h.node.worldId,
        x0: evt.clientX,
        y0: evt.clientY,
        pointerId: evt.pointerId,
      };
      selectNode(h.node.id);
      try { els.canvas.setPointerCapture(evt.pointerId); } catch (_) {}
      return;
    }
    if (tool === "select" && openId !== null && view &&
        (!h || h.where === "empty")) {
      ptr = {
        mode: "maybe-pan",
        pointerId: evt.pointerId,
        x0: evt.clientX,
        y0: evt.clientY,
        cx0: view.cx,
        cy0: view.cy,
        scale: worldPerPx(),
      };
      try { els.canvas.setPointerCapture(evt.pointerId); } catch (_) {}
      return;
    }
    ptr = { mode: "tap", pointerId: evt.pointerId, x0: evt.clientX, y0: evt.clientY };
  }

  function onPointerMove(evt) {
    if (!ptr || ptr.mode === "spawn-tool") {
      if (!ptr) onHover(evt);
      return;
    }
    const p = toWorld(evt.clientX, evt.clientY);
    if (p) pointerWorld = p;
    if (ptr.mode === "maybe-drag") {
      if (Math.hypot(evt.clientX - ptr.x0, evt.clientY - ptr.y0) < DRAG_PX) return;
      ptr.mode = "drag";
      acted();
    }
    if (ptr.mode === "maybe-pan") {
      if (Math.hypot(evt.clientX - ptr.x0, evt.clientY - ptr.y0) < DRAG_PX) return;
      ptr.mode = "pan";
      stopViewAnim();
      if (els.graph) els.graph.classList.add("is-panning");
      els.canvas.style.cursor = "grabbing";
    }
    if (ptr.mode === "pan") {
      applyView({
        cx: ptr.cx0 - (evt.clientX - ptr.x0) * ptr.scale,
        cy: ptr.cy0 - (evt.clientY - ptr.y0) * ptr.scale,
        half: view.half,
      });
      rememberCam();
      return;
    }
    if (ptr.mode === "drag") {
      const n = nodesById[ptr.id];
      if (!n || !p) return;
      n.homeX = p.x;
      n.homeY = p.y;
      n.cx = p.x;
      n.cy = p.y;
      place();
      return;
    }
    if (ptr.mode === "connect" && p) {
      const src = worldNodeOf(ptr.fromId);
      if (!src) return;
      const dstNode = worldDropAt(p, ptr.fromId);
      ptr.dropId = dstNode ? graphId(dstNode) : null;
      markWorldDrop(ptr.dropId);
      setRubber({ x: src.cx, y: src.cy, r: src.r }, dstNode
        ? { x: dstNode.cx, y: dstNode.cy, r: dstNode.r }
        : { x: p.x, y: p.y, r: 0 });
      place();
      els.canvas.style.cursor = dstNode ? "pointer" : "grabbing";
    }
  }

  function onPointerUp(evt) {
    if (!ptr || ptr.mode === "spawn-tool") return;
    const gesture = ptr;
    if (gesture.mode === "kill") {
      ptr = null;
      try { els.canvas.releasePointerCapture(gesture.pointerId); } catch (_) {}
      const h = hitAt(evt.clientX, evt.clientY);
      if (h && h.where === "kill" && h.node && graphId(h.node) === gesture.worldId) {
        if (gesture.kind === "cutscene-node") removeCutscene(gesture.worldId);
        else removeWorld(gesture.worldId);
      }
      return;
    }
    if (gesture.mode === "connect") {
      // Hit-test while still pulling. Clearing ptr first put the satellite
      // (which had been following the mouse) under the cursor, so every drop
      // read as "same World" and the noodle never stuck.
      const p = toWorld(evt.clientX, evt.clientY);
      const dst = worldDropAt(p, gesture.fromId);
      ptr = null;
      try { els.canvas.releasePointerCapture(gesture.pointerId); } catch (_) {}
      if (dst && graphId(dst) !== gesture.fromId) finishLink(gesture.fromId, graphId(dst));
      else {
        connectFrom = null;
        setRubber(null);
        paint();
        renderHud();
      }
      return;
    }
    ptr = null;
    try { els.canvas.releasePointerCapture(gesture.pointerId); } catch (_) {}
    if (gesture.mode === "pan") {
      if (els.graph) els.graph.classList.remove("is-panning");
      rememberCam();
      return;
    }
    if (gesture.mode === "maybe-pan") {
      if (els.graph) els.graph.classList.remove("is-panning");
      onTap(evt);
      return;
    }
    if (gesture.mode === "drag") {
      setRubber(null);
      const n = nodesById[gesture.id];
      if (n && n.homeX != null && n.worldId && B.moveExperienceWorld) {
        Promise.resolve(B.moveExperienceWorld(n.worldId, n.homeX, n.homeY)).catch(() => {});
      } else if (n && n.kind === "cutscene-node" && n.homeX != null && n.cutsceneId && B.moveExperienceCutscene) {
        Promise.resolve(B.moveExperienceCutscene(n.cutsceneId, n.homeX, n.homeY)).catch(() => {});
      } else if (n && n.kind === "experience-node" && n.homeX != null && B.moveExperience) {
        Promise.resolve(B.moveExperience(n.homeX, n.homeY)).catch(() => {});
      } else if (n && n.kind === "lore-node" && n.homeX != null && B.moveLore) {
        Promise.resolve(B.moveLore(n.homeX, n.homeY)).catch(() => {});
      }
      return;
    }
    if (gesture.mode === "tap" || gesture.mode === "maybe-drag") {
      onTap(evt);
    }
  }

  // ══════════════════════════════════════════════════════════════════
  // THE WINDOW — the few fields that steer this dot, and nothing else
  // ══════════════════════════════════════════════════════════════════
  function openSheet(n) {
    closeInspector();
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
      narrator: sheetNarrator,
      music: sheetMusic,
      pacing: sheetPacing,
      world: sheetWorld,
      text: sheetText,
      image: sheetImage,
      voice: sheetVoice,
      prompt: sheetPrompt,
      system: sheetSystem,
      "world-node": sheetWorldNode,
      "cutscene-node": sheetCutscene,
      transition: sheetTransition,
      "sound-palette": sheetSoundPalette,
      "sound-family": sheetSoundFamily,
    }[n.kind] || sheetSpec;
    fill(n, els.sheetBody);
    paint();
    syncMenuStack();
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
  function fieldRow(host, label, _help) {
    const row = document.createElement("label");
    row.className = "eg-field";
    const k = document.createElement("span");
    k.className = "eg-field-k";
    k.textContent = label;
    row.appendChild(k);
    host.appendChild(row);
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

  // What we last told someone about a key, so a rebuilt window can say it
  // again. Outlives the DOM node on purpose: the outcome of a save is a fact
  // about the key, and the element that displayed it is torn down BY the save.
  // Short-lived, because reopening a window ten minutes later to be told about
  // an edit you have forgotten making is its own kind of noise.
  const SAID_TTL_MS = 12000;
  const saidByKey = Object.create(null);

  function remember(key, text, warn) {
    if (!text) delete saidByKey[key];
    else saidByKey[key] = { text: text, warn: !!warn, at: Date.now() };
  }

  function recall(key) {
    const rec = saidByKey[key];
    if (!rec) return null;
    if (Date.now() - rec.at > SAID_TTL_MS) { delete saidByKey[key]; return null; }
    return rec;
  }

  // Say it to whichever element is on screen for this key RIGHT NOW, which is
  // not necessarily the one the caller was holding. A save re-renders, and the
  // re-render lands in the middle of the save — so by the time there is an
  // outcome to report, the paragraph the reporting code closed over has already
  // been detached, and writing to it puts the good news in a node nobody can
  // see while the rebuilt one still reads "Unsaved".
  function paintSaid(key) {
    if (!els.sheetBody) return;
    const el = els.sheetBody.querySelector('.eg-said[data-key="' + key + '"]');
    if (!el) return;
    const rec = recall(key);
    el.textContent = rec ? rec.text : "";
    el.classList.toggle("is-warn", !!(rec && rec.warn));
    el.classList.toggle("is-on", !!rec);
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

    const acts = document.createElement("div");
    acts.className = "eg-row eg-acts";
    host.appendChild(acts);

    // What happens to a saved edit, said plainly and in the same place every
    // time. Most re-steer the sim on the next turn; a couple only seed a fresh
    // world, and for those "it saved and nothing changed" is the correct and
    // deeply unhelpful outcome unless someone says so.
    const restarts = B.isRestartKey(key);
    const said = document.createElement("p");
    said.className = "eg-note eg-said";
    said.dataset.key = key;
    host.appendChild(said);
    const announce = (text, warn) => { remember(key, text, warn); paintSaid(key); };
    // Saving re-renders, and the re-render is what was eating the confirmation:
    // a successful save calls back through the bridge, which rebuilds the dots,
    // which reopens this window from scratch — so the one sentence telling you
    // the edit landed was destroyed a few milliseconds after being written, by
    // the save that wrote it. Which looks exactly like nothing having happened.
    paintSaid(key);

    let saving = false;
    let savedText = String(B.valOf(key) || "");
    async function commit(quiet) {
      if (saving || ta.value === savedText) return true;
      saving = true;
      const value = ta.value;
      const res = await B.saveField(key, value);
      saving = false;
      if (!res || !res.ok) {
        announce("That didn't save. Check the highlighted placeholder.", true);
        if (!quiet) B.toast("Couldn't save that.", "warn");
        return false;
      }
      savedText = value;
      announce(restarts
        ? "Saved. This one seeds the world \u2014 start a fresh run to hear it."
        : "Saved. The next turn uses it.");
      if (!quiet) B.toast("Saved \u2014 live now.");
      return true;
    }

    ta.addEventListener("input", () => {
      B.setEdit(key, ta.value);
      if (ta.value !== savedText) announce("Unsaved");
    });
    // Leaving the field IS the save, the same as every cast field in this
    // editor. Typing a new narrator, closing the window and hearing the old one
    // was the single most common way to conclude that the editor was broken:
    // the sheets that saved on blur and the sheets that hid the commit behind a
    // button looked identical, so which ones worked was a thing you had to
    // learn by being burned.
    ta.addEventListener("blur", () => { commit(true); });
    // ...and closing the window counts as leaving the field. Tearing out the
    // sheet's markup does not reliably fire `blur` on the node being removed,
    // so closeSheet asks every prompt on it to commit first.
    ta._commit = commit;

    button(acts, "Save", "we-btn-primary", async () => {
      if (await commit(false)) sync();
    });
    button(acts, "Full editor", "", async () => {
      await commit(true);
      closeSheet();
      B.openPrompt(key);
    });
    const clearBtn = button(acts, "Clear", "we-btn-ghost", async () => {
      ta.value = "";
      B.setEdit(key, "");
      const res = await B.saveField(key, "");
      if (!res || !res.ok) {
        announce("That didn't save.", true);
        B.toast("Couldn't clear that.", "warn");
        return;
      }
      savedText = "";
      remember(key, "Cleared.", false);
      B.toast("Cleared.");
      sync();
    });
    clearBtn.dataset.action = "clear-prompt";
    if (String(B.valOf(key) || "") !== String(B.defOf(key) || "")) {
      button(acts, "Reset", "we-btn-ghost", async () => {
        await B.resetField(key);
        ta.value = String(B.valOf(key) || "");
        savedText = ta.value;
        announce("Back to the shipped wording.");
        B.toast("Back to the shipped shot.");
      });
    }
    if (restarts) {
      button(acts, "Start a fresh run", "we-btn-ghost", async () => {
        await commit(true);
        try { B.saveAndRestart(); } catch (_) { B.toast("Couldn't restart.", "warn"); }
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

  // Report what the game is USING, not what we asked it to use.
  //
  // "Saved." was a lie the UI had no way of detecting: the endpoint answers with
  // the effective values, and we threw them away and printed optimism. So when a
  // knob silently clamped, or the store took it and the live module didn't, the
  // panel said the same reassuring word either way — and the editor stopped
  // being believable long before anyone found out which knob was lying.
  async function setTunable(name, value) {
    const res = await putJson("/api/admin/studio/tunables", { [name]: value });
    if (!res.ok) { B.toast(res.error || "Couldn't save that.", "warn"); return false; }
    tunables = res.data;
    const live = (res.data && res.data.values) ? res.data.values[name] : undefined;
    if (live !== undefined && String(live) !== String(value)) {
      B.toast("Saved, but the game is using " + live + ".", "warn");
      return true;
    }
    B.toast("Saved \u2014 live now.");
    return true;
  }

  // A group that has not filled in yet, saying so. A window whose panels are
  // blank while their fetches land is indistinguishable from a window with
  // nothing in it, and the reasonable thing to do with one of those is close it.
  function pending(host) {
    const p = note(host, "\u2026");
    if (p) p.classList.add("eg-pending");
    return p;
  }

  // ── Mechanics ─────────────────────────────────────────────────────────
  function savePacing(patch, quiet) {
    if (!B.savePacing) return Promise.resolve(false);
    return Promise.resolve(B.savePacing(patch)).then((ok) => {
      if (!quiet) B.toast(ok ? "Saved." : "Couldn't save that.", ok ? "" : "warn");
      return ok;
    }).catch(() => {
      if (!quiet) B.toast("Couldn't save that.", "warn");
      return false;
    });
  }

  function sheetPacing(n, body) {
    const t = pacingState();
    note(body, "The story clock rises each turn. At Escalate the choice slate hears the first beat. At Critical it hears the last stand. This is gameplay — it steers what you are offered, not the world's bible.");
    note(body, "Set Critical too low and the run reaches it in a turn or two and stays there: every beat after that is written as a last stand, which reads as chaos rather than tension.");

    const clock = group(body, "Clock");
    numberRow(clock, "Escalate at", { min: 1, max: 40, step: 1 },
      t.escalate_at,
      // These are POINTS, not turns, and saying "a typical turn adds one" was
      // how both the shipped clock and its test ended up holding turn numbers:
      // a MOVE TO or INTERACT adds two, and those are most turns in a real
      // run, so every authored mark was landing on half the turn intended.
      "Threat points, not turns \u2014 a choice adds 1, a MOVE TO or INTERACT adds 2. So 3 lands on turn 2 of a run that mostly scans. SOMEWHERE uses 3.",
      (v) => {
        savePacing({ escalate_at: Number(v) }, true).then(() => {
          if (sheetId === n.id) openSheet(n);
        });
      });
    numberRow(clock, "Critical at", { min: 2, max: 60, step: 1 },
      t.critical_at,
      "When the run is out of room \u2014 past here every turn is a last stand, so set it where you want the peak, not the start. SOMEWHERE uses 6, which is turn 3 of a scanning run (turn 6 of one that only clicks).",
      (v) => {
        savePacing({ critical_at: Number(v) }, true).then(() => {
          if (sheetId === n.id) openSheet(n);
        });
      });

    const beats = group(body, "What choices hear");
    note(beats, "These lines fill {beat_nudge}: Normal from turn one, then Escalating and Critical once the clock crosses each mark.");

    function beatBox(host, key, label, value) {
      const row = fieldRow(host, label);
      const ta = document.createElement("textarea");
      ta.className = "eg-prompt is-short";
      ta.spellcheck = false;
      ta.value = value;
      ta.setAttribute("aria-label", label);
      row.appendChild(ta);
      let timer = null;
      const flush = () => {
        const next = ta.value;
        const exp = (B.experience && B.experience()) || {};
        if (!exp.threat) exp.threat = {};
        exp.threat[key] = next;
        savePacing({ [key]: next }, true);
      };
      ta.addEventListener("input", () => {
        clearTimeout(timer);
        timer = setTimeout(flush, 360);
      });
      ta.addEventListener("blur", () => {
        clearTimeout(timer);
        flush();
      });
      return ta;
    }
    beatBox(beats, "beat_normal", "Normal (the opening)", t.beat_normal);
    beatBox(beats, "beat_escalating", "Escalating", t.beat_escalating);
    beatBox(beats, "beat_critical", "Critical", t.beat_critical);
  }

  function sheetScan(n, body) {
    const knobs = group(body, "Detector");
    const state = group(body, "Right now");
    pending(knobs);
    pending(state);

    // Two fetches, two groups, deliberately NOT awaited together. /api/health
    // asks whether the on-device detector loaded, and on the first call after a
    // boot that means waiting on MediaPipe to finish importing — seconds. The
    // knobs were behind the same Promise.all, so the one panel in the editor
    // whose whole job is changing the detector was empty for the entire time
    // anyone would plausibly be looking at it.
    loadTunables(true).then((t) => {
      knobs.innerHTML = "";
      const spec = (t && t.schema) || {};
      const vals = (t && t.values) || {};
      if (!spec.detect_backend && !spec.detect_min_score) {
        note(knobs, "Couldn't read the detector settings from the server.");
        return;
      }
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
    });

    getJson("/api/health").then((h) => {
      state.innerHTML = "";
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
        statusRow(state, "Fallback", "image model", true);
      }
    });
  }

  function sheetCamp(n, body) {
    const knobs = group(body, "The fire");
    pending(knobs);
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
    pending(who);
    getJson("/api/companions").then((c) => {
      who.innerHTML = "";
      const roster = (c && (c.companions || c.roster)) || [];
      if (!roster.length) {
        note(who, "Nobody yet.");
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
  }

  // The one voice that talks straight to the player: what it says, and who says
  // it. Both were unreachable — the wording was three f-strings in engine.py and
  // the voice was an environment variable — so this panel was four facts and a
  // button that did something you couldn't influence.
  function sheetNarrator(n, body) {
    const picks = group(body, "Voice");
    pending(picks);
    Promise.all([
      loadTunables(true),
      getJson("/api/talk/voices/library"),
      getJson("/api/talk/voices"),
    ]).then(([t, lib, reg]) => {
      picks.innerHTML = "";
      const vals = (t && t.values) || {};
      const spec = (t && t.schema) || {};
      const library = (lib && lib.voices) || [];
      const options = (library.length ? library : ((reg && reg.voices) || []))
        .map((v) => ({
          id: v.id,
          label: v.name + (v.category && v.category !== "premade" ? "  \u2022 yours" : ""),
        }));
      if (!options.length) {
        note(picks, LIBRARY_REASONS[lib && lib.reason] || "Couldn't read the voices.");
        return;
      }
      selectRow(picks, "Reads the story", options, vals.narrator_voice_id,
        spec.narrator_voice_id && spec.narrator_voice_id.help,
        (v) => setTunable("narrator_voice_id", v));
      if (!library.length) {
        note(picks, LIBRARY_REASONS[lib && lib.reason] ||
                    "Showing the shipped voices only.");
      }
    });

    // What it says.
    promptEditor(body, "narrator_direction", "What it says");

    const acts = group(body, "Try it");
    // The proof. Both settings above are next-utterance things, so the only
    // honest confirmation is hearing one — and it has to be a SINGLE line in
    // the narrator's own voice, because the rail's button asks for a radio play
    // that hands most of its lines to other members of the cast. Proxying to
    // that button meant "try it" demonstrated neither of the two things this
    // window sets.
    button(acts, "Speak a line now", "", () => {
      if (B.narratorPreview()) B.toast("Narrating\u2026");
      else B.toast("The narrator isn't available.", "warn");
    });
    const state = group(body, "Right now");
    pending(state);
    getJson("/api/health").then((h) => {
      state.innerHTML = "";
      const t = (h && h.talk) || {};
      statusRow(state, "Out loud", t.voice ? "ready" : "text only", !t.voice);
      if (!t.voice && t.reason) note(state, t.reason);
    });
  }

  // Music: write how the world sounds. That line is saved and mixed into
  // every scene score, including the next run. "Generate a loop" used to be
  // the only path, and typing without clicking it did nothing.
  function sheetMusic(n, body) {
    const write = group(body, "How it sounds");
    const now = group(body, "Playing");
    const bring = group(body, "Or bring your own");
    const title = group(body, "Title screen");
    const ambience = group(body, "Ambience");
    const test = group(body, "Test a scene");
    const stockBox = group(body, "Stock");
    const cacheBox = group(body, "Cache");
    pending(now);

    const wakeAudio = () => {
      try { window.Sound && window.Sound.resume && window.Sound.resume(); } catch (_) {}
      try { window.SceneAudio && window.SceneAudio.unlock && window.SceneAudio.unlock(); } catch (_) {}
    };

    const showClip = (host, url) => {
      if (!host || !url) return null;
      let audio = host.querySelector("audio.eg-audio");
      if (!audio) {
        audio = document.createElement("audio");
        audio.className = "eg-audio";
        audio.controls = true;
        audio.loop = true;
        host.appendChild(audio);
      }
      audio.preload = "auto";
      audio.src = url;
      return audio;
    };

    // Web Audio stays unlocked after the click. HTMLMediaElement.play after
    // an 8s generate is usually blocked — that is why Play loop never made
    // a sound. Adopt the bed first; the <audio> is just a visible control.
    const playNow = async (url, host) => {
      if (!url) return false;
      wakeAudio();
      if (host) showClip(host, url);
      try {
        const ok = await B.adoptLoop(url);
        if (ok) return true;
      } catch (_) {}
      const audio = host && host.querySelector("audio.eg-audio");
      if (audio) {
        const go = audio.play();
        if (go && typeof go.catch === "function") {
          go.catch(() => { B.toast("Hit play on the preview.", "warn"); });
        }
      }
      return false;
    };

    const playClip = async (url, host, loop) => {
      if (!url) return false;
      wakeAudio();
      const audio = showClip(host, url);
      if (audio) audio.loop = loop !== false;
      if (!audio) return false;
      try {
        await audio.play();
        return true;
      } catch (_) {
        B.toast("Hit play on the preview.", "warn");
        return false;
      }
    };

    const postMusic = async (path, payload) => {
      const r = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const d = await r.json().catch(() => null);
      return { ok: r.ok, data: (d && d.data) || d, message: d && d.message };
    };

    const draw = (state) => {
      const loop = state && state.loop;
      const direction = (state && state.direction) || "";
      const menuLoop = state && state.menu_loop;
      const menuDirection = (state && state.menu_direction) || "";
      const canGen = !!(state && state.can_generate);
      const whyGen = (state && state.can_generate_reason) ||
        "Generating needs an ElevenLabs sk_ key (ACCOUNT on the start screen).";

      now.innerHTML = "";
      if (loop) {
        statusRow(now, "Score", loop.source === "upload" ? "your upload (locked)" : "locked loop");
        if (loop.name) statusRow(now, "File", loop.name);
        if (loop.prompt) statusRow(now, "Prompt", loop.prompt);
        note(now, "This locked loop is what you hear, not the direction above, until you unlock it.");
        showClip(now, loop.url);
        const replay = button(now, "Play loop", "we-btn-primary", async () => {
          replay.disabled = true;
          replay.textContent = "Playing\u2026";
          const heard = await playNow(loop.url, now);
          B.toast(heard ? "Playing the loop." : "Couldn't start that loop.", heard ? "" : "warn");
          replay.disabled = false;
          replay.textContent = "Play loop";
        });
        button(now, "Unlock \u2014 use the direction", "we-btn-ghost", async () => {
          try {
            await fetch("/api/music", { method: "DELETE" });
            await B.adoptLoop(null);
            B.toast("Next scene uses your direction.");
            load();
          } catch (_) { B.toast("Couldn't unlock that.", "warn"); }
        });
      } else if (direction) {
        statusRow(now, "Score", "your direction, every scene");
        note(now, "Saved. The next scene \u2014 and the next run \u2014 uses this.");
      } else {
        statusRow(now, "Score", "follows each scene");
      }

      write.innerHTML = "";
      const ta = document.createElement("textarea");
      ta.className = "eg-prompt is-short";
      ta.spellcheck = false;
      ta.placeholder = "slow detuned piano, tape hiss, no drums, uneasy";
      ta.value = direction || (loop && loop.source === "generated" && loop.prompt) || "";
      write.appendChild(ta);
      const wacts = document.createElement("div");
      wacts.className = "eg-row eg-acts";
      write.appendChild(wacts);

      let saved = ta.value;
      const saveDirection = async (quiet) => {
        const prompt = ta.value.trim();
        if (prompt === saved) return true;
        try {
          const r = await fetch("/api/music", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ prompt: prompt }),
          });
          const d = await r.json().catch(() => null);
          if (!r.ok) {
            if (!quiet) B.toast((d && d.message) || "Couldn't save that.", "warn");
            return false;
          }
          saved = prompt;
          if (!quiet) B.toast(prompt ? "Saved \u2014 next scene uses it." : "Cleared.");
          return true;
        } catch (_) {
          if (!quiet) B.toast("Couldn't save that.", "warn");
          return false;
        }
      };
      ta.addEventListener("blur", () => { saveDirection(true); });
      ta._commit = saveDirection;

      const hear = button(wacts, "Play loop", "we-btn-primary", async () => {
        wakeAudio();
        if (loop && loop.url) {
          const heard = await playNow(loop.url, now);
          B.toast(heard ? "Playing the loop." : "Couldn't start that loop.", heard ? "" : "warn");
          return;
        }
        const prompt = ta.value.trim();
        if (!prompt) { ta.focus(); return; }
        hear.disabled = true;
        hear.textContent = "Writing\u2026";
        await saveDirection(true);
        B.toast("Writing a sample\u2026");
        try {
          const r = await postMusic("/api/music/preview", { prompt: prompt, seconds: 8 });
          const url = r.data && r.data.preview && r.data.preview.url;
          if (r.ok && url) {
            wakeAudio();
            const heard = await playNow(url, now);
            B.toast(heard ? "Playing the sample." : "Hit play on the preview.", heard ? "" : "warn");
          } else B.toast(r.message || "Couldn't play that.", "warn");
        } catch (_) { B.toast("Couldn't play that.", "warn"); }
        hear.disabled = false;
        hear.textContent = "Play loop";
      });
      const lock = button(wacts, "Lock as the only track", "", async () => {
        const prompt = ta.value.trim();
        if (!prompt) { ta.focus(); return; }
        lock.disabled = true;
        lock.textContent = "Locking\u2026";
        wakeAudio();
        await saveDirection(true);
        try {
          const r = await postMusic("/api/music/generate", { prompt: prompt, seconds: 12 });
          if (r.ok) {
            const made = r.data && r.data.loop;
            wakeAudio();
            await playNow(made && made.url, now);
            B.toast("Locked \u2014 this loop plays until you unlock it.");
            load();
          } else B.toast(r.message || "Couldn't lock that.", "warn");
        } catch (_) { B.toast("Couldn't lock that.", "warn"); }
        lock.disabled = false;
        lock.textContent = "Lock as the only track";
      });
      if (!canGen && !(loop && loop.url)) hear.disabled = true;
      if (!canGen) lock.disabled = true;
      if (!canGen) {
        note(write, whyGen + " Play loop still plays a loop you already have. The direction still saves. Uploading works either way.");
      } else {
        note(write, "Leave the field and it's saved. Play loop plays what you have, or writes a sample. Lock replaces every scene with one loop.");
      }
      statusRow(now, "ElevenLabs", canGen ? "ready" : whyGen, !canGen);

      bring.innerHTML = "";
      const file = document.createElement("input");
      file.type = "file";
      file.accept = "audio/*";
      file.className = "eg-file";
      file.addEventListener("change", () => {
        const f = file.files && file.files[0];
        if (!f) return;
        if (f.size > (state && state.max_bytes ? state.max_bytes : 12582912)) {
          B.toast("That file is too big.", "warn");
          file.value = "";
          return;
        }
        const reader = new FileReader();
        reader.onload = async () => {
          B.toast("Uploading\u2026");
          wakeAudio();
          try {
            const r = await postMusic("/api/music/upload", {
              audio: reader.result, name: f.name,
            });
            if (r.ok) {
              const made = r.data && r.data.loop;
              await playNow(made && made.url, now);
              B.toast("That's your loop now \u2014 playing.");
              load();
            } else B.toast(r.message || "Couldn't use that file.", "warn");
          } catch (_) { B.toast("Couldn't use that file.", "warn"); }
          file.value = "";
        };
        reader.readAsDataURL(f);
      });
      bring.appendChild(file);

      title.innerHTML = "";
      if (menuLoop) {
        statusRow(title, "Menu", menuLoop.source === "upload" ? "your upload" : "locked title track");
        if (menuLoop.name) statusRow(title, "File", menuLoop.name);
        if (menuLoop.prompt) statusRow(title, "Prompt", menuLoop.prompt);
      } else if (menuDirection) {
        statusRow(title, "Menu", "your direction");
      } else {
        statusRow(title, "Menu", "silent until you set one");
      }
      note(title, "This is the start screen, not the match. Leave it empty and the menu stays quiet instead of looping the last fight.");
      const mta = document.createElement("textarea");
      mta.className = "eg-prompt is-short";
      mta.spellcheck = false;
      mta.placeholder = "warm analog title theme, slow pulse, no vocals";
      mta.value = menuDirection || (menuLoop && menuLoop.source === "generated" && menuLoop.prompt) || "";
      title.appendChild(mta);
      const macts = document.createElement("div");
      macts.className = "eg-row eg-acts";
      title.appendChild(macts);
      if (menuLoop && menuLoop.url) showClip(title, menuLoop.url);

      let menuSaved = mta.value;
      const saveMenu = async (quiet) => {
        const prompt = mta.value.trim();
        if (prompt === menuSaved) return true;
        try {
          const r = await fetch("/api/music", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ menu_prompt: prompt }),
          });
          const d = await r.json().catch(() => null);
          if (!r.ok) {
            if (!quiet) B.toast((d && d.message) || "Couldn't save that.", "warn");
            return false;
          }
          menuSaved = prompt;
          if (!quiet) B.toast(prompt ? "Title screen saved." : "Title screen cleared.");
          return true;
        } catch (_) {
          if (!quiet) B.toast("Couldn't save that.", "warn");
          return false;
        }
      };
      mta.addEventListener("blur", () => { saveMenu(true); });

      const playMenu = button(macts, "Play menu", "we-btn-primary", async () => {
        wakeAudio();
        if (menuLoop && menuLoop.url) {
          const heard = await playNow(menuLoop.url, title);
          B.toast(heard ? "Playing the title track." : "Couldn't start that loop.", heard ? "" : "warn");
          return;
        }
        const prompt = mta.value.trim();
        if (!prompt) { mta.focus(); return; }
        playMenu.disabled = true;
        playMenu.textContent = "Writing\u2026";
        await saveMenu(true);
        try {
          const r = await postMusic("/api/music/preview", {
            prompt: prompt, seconds: 10, for: "menu",
          });
          const url = r.data && r.data.preview && r.data.preview.url;
          if (r.ok && url) {
            wakeAudio();
            const heard = await playNow(url, title);
            B.toast(heard ? "Playing the title sample." : "Hit play on the preview.", heard ? "" : "warn");
          } else B.toast(r.message || "Couldn't play that.", "warn");
        } catch (_) { B.toast("Couldn't play that.", "warn"); }
        playMenu.disabled = false;
        playMenu.textContent = "Play menu";
      });
      const lockMenu = button(macts, "Lock as menu track", "", async () => {
        const prompt = mta.value.trim();
        if (!prompt) { mta.focus(); return; }
        lockMenu.disabled = true;
        lockMenu.textContent = "Locking\u2026";
        wakeAudio();
        await saveMenu(true);
        try {
          const r = await postMusic("/api/music/generate", {
            prompt: prompt, seconds: 12, for: "menu",
          });
          const made = r.data && (r.data.menu_loop || r.data.loop);
          if (r.ok && made) {
            wakeAudio();
            await playNow(made.url, title);
            B.toast("Locked \u2014 the start screen uses this.");
            load();
          } else B.toast(r.message || "Couldn't lock that.", "warn");
        } catch (_) { B.toast("Couldn't lock that.", "warn"); }
        lockMenu.disabled = false;
        lockMenu.textContent = "Lock as menu track";
      });
      if (menuLoop) {
        button(macts, "Unlock menu", "we-btn-ghost", async () => {
          try {
            await fetch("/api/music?for=menu", { method: "DELETE" });
            B.toast("Title screen unlocked.");
            load();
          } catch (_) { B.toast("Couldn't unlock that.", "warn"); }
        });
      }
      if (!canGen && !(menuLoop && menuLoop.url)) playMenu.disabled = true;
      if (!canGen) lockMenu.disabled = true;

      const mfile = document.createElement("input");
      mfile.type = "file";
      mfile.accept = "audio/*";
      mfile.className = "eg-file";
      mfile.addEventListener("change", () => {
        const f = mfile.files && mfile.files[0];
        if (!f) return;
        if (f.size > (state && state.max_bytes ? state.max_bytes : 12582912)) {
          B.toast("That file is too big.", "warn");
          mfile.value = "";
          return;
        }
        const reader = new FileReader();
        reader.onload = async () => {
          B.toast("Uploading title track\u2026");
          wakeAudio();
          try {
            const r = await postMusic("/api/music/upload", {
              audio: reader.result, name: f.name, for: "menu",
            });
            const made = r.data && (r.data.menu_loop || r.data.loop);
            if (r.ok && made) {
              await playNow(made.url, title);
              B.toast("That's the title track now.");
              load();
            } else B.toast(r.message || "Couldn't use that file.", "warn");
          } catch (_) { B.toast("Couldn't use that file.", "warn"); }
          mfile.value = "";
        };
        reader.readAsDataURL(f);
      });
      title.appendChild(mfile);

      ambience.innerHTML = "";
      note(ambience, "World Foley under the score. Empty means we pick rain, cave, room tone, and so on from the scene.");
      const sta = document.createElement("textarea");
      sta.className = "eg-prompt is-short";
      sta.spellcheck = false;
      sta.placeholder = "wet steam pipes, distant metal, no music";
      sta.value = (state && state.sfx_direction) || "";
      ambience.appendChild(sta);
      let sfxSaved = sta.value;
      const saveSfx = async (quiet) => {
        const prompt = sta.value.trim();
        if (prompt === sfxSaved) return true;
        try {
          const r = await fetch("/api/music", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ sfx_prompt: prompt }),
          });
          const d = await r.json().catch(() => null);
          if (!r.ok) {
            if (!quiet) B.toast((d && d.message) || "Couldn't save that.", "warn");
            return false;
          }
          sfxSaved = prompt;
          if (!quiet) B.toast(prompt ? "Ambience saved." : "Ambience cleared.");
          return true;
        } catch (_) {
          if (!quiet) B.toast("Couldn't save that.", "warn");
          return false;
        }
      };
      sta.addEventListener("blur", () => { saveSfx(true); });
      const sacts = document.createElement("div");
      sacts.className = "eg-row eg-acts";
      ambience.appendChild(sacts);
      const hearSfx = button(sacts, "Play ambience", "we-btn-primary", async () => {
        await saveSfx(true);
        const scene = (testScene && testScene.value.trim()) || "an unknown place";
        hearSfx.disabled = true;
        hearSfx.textContent = "Writing\u2026";
        try {
          const r = await postMusic("/api/music/test", {
            prompt: scene, mode: testMode, layer: "sfx", seconds: 8,
          });
          const url = r.data && r.data.url;
          if (r.ok && url) {
            const heard = await playClip(url, ambience, true);
            B.toast(heard ? "Playing ambience." : "Hit play on the preview.", heard ? "" : "warn");
          } else B.toast(r.message || "Couldn't play that.", "warn");
        } catch (_) { B.toast("Couldn't play that.", "warn"); }
        hearSfx.disabled = false;
        hearSfx.textContent = "Play ambience";
      });
      if (!canGen) hearSfx.disabled = true;

      test.innerHTML = "";
      note(test, "See the exact prompts a scene would send, then generate a sample without locking a loop.");
      const testScene = document.createElement("textarea");
      testScene.className = "eg-prompt is-short";
      testScene.spellcheck = false;
      testScene.placeholder = "a collapsing grate in a flooded tunnel";
      testScene.value = "";
      test.appendChild(testScene);
      let testMode = "scene";
      picker(test, [
        { id: "scene", label: "Scene" },
        { id: "conversation", label: "Conversation" },
        { id: "encounter", label: "Encounter" },
      ], testMode, (id) => {
        testMode = id;
        refreshInspect();
      });
      const musicPrompt = document.createElement("textarea");
      musicPrompt.className = "eg-prompt is-short";
      musicPrompt.readOnly = true;
      musicPrompt.placeholder = "Music prompt appears here.";
      const sfxPrompt = document.createElement("textarea");
      sfxPrompt.className = "eg-prompt is-short";
      sfxPrompt.readOnly = true;
      sfxPrompt.placeholder = "Ambience prompt appears here.";
      test.appendChild(musicPrompt);
      test.appendChild(sfxPrompt);
      const inspectMeta = document.createElement("p");
      inspectMeta.className = "eg-note";
      test.appendChild(inspectMeta);
      const refreshInspect = async () => {
        const prompt = testScene.value.trim() || testScene.placeholder;
        try {
          const r = await postMusic("/api/music/inspect", { prompt: prompt, mode: testMode });
          const d = (r && r.data) || {};
          musicPrompt.value = d.music_prompt || "";
          sfxPrompt.value = d.sfx_prompt || "";
          const bits = [];
          if (d.ambience_kind) bits.push("ambience: " + d.ambience_kind);
          if (d.stinger_id) bits.push("stinger: " + d.stinger_id);
          inspectMeta.textContent = bits.join(" · ");
        } catch (_) {}
      };
      testScene.addEventListener("blur", refreshInspect);
      refreshInspect();
      const tacts = document.createElement("div");
      tacts.className = "eg-row eg-acts";
      test.appendChild(tacts);
      const runTest = (layer, label) => {
        const btn = button(tacts, label, layer === "music" ? "we-btn-primary" : "", async () => {
          const prompt = testScene.value.trim() || testScene.placeholder;
          btn.disabled = true;
          btn.textContent = "Writing\u2026";
          try {
            const r = await postMusic("/api/music/test", {
              prompt: prompt, mode: testMode, layer: layer, seconds: 8,
            });
            const url = r.data && r.data.url;
            if (r.ok && url) {
              const heard = await playClip(url, test, layer !== "stinger");
              B.toast(heard ? ("Playing " + layer + ".") : "Hit play on the preview.", heard ? "" : "warn");
            } else B.toast(r.message || "Couldn't play that.", "warn");
          } catch (_) { B.toast("Couldn't play that.", "warn"); }
          btn.disabled = false;
          btn.textContent = label;
        });
        if (!canGen) btn.disabled = true;
        return btn;
      };
      runTest("music", "Play music");
      runTest("sfx", "Play ambience");
      runTest("stinger", "Play stinger");

      stockBox.innerHTML = "";
      const files = (state && state.stock) || {};
      const readyN = Object.keys(files).filter((k) => files[k] && files[k].ready).length;
      const totalN = Object.keys(files).length;
      statusRow(stockBox, "Ready", readyN + " / " + totalN);
      note(stockBox, "Encounter hits and fallback rooms. Generate missing ones here; Play hears what's on disk.");
      const stockActs = document.createElement("div");
      stockActs.className = "eg-row eg-acts";
      stockBox.appendChild(stockActs);
      const genMissing = button(stockActs, "Generate missing", "we-btn-primary", async () => {
        genMissing.disabled = true;
        genMissing.textContent = "Writing\u2026";
        B.toast("Generating missing stock\u2026");
        try {
          const r = await postMusic("/api/music/stock", {});
          if (r.ok) {
            B.toast("Stock updated.");
            load();
          } else B.toast(r.message || "Couldn't generate stock.", "warn");
        } catch (_) { B.toast("Couldn't generate stock.", "warn"); }
        genMissing.disabled = false;
        genMissing.textContent = "Generate missing";
      });
      if (!canGen) genMissing.disabled = true;
      Object.keys(files).forEach((id) => {
        const rec = files[id] || {};
        const row = document.createElement("div");
        row.className = "eg-stock-row";
        const name = document.createElement("div");
        name.className = "eg-stock-name";
        name.textContent = (rec.ready ? "ready · " : "missing · ") + id.replace(/_/g, " ");
        row.appendChild(name);
        const play = button(row, "Play", "", async () => {
          if (rec.url) {
            await playClip(rec.url, stockBox, !!rec.loop);
            return;
          }
          play.disabled = true;
          try {
            const r = await postMusic("/api/music/stock", { id: id });
            const url = r.data && r.data.url;
            if (r.ok && url) await playClip(url, stockBox, !!rec.loop);
            else B.toast(r.message || "Not generated yet.", "warn");
            load();
          } catch (_) { B.toast("Couldn't play that.", "warn"); }
          play.disabled = false;
        });
        const regen = button(row, rec.ready ? "Regen" : "Generate", "we-btn-ghost", async () => {
          regen.disabled = true;
          regen.textContent = "Writing\u2026";
          try {
            const r = await postMusic("/api/music/stock", { id: id, force: !!rec.ready });
            if (r.ok && r.data && r.data.url) {
              await playClip(r.data.url, stockBox, !!rec.loop);
              B.toast(id.replace(/_/g, " ") + " ready.");
              load();
            } else B.toast(r.message || "Couldn't generate that.", "warn");
          } catch (_) { B.toast("Couldn't generate that.", "warn"); }
          regen.disabled = false;
          regen.textContent = rec.ready ? "Regen" : "Generate";
        });
        if (!canGen && !rec.url) play.disabled = true;
        if (!canGen) regen.disabled = true;
        stockBox.appendChild(row);
        if (rec.prompt) {
          const p = document.createElement("p");
          p.className = "eg-stock-prompt";
          p.textContent = rec.prompt;
          stockBox.appendChild(p);
        }
      });

      cacheBox.innerHTML = "";
      const clips = (state && state.cache) || [];
      statusRow(cacheBox, "Clips", String(clips.length));
      note(cacheBox, "Per-scene music and ambience already generated this session. Clearing forces a fresh score on the next turn. Locked loops and stock stay.");
      clips.forEach((clip) => {
        statusRow(cacheBox, clip.kind || "clip", clip.file);
      });
      if (clips.length) {
        button(cacheBox, "Clear scene cache", "we-btn-ghost", async () => {
          try {
            await fetch("/api/music/cache", { method: "DELETE" });
            B.toast("Scene cache cleared.");
            load();
          } catch (_) { B.toast("Couldn't clear that.", "warn"); }
        });
      }
    };

    const load = () => getJson("/api/music").then(draw);
    load();

    // The volume presets are the live HUD's, borrowed in.
    const seg = document.getElementById("rt-music-opts");
    if (seg && seg.children.length) {
      try { B.borrow(seg, group(body, "Level")); } catch (_) {}
    }
  }

  function soundCfg() {
    return (B.sound && B.sound()) || { palette: "tape", muted: [], volume: 1 };
  }

  function saveSound(patch, quiet) {
    if (!B.saveSound) return Promise.resolve(false);
    return Promise.resolve(B.saveSound(patch)).then((ok) => {
      if (!quiet) B.toast(ok ? "Saved." : "Couldn't save that.", ok ? "" : "warn");
      return ok;
    }).catch(() => {
      if (!quiet) B.toast("Couldn't save that.", "warn");
      return false;
    });
  }

  function sheetSoundPalette(n, body) {
    const cfg = soundCfg();
    const style = group(body, "Palette");
    note(style, "Tape is the shipped voice: dry clicks, tape hiss, no chimes. Quiet keeps the body and the lens. Silent leaves the score and the heartbeat.");
    picker(style, [
      { id: "tape", label: "Tape", tag: "default" },
      { id: "quiet", label: "Quiet" },
      { id: "silent", label: "Silent" },
    ], cfg.palette || "tape", (id) => {
      saveSound({ palette: id }).then(() => {
        try { window.Sound && window.Sound.preview && window.Sound.preview("select"); } catch (_) {}
        if (sheetId === n.id) openSheet(n);
      });
    });

    const level = group(body, "Level");
    numberRow(level, "Cues", { min: 0, max: 1, step: 0.05 },
      cfg.volume == null ? 1 : cfg.volume,
      "How loud the machine speaks. Music has its own level.",
      (v) => { saveSound({ volume: Number(v) }, true); });

    const hear = group(body, "Hear it");
    button(hear, "Play a click", "we-btn-primary", () => {
      try { window.Sound && window.Sound.resume && window.Sound.resume(); } catch (_) {}
      try { window.Sound && window.Sound.preview && window.Sound.preview("press"); } catch (_) {}
    });
    button(hear, "Play a scene jump", "", () => {
      try { window.Sound && window.Sound.resume && window.Sound.resume(); } catch (_) {}
      try { window.Sound && window.Sound.preview && window.Sound.preview("glitch"); } catch (_) {}
    });
  }

  function sheetSoundFamily(n, body) {
    const cfg = soundCfg();
    const fam = n.family;
    const mutedNow = (cfg.muted || []).indexOf(fam) >= 0;
    const gate = group(body, "This family");
    switchRow(gate, "Mute these", mutedNow,
      "They stay silent until you turn them back on.", (on) => {
        const next = (cfg.muted || []).filter((x) => x !== fam);
        if (on) next.push(fam);
        saveSound({ muted: next }).then(() => {
          if (sheetId === n.id) openSheet(n);
        });
      });

    const found = (window.Sound && window.Sound.families)
      ? window.Sound.families().find((f) => f.id === fam)
      : null;
    const cues = (found && found.cues) || [];
    const list = group(body, "Hear each one");
    if (!cues.length) {
      note(list, "The synth isn't loaded.");
      return;
    }
    cues.forEach((cue) => {
      const row = document.createElement("div");
      row.className = "eg-cue";
      const lab = document.createElement("span");
      lab.className = "eg-cue-k";
      lab.textContent = (window.Sound.cueHelp && window.Sound.cueHelp(cue)) || cue;
      const b = document.createElement("button");
      b.type = "button";
      b.className = "we-btn we-btn-ghost eg-cue-play";
      b.textContent = "Play";
      b.addEventListener("click", () => {
        try { window.Sound.resume && window.Sound.resume(); } catch (_) {}
        try { window.Sound.preview(cue); } catch (_) {}
      });
      row.appendChild(lab);
      row.appendChild(b);
      list.appendChild(row);
    });
  }

  // ── Models ────────────────────────────────────────────────────────────
  function liveWorldModelId(cfg) {
    try {
      if (window.ReactorRenderer && typeof window.ReactorRenderer.getModel === "function") {
        const id = window.ReactorRenderer.getModel();
        if (id) return id;
      }
    } catch (_) {}
    return (cfg && cfg.world_model) || null;
  }

  function fillPicturePicker(host, n) {
    host.innerHTML = "";
    Promise.all([
      getJson("/api/reactor/config"),
      getJson("/api/keys"),
    ]).then((pair) => {
      const cfg = pair[0] || {};
      const keys = pair[1] || {};
      const row = ((keys.providers) || []).find((p) => p.id === "reactor");
      const keySet = !!(row && row.set);
      const enabled = !!(cfg.enabled || keySet);
      let status = "";
      let showing = false;
      try { status = (window.ReactorRenderer && window.ReactorRenderer.getStatus()) || ""; } catch (_) {}
      try {
        showing = !!(window.ReactorRenderer && window.ReactorRenderer.isShowing
          && window.ReactorRenderer.isShowing());
      } catch (_) {}
      const tag = !enabled
        ? "needs a Reactor key"
        : showing ? "live"
        : status === "connecting" ? "connecting\u2026"
        : "stills until the world model connects";
      picker(host, [{ id: "reactor", label: "Live video", tag: tag }], "reactor", () => {
        try {
          if (B && B.prepareLiveScene) B.prepareLiveScene({ hard: true });
          if (window.Renderer && typeof window.Renderer.upgradeToLive === "function") {
            window.Renderer.upgradeToLive({ reason: "editor", hard: true });
          } else if (window.Renderer && typeof window.Renderer.setMode === "function") {
            window.Renderer.setMode("reactor");
          }
          B.toast("Live video \u2014 connecting\u2026");
        } catch (_) {
          B.toast("Couldn't start the world model.", "warn");
        }
        setTimeout(() => { if (sheetId === n.id) openSheet(n); }, 400);
      });
      if (!enabled) {
        note(host, "Live video needs a Reactor key (ACCOUNT on the start screen).");
        return;
      }
      if (status === "connecting") {
        note(host, "Connecting to the live world model\u2026");
      } else if (showing) {
        note(host, "World model is live. Stills stay underneath as a floor.");
      } else {
        note(host, "Showing stills until the world model connects. Click Live video to retry.");
      }
      try {
        if (window.Renderer && typeof window.Renderer.upgradeToLive === "function") {
          window.Renderer.upgradeToLive({ reason: "editor" });
        }
      } catch (_) {}
    });
  }

  function fillWorldModelPicker(host, n, opts) {
    opts = opts || {};
    getJson("/api/reactor/config").then((cfg) => {
      const models = (cfg && cfg.available_models) || [];
      if (!models.length) {
        if (opts.hideIfEmpty) {
          const wrap = host.closest(".eg-group");
          if (wrap) wrap.remove();
          return;
        }
        host.innerHTML = "";
        note(host, "No world models advertised by the server.");
        return;
      }
      host.innerHTML = "";
      const live = liveWorldModelId(cfg);
      picker(host, models.map((m) => ({
        id: m.id || m,
        label: m.label || m.name || m.id || m,
        tag: m.note || "",
      })), live, (id) => {
        try {
          if (window.Renderer && typeof window.Renderer.setWorldModel === "function") {
            window.Renderer.setWorldModel(id);
          } else {
            window.ReactorRenderer.setModel(id);
            if (window.Renderer && typeof window.Renderer.upgradeToLive === "function") {
              window.Renderer.upgradeToLive({ reason: "world-model" });
            }
          }
          B.toast("Switching world model\u2026");
        } catch (_) { B.toast("Couldn't switch the world model.", "warn"); }
        setTimeout(() => { if (sheetId === n.id) openSheet(n); }, 900);
      });
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
            if (window.Renderer && typeof window.Renderer.setWorldModel === "function") {
              window.Renderer.setWorldModel(raw);
            } else {
              window.ReactorRenderer.setModel(raw);
            }
            B.toast("Trying " + raw + "\u2026");
          } catch (_) { B.toast("Couldn't use that model.", "warn"); }
          setTimeout(() => { if (sheetId === n.id) openSheet(n); }, 900);
        });
        host.appendChild(form);
      }
    });
  }

  function fillImageModelPicker(host, n, opts) {
    opts = opts || {};
    getJson("/api/ai/config").then((cfg) => {
      const presets = (cfg && cfg.presets) || [];
      const active = cfg && cfg.active_preset;
      if (!presets.length) {
        if (opts.hideIfEmpty) {
          const wrap = host.closest(".eg-group");
          if (wrap) wrap.remove();
          return;
        }
        host.innerHTML = "";
        note(host, "No image presets configured.");
        return;
      }
      host.innerHTML = "";
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

  function sheetWorld(n, body) {
    fillWorldModelPicker(group(body, "World model"), n);
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

  function sheetText(n, body) {
    const host = group(body, "Narrator");
    getJson("/api/ai/config").then((cfg) => {
      host.innerHTML = "";
      const models = (cfg && cfg.text_models) || [];
      const current = cfg && cfg.current && cfg.current.text_model;
      if (!models.length) { note(host, "No text models configured."); return; }
      const ready = models.filter((m) => m.available !== false);
      const locked = models.filter((m) => m.available === false);
      if (!ready.length) {
        note(host, "Add an Anthropic or OpenAI key under KEYS to use Claude or GPT.");
      } else {
        picker(host, ready.map((m) => ({
          id: m.id,
          label: m.label || m.id,
          tag: m.note || m.provider || "",
        })), current, async (id) => {
          try {
            const r = await fetch("/api/ai/models", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ text_model: id }),
            });
            const data = await r.json().catch(() => ({}));
            B.toast(r.ok && data.status === "ok"
              ? "Narrator switched."
              : (data.error || "Couldn't switch that."),
              r.ok && data.status === "ok" ? "" : "warn");
          } catch (_) { B.toast("Couldn't switch that.", "warn"); }
          if (sheetId === n.id) openSheet(n);
        });
      }
      if (locked.length) {
        note(host, "Needs a key (ACCOUNT on the start screen): " +
          locked.map((m) => m.label || m.id).join(", ") + ".");
      }
    });
  }

  function sheetImage(n, body) {
    fillImageModelPicker(group(body, "Image model"), n);

    // Flipbook: draw the turn as a short sequence of in-between frames instead
    // of a single still. It lives next to the model picker because it is the
    // same decision — what one turn's generation buys you — and because the
    // frame count trades against the image size right above it: the frames are
    // slices of ONE generation, so more of them means each one is smaller.
    const motion = group(body, "Motion");
    pending(motion);
    loadTunables(true).then((t) => {
      motion.innerHTML = "";
      const spec = (t && t.schema) || {};
      const vals = (t && t.values) || {};
      if (!spec.flipbook_enabled) { note(motion, "Not available."); return; }
      switchRow(motion, spec.flipbook_enabled.label, vals.flipbook_enabled,
        spec.flipbook_enabled.help, (v) => setTunable("flipbook_enabled", v));
      selectRow(motion, spec.flipbook_frames.label,
        (spec.flipbook_frames.options || []).map((o) => ({ id: o, label: o + " frames" })),
        vals.flipbook_frames, spec.flipbook_frames.help,
        (v) => setTunable("flipbook_frames", v));
      numberRow(motion, spec.flipbook_frame_ms.label, spec.flipbook_frame_ms,
        vals.flipbook_frame_ms, spec.flipbook_frame_ms.help,
        (v) => setTunable("flipbook_frame_ms", v));
    });

    // The run's look book (look_book.py): the contact sheet every frame is drawn
    // against, and the designed plate for everything the run can roll. It lives
    // on the Image node because it is the same decision as the model above —
    // what one frame is made from — and because the switches are how a part of
    // it gets cut if it stops earning its place.
    fillLookBook(group(body, "Look book"), n);
  }

  // ── LOOK BOOK VIEW ────────────────────────────────────────────────────
  // The whole pipeline laid out in the order it runs, so the book can be
  // judged stage by stage and any stage redone on its own: the roster the
  // dice roll from, the brief the production designer wrote, the world sheet
  // and its nine panels, the casting sheet beside the crop check that decided
  // which frame is whose, and every plate with its designed look, its match
  // terms (struck through when unsafe) and where it came from. Everything here
  // is read from GET /api/look_book and redone through POST
  // /api/look_book/generate; the page polls while a stage is being shot.
  let lbvTimer = null;
  // The run this page is bound to (standalone.js sets it from ?session=).
  const lbSession = () => window.__SOMEWHERE_SESSION__ || "default";
  const lbUrl = () => "/api/look_book?session=" + encodeURIComponent(lbSession());
  function openLookBookView(startStage) {
    let root = document.getElementById("look-book-view");
    if (!root) {
      root = document.createElement("div");
      root.id = "look-book-view";
      root.setAttribute("role", "dialog");
      root.setAttribute("aria-modal", "true");
      root.setAttribute("aria-label", "Look book");
      document.body.appendChild(root);
    }
    root.classList.add("is-open");
    let stage = startStage || "plates";
    let last = null;
    let flashMsg = "";
    const close = () => {
      clearTimeout(lbvTimer);
      root.classList.remove("is-open");
      root.innerHTML = "";
      document.removeEventListener("keydown", onKey, true);
    };
    const onKey = (e) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      const box = root.querySelector(".lbv-lightbox");
      if (box) box.remove(); else close();
    };
    document.addEventListener("keydown", onKey, true);

    const el = (tag, cls, text) => {
      const n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      return n;
    };
    // Pictures open in place, over the desk, not in a new tab.
    const lightbox = (src, caption) => {
      const box = el("div", "lbv-lightbox");
      const im = el("img");
      im.src = src; im.alt = caption || "";
      box.appendChild(im);
      if (caption) box.appendChild(el("div", "lbv-lightbox-cap", caption));
      box.addEventListener("click", () => box.remove());
      root.appendChild(box);
    };
    const pic = (src, alt, cls) => {
      const b = el("button", "lbv-pic " + (cls || ""));
      b.type = "button";
      b.setAttribute("aria-label", "Open " + (alt || "picture"));
      const im = el("img");
      im.src = src; im.alt = alt || ""; im.loading = "lazy";
      b.appendChild(im);
      b.addEventListener("click", () => lightbox(src, alt));
      return b;
    };
    const gen = async (part, plate) => {
      try {
        const r = await fetch("/api/look_book/generate", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(plate == null ? { part, session: lbSession() }
                                             : { part, plate, session: lbSession() }),
        });
        if (!r.ok) {
          const b = await r.json().catch(() => ({}));
          flashMsg = b.message || "Could not start that.";
        }
      } catch (_) { flashMsg = "Could not reach the server."; }
      try { if (window.LookBookStatus) window.LookBookStatus.watch(); } catch (_) {}
      refresh();
    };
    const textBtn = (host, label, onClick, disabled) => {
      const b = el("button", "lbv-link", label);
      b.type = "button";
      b.disabled = !!disabled;
      b.addEventListener("click", onClick);
      host.appendChild(b);
      return b;
    };

    // The same five steps the in-game strip counts.
    const STEPS = [
      { keys: ["start", "roster"], label: "rolling the roster" },
      { keys: ["brief"], label: "writing the brief" },
      { keys: ["shoot", "world", "roster_sheet"], label: "shooting the sheets" },
      { keys: ["placement"], label: "checking the crops" },
      { keys: ["plates"], label: "cutting the plates" },
    ];
    const stepOf = (log) => {
      let idx = 0;
      (log || []).forEach((l) => STEPS.forEach((s, i) => { if (s.keys.includes(l.stage)) idx = Math.max(idx, i); }));
      return idx;
    };
    const RAIL = [
      ["roster", "01", "ROSTER"], ["brief", "02", "BRIEF"], ["world", "03", "WORLD"],
      ["casting", "04", "CASTING"], ["plates", "05", "PLATES"], ["log", "06", "LOG"],
    ];
    const liveStage = (lb, busy) => {
      if (!busy) return "";
      const i = stepOf(lb.log);
      return ["roster", "brief", "world", "casting", "plates"][Math.min(i === 3 ? 3 : i === 4 ? 4 : i, 4)];
    };

    const stageHead = (host, title, note, action) => {
      const h = el("div", "lbv-stage-head");
      const t = el("div", "lbv-stage-title");
      t.appendChild(el("h3", "", title));
      if (note) t.appendChild(el("span", "lbv-quiet", note));
      h.appendChild(t);
      if (action) h.appendChild(action);
      host.appendChild(h);
    };

    const draw = (lb) => {
      last = lb;
      const main0 = root.querySelector(".lbv-main");
      const scroll = main0 ? main0.scrollTop : 0;
      root.innerHTML = "";
      const busy = !!(lb && (lb.building || ["roster", "brief", "shooting"].includes(lb.status)));
      const live = lb ? liveStage(lb, busy) : "";

      const page = el("div", "lbv-page");
      root.appendChild(page);

      // Header — the menu's wordmark treatment, actions as tracked text.
      const head = el("header", "lbv-header");
      const brand = el("div", "lbv-brand");
      brand.appendChild(el("h2", "lbv-wordmark", "LOOK BOOK"));
      const bits = [];
      if (lb && lb.world_name) bits.push(String(lb.world_name).toUpperCase());
      if (lb && lb.roster && lb.roster.length) bits.push(`${lb.roster.length} ROSTER ENTRIES`);
      if (lb && lb.reason) bits.push(String(lb.reason).toUpperCase());
      if (lb && lb.stale) bits.push("SHOT FOR ANOTHER WORLD");
      brand.appendChild(el("div", "lbv-meta", bits.join(" · ")));
      head.appendChild(brand);
      const nav = el("nav", "lbv-nav");
      nav.setAttribute("aria-label", "Look book actions");
      textBtn(nav, "NEW BOOK", () => gen("all"), busy || !(lb && lb.enabled));
      textBtn(nav, "CLOSE", close);
      head.appendChild(nav);
      page.appendChild(head);

      const desk = el("div", "lbv-desk");
      page.appendChild(desk);

      // Stage rail
      const rail = el("nav", "lbv-rail");
      rail.setAttribute("aria-label", "Stages");
      const counts = {
        roster: lb && lb.roster ? String((lb.roster.length || (lb.roster_only || []).length) || "") : "",
        brief: lb && lb.frames && lb.frames.length ? `${lb.frames.length} FRAMES` : "",
        world: lb && lb.world_sheet ? "3 × 3" : "",
        casting: lb && lb.roster ? `${lb.roster.filter((r) => r.plate).length} / ${lb.roster.length}` : "",
        plates: lb && lb.roster ? String(lb.roster.filter((r) => r.plate).length || "") : "",
        log: lb && lb.log ? String(lb.log.length || "") : "",
      };
      RAIL.forEach(([key, n, name]) => {
        const b = el("button", "lbv-rail-item" + (key === stage ? " is-on" : "") + (key === live ? " is-live" : ""));
        b.type = "button";
        b.setAttribute("aria-current", key === stage ? "page" : "false");
        b.appendChild(el("span", "lbv-rail-dot"));
        b.appendChild(el("span", "lbv-rail-n", n));
        b.appendChild(el("span", "lbv-rail-name", name));
        b.appendChild(el("span", "lbv-rail-count", counts[key] || ""));
        b.addEventListener("click", () => { stage = key; draw(last); });
        rail.appendChild(b);
      });
      desk.appendChild(rail);

      const main = el("main", "lbv-main");
      desk.appendChild(main);
      if (flashMsg) { main.appendChild(el("p", "lbv-flash", flashMsg)); flashMsg = ""; }
      if (!lb) { main.appendChild(el("p", "lbv-quiet", "Could not read the look book.")); }
      else if (!lb.enabled) main.appendChild(el("p", "lbv-quiet", "The look book is off, or there is no image key — frames are drawn without it."));
      if (lb && lb.error) main.appendChild(el("p", "lbv-flash", lb.error));

      const act = (label, part, disabled) => {
        const b = el("button", "lbv-link lbv-underline", label);
        b.type = "button";
        b.disabled = !!disabled;
        b.addEventListener("click", () => { b.disabled = true; b.textContent = "STARTING…"; gen(part); });
        return b;
      };

      if (lb && stage === "roster") {
        stageHead(main, "ROSTER", "What this run can roll. Built at reset, so the fights and the book agree on who exists.",
          act("NEW BOOK", "all", busy));
        const kinds = (lb.roster && lb.roster.length) ? lb.roster.map((r) => r.kind) : (lb.roster_only || []);
        const ol = el("ol", "lbv-roster");
        kinds.forEach((k, i) => {
          const li = el("li");
          li.appendChild(el("span", "lbv-quiet", String(i + 1).padStart(2, "0")));
          li.appendChild(el("span", "", k));
          ol.appendChild(li);
        });
        if (!kinds.length) main.appendChild(el("p", "lbv-quiet", busy ? "Rolling…" : "No roster yet."));
        main.appendChild(ol);
      }

      if (lb && stage === "brief") {
        stageHead(main, "BRIEF", "The production designer's decisions, before a single frame is shot.",
          act("REWRITE BRIEF", "brief", busy || !(lb.roster || []).length));
        const rules = lb.look_rules || {};
        const palRaw = rules.palette || {};
        const pal = Array.isArray(palRaw)
          ? palRaw.map((c) => (c && typeof c === "object")
              ? [c.name || c.label || "", c.hex || c.color || c.value || ""] : [String(c), String(c)])
          : Object.entries(palRaw);
        if (pal.length) {
          const row = el("div", "lbv-palette");
          pal.forEach(([name, hex]) => {
            const sw = el("div", "lbv-swatch");
            const chip = el("div", "lbv-chip");
            chip.style.background = String(hex);
            sw.appendChild(chip);
            const cap = el("div", "lbv-swatch-cap");
            cap.appendChild(el("span", "lbv-quiet", String(name).toUpperCase()));
            cap.appendChild(el("span", "", String(hex)));
            sw.appendChild(cap);
            row.appendChild(sw);
          });
          main.appendChild(row);
        }
        const facts = el("dl", "lbv-facts");
        [["FILM", rules.film], ["LENS", rules.lens], ["MOTIFS", (rules.motifs || []).join("  ·  ")]].forEach(([k, v]) => {
          if (!v) return;
          facts.appendChild(el("dt", "", k));
          facts.appendChild(el("dd", "", v));
        });
        main.appendChild(facts);
        const grid = el("div", "lbv-frame-grid");
        (lb.frames || []).forEach((f) => {
          const c = el("article", "lbv-frame");
          c.appendChild(el("div", "lbv-micro", `${String(f.n || "").padStart(2, "0")} · ${f.row || ""}`));
          c.appendChild(el("div", "lbv-frame-title", f.title || ""));
          c.appendChild(el("div", "lbv-quiet", f.subject || ""));
          const ds = Array.isArray(f.design_specifics) ? f.design_specifics : [f.design_specifics].filter(Boolean);
          if (ds.length) {
            const ul = el("ul", "lbv-specs");
            ds.forEach((d) => ul.appendChild(el("li", "", d)));
            c.appendChild(ul);
          }
          grid.appendChild(c);
        });
        main.appendChild(grid);
      }

      if (lb && stage === "world") {
        stageHead(main, "WORLD", "Rides with every frame, last, as design — never as layout.",
          act("RESHOOT SHEET", "world", busy || !(lb.frames || []).length));
        const row = el("div", "lbv-world");
        if (lb.world_sheet) row.appendChild(pic(lb.world_sheet, "World contact sheet", "lbv-world-sheet"));
        else row.appendChild(el("div", "lbv-empty", busy ? "SHOOTING…" : "NO WORLD SHEET"));
        const side = el("div", "lbv-world-side");
        const caps = el("div", "lbv-caps");
        (lb.frames || []).forEach((f) => {
          const c = el("button", "lbv-cap");
          c.type = "button";
          c.appendChild(el("span", "lbv-micro", `${String(f.n || "").padStart(2, "0")} · ${f.row || ""}`));
          c.appendChild(el("span", "", f.title || ""));
          if (f.panel) c.addEventListener("click", () => lightbox(f.panel, `${f.title} — ${f.subject || ""}`));
          else c.disabled = true;
          caps.appendChild(c);
        });
        side.appendChild(caps);
        row.appendChild(side);
        main.appendChild(row);
      }

      if (lb && stage === "casting") {
        stageHead(main, "CASTING", "As the model drew it, beside what our cut and the crop check saw.",
          act("RESHOOT CASTING", "roster", busy || !(lb.roster || []).length));
        const pair = el("div", "lbv-pair");
        const fig = (src, alt, cap) => {
          const f = el("figure", "lbv-fig");
          f.appendChild(src ? pic(src, alt) : el("div", "lbv-empty", busy ? "SHOOTING…" : "NOTHING YET"));
          f.appendChild(el("figcaption", "lbv-micro", cap));
          return f;
        };
        pair.appendChild(fig(lb.roster_sheet, "Casting sheet as drawn", "AS DRAWN"));
        pair.appendChild(fig(lb.placement_check, "Crop check", "AS CUT · EACH TILE CHECKED BY EYE"));
        main.appendChild(pair);
      }

      if (lb && stage === "plates") {
        stageHead(main, "PLATES", "What each roster entry looks like when it arrives — in a fight, or any frame that names it.",
          act("RESHOOT CASTING", "roster", busy || !(lb.roster || []).length));
        const grid = el("div", "lbv-plates");
        (lb.roster || []).forEach((r, i) => {
          const f = el("figure", "lbv-plate");
          const frame = el("div", "lbv-plate-frame");
          frame.appendChild(r.plate ? pic(r.plate, r.kind) : el("div", "lbv-empty", "NO PLATE"));
          const re = el("button", "lbv-reshoot", "RESHOOT");
          re.type = "button";
          re.disabled = busy;
          re.addEventListener("click", (e) => { e.stopPropagation(); re.disabled = true; re.textContent = "…"; gen("plate", r.index); });
          frame.appendChild(re);
          f.appendChild(frame);
          const cap = el("figcaption", "");
          cap.appendChild(el("div", "lbv-micro", `${String(i + 1).padStart(2, "0")}  ${(r.plate_source || "").toUpperCase()}`));
          const name = el("div", "lbv-plate-name", r.kind);
          if (r.look) name.title = r.look;
          cap.appendChild(name);
          const terms = el("div", "lbv-terms");
          (r.terms || []).forEach((t) => {
            const ok = (r.usable_terms || []).includes(t);
            const sp = el("span", ok ? "is-ok" : "is-off", t);
            sp.title = ok ? "Names this entry in a frame's text" : "Not acted on: generic, shared, or could describe the player";
            terms.appendChild(sp);
          });
          cap.appendChild(terms);
          f.appendChild(cap);
          grid.appendChild(f);
        });
        if (!(lb.roster || []).length) main.appendChild(el("p", "lbv-quiet", busy ? "Shooting…" : "No plates yet."));
        main.appendChild(grid);
      }

      if (lb && stage === "log") {
        stageHead(main, "LOG", "Every stage of the last build, as it happened.", null);
        const ol = el("ol", "lbv-log");
        (lb.log || []).forEach((l) => {
          const li = el("li");
          li.appendChild(el("span", "lbv-quiet", `${l.t}s`));
          li.appendChild(el("span", "", l.msg));
          ol.appendChild(li);
        });
        if (!(lb.log || []).length) main.appendChild(el("p", "lbv-quiet", "Nothing yet."));
        main.appendChild(ol);
      }
      main.scrollTop = scroll;

      // Footer — the same micro text over five segments the game shows.
      const foot = el("footer", "lbv-footer");
      const idx = lb ? stepOf(lb.log) : 0;
      let line = "";
      if (!lb) line = "LOOK BOOK · UNREACHABLE";
      else if (busy) line = `LOOK BOOK ${idx + 1}/5 · ${STEPS[idx].label.toUpperCase()}`;
      else if (lb.status === "ready" && !lb.stale) line = `LOOK BOOK · READY${lb.timings && lb.timings.total ? " · SHOT IN " + Math.round(lb.timings.total) + "S" : ""}`;
      else line = `LOOK BOOK · ${(lb.stale ? "shot for another world" : lb.status || "none").toUpperCase()}`;
      const txt = el("div", "lbv-foot-text" + (busy ? " is-live" : lb && lb.status === "ready" && !lb.stale ? " is-ok" : ""), line);
      foot.appendChild(txt);
      const bar = el("div", "lbv-foot-bar");
      for (let k = 0; k < 5; k++) {
        const done = lb && (!busy ? (lb.status === "ready" && !lb.stale) : k < idx);
        bar.appendChild(el("i", done ? "done" : (busy && k === idx ? "live" : "")));
      }
      foot.appendChild(bar);
      page.appendChild(foot);

      clearTimeout(lbvTimer);
      if (busy && root.classList.contains("is-open")) lbvTimer = setTimeout(refresh, 2500);
    };
    const refresh = () => {
      if (!root.classList.contains("is-open")) return;
      if (root.querySelector(".lbv-lightbox")) { lbvTimer = setTimeout(refresh, 2500); return; }
      getJson(lbUrl()).then(draw);
    };
    root.innerHTML = '<div class="lbv-page"><p class="lbv-quiet" style="padding:56px">Reading the look book…</p></div>';
    refresh();
  }

  // Reachable without the graph too (a playtest or the console can open it).
  window.LookBookView = { open: openLookBookView };

  function fillLookBook(host, n) {
    const knobs = document.createElement("div");
    host.appendChild(knobs);
    pending(knobs);
    loadTunables(true).then((t) => {
      knobs.innerHTML = "";
      const spec = (t && t.schema) || {};
      const vals = (t && t.values) || {};
      ["look_book", "look_book_sheet_on_turns", "look_book_roster_plates", "look_book_story"].forEach((k) => {
        if (spec[k]) switchRow(knobs, spec[k].label, vals[k], spec[k].help, (v) => setTunable(k, v));
      });
    });

    const shelf = document.createElement("div");
    shelf.className = "eg-lb";
    host.appendChild(shelf);
    const openedFor = sheetId;
    let timer = null;

    const draw = (lb) => {
      shelf.innerHTML = "";
      if (!lb) { note(shelf, "Could not read the look book."); return; }
      const status = lb.stale ? "shot for another World" : (lb.building ? "shooting…" : lb.status);
      statusRow(shelf, "This run", status, lb.status === "failed" || lb.stale);
      if (lb.timings && lb.timings.total) statusRow(shelf, "Shot in", lb.timings.total + " s");
      if (!lb.enabled) note(shelf, "Off, or no image key — frames are drawn without it.");
      if (lb.error) note(shelf, lb.error);
      if (lb.world_sheet) {
        const a = document.createElement("a");
        a.href = lb.world_sheet;
        a.target = "_blank";
        a.rel = "noopener";
        a.className = "eg-lb-sheet";
        const img = document.createElement("img");
        img.src = lb.world_sheet + "?t=" + Date.now();
        img.alt = "World contact sheet: cast, conflicts, sets";
        a.appendChild(img);
        shelf.appendChild(a);
        note(shelf, "Cast · conflicts · sets. Rides with every frame as design, never as layout.");
      }
      const cast = (lb.roster || []).filter((r) => r.plate);
      if (cast.length) {
        const grid = document.createElement("div");
        grid.className = "eg-lb-roster";
        cast.forEach((r) => {
          const fig = document.createElement("figure");
          fig.className = "eg-lb-plate";
          const img = document.createElement("img");
          img.src = r.plate + "?t=" + Date.now();
          img.alt = r.kind;
          img.title = r.look || r.kind;
          fig.appendChild(img);
          const cap = document.createElement("figcaption");
          cap.textContent = r.kind;
          fig.appendChild(cap);
          grid.appendChild(fig);
        });
        shelf.appendChild(grid);
        note(shelf, "Everything this run can roll, as it will be drawn. An encounter, " +
                    "and any turn that names one, is drawn from its plate.");
      }
      const row = document.createElement("div");
      row.className = "eg-lb-actions";
      shelf.appendChild(row);
      button(row, "Open look book", "", () => openLookBookView());
      button(row, lb.building ? "Shooting…" : "Reshoot", "", async (ev) => {
        ev.currentTarget.disabled = true;
        try {
          await fetch("/api/look_book/generate", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ part: "all", session: lbSession() }),
          });
        } catch (_) { /* the poll below reports it */ }
        refresh();
      }).disabled = !!lb.building || !lb.enabled;
      // Keep the shelf live while a book is being shot and this window is open.
      clearTimeout(timer);
      if ((lb.building || ["roster", "brief", "shooting"].includes(lb.status)) && sheetId === openedFor) {
        timer = setTimeout(refresh, 4000);
      }
    };
    const refresh = () => {
      if (sheetId !== openedFor || !shelf.isConnected) return;
      getJson(lbUrl()).then(draw);
    };
    pending(shelf);
    refresh();
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
    });
  }

  function closeSheet() {
    if (!sheetId) return false;
    // Anything typed and not yet committed goes now, before the markup holding
    // it is thrown away. Fire-and-forget: the window should close at the speed
    // of the tap, and a failure surfaces as a toast from the save itself.
    //
    // Loudly (`false`), unlike the blur inside an open window: the panel that
    // would otherwise carry the confirmation is about to be deleted, so the
    // toast is the only place left to say that closing the window kept the
    // edit rather than discarding it.
    try {
      els.sheetBody.querySelectorAll(".eg-prompt").forEach((ta) => {
        if (typeof ta._commit === "function") ta._commit(false);
      });
    } catch (_) {}
    sheetId = null;
    openEdgeId = null;
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
    syncMenuStack();
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
    if (!B.isGraphMode()) { stopWobble(); closeSheet(); closeInspector(); return; }
    const openSheetId = sheetId;
    const active = document.activeElement;
    const typing = active && (active.tagName === "TEXTAREA" || active.tagName === "INPUT") &&
      els.sheetBody.contains(active);
    const typingInsp = !!(els.inspector && active && els.inspector.contains(active) &&
      (active.tagName === "INPUT" || active.tagName === "TEXTAREA"));
    const surfaceChanged = currentSurface() !== surfaceKind;
    // Don't tear down a window the player is typing in; the dots behind it can
    // wait for the next save. A tab switch is a different graph, so that wait
    // does not apply.
    if (openSheetId && typing && !surfaceChanged) return;
    if (typingInsp && !surfaceChanged) return;
    const keepWorld = openWorldId;
    const keepEdge = openEdgeId;
    const keepLore = openLore;
    if (openSheetId) {
      try { B.unmountInputControls(); } catch (_) {}
      try { B.unmountPanelControls(); } catch (_) {}
      try { B.giveBack(); } catch (_) {}
    }
    build();
    seedExperienceHome();
    seedLoreHome();
    seedWorldHomes();
    if (openId !== null && !nodesById[openId]) openId = root.id;
    if (selectId && !nodesById[selectId]) selectId = null;
    // Never stand inside a leaf (a spec sheet). A World is a container once
    // you have entered it; an un-entered World has no interior in this tree.
    while (openId && nodesById[openId] && openId !== root.id &&
           nodesById[openId].kind !== "world-node" &&
           !((nodesById[openId].children || []).length)) {
      const p = nodesById[openId].parent;
      openId = p ? p.id : root.id;
    }
    frame(false);
    paint();
    startWobble();
    if (openSheetId && nodesById[openSheetId]) openSheet(nodesById[openSheetId]);
    else if (openSheetId) closeSheet();
    if (keepLore && nodesById.lore) {
      openLoreInspector(nodesById.lore, { focus: false });
    } else if (keepWorld && nodesById["world:" + keepWorld]) {
      openWorldInspector(nodesById["world:" + keepWorld], { focus: false });
    } else if (keepEdge) {
      const t = ((experienceState().transitions) || []).find((x) => x.id === keepEdge);
      if (t) openLinkInspector(t);
    }
  }

  function onEscape() {
    if (closeSheet()) return true;
    if (closeInspector()) return true;
    return surface();
  }

  // Opening the panel is an arrival too: the dot blooms in rather than being
  // there already. Sync doesn't do this, or every save would replay it.
  function onOpen() {
    if (!B || !B.isGraphMode() || !root) return;
    const live = liveWorldId();
    if (live && nodesById["world:" + live] && onExperienceRing()) {
      selectId = "world:" + live;
      paint();
    }
    bloom(visibleNodes());
    startWobble();
    try { if (B && B.ensureWorldFrames) B.ensureWorldFrames(); } catch (_) {}
  }

  function setLiveWorld(id) {
    const next = id || null;
    let changed = false;
    Object.keys(nodesById).forEach((k) => {
      const n = nodesById[k];
      if (!n || n.kind !== "world-node") return;
      const here = !!(next && n.worldId === next);
      if (n.isHere !== here) { n.isHere = here; changed = true; }
    });
    if (changed) paint();
  }

  // Restarting a CSS animation needs the class off for a frame, or the browser
  // sees no change and does nothing.
  function bloom(nodes, waitMs) {
    if (!nodes.length) return;
    nodes.forEach((n) => { if (n.gi) n.gi.classList.remove("is-blooming", "is-popping"); });
    const go = () => nodes.forEach((n) => { if (n.gi) n.gi.classList.add("is-blooming"); });
    if (waitMs && !reduceMotion()) setTimeout(go, waitMs);
    else requestAnimationFrame(go);
  }

  function popNode(n) {
    if (!n || !n.gi || reduceMotion()) return;
    n.gi.classList.remove("is-popping", "is-blooming");
    requestAnimationFrame(() => {
      if (n.gi) n.gi.classList.add("is-popping");
    });
  }

  function init(bridge) {
    B = bridge;
    els = {
      graph: document.getElementById("we-graph"),
      canvas: document.getElementById("eg-canvas"),
      world: document.getElementById("eg-world"),
      edges: document.getElementById("eg-edges"),
      fx: document.getElementById("eg-fx"),
      up: document.getElementById("eg-up"),
      toolkit: document.getElementById("eg-toolkit"),
      captionName: document.getElementById("eg-caption-name"),
      sheet: document.getElementById("eg-sheet"),
      scrim: document.getElementById("eg-scrim"),
      sheetTitle: document.getElementById("eg-sheet-title"),
      sheetBody: document.getElementById("eg-sheet-body"),
      sheetClose: document.getElementById("eg-sheet-close"),
      hulls: document.getElementById("eg-hulls"),
    };
    if (!els.canvas || !els.world) return;
    if (!els.up && els.graph) {
      const d = document.createElement("div");
      d.id = "eg-up";
      d.className = "eg-up";
      d.setAttribute("aria-hidden", "true");
      d.innerHTML = '<svg viewBox="0 0 24 24" width="32" height="32">'
        + '<path d="M6 15l6-6 6 6" fill="none" stroke="currentColor" stroke-width="1.8"'
        + ' stroke-linecap="round" stroke-linejoin="round"/></svg>';
      els.graph.insertBefore(d, els.canvas);
      els.up = d;
    }
    fxHost();

    bindToolkit();
    els.canvas.addEventListener("pointerdown", onPointerDown);
    els.canvas.addEventListener("pointermove", onPointerMove);
    els.canvas.addEventListener("pointerup", onPointerUp);
    els.canvas.addEventListener("pointercancel", onPointerUp);
    els.canvas.addEventListener("dblclick", onDblClick);
    els.canvas.addEventListener("wheel", onWheel, { passive: false });
    els.canvas.addEventListener("mousemove", onHover);
    els.canvas.addEventListener("mouseleave", () => {
      setHover(null);
      if (els.graph) els.graph.classList.remove("is-paper-hot");
    });
    if (els.up) {
      els.up.addEventListener("click", (evt) => {
        evt.preventDefault();
        evt.stopPropagation();
        if (!paperSurfaces()) return;
        acted();
        closeInspector();
        surface();
      });
    }
    document.addEventListener("keydown", onGraphKey);
    if (els.sheetClose) els.sheetClose.addEventListener("click", closeSheet);
    if (els.scrim) els.scrim.addEventListener("click", closeSheet);
    const onCanvasResize = () => {
      if (!view) return;
      if (!userCam) frame(false);
      else applyView(view);
    };
    window.addEventListener("resize", onCanvasResize);
    if (typeof ResizeObserver !== "undefined" && els.canvas) {
      new ResizeObserver(onCanvasResize).observe(els.canvas);
    }
    // A hidden panel doesn't need a frame loop running behind it.
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stopWobble();
      else if (B.isGraphMode()) startWobble();
    });
  }

  window.EditorGraph = {
    init: init,
    sync: sync,
    syncFrames: syncFrames,
    markRendering: markRendering,
    onOpen: onOpen,
    setLiveWorld: setLiveWorld,
    onEscape: onEscape,
    onDelete: deleteSelection,
    isSheetOpen: () => !!sheetId,
    // Where you are: null while collapsed, else the open node's id.
    openId: () => openId,
    enter: (id) => {
      const n = nodesById[id];
      if (n) diveWorld(n);
    },
    // Open a container or a sheet without hoping a moving dot is under the
    // cursor. Tests (and the console) were missing Music and landing on Level.
    activate: (id) => {
      const n = nodesById[id];
      if (!n) return false;
      activate(n);
      return true;
    },
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
