/* ============================================================
   SOMEWHERE // Lobby — main menu controller

   Deliberately modeled on a familiar game main-menu flow (Minecraft's
   Singleplayer world list, a JRPG save screen): two buttons up front
   ("New Game" / "Continue"), each expanding an inline panel with exactly
   what's needed next. Nothing else to configure before you're playing.

   Responsibilities:
     - Ambient header clock
     - Accordion: only one of the New Game / Continue panels open at a time
     - "New Game": POST /api/lobby/create, remember the id locally, then
       hand the browser off to /play?session=<id>.
     - "Continue": list recent sessions (server-known + browser-known) as
       save-slot rows; also accepts a pasted code to jump straight in.
   ============================================================ */

(function () {
  "use strict";

  // Mark JS as available so the stylesheet can hide .reveal elements only
  // when we can actually animate them in (no-JS visitors see everything).
  document.documentElement.classList.add("js");

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
  function withComp(url) {
    if (!COMP_CODE || !url) return url;
    try {
      var joiner = url.indexOf("?") === -1 ? "?" : "&";
      if (url.indexOf("comp=") !== -1) return url;
      return url + joiner + "comp=" + encodeURIComponent(COMP_CODE);
    } catch (_) { return url; }
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
  // NEW GAME = instant start. No name, no panel, no second tap — just mint a
  // session and go. Naming is available in the optional disclosure below.
  if (startBtn) startBtn.addEventListener("click", function () { bootInstance({}); });
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
      card.href = withComp("/play?session=" + encodeURIComponent(sid));
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
        ? (count === 1 ? "1 saved run" : count + " saved runs")
        : "resume a saved run";
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
    "requesting instance…",
    "allocating world state…",
    "priming the signal…",
    "seeding first frame…",
    "opening the channel…",
    "handing off to /play…",
  ];

  function showBoot(code) {
    var overlay = el("boot-overlay");
    var codeNode = el("boot-code");
    var lineNode = el("boot-line");
    var fill = el("boot-progress-fill");
    if (!overlay) return { advance: function () {}, done: function () {} };
    overlay.hidden = false;
    overlay.setAttribute("aria-hidden", "false");
    if (codeNode) codeNode.textContent = code ? "CODE · " + code : "";
    var i = 0;
    var pct = 5;
    if (lineNode) lineNode.textContent = BOOT_MESSAGES[0];
    if (fill) fill.style.width = pct + "%";
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
        boot.advance("handing off to /play…");
        setTimeout(function () {
          boot.done();
          var dest = data.play_url || ("/play?session=" + encodeURIComponent(sid));
          window.location.href = withComp(dest);
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
      input.style.borderColor = "var(--accent-red)";
      setTimeout(function () { input.style.borderColor = ""; }, 900);
      return;
    }
    var sid = check.code;
    rememberSession(sid, "");
    fetch("/api/lobby/sessions/" + encodeURIComponent(sid))
      .then(function () {})
      .catch(function () {})
      .finally(function () {
        window.location.href = withComp("/play?session=" + encodeURIComponent(sid));
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

  /* ============================================================
     Landing-page interactions (nav, smooth-scroll, scroll-reveal,
     hero CTA -> menu panels). Kept separate from the menu controller
     above so the tested New Game / Continue flow is untouched.
     ============================================================ */

  // ---------- Sticky nav: condense on scroll ----------
  var siteNav = el("siteNav");
  function onScroll() {
    if (!siteNav) return;
    if (window.scrollY > 24) siteNav.classList.add("is-scrolled");
    else siteNav.classList.remove("is-scrolled");
  }
  onScroll();
  window.addEventListener("scroll", onScroll, { passive: true });

  // ---------- Smooth-scroll helper ----------
  function scrollToId(id) {
    var target = document.getElementById(id);
    if (!target) return;
    target.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---------- Open the Continue (resume) panel ----------
  function openContinue() {
    var btn = el("cta-continue");
    if (btn && btn.getAttribute("aria-expanded") !== "true") togglePanel("cta-continue");
    loadSessions();
  }

  // ---------- Wire every [data-scroll] and [data-enter] control ----------
  document.querySelectorAll("[data-scroll], [data-enter]").forEach(function (node) {
    node.addEventListener("click", function (ev) {
      var enter = node.getAttribute("data-enter");
      var href = node.getAttribute("href") || "";

      // NEW GAME from anywhere (hero ENTER, top-nav PLAY) = instant start.
      if (enter === "new") {
        ev.preventDefault();
        bootInstance({});
        return;
      }

      // CONTINUE = drop to the menu and reveal saved runs.
      if (enter === "continue") {
        ev.preventDefault();
        scrollToId("enter");
        setTimeout(openContinue, 460);
        return;
      }

      if (href.charAt(0) === "#") {
        ev.preventDefault();
        scrollToId(href.slice(1));
      }
    });
  });

  // ---------- Scroll-reveal ----------
  var revealNodes = Array.prototype.slice.call(document.querySelectorAll(".reveal"));
  function revealAll() { revealNodes.forEach(function (n) { n.classList.add("is-visible"); }); }
  if ("IntersectionObserver" in window && revealNodes.length) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          io.unobserve(entry.target);
        }
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    revealNodes.forEach(function (n) { io.observe(n); });
    // Safety net: never let content stay hidden. If the observer hasn't fired
    // for something within a few seconds (headless renderers, odd scroll
    // restoration, IO edge cases), reveal everything so the page is never
    // stuck blank below the fold.
    setTimeout(revealAll, 2600);
  } else {
    revealAll();
  }

})();
