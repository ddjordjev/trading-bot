from __future__ import annotations

import asyncio
import contextlib
import sqlite3
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

    It continuously samples Binance futures tickers/funding, persists snapshots,
    and computes confidence-weighted hot movers from the rolling history.
    """

    TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
    HORIZONS: tuple[tuple[str, timedelta], ...] = (
        ("1m", timedelta(minutes=1)),
        ("5m", timedelta(minutes=5)),
        ("1h", timedelta(hours=1)),
        ("4h", timedelta(hours=4)),
        ("1d", timedelta(days=1)),
        ("1w", timedelta(days=7)),
        ("3w", timedelta(days=21)),
        ("1mo", timedelta(days=30)),
        ("3mo", timedelta(days=90)),
        ("1y", timedelta(days=365)),
    )

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
        self._states: dict[str, dict[str, Any]] = {}
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
        await self._load_symbol_states()
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
        state_rows: list[dict[str, Any]] = []

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
            state_rows.append(
                self._update_symbol_state(
                    symbol=pair,
                    ts=ts,
                    price=price,
                    quote_volume=quote_volume,
                    change_24h=change_24h,
                    funding_rate=funding_rate,
                )
            )

        if rows:
            self._latest_scan = self._rows_to_coins(rows)
            self._hot_movers = self._compute_hot_movers()
            await self._persist_tick(rows, state_rows, now)
            self._evict_old_samples(now - timedelta(hours=self.history_hours))

    @staticmethod
    def _is_retryable_db_error(exc: Exception) -> bool:
        if not isinstance(exc, sqlite3.OperationalError):
            return False
        msg = str(exc).lower()
        return "database is locked" in msg or "disk i/o error" in msg

    async def _persist_tick(self, rows: list[dict[str, Any]], state_rows: list[dict[str, Any]], now: datetime) -> None:
        """Persist one scanner tick with short retries on transient SQLite contention."""
        last_exc: Exception | None = None
        for attempt in range(3):
            db = HubDB(path=self._db_path)
            try:
                db.connect()
                db.save_binance_snapshots(rows)
                db.save_binance_symbol_states(state_rows)
                if (now - self._last_cleanup) >= timedelta(hours=1):
                    cutoff = (now - timedelta(days=self.retention_days)).isoformat()
                    removed = db.cleanup_binance_snapshots_before(cutoff)
                    if removed:
                        logger.info("Binance futures scanner cleanup removed {} old rows", removed)
                    self._last_cleanup = now
                return
            except Exception as exc:
                last_exc = exc
                if not self._is_retryable_db_error(exc) or attempt == 2:
                    raise
                await asyncio.sleep(0.25 * (attempt + 1))
            finally:
                db.close()
        if last_exc:
            raise last_exc

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
            self._hot_movers = self._compute_hot_movers()

    async def _load_symbol_states(self) -> None:
        db = HubDB(path=self._db_path)
        try:
            db.connect()
            rows = db.load_binance_symbol_states()
        finally:
            db.close()

        for row in rows:
            d = dict(row)
            symbol = str(d.get("symbol", ""))
            if not symbol:
                continue
            self._states[symbol] = d

        if rows:
            logger.info("Binance futures scanner restored {} symbol states", len(rows))

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
        movers: list[TrendingCoin] = []
        for symbol, state in self._states.items():
            if not symbol:
                continue
            # Guard against stale DB state: only surface symbols that are
            # currently known on the active Binance market inventory.
            if self._binance_symbols and symbol not in self._binance_symbols:
                continue
            price = self._to_float(state.get("last_price", 0.0))
            quote_volume = self._to_float(state.get("last_quote_volume", 0.0))
            change_24h = self._to_float(state.get("last_change_24h", 0.0))
            funding_rate = self._to_float(state.get("last_funding_rate", 0.0))
            if quote_volume < self.min_quote_volume:
                continue

            chg_5m = self._to_float(state.get("chg_5m", 0.0))
            chg_1h = self._to_float(state.get("chg_1h", 0.0))
            chg_1w = self._to_float(state.get("chg_1w", 0.0))
            chg_1mo = self._to_float(state.get("chg_1mo", 0.0))
            confidence = self._to_float(state.get("confidence", 0.0))
            score = self._to_float(state.get("score", 0.0))

            movers.append(
                TrendingCoin(
                    symbol=symbol,
                    price=price,
                    volume_24h=quote_volume,
                    change_5m=chg_5m,
                    change_1h=chg_1h,
                    change_24h=change_24h,
                    change_7d=chg_1w,
                    change_30d=chg_1mo,
                    market_cap=0.0,
                    cex_confidence=confidence,
                    cex_vol_accel=self._to_float(state.get("vol_accel", 0.0)),
                    cex_score=score,
                    cex_funding_rate=funding_rate,
                    cex_change_1m=self._to_float(state.get("chg_1m", 0.0)),
                    cex_change_4h=self._to_float(state.get("chg_4h", 0.0)),
                    cex_change_1d=self._to_float(state.get("chg_1d", 0.0)),
                    cex_change_1w=chg_1w,
                    cex_change_3w=self._to_float(state.get("chg_3w", 0.0)),
                    cex_change_1mo=chg_1mo,
                    cex_change_3mo=self._to_float(state.get("chg_3mo", 0.0)),
                    cex_change_1y=self._to_float(state.get("chg_1y", 0.0)),
                    name=(
                        "binance_futures"
                        f"|funding={funding_rate:.6f}"
                        f"|conf={confidence:.2f}"
                        f"|score={score:.2f}"
                        f"|1m={self._to_float(state.get('chg_1m', 0.0)):+.2f}"
                        f"|4h={self._to_float(state.get('chg_4h', 0.0)):+.2f}"
                        f"|3mo={self._to_float(state.get('chg_3mo', 0.0)):+.2f}"
                        f"|1y={self._to_float(state.get('chg_1y', 0.0)):+.2f}"
                    ),
                )
            )

        movers.sort(key=lambda c: abs(self._state_score(c.symbol)), reverse=True)
        return movers[: self.top_movers_count]

    def _state_score(self, symbol: str) -> float:
        st = self._states.get(symbol, {})
        return self._to_float(st.get("score", 0.0))

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

    def _persist_symbol_states(self, rows: list[dict[str, Any]]) -> None:
        db = HubDB(path=self._db_path)
        try:
            db.connect()
            db.save_binance_symbol_states(rows)
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

    def _update_symbol_state(
        self,
        *,
        symbol: str,
        ts: datetime,
        price: float,
        quote_volume: float,
        change_24h: float,
        funding_rate: float,
    ) -> dict[str, Any]:
        state = self._states.get(symbol)
        ts_iso = ts.isoformat()
        if not state:
            state = {
                "symbol": symbol,
                "first_seen_at": ts_iso,
                "sample_count": 0,
                "avg_quote_volume": quote_volume,
            }
            for key, _ in self.HORIZONS:
                state[f"anchor_{key}_ts"] = ts_iso
                state[f"anchor_{key}_price"] = price
                state[f"chg_{key}"] = 0.0

        first_seen = self._parse_iso(str(state.get("first_seen_at", ts_iso)), fallback=ts)
        sample_count = int(state.get("sample_count", 0)) + 1
        prev_avg = self._to_float(state.get("avg_quote_volume", quote_volume))
        avg_quote_volume = prev_avg + (quote_volume - prev_avg) / max(sample_count, 1)
        vol_accel = quote_volume / max(1.0, avg_quote_volume)
        confidence = self._confidence(sample_count)
        samples = self._samples.get(symbol, deque())

        for key, window in self.HORIZONS:
            anchor_ts_key = f"anchor_{key}_ts"
            anchor_price_key = f"anchor_{key}_price"
            change_key = f"chg_{key}"

            anchor_ts = ts
            anchor_price = price
            if (ts - first_seen) >= window and samples:
                anchor_ts, anchor_price = self._sample_at_or_before(samples, ts - window)
                state[change_key] = self._pct(price, anchor_price) if anchor_price > 0 else 0.0
            else:
                state[change_key] = 0.0

            state[anchor_ts_key] = anchor_ts.isoformat()
            state[anchor_price_key] = anchor_price

        raw_score = abs(self._to_float(state.get("chg_1h", 0.0))) * 3.0
        raw_score += abs(self._to_float(state.get("chg_5m", 0.0))) * 1.5
        raw_score += abs(change_24h) * 0.25
        raw_score += min(3.0, max(0.0, vol_accel - 1.0))
        raw_score += min(2.0, abs(funding_rate) * 10_000.0) * 0.25
        score = raw_score * confidence

        state.update(
            {
                "updated_at": ts_iso,
                "last_price": price,
                "last_quote_volume": quote_volume,
                "last_change_24h": change_24h,
                "last_funding_rate": funding_rate,
                "sample_count": sample_count,
                "avg_quote_volume": avg_quote_volume,
                "vol_accel": vol_accel,
                "confidence": confidence,
                "score": score,
            }
        )
        self._states[symbol] = state
        return state

    @staticmethod
    def _sample_at_or_before(
        samples: deque[tuple[datetime, float, float, float, float]], cutoff: datetime
    ) -> tuple[datetime, float]:
        for ts, price, *_ in reversed(samples):
            if ts <= cutoff:
                return ts, price
        first_ts, first_price, *_ = samples[0]
        return first_ts, first_price

    @staticmethod
    def _parse_iso(value: str, *, fallback: datetime) -> datetime:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return fallback
