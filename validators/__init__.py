"""Lightweight per-bot-type validators for queue proposal spot-checks.

Each validator does a quick, single-symbol check to confirm that a trade
proposal from the hub queue is still valid before the bot executes it.
"""

from __future__ import annotations

from validators.base import ValidationResult, Validator
from validators.extreme import ExtremeValidator
from validators.indicators import IndicatorsValidator
from validators.meanrev import MeanRevValidator
from validators.momentum import MomentumValidator
from validators.swing import SwingValidator

VALIDATORS_BY_STYLE: dict[str, type[Validator]] = {
    "extreme": ExtremeValidator,
    "momentum": MomentumValidator,
    "indicators": IndicatorsValidator,
    "meanrev": MeanRevValidator,
    "swing": SwingValidator,
}


def get_validator(bot_style: str) -> Validator:
    """Return the appropriate validator for a bot style, defaulting to momentum."""
    cls = VALIDATORS_BY_STYLE.get(bot_style, MomentumValidator)
    return cls()


__all__ = [
    "VALIDATORS_BY_STYLE",
    "ExtremeValidator",
    "IndicatorsValidator",
    "MeanRevValidator",
    "MomentumValidator",
    "SwingValidator",
    "ValidationResult",
    "Validator",
    "get_validator",
]
