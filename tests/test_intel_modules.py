"""Comprehensive tests for intel/ modules (mocked HTTP, no external calls)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from intel.coingecko import CoinGeckoClient, GeckoCoin
from intel.coinmarketcap import CMCCoin, CoinMarketCapClient
from intel.defillama import DeFiLlamaClient, TVLSnapshot
from intel.fear_greed import FearGreedClient, FearGreedReading
from intel.liquidations import LiquidationMonitor, LiquidationSnapshot
from intel.macro_calendar import (
    EventImpact,
    MacroCalendar,
    MacroEvent,
)
from intel.market_intel import MarketCondition, MarketIntel, MarketRegime
from intel.santiment import SantimentClient, SocialData
from intel.tradingview import (
    TradingViewClient,
    TVAnalysis,
    TVRating,
)
from intel.whale_sentiment import (
    OISnapshot,
    WhaleSentiment,
    WhaleSentimentData,
)

# ── DeFiLlama ───────────────────────────────────────────────────────


class TestTVLSnapshot:
    def test_defaults(self):
        s = TVLSnapshot()
        assert s.total_tvl == 0.0
        assert s.tvl_24h_change_pct == 0.0
        assert s.top_gaining_chains == []
        assert s.top_losing_chains == []


class TestDeFiLlamaClient:
    @pytest.fixture
    def client(self):
        return DeFiLlamaClient(poll_interval=600)

    def test_initial_state(self, client):
        assert client.poll_interval == 600
        assert client._running is False
        assert client._prev_tvl == 0.0
        assert client.snapshot.total_tvl == 0.0

    def test_tvl_trend_stable(self, client):
        client._data = TVLSnapshot(tvl_24h_change_pct=0.0)
        assert client.tvl_trend == "stable"
        client._data = TVLSnapshot(tvl_24h_change_pct=1.5)
        assert client.tvl_trend == "stable"

    def test_tvl_trend_growing(self, client):
        client._data = TVLSnapshot(tvl_24h_change_pct=3.0)
        assert client.tvl_trend == "growing"

    def test_tvl_trend_shrinking(self, client):
        client._data = TVLSnapshot(tvl_24h_change_pct=-3.0)
        assert client.tvl_trend == "shrinking"

    def test_capital_flowing_to(self, client):
        client._data = TVLSnapshot(top_gaining_chains=["Ethereum", "Solana"])
        assert client.capital_flowing_to == ["Ethereum", "Solana"]

    def test_position_bias_growing(self, client):
        client._data = TVLSnapshot(tvl_24h_change_pct=5.0)
        assert client.position_bias() == 1.1

    def test_position_bias_shrinking(self, client):
        client._data = TVLSnapshot(tvl_24h_change_pct=-5.0)
        assert client.position_bias() == 0.85

    def test_position_bias_stable(self, client):
        client._data = TVLSnapshot(tvl_24h_change_pct=0.0)
        assert client.position_bias() == 1.0

    def test_summary_no_data(self, client):
        assert client.summary() == "DeFiLlama: no data"

    def test_summary_with_data(self, client):
        client._data = TVLSnapshot(
            total_tvl=50e9,
            tvl_24h_change_pct=2.5,
            top_gaining_chains=["Ethereum"],
            top_losing_chains=["BSC"],
        )
        out = client.summary()
        assert "50.0B" in out
        assert "growing" in out
        assert "Ethereum" in out
        assert "BSC" in out

    @pytest.mark.asyncio
    async def test_fetch_updates_snapshot(self, client):
        chains = [
            {"name": "Ethereum", "tvl": 30e9, "change_1d": 2.0},
            {"name": "Solana", "tvl": 5e9, "change_1d": -1.0},
        ]
        with patch.object(client, "_fetch_chains", new_callable=AsyncMock, return_value=chains):
            await client._fetch()
        assert client._data.total_tvl == 35e9
        assert "Ethereum" in client._data.top_gaining_chains
        assert "Solana" in client._data.top_losing_chains

    @pytest.mark.asyncio
    @patch("intel.defillama.aiohttp.ClientSession")
    async def test_fetch_chains_empty_on_non_200(self, mock_session_class, client):
        mock_resp = AsyncMock()
        mock_resp.status = 404
        mock_get = MagicMock()
        mock_get.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_get.__aexit__ = AsyncMock(return_value=None)
        mock_sess = AsyncMock()
        mock_sess.get.return_value = mock_get
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await client._fetch_chains(mock_sess)
        assert result == []


# ── Santiment ────────────────────────────────────────────────────────


class TestSocialData:
    def test_is_social_spike_false_no_avg(self):
        d = SocialData(social_volume=100, social_volume_avg=0)
        assert d.is_social_spike is False

    def test_is_social_spike_false_below_2x(self):
        d = SocialData(social_volume=150, social_volume_avg=100)
        assert d.is_social_spike is False

    def test_is_social_spike_true(self):
        d = SocialData(social_volume=250, social_volume_avg=100)
        assert d.is_social_spike is True

    def test_sentiment_signal_bearish_extreme_spike(self):
        d = SocialData(social_volume=350, social_volume_avg=100)
        assert d.sentiment_signal == "bearish"

    def test_sentiment_signal_bullish_spike(self):
        d = SocialData(social_volume=250, social_volume_avg=100)
        assert d.sentiment_signal == "bullish"

    def test_sentiment_signal_neutral(self):
        d = SocialData(social_volume=50, social_volume_avg=100)
        assert d.sentiment_signal == "neutral"


class TestSantimentClient:
    @pytest.fixture
    def client(self):
        return SantimentClient(symbols=["bitcoin", "ethereum"], api_key="")

    def test_initial_state(self, client):
        assert client.symbols == ["bitcoin", "ethereum"]
        assert client.get("BTC") is None

    def test_get_missing_returns_none(self, client):
        assert client.get("UNKNOWN") is None

    def test_get_with_data(self, client):
        client._data["bitcoin"] = SocialData(social_volume=100)
        assert client.get("BTC").social_volume == 100

    def test_sentiment_signal_no_data(self, client):
        assert client.sentiment_signal("BTC") == "neutral"

    def test_sentiment_signal_with_data(self, client):
        # Bearish requires social_volume > social_volume_avg * 3 (extreme spike)
        client._data["bitcoin"] = SocialData(social_volume=350, social_volume_avg=100)
        assert client.sentiment_signal("BTC") == "bearish"

    def test_is_social_spike_no_data(self, client):
        assert client.is_social_spike("BTC") is False

    def test_position_bias_no_btc(self, client):
        assert client.position_bias() == 1.0

    def test_position_bias_bearish(self, client):
        client._data["bitcoin"] = SocialData(social_volume=400, social_volume_avg=100)
        assert client.position_bias() == 0.7

    def test_position_bias_social_spike(self, client):
        client._data["bitcoin"] = SocialData(social_volume=250, social_volume_avg=100)
        assert client.position_bias() == 1.1

    def test_position_bias_neutral(self, client):
        client._data["bitcoin"] = SocialData(social_volume=50, social_volume_avg=100)
        assert client.position_bias() == 1.0

    def test_to_slug(self, client):
        assert client._to_slug("BTC") == "bitcoin"
        assert client._to_slug("ETH") == "ethereum"
        assert client._to_slug("BTC/USDT") == "bitcoin"
        assert client._to_slug("unknown") == "unknown"

    def test_summary_empty(self, client):
        assert client.summary() == "Santiment: no data"

    def test_summary_with_data(self, client):
        # SPIKE appears when is_social_spike (social_volume > social_volume_avg * 2)
        client._data["bitcoin"] = SocialData(social_volume=250, social_volume_avg=100)
        out = client.summary()
        assert "bitcoin" in out
        assert "SPIKE" in out


# ── CoinGecko ────────────────────────────────────────────────────────


class TestGeckoCoin:
    def test_trading_pair(self):
        c = GeckoCoin(symbol="btc")
        assert c.trading_pair == "BTC/USDT"

    def test_is_near_ath_true(self):
        c = GeckoCoin(symbol="btc", ath_change_pct=-3.0)
        assert c.is_near_ath is True

    def test_is_near_ath_false(self):
        c = GeckoCoin(symbol="btc", ath_change_pct=-10.0)
        assert c.is_near_ath is False

    def test_is_heavily_discounted_true(self):
        c = GeckoCoin(symbol="btc", ath_change_pct=-85.0)
        assert c.is_heavily_discounted is True

    def test_is_heavily_discounted_false(self):
        c = GeckoCoin(symbol="btc", ath_change_pct=-50.0)
        assert c.is_heavily_discounted is False

    def test_recent_trend_unknown_short_sparkline(self):
        c = GeckoCoin(symbol="btc", sparkline_7d=[1.0] * 5)
        assert c.recent_trend == "unknown"

    def test_recent_trend_up(self):
        # recent avg > older avg by >3%
        older = [100.0] * 24
        recent = [105.0] * 24
        c = GeckoCoin(symbol="btc", sparkline_7d=older + recent)
        assert c.recent_trend == "up"

    def test_recent_trend_down(self):
        older = [100.0] * 24
        recent = [95.0] * 24
        c = GeckoCoin(symbol="btc", sparkline_7d=older + recent)
        assert c.recent_trend == "down"

    def test_recent_trend_flat(self):
        c = GeckoCoin(symbol="btc", sparkline_7d=[100.0] * 60)
        assert c.recent_trend == "flat"

    def test_recent_trend_unknown_zero_older(self):
        c = GeckoCoin(symbol="btc", sparkline_7d=[0.0] * 50)
        assert c.recent_trend == "unknown"


class TestCoinGeckoClient:
    @pytest.fixture
    def client(self):
        return CoinGeckoClient(api_key="")

    def test_initial_state(self, client):
        assert client._trending == []
        assert client.trending == []
        assert client.top_volume == []
        assert client.top_gainers == []

    def test_base_url_free(self, client):
        assert client._base_url == client.BASE_URL

    def test_base_url_pro(self):
        c = CoinGeckoClient(api_key="key")
        assert c._base_url == c.PRO_URL

    def test_params_with_key(self):
        c = CoinGeckoClient(api_key="key")
        p = c._params()
        assert "x_cg_pro_api_key" in p
        p2 = c._params({"extra": "v"})
        assert p2["extra"] == "v"

    def test_all_interesting_dedup(self, client):
        client._trending = [GeckoCoin(symbol="btc"), GeckoCoin(symbol="eth")]
        client._top_gainers = [GeckoCoin(symbol="btc"), GeckoCoin(symbol="sol")]
        client._top_by_volume = [GeckoCoin(symbol="btc")]
        result = client.all_interesting
        symbols = [c.symbol for c in result]
        assert "btc" in symbols
        assert symbols.count("btc") == 1

    def test_find_by_symbol_not_found(self, client):
        assert client.find_by_symbol("XYZ") is None

    def test_find_by_symbol_found(self, client):
        client._top_by_volume = [GeckoCoin(symbol="btc", name="Bitcoin")]
        c = client.find_by_symbol("BTC/USDT")
        assert c is not None
        assert c.symbol == "btc"

    def test_summary_no_data(self, client):
        assert client.summary() == "CoinGecko: no data"

    def test_summary_with_data(self, client):
        client._trending = [GeckoCoin(symbol="btc")]
        client._top_gainers = [GeckoCoin(symbol="eth", change_24h=10.0)]
        out = client.summary()
        assert "trending" in out
        assert "movers" in out


# ── CoinMarketCap ───────────────────────────────────────────────────


class TestCMCCoin:
    def test_trading_pair(self):
        c = CMCCoin(symbol="btc")
        assert c.trading_pair == "BTC/USDT"

    def test_is_tradable_size_true(self):
        c = CMCCoin(symbol="btc", volume_24h=2e6, market_cap=20e6)
        assert c.is_tradable_size is True

    def test_is_tradable_size_false_volume(self):
        c = CMCCoin(symbol="btc", volume_24h=500_000, market_cap=20e6)
        assert c.is_tradable_size is False

    def test_is_tradable_size_false_mcap(self):
        c = CMCCoin(symbol="btc", volume_24h=2e6, market_cap=5e6)
        assert c.is_tradable_size is False


class TestCoinMarketCapClient:
    @pytest.fixture
    def client(self):
        return CoinMarketCapClient(api_key="")

    def test_initial_state(self, client):
        assert client.trending == []
        assert client.gainers == []
        assert client.losers == []
        assert client.recently_added == []

    def test_headers_without_key(self, client):
        h = client._headers()
        assert "X-CMC_PRO_API_KEY" not in h
        assert "Accept" in h

    def test_headers_with_key(self):
        c = CoinMarketCapClient(api_key="key")
        h = c._headers()
        assert h["X-CMC_PRO_API_KEY"] == "key"

    def test_all_interesting_dedup(self, client):
        client._trending = [CMCCoin(symbol="BTC")]
        client._gainers = [CMCCoin(symbol="BTC"), CMCCoin(symbol="ETH")]
        client._recently_added = [CMCCoin(symbol="BTC")]
        result = client.all_interesting
        assert len([c for c in result if c.symbol == "BTC"]) == 1

    def test_parse_spotlight_coin_valid(self, client):
        item = {
            "id": 1,
            "symbol": "BTC",
            "name": "Bitcoin",
            "slug": "bitcoin",
            "priceChange": {
                "price": 50000,
                "priceChange24h": 2.5,
                "volume24h": 1e9,
                "marketCap": 1e12,
            },
            "cmcRank": 1,
        }
        c = CoinMarketCapClient._parse_spotlight_coin(item)
        assert c is not None
        assert c.symbol == "BTC"
        assert c.change_24h == 2.5

    def test_parse_spotlight_coin_invalid(self, client):
        # Empty or minimal dict returns CMCCoin with defaults (no KeyError/TypeError)
        c = CoinMarketCapClient._parse_spotlight_coin({})
        assert c is not None
        assert c.symbol == ""
        c2 = CoinMarketCapClient._parse_spotlight_coin({"symbol": "BTC"})
        assert c2 is not None
        assert c2.symbol == "BTC"

    def test_parse_api_coin_valid(self, client):
        item = {
            "id": 1,
            "symbol": "BTC",
            "name": "Bitcoin",
            "slug": "bitcoin",
            "quote": {
                "USD": {
                    "price": 50000,
                    "volume_24h": 1e9,
                    "market_cap": 1e12,
                    "percent_change_1h": 1.0,
                    "percent_change_24h": 2.0,
                    "percent_change_7d": 5.0,
                }
            },
            "cmc_rank": 1,
        }
        c = CoinMarketCapClient._parse_api_coin(item)
        assert c is not None
        assert c.symbol == "BTC"
        assert c.change_24h == 2.0

    def test_parse_api_coin_invalid(self, client):
        # Empty dict returns CMCCoin with defaults (no exception)
        c = CoinMarketCapClient._parse_api_coin({})
        assert c is not None
        assert c.symbol == ""

    def test_summary_empty(self, client):
        assert client.summary() == "CMC: no data"

    def test_summary_with_data(self, client):
        client._trending = [CMCCoin(symbol="BTC")]
        client._gainers = [CMCCoin(symbol="ETH", change_24h=10.0)]
        client._losers = [CMCCoin(symbol="SOL", change_24h=-8.0)]
        client._recently_added = [CMCCoin(symbol="NEW")]
        out = client.summary()
        assert "trending" in out
        assert "gainers" in out
        assert "losers" in out
        assert "new" in out


# ── TradingView ─────────────────────────────────────────────────────


class TestTVAnalysis:
    def test_is_strong_signal_true(self):
        a = TVAnalysis(symbol="BTC", summary_rating=TVRating.STRONG_BUY)
        assert a.is_strong_signal is True
        a = TVAnalysis(symbol="BTC", summary_rating=TVRating.STRONG_SELL)
        assert a.is_strong_signal is True

    def test_is_strong_signal_false(self):
        a = TVAnalysis(symbol="BTC", summary_rating=TVRating.BUY)
        assert a.is_strong_signal is False

    def test_signal_direction_long(self):
        a = TVAnalysis(symbol="BTC", summary_rating=TVRating.BUY)
        assert a.signal_direction == "long"
        a = TVAnalysis(symbol="BTC", summary_rating=TVRating.STRONG_BUY)
        assert a.signal_direction == "long"

    def test_signal_direction_short(self):
        a = TVAnalysis(symbol="BTC", summary_rating=TVRating.SELL)
        assert a.signal_direction == "short"

    def test_signal_direction_neutral(self):
        a = TVAnalysis(symbol="BTC", summary_rating=TVRating.NEUTRAL)
        assert a.signal_direction == "neutral"

    def test_confidence_zero_signals(self):
        a = TVAnalysis(symbol="BTC", total_signals=0)
        assert a.confidence == 0.0

    def test_confidence_dominant(self):
        a = TVAnalysis(
            symbol="BTC",
            buy_count=8,
            sell_count=1,
            neutral_count=1,
            total_signals=10,
        )
        assert a.confidence == 0.8

    def test_trend_aligned_true(self):
        a = TVAnalysis(
            symbol="BTC",
            oscillators_rating=TVRating.BUY,
            moving_averages_rating=TVRating.BUY,
        )
        assert a.trend_aligned is True

    def test_trend_aligned_strong_still_aligned(self):
        a = TVAnalysis(
            symbol="BTC",
            oscillators_rating=TVRating.STRONG_BUY,
            moving_averages_rating=TVRating.BUY,
        )
        assert a.trend_aligned is True


class TestTradingViewClient:
    @pytest.fixture
    def client(self):
        return TradingViewClient(exchange="MEXC", intervals=["1h", "4h"])

    def test_initial_state(self, client):
        assert client.exchange == "MEXC"
        assert client.intervals == ["1h", "4h"]
        assert client.consensus("BTC/USDT") == "no_data"
        assert client.signal_boost("BTC/USDT", "long") == 1.0

    def test_score_to_rating_strong_buy(self):
        assert TradingViewClient._score_to_rating(0.6) == TVRating.STRONG_BUY
        assert TradingViewClient._score_to_rating(0.5) == TVRating.STRONG_BUY

    def test_score_to_rating_buy(self):
        assert TradingViewClient._score_to_rating(0.2) == TVRating.BUY
        assert TradingViewClient._score_to_rating(0.1) == TVRating.BUY

    def test_score_to_rating_neutral(self):
        assert TradingViewClient._score_to_rating(0.0) == TVRating.NEUTRAL
        assert TradingViewClient._score_to_rating(-0.05) == TVRating.NEUTRAL

    def test_score_to_rating_sell(self):
        # SELL is score in (-0.5, -0.1]; -0.5 is STRONG_SELL
        assert TradingViewClient._score_to_rating(-0.2) == TVRating.SELL
        assert TradingViewClient._score_to_rating(-0.4) == TVRating.SELL

    def test_score_to_rating_strong_sell(self):
        assert TradingViewClient._score_to_rating(-0.5) == TVRating.STRONG_SELL
        assert TradingViewClient._score_to_rating(-0.6) == TVRating.STRONG_SELL

    def test_consensus_long(self, client):
        client._cache["BTC/USDT"] = {
            "1h": TVAnalysis(symbol="BTC", summary_rating=TVRating.BUY),
            "4h": TVAnalysis(symbol="BTC", summary_rating=TVRating.BUY),
        }
        assert client.consensus("BTC/USDT") == "long"

    def test_consensus_short(self, client):
        client._cache["BTC/USDT"] = {
            "1h": TVAnalysis(symbol="BTC", summary_rating=TVRating.SELL),
            "4h": TVAnalysis(symbol="BTC", summary_rating=TVRating.SELL),
        }
        assert client.consensus("BTC/USDT") == "short"

    def test_consensus_neutral(self, client):
        client._cache["BTC/USDT"] = {
            "1h": TVAnalysis(symbol="BTC", summary_rating=TVRating.BUY),
            "4h": TVAnalysis(symbol="BTC", summary_rating=TVRating.SELL),
        }
        assert client.consensus("BTC/USDT") == "neutral"

    def test_signal_boost_agrees_high_confidence(self, client):
        # trend_aligned=False so boost stays 1.1 (no +0.1)
        a = TVAnalysis(
            symbol="BTC",
            summary_rating=TVRating.BUY,
            oscillators_rating=TVRating.SELL,
            moving_averages_rating=TVRating.BUY,
            buy_count=7,
            sell_count=0,
            neutral_count=3,
            total_signals=10,
        )
        client._cache["BTC/USDT"] = {"1h": a}
        assert client.signal_boost("BTC/USDT", "long") == 1.1

    def test_signal_boost_agrees_strong_signal(self, client):
        # trend_aligned=False so boost stays 1.2 (no +0.1)
        a = TVAnalysis(
            symbol="BTC",
            summary_rating=TVRating.STRONG_BUY,
            oscillators_rating=TVRating.SELL,
            moving_averages_rating=TVRating.BUY,
            buy_count=5,
            sell_count=0,
            neutral_count=0,
            total_signals=5,
        )
        client._cache["BTC/USDT"] = {"1h": a}
        assert client.signal_boost("BTC/USDT", "long") == 1.2

    def test_signal_boost_agrees_trend_aligned(self, client):
        a = TVAnalysis(
            symbol="BTC",
            summary_rating=TVRating.BUY,
            oscillators_rating=TVRating.BUY,
            moving_averages_rating=TVRating.BUY,
            buy_count=6,
            sell_count=0,
            neutral_count=4,
            total_signals=10,
        )
        client._cache["BTC/USDT"] = {"1h": a}
        boost = client.signal_boost("BTC/USDT", "long")
        assert boost >= 1.1  # 1.1 + 0.1 = 1.2, capped 1.3

    def test_signal_boost_disagrees_strong(self, client):
        a = TVAnalysis(
            symbol="BTC",
            summary_rating=TVRating.STRONG_SELL,
            buy_count=0,
            sell_count=8,
            neutral_count=0,
            total_signals=8,
        )
        client._cache["BTC/USDT"] = {"1h": a}
        assert client.signal_boost("BTC/USDT", "long") == 0.7

    def test_signal_boost_disagrees_not_strong(self, client):
        a = TVAnalysis(
            symbol="BTC",
            summary_rating=TVRating.SELL,
            buy_count=0,
            sell_count=5,
            neutral_count=5,
            total_signals=10,
        )
        client._cache["BTC/USDT"] = {"1h": a}
        assert client.signal_boost("BTC/USDT", "long") == 0.85

    def test_signal_boost_neutral_proposed(self, client):
        a = TVAnalysis(symbol="BTC", summary_rating=TVRating.NEUTRAL)
        client._cache["BTC/USDT"] = {"1h": a}
        assert client.signal_boost("BTC/USDT", "long") == 1.0

    def test_signal_boost_no_1h_uses_1_0(self, client):
        client._cache["BTC/USDT"] = {"4h": TVAnalysis(symbol="BTC")}
        assert client.signal_boost("BTC/USDT", "long") == 1.0

    def test_get_cached(self, client):
        a = TVAnalysis(symbol="BTC")
        client._cache["BTC"] = {"1h": a}
        assert client.get_cached("BTC", "1h") is a
        assert client.get_cached("BTC", "4h") is None
        assert client.get_cached("ETH", "1h") is None

    def test_summary_for_symbol_no_data(self, client):
        assert "no data" in client.summary("BTC")

    def test_summary_for_symbol_with_data(self, client):
        client._cache["BTC"] = {
            "1h": TVAnalysis(symbol="BTC", summary_rating=TVRating.BUY),
        }
        out = client.summary("BTC")
        assert "BTC" in out
        assert "BUY" in out

    def test_summary_global_no_cache(self, client):
        assert "no data" in client.summary("")

    def test_summary_global_with_cache(self, client):
        client._cache["BTC"] = {"1h": TVAnalysis(symbol="BTC")}
        out = client.summary("")
        assert "TradingView" in out
        assert "BTC" in out

    @pytest.mark.asyncio
    async def test_analyze_parses_response(self, client):
        # Mock at the point of use: session.post() as resp so analyze() gets JSON and parses it
        payload_data = {
            "data": [
                {
                    "d": [
                        0.2,  # recommend_all -> BUY
                        0.1,  # recommend_osc
                        -0.1,  # recommend_ma -> NEUTRAL
                        55.0,  # rsi
                        54.0,
                        0.5,
                        0.3,  # macd_signal
                        25.0,  # adx
                        1000.0,  # atr
                        50000.0,  # ema20
                        49500.0,  # sma50
                        51000.0,  # sma200
                        52000.0,  # bb_upper
                        48000.0,  # bb_lower
                        1,
                        -1,
                        0,
                        1,
                        0,  # extra oscillator votes (buy, sell, neutral...)
                    ]
                }
            ]
        }
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=payload_data)
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=None)
        mock_sess = MagicMock()
        mock_sess.post.return_value = mock_cm
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("intel.tradingview.aiohttp.ClientSession", return_value=mock_session):
            result = await client.analyze("BTC/USDT", "1h")
        assert result is not None
        assert result.symbol == "BTC/USDT"
        assert result.summary_rating == TVRating.BUY
        assert result.rsi_14 == 55.0
        assert result.ema_20 == 50000.0
        assert client.get_cached("BTC/USDT", "1h") is result

    @pytest.mark.asyncio
    @patch("intel.tradingview.aiohttp.ClientSession")
    async def test_analyze_empty_data_returns_none(self, mock_session_class, client):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"data": []})
        mock_post = MagicMock()
        mock_post.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post.__aexit__ = AsyncMock(return_value=None)
        mock_sess = AsyncMock()
        mock_sess.post.return_value = mock_post
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await client.analyze("BTC/USDT", "1h")
        assert result is None


# ── Liquidations ────────────────────────────────────────────────────


class TestLiquidationSnapshot:
    def test_long_ratio_24h_zero_total(self):
        s = LiquidationSnapshot(total_24h=0)
        assert s.long_ratio_24h == 0.5

    def test_long_ratio_24h(self):
        s = LiquidationSnapshot(total_24h=100, long_24h=60, short_24h=40)
        assert s.long_ratio_24h == 0.6

    def test_is_mass_liquidation_true(self):
        s = LiquidationSnapshot(total_24h=1_500_000_000)
        assert s.is_mass_liquidation is True

    def test_is_mass_liquidation_false(self):
        s = LiquidationSnapshot(total_24h=500_000_000)
        assert s.is_mass_liquidation is False

    def test_is_heavy_liquidation_true(self):
        s = LiquidationSnapshot(total_24h=600_000_000)
        assert s.is_heavy_liquidation is True

    def test_is_heavy_liquidation_false(self):
        s = LiquidationSnapshot(total_24h=100_000_000)
        assert s.is_heavy_liquidation is False

    def test_dominant_side_longs(self):
        s = LiquidationSnapshot(total_24h=100, long_24h=70, short_24h=30)
        assert s.dominant_side == "longs"

    def test_dominant_side_shorts(self):
        s = LiquidationSnapshot(total_24h=100, long_24h=30, short_24h=70)
        assert s.dominant_side == "shorts"

    def test_dominant_side_balanced(self):
        s = LiquidationSnapshot(total_24h=100, long_24h=50, short_24h=50)
        assert s.dominant_side == "balanced"


class TestLiquidationMonitor:
    @pytest.fixture
    def monitor(self):
        return LiquidationMonitor(poll_interval=300)

    def test_initial_state(self, monitor):
        assert monitor.latest is None
        assert monitor.is_reversal_zone() is False
        assert monitor.reversal_bias() == "neutral"
        assert monitor.aggression_boost() == 1.0

    def test_is_reversal_zone_true(self, monitor):
        monitor._latest = LiquidationSnapshot(total_24h=1_200_000_000)
        assert monitor.is_reversal_zone() is True

    def test_reversal_bias_heavy_longs(self, monitor):
        monitor._latest = LiquidationSnapshot(total_24h=600_000_000, long_24h=400_000_000, short_24h=200_000_000)
        assert monitor.reversal_bias() == "long"

    def test_reversal_bias_heavy_shorts(self, monitor):
        monitor._latest = LiquidationSnapshot(total_24h=600_000_000, long_24h=200_000_000, short_24h=400_000_000)
        assert monitor.reversal_bias() == "short"

    def test_reversal_bias_balanced(self, monitor):
        monitor._latest = LiquidationSnapshot(total_24h=600_000_000, long_24h=300_000_000, short_24h=300_000_000)
        assert monitor.reversal_bias() == "neutral"

    def test_aggression_boost_mass(self, monitor):
        monitor._latest = LiquidationSnapshot(total_24h=1_500_000_000)
        assert monitor.aggression_boost() == 1.3

    def test_aggression_boost_heavy(self, monitor):
        monitor._latest = LiquidationSnapshot(total_24h=600_000_000)
        assert monitor.aggression_boost() == 1.1

    def test_summary_no_data(self, monitor):
        assert monitor.summary() == "Liquidations: no data"

    def test_summary_with_data(self, monitor):
        monitor._latest = LiquidationSnapshot(total_24h=500_000_000, long_24h=300_000_000, short_24h=200_000_000)
        out = monitor.summary()
        assert "500" in out or "M" in out
        assert "longs" in out or "L:" in out


# ── MacroCalendar ───────────────────────────────────────────────────


class TestMacroEvent:
    def test_is_crypto_mover_high(self):
        e = MacroEvent(title="CPI", date=datetime.now(UTC), impact=EventImpact.HIGH)
        assert e.is_crypto_mover is True

    def test_is_crypto_mover_critical(self):
        e = MacroEvent(title="FOMC", date=datetime.now(UTC), impact=EventImpact.CRITICAL)
        assert e.is_crypto_mover is True

    def test_is_crypto_mover_low(self):
        e = MacroEvent(title="Retail", date=datetime.now(UTC), impact=EventImpact.LOW)
        assert e.is_crypto_mover is False

    def test_hours_until(self):
        later = datetime.now(UTC) + timedelta(hours=3)
        e = MacroEvent(title="E", date=later)
        assert 2.9 <= e.hours_until <= 3.1

    def test_is_imminent_true(self):
        later = datetime.now(UTC) + timedelta(hours=1)
        e = MacroEvent(title="E", date=later)
        assert e.is_imminent is True

    def test_is_imminent_false_past(self):
        past = datetime.now(UTC) - timedelta(hours=1)
        e = MacroEvent(title="E", date=past)
        assert e.is_imminent is False

    def test_is_happening_now_true(self):
        soon = datetime.now(UTC) + timedelta(minutes=10)
        e = MacroEvent(title="E", date=soon)
        assert e.is_happening_now is True

    def test_is_happening_now_false(self):
        later = datetime.now(UTC) + timedelta(hours=2)
        e = MacroEvent(title="E", date=later)
        assert e.is_happening_now is False


class TestMacroCalendar:
    @pytest.fixture
    def cal(self):
        return MacroCalendar(poll_interval=1800)

    def test_initial_state(self, cal):
        assert cal.upcoming_events == []
        assert cal.upcoming_high_impact == []
        assert cal.has_imminent_event() is False
        assert cal.has_event_now() is False
        assert cal.should_reduce_exposure() is False
        assert cal.is_spike_opportunity() is False
        assert cal.exposure_multiplier() == 1.0
        assert cal.next_event_info() is None

    def test_classify_impact_critical(self):
        assert MacroCalendar._classify_impact("Federal Funds Rate", "") == EventImpact.CRITICAL
        assert MacroCalendar._classify_impact("FOMC Decision", "") == EventImpact.CRITICAL

    def test_classify_impact_high(self):
        assert MacroCalendar._classify_impact("CPI Release", "") == EventImpact.HIGH
        assert MacroCalendar._classify_impact("Non-Farm Payrolls", "") == EventImpact.HIGH

    def test_classify_impact_raw_low(self):
        assert MacroCalendar._classify_impact("Other", "low") == EventImpact.LOW

    def test_classify_impact_raw_medium(self):
        assert MacroCalendar._classify_impact("Other", "medium") == EventImpact.MEDIUM

    def test_exposure_multiplier_critical_1h(self, cal):
        soon = datetime.now(UTC) + timedelta(hours=0.5)
        cal._events = [
            MacroEvent(title="FOMC", date=soon, impact=EventImpact.CRITICAL),
        ]
        assert cal.exposure_multiplier() == 0.3

    def test_exposure_multiplier_critical_2h(self, cal):
        soon = datetime.now(UTC) + timedelta(hours=1.5)
        cal._events = [
            MacroEvent(title="FOMC", date=soon, impact=EventImpact.CRITICAL),
        ]
        assert cal.exposure_multiplier() == 0.5

    def test_exposure_multiplier_critical_4h(self, cal):
        soon = datetime.now(UTC) + timedelta(hours=3)
        cal._events = [
            MacroEvent(title="FOMC", date=soon, impact=EventImpact.CRITICAL),
        ]
        assert cal.exposure_multiplier() == 0.7

    def test_exposure_multiplier_high_1h(self, cal):
        soon = datetime.now(UTC) + timedelta(hours=0.5)
        cal._events = [
            MacroEvent(title="CPI", date=soon, impact=EventImpact.HIGH),
        ]
        assert cal.exposure_multiplier() == 0.5

    def test_exposure_multiplier_high_2h(self, cal):
        soon = datetime.now(UTC) + timedelta(hours=1.5)
        cal._events = [
            MacroEvent(title="CPI", date=soon, impact=EventImpact.HIGH),
        ]
        assert cal.exposure_multiplier() == 0.7

    def test_exposure_multiplier_skips_past(self, cal):
        past = datetime.now(UTC) - timedelta(hours=1)
        cal._events = [
            MacroEvent(title="FOMC", date=past, impact=EventImpact.CRITICAL),
        ]
        assert cal.exposure_multiplier() == 1.0

    def test_exposure_multiplier_skips_low_impact(self, cal):
        soon = datetime.now(UTC) + timedelta(hours=1)
        cal._events = [
            MacroEvent(title="Housing Starts", date=soon, impact=EventImpact.LOW),
        ]
        assert cal.exposure_multiplier() == 1.0

    def test_next_event_info(self, cal):
        soon = datetime.now(UTC) + timedelta(hours=2)
        cal._events = [
            MacroEvent(title="CPI", date=soon, impact=EventImpact.HIGH),
        ]
        info = cal.next_event_info()
        assert info is not None
        assert "CPI" in info
        assert "high" in info

    def test_summary_no_high_impact(self, cal):
        assert "no high-impact" in cal.summary()

    def test_summary_with_events(self, cal):
        soon = datetime.now(UTC) + timedelta(hours=3)
        cal._events = [
            MacroEvent(title="CPI", date=soon, impact=EventImpact.HIGH),
        ]
        out = cal.summary()
        assert "CPI" in out
        assert "exposure" in out


# ── FearGreed (additional to test_intel.py) ──────────────────────────


class TestFearGreedClientSummary:
    @pytest.fixture
    def client(self):
        return FearGreedClient()

    def test_summary_no_data(self, client):
        assert client.summary() == "Fear & Greed: no data"

    def test_summary_with_data(self, client):
        client._latest = FearGreedReading(
            value=20,
            classification="Fear",
            timestamp=datetime.now(UTC),
        )
        out = client.summary()
        assert "20" in out
        assert "Fear" in out
        assert "long" in out or "neutral" in out

    def test_value_boundary_25(self, client):
        client._latest = FearGreedReading(value=25, classification="Extreme Fear", timestamp=datetime.now(UTC))
        assert client.trade_direction_bias() == "long"
        assert client.is_extreme_fear is True

    def test_value_boundary_75(self, client):
        client._latest = FearGreedReading(value=75, classification="Extreme Greed", timestamp=datetime.now(UTC))
        assert client.trade_direction_bias() == "short"
        assert client.is_extreme_greed is True

    def test_position_bias_10(self, client):
        client._latest = FearGreedReading(value=10, classification="Extreme Fear", timestamp=datetime.now(UTC))
        assert client.position_bias() == 1.4

    @pytest.mark.asyncio
    async def test_fetch_parses_response(self, client):
        ts = int(datetime.now(UTC).timestamp())
        payload = {
            "data": [
                {
                    "value": "28",
                    "value_classification": "Fear",
                    "timestamp": str(ts),
                },
                {
                    "value": "35",
                    "value_classification": "Fear",
                    "timestamp": str(ts - 86400),
                },
            ]
        }
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=payload)
        mock_get_cm = MagicMock()
        mock_get_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_get_cm.__aexit__ = AsyncMock(return_value=None)
        mock_sess = MagicMock()
        mock_sess.get.return_value = mock_get_cm
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("intel.fear_greed.aiohttp.ClientSession", return_value=mock_session):
            await client._fetch()
        assert client._latest is not None
        assert client._latest.value == 28
        assert client._latest.classification == "Fear"
        assert client._latest.previous_value == 35


# ── WhaleSentiment ───────────────────────────────────────────────────


class TestOISnapshot:
    def test_oi_surging_true(self):
        s = OISnapshot(oi_change_1h_pct=5.0)
        assert s.oi_surging is True

    def test_oi_surging_false(self):
        s = OISnapshot(oi_change_1h_pct=2.0)
        assert s.oi_surging is False

    def test_oi_collapsing_true(self):
        s = OISnapshot(oi_change_1h_pct=-6.0)
        assert s.oi_collapsing is True

    def test_oi_collapsing_false(self):
        s = OISnapshot(oi_change_1h_pct=-3.0)
        assert s.oi_collapsing is False


class TestWhaleSentimentData:
    def test_is_overleveraged_longs_true(self):
        d = WhaleSentimentData(funding_rate=0.001, long_short_ratio=1.6)
        assert d.is_overleveraged_longs is True

    def test_is_overleveraged_longs_false(self):
        d = WhaleSentimentData(funding_rate=0.0003, long_short_ratio=1.6)
        assert d.is_overleveraged_longs is False

    def test_is_overleveraged_shorts_true(self):
        d = WhaleSentimentData(funding_rate=-0.001, long_short_ratio=0.6)
        assert d.is_overleveraged_shorts is True

    def test_oi_building_true(self):
        d = WhaleSentimentData(open_interest_24h_change_pct=6.0)
        assert d.oi_building is True

    def test_oi_declining_true(self):
        d = WhaleSentimentData(open_interest_24h_change_pct=-6.0)
        assert d.oi_declining is True


class TestWhaleSentiment:
    @pytest.fixture
    def ws(self):
        return WhaleSentiment(symbols=["BTC", "ETH"])

    def test_get_no_data(self, ws):
        assert ws.get("BTC") is None

    def test_get_cleans_symbol(self, ws):
        ws._data["BTC"] = WhaleSentimentData(long_short_ratio=1.0)
        assert ws.get("BTC/USDT").long_short_ratio == 1.0

    def test_contrarian_bias_overleveraged_longs(self, ws):
        ws._data["BTC"] = WhaleSentimentData(funding_rate=0.06, long_short_ratio=1.6)
        assert ws.contrarian_bias("BTC") == "short"

    def test_contrarian_bias_overleveraged_shorts(self, ws):
        ws._data["BTC"] = WhaleSentimentData(funding_rate=-0.06, long_short_ratio=0.6)
        assert ws.contrarian_bias("BTC") == "long"

    def test_contrarian_bias_ratio_high(self, ws):
        ws._data["BTC"] = WhaleSentimentData(long_short_ratio=1.4)
        assert ws.contrarian_bias("BTC") == "short"

    def test_contrarian_bias_ratio_low(self, ws):
        ws._data["BTC"] = WhaleSentimentData(long_short_ratio=0.7)
        assert ws.contrarian_bias("BTC") == "long"

    def test_contrarian_bias_neutral(self, ws):
        ws._data["BTC"] = WhaleSentimentData(long_short_ratio=1.0)
        assert ws.contrarian_bias("BTC") == "neutral"

    def test_should_avoid_longs_true(self, ws):
        ws._data["BTC"] = WhaleSentimentData(funding_rate=0.06, long_short_ratio=1.6)
        assert ws.should_avoid_longs("BTC") is True

    def test_should_avoid_shorts_true(self, ws):
        ws._data["BTC"] = WhaleSentimentData(funding_rate=-0.06, long_short_ratio=0.6)
        assert ws.should_avoid_shorts("BTC") is True

    def test_breakout_expected_true(self, ws):
        ws._data["BTC"] = WhaleSentimentData(open_interest_24h_change_pct=6.0)
        assert ws.breakout_expected("BTC") is True

    def test_breakout_expected_false(self, ws):
        ws._data["BTC"] = WhaleSentimentData(open_interest_24h_change_pct=2.0)
        assert ws.breakout_expected("BTC") is False

    def test_summary_empty(self, ws):
        assert ws.summary() == "Whale: no data"

    def test_summary_with_data(self, ws):
        ws._data["BTC"] = WhaleSentimentData(long_short_ratio=1.5, funding_rate=0.01)
        out = ws.summary()
        assert "BTC" in out
        assert "L/S" in out


# ── MarketIntel ──────────────────────────────────────────────────────


class TestMarketCondition:
    def test_summary_lines(self):
        c = MarketCondition(
            regime=MarketRegime.NORMAL,
            fear_greed=50,
            liquidation_24h=100e6,
            preferred_direction="neutral",
        )
        lines = c.summary_lines()
        assert any("Regime" in ln for ln in lines)
        assert any("F&G" in ln for ln in lines)
        assert any("size" in ln for ln in lines)


class TestMarketIntel:
    @pytest.fixture
    def intel(self):
        return MarketIntel(
            coinglass_key="",
            symbols=["BTC", "ETH"],
            defillama_enabled=True,
        )

    def test_condition_initial(self, intel):
        c = intel.condition
        assert c.regime == MarketRegime.NORMAL
        assert c.position_size_multiplier == 1.0
        assert c.preferred_direction == "neutral"

    def test_assess_defaults(self, intel):
        c = intel.assess()
        assert c.fear_greed == 50
        assert c.fear_greed_bias == "neutral"
        assert c.liquidation_24h == 0.0
        assert c.mass_liquidation is False
        assert c.macro_event_imminent is False
        assert c.macro_exposure_mult == 1.0
        assert c.whale_bias == "neutral"
        assert c.tv_btc_consensus == "no_data"
        assert c.tv_eth_consensus == "no_data"
        assert c.tvl_trend == "stable"
        assert c.social_sentiment == "neutral"
        assert c.position_size_multiplier <= 1.5
        assert c.should_reduce_exposure is False
        assert c.regime == MarketRegime.NORMAL

    def test_assess_regime_capitulation(self, intel):
        intel.fear_greed._latest = FearGreedReading(
            value=10, classification="Extreme Fear", timestamp=datetime.now(UTC)
        )
        intel.liquidations._latest = LiquidationSnapshot(total_24h=1_500_000_000)
        c = intel.assess()
        assert c.regime == MarketRegime.CAPITULATION

    def test_assess_regime_risk_off_extreme_greed_macro(self, intel):
        intel.fear_greed._latest = FearGreedReading(
            value=80, classification="Extreme Greed", timestamp=datetime.now(UTC)
        )
        soon = datetime.now(UTC) + timedelta(hours=1)
        intel.macro._events = [
            MacroEvent(title="FOMC", date=soon, impact=EventImpact.CRITICAL),
        ]
        c = intel.assess()
        assert c.regime == MarketRegime.RISK_OFF

    def test_assess_regime_risk_off_overleveraged_greed(self, intel):
        intel.fear_greed._latest = FearGreedReading(value=65, classification="Greed", timestamp=datetime.now(UTC))
        intel.whales._data["BTC"] = WhaleSentimentData(funding_rate=0.06, long_short_ratio=1.6)
        c = intel.assess()
        assert c.regime == MarketRegime.RISK_OFF

    def test_assess_regime_caution(self, intel):
        soon = datetime.now(UTC) + timedelta(hours=1)
        intel.macro._events = [
            MacroEvent(title="CPI", date=soon, impact=EventImpact.HIGH),
        ]
        c = intel.assess()
        assert c.regime == MarketRegime.CAUTION
        assert c.should_reduce_exposure is True

    def test_assess_regime_risk_on_fear(self, intel):
        intel.fear_greed._latest = FearGreedReading(value=30, classification="Fear", timestamp=datetime.now(UTC))
        c = intel.assess()
        assert c.regime == MarketRegime.RISK_ON

    def test_assess_regime_risk_on_mass_liq(self, intel):
        intel.liquidations._latest = LiquidationSnapshot(total_24h=1_200_000_000)
        c = intel.assess()
        assert c.regime == MarketRegime.RISK_ON

    def test_assess_preferred_direction_long(self, intel):
        intel.fear_greed._latest = FearGreedReading(
            value=15, classification="Extreme Fear", timestamp=datetime.now(UTC)
        )
        intel.liquidations._latest = LiquidationSnapshot(total_24h=1_500_000_000, long_24h=1e9, short_24h=0.5e9)
        intel.whales._data["BTC"] = WhaleSentimentData(funding_rate=-0.06, long_short_ratio=0.6)
        intel.tradingview._cache["BTC/USDT"] = {
            "1h": TVAnalysis(symbol="BTC", summary_rating=TVRating.BUY),
            "4h": TVAnalysis(symbol="BTC", summary_rating=TVRating.BUY),
        }
        c = intel.assess()
        assert c.preferred_direction == "long"

    def test_assess_preferred_direction_short(self, intel):
        intel.fear_greed._latest = FearGreedReading(
            value=80, classification="Extreme Greed", timestamp=datetime.now(UTC)
        )
        intel.liquidations._latest = LiquidationSnapshot(total_24h=1_500_000_000, long_24h=0.3e9, short_24h=1.2e9)
        intel.whales._data["BTC"] = WhaleSentimentData(funding_rate=0.06, long_short_ratio=1.6)
        intel.tradingview._cache["BTC/USDT"] = {
            "1h": TVAnalysis(symbol="BTC", summary_rating=TVRating.SELL),
            "4h": TVAnalysis(symbol="BTC", summary_rating=TVRating.SELL),
        }
        c = intel.assess()
        assert c.preferred_direction == "short"

    def test_assess_reduce_exposure_extreme_greed(self, intel):
        intel.fear_greed._latest = FearGreedReading(
            value=85, classification="Extreme Greed", timestamp=datetime.now(UTC)
        )
        c = intel.assess()
        assert c.should_reduce_exposure is True

    def test_assess_oi_details_from_whale(self, intel):
        intel.whales._data["BTC"] = WhaleSentimentData(
            oi_snapshot=OISnapshot(
                total_oi_usd=1e10,
                oi_change_1h_pct=2.0,
                top_trader_long_ratio=0.6,
            )
        )
        c = intel.assess()
        assert c.btc_oi_total_usd == 1e10
        assert c.btc_oi_change_1h_pct == 2.0
        assert c.top_trader_long_ratio == 0.6

    def test_tv_signal_boost(self, intel):
        # trend_aligned=False so boost is 1.1 (no +0.1)
        intel.tradingview._cache["BTC/USDT"] = {
            "1h": TVAnalysis(
                symbol="BTC",
                summary_rating=TVRating.BUY,
                oscillators_rating=TVRating.SELL,
                moving_averages_rating=TVRating.BUY,
                buy_count=7,
                sell_count=0,
                neutral_count=3,
                total_signals=10,
            ),
        }
        assert intel.tv_signal_boost("BTC/USDT", "long") == 1.1

    def test_get_discovery_symbols_empty(self, intel):
        symbols = intel.get_discovery_symbols()
        assert symbols == []

    def test_get_discovery_symbols_from_cmc(self, intel):
        intel.coinmarketcap._trending = [
            CMCCoin(symbol="BTC", volume_24h=2e6, market_cap=20e6),
        ]
        symbols = intel.get_discovery_symbols()
        assert "BTC" in symbols

    def test_get_discovery_symbols_from_coingecko(self, intel):
        intel.coingecko._top_by_volume = [
            GeckoCoin(symbol="eth", volume_24h=1_500_000),
        ]
        intel.coingecko._trending = []
        intel.coingecko._top_gainers = []
        symbols = intel.get_discovery_symbols()
        assert "ETH" in symbols

    def test_get_discovery_symbols_filters_small_volume(self, intel):
        intel.coingecko._top_by_volume = [
            GeckoCoin(symbol="tiny", volume_24h=500_000),
        ]
        intel.coingecko._trending = []
        intel.coingecko._top_gainers = []
        symbols = intel.get_discovery_symbols()
        assert "TINY" not in symbols

    def test_full_summary(self, intel):
        out = intel.full_summary()
        assert "MARKET INTELLIGENCE" in out
        assert "Fear" in out or "Greed" in out
        assert "Liquidations" in out
        assert "Regime" in out

    @pytest.mark.asyncio
    async def test_analyze_symbol_returns_none(self, intel):
        with patch.object(
            intel.tradingview,
            "analyze",
            new_callable=AsyncMock,
            return_value=TVAnalysis(symbol="BTC/USDT"),
        ):
            result = await intel.analyze_symbol("BTC/USDT")
        assert result is None

    @pytest.mark.asyncio
    async def test_start_and_stop(self, intel):
        for attr in (
            "fear_greed",
            "liquidations",
            "macro",
            "whales",
            "tradingview",
            "coinmarketcap",
            "coingecko",
            "defillama",
            "santiment",
        ):
            getattr(intel, attr).start = AsyncMock()
            getattr(intel, attr).stop = AsyncMock()
        await intel.start()
        intel.fear_greed.start.assert_awaited_once()
        intel.tradingview.start.assert_awaited_once()
        intel.defillama.start.assert_awaited_once()
        await intel.stop()
        intel.fear_greed.stop.assert_awaited_once()
        intel.santiment.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_skips_defillama_when_disabled(self):
        intel = MarketIntel(coinglass_key="", symbols=["BTC"], defillama_enabled=False)
        for attr in (
            "fear_greed",
            "liquidations",
            "macro",
            "whales",
            "tradingview",
            "coinmarketcap",
            "coingecko",
            "defillama",
            "santiment",
        ):
            getattr(intel, attr).start = AsyncMock()
            getattr(intel, attr).stop = AsyncMock()
        await intel.start()
        intel.defillama.start.assert_not_awaited()


# ── WhaleSentiment _fetch_symbol ─────────────────────────────────────


class TestWhaleSentimentFetchSymbol:
    """Tests for WhaleSentiment._fetch_symbol with mocked HTTP responses."""

    @pytest.mark.asyncio
    async def test_fetch_symbol_parses_long_short_ratio(self):
        ws = WhaleSentiment(symbols=["BTC"], coinglass_key="test-key")
        ls_json = {"data": [{"longRate": 60, "shortRate": 40}]}
        funding_json = {"data": [{"rate": 0.001, "uMarginList": [{"rate": 0.001}]}]}
        oi_json = {"data": [{"y": 1e10}, {"y": 1.1e10}]}
        top_json = {"data": [{"longRate": 55}]}

        def make_mock_resp(data):
            resp = AsyncMock()
            resp.status = 200
            resp.json = AsyncMock(return_value=data)
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        responses = [
            make_mock_resp(ls_json),
            make_mock_resp(funding_json),
            make_mock_resp(oi_json),
            make_mock_resp(top_json),
        ]

        mock_session = MagicMock()
        call_idx = {"i": 0}

        def get_side_effect(*args, **kwargs):
            idx = call_idx["i"]
            call_idx["i"] += 1
            return responses[idx] if idx < len(responses) else make_mock_resp({})

        mock_session.get = MagicMock(side_effect=get_side_effect)
        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("intel.whale_sentiment.aiohttp.ClientSession", return_value=mock_session_cm):
            await ws._fetch_symbol("BTC")

        data = ws._data.get("BTC")
        assert data is not None
        assert data.long_short_ratio == pytest.approx(60.0 / 40.0)
        assert data.funding_rate == 0.001
        assert data.oi_snapshot is not None
        assert data.oi_snapshot.total_oi_usd == 1.1e10
        assert data.oi_snapshot.oi_change_1h_pct == pytest.approx(10.0)
        assert data.oi_snapshot.top_trader_long_ratio == pytest.approx(0.55)

    @pytest.mark.asyncio
    async def test_fetch_symbol_handles_api_errors(self):
        ws = WhaleSentiment(symbols=["ETH"])

        def make_error_resp():
            resp = AsyncMock()
            resp.status = 500
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=make_error_resp())
        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("intel.whale_sentiment.aiohttp.ClientSession", return_value=mock_session_cm):
            await ws._fetch_symbol("ETH")

        data = ws._data.get("ETH")
        assert data is not None
        assert data.long_short_ratio == 1.0
        assert data.funding_rate == 0.0

    def test_summary_overleveraged_longs(self):
        ws = WhaleSentiment(symbols=["BTC"])
        ws._data["BTC"] = WhaleSentimentData(funding_rate=0.001, long_short_ratio=1.6)
        out = ws.summary()
        assert "OVER-LONG" in out

    def test_summary_overleveraged_shorts(self):
        ws = WhaleSentiment(symbols=["BTC"])
        ws._data["BTC"] = WhaleSentimentData(funding_rate=-0.001, long_short_ratio=0.5)
        out = ws.summary()
        assert "OVER-SHORT" in out


# ── Santiment _fetch_symbol ──────────────────────────────────────────


class TestSantimentFetchSymbol:
    @pytest.mark.asyncio
    async def test_fetch_symbol_parses_social_volume(self):
        client = SantimentClient(symbols=["bitcoin"], api_key="test-key")
        graphql_resp = {
            "data": {
                "getMetric": {
                    "timeseriesData": [
                        {"datetime": "2026-02-14", "value": 100},
                        {"datetime": "2026-02-15", "value": 150},
                        {"datetime": "2026-02-16", "value": 200},
                        {"datetime": "2026-02-17", "value": 120},
                        {"datetime": "2026-02-18", "value": 180},
                        {"datetime": "2026-02-19", "value": 160},
                        {"datetime": "2026-02-20", "value": 300},
                    ]
                }
            }
        }
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=graphql_resp)
        mock_post_cm = MagicMock()
        mock_post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post_cm.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_post_cm)
        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("intel.santiment.aiohttp.ClientSession", return_value=mock_session_cm):
            await client._fetch_symbol("bitcoin")

        data = client._data.get("bitcoin")
        assert data is not None
        assert data.social_volume == 300
        assert data.social_volume_avg == pytest.approx(sum([100, 150, 200, 120, 180, 160, 300]) / 7)

    @pytest.mark.asyncio
    async def test_fetch_symbol_handles_empty_response(self):
        client = SantimentClient(symbols=["bitcoin"])
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"data": {"getMetric": {"timeseriesData": []}}})
        mock_post_cm = MagicMock()
        mock_post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post_cm.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_post_cm)
        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("intel.santiment.aiohttp.ClientSession", return_value=mock_session_cm):
            await client._fetch_symbol("bitcoin")

        data = client._data.get("bitcoin")
        assert data is not None
        assert data.social_volume == 0.0

    @pytest.mark.asyncio
    async def test_fetch_symbol_handles_network_error(self):
        client = SantimentClient(symbols=["bitcoin"])
        mock_post_cm = MagicMock()
        mock_post_cm.__aenter__ = AsyncMock(side_effect=ConnectionError("offline"))
        mock_post_cm.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_post_cm)
        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("intel.santiment.aiohttp.ClientSession", return_value=mock_session_cm):
            await client._fetch_symbol("bitcoin")

        data = client._data.get("bitcoin")
        assert data is not None
        assert data.social_volume == 0.0

    def test_summary_with_social_spike(self):
        client = SantimentClient(symbols=["bitcoin"])
        client._data["bitcoin"] = SocialData(social_volume=250, social_volume_avg=100)
        out = client.summary()
        assert "SPIKE" in out


# ── TradingView full_analysis, analyze_multi, poll_loop ──────────────


class TestTradingViewAnalyzeMulti:
    @pytest.mark.asyncio
    async def test_analyze_multi_returns_results(self):
        client = TradingViewClient(exchange="MEXC", intervals=["1h"])
        a1 = TVAnalysis(symbol="BTC/USDT", summary_rating=TVRating.BUY)
        a2 = TVAnalysis(symbol="ETH/USDT", summary_rating=TVRating.SELL)
        with patch.object(client, "analyze", new_callable=AsyncMock, side_effect=[a1, a2]):
            results = await client.analyze_multi(["BTC/USDT", "ETH/USDT"], "1h")
        assert "BTC/USDT" in results
        assert "ETH/USDT" in results
        assert results["BTC/USDT"].summary_rating == TVRating.BUY

    @pytest.mark.asyncio
    async def test_analyze_multi_skips_exceptions(self):
        client = TradingViewClient(exchange="MEXC", intervals=["1h"])
        a1 = TVAnalysis(symbol="BTC/USDT")
        with patch.object(client, "analyze", new_callable=AsyncMock, side_effect=[a1, RuntimeError("fail")]):
            results = await client.analyze_multi(["BTC/USDT", "ETH/USDT"], "1h")
        assert "BTC/USDT" in results
        assert "ETH/USDT" not in results


class TestTradingViewFullAnalysis:
    @pytest.mark.asyncio
    async def test_full_analysis_all_intervals(self):
        client = TradingViewClient(exchange="MEXC", intervals=["1h", "4h"])
        a1 = TVAnalysis(symbol="BTC", summary_rating=TVRating.BUY)
        a4 = TVAnalysis(symbol="BTC", summary_rating=TVRating.SELL)
        with patch.object(client, "analyze", new_callable=AsyncMock, side_effect=[a1, a4]):
            results = await client.full_analysis("BTC")
        assert "1h" in results
        assert "4h" in results

    @pytest.mark.asyncio
    async def test_full_analysis_skips_none(self):
        client = TradingViewClient(exchange="MEXC", intervals=["1h", "4h"])
        a1 = TVAnalysis(symbol="BTC")
        with patch.object(client, "analyze", new_callable=AsyncMock, side_effect=[a1, None]):
            results = await client.full_analysis("BTC")
        assert "1h" in results
        assert "4h" not in results


class TestTradingViewStartStop:
    @pytest.mark.asyncio
    async def test_start_creates_poll_task(self):
        client = TradingViewClient(exchange="MEXC", intervals=["1h"])
        with patch.object(client, "_poll_loop", new_callable=AsyncMock):
            await client.start()
            assert client._running is True
            assert client._poll_task is not None
            import asyncio

            await asyncio.sleep(0)
        await client.stop()
        assert client._running is False
        assert client._poll_task is None

    @pytest.mark.asyncio
    async def test_stop_when_not_started(self):
        client = TradingViewClient(exchange="MEXC", intervals=["1h"])
        await client.stop()
        assert client._running is False


class TestTradingViewPollLoop:
    @pytest.mark.asyncio
    async def test_poll_loop_iterates_symbols(self):
        client = TradingViewClient(exchange="MEXC", intervals=["1h"])
        client._running = True
        client._poll_symbols = ["BTC/USDT", "ETH/USDT"]

        call_count = {"n": 0}

        async def fake_analysis(sym):
            call_count["n"] += 1

        async def stop_after_sleep(sec):
            client._running = False

        with patch.object(client, "full_analysis", new_callable=AsyncMock, side_effect=fake_analysis):
            with patch("intel.tradingview.asyncio.sleep", side_effect=stop_after_sleep):
                await client._poll_loop()

        assert call_count["n"] == 2

    @pytest.mark.asyncio
    async def test_analyze_non_200(self):
        client = TradingViewClient(exchange="MEXC", intervals=["1h"])
        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=None)
        mock_sess = MagicMock()
        mock_sess.post.return_value = mock_cm
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("intel.tradingview.aiohttp.ClientSession", return_value=mock_session):
            result = await client.analyze("BTC/USDT", "1h")
        assert result is None

    @pytest.mark.asyncio
    async def test_analyze_non_dict_response(self):
        client = TradingViewClient(exchange="MEXC", intervals=["1h"])
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value="not a dict")
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=None)
        mock_sess = MagicMock()
        mock_sess.post.return_value = mock_cm
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("intel.tradingview.aiohttp.ClientSession", return_value=mock_session):
            result = await client.analyze("BTC/USDT", "1h")
        assert result is None

    @pytest.mark.asyncio
    async def test_analyze_too_few_values(self):
        client = TradingViewClient(exchange="MEXC", intervals=["1h"])
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"data": [{"d": [0.1, 0.2]}]})
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=None)
        mock_sess = MagicMock()
        mock_sess.post.return_value = mock_cm
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("intel.tradingview.aiohttp.ClientSession", return_value=mock_session):
            result = await client.analyze("BTC/USDT", "1h")
        assert result is None


# ── CoinMarketCap HTTP fetch, poll_loop, start/stop ──────────────────


def _make_cmc_get_mock(resp_status: int, json_data: dict | list):
    mock_resp = AsyncMock()
    mock_resp.status = resp_status
    mock_resp.json = AsyncMock(return_value=json_data)
    mock_get = MagicMock()
    mock_get.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_get.__aexit__ = AsyncMock(return_value=None)
    return mock_get


class TestCoinMarketCapClientFetch:
    """HTTP fetch methods for CoinMarketCapClient (mocked aiohttp)."""

    @pytest.fixture
    def client(self):
        return CoinMarketCapClient(api_key="", poll_interval=300)

    @pytest.mark.asyncio
    @patch("intel.coinmarketcap.aiohttp.ClientSession")
    async def test_fetch_trending_success(self, mock_session_class, client):
        data = {
            "data": {
                "cryptoTopSearchRanks": [
                    {
                        "id": 1,
                        "symbol": "BTC",
                        "name": "Bitcoin",
                        "slug": "bitcoin",
                        "priceChange": {
                            "price": 50000,
                            "priceChange24h": 2.5,
                            "volume24h": 1e9,
                            "marketCap": 1e12,
                        },
                    },
                ]
            }
        }
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_cmc_get_mock(200, data)
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await client._fetch_trending()

        assert len(client._trending) == 1
        assert client._trending[0].symbol == "BTC"
        assert client._trending[0].change_24h == 2.5

    @pytest.mark.asyncio
    @patch("intel.coinmarketcap.aiohttp.ClientSession")
    async def test_fetch_trending_non_200(self, mock_session_class, client):
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_cmc_get_mock(404, {})
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await client._fetch_trending()

        assert client._trending == []

    @pytest.mark.asyncio
    @patch("intel.coinmarketcap.aiohttp.ClientSession")
    async def test_fetch_trending_exception(self, mock_session_class, client):
        mock_sess = AsyncMock()
        mock_get = MagicMock()
        mock_get.__aenter__ = AsyncMock(side_effect=ConnectionError("offline"))
        mock_get.__aexit__ = AsyncMock(return_value=None)
        mock_sess.get.return_value = mock_get
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await client._fetch_trending()

        assert client._trending == []

    @pytest.mark.asyncio
    @patch("intel.coinmarketcap.aiohttp.ClientSession")
    async def test_fetch_trending_non_dict(self, mock_session_class, client):
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_cmc_get_mock(200, [])
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await client._fetch_trending()

        assert client._trending == []

    @pytest.mark.asyncio
    @patch("intel.coinmarketcap.aiohttp.ClientSession")
    async def test_fetch_gainers_losers_spotlight_success(self, mock_session_class, client):
        data = {
            "data": {
                "gainerList": [
                    {
                        "id": 1,
                        "symbol": "PEPE",
                        "name": "Pepe",
                        "slug": "pepe",
                        "priceChange": {"price": 0.01, "priceChange24h": 15.0, "volume24h": 2e6, "marketCap": 5e9},
                        "cmcRank": 50,
                    }
                ],
                "loserList": [
                    {
                        "id": 2,
                        "symbol": "DOGE",
                        "name": "Dogecoin",
                        "slug": "dogecoin",
                        "priceChange": {"price": 0.08, "priceChange24h": -10.0, "volume24h": 1e9, "marketCap": 10e9},
                        "cmcRank": 8,
                    }
                ],
            }
        }
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_cmc_get_mock(200, data)
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await client._fetch_gainers_losers()

        assert len(client._gainers) == 1
        assert client._gainers[0].symbol == "PEPE"
        assert client._gainers[0].change_24h == 15.0
        assert len(client._losers) == 1
        assert client._losers[0].symbol == "DOGE"
        assert client._losers[0].change_24h == -10.0

    @pytest.mark.asyncio
    @patch("intel.coinmarketcap.aiohttp.ClientSession")
    async def test_fetch_gainers_losers_non_200(self, mock_session_class, client):
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_cmc_get_mock(500, {})
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await client._fetch_gainers_losers()

        assert client._gainers == []
        assert client._losers == []

    @pytest.mark.asyncio
    @patch("intel.coinmarketcap.aiohttp.ClientSession")
    async def test_fetch_gainers_api_with_key_success(self, mock_session_class):
        client = CoinMarketCapClient(api_key="key", poll_interval=300)
        data = {
            "data": [
                {
                    "id": 1,
                    "symbol": "BTC",
                    "name": "Bitcoin",
                    "slug": "bitcoin",
                    "quote": {
                        "USD": {"price": 50000, "volume_24h": 1e9, "market_cap": 1e12, "percent_change_24h": 3.0}
                    },
                    "cmc_rank": 1,
                },
                {
                    "id": 2,
                    "symbol": "SOL",
                    "name": "Solana",
                    "slug": "solana",
                    "quote": {"USD": {"price": 100, "volume_24h": 5e8, "market_cap": 50e9, "percent_change_24h": -5.0}},
                    "cmc_rank": 5,
                },
            ]
        }
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_cmc_get_mock(200, data)
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await client._fetch_gainers_api()

        assert len(client._gainers) == 1
        assert client._gainers[0].symbol == "BTC"
        assert len(client._losers) == 1
        assert client._losers[0].symbol == "SOL"

    @pytest.mark.asyncio
    @patch("intel.coinmarketcap.aiohttp.ClientSession")
    async def test_fetch_recently_added_success(self, mock_session_class, client):
        data = {
            "data": {
                "cryptoCurrencyList": [
                    {
                        "id": 999,
                        "symbol": "NEW",
                        "name": "NewCoin",
                        "slug": "newcoin",
                        "quotes": [
                            {
                                "price": 1.0,
                                "volume24h": 2e6,
                                "marketCap": 20e6,
                                "percentChange24h": 0,
                                "percentChange1h": 0,
                                "percentChange7d": 0,
                            }
                        ],
                        "cmcRank": 100,
                    }
                ]
            }
        }
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_cmc_get_mock(200, data)
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await client._fetch_recently_added()

        assert len(client._recently_added) == 1
        assert client._recently_added[0].symbol == "NEW"

    @pytest.mark.asyncio
    @patch("intel.coinmarketcap.aiohttp.ClientSession")
    async def test_fetch_recently_added_non_200(self, mock_session_class, client):
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_cmc_get_mock(403, {})
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await client._fetch_recently_added()

        assert client._recently_added == []


class TestCoinMarketCapClientPollLoopAndLifecycle:
    @pytest.mark.asyncio
    async def test_poll_loop_calls_fetch_all_once(self):
        client = CoinMarketCapClient(api_key="", poll_interval=300)
        client._running = True
        fetch_all_called = {"n": 0}

        async def stop_after_sleep(sec):
            client._running = False

        async def count_fetch():
            fetch_all_called["n"] += 1
            await asyncio.sleep(0)

        with patch.object(client, "_fetch_all", new_callable=AsyncMock, side_effect=count_fetch):
            with patch("intel.coinmarketcap.asyncio.sleep", side_effect=stop_after_sleep):
                await client._poll_loop()

        assert fetch_all_called["n"] >= 1

    @pytest.mark.asyncio
    async def test_start_sets_running_and_creates_task(self):
        client = CoinMarketCapClient(api_key="", poll_interval=300)
        with patch.object(client, "_poll_loop", new_callable=AsyncMock):
            await client.start()
        assert client._running is True
        assert len(client._background_tasks) == 1
        await client.stop()
        assert client._running is False

    @pytest.mark.asyncio
    async def test_stop_clears_running(self):
        client = CoinMarketCapClient(api_key="", poll_interval=300)
        client._running = True
        await client.stop()
        assert client._running is False


# ── CoinGecko HTTP fetch, poll_loop, start/stop ──────────────────────


def _make_gecko_get_mock(resp_status: int, json_data: dict | list):
    mock_resp = AsyncMock()
    mock_resp.status = resp_status
    mock_resp.json = AsyncMock(return_value=json_data)
    mock_get = MagicMock()
    mock_get.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_get.__aexit__ = AsyncMock(return_value=None)
    return mock_get


class TestCoinGeckoClientFetch:
    """HTTP fetch methods for CoinGeckoClient (mocked aiohttp)."""

    @pytest.fixture
    def client(self):
        return CoinGeckoClient(api_key="", poll_interval=300)

    @pytest.mark.asyncio
    @patch("intel.coingecko.aiohttp.ClientSession")
    async def test_fetch_trending_success(self, mock_session_class, client):
        data = {
            "coins": [
                {
                    "item": {
                        "id": "bitcoin",
                        "symbol": "btc",
                        "name": "Bitcoin",
                        "data": {
                            "price": 50000,
                            "market_cap": "1,000,000,000,000",
                            "price_change_percentage_24h": {"usd": 2.5},
                            "total_volume": "50,000,000,000",
                            "sparkline": [1.0] * 7,
                        },
                        "market_cap_rank": 1,
                    }
                },
            ]
        }
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_gecko_get_mock(200, data)
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await client._fetch_trending()

        assert len(client._trending) == 1
        assert client._trending[0].symbol == "btc"
        assert client._trending[0].change_24h == 2.5

    @pytest.mark.asyncio
    @patch("intel.coingecko.aiohttp.ClientSession")
    async def test_fetch_trending_non_200(self, mock_session_class, client):
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_gecko_get_mock(429, {})
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await client._fetch_trending()

        assert client._trending == []

    @pytest.mark.asyncio
    @patch("intel.coingecko.aiohttp.ClientSession")
    async def test_fetch_trending_exception(self, mock_session_class, client):
        mock_sess = AsyncMock()
        mock_get = MagicMock()
        mock_get.__aenter__ = AsyncMock(side_effect=ConnectionError("offline"))
        mock_get.__aexit__ = AsyncMock(return_value=None)
        mock_sess.get.return_value = mock_get
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await client._fetch_trending()

        assert client._trending == []

    @pytest.mark.asyncio
    @patch("intel.coingecko.aiohttp.ClientSession")
    async def test_fetch_trending_non_list_coins(self, mock_session_class, client):
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_gecko_get_mock(200, {"coins": "not a list"})
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await client._fetch_trending()

        assert client._trending == []

    @pytest.mark.asyncio
    @patch("intel.coingecko.aiohttp.ClientSession")
    async def test_fetch_market_data_success(self, mock_session_class, client):
        data = [
            {
                "id": "bitcoin",
                "symbol": "btc",
                "name": "Bitcoin",
                "current_price": 50000,
                "market_cap": 1e12,
                "market_cap_rank": 1,
                "total_volume": 50e9,
                "price_change_percentage_1h_in_currency": 0.5,
                "price_change_percentage_24h": 2.0,
                "price_change_percentage_7d_in_currency": 5.0,
                "ath": 70000,
                "ath_change_percentage": -28.0,
                "sparkline_in_7d": {"price": [49000, 50000, 51000]},
            },
            {
                "id": "ethereum",
                "symbol": "eth",
                "name": "Ethereum",
                "current_price": 3000,
                "market_cap": 400e9,
                "market_cap_rank": 2,
                "total_volume": 20e9,
                "price_change_percentage_1h_in_currency": 0.2,
                "price_change_percentage_24h": 8.0,
                "price_change_percentage_7d_in_currency": 10.0,
                "ath": 4000,
                "ath_change_percentage": -25.0,
                "sparkline_in_7d": [2900, 3000, 3100],
            },
        ]
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_gecko_get_mock(200, data)
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await client._fetch_market_data()

        assert len(client._top_by_volume) >= 1
        assert any(c.symbol == "btc" for c in client._top_by_volume)
        assert len(client._top_gainers) >= 1

    @pytest.mark.asyncio
    @patch("intel.coingecko.aiohttp.ClientSession")
    async def test_fetch_market_data_non_200(self, mock_session_class, client):
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_gecko_get_mock(503, [])
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await client._fetch_market_data()

        assert client._top_by_volume == []
        assert client._top_gainers == []

    @pytest.mark.asyncio
    @patch("intel.coingecko.aiohttp.ClientSession")
    async def test_fetch_market_data_non_list(self, mock_session_class, client):
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_gecko_get_mock(200, {"data": []})
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await client._fetch_market_data()

        assert client._top_by_volume == []


class TestCoinGeckoClientPollLoopAndLifecycle:
    @pytest.mark.asyncio
    async def test_poll_loop_calls_fetch_all_once(self):
        client = CoinGeckoClient(api_key="", poll_interval=300)
        client._running = True
        fetch_all_called = {"n": 0}

        async def stop_after_sleep(sec):
            client._running = False

        async def count_fetch():
            fetch_all_called["n"] += 1
            await asyncio.sleep(0)

        with patch.object(client, "_fetch_all", new_callable=AsyncMock, side_effect=count_fetch):
            with patch("intel.coingecko.asyncio.sleep", side_effect=stop_after_sleep):
                await client._poll_loop()

        assert fetch_all_called["n"] >= 1

    @pytest.mark.asyncio
    async def test_start_sets_running_and_creates_task(self):
        client = CoinGeckoClient(api_key="", poll_interval=300)
        with patch.object(client, "_poll_loop", new_callable=AsyncMock):
            await client.start()
        assert client._running is True
        assert len(client._background_tasks) == 1
        await client.stop()
        assert client._running is False

    @pytest.mark.asyncio
    async def test_stop_clears_running(self):
        client = CoinGeckoClient(api_key="", poll_interval=300)
        client._running = True
        await client.stop()
        assert client._running is False


# ── LiquidationMonitor HTTP fetch, poll_loop, start/stop ──────────────


def _make_liq_get_mock(resp_status: int, json_data: dict):
    mock_resp = AsyncMock()
    mock_resp.status = resp_status
    mock_resp.json = AsyncMock(return_value=json_data)
    mock_get = MagicMock()
    mock_get.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_get.__aexit__ = AsyncMock(return_value=None)
    return mock_get


class TestLiquidationMonitorFetch:
    """HTTP _fetch for LiquidationMonitor (mocked aiohttp)."""

    @pytest.fixture
    def monitor(self):
        return LiquidationMonitor(poll_interval=300, api_key="test-key")

    @pytest.fixture
    def monitor_no_key(self):
        return LiquidationMonitor(poll_interval=300, api_key="")

    @pytest.mark.asyncio
    async def test_fetch_no_api_key_skips(self, monitor_no_key):
        await monitor_no_key._fetch()
        assert monitor_no_key._latest is None
        assert monitor_no_key._warned_no_key is True

    @pytest.mark.asyncio
    @patch("intel.liquidations.aiohttp.ClientSession")
    async def test_fetch_success_list_format(self, mock_session_class, monitor):
        data = {
            "code": "0",
            "msg": "success",
            "data": [
                {
                    "exchange": "All",
                    "liquidation_usd": 85_000_000,
                    "long_liquidation_usd": 50_000_000,
                    "short_liquidation_usd": 35_000_000,
                },
                {
                    "exchange": "Bybit",
                    "liquidation_usd": 30_000_000,
                    "long_liquidation_usd": 18_000_000,
                    "short_liquidation_usd": 12_000_000,
                },
            ],
        }
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_liq_get_mock(200, data)
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await monitor._fetch()

        assert monitor._latest is not None
        assert monitor._latest.total_24h == 85_000_000
        assert monitor._latest.long_24h == 50_000_000
        assert monitor._latest.short_24h == 35_000_000

    @pytest.mark.asyncio
    @patch("intel.liquidations.aiohttp.ClientSession")
    async def test_fetch_success_dict_format(self, mock_session_class, monitor):
        data = {
            "code": "0",
            "data": {
                "liquidation_usd": 500_000_000,
                "long_liquidation_usd": 300_000_000,
                "short_liquidation_usd": 200_000_000,
            },
        }
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_liq_get_mock(200, data)
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await monitor._fetch()

        assert monitor._latest is not None
        assert monitor._latest.total_24h == 500_000_000
        assert monitor._latest.long_24h == 300_000_000
        assert monitor._latest.short_24h == 200_000_000

    @pytest.mark.asyncio
    @patch("intel.liquidations.aiohttp.ClientSession")
    async def test_fetch_list_no_all_entry_sums_exchanges(self, mock_session_class, monitor):
        data = {
            "code": "0",
            "data": [
                {
                    "exchange": "Binance",
                    "liquidation_usd": 40_000_000,
                    "long_liquidation_usd": 25_000_000,
                    "short_liquidation_usd": 15_000_000,
                },
                {
                    "exchange": "Bybit",
                    "liquidation_usd": 30_000_000,
                    "long_liquidation_usd": 18_000_000,
                    "short_liquidation_usd": 12_000_000,
                },
            ],
        }
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_liq_get_mock(200, data)
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await monitor._fetch()

        assert monitor._latest is not None
        assert monitor._latest.total_24h == 70_000_000
        assert monitor._latest.long_24h == 43_000_000
        assert monitor._latest.short_24h == 27_000_000

    @pytest.mark.asyncio
    @patch("intel.liquidations.aiohttp.ClientSession")
    async def test_fetch_non_200(self, mock_session_class, monitor):
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_liq_get_mock(403, {})
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await monitor._fetch()

        assert monitor._latest is None

    @pytest.mark.asyncio
    @patch("intel.liquidations.aiohttp.ClientSession")
    async def test_fetch_exception(self, mock_session_class, monitor):
        mock_sess = AsyncMock()
        mock_get = MagicMock()
        mock_get.__aenter__ = AsyncMock(side_effect=ConnectionError("offline"))
        mock_get.__aexit__ = AsyncMock(return_value=None)
        mock_sess.get.return_value = mock_get
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await monitor._fetch()

        assert monitor._latest is None

    @pytest.mark.asyncio
    @patch("intel.liquidations.aiohttp.ClientSession")
    async def test_fetch_non_dict_data(self, mock_session_class, monitor):
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_liq_get_mock(200, [])
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await monitor._fetch()

        assert monitor._latest is None

    @pytest.mark.asyncio
    @patch("intel.liquidations.aiohttp.ClientSession")
    async def test_fetch_parses_wrapped_camel_case_and_commas(self, mock_session_class, monitor):
        data = {
            "code": "0",
            "data": {
                "list": [
                    {
                        "exchangeName": "All",
                        "liquidationUsd": "1,250,000.50",
                        "longLiquidationUsd": "700000",
                        "shortLiquidationUsd": "550000.50",
                    }
                ]
            },
        }
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_liq_get_mock(200, data)
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await monitor._fetch()

        assert monitor._latest is not None
        assert monitor._latest.total_24h == 1_250_000.50
        assert monitor._latest.long_24h == 700_000
        assert monitor._latest.short_24h == 550_000.50


class TestLiquidationMonitorPollLoopAndLifecycle:
    @pytest.mark.asyncio
    async def test_poll_loop_calls_fetch_once(self):
        monitor = LiquidationMonitor(poll_interval=300)
        monitor._running = True
        fetch_called = {"n": 0}

        async def stop_after_sleep(sec):
            monitor._running = False

        async def count_fetch():
            fetch_called["n"] += 1
            await asyncio.sleep(0)

        with patch.object(monitor, "_fetch", new_callable=AsyncMock, side_effect=count_fetch):
            with patch("intel.liquidations.asyncio.sleep", side_effect=stop_after_sleep):
                await monitor._poll_loop()

        assert fetch_called["n"] >= 1

    @pytest.mark.asyncio
    async def test_start_sets_running_and_creates_task(self):
        monitor = LiquidationMonitor(poll_interval=300)
        with (
            patch.object(monitor, "_poll_loop", new_callable=AsyncMock),
            patch.object(monitor, "_binance_ws_loop", new_callable=AsyncMock),
            patch.object(monitor, "_bybit_ws_loop", new_callable=AsyncMock),
        ):
            await monitor.start()
        assert monitor._running is True
        assert len(monitor._background_tasks) == 3
        await monitor.stop()
        assert monitor._running is False
        assert len(monitor._background_tasks) == 0

    @pytest.mark.asyncio
    async def test_stop_clears_running(self):
        monitor = LiquidationMonitor(poll_interval=300)
        monitor._running = True
        await monitor.stop()
        assert monitor._running is False

    def test_consume_binance_force_order_updates_aggregate(self):
        monitor = LiquidationMonitor(poll_interval=300)
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        payload = {
            "e": "forceOrder",
            "E": now_ms,
            "o": {
                "S": "SELL",
                "z": "0.5",
                "ap": "100000",
                "T": now_ms,
            },
        }
        monitor._consume_binance_force_order(payload)

        assert monitor.latest is not None
        assert monitor.latest.long_24h == 50_000.0
        assert monitor.latest.short_24h == 0.0
        assert monitor.latest.total_24h == 50_000.0

    def test_consume_bybit_liquidation_updates_aggregate(self):
        monitor = LiquidationMonitor(poll_interval=300)
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        payload = {
            "topic": "allLiquidation.BTCUSDT",
            "ts": now_ms,
            "data": [
                {
                    "T": now_ms,
                    "s": "BTCUSDT",
                    "S": "Buy",
                    "v": "0.25",
                    "p": "100000",
                }
            ],
        }
        monitor._consume_bybit_liquidation(payload)

        assert monitor.latest is not None
        assert monitor.latest.long_24h == 25_000.0
        assert monitor.latest.short_24h == 0.0
        assert monitor.latest.total_24h == 25_000.0


# ── MacroCalendar HTTP fetch, poll_loop, start/stop ────────────────────


def _make_macro_get_mock(resp_status: int, json_data: list | dict):
    mock_resp = AsyncMock()
    mock_resp.status = resp_status
    mock_resp.json = AsyncMock(return_value=json_data)
    mock_get = MagicMock()
    mock_get.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_get.__aexit__ = AsyncMock(return_value=None)
    return mock_get


class TestMacroCalendarFetch:
    """HTTP _fetch for MacroCalendar (mocked aiohttp)."""

    @pytest.fixture
    def cal(self):
        return MacroCalendar(poll_interval=1800)

    @pytest.mark.asyncio
    @patch("intel.macro_calendar.aiohttp.ClientSession")
    async def test_fetch_success(self, mock_session_class, cal):
        later = (datetime.now(UTC) + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        data = [
            {
                "title": "CPI Release",
                "country": "USD",
                "date": later,
                "impact": "high",
                "forecast": "3.2",
                "previous": "3.1",
                "actual": "",
            },
            {
                "title": "Housing Starts",
                "country": "EUR",
                "date": later,
                "impact": "low",
                "forecast": "",
                "previous": "",
                "actual": "",
            },
        ]
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_macro_get_mock(200, data)
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await cal._fetch()

        assert len(cal._events) == 1
        assert cal._events[0].title == "CPI Release"
        assert cal._events[0].impact == EventImpact.HIGH

    @pytest.mark.asyncio
    @patch("intel.macro_calendar.aiohttp.ClientSession")
    async def test_fetch_non_200(self, mock_session_class, cal):
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_macro_get_mock(503, [])
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await cal._fetch()

        assert cal._events == []

    @pytest.mark.asyncio
    @patch("intel.macro_calendar.aiohttp.ClientSession")
    async def test_fetch_exception(self, mock_session_class, cal):
        mock_session_class.return_value.__aenter__ = AsyncMock(side_effect=ConnectionError("offline"))
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await cal._fetch()

        assert cal._events == []

    @pytest.mark.asyncio
    @patch("intel.macro_calendar.aiohttp.ClientSession")
    async def test_fetch_non_list(self, mock_session_class, cal):
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_macro_get_mock(200, {"events": []})
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await cal._fetch()

        assert cal._events == []

    @pytest.mark.asyncio
    @patch("intel.macro_calendar.aiohttp.ClientSession")
    async def test_fetch_empty_list(self, mock_session_class, cal):
        mock_sess = MagicMock()
        mock_sess.get.return_value = _make_macro_get_mock(200, [])
        mock_session_class.return_value.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_session_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await cal._fetch()

        assert cal._events == []


class TestMacroCalendarPollLoopAndLifecycle:
    @pytest.mark.asyncio
    async def test_poll_loop_calls_fetch_once(self):
        cal = MacroCalendar(poll_interval=1800)
        cal._running = True
        fetch_called = {"n": 0}

        async def stop_after_sleep(sec):
            cal._running = False

        async def count_fetch():
            fetch_called["n"] += 1
            await asyncio.sleep(0)

        with patch.object(cal, "_fetch", new_callable=AsyncMock, side_effect=count_fetch):
            with patch("intel.macro_calendar.asyncio.sleep", side_effect=stop_after_sleep):
                await cal._poll_loop()

        assert fetch_called["n"] >= 1

    @pytest.mark.asyncio
    async def test_start_sets_running_and_creates_task(self):
        cal = MacroCalendar(poll_interval=1800)
        with patch.object(cal, "_poll_loop", new_callable=AsyncMock):
            await cal.start()
        assert cal._running is True
        assert len(cal._background_tasks) == 1
        await cal.stop()
        assert cal._running is False

    @pytest.mark.asyncio
    async def test_stop_clears_running(self):
        cal = MacroCalendar(poll_interval=1800)
        cal._running = True
        await cal.stop()
        assert cal._running is False
