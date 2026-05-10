from __future__ import annotations

import os
from pathlib import Path

from awf.core.config import load_awf_config
from awf.core.readiness import PI_AUTH_ENV_NAMES, collect_doctor_report


def _repo_root(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / ".awf.toml").write_text(
        "\n".join([
            "[provider]",
            'default = "fixture"',
            "",
            "[provider.fixture]",
            'result_file = ""',
            "",
        ]),
        encoding="utf-8",
    )
    return path


def test_doctor_report_detects_pi_runner_without_requiring_backend(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo_root(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pi = bin_dir / "pi"
    pi.write_text("#!/bin/sh\necho pi\n", encoding="utf-8")
    pi.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    for env_name in PI_AUTH_ENV_NAMES:
        monkeypatch.delenv(env_name, raising=False)

    config = load_awf_config(str(repo))
    report = collect_doctor_report(config, str(repo))
    runners = {item["runner"]: item for item in report["runners"]}

    assert runners["pi"]["kind"] == "terminal_harness"
    assert runners["pi"]["installed"]["status"] == "ok"
    assert runners["pi"]["installed"]["path"] == str(pi)
    assert runners["pi"]["configured"]["status"] == "skip"
    assert runners["pi"]["backend"]["status"] == "ok"
    assert "surface_preference=pi" in runners["pi"]["backend"]["detail"]
    assert report["pi_readiness"]["status"] == "ready"
    assert report["pi_readiness"]["command"] == "pi"
    assert report["pi_readiness"]["command_source"] == "default"
    assert report["pi_readiness"]["version"]["status"] == "ok"
    assert report["pi_readiness"]["version"]["version"] == "pi"
    assert report["pi_readiness"]["auth_env_any"] is False
    assert report["pi_readiness"]["dispatch_surface"] == "opt_in_only"
    assert report["pi_readiness"]["billing_warning"]["billing_context"] == "anthropic_extra_usage"
    assert "pi" in report["dispatch"]["available_surfaces"]


def test_doctor_report_honors_awf_pi_command_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo_root(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    custom_pi = bin_dir / "custom-pi"
    custom_pi.write_text("#!/bin/sh\necho pi\n", encoding="utf-8")
    custom_pi.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("AWF_PI_COMMAND", "custom-pi")

    config = load_awf_config(str(repo))
    report = collect_doctor_report(config, str(repo))
    runner = report["runners"][0]

    assert runner["runner"] == "pi"
    assert runner["installed"]["command"] == "custom-pi"
    assert runner["installed"]["path"] == str(custom_pi)
    assert report["pi_readiness"]["command"] == "custom-pi"
    assert report["pi_readiness"]["command_source"] == "AWF_PI_COMMAND"
    assert report["pi_readiness"]["path"] == str(custom_pi)
    assert report["dispatch"]["pi_backend_ready"] is True


def test_doctor_report_marks_missing_pi_readiness(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo_root(tmp_path / "repo")
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    monkeypatch.delenv("AWF_PI_COMMAND", raising=False)

    config = load_awf_config(str(repo))
    report = collect_doctor_report(config, str(repo))

    assert report["pi_readiness"]["status"] == "missing"
    assert report["pi_readiness"]["installed"]["status"] == "fail"
    assert report["pi_readiness"]["version"]["status"] == "skip"
    assert report["dispatch"]["pi_backend_ready"] is False
