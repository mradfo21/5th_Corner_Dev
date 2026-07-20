# COIN-OP MVP — go-to-market setup

The single-charge "insert coin to continue" feature. Ships dark by default;
turning it on takes ~10 minutes of Stripe dashboard + env vars.

**What it does:** on death, the player sees an "Insert Coin — Continue"
button in green next to the red "RESTART SIMULATION" button. Click →
redirected to Stripe Checkout → pay (default \$0.99) → redirected back →
server verifies with Stripe → engine revives the player on the same run.

**What it does NOT do (yet):** no credit packs, no saved cards, no Apple/Google
Pay one-tap, no PayPal Micropayments, no crypto rails. Those are all in the
plan (`COIN_OP_MONETIZATION_PLAN.md`); this MVP is *just* the plumbing that
gets the first real dollar in.

---

## 1. Stripe account setup (once)

1. Create / log in to your Stripe account: <https://dashboard.stripe.com/>.
2. Start in **test mode** (toggle top-right of the dashboard).
3. Go to **Developers → API keys**. Copy:
   - `Publishable key` → will become `STRIPE_PUBLISHABLE_KEY`
   - `Secret key` (reveal it, then copy) → `STRIPE_SECRET_KEY`
4. (Optional but recommended) **Developers → Webhooks → Add endpoint**:
   - Endpoint URL: `https://<your-public-host>/webhook/stripe`
   - Events to send: `checkout.session.completed`
   - After creating, click the endpoint and reveal `Signing secret` → `STRIPE_WEBHOOK_SECRET`
5. **Settings → Business → Public details** — fill in your business name and
   support email. Stripe requires this before you can flip to live mode.
6. When you're ready to accept real money: flip the dashboard to **live mode**,
   re-copy the live keys, redeploy with them. That's the entire "go live" step.

---

## 2. Environment variables

Set these on your server (Render dashboard → Environment for a hosted deploy,
or your local `.env` for dev).

| Variable | Required | Default | Notes |
|---|---|---|---|
| `FEATURE_COINOP` | **yes** | *(unset = disabled)* | Set to `1` to enable. Any other value = off. |
| `STRIPE_SECRET_KEY` | **yes** | — | `sk_test_...` or `sk_live_...`. |
| `STRIPE_PUBLISHABLE_KEY` | **yes** | — | `pk_test_...` or `pk_live_...`. |
| `STRIPE_WEBHOOK_SECRET` | no | — | `whsec_...`. Enables `/webhook/stripe`. MVP works without it. |
| `COINOP_CONTINUE_PRICE_CENTS` | no | `99` | Cents charged per continue. Min \$0.50 (Stripe minimum). |
| `COINOP_CONTINUE_CURRENCY` | no | `usd` | ISO-4217. Lowercase. |
| `COINOP_CONTINUE_LABEL` | no | `Insert Coin — Continue` | Text on the button. |
| `COINOP_PRODUCT_NAME` | no | `SOMEWHERE — Continue` | Line-item name shown to the buyer on the Stripe page + receipt. |
| `PUBLIC_BASE_URL` | no | *(derived from request)* | Set if your server sits behind a proxy / load balancer that doesn't preserve `Host`. e.g. `https://somewhere.example.com`. |

**If any of `FEATURE_COINOP`, `STRIPE_SECRET_KEY`, or `STRIPE_PUBLISHABLE_KEY`
is missing, the feature stays fully dark** — the client's `/api/coinop/config`
returns `{"enabled": false}` and the continue button is never rendered. Zero
risk to the existing game.

---

## 3. Dependencies

`stripe>=10.0.0` is now in `requirements.txt`. If your deploy pipeline
doesn't auto-reinstall, run:

```bash
pip install -r requirements.txt
```

---

## 4. End-to-end test (Stripe test mode)

1. Set env vars with `FEATURE_COINOP=1` + your `sk_test_...` / `pk_test_...`
   keys. Restart the server.
2. `curl https://<your-host>/api/coinop/config` — should return:
   ```json
   {
     "enabled": true,
     "publishable_key": "pk_test_...",
     "price_cents": 99,
     "currency": "usd",
     "label": "Insert Coin — Continue",
     "display_price": "$0.99 "
   }
   ```
3. Open `/play?session=test1` in your browser and get the player killed.
4. In the death overlay, the green **Insert Coin — Continue ($0.99)** button
   should be visible above the red RESTART button.
5. Click it → you land on Stripe Checkout.
6. Use Stripe's test card: `4242 4242 4242 4242`, any future expiry, any CVC,
   any zip.
7. Complete payment → you'll be redirected back to `/play?session=test1` and
   the death overlay should dismiss within a second or two, with new choices
   rendered under a *"A coin drops. The transmission stutters back to life."*
   line.
8. In the Stripe dashboard **Payments** view you should see the \$0.99 test
   charge with `game_session_id: test1` in the metadata.

Try the negative paths too:
- **Cancel** on the Stripe page → returns with `?coinop=cancel` → status text
  says *"Checkout cancelled. You can still restart."* and the death overlay
  stays put.
- **Refresh the return URL** → server sees the checkout is already redeemed
  → response is `{ok: true, already_redeemed: true}` → no double revive.
- **Replay someone else's return URL against your own session** → server
  refuses with `session_mismatch` and no revive is granted.

---

## 5. Going live

1. In the Stripe dashboard, flip to **Live mode** and complete Stripe's
   activation checklist (business details, bank account).
2. Copy your **live** `sk_live_...` + `pk_live_...` keys.
3. (If using webhooks) create the endpoint again in live mode; copy the new
   `whsec_...` signing secret.
4. Update env vars on the server. Restart.
5. Run the end-to-end test again with a real card for \$0.99 (you can refund
   yourself from the dashboard).
6. Watch the first live payment land. That's the entire launch.

---

## 6. Operating notes

- **Refunds:** issue from the Stripe dashboard (`Payments → click a payment
  → Refund`). Refunding does NOT auto-un-revive the player — this is fine at
  MVP volume; the effect of a revive is small and unrewinding it would be
  disruptive. Track this manually until we build a proper revocation flow.
- **Chargebacks:** Stripe emails you. \$15 fee per dispute + the disputed
  amount. At \$0.99/continue this is painful — one dispute wipes ~15 sales.
  Enable **Stripe Radar** (on by default on standard pricing) and add a
  simple rule: block > 5 attempts per hour per IP.
- **Disputes / support:** put a support email in the Stripe dashboard's
  public details; the buyer sees it on their receipt and statement.
- **Per-session grants file:** each game session writes a small JSON file at
  `sessions/<session_id>/coinop.json` tracking redeemed checkout ids. Do not
  delete these while a run is live — replays of the return URL would
  otherwise re-trigger a revive (server would refuse via Stripe's own
  `payment_status`, but the file is the fast local dedup).
- **Metrics you actually want:** count of `checkout.session.completed`
  webhook events / day (revenue), count of successful `/api/coinop/redeem`
  calls (delivered), and the ratio. A gap between the two means paid users
  aren't getting their revive — usually a webhook or return-URL misconfig.

---

## 7. What's next (post-MVP)

See `COIN_OP_MONETIZATION_PLAN.md` for the full roadmap. Nearest wins:

1. **Express Checkout Element (Apple Pay / Google Pay / Link)** — replace
   the redirect-to-Stripe-Checkout with an in-widget one-tap flow. Massive
   mobile conversion lift.
2. **Credit packs** — sell 5 continues at \$4.99 to drop the effective per-
   continue fee from ~33% to ~9%.
3. **Saved cards** — one-tap continues for returning users after the first
   purchase.
4. **PayPal Micropayments as a second rail** — the only card-adjacent rail
   where a true \$0.50 charge is viable (~23% fee vs Stripe's 33% at \$0.99).
5. **USDC-on-Solana** rail (via Helio) — the only rail where a real 50-cent
   charge actually nets ~49 cents.
