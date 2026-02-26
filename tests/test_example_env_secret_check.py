from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_example_env_secrets.py"


def _load_script() -> dict:
    return runpy.run_path(str(SCRIPT), run_name="__test__")


def test_example_env_secret_check_accepts_placeholders(tmp_path):
    module = _load_script()

    env = tmp_path / ".env.openclaw.example"
    env.write_text(
        "\n".join(
            [
                "OPENCLAW_TOKEN=",
                "API_KEY=your_api_key_here",
                "SMTP_PASSWORD=placeholder_password",
                "PRIVATE_KEY=<set_in_local_env>",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    issues = module["find_secret_issues"]([env])
    assert issues == []


def test_example_env_secret_check_flags_non_placeholder_secret(tmp_path):
    module = _load_script()

    env = tmp_path / ".env.example"
    env.write_text(
        "\n".join(
            [
                "OPENCLAW_TOKEN=sk_live_abcdef123456789",
                "BINANCE_PROD_API_SECRET=real_secret_value",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    issues = module["find_secret_issues"]([env])
    assert len(issues) == 2
    assert "OPENCLAW_TOKEN" in issues[0]


def test_example_env_secret_check_main_reports_error(monkeypatch, capsys, tmp_path):
    module = _load_script()

    env = tmp_path / ".env.example"
    env.write_text("OPENCLAW_TOKEN=real_value\n", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["check_example_env_secrets.py", "--root", str(tmp_path)])
    code = module["main"]()

    out = capsys.readouterr().out
    assert code == 1
    assert "Potential secrets found" in out


def test_example_env_secret_check_main_reports_ok(monkeypatch, capsys, tmp_path):
    module = _load_script()

    env = tmp_path / ".env.example"
    env.write_text("OPENCLAW_TOKEN=\n", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["check_example_env_secrets.py", "--root", str(tmp_path)])
    code = module["main"]()

    out = capsys.readouterr().out
    assert code == 0
    assert "placeholders only" in out
