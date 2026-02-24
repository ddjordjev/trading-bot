#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _build_request(url: str, token: str, timeout: float) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        body = resp.read().decode("utf-8")
    return {"status": int(status), "body": body}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate an OpenClaw intel endpoint payload.")
    parser.add_argument(
        "--url",
        default="http://localhost:18080/intel",
        help="OpenClaw advisory intel endpoint URL",
    )
    parser.add_argument(
        "--token",
        default="",
        help="Optional bearer token for OpenClaw endpoint auth",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=8.0,
        help="Request timeout in seconds (default: 8)",
    )
    parser.add_argument(
        "--print-payload",
        action="store_true",
        help="Print normalized validated payload",
    )
    args = parser.parse_args()

    try:
        result = _build_request(url=args.url, token=args.token, timeout=max(1.0, args.timeout))
    except urllib.error.HTTPError as exc:
        print(f"[ERROR] HTTP {exc.code} from {args.url}")
        return 1
    except urllib.error.URLError as exc:
        print(f"[ERROR] Connection failed: {exc}")
        return 1
    except TimeoutError:
        print(f"[ERROR] Connection timed out for {args.url}")
        return 1
    except Exception as exc:  # pragma: no cover - defensive fallback
        print(f"[ERROR] Unexpected fetch failure: {exc}")
        return 1

    status = result["status"]
    if status != 200:
        print(f"[ERROR] Endpoint returned HTTP {status}")
        return 1

    body = result["body"]
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        print(f"[ERROR] Response is not valid JSON: {exc}")
        return 1

    try:
        from intel.openclaw import OpenClawSnapshot

        parsed = OpenClawSnapshot.model_validate(payload)
    except Exception as exc:
        print(f"[ERROR] Payload validation failed: {exc}")
        return 1

    print("[OK] OpenClaw payload is valid.")
    print(f"  regime={parsed.regime_commentary.regime}")
    print(f"  confidence={parsed.regime_commentary.confidence:.2f}")
    print(f"  ideas={len(parsed.idea_briefs)}")
    print(f"  triage={len(parsed.failure_triage)}")
    print(f"  experiments={len(parsed.experiments)}")

    if args.print_payload:
        print(json.dumps(parsed.model_dump(), indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
