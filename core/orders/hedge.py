from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from loguru import logger
from pydantic import BaseModel, Field

from core.models import Candle, OrderSide, Position


class HedgeState(str, Enum):
    WATCHING = "watching"     # monitoring main position for reversal signals
    ACTIVE = "active"         # hedge is open
    CLOSED = "closed"         # hedge was closed (profit or stop)


class HedgePair(BaseModel):
    """Tracks a main position + its small counter-hedge.

    Example: Long $2500 BTC in profit. RSI overextended + volume fading.
    -> Tighten stop on the long
    -> Open $500 short to exploit the pullback
    -> If pullback happens: short prints, long gets stopped at profit
    -> If no pullback: short gets stopped for small loss, long keeps running
    """

    symbol: str
    main_side: str              # "long" or "short"
    main_entry: float
    main_size: float            # notional value of main position
    main_pnl_pct: float = 0.0

    hedge_side: str = ""        # opposite of main
    hedge_size: float = 0.0     # notional value of hedge (always smaller)
    hedge_entry: float = 0.0
    hedge_pnl_pct: float = 0.0
    hedge_order_id: str = ""

    state: HedgeState = HedgeState.WATCHING
    hedge_ratio: float = 0.20   # hedge is 20% of main position size
    min_main_profit_pct: float = 3.0  # main must be +3% before hedging
    hedge_stop_pct: float = 1.0  # tight stop on the hedge

    reversal_score: float = 0.0  # 0-1, how likely a reversal is
    reversal_reasons: list[str] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    hedged_at: Optional[datetime] = None

    @property
    def hedge_notional(self) -> float:
        return self.main_size * self.hedge_ratio

    def should_hedge(self) -> bool:
        if self.state != HedgeState.WATCHING:
            return False
        if self.main_pnl_pct < self.min_main_profit_pct:
            return False
        if self.reversal_score < 0.5:
            return False
        return True

    def activate_hedge(self, entry_price: float, amount: float, order_id: str) -> None:
        self.hedge_side = "short" if self.main_side == "long" else "long"
        self.hedge_entry = entry_price
        self.hedge_size = entry_price * amount
        self.hedge_order_id = order_id
        self.state = HedgeState.ACTIVE
        self.hedged_at = datetime.now(timezone.utc)
        logger.info("HEDGE ACTIVATED on {} | main: {} ${:.0f} | hedge: {} ${:.0f} | reversal: {:.0f}%",
                     self.symbol, self.main_side, self.main_size,
                     self.hedge_side, self.hedge_size, self.reversal_score * 100)

    def close_hedge(self) -> None:
        self.state = HedgeState.CLOSED

    def status_line(self) -> str:
        if self.state == HedgeState.WATCHING:
            return (f"Hedge {self.symbol}: WATCHING main={self.main_side} "
                    f"pnl={self.main_pnl_pct:+.1f}% reversal={self.reversal_score:.0%}")
        return (f"Hedge {self.symbol}: {self.state.value} "
                f"main={self.main_side} ${self.main_size:.0f} "
                f"hedge={self.hedge_side} ${self.hedge_size:.0f} "
                f"hedge_pnl={self.hedge_pnl_pct:+.1f}%")


class ReversalDetector:
    """Detects when a profitable position might be about to reverse.

    Signals checked:
    1. RSI overextension (>75 for longs, <25 for shorts)
    2. Volume divergence (price making highs but volume declining)
    3. Momentum fade (rate of price change slowing)
    4. Wick rejection (long upper wicks for longs, lower for shorts)

    Each signal adds to a reversal score (0-1). At 0.5+, hedging is triggered.
    """

    def __init__(
        self,
        rsi_overbought: float = 75.0,
        rsi_oversold: float = 25.0,
        volume_divergence_bars: int = 5,
        wick_ratio_threshold: float = 0.6,
    ):
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        self.vol_div_bars = volume_divergence_bars
        self.wick_threshold = wick_ratio_threshold

    def assess(self, candles: list[Candle], position_side: str) -> tuple[float, list[str]]:
        """Returns (reversal_score 0-1, list of reasons)."""
        if len(candles) < 30:
            return 0.0, []

        score = 0.0
        reasons: list[str] = []

        rsi = self._simple_rsi(candles, 14)
        vol_div = self._volume_divergence(candles, position_side)
        momentum_fade = self._momentum_fade(candles, position_side)
        wick_reject = self._wick_rejection(candles[-3:], position_side)

        if position_side == "long":
            if rsi > self.rsi_overbought:
                weight = min(0.35, (rsi - self.rsi_overbought) / 25 * 0.35)
                score += weight
                reasons.append(f"RSI overextended ({rsi:.0f})")
        else:
            if rsi < self.rsi_oversold:
                weight = min(0.35, (self.rsi_oversold - rsi) / 25 * 0.35)
                score += weight
                reasons.append(f"RSI oversold ({rsi:.0f})")

        if vol_div:
            score += 0.25
            reasons.append("volume divergence")

        if momentum_fade:
            score += 0.20
            reasons.append("momentum fading")

        if wick_reject:
            score += 0.20
            reasons.append("wick rejection")

        return min(1.0, score), reasons

    def _simple_rsi(self, candles: list[Candle], period: int = 14) -> float:
        if len(candles) < period + 1:
            return 50.0
        closes = [c.close for c in candles[-(period + 1):]]
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(0, diff))
            losses.append(max(0, -diff))
        avg_gain = sum(gains) / len(gains) if gains else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _volume_divergence(self, candles: list[Candle], side: str) -> bool:
        """Price making new highs/lows but volume declining = divergence."""
        n = self.vol_div_bars
        if len(candles) < n * 2:
            return False

        recent = candles[-n:]
        prev = candles[-n * 2:-n]

        recent_avg_vol = sum(c.volume for c in recent) / n
        prev_avg_vol = sum(c.volume for c in prev) / n

        if prev_avg_vol == 0:
            return False

        if side == "long":
            price_higher = recent[-1].close > prev[-1].close
            vol_lower = recent_avg_vol < prev_avg_vol * 0.7
            return price_higher and vol_lower
        else:
            price_lower = recent[-1].close < prev[-1].close
            vol_lower = recent_avg_vol < prev_avg_vol * 0.7
            return price_lower and vol_lower

    def _momentum_fade(self, candles: list[Candle], side: str) -> bool:
        """Rate of change slowing down."""
        if len(candles) < 10:
            return False

        recent_roc = (candles[-1].close - candles[-3].close) / candles[-3].close * 100
        earlier_roc = (candles[-4].close - candles[-7].close) / candles[-7].close * 100

        if side == "long":
            return earlier_roc > 0 and recent_roc > 0 and recent_roc < earlier_roc * 0.3
        return earlier_roc < 0 and recent_roc < 0 and abs(recent_roc) < abs(earlier_roc) * 0.3

    def _wick_rejection(self, candles: list[Candle], side: str) -> bool:
        """Long upper wicks for longs = rejection. Long lower wicks for shorts."""
        if not candles:
            return False

        rejection_count = 0
        for c in candles:
            full_range = c.high - c.low
            if full_range == 0:
                continue
            body_top = max(c.open, c.close)
            body_bottom = min(c.open, c.close)

            if side == "long":
                upper_wick = c.high - body_top
                if upper_wick / full_range > self.wick_threshold:
                    rejection_count += 1
            else:
                lower_wick = body_bottom - c.low
                if lower_wick / full_range > self.wick_threshold:
                    rejection_count += 1

        return rejection_count >= 2


class HedgeManager:
    """Manages hedge positions across all open main positions.

    Rules:
    - Only hedge positions that are already in decent profit (+3% default)
    - Hedge size is always a fraction of main (20% default)
    - Hedge gets a tight stop (1% default) -- this is a probe, not a trade
    - When hedge is opened, also tighten the main position's trailing stop
    - Max 1 hedge per symbol at a time
    - Only hedge on symbols with decent liquidity
    """

    def __init__(
        self,
        hedge_ratio: float = 0.20,
        min_main_profit_pct: float = 3.0,
        hedge_stop_pct: float = 1.0,
        max_hedges: int = 2,
    ):
        self.hedge_ratio = hedge_ratio
        self.min_main_profit_pct = min_main_profit_pct
        self.hedge_stop_pct = hedge_stop_pct
        self.max_hedges = max_hedges

        self.reversal_detector = ReversalDetector()
        self._pairs: dict[str, HedgePair] = {}

    def track_position(self, pos: Position) -> HedgePair:
        """Start watching a position for hedge opportunities."""
        side = "long" if pos.side == OrderSide.BUY else "short"
        pair = HedgePair(
            symbol=pos.symbol,
            main_side=side,
            main_entry=pos.entry_price,
            main_size=pos.notional_value,
            main_pnl_pct=pos.pnl_pct,
            hedge_ratio=self.hedge_ratio,
            min_main_profit_pct=self.min_main_profit_pct,
            hedge_stop_pct=self.hedge_stop_pct,
        )
        self._pairs[pos.symbol] = pair
        return pair

    def update(self, positions: list[Position], candles_map: dict[str, list[Candle]]) -> list[str]:
        """Update all tracked pairs. Returns symbols ready to hedge."""
        ready: list[str] = []

        pos_by_sym = {p.symbol: p for p in positions}

        for sym in list(self._pairs.keys()):
            pos = pos_by_sym.get(sym)
            if not pos or pos.amount == 0:
                self._pairs.pop(sym)
                continue

            pair = self._pairs[sym]
            pair.main_pnl_pct = pos.pnl_pct
            pair.main_size = pos.notional_value

            candles = candles_map.get(sym, [])
            if candles and pair.state == HedgeState.WATCHING:
                score, reasons = self.reversal_detector.assess(candles, pair.main_side)
                pair.reversal_score = score
                pair.reversal_reasons = reasons

                if pair.should_hedge() and self._active_hedge_count() < self.max_hedges:
                    ready.append(sym)

        return ready

    def get_hedge_params(self, symbol: str, current_price: float,
                         leverage: int = 10) -> Optional[dict]:
        """Get parameters for opening a hedge position."""
        pair = self._pairs.get(symbol)
        if not pair:
            return None

        notional = pair.hedge_notional
        if current_price == 0:
            return None

        amount = notional / current_price
        side = OrderSide.SELL if pair.main_side == "long" else OrderSide.BUY

        if side == OrderSide.SELL:
            stop_price = current_price * (1 + self.hedge_stop_pct / 100)
        else:
            stop_price = current_price * (1 - self.hedge_stop_pct / 100)

        return {
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "leverage": leverage,
            "stop_price": stop_price,
            "reasons": pair.reversal_reasons,
            "reversal_score": pair.reversal_score,
        }

    def activate(self, symbol: str, entry_price: float, amount: float, order_id: str) -> None:
        pair = self._pairs.get(symbol)
        if pair:
            pair.activate_hedge(entry_price, amount, order_id)

    def close(self, symbol: str) -> None:
        pair = self._pairs.get(symbol)
        if pair:
            pair.close_hedge()

    def remove(self, symbol: str) -> None:
        self._pairs.pop(symbol, None)

    def get(self, symbol: str) -> Optional[HedgePair]:
        return self._pairs.get(symbol)

    def has_active_hedge(self, symbol: str) -> bool:
        pair = self._pairs.get(symbol)
        return pair is not None and pair.state == HedgeState.ACTIVE

    def _active_hedge_count(self) -> int:
        return sum(1 for p in self._pairs.values() if p.state == HedgeState.ACTIVE)

    @property
    def active_pairs(self) -> dict[str, HedgePair]:
        return dict(self._pairs)
