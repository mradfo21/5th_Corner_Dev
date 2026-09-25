# Billing: know our costs, show them, then take money — Plan

> **Status: in progress.** See the Status column; the launch steps are in
`docs/LAUNCH_PAYMENTS.md`. Track progress in the table in
> §2; each row moves open → in progress → done (commit) → verified (the test
> or check that proved it).
>
> Since 2026-09-25 there is no ElevenLabs provider (docs/plans/ONE_KEY_AUDIO_PLAN.md):
> voices are Gemini TTS logged by `speech.py`, music and SFX are not generated. The
> ElevenLabs rows below are history; there is nothing of theirs left to reconcile.

---

## 0. TL;DR

The rule this plan works toward: **every cent a player pays traces to a
logged provider cost, and the player can see it.** A turn's charge is what the
models cost plus a stated markup, itemised, on prices that have been checked
against our actual provider bills.

We are not there. The cost ledger (`cost_tracker.py` → `usage.db`, priced by
`pricing.py` + `pricing.json`) is the only record of what things cost, and
today it is wrong in both directions: story text is priced ~150× too high overall (each rate is stored 1000× high),
several pictures are priced at ~$0, and some paid calls are never logged at
all. Charging players off it would be charging them fiction.

Three parts, in this order:

- **A. Cost accuracy** — log every paid call, price it right, use the usage
  the provider reports, and reconcile against real invoices until the ledger
  is within ±10% per provider.
- **B. Transparency** — the player sees provider cost and our markup
  separately, a receipt per run, and a rate card built from the same numbers
  the charge uses.
- **C. Taking money** — the four faults in the payment path (charging, who
  owns a wallet, where a payment lands, the return from checkout), then
  Stripe setup, a test-mode run, and live keys.

Hosted payments are **off** today (`render.yaml` sets `FEATURE_BILLING=1` but
no Stripe keys), so nothing here is urgent to protect players — but the host
is paying for every hosted session in the meantime, and A is what tells us how
much.

---

## 1. What the ledger gets wrong (checked 2026-09-21)

### 1a. Prices

| Problem | Evidence | Effect |
|---------|----------|--------|
| Token rates are per-million values stored as per-thousand | One `ask` of 9,731 in / 201 out tokens logged at $0.395; `gpt-4o-mini` stored as 0.15 / 0.60 and Anthropic as 3.0 / 15.0, which are those providers' per-million list prices | Story text ~150× high overall: every token rate is 1000× high, but calls land on the wrong rates; real text spend over 30 days was about $5 |
| Pictures logged under made-up model names fall back to a **token** rate | `talk_portrait`, `companion_place`, `prop_jeep`, `camp_enter`, `encounter_plate`, `encounter_resolve` are not in `pricing.json`; `get_rate` falls back to `gemini:default` (tokens) and prices 1 image as 1 output token | Each of those pictures ≈ $0.0003 (60 talk portraits logged at $0.02 total) |
| Some calls log zero units | `prop_jeep`, `camp_enter`, and encounter failures log `output_units=0` | $0 even when the provider billed |
| Known gaps | `gemini:models/lyria-realtime-exp`, `gemini_live:default` rates are `null` | Unpriced |
| Every rate is unsourced | No source URL or checked date in `pricing.json`; `gemini-3-pro-image` is marked "Estimate … priced high on purpose" | Can't tell which numbers are real |

### 1b. Coverage — paid calls never logged

Found by grepping provider endpoints against `record_usage` calls:

- `cutscene.py` — the opening cutscene grid (`gemini-3-pro-image` at 4K, the
  most expensive single call we make): no log.
- `ai_provider_manager.py` — `chat()`, `vision()`, `generate_image()`: no log.
- `gemini_live_vision.py`, `gemini_live_talk.py`: no log.
- `veo_video_utils.py`, `krea_image_utils.py`, `fal_image_utils.py`: no log in
  the module; to check whether every caller logs on their behalf.
- `world_frames.py` (world editor GENERATE): no log in the module; to check.

### 1c. Trust

- Live video seconds are **reported by the browser**
  (`POST /api/reactor/usage {duration_seconds}`): anyone can send 0, and a
  closed tab sends nothing.
- 28% of story and choice calls failed in the last 30 days (1,212 `ask` +
  491 `choices`). Failed calls are logged at $0. Some providers bill failed or
  blocked generations — unconfirmed for ours.

### 1d. What that means for the numbers given earlier today

The "$2.65 a play-day" median and the $572 "last 30 days" shown in ACCOUNT are
both inflated by the text bug, and both miss the unlogged calls. Until A1–A4
land, **there is no trustworthy cost-per-turn number**. The parts that look
right in unit terms (ElevenLabs sound $33.04, music $25.70, voice design
$16.95; Gemini pictures $17.04 over 30 days) still need A6.

---

## 2. Tracker

### A. Cost accuracy

| ID | Issue | Fix (short) | Proof | Status |
|----|-------|-------------|-------|--------|
| **A1** | Token rates 1000× high | Rename to `input_per_1m` / `output_per_1m`; `pricing.estimate_cost` reads the new names (and refuses the old ones, so the mistake can't return); re-check every rate on the provider's price page the day it's edited | test: a 10k-token flash-lite call prices below $0.01; test: loading a `*_per_1k` token rate fails | done (5f1ebbe) — test `test_per_1k_token_fields_are_refused`, `test_shipped_flash_lite_is_cheap` |
| **A2** | Every rate unsourced | Each rate carries `source` (URL) and `checked` (date); the admin pricing panel shows rates older than 60 days | test: every rate has both fields | done (5f1ebbe) — every rate has `source` + `checked`; test `test_shipped_table_is_sourced_and_per_million` |
| **A3** | Wrong model names → wrong rate | Log the **real model id** as `model` and the purpose (`talk_portrait`, `encounter_resolve`…) as `operation`; `get_rate` never falls back across unit types — a unit mismatch is logged as unpriced and flagged, never guessed | test: an image event against a token default is unpriced + flagged | done — Gemini calls are logged at the HTTP call under the model in the URL; no fallback across unit types (test `test_no_fallback_across_unit_types`, `test_wire_logs_a_picture_once_under_its_real_model_and_size`) |
| **A4** | Paid calls never logged | Log inside each provider utility, where the HTTP call is made (one place per provider), not at each caller: `gemini_image_utils`, `ai_provider_manager`, `gemini_live_*`, `veo_video_utils`, `krea_image_utils`, `fal_image_utils`; then remove duplicate caller-side logs | A5 coverage ≥ 99% over a 20-turn playtest | partly — every Gemini `generateContent` over `requests` (text and pictures, incl. `ai_provider_manager` chat/vision) is logged at the wire and callers' own logs of the same call fold into it; Veo, Krea, fal, ElevenLabs still log at the caller |
| **A5** | No way to see what's missed | Coverage meter: one hook counts outbound requests to provider hosts per session; the admin dashboard shows requests vs ledger rows; below 99% is an alarm | playtest shows coverage per provider | open |
| **A6** | Units guessed when the provider reports them | Use the usage in the response wherever the API returns it (Gemini `usageMetadata`, including image output tokens; ElevenLabs where it reports characters/credits); zero-unit logs (`prop_jeep`, `camp_enter`, failed plates) take the reported usage | test: logged units equal the response's reported usage | partly — wire logs take Gemini `usageMetadata` tokens and count the pictures in the response |
| **A7** | Live video time trusted from the browser | Measure server-side: start when the server mints the Reactor token, end at session close or last heartbeat; the browser's report becomes a cross-check only | test: a zero `duration_seconds` post does not reduce the logged seconds | done — meter from token / talk session to report; charged the longer; reaped at the last `/api/feed` poll (test `test_live_time_meter_charges_what_the_browser_left_out`); `/api/reactor/usage` no longer refuses a low wallet |
| **A8** | Billed failures unknown | During A9, confirm per provider whether failed / safety-blocked calls are billed; where they are, log them with cost | noted per provider in §4a | open |
| **A9** | Ledger never checked against real bills | Reconcile weekly: ledger totals per provider vs Google Cloud billing, ElevenLabs usage, Reactor, Krea invoices; record the variance in §4a | ±10% per provider for 2 consecutive weeks | open |
| **A10** | History priced wrong | After A1/A3, re-price `usage_events.cost_usd` from stored units and rebuild `session_cost_rollup` (keep the old value in a column for audit) | admin dashboard and ACCOUNT show the corrected history | done — first start re-prices `cost_usd`, keeps `cost_usd_v1`, rebuilds rollups (test `test_history_is_repriced_once_keeping_the_old_value`) |
| **A11** | 28% of text calls fail | Reliability, not pricing — but failures cost time and possibly money; investigate from `usage_events.error_message` | failure rate by error, before/after | open |

### B. Transparency

| ID | What the player sees | Built from | Proof | Status |
|----|----------------------|-----------|-------|--------|
| **B1** | Every charge split: **models $0.05 · GOD $0.05** | ledger rows gain `charged_usd` and `markup`; the wallet debit is their sum | test: debit = Σ cost × markup, both stored | done — ledger rows carry `wallet`, `charged_usd`, `markup`; receipts show both |
| **B2** | A receipt per run: tap a RECENT line in ACCOUNT → story / pictures / motion / voice / live video, count and cost each; failed calls listed as "not charged" | `usage_events` grouped by session + service | screenshot of a real run | done — RECENT lines in ACCOUNT open to story / pictures / live video / voice, count, charge, failed-not-charged (test `test_receipts_split_a_run_and_keep_wallets_apart`) |
| **B3** | A rate card — WHAT THINGS COST in ACCOUNT and a public `/pricing` page: each thing a turn can use, provider cost, our price, and the date the rates were last checked against our bills | `pricing.json` + `markup()` — the same numbers the charge uses, so the card can't drift from the bill | test: card total for a sample turn = the charge for that turn | done — `/pricing` page, `/api/pricing`, "What things cost" in ACCOUNT, all from `billing.rate_card()` (test `test_rate_card_is_the_price_a_charge_uses`) |
| **B4** | Before expensive actions, the cost on the button: world GENERATE, live video per minute, the 4K cutscene | rate card × expected units | shown in the UI | open |
| **B5** | "About N turns" on ADD MONEY from the player's own recent average, not a guess | the player's last 20 turns | test | open |
| **B6** | Desktop app (own keys): the same receipts at provider cost, no markup — so a player can predict their own provider bill | same code, markup 1 | screenshot | done — desktop receipts at provider cost from the same code |

### C. Taking money

| ID | Issue | Fix (short) | Proof | Status |
|----|-------|-------------|-------|--------|
| **C1** | Turns are almost never charged | Charge the session's owner inside `cost_tracker.record_usage`, whenever a cost lands; remove the four `settle_session` delta calls | test: a cost logged from a background thread after the response is debited | done — `cost_tracker.record_usage` charges the wallet behind each cost (request cookie, or inherited by the thread / pool job that request started); `settle_session` retired (test `test_a_render_thread_charges_the_wallet_that_asked`, `test_charge_goes_negative_rather_than_lose_a_cost`) |
| **C2** | Anyone can spend anyone's wallet; no-cookie visitors bill the last sign-in | Wallet per device: random id in a signed HttpOnly cookie; no email sign-in; no store-wide fallback on a hosted server | test: two browsers → two wallets; forged cookie rejected | done — per-browser wallet id in a signed HttpOnly cookie, no sign-in, no store-wide fallback on a request (test `test_one_wallet_per_browser`, `test_no_store_wide_wallet_on_a_request`) |
| **C3** | A payment can land in the wrong wallet | Credit the account id written into the Checkout Session | test: redeem under another cookie credits the payer; webhook + return credit once | done — credits `metadata.account` (the wallet id); legacy `metadata.email` sessions still land |
| **C4** | Checkout return drops the player's game | Carry a validated same-origin return path through `success_url` / `cancel_url` | test: `?session=` kept; `//evil` refused | done — test `test_return_path_stays_in_the_game`, `test_checkout_request` |
| **C5** | Arcade coins double-charge if both on | Refuse coin gating when `FEATURE_BILLING` is on | test | done — the coin turn meter is never on while the wallet is (test `test_turn_meter_never_runs_on_the_wallet`) |
| **C6** | $12/mo Play plan unused | Drop from checkout | test: `kind=play` → 400 | done — `kind=play` → 400, checked on the test server |
| **C7** | **Money can't land at all on the installed Stripe library (15.5.1)** — Stripe objects are no longer dicts: `billing.redeem`'s `dict(cs.metadata)` raises TypeError, and `coinop.handle_webhook`'s `data.get(...)` raises AttributeError | Read Stripe objects with `obj["key"]` / `getattr` or `.to_dict()`; pin `stripe` to the major version tested (`requirements.txt` says only `>=10.0.0`) | test against a real test-mode session: return and webhook both credit once | done — `billing._plain`; `stripe>=15,<16` pinned; tests on real 15.5.1 objects |
| **C8** | Checkout offers only cards (`payment_method_types=["card"]`) | Drop the list and let the Dashboard's payment-method settings decide (the smoke test showed card, Link, Klarna, Cash App, Amazon Pay available) | checkout shows the Dashboard's methods | done — no `payment_method_types` |
| **C9** | Delayed methods (Klarna, Cash App, bank) finish **after** checkout completes | Handle `checkout.session.async_payment_succeeded` / `…_failed` in the webhook; credit only on `payment_status == "paid"` | test with a delayed-method test payment | done — webhook handles `async_payment_succeeded` / `_failed`; credit only on `paid` — test `test_signed_events_credit_once_and_async_lands` |
| **C10** | The webhook is a backup, the browser return is the main path | Make the webhook the source of truth (the return only shows "landing…" and refreshes); both stay idempotent on the checkout id | close the tab mid-checkout → still credited | in progress — webhook and return share `fulfill_checkout`; needs the Render endpoint + `STRIPE_WEBHOOK_SECRET` |
| **C11** | No receipt/invoice for the player | Checkout `invoice_creation` (payment mode) gives each top-up a Stripe invoice PDF; `customer_creation="always"` keeps one Stripe customer per wallet | a test top-up produces an invoice in the Dashboard | done — `invoice_creation` + `customer_creation="always"`; `invoice.paid` for a top-up no longer touches the plan |
| **C12** | Server uses the full secret key | A restricted key with only Checkout Sessions (write), Customers (write), Events/Webhooks (read) for the server | Render runs on the restricted key | open |
| **C13** | **Checkout refused on this account**: Managed Payments (Stripe as merchant of record) is on by default, and it refuses any product without a tax code and any `ui_mode` except `hosted_page` / `embedded_page` — so Checkout Studio's embedded *form* (`ui_mode: form`) can't run here | Tax code on every line item (`txcd_10201003`, "Video Games - streamed - non subscription - with limited rights"; override `STRIPE_TAX_CODE`); checkout drawn in the ACCOUNT sheet with `ui_mode: embedded_page` (`STRIPE_CHECKOUT_UI=hosted_page` to leave for Stripe instead) | test-mode session created both ways with Managed Payments on | done — Matt to confirm the tax code with Stripe or an accountant |
| **C14** | Many paid routes weren't gated (new run, encounters, narrator, music, look book…): an empty wallet could keep starting paid work | One `before_request` guard: on a wallet server every POST/PUT under `/api/` answers 402 while the wallet can't pay, except a short list of routes that never spend | new routes are gated by default | done |
| **C15** | Gemini Live prototype routes (`/api/detect/live`, `/ws/talk/live`) bill wall-clock on sockets nothing meters | Refused while the wallet is on | — | done |

Order: **C7 first** (small, and nothing can be tested end-to-end in test
mode without it) → **A1 → A3 → A4 + A5 → A6 → A7 → A10**, then start **A9**
(it needs two weeks of clean data, so everything after runs alongside it) →
**C2 → C3 → C10 → C9 → C1** (C1 depends on C2's account id, and B1 on C1) **→ B1 → B2 → B3 → B5 → C4 →
C8 / C11 / C12 → C5 / C6 → B4 / B6** → test-mode run (§7) → markup decision on reconciled
numbers (§5) → live.

---

## 3. A. Cost accuracy — detail

**A1 / A2 — the rate table is data with provenance.** Shape of a token rate
after the change:

```json
"gemini:gemini-3.1-flash-lite": {
  "unit_type": "tokens",
  "input_per_1m": 0.0,
  "output_per_1m": 0.0,
  "source": "https://ai.google.dev/pricing",
  "checked": "YYYY-MM-DD"
}
```

(Values deliberately left as 0.0 here — fill them from the price page on the
day, not from this plan or from memory.) Image rates need the resolution they
apply to where the provider prices by size (1K / 2K / 4K), and the logged
event needs the size, or the 4K cutscene is priced as a 1K frame.

**A3 — one name for the model, another for the purpose.** Today purpose is
stuffed into `model`, which is what breaks the rate lookup. `model` is the id
sent to the provider; `operation` is why. The rate lookup key stays
`provider:model`. `get_rate`'s `provider:default` fallback is kept only when
the default's `unit_type` matches the event's.

**A4 — log where the money is spent.** Each provider utility gets one
`_log(session_id, …)` at its HTTP call, with the response's usage. Callers
pass the session id and the operation. This is also what makes C1 correct:
background threads, retries and fallbacks all pass through the utility.

**A5 — the coverage meter.** A thin hook on the outbound HTTP client (the
repo uses `requests`; one `Session` with a response hook, or a patch of
`requests.post` in the provider utilities) counts calls to
`generativelanguage.googleapis.com`, `api.elevenlabs.io`, `api.openai.com`,
`api.anthropic.com`, Krea, fal, Reactor — per session. The admin analytics tab
shows calls vs ledger rows by provider. It's how we find the next unlogged
call instead of waiting for an invoice to disagree.

**A7 — live video.** The server already mints the Reactor token
(`/api/reactor/*` in `api.py`); it records the mint time per session and
closes the interval on the session's end, a switch of renderer, or 60 s
without a heartbeat. The browser's `duration_seconds` stays as a cross-check
logged beside it.

**A9 — reconciliation.** Weekly, by hand at first (a script later): for each
provider, the ledger's total for the week vs the provider's bill for the same
dates. Google: Cloud Billing reports (or the billing export) for the
Generative Language API; ElevenLabs: its usage page; Reactor and Krea: their
dashboards. Record in §4a. A provider off by more than 10% blocks C-part go-live
until explained.

**A10 — re-price history.** `cost_usd` is recomputed from the stored units
with the corrected table; the old value is kept in `cost_usd_v1`; the rollup
is rebuilt. Rows logged under wrong model names (A3) are mapped by operation.

---

## 4. B. Transparency — detail

**Principle: the player is shown the same numbers the charge is computed
from.** No separate "display prices".

- **B1 split charge.** A debit of $0.10 for a turn shows as
  `models $0.05 · GOD $0.05`. With markup 2 the split is even; whatever markup
  is chosen (§5) is visible, not buried.
- **B2 receipt.** The ACCOUNT sheet's RECENT lines expand in place, in the
  sheet's existing row style: one line per part of the turn loop, count × unit
  price. Failed calls appear as "3 not charged". Live video shows minutes.
- **B3 rate card.** A row per thing a turn can use (story line, picture,
  flipbook, sound, voice line, live video minute, world picture at 1K / 4K),
  provider cost → our price, with "rates checked against our provider bills on
  DATE". Public at `/pricing` so a player can read it before paying.
- **B4 cost on the button.** Where a single action costs noticeably more than
  a turn — world GENERATE at 4K, the opening cutscene, live video — the
  button or a line under it gives the price before it's pressed.
- **B5 turns per top-up** from the player's own average, so a player who uses
  live video sees a different number from one who doesn't.
- **B6 own keys.** On the desktop app the same receipts at markup 1, labelled
  "billed to you by the provider".

### 4a. Reconciliation log (A8 / A9)

| Week | Provider | Ledger | Bill | Variance | Failed calls billed? | Notes |
|------|----------|--------|------|----------|----------------------|-------|
| — | — | — | — | — | — | — |

---

## 5. Decisions for Matt

1. **Markup** — shown to players by B1 / B3, so pick one you're happy to
   show. Decide on reconciled numbers (after two weeks of A9), including
   Stripe's per-payment fee (roughly 2.9% + 30¢ in the US; check the Stripe
   pricing page for your account).
2. **Top-up amounts** — today $10 / $25 / $50 with bonus credit on the larger
   two ($27.50, $57.50). A bonus is a hidden discount; with a visible markup,
   flat amounts may read cleaner.
3. **Refund policy** for unused balance — Stripe expects terms, a refund
   policy and a support contact for digital goods.
4. **Sales tax** on prepaid credit — ask an accountant (Stripe Tax exists).
5. **Moving a wallet to another device** (emailed link) — needed at launch or
   after? Needs an email sender.
6. **Play plan** — dropping it (C6) unless you want a subscription.

---

## 6. C. Taking money — detail

**C2 — a wallet per device** (first: C1 and C3 key on its id). Today
`link_email` makes anyone who types an address that account, and
`_active_email` falls back to `store["active_email"]` — the last address
linked on the server — when a request has no cookie.
- `account_id = secrets.token_urlsafe(24)`, created on the first gated call or
  on opening ACCOUNT; cookie `god_account = <id>|<hmac>`, HttpOnly, `Secure`
  on https, SameSite=Lax, one year.
- Store keyed by `account_id`; `email` optional, filled from Stripe at redeem.
- Hosted mode never falls back to a store-wide account; the desktop app keeps
  its single local one.
- `POST /api/billing/account` goes; `DELETE` becomes "forget this device"; the
  ACCOUNT sheet drops SIGN IN.
- `SOMEWHERE_BILLING_SECRET` required when payments are on (today it falls
  back to the Stripe key, then a hard-coded dev string).
- Check `billing.json` on Render holds no paid balances before deploying.

**C3 — money goes to whoever paid.** `create_checkout` writes `account_id`
into `metadata` and `client_reference_id`; `redeem` credits
`metadata.account_id`, never `current_account()`; one store-wide set of
redeemed checkout ids under the lock (safe at one gunicorn worker); webhook and
return share the path.

**C1 — charge when the cost happens.** `engine.api_choose` hands the turn to a
thread and returns, so the before/after read in `_credit_gated_choose` sees
≈$0. Instead:
- `_spend_blocked()` records `billing.bind_session(session_id, account_id)`.
- End of `cost_tracker.record_usage`: if a priced event was inserted and
  `billing.requires_wallet()`, `billing.charge_session(session_id, cost)`
  debits the owner `cost × markup` and writes `charged_usd` / `markup` onto the
  ledger row (B1). Never raises; `billing` imported lazily.
- Remove the four `billing.settle_session` calls.
- Unowned costs (world-editor `wf-*`, warm-ups, admin) charge nobody and add to
  a visible `house_usd`.
- The wallet may go below zero by what was already in flight (at most one
  turn); `gate()` blocks the next action.

**C4 — back to the game.** Client sends `return_to = pathname + search`;
accepted only if it starts with `/play` or `/standalone` with no `//`, scheme
or backslash; used for both return URLs.

**C5 / C6.** Coin gating refused when `FEATURE_BILLING` is on; checkout
refuses `kind=play`.

### 6b. Stripe review (2026-09-21, test mode)

Checked against the sandbox with the test keys (kept on Matt's machine in
`stripe_test.env`, gitignored by `*.env`, never committed):

- Account `acct_1UIBNrFLvfCv690K`, "Art Almost LLC sandbox", US / USD,
  `business_profile.url = https://5th-corner.com`, no support email set,
  `details_submitted: false` (activation only matters for live mode). No
  products, no webhook endpoints, test balance $0.
- A $10 Checkout Session built the way `billing.create_checkout` builds it
  opens fine in test mode — the payment page works.
- The installed `stripe` is 15.5.1: C7 above. Both credit paths fail today.
- Which Stripe products this needs:
  - **Payments (Checkout)** — yes: one-off top-ups into the wallet.
  - **Invoicing** — only as receipts for top-ups (C11); no invoices are
    sent to players otherwise.
  - **Billing** — not needed for prepaid top-ups. Stripe Billing's usage
    meters and credit grants are an alternative to our own wallet, but they
    are built around subscriptions and invoices, and our own ledger has to
    exist anyway for cost accuracy (A) and receipts (B). Revisit only if a
    subscription comes back (C6).
  - **Treasury** — not applicable: it is for platforms offering financial
    accounts to their own users through Connect, not for a game selling
    credit.
- The secret key was shared in a chat transcript. It is a test key (sandbox
  only), but roll it in the Dashboard once setup is done.

### Setup (Matt — needs your identity and bank details)

1. Stripe: activate the account (business details, bank, identity). Test mode
   first.
2. Render → Environment: `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`,
   `STRIPE_WEBHOOK_SECRET`, `PUBLIC_BASE_URL`, `SOMEWHERE_BILLING_SECRET`
   (random, 32+ characters). Leave `FEATURE_COINOP` unset.
3. Stripe → Webhooks: `https://<host>/webhook/stripe`,
   `checkout.session.completed`.
4. Terms / refund / support pages, and the public `/pricing` page (B3), linked
   from the site.
5. After §7 passes and A9 is green: live keys.

---

## 7. Test-mode run on Render (before live keys)

- Two browsers → two empty wallets.
- Top up $10 with card `4242 4242 4242 4242` → credited once; webhook fired;
  back in the same game.
- Play ten turns including one encounter and one live-video minute → the
  wallet's drop equals the sum of the run's receipt (B2), and the receipt's
  model cost matches the ledger rows for that session; coverage meter (A5) ≥
  99% for the run.
- Rate card (B3) × the run's counts = the charge.
- Wallet to zero → next action 402, sheet opens on ADD MONEY.
- Close the tab mid-checkout, pay, reopen → credited by the webhook.
- Cancel checkout → nothing charged.

---

## 8. Not in this plan

- Moving `billing.json` to a database (fine at one gunicorn worker on the
  persistent disk).
- Per-part "who pays" (wallet for pictures, own key for story) on a hosted
  server.
- Refunds from the app (Stripe dashboard).
