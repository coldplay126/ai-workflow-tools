from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from awf.commands.ready import run_ready
from awf.core.pi_field_smoke import write_pi_field_smoke_result
from awf.core.ready import collect_ready_report, evaluate_ready_gate


_WF_SKILLS = [
    "wf-orchestrator",
    "wf-status",
    "phase-plan",
    "phase-review",
    "phase-impl",
    "phase-verify",
    "phase-test",
    "phase-done",
]


def _write_skill(root: Path, name: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join([
            "---",
            f"name: {name}",
            f"description: Fixture skill {name}",
            "---",
            "",
            f"# {name}",
            "",
        ]),
        encoding="utf-8",
    )


def _prepare_repo(tmp_path: Path, *, workflow_started: bool = False) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / ".awf.toml").write_text(
        "\n".join([
            "[provider]",
            'default = "fixture"',
            "",
            "[provider.fixture]",
            'result_file = ""',
            "",
            "[paths]",
            f'analysis_docs = "{tmp_path / "analysis-docs"}"',
            "",
        ]),
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text("[project]\nname = 'repo'\n", encoding="utf-8")
    source = repo / "src" / "orders"
    source.mkdir(parents=True)
    (source / "service.py").write_text("def handle():\n    return 1\n", encoding="utf-8")
    if workflow_started:
        workflow = repo / ".workflow"
        workflow.mkdir()
        (workflow / "state.json").write_text("{}\n", encoding="utf-8")

    skills = tmp_path / "skills"
    for name in _WF_SKILLS:
        _write_skill(skills, name)
    return repo, skills


def test_collect_ready_report_summarizes_repo_and_next_steps(tmp_path: Path, monkeypatch) -> None:
    repo, skills = _prepare_repo(tmp_path)
    monkeypatch.setenv("AWF_SKILLS_DIR", str(skills))

    report = collect_ready_report(str(repo))

    assert report["config"]["status"] == "ready"
    assert report["provider"]["status"] == "ready"
    assert report["scan"]["status"] == "ready"
    assert report["scan"]["unit_count"] == 1
    assert report["scan"]["sample_units"][0]["name"] == "orders"
    assert report["skills"]["status"] == "ready"
    assert report["workflow"]["status"] == "not_started"
    assert report["automation_level"]["safe_level"] == 2
    assert any(
        item["command"] == "awf analyze repo orders --repo-root . --dry-run"
        for item in report["recommended_next"]
    )


def test_collect_ready_report_marks_started_workflow_as_level_three(tmp_path: Path, monkeypatch) -> None:
    repo, skills = _prepare_repo(tmp_path, workflow_started=True)
    monkeypatch.setenv("AWF_SKILLS_DIR", str(skills))

    report = collect_ready_report(str(repo))

    assert report["workflow"]["status"] == "ready"
    assert report["automation_level"]["safe_level"] == 3


def test_collect_ready_report_recommends_subproject_scan(tmp_path: Path, monkeypatch) -> None:
    repo, skills = _prepare_repo(tmp_path)
    monkeypatch.setenv("AWF_SKILLS_DIR", str(skills))
    (repo / "pyproject.toml").unlink()
    (repo / "src" / "orders" / "service.py").unlink()
    nested = repo / "cli"
    nested.mkdir()
    (nested / "pyproject.toml").write_text("[project]\nname = 'cli'\n", encoding="utf-8")

    report = collect_ready_report(str(repo))

    assert report["scan"]["status"] == "caution"
    assert report["scan"]["subprojects"][0]["path"] == "cli"
    assert any(
        item["command"] == "awf scan cli --no-ai"
        for item in report["recommended_next"]
    )


def test_collect_ready_report_accepts_requirements_txt_script_repo(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, skills = _prepare_repo(tmp_path)
    monkeypatch.setenv("AWF_SKILLS_DIR", str(skills))
    (repo / "pyproject.toml").unlink()
    (repo / "src" / "orders" / "service.py").unlink()
    (repo / "requirements.txt").write_text("requests\n", encoding="utf-8")
    collectors = repo / "collectors"
    collectors.mkdir()
    (collectors / "places.py").write_text("def collect():\n    return []\n", encoding="utf-8")

    report = collect_ready_report(str(repo))

    assert report["scan"]["status"] == "ready"
    assert report["scan"]["language"] == "python"
    assert report["scan"]["unit_count"] == 1
    assert report["scan"]["sample_units"][0]["name"] == "collectors"
    assert any(
        item["command"] == "awf analyze repo collectors --repo-root . --dry-run"
        for item in report["recommended_next"]
    )


def test_collect_ready_report_warns_when_workflow_is_gitignored(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, skills = _prepare_repo(tmp_path, workflow_started=True)
    monkeypatch.setenv("AWF_SKILLS_DIR", str(skills))
    (repo / ".gitignore").write_text(".workflow/\n", encoding="utf-8")

    report = collect_ready_report(str(repo))

    assert report["workflow"]["gitignored"] is True
    assert "local-only" in report["workflow"]["warning"]


def test_collect_ready_report_recommends_pi_quota_followup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, skills = _prepare_repo(tmp_path)
    monkeypatch.setenv("AWF_SKILLS_DIR", str(skills))
    write_pi_field_smoke_result(
        repo,
        {
            "schema": "awf_pi_field_smoke_v1",
            "ok": False,
            "reason": "provider_quota_exhausted",
            "billing_context": "anthropic_extra_usage",
            "diagnosis": {
                "kind": "provider_quota_exhausted",
                "next_action": "Enable Extra Usage.",
            },
        },
    )

    report = collect_ready_report(str(repo))

    pi_next = [
        item for item in report["recommended_next"]
        if "run_pi_field_smoke.py" in item["command"]
    ]
    assert pi_next
    assert "--write-result" in pi_next[0]["command"]
    assert "Extra Usage" in pi_next[0]["why"]
    assert "opt-in disabled" in pi_next[0]["why"]


def test_collect_ready_report_recommends_refreshing_stale_pi_smoke(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, skills = _prepare_repo(tmp_path)
    monkeypatch.setenv("AWF_SKILLS_DIR", str(skills))
    write_pi_field_smoke_result(
        repo,
        {
            "schema": "awf_pi_field_smoke_v1",
            "ok": True,
            "reason": "dispatch_ok",
            "diagnosis": {"kind": "dispatch_ok"},
        },
        recorded_at="2000-01-01T00:00:00+00:00",
    )

    report = collect_ready_report(str(repo))

    pi_next = [
        item for item in report["recommended_next"]
        if "run_pi_field_smoke.py" in item["command"]
    ]
    assert pi_next
    assert "--write-result" in pi_next[0]["command"]
    assert "stale" in pi_next[0]["why"]


def test_run_ready_json_and_human_output(tmp_path: Path, monkeypatch, capsys) -> None:
    repo, skills = _prepare_repo(tmp_path)
    monkeypatch.setenv("AWF_SKILLS_DIR", str(skills))
    write_pi_field_smoke_result(
        repo,
        {
            "schema": "awf_pi_field_smoke_v1",
            "ok": True,
            "reason": "dispatch_ok",
            "pi_command_source": "PATH",
            "pi_command": "/bin/pi",
            "diagnosis": {"kind": "dispatch_ok"},
        },
        recorded_at="2026-05-10T00:00:00+00:00",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pi = bin_dir / "pi"
    pi.write_text("#!/bin/sh\necho pi\n", encoding="utf-8")
    pi.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))

    rc = run_ready(argparse.Namespace(repo_root=str(repo), json=True, probe=False))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["repo_root"] == str(repo.resolve())
    assert payload["capabilities"][0]["name"] == "inspect"
    assert payload["doctor"]["runners"][0]["runner"] == "pi"
    assert payload["doctor"]["runners"][0]["installed"]["status"] == "ok"
    assert payload["doctor"]["pi_readiness"]["status"] == "ready"
    assert payload["doctor"]["pi_readiness"]["dispatch_surface"] == "opt_in_only"
    assert payload["doctor"]["pi_readiness"]["last_field_smoke"]["status"] == "found"
    assert payload["doctor"]["pi_readiness"]["last_field_smoke"]["reason"] == "dispatch_ok"
    assert payload["doctor"]["dispatch"]["surface_preference"]["surface_preference"] == "auto"

    rc = run_ready(argparse.Namespace(repo_root=str(repo), json=False, probe=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "automation_level: 2 (provider execution)" in out
    assert "runners: pi=ok" in out
    assert "pi: ready (version=ok, surface=opt_in_only)" in out
    assert "last_field_smoke: ok=True reason=dispatch_ok" in out
    assert "dispatch: preference=auto (ok)" in out
    assert "recommended_next:" in out


def test_evaluate_analysis_gate_allows_provider_run(tmp_path: Path, monkeypatch) -> None:
    repo, skills = _prepare_repo(tmp_path)
    monkeypatch.setenv("AWF_SKILLS_DIR", str(skills))

    report = collect_ready_report(str(repo))
    gate = evaluate_ready_gate(report, "analysis")

    assert gate["decision"] == "allow"
    assert gate["exit_code"] == 0
    assert gate["required_capabilities"] == ["analysis_run"]


def test_evaluate_analysis_gate_allows_provider_caution(tmp_path: Path, monkeypatch) -> None:
    repo, skills = _prepare_repo(tmp_path)
    monkeypatch.setenv("AWF_SKILLS_DIR", str(skills))

    report = collect_ready_report(str(repo))
    report["provider"]["status"] = "caution"
    gate = evaluate_ready_gate(report, "analysis")

    assert gate["decision"] == "allow"
    assert gate["exit_code"] == 0
    assert gate["required_capabilities"] == ["analysis_run"]


def test_evaluate_analysis_gate_downgrades_when_provider_blocked(tmp_path: Path, monkeypatch) -> None:
    repo, skills = _prepare_repo(tmp_path)
    monkeypatch.setenv("AWF_SKILLS_DIR", str(skills))

    report = collect_ready_report(str(repo))
    report["provider"]["status"] = "blocked"
    gate = evaluate_ready_gate(report, "analysis")

    assert gate["decision"] == "dry_run_only"
    assert gate["exit_code"] == 10
    assert gate["required_capabilities"] == ["analysis_dry_run"]


def test_evaluate_analysis_gate_blocks_without_scan_unit(tmp_path: Path, monkeypatch) -> None:
    repo, skills = _prepare_repo(tmp_path)
    monkeypatch.setenv("AWF_SKILLS_DIR", str(skills))
    (repo / "pyproject.toml").unlink()
    (repo / "src" / "orders" / "service.py").unlink()
    nested = repo / "cli"
    nested.mkdir()
    (nested / "pyproject.toml").write_text("[project]\nname = 'cli'\n", encoding="utf-8")

    report = collect_ready_report(str(repo))
    gate = evaluate_ready_gate(report, "analysis")

    assert gate["decision"] == "block"
    assert gate["exit_code"] == 20
    assert gate["recommended_next"][0]["command"] == "awf scan cli --no-ai"


def test_run_ready_gate_json_returns_gate_exit(tmp_path: Path, monkeypatch, capsys) -> None:
    repo, skills = _prepare_repo(tmp_path)
    monkeypatch.setenv("AWF_SKILLS_DIR", str(skills))

    rc = run_ready(argparse.Namespace(repo_root=str(repo), json=True, probe=False, gate="workflow-run"))

    assert rc == 20
    payload = json.loads(capsys.readouterr().out)
    assert payload["gate"]["name"] == "workflow-run"
    assert payload["gate"]["decision"] == "block"
    assert payload["gate"]["exit_code"] == 20


def test_run_ready_workflow_run_gate_allows_started_workflow(tmp_path: Path, monkeypatch) -> None:
    repo, skills = _prepare_repo(tmp_path, workflow_started=True)
    monkeypatch.setenv("AWF_SKILLS_DIR", str(skills))

    rc = run_ready(argparse.Namespace(repo_root=str(repo), json=False, probe=False, gate="workflow-run"))

    assert rc == 0


def test_evaluate_workflow_run_gate_allows_provider_caution(tmp_path: Path, monkeypatch) -> None:
    repo, skills = _prepare_repo(tmp_path, workflow_started=True)
    monkeypatch.setenv("AWF_SKILLS_DIR", str(skills))

    report = collect_ready_report(str(repo))
    report["provider"]["status"] = "caution"
    gate = evaluate_ready_gate(report, "workflow-run")

    assert gate["decision"] == "allow"
    assert gate["exit_code"] == 0


# ---------------------------------------------------------------------------
# sibling_repos manifest validation in ready (PR #117 follow-up)
# ---------------------------------------------------------------------------


def _write_manifest(repo: Path, payload: dict) -> None:
    workflow = repo / ".workflow"
    workflow.mkdir(exist_ok=True)
    (workflow / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def test_collect_ready_report_marks_valid_sibling_manifest_as_ok(
    tmp_path: Path, monkeypatch
) -> None:
    repo, skills = _prepare_repo(tmp_path, workflow_started=True)
    monkeypatch.setenv("AWF_SKILLS_DIR", str(skills))
    _write_manifest(repo, {
        "version": "1.0.0",
        "sibling_repos": [{"name": "api", "path": "../sibling-api"}],
    })

    report = collect_ready_report(str(repo))
    assert report["workflow"]["manifest_status"] == "ok"
    assert report["workflow"]["sibling_repo_count"] == 1
    assert report["workflow"]["manifest_error"] is None


def test_collect_ready_report_marks_invalid_sibling_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    repo, skills = _prepare_repo(tmp_path, workflow_started=True)
    monkeypatch.setenv("AWF_SKILLS_DIR", str(skills))
    # `path` must start with ".." per docs/specs/multi-repo-scope.md §3.1
    _write_manifest(repo, {
        "version": "1.0.0",
        "sibling_repos": [{"name": "api", "path": "./sub"}],
    })

    report = collect_ready_report(str(repo))
    assert report["workflow"]["manifest_status"] == "invalid"
    assert "sibling" in (report["workflow"]["manifest_error"] or "").lower()


def test_workflow_run_gate_blocks_on_invalid_sibling_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    repo, skills = _prepare_repo(tmp_path, workflow_started=True)
    monkeypatch.setenv("AWF_SKILLS_DIR", str(skills))
    _write_manifest(repo, {
        "version": "1.0.0",
        "sibling_repos": [{"name": "bad name", "path": "../x"}],  # invalid chars
    })

    report = collect_ready_report(str(repo))
    gate = evaluate_ready_gate(report, "workflow-run")
    assert gate["decision"] == "block"
    assert "sibling_repos invalid" in gate["reason"]


def test_workflow_run_gate_allows_when_no_sibling_repos(
    tmp_path: Path, monkeypatch
) -> None:
    """Backward compat: manifest without sibling_repos is still 'ok'."""
    repo, skills = _prepare_repo(tmp_path, workflow_started=True)
    monkeypatch.setenv("AWF_SKILLS_DIR", str(skills))
    _write_manifest(repo, {"version": "1.0.0"})  # no sibling_repos field

    report = collect_ready_report(str(repo))
    assert report["workflow"]["manifest_status"] == "ok"
    assert report["workflow"]["sibling_repo_count"] == 0
    gate = evaluate_ready_gate(report, "workflow-run")
    assert gate["decision"] == "allow"
