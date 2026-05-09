from __future__ import annotations

import argparse

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


def test_wf_init_enforces_ready_gate(monkeypatch) -> None:
    monkeypatch.setattr(wf_command, "enforce_ready_gate", lambda *args, **kwargs: 20)

    rc = wf_command.run_wf_init(argparse.Namespace(repo_root=".", concept="x", force=False))

    assert rc == 20


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
