from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import web.command_server as cs


class _Req:
    def __init__(self, payload: dict[str, object] | None = None):
        self._payload = payload or {}

    async def json(self) -> dict[str, object]:
        return self._payload


class _DoneTask:
    def add_done_callback(self, cb):
        cb(self)


def _resp_json(resp) -> dict[str, object]:
    return json.loads(resp.text)


@pytest.fixture(autouse=True)
def _reset_globals():
    cs._bot = None
    cs._background_tasks.clear()
    cs._start_time = 0.0
    yield
    cs._bot = None
    cs._background_tasks.clear()


@pytest.fixture
def bot_stub():
    bot = MagicMock()
    bot._open_trades = {}
    bot._running = False
    bot._hub_proposal = object()
    bot._process_signal = AsyncMock()
    bot._prepare_symbol_for_forced_close = AsyncMock()
    bot._claim_orphan_position = AsyncMock(return_value=(True, "claimed"))
    bot._close_all_positions = AsyncMock()
    bot._write_deployment_status = AsyncMock()
    bot._quick_hub_check = AsyncMock()
    bot.start = AsyncMock()
    bot.stop = AsyncMock()

    stop_file = MagicMock()
    stop_file.touch = MagicMock()
    stop_file.unlink = MagicMock()
    bot.target = SimpleNamespace(STOP_FILE=stop_file)

    trailing = SimpleNamespace(active_stops={})
    bot.orders = SimpleNamespace(manual_take_profit=AsyncMock(return_value=None), trailing=trailing)
    bot.exchange = SimpleNamespace(fetch_positions=AsyncMock(return_value=[]))
    bot.extreme_watcher = SimpleNamespace(drain_signals=MagicMock(), sync_watchlist=AsyncMock())
    return bot


@pytest.mark.asyncio
async def test_health_and_metrics_paths(bot_stub):
    data = _resp_json(await cs.health(_Req()))
    assert data == {"status": "ok", "bot_running": False}
    cs.set_bot(bot_stub)
    data2 = _resp_json(await cs.health(_Req()))
    assert data2["bot_running"] is True

    with patch("web.metrics.collect_metrics", return_value="m 1\n"):
        resp = await cs.metrics(_Req())
    assert "text/plain" in resp.content_type
    assert resp.text == "m 1\n"


@pytest.mark.asyncio
async def test_close_position_missing_bot_or_symbol(bot_stub):
    r1 = _resp_json(await cs.close_position(_Req({"symbol": "BTC/USDT"})))
    assert r1["success"] is False
    assert "not initialized" in r1["message"].lower()

    cs.set_bot(bot_stub)
    r2 = _resp_json(await cs.close_position(_Req({})))
    assert r2["success"] is False
    assert r2["message"] == "Missing symbol"


@pytest.mark.asyncio
async def test_close_position_success_and_incomplete(bot_stub):
    cs.set_bot(bot_stub)
    with patch("asyncio.create_task", return_value=_DoneTask()):
        ok = _resp_json(await cs.close_position(_Req({"symbol": "BTC/USDT"})))
    assert ok["success"] is True
    assert "Closed BTC/USDT" in ok["message"]
    bot_stub._prepare_symbol_for_forced_close.assert_awaited_once_with("BTC/USDT")

    bot_stub._open_trades = {"ETH/USDT": object()}
    with patch("asyncio.create_task", return_value=_DoneTask()):
        res = _resp_json(await cs.close_position(_Req({"symbol": "ETH/USDT"})))
    assert res["success"] is False
    assert "did not complete" in res["message"]


@pytest.mark.asyncio
async def test_close_position_exception(bot_stub):
    cs.set_bot(bot_stub)
    bot_stub._process_signal = AsyncMock(side_effect=RuntimeError("boom"))
    out = _resp_json(await cs.close_position(_Req({"symbol": "BTC/USDT"})))
    assert out["success"] is False
    assert "boom" in out["message"]


@pytest.mark.asyncio
async def test_claim_position_paths(bot_stub):
    cs.set_bot(bot_stub)
    miss = _resp_json(await cs.claim_position(_Req({})))
    assert miss["success"] is False
    assert miss["message"] == "Missing symbol"

    with patch("asyncio.create_task", return_value=_DoneTask()):
        ok = _resp_json(await cs.claim_position(_Req({"symbol": "SOL/USDT", "strategy": "manual_claim"})))
    assert ok["success"] is True
    assert "claimed" in ok["message"]

    bot_stub._claim_orphan_position = AsyncMock(side_effect=RuntimeError("claim failed"))
    err = _resp_json(await cs.claim_position(_Req({"symbol": "SOL/USDT"})))
    assert err["success"] is False
    assert "claim failed" in err["message"]


@pytest.mark.asyncio
async def test_take_profit_paths(bot_stub):
    cs.set_bot(bot_stub)
    miss = _resp_json(await cs.take_profit(_Req({})))
    assert miss["success"] is False
    assert miss["message"] == "Missing symbol"

    none = _resp_json(await cs.take_profit(_Req({"symbol": "BTC/USDT", "pct": "bad"})))
    assert none["success"] is False
    assert "No tracked open position" in none["message"]

    fake_order = SimpleNamespace(filled=0.1234)
    bot_stub.orders.manual_take_profit = AsyncMock(return_value=fake_order)
    with patch("asyncio.create_task", return_value=_DoneTask()):
        ok = _resp_json(await cs.take_profit(_Req({"symbol": "BTC/USDT", "pct": 30})))
    assert ok["success"] is True
    assert "Took 30.0% profit" in ok["message"]

    bot_stub.orders.manual_take_profit = AsyncMock(side_effect=RuntimeError("tp boom"))
    err = _resp_json(await cs.take_profit(_Req({"symbol": "BTC/USDT"})))
    assert err["success"] is False
    assert "tp boom" in err["message"]


@pytest.mark.asyncio
async def test_tighten_stop_paths(bot_stub):
    cs.set_bot(bot_stub)
    miss = _resp_json(await cs.tighten_stop(_Req({})))
    assert miss["success"] is False
    assert miss["message"] == "Missing symbol"

    no_ts = _resp_json(await cs.tighten_stop(_Req({"symbol": "BTC/USDT"})))
    assert no_ts["success"] is False
    assert "No trailing stop" in no_ts["message"]

    ts = SimpleNamespace(current_stop=0.0)
    bot_stub.orders.trailing.active_stops = {"BTC/USDT": ts}
    bot_stub.exchange.fetch_positions = AsyncMock(return_value=[])
    no_pos = _resp_json(await cs.tighten_stop(_Req({"symbol": "BTC/USDT"})))
    assert no_pos["success"] is False
    assert "No position" in no_pos["message"]

    pos = SimpleNamespace(symbol="BTC/USDT", current_price=0, side=SimpleNamespace(value="buy"))
    bot_stub.exchange.fetch_positions = AsyncMock(return_value=[pos])
    no_price = _resp_json(await cs.tighten_stop(_Req({"symbol": "BTC/USDT"})))
    assert no_price["success"] is False
    assert "No current price" in no_price["message"]

    pos.current_price = 100.0
    ok = _resp_json(await cs.tighten_stop(_Req({"symbol": "BTC/USDT", "pct": 5})))
    assert ok["success"] is True
    assert "Stop tightened" in ok["message"]
    assert ts.current_stop == pytest.approx(95.0)

    pos.side = SimpleNamespace(value="sell")
    ok2 = _resp_json(await cs.tighten_stop(_Req({"symbol": "BTC/USDT", "pct": 5})))
    assert ok2["success"] is True
    assert ts.current_stop == pytest.approx(105.0)


@pytest.mark.asyncio
async def test_close_all_stop_resume_paths(bot_stub):
    cs.set_bot(bot_stub)
    with patch("asyncio.create_task", return_value=_DoneTask()):
        closed = _resp_json(await cs.close_all(_Req()))
    assert closed["success"] is True
    bot_stub.target.STOP_FILE.touch.assert_called()
    bot_stub.extreme_watcher.drain_signals.assert_called()
    bot_stub.extreme_watcher.sync_watchlist.assert_awaited_once()
    bot_stub._close_all_positions.assert_awaited_once()

    with patch("asyncio.create_task", return_value=_DoneTask()):
        halted = _resp_json(await cs.stop_trading(_Req()))
    assert halted["success"] is True
    assert halted["message"] == "Trading halted"

    with patch("asyncio.create_task", return_value=_DoneTask()):
        resumed = _resp_json(await cs.resume_trading(_Req()))
    assert resumed["success"] is True
    assert resumed["message"] == "Trading resumed"
    bot_stub.target.STOP_FILE.unlink.assert_called()


@pytest.mark.asyncio
async def test_bot_start_stop_and_no_bot(bot_stub):
    no_bot = _resp_json(await cs.bot_start(_Req()))
    assert no_bot["success"] is False

    cs.set_bot(bot_stub)
    with patch("asyncio.create_task", return_value=_DoneTask()):
        started = _resp_json(await cs.bot_start(_Req()))
    assert started["success"] is True

    bot_stub._running = True
    already = _resp_json(await cs.bot_start(_Req()))
    assert already["success"] is False
    assert already["message"] == "Already running"

    bot_stub._running = False
    not_running = _resp_json(await cs.bot_stop(_Req()))
    assert not_running["success"] is False
    assert not_running["message"] == "Not running"

    bot_stub._running = True
    stopped = _resp_json(await cs.bot_stop(_Req()))
    assert stopped["success"] is True
    bot_stub.stop.assert_awaited_once()


def test_create_app_routes():
    app = cs.create_app()
    resources = {r.canonical for r in app.router.resources()}
    assert "/health" in resources
    assert "/metrics" in resources
    assert "/api/position/claim" in resources
    assert "/api/close-all" in resources
    assert "/api/bot/start" in resources


@pytest.mark.asyncio
async def test_start_command_server_constructs_runner_and_site(bot_stub):
    runner = AsyncMock()
    site = AsyncMock()
    site.start = AsyncMock()

    with (
        patch("web.command_server.web.AppRunner", return_value=runner) as app_runner_cls,
        patch("web.command_server.web.TCPSite", return_value=site) as site_cls,
    ):
        out = await cs.start_command_server(bot_stub, host="127.0.0.1", port=9900)

    assert out is runner
    app_runner_cls.assert_called_once()
    site_cls.assert_called_once_with(runner, "127.0.0.1", 9900)
    runner.setup.assert_awaited_once()
    site.start.assert_awaited_once()
