# COIN-OP MVP — go-to-market setup

The arcade "insert coin" money loop. Ships dark by default; turning it on
takes ~10 minutes of Stripe dashboard + env vars.

**What it does today:** two payment loops on the same Stripe SKU.

* **Death continue** — when the player dies, they see an "Insert Coin —
  Continue" button in green next to the red "RESTART SIMULATION". Click →
  redirected to Stripe Checkout → pay (default \$0.99) → redirected back →
  server verifies with Stripe → engine revives them on the same run.
* **Arcade credit meter** *(new — see §11)* — always-visible top-right
  chip showing remaining credits + total spent. Every turn spends 1 credit.
  When the meter hits zero the world freezes with an "INSERT COIN" pause
  overlay. Same button, same one-click flow, same Stripe SKU — server
  decides revive-vs-topup from actual player state. Deploy dark first,
  then flip `COINOP_CREDIT_GATING=1` when you're ready.

**Ship-it checklist for the first real dollar:**

1. Complete §1 (Stripe account setup) once.
2. Set the three required env vars in §2 with your **live** `sk_live_...`
   / `pk_live_...` keys.
3. Redeploy. That's it — the button appears on death and the game keeps
   working exactly as before for anyone who doesn't click it.
4. **(Optional, do next)** flip `COINOP_CREDIT_GATING=1` to turn on the
   arcade meter for continuous monetization, not just deaths.

**What it does NOT do (yet):** no credit packs, no saved cards, no Apple/Google
Pay one-tap, no PayPal Micropayments, no crypto rails. Those are all in the
plan (`COIN_OP_MONETIZATION_PLAN.md`).

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
| `COINOP_CREDIT_GATING` | no | `0` | Set to `1` to enable the arcade credit meter (see §11). Off by default so the paid death-continue flow ships independently. |
| `COINOP_FREE_STARTING_CREDITS` | no | `10` | Free credits granted the first time a session's balance is checked (only fires when gating is on). Enough to fall in love with the game before the first "insert coin" prompt. |
| `COINOP_CREDITS_PER_COIN` | no | `20` | Credits granted per successful checkout — paid, comp, or test-mode. |

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

## 4. Where the button appears

The continue button lives inside the death overlay, which is shared by all
three renderer modes:

- **`/play` / `/standalone`** — image (stills) mode.
- **`/play?renderer=reactor` or `/realtime` or `/live`** — realtime world-model
  mode.
- **Discord embedded app** iframe — same underlying page, same overlay.

In realtime mode two additional things happen automatically on a coin-op
revive that don't apply to stills mode:

- The paused reactor stream (`ReactorRenderer.pause()` fires on death) is
  resumed via `ReactorRenderer.resume()`, so the live video keeps flowing.
- The client-side `DangerSystem` is reset to `SAFE` with a full HP bar, so
  the player doesn't immediately re-die from a lingering peripheral-vignette
  damage state. (This matters because a realtime session can die *client-side*
  from vision-driven HP damage before the server's own death verdict fires;
  the revive works uniformly in both cases.)

Both are handled inside the client's `exitGameOverAndResume()` helper; no
per-mode branching or configuration is required.

---

## 5. End-to-end test (Stripe test mode)

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

## 6. Going live

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

## 7. Operating notes

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

## 8. Free-play testing & influencer links

Two ways to give someone free continues without a real charge — pick whichever
fits.

### Option A — Global "test mode" (best for staging / your own dogfooding)

Set `COINOP_TEST_MODE=1` on any deploy. Every continue on that deploy is free
and never touches Stripe — the button just relabels itself
**⚡ TEST MODE — FREE CONTINUE** in gold, and clicking it revives the player
immediately.

Recommended pattern: enable this on a **preview / staging URL** (a second
Render service pointing at the same repo but a `staging` branch, for example),
never on the production one. Every dev + tester who lands on the staging URL
gets the full paid flow for free.

```env
FEATURE_COINOP=1
COINOP_TEST_MODE=1
STRIPE_SECRET_KEY=sk_test_...   # still needed so the module considers itself
STRIPE_PUBLISHABLE_KEY=pk_test_... # "enabled"; the keys are never actually called
```

### Option B — Named comp codes (best for influencer / friends-and-family links)

On any deploy (including production), add an allowlist of "comp" codes and a
per-code global cap:

```env
FEATURE_COINOP=1
COINOP_FREE_PLAY_CODES=alpha,influencer-jane,podcast-ep12,gdc26
COINOP_FREE_PLAY_CAP=50
```

Then hand out a URL to a specific person:

- **`https://somewhere.example.com/play?comp=influencer-jane`**

When they land on it:

1. The client picks up `?comp=influencer-jane`, saves it to `sessionStorage`,
   and strips it from the visible URL (no code showing in screen recordings).
2. `/api/coinop/config?comp=influencer-jane` responds with a `comp.active: true`
   block, so the continue button renders in **gold** as
   **⚡ COMP — FREE CONTINUE (49 left)** instead of the paid label.
3. Clicking it does the exact same UX ceremony as a paid continue — same
   coin-drop sound, same "the transmission stutters back to life" line, same
   revive state — but skips the Stripe hop entirely. One click, one revive.

Details worth knowing:

- **Codes are case-insensitive.** `?comp=Jane` matches an allowlist entry of
  `jane`.
- **The cap is global per code**, not per person. Setting the cap to `50` and
  handing `influencer-jane` to a podcast host means the *whole world* using
  that link gets at most 50 free continues combined. Once exhausted the button
  reverts to the paid flow automatically.
- **Codes never grant anything except the "continue" feature.** They don't
  unlock unlimited plays or bypass other paid features.
- **Every comp is logged.** Each session's `sessions/<sid>/coinop.json` gets a
  `grants` entry with `source: "comp"` (vs `source: "stripe"` for real
  payments) so analytics / tax / dashboards can cleanly split the two.
- **The global counter file** lives at `sessions/_coinop_comp_counters.json`
  and just tracks `{code: uses}` — safe to delete if you want to reset a code.
- **Forging is server-checked.** Comp voucher ids look like `comp_<hex>` and
  are only accepted at redemption if the server itself minted them for the
  caller's session. Hand-typing a `comp_...` in the URL bar does nothing.

### Which one to use when

| Situation | Use |
|---|---|
| Internal QA on a staging URL | `COINOP_TEST_MODE=1` |
| Give ONE influencer / streamer 20 free continues | `COINOP_FREE_PLAY_CODES=name-of-influencer` + link |
| Onboard beta testers with different tracked cohorts | `COINOP_FREE_PLAY_CODES=beta-slack,beta-discord,beta-newsletter` |
| Convention floor / demo laptop | `COINOP_TEST_MODE=1` on that specific deploy |
| Anyone with any code, on prod | Do NOT set `COINOP_TEST_MODE=1` on prod. Use codes only. |

### Sharing a link with an influencer — copy/paste example

> Hey Jane — this is a private preview link. It bypasses payment for you, so
> continues are free while you play (up to 50 total across your audience if you
> stream it). Every time you die you'll see a gold **⚡ COMP — FREE CONTINUE**
> button next to the red RESTART — one click and you're back in the run. If it
> ever shows the regular $0.99 button instead, the comp budget was used up;
> just DM me and I'll bump it.
>
> **Stills (photorealistic AI scenes):** <https://somewhere.example.com/play?comp=influencer-jane>
> **Realtime (live AI video, best in Chrome desktop):** <https://somewhere.example.com/live?comp=influencer-jane>
> **Lobby (recommended — pick your own run name, resume saved runs):** <https://somewhere.example.com/lobby?comp=influencer-jane>
> **Root also works:** <https://somewhere.example.com/?comp=influencer-jane>
>
> The comp code works on all of them. Same gold button, same one-click revive.

The `?comp=` param is honored by every entry point: `/`, `/lobby`, `/play`,
`/standalone`, and `/live` (a.k.a. `/realtime`).

* On the **lobby** it flashes a small gold `COMP · <code>` chip in the top-left
  HUD (next to REC/clock) so the tester knows the token registered, then rides
  through every "New Game", "Continue", and join-by-code exit into
  `/play?session=<id>&comp=<code>` — so a fresh run started from the lobby
  still gets free continues without you having to hand-craft the play URL.
* On **`/`**, the query string is preserved through the redirect to `/lobby`
  (so `?comp=<code>` is not silently dropped when someone shares the naked
  domain).
* On **`/play`** / **`/live`** / **`/standalone`** it's stripped from the URL
  on load and stashed in `sessionStorage` for the tab's lifetime, so a
  death → revive → new run cycle in the same tab keeps working without
  needing the code back in the URL. The lobby uses the SAME `sessionStorage`
  key, so a comp picked up on `/lobby` also survives if the URL forwarding
  ever misses an exit for any reason.

---

## 11. Arcade credit economy (`COINOP_CREDIT_GATING=1`)

The paid death-continue MVP is orthogonal to a second, richer mechanic —
the **coin meter**. When gating is on:

- Each session starts with `COINOP_FREE_STARTING_CREDITS` (default: 10) free
  credits, granted on the first request that touches the ledger. The
  starter is a one-shot flag — a returning player who ran their free tier
  to zero can't refresh their way back into more free turns.
- Every successful `/api/choose` (i.e. every player action / turn) spends 1
  credit, atomically, server-side. The debit is guarded by the same lock
  as the death-continue grants ledger, so concurrent turns for the same
  session can't over-spend.
- When the balance hits zero and the client tries another turn, the server
  responds `HTTP 402` with `{"needs_coin": true, "balance": 0}` and does
  NOT process the turn — the client pops the **INSERT COIN** pause overlay
  and freezes the world visually until a top-up lands.
- Every successful checkout (paid Stripe, comp code, or test-mode) grants
  `COINOP_CREDITS_PER_COIN` (default: 20) credits — same pack size across
  all three payment paths. Paid credits accrue toward the "SPENT $X.XX"
  subtitle on the HUD; comp/starter credits do not.

### What the player sees

- **Always-visible HUD chip** in the top-right, just below the REC
  timecode: `◉ 07 · $1.98` — remaining credits + total spent this run.
  Pulses amber at ≤ 2, red at 0, and glows green briefly on any top-up.
  Clickable — a click opens the pause overlay so a player can proactively
  buy more before hitting zero.
- **"INSERT COIN" pause overlay** when they run out mid-turn: same
  coin-op button, same C-to-continue keyboard shortcut, same coin-drop
  and phosphor return ceremony as the death overlay — just framed as
  "keep playing" instead of "revive". The world dims / grayscales
  behind it so the pause feels physical.

### How the payment endpoint decides what to do

One redeem endpoint serves both flows:

- If the player is **dead** when the redeem lands → mint a revive (same
  as before), *and* grant `COINOP_CREDITS_PER_COIN` credits on top so
  they can immediately keep playing.
- If the player is **alive** but the meter emptied → just grant the
  credits, dismiss the pause overlay, and resume.

The client doesn't have to know which of those two happened — the
server figures it out from the actual player state on redeem. Same
Stripe SKU covers both.

### Testing the flow

The fastest end-to-end test:

```bash
export FEATURE_COINOP=1
export COINOP_CREDIT_GATING=1
export COINOP_TEST_MODE=1             # every checkout is free — no Stripe hit
export COINOP_FREE_STARTING_CREDITS=3 # small so you see the wall fast
export COINOP_CREDITS_PER_COIN=5      # small so you can watch the meter refill
export STRIPE_SECRET_KEY=sk_test_dummy
export STRIPE_PUBLISHABLE_KEY=pk_test_dummy
python api.py
```

Open `/lobby`, start a new run, make ~3 choices. On the 4th click the
"INSERT COIN" overlay fires. Click **Insert Coin**, watch the coin-drop
+ ceremony, get 5 credits granted for free (test mode = comp), overlay
dismisses, meter reads `◉ 05 · $0.00`, game resumes.

### Deploy sequencing

Turning on the meter is a **behavior change**, not just a UI reveal.
Ship it separately from the paid death-continue MVP:

1. Deploy with `FEATURE_COINOP=1` and `COINOP_CREDIT_GATING=0` (default).
   You get the death-continue flow, no meter, no gating. Zero change to
   the normal player experience.
2. Once payments are proven and the arcade meter feels good in a preview
   deploy, flip `COINOP_CREDIT_GATING=1` on the production env vars.
   All active sessions get the free starter on their next turn; new
   sessions get it on their first.

---

## 9. What's next (post-MVP)

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
