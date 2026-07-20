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
    }


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


def public_config() -> Dict[str, Any]:
    """Safe subset of config to expose to the browser."""
    c = _cfg()
    if not is_enabled():
        return {"enabled": False}
    return {
        "enabled": True,
        "publishable_key": c["publishable_key"],
        "price_cents": c["price_cents"],
        "currency": c["currency"],
        "label": c["label"],
        "display_price": _display_price(c["price_cents"], c["currency"]),
    }


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
        return {"redeemed": [], "seen_paid": [], "revives_granted": 0}
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return {"redeemed": [], "seen_paid": [], "revives_granted": 0}


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


def _mark_redeemed(session_id: str, checkout_session_id: str) -> None:
    with _GRANT_LOCK:
        g = _load_grants(session_id)
        if checkout_session_id in (g.get("redeemed") or []):
            return
        g.setdefault("redeemed", []).append(checkout_session_id)
        g["revives_granted"] = int(g.get("revives_granted", 0)) + 1
        _save_grants(session_id, g)


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


def create_checkout(session_id: str, request) -> Dict[str, Any]:
    """Create a Stripe Checkout Session for a single 'continue' purchase.

    Returns {'url': ..., 'checkout_session_id': ...} on success. Raises
    on config errors so the API layer can 500 cleanly.
    """
    if not is_enabled():
        raise RuntimeError("coinop feature is not enabled")

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
    return {"url": checkout.url, "checkout_session_id": checkout.id}


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
    if not checkout_session_id or not checkout_session_id.startswith("cs_"):
        return {"ok": False, "reason": "bad_checkout_id"}

    if _already_redeemed(session_id, checkout_session_id):
        # Idempotent: replaying the return URL is a no-op success. The client
        # should just proceed to revive if it hasn't already; the engine's
        # api_revive is itself idempotent w.r.t. an already-alive player.
        return {"ok": True, "already_redeemed": True}

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
    _mark_redeemed(session_id, checkout_session_id)
    log.info("coinop: redeemed checkout %s for game session %s", checkout_session_id, session_id)
    return {"ok": True, "already_redeemed": False}


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
