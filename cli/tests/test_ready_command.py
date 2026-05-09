from __future__ import annotations

import argparse
import json
from pathlib import Path

from awf.commands.ready import run_ready
from awf.core.ready import collect_ready_report


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


def test_run_ready_json_and_human_output(tmp_path: Path, monkeypatch, capsys) -> None:
    repo, skills = _prepare_repo(tmp_path)
    monkeypatch.setenv("AWF_SKILLS_DIR", str(skills))

    rc = run_ready(argparse.Namespace(repo_root=str(repo), json=True, probe=False))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["repo_root"] == str(repo.resolve())
    assert payload["capabilities"][0]["name"] == "inspect"

    rc = run_ready(argparse.Namespace(repo_root=str(repo), json=False, probe=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "automation_level: 2 (provider execution)" in out
    assert "recommended_next:" in out
