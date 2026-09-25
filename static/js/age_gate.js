/* The first-launch 18+ question (Distribution MVP, M5).
 *
 * ABYSS plays on the player's own Gemini or OpenAI key, and both providers'
 * API terms are for adults; the game is horror besides. A packaged build asks
 * once, on the start menu, before anything else can be pressed, and remembers
 * the answer in the player's data folder (/api/consent) — the window runs in
 * private mode, so browser storage would forget it every launch. From source
 * it never asks unless ABYSS_AGE_GATE=1, so the suites and the harness are
 * unaffected.
 */
(function () {
  "use strict";
  const gate = document.getElementById("age-gate");
  if (!gate) return;
  const yes = document.getElementById("age-yes");
  const no = document.getElementById("age-no");

  function open() {
    gate.hidden = false;
    document.body.classList.add("age-gate-open");
    setTimeout(() => { try { yes.focus(); } catch (_) {} }, 50);
  }
  function close() {
    gate.hidden = true;
    document.body.classList.remove("age-gate-open");
  }

  fetch("/api/consent", { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : null))
    .then((c) => { if (c && c.required && !c.confirmed) open(); })
    .catch(() => {});

  yes.addEventListener("click", () => {
    yes.disabled = true;
    fetch("/api/consent", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ adult: true }),
    }).then((r) => { if (r.ok) close(); else yes.disabled = false; })
      .catch(() => { yes.disabled = false; });
  });

  no.addEventListener("click", () => {
    no.disabled = true;
    fetch("/api/shutdown", { method: "POST" }).catch(() => {});
    // Hosted, or the shutdown was refused: at least nothing is playable.
    setTimeout(() => { document.body.innerHTML = ""; }, 1500);
  });

  // Nothing behind the question can be reached from the keyboard either.
  document.addEventListener("keydown", (e) => {
    if (gate.hidden) return;
    if (e.key === "Tab") {
      e.preventDefault();
      (document.activeElement === yes ? no : yes).focus();
    } else if (e.key === "Escape") {
      e.preventDefault();
    }
  }, true);
})();
