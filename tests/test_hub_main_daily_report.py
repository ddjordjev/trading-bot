from __future__ import annotations

from hub_main import _build_bot_similarity_lines, _strategy_set_jaccard, _summarize_reported_strategies


def test_summarize_reported_strategies_compacts_duplicates() -> None:
    strategies = [
        {"name": "manual_override"},
        {"name": "manual_override"},
        {"name": "trending_momentum"},
        {"name": "trending_momentum"},
        {"name": "trending_momentum"},
        {"name": "risk_manager"},
    ]
    out = _summarize_reported_strategies(strategies, max_items=5)
    assert "trending_momentum (3)" in out
    assert "manual_override (2)" in out
    assert "risk_manager (1)" in out


def test_strategy_set_jaccard_handles_empty_sets() -> None:
    assert _strategy_set_jaccard(set(), set()) == 1.0
    assert _strategy_set_jaccard({"a"}, set()) == 0.0
    assert _strategy_set_jaccard({"a", "b"}, {"a", "c"}) == 1 / 3


def test_build_bot_similarity_lines_detects_near_duplicates() -> None:
    bot_ids = ["momentum", "extreme", "swing"]
    config_sets = {
        "momentum": {"compound_momentum", "market_open_volatility"},
        "extreme": {"compound_momentum", "market_open_volatility"},
        "swing": {"swing_opportunity", "grid"},
    }
    realized_sets = {
        "momentum": {"trending_momentum"},
        "extreme": {"trending_momentum"},
        "swing": {"swing_opportunity"},
    }
    lines = _build_bot_similarity_lines(bot_ids, config_sets, realized_sets)
    assert any("momentum ~ extreme" in line for line in lines)
    assert not any("momentum ~ swing" in line for line in lines)
