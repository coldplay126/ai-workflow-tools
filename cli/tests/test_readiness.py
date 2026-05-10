from __future__ import annotations

import json
import os
from pathlib import Path

from awf.core.config import load_awf_config
from awf.core.pi_field_smoke import write_pi_field_smoke_result
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


def _write_provider_config(repo: Path, payload: dict) -> None:
    workflow_dir = repo / ".workflow"
    workflow_dir.mkdir()
    (workflow_dir / "provider-config.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


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
    assert (
        report["pi_readiness"]["field_smoke_command"]
        == "python3 cli/tests/run_pi_field_smoke.py --json"
    )
    assert report["pi_readiness"]["last_field_smoke"]["status"] == "missing"
    assert report["dispatch"]["surface_preference"]["surface_preference"] == "auto"
    assert report["dispatch"]["surface_preference"]["source"] == "default"
    assert report["dispatch"]["surface_preference_ready"]["status"] == "ok"
    assert "pi" in report["dispatch"]["available_surfaces"]


def test_doctor_report_normalizes_dispatch_paths_from_relative_repo_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo_root(tmp_path / "repo")
    monkeypatch.chdir(repo)

    config = load_awf_config(".")
    report = collect_doctor_report(config, ".")

    assert report["paths"]["repo_root"] == str(repo.resolve())
    assert report["dispatch"]["cwd_checked"] == str(repo.resolve())
    assert (
        report["dispatch"]["surface_preference"]["provider_config_path"]
        == str(repo.resolve() / ".workflow" / "provider-config.json")
    )


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


def test_doctor_report_marks_configured_pi_dispatch_preference_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo_root(tmp_path / "repo")
    _write_provider_config(repo, {"dispatch": {"surface_preference": "pi"}})
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pi = bin_dir / "pi"
    pi.write_text("#!/bin/sh\necho pi\n", encoding="utf-8")
    pi.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    config = load_awf_config(str(repo))
    report = collect_doctor_report(config, str(repo))

    preference = report["dispatch"]["surface_preference"]
    assert preference["provider_config_exists"] is True
    assert preference["surface_preference"] == "pi"
    assert preference["source"] == "provider_config"
    assert report["dispatch"]["surface_preference_ready"]["status"] == "ok"
    assert "field smoke" in report["dispatch"]["surface_preference_ready"]["detail"]
    assert "auth/quota" in report["dispatch"]["surface_preference_ready"]["detail"]


def test_doctor_report_marks_configured_pi_dispatch_preference_caution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo_root(tmp_path / "repo")
    _write_provider_config(repo, {"dispatch": {"surface_preference": "pi"}})
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    config = load_awf_config(str(repo))
    report = collect_doctor_report(config, str(repo))

    preference = report["dispatch"]["surface_preference"]
    assert preference["surface_preference"] == "pi"
    assert report["dispatch"]["surface_preference_ready"]["status"] == "caution"
    assert (
        "falls back to inline"
        in report["dispatch"]["surface_preference_ready"]["detail"]
    )


def test_doctor_report_marks_invalid_dispatch_preference(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo_root(tmp_path / "repo")
    _write_provider_config(repo, {"dispatch": {"surface_preference": "sideways"}})
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    config = load_awf_config(str(repo))
    report = collect_doctor_report(config, str(repo))

    preference = report["dispatch"]["surface_preference"]
    assert preference["surface_preference"] == "auto"
    assert preference["raw_surface_preference"] == "sideways"
    assert preference["status"] == "invalid"
    assert report["dispatch"]["surface_preference_ready"]["status"] == "ok"


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
    assert (
        report["pi_readiness"]["field_smoke_command"]
        == "python3 cli/tests/run_pi_field_smoke.py --npm-exec --json"
    )
    assert report["dispatch"]["pi_backend_ready"] is False


def test_doctor_report_includes_latest_pi_field_smoke_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo_root(tmp_path / "repo")
    write_pi_field_smoke_result(
        repo,
        {
            "schema": "awf_pi_field_smoke_v1",
            "ok": False,
            "reason": "provider_quota_exhausted",
            "pi_command_source": "PATH",
            "pi_command": "/bin/pi",
            "billing_context": "anthropic_extra_usage",
            "diagnosis": {
                "kind": "provider_quota_exhausted",
                "next_action": "Enable Extra Usage.",
            },
        },
        recorded_at="2026-05-10T00:00:00+00:00",
    )
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    config = load_awf_config(str(repo))
    report = collect_doctor_report(config, str(repo))

    last_smoke = report["pi_readiness"]["last_field_smoke"]
    assert last_smoke["status"] == "found"
    assert last_smoke["recorded_at"] == "2026-05-10T00:00:00+00:00"
    assert last_smoke["ok"] is False
    assert last_smoke["reason"] == "provider_quota_exhausted"
    assert last_smoke["billing_context"] == "anthropic_extra_usage"
    assert last_smoke["next_action"] == "Enable Extra Usage."
