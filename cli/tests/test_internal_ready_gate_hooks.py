from __future__ import annotations

import argparse
import json

from awf.commands import analyze as analyze_command
from awf.commands import wf as wf_command
from awf.commands import wiki as wiki_command


def test_analyze_enforces_ready_gate_before_provider_work(monkeypatch) -> None:
    monkeypatch.setattr(analyze_command, "enforce_ready_gate", lambda *args, **kwargs: 20)

    rc = analyze_command.run_analyze(argparse.Namespace(
        check=False,
        catalog=False,
        cycles=False,
        dry_run=False,
        all=False,
        domain="orders",
        output_format="text",
    ))

    assert rc == 20


def test_analyze_skips_ready_gate_for_dry_run(monkeypatch) -> None:
    called = False

    def fake_gate(*_args, **_kwargs):
        nonlocal called
        called = True
        return 20

    monkeypatch.setattr(analyze_command, "enforce_ready_gate", fake_gate)

    rc = analyze_command.run_analyze(argparse.Namespace(
        check=False,
        catalog=False,
        cycles=False,
        dry_run=True,
        all=False,
        domain=None,
    ))

    assert rc == 2
    assert called is False


def test_analyze_status_skips_ready_gate(monkeypatch, capsys) -> None:
    called = False

    def fake_gate(*_args, **_kwargs):
        nonlocal called
        called = True
        return 20

    monkeypatch.setattr(analyze_command, "enforce_ready_gate", fake_gate)
    monkeypatch.setattr(
        analyze_command, "resolve_analysis_context", lambda **_kwargs: object()
    )
    monkeypatch.setattr(
        analyze_command, "load_analysis_state", lambda _context: {"status": "ok"}
    )
    monkeypatch.setattr(
        analyze_command, "summarize_analysis_state", lambda _state: "status summary"
    )

    rc = analyze_command.run_analyze(argparse.Namespace(
        check=False,
        catalog=False,
        cycles=False,
        all=False,
        service="api",
        domain="orders",
        status=True,
        repo_root=".",
        docs_root=None,
        github_root=None,
        json=False,
    ))

    out = capsys.readouterr().out
    assert rc == 0
    assert out == "status summary\n"
    assert called is False


def test_analyze_dry_run_uses_deterministic_discovery_only(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    from awf.commands import analyze as analyze_command
    from awf.core import scanner

    github_root = tmp_path / "github"
    repo = github_root / "script_repo"
    target = repo / "manual"
    target.mkdir(parents=True)
    (repo / ".git").mkdir()
    (target / "job.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    def fail_ai_discovery(*_args, **_kwargs):
        raise AssertionError("dry-run must not invoke AI unit discovery")

    monkeypatch.setattr(scanner, "_ai_discover_units", fail_ai_discovery)

    rc = analyze_command.run_analyze(argparse.Namespace(
        check=False,
        catalog=False,
        cycles=False,
        all=False,
        status=False,
        service="script_repo",
        domain="manual",
        repo_root=str(repo),
        docs_root=str(tmp_path / "analysis-docs"),
        github_root=str(github_root),
        provider=None,
        mode=None,
        non_interactive=False,
        dry_run=True,
        print_prompt=False,
        output_format="json",
    ))

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["command"] == "analyze"
    assert payload["service"] == "script_repo"
    assert payload["domain"] == "manual"
    assert payload["domain_directories"] == [str(target.resolve())]
    assert "Analyze the `manual` unit" in payload["prompt"]


def test_wf_init_enforces_ready_gate(monkeypatch) -> None:
    monkeypatch.setattr(wf_command, "enforce_ready_gate", lambda *args, **kwargs: 20)

    rc = wf_command.run_wf_init(argparse.Namespace(repo_root=".", concept="x", force=False))

    assert rc == 20


def test_wf_next_dry_run_writes_no_state_or_prompt(monkeypatch, capsys) -> None:
    gate_called = False

    def fake_gate(*_args, **_kwargs):
        nonlocal gate_called
        gate_called = True
        return 20

    def fail_write(*_args, **_kwargs):
        raise AssertionError("dry-run must not write workflow state or prompt files")

    monkeypatch.setattr(wf_command, "enforce_ready_gate", fake_gate)
    monkeypatch.setattr(wf_command, "load_awf_config", lambda _repo_root: object())
    monkeypatch.setattr(
        wf_command, "load_workflow_state", lambda _repo_root: {"phases": {}}
    )
    monkeypatch.setattr(
        wf_command, "load_workflow_provider_config", lambda _repo_root: {}
    )
    monkeypatch.setattr(wf_command, "resolve_next_phase", lambda _state, _phase: "plan")
    monkeypatch.setattr(wf_command, "build_workflow_prompt", lambda *_args: "PROMPT")
    monkeypatch.setattr(wf_command, "save_workflow_prompt", fail_write)
    monkeypatch.setattr("awf.core.state.save_workflow_state_snapshot", fail_write)

    rc = wf_command.run_wf_next(argparse.Namespace(
        repo_root=".",
        auto_apply=False,
        dry_run=True,
        output_format="text",
        phase=None,
        provider="fixture",
        mode=None,
        print_prompt=False,
        non_interactive=False,
    ))

    out = capsys.readouterr().out
    assert rc == 0
    assert gate_called is False
    assert "prompt_file: (dry-run, not written)" in out
    assert "PROMPT" in out


def test_wf_next_dry_run_json_outputs_structured_prompt(monkeypatch, capsys) -> None:
    monkeypatch.setattr(wf_command, "enforce_ready_gate", lambda *args, **kwargs: 20)
    monkeypatch.setattr(wf_command, "load_awf_config", lambda _repo_root: object())
    monkeypatch.setattr(
        wf_command, "load_workflow_state", lambda _repo_root: {"phases": {}}
    )
    monkeypatch.setattr(
        wf_command, "load_workflow_provider_config", lambda _repo_root: {}
    )
    monkeypatch.setattr(wf_command, "resolve_next_phase", lambda _state, _phase: "plan")
    monkeypatch.setattr(wf_command, "build_workflow_prompt", lambda *_args: "PROMPT")

    rc = wf_command.run_wf_next(argparse.Namespace(
        repo_root=".",
        auto_apply=False,
        dry_run=True,
        output_format="json",
        phase=None,
        provider="fixture",
        mode=None,
        print_prompt=False,
        non_interactive=False,
    ))

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["phase"] == "plan"
    assert payload["provider"] == "fixture"
    assert payload["prompt_file"] == "(dry-run, not written)"
    assert payload["prompt"] == "PROMPT"


def test_wiki_decision_enforces_ready_gate(monkeypatch) -> None:
    monkeypatch.setattr(wiki_command, "enforce_ready_gate", lambda *args, **kwargs: 20)

    rc = wiki_command.run_wiki_decision(argparse.Namespace(repo_root=".", title="x"))

    assert rc == 20


def test_wiki_compile_skips_ready_gate_for_dry_run(monkeypatch, tmp_path) -> None:
    called = False

    def fake_gate(*_args, **_kwargs):
        nonlocal called
        called = True
        return 20

    monkeypatch.setattr(wiki_command, "enforce_ready_gate", fake_gate)

    rc = wiki_command.run_wiki_compile(argparse.Namespace(
        repo_root=str(tmp_path),
        since=None,
        topic=None,
        dry_run=True,
        show_body=False,
        json=False,
    ))

    assert rc == 0
    assert called is False
