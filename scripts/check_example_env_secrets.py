#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

_ENV_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|_)(?:API_KEY|API_SECRET|SECRET|TOKEN|PASSWORD|PASS|PRIVATE_KEY|ACCESS_KEY|CLIENT_SECRET)$",
    re.IGNORECASE,
)
_PLACEHOLDER_PREFIXES = (
    "your_",
    "example",
    "replace",
    "change_me",
    "changeme",
    "dummy",
    "sample",
    "fake",
    "test_",
    "test-",
    "redacted",
    "placeholder",
)
_PLACEHOLDER_EXACT = {
    "todo",
    "tbd",
    "none",
    "null",
    "unset",
    "xxx",
}


def _discover_example_env_files(root: Path) -> list[Path]:
    patterns = (
        ".env.example",
        ".env.*.example",
        "*.env.example",
        "*.example.env",
    )
    files: set[Path] = set()
    for pattern in patterns:
        for match in root.rglob(pattern):
            if match.is_file():
                files.add(match)
    return sorted(files)


def _clean_env_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value.startswith("#"):
        return ""
    if len(value) >= 2 and (
        (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'"))
    ):
        value = value[1:-1]
    elif "#" in value:
        value = value.split("#", 1)[0].rstrip()
    return value.strip()


def _is_placeholder(value: str) -> bool:
    if not value:
        return True
    lowered = value.strip().lower()
    if lowered in _PLACEHOLDER_EXACT:
        return True
    if lowered.startswith(_PLACEHOLDER_PREFIXES):
        return True
    if lowered.endswith("_here"):
        return True
    if lowered.startswith("<") and lowered.endswith(">"):
        return True
    return bool("${" in lowered and "}" in lowered)


def find_secret_issues(files: list[Path]) -> list[str]:
    issues: list[str] = []
    for file in files:
        lines = file.read_text(encoding="utf-8").splitlines()
        for line_no, line in enumerate(lines, start=1):
            if not line or line.lstrip().startswith("#"):
                continue
            match = _ENV_ASSIGN_RE.match(line)
            if not match:
                continue
            key, raw_value = match.group(1), match.group(2)
            if not _SENSITIVE_KEY_RE.search(key):
                continue
            value = _clean_env_value(raw_value)
            if _is_placeholder(value):
                continue
            issues.append(f"{file}:{line_no}: {key} appears to contain a non-placeholder secret value.")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail if example env templates contain non-placeholder secrets.")
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root path to scan.",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    files = _discover_example_env_files(root)
    if not files:
        print("[OK] No example env templates found.")
        return 0

    issues = find_secret_issues(files)
    if issues:
        print("[ERROR] Potential secrets found in example env templates:")
        for issue in issues:
            print(f"  - {issue}")
        return 1

    print("[OK] Example env templates contain placeholders only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
