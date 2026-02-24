from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import UTC, datetime

import aiohttp
from loguru import logger
from pydantic import BaseModel

try:
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover - optional at import time
    async_playwright = None


class LiquidationSnapshot(BaseModel):
    total_24h: float = 0.0  # total liquidations in USD (24h)
    total_24h_text: str = ""
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

    WEB_URL = "https://www.coinglass.com/liquidations"
    # Guard against parsing label fragments like "24h" as dollar values.
    MIN_PLAUSIBLE_TOTAL_24H_USD = 1_000_000.0

    def __init__(self, poll_interval: int = 300, api_key: str = ""):
        self.poll_interval = poll_interval
        self.api_key = api_key  # kept for config compatibility; not used
        self._latest: LiquidationSnapshot | None = None
        self._coinglass_latest: LiquidationSnapshot | None = None
        self._running = False
        self._history: list[LiquidationSnapshot] = []
        self._background_tasks: list[asyncio.Task[None]] = []
        self._warned_no_headless = False

    @staticmethod
    def _format_usd_compact(amount: float) -> str:
        if amount <= 0:
            return "$0"
        if amount >= 1e9:
            return f"${amount / 1e9:.2f}B"
        if amount >= 1e6:
            return f"${amount / 1e6:.2f}M"
        if amount >= 1e3:
            return f"${amount / 1e3:.2f}K"
        return f"${amount:.0f}"

    async def start(self) -> None:
        self._running = True
        self._background_tasks.append(asyncio.create_task(self._poll_loop()))
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
            label_matches = list(re.finditer(r"24h\s*Rekt", html, flags=re.IGNORECASE))
            if not label_matches:
                return None

            # Search each "24h Rekt" section independently to avoid accidentally
            # picking unrelated "$0" numbers from explanatory text farther away.
            for match in label_matches:
                window = html[match.start() : match.start() + 6000]

                # Strict first pass: compact K/M/B values right after "$" (the card value format).
                compact_tokens = re.findall(
                    r"\$(?:[^0-9]{0,120})([0-9][0-9,]*(?:\.[0-9]+)?\s*(?:[kmb]|million|billion|thousand))",
                    window,
                    flags=re.IGNORECASE,
                )
                compact_values = [_parse_compact_usd(t) for t in compact_tokens]
                compact_values = [v for v in compact_values if v > 0]
                if compact_values:
                    total = compact_values[0]
                    if total < self.MIN_PLAUSIBLE_TOTAL_24H_USD:
                        continue
                    long_24h = compact_values[1] if len(compact_values) > 1 else 0.0
                    short_24h = compact_values[2] if len(compact_values) > 2 else 0.0
                    if long_24h <= 0 and short_24h <= 0:
                        long_24h = total * 0.5
                        short_24h = total * 0.5
                    return LiquidationSnapshot(
                        total_24h=total,
                        total_24h_text=f"${compact_tokens[0].strip()}",
                        long_24h=long_24h,
                        short_24h=short_24h,
                        timestamp=datetime.now(UTC),
                    )

                # Fallback for rare plain USD formats with no suffix.
                nums = re.findall(
                    r"\$(?:[^0-9]{0,120})([0-9][0-9,]*(?:\.[0-9]+)?)",
                    window,
                    flags=re.IGNORECASE,
                )
                parsed = [_parse_compact_usd(n) for n in nums]
                parsed = [p for p in parsed if p > 0]
                if not parsed:
                    continue
                total = parsed[0]
                if total < self.MIN_PLAUSIBLE_TOTAL_24H_USD:
                    continue
                long_24h = parsed[1] if len(parsed) > 1 else 0.0
                short_24h = parsed[2] if len(parsed) > 2 else 0.0
                if long_24h <= 0 and short_24h <= 0:
                    long_24h = total * 0.5
                    short_24h = total * 0.5
                return LiquidationSnapshot(
                    total_24h=total,
                    total_24h_text=self._format_usd_compact(total),
                    long_24h=long_24h,
                    short_24h=short_24h,
                    timestamp=datetime.now(UTC),
                )

            return None

        async def _fetch_headless_html() -> str | None:
            if async_playwright is None:
                if not self._warned_no_headless:
                    logger.warning("Playwright not installed; liquidation headless mode unavailable")
                    self._warned_no_headless = True
                return None
            try:
                async with async_playwright() as p:
                    browser = await p.chromium.launch(
                        headless=True,
                        args=["--no-sandbox", "--disable-dev-shm-usage"],
                    )
                    context = await browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/122.0.0.0 Safari/537.36"
                        )
                    )
                    page = await context.new_page()
                    await page.goto(self.WEB_URL, wait_until="domcontentloaded", timeout=30_000)
                    await page.wait_for_timeout(3000)
                    html = await page.content()
                    await browser.close()
                    return html
            except Exception as e:
                logger.warning("Liquidation headless render failed: {}", e)
                return None

        snap: LiquidationSnapshot | None = None
        try:
            # Primary: JS-rendered DOM via headless browser.
            headless_html = await _fetch_headless_html()
            if headless_html:
                snap = _parse_liquidations_from_html(headless_html)

            # Fallback: raw HTTP HTML.
            async with aiohttp.ClientSession() as session:
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

    def _rebuild_combined_snapshot(self) -> None:
        """Use CoinGlass webpage snapshot only."""
        self._latest = self._coinglass_latest

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
