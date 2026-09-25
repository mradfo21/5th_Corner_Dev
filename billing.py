"""Account + hosted usage wallet — Cursor-style billing.

BYOK stays free (their keys, their provider bill). Hosted play is how
SOMEWHERE gets paid: prepaid wallet top-ups through the same Stripe account
as coin-op. Every paid model call is charged to the wallet that caused it,
at provider cost times a markup, the moment cost_tracker logs it.

A wallet belongs to one browser (BILLING_LIVE_PLAN C2): a random id in a
signed, HttpOnly cookie, made on the first hosted request. No sign-in, and
no store-wide "current account" on a hosted server, so nobody can spend a
wallet by typing someone's email. Threads started while serving a request
inherit that request's wallet, so background renders charge the right
player.

Environment:
  STRIPE_SECRET_KEY / STRIPE_PUBLISHABLE_KEY   same as coin-op
  FEATURE_BILLING         "1" to pause generation when the wallet is empty
  BILLING_MARKUP          default 2.0 (we charge 2× provider cost)
  BILLING_HOST_PAYS       "1" = this host foots the bill (dev / operator)
  PUBLIC_BASE_URL         checkout return host
  SOMEWHERE_BILLING_PATH  override store path (tests)
  SOMEWHERE_BILLING_SECRET signs the wallet cookie (default: a random
                           secret kept beside the wallet store)
  STRIPE_TAX_CODE         product tax code (Managed Payments)
  STRIPE_CHECKOUT_UI      embedded_page (default) | hosted_page
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import app_identity

try:
    import stripe  # type: ignore
except Exception:  # noqa: BLE001
    stripe = None  # type: ignore

log = logging.getLogger("billing")

TITLE = app_identity.LEGACY_DATA_DIR_NAME
# A new name: the old email cookie ("somewhere_account") is ignored.
COOKIE = "somewhere_wallet"
_KEY_RE = re.compile(r"^w_[0-9a-f]{32}$")

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
    return app_identity.appdata_root() / "billing.json"


def _path() -> Path:
    return _store_path or _default_store_path()


def _empty_store() -> Dict[str, Any]:
    return {"active_key": None, "accounts": {}}


def _read() -> Dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty_store()
        data.setdefault("accounts", {})
        data.setdefault("active_key", None)
        return data
    except Exception:
        return _empty_store()


def _write(payload: Dict[str, Any]) -> None:
    dest = _path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(dest)


def _new_account(key: str, email: Optional[str] = None) -> Dict[str, Any]:
    return {
        "key": key,
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


_secret_cache: Dict[str, bytes] = {}
_SECRET_LOCK = threading.Lock()


def _cookie_secret() -> bytes:
    """Signs wallet cookies. SOMEWHERE_BILLING_SECRET if set; otherwise a
    random secret made once and kept next to the wallet store (on the
    persistent disk), so wallets survive redeploys and a rotated Stripe key."""
    env = (os.environ.get("SOMEWHERE_BILLING_SECRET") or "").strip()
    if env:
        return env.encode("utf-8")
    path = _path().with_name("billing_secret")
    cached = _secret_cache.get(str(path))
    if cached:
        return cached
    with _SECRET_LOCK:
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except Exception:
            raw = ""
        if not raw:
            raw = secrets.token_hex(32)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(raw, encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                log.warning("billing: could not keep the cookie secret at %s: %s", path, e)
        _secret_cache[str(path)] = raw.encode("utf-8")
        return _secret_cache[str(path)]


def new_key() -> str:
    return "w_" + secrets.token_hex(16)


def cookie_value(key: str) -> str:
    if not _KEY_RE.match(key or ""):
        raise ValueError("Not a wallet id.")
    sig = hmac.new(_cookie_secret(), key.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"{key}|{sig}"


def key_from_cookie(raw: Optional[str]) -> Optional[str]:
    text = (raw or "").strip()
    if "|" not in text:
        return None
    key, sig = text.rsplit("|", 1)
    if not _KEY_RE.match(key):
        return None
    expect = hmac.new(_cookie_secret(), key.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expect):
        return None
    return key


def _in_request() -> bool:
    try:
        from flask import has_request_context
        return bool(has_request_context())
    except Exception:
        return False


def _request_key(*, mint: bool = False) -> Optional[str]:
    """This browser's wallet id. With ``mint``, a browser without one gets a
    new id now, and the response carries the cookie."""
    if not _in_request():
        return None
    try:
        from flask import after_this_request, g, request
    except Exception:
        return None
    key = key_from_cookie(request.cookies.get(COOKIE))
    if key:
        return key
    key = getattr(g, "_billing_new_key", None)
    if key or not mint:
        return key
    key = new_key()
    g._billing_new_key = key

    @after_this_request
    def _set_wallet_cookie(resp):
        resp.set_cookie(COOKIE, cookie_value(key), max_age=5 * 365 * 24 * 3600,
                        httponly=True, samesite="Lax", path="/",
                        secure=request.is_secure)
        return resp

    return key


# ── wallet attribution for work that outlives the request ─────────────────
# A render started by a turn finishes on its own thread after the response
# went out; the wallet that asked for it still pays. Every Thread made while
# a wallet is known carries that wallet into its run().
_tl = threading.local()


def thread_key() -> Optional[str]:
    return getattr(_tl, "key", None)


def _known_key() -> Optional[str]:
    return _request_key() or thread_key()


def _install_thread_inheritance() -> None:
    if getattr(threading.Thread, "_billing_inherits", False):
        return
    original_init = threading.Thread.__init__

    def __init__(self, *args, **kwargs):  # type: ignore[no-redef]
        original_init(self, *args, **kwargs)
        try:
            key = _known_key()
        except Exception:  # noqa: BLE001
            key = None
        if not key:
            return
        run = self.run

        def run_as_wallet():
            _tl.key = key
            try:
                run()
            finally:
                _tl.key = None

        self.run = run_as_wallet

    threading.Thread.__init__ = __init__  # type: ignore[method-assign]
    threading.Thread._billing_inherits = True  # type: ignore[attr-defined]

    # A pool's worker threads outlive the job that started them: each job
    # runs as the wallet that submitted it (or none), never as whoever
    # happened to be first to wake the pool.
    try:
        import concurrent.futures.thread as cft
    except Exception:  # noqa: BLE001
        return
    original_submit = cft.ThreadPoolExecutor.submit

    def submit(self, fn, /, *args, **kwargs):
        try:
            key = _known_key()
        except Exception:  # noqa: BLE001
            key = None

        def run_job(*a, **kw):
            before = getattr(_tl, "key", None)
            _tl.key = key
            try:
                return fn(*a, **kw)
            finally:
                _tl.key = before

        return original_submit(self, run_job, *args, **kwargs)

    cft.ThreadPoolExecutor.submit = submit  # type: ignore[method-assign]


_install_thread_inheritance()


def _roll_period(acct: Dict[str, Any]) -> None:
    start = _period_start()
    if acct.get("period_start") == start:
        return
    acct["period_start"] = start
    acct["included_used_usd"] = 0.0
    acct["billed_usd"] = 0.0
    if acct.get("plan") == "play":
        acct["included_usd"] = float(PLANS["play"]["included_usd"])


def current_key(*, mint: bool = False, ignore_cookie: bool = False) -> Optional[str]:
    """The wallet this code is running for.

    Serving a request: that browser's cookie, and nothing else — a hosted
    server never falls back to a store-wide account. A thread started by a
    request: the wallet it inherited. Outside both (the desktop app, tests):
    the store's one active wallet.
    """
    if _in_request():
        if ignore_cookie:
            return None
        return _request_key(mint=mint)
    key = thread_key()
    if key:
        return key
    return _read().get("active_key")


def current_account(*, ignore_cookie: bool = False, mint: bool = False) -> Dict[str, Any]:
    key = current_key(mint=mint, ignore_cookie=ignore_cookie)
    if not key:
        return _new_account("")
    acct = _read().get("accounts", {}).get(key)
    if not isinstance(acct, dict):
        return _new_account(key)
    acct.setdefault("key", key)
    _roll_period(acct)
    return acct


def _save_account(acct: Dict[str, Any]) -> Dict[str, Any]:
    key = (acct.get("key") or "").strip()
    if not key:
        raise ValueError("Account has no wallet id.")
    with _LOCK:
        store = _read()
        existing = store.get("accounts", {}).get(key) or {}
        merged = dict(existing)
        merged.update(acct)
        store.setdefault("accounts", {})[key] = merged
        if not _in_request() and not thread_key():
            store["active_key"] = key
        _write(store)
        return merged


def _update_account(key: str, **fields: Any) -> Dict[str, Any]:
    """Set just these fields on the wallet, read fresh under the lock — never
    a whole stale snapshot, which could undo a charge or a payment that
    landed in between."""
    if not key:
        raise ValueError("No wallet.")
    with _LOCK:
        store = _read()
        acct = store.get("accounts", {}).get(key)
        acct = dict(acct) if isinstance(acct, dict) else _new_account(key)
        acct["key"] = key
        acct.update(fields)
        store.setdefault("accounts", {})[key] = acct
        if not _in_request() and not thread_key():
            store["active_key"] = key
        _write(store)
        return acct


def link_email(raw: Any) -> str:
    """Where Stripe sends this wallet's receipts. Not a sign-in: it never
    moves the browser to another wallet."""
    email = _normalize_email(raw)
    if _in_request():
        key = current_key(mint=True)
    else:
        key = current_key() or new_key()
    _update_account(key, email=email)
    return email


def unlink() -> None:
    """Forget the receipt email. Outside a request (desktop, tests) also
    deselect the active wallet."""
    if _in_request():
        key = current_key()
        if key and current_account().get("email"):
            _update_account(key, email=None)
        return
    with _LOCK:
        store = _read()
        store["active_key"] = None
        _write(store)


def set_on_demand(value: Any) -> bool:
    acct = current_account(mint=True)
    if not acct.get("key"):
        raise ValueError("No wallet on this device yet.")
    _update_account(acct["key"], on_demand=bool(value))
    return bool(value)


def set_monthly_cap_usd(value: Any) -> Optional[float]:
    acct = current_account(mint=True)
    if not acct.get("key"):
        raise ValueError("No wallet on this device yet.")
    cap: Optional[float] = None
    if value is not None and str(value).strip() != "":
        n = float(value)
        if n < 0:
            raise ValueError("Monthly cap cannot be negative.")
        if n > 0:
            cap = round(n, 2)
    _update_account(acct["key"], monthly_cap_usd=cap)
    return cap


def set_unlimited(value: bool) -> str:
    """The owner's switch: this browser's wallet plays free and is never
    charged (costs are still logged, at provider cost). Set from /owner with
    the ADMIN_TOKEN."""
    acct = current_account(mint=True)
    if not acct.get("key"):
        raise ValueError("No wallet on this device yet.")
    _update_account(acct["key"], unlimited=bool(value))
    return acct["key"]


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


def debit(amount_usd: float, *, key: Optional[str] = None,
          allow_negative: bool = False) -> Dict[str, Any]:
    """Burn included, then wallet. Returns the new account snapshot.

    ``allow_negative``: a cost that has already happened is charged in full
    even past zero; the next top-up covers it. (The gate stops new work at
    zero, so the overdraft is at most one in-flight call.)"""
    if amount_usd <= 0:
        return current_account()
    with _LOCK:
        store = _read()
        k = key or current_key()
        if not k:
            raise ValueError("No wallet.")
        acct = store.get("accounts", {}).get(k)
        acct = dict(acct) if isinstance(acct, dict) else _new_account(k)
        acct["key"] = k
        _roll_period(acct)
        left = included_left(acct)
        take_included = min(left, amount_usd)
        rest = round(amount_usd - take_included, 6)
        acct["included_used_usd"] = round(float(acct.get("included_used_usd") or 0.0) + take_included, 6)
        if rest > 0:
            wallet = float(acct.get("wallet_usd") or 0.0) - rest
            acct["wallet_usd"] = round(wallet if allow_negative else max(0.0, wallet), 6)
        acct["billed_usd"] = round(float(acct.get("billed_usd") or 0.0) + amount_usd, 6)
        store.setdefault("accounts", {})[k] = acct
        _write(store)
        return acct


# session id -> the wallet that last played it, for costs logged on a thread
# that carries no wallet of its own (a timer, a pool made at import).
_session_owner: Dict[str, str] = {}


def charge_cost(cost_usd: Optional[float], session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Charge one logged provider cost to the wallet that caused it
    (BILLING_LIVE_PLAN C1). Called by cost_tracker.record_usage for every
    priced event. Returns {wallet, charged_usd, markup} or None when nothing
    is charged (not a hosted wallet server, unpriced, or no wallet known)."""
    if cost_usd is None or cost_usd <= 0 or not requires_wallet():
        return None
    sid = str(session_id or "").strip()
    key = _known_key()
    if key and sid:
        _session_owner[sid] = key
    elif sid:
        key = _session_owner.get(sid)
    if not key:
        log.warning("billing: unattributed cost $%.6f session=%s", cost_usd, sid or "?")
        return None
    try:
        if (_read().get("accounts", {}).get(key) or {}).get("unlimited"):
            return {"wallet": key, "charged_usd": 0.0, "markup": 0.0}
    except Exception:  # noqa: BLE001
        pass
    m = markup()
    retail = round(float(cost_usd) * m, 6)
    try:
        debit(retail, key=key, allow_negative=True)
    except Exception as e:  # noqa: BLE001
        log.warning("billing: charge failed wallet=%s: %s", key, e)
        return None
    return {"wallet": key, "charged_usd": retail, "markup": m}


def grant_wallet(amount_usd: float, *, source: str, checkout_session_id: Optional[str] = None) -> Dict[str, Any]:
    acct = current_account(mint=True)
    if not acct.get("key"):
        raise ValueError("No wallet.")
    key = acct["key"]
    with _LOCK:
        store = _read()
        row = store.get("accounts", {}).get(key)
        row = dict(row) if isinstance(row, dict) else _new_account(key)
        row["key"] = key
        row["wallet_usd"] = round(float(row.get("wallet_usd") or 0.0) + max(0.0, amount_usd), 6)
        row["on_demand"] = True
        if checkout_session_id:
            redeemed = list(row.get("redeemed") or [])
            if checkout_session_id not in redeemed:
                redeemed.append(checkout_session_id)
            row["redeemed"] = redeemed
        store.setdefault("accounts", {})[key] = row
        _write(store)
        return row


# ── live time, measured here (BILLING_LIVE_PLAN A5) ───────────────────────
# Reactor video and TALK agents stream browser <-> provider directly, so the
# only duration the server is told is the one the browser reports when it
# hangs up. A meter starts when the server hands out the connection (token /
# talk session) and stops at the report; the player pays the longer of the
# two. A meter whose browser stops polling /api/feed for REAP_AFTER_S is
# closed at the last poll and charged then.
REAP_AFTER_S = 90.0
METER_CAP_S = 2 * 3600.0
_METERS: Dict[tuple, Dict[str, Any]] = {}
_LAST_SEEN: Dict[str, float] = {}
_METER_LOCK = threading.Lock()
_reaper_started = False


def heartbeat() -> None:
    """The browser is still here (called on /api/feed)."""
    key = _request_key()
    if key:
        _LAST_SEEN[key] = time.time()


def meter_start(kind: str, session_id: str, *, service_type: str, provider: str, model: str) -> None:
    if not requires_wallet():
        return
    key = _known_key()
    if not key:
        return
    now = time.time()
    _LAST_SEEN[key] = now
    with _METER_LOCK:
        # A token refreshed mid-stream keeps the meter it already had.
        _METERS.setdefault((key, kind, str(session_id or "default")), {
            "start": now, "service_type": service_type, "provider": provider, "model": model,
        })
    _ensure_reaper()


def meter_stop(kind: str, session_id: str, client_seconds: float, *, model: Optional[str] = None) -> float:
    """Close the meter; return the seconds the browser left unreported (to be
    logged on top of what it did report). 0 when no meter was running."""
    key = _known_key()
    if not key:
        return 0.0
    with _METER_LOCK:
        m = _METERS.pop((key, kind, str(session_id or "default")), None)
    if not m:
        return 0.0
    server = min(METER_CAP_S, max(0.0, time.time() - float(m["start"])))
    reported = max(0.0, float(client_seconds or 0.0))
    gap = server - reported
    return round(gap, 1) if gap > 5.0 else 0.0


def _reap_once(now: Optional[float] = None) -> int:
    now = now or time.time()
    stale = []
    with _METER_LOCK:
        for mk, m in list(_METERS.items()):
            last = _LAST_SEEN.get(mk[0], m["start"])
            if now - last > REAP_AFTER_S:
                stale.append((mk, m, last))
                _METERS.pop(mk, None)
    for (key, kind, sid), m, last in stale:
        seconds = min(METER_CAP_S, max(0.0, last - float(m["start"])))
        if seconds <= 0:
            continue
        before = getattr(_tl, "key", None)
        _tl.key = key
        try:
            import cost_tracker
            cost_tracker.record_usage(sid, m["service_type"], m["provider"], m["model"],
                                      operation="server_meter_reaped",
                                      output_units=seconds, unit_type="seconds", success=True)
        except Exception as e:  # noqa: BLE001
            log.warning("billing: reaping %s failed: %s", kind, e)
        finally:
            _tl.key = before
    return len(stale)


def _ensure_reaper() -> None:
    global _reaper_started
    if _reaper_started:
        return
    _reaper_started = True

    def loop():
        _tl.key = None
        while True:
            time.sleep(30)
            try:
                _reap_once()
            except Exception as e:  # noqa: BLE001
                log.warning("billing: reaper: %s", e)

    t = threading.Thread(target=loop, name="billing-meter-reaper", daemon=True)
    t.run = loop  # never inherit the wallet of the request that woke it
    t.start()


# ── the rate card (BILLING_LIVE_PLAN B3) ──────────────────────────────────
# Each thing a run can use, computed from pricing.json and markup() at the
# moment it is read — the same numbers every charge is computed from, so the
# card cannot drift from the bill.
_CARD = (
    # label, provider, model, unit_type, units, size, per
    ("Story beat", "gemini", "gemini-3.1-flash-lite", "tokens", (10000, 400), None,
     "about 10K tokens read, 400 written"),
    ("Picture", "gemini", "gemini-3.1-flash-lite-image", "images", 1, "1K", "each, 1K"),
    ("Sharp picture", "gemini", "gemini-3.1-flash-image", "images", 1, "2K", "each, 2K"),
    ("Cutscene frame", "gemini", "gemini-3-pro-image", "images", 1, "4K", "each, 4K"),
    ("Spoken line", "elevenlabs", "tts", "characters", 200, None, "about 200 characters"),
    ("Sound effect", "elevenlabs", "eleven_text_to_sound_v2", "seconds", 60, None, "per minute"),
    ("Music", "elevenlabs", "music_v2", "seconds", 60, None, "per minute"),
    ("Talking with someone", "elevenlabs", "talk_agent", "seconds", 60, None, "per minute"),
    ("Live video", "reactor", "happy-oyster", "seconds", 60, None, "per minute"),
    ("Live video, light", "reactor", "lingbot-world-2", "seconds", 60, None, "per minute"),
    ("Video clip", "veo", "veo-3.1-generate-preview", "seconds", 8, None, "8 seconds"),
)


def rate_card() -> Dict[str, Any]:
    import pricing
    m = markup() if requires_wallet() else 1.0
    rows = []
    checked = []
    for label, provider, model, unit_type, units, size, per in _CARD:
        if isinstance(units, tuple):
            cost = pricing.estimate_cost(provider, model, unit_type, units[0], units[1])
        else:
            cost = pricing.estimate_cost(provider, model, unit_type, None, units, size=size)
        rate = pricing.get_rate(provider, model, unit_type) or {}
        if rate.get("checked"):
            checked.append(str(rate["checked"]))
        rows.append({
            "thing": label, "per": per, "provider": provider, "model": model,
            "provider_usd": None if cost is None else round(cost, 4),
            "price_usd": None if cost is None else round(cost * m, 4),
            "source": rate.get("source"),
        })
    return {"markup": m, "checked": max(checked) if checked else None, "rows": rows}


def settle_session(session_id: str, cost_before_usd: float) -> Optional[Dict[str, Any]]:
    """Retired: costs are charged as they are logged (charge_cost). Kept so
    older call sites stay harmless — charging here too would bill twice."""
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
    acct = current_account(mint=True)
    if acct.get("unlimited"):
        return None
    if not (acct.get("key") or "").strip():
        return {
            "needs_billing": True,
            "reason": "needs_account",
            "message": "Open ACCOUNT to add money and play on our keys.",
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
                else "This costs more than what's left in your wallet. Add money to go on."
                if reason == "insufficient_balance"
                else "Your wallet is empty. Add money to keep playing."
                if acct.get("payments")
                else "Add money to play. You pay what each turn's AI costs — every run gets a receipt."
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
        for key, acct in (store.get("accounts") or {}).items():
            if not isinstance(acct, dict):
                continue
            if str(acct.get("stripe_customer_id") or "") == customer:
                found = key
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
    log.info("billing: invoice.paid refreshed Play included wallet=%s", found)
    return {"ok": True, "account": found, "included_usd": float(PLANS["play"]["included_usd"])}


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


# Stripe is the merchant of record on this account (Managed Payments): it
# works out and remits sales tax / VAT, and every product must carry a tax
# code. A wallet top-up is time on a game that is generated and streamed to
# the player, never downloaded: "Video Games - streamed - non subscription -
# with limited rights". Override with STRIPE_TAX_CODE if Stripe or your
# accountant classify it differently.
DEFAULT_TAX_CODE = "txcd_10201003"


def _tax_code() -> str:
    return (os.environ.get("STRIPE_TAX_CODE") or DEFAULT_TAX_CODE).strip()


def checkout_ui_mode() -> str:
    """embedded_page: Stripe's checkout drawn inside the ACCOUNT sheet (the
    default). hosted_page: the player leaves for checkout.stripe.com and comes
    back. Managed Payments allows only these two."""
    mode = (os.environ.get("STRIPE_CHECKOUT_UI") or "embedded_page").strip().lower()
    return mode if mode in ("embedded_page", "hosted_page") else "embedded_page"


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
    acct = current_account(mint=True)
    key = (acct.get("key") or "").strip()
    if not key:
        raise ValueError("No wallet on this device.")
    email = (acct.get("email") or "").strip()
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
                "product_data": {"name": "ABYSS Play", "tax_code": _tax_code()},
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
                "product_data": {"name": f"ABYSS wallet top-up · {pack['label']}",
                                 "tax_code": _tax_code()},
            },
        }]
        mode = "payment"

    # No payment_method_types: the Dashboard's payment-method settings decide
    # (card, Link, wallets, Klarna…). Delayed methods finish after checkout —
    # the webhook's checkout.session.async_payment_succeeded credits those.
    kwargs: Dict[str, Any] = {
        "mode": mode,
        "line_items": line_items,
        "client_reference_id": key,
        "metadata": {
            "purpose": kind,
            "account": key,
            "pack": (pack or {}).get("id") or "",
        },
        "expires_at": int(time.time()) + 30 * 60,
        "billing_address_collection": "auto",
        "phone_number_collection": {"enabled": False},
        "submit_type": "auto",
    }
    ui_mode = checkout_ui_mode()
    kwargs["ui_mode"] = ui_mode
    if ui_mode == "embedded_page":
        # Cards finish inside the sheet; methods that leave for a bank come
        # back here, and the page redeems exactly as after hosted checkout.
        kwargs["return_url"] = success
    else:
        kwargs["success_url"] = success
        kwargs["cancel_url"] = cancel
    if mode == "payment":
        # Every top-up gets a Stripe invoice: the player's receipt, and a
        # record the Dashboard can show next to the wallet credit.
        # Managed Payments refuses invoice_data (checked in test mode): the
        # invoice is Stripe's own, issued in the seller-of-record's name.
        kwargs["invoice_creation"] = {"enabled": True}
    if acct.get("stripe_customer_id"):
        kwargs["customer"] = acct["stripe_customer_id"]
    else:
        # Checkout asks for the receipt email itself when we don't know it.
        if email:
            kwargs["customer_email"] = email
        if mode == "payment":
            kwargs["customer_creation"] = "always"

    checkout = s.checkout.Session.create(**kwargs)
    log.info("billing: checkout %s kind=%s wallet=%s", checkout.id, kind, key)
    return {
        "ui_mode": ui_mode,
        "url": checkout.url,
        "client_secret": getattr(checkout, "client_secret", None),
        "publishable_key": os.environ.get("STRIPE_PUBLISHABLE_KEY", "").strip(),
        "checkout_session_id": checkout.id,
        "kind": kind,
        "pack": (pack or {}).get("id"),
    }


def fulfill_checkout(session: Any) -> Dict[str, Any]:
    """Credit a paid top-up Checkout Session to the account that paid.

    The one path money takes into a wallet — the browser's return
    (``redeem``) and the webhook both land here, in either order, any number
    of times. The wallet is the one written into the session at checkout
    (``metadata.account``; ``metadata.email`` on sessions made before
    per-device wallets), never the browser's current one.
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
    receipt_email = str(details.get("email") or cs.get("customer_email") or "").strip().lower() or None
    target = str(md.get("account") or "").strip()
    if not _KEY_RE.match(target):
        try:
            target = _normalize_email(md.get("email") or "")  # legacy wallet keyed by email
        except ValueError:
            return {"ok": False, "reason": "no_account"}

    pack = pack_by_id(md.get("pack"))
    credit = float(pack["credit_usd"])
    with _LOCK:
        store = _read()
        done = store.setdefault("redeemed_checkouts", [])
        accounts = store.setdefault("accounts", {})
        acct = accounts.get(target)
        legacy = bool(acct and cs_id in (acct.get("redeemed") or []))
        if cs_id in done or legacy:
            return {"ok": True, "already_redeemed": True, "account": target}
        if not isinstance(acct, dict):
            acct = _new_account(target)
        acct.setdefault("key", target)
        if receipt_email and not acct.get("email"):
            acct["email"] = receipt_email
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
        accounts[target] = acct
        done.append(cs_id)
        store["redeemed_checkouts"] = done[-5000:]
        _write(store)
    log.info("billing: credited %s -> %s (+$%.2f)", cs_id, target, credit)
    return {"ok": True, "already_redeemed": False, "account": target, "credit_usd": credit}


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
    # A hosted wallet server gives every browser its wallet on first look.
    acct = current_account(ignore_cookie=ignore_cookie, mint=requires_wallet())
    _roll_period(acct)
    has = bool((acct.get("key") or "").strip())
    email = (acct.get("email") or "").strip() or None
    plan_id = acct.get("plan") if has else "free"
    if plan_id not in PLANS:
        plan_id = "free"
    plan = PLANS[plan_id]
    cap = acct.get("monthly_cap_usd") if has else None
    billed = float(acct.get("billed_usd") or 0.0) if has else 0.0
    remaining_cap = None if cap is None else round(max(0.0, float(cap) - billed), 6)
    hosted = requires_wallet() or plan_id == "play"
    payments_on = is_payments_enabled()
    return {
        "payments_enabled": payments_on,
        "requires_wallet": requires_wallet(),
        "hosted_view": hosted,
        "markup": markup() if hosted else 1.0,
        "unlimited": bool(acct.get("unlimited")) if has else False,
        "account": {
            "email": email,
            "linked": has,
            "has_customer": bool(acct.get("stripe_customer_id")),
        },
        "plan": {
            "id": plan_id,
            "label": plan["label"],
            "price_cents": plan["price_cents"],
            "included_usd": float(acct.get("included_usd") or plan["included_usd"] or 0.0) if has else 0.0,
            "included_left_usd": included_left(acct) if has else 0.0,
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
        "wallet_usd": round(float(acct.get("wallet_usd") or 0.0), 6) if has else 0.0,
        "billed_usd": round(billed, 6),
        "available_usd": available_usd(acct) if has else 0.0,
        "on_demand": bool(acct.get("on_demand", True)) if has else True,
        "account_cap_usd": cap,
        "account_remaining_usd": remaining_cap,
        "account_over_cap": over_account_cap(acct) if has else False,
        "period_start": acct.get("period_start") if has else _period_start(),
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
        "payments": list(acct.get("payments") or [])[:8] if has else [],
    }
