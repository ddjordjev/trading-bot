from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
from loguru import logger

from config.settings import Settings
from db.hub_store import HubDB
from hub.state import HubState


class OpenClawAdvisorService:
    """Daily OpenClaw optimization loop with persistent suggestion lifecycle."""

    def __init__(self, *, settings: Settings, state: HubState, db_path: Path = Path("data/hub.db")) -> None:
        self.settings = settings
        self.state = state
        self.db = HubDB(path=db_path)
        self._running = False
        self._loop_sleep_seconds = 300

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.openclaw_daily_review_enabled
            and self.settings.openclaw_enabled
            and self.settings.openclaw_configured
        )

    def _daily_review_url(self) -> str:
        base = str(self.settings.openclaw_url or "").strip()
        if not base:
            return ""
        if base.endswith("/intel"):
            return base[: -len("/intel")] + "/daily-review"
        return base.rstrip("/") + "/daily-review"

    @staticmethod
    def _resolve_lane_used(response_payload: dict[str, Any]) -> str:
        """Derive persisted lane from bridge meta with paid-safety fallback."""
        if not isinstance(response_payload, dict):
            return "fallback"
        meta = response_payload.get("meta") or {}
        if not isinstance(meta, dict):
            return "fallback"

        lane = str(meta.get("lane_used", "") or "").strip().lower()
        if lane in {"paid", "local", "fallback"}:
            # Some bridge responses can keep lane=fallback while still reporting a paid model.
            if lane == "fallback" and str(meta.get("paid_model_used", "") or "").strip():
                return "paid"
            return lane

        if str(meta.get("paid_model_used", "") or "").strip():
            return "paid"
        return "fallback"

    async def start(self) -> None:
        if not self.enabled:
            logger.info("OpenClaw advisor service disabled")
            return
        self.db.connect()
        self._running = True
        await self._run_if_due("startup")
        await self._run_loop()

    async def stop(self) -> None:
        self._running = False
        self.db.close()

    async def trigger_now(self, run_kind: str = "manual") -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "message": "openclaw_daily_review_disabled"}
        return await self._run_once(run_kind=run_kind)

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._run_if_due("scheduled")
                await asyncio.sleep(self._loop_sleep_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("OpenClaw advisor loop error: {}", e)
                await asyncio.sleep(30)

    async def _run_if_due(self, run_kind: str) -> None:
        last_iso = self.db.get_latest_openclaw_report_completed_at()
        now = datetime.now(UTC)
        if not last_iso:
            await self._run_once(run_kind=run_kind)
            return
        try:
            last = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
        except Exception:
            await self._run_once(run_kind=run_kind)
            return

        if now - last >= timedelta(hours=max(1, self.settings.openclaw_daily_review_interval_hours)):
            await self._run_once(run_kind=run_kind)

    def _compact_context(self) -> dict[str, Any]:
        analytics = self.state.read_analytics()
        latest_report = self.db.get_latest_openclaw_daily_report() or {}
        previous_summary = ""
        previous_response = latest_report.get("response") if isinstance(latest_report, dict) else {}
        if isinstance(previous_response, dict):
            previous_summary = str(previous_response.get("summary", "") or "")

        return {
            "as_of": datetime.now(UTC).isoformat(),
            "notes": "compact_daily_optimization_payload",
            "trade_daily_rollup": self.db.get_openclaw_daily_trade_rollup(days=30),
            "strategy_rollup": self.db.get_openclaw_strategy_rollup(limit=20),
            "symbol_rollup": self.db.get_openclaw_symbol_rollup(limit=20),
            "analytics_weights": [w.model_dump() for w in analytics.weights[:25]],
            "analytics_patterns": list(analytics.patterns[:30]),
            "analytics_suggestions": list(analytics.suggestions[:30]),
            "openclaw_suggestion_history": self.db.get_openclaw_suggestion_context(limit=40),
            "previous_openclaw_summary": previous_summary[:1200],
        }

    async def _run_once(self, *, run_kind: str) -> dict[str, Any]:
        requested_at = datetime.now(UTC).isoformat()
        report_day = requested_at[:10]
        url = self._daily_review_url()
        context_payload = self._compact_context()
        lane_used = "fallback"
        status = "error"
        error_text = ""
        response_payload: dict[str, Any] = {}

        if not url:
            error_text = "openclaw_daily_review_url_empty"
        else:
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            if self.settings.openclaw_token:
                headers["Authorization"] = f"Bearer {self.settings.openclaw_token}"
            body = {
                "context": context_payload,
                "policy": {
                    "advisory_only": True,
                    "force_paid": bool(self.settings.openclaw_daily_review_force_paid),
                },
            }
            try:
                timeout = aiohttp.ClientTimeout(total=max(5, self.settings.openclaw_timeout_seconds * 3))
                async with (
                    aiohttp.ClientSession(timeout=timeout) as session,
                    session.post(url, headers=headers, json=body) as resp,
                ):
                    if resp.status != 200:
                        error_text = f"http_{resp.status}"
                    else:
                        response_payload = await resp.json()
                        lane_used = self._resolve_lane_used(response_payload)
                        status = "ok"
            except Exception as e:
                error_text = repr(e)

        completed_at = datetime.now(UTC).isoformat()
        report_id = self.db.insert_openclaw_daily_report(
            report_day=report_day,
            run_kind=run_kind,
            requested_at=requested_at,
            completed_at=completed_at,
            lane_used=lane_used,
            source_url=url,
            context_payload=context_payload,
            response_payload=response_payload,
            status=status,
            error_text=error_text,
        )

        suggestions = response_payload.get("suggestions", []) if isinstance(response_payload, dict) else []
        if isinstance(suggestions, list):
            for sug in suggestions[:80]:
                if isinstance(sug, dict):
                    self.db.upsert_openclaw_suggestion(sug, report_id=report_id)

        if status == "ok":
            logger.info(
                "OpenClaw daily review stored: report_id={} lane={} suggestions={}",
                report_id,
                lane_used,
                len(suggestions) if isinstance(suggestions, list) else 0,
            )
        else:
            logger.warning("OpenClaw daily review failed: {}", error_text)

        return {
            "ok": status == "ok",
            "report_id": report_id,
            "status": status,
            "lane_used": lane_used,
            "error": error_text,
        }
