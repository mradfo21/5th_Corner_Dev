"""Optional monthly spend cap — Cursor-style usage, not arcade credits.

The live cost ledger is ``cost_tracker``. This module only stores whether
the player asked to stop generating after a dollar amount this month.
Unset (the default) means unlimited, same as Cursor with no spend limit.
"""

from __future__ import annotations

import json
import threading
from os import PathLike
from pathlib import Path
from typing import Any, Dict, Optional, Union

ROOT = Path(__file__).parent.resolve()
import paths as _paths  # where the game writes (M2): the repo from source, %APPDATA%/ABYSS built
LIMITS_PATH = _paths.data_root() / "sessions" / "_analytics" / "usage_limits.json"

_lock = threading.Lock()
_limits_path: Optional[Path] = None


def set_limits_path(path: Optional[Union[PathLike, str]]) -> None:
    """Tests point the store at a temp file so they never touch sessions/."""
    global _limits_path
    _limits_path = Path(path) if path else None


def _path() -> Path:
    return _limits_path or LIMITS_PATH


def _read() -> Dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(payload: Dict[str, Any]) -> None:
    dest = _path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(dest)


def monthly_cap_usd() -> Optional[float]:
    raw = _read().get("monthly_cap_usd")
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return round(n, 2)


def set_monthly_cap_usd(value: Any) -> Optional[float]:
    """``None`` / 0 / empty clears the cap. Positive dollars set it."""
    cap: Optional[float] = None
    if value is not None and str(value).strip() != "":
        try:
            n = float(value)
        except (TypeError, ValueError):
            raise ValueError("Monthly cap must be a number.")
        if n < 0:
            raise ValueError("Monthly cap cannot be negative.")
        if n > 0:
            cap = round(n, 2)
    with _lock:
        _write({"monthly_cap_usd": cap})
    return cap


def month_spend_usd() -> float:
    try:
        import cost_tracker
        return float(cost_tracker.get_summary("30d").get("total_cost_usd") or 0.0)
    except Exception:
        return 0.0


def over_cap() -> bool:
    cap = monthly_cap_usd()
    if cap is None:
        return False
    return month_spend_usd() >= cap


def public_status() -> Dict[str, Any]:
    """What the ACCOUNT → USAGE pane (and a 402) need to show."""
    try:
        import cost_tracker
        summary = cost_tracker.get_summary("30d")
    except Exception:
        summary = {
            "total_cost_usd": 0.0,
            "spend_today_usd": 0.0,
            "event_count": 0,
            "cost_by_service": [],
            "cost_by_provider": [],
        }
    cap = monthly_cap_usd()
    spent = float(summary.get("total_cost_usd") or 0.0)
    return {
        "range": "30d",
        "spend_usd": round(spent, 6),
        "spend_today_usd": round(float(summary.get("spend_today_usd") or 0.0), 6),
        "event_count": int(summary.get("event_count") or 0),
        "cost_by_service": summary.get("cost_by_service") or [],
        "cost_by_provider": summary.get("cost_by_provider") or [],
        "monthly_cap_usd": cap,
        "remaining_usd": None if cap is None else round(max(0.0, cap - spent), 6),
        "over_cap": bool(cap is not None and spent >= cap),
        "byok": True,
    }
