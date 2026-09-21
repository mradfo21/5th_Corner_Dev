// ── ACCOUNT ──────────────────────────────────────────────────────────────
// The sheet that opens on the right of the start menu. One surface for the
// two ways GOD is paid for:
//
//   On this machine (the desktop app): your own keys. The sheet shows what
//   they have cost this month (cost_tracker's estimate), the monthly limit,
//   every provider key, and a CUSTOM key — any OpenAI-compatible address and
//   model, which becomes the narrator.
//
//   On a hosted server: a wallet that belongs to this browser (no sign-in).
//   See the balance, ADD MONEY through Stripe Checkout, the limit, and the
//   payments made.
//   The host's keys are not shown and cannot be edited.
//
// It never holds a secret: /api/keys returns presence and a last-four hint.
// standalone.js owns opening and closing (Accounts); this file owns what is
// drawn inside and every request it makes.
(function () {
  "use strict";

  let keys = null;        // GET /api/keys
  let usage = null;       // GET /api/usage
  let view = "home";      // home | add | pay | custom
  let openRow = null;     // the one row expanded inline (a provider id, or "limit")
  let pack = null;        // the pack picked on ADD MONEY
  let busy = false;
  let billingReturn = false;
  let onClose = null;
  let payHost = null;     // the node Stripe's embedded checkout is mounted in
  let embedded = null;    // that checkout, while it is up

  const $ = (id) => document.getElementById(id);

  // ── tiny DOM helper ──────────────────────────────────────────────────
  function h(tag, attrs, kids) {
    const n = document.createElement(tag);
    if (attrs) {
      for (const k of Object.keys(attrs)) {
        const v = attrs[k];
        if (v == null || v === false) continue;
        if (k === "class") n.className = v;
        else if (k === "text") n.textContent = v;
        else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
        else n.setAttribute(k, v === true ? "" : String(v));
      }
    }
    (kids || []).forEach((c) => { if (c) n.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return n;
  }
  const label = (t) => h("div", { class: "acct-label", text: t });

  function usd(n) {
    const v = Number(n);
    return "$" + (Number.isFinite(v) ? v : 0).toFixed(2);
  }
  function shortDate(when) {
    const d = typeof when === "number" ? new Date(when * 1000) : new Date(when);
    if (isNaN(d.getTime())) return "";
    const today = new Date();
    if (d.toDateString() === today.toDateString()) return "TODAY";
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }).toUpperCase();
  }

  // ── what kind of account this is ────────────────────────────────────
  function isLocal() { return !!(keys && keys.editable); }
  function isHosted() {
    if (!usage) return false;
    return !!usage.requires_wallet || (!isLocal() && !!usage.payments_enabled);
  }
  function linked() { return !!(usage && usage.account && usage.account.linked); }
  function capEditable() { return isHosted() ? linked() : isLocal(); }

  // ── messages ─────────────────────────────────────────────────────────
  function setMsg(text, kind) {
    const m = $("acct-msg");
    if (!m) return;
    const t = String(text || "");
    m.textContent = t ? t.charAt(0).toUpperCase() + t.slice(1) : "";
    m.hidden = !text;
    m.classList.toggle("is-error", kind === "error");
    m.classList.toggle("is-ok", kind === "ok");
  }

  async function call(url, opts) {
    const r = await fetch(url, Object.assign({ cache: "no-store" }, opts || {}));
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error((data && (data.details || data.error || data.reason)) || "Something went wrong.");
      err.data = data;
      throw err;
    }
    return data;
  }
  const json = (method, body) => ({
    method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
  });

  // ── loading ──────────────────────────────────────────────────────────
  async function refresh() {
    const [k, u] = await Promise.all([
      call("/api/keys").catch(() => ({ editable: false, providers: [], offline: true })),
      call("/api/usage").catch(() => null),
    ]);
    keys = k;
    if (u) usage = u;
    render();
    return keys;
  }

  async function refreshUsage() {
    try { usage = await call("/api/usage"); render(); } catch (e) { setMsg(e.message, "error"); }
  }

  // ── actions ──────────────────────────────────────────────────────────
  async function guarded(fn) {
    if (busy) return;
    busy = true;
    try { await fn(); } catch (e) { setMsg(e.message, "error"); } finally { busy = false; }
  }

  function saveKey(id, value) {
    return guarded(async () => {
      keys = await call("/api/keys", json("PUT", { id, value }));
      openRow = null;
      setMsg(value ? "Saved." : "Removed.", "ok");
      if (id === "reactor") {
        try { if (window.Renderer && window.Renderer.onReactorKeyChanged) window.Renderer.onReactorKeyChanged(!!value); } catch (_) {}
      }
      render();
      refreshUsage();
    });
  }

  function saveCustom(address, model, value) {
    return guarded(async () => {
      keys = await call("/api/keys/custom", json("PUT", { address, model, value }));
      view = "home";
      setMsg("Custom key saved. The story now comes from " + model + ".", "ok");
      render();
    });
  }

  function removeCustom() {
    return guarded(async () => {
      keys = await call("/api/keys/custom", { method: "DELETE" });
      view = "home";
      setMsg("Custom key removed. The story is back on Gemini.", "ok");
      render();
    });
  }

  function saveCap(raw) {
    return guarded(async () => {
      let cap = null;
      if (raw !== null) {
        const n = Number(String(raw).replace(/[$,\s]/g, ""));
        if (!Number.isFinite(n) || n <= 0) throw new Error("Enter a dollar amount, or choose NONE.");
        cap = n;
      }
      usage = await call("/api/usage", json("PUT", { monthly_cap_usd: cap, on_demand: true }));
      openRow = null;
      setMsg(cap == null ? "No monthly limit." : "Stops at " + usd(cap) + " a month.", "ok");
      render();
    });
  }

  // Stripe.js comes from js.stripe.com on demand (never bundled: PCI), so
  // the desktop app, which never takes payments, never loads it.
  let stripeJs = null;
  function loadStripeJs() {
    if (window.Stripe) return Promise.resolve(window.Stripe);
    if (stripeJs) return stripeJs;
    stripeJs = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "https://js.stripe.com/dahlia/stripe.js";
      s.onload = () => (window.Stripe ? resolve(window.Stripe) : reject(new Error("Stripe did not load.")));
      s.onerror = () => { stripeJs = null; reject(new Error("Couldn't reach Stripe. Check your connection.")); };
      document.head.appendChild(s);
    });
    return stripeJs;
  }

  function destroyCheckout() {
    if (embedded) { try { embedded.destroy(); } catch (_) {} }
    embedded = null;
    payHost = null;
  }

  function checkout(packId) {
    return guarded(async () => {
      setMsg("Opening checkout…", "");
      // Come back to this game, not the default session (BILLING_LIVE_PLAN C4).
      const back = location.pathname + location.search;
      const data = await call("/api/billing/checkout", json("POST", { kind: "pack", pack: packId, return_to: back }));
      if (data.ui_mode === "embedded_page" && data.client_secret && data.publishable_key) {
        // Stripe's checkout, drawn in the sheet. Paying by card finishes
        // here and returns to this page with ?billing=success&cs=…, which
        // redeemReturn() lands exactly as it does after hosted checkout.
        const StripeCtor = await loadStripeJs();
        destroyCheckout();
        view = "pay";
        render();
        const stripe = StripeCtor(data.publishable_key);
        embedded = await stripe.createEmbeddedCheckoutPage({ fetchClientSecret: async () => data.client_secret });
        if (view !== "pay" || !payHost) { destroyCheckout(); return; }
        embedded.mount(payHost);
        setMsg("");
        return;
      }
      if (!data.url) throw new Error("Checkout did not open.");
      window.location.href = data.url;
    });
  }

  // Back from Stripe: ?billing=success&cs=… lands the money.
  async function redeemReturn() {
    let cs = "";
    try {
      const q = new URLSearchParams(location.search);
      const b = q.get("billing");
      if (b !== "success" && b !== "cancel") return;
      billingReturn = true;
      cs = (q.get("cs") || "").trim();
      q.delete("billing");
      q.delete("cs");
      const rest = q.toString();
      history.replaceState(null, "", location.pathname + (rest ? "?" + rest : "") + location.hash);
      if (b === "cancel") { setMsg("Checkout canceled. Nothing was charged.", ""); return; }
    } catch (_) { return; }
    if (!cs) { setMsg("The payment came back without a receipt.", "error"); return; }
    setMsg("Adding your payment…", "");
    try {
      const data = await call("/api/billing/redeem", json("POST", { checkout_session_id: cs }));
      if (data.usage) usage = data.usage;
      else await refreshUsage();
      render();
      setMsg(data.already_redeemed ? "That payment is already in your wallet." : "Money added.", "ok");
    } catch (e) {
      const reason = e.data && e.data.reason;
      if (reason === "processing") {
        // Klarna, bank payments and the like clear after checkout; the
        // webhook adds the money when they do.
        setMsg("Your payment is still clearing. The money lands in your wallet as soon as it does.", "");
      } else if (reason === "unpaid") {
        setMsg("That checkout wasn't paid. Nothing was charged.", "");
      } else {
        setMsg(e.message || "Could not apply the payment.", "error");
      }
    }
  }

  // ── pieces ───────────────────────────────────────────────────────────
  function row(name, value, opts) {
    opts = opts || {};
    return h("button", {
      type: "button",
      class: "acct-row" + (opts.open ? " is-open" : ""),
      "aria-expanded": opts.expandable ? String(!!opts.open) : null,
      onclick: opts.onclick,
    }, [
      h("span", { class: "acct-row-name", text: name }),
      h("span", { class: "acct-row-value" + (opts.tone ? " is-" + opts.tone : ""), text: value }),
    ]);
  }

  function field(name, input) {
    return h("label", { class: "acct-field" }, [h("span", { class: "acct-field-name", text: name }), input]);
  }

  function input(attrs) {
    const n = h("input", Object.assign({ class: "acct-input", autocomplete: "off", spellcheck: "false" }, attrs));
    return n;
  }

  function word(text, onclick, tone) {
    return h("button", { type: "button", class: "acct-word" + (tone ? " is-" + tone : ""), text, onclick });
  }

  function onEnter(node, fn) {
    node.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); fn(); } });
    return node;
  }

  // ── the money block ─────────────────────────────────────────────────
  function moneyBlock() {
    const out = h("section", { class: "acct-section" });
    const hosted = isHosted();
    // cost_tracker's window is the last 30 days — the same one the limit is
    // checked against — so that is what the number says it is.
    const unlimited = hosted && usage.unlimited;
    out.appendChild(label(unlimited ? "WALLET · OWNER" : hosted ? "WALLET" : "LAST 30 DAYS"));
    const amount = hosted ? usage.available_usd : (usage ? usage.spend_usd : 0);
    const top = h("div", { class: "acct-hero" }, [h("div", { class: "acct-big", text: unlimited ? "∞" : usd(amount) })]);
    if (hosted && usage.payments_enabled && !unlimited) {
      top.appendChild(word("ADD MONEY", () => { view = "add"; setMsg(""); render(); }, "go"));
    }
    out.appendChild(top);

    const cap = usage ? usage.monthly_cap_usd : null;
    const spent = usage ? Number(hosted ? usage.billed_usd : usage.spend_usd) || 0 : 0;
    if (cap) {
      const pct = Math.max(0, Math.min(100, (spent / cap) * 100));
      out.appendChild(h("div", { class: "acct-bar" + (usage.over_cap ? " is-over" : ""), "aria-hidden": "true" }, [h("i", { style: "width:" + pct.toFixed(1) + "%" })]));
    }
    out.appendChild(h("p", { class: "acct-note", text: hosted
      ? usd(spent) + " spent this month."
      : "What your own keys cost, at each provider's rates. You're billed by them, not us." }));

    if (capEditable()) {
      const isOpen = openRow === "limit";
      out.appendChild(row("Monthly limit", cap ? "Stops at " + usd(cap) : "None", {
        expandable: true, open: isOpen, onclick: () => { openRow = isOpen ? null : "limit"; render(); },
      }));
      if (isOpen) {
        const amt = onEnter(input({ type: "text", inputmode: "decimal", placeholder: "$30", value: cap ? String(cap) : "", "aria-label": "Monthly limit in dollars" }), () => saveCap(amt.value));
        out.appendChild(h("div", { class: "acct-edit" }, [
          field("$", amt),
          h("div", { class: "acct-actions" }, [word("NONE", () => saveCap(null)), word("SAVE", () => saveCap(amt.value), "go")]),
        ]));
        setTimeout(() => { try { amt.focus(); } catch (_) {} }, 0);
      }
    }
    if (usage && usage.over_cap) {
      out.appendChild(h("p", { class: "acct-note is-warn", text: "The limit is reached — nothing new is generated until you raise it or the month turns." }));
    } else if (hosted && Number(usage.available_usd) <= 0) {
      out.appendChild(h("p", { class: "acct-note is-warn", text: "The wallet is empty. Add money to keep playing." }));
    }
    return out;
  }

  // ── keys (this machine only) ────────────────────────────────────────
  function keyValue(p) {
    if (p.problem) return { text: "Won't work", tone: "warn" };
    if (p.set) return { text: p.hint && p.hint !== "set" ? "••••" + p.hint : "On", tone: "on" };
    if (p.required) return { text: "Needed", tone: "warn" };
    return { text: "Add", tone: "" };
  }

  function keysBlock() {
    const out = h("section", { class: "acct-section" });
    out.appendChild(label("KEYS"));
    const custom = (keys && keys.custom) || {};
    (keys.providers || []).forEach((p) => {
      if (p.id === "openai" && custom.set) return; // the custom key rides the OpenAI slot
      const v = keyValue(p);
      const isOpen = openRow === p.id;
      out.appendChild(row(p.label || p.id, v.text, {
        tone: v.tone, expandable: true, open: isOpen,
        onclick: () => { openRow = isOpen ? null : p.id; setMsg(""); render(); },
      }));
      if (isOpen) {
        const secret = onEnter(input({ type: "password", placeholder: p.set ? "Paste a new key" : "Paste the key", "aria-label": (p.label || p.id) + " key" }), () => saveKey(p.id, secret.value));
        const acts = [word("SAVE", () => saveKey(p.id, secret.value), "go")];
        if (p.set) acts.unshift(word("REMOVE", () => saveKey(p.id, "")));
        out.appendChild(h("div", { class: "acct-edit" }, [
          h("p", { class: "acct-note", text: p.problem || p.blurb || "" }),
          field("KEY", secret),
          h("div", { class: "acct-actions" }, acts),
        ]));
        setTimeout(() => { try { secret.focus(); } catch (_) {} }, 0);
      }
    });
    out.appendChild(row("Custom", custom.set ? custom.model : "Add", {
      tone: custom.set ? "on" : "",
      onclick: () => { view = "custom"; openRow = null; setMsg(""); render(); },
    }));
    return out;
  }

  // ── recent ───────────────────────────────────────────────────────────
  // One receipt per run per day (BILLING_LIVE_PLAN B2 / B6): tap it for
  // what the run used. Hosted: what you were charged. Desktop: what your own
  // keys cost at the provider's rates.
  const PART_NAMES = { text: "Story", image: "Pictures", video: "Live video", voice: "Voice & talk", realtime: "Live" };
  function partLine(p) {
    const n = p.unit === "seconds"
      ? (p.count >= 60 ? Math.round(p.count / 60) + " min" : Math.round(p.count) + " s")
      : Math.round(p.count) + (p.unit === "pictures" ? " pictures" : " calls");
    const amt = isHosted() ? p.charged_usd : p.cost_usd;
    const bits = [n];
    if (p.failed) bits.push(p.failed + " failed, not charged");
    return h("div", { class: "acct-line is-part" }, [
      h("span", { class: "acct-line-when", text: "" }),
      h("span", { class: "acct-line-what", text: (PART_NAMES[p.service] || p.service) + " · " + bits.join(" · ") }),
      h("span", { class: "acct-line-amt", text: usd(amt) }),
    ]);
  }

  function recentBlock() {
    const items = [];
    const hosted = isHosted();
    if (usage && Array.isArray(usage.recent)) {
      usage.recent.forEach((r, i) => items.push({
        id: "run" + i, when: r.when, what: r.kind === "world" ? "World picture" : "Play",
        amt: "−" + usd(hosted ? r.charged_usd : r.cost_usd), dir: "out", parts: r.parts || [],
      }));
    }
    if (hosted && linked() && Array.isArray(usage.payments)) {
      usage.payments.slice(0, 4).forEach((p) => items.push({
        when: p.ts, what: p.kind === "play" ? "Play plan" : "Added money",
        amt: "+" + usd(p.credit_usd != null ? p.credit_usd : (p.amount_cents || 0) / 100), dir: "in",
      }));
    }
    if (!items.length) return null;
    const t = (w) => (typeof w === "number" ? w * 1000 : Date.parse(w)) || 0;
    items.sort((a, b) => t(b.when) - t(a.when));
    const out = h("section", { class: "acct-section" }, [label("RECENT")]);
    items.slice(0, 8).forEach((it) => {
      const open = it.id && openRow === it.id;
      const line = h(it.parts && it.parts.length ? "button" : "div", {
        type: it.parts && it.parts.length ? "button" : null,
        class: "acct-line" + (it.parts && it.parts.length ? " is-tap" : "") + (open ? " is-open" : ""),
        "aria-expanded": it.parts && it.parts.length ? String(!!open) : null,
        onclick: it.parts && it.parts.length ? () => { openRow = open ? null : it.id; render(); } : null,
      }, [
        h("span", { class: "acct-line-when", text: shortDate(typeof it.when === "number" ? it.when : it.when) }),
        h("span", { class: "acct-line-what", text: it.what }),
        h("span", { class: "acct-line-amt" + (it.dir === "in" ? " is-in" : ""), text: it.amt }),
      ]);
      out.appendChild(line);
      if (open) it.parts.forEach((p) => out.appendChild(partLine(p)));
    });
    out.appendChild(row("What things cost", usage && usage.markup > 1 ? "provider cost × " + usage.markup : "provider cost", {
      onclick: () => { try { window.open("/pricing", "_blank", "noopener"); } catch (_) { location.href = "/pricing"; } },
    }));
    return out;
  }

  function accountFoot() {
    if (!isHosted()) return null;
    const email = usage.account && usage.account.email;
    return h("p", { class: "acct-note", text:
      "This wallet lives in this browser. Clearing its cookies loses it — keep the Stripe receipt"
      + (email ? " sent to " + email : "") + " and support can move the balance." });
  }

  // ── views ────────────────────────────────────────────────────────────
  function homeView() {
    const body = [moneyBlock()];
    if (isLocal()) body.push(keysBlock());
    const recent = recentBlock();
    if (recent) body.push(recent);
    const foot = accountFoot();
    if (foot) body.push(foot);
    if (!isLocal() && !isHosted()) {
      body.push(h("p", { class: "acct-note", text: "This server runs on its host's keys. There's nothing to set up here." }));
    }
    return body;
  }

  function addView() {
    const packs = (usage && usage.packs) || [];
    if (!pack) { const u = packs.find((p) => p.usual) || packs[0]; pack = u ? u.id : null; }
    const chosen = packs.find((p) => p.id === pack) || packs[0];
    const out = [];
    out.push(h("p", { class: "acct-note", text: "You have " + usd(usage.available_usd) + "." }));
    out.push(h("div", { class: "acct-amounts", role: "radiogroup", "aria-label": "Amount" },
      packs.map((p) => h("button", {
        type: "button", role: "radio", "aria-checked": String(p.id === (chosen && chosen.id)),
        class: "acct-amount" + (p.id === (chosen && chosen.id) ? " is-on" : ""),
        text: p.display_price || usd(p.price_cents / 100),
        onclick: () => { pack = p.id; render(); },
      }))));
    if (chosen) {
      const credit = Number(chosen.credit_usd) || chosen.price_cents / 100;
      out.push(h("div", { class: "acct-section" }, [
        h("div", { class: "acct-row is-static" }, [h("span", { class: "acct-row-name", text: "Goes into your wallet" }), h("span", { class: "acct-row-value is-on", text: usd(credit) })]),
        h("div", { class: "acct-row is-static" }, [h("span", { class: "acct-row-name", text: "Wallet after" }), h("span", { class: "acct-row-value", text: usd(Number(usage.available_usd || 0) + credit) })]),
      ]));
      out.push(h("button", { type: "button", class: "acct-pay", text: "PAY " + (chosen.display_price || usd(chosen.price_cents / 100)), onclick: () => checkout(chosen.id) }));
    }
    out.push(h("p", { class: "acct-note", text: "Checkout is Stripe's. Each turn costs what the models cost" + (usage.markup > 1 ? ", times " + usage.markup : "") + "." }));
    return out;
  }

  function payView() {
    if (!payHost) payHost = h("div", { class: "acct-checkout", id: "acct-checkout" });
    return [
      payHost,
      h("p", { class: "acct-note", text: "Stripe takes the payment and sends the receipt. Your card never touches our server." }),
    ];
  }

  function customView() {
    const c = (keys && keys.custom) || {};
    const address = input({ type: "url", placeholder: "http://localhost:11434/v1", value: c.address || "", "aria-label": "Address" });
    const model = input({ type: "text", placeholder: "llama3.1", value: c.model || "", "aria-label": "Model" });
    const secret = input({ type: "password", placeholder: c.set ? (c.keyless ? "No key" : "Leave blank to keep it") : "Blank for a local server", "aria-label": "Key" });
    const go = () => saveCustom(address.value, model.value, secret.value);
    [address, model, secret].forEach((n) => onEnter(n, go));
    const acts = [word("SAVE", go, "go")];
    if (c.set) acts.unshift(word("REMOVE", () => removeCustom()));
    setTimeout(() => { try { (c.set ? model : address).focus(); } catch (_) {} }, 0);
    return [
      h("p", { class: "acct-note", text: "Any service that speaks the OpenAI API — a model on this machine, OpenRouter, your own server. It writes the story; pictures and voice keep their own keys." }),
      h("div", { class: "acct-fields" }, [field("ADDRESS", address), field("MODEL", model), field("KEY", secret)]),
      h("div", { class: "acct-actions" }, acts),
      h("p", { class: "acct-note", text: "Stays on this machine." }),
    ];
  }

  function render() {
    const body = $("acct-body");
    if (!body) return;
    if ((view === "add" || view === "pay") && !isHosted()) view = "home";
    if (view !== "pay") destroyCheckout();
    // The checkout is an iframe mid-payment: re-drawing the sheet would
    // reload it, so while it is up the sheet stays as it is.
    else if (payHost && body.contains(payHost)) return;
    if (view === "custom" && !isLocal()) view = "home";
    const titles = { home: "ACCOUNT", add: "ADD MONEY", pay: "PAY", custom: "CUSTOM KEY" };
    const t = $("acct-title");
    if (t) t.textContent = titles[view] || "ACCOUNT";
    const back = $("acct-back");
    if (back) back.hidden = view === "home";
    body.replaceChildren(...(view === "add" ? addView() : view === "pay" ? payView() : view === "custom" ? customView() : homeView()).filter(Boolean));
  }

  function goHome() { view = "home"; openRow = null; setMsg(""); render(); }

  function init(opts) {
    onClose = (opts && opts.onClose) || null;
    const back = $("acct-back");
    if (back) back.addEventListener("click", goHome);
    const close = $("acct-close");
    if (close) close.addEventListener("click", () => { if (onClose) onClose(); });
    const shade = document.querySelector("#account-panel .acct-shade");
    if (shade) shade.addEventListener("click", () => { if (onClose) onClose(); });
    refresh();
    redeemReturn();
  }

  // Escape: step back inside the sheet first, then close it.
  function escape() {
    if (openRow) { openRow = null; render(); return true; }
    if (view === "pay") { view = "add"; setMsg(""); render(); return true; }
    if (view !== "home") { goHome(); return true; }
    return false;
  }

  window.AccountPanel = {
    init, refresh, refreshUsage, setMsg, escape,
    show(v) { view = v === "add" ? "add" : "home"; openRow = null; render(); },
    get billingReturn() { return billingReturn; },
  };
})();
