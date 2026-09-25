/* THE PACK — what the run is carrying, and the moment each thing goes in.
 *
 * Designed on the canvas (GOD — The Goal, row 4, 2026-09-22): a backpack
 * beside the fist; a find card when a goal's prize is taken or a beaten
 * hostile drops something; and a simplified RPG pack — slots on the left, the
 * thing itself on the right. "a new icon similar to the choices icon (to the
 * right of it though) that is a backpack. devise a simplified version of an
 * RPG backpack interface" (Matt).
 *
 * The server owns the list. goal.held() is written by goal.take_prize and
 * goal.award_spoil, every /api/goal/sight answer carries `pack` (and
 * `world_gear`), and /api/encounter/resolve carries `loot` + `pack` for a won
 * fight. This file only draws what it is told: GoalTag.setPack / showFind and
 * the encounter's resolve hand it over through window.Pack.
 *
 * WEARING (2026-09-24, the Characters build — characters.py): a run that is
 * a Character opens its pack in the character's own frame, designed on the
 * canvas "GOD — Characters" (Inventory / Inventory — wearing it): the
 * character framed right, the pack left, "so we can mod him" (Matt). WEAR IT
 * puts a thing on — the server redraws the turnaround with it (a fitting,
 * ~20 s) and the run takes the new look at its next move; the figure here
 * develops into it. TAKE IT OFF is instant when that outfit was drawn before.
 * GET /api/character for the figure, POST /api/character/wear to change it.
 * A run that is not a character (an older save) is the same pack, no figure.
 *
 * Still NOT here: dropping, sorting, weight, stacks, drag and drop.
 */
(function () {
  "use strict";
  if (window.Pack) return;

  const BASE_SLOTS = 12;         // three rows of four; grows by rows, never loses a thing
  const COLS = 4;
  const FIT_POLL_MS = 2000;
  const SLOT_NAME = { head: "Head", face: "Face", neck: "Neck", torso: "Body", outer: "Over the clothes",
                      hands: "Hands", legs: "Legs", feet: "Feet", back: "Back", held: "In hand" };
  const FIND_MS = 4600;          // the find card hands the game back on its own
  const FLY_MS = 760;
  const KIND = { weapon: "Weapon", armor: "Armor", armour: "Armor", upgrade: "Upgrade",
                 tool: "Tool", relic: "Relic" };
  const ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M9.4 6V4.9c0-.8.7-1.5 1.5-1.5h2.2c.8 0 1.5.7 1.5 1.5V6"/>' +
    '<path d="M5.6 10.4C5.6 8 7.6 6 10 6h4c2.4 0 4.4 2 4.4 4.4v8.4c0 1.1-.9 2-2 2H7.6c-1.1 0-2-.9-2-2z"/>' +
    '<path d="M8.6 14.2h6.8v3.2c0 .6-.4 1-1 1H9.6c-.6 0-1-.4-1-1z"/>' +
    '<path d="M12 14.2v1.5"/></svg>';
  // While any of these is on, the game is not being played: no pack button,
  // no pack, and a find waits its turn.
  const AWAY = ["start-menu-on", "mode-watch", "world-editor-on", "moment-active",
                "camera-mode", "awaiting-first-scene", "opening-blackout"];

  const S = {
    items: [],               // newest last, exactly as the server sent it
    world: null,             // {found, of}: how much of this world's gear is in here
    open: false,
    sel: 0,
    known: null,             // names already told about (null until the first answer)
    fresh: new Set(),        // names in the pack the player has not looked at yet
    pending: new Set(),      // names whose find card is up / in flight
    queue: Promise.resolve(),
    shown: 0,                // finds shown this page, for the harness
    renders: 0,              // real rebuilds of the open pack, for the harness
    char: null,              // the run's character (GET /api/character), or null
    wearing: "",             // item id whose WEAR / TAKE OFF is on its way
    fitName: "",             // what is going on / coming off, for the status line
    fitOn: true,             // putting it on (true) or taking it off
    early: false,            // the press is in flight long enough to show the fitting
    fitT: null,              // polling a fitting
    note: "",                // the line under the verb
    needLoad: true,          // fetch the character's pack once the run is on screen
  };
  const sid = () => window.__SOMEWHERE_SESSION__ || "default";
  async function call(method, url, body) {
    const opt = { method, headers: { "X-Session-Id": sid() } };
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(Object.assign({ session_id: sid() }, body));
    }
    const sep = url.includes("?") ? "&" : "?";
    const res = await fetch(method === "GET" ? `${url}${sep}session_id=${encodeURIComponent(sid())}` : url, opt);
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (!res.ok || data.ok === false) throw new Error((data && data.error) || `HTTP ${res.status}`);
    return data;
  }
  // The last thing the open pack was drawn as. Every server answer re-syncs
  // the pack, and a rebuild makes new <img>s that paint blank until they
  // decode — the playtest caught the open pack with every cell empty right
  // after the win. Same content: leave the DOM alone.
  let drawnAs = "";
  // One decoded picture per plate and place, handed back to the next rebuild
  // instead of a fresh <img> that has to decode again.
  const imgs = new Map();
  let built = false;
  let btn, badge, scrim, panel, grid, detail, countEl, worldEl, findEl, fig, wearingEl, etaEl, statusEl, barEl, ringEl, eyebrowEl, verbEl, noteEl;

  const keyOf = (g) => String((g && g.name) || "").trim().toLowerCase();
  const kindLabel = (g) => KIND[String((g && g.kind) || "").toLowerCase()] ||
    (g && g.source === "encounter" ? "Spoils" : "Found");
  // The two rules the fight reads off the pack (combat.WEAPON_ATTACK /
  // WEAPON_DAMAGE and the armour save), said where the player looks at the
  // thing.
  const EDGE = {
    weapon: "In a fight: +1 to hit, and 2d6+3 damage instead of 1d8+3.",
    armor: "In a fight: takes one killing blow.",
    armour: "In a fight: takes one killing blow.",
  };
  const edgeOf = (g) => (g && g.source !== "pickup" && EDGE[String(g.kind || "").toLowerCase()]) || "";
  const initial = (g) => (String((g && g.name) || "?").replace(/^the\s+/i, "").trim()[0] || "?").toUpperCase();
  const away = () => AWAY.some((c) => document.body && document.body.classList.contains(c));
  const reduceMotion = () => {
    try { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (_) { return false; }
  };
  function fromLine(g) {
    if (g && g.source === "pickup") return "Picked up along the way";
    const f = String((g && g.from) || "").trim();
    if (!f) return "";
    return g.source === "encounter" ? `Off ${f}` : `Out of ${f}`;
  }
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function mono(g, cls) {
    return el("span", cls || "pk-mono", initial(g));
  }
  // A plate that will not load shows the letter, never a broken picture —
  // the strip this replaces put alt text over the fist for a whole run.
  function plate(g, cls) {
    if (!g.plate) return mono(g);
    const key = `${cls || "pk-plate"}|${g.plate}`;
    const had = imgs.get(key);
    if (had && !had.isConnected) return had;
    const im = new Image();
    im.className = cls || "pk-plate";
    im.alt = "";
    im.decoding = "async";
    im.draggable = false;
    im.addEventListener("error", () => { imgs.delete(key); im.replaceWith(mono(g)); });
    im.src = g.plate;
    if (!had) imgs.set(key, im);
    return im;
  }

  // ── BUILD ────────────────────────────────────────────────────────────────
  function build() {
    if (built) return true;
    const wheel = document.getElementById("action-wheel");
    if (!wheel || !document.body) return false;

    btn = el("button");
    btn.id = "pack-btn";
    btn.type = "button";
    btn.className = "empty";
    btn.title = "Pack (B)";
    btn.setAttribute("aria-haspopup", "dialog");
    btn.setAttribute("aria-expanded", "false");
    btn.innerHTML = ICON.replace("<svg ", '<svg class="pack-icon" ') +
      '<svg class="pack-ring" viewBox="0 0 60 60" aria-hidden="true"><circle class="pack-ring-bg" cx="30" cy="30" r="28"/>' +
      '<circle class="pack-ring-p" cx="30" cy="30" r="28" pathLength="100"/></svg>' +
      '<span class="pack-badge" aria-hidden="true"></span>';
    badge = btn.querySelector(".pack-badge");
    const fist = document.getElementById("fist-btn");
    if (fist && fist.parentNode === wheel) fist.insertAdjacentElement("afterend", btn);
    else wheel.appendChild(btn);
    btn.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); toggle(); });

    scrim = el("div", "hidden");
    scrim.id = "pack-scrim";
    scrim.addEventListener("pointerdown", (e) => { e.stopPropagation(); close(); });

    // The character's frame (canvas "GOD — Characters", Inventory): the game
    // dimmed and blurred behind, the studio light, the character on the right, the
    // pack and the verb on the left.
    const art = window.CharacterArt || {};
    panel = el("div", "hidden");
    panel.id = "pack-panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Pack");
    panel.innerHTML =
      '<div class="pk-veil"></div>' +
      (art.backdrop ? art.backdrop() : "") +
      '<div class="pk-figure"><div class="pk-figure-in">' +
      '<img class="cs-fig pk-fig-a" alt="" draggable="false"><img class="cs-fig pk-fig-b" alt="" draggable="false"></div></div>' +
      '<div class="pk-fade"></div>' +
      '<button type="button" class="pk-close">CLOSE <span>B</span></button>' +
      '<div class="pk-left">' +
      '  <div class="pk-eyebrow"><span class="pk-who">PACK</span><span class="pk-count"></span></div>' +
      '  <div class="pk-grid" role="listbox" aria-label="What you are carrying"></div>' +
      '  <div class="pk-detail" aria-live="polite"></div>' +
      '</div>' +
      '<div class="pk-verb"><button type="button" class="pk-wear"></button><div class="pk-note"></div>' +
      '  <div class="pk-prog" role="status" aria-live="polite"><div class="pk-status"></div>' +
      '<div class="cs-bar pk-bar"><i></i></div><div class="pk-eta"></div></div></div>' +
      '<div class="pk-wearing" aria-live="polite"></div>' +
      '<div class="pk-world"></div>';
    // The panel is its own world: nothing clicked in it reaches the game.
    ["pointerdown", "click", "wheel"].forEach((t) =>
      panel.addEventListener(t, (e) => e.stopPropagation()));
    grid = panel.querySelector(".pk-grid");
    detail = panel.querySelector(".pk-detail");
    countEl = panel.querySelector(".pk-count");
    worldEl = panel.querySelector(".pk-world");
    // The figure is the character screen's (characters.js makeFigure): two
    // slots that cross-fade, a fitting developing in over the old pose.
    fig = art.Figure ? art.Figure(panel.querySelector(".pk-fig-a"), panel.querySelector(".pk-fig-b"),
                                  panel, () => S.open) : null;
    wearingEl = panel.querySelector(".pk-wearing");
    etaEl = panel.querySelector(".pk-eta");
    statusEl = panel.querySelector(".pk-status");
    barEl = panel.querySelector(".pk-bar");
    ringEl = btn.querySelector(".pack-ring-p");
    eyebrowEl = panel.querySelector(".pk-who");
    verbEl = panel.querySelector(".pk-wear");
    noteEl = panel.querySelector(".pk-note");
    panel.querySelector(".pk-close").addEventListener("click", () => close());
    verbEl.addEventListener("click", () => wearSelected());

    findEl = el("div", "hidden");
    findEl.id = "pack-find";
    findEl.setAttribute("role", "status");
    findEl.setAttribute("aria-live", "polite");
    findEl.innerHTML =
      '<div class="pf-dim"></div>' +
      '<div class="pf-card">' +
      '<div class="pf-eyebrow"></div>' +
      '<div class="pf-plate"></div>' +
      '<div class="pf-kind"></div><div class="pf-name"></div>' +
      '<div class="pf-power"></div><div class="pf-edge"></div><div class="pf-worth"></div>' +
      '<div class="pf-from"></div>' +
      '<div class="pf-hint"><span>Into your pack</span></div>' +
      '</div>';

    document.body.appendChild(scrim);
    document.body.appendChild(panel);
    document.body.appendChild(findEl);
    window.addEventListener("keydown", onKey, true);
    // A fight, a cutscene or the menu takes the screen: the pack gets out of
    // the way rather than sitting over it.
    // A run that is a character starts with its pack full (characters.py):
    // the moment the game is being played, ask for it — the goal's first
    // sight answer can be a while.
    try {
      new MutationObserver(() => {
        if (S.open && away()) close();
        if (S.needLoad && !away()) { S.needLoad = false; loadCharacter(); }
      }).observe(document.body, { attributes: true, attributeFilter: ["class"] });
    } catch (_) {}
    built = true;
    return true;
  }

  // ── DRAW ─────────────────────────────────────────────────────────────────
  function slots() {
    return Math.max(BASE_SLOTS, Math.ceil(S.items.length / COLS) * COLS);
  }
  function shownCount() {
    return S.items.filter((g) => !S.pending.has(keyOf(g))).length;
  }
  function render() {
    if (!build()) return;
    const n = shownCount();
    btn.classList.toggle("empty", S.items.length === 0);
    btn.classList.toggle("has-new", S.fresh.size > 0 && !S.open);
    btn.classList.toggle("open", S.open);
    badge.textContent = n ? String(n) : "";
    btn.setAttribute("aria-expanded", S.open ? "true" : "false");
    btn.setAttribute("aria-label", S.open ? "Close the pack"
      : `Open the pack — ${n} ${n === 1 ? "thing" : "things"} in it`);
    // A fitting keeps its clock when the pack is shut: a ring round the pack
    // button fills while the character changes, and pulses when it lands.
    const drawing = isDrawing();
    btn.classList.toggle("fitting", drawing);
    if (drawing) startProg(); else stopProg();
    if (!S.open) { drawnAs = ""; return; }
    const total = slots();
    const ch = S.char;
    const sig = JSON.stringify([total, S.sel, Array.from(S.fresh), S.world, S.wearing, S.early, S.note,
      ch ? [ch.id, ch.idle, ch.look, ch.fitting, ch.finishing, (ch.dev || {}).idle] : null,
      S.items.map((g) => [g.name, g.kind, g.plate, g.power, g.worth, g.source, g.from, g.worn, g.slot])]);
    if (sig === drawnAs && grid.childElementCount) return;
    drawnAs = sig;
    S.renders += 1;
    countEl.textContent = `${S.items.length} / ${total}`;
    eyebrowEl.textContent = ch && ch.name ? `INVENTORY \u00b7 ${ch.name.toUpperCase()}` : "PACK";
    grid.style.gridTemplateColumns = `repeat(${COLS}, minmax(0, 1fr))`;
    renderFigure();
    renderGrid(total);
    renderDetail();
    renderVerb();
    renderWorld();
    renderWearing();
  }
  // The cells are built once per list and updated in place: rebuilt on every
  // arrow press, the selection ring could not ease from cell to cell and the
  // plates were re-inserted each time.
  let gridKey = "";
  function renderGrid(total) {
    const key = JSON.stringify([total, S.items.map((g) => [g.name, g.plate])]);
    if (key !== gridKey || grid.childElementCount !== total) {
      gridKey = key;
      grid.innerHTML = "";
      for (let i = 0; i < total; i++) {
        const g = S.items[i];
        const cell = el("button", "pk-cell" + (g ? "" : " empty"));
        cell.type = "button";
        cell.setAttribute("role", "option");
        cell.setAttribute("aria-label", g ? `${g.name}, ${kindLabel(g)}` : `Empty slot ${i + 1}`);
        if (g) {
          cell.appendChild(plate(g));
          cell.appendChild(el("span", "pk-kind"));
          cell.appendChild(el("span", "pk-new"));
        }
        cell.addEventListener("click", () => pick(i));
        grid.appendChild(cell);
      }
    }
    Array.from(grid.children).forEach((cell, i) => {
      const g = S.items[i];
      cell.classList.toggle("sel", i === S.sel);
      cell.setAttribute("aria-selected", i === S.sel ? "true" : "false");
      if (!g) return;
      const was = cell.classList.contains("worn");
      cell.classList.toggle("worn", !!g.worn);
      cell.classList.toggle("fitting", S.wearing === g.id || !!(S.char && S.char.fitting && g.worn));
      const tag = cell.querySelector(".pk-kind");
      const txt = g.worn ? "WORN" : (g.kind ? kindLabel(g) : "");
      if (tag.textContent !== txt) {
        tag.textContent = txt;
        tag.classList.toggle("pk-worn", !!g.worn);
        if (was !== !!g.worn && !reduceMotion()) { tag.classList.remove("pk-set"); void tag.offsetWidth; tag.classList.add("pk-set"); }
      }
      cell.querySelector(".pk-new").hidden = !S.fresh.has(keyOf(g));
    });
  }
  // What the character has on, said under them: the pack is where you change
  // it, and this is what the next move will show.
  function renderWearing() {
    const on = S.char ? S.items.filter((g) => g.worn).map((g) => g.name) : [];
    const txt = on.length ? `WEARING \u00b7 ${on.join(" \u00b7 ").toUpperCase()}` : (S.char ? "WEARING NOTHING FROM THE PACK" : "");
    if (wearingEl.textContent !== txt) {
      wearingEl.textContent = txt;
      if (S.open && !reduceMotion()) { wearingEl.classList.remove("pk-set"); void wearingEl.offsetWidth; wearingEl.classList.add("pk-set"); }
    }
  }
  // The character, big, on the right: their current look, and while a
  // fitting draws, the old one as a shadow with the new one developing over it.
  function renderFigure() {
    const ch = S.char;
    panel.classList.toggle("pk-has-char", !!ch);
    // A fitting is worn from the next move as soon as its turnaround lands,
    // but its portrait is posed ~25 s later. Until then the pack keeps the
    // pose it had as a black shape with a rim of light, the bar under the
    // verb runs, and the new pose develops in over it — never the
    // turnaround's A-pose, the four-view strip ("why is he in an A pose") or
    // a drawn stand-in figure ("get rid of this stupid stick figure").
    //
    // It starts on the PRESS ("no loading progress when wearing items. feels
    // frozen"): the shadow, the status line and the bar are up before the
    // server has answered, not after it.
    const dev = ch ? ((ch.dev || {}).idle || "") : "";
    const drawing = isDrawing();
    panel.classList.toggle("pk-fitting", drawing);
    if (!fig) return;
    if (!ch) fig.set("", "idle");
    else if (dev && (ch.fitting || ch.finishing)) fig.set(dev, "idle", { develop: true });
    else fig.set(ch.idle || "", drawing ? "ghost" : "idle");
  }
  // Is the character being redrawn — or about to be, the press still in
  // flight? A fitting's pose develops in over the shadow (``dev``); once it
  // has, the pack is showing the new look and the clock stops.
  function isDrawing() {
    const ch = S.char;
    if (!ch) return false;
    const dev = (ch.dev || {}).idle || "";
    return !!(((ch.fitting || ch.finishing) || (S.wearing && S.early)) && !dev);
  }
  // ── the fitting's progress: the same clock as the character screen ──────
  const PROG = { raf: 0, p: 0, last: 0, t0: 0, txt: "", st: "" };
  function startProg() {
    if (PROG.raf) return;
    PROG.p = 0; PROG.last = performance.now(); PROG.t0 = Date.now() / 1000;
    const tick = (t) => {
      PROG.raf = requestAnimationFrame(tick);
      const dt = Math.min(0.1, (t - PROG.last) / 1000);
      PROG.last = t;
      const job = (S.char && S.char.job) || {};
      const eta = Math.max(6, Number(job.eta) || 25);
      const total = Math.max(eta + 6, Number(job.eta_total) || eta * 1.9);
      const now = Date.now() / 1000;
      // The server's start when it has one (a pack reopened on a fitting
      // already under way); the press when the answer has not come back.
      const started = Number(job.started) && Number(job.started) <= now + 2 ? Number(job.started) : PROG.t0;
      const el_ = Math.max(0, now - started);
      // A first step the moment it starts, so the press is answered by
      // movement; then the learned clock.
      const target = Math.min(0.94, 0.04 + 0.96 * (1 - Math.exp(-2.3 * el_ / total)));
      PROG.p = Math.max(PROG.p, PROG.p + (target - PROG.p) * Math.min(1, dt * 2.2));
      // On the two elements that draw it, not the panel: a custom property
      // set on #pack-panel restyles everything in it, sixty times a second.
      const pv = PROG.p.toFixed(4);
      if (barEl) barEl.style.setProperty("--cs-p", pv);
      if (ringEl) ringEl.style.setProperty("--pk-p", pv);
      const worn = job.stage === "finishing";
      const left = (worn ? total : eta) - el_;
      const when = left > 7 ? `ABOUT ${Math.ceil(left / 5) * 5} SECONDS` : left > 2 ? "A FEW SECONDS" : "ANY MOMENT";
      const txt = worn ? `THE PORTRAIT IN ${when}` : `ON IN ${when}`;
      if (txt !== PROG.txt) { PROG.txt = txt; if (etaEl) etaEl.textContent = S.fitOn || worn ? txt : txt.replace(/^ON IN/, "OFF IN"); }
      const st = statusLine(worn);
      if (st !== PROG.st) {
        PROG.st = st;
        if (statusEl) {
          statusEl.textContent = st;
          if (S.open && !reduceMotion()) { statusEl.classList.remove("pk-set"); void statusEl.offsetWidth; statusEl.classList.add("pk-set"); }
        }
      }
    };
    PROG.raf = requestAnimationFrame(tick);
  }
  function statusLine(worn) {
    const [he, him] = pronoun();
    const who = S.char && S.char.name ? String(S.char.name).split(/\s+/)[0] : (he === "THEY" ? "They" : he === "SHE" ? "She" : "He");
    const what = S.fitName ? `the ${S.fitName.replace(/^the\s+/i, "")}` : "it";
    if (worn) return `${S.fitOn ? "On" : "Off"} ${him.toLowerCase()} from your next move — posing ${him.toLowerCase()} in it\u2026`;
    return S.fitOn ? `${who} is putting on ${what}\u2026` : `${who} is taking off ${what}\u2026`;
  }
  function stopProg() {
    if (PROG.raf) cancelAnimationFrame(PROG.raf);
    PROG.raf = 0; PROG.txt = ""; PROG.st = "";
    if (etaEl) etaEl.textContent = "";
    if (statusEl) statusEl.textContent = "";
    if (ringEl) ringEl.style.removeProperty("--pk-p");
  }
  function pronoun() {
    const p = String((S.char && S.char.pronouns) || "").toLowerCase();
    return p.startsWith("she") ? ["SHE", "HER"] : p.startsWith("he") ? ["HE", "HIM"] : ["THEY", "THEM"];
  }
  function renderVerb() {
    const g = S.items[S.sel];
    const ch = S.char;
    const can = !!(ch && g && g.id && g.wearable);
    verbEl.hidden = !can;
    verbEl.disabled = !!S.wearing;
    const [he, him] = pronoun();
    if (can) verbEl.textContent = g.worn ? "TAKE IT OFF" : "WEAR IT \u23ce";
    let note = S.note;
    if (!note && ch && ch.fitting) note = `${he === "THEY" ? "THEY'RE" : he + "'S"} PUTTING IT ON\u2026`;
    if (!note && ch && ch.finishing) note = `ON ${him} FROM YOUR NEXT MOVE`;
    if (S.wearing) note = g && g.worn ? "TAKING IT OFF\u2026" : `${he === "THEY" ? "THEY'RE" : he + "'S"} PUTTING IT ON\u2026`;
    if (!note && can && !g.worn) note = `${he} PUTS IT ON IN ABOUT HALF A MINUTE`;
    if (!note && can && g.worn) note = `ON ${him} FROM YOUR NEXT MOVE`;
    if (!note && ch && g && g.id && !g.wearable) note = "CARRIED, NOT WORN";
    // The status line under it says it, bigger, while the character changes.
    if (isDrawing() && !(ch && ch.error)) note = "";
    noteEl.textContent = note || "";
  }
  let detailWas = "";
  function renderDetail() {
    const g = S.items[S.sel];
    const key = g ? JSON.stringify([g.name, g.worn, g.power, g.slot]) : `empty${S.sel}`;
    const moved = detailWas && key !== detailWas;
    detailWas = key;
    detail.innerHTML = "";
    if (moved && !reduceMotion()) { detail.classList.remove("pk-set"); void detail.offsetWidth; detail.classList.add("pk-set"); }
    if (!g) {
      detail.appendChild(el("div", "pk-d-kind", "Empty"));
      detail.appendChild(el("div", "pk-d-power quiet",
        "Goals and won fights fill it. Every world has its own gear to find."));
      return;
    }
    const slotLine = g.slot ? `${(SLOT_NAME[g.slot] || g.slot).toUpperCase()} \u00b7 ${g.worn ? "WORN" : "NOT WORN"}`
      : kindLabel(g).toUpperCase() + (g.id ? " \u00b7 CARRIED" : "");
    const kindEl = el("div", "pk-d-kind" + (g.worn ? " worn" : ""), slotLine);
    detail.appendChild(kindEl);
    detail.appendChild(el("div", "pk-d-name", g.name));
    if (g.source === "encounter") detail.appendChild(el("div", "pk-d-tag", "Spoils"));
    detail.appendChild(el("div", "pk-d-power" + (g.power ? "" : " quiet"),
      g.power || "Nothing is written about what it does."));
    if (edgeOf(g)) detail.appendChild(el("div", "pk-d-edge", edgeOf(g)));
    if (g.worth) detail.appendChild(el("div", "pk-d-worth", g.worth));
    const from = fromLine(g);
    if (from) detail.appendChild(el("div", "pk-d-from", from));
  }
  function renderWorld() {
    worldEl.innerHTML = "";
    const w = S.world;
    if (!w || !w.of) return;
    const dots = el("span", "pk-dots");
    for (let i = 0; i < w.of; i++) dots.appendChild(el("i", i < w.found ? "on" : ""));
    worldEl.appendChild(dots);
    worldEl.appendChild(el("span", "pk-world-line", `${w.found} of this world's ${w.of} found`));
  }

  // ── STATE IN ─────────────────────────────────────────────────────────────
  // The server's list, whole. Anything in it the page has not been told about
  // before is NEW until the player looks at it — except on the first answer
  // of a page, which is a resume, not a find.
  function sync(list, world) {
    const items = (Array.isArray(list) ? list : []).filter((g) => g && g.name);
    if (world && typeof world === "object" && Number.isFinite(Number(world.of))) {
      S.world = { found: Number(world.found) || 0, of: Number(world.of) || 0 };
    }
    const names = items.map(keyOf);
    if (S.known === null) {
      S.known = new Set(names);
    } else {
      names.forEach((k) => {
        if (!S.known.has(k)) { S.known.add(k); if (!S.pending.has(k)) S.fresh.add(k); }
      });
    }
    Array.from(S.fresh).forEach((k) => { if (!names.includes(k)) S.fresh.delete(k); });
    if (!items.length) {
      // A new run: nothing new, no haul on any card. The next answer is a
      // resume, not a find — a character's pack comes back full.
      document.querySelectorAll(".pk-haul").forEach((n) => n.remove());
      S.known = null;
      S.char = null;
      S.needLoad = true;
      S.fresh.clear();
      S.pending.clear();
      if (S.open) close();
    }
    S.items = items;
    if (S.sel >= slots()) S.sel = 0;
    render();
  }

  // ── THE FIND ─────────────────────────────────────────────────────────────
  // It becomes theirs: the thing, big, on nothing — then it goes into the
  // pack, and the pack says so. Queued, so a fight's spoils and a goal's
  // prize never talk over each other; held while a fight or a cutscene owns
  // the screen. Resolves once it has landed.
  function found(gear, opts) {
    if (!gear || !gear.name || !build()) return Promise.resolve(false);
    const k = keyOf(gear);
    S.pending.add(k);
    S.fresh.delete(k);        // it is NEW once it has landed, not before
    render();
    const o = opts || {};
    S.queue = S.queue.then(() => showFind(gear, o)).catch(() => {
      S.pending.delete(k);
      render();
      return false;
    });
    return S.queue;
  }
  function waitUntil(test, ms) {
    return new Promise((resolve) => {
      const t0 = Date.now();
      (function poll() {
        if (test() || Date.now() - t0 > ms) { resolve(); return; }
        setTimeout(poll, 250);
      })();
    });
  }
  async function showFind(g, o) {
    await waitUntil(() => !away(), 15000);
    const k = keyOf(g);
    const spoils = (o.source || g.source) === "encounter";
    findEl.classList.toggle("spoils", spoils);
    findEl.querySelector(".pf-eyebrow").textContent = spoils ? "Spoils" : "Found";
    const pl = findEl.querySelector(".pf-plate");
    pl.innerHTML = "";
    pl.appendChild(plate(g, "pf-img"));
    findEl.querySelector(".pf-kind").textContent = kindLabel(g);
    findEl.querySelector(".pf-name").textContent = g.name;
    findEl.querySelector(".pf-power").textContent = g.power || "";
    findEl.querySelector(".pf-edge").textContent = edgeOf(g);
    findEl.querySelector(".pf-worth").textContent = g.worth || "";
    const b = o.board;
    findEl.querySelector(".pf-from").textContent = [
      fromLine(g), b && b.of ? `Goal ${b.done} of ${b.of}` : "",
    ].filter(Boolean).join("   ·   ");
    findEl.classList.remove("hidden", "flying");
    void findEl.offsetWidth;
    findEl.classList.add("on");
    S.shown += 1;
    await new Promise((resolve) => {
      let done = false;
      const go = () => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        findEl.removeEventListener("pointerdown", go);
        resolve();
      };
      const timer = setTimeout(go, Number(o.ms) || FIND_MS);
      findEl.addEventListener("pointerdown", go);
    });
    await fly();
    S.pending.delete(k);
    S.fresh.add(k);
    if (S.known) S.known.add(k);
    findEl.classList.remove("on");
    setTimeout(() => { if (!findEl.classList.contains("on")) findEl.classList.add("hidden"); }, 450);
    land();
    return true;
  }
  // The plate leaves the card and goes into the button.
  function fly() {
    return new Promise((resolve) => {
      try {
        const from = findEl.querySelector(".pf-plate");
        const a = from.getBoundingClientRect();
        const b = btn.getBoundingClientRect();
        const visible = b.width > 0 && getComputedStyle(btn).opacity !== "0";
        if (!a.width || !visible || reduceMotion() || typeof from.animate !== "function") {
          resolve();
          return;
        }
        const ghost = from.cloneNode(true);
        ghost.className = "pf-plate pack-ghost";
        Object.assign(ghost.style, {
          left: `${a.left}px`, top: `${a.top}px`, width: `${a.width}px`, height: `${a.height}px`,
        });
        document.body.appendChild(ghost);
        findEl.classList.add("flying");
        const dx = (b.left + b.width / 2) - (a.left + a.width / 2);
        const dy = (b.top + b.height / 2) - (a.top + a.height / 2);
        const end = Math.max(0.1, (b.width / a.width) * 0.9);
        const anim = ghost.animate([
          { transform: "translate(0px, 0px) scale(1)", opacity: 1 },
          { transform: `translate(${dx * 0.5}px, ${dy * 0.3 - 50}px) scale(${(1 + end) / 2})`,
            opacity: 1, offset: 0.5 },
          { transform: `translate(${dx}px, ${dy}px) scale(${end})`, opacity: 0.15 },
        ], { duration: FLY_MS, easing: "cubic-bezier(0.55, 0, 0.3, 1)", fill: "forwards" });
        let over = false;
        const end_ = () => { if (over) return; over = true; ghost.remove(); resolve(); };
        anim.onfinish = end_;
        setTimeout(end_, FLY_MS + 400);
      } catch (_) { resolve(); }
    });
  }
  function land() {
    render();
    if (!btn) return;
    btn.classList.remove("got");
    void btn.offsetWidth;
    btn.classList.add("got");
    setTimeout(() => btn.classList.remove("got"), 1000);
  }

  // ── OPEN / CLOSE ─────────────────────────────────────────────────────────
  function openPack() {
    if (!build() || S.open || away()) return false;
    S.open = true;
    // It opens on what they have not looked at yet — the newest find.
    let at = -1;
    S.items.forEach((g, i) => { if (S.fresh.has(keyOf(g))) at = i; });
    S.sel = at >= 0 ? at : Math.max(0, S.items.length - 1);
    if (S.items[S.sel]) S.fresh.delete(keyOf(S.items[S.sel]));
    scrim.classList.remove("hidden");
    panel.classList.remove("hidden");
    void panel.offsetWidth;
    scrim.classList.add("on");
    panel.classList.add("on");
    // It sets in like the character screen: the figure rises into place, the
    // pack and the words follow it, the verb last.
    if (!reduceMotion()) {
      panel.classList.remove("pk-enter");
      void panel.offsetWidth;
      panel.classList.add("pk-enter");
      setTimeout(() => panel.classList.remove("pk-enter"), 1600);
    }
    document.body.classList.add("pack-open");
    S.note = "";
    render();
    try { const c = grid.querySelector(".pk-cell.sel"); if (c) c.focus({ preventScroll: true }); } catch (_) {}
    loadCharacter();
    return true;
  }

  // ── THE CHARACTER ────────────────────────────────────────────────────────
  async function loadCharacter() {
    try {
      const res = await call("GET", "/api/character");
      S.char = res.character || null;
      if (Array.isArray(res.pack)) sync(res.pack, res.world_gear);
      else render();
      if (S.char && (S.char.fitting || S.char.finishing)) watchFitting();
    } catch (_) { /* the pack still opens: it just has no figure */ }
  }
  function watchFitting() {
    if (S.fitT) return;
    S.fitT = setInterval(async () => {
      let res;
      try { res = await call("GET", "/api/character"); } catch (_) { return; }
      const was = S.char;
      S.char = res.character || null;
      if (Array.isArray(res.pack)) S.items = res.pack.filter((g) => g && g.name);
      // A fitting is playable the moment its turnaround lands; the hero pose
      // finishes behind it (``finishing``) and crossfades in when it does.
      if (was && was.fitting && !(S.char && S.char.fitting)) {
        const [he, him] = pronoun();
        S.note = S.char && S.char.look !== was.look ? `ON ${him} FROM YOUR NEXT MOVE`
          : (S.char && S.char.error ? "IT WOULD NOT GO ON \u2014 TRY AGAIN" : "");
        try { if (window.Sound && Sound.itemReveal) Sound.itemReveal(); } catch (_) {}
      }
      const done = !S.char || !(S.char.fitting || S.char.finishing);
      if (done) {
        clearInterval(S.fitT); S.fitT = null;
        // The pose landed: with the pack shut, the button says so.
        if (was && (was.fitting || was.finishing) && S.char && !S.char.error && !S.open) {
          land();
          try { if (window.Sound && Sound.itemReveal) Sound.itemReveal(); } catch (_) {}
        }
        S.fitName = S.fitName && S.char && S.char.error ? S.fitName : "";
      }
      render();
    }, FIT_POLL_MS);
  }
  async function wearSelected() {
    const g = S.items[S.sel];
    if (!g || !g.id || !g.wearable || !S.char || S.wearing) return;
    S.wearing = g.id;
    S.fitName = String(g.name || "");
    S.fitOn = !g.worn;
    S.early = false;
    S.note = "";
    render();
    // The shadow and the clock come up on the press — after a beat, so a
    // look already drawn (taking a thing back off) swaps without a flash.
    setTimeout(() => { if (S.wearing === g.id) { S.early = true; render(); } }, 160);
    try {
      const res = await call("POST", "/api/character/wear", { item: g.id, on: !g.worn });
      S.char = res.character || S.char;
      if (Array.isArray(res.pack)) S.items = res.pack.filter((x) => x && x.name);
      try { if (window.Sound && Sound.select) Sound.select(); } catch (_) {}
      if (S.char && (S.char.fitting || S.char.finishing)) watchFitting();
      if (!(S.char && S.char.fitting)) S.note = `ON ${pronoun()[1]} FROM YOUR NEXT MOVE`;
    } catch (e) {
      S.note = String(e.message || "could not").toUpperCase();
    } finally {
      S.wearing = "";
      S.early = false;
      render();
    }
  }
  function close() {
    if (!S.open) return;
    S.open = false;
    // Everything in there has now been seen.
    S.fresh.clear();
    scrim.classList.remove("on");
    panel.classList.remove("on");
    document.body.classList.remove("pack-open");
    setTimeout(() => {
      if (S.open) return;
      scrim.classList.add("hidden");
      panel.classList.add("hidden");
      if (fig) fig.reset();     // the next open rises into place again
    }, 240);
    render();
  }
  function toggle() { if (S.open) close(); else openPack(); }
  function pick(i) {
    const total = slots();
    S.sel = Math.max(0, Math.min(total - 1, i));
    if (S.items[S.sel]) S.fresh.delete(keyOf(S.items[S.sel]));
    render();
    try { const c = grid.querySelector(".pk-cell.sel"); if (c) c.focus({ preventScroll: true }); } catch (_) {}
  }

  // B opens it (I is the image-model menu). While it is open, Esc or B closes
  // it and the arrows walk the slots; every other key belongs to the game —
  // the pack is non-modal, like the journal.
  function onKey(e) {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" ||
              t.isContentEditable)) return;
    const k = e.key;
    if (S.open) {
      if (k === "Escape" || k === "b" || k === "B") {
        e.preventDefault(); e.stopImmediatePropagation(); close(); return;
      }
      if (k === "Enter" && !e.repeat) { e.preventDefault(); e.stopImmediatePropagation(); wearSelected(); return; }
      const cols = COLS;
      const mv = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -cols, ArrowDown: cols }[k];
      if (mv) { e.preventDefault(); e.stopImmediatePropagation(); pick(S.sel + mv); }
      return;
    }
    if ((k === "b" || k === "B") && !e.repeat && !away()) {
      e.preventDefault(); e.stopImmediatePropagation(); openPack();
    }
  }

  // THE HAUL. The run is won: what they walked out with, as the things
  // themselves, on the card that says so.
  function haul(card) {
    if (!card) return;
    let h = card.querySelector(".pk-haul");
    if (!S.items.length) { if (h) h.remove(); return; }
    if (!h) {
      h = el("div", "pk-haul");
      const line = card.querySelector("#goal-reached-line");
      if (line) line.insertAdjacentElement("afterend", h); else card.appendChild(h);
    }
    h.innerHTML = "";
    h.appendChild(el("div", "pk-haul-kind", `Carried out \u00b7 ${S.items.length}`));
    const row = el("div", "pk-haul-row");
    S.items.forEach((g) => {
      const it = el("div", "pk-haul-item");
      it.title = [g.name, g.power].filter(Boolean).join(" \u2014 ");
      const pl = el("div", "pk-haul-plate");
      pl.appendChild(plate(g, "pk-haul-img"));
      it.appendChild(pl);
      it.appendChild(el("div", "pk-haul-name", g.name));
      row.appendChild(it);
    });
    h.appendChild(row);
  }

  // Pictures in the open pack that have actually decoded and have a size on
  // screen — not just "the request finished".
  function imgs0() {
    if (!panel) return 0;
    return Array.from(panel.querySelectorAll(".pk-cell img, .pk-d-plate img")).filter((im) => {
      const r = im.getBoundingClientRect();
      return im.complete && im.naturalWidth > 0 && r.width > 8 && r.height > 8 &&
        Number(getComputedStyle(im).opacity) > 0.5;
    }).length;
  }
  function debug() {
    const cells = panel ? Array.from(panel.querySelectorAll(".pk-cell")) : [];
    const imgs = panel ? Array.from(panel.querySelectorAll(".pk-cell img")) : [];
    const r = btn ? btn.getBoundingClientRect() : null;
    const findImg = findEl ? findEl.querySelector(".pf-plate img") : null;
    return {
      items: S.items.map((g) => ({ name: g.name, kind: g.kind || "", source: g.source || "",
                                    plate: g.plate || "" })),
      world: S.world, open: S.open, sel: S.sel,
      fresh: Array.from(S.fresh), pending: Array.from(S.pending), finds: S.shown,
      renders: S.renders,
      platesPainted: imgs0(),
      badge: badge ? badge.textContent : "",
      button: btn ? {
        cls: btn.className, x: r && Math.round(r.x), y: r && Math.round(r.y),
        w: r && Math.round(r.width),
        opacity: Number(getComputedStyle(btn).opacity),
      } : null,
      cells: cells.length,
      filled: cells.filter((c) => !c.classList.contains("empty")).length,
      platesLoaded: imgs.filter((im) => im.complete && im.naturalWidth > 0).length,
      platesBroken: imgs.filter((im) => im.complete && im.naturalWidth === 0).length,
      findOn: !!(findEl && findEl.classList.contains("on")),
      findName: findEl ? findEl.querySelector(".pf-name").textContent : "",
      findPlateLoaded: !!(findImg && findImg.complete && findImg.naturalWidth > 0),
      detailName: detail ? ((detail.querySelector(".pk-d-name") || {}).textContent || "") : "",
      character: S.char ? { id: S.char.id, name: S.char.name, look: S.char.look, fitting: !!S.char.fitting } : null,
      figurePainted: !!(fig && fig.front && fig.front.classList.contains("on") && fig.front.naturalWidth > 0),
      figureSrc: fig && fig.front ? (fig.front.getAttribute("src") || "") : "",
      figureGhost: !!(fig && fig.front && fig.front.classList.contains("cs-ghost")),
      fittingShown: !!(panel && panel.classList.contains("pk-fitting")),
      status: statusEl ? statusEl.textContent : "",
      eta: etaEl ? etaEl.textContent : "",
      progress: Number(PROG.p.toFixed(3)),
      ringOn: !!(btn && btn.classList.contains("fitting")),
      wearing: wearingEl ? wearingEl.textContent : "",
      verb: verbEl && !verbEl.hidden ? verbEl.textContent : "",
      note: noteEl ? noteEl.textContent : "",
      worn: S.items.filter((g) => g.worn).map((g) => g.name),
    };
  }

  window.Pack = {
    sync, found, haul, open: openPack, close, toggle, wear: wearSelected, pick,
    isOpen: () => S.open,
    items: () => S.items.slice(),
    debug,
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => { build(); render(); });
  } else {
    build();
    render();
  }
})();
