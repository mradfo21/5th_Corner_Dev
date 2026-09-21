# Taking money: launch steps

How GOD takes money on the web, and what you need to do once. The code side
is done; everything below is dashboard work only you can do (your identity,
your bank, your keys).

## How it works (one paragraph)

**Desktop app:** unchanged. Players use their own keys (ACCOUNT → keys), GOD
charges nothing, and ACCOUNT shows what their keys cost at each provider's
rates.

**Web (Render):** each browser gets its own wallet the first time it opens
the game. There's no sign-in. Players open ACCOUNT → ADD MONEY, and Stripe's
checkout opens inside the sheet. Every paid model call a run makes is
charged to that wallet as it happens, at provider cost × `BILLING_MARKUP`
(currently 2). When the wallet is empty, anything that would spend answers
"add money".

Players can see:

- a receipt for each run (tap a RECENT line);
- the public rate card at `/pricing`.

Stripe is the merchant of record (Managed Payments). It works out, collects
and files sales tax / VAT, and sends the receipt.

## 1. Stripe dashboard, once

1. **Activate the account** (live mode). Stripe asks for business details,
   identity and bank account.
2. **Support email:** Settings → Business → Public details. It appears on
   receipts, and it's where players will write if they lose a wallet.
3. **Managed Payments:** Settings → Managed Payments. Check that it's on
   (it's on for your sandbox).
4. **Tax code.** Every top-up is sold as `txcd_10201003`, "Video Games -
   streamed - non subscription - with limited rights". If Stripe or your
   accountant says otherwise, set `STRIPE_TAX_CODE` on Render to the right
   code. No code change is needed.
5. **Checkout branding** (optional): Settings → Branding. It sets the colours
   of the checkout drawn in the sheet.

## 2. Webhook, once per mode (test, then live)

The webhook is how money lands even if the player closes the tab
mid-payment. It also lands Klarna and bank payments, which clear later.

1. Stripe dashboard → **Workbench → Webhooks → Add destination**.
2. Endpoint URL: `https://<your Render address>/webhook/stripe`.
3. Events, exactly these three:
   - `checkout.session.completed`
   - `checkout.session.async_payment_succeeded`
   - `checkout.session.async_payment_failed`
4. Save, then copy the **signing secret** (`whsec_…`).

## 3. Render environment, once per mode

Render dashboard → the game service → **Environment**. Add:

| Key | Value |
|---|---|
| `STRIPE_SECRET_KEY` | `sk_test_…` first, `sk_live_…` (or a restricted `rk_live_…`) at launch |
| `STRIPE_PUBLISHABLE_KEY` | `pk_test_…` first, then `pk_live_…` |
| `STRIPE_WEBHOOK_SECRET` | the `whsec_…` from step 2 (test and live each have their own) |
| `PUBLIC_BASE_URL` | the game's public address, e.g. `https://play.5th-corner.com` |

These are already set in `render.yaml`, so leave them alone:

- `FEATURE_BILLING=1`
- `BILLING_MARKUP=2`
- `SOMEWHERE_BILLING_PATH`, which puts the wallets on the persistent disk

Save and let it redeploy.

**Restricted key (recommended for live):** Developers → API keys → Create
restricted key, with **Checkout Sessions: Write** and everything else None.
Use the `rk_live_…` as `STRIPE_SECRET_KEY`.

## 4. Test run on Render (test keys), about 5 minutes

1. Open the game on Render in a private window.
2. ACCOUNT → ADD MONEY → $10 → PAY. Checkout opens in the sheet.
3. Pay with card `4242 4242 4242 4242`, any future date, any CVC, any postcode.
4. The sheet says **Money added** and the wallet shows $10.00.
5. Play two or three turns. Back in ACCOUNT, the wallet has gone down and
   RECENT has a **Play** line. Tap it for story / pictures / voice.
6. Open `/pricing`. The table should load.
7. In the Stripe dashboard (test mode) → Payments, the payment is there with
   an invoice. Webhooks → your endpoint shows the events delivered (200).

If step 4 says "still clearing", the webhook isn't reaching the server.
Check the URL and `STRIPE_WEBHOOK_SECRET`.

## 5. Go live

Swap the three Stripe values on Render for the live ones (live webhook
secret too), and redeploy. Then do one real $10 top-up yourself and refund
it from the dashboard.

## Playing free yourself

Open `https://<your Render address>/owner?token=<ADMIN_TOKEN>` once in each
browser you play in. That wallet shows **∞** and is never charged; costs
are still logged, so you can see what your own play costs. To undo it, add
`&off=1`. `ADMIN_TOKEN` is the one already set on Render for `/admin`.

## Things to know

- **A wallet lives in one browser.** If a player clears cookies, it's gone
  from their side. The money is still in `sessions/_billing/billing.json`
  under a wallet id, and each payment records its Stripe checkout id.
  Support can find the wallet from the player's receipt and move the
  balance.
- **New visitors start at $0.** There's no free credit yet. If you want a
  free first taste, say how much and it's a small change.
- **Refunds:** refund in the Stripe dashboard. The wallet isn't reduced
  automatically. Take it down by hand, or ask for that to be wired to the
  `charge.refunded` event.
- **Rotating the Stripe key is safe.** Wallet cookies are signed with their
  own secret, kept on the disk at `sessions/_billing/billing_secret`.
