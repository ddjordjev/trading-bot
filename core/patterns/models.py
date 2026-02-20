"""Data structures for chart pattern recognition and smart SL/TP placement.

Design philosophy: SLs go DEEPER than textbook to survive market-maker stop
hunts (wicks through obvious levels).  Only after a wick confirms the level
do we tighten to textbook + offset.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class SwingType(str, Enum):
    HIGH = "high"
    LOW = "low"


class PatternType(str, Enum):
    DOUBLE_BOTTOM = "double_bottom"
    DOUBLE_TOP = "double_top"
    ASCENDING_TRIANGLE = "ascending_triangle"
    DESCENDING_TRIANGLE = "descending_triangle"
    BULL_FLAG = "bull_flag"
    BEAR_FLAG = "bear_flag"
    HEAD_SHOULDERS = "head_shoulders"
    INV_HEAD_SHOULDERS = "inv_head_shoulders"
    HIGHER_LOW = "higher_low"
    LOWER_HIGH = "lower_high"
    RANGE_BOUND = "range_bound"


class LevelStrength(str, Enum):
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"
    FORTRESS = "fortress"


class SwingPoint(BaseModel):
    """A local high or low in the price series."""

    index: int
    price: float
    swing_type: SwingType
    volume: float = 0.0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_high(self) -> bool:
        return self.swing_type == SwingType.HIGH

    @property
    def is_low(self) -> bool:
        return self.swing_type == SwingType.LOW


class KeyLevel(BaseModel):
    """A support or resistance zone derived from swing point clusters."""

    price: float
    zone_low: float
    zone_high: float
    strength: LevelStrength = LevelStrength.MODERATE
    touch_count: int = 1
    total_volume: float = 0.0
    is_support: bool = True
    last_touch_index: int = 0

    @property
    def zone_width_pct(self) -> float:
        if self.price == 0:
            return 0.0
        return (self.zone_high - self.zone_low) / self.price * 100

    @property
    def stop_hunt_zone(self) -> float:
        """The price where MMs typically wick to grab stops.

        For support: slightly below the zone.  For resistance: slightly above.
        We place our REAL stop beyond this zone.
        """
        width = self.zone_high - self.zone_low
        if self.is_support:
            return self.zone_low - width * 0.5
        return self.zone_high + width * 0.5


class PatternMatch(BaseModel):
    """A detected chart pattern with confidence and trading parameters."""

    pattern_type: PatternType
    confidence: float = 0.0  # 0..1: how textbook the formation is
    reliability: float = 0.0  # historical win rate from Bulkowski et al.
    direction: str = "long"  # "long" or "short"

    entry_zone: float = 0.0
    textbook_stop: float = 0.0  # where most traders put their SL
    deep_stop: float = 0.0  # our SL: past the stop hunt zone
    target_1: float = 0.0  # conservative TP (measured move * 0.8)
    target_2: float = 0.0  # full measured move TP
    invalidation: float = 0.0  # if price breaks this, pattern is dead

    anchor_levels: list[KeyLevel] = Field(default_factory=list)
    swing_points: list[SwingPoint] = Field(default_factory=list)
    detected_at_index: int = 0

    @property
    def risk_reward(self) -> float:
        """R:R based on deep stop (our actual SL) and target_1."""
        risk = abs(self.entry_zone - self.deep_stop)
        reward = abs(self.target_1 - self.entry_zone)
        if risk == 0:
            return 0.0
        return reward / risk

    @property
    def signal_boost(self) -> float:
        """How much this pattern should boost signal strength.

        High-confidence, high-reliability patterns with good R:R get
        the biggest boost.
        """
        base = self.reliability * self.confidence
        rr_bonus = min(self.risk_reward / 3.0, 0.3)
        return base * 0.3 + rr_bonus


class SmartStops(BaseModel):
    """Final SL/TP recommendations combining structure + patterns.

    Two-tier stop system:
    - initial_stop: deep, past the stop hunt zone (survive the wick)
    - tightened_stop: textbook level + offset (activate after wick confirms)
    """

    initial_stop: float = 0.0
    tightened_stop: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    invalidation: float = 0.0

    nearest_support: KeyLevel | None = None
    nearest_resistance: KeyLevel | None = None
    pattern: PatternMatch | None = None

    fallback_pct: float = 0.0  # %-based stop if no structure found

    @property
    def has_structure(self) -> bool:
        return self.nearest_support is not None or self.nearest_resistance is not None

    @property
    def has_pattern(self) -> bool:
        return self.pattern is not None

    def stop_loss_pct(self, entry: float) -> float:
        """Return the initial SL as a percentage distance from entry."""
        if entry == 0 or self.initial_stop == 0:
            return self.fallback_pct
        return abs(entry - self.initial_stop) / entry * 100
