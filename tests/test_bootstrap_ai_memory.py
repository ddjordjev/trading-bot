from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap_ai_memory.sh"


def _run(query: str, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [str(SCRIPT), query],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_bootstrap_fails_without_configured_command() -> None:
    result = _run(
        "startup context",
        {
            "AI_MEMORY_QUERY_CMD": "",
            "AI_MEMORY_DISABLE_FALLBACK": "1",
        },
    )
    assert result.returncode == 2
    assert "AI-MEMORY NOT AVAILABLE AND I'M NOT ABLE TO USE IT ATM" in result.stdout


def test_bootstrap_supports_placeholder_query_expansion() -> None:
    result = _run("btc drawdown", {"AI_MEMORY_QUERY_CMD": "echo memory:%QUERY%"})
    assert result.returncode == 0
    assert "memory:btc drawdown" in result.stdout


def test_bootstrap_supports_query_appended_as_last_argument() -> None:
    result = _run("intel summary", {"AI_MEMORY_QUERY_CMD": "echo memory"})
    assert result.returncode == 0
    assert "memory intel summary" in result.stdout
