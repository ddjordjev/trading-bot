from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_SRC = ROOT / "scripts" / "materialize_runtime_secrets.sh"


def _setup_temp_repo(tmp_path: Path, *, exchange: str, source_env: str, mode: str) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "env").mkdir(parents=True)
    shutil.copy2(SCRIPT_SRC, repo / "scripts" / "materialize_runtime_secrets.sh")
    script = repo / "scripts" / "materialize_runtime_secrets.sh"
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    (repo / ".env").write_text(source_env, encoding="utf-8")
    runtime_file = "local.runtime.env" if mode == "local" else "prod.runtime.env"
    (repo / "env" / runtime_file).write_text(f"EXCHANGE={exchange}\n", encoding="utf-8")
    return repo


def _run(repo: Path, mode: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    return subprocess.run(
        [str(repo / "scripts" / "materialize_runtime_secrets.sh"), mode],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_materialize_local_uses_test_keys_for_selected_exchange(tmp_path: Path) -> None:
    repo = _setup_temp_repo(
        tmp_path,
        exchange="binance_testnet",
        mode="local",
        source_env=(
            "BINANCE_TEST_API_KEY=bn_test_key_1234\n"
            "BINANCE_TEST_API_SECRET=bn_test_secret_5678\n"
            "BYBIT_TEST_API_KEY=bb_test_key_9012\n"
            "BYBIT_TEST_API_SECRET=bb_test_secret_3456\n"
        ),
    )
    result = _run(repo, "local")
    assert result.returncode == 0
    output_file = repo / "env" / "local.runtime.secrets.env"
    assert output_file.exists()
    rendered = output_file.read_text(encoding="utf-8")
    assert "BINANCE_API_KEY=bn_test_key_1234" in rendered
    assert "BINANCE_API_SECRET=bn_test_secret_5678" in rendered
    assert "BYBIT_API_KEY=bb_test_key_9012" in rendered
    assert "BYBIT_API_SECRET=bb_test_secret_3456" in rendered
    assert "BINANCE_API_KEY=***1234" in result.stdout
    assert "BYBIT_API_SECRET=***3456" in result.stdout


def test_materialize_prod_fails_when_selected_exchange_prod_keys_missing(tmp_path: Path) -> None:
    repo = _setup_temp_repo(
        tmp_path,
        exchange="bybit",
        mode="prod",
        source_env=("BINANCE_PROD_API_KEY=bn_prod_key_1234\nBINANCE_PROD_API_SECRET=bn_prod_secret_5678\n"),
    )
    result = _run(repo, "prod")
    assert result.returncode == 1
    assert "Missing BYBIT_PROD_API_KEY/SECRET" in (result.stdout + result.stderr)


def test_materialize_rejects_invalid_mode(tmp_path: Path) -> None:
    repo = _setup_temp_repo(
        tmp_path,
        exchange="binance_testnet",
        mode="local",
        source_env="BINANCE_TEST_API_KEY=x\nBINANCE_TEST_API_SECRET=y\n",
    )
    result = _run(repo, "staging")
    assert result.returncode == 1
    assert "Usage:" in (result.stdout + result.stderr)
