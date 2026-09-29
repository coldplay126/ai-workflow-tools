"""Editable CLI source skills remain usable outside the AWF checkout."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from awf.core.skills import find_skill_dir
from fixture_support import write_minimal_workflow_artifacts


SOURCE_SKILL = Path(__file__).resolve().parents[2] / "claude" / "skills" / "wf-orchestrator"


def _run_awf(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "awf", "wf", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result


@pytest.mark.parametrize("phase", ["plan", "review", "verify"])
def test_consumer_workflow_prompt_uses_editable_source_templates(
    tmp_path: Path, phase: str,
) -> None:
    home = tmp_path / "empty-home"
    home.mkdir()
    repo = tmp_path / "consumer"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".awf.toml").write_text(
        '[provider]\ndefault = "fixture"\n\n[provider.fixture]\nresult_file = ""\n',
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text("[project]\nname = 'consumer'\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"))
    env.pop("AWF_SKILLS_DIR", None)
    env.pop("AWF_WORKFLOW_TEMPLATE_DIR", None)
    # Child commands run from the consumer cwd while importing this checkout's
    # editable package, not skills provided by the consumer or a test fixture.
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    _run_awf(repo, env, "init", "Prompt templates are required")
    assert (repo / ".workflow" / "agent-cards" / f"{phase}.json").is_file()
    if phase != "plan":
        write_minimal_workflow_artifacts(repo)
        state_path = repo / ".workflow" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["gates"]["G1"]["passed"] = True
        state["phases"]["plan"]["status"] = "completed"
        if phase == "verify":
            state["gates"]["G4"]["passed"] = True
            state["phases"]["impl"]["status"] = "completed"
        state_path.write_text(json.dumps(state), encoding="utf-8")

    result = _run_awf(
        repo, env, "next", "--phase", phase, "--provider", "fixture",
        "--dry-run", "--output-format", "json",
    )
    prompt = json.loads(result.stdout)["prompt"]
    base_lines = (
        (SOURCE_SKILL / "prompts" / "base.md")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert base_lines[2] in prompt
    for field in ("phase_mode:", "recommended_protocol:"):
        template_prefix = next(
            line.split("{")[0] for line in base_lines if line.startswith(field)
        )
        assert any(
            line.startswith(template_prefix) and len(line) > len(template_prefix)
            for line in prompt.splitlines()
        )
    envelope_lines = (
        (SOURCE_SKILL / "prompts" / "envelope-schema.md")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert envelope_lines[0] in prompt
    assert envelope_lines[2] in prompt
    if phase in {"review", "verify"}:
        gate_lines = (
            (SOURCE_SKILL / "prompts" / f"{phase}-gate.md")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        assert gate_lines[0] in prompt
        assert gate_lines[4] in prompt


def test_runtime_installed_skill_precedes_editable_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "consumer"
    repo.mkdir()
    (repo / ".git").mkdir()
    installed = tmp_path / "home" / ".config" / "awf" / "skills" / "wf-orchestrator"
    installed.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AWF_SKILLS_DIR", raising=False)
    monkeypatch.chdir(repo)

    assert find_skill_dir("wf-orchestrator") == installed


def test_editable_source_lookup_also_works_without_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.delenv("AWF_SKILLS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    assert find_skill_dir("wf-orchestrator") == SOURCE_SKILL


def test_nested_wheel_is_not_an_editable_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "other-checkout"
    source = checkout / "cli" / "src" / "awf"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("", encoding="utf-8")
    (checkout / "cli" / "pyproject.toml").write_text(
        "[project]\nname='awf-cli'\n", encoding="utf-8",
    )
    installed = checkout / "venv" / "lib" / "site-packages" / "awf" / "__init__.py"
    installed.parent.mkdir(parents=True)
    installed.write_text("", encoding="utf-8")
    (checkout / "claude" / "skills" / "wf-orchestrator").mkdir(parents=True)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / ".git").mkdir()
    monkeypatch.setattr("awf.__file__", str(installed))
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.delenv("AWF_SKILLS_DIR", raising=False)
    monkeypatch.chdir(consumer)

    assert find_skill_dir("wf-orchestrator") is None
