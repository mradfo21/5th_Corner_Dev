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
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import pricing

ROOT = Path(__file__).parent.resolve()
ANALYTICS_DIR = ROOT / "sessions" / "_analytics"
DB_PATH = ANALYTICS_DIR / "usage.db"

_lock = threading.Lock()
_initialized = False

SERVICE_TYPES = ("text", "image", "video", "voice", "realtime")


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
                conn.commit()
            finally:
                conn.close()
            _initialized = True
        except Exception as e:
            print(f"[COST TRACKER] init_db failed (non-fatal, tracking disabled): {e}", flush=True)


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
        init_db()
        cost_usd = None
        if success:
            cost_usd = pricing.estimate_cost(provider, model, unit_type, input_units, output_units)
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
                         error_message, discord_guild_id, discord_channel_id, meta_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (ts, session_id, turn_count, service_type, provider, model, operation,
                     input_units, output_units, unit_type, cost_usd, latency_ms, 1 if success else 0,
                     error_text, discord_guild_id, discord_channel_id,
                     json.dumps(meta) if meta else None),
                )
                _upsert_rollup(conn, session_id, service_type, provider, cost_usd, success, ts)
                conn.commit()
            finally:
                conn.close()
        return cost_usd
    except Exception as e:
        print(f"[COST TRACKER] record_usage failed (non-fatal): {e}", flush=True)
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
