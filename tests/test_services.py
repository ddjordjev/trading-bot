"""Tests for services: monitor, analytics_service, signal_generator."""

from __future__ import annotations

from datetime import UTC
from unittest.mock import MagicMock, patch

import pytest

from shared.models import (
    BotDeploymentStatus,
    DeploymentLevel,
    IntelSnapshot,
    SignalPriority,
    TradeProposal,
    TradeQueue,
    TrendingSnapshot,
    TVSymbolSnapshot,
)

# ── MonitorService ───────────────────────────────────────────────────


class TestMonitorService:
    @pytest.fixture
    def mock_settings(self):
        s = MagicMock()
        s.coinglass_api_key = ""
        s.intel_symbol_list = ["BTC", "ETH"]
        s.tv_exchange = "MEXC"
        s.tv_interval_list = ["1h", "4h"]
        s.tv_poll_interval = 120
        s.cmc_api_key = ""
        s.cmc_poll_interval = 300
        s.coingecko_api_key = ""
        s.coingecko_poll_interval = 300
        return s

    @pytest.fixture
    def monitor(self, mock_settings, tmp_path):
        with patch("services.monitor.SharedState") as mock_state_cls, patch("services.monitor.FearGreedClient"):
            with patch("services.monitor.LiquidationMonitor"):
                with patch("services.monitor.MacroCalendar"):
                    with patch("services.monitor.WhaleSentiment"):
                        with patch("services.monitor.TradingViewClient"):
                            with patch("services.monitor.CoinMarketCapClient"):
                                with patch("services.monitor.CoinGeckoClient"):
                                    with patch("services.monitor.TrendingScanner"):
                                        with patch("services.monitor.SignalGenerator"):
                                            with patch("services.monitor.get_settings", return_value=mock_settings):
                                                from services.monitor import MonitorService

                                                mock_state = MagicMock()
                                                mock_state.read_bot_status.return_value = BotDeploymentStatus(
                                                    level=DeploymentLevel.HUNTING
                                                )
                                                mock_state_cls.return_value = mock_state
                                                svc = MonitorService(settings=mock_settings)
                                                svc.state = mock_state
                                                return svc

    def test_init_sets_current_level(self, monitor):
        assert monitor._current_level == DeploymentLevel.HUNTING

    def test_update_intensity_changes_level(self, monitor):
        monitor._current_level = DeploymentLevel.HUNTING
        status = BotDeploymentStatus(level=DeploymentLevel.DEPLOYED)
        monitor._update_intensity(status)
        assert monitor._current_level == DeploymentLevel.DEPLOYED

    def test_update_intensity_unchanged_when_same(self, monitor):
        monitor._current_level = DeploymentLevel.ACTIVE
        status = BotDeploymentStatus(level=DeploymentLevel.ACTIVE)
        monitor._update_intensity(status)
        assert monitor._current_level == DeploymentLevel.ACTIVE

    def test_derive_regime_capitulation(self, monitor):
        monitor.fear_greed.is_extreme_fear = True
        monitor.fear_greed.is_extreme_greed = False
        monitor.fear_greed.is_fear = True
        monitor.fear_greed.is_greed = False
        snap = IntelSnapshot(mass_liquidation=True)
        assert monitor._derive_regime(snap) == "capitulation"

    def test_derive_regime_risk_off(self, monitor):
        monitor.fear_greed.is_extreme_fear = False
        monitor.fear_greed.is_extreme_greed = True
        monitor.fear_greed.is_greed = True
        snap = IntelSnapshot(macro_event_imminent=True, overleveraged_side="")
        assert monitor._derive_regime(snap) == "risk_off"

    def test_derive_regime_risk_on(self, monitor):
        monitor.fear_greed.is_extreme_fear = False
        monitor.fear_greed.is_extreme_greed = False
        monitor.fear_greed.is_fear = True
        snap = IntelSnapshot(mass_liquidation=True, should_reduce_exposure=False)
        assert monitor._derive_regime(snap) == "risk_on"

    def test_derive_regime_normal(self, monitor):
        monitor.fear_greed.is_extreme_fear = False
        monitor.fear_greed.is_fear = False
        monitor.fear_greed.is_greed = False
        snap = IntelSnapshot(mass_liquidation=False, should_reduce_exposure=False)
        assert monitor._derive_regime(snap) == "normal"

    def test_compute_size_mult_caps_at_1_5(self, monitor):
        monitor.fear_greed.position_bias = MagicMock(return_value=2.0)
        monitor.liquidations.aggression_boost = MagicMock(return_value=1.5)
        snap = IntelSnapshot(macro_exposure_mult=1.0)
        mult = monitor._compute_size_mult(snap)
        assert mult <= 1.5

    def test_compute_direction_long_wins(self, monitor):
        snap = IntelSnapshot(
            fear_greed_bias="long",
            liquidation_bias="long",
            mass_liquidation=False,
            whale_bias="neutral",
            tv_btc_consensus="long",
        )
        assert monitor._compute_direction(snap) == "long"

    def test_compute_direction_short_wins(self, monitor):
        snap = IntelSnapshot(
            fear_greed_bias="short",
            liquidation_bias="short",
            mass_liquidation=False,
            whale_bias="short",
            tv_btc_consensus="neutral",
        )
        assert monitor._compute_direction(snap) == "short"

    def test_compute_direction_neutral(self, monitor):
        snap = IntelSnapshot(
            fear_greed_bias="neutral",
            liquidation_bias="neutral",
            whale_bias="neutral",
            tv_btc_consensus="neutral",
        )
        assert monitor._compute_direction(snap) == "neutral"

    def test_refresh_scanner_symbols_adds_base(self, monitor):
        monitor.cmc.all_interesting = []
        monitor.gecko.all_interesting = []
        monitor._refresh_scanner_symbols()
        assert "BTC/USDT" in monitor._tv_symbols
        assert "ETH/USDT" in monitor._tv_symbols


# ── AnalyticsService ────────────────────────────────────────────────


class TestAnalyticsService:
    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.trade_count.return_value = 0
        db.connect = MagicMock()
        db.close = MagicMock()
        db.get_strategy_names.return_value = []
        return db

    @pytest.fixture
    def mock_engine(self):
        engine = MagicMock()
        engine.scores = {}
        engine.patterns = []
        engine.suggestions = []
        engine.refresh = MagicMock()
        return engine

    def test_init_defaults(self):
        with patch("services.analytics_service.SharedState"), patch("services.analytics_service.TradeDB"):
            with patch("services.analytics_service.AnalyticsEngine"):
                from services.analytics_service import AnalyticsService

                svc = AnalyticsService(refresh_interval=300)
                assert svc.refresh_interval == 300
                assert svc.engine is None
                assert svc._running is False
                assert svc._last_trade_count == 0

    def test_do_refresh_writes_state(self, mock_db, mock_engine, tmp_path):
        with patch("services.analytics_service.SharedState") as state_cls:
            with patch("services.analytics_service.TradeDB", return_value=mock_db):
                with patch("services.analytics_service.AnalyticsEngine", return_value=mock_engine):
                    from services.analytics_service import AnalyticsService
                    from shared.state import SharedState

                    state = SharedState(data_dir=tmp_path)
                    state_cls.return_value = state
                    svc = AnalyticsService(refresh_interval=60)
                    svc.state = state
                    svc.db = mock_db
                    svc.engine = mock_engine
                    mock_engine.scores = {
                        "strat1": MagicMock(
                            weight=1.0,
                            win_rate=0.5,
                            total_trades=10,
                            total_pnl=100.0,
                            streak_current=0,
                        )
                    }
                    mock_engine.patterns = []
                    mock_engine.suggestions = []
                    mock_db.trade_count.return_value = 5

                    svc._do_refresh()

                    mock_engine.refresh.assert_called_once()
                    read = state.read_analytics()
                    assert read.total_trades_logged == 5
                    assert len(read.weights) == 1
                    assert read.weights[0].strategy == "strat1"


# ── SignalGenerator ───────────────────────────────────────────────────


class TestSignalGenerator:
    @pytest.fixture
    def gen(self):
        from services.signal_generator import SignalGenerator

        return SignalGenerator()

    @pytest.fixture
    def empty_snap(self):
        return IntelSnapshot()

    @pytest.fixture
    def empty_queue(self):
        return TradeQueue()

    def test_init_cooldowns(self, gen):
        assert gen._cooldown_seconds[SignalPriority.CRITICAL] == 30
        assert gen._cooldown_seconds[SignalPriority.DAILY] == 3600
        assert gen._cooldown_seconds[SignalPriority.SWING] == 86400

    def test_generate_returns_same_queue(self, gen, empty_snap, empty_queue):
        out = gen.generate(empty_snap, empty_queue)
        assert out is empty_queue

    def test_generate_critical_mass_liquidation(self, gen, empty_queue):
        snap = IntelSnapshot(
            mass_liquidation=True,
            liquidation_24h=2e9,
            liquidation_bias="long",
        )
        gen.generate(snap, empty_queue)
        actionable = empty_queue.get_actionable(SignalPriority.CRITICAL)
        assert len(actionable) >= 1
        prop = actionable[0]
        assert prop.symbol == "BTC/USDT"
        assert prop.side == "long"
        assert prop.strategy == "liq_reversal"

    def test_generate_critical_macro_spike(self, gen, empty_queue):
        snap = IntelSnapshot(
            macro_spike_opportunity=True,
            next_macro_event="FOMC",
            preferred_direction="short",
        )
        gen.generate(snap, empty_queue)
        actionable = empty_queue.get_actionable(SignalPriority.CRITICAL)
        assert any(p.strategy == "macro_spike" for p in actionable)

    def test_generate_critical_extreme_mover(self, gen, empty_queue):
        snap = IntelSnapshot(
            hot_movers=[
                TrendingSnapshot(
                    symbol="PEPE",
                    change_1h=10.0,
                    volume_24h=10e6,
                    is_low_liquidity=False,
                )
            ],
        )
        gen.generate(snap, empty_queue)
        actionable = empty_queue.get_actionable(SignalPriority.CRITICAL)
        assert any("PEPE" in p.symbol for p in actionable)

    def test_generate_daily_fear_accumulation(self, gen, empty_queue):
        snap = IntelSnapshot(
            fear_greed=25,
            preferred_direction="long",
        )
        gen.generate(snap, empty_queue)
        actionable = empty_queue.get_actionable(SignalPriority.DAILY)
        assert any(p.strategy == "fear_accumulation" for p in actionable)

    def test_generate_daily_overleveraged_fade(self, gen, empty_queue):
        snap = IntelSnapshot(overleveraged_side="longs")
        gen.generate(snap, empty_queue)
        actionable = empty_queue.get_actionable(SignalPriority.DAILY)
        assert any(p.strategy == "overleveraged_fade" and p.side == "short" for p in actionable)

    def test_propose_respects_cooldown(self, gen, empty_queue):
        from datetime import datetime

        prop = TradeProposal(
            priority=SignalPriority.CRITICAL,
            symbol="BTC/USDT",
            side="long",
            strategy="liq_reversal",
            source="monitor",
        )
        gen._recent_ids["critical_BTC/USDT_liq_reversal"] = datetime.now(UTC)
        gen._propose(empty_queue, prop)
        assert empty_queue.total == 0

    def test_propose_skips_duplicate_strategy_in_queue(self, gen, empty_queue):
        prop = TradeProposal(
            priority=SignalPriority.DAILY,
            symbol="BTC/USDT",
            side="long",
            strategy="fear_accumulation",
            source="monitor",
        )
        empty_queue.add(prop)
        gen._propose(empty_queue, prop)
        assert len(empty_queue.daily) == 1

    def test_purge_cooldowns_removes_old(self, gen):
        from datetime import datetime, timedelta

        old = datetime.now(UTC) - timedelta(hours=25)
        gen._recent_ids["old_key"] = old
        gen._recent_ids["new_key"] = datetime.now(UTC)
        gen._purge_cooldowns()
        assert "old_key" not in gen._recent_ids
        assert "new_key" in gen._recent_ids

    def test_merge_trending_deduplicates(self, gen):
        snap = IntelSnapshot(
            hot_movers=[TrendingSnapshot(symbol="BTC", source="a")],
            cmc_trending=[TrendingSnapshot(symbol="BTC", source="b")],
            coingecko_trending=[TrendingSnapshot(symbol="ETH", source="c")],
        )
        merged = gen._merge_trending(snap)
        symbols = [m.symbol.upper() for m in merged]
        assert symbols.count("BTC") == 1
        assert "ETH" in symbols

    def test_count_directional_agreement_zero_for_neutral(self, gen):
        snap = IntelSnapshot(preferred_direction="neutral")
        assert gen._count_directional_agreement(snap) == 0

    def test_count_directional_agreement_counts_sources(self, gen):
        snap = IntelSnapshot(
            preferred_direction="long",
            fear_greed_bias="long",
            liquidation_bias="long",
            whale_bias="long",
            tv_btc_consensus="long",
            regime="risk_on",
        )
        count = gen._count_directional_agreement(snap)
        assert count >= 3

    def test_get_tv_analysis_returns_match(self, gen):
        snap = IntelSnapshot(
            tv_analyses=[
                TVSymbolSnapshot(symbol="BTC/USDT", interval="1h", rsi_14=45.0),
            ]
        )
        tv = gen._get_tv_analysis(snap, "BTC/USDT")
        assert tv is not None
        assert tv.rsi_14 == 45.0

    def test_get_tv_analysis_returns_none_wrong_symbol(self, gen):
        snap = IntelSnapshot(
            tv_analyses=[TVSymbolSnapshot(symbol="ETH/USDT", interval="1h")],
        )
        assert gen._get_tv_analysis(snap, "BTC/USDT") is None

    def test_get_tv_analysis_returns_none_empty(self, gen):
        snap = IntelSnapshot()
        assert gen._get_tv_analysis(snap, "BTC/USDT") is None

    # --- Major coin proposal tests ---

    def test_major_momentum_lower_threshold(self, gen, empty_queue):
        """Major coins get DAILY proposals at 2% move, not 5%."""
        snap = IntelSnapshot(
            hot_movers=[
                TrendingSnapshot(symbol="SOL", change_24h=3.0, volume_24h=50e6, is_low_liquidity=False),
            ],
        )
        gen.generate(snap, empty_queue)
        daily = empty_queue.get_actionable(SignalPriority.DAILY)
        assert any(p.strategy == "major_momentum" and "SOL" in p.symbol for p in daily)

    def test_major_momentum_skips_below_2pct(self, gen, empty_queue):
        """Major coins below 2% 24h move don't get trending proposals."""
        snap = IntelSnapshot(
            hot_movers=[
                TrendingSnapshot(symbol="BTC", change_24h=1.5, volume_24h=500e6, is_low_liquidity=False),
            ],
        )
        gen.generate(snap, empty_queue)
        daily = empty_queue.get_actionable(SignalPriority.DAILY)
        assert not any(p.strategy == "major_momentum" for p in daily)

    def test_altcoin_not_in_major_momentum(self, gen, empty_queue):
        """Altcoins don't get major_momentum proposals even at >2% move."""
        snap = IntelSnapshot(
            hot_movers=[
                TrendingSnapshot(symbol="PEPE", change_24h=4.0, volume_24h=10e6, is_low_liquidity=False),
            ],
        )
        gen.generate(snap, empty_queue)
        daily = empty_queue.get_actionable(SignalPriority.DAILY)
        assert not any(p.strategy == "major_momentum" for p in daily)

    def test_major_intel_direction_proposals(self, gen, empty_queue):
        """When intel has a direction, all majors get DAILY proposals."""
        snap = IntelSnapshot(preferred_direction="long", regime="risk_on")
        gen.generate(snap, empty_queue)
        daily = empty_queue.get_actionable(SignalPriority.DAILY)
        major_intel = [p for p in daily if p.strategy == "major_intel_direction"]
        symbols = {p.symbol for p in major_intel}
        assert "BTC/USDT" in symbols
        assert "SOL/USDT" in symbols
        assert "ETH/USDT" in symbols

    def test_major_intel_direction_skipped_neutral(self, gen, empty_queue):
        """Neutral direction produces no major_intel_direction proposals."""
        snap = IntelSnapshot(preferred_direction="neutral")
        gen.generate(snap, empty_queue)
        daily = empty_queue.get_actionable(SignalPriority.DAILY)
        assert not any(p.strategy == "major_intel_direction" for p in daily)

    def test_major_swing_proposals_generated(self, gen, empty_queue):
        """With regime + intel alignment, majors get SWING proposals."""
        snap = IntelSnapshot(
            preferred_direction="long",
            regime="risk_on",
            fear_greed_bias="long",
            liquidation_bias="long",
            whale_bias="long",
            tv_btc_consensus="long",
            fear_greed=30,
        )
        gen.generate(snap, empty_queue)
        swing = empty_queue.get_actionable(SignalPriority.SWING)
        major_swing = [p for p in swing if p.strategy == "major_swing"]
        assert len(major_swing) >= 3
        symbols = {p.symbol for p in major_swing}
        assert "BTC/USDT" in symbols

    def test_major_swing_not_generated_weak_alignment(self, gen, empty_queue):
        """Fewer than 2 aligned sources = no major swing proposals."""
        snap = IntelSnapshot(
            preferred_direction="long",
            regime="risk_on",
            fear_greed_bias="short",
            liquidation_bias="short",
            whale_bias="short",
            tv_btc_consensus="short",
        )
        gen.generate(snap, empty_queue)
        swing = empty_queue.get_actionable(SignalPriority.SWING)
        assert not any(p.strategy == "major_swing" for p in swing)

    def test_major_swing_not_generated_wrong_regime(self, gen, empty_queue):
        """Long direction + risk_off regime = no major swing."""
        snap = IntelSnapshot(
            preferred_direction="long",
            regime="risk_off",
            fear_greed_bias="long",
            liquidation_bias="long",
            whale_bias="long",
        )
        gen.generate(snap, empty_queue)
        swing = empty_queue.get_actionable(SignalPriority.SWING)
        assert not any(p.strategy == "major_swing" for p in swing)

    def test_custom_major_symbols(self, empty_queue):
        """SignalGenerator accepts custom major symbol set."""
        from services.signal_generator import SignalGenerator

        gen = SignalGenerator(major_symbols={"DOGE/USDT", "XRP/USDT"})
        snap = IntelSnapshot(
            hot_movers=[
                TrendingSnapshot(symbol="DOGE", change_24h=3.0, volume_24h=10e6, is_low_liquidity=False),
            ],
        )
        gen.generate(snap, empty_queue)
        daily = empty_queue.get_actionable(SignalPriority.DAILY)
        assert any(p.strategy == "major_momentum" and "DOGE" in p.symbol for p in daily)
