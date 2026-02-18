from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import ta

from core.models import Candle, Ticker, Signal, SignalAction
from strategies.base import BaseStrategy


class CompoundMomentumStrategy(BaseStrategy):
    """Hit-and-run scalping strategy. Get in, grab profit, get out.

    Default behavior: quick trades with tight time limits.
    Every entry is a quick trade unless it's a massive breakout.

    Entry:
      - Spike detection: rapid move + volume = scalp it
      - Breakout: price breaks consolidation range = quick ride

    All entries are quick_trade=True with auto-close timers.
    Trailing stops lock in profit as it runs.
    """

    @property
    def name(self) -> str:
        return "compound_momentum"

    def __init__(self, symbol: str, market_type: str = "futures", leverage: int = 10, **params: object):
        super().__init__(symbol, market_type, leverage, **params)

        # Breakout detection
        self.consolidation_period = int(params.get("consolidation_period", 20))
        self.breakout_threshold_pct = float(params.get("breakout_threshold_pct", 0.5))

        # Momentum confirmation
        self.rsi_period = int(params.get("rsi_period", 14))
        self.rsi_bull_min = float(params.get("rsi_bull_min", 50))
        self.rsi_bear_max = float(params.get("rsi_bear_max", 50))
        self.volume_surge_mult = float(params.get("volume_surge_mult", 1.8))

        # Spike detection (primary scalp entry)
        self.spike_pct = float(params.get("spike_pct", 1.0))
        self.spike_candles = int(params.get("spike_candles", 3))
        self.spike_max_hold = int(params.get("spike_max_hold", 8))

        # Breakout quick trade timing
        self.breakout_max_hold = int(params.get("breakout_max_hold", 15))

        # Trailing stop config
        self.initial_stop_pct = float(params.get("initial_stop_pct", 1.0))
        self.trail_pct = float(params.get("trail_pct", 0.5))

        # Exit signals
        self.exit_rsi_overbought = float(params.get("exit_rsi_overbought", 75))
        self.exit_rsi_oversold = float(params.get("exit_rsi_oversold", 25))

        self._in_position = False
        self._position_side: Optional[str] = None
        self._entry_time: Optional[datetime] = None

    def analyze(self, candles: list[Candle], ticker: Optional[Ticker] = None) -> Optional[Signal]:
        df = self.candles_to_df(candles)
        if len(df) < max(self.consolidation_period, self.rsi_period) + 5:
            return None

        price = df["close"].iloc[-1]

        if self._in_position:
            exit_signal = self._check_exit(df, price)
            if exit_signal:
                self._in_position = False
                self._position_side = None
                return exit_signal
            return None

        # Priority 1: spike scalps
        spike = self._detect_spike(df, price)
        if spike:
            return spike

        # Priority 2: breakout scalps
        breakout = self._detect_breakout(df, price)
        if breakout:
            return breakout

        return None

    def _detect_spike(self, df: object, price: float) -> Optional[Signal]:
        """Detect rapid price spikes for quick scalp trades."""
        import pandas as pd
        assert isinstance(df, pd.DataFrame)

        if len(df) < self.spike_candles + 1:
            return None

        recent = df["close"].iloc[-(self.spike_candles + 1):]
        start_price = recent.iloc[0]
        if start_price == 0:
            return None

        change_pct = (price - start_price) / start_price * 100

        avg_vol = df["volume"].iloc[-20:].mean()
        recent_vol = df["volume"].iloc[-self.spike_candles:].mean()
        vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 0

        if abs(change_pct) < self.spike_pct:
            return None
        if vol_ratio < 1.5:
            return None

        direction_up = change_pct > 0
        action = SignalAction.BUY if direction_up else SignalAction.SELL
        strength = min(1.0, abs(change_pct) / (self.spike_pct * 2))

        self._in_position = True
        self._position_side = "long" if direction_up else "short"
        self._entry_time = datetime.now(timezone.utc)

        return Signal(
            symbol=self.symbol,
            action=action,
            strength=strength,
            strategy=self.name,
            reason=f"SCALP spike {change_pct:+.1f}% in {self.spike_candles}m (vol={vol_ratio:.1f}x)",
            suggested_price=price,
            market_type=self.market_type,
            leverage=self.leverage,
            quick_trade=True,
            max_hold_minutes=self.spike_max_hold,
        )

    def _detect_breakout(self, df: object, price: float) -> Optional[Signal]:
        """Breakout detection -- still a quick trade, just slightly longer hold."""
        import pandas as pd
        assert isinstance(df, pd.DataFrame)

        highs = df["high"].iloc[-self.consolidation_period:-1]
        lows = df["low"].iloc[-self.consolidation_period:-1]
        resistance = highs.max()
        support = lows.min()

        range_size = (resistance - support) / support * 100 if support > 0 else 0
        if range_size < 0.3 or range_size > 8.0:
            return None

        rsi = ta.momentum.RSIIndicator(df["close"], window=self.rsi_period).rsi()
        current_rsi = rsi.iloc[-1]

        avg_vol = df["volume"].iloc[-20:].mean()
        current_vol = df["volume"].iloc[-1]
        volume_surge = current_vol > avg_vol * self.volume_surge_mult

        # Bullish breakout
        breakout_up = price > resistance * (1 + self.breakout_threshold_pct / 100)
        if breakout_up and current_rsi >= self.rsi_bull_min and volume_surge:
            self._in_position = True
            self._position_side = "long"
            self._entry_time = datetime.now(timezone.utc)
            strength = min(1.0, (price - resistance) / resistance * 100 / self.breakout_threshold_pct)

            return Signal(
                symbol=self.symbol, action=SignalAction.BUY, strength=strength,
                strategy=self.name,
                reason=f"SCALP breakout above {resistance:.2f} (RSI={current_rsi:.0f}, vol={current_vol/avg_vol:.1f}x)",
                suggested_price=price, suggested_stop_loss=support,
                market_type=self.market_type, leverage=self.leverage,
                quick_trade=True, max_hold_minutes=self.breakout_max_hold,
            )

        # Bearish breakout
        breakout_down = price < support * (1 - self.breakout_threshold_pct / 100)
        if breakout_down and current_rsi <= self.rsi_bear_max and volume_surge:
            self._in_position = True
            self._position_side = "short"
            self._entry_time = datetime.now(timezone.utc)
            strength = min(1.0, (support - price) / support * 100 / self.breakout_threshold_pct)

            return Signal(
                symbol=self.symbol, action=SignalAction.SELL, strength=strength,
                strategy=self.name,
                reason=f"SCALP breakout below {support:.2f} (RSI={current_rsi:.0f}, vol={current_vol/avg_vol:.1f}x)",
                suggested_price=price, suggested_stop_loss=resistance,
                market_type=self.market_type, leverage=self.leverage,
                quick_trade=True, max_hold_minutes=self.breakout_max_hold,
            )

        return None

    def _check_exit(self, df: object, price: float) -> Optional[Signal]:
        """Only close LOSING positions when momentum has died.

        RIDE THE WINNERS: if in profit, NEVER close from strategy.
        Let the trailing stop do its job -- it will ratchet up and
        eventually get hit when the move exhausts. That's the exit.

        CUT THE LOSERS: if in a loss AND momentum has faded AND
        volume is gone, close it. The trade thesis failed.
        """
        import pandas as pd
        assert isinstance(df, pd.DataFrame)

        if not self._entry_time:
            return None

        rsi = ta.momentum.RSIIndicator(df["close"], window=self.rsi_period).rsi()
        current_rsi = rsi.iloc[-1]

        avg_vol = df["volume"].iloc[-20:].mean()
        recent_vol = df["volume"].iloc[-3:].mean()
        volume_drying = recent_vol < avg_vol * 0.5

        # Estimate if position is in loss (compare current price to entry-area price)
        entry_candle_idx = max(0, len(df) - self.spike_candles - 1)
        entry_area_price = df["close"].iloc[entry_candle_idx]
        if entry_area_price == 0:
            return None

        if self._position_side == "long":
            in_profit = price > entry_area_price
        else:
            in_profit = price < entry_area_price

        # IN PROFIT -> ride it. Trailing stop will handle the exit.
        if in_profit:
            return None

        # IN LOSS + momentum dead + volume gone -> cut the loser
        if self._position_side == "long" and current_rsi < 45 and volume_drying:
            return Signal(
                symbol=self.symbol, action=SignalAction.CLOSE, strategy=self.name,
                reason=f"Cut loser - momentum dead (RSI={current_rsi:.0f}, vol drying)",
                market_type=self.market_type,
            )

        if self._position_side == "short" and current_rsi > 55 and volume_drying:
            return Signal(
                symbol=self.symbol, action=SignalAction.CLOSE, strategy=self.name,
                reason=f"Cut loser - momentum dead (RSI={current_rsi:.0f}, vol drying)",
                market_type=self.market_type,
            )

        return None
