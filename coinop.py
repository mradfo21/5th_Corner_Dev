"""
coinop.py — SOMEWHERE monetization MVP.

The 80s-arcade "insert coin to continue" feature. When a player dies, they can
pay a small amount via Stripe Checkout to get one revive on the current run.

Design goals (MVP):
  * Simplest possible integration with Stripe: hosted Checkout, no embedded
    Payment Element, no saved cards, no credit packs. One button → one charge
    → one revive.
  * Zero impact on the game when disabled. If FEATURE_COINOP is not set or if
    Stripe keys are missing, /api/coinop/config returns {"enabled": false} and
    the client never renders the button.
  * Webhook is nice-to-have, not required. Redemption re-fetches the Checkout
    Session from the Stripe API and trusts payment_status='paid' as the source
    of truth. Webhook, if configured, adds belt-and-suspenders coverage.

Environment variables:
  FEATURE_COINOP                  "1" to enable (default off)
  STRIPE_SECRET_KEY               sk_test_... or sk_live_...
  STRIPE_PUBLISHABLE_KEY          pk_test_... or pk_live_... (surfaced to client)
  STRIPE_WEBHOOK_SECRET           whsec_...  (optional; if set, /webhook/stripe verifies)
  COINOP_CONTINUE_PRICE_CENTS     integer cents (default: 99)
  COINOP_CONTINUE_CURRENCY        ISO 4217 (default: usd)
  COINOP_CONTINUE_LABEL           button label (default: "Insert Coin — Continue")
  COINOP_PRODUCT_NAME             Stripe line-item name (default: "SOMEWHERE — Continue")
  PUBLIC_BASE_URL                 e.g. https://somewhere.example.com; falls back to request.host_url

Free-play (dev / QA / influencer):
  COINOP_TEST_MODE                "1" makes ALL continues free (no Stripe hit).
                                  Intended for staging / preview deploys ONLY.
  COINOP_FREE_PLAY_CODES          comma-separated allowlist of "comp" codes,
                                  e.g. "alpha,beta,influencer-jane,gdc26".
                                  Any player who lands on /play?comp=<code>
                                  gets free continues (up to the cap below).
  COINOP_FREE_PLAY_CAP            max free continues per code, globally
                                  (default: 100). A leaked link can't drain
                                  more than this before the button reverts to
                                  the normal paid flow.

Arcade credit economy (the "insert coin to keep playing" loop):
  COINOP_CREDIT_GATING            "1" turns on the meter: every /api/choose
                                  turn spends 1 credit, and when the balance
                                  hits zero the server blocks the next turn
                                  (HTTP 402) so the client can pop the
                                  "OUT OF COINS" pause overlay. Off by default
                                  so the paid death-continue flow can ship
                                  independently of the meter.
  COINOP_FREE_STARTING_CREDITS    credits granted on the first look at a
                                  brand new session (default: 10). Enough to
                                  let a first-time visitor fall in love with
                                  the world before the first insert-coin prompt.
  COINOP_CREDITS_PER_COIN         credits granted per successful $0.99 (or
                                  whatever price_cents is) Stripe checkout
                                  (default: 20). Also the pack size a comp /
                                  test-mode redemption grants.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import stripe  # type: ignore
except Exception:  # noqa: BLE001
    stripe = None  # type: ignore

log = logging.getLogger("coinop")

# ─── Config ─────────────────────────────────────────────────────────────

_DEFAULT_PRICE_CENTS = 99
_DEFAULT_CURRENCY = "usd"
_DEFAULT_LABEL = "Insert Coin — Continue"
_DEFAULT_PRODUCT_NAME = "SOMEWHERE — Continue"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _cfg() -> Dict[str, Any]:
    return {
        "feature_flag": os.environ.get("FEATURE_COINOP", "").strip() in ("1", "true", "on", "yes"),
        "secret_key": os.environ.get("STRIPE_SECRET_KEY", "").strip(),
        "publishable_key": os.environ.get("STRIPE_PUBLISHABLE_KEY", "").strip(),
        "webhook_secret": os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip(),
        "price_cents": _int_env("COINOP_CONTINUE_PRICE_CENTS", _DEFAULT_PRICE_CENTS),
        "currency": os.environ.get("COINOP_CONTINUE_CURRENCY", _DEFAULT_CURRENCY).strip().lower() or _DEFAULT_CURRENCY,
        "label": os.environ.get("COINOP_CONTINUE_LABEL", _DEFAULT_LABEL).strip() or _DEFAULT_LABEL,
        "product_name": os.environ.get("COINOP_PRODUCT_NAME", _DEFAULT_PRODUCT_NAME).strip() or _DEFAULT_PRODUCT_NAME,
        "public_base_url": os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/"),
        "test_mode": os.environ.get("COINOP_TEST_MODE", "").strip() in ("1", "true", "on", "yes"),
        "free_play_codes": _parse_codes(os.environ.get("COINOP_FREE_PLAY_CODES", "")),
        "free_play_cap": _int_env("COINOP_FREE_PLAY_CAP", 100),
        "credit_gating": os.environ.get("COINOP_CREDIT_GATING", "").strip() in ("1", "true", "on", "yes"),
        "free_starting_credits": max(0, _int_env("COINOP_FREE_STARTING_CREDITS", 10)),
        "credits_per_coin": max(1, _int_env("COINOP_CREDITS_PER_COIN", 20)),
    }


def _parse_codes(raw: str) -> set:
    """Parse a comma-separated env var into a normalized set of comp codes.

    Codes are normalized: trimmed, lowercased. That way the URL param match
    is case-insensitive — a link shared as ?comp=Jane still hits an
    allowlist entry of `jane`, which is what humans expect from a code.
    Empty entries are dropped so a stray comma doesn't accidentally allow
    an empty ('') code."""
    return {p.strip().lower() for p in (raw or "").split(",") if p.strip()}


def _normalize_code(code: Optional[str]) -> str:
    return (code or "").strip().lower()


def is_enabled() -> bool:
    c = _cfg()
    if not c["feature_flag"]:
        return False
    if stripe is None:
        log.warning("coinop: stripe python package is not importable; feature disabled")
        return False
    if not c["secret_key"] or not c["publishable_key"]:
        log.warning("coinop: missing STRIPE_SECRET_KEY or STRIPE_PUBLISHABLE_KEY; feature disabled")
        return False
    return True


def is_credit_gating_enabled() -> bool:
    """Should turns be metered against a per-session credit balance?

    Separate from `is_enabled()` on purpose. The paid death-continue flow
    can ship without the arcade meter (that's the original MVP). Turning
    the meter on flips the game into "insert coin to keep playing" mode
    on top of it, so the two features can be released independently.
    """
    if not is_enabled():
        return False
    return _cfg()["credit_gating"]


def public_config(comp: Optional[str] = None) -> Dict[str, Any]:
    """Safe subset of config to expose to the browser.

    When called with a `comp` code, the response includes a `comp` sub-object
    describing whether the code is currently valid (and, if so, how many
    free continues remain against the global cap). The client uses this to
    style the continue button as 'COMP MODE' before the player ever clicks.
    """
    c = _cfg()
    if not is_enabled():
        return {"enabled": False}
    out: Dict[str, Any] = {
        "enabled": True,
        "publishable_key": c["publishable_key"],
        "price_cents": c["price_cents"],
        "currency": c["currency"],
        "label": c["label"],
        "display_price": _display_price(c["price_cents"], c["currency"]),
        # Arcade credit economy (may be inactive; the client just needs
        # the numbers to render the HUD chip and the pause overlay copy).
        "credit_gating": _cfg()["credit_gating"] and is_enabled(),
        "credits_per_coin": c["credits_per_coin"],
        "free_starting_credits": c["free_starting_credits"],
    }
    # Test-mode: everything is free on this deploy, no code required.
    if c["test_mode"]:
        out["comp"] = {
            "active": True,
            "reason": "test_mode",
            "label": "\u26A1 TEST MODE \u2014 FREE CONTINUE",
            "remaining": None,
        }
        return out
    # Comp-code path (only reported when a code was passed in — the client
    # doesn't get to enumerate the allowlist just by hitting /config).
    code = _normalize_code(comp)
    if code and code in c["free_play_codes"]:
        used = _comp_used(code)
        remaining = max(0, c["free_play_cap"] - used)
        if remaining > 0:
            out["comp"] = {
                "active": True,
                "reason": "code",
                "code": code,
                "label": f"\u26A1 COMP \u2014 FREE CONTINUE",
                "remaining": remaining,
            }
        else:
            out["comp"] = {
                "active": False,
                "reason": "code_exhausted",
                "code": code,
                "label": None,
                "remaining": 0,
            }
    return out


def _display_price(cents: int, currency: str) -> str:
    sym = {"usd": "$", "eur": "€", "gbp": "£"}.get(currency, "")
    return f"{sym}{cents/100:.2f} {currency.upper() if not sym else ''}".strip()


# ─── Grant storage (per game session) ──────────────────────────────────
#
# We track which Stripe Checkout Session ids have already been redeemed for
# a given game session, so a player can't refresh the return URL and get
# multiple revives from a single payment. The file lives alongside the game
# state under sessions/<sid>/coinop.json.
#
# The file also serves as the receipt log for humans looking at what
# happened, and (optionally) as the record populated by the Stripe webhook.

_GRANT_LOCK = threading.Lock()


def _session_root(session_id: str) -> Path:
    # We reuse the engine's session directory layout: sessions/<sid>/. Import
    # is lazy so this module stays importable even if engine.py fails to
    # import for some unrelated reason during startup.
    try:
        import engine  # noqa: WPS433 (intentional local import)
        base = Path(engine._get_state_path(session_id)).parent  # type: ignore[attr-defined]
    except Exception:
        base = Path("sessions") / session_id
    base.mkdir(parents=True, exist_ok=True)
    return base


def _grant_path(session_id: str) -> Path:
    return _session_root(session_id) / "coinop.json"


def _load_grants(session_id: str) -> Dict[str, Any]:
    p = _grant_path(session_id)
    if not p.exists():
        return {
            "redeemed": [],
            "seen_paid": [],
            "revives_granted": 0,
            # Arcade credit ledger. `credits_purchased` counts credits
            # granted via paid checkouts, `credits_bonus` counts credits
            # granted via comps/test-mode, `credits_used` counts credits
            # spent on turns. `free_starter_granted` is a one-shot flag so
            # we don't top a returning session back up to the free tier.
            "credits_purchased": 0,
            "credits_bonus": 0,
            "credits_used": 0,
            "free_starter_granted": False,
            "free_starter_amount": 0,
        }
    try:
        loaded = json.loads(p.read_text("utf-8"))
    except Exception:
        loaded = {}
    # Back-fill any missing keys so upgrades to the schema don't blow up
    # on sessions that were created before the credit ledger existed.
    for k, v in (
        ("redeemed", []),
        ("seen_paid", []),
        ("revives_granted", 0),
        ("credits_purchased", 0),
        ("credits_bonus", 0),
        ("credits_used", 0),
        ("free_starter_granted", False),
        ("free_starter_amount", 0),
    ):
        loaded.setdefault(k, v)
    return loaded


def _save_grants(session_id: str, data: Dict[str, Any]) -> None:
    p = _grant_path(session_id)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def _mark_seen_paid(session_id: str, checkout_session_id: str, amount_cents: int, currency: str) -> None:
    with _GRANT_LOCK:
        g = _load_grants(session_id)
        for row in g.get("seen_paid", []):
            if row.get("cs") == checkout_session_id:
                return
        g.setdefault("seen_paid", []).append({
            "cs": checkout_session_id,
            "amount_cents": amount_cents,
            "currency": currency,
            "ts": int(time.time()),
        })
        _save_grants(session_id, g)


def _already_redeemed(session_id: str, checkout_session_id: str) -> bool:
    g = _load_grants(session_id)
    return checkout_session_id in (g.get("redeemed") or [])


def _mark_redeemed(session_id: str, checkout_session_id: str, source: str = "stripe", code: Optional[str] = None) -> None:
    with _GRANT_LOCK:
        g = _load_grants(session_id)
        if checkout_session_id in (g.get("redeemed") or []):
            return
        g.setdefault("redeemed", []).append(checkout_session_id)
        g["revives_granted"] = int(g.get("revives_granted", 0)) + 1
        # Audit trail: was this a real Stripe payment, or a comp/test-mode grant?
        # Kept separate from `seen_paid` so tax / analytics / dashboards can
        # cleanly distinguish comped revives from paid ones.
        g.setdefault("grants", []).append({
            "cs": checkout_session_id,
            "source": source,
            "code": code,
            "ts": int(time.time()),
        })
        _save_grants(session_id, g)


# ─── Credit ledger (arcade "insert coin to keep playing") ───────────────
#
# Sits on the SAME per-session grants file as the death-continue ledger,
# under _GRANT_LOCK, so we don't need a second lock and every mutation is
# atomic w.r.t. every other ledger mutation. Reads outside the lock are
# fine (worst case: HUD shows a stale-by-one-turn balance for a fraction
# of a second before the next poll).

def _compute_balance(g: Dict[str, Any]) -> int:
    return max(
        0,
        int(g.get("credits_purchased", 0))
        + int(g.get("credits_bonus", 0))
        + int(g.get("free_starter_amount", 0))
        - int(g.get("credits_used", 0)),
    )


def _ensure_free_starter(g: Dict[str, Any]) -> Dict[str, Any]:
    """Grant the free starter credits lazily, exactly once per session.

    Called from get_balance() so a brand-new session that never talks to
    a payment endpoint still gets its free tier. The `free_starter_granted`
    flag makes this idempotent: a returning player who ran their free
    tier to zero can't refresh their way back into more free turns.
    Returns the (possibly-mutated) grants dict; caller decides whether
    to persist.
    """
    if g.get("free_starter_granted"):
        return g
    if not is_credit_gating_enabled():
        # If gating is off, there's nothing to hand out — leave the flag
        # unset so if gating gets turned on later the starter still fires
        # once for this session.
        return g
    amount = int(_cfg()["free_starting_credits"])
    g["free_starter_granted"] = True
    g["free_starter_amount"] = amount
    g.setdefault("grants", []).append({
        "cs": None,
        "source": "free_starter",
        "amount": amount,
        "ts": int(time.time()),
    })
    return g


def get_balance(session_id: str) -> Dict[str, Any]:
    """Snapshot of the session's credit ledger for the HUD / gating.

    Grants the one-shot free starter tier the first time this is called
    on a session that hasn't seen it (see _ensure_free_starter). Safe to
    call from anywhere — reads are cheap and the write only happens once
    per session's lifetime.
    """
    c = _cfg()
    with _GRANT_LOCK:
        g = _load_grants(session_id)
        before = (g.get("free_starter_granted"), int(g.get("free_starter_amount", 0)))
        g = _ensure_free_starter(g)
        after = (g.get("free_starter_granted"), int(g.get("free_starter_amount", 0)))
        if before != after:
            _save_grants(session_id, g)
        balance = _compute_balance(g)
    return {
        "enabled": is_enabled(),
        "gating_enabled": is_credit_gating_enabled(),
        "balance": balance,
        "used": int(g.get("credits_used", 0)),
        "purchased": int(g.get("credits_purchased", 0)),
        "bonus": int(g.get("credits_bonus", 0)),
        "free_starter_amount": int(g.get("free_starter_amount", 0)),
        "credits_per_coin": c["credits_per_coin"],
        "price_cents": c["price_cents"],
        "currency": c["currency"],
        "display_price": _display_price(c["price_cents"], c["currency"]),
        # Total dollars ever spent on this session (paid credits only —
        # comps and the free starter aren't counted). Powers the "SPENT
        # $X.XX" subtitle in the HUD chip so the player always knows how
        # much this run has cost them.
        "spent_cents": int(g.get("credits_purchased", 0)) // max(1, c["credits_per_coin"]) * c["price_cents"],
    }


def spend_credit(session_id: str, amount: int = 1, reason: str = "turn") -> Dict[str, Any]:
    """Debit `amount` credits atomically. Returns the new balance snapshot.

    Refuses (ok=False) if the balance would go negative. Callers should
    treat that as "session is out of coins — pop the pause overlay" and
    NOT proceed with the turn. Free starter is materialized here too so
    a spend call is always well-defined even on a totally fresh session.
    """
    amount = max(1, int(amount))
    with _GRANT_LOCK:
        g = _load_grants(session_id)
        g = _ensure_free_starter(g)
        available = _compute_balance(g)
        if available < amount:
            _save_grants(session_id, g)  # persist any starter grant
            return {"ok": False, "reason": "insufficient_credits", "balance": available}
        g["credits_used"] = int(g.get("credits_used", 0)) + amount
        g.setdefault("spends", []).append({
            "amount": amount,
            "reason": reason,
            "ts": int(time.time()),
        })
        _save_grants(session_id, g)
        new_balance = _compute_balance(g)
    return {"ok": True, "balance": new_balance, "spent": amount}


def grant_credits(session_id: str, amount: int, source: str = "stripe",
                  checkout_session_id: Optional[str] = None) -> Dict[str, Any]:
    """Credit `amount` credits to a session and return the new balance.

    `source` is one of 'stripe' (paid), 'comp' (comp code / test mode),
    'admin' (manual grant), 'free_starter' (used by _ensure_free_starter,
    not by this public helper). Paid grants land in `credits_purchased`
    so the "SPENT $X.XX" subtitle in the HUD is accurate; everything
    else lands in `credits_bonus` so it doesn't inflate the spend total.
    """
    amount = max(0, int(amount))
    if amount == 0:
        return {"ok": True, "balance": get_balance(session_id)["balance"], "granted": 0}
    with _GRANT_LOCK:
        g = _load_grants(session_id)
        if source == "stripe":
            g["credits_purchased"] = int(g.get("credits_purchased", 0)) + amount
        else:
            g["credits_bonus"] = int(g.get("credits_bonus", 0)) + amount
        g.setdefault("credit_grants", []).append({
            "amount": amount,
            "source": source,
            "cs": checkout_session_id,
            "ts": int(time.time()),
        })
        _save_grants(session_id, g)
        new_balance = _compute_balance(g)
    return {"ok": True, "balance": new_balance, "granted": amount, "source": source}


# ─── Global comp counter (shared across all sessions) ───────────────────
#
# Codes have a GLOBAL usage cap (e.g. 100 per code). We keep the counters
# in one small file at sessions/_coinop_comp_counters.json rather than
# scanning every per-session coinop.json to sum. Cheap, atomic under the
# same _GRANT_LOCK.

def _comp_counters_path() -> Path:
    root = Path("sessions")
    root.mkdir(parents=True, exist_ok=True)
    return root / "_coinop_comp_counters.json"


def _load_comp_counters() -> Dict[str, int]:
    p = _comp_counters_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text("utf-8"))
        return {str(k): int(v) for k, v in (data or {}).items()}
    except Exception:
        return {}


def _save_comp_counters(counters: Dict[str, int]) -> None:
    p = _comp_counters_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(counters, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def _comp_used(code: str) -> int:
    if not code:
        return 0
    return int(_load_comp_counters().get(code, 0))


def _bump_comp_counter(code: str) -> int:
    """Atomically increment the counter for a comp code. Returns the new value."""
    with _GRANT_LOCK:
        counters = _load_comp_counters()
        counters[code] = int(counters.get(code, 0)) + 1
        _save_comp_counters(counters)
        return counters[code]


def _comp_available(comp_code: Optional[str]) -> Dict[str, Any]:
    """Decide whether a comp grant should be issued for this request.

    Returns {'ok': bool, 'reason': str, 'code': Optional[str]}. Three
    accept paths: (1) COINOP_TEST_MODE=1 makes every request a comp;
    (2) an allowlisted code below its cap; (3) nothing else — normal paid
    flow proceeds.
    """
    c = _cfg()
    if c["test_mode"]:
        return {"ok": True, "reason": "test_mode", "code": None}
    code = _normalize_code(comp_code)
    if not code:
        return {"ok": False, "reason": "no_code", "code": None}
    if code not in c["free_play_codes"]:
        return {"ok": False, "reason": "unknown_code", "code": code}
    if _comp_used(code) >= c["free_play_cap"]:
        return {"ok": False, "reason": "code_exhausted", "code": code}
    return {"ok": True, "reason": "code", "code": code}


def _mint_comp_id() -> str:
    """Produce a Stripe-Checkout-Session-shaped-but-clearly-not-real id.

    The prefix `comp_` is how verify_and_redeem tells a comp from a real
    Stripe id at redemption time — real Stripe checkout ids start `cs_`.
    Using the same length + shape keeps client code paths identical.
    """
    import secrets
    return f"comp_{secrets.token_hex(12)}"


# ─── Stripe Checkout ────────────────────────────────────────────────────

def _stripe_client():
    if stripe is None:
        raise RuntimeError("stripe package not installed")
    stripe.api_key = _cfg()["secret_key"]
    return stripe


def _resolve_return_base(request) -> str:
    c = _cfg()
    if c["public_base_url"]:
        return c["public_base_url"]
    # Fall back to the request's own host — works fine in dev and on any
    # single-host deployment. Trailing slash trimmed for clean concatenation.
    return request.host_url.rstrip("/")


def create_checkout(session_id: str, request, comp_code: Optional[str] = None) -> Dict[str, Any]:
    """Create a Stripe Checkout Session for a single 'continue' purchase,
    OR — if COINOP_TEST_MODE is on, or a valid comp code was supplied —
    mint a comp voucher instead and return it directly.

    Return shape:
      * paid path:  {'url': <stripe url>, 'checkout_session_id': 'cs_...',  'comp': False}
      * comp path:  {'url': null,          'checkout_session_id': 'comp_...', 'comp': True,
                     'comp_reason': 'test_mode' | 'code',
                     'comp_code': <lowercased code or null>}

    Client behavior differs only in whether to redirect to Stripe (paid) or
    directly call /api/coinop/redeem with the comp id (comp). Server-side,
    verify_and_redeem handles the two id prefixes ("cs_" vs "comp_")
    interchangeably.
    """
    if not is_enabled():
        raise RuntimeError("coinop feature is not enabled")

    # Free-play short-circuit. Everything from here to the Stripe call is
    # skipped when a comp applies — no Stripe API call, no network hop.
    avail = _comp_available(comp_code)
    if avail["ok"]:
        comp_id = _mint_comp_id()
        # Global counter bumped now so a burst of clicks can't over-grant
        # against a code's cap. verify_and_redeem later just checks that
        # the id was minted (via the per-session seen_paid record we add
        # here) — it does NOT re-check the cap, so no race.
        if avail["code"]:
            _bump_comp_counter(avail["code"])
        _mark_seen_paid(session_id, comp_id, 0, "comp")
        log.info(
            "coinop: minted COMP id=%s for session=%s reason=%s code=%s",
            comp_id, session_id, avail["reason"], avail["code"],
        )
        return {
            "url": None,
            "checkout_session_id": comp_id,
            "comp": True,
            "comp_reason": avail["reason"],
            "comp_code": avail["code"],
        }

    c = _cfg()
    s = _stripe_client()

    base = _resolve_return_base(request)
    # We include {CHECKOUT_SESSION_ID} as a Stripe template variable so the
    # return URL contains the id we need to redeem server-side without a
    # webhook. The literal braces are required — Stripe substitutes on redirect.
    success_url = (
        f"{base}/play?session={session_id}"
        f"&coinop=success&cs={{CHECKOUT_SESSION_ID}}"
    )
    cancel_url = f"{base}/play?session={session_id}&coinop=cancel"

    checkout = s.checkout.Session.create(
        mode="payment",
        payment_method_types=["card"],
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": c["currency"],
                "unit_amount": c["price_cents"],
                "product_data": {"name": c["product_name"]},
            },
        }],
        # Metadata is how we correlate a payment back to the game session.
        # The redeem endpoint refuses to grant a revive to any session_id
        # that doesn't match this value — so replaying someone else's success
        # URL against your own session does nothing.
        metadata={
            "game_session_id": session_id,
            "purpose": "continue",
        },
        payment_intent_data={
            "metadata": {
                "game_session_id": session_id,
                "purpose": "continue",
            },
            "description": f"SOMEWHERE continue for session {session_id}",
        },
        success_url=success_url,
        cancel_url=cancel_url,
        # Short expiry keeps stale unfinished checkouts from lingering.
        expires_at=int(time.time()) + 30 * 60,
    )
    log.info("coinop: created checkout session %s for game session %s", checkout.id, session_id)
    return {"url": checkout.url, "checkout_session_id": checkout.id, "comp": False}


def _fetch_checkout(checkout_session_id: str):
    s = _stripe_client()
    return s.checkout.Session.retrieve(checkout_session_id)


def verify_and_redeem(session_id: str, checkout_session_id: str) -> Dict[str, Any]:
    """Redeem a paid Stripe Checkout Session for exactly one revive.

    Returns {'ok': True, 'already_redeemed': bool} on success. Returns
    {'ok': False, 'reason': ...} on any validation failure.

    Server-authoritative: pulls the fresh Checkout Session from Stripe and
    checks payment_status; never trusts client claims of payment. Also
    verifies that the checkout's metadata.game_session_id matches the caller's
    session, so a URL leak can't fund a stranger's run.
    """
    if not is_enabled():
        return {"ok": False, "reason": "feature_disabled"}
    if not checkout_session_id or not (
        checkout_session_id.startswith("cs_") or checkout_session_id.startswith("comp_")
    ):
        return {"ok": False, "reason": "bad_checkout_id"}

    if _already_redeemed(session_id, checkout_session_id):
        # Idempotent: replaying the return URL is a no-op success. The client
        # should just proceed to revive if it hasn't already; the engine's
        # api_revive is itself idempotent w.r.t. an already-alive player.
        return {"ok": True, "already_redeemed": True}

    # Comp path: no Stripe call. We only accept a comp id we ourselves
    # minted for THIS session earlier (checked via seen_paid). This closes
    # the loop where somebody hand-forges a `comp_...` string.
    if checkout_session_id.startswith("comp_"):
        g = _load_grants(session_id)
        minted = any(row.get("cs") == checkout_session_id for row in g.get("seen_paid", []))
        if not minted:
            return {"ok": False, "reason": "unknown_comp_id"}
        # Recover the comp code from the minting log for the audit trail.
        # (seen_paid rows for comp ids record currency='comp'; if we ever
        # care about which code was used, verify_and_redeem's grants entry
        # captures it via the source/code fields.)
        _mark_redeemed(session_id, checkout_session_id, source="comp", code=None)
        credits_added = _cfg()["credits_per_coin"]
        grant_credits(session_id, credits_added, source="comp",
                      checkout_session_id=checkout_session_id)
        log.info("coinop: redeemed COMP %s for game session %s (+%d credits)",
                 checkout_session_id, session_id, credits_added)
        return {
            "ok": True, "already_redeemed": False, "comp": True,
            "credits_added": credits_added,
            "balance": get_balance(session_id)["balance"],
        }

    try:
        cs = _fetch_checkout(checkout_session_id)
    except Exception as e:  # noqa: BLE001
        log.exception("coinop: failed to retrieve checkout session %s", checkout_session_id)
        return {"ok": False, "reason": f"stripe_fetch_failed:{e}"}

    payment_status = getattr(cs, "payment_status", None) or (cs.get("payment_status") if hasattr(cs, "get") else None)
    if payment_status != "paid":
        return {"ok": False, "reason": f"not_paid:{payment_status}"}

    md = getattr(cs, "metadata", None) or {}
    if hasattr(md, "get"):
        cs_game_sid = md.get("game_session_id")
    else:
        cs_game_sid = None
    if cs_game_sid and cs_game_sid != session_id:
        log.warning("coinop: session id mismatch on redeem (cs=%s wants=%s got=%s)",
                    checkout_session_id, cs_game_sid, session_id)
        return {"ok": False, "reason": "session_mismatch"}

    _mark_seen_paid(
        session_id,
        checkout_session_id,
        int(getattr(cs, "amount_total", 0) or 0),
        (getattr(cs, "currency", "") or "").lower(),
    )
    _mark_redeemed(session_id, checkout_session_id, source="stripe")
    credits_added = _cfg()["credits_per_coin"]
    grant_credits(session_id, credits_added, source="stripe",
                  checkout_session_id=checkout_session_id)
    log.info("coinop: redeemed checkout %s for game session %s (+%d credits)",
             checkout_session_id, session_id, credits_added)
    return {
        "ok": True, "already_redeemed": False, "comp": False,
        "credits_added": credits_added,
        "balance": get_balance(session_id)["balance"],
    }


# ─── Webhook (optional) ────────────────────────────────────────────────

def handle_webhook(payload: bytes, signature: str) -> Dict[str, Any]:
    """Verify + record a Stripe webhook.

    Optional in the MVP: the redeem endpoint is already server-authoritative
    via the Stripe API. The webhook exists so that if the user closes the
    browser mid-return, we still have a durable record of the payment; a
    future 'pending revive' recovery flow can then read from `seen_paid`.
    """
    if stripe is None:
        return {"ok": False, "reason": "stripe_not_installed"}
    c = _cfg()
    secret = c["webhook_secret"]
    if not secret:
        return {"ok": False, "reason": "no_webhook_secret"}
    try:
        event = stripe.Webhook.construct_event(payload, signature, secret)
    except Exception as e:  # noqa: BLE001
        log.warning("coinop: webhook signature verification failed: %s", e)
        return {"ok": False, "reason": "bad_signature"}

    etype = event.get("type") if hasattr(event, "get") else getattr(event, "type", None)
    if etype != "checkout.session.completed":
        return {"ok": True, "ignored": etype}

    data = event["data"]["object"] if hasattr(event, "__getitem__") else event.data.object  # type: ignore
    md = data.get("metadata", {}) or {}
    session_id = md.get("game_session_id")
    checkout_session_id = data.get("id")
    if not session_id or not checkout_session_id:
        return {"ok": False, "reason": "missing_metadata"}
    if (data.get("payment_status") or "").lower() != "paid":
        return {"ok": True, "ignored": "unpaid"}

    _mark_seen_paid(
        session_id,
        checkout_session_id,
        int(data.get("amount_total") or 0),
        (data.get("currency") or "").lower(),
    )
    return {"ok": True, "recorded": checkout_session_id}


# ─── Diagnostics ────────────────────────────────────────────────────────

def summary_for_session(session_id: str) -> Dict[str, Any]:
    return {"enabled": is_enabled(), **_load_grants(session_id)}
