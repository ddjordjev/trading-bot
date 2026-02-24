#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "docs" / "reports"
API_BASE = "http://localhost:9035"
SERVICES = ["bot-hub", "bot-momentum", "bot-aggressive", "bot-indicators", "bot-meanrev"]


def _run(cmd: list[str]) -> str:
    env = os.environ.copy()
    env["PATH"] = f"/Applications/Docker.app/Contents/Resources/bin:{env.get('PATH', '')}"
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout + (f"\n{proc.stderr}" if proc.stderr else "")


def _get_json(path: str) -> dict:
    req = Request(f"{API_BASE}{path}", headers={"Accept": "application/json"})
    with urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _scan_logs(since: str) -> dict:
    findings: dict[str, dict[str, int]] = {}
    for svc in SERVICES:
        out = _run(["docker", "compose", "logs", "--since", since, svc])
        lines = out.splitlines()
        findings[svc] = {
            "errors": sum(1 for l in lines if " ERROR " in l),
            "critical": sum(1 for l in lines if " CRITICAL " in l),
            "tracebacks": sum(1 for l in lines if "Traceback" in l),
            "warnings": sum(1 for l in lines if " WARNING " in l),
        }
    return findings


def snapshot(since: str) -> dict:
    now = datetime.now(UTC).isoformat()
    health = _get_json("/health")
    status = _get_json("/api/status")
    intel = _get_json("/api/intel")
    log_scan = _scan_logs(since)
    return {
        "ts": now,
        "health": health,
        "status": {
            "running": status.get("running"),
            "balance": status.get("balance"),
            "daily_pnl": status.get("daily_pnl"),
            "open_positions": status.get("open_positions"),
            "manual_stop_active": status.get("manual_stop_active"),
        },
        "intel": {
            "regime": intel.get("regime"),
            "liquidation_24h": intel.get("liquidation_24h"),
            "liquidation_bias": intel.get("liquidation_bias"),
            "sources_active": intel.get("sources_active", []),
        },
        "log_scan": log_scan,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Night monitor snapshot runner")
    parser.add_argument("--interval-sec", type=int, default=1800)
    parser.add_argument("--cycles", type=int, default=24)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = REPORT_DIR / f"night_monitor_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.jsonl"

    cycles = 1 if args.once else max(1, args.cycles)
    for i in range(cycles):
        data = snapshot(since=f"{max(args.interval_sec // 60, 1)}m")
        report.write_text("", encoding="utf-8") if not report.exists() else None
        with report.open("a", encoding="utf-8") as f:
            f.write(json.dumps(data, separators=(",", ":")) + "\n")
        print(
            f"[{i + 1}/{cycles}] {data['ts']} running={data['status']['running']} "
            f"pnl={data['status']['daily_pnl']} liq24h={data['intel']['liquidation_24h']}"
        )
        if i < cycles - 1:
            time.sleep(max(10, args.interval_sec))

    print(f"Report written to {report}")


if __name__ == "__main__":
    main()
