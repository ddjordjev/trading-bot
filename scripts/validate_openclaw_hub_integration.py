#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request


def _get_json(url: str, token: str, timeout: float) -> tuple[int, object]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = int(getattr(resp, "status", 200))
        payload = json.loads(resp.read().decode("utf-8"))
    return status, payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate OpenClaw visibility in hub API endpoints.")
    parser.add_argument("--hub-url", default="http://localhost:9035", help="Hub base URL")
    parser.add_argument(
        "--dashboard-token",
        default="",
        help="Optional dashboard auth token for /api/* endpoints",
    )
    parser.add_argument("--timeout", type=float, default=8.0, help="Request timeout in seconds")
    args = parser.parse_args()

    timeout = max(1.0, float(args.timeout))
    modules_url = f"{args.hub_url.rstrip('/')}/api/modules"
    intel_url = f"{args.hub_url.rstrip('/')}/api/intel"

    try:
        mod_status, modules = _get_json(modules_url, args.dashboard_token, timeout)
        intel_status, intel = _get_json(intel_url, args.dashboard_token, timeout)
    except urllib.error.HTTPError as exc:
        print(f"[ERROR] HTTP {exc.code}: {exc.reason}")
        return 1
    except urllib.error.URLError as exc:
        print(f"[ERROR] Connection error: {exc}")
        return 1
    except Exception as exc:  # pragma: no cover - defensive fallback
        print(f"[ERROR] Unexpected error: {exc}")
        return 1

    if mod_status != 200 or intel_status != 200:
        print(f"[ERROR] Non-200 response(s): modules={mod_status} intel={intel_status}")
        return 1

    if not isinstance(modules, list):
        print("[ERROR] /api/modules did not return a list")
        return 1

    openclaw_module = next((m for m in modules if isinstance(m, dict) and m.get("name") == "openclaw"), None)
    if openclaw_module is None:
        print("[ERROR] OpenClaw module not found in /api/modules")
        return 1

    if intel is None or not isinstance(intel, dict):
        print("[WARN] /api/intel returned no active snapshot yet (null or invalid)")
        print("[OK] Module visibility validated, intel payload not yet available.")
        return 0

    required_keys = {
        "openclaw_regime",
        "openclaw_regime_confidence",
        "openclaw_sentiment_score",
        "openclaw_idea_briefs",
        "openclaw_failure_triage",
    }
    missing = sorted(k for k in required_keys if k not in intel)
    if missing:
        print(f"[ERROR] /api/intel missing OpenClaw keys: {', '.join(missing)}")
        return 1

    print("[OK] OpenClaw module and intel surface are visible in hub API.")
    print(f"  module_enabled={bool(openclaw_module.get('enabled'))}")
    stats = openclaw_module.get("stats", {})
    if isinstance(stats, dict):
        print(f"  module_connected={bool(stats.get('connected', False))}")
        print(f"  regime={stats.get('regime', 'unknown')}")
        print(f"  confidence={stats.get('confidence', 0.0)}")
    print(f"  intel_regime={intel.get('openclaw_regime', 'unknown')}")
    print(f"  intel_ideas={len(intel.get('openclaw_idea_briefs', []) or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
