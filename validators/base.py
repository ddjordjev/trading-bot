from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from core.models import Candle, Ticker


@dataclass
class ValidationResult:
    valid: bool
    reason: str
    confidence: float = 1.0


class Validator(ABC):
    """Base class for per-bot-type proposal validators.

    Given a short candle window and current ticker for ONE symbol,
    decide whether a hub-generated trade proposal is still actionable.
    """

    def __init__(self, *, paper_mode: bool = False) -> None:
        self.paper_mode = paper_mode

    @abstractmethod
    def validate(
        self,
        candles: list[Candle],
        ticker: Ticker | None,
        side: str,
        strategy: str,
    ) -> ValidationResult:
        """Return whether the proposal is still valid right now."""
        ...
