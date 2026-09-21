"""Account + hosted usage wallet — Cursor-style billing.

BYOK stays free (their keys, their provider bill). Hosted play is how
SOMEWHERE gets paid: prepaid packs and a monthly Play plan through the
same Stripe account as coin-op. We debit the wallet at provider cost
times a markup.

Environment:
  STRIPE_SECRET_KEY / STRIPE_PUBLISHABLE_KEY   same as coin-op
  FEATURE_BILLING         "1" to pause generation when the wallet is empty
  BILLING_MARKUP          default 2.0 (we charge 2× provider cost)
  BILLING_HOST_PAYS       "1" = this host foots the bill (dev / operator)
  PUBLIC_BASE_URL         checkout return host
  SOMEWHERE_BILLING_PATH  override store path (tests)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import stripe  # type: ignore
except Exception:  # noqa: BLE001
    stripe = None  # type: ignore

log = logging.getLogger("billing")

TITLE = "SOMEWHERE"
COOKIE = "somewhere_account"

PLANS = {
    "free": {
        "id": "free",
        "label": "Free",
        "price_cents": 0,
        "included_usd": 0.0,
        "blurb": "Your keys. Your provider bill.",
        "mode": "byok",
    },
    "play": {
        "id": "play",
        "label": "Play",
        "price_cents": 1200,
        "included_usd": 10.0,
        "blurb": "We run the models. $10 included each month.",
        "mode": "hosted",
    },
}

PACKS = (
    {"id": "ten", "label": "$10", "price_cents": 1000, "credit_usd": 10.0,
     "blurb": "A short run."},
    {"id": "twentyfive", "label": "$25", "price_cents": 2500, "credit_usd": 27.5,
     "blurb": "The usual drop.", "usual": True},
    {"id": "fifty", "label": "$50", "price_cents": 5000, "credit_usd": 57.5,
     "blurb": "Best value."},
)

DEFAULT_PACK = "twentyfive"
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_LOCK = threading.Lock()
_store_path: Optional[Path] = None
_local_app = False
ROOT = Path(__file__).resolve().parent


def mark_local_app() -> None:
    """play.py / run_local.py: this process is BYOK, not a hosted cashier."""
    global _local_app
    _local_app = True


def set_store_path(path: Optional[os.PathLike | str]) -> None:
    global _store_path
    _store_path = Path(path) if path else None


def _yes(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "on", "yes")


def markup() -> float:
    try:
        n = float(os.environ.get("BILLING_MARKUP", "2.0"))
    except (TypeError, ValueError):
        n = 2.0
    return max(1.0, n)


def host_pays() -> bool:
    return _yes("BILLING_HOST_PAYS")


def is_payments_enabled() -> bool:
    if stripe is None:
        return False
    return bool(
        os.environ.get("STRIPE_SECRET_KEY", "").strip()
        and os.environ.get("STRIPE_PUBLISHABLE_KEY", "").strip()
    )


def requires_wallet() -> bool:
    """Hosted play on our keys — visitors pay us unless the host foots it.

    Dark until FEATURE_BILLING (or BILLING_REQUIRED) is on, same idea as
    FEATURE_COINOP. Stripe keys alone light the checkout UI; the flag is
    what pauses generation when the wallet is empty.
    """
    if host_pays() or _local_app:
        return False
    if not is_payments_enabled():
        return False
    return _yes("FEATURE_BILLING") or _yes("BILLING_REQUIRED")


def pack_by_id(pack_id: Optional[str]) -> Dict[str, Any]:
    want = (pack_id or "").strip().lower()
    for p in PACKS:
        if p["id"] == want:
            return dict(p)
    for p in PACKS:
        if p.get("usual"):
            return dict(p)
    return dict(PACKS[0])


def _default_store_path() -> Path:
    raw = (os.environ.get("SOMEWHERE_BILLING_PATH") or "").strip()
    if raw:
        return Path(raw)
    # Hosted: keep wallets on the game volume so a redeploy does not wipe them.
    # Local play.py / run_local.py stay in the user profile (BYOK, not a cashier).
    if not _local_app:
        return ROOT / "sessions" / "_analytics" / "billing.json"
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / TITLE / "billing.json"
    return Path.home() / ".somewhere" / "billing.json"


def _path() -> Path:
    return _store_path or _default_store_path()


def _empty_store() -> Dict[str, Any]:
    return {"active_email": None, "accounts": {}}


def _read() -> Dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty_store()
        data.setdefault("accounts", {})
        data.setdefault("active_email", None)
        return data
    except Exception:
        return _empty_store()


def _write(payload: Dict[str, Any]) -> None:
    dest = _path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(dest)


def _new_account(email: str) -> Dict[str, Any]:
    return {
        "email": email,
        "plan": "free",
        "stripe_customer_id": None,
        "stripe_subscription_id": None,
        "wallet_usd": 0.0,
        "included_usd": 0.0,
        "included_used_usd": 0.0,
        "billed_usd": 0.0,
        "on_demand": True,
        "monthly_cap_usd": None,
        "period_start": _period_start(),
        "linked_at": int(time.time()),
        "redeemed": [],
        "payments": [],
    }


def _period_start() -> str:
    return date.today().replace(day=1).isoformat()


def _normalize_email(raw: Any) -> str:
    email = str(raw or "").strip().lower()
    if not email or not _EMAIL_RE.match(email):
        raise ValueError("Enter a valid email.")
    return email


def _cookie_secret() -> bytes:
    raw = (
        os.environ.get("STRIPE_SECRET_KEY")
        or os.environ.get("SOMEWHERE_BILLING_SECRET")
        or "somewhere-billing-dev"
    ).encode("utf-8")
    return raw


def cookie_value(email: str) -> str:
    email = _normalize_email(email)
    sig = hmac.new(_cookie_secret(), email.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"{email}|{sig}"


def email_from_cookie(raw: Optional[str]) -> Optional[str]:
    text = (raw or "").strip()
    if "|" not in text:
        return None
    email, sig = text.rsplit("|", 1)
    try:
        email = _normalize_email(email)
    except ValueError:
        return None
    expect = hmac.new(_cookie_secret(), email.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expect):
        return None
    return email


def _request_email() -> Optional[str]:
    try:
        from flask import has_request_context, request
        if has_request_context():
            return email_from_cookie(request.cookies.get(COOKIE))
    except Exception:
        pass
    return None


def _roll_period(acct: Dict[str, Any]) -> None:
    start = _period_start()
    if acct.get("period_start") == start:
        return
    acct["period_start"] = start
    acct["included_used_usd"] = 0.0
    acct["billed_usd"] = 0.0
    if acct.get("plan") == "play":
        acct["included_usd"] = float(PLANS["play"]["included_usd"])


def _active_email(store: Dict[str, Any], *, ignore_cookie: bool = False) -> Optional[str]:
    if ignore_cookie:
        return store.get("active_email")
    return _request_email() or store.get("active_email")


def current_account(*, ignore_cookie: bool = False) -> Dict[str, Any]:
    store = _read()
    email = _active_email(store, ignore_cookie=ignore_cookie)
    if not email:
        return _new_account("")
    acct = store.get("accounts", {}).get(email)
    if not isinstance(acct, dict):
        return _new_account(email)
    _roll_period(acct)
    return acct


def _save_account(acct: Dict[str, Any]) -> Dict[str, Any]:
    email = (acct.get("email") or "").strip().lower()
    if not email:
        raise ValueError("Account has no email.")
    with _LOCK:
        store = _read()
        existing = store.get("accounts", {}).get(email) or {}
        if existing.get("period_start") == acct.get("period_start"):
            pass
        merged = dict(existing)
        merged.update(acct)
        store.setdefault("accounts", {})[email] = merged
        store["active_email"] = email
        _write(store)
        return merged


def link_email(raw: Any) -> str:
    email = _normalize_email(raw)
    with _LOCK:
        store = _read()
        if email not in store.get("accounts", {}):
            store.setdefault("accounts", {})[email] = _new_account(email)
        store["active_email"] = email
        _write(store)
    return email


def unlink() -> None:
    with _LOCK:
        store = _read()
        store["active_email"] = None
        _write(store)


def set_on_demand(value: Any) -> bool:
    acct = current_account()
    if not acct.get("email"):
        raise ValueError("Link an email first.")
    acct["on_demand"] = bool(value)
    _save_account(acct)
    return bool(acct["on_demand"])


def set_monthly_cap_usd(value: Any) -> Optional[float]:
    acct = current_account()
    if not acct.get("email"):
        raise ValueError("Link an email first.")
    cap: Optional[float] = None
    if value is not None and str(value).strip() != "":
        n = float(value)
        if n < 0:
            raise ValueError("Monthly cap cannot be negative.")
        if n > 0:
            cap = round(n, 2)
    acct["monthly_cap_usd"] = cap
    _save_account(acct)
    return cap


def included_left(acct: Optional[Dict[str, Any]] = None) -> float:
    row = acct or current_account()
    _roll_period(row)
    return max(0.0, float(row.get("included_usd") or 0.0) - float(row.get("included_used_usd") or 0.0))


def available_usd(acct: Optional[Dict[str, Any]] = None) -> float:
    row = acct or current_account()
    _roll_period(row)
    left = included_left(row)
    wallet = max(0.0, float(row.get("wallet_usd") or 0.0))
    if left > 0:
        return round(left + (wallet if row.get("on_demand", True) else 0.0), 6)
    if row.get("on_demand", True):
        return round(wallet, 6)
    return 0.0


def over_account_cap(acct: Optional[Dict[str, Any]] = None) -> bool:
    row = acct or current_account()
    _roll_period(row)
    cap = row.get("monthly_cap_usd")
    try:
        cap_n = float(cap) if cap is not None else None
    except (TypeError, ValueError):
        cap_n = None
    if not cap_n or cap_n <= 0:
        return False
    return float(row.get("billed_usd") or 0.0) >= cap_n


def debit(amount_usd: float) -> Dict[str, Any]:
    """Burn included, then wallet. Returns the new account snapshot."""
    if amount_usd <= 0:
        return current_account()
    acct = current_account()
    if not acct.get("email"):
        raise ValueError("No linked account.")
    _roll_period(acct)
    left = included_left(acct)
    take_included = min(left, amount_usd)
    rest = round(amount_usd - take_included, 6)
    acct["included_used_usd"] = round(float(acct.get("included_used_usd") or 0.0) + take_included, 6)
    if rest > 0:
        wallet = max(0.0, float(acct.get("wallet_usd") or 0.0) - rest)
        acct["wallet_usd"] = round(wallet, 6)
    acct["billed_usd"] = round(float(acct.get("billed_usd") or 0.0) + amount_usd, 6)
    return _save_account(acct)


def grant_wallet(amount_usd: float, *, source: str, checkout_session_id: Optional[str] = None) -> Dict[str, Any]:
    acct = current_account()
    if not acct.get("email"):
        raise ValueError("No linked account.")
    acct["wallet_usd"] = round(float(acct.get("wallet_usd") or 0.0) + max(0.0, amount_usd), 6)
    acct["on_demand"] = True
    if checkout_session_id:
        redeemed = list(acct.get("redeemed") or [])
        if checkout_session_id not in redeemed:
            redeemed.append(checkout_session_id)
        acct["redeemed"] = redeemed
    return _save_account(acct)


def settle_session(session_id: str, cost_before_usd: float) -> Optional[Dict[str, Any]]:
    """Debit retail (cost × markup) for hosted play after a successful turn."""
    if not requires_wallet():
        return None
    try:
        import cost_tracker
        after = float(cost_tracker.session_cost_usd(session_id) or 0.0)
    except Exception:
        return None
    delta = max(0.0, after - max(0.0, float(cost_before_usd or 0.0)))
    if delta <= 0:
        return None
    retail = round(delta * markup(), 6)
    try:
        return debit(retail)
    except Exception as e:
        log.warning("billing: settle failed session=%s: %s", session_id, e)
        return None


def gate(min_usd: float = 0.0) -> Optional[Dict[str, Any]]:
    """402 payload if generation should pause, else None.

    ``min_usd`` is a pre-flight hold: refuse when the wallet cannot cover an
    estimated spike (a render, a Reactor connect) rather than debiting after
    the fact and flooring at zero.
    """
    try:
        import usage_limits
        if usage_limits.over_cap():
            return {
                "needs_usage": True,
                "reason": "usage_limit",
                "message": "Monthly usage limit reached.",
            }
    except Exception:
        pass
    if not requires_wallet():
        return None
    acct = current_account()
    if not (acct.get("email") or "").strip():
        return {
            "needs_billing": True,
            "reason": "needs_account",
            "message": "Link an email in ACCOUNT to play on our keys.",
        }
    if over_account_cap(acct):
        return {
            "needs_billing": True,
            "needs_usage": True,
            "reason": "spend_limit",
            "message": "Monthly spend limit reached. Raise the cap to keep generating.",
        }
    need = max(0.0, float(min_usd or 0.0))
    if available_usd(acct) <= 0 or (need > 0 and available_usd(acct) < need):
        reason = "on_demand_off" if not acct.get("on_demand", True) and included_left(acct) <= 0 else "empty_wallet"
        if need > 0 and available_usd(acct) > 0:
            reason = "insufficient_balance"
        return {
            "needs_billing": True,
            "reason": reason,
            "message": (
                "Included usage is gone. Turn on on-demand or add funds."
                if reason == "on_demand_off"
                else "This action costs more than the remaining balance. Add funds in ACCOUNT."
                if reason == "insufficient_balance"
                else "Usage balance empty. Add funds in ACCOUNT."
            ),
        }
    return None


def apply_invoice_paid(invoice: Any) -> Dict[str, Any]:
    """Refresh Play included usage when Stripe bills the monthly subscription.

    Checkout redeem sets the first month. Recurring ``invoice.paid`` events
    are what keep included_usd from going stale.
    """
    data = _plain(invoice)
    if not (data.get("subscription") or (_plain(data.get("parent")).get("subscription_details"))):
        # A top-up's own invoice (invoice_creation) — the wallet was credited
        # by fulfill_checkout; this must not turn the payer into a Play member.
        return {"ok": True, "ignored": "not_a_subscription_invoice"}
    customer = str(data.get("customer") or "").strip()
    if not customer:
        return {"ok": False, "reason": "no_customer"}
    with _LOCK:
        store = _read()
        found = None
        for email, acct in (store.get("accounts") or {}).items():
            if not isinstance(acct, dict):
                continue
            if str(acct.get("stripe_customer_id") or "") == customer:
                found = email
                break
        if not found:
            return {"ok": False, "reason": "unknown_customer"}
        acct = dict(store["accounts"][found])
        if acct.get("plan") != "play":
            acct["plan"] = "play"
        acct["included_usd"] = float(PLANS["play"]["included_usd"])
        acct["included_used_usd"] = 0.0
        acct["period_start"] = _period_start()
        store["accounts"][found] = acct
        _write(store)
    log.info("billing: invoice.paid refreshed Play included email=%s", found)
    return {"ok": True, "email": found, "included_usd": float(PLANS["play"]["included_usd"])}


def _plain(obj: Any) -> Dict[str, Any]:
    """A Stripe object as a plain dict. stripe-python 15 objects are not
    dicts (`dict(obj)` raises TypeError, `obj.get` raises AttributeError),
    older ones were; webhook payloads may already be plain dicts."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    for name in ("to_dict_recursive", "to_dict"):
        fn = getattr(type(obj), name, None)
        if callable(fn):
            try:
                out = fn(obj)
                if isinstance(out, dict):
                    return out
            except Exception:  # noqa: BLE001
                pass
    try:
        return dict(obj)
    except Exception:  # noqa: BLE001
        return {}


def _stripe_client():
    if stripe is None:
        raise RuntimeError("stripe package not installed")
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    return stripe


def _return_base(request) -> str:
    base = (os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if base:
        return base
    return request.host_url.rstrip("/")


def _safe_return_path(raw: Any) -> str:
    """Same-origin path to come back to after checkout. Only the game's own
    pages; anything else (absolute URLs, //host, backslashes) -> /standalone."""
    path = str(raw or "").strip()
    if (not path.startswith(("/play", "/standalone")) or "//" in path
            or "\\" in path or ":" in path.split("?", 1)[0] or len(path) > 512):
        return "/standalone"
    # Drop any billing params left over from a previous return.
    if "?" in path:
        base, _, query = path.partition("?")
        keep = [p for p in query.split("&") if p and not p.startswith(("billing=", "cs="))]
        path = base + ("?" + "&".join(keep) if keep else "")
    return path


def create_checkout(kind: str, request, pack_id: Optional[str] = None,
                    return_to: Optional[str] = None) -> Dict[str, Any]:
    if not is_payments_enabled():
        raise RuntimeError("Payments are not configured.")
    acct = current_account()
    email = (acct.get("email") or "").strip()
    if not email:
        raise ValueError("Link an email before paying.")
    kind = (kind or "pack").strip().lower()
    if kind == "play":
        # The monthly plan is not sold (BILLING_LIVE_PLAN C6): one way in,
        # wallet top-ups.
        raise ValueError("The Play plan isn't sold. Add money to the wallet instead.")
    if kind != "pack":
        raise ValueError("Unknown checkout kind.")

    s = _stripe_client()
    base = _return_base(request)
    back = _safe_return_path(return_to)
    sep = "&" if "?" in back else "?"
    success = f"{base}{back}{sep}billing=success&cs={{CHECKOUT_SESSION_ID}}"
    cancel = f"{base}{back}{sep}billing=cancel"
    currency = (os.environ.get("COINOP_CONTINUE_CURRENCY") or "usd").strip().lower()

    if kind == "play":
        plan = PLANS["play"]
        line_items: List[Dict[str, Any]] = [{
            "quantity": 1,
            "price_data": {
                "currency": currency,
                "recurring": {"interval": "month"},
                "unit_amount": int(plan["price_cents"]),
                "product_data": {"name": "SOMEWHERE Play"},
            },
        }]
        mode = "subscription"
        pack = None
    else:
        pack = pack_by_id(pack_id)
        line_items = [{
            "quantity": 1,
            "price_data": {
                "currency": currency,
                "unit_amount": int(pack["price_cents"]),
                "product_data": {"name": f"SOMEWHERE usage · {pack['label']}"},
            },
        }]
        mode = "payment"

    # No payment_method_types: the Dashboard's payment-method settings decide
    # (card, Link, wallets, Klarna…). Delayed methods finish after checkout —
    # the webhook's checkout.session.async_payment_succeeded credits those.
    kwargs: Dict[str, Any] = {
        "mode": mode,
        "line_items": line_items,
        "client_reference_id": email,
        "metadata": {
            "purpose": kind,
            "email": email,
            "pack": (pack or {}).get("id") or "",
        },
        "success_url": success,
        "cancel_url": cancel,
        "expires_at": int(time.time()) + 30 * 60,
    }
    if mode == "payment":
        # Every top-up gets a Stripe invoice: the player's receipt, and a
        # record the Dashboard can show next to the wallet credit.
        kwargs["invoice_creation"] = {
            "enabled": True,
            "invoice_data": {
                "description": f"GOD wallet top-up · {(pack or {}).get('label', '')}".strip(),
                "metadata": {"email": email, "pack": (pack or {}).get("id") or ""},
            },
        }
    if acct.get("stripe_customer_id"):
        kwargs["customer"] = acct["stripe_customer_id"]
    else:
        kwargs["customer_email"] = email
        if mode == "payment":
            kwargs["customer_creation"] = "always"

    checkout = s.checkout.Session.create(**kwargs)
    log.info("billing: checkout %s kind=%s email=%s", checkout.id, kind, email)
    return {
        "url": checkout.url,
        "checkout_session_id": checkout.id,
        "kind": kind,
        "pack": (pack or {}).get("id"),
    }


def fulfill_checkout(session: Any) -> Dict[str, Any]:
    """Credit a paid top-up Checkout Session to the account that paid.

    The one path money takes into a wallet — the browser's return
    (``redeem``) and the webhook both land here, in either order, any number
    of times. The account is the one written into the session at checkout
    (``metadata.email``), never the browser's current account: a shared or
    signed-in-elsewhere browser must not collect someone else's payment.
    Idempotent on the checkout id, store-wide, under the lock.
    """
    cs = _plain(session)
    cs_id = str(cs.get("id") or "").strip()
    if not cs_id.startswith("cs_"):
        return {"ok": False, "reason": "bad_checkout_id"}
    md = _plain(cs.get("metadata"))
    kind = (md.get("purpose") or "pack").strip().lower()
    if kind != "pack":
        return {"ok": False, "reason": "not_a_top_up"}
    payment_status = str(cs.get("payment_status") or "").lower()
    if payment_status != "paid":
        # complete + unpaid = a delayed method (Klarna, bank) still clearing;
        # async_payment_succeeded arrives later and lands it.
        status = str(cs.get("status") or "").lower()
        return {"ok": False, "reason": "processing" if status == "complete" else "unpaid"}
    details = _plain(cs.get("customer_details"))
    raw_email = md.get("email") or cs.get("customer_email") or details.get("email") or ""
    try:
        email = _normalize_email(raw_email)
    except ValueError:
        return {"ok": False, "reason": "no_account"}

    pack = pack_by_id(md.get("pack"))
    credit = float(pack["credit_usd"])
    with _LOCK:
        store = _read()
        done = store.setdefault("redeemed_checkouts", [])
        accounts = store.setdefault("accounts", {})
        acct = accounts.get(email)
        legacy = bool(acct and cs_id in (acct.get("redeemed") or []))
        if cs_id in done or legacy:
            return {"ok": True, "already_redeemed": True, "email": email}
        if not isinstance(acct, dict):
            acct = _new_account(email)
        _roll_period(acct)
        customer = cs.get("customer")
        if customer:
            acct["stripe_customer_id"] = str(customer)
        acct["wallet_usd"] = round(float(acct.get("wallet_usd") or 0.0) + credit, 6)
        acct["on_demand"] = True
        receipt = {
            "cs": cs_id,
            "kind": "pack",
            "label": pack["label"],
            "pack": pack["id"],
            "credit_usd": credit,
            "amount_cents": int(cs.get("amount_total") or 0),
            "invoice": str(cs.get("invoice") or "") or None,
            "ts": int(time.time()),
        }
        acct["payments"] = ([receipt] + list(acct.get("payments") or []))[:24]
        redeemed = list(acct.get("redeemed") or [])
        redeemed.append(cs_id)
        acct["redeemed"] = redeemed[-200:]
        accounts[email] = acct
        done.append(cs_id)
        store["redeemed_checkouts"] = done[-5000:]
        _write(store)
    log.info("billing: credited %s -> %s (+$%.2f)", cs_id, email, credit)
    return {"ok": True, "already_redeemed": False, "email": email, "credit_usd": credit}


def redeem(checkout_session_id: str) -> Dict[str, Any]:
    """Browser return from Checkout: look the session up at Stripe (never
    trust the URL) and fulfil it. The webhook may already have."""
    if not is_payments_enabled():
        return {"ok": False, "reason": "payments_disabled"}
    cs_id = (checkout_session_id or "").strip()
    if not cs_id.startswith("cs_"):
        return {"ok": False, "reason": "bad_checkout_id"}
    s = _stripe_client()
    try:
        cs = s.checkout.Session.retrieve(cs_id)
    except Exception as e:  # noqa: BLE001
        log.warning("billing: retrieve %s failed: %s", cs_id, e)
        return {"ok": False, "reason": "checkout_not_found"}
    return fulfill_checkout(cs)


def handle_paid_checkout(checkout_session_id: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Kept for callers of the old name: re-fetch and fulfil."""
    return redeem(checkout_session_id)


def public_status(*, ignore_cookie: bool = False) -> Dict[str, Any]:
    acct = current_account(ignore_cookie=ignore_cookie)
    _roll_period(acct)
    email = (acct.get("email") or "").strip() or None
    plan_id = acct.get("plan") if email else "free"
    if plan_id not in PLANS:
        plan_id = "free"
    plan = PLANS[plan_id]
    cap = acct.get("monthly_cap_usd") if email else None
    billed = float(acct.get("billed_usd") or 0.0) if email else 0.0
    remaining_cap = None if cap is None else round(max(0.0, float(cap) - billed), 6)
    hosted = requires_wallet() or plan_id == "play"
    payments_on = is_payments_enabled()
    return {
        "payments_enabled": payments_on,
        "requires_wallet": requires_wallet(),
        "hosted_view": hosted,
        "markup": markup() if hosted else 1.0,
        "account": {
            "email": email,
            "linked": bool(email),
            "has_customer": bool(acct.get("stripe_customer_id")),
        },
        "plan": {
            "id": plan_id,
            "label": plan["label"],
            "price_cents": plan["price_cents"],
            "included_usd": float(acct.get("included_usd") or plan["included_usd"] or 0.0) if email else 0.0,
            "included_left_usd": included_left(acct) if email else 0.0,
            "blurb": plan["blurb"],
        },
        "plans": [
            {
                "id": p["id"],
                "label": p["label"],
                "price_cents": p["price_cents"],
                "included_usd": p["included_usd"],
                "blurb": p["blurb"],
                "current": p["id"] == plan_id,
            }
            for p in PLANS.values()
        ],
        "wallet_usd": round(float(acct.get("wallet_usd") or 0.0), 6) if email else 0.0,
        "billed_usd": round(billed, 6),
        "available_usd": available_usd(acct) if email else 0.0,
        "on_demand": bool(acct.get("on_demand", True)) if email else True,
        "account_cap_usd": cap,
        "account_remaining_usd": remaining_cap,
        "account_over_cap": over_account_cap(acct) if email else False,
        "period_start": acct.get("period_start") if email else _period_start(),
        "packs": [
            {
                "id": p["id"],
                "label": p["label"],
                "price_cents": p["price_cents"],
                "credit_usd": p["credit_usd"],
                "blurb": p.get("blurb") or "",
                "usual": bool(p.get("usual")),
                "display_price": f"${p['price_cents'] / 100:.0f}",
            }
            for p in PACKS
        ],
        "payments": list(acct.get("payments") or [])[:8] if email else [],
    }
