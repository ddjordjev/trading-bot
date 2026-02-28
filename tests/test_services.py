"""Tests for services: monitor, analytics_service, signal_generator."""

from __future__ import annotations

from datetime import UTC
from unittest.mock import MagicMock, patch

import pytest

from hub.state import HubState
from shared.models import (
    AnalyticsSnapshot,
    BotDeploymentStatus,
    DeploymentLevel,
    IntelSnapshot,
    SignalPriority,
    StrategyWeightEntry,
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
        s.tv_exchange = "BYBIT"
        s.tv_interval_list = ["1h", "4h"]
        s.tv_poll_interval = 120
        s.cmc_api_key = ""
        s.cmc_poll_interval = 300
        s.coingecko_api_key = ""
        s.coingecko_poll_interval = 300
        s.cex_scanner_enabled = True
        s.binance_scanner_enabled = True
        s.binance_scanner_poll_interval = 60
        s.binance_scanner_min_quote_volume = 5_000_000.0
        s.binance_scanner_top_movers_count = 15
        s.binance_scanner_history_hours = 24
        s.binance_scanner_retention_days = 7
        return s

    @pytest.fixture
    def monitor(self, mock_settings, tmp_path):
        with patch("services.monitor.HubState") as mock_state_cls, patch("services.monitor.FearGreedClient"):
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

    def test_refresh_scanner_filters_by_exchange_symbols(self, monitor):
        """Scanner refresh only includes symbols on known exchanges."""
        monitor._exchange_symbols = {"BINANCE": {"BTC/USDT", "ETH/USDT", "SOL/USDT"}}
        coin = MagicMock()
        coin.symbol = "FAKE"
        coin.is_tradable_size = True
        monitor.cmc.all_interesting = [coin]
        monitor.gecko.all_interesting = []
        monitor._refresh_scanner_symbols()
        assert "FAKE/USDT" not in monitor._tv_symbols
        assert "BTC/USDT" in monitor._tv_symbols

    def test_build_ta_candidates_handles_pair_symbols(self, monitor):
        """TA candidates should not double-suffix pair-formatted symbols."""
        monitor.signal_gen._major_symbols = {"ETH/USDT"}
        snap = IntelSnapshot(
            hot_movers=[
                TrendingSnapshot(symbol="BTC/USDT", is_low_liquidity=False),
            ]
        )
        candidates = monitor._build_ta_candidates(snap)
        assert "BTC/USDT" in candidates
        assert "BTC/USDT/USDT" not in candidates

    def test_route_to_bots_drops_unavailable_symbols(self, monitor):
        """Proposals for symbols not on any exchange are filtered out before queuing."""
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        monitor.state = HubState()
        monitor._exchange_symbols = {"BINANCE": {"BTC/USDT", "ETH/USDT"}}

        staging = TradeQueue()
        prop = TradeProposal(
            priority=SignalPriority.DAILY,
            symbol="FAKE/USDT",
            side="long",
            strategy="test",
        )
        staging.add(prop)

        bot_status = BotDeploymentStatus(
            bot_id="momentum",
            bot_style="momentum",
            exchange="BINANCE",
            should_trade=True,
            open_positions=0,
            max_positions=3,
        )
        monitor._route_to_bots(staging, [bot_status])
        assert monitor.state.read_trade_queue().pending_count == 0

    def test_route_to_bots_allows_supported_exchange(self, monitor):
        """Proposals for supported symbols get routed to the shared hub queue."""
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        hub_state = HubState()
        hub_state.write_trade_queue = MagicMock()
        monitor.state = hub_state

        staging = TradeQueue()
        prop = TradeProposal(
            priority=SignalPriority.DAILY,
            symbol="BTC/USDT",
            side="long",
            strategy="test",
            supported_exchanges=["BINANCE"],
        )
        staging.add(prop)

        bot_status = BotDeploymentStatus(
            bot_id="momentum",
            bot_style="momentum",
            exchange="BINANCE",
            should_trade=True,
            open_positions=0,
            max_positions=3,
        )
        monitor._route_to_bots(staging, [bot_status])
        hub_state.write_trade_queue.assert_called_once()

    def test_route_to_bots_paper_live_excludes_non_selected_testnet_symbols(self, monitor):
        """paper_live mode drops proposals unsupported by the active bot fleet."""
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        with patch("db.hub_store.HubDB") as mock_db:
            mock_db.return_value.connect = MagicMock()
            mock_db.return_value.close = MagicMock()
            mock_db.return_value.get_open_trade_symbols = MagicMock(return_value=set())

            monitor.settings.trading_mode = "paper_live"
            monitor.settings.exchange = "bybit"
            monitor.state = HubState()
            monitor._exchange_symbols = {
                "BYBIT": {"BTC/USDT"},
                "BINANCE": {"BTC/USDT", "PEPE/USDT"},
            }

            staging = TradeQueue()
            staging.add(
                TradeProposal(
                    priority=SignalPriority.DAILY,
                    symbol="PEPE/USDT",
                    side="long",
                    strategy="test",
                    supported_exchanges=["BYBIT"],
                )
            )

            statuses = [
                BotDeploymentStatus(
                    bot_id="bot-bybit",
                    bot_style="momentum",
                    exchange="BYBIT",
                    should_trade=True,
                    open_positions=0,
                    max_positions=3,
                )
            ]
            monitor._route_to_bots(staging, statuses)
            assert monitor.state.read_trade_queue().pending_count == 0

    def test_route_to_bots_paper_live_forces_selected_exchange(self, monitor):
        """paper_live mode keeps proposals executable on active bot exchanges."""
        from shared.models import SignalPriority, TradeProposal, TradeQueue

        with patch("db.hub_store.HubDB") as mock_db:
            mock_db.return_value.connect = MagicMock()
            mock_db.return_value.close = MagicMock()
            mock_db.return_value.get_open_trade_symbols = MagicMock(return_value=set())

            monitor.settings.trading_mode = "paper_live"
            monitor.settings.exchange = "bybit"
            monitor.state = HubState()
            monitor._exchange_symbols = {
                "BYBIT": {"BTC/USDT"},
                "BINANCE": {"BTC/USDT"},
            }

            staging = TradeQueue()
            staging.add(
                TradeProposal(
                    priority=SignalPriority.DAILY,
                    symbol="BTC/USDT",
                    side="long",
                    strategy="test",
                    supported_exchanges=["BYBIT", "BYBIT", "BINANCE"],
                )
            )

            statuses = [
                BotDeploymentStatus(
                    bot_id="bot-bybit",
                    bot_style="momentum",
                    exchange="BYBIT",
                    should_trade=True,
                    open_positions=0,
                    max_positions=3,
                )
            ]
            monitor._route_to_bots(staging, statuses)
            queued = monitor.state.read_trade_queue().proposals
            assert len(queued) == 1
            assert queued[0].supported_exchanges == ["BYBIT"]


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
        with patch("services.analytics_service.HubState"), patch("services.analytics_service.TradeDB"):
            with patch("services.analytics_service.AnalyticsEngine"):
                from services.analytics_service import AnalyticsService

                svc = AnalyticsService(refresh_interval=300)
                assert svc.refresh_interval == 300
                assert svc.engine is None
                assert svc._running is False
                assert svc._last_trade_count == 0

    def test_do_refresh_writes_state(self, mock_db, mock_engine, tmp_path):
        with patch("services.analytics_service.HubState") as state_cls:
            with patch("services.analytics_service.TradeDB", return_value=mock_db):
                with patch("services.analytics_service.AnalyticsEngine", return_value=mock_engine):
                    from hub.state import HubState
                    from services.analytics_service import AnalyticsService

                    state = HubState(data_dir=tmp_path)
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

    def test_merge_openclaw_suggestions_marks_actionable_implemented(self):
        with patch("services.analytics_service.HubState"), patch("services.analytics_service.TradeDB"):
            from services.analytics_service import AnalyticsService

            svc = AnalyticsService(refresh_interval=300)
            svc.hub_db = MagicMock()
            svc.hub_db.list_openclaw_suggestions.return_value = [
                {
                    "id": 11,
                    "status": "new",
                    "suggestion_type": "reduce_weight",
                    "strategy": "compound_momentum",
                    "symbol": "BTC/USDT",
                    "title": "Trim size",
                    "description": "Reduce weight for drawdown control",
                    "suggested_value": "0.7",
                },
                {
                    "id": 12,
                    "status": "rejected",
                    "suggestion_type": "disable",
                    "strategy": "mean_reversion",
                },
            ]
            base = [{"strategy": "rsi", "suggestion_type": "reduce_weight"}]
            merged = svc._merge_openclaw_suggestions(base)

            assert len(merged) == 2
            assert merged[1]["source"] == "openclaw"
            assert merged[1]["strategy"] == "compound_momentum"
            svc.hub_db.mark_openclaw_suggestion_status.assert_called_once_with(
                11, "implemented", notes="auto_applied_by_signal_generator"
            )

    def test_do_refresh_includes_openclaw_suggestions_in_state(self, mock_db, mock_engine, tmp_path):
        with patch("services.analytics_service.HubState") as state_cls:
            with patch("services.analytics_service.TradeDB", return_value=mock_db):
                with patch("services.analytics_service.AnalyticsEngine", return_value=mock_engine):
                    from hub.state import HubState
                    from services.analytics_service import AnalyticsService

                    state = HubState(data_dir=tmp_path)
                    state_cls.return_value = state
                    svc = AnalyticsService(refresh_interval=60)
                    svc.state = state
                    svc.db = mock_db
                    svc.engine = mock_engine
                    svc.hub_db = MagicMock()
                    svc.hub_db.list_openclaw_suggestions.return_value = [
                        {
                            "id": 21,
                            "status": "accepted",
                            "suggestion_type": "time_filter",
                            "strategy": "compound_momentum",
                            "symbol": "ETH/USDT",
                            "title": "Skip bad hour",
                            "description": "Avoid low-win-rate hour",
                            "suggested_value": "skip hour 3",
                        }
                    ]
                    svc.hub_db.mark_openclaw_suggestion_status.return_value = True
                    mock_engine.scores = {}
                    mock_engine.patterns = []
                    mock_engine.suggestions = []
                    mock_db.trade_count.return_value = 9

                    svc._do_refresh()

                    read = state.read_analytics()
                    assert len(read.suggestions) == 1
                    assert read.suggestions[0]["source"] == "openclaw"
                    assert read.suggestions[0]["suggestion_type"] == "time_filter"


# ── SignalGenerator ───────────────────────────────────────────────────


class TestSignalGenerator:
    _TEST_SYMBOLS = {
        "BTC/USDT",
        "ETH/USDT",
        "SOL/USDT",
        "XRP/USDT",
        "DOGE/USDT",
        "ADA/USDT",
        "AVAX/USDT",
        "LINK/USDT",
        "DOT/USDT",
        "SHIB/USDT",
        "PEPE/USDT",
        "MATIC/USDT",
        "LTC/USDT",
        "NEAR/USDT",
        "UNI/USDT",
        "OP/USDT",
        "ARB/USDT",
        "ANYTHING/USDT",
        "LOW/USDT",
        "ONLY/USDT",
        "NEW/USDT",
        "JUNK/USDT",
        "FAKE/USDT",
        *(f"PRE{i}/USDT" for i in range(20)),
        *(f"COIN{i}/USDT" for i in range(20)),
    }

    @pytest.fixture
    def gen(self):
        from services.signal_generator import SignalGenerator

        g = SignalGenerator()
        g.update_exchange_symbols({"BINANCE": self._TEST_SYMBOLS})
        return g

    @pytest.fixture
    def empty_snap(self):
        return IntelSnapshot()

    @pytest.fixture
    def empty_queue(self):
        return TradeQueue()

    def test_init_cooldowns(self, gen):
        assert gen._cooldown_seconds[SignalPriority.CRITICAL] == 30
        assert gen._cooldown_seconds[SignalPriority.DAILY] == 900
        assert gen._cooldown_seconds[SignalPriority.SWING] == 14400

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

    def test_generate_handles_pair_formatted_hot_mover_symbol(self, gen, empty_queue):
        snap = IntelSnapshot(
            hot_movers=[
                TrendingSnapshot(
                    symbol="PEPE/USDT",
                    change_1h=10.0,
                    change_24h=6.0,
                    volume_24h=10e6,
                    is_low_liquidity=False,
                )
            ],
        )
        gen.generate(snap, empty_queue)
        actionable = empty_queue.get_actionable(SignalPriority.CRITICAL) + empty_queue.get_actionable(
            SignalPriority.DAILY
        )
        assert any(p.symbol == "PEPE/USDT" for p in actionable)
        assert not any("/USDT/USDT" in p.symbol for p in actionable)

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
        assert empty_queue.total == 1

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

    def test_merge_trending_deduplicates_pair_and_token_formats(self, gen):
        snap = IntelSnapshot(
            hot_movers=[TrendingSnapshot(symbol="BTC/USDT", source="a")],
            cmc_trending=[TrendingSnapshot(symbol="BTC", source="b")],
        )
        merged = gen._merge_trending(snap)
        assert len(merged) == 1

    def test_cex_weighting_boosts_trend_aligned_signal(self, gen, empty_queue):
        snap = IntelSnapshot(
            tv_btc_consensus="neutral",
            hot_movers=[
                TrendingSnapshot(
                    symbol="PEPE/USDT",
                    source="binance_scanner",
                    change_5m=2.5,
                    change_1h=7.0,
                    change_24h=3.0,
                    cex_change_4h=10.0,
                    cex_confidence=0.95,
                    cex_vol_accel=1.8,
                    cex_score=20.0,
                    volume_24h=20e6,
                    is_low_liquidity=False,
                )
            ],
        )
        gen.generate(snap, empty_queue)
        daily = empty_queue.get_actionable(SignalPriority.DAILY)
        proposal = next(p for p in daily if p.strategy == "trending_momentum" and p.symbol == "PEPE/USDT")
        # Baseline without cex weighting would be 0.55 for this setup.
        assert proposal.strength > 0.55

    def test_cex_weighting_penalizes_trend_misaligned_signal(self, gen, empty_queue):
        snap = IntelSnapshot(
            tv_btc_consensus="neutral",
            hot_movers=[
                TrendingSnapshot(
                    symbol="PEPE/USDT",
                    source="binance_scanner",
                    change_5m=-2.0,
                    change_1h=-8.0,
                    change_24h=3.0,  # still produces a LONG trending_momentum proposal
                    cex_change_4h=-12.0,
                    cex_confidence=0.95,
                    cex_vol_accel=1.2,
                    cex_score=12.0,
                    volume_24h=20e6,
                    is_low_liquidity=False,
                )
            ],
        )
        gen.generate(snap, empty_queue)
        daily = empty_queue.get_actionable(SignalPriority.DAILY)
        proposal = next(p for p in daily if p.strategy == "trending_momentum" and p.symbol == "PEPE/USDT")
        assert proposal.strength < 0.55

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

    def test_major_swing_generated_in_normal_regime(self, gen, empty_queue):
        """Long direction in normal regime should still allow major swings."""
        snap = IntelSnapshot(
            preferred_direction="long",
            regime="normal",
            fear_greed_bias="long",
            liquidation_bias="long",
            whale_bias="long",
            tv_btc_consensus="long",
        )
        gen.generate(snap, empty_queue)
        swing = empty_queue.get_actionable(SignalPriority.SWING)
        assert any(p.strategy == "major_swing" for p in swing)

    def test_custom_major_symbols(self, empty_queue):
        """SignalGenerator accepts custom major symbol set."""
        from services.signal_generator import SignalGenerator

        gen = SignalGenerator(major_symbols={"DOGE/USDT", "XRP/USDT"})
        gen.update_exchange_symbols({"BINANCE": self._TEST_SYMBOLS})
        snap = IntelSnapshot(
            hot_movers=[
                TrendingSnapshot(symbol="DOGE", change_24h=3.0, volume_24h=10e6, is_low_liquidity=False),
            ],
        )
        gen.generate(snap, empty_queue)
        daily = empty_queue.get_actionable(SignalPriority.DAILY)
        assert any(p.strategy == "major_momentum" and "DOGE" in p.symbol for p in daily)

    def test_filler_populates_queue_to_minimum(self, gen, empty_queue):
        """Filler ensures at least MIN_QUEUE_SIZE pending proposals."""
        snap = IntelSnapshot(
            hot_movers=[
                TrendingSnapshot(symbol=f"COIN{i}", change_24h=float(i + 1), volume_24h=10e6, is_low_liquidity=False)
                for i in range(15)
            ],
        )
        gen.generate(snap, empty_queue)
        assert empty_queue.pending_count >= gen.MIN_QUEUE_SIZE

    def test_filler_skips_low_liquidity(self, gen, empty_queue):
        """Filler does not propose low-liquidity coins."""
        snap = IntelSnapshot(
            hot_movers=[
                TrendingSnapshot(symbol="JUNK", change_24h=5.0, volume_24h=1e3, is_low_liquidity=True),
            ],
        )
        gen.generate(snap, empty_queue)
        daily = empty_queue.get_actionable(SignalPriority.DAILY)
        assert not any("JUNK" in p.symbol for p in daily)

    def test_filler_not_needed_when_queue_full(self, gen, empty_queue):
        """No filler proposals when queue already has enough pending."""
        for i in range(12):
            empty_queue.add(
                TradeProposal(
                    priority=SignalPriority.DAILY,
                    symbol=f"PRE{i}/USDT",
                    side="long",
                    strategy="pre_existing",
                    strength=0.7,
                    max_age_seconds=7200,
                )
            )
        snap = IntelSnapshot(
            hot_movers=[
                TrendingSnapshot(symbol="NEW", change_24h=3.0, volume_24h=10e6, is_low_liquidity=False),
            ],
        )
        gen.generate(snap, empty_queue)
        daily = empty_queue.get_actionable(SignalPriority.DAILY)
        assert not any(p.strategy == "trending_filler" for p in daily)

    def test_filler_uses_majors_as_fallback(self, empty_queue):
        """Filler falls back to major symbols when trending coins insufficient."""
        from services.signal_generator import SignalGenerator

        gen = SignalGenerator(
            major_symbols={
                "BTC/USDT",
                "ETH/USDT",
                "SOL/USDT",
                "XRP/USDT",
                "DOGE/USDT",
                "ADA/USDT",
                "AVAX/USDT",
                "LINK/USDT",
            }
        )
        gen.update_exchange_symbols({"BINANCE": self._TEST_SYMBOLS})
        snap = IntelSnapshot(
            hot_movers=[
                TrendingSnapshot(symbol="ONLY", change_24h=3.0, volume_24h=10e6, is_low_liquidity=False),
            ],
        )
        gen.generate(snap, empty_queue)
        daily = empty_queue.get_actionable(SignalPriority.DAILY)
        has_major_filler = any(p.strategy == "major_filler" for p in daily)
        assert has_major_filler

    def test_trending_momentum_lower_threshold(self, gen, empty_queue):
        """Trending momentum now triggers at 2% instead of 5%."""
        snap = IntelSnapshot(
            hot_movers=[
                TrendingSnapshot(symbol="LOW", change_24h=2.5, volume_24h=10e6, is_low_liquidity=False),
            ],
        )
        gen.generate(snap, empty_queue)
        daily = empty_queue.get_actionable(SignalPriority.DAILY)
        assert any(p.strategy == "trending_momentum" and "LOW" in p.symbol for p in daily)

    # --- Exchange-aware filtering ---

    def test_update_exchange_symbols(self, gen):
        """Exchange symbol sets update correctly."""
        gen.update_exchange_symbols({"BINANCE": {"BTC/USDT", "ETH/USDT"}, "BYBIT": {"BTC/USDT", "PEPE/USDT"}})
        assert gen._symbol_tradeable("BTC/USDT")
        assert gen._symbol_tradeable("PEPE/USDT")
        assert gen._symbol_tradeable("ETH/USDT")
        assert not gen._symbol_tradeable("FAKE/USDT")

    def test_supported_exchanges_tagging(self, gen):
        """Symbols are tagged with exchanges that have them."""
        gen.update_exchange_symbols({"BINANCE": {"BTC/USDT"}, "BYBIT": {"BTC/USDT", "PEPE/USDT"}})
        assert "BYBIT" in gen._supported_exchanges("PEPE/USDT")
        assert "BINANCE" not in gen._supported_exchanges("PEPE/USDT")
        assert sorted(gen._supported_exchanges("BTC/USDT")) == ["BINANCE", "BYBIT"]

    def test_propose_skips_untradeable_symbol(self, gen, empty_queue):
        """Proposals for symbols not on any exchange are dropped."""
        gen.update_exchange_symbols({"BINANCE": {"BTC/USDT", "ETH/USDT"}})
        prop = TradeProposal(
            priority=SignalPriority.DAILY,
            symbol="FAKE/USDT",
            side="long",
            strategy="test",
            source="monitor",
        )
        gen._propose(empty_queue, prop)
        assert empty_queue.total == 0

    def test_propose_tags_supported_exchanges(self, gen, empty_queue):
        """Proposals get tagged with exchanges that support the symbol."""
        gen.update_exchange_symbols({"BINANCE": {"BTC/USDT"}, "BYBIT": {"BTC/USDT", "SOL/USDT"}})
        prop = TradeProposal(
            priority=SignalPriority.DAILY,
            symbol="SOL/USDT",
            side="long",
            strategy="test",
            source="monitor",
        )
        gen._propose(empty_queue, prop)
        assert empty_queue.total == 1
        queued = empty_queue.proposals[0]
        assert "BYBIT" in queued.supported_exchanges
        assert "BINANCE" not in queued.supported_exchanges

    def test_propose_blocks_when_no_exchange_data(self, gen, empty_queue):
        """When no exchange data loaded, proposals are blocked (pessimistic)."""
        from services.signal_generator import SignalGenerator

        bare = SignalGenerator()
        prop = TradeProposal(
            priority=SignalPriority.DAILY,
            symbol="ANYTHING/USDT",
            side="long",
            strategy="test",
            source="monitor",
        )
        bare._propose(empty_queue, prop)
        assert empty_queue.total == 0

    def test_generate_filters_untradeable_extreme_movers(self, gen, empty_queue):
        """Extreme mover proposals for untradeable symbols are dropped."""
        gen.update_exchange_symbols({"BINANCE": {"BTC/USDT", "ETH/USDT"}})
        snap = IntelSnapshot(
            hot_movers=[
                TrendingSnapshot(symbol="FAKE", change_1h=12.0, volume_24h=10e6, is_low_liquidity=False),
            ],
        )
        gen.generate(snap, empty_queue)
        crit = empty_queue.get_actionable(SignalPriority.CRITICAL)
        assert not any("FAKE" in p.symbol for p in crit)

    def test_generate_allows_tradeable_extreme_movers(self, gen, empty_queue):
        """Extreme mover proposals for tradeable symbols pass through."""
        gen.update_exchange_symbols({"BINANCE": {"BTC/USDT", "PEPE/USDT"}})
        snap = IntelSnapshot(
            hot_movers=[
                TrendingSnapshot(symbol="PEPE", change_1h=10.0, volume_24h=10e6, is_low_liquidity=False),
            ],
        )
        gen.generate(snap, empty_queue)
        crit = empty_queue.get_actionable(SignalPriority.CRITICAL)
        assert any("PEPE" in p.symbol for p in crit)


# ── SignalGenerator Analytics Feedback ────────────────────────────────


class TestSignalGeneratorAnalytics:
    """Test that analytics data modulates proposal strength."""

    @pytest.fixture
    def gen(self):
        from services.signal_generator import SignalGenerator

        g = SignalGenerator()
        g.update_exchange_symbols({"BINANCE": TestSignalGenerator._TEST_SYMBOLS})
        return g

    @pytest.fixture
    def empty_queue(self):
        return TradeQueue()

    def _make_analytics(
        self,
        weights: list[StrategyWeightEntry] | None = None,
        patterns: list[dict] | None = None,
        suggestions: list[dict] | None = None,
    ) -> AnalyticsSnapshot:
        return AnalyticsSnapshot(
            weights=weights or [],
            patterns=patterns or [],
            suggestions=suggestions or [],
            total_trades_logged=50,
        )

    # --- update_analytics parsing ---

    def test_update_analytics_loads_weights(self, gen):
        snap = self._make_analytics(
            weights=[
                StrategyWeightEntry(strategy="compound_momentum", weight=0.5, streak=-3),
                StrategyWeightEntry(strategy="swing_opportunity", weight=1.3, streak=4),
            ]
        )
        gen.update_analytics(snap)
        assert gen._strategy_weights["compound_momentum"] == 0.5
        assert gen._strategy_weights["swing_opportunity"] == 1.3
        assert gen._strategy_streaks["compound_momentum"] == -3
        assert gen._strategy_streaks["swing_opportunity"] == 4

    def test_update_analytics_loads_regime_patterns(self, gen):
        snap = self._make_analytics(
            patterns=[{"pattern_type": "market_regime", "data": {"regime": "risk_off", "loss_rate": 0.8}}]
        )
        gen.update_analytics(snap)
        assert "risk_off" in gen._global_bad_regimes

    def test_update_analytics_loads_time_patterns(self, gen):
        snap = self._make_analytics(patterns=[{"pattern_type": "time_of_day", "data": {"hour": 14, "loss_rate": 0.8}}])
        gen.update_analytics(snap)
        assert 14 in gen._global_bad_hours

    def test_update_analytics_loads_combo_penalties(self, gen):
        snap = self._make_analytics(
            patterns=[
                {
                    "pattern_type": "strategy_symbol",
                    "affected_strategy": "compound_momentum",
                    "affected_symbol": "SOL/USDT",
                    "data": {"loss_rate": 0.8},
                }
            ]
        )
        gen.update_analytics(snap)
        assert ("compound_momentum", "SOL/USDT") in gen._combo_penalties
        assert gen._combo_penalties[("compound_momentum", "SOL/USDT")] == pytest.approx(0.2, abs=0.01)

    def test_update_analytics_loads_quick_trade_penalty(self, gen):
        snap = self._make_analytics(patterns=[{"pattern_type": "quick_trade", "data": {"loss_rate": 0.7}}])
        gen.update_analytics(snap)
        assert gen._quick_trade_penalty == pytest.approx(0.3, abs=0.01)

    def test_update_analytics_loads_per_strategy_regime_filter(self, gen):
        snap = self._make_analytics(
            suggestions=[
                {
                    "suggestion_type": "regime_filter",
                    "strategy": "swing_opportunity",
                    "suggested_value": "skip risk_off",
                }
            ]
        )
        gen.update_analytics(snap)
        assert "risk_off" in gen._strat_bad_regimes.get("swing_opportunity", set())

    def test_update_analytics_loads_per_strategy_time_filter(self, gen):
        snap = self._make_analytics(
            suggestions=[
                {
                    "suggestion_type": "time_filter",
                    "strategy": "compound_momentum",
                    "suggested_value": "skip hour 3",
                }
            ]
        )
        gen.update_analytics(snap)
        assert 3 in gen._strat_bad_hours.get("compound_momentum", set())

    def test_update_analytics_clears_previous(self, gen):
        """Calling update_analytics again fully replaces old data."""
        gen.update_analytics(
            self._make_analytics(
                weights=[StrategyWeightEntry(strategy="old_strat", weight=0.1)],
                patterns=[{"pattern_type": "market_regime", "data": {"regime": "caution"}}],
            )
        )
        assert "old_strat" in gen._strategy_weights
        assert "caution" in gen._global_bad_regimes

        gen.update_analytics(self._make_analytics(weights=[StrategyWeightEntry(strategy="new_strat", weight=1.5)]))
        assert "old_strat" not in gen._strategy_weights
        assert "new_strat" in gen._strategy_weights
        assert len(gen._global_bad_regimes) == 0

    # --- _analytics_strength_modifier ---

    def test_modifier_no_analytics_returns_1(self, gen):
        assert gen._analytics_strength_modifier("whatever", "BTC/USDT") == 1.0

    def test_modifier_uses_strategy_weight(self, gen):
        gen.update_analytics(
            self._make_analytics(weights=[StrategyWeightEntry(strategy="compound_momentum", weight=0.4)])
        )
        mod = gen._analytics_strength_modifier("trending_momentum", "BTC/USDT")
        assert mod < 1.0
        assert mod == pytest.approx(0.4, abs=0.01)

    def test_modifier_high_weight_boosts(self, gen):
        gen.update_analytics(
            self._make_analytics(weights=[StrategyWeightEntry(strategy="compound_momentum", weight=1.3)])
        )
        mod = gen._analytics_strength_modifier("trending_momentum", "BTC/USDT")
        assert mod == pytest.approx(1.3, abs=0.01)

    def test_modifier_losing_streak_stacks(self, gen):
        gen.update_analytics(
            self._make_analytics(weights=[StrategyWeightEntry(strategy="compound_momentum", weight=0.7, streak=-5)])
        )
        mod = gen._analytics_strength_modifier("trending_momentum", "BTC/USDT")
        assert mod < 0.7
        assert mod == pytest.approx(0.7 * 0.5, abs=0.01)

    def test_modifier_bad_regime_stacks(self, gen):
        gen.update_analytics(
            self._make_analytics(
                weights=[StrategyWeightEntry(strategy="compound_momentum", weight=1.0)],
                patterns=[{"pattern_type": "market_regime", "data": {"regime": "risk_off"}}],
            )
        )
        gen._current_regime = "risk_off"
        mod = gen._analytics_strength_modifier("trending_momentum", "BTC/USDT")
        assert mod == pytest.approx(0.7, abs=0.01)

    def test_modifier_per_strat_bad_regime_stacks(self, gen):
        gen.update_analytics(
            self._make_analytics(
                weights=[StrategyWeightEntry(strategy="swing_opportunity", weight=1.0)],
                suggestions=[
                    {
                        "suggestion_type": "regime_filter",
                        "strategy": "swing_opportunity",
                        "suggested_value": "skip caution",
                    }
                ],
            )
        )
        gen._current_regime = "caution"
        mod = gen._analytics_strength_modifier("capitulation_dip_buy", "BTC/USDT")
        assert mod == pytest.approx(0.6, abs=0.01)

    def test_modifier_bad_hour_stacks(self, gen):
        gen.update_analytics(
            self._make_analytics(
                weights=[StrategyWeightEntry(strategy="compound_momentum", weight=1.0)],
                patterns=[{"pattern_type": "time_of_day", "data": {"hour": 14}}],
            )
        )
        gen._current_hour = 14
        mod = gen._analytics_strength_modifier("trending_momentum", "BTC/USDT")
        assert mod == pytest.approx(0.75, abs=0.01)

    def test_modifier_combo_penalty(self, gen):
        gen.update_analytics(
            self._make_analytics(
                weights=[StrategyWeightEntry(strategy="compound_momentum", weight=1.0)],
                patterns=[
                    {
                        "pattern_type": "strategy_symbol",
                        "affected_strategy": "compound_momentum",
                        "affected_symbol": "SOL/USDT",
                        "data": {"loss_rate": 0.75},
                    }
                ],
            )
        )
        mod_sol = gen._analytics_strength_modifier("trending_momentum", "SOL/USDT")
        mod_btc = gen._analytics_strength_modifier("trending_momentum", "BTC/USDT")
        assert mod_sol < mod_btc
        assert mod_sol == pytest.approx(0.3, abs=0.01)
        assert mod_btc == pytest.approx(1.0, abs=0.01)

    def test_modifier_quick_trade_penalty(self, gen):
        gen.update_analytics(
            self._make_analytics(
                weights=[StrategyWeightEntry(strategy="compound_momentum", weight=1.0)],
                patterns=[{"pattern_type": "quick_trade", "data": {"loss_rate": 0.7}}],
            )
        )
        mod_quick = gen._analytics_strength_modifier("liq_reversal", "BTC/USDT", is_quick=True)
        mod_normal = gen._analytics_strength_modifier("trending_momentum", "BTC/USDT", is_quick=False)
        assert mod_quick < mod_normal
        assert mod_quick == pytest.approx(0.3, abs=0.01)

    def test_modifier_everything_stacks_to_floor(self, gen):
        """When all penalties stack, modifier bottoms out at 0.3."""
        gen.update_analytics(
            self._make_analytics(
                weights=[StrategyWeightEntry(strategy="compound_momentum", weight=0.15, streak=-6)],
                patterns=[
                    {"pattern_type": "market_regime", "data": {"regime": "risk_off"}},
                    {"pattern_type": "time_of_day", "data": {"hour": 3}},
                    {
                        "pattern_type": "strategy_symbol",
                        "affected_strategy": "compound_momentum",
                        "affected_symbol": "BTC/USDT",
                        "data": {"loss_rate": 0.9},
                    },
                    {"pattern_type": "quick_trade", "data": {"loss_rate": 0.8}},
                ],
            )
        )
        gen._current_regime = "risk_off"
        gen._current_hour = 3
        mod = gen._analytics_strength_modifier("liq_reversal", "BTC/USDT", is_quick=True)
        assert mod == 0.3

    def test_modifier_capped_at_1_5(self, gen):
        gen.update_analytics(
            self._make_analytics(weights=[StrategyWeightEntry(strategy="compound_momentum", weight=2.0)])
        )
        mod = gen._analytics_strength_modifier("trending_momentum", "BTC/USDT")
        assert mod == 1.5

    def test_openclaw_modifier_is_neutral_without_openclaw_data(self, gen):
        proposal = TradeProposal(
            priority=SignalPriority.DAILY,
            symbol="BTC/USDT",
            side="long",
            strategy="trending_momentum",
            strength=1.0,
            source="monitor",
        )
        assert gen._openclaw_strength_modifier(proposal) == 1.0

    def test_openclaw_modifier_boosts_aligned_and_penalizes_opposed_direction(self, gen):
        gen._openclaw_snapshot = IntelSnapshot(
            openclaw_regime="risk_on",
            openclaw_regime_confidence=0.9,
            openclaw_sentiment_score=75,
            openclaw_long_short_ratio=0.8,
        )
        long_prop = TradeProposal(
            priority=SignalPriority.DAILY,
            symbol="BTC/USDT",
            side="long",
            strategy="trending_momentum",
            strength=1.0,
            source="monitor",
        )
        short_prop = TradeProposal(
            priority=SignalPriority.DAILY,
            symbol="BTC/USDT",
            side="short",
            strategy="trending_momentum",
            strength=1.0,
            source="monitor",
        )
        long_mod = gen._openclaw_strength_modifier(long_prop)
        short_mod = gen._openclaw_strength_modifier(short_prop)
        assert long_mod > 1.0
        assert short_mod < 1.0

    def test_openclaw_idea_brief_boosts_matching_symbol_and_side(self, gen):
        gen._openclaw_snapshot = IntelSnapshot(
            openclaw_regime="risk_on",
            openclaw_regime_confidence=0.6,
            openclaw_sentiment_score=55,
            openclaw_idea_briefs=[{"symbol": "SOL/USDT", "side": "long", "confidence": 0.9}],
        )
        prop = TradeProposal(
            priority=SignalPriority.DAILY,
            symbol="SOL/USDT",
            side="long",
            strategy="trending_momentum",
            strength=1.0,
            source="monitor",
        )
        assert gen._openclaw_strength_modifier(prop) > 1.0

    # --- End-to-end: proposals get reduced strength ---

    def test_proposal_strength_reduced_by_analytics(self, gen, empty_queue):
        """A mass-liq proposal normally has 0.85 strength; with bad analytics it drops."""
        gen.update_analytics(
            self._make_analytics(weights=[StrategyWeightEntry(strategy="compound_momentum", weight=0.4, streak=-4)])
        )
        snap = IntelSnapshot(
            mass_liquidation=True,
            liquidation_24h=2e9,
            liquidation_bias="long",
        )
        gen.generate(snap, empty_queue)
        crit = empty_queue.get_actionable(SignalPriority.CRITICAL)
        liq = [p for p in crit if p.strategy == "liq_reversal"]
        assert len(liq) == 1
        assert liq[0].strength < 0.85
        assert liq[0].strength == pytest.approx(0.85 * 0.3, abs=0.01)

    def test_proposal_strength_boosted_by_analytics(self, gen, empty_queue):
        """A proposal for an outperforming strategy gets boosted strength."""
        gen.update_analytics(
            self._make_analytics(weights=[StrategyWeightEntry(strategy="compound_momentum", weight=1.4, streak=5)])
        )
        snap = IntelSnapshot(
            mass_liquidation=True,
            liquidation_24h=2e9,
            liquidation_bias="long",
        )
        gen.generate(snap, empty_queue)
        crit = empty_queue.get_actionable(SignalPriority.CRITICAL)
        liq = [p for p in crit if p.strategy == "liq_reversal"]
        assert len(liq) == 1
        assert liq[0].strength > 0.85

    def test_proposal_strength_unaffected_without_analytics(self, gen, empty_queue):
        """Without analytics loaded, strengths stay at raw values."""
        snap = IntelSnapshot(
            mass_liquidation=True,
            liquidation_24h=2e9,
            liquidation_bias="long",
        )
        gen.generate(snap, empty_queue)
        crit = empty_queue.get_actionable(SignalPriority.CRITICAL)
        liq = [p for p in crit if p.strategy == "liq_reversal"]
        assert len(liq) == 1
        assert liq[0].strength == pytest.approx(0.85, abs=0.01)

    def test_different_strategies_get_different_modifiers(self, gen, empty_queue):
        """Momentum and swing get different modifiers based on their analytics names."""
        gen.update_analytics(
            self._make_analytics(
                weights=[
                    StrategyWeightEntry(strategy="compound_momentum", weight=0.3),
                    StrategyWeightEntry(strategy="swing_opportunity", weight=1.3),
                ]
            )
        )
        snap = IntelSnapshot(
            fear_greed=10,
            mass_liquidation=True,
            liquidation_24h=3e9,
            liquidation_bias="long",
            preferred_direction="long",
            regime="risk_on",
            fear_greed_bias="long",
            whale_bias="long",
            tv_btc_consensus="long",
        )
        gen.generate(snap, empty_queue)

        crit = empty_queue.get_actionable(SignalPriority.CRITICAL)
        liq = [p for p in crit if p.strategy == "liq_reversal"]
        assert len(liq) == 1
        assert liq[0].strength < 0.5

        swing = empty_queue.get_actionable(SignalPriority.SWING)
        cap = [p for p in swing if p.strategy == "capitulation_dip_buy"]
        assert len(cap) == 1
        assert cap[0].strength > 0.9

    def test_generate_sets_current_regime_and_hour(self, gen, empty_queue):
        snap = IntelSnapshot(regime="caution")
        gen.generate(snap, empty_queue)
        assert gen._current_regime == "caution"
        assert gen._current_hour >= 0
