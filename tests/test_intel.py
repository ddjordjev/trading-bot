"""Tests for intel/ modules (mocked HTTP calls)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from intel.fear_greed import FearGreedClient
from intel.liquidations import LiquidationMonitor
from intel.macro_calendar import MacroCalendar
from intel.whale_sentiment import WhaleSentiment
from intel.tradingview import TradingViewClient
from intel.coinmarketcap import CoinMarketCapClient
from intel.coingecko import CoinGeckoClient


# ── FearGreedClient ─────────────────────────────────────────────────

class TestFearGreedClient:
    @pytest.fixture()
    def client(self):
        return FearGreedClient()

    def test_initial_state(self, client):
        assert client.value == 50
        assert client.latest is None

    def test_direction_bias_neutral(self, client):
        assert client.trade_direction_bias() == "neutral"

    def test_direction_bias_long_on_fear(self, client):
        from intel.fear_greed import FearGreedReading
        client._latest = FearGreedReading(
            value=15, classification="Extreme Fear",
            timestamp=datetime.now(timezone.utc))
        assert client.trade_direction_bias() == "long"
        assert client.is_extreme_fear is True

    def test_direction_bias_short_on_greed(self, client):
        from intel.fear_greed import FearGreedReading
        client._latest = FearGreedReading(
            value=80, classification="Extreme Greed",
            timestamp=datetime.now(timezone.utc))
        assert client.trade_direction_bias() == "short"
        assert client.is_extreme_greed is True

    def test_position_bias_extreme_fear(self, client):
        from intel.fear_greed import FearGreedReading
        client._latest = FearGreedReading(
            value=5, classification="Extreme Fear",
            timestamp=datetime.now(timezone.utc))
        assert client.position_bias() == 1.4

    def test_position_bias_greed(self, client):
        from intel.fear_greed import FearGreedReading
        client._latest = FearGreedReading(
            value=65, classification="Greed",
            timestamp=datetime.now(timezone.utc))
        assert client.position_bias() == 0.8

    def test_position_bias_extreme_greed(self, client):
        from intel.fear_greed import FearGreedReading
        client._latest = FearGreedReading(
            value=80, classification="Extreme Greed",
            timestamp=datetime.now(timezone.utc))
        assert client.position_bias() == 0.6

    def test_is_fear(self, client):
        from intel.fear_greed import FearGreedReading
        client._latest = FearGreedReading(
            value=35, classification="Fear",
            timestamp=datetime.now(timezone.utc))
        assert client.is_fear is True
        assert client.is_greed is False

    def test_is_greed(self, client):
        from intel.fear_greed import FearGreedReading
        client._latest = FearGreedReading(
            value=65, classification="Greed",
            timestamp=datetime.now(timezone.utc))
        assert client.is_greed is True


# ── LiquidationMonitor ─────────────────────────────────────────────

class TestLiquidationMonitor:
    @pytest.fixture()
    def monitor(self):
        return LiquidationMonitor()

    def test_initial_state(self, monitor):
        assert monitor.latest is None

    def test_not_reversal_zone_initially(self, monitor):
        assert monitor.is_reversal_zone() is False

    def test_reversal_bias_neutral(self, monitor):
        assert monitor.reversal_bias() == "neutral"


# ── MacroCalendar ───────────────────────────────────────────────────

class TestMacroCalendar:
    @pytest.fixture()
    def cal(self):
        return MacroCalendar()

    def test_initial_state(self, cal):
        assert cal.has_imminent_event() is False
        assert cal.exposure_multiplier() == 1.0

    def test_spike_opportunity_initially_false(self, cal):
        assert cal.is_spike_opportunity() is False

    def test_upcoming_events_empty(self, cal):
        assert cal.upcoming_events == []

    def test_next_event_info_none(self, cal):
        assert cal.next_event_info() is None


# ── WhaleSentiment ──────────────────────────────────────────────────

class TestWhaleSentiment:
    @pytest.fixture()
    def ws(self):
        return WhaleSentiment(symbols=["BTC", "ETH"])

    def test_initial_state(self, ws):
        assert ws.contrarian_bias("BTC") == "neutral"

    def test_get_no_data(self, ws):
        assert ws.get("BTC") is None

    def test_should_avoid_no_data(self, ws):
        assert ws.should_avoid_longs("BTC") is False
        assert ws.should_avoid_shorts("BTC") is False


# ── TradingViewClient ──────────────────────────────────────────────

class TestTradingViewClient:
    @pytest.fixture()
    def client(self):
        return TradingViewClient(exchange="MEXC", intervals=["1h", "4h"])

    def test_initial_state(self, client):
        assert client.exchange == "MEXC"
        assert client.intervals == ["1h", "4h"]

    def test_signal_boost_no_data(self, client):
        boost = client.signal_boost("BTC/USDT", "long")
        assert boost == 1.0

    def test_consensus_no_data(self, client):
        direction = client.consensus("BTC/USDT")
        assert direction == "no_data"


# ── CoinMarketCapClient ────────────────────────────────────────────

class TestCoinMarketCapClient:
    @pytest.fixture()
    def client(self):
        return CoinMarketCapClient(api_key="")

    def test_initial_state(self, client):
        assert client._trending == []

    @property
    def trending(self):
        return []


# ── CoinGeckoClient ────────────────────────────────────────────────

class TestCoinGeckoClient:
    @pytest.fixture()
    def client(self):
        return CoinGeckoClient(api_key="")

    def test_initial_state(self, client):
        assert client._trending == []
