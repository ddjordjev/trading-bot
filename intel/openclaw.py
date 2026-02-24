from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime

import aiohttp
from loguru import logger
from pydantic import BaseModel, Field, ValidationError


class OpenClawRegimeCommentary(BaseModel):
    regime: str = Field(default="unknown", max_length=32)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    why: list[str] = Field(default_factory=list, max_length=20)


class OpenClawIdeaBrief(BaseModel):
    symbol: str = Field(default="", max_length=32)
    side: str = Field(default="neutral", max_length=16)
    timeframe: str = Field(default="", max_length=32)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    thesis: str = Field(default="", max_length=512)
    evidence: list[str] = Field(default_factory=list, max_length=20)
    risk_notes: list[str] = Field(default_factory=list, max_length=20)


class OpenClawAltData(BaseModel):
    long_short_ratio: float = Field(default=0.0, ge=0.0, le=10.0)
    liquidations_24h_usd: float = Field(default=0.0, ge=0.0)
    open_interest_24h_usd: float = Field(default=0.0, ge=0.0)
    sentiment_score: int = Field(default=50, ge=0, le=100)


class OpenClawTriageEntry(BaseModel):
    severity: str = Field(default="info", max_length=16)
    component: str = Field(default="", max_length=64)
    issue: str = Field(default="", max_length=512)
    likely_root_cause: str = Field(default="", max_length=512)
    suggested_checks: list[str] = Field(default_factory=list, max_length=20)


class OpenClawExperiment(BaseModel):
    name: str = Field(default="", max_length=128)
    safety: str = Field(default="paper_only", max_length=32)
    expected_effect: str = Field(default="", max_length=512)
    rollback_rule: str = Field(default="", max_length=512)


class OpenClawSnapshot(BaseModel):
    as_of: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    regime_commentary: OpenClawRegimeCommentary = OpenClawRegimeCommentary()
    idea_briefs: list[OpenClawIdeaBrief] = Field(default_factory=list, max_length=25)
    alt_data: OpenClawAltData = OpenClawAltData()
    failure_triage: list[OpenClawTriageEntry] = Field(default_factory=list, max_length=25)
    experiments: list[OpenClawExperiment] = Field(default_factory=list, max_length=25)
    fetched_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class OpenClawClient:
    """Pull advisory intelligence from an OpenClaw service endpoint."""

    _MAX_RESPONSE_BYTES = 256_000

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

    @property
    def is_enabled(self) -> bool:
        return self.enabled

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self.enabled:
            logger.info("OpenClaw client disabled")
            return
        if self.is_running:
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

    async def set_enabled(self, enabled: bool) -> bool:
        """Enable/disable polling at runtime and clear cached advisory data on disable."""
        should_enable = bool(enabled and self.base_url.strip())
        if not should_enable:
            self.enabled = False
            await self.stop()
            self._latest = None
            logger.info("OpenClaw client disabled via runtime toggle")
            return False

        self.enabled = True
        if not self.is_running:
            await self.start()
        return True

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self.fetch_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "OpenClaw fetch error (url={}, timeout={}s, type={}): {!r}",
                    self.base_url,
                    self.timeout_seconds,
                    type(e).__name__,
                    e,
                )
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
                self._latest = None
                return None
            body = await resp.read()
            if len(body) > self._MAX_RESPONSE_BYTES:
                logger.warning("OpenClaw payload too large: {} bytes", len(body))
                self._latest = None
                return None
            raw = json.loads(body.decode("utf-8"))

        try:
            parsed = OpenClawSnapshot.model_validate(raw)
        except ValidationError as e:
            logger.warning("OpenClaw payload validation failed: {}", e)
            self._latest = None
            return None

        parsed.fetched_at = datetime.now(UTC).isoformat()
        self._latest = parsed
        return parsed
