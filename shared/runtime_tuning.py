from __future__ import annotations

import hashlib
import json
from typing import Any

RUNTIME_TUNABLE_CASTERS: dict[str, type] = {
    "max_position_size_pct": float,
    "max_daily_loss_pct": float,
    "stop_loss_pct": float,
    "take_profit_pct": float,
    "max_concurrent_positions": int,
    "min_signal_strength": float,
    "risk_env_multiplier": float,
    "max_total_exposure_mult": float,
    "min_tradeable_equity_usdt": float,
    "default_leverage": int,
    "initial_risk_amount": float,
    "dca_partial_take_pct": float,
    "max_concurrent_limit_orders_on_cex": int,
}


def normalize_runtime_tuning(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only supported keys and cast to expected runtime types."""
    out: dict[str, Any] = {}
    for key, caster in RUNTIME_TUNABLE_CASTERS.items():
        if key not in raw:
            continue
        val = raw[key]
        if val is None:
            continue
        try:
            out[key] = caster(val)
        except Exception:
            continue
    return out


def runtime_tuning_revision(values: dict[str, Any]) -> str:
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
