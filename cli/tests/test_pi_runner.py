from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from awf.runners.pi import PiRunnerConfig, build_pi_print_command, run_pi_print


def _write_fake_pi(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_build_pi_print_command_defaults_to_ephemeral_print_mode() -> None:
    cmd = build_pi_print_command("hello", PiRunnerConfig(command="pi"))

    assert cmd == ["pi", "--no-session", "-p", "hello"]


def test_run_pi_print_invokes_fake_binary_and_returns_provider_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    argv_log = tmp_path / "argv.log"
    env_log = tmp_path / "env.log"
    fake_pi = _write_fake_pi(
        tmp_path / "pi",
        "\n".join([
            "#!/bin/sh",
            ': > "$PI_ARGV_LOG"',
            'for arg in "$@"; do printf "%s\\n" "$arg" >> "$PI_ARGV_LOG"; done',
            'printf "%s\\n" "$PI_SKIP_VERSION_CHECK" > "$PI_ENV_LOG"',
            'printf "pi answer\\n"',
            "",
        ]),
    )
    monkeypatch.setenv("PI_ARGV_LOG", str(argv_log))
    monkeypatch.setenv("PI_ENV_LOG", str(env_log))

    result = run_pi_print(
        "summarize this",
        cwd=str(tmp_path),
        config=PiRunnerConfig(command=str(fake_pi)),
    )

    assert result.returncode == 0
    assert result.provider_name == "pi"
    assert result.stdout == "pi answer\n"
    assert result.stderr == ""
    assert result.elapsed_sec >= 0
    assert argv_log.read_text(encoding="utf-8").splitlines() == [
        "--no-session",
        "-p",
        "summarize this",
    ]
    assert env_log.read_text(encoding="utf-8").strip() == "1"


def test_run_pi_print_honors_awf_pi_command_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_pi = _write_fake_pi(
        bin_dir / "custom-pi",
        "#!/bin/sh\nprintf custom\n",
    )
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("AWF_PI_COMMAND", "custom-pi")

    result = run_pi_print("hello", cwd=str(tmp_path))

    assert result.returncode == 0
    assert result.stdout == "custom"
    assert fake_pi.name == "custom-pi"


def test_run_pi_print_reports_missing_command() -> None:
    result = run_pi_print(
        "hello",
        config=PiRunnerConfig(command="/definitely/missing/pi"),
    )

    assert result.returncode == 127
    assert result.provider_name == "pi"
    assert "pi command not found" in result.stderr


def test_run_pi_print_reports_timeout() -> None:
    with patch(
        "awf.runners.pi.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["pi"], timeout=1),
    ):
        result = run_pi_print(
            "hello",
            config=PiRunnerConfig(command="pi", timeout_sec=1),
        )

    assert result.returncode == 124
    assert result.provider_name == "pi"
    assert result.stderr == "runner_timeout: pi timed out after 1s"


def test_pi_runner_config_from_env_falls_back_on_bad_timeout(monkeypatch) -> None:
    monkeypatch.setenv("AWF_PI_COMMAND", "custom-pi")
    monkeypatch.setenv("AWF_PI_TIMEOUT_SEC", "not-int")

    config = PiRunnerConfig.from_env()

    assert config.command == "custom-pi"
    assert config.timeout_sec == 300
