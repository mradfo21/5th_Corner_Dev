/* ============================================================
   SOMEWHERE // Lobby — web start screen

   Same first beat as the desktop Play / Watch menu: SOMEWHERE, then
   PLAY / CREATE / CONTINUE / ACCOUNT. PLAY mints a session and enters
   the game; CREATE opens the studio; CONTINUE lists saved runs.

   Responsibilities:
     - Ambient clock
     - PLAY: POST /api/lobby/create, then /play?session=<id>&mode=play
     - WATCH: /standalone?mode=watch
     - CONTINUE: saved-run list + join-by-code
   ============================================================ */

(function () {
  "use strict";

  var LS_RECENT_KEY = "somewhere.lobby.recent";
  var LS_LAST_KEY = "somewhere.lobby.last_session";
  var MAX_RECENT = 12;
  // sessionStorage key MUST match the one standalone.js reads in the
  // CoinOp module (`coinop_comp_code`). Picking a comp code up here on
  // the lobby and stashing it under that exact key means that when the
  // lobby navigates to /play?session=<new_id>, the immersive UI will
  // recover the code even if we did not manage to append it to the URL
  // for whatever reason.
  var SS_COMP_KEY = "coinop_comp_code";

  // ---------- Utilities ----------

  function el(id) { return document.getElementById(id); }

  function clamp(n, lo, hi) { return Math.max(lo, Math.min(hi, n)); }

  function readRecent() {
    try {
      var raw = localStorage.getItem(LS_RECENT_KEY);
      if (!raw) return [];
      var parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (_) { return []; }
  }

  function writeRecent(list) {
    try {
      localStorage.setItem(LS_RECENT_KEY, JSON.stringify(list.slice(0, MAX_RECENT)));
    } catch (_) {}
  }

  function rememberSession(sid, name) {
    if (!sid) return;
    var list = readRecent().filter(function (r) { return r && r.session_id !== sid; });
    list.unshift({ session_id: sid, name: name || "", touched_at: new Date().toISOString() });
    writeRecent(list);
    try { localStorage.setItem(LS_LAST_KEY, sid); } catch (_) {}
  }

  function recentSet() {
    var s = new Set();
    readRecent().forEach(function (r) { if (r && r.session_id) s.add(r.session_id); });
    return s;
  }

  function friendlyTime(iso) {
    if (!iso) return "";
    try {
      var d = new Date(iso);
      if (isNaN(d)) return "";
      var diff = (Date.now() - d.getTime()) / 1000;
      if (diff < 60) return "just now";
      if (diff < 3600) return Math.floor(diff / 60) + "m ago";
      if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
      if (diff < 86400 * 30) return Math.floor(diff / 86400) + "d ago";
      return d.toLocaleDateString();
    } catch (_) { return ""; }
  }

  function escapeHTML(str) {
    return String(str == null ? "" : str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // Validate a client-supplied session code before we send it to the server.
  // The engine allows [A-Za-z0-9_-]{1,100}; we mirror that in the UI so bad
  // input is caught locally with a friendly message.
  function validateCode(code) {
    if (!code) return { ok: true, code: "" };
    var trimmed = String(code).trim();
    if (!trimmed) return { ok: true, code: "" };
    if (trimmed.length > 40) return { ok: false, reason: "too long (max 40 chars)" };
    if (!/^[A-Za-z0-9_\-]+$/.test(trimmed)) {
      return { ok: false, reason: "only letters, numbers, hyphens, underscores" };
    }
    if (trimmed.toLowerCase() === "default") {
      return { ok: false, reason: "'default' is reserved — pick a unique code" };
    }
    return { ok: true, code: trimmed };
  }

  function safeJSON(res) {
    return res.json().catch(function () { return null; });
  }

  // ---------- Coin-op comp code (free-play tokens for influencers/QA) ----------
  //
  // The coin-op continue feature (see coinop.py + standalone.js CoinOp)
  // accepts a `?comp=<code>` query parameter that grants free continues
  // for allowlisted codes (COINOP_FREE_PLAY_CODES). Historically the
  // lobby had no idea about this, so a link like /lobby?comp=jane would
  // drop the code the moment the visitor hit "New Game" and the server
  // redirected them to /play?session=<newly-minted-id>.
  //
  // We now hoist that logic up to the lobby: read the code from the URL
  // (or from a prior sessionStorage stash from earlier in this tab),
  // persist it under the SAME key standalone.js reads, strip it from the
  // visible URL for tidiness, and forward it on every navigation into
  // /play. Belt AND suspenders: the URL param is authoritative, and the
  // sessionStorage stash catches any code path we forgot to instrument.
  function readComp() {
    var fromUrl = "";
    try {
      var q = new URLSearchParams(location.search);
      fromUrl = (q.get("comp") || "").trim();
    } catch (_) {}
    if (fromUrl) {
      try { sessionStorage.setItem(SS_COMP_KEY, fromUrl); } catch (_) {}
      try {
        var q2 = new URLSearchParams(location.search);
        q2.delete("comp");
        var rest = q2.toString();
        var clean = location.pathname + (rest ? "?" + rest : "") + location.hash;
        history.replaceState(null, "", clean);
      } catch (_) {}
      return fromUrl;
    }
    try {
      var fromStore = sessionStorage.getItem(SS_COMP_KEY);
      if (fromStore) return String(fromStore).trim();
    } catch (_) {}
    return "";
  }

  var COMP_CODE = readComp();

  // Append ?comp=<code> to any /play (or /live, /standalone) URL we hand
  // to the browser. Idempotent: if the URL already has comp we leave it
  // alone. Used by New Game, resume cards, and join-by-code so ALL exits
  // from the lobby preserve the token.
  function withQuery(url, key, value) {
    if (!url || !key || !value) return url;
    try {
      if (url.indexOf(key + "=") !== -1) return url;
      var joiner = url.indexOf("?") === -1 ? "?" : "&";
      return url + joiner + encodeURIComponent(key) + "=" + encodeURIComponent(value);
    } catch (_) { return url; }
  }

  function withComp(url) {
    return withQuery(url, "comp", COMP_CODE);
  }

  function playUrl(url) {
    return withComp(withQuery(url, "mode", "play"));
  }

  function showCompBadge() {
    if (!COMP_CODE) return;
    var badge = el("comp-badge");
    if (!badge) return;
    // Only surface the code itself when it's short — long codes would
    // wreck the HUD layout and are usually opaque to the human anyway.
    var display = COMP_CODE.length <= 16 ? COMP_CODE : "ON";
    badge.textContent = "COMP · " + display;
    badge.hidden = false;
  }
  showCompBadge();

  // ---------- Ambient clock ----------

  function tickClock() {
    var node = el("lobby-clock");
    if (!node) return;
    var now = new Date();
    var hh = String(now.getHours()).padStart(2, "0");
    var mm = String(now.getMinutes()).padStart(2, "0");
    var ss = String(now.getSeconds()).padStart(2, "0");
    node.textContent = hh + ":" + mm + ":" + ss;
  }
  tickClock();
  setInterval(tickClock, 1000);

  // ---------- Continue panel (New Game starts instantly, no panel) ----------

  var panels = {
    "cta-continue": "panel-resume",
  };

  function closeAllPanels(exceptId) {
    Object.keys(panels).forEach(function (btnId) {
      if (btnId === exceptId) return;
      var btn = el(btnId);
      var panel = el(panels[btnId]);
      if (btn) btn.setAttribute("aria-expanded", "false");
      if (panel) panel.hidden = true;
    });
  }

  function togglePanel(btnId) {
    var btn = el(btnId);
    var panel = el(panels[btnId]);
    if (!btn || !panel) return;
    var isOpen = btn.getAttribute("aria-expanded") === "true";
    if (isOpen) {
      btn.setAttribute("aria-expanded", "false");
      panel.hidden = true;
      return;
    }
    closeAllPanels(btnId);
    btn.setAttribute("aria-expanded", "true");
    panel.hidden = false;
    var hint = el("continue-hint");
    if (hint && btnId === "cta-continue") hint.hidden = !hint.textContent;
    // Focus the first meaningful field so keyboard/quick players can go
    // straight into typing without an extra click — mirrors how console
    // menus auto-focus the first list item on expand.
    var firstField = panel.querySelector("input[type='text']");
    if (firstField) {
      try { firstField.focus({ preventScroll: true }); } catch (_) { firstField.focus(); }
    }
  }

  var startBtn = el("cta-start");
  var continueBtn = el("cta-continue");
  var createBtn = el("start-create");
  var accountBtn = el("start-account");
  if (startBtn) startBtn.addEventListener("click", function () { bootInstance({}); });
  if (createBtn) {
    createBtn.addEventListener("click", function () {
      window.location.href = withComp("/standalone?mode=create");
    });
  }
  if (accountBtn) {
    accountBtn.addEventListener("click", function () {
      window.location.href = withComp("/standalone?account=1");
    });
  }
  if (continueBtn) {
    continueBtn.addEventListener("click", function () {
      togglePanel("cta-continue");
      if (continueBtn.getAttribute("aria-expanded") === "true") loadSessions();
    });
  }

  // ---------- Advanced (custom code) disclosure ----------

  var advToggle = el("advanced-toggle");
  var advFields = el("advanced-fields");
  if (advToggle && advFields) {
    advToggle.addEventListener("click", function () {
      var isOpen = advToggle.getAttribute("aria-expanded") === "true";
      advToggle.setAttribute("aria-expanded", isOpen ? "false" : "true");
      advFields.hidden = isOpen;
      if (!isOpen) {
        var input = advFields.querySelector("input[type='text']");
        if (input) input.focus();
      }
    });
  }

  // ---------- Resume / save-slot list ----------

  function renderResumeList(sessions) {
    var host = el("resume-list");
    if (!host) return;

    var recents = recentSet();

    // Merge in any locally-known sessions the server didn't return (e.g. the
    // server was restarted or the session file was cleaned up but the user
    // still has the code). We show them tentatively so the visitor can try.
    var seen = new Set();
    var rows = (sessions || []).slice();
    rows.forEach(function (r) { if (r && r.session_id) seen.add(r.session_id); });
    readRecent().forEach(function (r) {
      if (r && r.session_id && !seen.has(r.session_id)) {
        rows.push({
          session_id: r.session_id,
          name: r.name || "Run " + r.session_id,
          turn_count: 0,
          player_alive: true,
          last_accessed: r.touched_at,
          _tentative: true,
        });
        seen.add(r.session_id);
      }
    });

    rows.sort(function (a, b) {
      var aR = recents.has(a.session_id) ? 1 : 0;
      var bR = recents.has(b.session_id) ? 1 : 0;
      if (aR !== bR) return bR - aR;
      return (b.last_accessed || "").localeCompare(a.last_accessed || "");
    });

    // The shared 'default' slot isn't a personal run — only surface it if
    // this browser has explicitly touched it.
    rows = rows.filter(function (r) {
      if (r.session_id === "default") return recents.has("default");
      return true;
    });

    updateContinueBadge(rows.length);

    host.innerHTML = "";
    if (rows.length === 0) {
      var e = document.createElement("div");
      e.className = "resume-empty";
      e.innerHTML = "<span>No saved runs yet — start a New Game and it'll show up here.</span>";
      host.appendChild(e);
      return;
    }

    rows.forEach(function (row) {
      var sid = row.session_id;
      var name = row.name || ("Run " + sid);
      var turns = Number(row.turn_count || 0);
      var alive = row.player_alive !== false;
      var when = friendlyTime(row.last_accessed);
      var classes = ["resume-card"];
      if (!alive) classes.push("is-dead");
      if (recents.has(sid)) classes.push("is-recent");

      var card = document.createElement("a");
      card.className = classes.join(" ");
      card.href = playUrl("/play?session=" + encodeURIComponent(sid));
      card.setAttribute("data-session-id", sid);
      card.setAttribute("title", "Continue '" + sid + "'");
      card.innerHTML =
        '<div class="resume-card-main">' +
          '<div class="resume-card-name">' + escapeHTML(name) + '</div>' +
          '<div class="resume-card-meta">' +
            '<span>' + (turns > 0 ? (turns + ' turn' + (turns === 1 ? '' : 's')) : 'fresh') + '</span>' +
            (when ? '<span class="sep">·</span><span>' + escapeHTML(when) + '</span>' : '') +
            (!alive ? '<span class="sep">·</span><span>ended</span>' : '') +
          '</div>' +
        '</div>' +
        '<div class="resume-card-play" aria-hidden="true">▶</div>';

      card.addEventListener("click", function () {
        rememberSession(sid, name);
      });
      host.appendChild(card);
    });
  }

  function updateContinueBadge(count) {
    var badge = el("continue-badge");
    var hint = el("continue-hint");
    if (badge) {
      if (count > 0) {
        badge.hidden = false;
        badge.textContent = String(count);
      } else {
        badge.hidden = true;
      }
    }
    if (hint) {
      hint.textContent = count > 0
        ? (count === 1 ? "1 run waiting" : count + " runs waiting")
        : "";
      var cont = el("cta-continue");
      hint.hidden = !hint.textContent || !cont || cont.getAttribute("aria-expanded") !== "true";
    }
  }

  var sessionsLoaded = false;
  function loadSessions() {
    fetch("/api/lobby/sessions?limit=25&include_default=false")
      .then(function (res) { return safeJSON(res); })
      .then(function (json) {
        var sessions = (json && json.data && json.data.sessions) || [];
        sessionsLoaded = true;
        renderResumeList(sessions);
      })
      .catch(function (err) {
        console.warn("[lobby] failed to list sessions:", err);
        renderResumeList([]);
      });
  }

  // Pre-fetch quietly on load (without opening the panel) so the Continue
  // button's badge count is accurate the moment the page renders — like a
  // console menu that already knows how many save files exist.
  loadSessions();
  window.addEventListener("focus", loadSessions);

  // ---------- New game flow ----------

  var BOOT_MESSAGES = [
    "finding the road…",
    "waking the world…",
    "letting your eyes adjust…",
    "the horizon comes into focus…",
    "you're somewhere now…",
    "step forward…",
  ];

  // World-flavoured loading tips (classic loading-screen tooltip). Kept about
  // the world and staying alive, never about the tech behind it.
  var BOOT_TIPS = [
    "The road never runs the same way twice.",
    "Fifteen seconds to decide — hesitation is a choice too.",
    "The world remembers what you did.",
    "No map out here. Trust your gut and keep moving.",
    "Stuck? Type your own way out — the world will answer.",
    "Every place you find is somewhere you've never been.",
    "Something out here is watching. Don't linger.",
    "One life. No way back. Make it count.",
  ];

  function showBoot(code) {
    var overlay = el("boot-overlay");
    var codeNode = el("boot-code");
    var lineNode = el("boot-line");
    var fill = el("boot-progress-fill");
    var tipNode = el("boot-tip-text");
    if (!overlay) return { advance: function () {}, done: function () {} };
    overlay.hidden = false;
    overlay.setAttribute("aria-hidden", "false");
    if (codeNode) codeNode.textContent = code ? "CODE · " + code : "";
    var i = 0;
    var pct = 5;
    if (lineNode) lineNode.textContent = BOOT_MESSAGES[0];
    if (fill) fill.style.width = pct + "%";

    // Rotate a loading tip, starting from a random one so it feels fresh.
    var tipIx = Math.floor(Math.random() * BOOT_TIPS.length);
    if (tipNode) tipNode.textContent = BOOT_TIPS[tipIx];
    var tipInterval = setInterval(function () {
      if (!tipNode) return;
      tipNode.classList.add("is-swapping");
      setTimeout(function () {
        tipIx = (tipIx + 1) % BOOT_TIPS.length;
        tipNode.textContent = BOOT_TIPS[tipIx];
        tipNode.classList.remove("is-swapping");
      }, 400);
    }, 3200);

    var interval = setInterval(function () {
      i = (i + 1) % BOOT_MESSAGES.length;
      if (lineNode) lineNode.textContent = BOOT_MESSAGES[i];
      pct = clamp(pct + 12 + Math.random() * 10, 5, 92);
      if (fill) fill.style.width = pct + "%";
    }, 380);
    return {
      advance: function (msg) {
        if (lineNode && msg) lineNode.textContent = msg;
      },
      done: function () {
        clearInterval(interval);
        clearInterval(tipInterval);
        if (fill) fill.style.width = "100%";
      },
    };
  }

  function hideBoot() {
    var overlay = el("boot-overlay");
    if (overlay) {
      overlay.hidden = true;
      overlay.setAttribute("aria-hidden", "true");
    }
  }

  function showFormError(msg) {
    var e = el("new-run-error");
    if (!e) return;
    if (!msg) { e.hidden = true; e.textContent = ""; return; }
    e.hidden = false;
    e.textContent = msg;
  }

  function bootInstance(payload) {
    var boot = showBoot(payload && payload.session_id);
    return fetch("/api/lobby/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    })
      .then(function (res) { return safeJSON(res).then(function (j) { return { ok: res.ok, json: j }; }); })
      .then(function (r) {
        if (!r.ok || !r.json || !r.json.data) {
          throw new Error((r.json && r.json.error) || "Server rejected the request");
        }
        var data = r.json.data;
        var sid = data.session_id;
        rememberSession(sid, (payload && payload.name) || "");
        boot.advance("step forward…");
        setTimeout(function () {
          boot.done();
          var dest = data.play_url || ("/play?session=" + encodeURIComponent(sid));
          window.location.href = playUrl(dest);
        }, 550);
      })
      .catch(function (err) {
        boot.done();
        hideBoot();
        showFormError("Could not start: " + (err && err.message ? err.message : err));
      });
  }

  var form = el("new-run-form");
  if (form) {
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      showFormError(null);
      var nameInput = el("new-run-name");
      var codeInput = el("new-run-code");
      var payload = {};
      var name = nameInput && nameInput.value ? nameInput.value.trim() : "";
      var code = codeInput && codeInput.value ? codeInput.value.trim() : "";
      var codeCheck = validateCode(code);
      if (!codeCheck.ok) {
        showFormError("Custom code: " + codeCheck.reason);
        if (codeInput) codeInput.focus();
        return;
      }
      if (name) payload.name = name;
      if (codeCheck.code) payload.session_id = codeCheck.code;
      bootInstance(payload);
    });
  }

  // ---------- Join by code (inside the Continue panel) ----------

  function joinByCode() {
    var input = el("resume-code");
    if (!input) return;
    var raw = (input.value || "").trim();
    if (!raw) { input.focus(); return; }
    var check = validateCode(raw);
    if (!check.ok) {
      input.style.borderBottomColor = "#ff8f7d";
      setTimeout(function () { input.style.borderBottomColor = ""; }, 900);
      return;
    }
    var sid = check.code;
    rememberSession(sid, "");
    fetch("/api/lobby/sessions/" + encodeURIComponent(sid))
      .then(function () {})
      .catch(function () {})
      .finally(function () {
        window.location.href = playUrl("/play?session=" + encodeURIComponent(sid));
      });
  }

  var joinBtn = el("resume-code-go");
  if (joinBtn) joinBtn.addEventListener("click", joinByCode);
  var resumeCodeInput = el("resume-code");
  if (resumeCodeInput) {
    resumeCodeInput.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { ev.preventDefault(); joinByCode(); }
    });
  }

  // Last-run footage is the start-menu wallpaper. SOMEWHERE sits on top.
  // Quiet if there is nothing to play; never generates frames.
  var BrandFill = (function () {
    var STILL_MS = 2400;
    var VIDEO_RATE = 0.72;
    var MAX_FRAMES = 24;
    var playFrames = [];
    var watchFrames = [];
    var watchVideoUrl = "";
    var peekMode = null;
    var cycleTimer = null;
    var cycleIdx = 0;
    var cycleOnB = false;
    var activeVideo = "";
    var videoRaf = 0;

    function reduceMotion() {
      try { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; }
      catch (_) { return false; }
    }

    function fileUrl(rel) {
      if (!rel || typeof rel !== "string") return "";
      if (/^https?:\/\//i.test(rel) || rel.charAt(0) === "/") return rel;
      return "/api/render/file/" + rel;
    }

    function decode(url) {
      return new Promise(function (resolve) {
        if (!url) return resolve(null);
        var img = new Image();
        img.decoding = "async";
        img.onload = function () { resolve(url); };
        img.onerror = function () { resolve(null); };
        img.src = url;
      });
    }

    function decodeList(urls) {
      return Promise.all((urls || []).slice(0, MAX_FRAMES).map(decode))
        .then(function (got) { return got.filter(Boolean); });
    }

    function brand() { return el("start-brand") || document.querySelector("#start-menu .start-brand"); }
    function signal() { return el("start-signal"); }
    function imgA() { return el("start-signal-img"); }
    function imgB() { return el("start-signal-img-b"); }
    function video() { return el("start-signal-video"); }
    function canvas() { return el("start-signal-canvas"); }

    function ensureDom() {
      var menu = el("start-menu");
      var b = brand();
      if (!menu || !b) return;
      if (!b.id) b.id = "start-brand";
      var s = signal();
      if (s && s.parentElement !== menu) menu.insertBefore(s, menu.firstChild);
      if (!signal()) {
        var host = document.createElement("div");
        host.id = "start-signal";
        host.className = "start-signal";
        host.setAttribute("aria-hidden", "true");
        host.innerHTML =
          '<video id="start-signal-video" class="start-signal-video" muted loop playsinline preload="none"></video>' +
          '<canvas id="start-signal-canvas" class="start-signal-canvas"></canvas>' +
          '<div id="start-signal-img" class="start-signal-img"></div>' +
          '<div id="start-signal-img-b" class="start-signal-img"></div>';
        menu.insertBefore(host, menu.firstChild);
      }
      if (b.querySelector(".start-brand-knockout")) {
        var word = ((b.querySelector(".start-brand-type") || b).textContent || "").trim() || "ABYSS";
        b.innerHTML = '<span class="start-brand-type"></span>';
        var type = b.querySelector(".start-brand-type");
        if (type) type.textContent = word;
      }
    }

    function markMedia(on) {
      var menu = el("start-menu");
      var b = brand();
      var s = signal();
      if (menu) menu.classList.toggle("has-media", !!on);
      if (b) b.classList.toggle("has-media", !!on);
      if (s) s.classList.toggle("has-img", !!on);
    }

    function paintLayer(node, url) {
      if (!node) return;
      node.style.backgroundImage = url ? 'url("' + url + '")' : "";
    }

    function stopPump() {
      if (videoRaf) { cancelAnimationFrame(videoRaf); videoRaf = 0; }
    }

    function pumpCanvas() {
      videoRaf = 0;
      var v = video();
      var c = canvas();
      var s = signal();
      if (!v || !c || !s || !s.classList.contains("has-video")) return;
      var box = s.getBoundingClientRect();
      var dpr = Math.min(2, window.devicePixelRatio || 1);
      var w = Math.max(1, Math.round(box.width * dpr));
      var h = Math.max(1, Math.round(box.height * dpr));
      if (c.width !== w) c.width = w;
      if (c.height !== h) c.height = h;
      var ctx = c.getContext("2d");
      if (ctx && v.readyState >= 2 && v.videoWidth) {
        var vw = v.videoWidth, vh = v.videoHeight;
        var scale = Math.max(w / vw, h / vh);
        var dw = vw * scale, dh = vh * scale;
        ctx.drawImage(v, (w - dw) / 2, (h - dh) / 2, dw, dh);
      }
      videoRaf = requestAnimationFrame(pumpCanvas);
    }

    function stopVideo() {
      var v = video();
      var s = signal();
      stopPump();
      if (!v) return;
      try { v.pause(); } catch (_) {}
      if (s) s.classList.remove("has-video");
      if (v.getAttribute("src")) {
        try { v.removeAttribute("src"); v.load(); } catch (_) {}
      }
      activeVideo = "";
    }

    function stopCycle() {
      if (cycleTimer) { clearInterval(cycleTimer); cycleTimer = null; }
      cycleIdx = 0;
    }

    function showStill(url, instant) {
      var a = imgA();
      var b = imgB();
      if (!a) return;
      if (!url) {
        paintLayer(a, "");
        paintLayer(b, "");
        a.classList.remove("is-on");
        if (b) b.classList.remove("is-on");
        markMedia(false);
        return;
      }
      markMedia(true);
      var incoming = cycleOnB ? a : b;
      var outgoing = cycleOnB ? b : a;
      if (!b || instant || !outgoing || !outgoing.classList.contains("is-on")) {
        paintLayer(a, url);
        a.classList.add("is-on");
        if (b) { b.classList.remove("is-on"); paintLayer(b, ""); }
        cycleOnB = false;
        return;
      }
      paintLayer(incoming, url);
      incoming.classList.add("is-on");
      outgoing.classList.remove("is-on");
      cycleOnB = !cycleOnB;
    }

    function startCycle(frames) {
      stopCycle();
      var list = (frames || []).filter(Boolean);
      if (!list.length) { showStill("", true); return; }
      cycleIdx = 0;
      showStill(list[0], true);
      if (list.length < 2 || reduceMotion()) return;
      cycleTimer = setInterval(function () {
        cycleIdx = (cycleIdx + 1) % list.length;
        showStill(list[cycleIdx], false);
      }, STILL_MS);
    }

    function startVideo(url) {
      var v = video();
      var s = signal();
      if (!v || !url || reduceMotion()) return false;
      stopCycle();
      if (activeVideo === url && s && s.classList.contains("has-video")) {
        try { v.play().catch(function () {}); } catch (_) {}
        markMedia(true);
        return true;
      }
      activeVideo = url;
      v.src = url;
      try { v.playbackRate = VIDEO_RATE; } catch (_) {}
      if (s) s.classList.add("has-video");
      markMedia(true);
      stopPump();
      var go = v.play();
      if (go && go.catch) {
        go.catch(function () {
          if (s) s.classList.remove("has-video");
          stopPump();
          activeVideo = "";
          startCycle(watchFrames.length ? watchFrames : playFrames);
        });
      }
      return true;
    }

    function sourceFor(mode) {
      var preferWatch = mode === "watch" || (!mode && (watchVideoUrl || watchFrames.length));
      if (preferWatch) {
        return { video: watchVideoUrl, frames: watchFrames.length ? watchFrames : playFrames };
      }
      return { video: "", frames: playFrames.length ? playFrames : watchFrames };
    }

    function apply() {
      var src = sourceFor(peekMode);
      if (src.video && startVideo(src.video)) return;
      if (src.frames && src.frames.length) {
        stopVideo();
        startCycle(src.frames);
        return;
      }
      stopVideo();
      startCycle([]);
    }

    function peek(mode) {
      peekMode = mode || null;
      document.body.classList.toggle("signal-peek-play", peekMode === "play");
      document.body.classList.toggle("signal-peek-watch", peekMode === "watch");
      apply();
    }

    function warm() {
      try { ensureDom(); } catch (_) {}
      Promise.all([
        fetch("/api/status").then(function (r) { return r.ok ? r.json() : {}; }).catch(function () { return {}; }),
        fetch("/api/render/history").then(function (r) { return r.ok ? r.json() : { renders: [] }; }).catch(function () { return { renders: [] }; }),
        fetch("/api/tape").then(function (r) { return r.ok ? r.json() : { frames: [] }; }).catch(function () { return { frames: [] }; }),
      ]).then(function (pack) {
        var status = pack[0] || {};
        var hist = pack[1] || {};
        var tape = pack[2] || {};
        var still = status.current_image_url || status.setting_plate_url || "";
        var tapeList = (tape.frames || []).filter(function (u) { return typeof u === "string" && u; });
        var runs = hist.renders || [];
        var latest = null;
        for (var i = 0; i < runs.length; i++) {
          var run = runs[i];
          if (run && (run.video || run.gif || run.thumbnail || (run.frames && run.frames.length))) {
            latest = run;
            break;
          }
        }
        watchVideoUrl = latest && latest.video ? fileUrl(latest.video) : "";
        var gif = latest ? fileUrl(latest.gif || "") : "";
        var thumb = latest && latest.thumbnail ? fileUrl(latest.thumbnail) : "";
        var wFrames = [];
        if (latest && latest.frames) {
          latest.frames.forEach(function (f) { var u = fileUrl(f); if (u) wFrames.push(u); });
        }
        if (gif && !watchVideoUrl) wFrames = [gif];
        else if (!wFrames.length && thumb) wFrames = [thumb];
        return Promise.all([decode(still), decodeList(tapeList), decodeList(wFrames)]);
      }).then(function (got) {
        var okStill = got[0];
        var okTape = got[1] || [];
        var okWatch = got[2] || [];
        playFrames = okTape.length ? okTape : (okStill ? [okStill] : []);
        watchFrames = okWatch;
        apply();
      }).catch(function () {});
    }

    function bindPeek(node, mode) {
      if (!node) return;
      node.addEventListener("pointerenter", function () { peek(mode); });
      node.addEventListener("pointerleave", function () { peek(null); });
      node.addEventListener("focus", function () { peek(mode); });
      node.addEventListener("blur", function () { peek(null); });
    }

    function init() {
      bindPeek(el("cta-start"), "play");
      bindPeek(el("start-watch"), "watch");
      try { warm(); } catch (_) {}
    }

    return { init: init };
  })();

  function revealStart() {
    document.body.classList.add("start-arrived");
    var menu = el("start-menu");
    if (menu) menu.setAttribute("aria-busy", "false");
  }

  (function scheduleStartArrive() {
    var reduce = false;
    try { reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches; }
    catch (_) {}
    if (reduce) { revealStart(); return; }
    var t0 = performance.now();
    var go = function () {
      var wait = Math.max(0, 1600 - (performance.now() - t0));
      setTimeout(revealStart, wait);
    };
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(go).catch(go);
    else go();
  })();

  try { BrandFill.init(); } catch (_) {}

})();
