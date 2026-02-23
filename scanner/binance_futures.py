from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
from loguru import logger

from db.hub_store import HubDB
from scanner.trending import TrendingCoin


class BinanceFuturesScanner:
    """Binance futures-native market scanner with DB-backed rolling memory.

    This scanner is intentionally independent from proposal routing for now.
    It continuously samples Binance futures tickers/funding, persists snapshots,
    and computes confidence-weighted hot movers from the rolling history.
    """

    TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"

    def __init__(
        self,
        *,
        poll_interval: int = 60,
        min_quote_volume: float = 5_000_000.0,
        top_movers_count: int = 15,
        history_hours: int = 24,
        retention_days: int = 7,
        db_path: Path = Path("data/hub.db"),
        enabled: bool = True,
    ) -> None:
        self.poll_interval = max(30, poll_interval)
        self.min_quote_volume = max(0.0, min_quote_volume)
        self.top_movers_count = max(1, top_movers_count)
        self.history_hours = max(1, history_hours)
        self.retention_days = max(1, retention_days)
        self.enabled = enabled
        self._db_path = db_path

        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._last_cleanup = datetime.now(UTC)

        self._binance_symbols: set[str] = set()
        self._samples: dict[str, deque[tuple[datetime, float, float, float, float]]] = defaultdict(
            lambda: deque(maxlen=self.history_hours * 60 + 30)
        )
        self._latest_scan: list[TrendingCoin] = []
        self._hot_movers: list[TrendingCoin] = []

    def set_exchange_symbols(self, symbols: set[str] | list[str]) -> None:
        """Reuse monitor-fetched BINANCE symbol list to avoid duplicate existence checks."""
        self._binance_symbols = {s.upper().split(":")[0] for s in symbols}

    @property
    def latest_scan(self) -> list[TrendingCoin]:
        return list(self._latest_scan)

    @property
    def hot_movers(self) -> list[TrendingCoin]:
        return list(self._hot_movers)

    async def start(self) -> None:
        if not self.enabled or self._running:
            return
        self._running = True
        await self._load_recent_history()
        self._task = asyncio.create_task(self._scan_loop())
        logger.info(
            "Binance futures scanner started (interval={}s, min_quote_vol=${:.0f}, history={}h)",
            self.poll_interval,
            self.min_quote_volume,
            self.history_hours,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _scan_loop(self) -> None:
        while self._running:
            try:
                await self._tick_once()
            except Exception as e:
                logger.warning("Binance futures scanner tick failed: {}", e)
            await asyncio.sleep(self.poll_interval)

    async def _tick_once(self) -> None:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with asyncio.TaskGroup() as tg:
                t1 = tg.create_task(self._fetch_json(sess, self.TICKER_URL))
                t2 = tg.create_task(self._fetch_json(sess, self.FUNDING_URL))
            tickers_raw = t1.result()
            funding_raw = t2.result()

        funding_map: dict[str, float] = {}
        for row in funding_raw if isinstance(funding_raw, list) else []:
            sym = str(row.get("symbol", "")).upper()
            if not sym:
                continue
            funding_map[sym] = self._to_float(row.get("lastFundingRate"))

        now = datetime.now(UTC)
        ts = now.replace(second=0, microsecond=0)
        rows: list[dict[str, Any]] = []

        for row in tickers_raw if isinstance(tickers_raw, list) else []:
            raw = str(row.get("symbol", "")).upper()
            if not raw.endswith("USDT") or "_" in raw:
                continue
            pair = f"{raw[:-4]}/USDT"
            if self._binance_symbols and pair not in self._binance_symbols:
                continue

            price = self._to_float(row.get("lastPrice"))
            quote_volume = self._to_float(row.get("quoteVolume"))
            change_24h = self._to_float(row.get("priceChangePercent"))
            funding_rate = funding_map.get(raw, 0.0)

            if price <= 0.0:
                continue

            rows.append(
                {
                    "timestamp": ts.isoformat(),
                    "symbol": pair,
                    "price": price,
                    "quote_volume": quote_volume,
                    "change_24h": change_24h,
                    "funding_rate": funding_rate,
                }
            )
            self._samples[pair].append((ts, price, quote_volume, change_24h, funding_rate))

        if rows:
            self._latest_scan = self._rows_to_coins(rows)
            self._hot_movers = self._compute_hot_movers()
            self._persist_rows(rows)
            self._evict_old_samples(now - timedelta(hours=self.history_hours))
            self._maybe_cleanup_old_db_rows(now)

    async def _load_recent_history(self) -> None:
        since = (datetime.now(UTC) - timedelta(hours=self.history_hours)).isoformat()
        db = HubDB(path=self._db_path)
        try:
            db.connect()
            rows = db.load_binance_snapshots_since(since)
        finally:
            db.close()

        for row in rows:
            ts = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
            symbol = str(row["symbol"])
            self._samples[symbol].append(
                (
                    ts,
                    self._to_float(row["price"]),
                    self._to_float(row["quote_volume"]),
                    self._to_float(row["change_24h"]),
                    self._to_float(row["funding_rate"]),
                )
            )
        if rows:
            logger.info("Binance futures scanner restored {} rows from DB history", len(rows))

    @staticmethod
    async def _fetch_json(sess: aiohttp.ClientSession, url: str) -> Any:
        async with sess.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"{url} returned {resp.status}")
            return await resp.json()

    @staticmethod
    def _to_float(v: Any) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    def _rows_to_coins(self, rows: list[dict[str, Any]]) -> list[TrendingCoin]:
        out: list[TrendingCoin] = []
        for row in rows:
            out.append(
                TrendingCoin(
                    symbol=row["symbol"],
                    price=self._to_float(row["price"]),
                    volume_24h=self._to_float(row["quote_volume"]),
                    change_24h=self._to_float(row["change_24h"]),
                    name="",
                    market_cap=0.0,
                )
            )
        return out

    def _compute_hot_movers(self) -> list[TrendingCoin]:
        now = datetime.now(UTC)
        movers: list[TrendingCoin] = []
        for symbol, samples in self._samples.items():
            if not samples:
                continue
            _, price, quote_volume, change_24h, funding_rate = samples[-1]
            if quote_volume < self.min_quote_volume:
                continue

            p_5m = self._price_ago(samples, now - timedelta(minutes=5))
            p_1h = self._price_ago(samples, now - timedelta(hours=1))
            chg_5m = self._pct(price, p_5m)
            chg_1h = self._pct(price, p_1h)

            vol_avg = sum(s[2] for s in samples) / max(1, len(samples))
            vol_accel = quote_volume / max(1.0, vol_avg)

            raw_score = abs(chg_1h) * 3.0 + abs(chg_5m) * 1.5 + abs(change_24h) * 0.25
            raw_score += min(3.0, max(0.0, vol_accel - 1.0))
            raw_score += min(2.0, abs(funding_rate) * 10_000.0) * 0.25

            confidence = self._confidence(len(samples))
            score = raw_score * confidence

            movers.append(
                TrendingCoin(
                    symbol=symbol,
                    price=price,
                    volume_24h=quote_volume,
                    change_5m=chg_5m,
                    change_1h=chg_1h,
                    change_24h=change_24h,
                    market_cap=0.0,
                    name=f"binance_futures|funding={funding_rate:.6f}|conf={confidence:.2f}|score={score:.2f}",
                )
            )

        movers.sort(key=lambda c: abs(c.momentum_score), reverse=True)
        return movers[: self.top_movers_count]

    @staticmethod
    def _price_ago(samples: deque[tuple[datetime, float, float, float, float]], cutoff: datetime) -> float:
        for ts, price, *_ in reversed(samples):
            if ts <= cutoff:
                return price
        return samples[0][1]

    @staticmethod
    def _pct(cur: float, prev: float) -> float:
        if prev <= 0:
            return 0.0
        return ((cur - prev) / prev) * 100.0

    @staticmethod
    def _confidence(sample_count: int) -> float:
        # Gradual confidence build (no hard warmup):
        # 5 samples => ~0.18, 30 samples => ~0.55, 60+ => up to 1.0.
        if sample_count <= 0:
            return 0.1
        return min(1.0, 0.1 + (sample_count / 60.0) * 0.9)

    def _persist_rows(self, rows: list[dict[str, Any]]) -> None:
        db = HubDB(path=self._db_path)
        try:
            db.connect()
            db.save_binance_snapshots(rows)
        finally:
            db.close()

    def _maybe_cleanup_old_db_rows(self, now: datetime) -> None:
        if (now - self._last_cleanup) < timedelta(hours=1):
            return
        cutoff = (now - timedelta(days=self.retention_days)).isoformat()
        db = HubDB(path=self._db_path)
        try:
            db.connect()
            removed = db.cleanup_binance_snapshots_before(cutoff)
            if removed:
                logger.info("Binance futures scanner cleanup removed {} old rows", removed)
        finally:
            db.close()
        self._last_cleanup = now

    def _evict_old_samples(self, cutoff: datetime) -> None:
        for symbol in list(self._samples.keys()):
            dq = self._samples[symbol]
            while dq and dq[0][0] < cutoff:
                dq.popleft()
            if not dq:
                self._samples.pop(symbol, None)
