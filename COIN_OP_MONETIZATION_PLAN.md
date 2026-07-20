# 🎰 COIN-OP MONETIZATION PLAN
### "Insert Coin to Continue" — an 80s-arcade micro-payment system for SOMEWHERE

**Status:** Proposal / research plan (not yet implemented)
**Author:** cloud agent research pass
**Related code:** `engine.py` (death detection), `templates/standalone.html` (`#death-overlay`), `static/js/standalone.js` (`enterGameOver`), `api.py` (Flask routes)

---

## 1. The pitch, in one paragraph

When the player dies, instead of only offering "▶ RESTART SIMULATION" we also offer, tastefully, **"▶ INSERT COIN TO CONTINUE"** — a small pixel-CRT widget with a coin-slot, a countdown, and a one-tap payment. Pay a small amount, get revived on the same run with the same inventory and world state, minus a small penalty. The widget is a self-contained drop-in (`<div data-somewhere-continue>` + one `<script>` tag) so we can also place it in the main site, the lobby, and anywhere else. It uses Stripe under the hood — Payment Element + Express Checkout Element (Apple Pay / Google Pay / Link) for one-tap, with an arcade-native **credit-pack ("roll of quarters")** model so the economics work at the small dollar amounts we care about.

---

## 2. Why "coin-op" and not "subscription"

- **Emotional model matches the game.** SOMEWHERE is a survival-horror run-based experience with permadeath (`engine.py:5104` — "DEATH: single mechanism"). Every run ends; every run is finite. That is *exactly* the shape of "insert coin to continue" — an arcade cabinet with a coin slot on the side.
- **Tasteful economics.** A subscription forces a monthly relationship on someone who might play once a month. A per-continue micro-payment is a bite-sized, self-selected commitment that only fires at a *moment of maximum motivation* (the player just lost).
- **Aligns with cost curve.** Per `README.md`, a 30-turn run costs ~\$1.25 in API. A revive that grants ~10 more turns costs us ~\$0.40 in variable cost — a \$0.99 revive is clean gross-margin positive on the marginal turns, and the fixed-cost portion of that first \$1.25 is already sunk.
- **Discoverable, non-coercive.** The widget only appears at death (and optionally as a soft top-up in a menu). We never gate mid-run gameplay behind payment. That preserves goodwill and keeps us clear of "dark-pattern free-to-play" reputational risk.

---

## 3. The core economic decision: **per-continue** vs **credit pack** vs **hybrid**

### The Stripe reality that forces this decision

Stripe's US minimum charge is **\$0.50** and fee structure is **2.9% + \$0.30**. That means:

| Charge  | Stripe fee | We keep | Effective fee |
|--------:|-----------:|--------:|--------------:|
| \$0.50  | \$0.31     | \$0.19  | **62.9%**     |
| \$0.99  | \$0.33     | \$0.66  | 33.3%         |
| \$1.99  | \$0.36     | \$1.63  | 18.1%         |
| \$4.99  | \$0.44     | \$4.55  | 8.8%          |
| \$9.99  | \$0.59     | \$9.40  | 5.9%          |

You cannot make a \$0.25 arcade quarter charge on Stripe. Even a \$0.99 single-continue leaks a third of revenue to fees. **The solution is the exact primitive an 80s arcade already used: sell tokens in packs, spend them one at a time.** That gives the player the emotional experience of a "quarter drop" while we only pay Stripe fees on the pack purchase.

### Recommendation: **Hybrid, credit-first**

- **Default price surface at death:**
  - **Roll of Quarters** — \$4.99 → **5 continues** (\~\$1.00/continue perceived, ~\$0.91 real)
  - **Handful of Tokens** — \$9.99 → **12 continues** (bonus 2, drives ARPPU) [*best-value badge*]
  - **Full Bucket** — \$19.99 → **30 continues** (best deal, whales) [*optional, hidden behind "more options"*]
- **Fallback single continue** — \$1.99 → 1 continue, shown only when player has 0 credits AND has previously bought a pack. Removes the "I don't want to commit \$4.99" objection on repeat.
- **First-timer offer** — first pack ever gets +1 bonus continue and a coin-drop animation with the receipt.
- **Credits never expire.** They persist across runs, sessions, and devices (tied to the player identifier — see §6).

### Second-order pricing knobs

- **Escalating cost within a single run** (the arcade cabinet feel): 1st continue = 1 token, 2nd = 2 tokens, 3rd = 3 tokens. Caps a "pay to invincibility" spiral, and makes each save feel harder-won.
- **Discount at ceremony moments:** if death happens on the intro or turn 1 (unfair-feeling deaths), the continue is free. That is a *goodwill* rule; it is worth much more than it costs.
- **Currency-localized packs.** Show \$/€/£/¥ from the browser locale; Stripe Checkout can auto-present the right currency.

---

## 4. Stripe architecture (which primitives, and why)

### The primitives we should use

| Primitive | Purpose | Why |
|---|---|---|
| **Stripe Payment Element** (embedded) | The in-widget card form | Renders inside our own UI, keeps us PCI SAQ-A (never touches card data). Beautiful, themeable to CRT. |
| **Express Checkout Element** | Apple Pay / Google Pay / Link / PayPal buttons | Literally one-tap on mobile. Highest-converting surface. Must be first-in-tab-order. |
| **CustomerSession** with `payment_method_save=enabled` | Save-card checkbox and returning-user list | After 1st purchase, returning users see their saved card and buy in one click. This is the "insert coin, second time is instant" experience. |
| **PaymentIntent** (`automatic_payment_methods={enabled: true}`) | The charge itself | Stripe's recommended modern primitive; handles 3DS, SCA, off-session flows. |
| **Webhook** on `payment_intent.succeeded` | The **only** trigger that grants credits | Client cannot be trusted; the server credits the player's balance only when Stripe confirms funds. |
| **Stripe Tax** | VAT / sales-tax handling | One toggle. Do not build tax logic ourselves. |
| **Stripe Radar** | Fraud rules | On by default; add a rule to block > N attempts per session per hour. |
| **Idempotency-Key** header | On every PaymentIntent create | Prevents double-charges from retries / double-clicks / poll-storms. |

### What we should NOT use

- **Stripe Checkout (hosted redirect page).** Too heavy for a "coin drop" — you leave our beautiful CRT UI, land on a Stripe page, come back. Kills the ceremony. (Fine as a fallback for the < 1% of browsers where Payment Element misbehaves.)
- **Stripe Subscriptions / Billing.** Wrong business model here.
- **In-app purchase via Apple / Google stores.** We are a web app + Discord bot. As of the May 2025 Apple ruling US developers may link out to web payment — but we don't ship in any app store today, so this is a non-issue. If we ever ship a native wrapper, we'd revisit.

### End-to-end flow — first purchase

```
Player dies
    → Client (standalone.js) sees `game_over` feed item
    → Instead of showing bare "RESTART", inject the <continue-coin-slot> widget
    → Widget calls POST /api/coinop/quote  { pack: "roll" }
        ← server returns { pack, price_cents, currency, client_secret,
                           customer_session_client_secret,
                           idempotency_hint }
    → Widget mounts Express Checkout Element + Payment Element with client_secret
    → Player taps Apple Pay (or types card once)
    → Stripe confirms → client sees paymentIntent.status === "succeeded"
    → Client optimistically shows "COIN ACCEPTED" + coin-drop sound
    → In parallel, Stripe → POST our /webhook/stripe → server credits balance
    → Client polls (or SSE) GET /api/coinop/balance → sees +5 credits
    → Client calls POST /api/coinop/spend  { on: "continue", run_id }
    → Server decrements by 1 (or by N per §3 escalation), grants revive token
    → Client calls POST /api/reset?revive=<token>   (see §5)
```

### End-to-end flow — returning purchase (this is the magic moment)

```
Player dies
    → Widget calls GET /api/coinop/balance
        ← { credits: 3, has_saved_pm: true }
    → Widget shows: [ INSERT TOKEN (3 left) ]  — no payment UI at all
    → Player taps once → POST /api/coinop/spend → revive
    → Total UI: one button, one tap, one satisfying "kaCHUNK" sound
```

Once credits run out, the Express Checkout Element becomes visible again with "REFILL".

---

## 5. How the revive integrates with the existing engine

The death path today (`engine.py:5104-5130`) is clean and gives us a great intercept:

```5117:5127:engine.py
                game_over_item = create_feed_item(type="game_over", content="You have succumbed to the horrors. The transmission ends.")
                game_over_choices = _structure_choices_for_feed(
                    ["Restart Simulation"], "GAME OVER",
                    image_url=turn_state.get("current_image_url"),
                )
                with WORLD_STATE_LOCK:
                    st = _load_state(SID)
                    _feed_append(st, game_over_item)
                    _feed_append(st, game_over_choices)
                    st["turn_count"] = int(st.get("turn_count", 0)) + 1
                    _save_state(st, SID)
```

We need three engine-side additions and one client-side one — all small.

### 5.1 Engine: add a revive endpoint

New function `engine.api_revive(session_id, revive_token)` that:

1. Verifies the `revive_token` server-side (signed, single-use, tied to session).
2. Loads the last known-good pre-death `state` snapshot from `_save_state` history (we already have per-turn state).
3. Rewinds `player_alive → True`, restores HP to a partial value (e.g., 25%), and applies a *narrative* penalty (drop a random inventory item, mark a scar, add a "revived" flag to lore).
4. Emits a `continue_used` feed item: *"A quarter drops. The transmission crackles back to life."* — with a special CRT flicker on the client.
5. Regenerates choices grounded on the current image (same primitive `api_regenerate_choices` already uses).

`api.py` exposes `POST /api/revive` scoped by `_session_scoped` (matches existing `/api/reset` pattern on `api.py:93`).

### 5.2 Engine: emit a new feed item type at death

Change `game_over_choices` to include *two* options: `"Restart Simulation"` and `"Insert Coin — Continue"`. The client-side widget takes over rendering the second one; this keeps the engine feed backward-compatible (any older client still sees only text).

### 5.3 Client: swap the death-overlay button for the widget

`templates/standalone.html:576-585` currently hardcodes the restart button; `standalone.js:enterGameOver` shows the overlay. We add a sibling container `#continue-coin-slot` inside `#death-overlay`, and mount the widget into it whenever `SomewhereCoinOp.available()` returns true. The RESTART button stays, secondary, below it — the player always has a free out.

### 5.4 State snapshotting for revive

We already write per-turn state via `_save_state`. We add a rolling `state_history` array (last 3 states) so revive rewinds to the pre-death state, not the corrupted death state. This is a ~10-line change in `engine.py` around the `_save_state` calls.

---

## 6. The widget — what it looks and feels like

### Look

- Full-width inside the death overlay, ~360px in menus, drop-in ~320px minimum elsewhere.
- CRT phosphor green on black background, `VT323` monospace (already loaded by `standalone.html`).
- A pixel-art coin slot on the left. A wallet counter on the right (`3 TOKENS`).
- Big central button: **`▶ INSERT COIN`**. On tap, the coin slot animates ingest → a coin sprite falls in → a satisfying `ka-CHUNK` sound (reuse `Sound.pickup()`, or add `Sound.coin()`).
- If Apple Pay/Google Pay is available: a second button above it labeled with the native wallet mark — the Express Checkout Element handles this natively.

### Feel

- **Never interrupt gameplay.** Widget only appears at death (or in a hidden menu accessible from the lobby).
- **Never speak in dollars first.** Speak in tokens: "1 TOKEN TO CONTINUE". The dollar amount appears in small text below.
- **Silent when full.** With credits > 0, no payment UI at all — just the token counter and the continue button.
- **Instant feedback.** Optimistic UI: "COIN ACCEPTED" the instant Stripe returns success, before webhook lands. Server reconciles.
- **One-tap dignity.** A returning user with a saved Payment Method sees exactly one button. That is the entire spec.

### Drop-in surface

```html
<!-- Anywhere in our app or a partner site -->
<script src="https://somewhere.game/coinop.js" async></script>
<div data-somewhere-continue
     data-pack="roll"
     data-theme="crt"></div>
```

`coinop.js` is a self-contained IIFE that:
- Reads `data-*` attributes.
- Fetches `/api/coinop/config` (publishable key, packs, currency).
- Mounts an isolated shadow-DOM widget so partner sites can't leak CSS into it.
- Emits `CustomEvent('somewhere:coin-accepted', { detail: {...} })` on success so hosts can react.

This is the exact pattern used in `examples/embed_dashboard.html`, so we already have an embed idiom to match.

---

## 7. Identity & credit persistence

Credits must survive:

- The same tab across a game restart. **(session cookie)**
- A closed & reopened browser next week. **(persistent, signed cookie tied to a Stripe Customer id)**
- A device change. **(optional email or Discord OAuth link — off-critical-path)**

### Recommended identity ladder

1. **Anonymous:** every browser gets a `player_id` cookie (UUID, signed, HttpOnly-adjacent). Credits are stored keyed by `player_id`. This is enough for 90% of players.
2. **Stripe Customer:** on first purchase, we `POST /v1/customers` and store `stripe_customer_id` alongside `player_id`. All future PaymentIntents attach to that customer → saved cards Just Work.
3. **Optional email or Discord link:** in a "sync your tokens" menu. If two `player_id`s ever end up with the same email, we merge balances.

We do **not** require an account to buy or spend credits. Requiring signup at the death screen is the single fastest way to kill conversion.

---

## 8. Server design (concrete)

### New Flask endpoints (mirrors existing `_session_scoped` pattern in `api.py`)

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/api/coinop/config` | Public: packs, prices, publishable key, currency |
| `GET`  | `/api/coinop/balance` | Player's credits + has-saved-PM flag |
| `POST` | `/api/coinop/quote` | Creates PaymentIntent + CustomerSession, returns client_secrets |
| `POST` | `/api/coinop/spend` | Decrements balance, mints one-use revive token |
| `POST` | `/api/revive` | Consumes a revive token, revives player (calls into engine) |
| `POST` | `/webhook/stripe` | **The only** place credits are actually created |
| `GET`  | `/api/coinop/receipts` | Player's own history (transparency) |

### Data model

```
players            (player_id PK, stripe_customer_id, created_at, last_seen_at)
credit_balances    (player_id PK, credits INT, updated_at)
credit_ledger      (id PK, player_id, delta, reason, stripe_pi_id, run_id, created_at)
                   -- append-only; balance is always SUM(delta), or a materialized cache
revive_tokens      (token PK, player_id, run_id, minted_at, consumed_at, tokens_spent)
webhook_events     (stripe_event_id PK, received_at, processed_at)  -- idempotency
```

Storage: reuse the existing session/state file pattern for MVP (JSON on disk, per-session directory), swap to SQLite when credits ledger crosses ~10k rows or when we go multi-instance. Render's ephemeral-disk limitation (`RENDER_STORAGE_LIMITATION.md`) means we *must* pick a persistent store before shipping — probably **Render Postgres** (small tier, ~\$7/mo).

### Webhook contract (never skip)

- Verify signature with `stripe.Webhook.construct_event`.
- Dedupe on `event.id` in `webhook_events` table (Stripe retries).
- On `payment_intent.succeeded`: read `metadata.player_id` and `metadata.pack`, credit balance, append ledger row, done.
- On `charge.refunded`: debit balance (may go negative; that's fine, blocks future spends).
- On `payment_intent.payment_failed`: no-op (nothing was credited).
- On `customer.updated` / `payment_method.detached`: keep our cache in sync.

**All balance mutations happen in the webhook, never in the client-facing endpoint.** The `/api/coinop/spend` endpoint reads from the ledger, not the other way around.

---

## 9. Security, abuse, and refunds

- **PCI:** Payment Element / Express Checkout Element keeps us SAQ-A (Stripe's simplest scope).
- **CSRF:** all POST endpoints require the standard app CSRF token; webhooks are signature-verified instead.
- **Idempotency:** every `PaymentIntent` create sends `Idempotency-Key: <player_id>:<quote_nonce>`; the client is free to retry.
- **Rate-limiting:** cap `/api/coinop/quote` at 6 / minute / player, `/api/coinop/spend` at 30 / minute / player. Stripe Radar handles card-side fraud.
- **Refund policy:** clearly stated 1-tap refund within 24h if unused. If used, no refund. Post the policy inline in the widget footer. In EU/UK, digital-content law requires that the buyer explicitly waives the 14-day cooling-off before we deliver — Stripe Checkout has a checkbox for this; embedded Payment Element requires us to render it ourselves.
- **Age gate:** SOMEWHERE is horror. Reuse whatever age confirmation the main site already has; the widget will show a compact 18+ acknowledgement on first-ever purchase.
- **Under-13 protection (COPPA):** never store PII from users without confirmed 13+. Because we don't collect email by default this is easy.
- **Chargebacks:** log per-player chargeback count; auto-block after 2 in 90 days.

---

## 10. Observability & analytics

Every step in the funnel is a named event; log both to our own store and (optionally) forward to Stripe/PostHog/Segment.

| Event | Emitted where |
|---|---|
| `death.shown` | Client, at overlay mount |
| `coinop.widget.impression` | Client, when widget renders |
| `coinop.quote.created` | Server, on `/quote` |
| `coinop.checkout.started` | Client, on Payment Element interaction |
| `coinop.checkout.completed` | Client, on Stripe success |
| `coinop.credits.granted` | Server, on webhook |
| `coinop.credits.spent` | Server, on spend |
| `run.revived` | Server, on revive |
| `run.restarted_free` | Server, on classic reset |

**Headline metrics we track from day one:**
- Death → widget-impression rate (should be ~100%)
- Widget-impression → checkout-completed conversion (industry benchmark: 3–8% for first-time; 20–40% for returning w/ saved PM)
- Continues per paying user per run
- ARPPU (average revenue per paying user)
- Refund rate (target: < 2%)
- Chargeback rate (target: < 0.5%)

---

## 11. Legal / operational checklist (do before switching to live keys)

- [ ] Stripe account activated with real business details
- [ ] Terms of Service updated: virtual-currency clause, no cash-out, expiry policy
- [ ] Privacy policy updated: Stripe as processor, cookie disclosure
- [ ] Refund policy published & linked from widget
- [ ] Stripe Tax enabled (`tax_behavior=exclusive` recommended)
- [ ] Support email routed
- [ ] Sandbox → live key rotation runbook written
- [ ] Webhook endpoint on a *stable* URL (Render web service, not the worker)
- [ ] Load test: 100 concurrent revives, single-instance
- [ ] Disaster: what happens if webhook is down for 1h? (Stripe retries for days; we're fine — just deferred credit)

---

## 12. Rollout plan (phased, technical)

Broken into shippable chunks. Each phase is behind a feature flag (`FEATURE_COINOP=1`) so we can dark-ship.

### Phase 0 — Scaffold (no user impact)
- Add Stripe SDK to `requirements.txt`.
- New module `coinop.py`: config, packs, balance/ledger, webhook handler.
- New env vars: `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET`, `COINOP_PACKS_JSON`, `FEATURE_COINOP`.
- Migrate state persistence to SQLite (or Postgres if we're already going that way for other reasons). Same shape as today's JSON, just moved.
- Unit tests: pack math, ledger sum, webhook dedupe, signature verify.

### Phase 1 — Server-side revive plumbing (still no UI)
- Add `state_history` snapshotting to `engine.py` around `_save_state`.
- Implement `engine.api_revive` + `POST /api/revive`.
- Emit second choice (`Insert Coin — Continue`) alongside `Restart Simulation`.
- Manual test with hard-coded "always-1-credit" balance.

### Phase 2 — Stripe integration in test mode
- Implement `/api/coinop/config`, `/api/coinop/quote`, `/api/coinop/balance`, `/api/coinop/spend`.
- Implement `/webhook/stripe` with signature verification + dedupe.
- End-to-end with Stripe test cards, using the built-in Payment Element in a throwaway page (`templates/coinop_dev.html`).

### Phase 3 — The widget
- Build `static/js/coinop.js` (shadow-DOM, self-contained IIFE).
- Design the pixel-CRT coin-slot: SVG sprite + CSS animation + `Sound.coin()` cue.
- Mount inside `#death-overlay` (`templates/standalone.html`), gated on `FEATURE_COINOP`.
- Also mount on the lobby (as a "top up tokens" affordance) — this is where casual players will refill outside a death moment.

### Phase 4 — Polish
- Escalating cost within a run (1, 2, 3 tokens).
- First-turn deaths are free.
- Locale-aware currency.
- Copy pass — every single string, in-widget and in receipts, should feel like a coin slot.

### Phase 5 — Go live
- Legal checklist (§11).
- Ship to 5% of sessions via the flag, watch dashboards for 48h.
- Ramp to 100%.
- Post-mortem after 2 weeks: pricing, conversion, complaints.

**Risk profile of the phases:** Phase 0-1 are pure additions — near-zero risk to the running game. Phase 2 is isolated to new endpoints. The only phase that touches user-visible behavior is Phase 3, and it's flag-gated. Phase 5 (live keys) is the only phase that can lose real money if wrong; the flag lets us abort in one env-var edit.

---

## 13. Open questions / decisions we still want to make

1. **Discord parity.** The bot in `bot.py` doesn't currently support in-Discord payments cleanly (Discord's own store is complex; Stripe links out of Discord). Do we accept "coin-op is web-only for now" and offer a link, or defer Discord monetization entirely?
2. **Token gifting.** Highly viral in horror communities — a friend dies, you gift them a token so they can keep streaming. Great for growth, adds fraud surface.
3. **Cosmetic economy on top of continues.** Would be tempting to sell filter packs / VHS overlays as one-time cosmetic buys. Out of scope for MVP, but the same infrastructure supports it.
4. **Post-run "high score → save your run"** as a paid feature? Feels dirty at the moment of a great run; better as free.
5. **Currency floor for non-USD markets.** \$4.99 pack in JPY/INR needs a per-market pack table.

---

## 14. TL;DR

1. **Model:** arcade-token credit pack. Not per-charge, not subscription.
2. **Stack:** Stripe Payment Element + Express Checkout Element + CustomerSession, PaymentIntent for the actual charge, webhook is the source of truth for credit grants.
3. **Product surface:** a shadow-DOM CRT widget that drops in anywhere with one `<script>` and one `<div>`; mounts by default into the existing `#death-overlay`.
4. **Engine change:** small — add `state_history` snapshotting, add `api_revive`, add one extra choice at death.
5. **Ship in five flag-gated phases**, live-keys turn-on is a single env var flip.
6. **First quarter of real revenue** is achievable once phases 0–5 are complete, without touching a single line of the story-generation pipeline.

The coin slot is the interface. The pack is the transaction. The webhook is the truth. Everything else is decoration — and we should make the decoration *beautiful*.

---
---

# APPENDIX A — "The \$0.50 problem": how to *actually* charge a real fifty cents

> The core plan above uses a credit-pack model to work *around* the Stripe fee floor. This appendix answers the different question the user asked: **if we insist on charging a real \$0.50 per continue, how do we build it and which rail costs us the least?** Short answer: no card rail can do it profitably — the answer is either PayPal Micropayments (best off-the-shelf), USDC on Solana (best-in-class), or a hybrid multi-rail wallet that lets each user pick.

## A1. First, the honest fee table for a **single \$0.50 charge**

| Rail | Formula (2026, US) | Fee on \$0.50 | You keep | Effective fee | Notes |
|---|---|---:|---:|---:|---|
| **Stripe (cards, standard)** | 2.9% + \$0.30 | \$0.315 | **\$0.185** | **62.9%** | Also: \$0.50 is Stripe's minimum charge. |
| Stripe (cards, EU/UK) | 1.5% + \$0.25 | \$0.258 | \$0.242 | 51.5% | Regulated interchange helps a bit. |
| Square | 2.9% + \$0.30 | \$0.315 | \$0.185 | 62.9% | No micropayments tier. |
| PayPal standard | 2.99% + \$0.49 | \$0.505 | –\$0.005 | 100%+ | You *lose* money. |
| **PayPal Micropayments** (opt-in) | 4.99% + \$0.09 | \$0.115 | **\$0.385** | **23.0%** | Best off-the-shelf card-adjacent rail for \$0.50. |
| Braintree Micropayments | 4.99% + \$0.09 | \$0.115 | \$0.385 | 23.0% | Same as PayPal (same parent). |
| Stripe ACH Direct Debit | 0.8%, no flat, \$5 cap | \$0.004 → \$0.01 min | \$0.49–0.50 | \~1% | Cheap, but 4-day settle + microdeposit setup. Fails the "instant continue" test. |
| Stripe Instant Bank Payments | 2.6% + \$0.30 | \$0.313 | \$0.187 | 62.6% | Same shape as cards. |
| Cash App Pay via Stripe | 2.9% + \$0.30 | \$0.315 | \$0.185 | 62.9% | Same cost as cards; different UX. |
| **Coinbase Commerce** (USDC) | 1%, no flat | \$0.005 | **\$0.495** | **1.0%** | Custodial-ish hosted checkout, multi-chain. |
| **Helio / MoonPay Commerce** (USDC on Solana) | 0.75%, no flat | \$0.004 | **\$0.496** | **0.75%** | Non-custodial, funds land in your wallet. |
| **Self-integrated Solana Pay** (USDC on Solana) | \~\$0.0005 network fee | \~\$0.001 | **\$0.499+** | **<0.2%** | You run the wallet + reconciler; no processor cut at all. |
| USDC on Base (Ethereum L2) | \~\$0.005–\$0.01 network fee | \~\$0.01 | \~\$0.49 | \~2% | Broader wallet support than Solana in some segments. |
| USDC on Ethereum L1 | \~\$0.10–\$2 gas | \$0.10+ | \$0.40 or less | **20%+** | Do not use for micropayments. |

### What this tells us bluntly

- **Card rails cannot profitably charge \$0.50.** Not Stripe, not Square, not any competitor. The \$0.30 flat fee is baked into the entire card-network economics. This is not a Stripe problem; it is a Visa/Mastercard/AmEx problem, and every card processor inherits it.
- **PayPal's Micropayments tier** is the *only* off-the-shelf card-adjacent rail where \$0.50 gross yields > \$0.35 net. That's the honest "we swapped Stripe out" answer.
- **Crypto (USDC on Solana) is the only rail where a real \$0.50 charge keeps ~\$0.49.** No processor is involved; you're paying the network directly and it charges tenths of a cent.
- **ACH is nearly free but not "instant"** in the emotional sense — 4-day settlement and a mandate-setup step, so useless as a death-screen "insert coin" button. Great for large top-ups.

## A2. Chargebacks change the picture

Fee % is only half the cost. The other half is chargeback risk, and it's *very* real for a game selling revives to frustrated players.

| Rail | Chargeback fee | Chargeback probability (games segment) | Effective added cost |
|---|---|---|---|
| Stripe cards | \$15 per dispute | 0.3–1.5% typical | +\$0.05–\$0.23 per successful \$0.50 charge |
| PayPal Micropayments | \$20 per dispute + PayPal tends to side with buyer | Higher than cards | +\$0.10–\$0.40 per successful \$0.50 charge |
| ACH | \$15 dispute + \$4 for failed debit | Low but returns are painful | +\$0.05 per successful charge |
| **Crypto (USDC)** | **\$0. Chargebacks do not exist on-chain.** | 0% | \$0 |

Once chargebacks are priced in, **crypto's advantage over cards for micro-transactions is larger than the raw fee table already shows.** Card-adjacent rails for a \$0.50 continue realistically net you \$0.15–\$0.30, not the \$0.18–\$0.38 the fee table implies.

## A3. The **structural** ways to bypass the fee (rail-agnostic)

Independent of which processor we pick, three architectural patterns each let you *not pay the flat fee per continue*:

### Pattern 1 — **Prepaid credit wallet** (the core plan above)
Charge \$5 once, grant 10 tokens, spend one per continue. Stripe fee on the \$5 is \$0.445 (8.9%), amortized across 10 continues = \~\$0.044/continue. **Perceived price: \$0.50/continue. Actual cost to us: 8.9% not 63%.** This is what real arcades did in 1983. It is still the right answer for the majority of players.

### Pattern 2 — **Auth-and-Capture "tab"** (Stripe Multi-Capture, GA in 2025)
Authorize \$5 up front, then use Stripe's *multi-capture* API to capture \$0.50 at a time as continues are used. Card auth holds for ~7 days on most Visa/MC. When it expires or is drained, we auto-re-authorize. Fees are still charged per capture (2.9% + \$0.30 each) *unless* we batch → so this is only useful if we capture in **\$5 chunks** anyway. In practice this collapses back into Pattern 1, but it lets us refund unspent tokens frictionlessly. Nice-to-have, not the primary answer.

### Pattern 3 — **Deferred settlement via a stablecoin channel**
User funds a merchant-controlled sub-wallet on Solana (or a "streaming payment" via Superfluid on Base). Each continue debits the sub-wallet on-chain for exactly \$0.50 USDC at \~\$0.0005 network cost. When the sub-wallet is empty, the widget prompts a top-up. This is closest to a true "\$0.50 charge per continue" with near-zero fees. Wallet UX friction is the tax you trade for the fee savings.

## A4. Recommended architecture — **Multi-rail Coin Op with a smart router**

Rather than pick one rail, expose all three intelligently through the same widget. The user always sees "1 TOKEN = \$0.50"; the router decides how they pay.

```
                   ┌─ WIDGET (CRT coin-slot) ─┐
                   │                          │
                   │  [ ▶ INSERT COIN ]       │
                   │                          │
                   │  paid with ▼ (Cards | PayPal | Crypto | Bank)  │
                   └────────────┬─────────────┘
                                │
              ┌─────────────────┴──────────────────┐
              │      COIN-OP ROUTER  (server)      │
              └───┬────────────┬────────────┬──────┘
                  │            │            │
              ┌───▼───┐   ┌────▼─────┐  ┌───▼─────────┐
              │Stripe │   │ PayPal   │  │ Helio /     │
              │Payment│   │ Micro-   │  │ Solana Pay  │
              │Element│   │ payments │  │ (USDC)      │
              └───┬───┘   └────┬─────┘  └───┬─────────┘
                  │            │            │
                  └────────────┴────────────┘
                               │
                        WEBHOOK / on-chain listener
                               │
                        credit_ledger (single source of truth)
```

### Rail selection defaults

- **Default rail = Stripe cards + Express Checkout Element** for the widest audience.
  - Only sells packs ≥ \$5 (≤ 8.9% fee).
  - This is the "boring, works everywhere" rail.
- **Second rail = PayPal Micropayments** for users who prefer PayPal, or for anyone who insists on a \$1 pack (still 23% but not fatal). We apply for the Micropayments tier when we hit the volume threshold to enable it (PayPal opts merchants in on request).
- **Third rail = Crypto (USDC on Solana via Helio)** for the true bypass.
  - This is the only rail that offers a **real \$0.50 single-continue** with <1% fees.
  - Widget shows this option as: `Pay \$0.50 with USDC ⚡ (best price)`. Small, unobtrusive; power-user path.
  - Helio is the fastest integration (Shopify-plugin-quality embed, non-custodial — funds arrive in *our* wallet); self-integrated Solana Pay is a cheaper long-term destination if volume justifies it.
- **Fourth rail = ACH Direct Debit** for the "power user pack" only: top up \$25 for \$0.20 fee (0.8%), get 50 tokens. Never offered as a per-continue path.

### What the user sees

- Casual player, first time: cards path. Sees packs. Picks \$5, gets 10 tokens, plays.
- Casual player, second time: one-tap Apple Pay (Express Checkout Element) → \$5 → 10 tokens.
- Crypto-native player: sees the `PAY \$0.50 WITH USDC` chip, taps → Phantom or wallet-connect popup → signs → \$0.499 lands in our Solana wallet → 1 token → continue. **This is the user who literally pays 50 cents.**
- Whale: taps "MORE OPTIONS" → picks the ACH-funded \$25 super-pack. Slower settle, but 0.8% fee.

## A5. Concrete recommendation, in three lines

1. **Ship the credit-pack UX (from the core plan) with Stripe cards as the default rail.** Non-negotiable — it's the widest, most-conversion-optimized experience and it makes \$0.50 tokens feel like a real thing.
2. **Add PayPal Micropayments as a secondary rail** as soon as we have any evidence of PayPal-preferring players. This is our best off-the-shelf answer for a \$1 pack or smaller.
3. **Integrate Helio (or self-Solana Pay) as a "power user" rail** to unlock literal \$0.50 continues at near-zero fee. Advertise it in the widget as `PAY \$0.50 WITH CRYPTO ⚡` — a tasteful nod, not a shove. This is the only actual answer to "how do we let users pay exactly fifty cents without losing money."

Combined, this stack lets us honestly tell a user "yes, you can pay just fifty cents" while still being economically sane for the 95% of players who don't want to touch a crypto wallet.

## A6. Fee math summary — cost per continue at \$0.50 perceived price

Assume 100 continues delivered to a mix of users. Which rails do they use?

| Scenario | Rail mix | Weighted revenue kept |
|---|---|---:|
| Cards only, \$0.50 charges | 100% Stripe cards @ 63% fee | **\$18.50** of \$50 |
| Cards with credit-pack UX | 100% Stripe cards in \$5 packs @ 8.9% | **\$45.55** of \$50 |
| Cards + PayPal Micro | 70% Stripe packs + 30% PayPal \$0.50 | \$45.55 × 0.7 + \$38.50 × 0.3 = **\$43.44** of \$50 |
| **Multi-rail (recommended)** | 60% Stripe packs + 20% PayPal Micro + 20% Crypto \$0.50 | \$45.55 × 0.6 + \$38.50 × 0.2 + \$49.60 × 0.2 = **\$44.96** of \$50 |
| Pure crypto (Solana self-integrated) | 100% USDC \$0.50 charges | **\$49.90** of \$50 (but tiny addressable audience without wallet UX) |

The multi-rail model captures ~90% of revenue AND lets any user who insists on paying real fifty cents do so. Pure-crypto captures 99.8% but only from the sliver of players who already have a Solana wallet.

## A7. Additional engineering deltas vs the core plan

Everything in the core plan still applies. Only the following changes:

1. **`coinop.py` module becomes a router,** not a Stripe wrapper. It exposes the same public interface (`quote`, `spend`, `balance`, `webhook`) but dispatches to one of `rails/stripe.py`, `rails/paypal.py`, `rails/helio.py`, `rails/solana_pay.py`.
2. **`credit_ledger` gains a `rail` column** (`stripe|paypal|helio|solana|ach`) and a `fee_paid_cents` column (denormalized from the processor confirmation) so we can report gross vs net per-continue economics in the dashboard.
3. **Webhooks become plural.** One per rail, each verified with that rail's signing method. For Solana Pay self-integrated, we run a lightweight on-chain listener (or use Helio's webhook if we're on Helio).
4. **Currency handling.** USDC is dollar-denominated so no FX exposure. PayPal Micropayments is per-currency (rate table above). Cards go through Stripe's currency layer. All are normalized to USD in the ledger for reporting.
5. **KYT / AML for crypto.** Helio bundles this; if we self-integrate Solana Pay we should add a Chainalysis or TRM Labs address-screening call on incoming payments. At \$0.50/txn our exposure is small but we still need it.
6. **Legal.** Adding a crypto rail means adding a crypto-specific ToS clause (no refunds to burned wallets, we don't hold your crypto, etc.). Standard boilerplate — Helio provides a template.
7. **Widget copy.** The `PAY \$0.50 WITH CRYPTO ⚡` chip is only rendered when the browser has a detectable wallet (Phantom, MetaMask, Solflare) OR when the user opts into "power user options". We never surface it to a player who has no idea what a wallet is; that would feel weird and reduce conversion.

## A8. What we should NOT do

- **Do not build our own custodial crypto wallet.** Regulatory nightmare (money transmitter licensing state-by-state). Use Helio's non-custodial model — funds go straight to our wallet, we never hold user funds.
- **Do not accept "raw" ETH or BTC.** Volatile, fee-heavy, and half the payments would settle for the wrong amount by the time they confirm. Stablecoins (USDC) only.
- **Do not offer literal \$0.50 card charges as a UX option.** Even if PayPal Micropayments makes it technically viable, that flow encourages a 23%-tax pattern when the same user could have paid \$5 once at 8.9%. Nudge users to packs; reserve the true-\$0.50 path for the crypto rail where it's actually cheap.
- **Do not let widget copy imply crypto is the "cheaper for you" option.** It isn't — the user pays the same \$0.50 regardless. It's only cheaper *for us*. Say `⚡ instant` or `⚡ no card needed`, not "save money."

## A9. TL;DR of the appendix

- **Card rails cannot profitably charge \$0.50.** That is a card-network fact, not a Stripe-specific fact. Switching processors alone will not solve this.
- **PayPal Micropayments** is the best off-the-shelf swap-for-Stripe answer: 23% fee instead of 63% on a \$0.50 charge.
- **USDC on Solana** (via Helio, or self-integrated Solana Pay) is the only rail where a real \$0.50 charge nets us ~\$0.49 — because on that rail there is no "processor" at all, only a fifty-thousandth-of-a-dollar network fee.
- **Recommended shipping order:** (1) core plan's Stripe credit-pack UX, (2) add PayPal Micropayments as second rail, (3) add Helio USDC as the "power user \$0.50 button." All three route into the same credit ledger through a small `rails/*` dispatcher. The widget stays a single beautiful CRT coin-slot; the router quietly picks the cheapest rail per user.
- The user's ask — *"let people pay just 50 cents, losing as little money as possible"* — has a real answer: **make the crypto rail the actual \$0.50 path, and keep the card path on packs.** That is the way to do it "the right way."
