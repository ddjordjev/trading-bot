"""Extreme mover validator — confirms the big move is still active."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from core.models import Candle, Ticker
from validators.base import ValidationResult, Validator


def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


class ExtremeValidator(Validator):
    MIN_CANDLES = 10
    VOLUME_SURGE_MULT = 1.5
    MOMENTUM_THRESHOLD_PCT = 0.5

    def validate(
        self,
        candles: list[Candle],
        ticker: Ticker | None,
        side: str,
        strategy: str,
    ) -> ValidationResult:
        if len(candles) < self.MIN_CANDLES:
            return ValidationResult(valid=False, reason="insufficient candles")

        closes = pd.Series([c.close for c in candles])
        volumes = pd.Series([c.volume for c in candles])

        if not self.paper_mode:
            recent_vol = volumes.iloc[-5:].mean()
            older_vol = volumes.iloc[-20:-5].mean() if len(volumes) >= 20 else volumes.mean()
            if older_vol > 0 and recent_vol / older_vol < self.VOLUME_SURGE_MULT:
                return ValidationResult(
                    valid=False,
                    reason=f"volume surge fading ({recent_vol / older_vol:.1f}x, need {self.VOLUME_SURGE_MULT}x)",
                )

        price_now = closes.iloc[-1]
        price_recent = closes.iloc[-5]
        if not _finite(price_now) or not _finite(price_recent):
            return ValidationResult(valid=False, reason="invalid or missing price")
        if price_recent == 0:
            return ValidationResult(valid=False, reason="zero price")

        move_pct = (price_now - price_recent) / price_recent * 100
        if side == "long" and move_pct < self.MOMENTUM_THRESHOLD_PCT:
            return ValidationResult(valid=False, reason=f"long momentum stalled ({move_pct:+.2f}%)")
        if side == "short" and move_pct > -self.MOMENTUM_THRESHOLD_PCT:
            return ValidationResult(valid=False, reason=f"short momentum stalled ({move_pct:+.2f}%)")

        if ticker and ticker.spread_pct > 0.3:
            return ValidationResult(
                valid=False,
                reason=f"spread too wide ({ticker.spread_pct:.2f}%)",
                confidence=0.5,
            )

        return ValidationResult(valid=True, reason="extreme move still active", confidence=0.9)
