from __future__ import annotations

from scripts.coverage_guard import evaluate_coverage


def test_below_floor_fails() -> None:
    decision = evaluate_coverage(
        coverage_pct=79.9,
        fail_under=80.0,
        surge_trigger=82.0,
        surge_target=88.0,
        enforce_surge=False,
    )
    assert decision.should_fail is True
    assert decision.status == "below_floor"


def test_near_floor_warns_by_default() -> None:
    decision = evaluate_coverage(
        coverage_pct=81.2,
        fail_under=80.0,
        surge_trigger=82.0,
        surge_target=88.0,
        enforce_surge=False,
    )
    assert decision.should_fail is False
    assert decision.status == "surge_required"


def test_coast_low_buffer_passes() -> None:
    decision = evaluate_coverage(
        coverage_pct=85.3,
        fail_under=80.0,
        surge_trigger=82.0,
        surge_target=88.0,
        enforce_surge=False,
    )
    assert decision.should_fail is False
    assert decision.status == "coast_low_buffer"


def test_healthy_buffer_passes() -> None:
    decision = evaluate_coverage(
        coverage_pct=90.1,
        fail_under=80.0,
        surge_trigger=82.0,
        surge_target=88.0,
        enforce_surge=False,
    )
    assert decision.should_fail is False
    assert decision.status == "healthy_buffer"


def test_near_floor_fails_when_surge_is_enforced() -> None:
    decision = evaluate_coverage(
        coverage_pct=81.2,
        fail_under=80.0,
        surge_trigger=82.0,
        surge_target=88.0,
        enforce_surge=True,
    )
    assert decision.should_fail is True
    assert decision.status == "surge_required"
