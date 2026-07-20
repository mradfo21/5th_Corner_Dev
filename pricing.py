"""
Pricing - Centralized, live-editable provider rate table for cost estimation.

Mirrors `ai_provider_manager.py`'s pattern (JSON file + 5s cache + thread
lock) so it's hot-reloadable and editable from the admin dashboard without a
redeploy, exactly like `ai_config.json` / the AI Models switcher.

Rates are looked up by "{provider}:{model}", falling back to
"{provider}:default" when the exact model isn't in the table. A missing rate
(or a `per_unit`/`input_per_1k`/`output_per_1k` of `null`) means "unpriced" —
`estimate_cost()` returns `None` rather than silently reporting $0, so gaps
are visible in the dashboard instead of hidden.
"""

import json
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, Optional

ROOT = Path(__file__).parent.resolve()
PRICING_PATH = ROOT / "pricing.json"
PRICING_LOCK = threading.Lock()

_cached_pricing: Optional[Dict[str, Any]] = None
_cache_timestamp = 0.0

_DEFAULT_PRICING: Dict[str, Any] = {
    "last_updated": None,
    "notes": "Auto-created default pricing table. Edit via the admin dashboard's Analytics > Pricing panel.",
    "rates": {}
}


def load_pricing(force: bool = False) -> Dict[str, Any]:
    """Load pricing.json with a 5s cache (hot-reloadable, matches ai_provider_manager)."""
    global _cached_pricing, _cache_timestamp

    now = datetime.now(timezone.utc).timestamp()
    if not force and _cached_pricing and (now - _cache_timestamp) < 5:
        return _cached_pricing

    with PRICING_LOCK:
        try:
            with PRICING_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            _cached_pricing = data
            _cache_timestamp = now
            return data
        except FileNotFoundError:
            print("[PRICING] pricing.json not found, creating default...", flush=True)
            try:
                save_pricing(_DEFAULT_PRICING)
            except Exception as e:
                print(f"[PRICING WARN] Could not save default pricing: {e}", flush=True)
            _cached_pricing = _DEFAULT_PRICING
            _cache_timestamp = now
            return _DEFAULT_PRICING
        except Exception as e:
            print(f"[PRICING ERROR] Failed to load pricing.json: {e}", flush=True)
            return _cached_pricing or _DEFAULT_PRICING


def save_pricing(data: Dict[str, Any]) -> None:
    """Persist pricing.json and refresh the in-memory cache immediately."""
    global _cached_pricing, _cache_timestamp

    data = dict(data)
    data["last_updated"] = datetime.now(timezone.utc).isoformat()

    with PRICING_LOCK:
        with PRICING_PATH.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        _cached_pricing = data
        _cache_timestamp = datetime.now(timezone.utc).timestamp()


def get_rate(provider: str, model: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Look up the rate row for provider:model, falling back to provider:default."""
    rates = load_pricing().get("rates", {})
    key = f"{provider}:{model}" if model else None
    if key and key in rates:
        return rates[key]
    fallback = f"{provider}:default"
    return rates.get(fallback)


def set_rate(provider: str, model: str, rate: Dict[str, Any]) -> Dict[str, Any]:
    """Update (or add) a single provider:model rate row and persist it."""
    data = load_pricing(force=True)
    rates = dict(data.get("rates", {}))
    rates[f"{provider}:{model}"] = rate
    data = dict(data)
    data["rates"] = rates
    save_pricing(data)
    return data


def estimate_cost(provider: str, model: Optional[str], unit_type: Optional[str],
                   input_units: Optional[float] = None,
                   output_units: Optional[float] = None) -> Optional[float]:
    """
    Estimate the $ cost of a single call. Returns None when no rate is
    configured (or the configured rate is explicitly null) so "unpriced"
    calls are visible in the dashboard rather than silently reported as $0.
    """
    rate = get_rate(provider, model)
    if not rate:
        return None

    rate_unit_type = rate.get("unit_type", unit_type)

    if rate_unit_type == "tokens":
        input_per_1k = rate.get("input_per_1k")
        output_per_1k = rate.get("output_per_1k")
        if input_per_1k is None and output_per_1k is None:
            return None
        cost = 0.0
        if input_units:
            cost += (input_units / 1000.0) * (input_per_1k or 0.0)
        if output_units:
            cost += (output_units / 1000.0) * (output_per_1k or 0.0)
        return round(cost, 8)

    if rate_unit_type == "characters":
        per_1k = rate.get("per_1k")
        if per_1k is None:
            return None
        units = input_units if input_units is not None else output_units
        return round(((units or 0.0) / 1000.0) * per_1k, 8)

    # images / seconds / calls / minutes — all flat "per unit" rates.
    per_unit = rate.get("per_unit")
    if per_unit is None:
        return None
    units = output_units if output_units is not None else input_units
    return round((units if units is not None else 1.0) * per_unit, 8)


def list_rates() -> Dict[str, Any]:
    """All configured provider:model rates, for the admin pricing panel."""
    return load_pricing().get("rates", {})
