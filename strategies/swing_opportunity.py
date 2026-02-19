from __future__ import annotations

from typing import Any

import ta

from core.models import Candle, Signal, SignalAction, Ticker
from strategies.base import BaseStrategy


class SwingOpportunityStrategy(BaseStrategy):
    """Extremely selective swing trade detector. Only fires on rare events.

    This is NOT a regular strategy. It sits silent 99% of the time and only
    triggers when it detects a potential "opportunity of a lifetime":

    - Massive daily drop (15-20%+) hitting major support
    - Weekly RSI at historically oversold levels
    - Volume explosion confirming capitulation
    - Price at or below a long-term moving average (200 MA on hourly)

    These are meant to be held longer (hours to days) with wider stops,
    because the potential upside justifies it. Still has a max hold time
    to avoid turning into a bag holder.
    """

    @property
    def name(self) -> str:
        return "swing_opportunity"

    def __init__(self, symbol: str, market_type: str = "futures", leverage: int = 10, **params: Any):
        super().__init__(symbol, market_type, leverage, **params)

        # How extreme does the drop need to be?
        self.crash_threshold_pct = float(params.get("crash_threshold_pct", 15.0))
        self.extreme_crash_pct = float(params.get("extreme_crash_pct", 25.0))

        # RSI thresholds for "historically oversold"
        self.rsi_period = int(params.get("rsi_period", 14))
        self.rsi_extreme_oversold = float(params.get("rsi_extreme_oversold", 20))
        self.rsi_extreme_overbought = float(params.get("rsi_extreme_overbought", 85))

        # Volume must be significantly above average
        self.capitulation_volume_mult = float(params.get("capitulation_volume_mult", 3.0))

        # Support detection (200-period MA)
        self.ma_period = int(params.get("ma_period", 200))

        # Wider stops and longer holds for swing trades
        self.swing_stop_pct = float(params.get("swing_stop_pct", 5.0))
        self.swing_max_hold_hours = int(params.get("swing_max_hold_hours", 48))

        self._last_signal_price: float = 0
        self._cooldown_candles: int = 0

    def analyze(self, candles: list[Candle], ticker: Ticker | None = None) -> Signal | None:
        df = self.candles_to_df(candles)
        if len(df) < self.ma_period:
            return None

        if self._cooldown_candles > 0:
            self._cooldown_candles -= 1
            return None

        price = df["close"].iloc[-1]

        crash_buy = self._detect_crash_buy(df, price)
        if crash_buy:
            self._cooldown_candles = 60  # don't fire again for an hour
            return crash_buy

        blow_off_short = self._detect_blow_off_top(df, price)
        if blow_off_short:
            self._cooldown_candles = 60
            return blow_off_short

        return None

    def _detect_crash_buy(self, df: object, price: float) -> Signal | None:
        """Detect a major crash where buying the dip is justified."""
        import pandas as pd

        assert isinstance(df, pd.DataFrame)

        # How much has price dropped from recent high?
        recent_high = df["high"].iloc[-60:].max()  # last 60 candles
        if recent_high == 0:
            return None
        drop_pct = (price - recent_high) / recent_high * 100

        if drop_pct > -self.crash_threshold_pct:
            return None  # not a big enough drop

        # Check RSI is at extreme oversold
        rsi = ta.momentum.RSIIndicator(df["close"], window=self.rsi_period).rsi()
        current_rsi = rsi.iloc[-1]
        if current_rsi > self.rsi_extreme_oversold:
            return None

        # Check volume spike (capitulation volume)
        avg_vol = df["volume"].iloc[-60:].mean()
        recent_vol = df["volume"].iloc[-3:].mean()
        vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 0
        if vol_ratio < self.capitulation_volume_mult:
            return None

        # Check if price is near or below the long-term MA (support)
        ma = df["close"].rolling(min(self.ma_period, len(df))).mean().iloc[-1]
        near_support = price <= ma * 1.02  # within 2% of MA

        # Determine signal strength based on severity
        severity = abs(drop_pct) / self.extreme_crash_pct
        strength = min(1.0, severity)

        is_extreme = abs(drop_pct) >= self.extreme_crash_pct
        label = "EXTREME CRASH" if is_extreme else "MAJOR CRASH"

        stop_loss = price * (1 - self.swing_stop_pct / 100)

        return Signal(
            symbol=self.symbol,
            action=SignalAction.BUY,
            strength=strength,
            strategy=self.name,
            reason=(
                f"SWING {label}: {drop_pct:.1f}% from high | "
                f"RSI={current_rsi:.0f} | vol={vol_ratio:.1f}x | "
                f"{'at support' if near_support else 'approaching support'}"
            ),
            suggested_price=price,
            suggested_stop_loss=stop_loss,
            market_type=self.market_type,
            leverage=max(3, self.leverage // 2),  # lower leverage for swings
            quick_trade=False,
            max_hold_minutes=self.swing_max_hold_hours * 60,
        )

    def _detect_blow_off_top(self, df: object, price: float) -> Signal | None:
        """Detect a blow-off top / parabolic spike to short."""
        import pandas as pd

        assert isinstance(df, pd.DataFrame)

        recent_low = df["low"].iloc[-60:].min()
        if recent_low == 0:
            return None
        rally_pct = (price - recent_low) / recent_low * 100

        if rally_pct < self.crash_threshold_pct:
            return None

        rsi = ta.momentum.RSIIndicator(df["close"], window=self.rsi_period).rsi()
        current_rsi = rsi.iloc[-1]
        if current_rsi < self.rsi_extreme_overbought:
            return None

        avg_vol = df["volume"].iloc[-60:].mean()
        recent_vol = df["volume"].iloc[-3:].mean()
        vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 0
        if vol_ratio < self.capitulation_volume_mult:
            return None

        # Price well above MA = overextended
        ma = df["close"].rolling(min(self.ma_period, len(df))).mean().iloc[-1]
        above_ma_pct = (price - ma) / ma * 100 if ma > 0 else 0
        if above_ma_pct < 10:
            return None

        severity = rally_pct / self.extreme_crash_pct
        strength = min(1.0, severity)

        stop_loss = price * (1 + self.swing_stop_pct / 100)

        return Signal(
            symbol=self.symbol,
            action=SignalAction.SELL,
            strength=strength,
            strategy=self.name,
            reason=(
                f"SWING BLOW-OFF TOP: +{rally_pct:.1f}% from low | "
                f"RSI={current_rsi:.0f} | vol={vol_ratio:.1f}x | "
                f"{above_ma_pct:.0f}% above MA"
            ),
            suggested_price=price,
            suggested_stop_loss=stop_loss,
            market_type=self.market_type,
            leverage=max(3, self.leverage // 2),
            quick_trade=False,
            max_hold_minutes=self.swing_max_hold_hours * 60,
        )
