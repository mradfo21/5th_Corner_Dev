# Billing: taking real money — Plan

> **Status: open.** Nothing below is built yet. Track progress in the table in
> §2; each row moves open → in progress → done (with the commit) → verified
> (with the test or check that proved it).

---

## 0. TL;DR

Stripe checkout, redeem, the webhook, packs, the monthly limit, the 402 pause
and the ACCOUNT sheet all exist (`billing.py`, `coinop.handle_webhook`,
`api.py` `_spend_blocked` / `/api/billing/*`, `static/js/account.js`). Hosted
payments are **off** today: `render.yaml` sets `FEATURE_BILLING=1` but no Stripe
keys, so `requires_wallet()` is false and hosted play runs on the host's
dime.

Turning the keys on as the code stands would lose money and let players spend
each other's balances. Four code fixes (B1–B4) and four checks (P1–P4) come
first, then Matt's Stripe/Render setup (§5), then a test-mode run on Render
(§6), then live keys.

Order: **P1 → B2 → B3 → B1 → B4 → P5/P6 → §6 test run → P3 week of
reconciliation → P4 prices → live.** P1 is first because every number in this
plan and the ledger is wrong until it lands; B2 is first of the code fixes
because B1 and B3 both key on the account id it introduces.

---

## 1. What the ledger actually says (2026-09-21, last 30 days)

Correcting the numbers given in conversation earlier today:

- **Text is overpriced ~1000×.** `pricing.json` token rates are per-million
  prices stored in `input_per_1k` / `output_per_1k`. One `ask` of 9,731 in /
  201 out tokens is logged at $0.395; at the stored rate read as per-million it
  is $0.0004. The ledger's $479.60 for `gemini-3.1-flash-lite` over 6,110 calls
  is really about $0.50. Same pattern on every token rate (OpenAI
  `gpt-4o-mini` 0.15 / 0.60 and Anthropic 3.0 / 15.0 are their per-million
  list prices).
- **So the "$2.65 a play-day" median is wrong** — it is dominated by the
  inflated text. The real per-day cost is mostly pictures and ElevenLabs; recompute
  after P1.
- **The "1,703 unpriced calls" are failures, not gaps.** `record_usage` prices
  only `success=True` events; 1,212 `ask` + 491 `choices` calls failed — 28%
  of all text calls. Not a billing blocker (a failed call is correctly $0),
  but worth its own look (P2).
- Real unpriced gaps: `gemini:models/lyria-realtime-exp` and `gemini_live:*`
  have `null` rates; `encounter_plate` logged 1 unpriced event.
- Other 30-day spend by provider (these units look right): ElevenLabs sound
  $33.04, music $25.70, voice design $16.95; Gemini images $17.04; Reactor
  $0.17.

---

## 2. Tracker

| ID | Issue | Fix (short) | Main files | Proof | Status |
|----|-------|-------------|-----------|-------|--------|
| **B1** | Turns are almost never charged | Charge the session's owner inside `cost_tracker.record_usage`, whenever a cost lands | `cost_tracker.py`, `billing.py`, `api.py` | test: a cost recorded from a background thread after the response is debited ×markup | open |
| **B2** | Anyone can spend anyone's wallet; no-cookie visitors bill the last sign-in | Per-device account: random id in a signed HttpOnly cookie; no email sign-in; no store-wide fallback on a hosted server | `billing.py`, `api.py`, `static/js/account.js` | test: two browsers → two wallets; no cookie → new empty wallet; forged cookie → rejected | open |
| **B3** | A payment can land in the wrong wallet | Credit the account id written into the Checkout Session, never the browser's current account | `billing.py`, `coinop.py` | test: redeem with another account's cookie credits the payer; webhook + return credit once | open |
| **B4** | Returning from checkout drops the player's game | Carry a validated same-origin return path through `success_url` / `cancel_url` | `billing.py`, `api.py`, `static/js/account.js` | test: success URL keeps `?session=`; `//evil` and absolute URLs refused | open |
| **P1** | Token rates 1000× too high | Fix units in `pricing.json`; re-check every rate against the provider's current price page | `pricing.json`, `pricing.py` | test: 10k-token flash-lite call < $0.01; ledger recomputed | open |
| **P2** | 28% of text calls fail | Separate investigation (error messages are in `usage_events.error_message`) | `engine.py`, `choices.py` | failure rate by error, before/after | open |
| **P3** | Ledger never checked against real bills | One week: Google Cloud billing + ElevenLabs usage vs the ledger, per provider; price the `null` rates | `pricing.json` | within ±15% per provider | open |
| **P4** | Prices set on wrong numbers | After P1+P3: median cost per play-day → markup and pack sizes; include Stripe's fee | `billing.py` `PACKS`, `BILLING_MARKUP` | written decision in §4 | open |
| **P5** | Arcade coins double-charge if both on | Refuse to start (or log loudly and disable coin gating) when `FEATURE_COINOP` and `FEATURE_BILLING` are both on | `api.py` / `coinop.py` | test | open |
| **P6** | $12/mo Play plan is unused and has its own paths | Drop: `create_checkout` refuses `play`; leave `invoice.paid` handler inert | `billing.py` | test: `kind=play` → 400 | open (decision: drop) |

---

## 3. The fixes in detail

### B2 — a wallet per device (first: B1 and B3 key on its id)

Today (`billing.py`): `link_email` makes anyone who types an address that
account, and `_active_email` falls back to `store["active_email"]` — the last
address linked on the server — when a request has no cookie. On a hosted
server that means a new visitor plays on, and pays from, a stranger's wallet.

- `account_id = secrets.token_urlsafe(24)`, created the first time a hosted
  request needs one (the first gated call, or opening ACCOUNT). Cookie
  `god_account = <id>|<hmac>`: HttpOnly, `Secure` when served over https,
  SameSite=Lax, one year.
- Store keyed by `account_id`; `email` becomes an optional attribute filled
  from Stripe (`customer_details.email`) at redeem — for receipts and for the
  later "use on another device" link.
- Hosted mode never falls back to a store-wide active account. The desktop app
  (`mark_local_app`) keeps its single local account.
- `POST /api/billing/account` (email link) goes; `DELETE` becomes "forget this
  device". The ACCOUNT sheet loses its SIGN IN block and shows the wallet
  straight away.
- `SOMEWHERE_BILLING_SECRET` required when payments are on (today it falls
  back to the Stripe key, then to a hard-coded dev string). Payments refuse to
  enable without it.
- Migration: hosted payments have never been on, so `billing.json` on Render
  should hold no paid balances. Check before deploying; if it does, keep the
  email-keyed rows readable and credit them to a device on first redeem.
- Later (B2b, not a blocker): "Use on another device" — email a one-time link
  that sets the cookie on the new device. Needs an email sender (Resend,
  Postmark, SendGrid…) — a decision for §4.

### B3 — money goes to whoever paid

Today `redeem()` calls `link_email(metadata.email)` and then credits
`current_account()` — which is the *cookie's* account whenever the browser has
one. A shared or signed-in-elsewhere browser credits the wrong wallet.

- `create_checkout` writes `account_id` into `metadata` and
  `client_reference_id`.
- `redeem(cs_id)` retrieves the session from Stripe, requires it paid, and
  credits `metadata.account_id` — never `current_account()`.
- Idempotency moves from per-account `redeemed` lists to one store-wide set of
  redeemed checkout ids, checked and written under the same lock (safe: one
  gunicorn worker, see `start_production.sh`).
- The webhook (`checkout.session.completed`) and the browser's return both
  call the same `redeem`; whichever lands second is a no-op.

### B1 — charge when the cost happens

Today `_credit_gated_choose` and three other routes read the session's ledger
total before and after the request and debit the difference. But
`engine.api_choose` hands the turn to `_process_turn_background_guarded` on a
thread and returns at once — story, pictures, flipbook, sound and voice all
land after the "after" reading. The difference is about $0.

- **Ownership.** `_spend_blocked()` already runs on every paid route with both
  the session id and the cookie in hand: it records
  `billing.bind_session(session_id, account_id)` (stored in `billing.json`,
  pruned after 7 days idle).
- **Charge at the source.** At the end of `cost_tracker.record_usage`, when a
  priced event was inserted and `billing.requires_wallet()`:
  `billing.charge_session(session_id, cost_usd)` debits the owner
  `cost × markup()`. Never raises (same contract as the rest of
  `record_usage`). `billing` is imported lazily to avoid a cycle.
- **Remove** the four `billing.settle_session(sid, cost_before)` calls, or
  every synchronous cost is charged twice.
- **Unowned costs** (world-editor `wf-*` renders, warm-ups, admin) are
  charged to nobody and counted: `billing.json` keeps a running
  `house_usd` so leaks show up.
- **Overdraft.** The wallet may go below zero by what was already in flight
  when it ran out (at most one turn); `gate()` then blocks the next action.
  No free turns, no floor at zero.

### B4 — come back to the game you left

`create_checkout` builds `success_url = {base}/standalone?billing=success…`.
Hosted players arrive via `/lobby` → `/play?session=<id>`, so they come back
to the default session.

- The client sends `return_to = location.pathname + location.search`.
- Server accepts it only if it starts with `/play` or `/standalone` and holds
  no `//`, no scheme, no backslash; otherwise `/standalone`.
- `success_url = base + return_to + (? or &) + billing=success&cs={CHECKOUT_SESSION_ID}`;
  `cancel_url` the same with `billing=cancel`.

### P1 — token rates

- Every `unit_type: "tokens"` entry in `pricing.json`: the numbers are per
  million. Either divide by 1000 or (clearer) rename the fields to
  `input_per_1m` / `output_per_1m` and teach `pricing.estimate_cost` both
  names. Re-check each against the provider's current price page on the day
  it's edited — do not trust the stored values or memory.
- Recompute the ledger's history in place (`UPDATE usage_events SET cost_usd =
  cost_usd/1000 WHERE unit_type='tokens'` plus a rollup rebuild), so the admin
  dashboard and ACCOUNT stop showing $572 for a month of play.

### P5 / P6

- P5: at startup, if `FEATURE_COINOP` and `FEATURE_BILLING` are both on, log an
  error and force coin gating off. Coin-op's paid continue is a second economy
  and has no place next to a wallet.
- P6: drop the Play subscription from checkout (the ACCOUNT sheet never offers
  it). Keeps one money-in path: top-ups.

---

## 4. Decisions for Matt

1. **Markup and packs** (after P3). Today: 2× cost; packs $10 → $10,
   $25 → $27.50, $50 → $57.50. The new ACCOUNT sheet shows whatever the
   server sends.
2. **Refund policy for unused balance** — Stripe expects terms, a refund
   policy and a support contact to be visible for digital goods.
3. **Sales tax** on prepaid credit — ask an accountant; Stripe Tax exists if
   needed.
4. **"Use on another device"** (B2b) — which email sender, and whether it's
   needed for launch or can follow.
5. **Play plan** — dropping it (P6) unless you want a subscription.

---

## 5. Setup (Matt — needs your identity and bank details)

1. Stripe: activate the account (business details, bank account, identity
   check). Everything below first in **test mode**.
2. Render → Environment: `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`,
   `STRIPE_WEBHOOK_SECRET`, `PUBLIC_BASE_URL`, `SOMEWHERE_BILLING_SECRET`
   (random, 32+ chars). `FEATURE_BILLING=1` is already in `render.yaml`. Leave
   `FEATURE_COINOP` unset.
3. Stripe → Developers → Webhooks: endpoint
   `https://<host>/webhook/stripe`, event `checkout.session.completed`.
4. Terms / refund / support pages linked from the site (decision 2).
5. After §6 passes: swap the three Stripe values for live ones.

---

## 6. Test-mode run on Render (before live keys)

- Two browsers → two empty wallets; neither sees the other's.
- Top up $10 with card `4242 4242 4242 4242` → wallet shows the pack's credit
  once; check `billing.json` has one redeemed id; the webhook fired too.
- Return lands back in the same game (`?session=` kept).
- Play five turns → wallet falls by ≈ 2× the ledger cost of those turns
  (compare `/api/usage` with the admin analytics session view).
- Run the wallet to zero → the next action returns 402 and the sheet opens on
  ADD MONEY.
- Close the tab mid-checkout, pay, reopen → the webhook credited it anyway.
- Canceled checkout → nothing charged, message shown.

---

## 7. Not in this plan

- Moving `billing.json` to a database. Fine at one gunicorn worker on the
  persistent disk; revisit if the service scales out.
- Per-part "who pays" (wallet for pictures, own key for story) on a hosted
  server.
- Refunds from the app (do them in the Stripe dashboard).
