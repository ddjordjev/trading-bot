from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import web.command_server as cs


class _Req:
    def __init__(self, payload: dict | None = None):
        self._payload = payload or {}

    async def json(self) -> dict:
        return self._payload


class _TaskStub:
    def add_done_callback(self, _cb):
        return None


@pytest.fixture
def bot_stub(tmp_path):
    stop_file = tmp_path / "STOP"
    bot = SimpleNamespace(
        _open_trades={},
        _process_signal=AsyncMock(),
        _prepare_symbol_for_forced_close=AsyncMock(),
        _claim_orphan_position=AsyncMock(return_value=(True, "claimed")),
        _create_manual_swing_plan_now=AsyncMock(return_value=(True, "created")),
        _write_deployment_status=AsyncMock(),
        _quick_hub_check=AsyncMock(),
        _hub_proposal=None,
        _running=False,
        start=AsyncMock(),
        stop=AsyncMock(),
        _close_all_positions=AsyncMock(),
        target=SimpleNamespace(STOP_FILE=stop_file),
        extreme_watcher=SimpleNamespace(drain_signals=MagicMock(), sync_watchlist=AsyncMock()),
        orders=SimpleNamespace(
            manual_take_profit=AsyncMock(),
            _close_sub_position_wick=AsyncMock(return_value=SimpleNamespace(status="filled")),
            trailing=SimpleNamespace(active_stops={}),
        ),
        exchange=SimpleNamespace(fetch_positions=AsyncMock(return_value=[])),
    )
    return bot


@pytest.fixture(autouse=True)
def reset_globals():
    cs._bot = None
    cs._background_tasks.clear()
    cs._start_time = 0.0
    yield
    cs._bot = None
    cs._background_tasks.clear()


@pytest.fixture
def stub_tasks(monkeypatch):
    import asyncio

    def _fake_create_task(coro):
        coro.close()
        return _TaskStub()

    monkeypatch.setattr(asyncio, "create_task", _fake_create_task)


def _json_body(resp) -> dict:
    import json

    return json.loads(resp.text)


@pytest.mark.asyncio
async def test_health_without_bot():
    body = _json_body(await cs.health(_Req()))
    assert body == {"status": "ok", "bot_running": False}


@pytest.mark.asyncio
async def test_health_with_bot(bot_stub):
    cs.set_bot(bot_stub)
    body = _json_body(await cs.health(_Req()))
    assert body == {"status": "ok", "bot_running": True}


@pytest.mark.asyncio
async def test_metrics_uses_collect_metrics(monkeypatch):
    monkeypatch.setattr("web.metrics.collect_metrics", lambda _bot, uptime: f"u={uptime:.1f}")
    cs._start_time = 1.0
    resp = await cs.metrics(_Req())
    assert "u=" in resp.body.decode()


@pytest.mark.asyncio
async def test_nudge_hub_quick_and_full(bot_stub):
    cs.set_bot(bot_stub)
    await cs._nudge_hub(full_snapshot=False)
    bot_stub._quick_hub_check.assert_awaited_once()
    await cs._nudge_hub(full_snapshot=True)
    bot_stub._write_deployment_status.assert_awaited_once()


@pytest.mark.asyncio
async def test_nudge_hub_suppresses_exception(bot_stub):
    bot_stub._quick_hub_check = AsyncMock(side_effect=RuntimeError("boom"))
    cs.set_bot(bot_stub)
    await cs._nudge_hub(full_snapshot=False)  # no raise


@pytest.mark.asyncio
async def test_close_position_requires_bot():
    body = _json_body(await cs.close_position(_Req({"symbol": "BTC/USDT"})))
    assert body["success"] is False


@pytest.mark.asyncio
async def test_close_position_requires_symbol(bot_stub):
    cs.set_bot(bot_stub)
    body = _json_body(await cs.close_position(_Req({})))
    assert body == {"success": False, "message": "Missing symbol"}


@pytest.mark.asyncio
async def test_close_position_success(bot_stub, stub_tasks):
    cs.set_bot(bot_stub)
    body = _json_body(await cs.close_position(_Req({"symbol": "BTC/USDT"})))
    assert body["success"] is True
    assert "Closed BTC/USDT" in body["message"]
    bot_stub._prepare_symbol_for_forced_close.assert_awaited_once_with("BTC/USDT")


@pytest.mark.asyncio
async def test_close_position_reports_not_completed(bot_stub, stub_tasks):
    symbol = "BTC/USDT"
    bot_stub._open_trades[symbol] = object()
    bot_stub._process_signal = AsyncMock()
    cs.set_bot(bot_stub)
    body = _json_body(await cs.close_position(_Req({"symbol": symbol})))
    assert body["success"] is False
    assert "did not complete" in body["message"]


@pytest.mark.asyncio
async def test_close_position_exception(bot_stub):
    bot_stub._process_signal = AsyncMock(side_effect=RuntimeError("bad close"))
    cs.set_bot(bot_stub)
    body = _json_body(await cs.close_position(_Req({"symbol": "BTC/USDT"})))
    assert body == {"success": False, "message": "bad close"}


@pytest.mark.asyncio
async def test_close_wick_scalp_requires_bot():
    body = _json_body(await cs.close_wick_scalp(_Req({"symbol": "BTC/USDT"})))
    assert body["success"] is False


@pytest.mark.asyncio
async def test_close_wick_scalp_requires_symbol(bot_stub):
    cs.set_bot(bot_stub)
    body = _json_body(await cs.close_wick_scalp(_Req({})))
    assert body == {"success": False, "message": "Missing symbol"}


@pytest.mark.asyncio
async def test_close_wick_scalp_success(bot_stub, stub_tasks):
    cs.set_bot(bot_stub)
    body = _json_body(await cs.close_wick_scalp(_Req({"symbol": "BTC/USDT"})))
    assert body["success"] is True
    assert "Closed wick scalp on BTC/USDT" in body["message"]
    bot_stub.orders._close_sub_position_wick.assert_awaited_once_with("BTC/USDT")


@pytest.mark.asyncio
async def test_close_wick_scalp_no_active_scalp(bot_stub):
    cs.set_bot(bot_stub)
    bot_stub.orders._close_sub_position_wick = AsyncMock(return_value=None)
    body = _json_body(await cs.close_wick_scalp(_Req({"symbol": "BTC/USDT"})))
    assert body == {"success": False, "message": "No active wick scalp for BTC/USDT"}


@pytest.mark.asyncio
async def test_close_wick_scalp_error(bot_stub):
    cs.set_bot(bot_stub)
    bot_stub.orders._close_sub_position_wick = AsyncMock(side_effect=RuntimeError("boom"))
    body = _json_body(await cs.close_wick_scalp(_Req({"symbol": "BTC/USDT"})))
    assert body["success"] is False
    assert "boom" in body["message"]


@pytest.mark.asyncio
async def test_claim_position_paths(bot_stub, stub_tasks):
    cs.set_bot(bot_stub)
    missing = _json_body(await cs.claim_position(_Req({})))
    assert missing["success"] is False
    ok = _json_body(await cs.claim_position(_Req({"symbol": "SOL/USDT", "strategy": "manual_claim"})))
    assert ok["success"] is True
    bot_stub._claim_orphan_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_claim_position_exception(bot_stub):
    bot_stub._claim_orphan_position = AsyncMock(side_effect=RuntimeError("claim fail"))
    cs.set_bot(bot_stub)
    body = _json_body(await cs.claim_position(_Req({"symbol": "SOL/USDT"})))
    assert body == {"success": False, "message": "claim fail"}


@pytest.mark.asyncio
async def test_create_manual_swing_plan_now_paths(bot_stub, stub_tasks):
    no_bot = _json_body(await cs.create_manual_swing_plan_now(_Req({"symbol": "SOL/USDT", "plan_id": "p1"})))
    assert no_bot["success"] is False

    cs.set_bot(bot_stub)
    missing = _json_body(await cs.create_manual_swing_plan_now(_Req({"symbol": "SOL/USDT"})))
    assert missing["success"] is False

    ok = _json_body(await cs.create_manual_swing_plan_now(_Req({"symbol": "SOL/USDT", "plan_id": "p1"})))
    assert ok["success"] is True
    bot_stub._create_manual_swing_plan_now.assert_awaited_once_with("SOL/USDT", "p1")


@pytest.mark.asyncio
async def test_take_profit_paths(bot_stub, stub_tasks):
    no_bot = _json_body(await cs.take_profit(_Req({"symbol": "BTC/USDT"})))
    assert no_bot["success"] is False

    cs.set_bot(bot_stub)
    missing = _json_body(await cs.take_profit(_Req({})))
    assert missing["success"] is False

    bot_stub.orders.manual_take_profit = AsyncMock(return_value=None)
    none_resp = _json_body(await cs.take_profit(_Req({"symbol": "BTC/USDT", "pct": "oops"})))
    assert none_resp["success"] is False
    assert "No tracked open position" in none_resp["message"]

    bot_stub.orders.manual_take_profit = AsyncMock(return_value=SimpleNamespace(filled=0.123456))
    ok = _json_body(await cs.take_profit(_Req({"symbol": "BTC/USDT", "pct": 33})))
    assert ok["success"] is True
    assert "Took 33.0% profit" in ok["message"]


@pytest.mark.asyncio
async def test_take_profit_exception(bot_stub):
    cs.set_bot(bot_stub)
    bot_stub.orders.manual_take_profit = AsyncMock(side_effect=RuntimeError("tp fail"))
    body = _json_body(await cs.take_profit(_Req({"symbol": "BTC/USDT"})))
    assert body == {"success": False, "message": "tp fail"}


@pytest.mark.asyncio
async def test_tighten_stop_paths(bot_stub):
    no_bot = _json_body(await cs.tighten_stop(_Req({"symbol": "BTC/USDT"})))
    assert no_bot["success"] is False

    cs.set_bot(bot_stub)
    missing = _json_body(await cs.tighten_stop(_Req({})))
    assert missing["success"] is False

    no_ts = _json_body(await cs.tighten_stop(_Req({"symbol": "BTC/USDT"})))
    assert no_ts["success"] is False
    assert "No trailing stop" in no_ts["message"]

    ts = SimpleNamespace(current_stop=0.0)
    bot_stub.orders.trailing.active_stops["BTC/USDT"] = ts
    bot_stub.exchange.fetch_positions = AsyncMock(return_value=[])
    no_pos = _json_body(await cs.tighten_stop(_Req({"symbol": "BTC/USDT", "pct": "x"})))
    assert no_pos["success"] is False
    assert "No position" in no_pos["message"]

    pos = SimpleNamespace(symbol="BTC/USDT", current_price=0.0, side=SimpleNamespace(value="buy"))
    bot_stub.exchange.fetch_positions = AsyncMock(return_value=[pos])
    no_price = _json_body(await cs.tighten_stop(_Req({"symbol": "BTC/USDT"})))
    assert no_price["success"] is False
    assert "No current price" in no_price["message"]

    pos.current_price = 100.0
    ok_long = _json_body(await cs.tighten_stop(_Req({"symbol": "BTC/USDT", "pct": 2})))
    assert ok_long["success"] is True
    assert ts.current_stop == pytest.approx(98.0)

    pos.side = SimpleNamespace(value="sell")
    ok_short = _json_body(await cs.tighten_stop(_Req({"symbol": "BTC/USDT", "pct": 2})))
    assert ok_short["success"] is True
    assert ts.current_stop == pytest.approx(102.0)


@pytest.mark.asyncio
async def test_close_all_stop_resume(bot_stub, stub_tasks):
    no_bot = _json_body(await cs.close_all(_Req()))
    assert no_bot["success"] is False

    cs.set_bot(bot_stub)
    ok = _json_body(await cs.close_all(_Req()))
    assert ok == {"success": True, "message": "All positions closed"}
    assert bot_stub.target.STOP_FILE.exists()
    bot_stub._close_all_positions.assert_awaited_once()

    stopped = _json_body(await cs.stop_trading(_Req()))
    assert stopped == {"success": True, "message": "Trading halted"}

    resumed = _json_body(await cs.resume_trading(_Req()))
    assert resumed == {"success": True, "message": "Trading resumed"}
    assert not bot_stub.target.STOP_FILE.exists()


@pytest.mark.asyncio
async def test_stop_resume_require_bot():
    assert _json_body(await cs.stop_trading(_Req()))["success"] is False
    assert _json_body(await cs.resume_trading(_Req()))["success"] is False


@pytest.mark.asyncio
async def test_bot_start_and_stop_paths(bot_stub, stub_tasks):
    assert _json_body(await cs.bot_start(_Req()))["success"] is False
    assert _json_body(await cs.bot_stop(_Req()))["success"] is False

    cs.set_bot(bot_stub)
    bot_stub._running = True
    already = _json_body(await cs.bot_start(_Req()))
    assert already == {"success": False, "message": "Already running"}

    bot_stub._running = False
    started = _json_body(await cs.bot_start(_Req()))
    assert started == {"success": True, "message": "Bot starting"}

    not_running = _json_body(await cs.bot_stop(_Req()))
    assert not_running == {"success": False, "message": "Not running"}

    bot_stub._running = True
    stopped = _json_body(await cs.bot_stop(_Req()))
    assert stopped == {"success": True, "message": "Bot stopped"}
    bot_stub.stop.assert_awaited_once()


def test_create_app_has_expected_routes():
    app = cs.create_app()
    route_paths = {r.resource.canonical for r in app.router.routes()}
    assert "/health" in route_paths
    assert "/metrics" in route_paths
    assert "/api/position/close" in route_paths
    assert "/api/wick-scalp/close" in route_paths
    assert "/api/position/claim" in route_paths
    assert "/api/position/take-profit" in route_paths
    assert "/api/position/tighten-stop" in route_paths
    assert "/api/close-all" in route_paths
    assert "/api/stop-trading" in route_paths
    assert "/api/resume-trading" in route_paths
    assert "/api/bot/start" in route_paths
    assert "/api/bot/stop" in route_paths


@pytest.mark.asyncio
async def test_start_command_server_wires_runner(monkeypatch, bot_stub):
    runner = MagicMock()
    runner.setup = AsyncMock()
    site = MagicMock()
    site.start = AsyncMock()
    monkeypatch.setattr(cs.web, "AppRunner", lambda _app, access_log=None: runner)
    monkeypatch.setattr(cs.web, "TCPSite", lambda _runner, _host, _port: site)

    result = await cs.start_command_server(bot_stub, host="127.0.0.1", port=9999)
    assert result is runner
    runner.setup.assert_awaited_once()
    site.start.assert_awaited_once()
    assert cs._bot is bot_stub
    assert cs._start_time > 0
