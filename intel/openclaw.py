from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

import aiohttp
from loguru import logger
from pydantic import BaseModel, Field, ValidationError


class OpenClawRegimeCommentary(BaseModel):
    regime: str = "unknown"
    confidence: float = 0.0
    why: list[str] = []


class OpenClawIdeaBrief(BaseModel):
    symbol: str = ""
    side: str = "neutral"
    timeframe: str = ""
    confidence: float = 0.0
    thesis: str = ""
    evidence: list[str] = []
    risk_notes: list[str] = []


class OpenClawAltData(BaseModel):
    long_short_ratio: float = 0.0
    liquidations_24h_usd: float = 0.0
    open_interest_24h_usd: float = 0.0
    sentiment_score: int = 50


class OpenClawTriageEntry(BaseModel):
    severity: str = "info"
    component: str = ""
    issue: str = ""
    likely_root_cause: str = ""
    suggested_checks: list[str] = []


class OpenClawExperiment(BaseModel):
    name: str = ""
    safety: str = "paper_only"
    expected_effect: str = ""
    rollback_rule: str = ""


class OpenClawSnapshot(BaseModel):
    as_of: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    regime_commentary: OpenClawRegimeCommentary = OpenClawRegimeCommentary()
    idea_briefs: list[OpenClawIdeaBrief] = []
    alt_data: OpenClawAltData = OpenClawAltData()
    failure_triage: list[OpenClawTriageEntry] = []
    experiments: list[OpenClawExperiment] = []
    fetched_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class OpenClawClient:
    """Pull advisory intelligence from an OpenClaw service endpoint."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        base_url: str = "",
        token: str = "",
        poll_interval: int = 120,
        timeout_seconds: int = 8,
    ) -> None:
        self.enabled = bool(enabled and base_url.strip())
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.poll_interval = max(15, int(poll_interval))
        self.timeout_seconds = max(2, int(timeout_seconds))
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._latest: OpenClawSnapshot | None = None

    @property
    def latest(self) -> OpenClawSnapshot | None:
        return self._latest

    async def start(self) -> None:
        if not self.enabled:
            logger.info("OpenClaw client disabled")
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="openclaw_poll")
        logger.info("OpenClaw client started (url={}, poll={}s)", self.base_url, self.poll_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self.fetch_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("OpenClaw fetch error: {}", e)
            await asyncio.sleep(self.poll_interval)

    async def fetch_once(self) -> OpenClawSnapshot | None:
        if not self.enabled:
            return None
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(self.base_url, headers=headers) as resp,
        ):
            if resp.status != 200:
                logger.warning("OpenClaw returned HTTP {}", resp.status)
                return None
            raw = await resp.json()

        try:
            parsed = OpenClawSnapshot.model_validate(raw)
        except ValidationError as e:
            logger.warning("OpenClaw payload validation failed: {}", e)
            return None

        parsed.fetched_at = datetime.now(UTC).isoformat()
        self._latest = parsed
        return parsed
