/* ============================================================
   SOMEWHERE // Lobby — splash page controller

   Responsibilities:
     - Render the ambient clock in the header
     - "Start a new run" flow: POST /api/lobby/create, remember the id
       locally, then hand the browser off to /play?session=<id>.
     - "Resume" flow: list recent sessions (server-known + browser-known)
       and let the visitor either click one or paste a code to jump in.

   The lobby maintains its own tiny local index of recently-played sessions
   in localStorage keyed under "somewhere.lobby.recent". Server-known
   sessions still show up even in a brand-new browser; the local index
   only adds "◆" recent-badges to rows we know this browser has touched.
   ============================================================ */

(function () {
  "use strict";

  var LS_RECENT_KEY = "somewhere.lobby.recent";
  var LS_LAST_KEY = "somewhere.lobby.last_session";
  var MAX_RECENT = 12;

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

  // ---------- Resume list ----------

  function renderResumeList(sessions) {
    var host = el("resume-list");
    var empty = el("resume-empty");
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

    // Sort recents to the top; otherwise use the server ordering (last accessed).
    rows.sort(function (a, b) {
      var aR = recents.has(a.session_id) ? 1 : 0;
      var bR = recents.has(b.session_id) ? 1 : 0;
      if (aR !== bR) return bR - aR;
      return (b.last_accessed || "").localeCompare(a.last_accessed || "");
    });

    // Filter out the shared 'default' slot unless the user has explicitly
    // touched it in this browser — it isn't a personal run.
    rows = rows.filter(function (r) {
      if (r.session_id === "default") return recents.has("default");
      return true;
    });

    host.innerHTML = "";
    if (rows.length === 0) {
      var e = document.createElement("div");
      e.className = "resume-empty";
      e.innerHTML = '<span class="resume-empty-glyph" aria-hidden="true">□</span>' +
        '<span>No saved runs yet — start one above and it\'ll show up here.</span>';
      host.appendChild(e);
      return;
    }
    if (empty && empty.parentNode) empty.parentNode.removeChild(empty);

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
      card.href = "/play?session=" + encodeURIComponent(sid);
      card.setAttribute("data-session-id", sid);
      card.setAttribute("title", "Resume '" + sid + "'");
      card.innerHTML =
        '<div>' +
          '<div class="resume-card-name">' + escapeHTML(name) + '</div>' +
          '<div class="resume-card-code">' + escapeHTML(sid) + '</div>' +
        '</div>' +
        '<div class="resume-card-play" aria-hidden="true">▶</div>' +
        '<div class="resume-card-meta">' +
          '<span>' + (turns > 0 ? (turns + ' TURN' + (turns === 1 ? '' : 'S')) : 'FRESH') + '</span>' +
          (when ? '<span class="sep">·</span><span>' + escapeHTML(when) + '</span>' : '') +
          (row._tentative ? '<span class="sep">·</span><span>LOCAL</span>' : '') +
          (!alive ? '<span class="sep">·</span><span>ENDED</span>' : '') +
        '</div>';

      card.addEventListener("click", function (ev) {
        // Let normal navigation take over so opening in a new tab still works,
        // but stamp the local recent-index first so the badge shows next time.
        rememberSession(sid, name);
      });
      host.appendChild(card);
    });
  }

  function loadSessions() {
    fetch("/api/lobby/sessions?limit=25&include_default=false")
      .then(function (res) { return safeJSON(res); })
      .then(function (json) {
        var sessions = (json && json.data && json.data.sessions) || [];
        renderResumeList(sessions);
      })
      .catch(function (err) {
        console.warn("[lobby] failed to list sessions:", err);
        renderResumeList([]);
      });
  }

  // ---------- New instance flow ----------

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
        // Small delay so the transition feels intentional. The redirect will
        // then trigger a full reload into the immersive UI.
        setTimeout(function () {
          boot.done();
          window.location.href = data.play_url || ("/play?session=" + encodeURIComponent(sid));
        }, 550);
      })
      .catch(function (err) {
        boot.done();
        hideBoot();
        showFormError("Could not boot instance: " + (err && err.message ? err.message : err));
      });
  }

  // ---------- Wire up controls ----------

  var startBtn = el("cta-start");
  if (startBtn) {
    startBtn.addEventListener("click", function () {
      showFormError(null);
      // Prefer any values the user already typed into the form below; fall
      // back to a fully auto-generated instance if the fields are empty.
      var nameInput = el("new-run-name");
      var codeInput = el("new-run-code");
      var payload = {};
      var name = (nameInput && nameInput.value ? nameInput.value.trim() : "");
      var code = (codeInput && codeInput.value ? codeInput.value.trim() : "");
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

  var resumeFocusBtn = el("cta-resume-focus");
  if (resumeFocusBtn) {
    resumeFocusBtn.addEventListener("click", function () {
      var panel = el("panel-resume");
      if (panel && panel.scrollIntoView) panel.scrollIntoView({ behavior: "smooth", block: "start" });
      var input = el("resume-code");
      if (input) input.focus({ preventScroll: true });
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

  function joinByCode() {
    var input = el("resume-code");
    if (!input) return;
    var raw = (input.value || "").trim();
    if (!raw) { input.focus(); return; }
    var check = validateCode(raw);
    if (!check.ok) {
      // Reuse the resume input's UI: flash the border briefly.
      input.style.borderColor = "var(--accent-red)";
      setTimeout(function () { input.style.borderColor = ""; }, 900);
      return;
    }
    var sid = check.code;
    rememberSession(sid, "");
    // Probe first so we don't send the user into a 404 experience if the code
    // is stale. If the probe fails, fall through and let /play handle it —
    // the standalone UI will boot the run from scratch (session_context
    // creates missing sessions on demand).
    fetch("/api/lobby/sessions/" + encodeURIComponent(sid))
      .then(function () {})
      .catch(function () {})
      .finally(function () {
        window.location.href = "/play?session=" + encodeURIComponent(sid);
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

  // ---------- Initial load ----------

  loadSessions();
  // Refresh the resume list on window focus so a user who plays a run and
  // clicks the "Lobby" link sees the updated turn count without a hard reload.
  window.addEventListener("focus", loadSessions);

})();
