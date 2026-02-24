from __future__ import annotations

import asyncio
import contextlib
import json
import re
from datetime import UTC, datetime

import aiohttp
from loguru import logger
from pydantic import BaseModel


class LiquidationSnapshot(BaseModel):
    total_24h: float = 0.0  # total liquidations in USD (24h)
    long_24h: float = 0.0
    short_24h: float = 0.0
    total_1h: float = 0.0
    long_1h: float = 0.0
    short_1h: float = 0.0
    timestamp: datetime = datetime.now(UTC)

    @property
    def long_ratio_24h(self) -> float:
        if self.total_24h == 0:
            return 0.5
        return self.long_24h / self.total_24h

    @property
    def is_mass_liquidation(self) -> bool:
        """$1B+ in 24h = mass liquidation event (potential reversal zone)."""
        return self.total_24h >= 1_000_000_000

    @property
    def is_heavy_liquidation(self) -> bool:
        return self.total_24h >= 500_000_000

    @property
    def dominant_side(self) -> str:
        """Which side is getting liquidated more -- that's the exhaustion side."""
        if self.long_ratio_24h > 0.6:
            return "longs"  # longs getting rekt = bottom might be near
        if self.long_ratio_24h < 0.4:
            return "shorts"  # shorts getting rekt = top might be near
        return "balanced"


class LiquidationMonitor:
    """Monitors crypto-wide liquidation data from CoinGlass.

    Trading rules:
    - $1B+ liquidations in 24h: mass capitulation / squeeze. Look for reversal.
    - Longs dominant: potential bottom (everyone who could sell has been liquidated)
    - Shorts dominant: potential top (short squeeze exhausted)
    - 1h spike > $100M: immediate volatility, spike scalp territory

    Source: https://www.coinglass.com/liquidations
    """

    API_URL_V4 = "https://open-api-v4.coinglass.com/api/futures/liquidation/exchange-list"
    API_URL_V3 = "https://open-api-v3.coinglass.com/api/futures/liquidation/exchange-list"
    WEB_URL = "https://www.coinglass.com/LiquidationData"
    # Guard against parsing label fragments like "24h" as dollar values.
    MIN_PLAUSIBLE_TOTAL_24H_USD = 1_000_000.0
    BINANCE_LIQ_STREAM = "wss://fstream.binance.com/ws/!forceOrder@arr"
    BYBIT_LIQ_STREAM = "wss://stream.bybit.com/v5/public/linear"
    BYBIT_TOP_SYMBOLS: tuple[str, ...] = (
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "XRPUSDT",
        "BNBUSDT",
        "DOGEUSDT",
        "ADAUSDT",
        "AVAXUSDT",
        "LINKUSDT",
        "LTCUSDT",
        "TRXUSDT",
        "BCHUSDT",
        "DOTUSDT",
        "MATICUSDT",
        "ATOMUSDT",
        "NEARUSDT",
        "ETCUSDT",
        "APTUSDT",
        "ARBUSDT",
        "OPUSDT",
    )

    def __init__(self, poll_interval: int = 300, api_key: str = ""):
        self.poll_interval = poll_interval
        self.api_key = api_key
        self._latest: LiquidationSnapshot | None = None
        self._coinglass_latest: LiquidationSnapshot | None = None
        self._streams_latest: LiquidationSnapshot | None = None
        self._running = False
        self._history: list[LiquidationSnapshot] = []
        self._background_tasks: list[asyncio.Task[None]] = []
        self._warned_no_key = False
        # Public fallback source: aggregate force-liquidation notional from WS streams.
        self._fallback_events: list[tuple[datetime, float, float]] = []

    async def start(self) -> None:
        self._running = True
        self._background_tasks.append(asyncio.create_task(self._poll_loop()))
        self._background_tasks.append(asyncio.create_task(self._binance_ws_loop()))
        self._background_tasks.append(asyncio.create_task(self._bybit_ws_loop()))
        logger.info("Liquidation monitor started (poll={}s)", self.poll_interval)

    async def stop(self) -> None:
        self._running = False
        for task in self._background_tasks:
            task.cancel()
        for task in self._background_tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._background_tasks.clear()

    @property
    def latest(self) -> LiquidationSnapshot | None:
        return self._latest

    def is_reversal_zone(self) -> bool:
        if not self._latest:
            return False
        return self._latest.is_mass_liquidation

    def reversal_bias(self) -> str:
        """If mass liq of longs -> buy bias (bottom). If shorts -> sell bias (top)."""
        if not self._latest or not self._latest.is_heavy_liquidation:
            return "neutral"
        dom = self._latest.dominant_side
        if dom == "longs":
            return "long"  # longs got liquidated = potential bottom
        if dom == "shorts":
            return "short"  # shorts got liquidated = potential top
        return "neutral"

    def aggression_boost(self) -> float:
        """Boost position sizing during mass liquidation events (reversal opportunity)."""
        if not self._latest:
            return 1.0
        if self._latest.is_mass_liquidation:
            return 1.3
        if self._latest.is_heavy_liquidation:
            return 1.1
        return 1.0

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._fetch()
            except Exception as e:
                logger.error("Liquidation fetch error: {}", e)
            await asyncio.sleep(self.poll_interval)

    async def _fetch(self) -> None:
        if not self.api_key and not self._warned_no_key:
            logger.warning("No CoinGlass API key set (COINGLASS_API_KEY). Using webpage fallback for liquidations.")
            self._warned_no_key = True

        def _num(v: object) -> float:
            if v is None:
                return 0.0
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, str):
                cleaned = v.replace(",", "").replace("$", "").strip()
                if not cleaned:
                    return 0.0
                return float(cleaned)
            return 0.0

        def _pick(item: dict, keys: tuple[str, ...]) -> float:
            for k in keys:
                if k in item:
                    return _num(item.get(k))
            return 0.0

        def _parse_compact_usd(token: str) -> float:
            t = (token or "").strip().lower().replace(",", "")
            if not t:
                return 0.0
            mult = 1.0
            if t.endswith("billion"):
                mult = 1e9
                t = t[:-7].strip()
            elif t.endswith("million"):
                mult = 1e6
                t = t[:-7].strip()
            elif t.endswith("thousand"):
                mult = 1e3
                t = t[:-8].strip()
            elif t.endswith("b"):
                mult = 1e9
                t = t[:-1].strip()
            elif t.endswith("m"):
                mult = 1e6
                t = t[:-1].strip()
            elif t.endswith("k"):
                mult = 1e3
                t = t[:-1].strip()
            try:
                return float(t) * mult
            except (TypeError, ValueError):
                return 0.0

        def _parse_liquidations_from_html(html: str) -> LiquidationSnapshot | None:
            idx = html.lower().find("24h rekt")
            if idx < 0:
                return None
            window = html[idx : idx + 1600]
            # Prefer the exact DOM pattern seen on CoinGlass cards:
            # label "24h Rekt" followed by a Number container with "$" and value text.
            dom_match = re.search(
                r"24h\s*Rekt.*?\$[^0-9]*([0-9][0-9,]*(?:\.[0-9]+)?\s*(?:[kmb]|million|billion|thousand)?)",
                window,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if dom_match:
                total = _parse_compact_usd(dom_match.group(1))
                if total >= self.MIN_PLAUSIBLE_TOTAL_24H_USD:
                    long_match = re.search(
                        r"Long[^0-9$]*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?\s*(?:[kmb]|million|billion|thousand)?)",
                        window,
                        flags=re.IGNORECASE | re.DOTALL,
                    )
                    short_match = re.search(
                        r"Short[^0-9$]*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?\s*(?:[kmb]|million|billion|thousand)?)",
                        window,
                        flags=re.IGNORECASE | re.DOTALL,
                    )
                    long_24h = _parse_compact_usd(long_match.group(1)) if long_match else 0.0
                    short_24h = _parse_compact_usd(short_match.group(1)) if short_match else 0.0
                    if long_24h <= 0 and short_24h <= 0:
                        long_24h = total * 0.5
                        short_24h = total * 0.5
                    return LiquidationSnapshot(
                        total_24h=total,
                        long_24h=long_24h,
                        short_24h=short_24h,
                        timestamp=datetime.now(UTC),
                    )
            nums = re.findall(
                r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?\s*(?:[kmb]|million|billion|thousand)?)",
                window,
                flags=re.IGNORECASE,
            )
            parsed = [_parse_compact_usd(n) for n in nums]
            parsed = [p for p in parsed if p > 0]
            if not parsed:
                return None
            total = parsed[0]
            if total < self.MIN_PLAUSIBLE_TOTAL_24H_USD:
                return None
            long_24h = parsed[1] if len(parsed) > 1 else 0.0
            short_24h = parsed[2] if len(parsed) > 2 else 0.0
            if long_24h <= 0 and short_24h <= 0:
                long_24h = total * 0.5
                short_24h = total * 0.5
            return LiquidationSnapshot(
                total_24h=total,
                long_24h=long_24h,
                short_24h=short_24h,
                timestamp=datetime.now(UTC),
            )

        snap: LiquidationSnapshot | None = None
        try:
            async with aiohttp.ClientSession() as session:
                if self.api_key:
                    headers = {"CG-API-KEY": self.api_key}
                    for url in (self.API_URL_V4, self.API_URL_V3):
                        try:
                            async with session.get(
                                url,
                                headers=headers,
                                params={"symbol": "", "range": "24h"},
                                timeout=aiohttp.ClientTimeout(total=15),
                            ) as resp:
                                if resp.status != 200:
                                    continue
                                data = await resp.json()
                                if not isinstance(data, dict):
                                    continue
                                info = data.get("data", [])
                                # Some API variants wrap rows in {"list": [...]}.
                                if isinstance(info, dict) and isinstance(info.get("list"), list):
                                    info = info.get("list", [])

                                api_snap = LiquidationSnapshot(timestamp=datetime.now(UTC))
                                if isinstance(info, list):
                                    for item in info:
                                        if not isinstance(item, dict):
                                            continue
                                        ex = str(item.get("exchange", item.get("exchangeName", ""))).strip()
                                        if ex.lower() == "all":
                                            api_snap.total_24h = _pick(
                                                item, ("liquidation_usd", "liquidationUsd", "totalUsd")
                                            )
                                            api_snap.long_24h = _pick(
                                                item, ("long_liquidation_usd", "longLiquidationUsd", "longUsd")
                                            )
                                            api_snap.short_24h = _pick(
                                                item, ("short_liquidation_usd", "shortLiquidationUsd", "shortUsd")
                                            )
                                            break
                                    else:
                                        for item in info:
                                            if not isinstance(item, dict):
                                                continue
                                            api_snap.total_24h += _pick(
                                                item, ("liquidation_usd", "liquidationUsd", "totalUsd")
                                            )
                                            api_snap.long_24h += _pick(
                                                item, ("long_liquidation_usd", "longLiquidationUsd", "longUsd")
                                            )
                                            api_snap.short_24h += _pick(
                                                item, ("short_liquidation_usd", "shortLiquidationUsd", "shortUsd")
                                            )
                                elif isinstance(info, dict):
                                    api_snap.total_24h = _pick(info, ("liquidation_usd", "liquidationUsd", "totalUsd"))
                                    api_snap.long_24h = _pick(
                                        info, ("long_liquidation_usd", "longLiquidationUsd", "longUsd")
                                    )
                                    api_snap.short_24h = _pick(
                                        info, ("short_liquidation_usd", "shortLiquidationUsd", "shortUsd")
                                    )
                                if api_snap.total_24h > 0:
                                    snap = api_snap
                                    break
                        except Exception:
                            continue

                # If key is missing/invalid/unavailable, scrape webpage value.
                if snap is None:
                    try:
                        async with session.get(
                            self.WEB_URL,
                            headers={
                                "User-Agent": (
                                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/122.0.0.0 Safari/537.36"
                                )
                            },
                            timeout=aiohttp.ClientTimeout(total=20),
                        ) as resp:
                            if resp.status == 200:
                                html = await resp.text()
                                snap = _parse_liquidations_from_html(html)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("Liquidation fetch failed: {}", e)
            return

        if snap is None:
            logger.warning("Liquidation sources returned empty data")
            return

        self._coinglass_latest = snap
        self._rebuild_combined_snapshot()
        if self._latest:
            self._history.append(self._latest)
        if len(self._history) > 288:  # ~24h at 5min intervals
            self._history = self._history[-288:]

        s = self._latest or snap
        if s.is_mass_liquidation:
            logger.warning(
                "MASS LIQUIDATION: ${:.0f}B in 24h | longs: {:.0f}% | shorts: {:.0f}%",
                s.total_24h / 1e9,
                s.long_ratio_24h * 100,
                (1 - s.long_ratio_24h) * 100,
            )
        else:
            logger.info(
                "Liquidations 24h: ${:.0f}M | L:{:.0f}% S:{:.0f}% | dom: {}",
                s.total_24h / 1e6,
                s.long_ratio_24h * 100,
                (1 - s.long_ratio_24h) * 100,
                s.dominant_side,
            )

    def _refresh_from_streams(self) -> None:
        now = datetime.now(UTC)
        cutoff_24h = now.timestamp() - (24 * 3600)
        self._fallback_events = [e for e in self._fallback_events if e[0].timestamp() >= cutoff_24h]
        if not self._fallback_events:
            self._streams_latest = None
            self._rebuild_combined_snapshot()
            return
        long_24h = sum(e[1] for e in self._fallback_events)
        short_24h = sum(e[2] for e in self._fallback_events)
        total_24h = long_24h + short_24h
        cutoff_1h = now.timestamp() - 3600
        long_1h = sum(e[1] for e in self._fallback_events if e[0].timestamp() >= cutoff_1h)
        short_1h = sum(e[2] for e in self._fallback_events if e[0].timestamp() >= cutoff_1h)
        total_1h = long_1h + short_1h
        self._streams_latest = LiquidationSnapshot(
            total_24h=total_24h,
            long_24h=long_24h,
            short_24h=short_24h,
            total_1h=total_1h,
            long_1h=long_1h,
            short_1h=short_1h,
            timestamp=now,
        )
        self._rebuild_combined_snapshot()

    def _rebuild_combined_snapshot(self) -> None:
        """Prefer CoinGlass/API/web snapshot; fall back to exchange streams."""
        self._latest = self._coinglass_latest or self._streams_latest

    def _consume_binance_force_order(self, payload: dict) -> None:
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        order = data.get("o", {}) if isinstance(data, dict) else {}
        if not isinstance(order, dict):
            return

        side = str(order.get("S", "")).upper()
        qty_s = order.get("z", order.get("l", order.get("q", 0)))
        px_s = order.get("ap", order.get("p", 0))
        try:
            qty = float(qty_s or 0)
            px = float(px_s or 0)
        except (TypeError, ValueError):
            return
        if qty <= 0 or px <= 0:
            return

        ts_ms = order.get("T", data.get("E"))
        try:
            ts = datetime.fromtimestamp(float(ts_ms) / 1000, tz=UTC) if ts_ms else datetime.now(UTC)
        except (TypeError, ValueError):
            ts = datetime.now(UTC)

        usd = qty * px
        long_usd = usd if side == "SELL" else 0.0
        short_usd = usd if side == "BUY" else 0.0
        self._fallback_events.append((ts, long_usd, short_usd))
        self._refresh_from_streams()

    def _consume_bybit_liquidation(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        rows = payload.get("data", [])
        if not isinstance(rows, list):
            return

        for row in rows:
            if not isinstance(row, dict):
                continue
            side = str(row.get("S", "")).upper()
            qty_s = row.get("v", 0)
            px_s = row.get("p", 0)
            try:
                qty = float(qty_s or 0)
                px = float(px_s or 0)
            except (TypeError, ValueError):
                continue
            if qty <= 0 or px <= 0:
                continue

            ts_ms = row.get("T", payload.get("ts"))
            try:
                ts = datetime.fromtimestamp(float(ts_ms) / 1000, tz=UTC) if ts_ms else datetime.now(UTC)
            except (TypeError, ValueError):
                ts = datetime.now(UTC)

            usd = qty * px
            # Bybit allLiquidation docs: "BUY" indicates long position liquidation.
            long_usd = usd if side == "BUY" else 0.0
            short_usd = usd if side == "SELL" else 0.0
            self._fallback_events.append((ts, long_usd, short_usd))

        self._refresh_from_streams()

    async def _binance_ws_loop(self) -> None:
        while self._running:
            try:
                timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_read=35)
                async with (
                    aiohttp.ClientSession(timeout=timeout) as session,
                    session.ws_connect(self.BINANCE_LIQ_STREAM, heartbeat=20) as ws,
                ):
                    logger.info("Liquidation fallback stream connected: Binance !forceOrder@arr")
                    async for msg in ws:
                        if not self._running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                payload = json.loads(msg.data)
                            except (TypeError, json.JSONDecodeError):
                                continue
                            self._consume_binance_force_order(payload)
                        elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                            break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Binance liquidation stream error: {}", e)
            await asyncio.sleep(3)

    async def _bybit_ws_loop(self) -> None:
        while self._running:
            try:
                timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_read=35)
                async with (
                    aiohttp.ClientSession(timeout=timeout) as session,
                    session.ws_connect(self.BYBIT_LIQ_STREAM, heartbeat=20) as ws,
                ):
                    args = [f"allLiquidation.{s}" for s in self.BYBIT_TOP_SYMBOLS]
                    await ws.send_json({"op": "subscribe", "args": args})
                    logger.info("Liquidation fallback stream connected: Bybit allLiquidation ({} symbols)", len(args))
                    async for msg in ws:
                        if not self._running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                payload = json.loads(msg.data)
                            except (TypeError, json.JSONDecodeError):
                                continue
                            self._consume_bybit_liquidation(payload)
                        elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                            break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Bybit liquidation stream error: {}", e)
            await asyncio.sleep(3)

    def summary(self) -> str:
        if not self._latest:
            return "Liquidations: no data"
        s = self._latest
        tag = " ** MASS LIQ **" if s.is_mass_liquidation else ""
        return (
            f"Liq 24h: ${s.total_24h / 1e6:.0f}M | "
            f"L:{s.long_ratio_24h:.0%} S:{1 - s.long_ratio_24h:.0%} | "
            f"dom: {s.dominant_side}{tag}"
        )
