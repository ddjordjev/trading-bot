#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Colors:
    reset: str
    dim: str
    bold: str
    red: str
    yellow: str
    green: str
    cyan: str
    magenta: str
    white: str


def _supports_color(no_color: bool) -> bool:
    return not no_color and sys.stdout.isatty()


def _colors(enabled: bool) -> Colors:
    if not enabled:
        return Colors("", "", "", "", "", "", "", "", "")
    return Colors(
        reset="\033[0m",
        dim="\033[2m",
        bold="\033[1m",
        red="\033[31m",
        yellow="\033[33m",
        green="\033[32m",
        cyan="\033[36m",
        magenta="\033[35m",
        white="\033[37m",
    )


def _try_json(value: str) -> dict[str, Any] | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            loaded = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return loaded if isinstance(loaded, dict) else None


def _coerce_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return _try_json(value)
    return None


def _parse_line(line: str) -> tuple[dict[str, Any] | None, str]:
    raw = line.rstrip("\n")
    payload = _try_json(raw)
    if payload is None:
        return None, raw
    return payload, raw


def _level_name(payload: dict[str, Any], event_payload: dict[str, Any] | None) -> str:
    level = payload.get("level")
    if isinstance(level, int):
        if level >= 50:
            return "ERROR"
        if level >= 40:
            return "WARN"
        if level >= 30:
            return "INFO"
        if level >= 20:
            return "DEBUG"
        return "TRACE"
    if isinstance(level, str) and level.strip():
        return level.strip().upper()
    if event_payload:
        severity = str(event_payload.get("severity", "")).strip().upper()
        if severity:
            return severity
    return "INFO"


def _to_short_time(value: Any) -> str:
    if value is None:
        return "--:--:--"
    try:
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1_000_000_000_000:
                ts /= 1000.0
            return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        text = str(value).strip()
        if not text:
            return "--:--:--"
        if text.isdigit():
            return _to_short_time(int(text))
        if "T" in text:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt.strftime("%H:%M:%S")
    except Exception:
        return "--:--:--"
    return "--:--:--"


def _event_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("msg", "message", "raw", "data"):
        nested = _coerce_dict(payload.get(key))
        if nested:
            return nested
    return None


def _event_name(payload: dict[str, Any], event_payload: dict[str, Any] | None) -> str:
    if event_payload:
        event = str(event_payload.get("event", "")).strip()
        if event:
            return event
    event = str(payload.get("event", "")).strip()
    if event:
        return event
    kind = str(payload.get("type", "")).strip()
    return kind or "log"


def _pick(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _details(payload: dict[str, Any], event_payload: dict[str, Any] | None) -> str:
    src = event_payload or payload
    event = _event_name(payload, event_payload)
    details: list[str] = []
    if event == "channelSummary":
        for key, label in (
            ("sessionEvents", "sess"),
            ("totalEvents", "total"),
            ("contextTokens", "ctx"),
            ("streamingTokens", "stream"),
            ("totalTokens", "tok"),
            ("count", "count"),
            ("everyMs", "every_ms"),
        ):
            if key in src:
                details.append(f"{label}={_pick(src.get(key))}")
    elif event == "heartbeat":
        for key, label in (
            ("agentId", "agent"),
            ("model", "model"),
            ("status", "status"),
            ("contextTokens", "ctx"),
            ("streamingTokens", "stream"),
            ("totalTokens", "tok"),
            ("everyMs", "every_ms"),
        ):
            if key in src:
                details.append(f"{label}={_pick(src.get(key))}")
    else:
        for key, label in (
            ("agentId", "agent"),
            ("sessionId", "session"),
            ("reason", "reason"),
            ("gateway", "gateway"),
            ("path", "path"),
            ("status", "status"),
            ("error", "error"),
            ("contextTokens", "ctx"),
            ("totalTokens", "tok"),
            ("count", "count"),
        ):
            if key in src:
                details.append(f"{label}={_pick(src.get(key))}")

    if not details:
        base_msg = payload.get("msg") or payload.get("message") or ""
        if isinstance(base_msg, str):
            compact = " ".join(base_msg.split())
            if compact:
                details.append(compact[:120])

    return " ".join(details)


def _event_color(level_name: str, event: str, c: Colors) -> str:
    upper = level_name.upper()
    if upper in {"ERROR", "FATAL"}:
        return c.red
    if upper == "WARN":
        return c.yellow
    if event in {"heartbeat"}:
        return c.green
    if event in {"channelSummary"}:
        return c.cyan
    return c.white


def _print_header(c: Colors) -> None:
    print(
        f"{c.bold}{'TIME':<8} {'LEVEL':<6} {'EVENT':<16} DETAILS{c.reset}",
        flush=True,
    )


def _format_line(payload: dict[str, Any], raw_line: str, c: Colors) -> tuple[str, str]:
    event_payload = _event_payload(payload)
    when = _to_short_time(payload.get("time") or payload.get("timestamp"))
    level_name = _level_name(payload, event_payload)
    event = _event_name(payload, event_payload)
    detail = _details(payload, event_payload)
    color = _event_color(level_name, event, c)
    line = f"{c.dim}{when:<8}{c.reset} {color}{level_name:<6} {event:<16}{c.reset} {detail}"
    return event, line


def _format_raw(raw_line: str, c: Colors) -> str:
    compact = " ".join(raw_line.split())
    trimmed = compact[:220]
    return f"{c.dim}{trimmed}{c.reset}"


def _print_summary(counter: Counter[str], lines_seen: int, c: Colors) -> None:
    top = ", ".join(f"{event}:{count}" for event, count in counter.most_common(4))
    print(f"{c.magenta}--- summary lines={lines_seen} events=[{top}] ---{c.reset}", flush=True)


def _stream_lines(path: Path | None, follow: bool) -> Any:
    if path is None:
        for line in sys.stdin:
            yield line
        return

    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        while True:
            line = handle.readline()
            if line:
                yield line
                continue
            if not follow:
                break
            time.sleep(0.25)


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert noisy OpenClaw JSON logs into a color-coded overview.")
    parser.add_argument(
        "path",
        nargs="?",
        help="Optional log file path. If omitted, reads from stdin.",
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help="Follow file input (tail -f style). Ignored for stdin.",
    )
    parser.add_argument(
        "--header-every",
        type=int,
        default=30,
        help="Repeat table header every N parsed lines (default: 30).",
    )
    parser.add_argument(
        "--summary-every",
        type=int,
        default=0,
        help="Print short event-count summary every N lines (0 disables).",
    )
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors.")
    args = parser.parse_args()

    c = _colors(_supports_color(args.no_color))
    line_count = 0
    event_counts: Counter[str] = Counter()
    path = Path(args.path) if args.path else None

    _print_header(c)
    try:
        for line in _stream_lines(path, follow=args.follow):
            payload, raw_line = _parse_line(line)
            if payload is None:
                print(_format_raw(raw_line, c), flush=True)
                continue

            event, rendered = _format_line(payload, raw_line, c)
            print(rendered, flush=True)
            line_count += 1
            event_counts[event] += 1

            if args.header_every > 0 and line_count % args.header_every == 0:
                _print_header(c)

            if args.summary_every > 0 and line_count % args.summary_every == 0:
                _print_summary(event_counts, line_count, c)
    except BrokenPipeError:
        return 0
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
