"""Editable CLI source skills remain usable outside the AWF checkout."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from awf.core.skills import find_skill_dir
from awf.core.spec_loader import clear_cache, list_skill_resources, load_json_resource
from awf.core.state import initialize_workflow
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


def _consumer_env(tmp_path: Path) -> tuple[Path, dict[str, str]]:
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
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return repo, env


def _template_lines(name: str) -> list[str]:
    return (SOURCE_SKILL / "prompts" / f"{name}.md").read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("phase", ["plan", "review", "verify"])
def test_consumer_workflow_prompt_uses_editable_source_templates(
    tmp_path: Path, phase: str,
) -> None:
    # Child commands use the consumer cwd without installed workflow links.
    repo, env = _consumer_env(tmp_path)

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
    base_lines = _template_lines("base")
    base_heading = next(line for line in base_lines if line.startswith("=== ") and line.strip())
    assert base_heading in prompt
    for field in ("phase_mode:", "recommended_protocol:"):
        template_prefix = next(
            line.split("{")[0] for line in base_lines if line.startswith(field)
        )
        assert any(
            line.startswith(template_prefix) and len(line) > len(template_prefix)
            for line in prompt.splitlines()
        )
    envelope_lines = _template_lines("envelope-schema")
    envelope_heading = next(line for line in envelope_lines if line.startswith("## ") and line.strip())
    envelope_instruction = next(line for line in envelope_lines[1:] if line.strip())
    assert envelope_heading in prompt
    assert envelope_instruction in prompt
    if phase in {"review", "verify"}:
        gate_lines = _template_lines(f"{phase}-gate")
        gate_heading = next(line for line in gate_lines if line.startswith("## ") and line.strip())
        gate_condition = next(line for line in gate_lines if line.startswith("1. ") and line.strip())
        assert gate_heading in prompt
        assert gate_condition in prompt


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
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
    monkeypatch.delenv("AWF_WORKFLOW_TEMPLATE_DIR", raising=False)
    monkeypatch.chdir(consumer)

    assert find_skill_dir("wf-orchestrator") is None
    initialize_workflow(str(consumer), "No editable checkout")
    assert not (consumer / ".workflow" / "agent-cards").exists()
    assert "workflow templates unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("partial_override", ["project-skill", "home-base"])
def test_partial_skill_install_keeps_workflow_usable(
    tmp_path: Path, partial_override: str,
) -> None:
    repo, env = _consumer_env(tmp_path)
    if partial_override == "project-skill":
        partial = repo / ".claude" / "skills" / "wf-orchestrator"
        partial.mkdir(parents=True)
        (partial / "SKILL.md").write_text("# partial\n", encoding="utf-8")
    else:
        partial = Path(env["HOME"]) / ".claude" / "skills" / "wf-orchestrator"
        prompts = partial / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "base.md").write_text("Operator-provided base prompt\n", encoding="utf-8")

    _run_awf(repo, env, "init", "Partial skill install")
    assert (repo / ".workflow" / "agent-cards" / "plan.json").is_file()
    result = _run_awf(
        repo, env, "next", "--phase", "plan", "--provider", "fixture",
        "--dry-run", "--output-format", "json",
    )
    prompt = json.loads(result.stdout)["prompt"]
    if partial_override == "project-skill":
        base_heading = next(
            line for line in _template_lines("base") if line.startswith("=== ") and line.strip()
        )
        assert base_heading in prompt
    else:
        assert (partial / "prompts" / "base.md").read_text(encoding="utf-8").strip() in prompt
    envelope_heading = next(
        line for line in _template_lines("envelope-schema") if line.startswith("## ") and line.strip()
    )
    assert envelope_heading in prompt


def test_workflow_bootstrap_keeps_project_templates_before_home(
    tmp_path: Path,
) -> None:
    repo, env = _consumer_env(tmp_path)
    source_templates = SOURCE_SKILL / "templates"
    project_templates = repo / "claude" / "skills" / "wf-orchestrator" / "templates"
    home_templates = Path(env["HOME"]) / ".claude" / "skills" / "wf-orchestrator" / "templates"
    for templates, description in (
        (project_templates, "Consumer project agent card"),
        (home_templates, "Installed home agent card"),
    ):
        shutil.copytree(source_templates, templates)
        card_path = templates / "agent-cards" / "plan.json"
        card = json.loads(card_path.read_text(encoding="utf-8"))
        card["description"] = description
        card_path.write_text(json.dumps(card), encoding="utf-8")

    _run_awf(repo, env, "init", "Local template priority")
    installed_card = json.loads(
        (repo / ".workflow" / "agent-cards" / "plan.json").read_text(encoding="utf-8")
    )
    assert installed_card["description"] == "Consumer project agent card"


def test_partial_analysis_resource_uses_next_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, env = _consumer_env(tmp_path)
    override = Path(env["HOME"]) / ".config" / "awf" / "skills" / "analysis" / "modes"
    override.mkdir(parents=True)
    (override / "document.json").write_text('{"source":"operator"}', encoding="utf-8")
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.delenv("AWF_SKILLS_DIR", raising=False)
    monkeypatch.chdir(repo)
    clear_cache()
    try:
        assert load_json_resource("analysis", "modes", "document") == {"source": "operator"}
        assert load_json_resource("analysis", "modes", "review")["mode"] == "review"
        assert {"document", "review"} <= set(list_skill_resources("analysis", "modes"))
    finally:
        clear_cache()


def test_missing_prompt_templates_emit_visible_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    repo, env = _consumer_env(tmp_path)
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.delenv("AWF_SKILLS_DIR", raising=False)
    monkeypatch.delenv("AWF_WORKFLOW_TEMPLATE_DIR", raising=False)
    monkeypatch.chdir(repo)
    initialize_workflow(str(repo), "Missing prompt warning")
    from awf.core.workflow_prompt import build_workflow_prompt
    monkeypatch.setattr("awf.core.skills.installed_source_checkout", lambda: None)

    state = json.loads((repo / ".workflow" / "state.json").read_text(encoding="utf-8"))
    config = json.loads((repo / ".workflow" / "provider-config.json").read_text(encoding="utf-8"))
    prompt = build_workflow_prompt(str(repo), state, config, "plan")

    assert "phase: plan" in prompt
    warnings = capsys.readouterr().err
    assert "base.md unavailable" in warnings
    assert "phase-fallback.md unavailable" in warnings
    assert "envelope-schema.md unavailable" in warnings
