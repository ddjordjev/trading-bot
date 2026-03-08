from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorDecision:
    code: str
    retryable: bool
    cooldown_seconds: int
    reason: str


def map_exchange_error(error: Exception) -> ErrorDecision:
    """Normalize exchange/runtime exceptions into handling decisions."""
    text = str(error or "")
    low = text.lower()

    if "-4164" in low or "notional must be no smaller than 5" in low:
        return ErrorDecision(
            code="min_notional",
            retryable=False,
            cooldown_seconds=1800,
            reason="Order below exchange min notional.",
        )

    if any(
        token in low
        for token in (
            "insufficient balance",
            "not enough for new order",
            "ab not enough",
            "110007",
            "170131",
        )
    ):
        return ErrorDecision(
            code="insufficient_balance",
            retryable=True,
            cooldown_seconds=120,
            reason="Insufficient balance for requested order.",
        )

    if any(
        token in low
        for token in (
            "invalid api-key",
            "api-key format invalid",
            "invalid api key",
            "permission denied",
            "unauthorized",
            "-2015",
            "-2014",
        )
    ):
        return ErrorDecision(
            code="auth_error",
            retryable=False,
            cooldown_seconds=3600,
            reason="Exchange authentication/permission error.",
        )

    if any(token in low for token in ("rate limit", "too many requests", "429")):
        return ErrorDecision(
            code="rate_limited",
            retryable=True,
            cooldown_seconds=120,
            reason="Rate limited by exchange.",
        )

    return ErrorDecision(
        code="unknown",
        retryable=True,
        cooldown_seconds=60,
        reason="Unclassified exchange/runtime error.",
    )
