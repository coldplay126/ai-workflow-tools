from __future__ import annotations

import hashlib

import json
from pathlib import Path

from awf.cli import main
from awf.core.omp_agents import compile_claude_agent, sync_omp_agents


REPO_ROOT = Path(__file__).resolve().parents[2]

def _write_claude_agent(path: Path, *, name: str = "implementer") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'''---
name: {name}
description: "Implementation specialist"
tools: Read, Grep, Glob, Edit, Write, Bash
model: opus
isolation: worktree
provider_hint: claude-code
roles: [implementer]
---

# Implementer

Implement the assigned contract.
''',
        encoding="utf-8",
    )
    return path


def test_sync_omp_agents_compiles_supported_frontmatter(tmp_path: Path):
    _write_claude_agent(tmp_path / "claude" / "agents" / "implementer.md")
    result = sync_omp_agents(tmp_path)
    assert result["created"] == ["implementer.md"]
    assert result["conflicts"] == []

    target = tmp_path / ".omp" / "agents" / "implementer.md"
    generated = target.read_text(encoding="utf-8")
    assert "tools: read, grep, glob, edit, write, bash" in generated
    assert "model:" not in generated
    assert "isolation:" not in generated
    assert "provider_hint:" not in generated
    assert "roles:" not in generated
    assert "Implement the assigned contract." in generated

    manifest = json.loads(
        (tmp_path / ".omp" / "agents" / ".awf-generated-agents.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["files"][0]["name"] == "implementer.md"
    assert len(manifest["files"][0]["sha256"]) == 64


def test_sync_omp_agents_role_selector_overrides_explicit_model(tmp_path: Path):
    source = _write_claude_agent(
        tmp_path / "claude" / "agents" / "implementer.md"
    )
    source.write_text(
        source.read_text(encoding="utf-8")
        .replace("model: opus", "model: openai-codex/gpt-5.6-sol")
        .replace(
            "provider_hint: claude-code",
            'omp_model_role: " @task "\nprovider_hint: claude-code',
        ),
        encoding="utf-8",
    )
    sync_omp_agents(tmp_path)
    generated = (
        tmp_path / ".omp" / "agents" / "implementer.md"
    ).read_text(encoding="utf-8")
    assert generated.count("\nmodel: ") == 1
    assert 'model: "@task"' in generated
    assert "openai-codex/gpt-5.6-sol" not in generated


def test_sync_omp_agents_preserves_explicit_cross_runtime_model(tmp_path: Path):
    source = _write_claude_agent(
        tmp_path / "claude" / "agents" / "implementer.md"
    )
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "model: opus", "model: openai-codex/gpt-5.6-sol"
        ),
        encoding="utf-8",
    )
    sync_omp_agents(tmp_path)
    generated = (
        tmp_path / ".omp" / "agents" / "implementer.md"
    ).read_text(encoding="utf-8")
    assert "model: openai-codex/gpt-5.6-sol" in generated



def test_sync_omp_agents_is_idempotent_and_removes_only_tracked_stale_files(tmp_path: Path):
    source = _write_claude_agent(tmp_path / "claude" / "agents" / "worker.md", name="worker")
    sync_omp_agents(tmp_path)
    second = sync_omp_agents(tmp_path)
    assert second["unchanged"] == ["worker.md"]

    custom = tmp_path / ".omp" / "agents" / "custom.md"
    custom.write_text("custom", encoding="utf-8")
    source.unlink()
    replacement = _write_claude_agent(
        tmp_path / "claude" / "agents" / "reviewer.md",
        name="reviewer",
    )
    assert replacement.is_file()
    third = sync_omp_agents(tmp_path)
    assert third["removed"] == ["worker.md"]
    assert custom.read_text(encoding="utf-8") == "custom"


def test_sync_omp_agents_refuses_untracked_name_collision(tmp_path: Path):
    _write_claude_agent(tmp_path / "claude" / "agents" / "worker.md", name="worker")
    _write_claude_agent(tmp_path / "claude" / "agents" / "other.md", name="other")
    target = tmp_path / ".omp" / "agents" / "worker.md"
    target.parent.mkdir(parents=True)
    target.write_text("hand-written", encoding="utf-8")
    result = sync_omp_agents(tmp_path)
    assert result["conflicts"] == ["worker.md"]
    assert target.read_text(encoding="utf-8") == "hand-written"
    assert not (tmp_path / ".omp" / "agents" / "other.md").exists()
    assert not (tmp_path / ".omp" / "agents" / ".awf-generated-agents.json").exists()


def test_agents_sync_omp_cli_supports_dry_run_json(tmp_path: Path, capsys):
    _write_claude_agent(tmp_path / "claude" / "agents" / "worker.md", name="worker")
    exit_code = main(
        [
            "agents",
            "sync-omp",
            "--repo-root",
            str(tmp_path),
            "--dry-run",
            "--json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == ["worker.md"]
    assert not (tmp_path / ".omp").exists()


def test_spec_writer_ask_capability_survives_omp_agent_compilation() -> None:
    source = REPO_ROOT / "claude" / "agents" / "spec-writer.md"

    name, generated = compile_claude_agent(source)

    assert name == "spec-writer"
    assert "tools: read, grep, glob, edit, write, bash, ask" in generated


def test_checked_in_omp_agents_match_database_contract_sources() -> None:
    generated_root = REPO_ROOT / ".omp" / "agents"
    manifest = json.loads(
        (generated_root / ".awf-generated-agents.json").read_text(encoding="utf-8")
    )
    hashes = {entry["name"]: entry["sha256"] for entry in manifest["files"]}

    for name in ("spec-writer.md", "spec-verifier.md", "happy-path-tester.md"):
        source = REPO_ROOT / "claude" / "agents" / name
        _, expected = compile_claude_agent(source)
        generated = (generated_root / name).read_bytes()

        assert generated == expected.encode("utf-8")
        assert hashes[name] == hashlib.sha256(generated).hexdigest()

    spec_writer = (generated_root / "spec-writer.md").read_text(encoding="utf-8")
    assert "tools: read, grep, glob, edit, write, bash, ask" in spec_writer
    assert "database-validation-evidence.json" in spec_writer
