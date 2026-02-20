"""Chart pattern catalog with reliability stats and SL/TP rules.

Reliability percentages from Bulkowski's "Encyclopedia of Chart Patterns"
and cross-referenced with crypto-specific backtests.  Crypto is noisier
than equities, so we apply a 0.85x discount to Bulkowski's numbers.

The DEEP_STOP_MULT controls how far past the textbook stop we go to
survive market-maker stop hunts.  For low-liquidity tokens this is
even wider.
"""

from __future__ import annotations

from core.patterns.models import PatternType

DEEP_STOP_MULT = 1.5
DEEP_STOP_MULT_LOW_LIQ = 2.0


class PatternSpec:
    """Specification for a single chart pattern type."""

    __slots__ = (
        "description",
        "direction",
        "pattern_type",
        "reliability",
        "sl_rule",
        "stop_hunt_prob",
        "tp_rule",
    )

    def __init__(
        self,
        pattern_type: PatternType,
        direction: str,
        reliability: float,
        sl_rule: str,
        tp_rule: str,
        stop_hunt_prob: float = 0.4,
        description: str = "",
    ):
        self.pattern_type = pattern_type
        self.direction = direction
        self.reliability = reliability
        self.sl_rule = sl_rule
        self.tp_rule = tp_rule
        self.stop_hunt_prob = stop_hunt_prob
        self.description = description


PATTERN_CATALOG: dict[PatternType, PatternSpec] = {
    PatternType.DOUBLE_BOTTOM: PatternSpec(
        pattern_type=PatternType.DOUBLE_BOTTOM,
        direction="long",
        reliability=0.66,
        sl_rule="below_valley",
        tp_rule="height_from_neckline",
        stop_hunt_prob=0.55,
        description="W-shape: two lows near same price, breakout above neckline",
    ),
    PatternType.DOUBLE_TOP: PatternSpec(
        pattern_type=PatternType.DOUBLE_TOP,
        direction="short",
        reliability=0.65,
        sl_rule="above_peak",
        tp_rule="height_from_neckline",
        stop_hunt_prob=0.50,
        description="M-shape: two highs near same price, breakdown below neckline",
    ),
    PatternType.ASCENDING_TRIANGLE: PatternSpec(
        pattern_type=PatternType.ASCENDING_TRIANGLE,
        direction="long",
        reliability=0.61,
        sl_rule="below_last_higher_low",
        tp_rule="triangle_height",
        stop_hunt_prob=0.45,
        description="Flat resistance + rising support, breakout up",
    ),
    PatternType.DESCENDING_TRIANGLE: PatternSpec(
        pattern_type=PatternType.DESCENDING_TRIANGLE,
        direction="short",
        reliability=0.60,
        sl_rule="above_last_lower_high",
        tp_rule="triangle_height",
        stop_hunt_prob=0.45,
        description="Flat support + falling resistance, breakdown down",
    ),
    PatternType.BULL_FLAG: PatternSpec(
        pattern_type=PatternType.BULL_FLAG,
        direction="long",
        reliability=0.57,
        sl_rule="below_flag_low",
        tp_rule="flagpole_height",
        stop_hunt_prob=0.35,
        description="Sharp rally, shallow pullback, continuation up",
    ),
    PatternType.BEAR_FLAG: PatternSpec(
        pattern_type=PatternType.BEAR_FLAG,
        direction="short",
        reliability=0.55,
        sl_rule="above_flag_high",
        tp_rule="flagpole_height",
        stop_hunt_prob=0.35,
        description="Sharp drop, shallow bounce, continuation down",
    ),
    PatternType.HEAD_SHOULDERS: PatternSpec(
        pattern_type=PatternType.HEAD_SHOULDERS,
        direction="short",
        reliability=0.70,
        sl_rule="above_right_shoulder",
        tp_rule="head_to_neckline",
        stop_hunt_prob=0.50,
        description="Left shoulder, head (higher), right shoulder, break neckline",
    ),
    PatternType.INV_HEAD_SHOULDERS: PatternSpec(
        pattern_type=PatternType.INV_HEAD_SHOULDERS,
        direction="long",
        reliability=0.70,
        sl_rule="below_right_shoulder",
        tp_rule="head_to_neckline",
        stop_hunt_prob=0.50,
        description="Inverse H&S: head (lower), shoulders, break neckline up",
    ),
    PatternType.HIGHER_LOW: PatternSpec(
        pattern_type=PatternType.HIGHER_LOW,
        direction="long",
        reliability=0.52,
        sl_rule="below_higher_low",
        tp_rule="swing_projection",
        stop_hunt_prob=0.60,
        description="Simple: latest swing low is higher than previous → uptrend structure",
    ),
    PatternType.LOWER_HIGH: PatternSpec(
        pattern_type=PatternType.LOWER_HIGH,
        direction="short",
        reliability=0.50,
        sl_rule="above_lower_high",
        tp_rule="swing_projection",
        stop_hunt_prob=0.60,
        description="Simple: latest swing high is lower than previous → downtrend structure",
    ),
    PatternType.RANGE_BOUND: PatternSpec(
        pattern_type=PatternType.RANGE_BOUND,
        direction="long",
        reliability=0.45,
        sl_rule="below_range_low",
        tp_rule="range_high",
        stop_hunt_prob=0.70,
        description="Price oscillating between S/R — high stop hunt probability",
    ),
}


def get_spec(pattern_type: PatternType) -> PatternSpec:
    return PATTERN_CATALOG[pattern_type]


def deep_stop_distance(
    textbook_stop: float,
    entry: float,
    low_liquidity: bool = False,
) -> float:
    """Calculate the deep SL that goes past the stop hunt zone.

    Returns the deep stop price.  For longs: deeper = lower.
    For shorts: deeper = higher.
    """
    mult = DEEP_STOP_MULT_LOW_LIQ if low_liquidity else DEEP_STOP_MULT
    distance = abs(entry - textbook_stop)
    if textbook_stop < entry:
        return entry - distance * mult
    return entry + distance * mult
