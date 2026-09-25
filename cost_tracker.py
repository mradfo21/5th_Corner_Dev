"""
Cost Tracker - Append-only usage ledger for every paid provider call.

Design goals (see ADMIN_COST_ANALYTICS_DASHBOARD_PLAN.md):
  * Never raise, never block the game loop. A tracking bug must not break
    gameplay — every public function catches its own exceptions and logs.
  * SQLite on disk (sessions/_analytics/usage.db) — same disk the project
    already recommends a persistent volume for (RENDER_STORAGE_LIMITATION.md).
    Zero new dependency (stdlib sqlite3), real GROUP BY/SUM/date-range
    queries for the dashboard.
  * `usage_events` is the source of truth; `session_cost_rollup` is a cheap
    denormalized cache kept up to date on every insert so the dashboard's
    main session list doesn't have to aggregate the whole ledger every load.
"""

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import pricing

ROOT = Path(__file__).parent.resolve()
SESSIONS_DIR = ROOT / "sessions"
# SOMEWHERE_ANALYTICS_DIR: a ledger of its own (tools/film_run.py --first-launch
# films a machine that has never spent anything).
ANALYTICS_DIR = Path(os.environ.get("SOMEWHERE_ANALYTICS_DIR") or SESSIONS_DIR / "_analytics")
DB_PATH = ANALYTICS_DIR / "usage.db"

_lock = threading.Lock()
_initialized = False

SERVICE_TYPES = ("text", "image", "video", "voice", "realtime")

# When THIS process started — used purely to answer "did the ledger survive
# a restart?" (see get_storage_health()). Deliberately captured at import
# time, before init_db() ever runs, so it's a true process-start timestamp.
_PROCESS_STARTED_AT = datetime.now(timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables/indexes if they don't exist yet. Safe to call repeatedly."""
    global _initialized
    if _initialized:
        return
    with _lock:
        if _initialized:
            return
        try:
            conn = _connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS usage_events (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts            TEXT NOT NULL,
                        session_id    TEXT NOT NULL,
                        turn_count    INTEGER,
                        service_type  TEXT NOT NULL,
                        provider      TEXT NOT NULL,
                        model         TEXT NOT NULL,
                        operation     TEXT,
                        input_units   REAL,
                        output_units  REAL,
                        unit_type     TEXT,
                        cost_usd      REAL,
                        latency_ms    INTEGER,
                        success       INTEGER NOT NULL DEFAULT 1,
                        error_message TEXT,
                        discord_guild_id TEXT,
                        discord_channel_id TEXT,
                        meta_json     TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_events(session_id);
                    CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts);
                    CREATE INDEX IF NOT EXISTS idx_usage_provider ON usage_events(provider, model);
                    CREATE INDEX IF NOT EXISTS idx_usage_service ON usage_events(service_type);

                    CREATE TABLE IF NOT EXISTS session_cost_rollup (
                        session_id            TEXT PRIMARY KEY,
                        total_cost_usd        REAL NOT NULL DEFAULT 0,
                        unpriced_event_count   INTEGER NOT NULL DEFAULT 0,
                        cost_by_service_json  TEXT,
                        cost_by_provider_json TEXT,
                        event_count           INTEGER NOT NULL DEFAULT 0,
                        error_count           INTEGER NOT NULL DEFAULT 0,
                        first_event_ts        TEXT,
                        last_event_ts         TEXT
                    );
                    """
                )
                # What the player was charged for the event (hosted wallet
                # servers; BILLING_LIVE_PLAN C1 / B1), next to what it cost us.
                have = {r[1] for r in conn.execute("PRAGMA table_info(usage_events)")}
                for col, decl in (("wallet", "TEXT"), ("charged_usd", "REAL"), ("markup", "REAL")):
                    if col not in have:
                        conn.execute(f"ALTER TABLE usage_events ADD COLUMN {col} {decl}")
                if "cost_usd_v1" not in have:
                    _reprice_history(conn)
                conn.commit()
            finally:
                conn.close()
            _initialized = True
        except Exception as e:
            print(f"[COST TRACKER] init_db failed (non-fatal, tracking disabled): {e}", flush=True)


def _reprice_history(conn: sqlite3.Connection) -> None:
    """One-time (BILLING_LIVE_PLAN A10): price every past event again on the
    corrected table (per-million tokens, pictures by size), keeping what it
    was first logged at in ``cost_usd_v1``, and rebuild the session rollups
    from the ledger."""
    conn.execute("ALTER TABLE usage_events ADD COLUMN cost_usd_v1 REAL")
    conn.execute("UPDATE usage_events SET cost_usd_v1 = cost_usd")
    rows = conn.execute(
        "SELECT id, provider, model, unit_type, input_units, output_units, meta_json "
        "FROM usage_events WHERE success = 1").fetchall()
    for r in rows:
        size = None
        try:
            meta = json.loads(r["meta_json"]) if r["meta_json"] else {}
            size = meta.get("size") or meta.get("image_size")
        except Exception:
            size = None
        try:
            cost = pricing.estimate_cost(r["provider"], r["model"], r["unit_type"],
                                         r["input_units"], r["output_units"], size=size)
        except Exception:
            continue
        conn.execute("UPDATE usage_events SET cost_usd = ? WHERE id = ?", (cost, r["id"]))
    conn.execute("DELETE FROM session_cost_rollup")
    for r in conn.execute("SELECT session_id, service_type, provider, cost_usd, success, ts "
                          "FROM usage_events ORDER BY id").fetchall():
        _upsert_rollup(conn, r["session_id"], r["service_type"], r["provider"],
                       r["cost_usd"], bool(r["success"]), r["ts"])
    print(f"[COST TRACKER] re-priced {len(rows)} past events on the corrected rate table", flush=True)


def _upsert_rollup(conn: sqlite3.Connection, session_id: str, service_type: str,
                    provider: str, cost_usd: Optional[float], success: bool, ts: str) -> None:
    row = conn.execute(
        "SELECT * FROM session_cost_rollup WHERE session_id = ?", (session_id,)
    ).fetchone()

    by_service = json.loads(row["cost_by_service_json"]) if row and row["cost_by_service_json"] else {}
    by_provider = json.loads(row["cost_by_provider_json"]) if row and row["cost_by_provider_json"] else {}

    if cost_usd is not None:
        by_service[service_type] = round(by_service.get(service_type, 0.0) + cost_usd, 8)
        by_provider[provider] = round(by_provider.get(provider, 0.0) + cost_usd, 8)

    total_cost = round((row["total_cost_usd"] if row else 0.0) + (cost_usd or 0.0), 8)
    unpriced = (row["unpriced_event_count"] if row else 0) + (1 if cost_usd is None else 0)
    event_count = (row["event_count"] if row else 0) + 1
    error_count = (row["error_count"] if row else 0) + (0 if success else 1)
    first_ts = row["first_event_ts"] if row and row["first_event_ts"] else ts

    conn.execute(
        """
        INSERT INTO session_cost_rollup
            (session_id, total_cost_usd, unpriced_event_count, cost_by_service_json,
             cost_by_provider_json, event_count, error_count, first_event_ts, last_event_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            total_cost_usd = excluded.total_cost_usd,
            unpriced_event_count = excluded.unpriced_event_count,
            cost_by_service_json = excluded.cost_by_service_json,
            cost_by_provider_json = excluded.cost_by_provider_json,
            event_count = excluded.event_count,
            error_count = excluded.error_count,
            first_event_ts = excluded.first_event_ts,
            last_event_ts = excluded.last_event_ts
        """,
        (session_id, total_cost, unpriced, json.dumps(by_service), json.dumps(by_provider),
         event_count, error_count, first_ts, ts),
    )


# ── Gemini calls, logged at the wire (BILLING_LIVE_PLAN A3) ───────────────
# Some callers never logged their Gemini calls (ai_provider_manager's chat and
# vision, several picture paths) and others logged pictures under a guessed
# model. Every generateContent request to the Gemini API is now logged from
# the HTTP call itself: the model in the URL, the picture size in the
# payload, the tokens and pictures in the response. A caller that still logs
# the same call afterwards on the same thread is folded into this record
# instead of counted twice (see _claim_wire_logged).
_wire = threading.local()
_WIRE_TTL_S = 120.0
_GEMINI_HOST = "generativelanguage.googleapis.com"


def _wire_pending(kind: str) -> list:
    table = getattr(_wire, "pending", None)
    if table is None:
        table = _wire.pending = {}
    pending = table.setdefault(kind, [])
    now = time.time()
    pending[:] = [t for t in pending if now - t < _WIRE_TTL_S]
    return pending


def _claim_wire_logged(service_type: str, provider: str, operation: Optional[str],
                       output_units: Optional[float], model: Optional[str] = None) -> bool:
    if provider != "gemini" or operation == "wire" or service_type not in ("image", "text"):
        return False
    # Text callers name the real model, so only that model's call folds in.
    # Picture callers name a purpose ("talk_portrait"), so any picture does.
    pending = _wire_pending(f"text:{model}" if service_type == "text" else "image")
    if not pending:
        return False
    n = max(1, int(round(float(output_units or 1)))) if service_type == "image" else 1
    del pending[:n]
    return True


def _gemini_model(url: str) -> Optional[str]:
    if _GEMINI_HOST not in url or ":generateContent" not in url:
        return None
    model = url.split("/models/", 1)[-1].split(":", 1)[0].split("?", 1)[0]
    return model[:-len("-preview")] if model.endswith("-preview") else model


def _log_wire(model: str, payload: Any, response: Any, latency_ms: int) -> None:
    try:
        is_image = "image" in model
        ok = getattr(response, "status_code", 0) == 200
        count = 0
        usage: Dict[str, Any] = {}
        if ok:
            try:
                body = response.json()
                usage = body.get("usageMetadata") or {}
                for cand in body.get("candidates") or []:
                    for part in ((cand or {}).get("content") or {}).get("parts") or []:
                        if isinstance(part, dict) and "inlineData" in part:
                            count += 1
            except Exception:
                count = 0
        if ok and is_image and count <= 0:
            return  # nothing drawn (blocked / text only): no picture billed
        try:
            import engine as _engine
            sid = _engine.get_active_session_id() or "default"
        except Exception:
            sid = "default"
        err = None if ok else f"HTTP {getattr(response, 'status_code', '?')}"
        if is_image:
            size = None
            try:
                size = (((payload or {}).get("generationConfig") or {}).get("imageConfig") or {}).get("imageSize")
            except Exception:
                size = None
            meta = {"size": size, "source": "wire"}
            if usage:
                meta["usage"] = usage
            record_usage(sid, "image", "gemini", model, operation="wire",
                         output_units=count if ok else None, unit_type="images",
                         latency_ms=latency_ms, success=ok, error_message=err, meta=meta)
            if ok:
                _wire_pending("image").extend([time.time()] * count)
        else:
            out_tokens = (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0)
            record_usage(sid, "text", "gemini", model, operation="wire",
                         input_units=usage.get("promptTokenCount") if ok else None,
                         output_units=out_tokens if ok else None, unit_type="tokens",
                         latency_ms=latency_ms, success=ok, error_message=err,
                         meta={"source": "wire"})
            if ok:
                _wire_pending(f"text:{model}").append(time.time())
    except Exception as e:  # noqa: BLE001
        print(f"[COST TRACKER] wire log failed (non-fatal): {e}", flush=True)


def _note_gemini_refusal(response: Any) -> None:
    """A Gemini key out of quota or refused: tell the HUD (provider_bridge)."""
    try:
        import provider_bridge
        status = getattr(response, "status_code", 0)
        if status in (401, 403, 429):
            provider_bridge.note_problem("gemini", status, provider_bridge._err_text(response))
        elif status == 200:
            provider_bridge.note_ok("gemini")
    except Exception:
        pass


def _openai_answers_gemini() -> bool:
    try:
        import provider_bridge
        return provider_bridge.active()
    except Exception:
        return False


def _install_wire_meter() -> None:
    try:
        import requests
    except Exception:
        return
    if getattr(requests.Session, "_cost_wire", False):
        return
    original = requests.Session.request

    def request(self, method, url, *args, **kwargs):
        model = _gemini_model(str(url or "")) if str(method).upper() == "POST" else None
        t0 = time.time()
        response = original(self, method, url, *args, **kwargs)
        if model and not getattr(response, "_bridged", False):
            _log_wire(model, kwargs.get("json"), response, int((time.time() - t0) * 1000))
            _note_gemini_refusal(response)
        return response

    requests.Session.request = request
    requests.Session._cost_wire = True


_install_wire_meter()


def record_usage(session_id: str, service_type: str, provider: str, model: str, *,
                  operation: Optional[str] = None,
                  input_units: Optional[float] = None,
                  output_units: Optional[float] = None,
                  unit_type: Optional[str] = None,
                  latency_ms: Optional[int] = None,
                  success: bool = True,
                  error_message: Optional[str] = None,
                  turn_count: Optional[int] = None,
                  discord_guild_id: Optional[str] = None,
                  discord_channel_id: Optional[str] = None,
                  meta: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """
    Best-effort insert of one usage event. Never raises. Returns the
    estimated cost (or None if unpriced/tracking failed) purely as a
    convenience for callers/tests — nothing depends on the return value.
    """
    try:
        if success and _claim_wire_logged(service_type, provider, operation, output_units, model):
            return None  # this picture was already logged from the HTTP call
        if provider == "gemini" and service_type in ("text", "image") and _openai_answers_gemini():
            # OpenAI chosen in ACCOUNT: this "Gemini" call was answered by
            # OpenAI, and provider_bridge logged it at OpenAI's model.
            return None
        init_db()
        cost_usd = None
        if success:
            size = None
            if isinstance(meta, dict):
                size = meta.get("size") or meta.get("image_size")
            cost_usd = pricing.estimate_cost(provider, model, unit_type, input_units, output_units,
                                             size=size)
        charge = _charge(cost_usd, session_id) or _wallet_only()
        ts = _now_iso()
        error_text = (str(error_message)[:1000] if error_message else None)

        with _lock:
            conn = _connect()
            try:
                conn.execute(
                    """
                    INSERT INTO usage_events
                        (ts, session_id, turn_count, service_type, provider, model, operation,
                         input_units, output_units, unit_type, cost_usd, latency_ms, success,
                         error_message, discord_guild_id, discord_channel_id, meta_json,
                         wallet, charged_usd, markup)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (ts, session_id, turn_count, service_type, provider, model, operation,
                     input_units, output_units, unit_type, cost_usd, latency_ms, 1 if success else 0,
                     error_text, discord_guild_id, discord_channel_id,
                     json.dumps(meta) if meta else None,
                     (charge or {}).get("wallet"), (charge or {}).get("charged_usd"),
                     (charge or {}).get("markup")),
                )
                _upsert_rollup(conn, session_id, service_type, provider, cost_usd, success, ts)
                conn.commit()
            finally:
                conn.close()
        return cost_usd
    except Exception as e:
        print(f"[COST TRACKER] record_usage failed (non-fatal): {e}", flush=True)
        return None


def _charge(cost_usd: Optional[float], session_id: str) -> Optional[Dict[str, Any]]:
    """Charge the wallet behind this call, if this is a hosted wallet server.
    Never raises: a billing fault must not break the game or the ledger."""
    if cost_usd is None or cost_usd <= 0:
        return None
    try:
        import billing
        return billing.charge_cost(cost_usd, session_id)
    except Exception as e:  # noqa: BLE001
        print(f"[COST TRACKER] charge failed (non-fatal): {e}", flush=True)
        return None


def _wallet_only() -> Optional[Dict[str, Any]]:
    """The wallet behind an uncharged event (failed, unpriced), so its
    receipt can still list it as "not charged"."""
    try:
        import billing
        if billing.requires_wallet():
            key = billing._known_key()
            if key:
                return {"wallet": key, "charged_usd": 0.0, "markup": None}
    except Exception:
        pass
    return None


@contextmanager
def track(session_id: str, service_type: str, provider: str, model: str, **kwargs):
    """
    Context manager for call sites where units aren't known until after the
    call completes:

        with cost_tracker.track(session_id, "voice", "elevenlabs", "tts") as t:
            audio = synthesize(text)
            t["output_units"] = len(text)
            t["unit_type"] = "characters"

    Records on exit either way (success=False + error_message on exception).
    """
    ctx: Dict[str, Any] = {"success": True, "error_message": None}
    t0 = time.time()
    try:
        yield ctx
    except Exception as e:
        ctx["success"] = False
        ctx["error_message"] = str(e)
        raise
    finally:
        latency_ms = int((time.time() - t0) * 1000)
        record_usage(
            session_id, service_type, provider, model,
            operation=kwargs.get("operation"),
            input_units=ctx.get("input_units", kwargs.get("input_units")),
            output_units=ctx.get("output_units", kwargs.get("output_units")),
            unit_type=ctx.get("unit_type", kwargs.get("unit_type")),
            latency_ms=latency_ms,
            success=ctx["success"],
            error_message=ctx["error_message"],
            turn_count=kwargs.get("turn_count"),
            discord_guild_id=kwargs.get("discord_guild_id"),
            discord_channel_id=kwargs.get("discord_channel_id"),
            meta=kwargs.get("meta"),
        )


# ─────────────────────────── read-side queries ───────────────────────────
# Used by the /api/admin/analytics/* routes in api.py. Kept here (rather than
# in api.py) so they're unit-testable without spinning up Flask.

_RANGE_TO_SQL = {
    "24h": "-1 day",
    "7d": "-7 days",
    "30d": "-30 days",
    "all": None,
}


def _since_iso(range_key: str) -> Optional[str]:
    import re as _re
    delta = _RANGE_TO_SQL.get(range_key, _RANGE_TO_SQL["7d"])
    if delta is None:
        return None
    n, unit = _re.match(r"-(\d+)\s*(day|days)", delta).groups()
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=int(n))).isoformat()


def get_storage_health() -> Dict[str, Any]:
    """Diagnose whether the ledger is actually on persistent storage.

    There's no way to ask "is this disk ephemeral?" directly from inside the
    container, so this combines two independent signals:

      1. `mount_detected` — is `sessions/` its own mount point (Render's
         persistent disk is mounted exactly there, per render.yaml)? Running
         locally, or on a container without the disk attached, this is a
         plain directory on the root filesystem and comes back False.
      2. `survived_restart` — is the OLDEST event in the ledger older than
         THIS process's start time? If so, that event was written by a
         previous process and the file demonstrably outlived a restart —
         the strongest possible evidence persistence is actually working.
         None (not False) if there's no data yet or no restart has
         happened to observe, since that's inconclusive rather than bad.

    Surfaced on the admin dashboard so "is my cost data actually going to
    stick around" is answered in the UI instead of guessed at after the
    next redeploy silently wipes it (again).
    """
    init_db()
    mount_detected = False
    try:
        mount_detected = os.path.ismount(str(SESSIONS_DIR))
    except OSError:
        pass

    oldest_event_at = None
    try:
        with _lock:
            conn = _connect()
            try:
                row = conn.execute("SELECT MIN(ts) AS oldest FROM usage_events").fetchone()
                oldest_event_at = row["oldest"] if row else None
            finally:
                conn.close()
    except Exception:
        pass

    survived_restart = None
    if oldest_event_at:
        try:
            oldest_dt = datetime.fromisoformat(oldest_event_at)
            survived_restart = oldest_dt < _PROCESS_STARTED_AT
        except ValueError:
            pass

    return {
        "db_path": str(DB_PATH),
        "mount_detected": mount_detected,
        "process_started_at": _PROCESS_STARTED_AT.isoformat(),
        "oldest_event_at": oldest_event_at,
        "survived_restart": survived_restart,
    }


def get_summary(range_key: str = "7d") -> Dict[str, Any]:
    init_db()
    since = _since_iso(range_key)
    with _lock:
        conn = _connect()
        try:
            where = "WHERE ts >= ?" if since else ""
            params = (since,) if since else ()

            total_row = conn.execute(
                f"SELECT COUNT(*) AS n, SUM(cost_usd) AS total, "
                f"SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS errors, "
                f"SUM(CASE WHEN cost_usd IS NULL AND success=1 THEN 1 ELSE 0 END) AS unpriced "
                f"FROM usage_events {where}", params
            ).fetchone()

            by_service = conn.execute(
                f"SELECT service_type, SUM(cost_usd) AS cost, COUNT(*) AS n FROM usage_events "
                f"{where} GROUP BY service_type ORDER BY cost DESC", params
            ).fetchall()

            by_provider = conn.execute(
                f"SELECT provider, SUM(cost_usd) AS cost, COUNT(*) AS n FROM usage_events "
                f"{where} GROUP BY provider ORDER BY cost DESC", params
            ).fetchall()

            session_count = conn.execute(
                f"SELECT COUNT(DISTINCT session_id) AS n FROM usage_events {where}", params
            ).fetchone()["n"]

            today_since = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
            today_total = conn.execute(
                "SELECT SUM(cost_usd) AS total FROM usage_events WHERE ts >= ?", (today_since,)
            ).fetchone()["total"] or 0.0

            # Trailing daily average over the selected range, for a simple
            # projected-monthly-spend figure.
            range_days = {"24h": 1, "7d": 7, "30d": 30}.get(range_key)
            total_cost = total_row["total"] or 0.0
            projected_monthly = None
            if range_days:
                projected_monthly = round((total_cost / range_days) * 30, 4)

            return {
                "range": range_key,
                "total_cost_usd": round(total_cost, 6),
                "spend_today_usd": round(today_total, 6),
                "projected_monthly_usd": projected_monthly,
                "event_count": total_row["n"] or 0,
                "error_count": total_row["errors"] or 0,
                "error_rate": round((total_row["errors"] or 0) / total_row["n"], 4) if total_row["n"] else 0.0,
                "unpriced_event_count": total_row["unpriced"] or 0,
                "session_count": session_count or 0,
                "avg_cost_per_session_usd": round(total_cost / session_count, 6) if session_count else 0.0,
                "cost_by_service": [
                    {"service_type": r["service_type"], "cost_usd": round(r["cost"] or 0.0, 6), "event_count": r["n"]}
                    for r in by_service
                ],
                "cost_by_provider": [
                    {"provider": r["provider"], "cost_usd": round(r["cost"] or 0.0, 6), "event_count": r["n"]}
                    for r in by_provider
                ],
            }
        finally:
            conn.close()


def get_timeseries(range_key: str = "7d", granularity: str = "day") -> Dict[str, Any]:
    init_db()
    since = _since_iso(range_key)
    fmt = "%Y-%m-%dT%H:00:00" if granularity == "hour" else "%Y-%m-%d"
    bucket_expr = "strftime('%Y-%m-%dT%H:00:00', ts)" if granularity == "hour" else "strftime('%Y-%m-%d', ts)"
    with _lock:
        conn = _connect()
        try:
            where = "WHERE ts >= ?" if since else ""
            params = (since,) if since else ()
            rows = conn.execute(
                f"SELECT {bucket_expr} AS bucket, service_type, SUM(cost_usd) AS cost, COUNT(*) AS n "
                f"FROM usage_events {where} GROUP BY bucket, service_type ORDER BY bucket ASC", params
            ).fetchall()

            buckets: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                b = buckets.setdefault(r["bucket"], {"bucket": r["bucket"], "total_cost_usd": 0.0, "by_service": {}})
                cost = r["cost"] or 0.0
                b["by_service"][r["service_type"]] = round(cost, 6)
                b["total_cost_usd"] = round(b["total_cost_usd"] + cost, 6)

            return {"range": range_key, "granularity": granularity, "buckets": list(buckets.values())}
        finally:
            conn.close()


def get_sessions(sort: str = "cost_desc", limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    init_db()
    order = {
        "cost_desc": "total_cost_usd DESC",
        "cost_asc": "total_cost_usd ASC",
        "recent": "last_event_ts DESC",
        "events_desc": "event_count DESC",
    }.get(sort, "total_cost_usd DESC")

    with _lock:
        conn = _connect()
        try:
            total = conn.execute("SELECT COUNT(*) AS n FROM session_cost_rollup").fetchone()["n"]
            rows = conn.execute(
                f"SELECT * FROM session_cost_rollup ORDER BY {order} LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
            sessions = []
            for r in rows:
                sessions.append({
                    "session_id": r["session_id"],
                    "total_cost_usd": round(r["total_cost_usd"] or 0.0, 6),
                    "unpriced_event_count": r["unpriced_event_count"],
                    "cost_by_service": json.loads(r["cost_by_service_json"]) if r["cost_by_service_json"] else {},
                    "cost_by_provider": json.loads(r["cost_by_provider_json"]) if r["cost_by_provider_json"] else {},
                    "event_count": r["event_count"],
                    "error_count": r["error_count"],
                    "first_event_ts": r["first_event_ts"],
                    "last_event_ts": r["last_event_ts"],
                })
            return {"total": total, "sessions": sessions}
        finally:
            conn.close()


def receipts(wallet: Optional[str] = None, *, days: int = 30, limit: int = 6) -> list:
    """Receipts for ACCOUNT (BILLING_LIVE_PLAN B2 / B6): one per run per day,
    newest first, each split by what it used — story, pictures, motion,
    voice, live video — with the count, what it cost us, what the player was
    charged, and the calls that failed (never charged).

    ``wallet``: a hosted player's wallet id — only the rows charged to it.
    None: every row (the desktop app, whose ledger is its one player's).
    """
    from datetime import timedelta
    try:
        init_db()
        since = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
        where = "ts >= ?"
        args: list = [since]
        if wallet:
            where += " AND wallet = ?"
            args.append(wallet)
        with _lock:
            conn = _connect()
            try:
                runs = conn.execute(
                    f"SELECT session_id, substr(ts, 1, 10) AS day, MAX(ts) AS last "
                    f"FROM usage_events WHERE {where} GROUP BY session_id, day "
                    f"ORDER BY last DESC LIMIT ?", (*args, int(limit)),
                ).fetchall()
                out = []
                for r in runs:
                    parts = conn.execute(
                        f"SELECT service_type, "
                        f"SUM(CASE WHEN success = 1 THEN COALESCE(output_units, 1) ELSE 0 END) AS units, "
                        f"SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS calls, "
                        f"SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failed, "
                        f"MAX(unit_type) AS unit_type, "
                        f"SUM(COALESCE(cost_usd, 0)) AS cost, SUM(COALESCE(charged_usd, 0)) AS charged "
                        f"FROM usage_events WHERE {where} AND session_id = ? AND substr(ts, 1, 10) = ? "
                        f"GROUP BY service_type ORDER BY charged DESC, cost DESC",
                        (*args, r["session_id"], r["day"]),
                    ).fetchall()
                    items = []
                    for p in parts:
                        unit = p["unit_type"] or ""
                        count = p["units"] if unit in ("images", "seconds") else p["calls"]
                        items.append({
                            "service": p["service_type"],
                            "count": round(float(count or 0), 1),
                            "unit": "seconds" if unit == "seconds" else ("pictures" if unit == "images" else "calls"),
                            "failed": int(p["failed"] or 0),
                            "cost_usd": round(float(p["cost"] or 0), 4),
                            "charged_usd": round(float(p["charged"] or 0), 4),
                        })
                    out.append({
                        "when": r["last"],
                        "kind": "world" if str(r["session_id"] or "").startswith("wf-") else "play",
                        "cost_usd": round(sum(i["cost_usd"] for i in items), 4),
                        "charged_usd": round(sum(i["charged_usd"] for i in items), 4),
                        "parts": items,
                    })
                return out
            finally:
                conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"[COST TRACKER] receipts failed (non-fatal): {e}", flush=True)
        return []


def session_cost_usd(session_id: str) -> float:
    """Running provider-cost total for one session. 0 if unknown."""
    try:
        detail = get_session_detail(session_id, limit=1)
        rollup = detail.get("rollup") or {}
        return float(rollup.get("total_cost_usd") or 0.0)
    except Exception:
        return 0.0


def get_session_detail(session_id: str, limit: int = 500) -> Dict[str, Any]:
    init_db()
    with _lock:
        conn = _connect()
        try:
            rollup = conn.execute(
                "SELECT * FROM session_cost_rollup WHERE session_id = ?", (session_id,)
            ).fetchone()
            events = conn.execute(
                "SELECT * FROM usage_events WHERE session_id = ? ORDER BY ts DESC LIMIT ?",
                (session_id, limit)
            ).fetchall()
            return {
                "session_id": session_id,
                "rollup": {
                    "total_cost_usd": round(rollup["total_cost_usd"] or 0.0, 6) if rollup else 0.0,
                    "cost_by_service": json.loads(rollup["cost_by_service_json"]) if rollup and rollup["cost_by_service_json"] else {},
                    "cost_by_provider": json.loads(rollup["cost_by_provider_json"]) if rollup and rollup["cost_by_provider_json"] else {},
                    "event_count": rollup["event_count"] if rollup else 0,
                    "error_count": rollup["error_count"] if rollup else 0,
                    "unpriced_event_count": rollup["unpriced_event_count"] if rollup else 0,
                } if rollup else None,
                "events": [dict(r) for r in events],
            }
        finally:
            conn.close()


def get_providers_breakdown(range_key: str = "30d") -> Dict[str, Any]:
    init_db()
    since = _since_iso(range_key)
    with _lock:
        conn = _connect()
        try:
            where = "WHERE ts >= ?" if since else ""
            params = (since,) if since else ()
            rows = conn.execute(
                f"SELECT provider, model, service_type, SUM(cost_usd) AS cost, "
                f"SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS unpriced, "
                f"AVG(latency_ms) AS avg_latency_ms, COUNT(*) AS n "
                f"FROM usage_events {where} GROUP BY provider, model ORDER BY cost DESC", params
            ).fetchall()
            return {"range": range_key, "providers": [dict(r) for r in rows]}
        finally:
            conn.close()


def get_errors(range_key: str = "7d", limit: int = 100) -> Dict[str, Any]:
    init_db()
    since = _since_iso(range_key)
    with _lock:
        conn = _connect()
        try:
            where = "WHERE success = 0"
            params: tuple = ()
            if since:
                where += " AND ts >= ?"
                params = (since,)
            rows = conn.execute(
                f"SELECT * FROM usage_events {where} ORDER BY ts DESC LIMIT ?", params + (limit,)
            ).fetchall()
            return {"range": range_key, "errors": [dict(r) for r in rows]}
        finally:
            conn.close()


def iter_events_for_export(range_key: str = "30d") -> Iterable[sqlite3.Row]:
    init_db()
    since = _since_iso(range_key)
    conn = _connect()
    where = "WHERE ts >= ?" if since else ""
    params = (since,) if since else ()
    cur = conn.execute(f"SELECT * FROM usage_events {where} ORDER BY ts ASC", params)
    for row in cur:
        yield row
    conn.close()
