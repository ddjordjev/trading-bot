#!/usr/bin/env python3
"""Enforce the project coverage surge/coast policy.

Policy:
- Hard floor (`fail_under`) remains 80%.
- If coverage drops near the floor (`surge_trigger`), a surge is required.
- Surge is considered complete when coverage reaches `surge_target`.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class CoverageDecision:
    status: str
    should_fail: bool
    message: str


def _parse_total_coverage(output: str) -> float:
    """Parse total coverage percentage from `coverage report --format=total` output."""
    text = output.strip()
    if not text:
        raise ValueError("empty coverage output")
    return float(text)


def evaluate_coverage(
    *,
    coverage_pct: float,
    fail_under: float,
    surge_trigger: float,
    surge_target: float,
    enforce_surge: bool,
) -> CoverageDecision:
    if coverage_pct < fail_under:
        return CoverageDecision(
            status="below_floor",
            should_fail=True,
            message=(
                f"Coverage {coverage_pct:.2f}% is below fail_under {fail_under:.2f}%. Write tests before merging."
            ),
        )

    if coverage_pct < surge_trigger:
        return CoverageDecision(
            status="surge_required",
            should_fail=enforce_surge,
            message=(
                f"Coverage {coverage_pct:.2f}% is near floor (< {surge_trigger:.2f}%). "
                f"Start SURGE and raise to at least {surge_target:.2f}%."
            ),
        )

    if coverage_pct < surge_target:
        return CoverageDecision(
            status="coast_low_buffer",
            should_fail=False,
            message=(
                f"Coverage {coverage_pct:.2f}% is passing but below surge target "
                f"{surge_target:.2f}%. Coast mode is active with low buffer."
            ),
        )

    return CoverageDecision(
        status="healthy_buffer",
        should_fail=False,
        message=f"Coverage {coverage_pct:.2f}% is healthy (>= {surge_target:.2f}%).",
    )


def _run_coverage_total() -> float:
    proc = subprocess.run(
        [sys.executable, "-m", "coverage", "report", "--format=total"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or "unknown error"
        raise RuntimeError(f"coverage report failed: {stderr}")
    return _parse_total_coverage(proc.stdout)


def _extract_fail_under_from_pyproject(pyproject_text: str) -> float:
    match = re.search(r"(?m)^fail_under\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*$", pyproject_text)
    if not match:
        raise ValueError("Could not find fail_under in pyproject.toml")
    return float(match.group(1))


def main() -> int:
    parser = argparse.ArgumentParser(description="Coverage surge/coast policy guard")
    parser.add_argument("--pyproject", default="pyproject.toml", help="Path to pyproject.toml")
    parser.add_argument("--surge-trigger", type=float, default=82.0, help="Below this, surge is required")
    parser.add_argument("--surge-target", type=float, default=88.0, help="Surge completion target")
    parser.add_argument(
        "--enforce-surge",
        action="store_true",
        help="Fail CI when coverage is between fail_under and surge_trigger",
    )
    args = parser.parse_args()

    with open(args.pyproject, encoding="utf-8") as f:
        pyproject = f.read()
    fail_under = _extract_fail_under_from_pyproject(pyproject)
    coverage_pct = _run_coverage_total()

    decision = evaluate_coverage(
        coverage_pct=coverage_pct,
        fail_under=fail_under,
        surge_trigger=args.surge_trigger,
        surge_target=args.surge_target,
        enforce_surge=args.enforce_surge,
    )
    print(f"[coverage-guard] status={decision.status} | {decision.message}")
    return 1 if decision.should_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
