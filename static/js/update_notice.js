/* UPDATE READY — RESTART on the start menu (updater.py, Distribution MVP M4).
 *
 * An installed build checks for a newer one in the background at launch. This
 * asks /api/update until the answer settles, shows the real version in the
 * build tag, and — only when a newer build is downloaded and waiting — shows
 * one button. Pressing it restarts into the new version through the EXIT path;
 * not pressing it is always fine (Velopack applies it on the next launch).
 * It never appears once a run has started: the start menu is the only place
 * it lives, and it is hidden whenever the menu is.
 */
(function () {
  "use strict";
  const btn = document.getElementById("start-update");
  const tag = document.getElementById("start-build");
  if (!btn) return;
  const SETTLED = new Set(["current", "ready", "not-installed", "error", "disabled"]);
  let tries = 0;

  function show(s) {
    if (tag && s.current) tag.textContent = "Build " + String(s.current).replace(/-dev$/, " dev");
    const ready = s.state === "ready" && s.available;
    btn.hidden = !ready;
    if (ready) btn.title = "Restart into " + s.available + " — your runs, characters and keys stay";
  }

  async function poll() {
    tries += 1;
    let s = null;
    try {
      const r = await fetch("/api/update", { cache: "no-store" });
      if (r.ok) s = await r.json();
    } catch (_) { /* offline or hosted: stay quiet */ }
    if (s) show(s);
    // Checking and downloading take a while; keep asking for ten minutes.
    if ((!s || !SETTLED.has(s.state)) && tries < 120) setTimeout(poll, 5000);
  }

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "RESTARTING…";
    try {
      const r = await fetch("/api/update/apply", { method: "POST" });
      if (!r.ok) throw new Error(String(r.status));
    } catch (_) {
      btn.disabled = false;
      btn.textContent = "UPDATE READY — RESTART";
    }
  });

  poll();
})();
