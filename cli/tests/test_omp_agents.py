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


def test_spec_writer_omits_direct_user_prompt_capability() -> None:
    source = REPO_ROOT / "claude" / "agents" / "spec-writer.md"

    name, generated = compile_claude_agent(source)

    assert name == "spec-writer"
    assert generated.splitlines()[3] == "tools: read, grep, glob, edit, write, bash"



def test_omp_compiler_maps_additive_tool_aliases_to_exact_omp_names(tmp_path: Path) -> None:
    source = _write_claude_agent(tmp_path / "claude" / "agents" / "tool-worker.md")
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "Read, Grep, Glob, Edit, Write, Bash",
            "LSP, AST Search, AST Edit, Debug, Browser, Security Scan",
        ),
        encoding="utf-8",
    )

    _, generated = compile_claude_agent(source)

    assert generated.splitlines()[3] == (
        "tools: lsp, ast_grep, ast_edit, debug, browser, security_scan"
    )


def test_phase_agent_tool_allowlists_preserve_read_only_reviewers() -> None:
    expected_tools = {
        "implementer.md": (
            "read, grep, glob, edit, write, bash, lsp, ast_grep, ast_edit"
        ),
        "spec-verifier.md": "read, grep, glob, bash, security_scan",
        "happy-path-tester.md": "read, grep, glob, bash, browser, debug",
        "adversarial-tester.md": "read, grep, glob, bash, browser, debug",
    }
    for filename, expected in expected_tools.items():
        _, generated = compile_claude_agent(REPO_ROOT / "claude" / "agents" / filename)
        tool_line = generated.splitlines()[3]
        assert tool_line == f"tools: {expected}"
        assert "ask" not in tool_line

    for filename in (
        "analyzer.md",
        "artifact-reviewer.md",
        "code-reviewer.md",
        "plan-validator.md",
        "quality-validator.md",
        "spec-verifier.md",
    ):
        source = (REPO_ROOT / "claude" / "agents" / filename).read_text(encoding="utf-8")
        _, generated = compile_claude_agent(REPO_ROOT / "claude" / "agents" / filename)
        tool_line = generated.splitlines()[3]
        assert "codex_sandbox: read-only" in source
        assert "edit" not in tool_line
        assert "write" not in tool_line
        assert "ask" not in tool_line
        if filename in {"analyzer.md", "artifact-reviewer.md"}:
            assert tool_line == "tools: read, grep, glob"
            assert "bash" not in tool_line


def test_optional_omp_capabilities_are_evidence_not_gate_or_hil_ownership() -> None:
    agents_root = REPO_ROOT / "claude" / "agents"
    verifier = (agents_root / "spec-verifier.md").read_text(encoding="utf-8")
    assert "Security Scan evidence" in verifier
    assert "`not_run` 또는 `skipped`" in verifier
    assert "G5, gate, HIL, workflow state를\n수정하지 않는다" in verifier

    for filename in ("happy-path-tester.md", "adversarial-tester.md"):
        source = (agents_root / filename).read_text(encoding="utf-8")
        assert "unique namespace" in source
        assert "`not_run` 또는 `skipped`" in source
        assert "G6, gate, HIL, workflow state를 수정하지 않고" in source

    cards_root = REPO_ROOT / "claude" / "skills" / "wf-orchestrator" / "templates" / "agent-cards"
    verify_card = json.loads((cards_root / "verify.json").read_text(encoding="utf-8"))
    test_card = json.loads((cards_root / "test.json").read_text(encoding="utf-8"))
    assert verify_card["capabilities"]["sandbox_modes"] == ["read-only"]
    verify_capabilities = verify_card["output"]["structured_result"][
        "capability_evidence"
    ]
    test_capabilities = test_card["output"]["structured_result"][
        "capability_evidence"
    ]
    assert verify_capabilities == [
        {
            "capability": "security_scan",
            "status": "pass|not_run|skipped|failed",
            "reason": "string (required unless pass)",
        }
    ]
    assert test_capabilities == [
        {
            "capability": "browser",
            "status": "pass|not_run|skipped|failed",
            "reason": "string (required unless pass)",
        },
        {
            "capability": "debug",
            "status": "pass|not_run|skipped|failed",
            "reason": "string (required unless pass)",
        },
    ]
    assert (
        '"capability":"security_scan","status":"pass|not_run|skipped|failed"'
        in verifier
    )
    for filename in ("happy-path-tester.md", "adversarial-tester.md"):
        source = (agents_root / filename).read_text(encoding="utf-8")
        assert (
            '"capability":"browser","status":"pass|not_run|skipped|failed"'
            in source
        )
        assert (
            '"capability":"debug","status":"pass|not_run|skipped|failed"'
            in source
        )

def test_checked_in_omp_agents_match_all_canonical_sources() -> None:
    generated_root = REPO_ROOT / ".omp" / "agents"
    manifest = json.loads(
        (generated_root / ".awf-generated-agents.json").read_text(encoding="utf-8")
    )
    records = manifest["files"]
    sources = sorted((REPO_ROOT / "claude" / "agents").glob("*.md"))

    assert manifest["generator"] == "awf agents sync-omp"
    assert manifest["schema_version"] == 1
    assert manifest["source_roots"] == ["claude/agents"]
    assert len(records) == len(sources)
    assert [record["name"] for record in records] == [source.name for source in sources]

    for source, record in zip(sources, records, strict=True):
        output_name, expected = compile_claude_agent(source)
        generated = (generated_root / record["name"]).read_bytes()

        assert record["source"] == f"claude/agents/{source.name}"
        assert record["name"] == f"{output_name}.md"
        assert generated == expected.encode("utf-8")
        assert record["sha256"] == hashlib.sha256(generated).hexdigest()


    primary_policy = (
        "Production primary is never a verify/test benchmark or executable-query target.",
        "read-only schema metadata",
        "explicitly approved replica",
        "warehouse",
        "sanitized local",
    )
    for name in ("spec-verifier.md", "happy-path-tester.md"):
        generated = (generated_root / name).read_text(encoding="utf-8")
        for requirement in primary_policy:
            assert requirement in generated, f"{name}: missing {requirement}"
