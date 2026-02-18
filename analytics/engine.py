from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from loguru import logger

from db.store import TradeDB
from db.models import (
    TradeRecord, StrategyScore, PatternInsight, ModificationSuggestion,
)

MIN_TRADES_FOR_ANALYSIS = 10
MIN_TRADES_FOR_SUGGESTION = 15


class AnalyticsEngine:
    """Analyzes trade history to detect patterns, score strategies, and suggest modifications.

    Weight factor logic:
    - Base weight = 1.0
    - Win rate penalty: below 40% -> reduce, below 25% -> severely reduce
    - Profit factor bonus: above 1.5 -> boost, below 0.8 -> reduce
    - Streak penalty: 5+ consecutive losses -> reduce
    - Regime awareness: skip regimes where strategy consistently fails
    - Time awareness: identify hours where strategy loses money
    """

    def __init__(self, db: TradeDB):
        self._db = db
        self._scores: dict[str, StrategyScore] = {}
        self._patterns: list[PatternInsight] = []
        self._suggestions: list[ModificationSuggestion] = []

    def refresh(self) -> None:
        self._compute_strategy_scores()
        self._detect_patterns()
        self._generate_suggestions()
        logger.info("Analytics refreshed: {} strategies scored, {} patterns, {} suggestions",
                     len(self._scores), len(self._patterns), len(self._suggestions))

    @property
    def scores(self) -> dict[str, StrategyScore]:
        return dict(self._scores)

    @property
    def patterns(self) -> list[PatternInsight]:
        return list(self._patterns)

    @property
    def suggestions(self) -> list[ModificationSuggestion]:
        return list(self._suggestions)

    def get_weight(self, strategy: str) -> float:
        score = self._scores.get(strategy)
        return score.weight if score else 1.0

    # ------------------------------------------------------------------ #
    #  Strategy scoring
    # ------------------------------------------------------------------ #

    def _compute_strategy_scores(self) -> None:
        self._scores.clear()
        strategies = self._db.get_strategy_names()

        for name in strategies:
            stats = self._db.get_strategy_stats(name)
            if not stats or stats["total"] < 3:
                continue

            total = stats["total"]
            winners = stats["winners"] or 0
            losers = stats["losers"] or 0
            avg_win = stats["avg_win"] or 0
            avg_loss = stats["avg_loss"] or 0
            total_pnl = stats["total_pnl"] or 0
            gross_profit = stats["gross_profit"] or 0
            gross_loss = stats["gross_loss"] or 0.001
            avg_hold = stats["avg_hold"] or 0
            win_rate = winners / total if total else 0

            profit_factor = gross_profit / gross_loss if gross_loss > 0 else 10.0
            expectancy = total_pnl / total if total else 0

            streak = self._db.get_recent_streak(name)
            max_loss_streak = self._db.get_max_loss_streak(name)

            hourly = self._db.get_hourly_performance(name)
            best_hour, worst_hour = -1, -1
            best_pnl, worst_pnl = float("-inf"), float("inf")
            for h in hourly:
                if h["trades"] >= 3:
                    if h["total_pnl"] > best_pnl:
                        best_pnl = h["total_pnl"]
                        best_hour = h["hour_utc"]
                    if h["total_pnl"] < worst_pnl:
                        worst_pnl = h["total_pnl"]
                        worst_hour = h["hour_utc"]

            regime_data = self._db.get_regime_performance(name)
            best_regime, worst_regime = "", ""
            if regime_data:
                best_regime = max(regime_data, key=lambda r: r["avg_pnl"])["market_regime"]
                worst_regime = min(regime_data, key=lambda r: r["avg_pnl"])["market_regime"]

            weight = self._compute_weight(
                win_rate, profit_factor, expectancy, streak, max_loss_streak, total,
            )

            self._scores[name] = StrategyScore(
                strategy=name,
                total_trades=total,
                winners=winners,
                losers=losers,
                win_rate=win_rate,
                avg_win_pct=avg_win,
                avg_loss_pct=avg_loss,
                total_pnl=total_pnl,
                profit_factor=profit_factor,
                expectancy=expectancy,
                weight=weight,
                streak_current=streak,
                streak_max_loss=max_loss_streak,
                avg_hold_minutes=avg_hold,
                best_hour_utc=best_hour,
                worst_hour_utc=worst_hour,
                best_regime=best_regime,
                worst_regime=worst_regime,
                last_updated=datetime.now(timezone.utc).isoformat(),
            )

    @staticmethod
    def _compute_weight(
        win_rate: float, profit_factor: float, expectancy: float,
        streak: int, max_loss_streak: int, total: int,
    ) -> float:
        if total < MIN_TRADES_FOR_ANALYSIS:
            return 1.0  # not enough data, keep default

        w = 1.0

        # Win rate adjustment
        if win_rate >= 0.6:
            w *= 1.2
        elif win_rate >= 0.45:
            w *= 1.0
        elif win_rate >= 0.35:
            w *= 0.7
        elif win_rate >= 0.25:
            w *= 0.4
        else:
            w *= 0.15  # almost always losing

        # Profit factor adjustment
        if profit_factor >= 2.0:
            w *= 1.3
        elif profit_factor >= 1.5:
            w *= 1.1
        elif profit_factor >= 1.0:
            w *= 1.0
        elif profit_factor >= 0.7:
            w *= 0.7
        else:
            w *= 0.4

        # Expectancy: if negative, reduce further
        if expectancy < 0 and total >= MIN_TRADES_FOR_ANALYSIS:
            w *= 0.5

        # Current losing streak penalty
        if streak <= -5:
            w *= 0.5
        elif streak <= -3:
            w *= 0.7

        # Historical max loss streak
        if max_loss_streak >= 8:
            w *= 0.8

        return round(max(0.05, min(2.0, w)), 3)

    # ------------------------------------------------------------------ #
    #  Pattern detection
    # ------------------------------------------------------------------ #

    def _detect_patterns(self) -> None:
        self._patterns.clear()

        all_trades = self._db.get_all_trades(limit=500)
        if len(all_trades) < MIN_TRADES_FOR_ANALYSIS:
            return

        losers = [t for t in all_trades if not t.is_winner and t.pnl_usd != 0]
        winners = [t for t in all_trades if t.is_winner]

        self._detect_time_patterns(losers, winners)
        self._detect_regime_patterns(losers, winners)
        self._detect_strategy_symbol_patterns(losers, winners)
        self._detect_volatility_patterns(losers, winners)
        self._detect_quick_trade_patterns(losers, winners)
        self._detect_dca_patterns(all_trades)

    def _detect_time_patterns(self, losers: list[TradeRecord], winners: list[TradeRecord]) -> None:
        hour_losses: dict[int, int] = defaultdict(int)
        hour_wins: dict[int, int] = defaultdict(int)
        for t in losers:
            hour_losses[t.hour_utc] += 1
        for t in winners:
            hour_wins[t.hour_utc] += 1

        for hour in range(24):
            losses = hour_losses.get(hour, 0)
            wins = hour_wins.get(hour, 0)
            total = losses + wins
            if total < 5:
                continue
            loss_rate = losses / total
            if loss_rate >= 0.75:
                self._patterns.append(PatternInsight(
                    pattern_type="time_of_day",
                    description=f"Hour {hour}:00 UTC has {loss_rate:.0%} loss rate ({losses}/{total} trades)",
                    severity="warning" if loss_rate < 0.85 else "critical",
                    sample_size=total,
                    confidence=min(1.0, total / 20),
                    suggestion=f"Consider reducing activity at {hour}:00 UTC",
                    data={"hour": hour, "losses": losses, "wins": wins, "loss_rate": loss_rate},
                ))

    def _detect_regime_patterns(self, losers: list[TradeRecord], winners: list[TradeRecord]) -> None:
        regime_losses: dict[str, int] = defaultdict(int)
        regime_wins: dict[str, int] = defaultdict(int)
        regime_pnl: dict[str, float] = defaultdict(float)
        for t in losers:
            if t.market_regime:
                regime_losses[t.market_regime] += 1
                regime_pnl[t.market_regime] += t.pnl_usd
        for t in winners:
            if t.market_regime:
                regime_wins[t.market_regime] += 1
                regime_pnl[t.market_regime] += t.pnl_usd

        for regime in set(list(regime_losses.keys()) + list(regime_wins.keys())):
            losses = regime_losses.get(regime, 0)
            wins = regime_wins.get(regime, 0)
            total = losses + wins
            if total < 5:
                continue
            loss_rate = losses / total
            if loss_rate >= 0.7:
                self._patterns.append(PatternInsight(
                    pattern_type="market_regime",
                    description=(f"Regime '{regime}' has {loss_rate:.0%} loss rate "
                                 f"(${regime_pnl[regime]:+.2f} total PnL)"),
                    severity="warning",
                    sample_size=total,
                    confidence=min(1.0, total / 15),
                    suggestion=f"Reduce position size or skip entries during '{regime}' regime",
                    data={"regime": regime, "loss_rate": loss_rate, "total_pnl": regime_pnl[regime]},
                ))

    def _detect_strategy_symbol_patterns(self, losers: list[TradeRecord], winners: list[TradeRecord]) -> None:
        combo_losses: dict[tuple[str, str], int] = defaultdict(int)
        combo_wins: dict[tuple[str, str], int] = defaultdict(int)
        combo_pnl: dict[tuple[str, str], float] = defaultdict(float)

        for t in losers:
            k = (t.strategy, t.symbol)
            combo_losses[k] += 1
            combo_pnl[k] += t.pnl_usd
        for t in winners:
            k = (t.strategy, t.symbol)
            combo_wins[k] += 1
            combo_pnl[k] += t.pnl_usd

        for combo in set(list(combo_losses.keys()) + list(combo_wins.keys())):
            losses = combo_losses.get(combo, 0)
            wins = combo_wins.get(combo, 0)
            total = losses + wins
            if total < 8:
                continue
            loss_rate = losses / total
            if loss_rate >= 0.65 and combo_pnl[combo] < 0:
                strat, sym = combo
                self._patterns.append(PatternInsight(
                    pattern_type="strategy_symbol",
                    description=(f"'{strat}' on {sym}: {loss_rate:.0%} loss rate, "
                                 f"${combo_pnl[combo]:+.2f} total"),
                    severity="critical" if loss_rate >= 0.8 else "warning",
                    affected_strategy=strat,
                    affected_symbol=sym,
                    sample_size=total,
                    confidence=min(1.0, total / 15),
                    suggestion=f"Consider disabling '{strat}' for {sym} or changing parameters",
                    data={"loss_rate": loss_rate, "pnl": combo_pnl[combo]},
                ))

    def _detect_volatility_patterns(self, losers: list[TradeRecord], winners: list[TradeRecord]) -> None:
        high_vol_losses = sum(1 for t in losers if t.volatility_pct > 5)
        high_vol_wins = sum(1 for t in winners if t.volatility_pct > 5)
        low_vol_losses = sum(1 for t in losers if 0 < t.volatility_pct <= 1)
        low_vol_wins = sum(1 for t in winners if 0 < t.volatility_pct <= 1)

        total_high = high_vol_losses + high_vol_wins
        total_low = low_vol_losses + low_vol_wins

        if total_high >= 5 and high_vol_losses / total_high >= 0.7:
            self._patterns.append(PatternInsight(
                pattern_type="volatility",
                description=f"High volatility (>5%) trades lose {high_vol_losses}/{total_high} times",
                severity="warning",
                sample_size=total_high,
                confidence=min(1.0, total_high / 15),
                suggestion="Tighter stops or smaller positions during high volatility",
                data={"vol_threshold": 5, "loss_rate": high_vol_losses / total_high},
            ))

        if total_low >= 5 and low_vol_losses / total_low >= 0.7:
            self._patterns.append(PatternInsight(
                pattern_type="volatility",
                description=f"Low volatility (<1%) trades lose {low_vol_losses}/{total_low} times",
                severity="info",
                sample_size=total_low,
                confidence=min(1.0, total_low / 15),
                suggestion="Skip entries during very low volatility — not enough movement to profit",
                data={"vol_threshold": 1, "loss_rate": low_vol_losses / total_low},
            ))

    def _detect_quick_trade_patterns(self, losers: list[TradeRecord], winners: list[TradeRecord]) -> None:
        qt_losses = sum(1 for t in losers if t.was_quick_trade)
        qt_wins = sum(1 for t in winners if t.was_quick_trade)
        total = qt_losses + qt_wins

        if total >= 10:
            loss_rate = qt_losses / total
            if loss_rate >= 0.65:
                self._patterns.append(PatternInsight(
                    pattern_type="quick_trade",
                    description=f"Quick (scalp) trades lose {loss_rate:.0%} of the time ({qt_losses}/{total})",
                    severity="warning",
                    sample_size=total,
                    confidence=min(1.0, total / 20),
                    suggestion="Consider longer hold times or stricter entry criteria for scalps",
                    data={"loss_rate": loss_rate},
                ))

    def _detect_dca_patterns(self, all_trades: list[TradeRecord]) -> None:
        dca_trades = [t for t in all_trades if t.dca_count > 0]
        if len(dca_trades) < 5:
            return
        dca_winners = sum(1 for t in dca_trades if t.is_winner)
        dca_total = len(dca_trades)
        win_rate = dca_winners / dca_total

        high_dca = [t for t in dca_trades if t.dca_count >= 3]
        if len(high_dca) >= 3:
            high_dca_wins = sum(1 for t in high_dca if t.is_winner)
            high_win_rate = high_dca_wins / len(high_dca)
            if high_win_rate < 0.3:
                self._patterns.append(PatternInsight(
                    pattern_type="dca_depth",
                    description=f"Trades with 3+ DCA adds win only {high_win_rate:.0%} of the time",
                    severity="warning",
                    sample_size=len(high_dca),
                    confidence=min(1.0, len(high_dca) / 10),
                    suggestion="Consider wider DCA intervals or earlier stop when thesis is invalidated",
                    data={"dca_min": 3, "win_rate": high_win_rate},
                ))

    # ------------------------------------------------------------------ #
    #  Modification suggestions
    # ------------------------------------------------------------------ #

    def _generate_suggestions(self) -> None:
        self._suggestions.clear()

        for name, score in self._scores.items():
            if score.total_trades < MIN_TRADES_FOR_SUGGESTION:
                continue

            # Suggest disabling strategies with very low win rate
            if score.win_rate < 0.25 and score.total_pnl < 0:
                self._suggestions.append(ModificationSuggestion(
                    strategy=name,
                    suggestion_type="disable",
                    title=f"Disable '{name}'",
                    description=(f"Only {score.win_rate:.0%} win rate over {score.total_trades} trades. "
                                 f"Total PnL: ${score.total_pnl:+.2f}. "
                                 f"This strategy is consistently losing money."),
                    confidence=min(1.0, score.total_trades / 30),
                    based_on_trades=score.total_trades,
                ))

            # Suggest reducing weight for underperforming strategies
            elif score.win_rate < 0.4 and score.profit_factor < 1.0:
                self._suggestions.append(ModificationSuggestion(
                    strategy=name,
                    suggestion_type="reduce_weight",
                    title=f"Reduce weight for '{name}'",
                    description=(f"Win rate: {score.win_rate:.0%}, PF: {score.profit_factor:.2f}. "
                                 f"Reducing position size will limit losses while keeping exposure."),
                    confidence=min(1.0, score.total_trades / 25),
                    current_value="1.0",
                    suggested_value=f"{score.weight:.2f}",
                    expected_improvement=f"Reduce losses by ~{(1 - score.weight) * 100:.0f}%",
                    based_on_trades=score.total_trades,
                ))

            # Time-based filter suggestion
            if score.worst_hour_utc >= 0:
                hourly = self._db.get_hourly_performance(name)
                worst_data = next((h for h in hourly if h["hour_utc"] == score.worst_hour_utc), None)
                if worst_data and worst_data["trades"] >= 5 and worst_data["total_pnl"] < -10:
                    win_rate_at_hour = worst_data["wins"] / worst_data["trades"]
                    if win_rate_at_hour < 0.3:
                        self._suggestions.append(ModificationSuggestion(
                            strategy=name,
                            suggestion_type="time_filter",
                            title=f"Skip '{name}' at {score.worst_hour_utc}:00 UTC",
                            description=(f"At {score.worst_hour_utc}:00 UTC: {win_rate_at_hour:.0%} win rate, "
                                         f"${worst_data['total_pnl']:+.2f} total. "
                                         f"Avoiding this hour would improve results."),
                            confidence=min(1.0, worst_data["trades"] / 10),
                            current_value="active all hours",
                            suggested_value=f"skip hour {score.worst_hour_utc}",
                            based_on_trades=worst_data["trades"],
                        ))

            # Regime filter suggestion
            if score.worst_regime:
                regime_data = self._db.get_regime_performance(name)
                worst = next((r for r in regime_data if r["market_regime"] == score.worst_regime), None)
                if worst and worst["trades"] >= 5 and worst["total_pnl"] < -10:
                    wr = worst["wins"] / worst["trades"]
                    if wr < 0.3:
                        self._suggestions.append(ModificationSuggestion(
                            strategy=name,
                            suggestion_type="regime_filter",
                            title=f"Skip '{name}' during '{score.worst_regime}' regime",
                            description=(f"In '{score.worst_regime}': {wr:.0%} win rate, "
                                         f"${worst['total_pnl']:+.2f} total. "
                                         f"Strategy doesn't work in this market condition."),
                            confidence=min(1.0, worst["trades"] / 10),
                            current_value="active in all regimes",
                            suggested_value=f"skip {score.worst_regime}",
                            based_on_trades=worst["trades"],
                        ))

            # Current losing streak warning
            if score.streak_current <= -5:
                self._suggestions.append(ModificationSuggestion(
                    strategy=name,
                    suggestion_type="reduce_weight",
                    title=f"'{name}' on {abs(score.streak_current)}-loss streak",
                    description=(f"Currently {abs(score.streak_current)} consecutive losses. "
                                 f"Temporarily reducing allocation until streak breaks."),
                    confidence=0.8,
                    current_value=f"weight={score.weight:.2f}",
                    suggested_value=f"weight={max(0.1, score.weight * 0.5):.2f}",
                    based_on_trades=abs(score.streak_current),
                ))

        # Cross-strategy suggestions from patterns
        for pattern in self._patterns:
            if pattern.pattern_type == "strategy_symbol" and pattern.severity == "critical":
                self._suggestions.append(ModificationSuggestion(
                    strategy=pattern.affected_strategy,
                    symbol=pattern.affected_symbol,
                    suggestion_type="disable",
                    title=f"Disable '{pattern.affected_strategy}' on {pattern.affected_symbol}",
                    description=pattern.description,
                    confidence=pattern.confidence,
                    based_on_trades=pattern.sample_size,
                ))

    def summary(self) -> str:
        lines = ["=== ANALYTICS SUMMARY ==="]
        for name, score in sorted(self._scores.items(), key=lambda x: x[1].total_pnl, reverse=True):
            lines.append(
                f"  {name}: {score.total_trades} trades | "
                f"WR: {score.win_rate:.0%} | PF: {score.profit_factor:.1f} | "
                f"PnL: ${score.total_pnl:+.2f} | Weight: {score.weight:.2f} | "
                f"Streak: {score.streak_current:+d}"
            )
        if self._suggestions:
            lines.append(f"  {len(self._suggestions)} modification suggestion(s) pending")
        return "\n".join(lines)
