"""§3.4 `awf wf pr` tests.

Covers body composition, dry-run output, and the no-gh / no-state error paths.
The actual `gh pr create` invocation is mocked out — we never need to talk
to GitHub from tests.
"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from awf.commands import wf_pr


def _scaffold_workflow(tmp_path: Path, *, gates: dict | None = None, branch: str = "feat/x") -> Path:
    wf = tmp_path / ".workflow"
    (wf / "artifacts").mkdir(parents=True)
    state = {
        "id": "2026-05-13-demo-cycle",
        "repo": tmp_path.name,
        "branch": branch,
        "changeClass": "standard",
        "currentPhase": "done",
        "phases": {},
        "gates": gates or {
            "G1": {"passed": True},
            "G2": {"passed": True},
            "G3": {"passed": True},
            "G4": {"passed": True},
            "G5": {"passed": True},
            "G6": {"passed": None},
        },
        "totalExecutions": 7,
        "history": [],
    }
    (wf / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (wf / "concept.md").write_text(
        "# Concept\n\n## 요구사항\nDemo cycle for §3.4 testing.\n",
        encoding="utf-8",
    )
    return tmp_path


def test_build_body_contains_gates_and_concept(tmp_path: Path) -> None:
    repo = _scaffold_workflow(tmp_path)
    state = json.loads((repo / ".workflow" / "state.json").read_text())
    body = wf_pr._build_body(state, repo)
    assert "## Summary" in body
    assert "`2026-05-13-demo-cycle`" in body
    assert "`feat/x`" in body
    assert "## Gates" in body
    # 5 gates pass + 1 pending
    assert body.count("✅") == 5
    assert body.count("—") >= 1
    assert "## Concept" in body
    assert "Demo cycle" in body


def test_dry_run_prints_args_and_returns_zero(tmp_path: Path, capsys) -> None:
    repo = _scaffold_workflow(tmp_path)
    rc = wf_pr.run_wf_pr(Namespace(
        repo_root=str(repo),
        base="main",
        title=None,
        body=None,
        no_fill=False,
        draft=False,
        dry_run=True,
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run gh pr create" in out
    assert "base : main" in out
    assert "title: 2026-05-13-demo-cycle" in out


def test_no_state_returns_error(tmp_path: Path, capsys) -> None:
    # repo root marker (.git) present but no .workflow/state.json
    (tmp_path / ".git").mkdir()
    rc = wf_pr.run_wf_pr(Namespace(
        repo_root=str(tmp_path),
        base="main",
        title=None,
        body=None,
        no_fill=False,
        draft=False,
        dry_run=True,
    ))
    assert rc == 2
    err = capsys.readouterr().err
    assert "Missing workflow state" in err
    assert "awf wf init" in err


def test_no_fill_skips_body(tmp_path: Path, capsys) -> None:
    repo = _scaffold_workflow(tmp_path)
    rc = wf_pr.run_wf_pr(Namespace(
        repo_root=str(repo),
        base="main",
        title=None,
        body=None,
        no_fill=True,
        draft=False,
        dry_run=True,
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "body" not in out.lower()  # body section omitted


def test_gh_missing_short_circuits(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = _scaffold_workflow(tmp_path)
    monkeypatch.setattr(wf_pr, "_gh_available", lambda: False)
    rc = wf_pr.run_wf_pr(Namespace(
        repo_root=str(repo),
        base="main",
        title=None,
        body=None,
        no_fill=False,
        draft=False,
        dry_run=False,
    ))
    assert rc == 2
    err = capsys.readouterr().err
    assert "`gh` CLI not found" in err


def test_subprocess_invocation_uses_repo_cwd(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = _scaffold_workflow(tmp_path)
    monkeypatch.setattr(wf_pr, "_gh_available", lambda: True)

    captured = {}

    class _Completed:
        returncode = 0
        stdout = "https://github.com/x/y/pull/1\n"
        stderr = ""

    def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return _Completed()

    monkeypatch.setattr(wf_pr.subprocess, "run", fake_run)
    rc = wf_pr.run_wf_pr(Namespace(
        repo_root=str(repo),
        base="main",
        title=None,
        body=None,
        no_fill=False,
        draft=True,
        dry_run=False,
    ))
    assert rc == 0
    assert captured["cwd"] == str(repo)
    assert captured["cmd"][:3] == ["gh", "pr", "create"]
    assert "--draft" in captured["cmd"]
    assert "--title" in captured["cmd"]
    assert "--body" in captured["cmd"]
    out = capsys.readouterr().out
    assert "https://github.com/x/y/pull/1" in out


def test_subprocess_failure_propagates_returncode(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = _scaffold_workflow(tmp_path)
    monkeypatch.setattr(wf_pr, "_gh_available", lambda: True)

    class _Completed:
        returncode = 1
        stdout = ""
        stderr = "no commits between feat/x and main"

    monkeypatch.setattr(wf_pr.subprocess, "run", lambda *a, **k: _Completed())
    rc = wf_pr.run_wf_pr(Namespace(
        repo_root=str(repo),
        base="main",
        title=None,
        body=None,
        no_fill=False,
        draft=False,
        dry_run=False,
    ))
    assert rc == 1
    err = capsys.readouterr().err
    assert "gh exited 1" in err
    assert "no commits between feat/x and main" in err
