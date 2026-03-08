from __future__ import annotations

from core.errors.error_mapper import map_exchange_error


def test_map_exchange_error_detects_min_notional() -> None:
    decision = map_exchange_error(
        Exception('binance {"code":-4164,"msg":"Order\'s notional must be no smaller than 5 (unless reduce only)."}')
    )
    assert decision.code == "min_notional"
    assert decision.retryable is False
    assert decision.cooldown_seconds >= 1800


def test_map_exchange_error_detects_rate_limit() -> None:
    decision = map_exchange_error(Exception("429 Too many requests - rate limit"))
    assert decision.code == "rate_limited"
    assert decision.retryable is True
