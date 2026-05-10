from __future__ import annotations

import os
from pathlib import Path

from awf.core.config import load_awf_config
from awf.core.readiness import collect_doctor_report


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

    config = load_awf_config(str(repo))
    report = collect_doctor_report(config, str(repo))
    runners = {item["runner"]: item for item in report["runners"]}

    assert runners["pi"]["kind"] == "terminal_harness"
    assert runners["pi"]["installed"]["status"] == "ok"
    assert runners["pi"]["installed"]["path"] == str(pi)
    assert runners["pi"]["configured"]["status"] == "skip"
    assert runners["pi"]["backend"]["status"] == "skip"
    assert "dispatch backend yet" in runners["pi"]["backend"]["detail"]


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
