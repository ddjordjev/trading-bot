"""Signal generator — converts intel + market data into prioritised trade proposals.

Runs inside the MonitorService after each data refresh.  Produces
TradeProposal objects at three priority tiers:

  CRITICAL  — act within seconds (spikes, mass-liq reversals, wick scalps)
  DAILY     — valid for hours (momentum entries, trending setups, TV alignment)
  SWING     — limit-order plans valid for days with full entry/exit blueprints

The proposals are written to data/trade_queue.json and consumed by the bot.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from loguru import logger

from shared.models import (
    EntryPlan,
    IntelSnapshot,
    SignalPriority,
    TradeProposal,
    TradeQueue,
    TrendingSnapshot,
)


class SignalGenerator:
    """Stateful generator that avoids duplicate proposals via cooldowns."""

    def __init__(self) -> None:
        self._recent_ids: dict[str, datetime] = {}
        self._cooldown_seconds = {
            SignalPriority.CRITICAL: 30,
            SignalPriority.DAILY: 3600,
            SignalPriority.SWING: 86400,
        }

    def generate(self, snap: IntelSnapshot, queue: TradeQueue) -> TradeQueue:
        """Evaluate the current intel snapshot and append new proposals."""
        self._purge_cooldowns()
        queue.purge_stale()

        self._generate_critical(snap, queue)
        self._generate_daily(snap, queue)
        self._generate_swing(snap, queue)

        return queue

    # ------------------------------------------------------------------
    # CRITICAL — seconds to act
    # ------------------------------------------------------------------

    def _generate_critical(self, snap: IntelSnapshot, q: TradeQueue) -> None:
        # Mass liquidation reversal — contrarian entry on capitulation
        if snap.mass_liquidation:
            side = snap.liquidation_bias  # "long" when longs liquidated (buy dip)
            if side in ("long", "short"):
                self._propose(
                    q,
                    TradeProposal(
                        priority=SignalPriority.CRITICAL,
                        symbol="BTC/USDT",
                        side=side,
                        strategy="liq_reversal",
                        reason=f"Mass liq ${snap.liquidation_24h / 1e9:.1f}B — bias {side} (exhaustion reversal)",
                        strength=0.85,
                        leverage=10,
                        quick_trade=True,
                        max_hold_minutes=15,
                        max_age_seconds=120,
                        source="monitor",
                    ),
                )

        # Macro spike opportunity — FOMC/CPI just dropped, expect volatility
        if snap.macro_spike_opportunity:
            direction = snap.preferred_direction
            if direction in ("long", "short"):
                self._propose(
                    q,
                    TradeProposal(
                        priority=SignalPriority.CRITICAL,
                        symbol="BTC/USDT",
                        side=direction,
                        strategy="macro_spike",
                        reason=f"Macro event spike — {snap.next_macro_event}",
                        strength=0.75,
                        leverage=10,
                        quick_trade=True,
                        max_hold_minutes=10,
                        max_age_seconds=60,
                        source="monitor",
                    ),
                )

        # Hot mover with extreme 1h move — scalp the momentum
        for mover in snap.hot_movers[:5]:
            if abs(mover.change_1h) >= 8.0 and not mover.is_low_liquidity:
                side = "long" if mover.change_1h > 0 else "short"
                sym = f"{mover.symbol.upper()}/USDT"
                self._propose(
                    q,
                    TradeProposal(
                        priority=SignalPriority.CRITICAL,
                        symbol=sym,
                        side=side,
                        strategy="extreme_mover",
                        reason=f"{mover.symbol} moved {mover.change_1h:+.1f}% in 1h",
                        strength=min(0.9, abs(mover.change_1h) / 12.0),
                        leverage=10,
                        quick_trade=True,
                        max_hold_minutes=10,
                        max_age_seconds=90,
                        source="monitor",
                    ),
                )

    # ------------------------------------------------------------------
    # DAILY — valid for hours
    # ------------------------------------------------------------------

    def _generate_daily(self, snap: IntelSnapshot, q: TradeQueue) -> None:
        # Trending coins with TV alignment — momentum entry
        tv_direction = snap.tv_btc_consensus
        for mover in self._merge_trending(snap):
            if mover.is_low_liquidity:
                continue
            if abs(mover.change_24h) < 5.0:
                continue

            side = "long" if mover.change_24h > 0 else "short"
            sym = f"{mover.symbol.upper()}/USDT"

            tv_aligned = (tv_direction == side) or tv_direction == "neutral"
            strength = 0.55
            if tv_aligned:
                strength += 0.15
            if abs(mover.change_24h) > 10:
                strength += 0.1

            self._propose(
                q,
                TradeProposal(
                    priority=SignalPriority.DAILY,
                    symbol=sym,
                    side=side,
                    strategy="trending_momentum",
                    reason=f"{mover.symbol} 24h:{mover.change_24h:+.1f}% vol:${mover.volume_24h / 1e6:.0f}M"
                    f" TV:{tv_direction}",
                    strength=min(0.9, strength),
                    leverage=10,
                    max_age_seconds=14400,  # 4 hours
                    source="monitor",
                ),
            )

        # Fear zone BTC buy — accumulate when fearful
        if snap.fear_greed <= 30 and snap.preferred_direction == "long":
            self._propose(
                q,
                TradeProposal(
                    priority=SignalPriority.DAILY,
                    symbol="BTC/USDT",
                    side="long",
                    strategy="fear_accumulation",
                    reason=f"F&G={snap.fear_greed} (fear) — contrarian buy zone",
                    strength=0.65,
                    leverage=5,
                    max_age_seconds=7200,
                    source="monitor",
                ),
            )

        # Multiple intel sources agree on direction — high-conviction entry
        if snap.preferred_direction in ("long", "short"):
            aligned_sources = self._count_directional_agreement(snap)
            if aligned_sources >= 3:
                for sym in ("BTC/USDT", "ETH/USDT"):
                    self._propose(
                        q,
                        TradeProposal(
                            priority=SignalPriority.DAILY,
                            symbol=sym,
                            side=snap.preferred_direction,
                            strategy="multi_intel_convergence",
                            reason=f"{aligned_sources} intel sources agree: "
                            f"{snap.preferred_direction} (regime={snap.regime})",
                            strength=min(0.85, 0.5 + aligned_sources * 0.1),
                            leverage=10,
                            max_age_seconds=7200,
                            source="monitor",
                        ),
                    )

        # Overleveraged fade — contrarian against crowded positioning
        if snap.overleveraged_side in ("longs", "shorts"):
            fade_side = "short" if snap.overleveraged_side == "longs" else "long"
            self._propose(
                q,
                TradeProposal(
                    priority=SignalPriority.DAILY,
                    symbol="BTC/USDT",
                    side=fade_side,
                    strategy="overleveraged_fade",
                    reason=f"{snap.overleveraged_side} overleveraged — fade towards squeeze",
                    strength=0.55,
                    leverage=5,
                    max_age_seconds=14400,
                    source="monitor",
                ),
            )

    # ------------------------------------------------------------------
    # SWING — limit order plans with full entry blueprint
    # ------------------------------------------------------------------

    def _generate_swing(self, snap: IntelSnapshot, q: TradeQueue) -> None:
        # Extreme fear + heavy liquidations = "opportunity of a lifetime" candidate
        if snap.fear_greed <= 15 and snap.mass_liquidation:
            btc_tv = self._get_tv_analysis(snap, "BTC/USDT")
            rsi = btc_tv.rsi_14 if btc_tv else 30.0

            self._propose(
                q,
                TradeProposal(
                    priority=SignalPriority.SWING,
                    symbol="BTC/USDT",
                    side="long",
                    strategy="capitulation_dip_buy",
                    reason=f"Extreme fear ({snap.fear_greed}) + mass liq "
                    f"${snap.liquidation_24h / 1e9:.1f}B + RSI {rsi:.0f} — "
                    f"potential generational bottom",
                    strength=0.9,
                    leverage=3,
                    max_age_seconds=259200,  # 3 days
                    entry_plan=EntryPlan(
                        entry_zone_low=0,  # bot fills from current price
                        entry_zone_high=0,  # bot calculates from live ticker
                        stop_loss=0,  # wide — 15% below entry zone (PYRAMID will DCA)
                        take_profit_targets=[],
                        dca_levels=[],  # bot auto-DCA via PYRAMID mode
                        initial_leverage=3,
                        max_leverage=10,
                        scale_in_pct=3.0,
                        notes="Capitulation event. PYRAMID mode: start tiny, DCA into "
                        "wicks, lever up on recovery. Do NOT risk more than 3% "
                        "of portfolio on initial entry. Add at -5%, -10%, -15%. "
                        "Move stop to break-even at +5%. Let it ride.",
                    ),
                    source="monitor",
                ),
            )

        # Caution regime + greed = short setup with planned entries
        if snap.regime in ("risk_off", "caution") and snap.fear_greed >= 70:
            self._propose(
                q,
                TradeProposal(
                    priority=SignalPriority.SWING,
                    symbol="BTC/USDT",
                    side="short",
                    strategy="greed_reversal_plan",
                    reason=f"Risk-off regime + F&G={snap.fear_greed} (greed) — planned short if momentum breaks",
                    strength=0.6,
                    leverage=5,
                    max_age_seconds=172800,  # 2 days
                    entry_plan=EntryPlan(
                        initial_leverage=5,
                        max_leverage=10,
                        scale_in_pct=2.0,
                        notes="Wait for momentum break (lower high on 4h). "
                        "Enter on confirmed rejection. Tight stop above "
                        "recent high. Take profit at fear zone (F&G < 40). "
                        "Trail 2% below each new lower high.",
                    ),
                    source="monitor",
                ),
            )

        # ETH swing when strong BTC + ETH lagging (rotation play)
        btc_tv = self._get_tv_analysis(snap, "BTC/USDT")
        eth_tv = self._get_tv_analysis(snap, "ETH/USDT")
        if btc_tv and eth_tv:
            btc_strong = btc_tv.consensus in ("long", "strong_buy")
            eth_weak = eth_tv.consensus in ("neutral", "short")
            if btc_strong and eth_weak and snap.fear_greed < 60:
                self._propose(
                    q,
                    TradeProposal(
                        priority=SignalPriority.SWING,
                        symbol="ETH/USDT",
                        side="long",
                        strategy="eth_rotation_play",
                        reason="BTC strong, ETH lagging — rotation play when ETH catches up",
                        strength=0.55,
                        leverage=5,
                        max_age_seconds=259200,
                        entry_plan=EntryPlan(
                            initial_leverage=3,
                            max_leverage=8,
                            scale_in_pct=2.0,
                            notes="Enter when ETH/BTC ratio starts turning up. "
                            "Use limit orders at recent support. "
                            "DCA if it dips further. Trail stop at 4%.",
                        ),
                        source="monitor",
                    ),
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _propose(self, queue: TradeQueue, proposal: TradeProposal) -> None:
        """Add proposal if not on cooldown and not a duplicate."""
        key = f"{proposal.priority.value}_{proposal.symbol}_{proposal.strategy}"

        if key in self._recent_ids:
            cooldown = self._cooldown_seconds[proposal.priority]
            elapsed = (datetime.now(UTC) - self._recent_ids[key]).total_seconds()
            if elapsed < cooldown:
                return

        existing = queue.get_actionable(proposal.priority)
        for ex in existing:
            if ex.symbol == proposal.symbol and ex.strategy == proposal.strategy:
                return

        queue.add(proposal)
        self._recent_ids[key] = datetime.now(UTC)
        logger.info(
            "QUEUE [{}] {} {} — {} (str={:.2f})",
            proposal.priority.value.upper(),
            proposal.side.upper(),
            proposal.symbol,
            proposal.reason,
            proposal.strength,
        )

    def _purge_cooldowns(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        self._recent_ids = {k: v for k, v in self._recent_ids.items() if v > cutoff}

    def _merge_trending(self, snap: IntelSnapshot) -> list[TrendingSnapshot]:
        """Deduplicate trending coins from all sources."""
        seen: set[str] = set()
        result: list[TrendingSnapshot] = []
        for coin in snap.hot_movers + snap.cmc_trending + snap.coingecko_trending:
            if coin.symbol.upper() not in seen:
                seen.add(coin.symbol.upper())
                result.append(coin)
        return result

    def _count_directional_agreement(self, snap: IntelSnapshot) -> int:
        """Count how many independent intel sources agree on the preferred direction."""
        target = snap.preferred_direction
        if target == "neutral":
            return 0

        count = 0
        if snap.fear_greed_bias == target:
            count += 1
        if snap.liquidation_bias == target:
            count += 1
        if snap.whale_bias == target:
            count += 1
        if snap.tv_btc_consensus == target:
            count += 1
        if snap.regime == "risk_on" and target == "long":
            count += 1
        if snap.regime == "risk_off" and target == "short":
            count += 1
        return count

    def _get_tv_analysis(self, snap: IntelSnapshot, symbol: str):
        for tv in snap.tv_analyses:
            if tv.symbol == symbol and tv.interval == "1h":
                return tv
        return None
