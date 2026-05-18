"""wf status command integration tests with cmux health."""

from __future__ import annotations

import argparse
import io
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from awf.commands.wf import run_wf_status
from awf.core.state import initialize_workflow


@pytest.fixture(autouse=True)
def _clear_disable_env(monkeypatch):
    monkeypatch.delenv("AWF_WF_STATUS_NO_CMUX", raising=False)


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["cmux-agent", "doctor"], returncode=returncode, stdout=stdout, stderr=stderr,
    )


def _init_workflow(tmp_path: Path) -> Path:
    # Make tmp_path satisfy _looks_like_repo_root via .awf.toml sentinel.
    (tmp_path / ".awf.toml").write_text("# fixture\n", encoding="utf-8")
    initialize_workflow(str(tmp_path), "test concept for cmux health", force=True)
    return tmp_path


class TestWfStatusJson:
    def test_status_json_includes_cmux_health(self, tmp_path: Path, capsys):
        _init_workflow(tmp_path)
        payload = {
            "health": {
                "broker_daemon": {"status": "alive", "pid": 200},
                "events_log": {"status": "fresh"},
                "sqlite_integrity": {"status": "ok"},
            }
        }
        args = argparse.Namespace(repo_root=str(tmp_path), json=True)
        with patch(
            "awf.core.cmux_health.subprocess.run",
            return_value=_completed(stdout=json.dumps(payload)),
        ):
            rc = run_wf_status(args)
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert "cmux_broker_health" in out
        assert out["cmux_broker_health"]["status"] == "alive"

    def test_status_text_includes_cmux_health(self, tmp_path: Path, capsys):
        _init_workflow(tmp_path)
        with patch("awf.core.cmux_health.subprocess.run", side_effect=FileNotFoundError):
            args = argparse.Namespace(repo_root=str(tmp_path), json=False)
            rc = run_wf_status(args)
        assert rc == 0
        text = capsys.readouterr().out
        assert "cmux_broker_health: unavailable" in text


class TestWfStatusWatch:
    """ATC-001/003/007 — argparse + mutex + no-watch backward compat."""

    def test_watch_option_parsed(self):
        from awf.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["wf", "status", "--watch", "--interval", "3"])
        assert args.watch is True
        assert args.interval == 3

    def test_watch_defaults(self):
        from awf.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["wf", "status"])
        assert args.watch is False
        assert args.interval == 5

    def test_watch_json_mutex(self, tmp_path: Path, capsys):
        _init_workflow(tmp_path)
        args = argparse.Namespace(
            repo_root=str(tmp_path), json=True, watch=True, interval=5,
        )
        rc = run_wf_status(args)
        assert rc == 2
        err = capsys.readouterr().err
        assert "--watch is incompatible with --json" in err

    def test_status_no_watch_unchanged(self, tmp_path: Path, capsys):
        """ATC-007 regression: omitting --watch keeps the single-shot text path."""
        _init_workflow(tmp_path)
        with patch("awf.core.cmux_health.subprocess.run", side_effect=FileNotFoundError):
            args = argparse.Namespace(
                repo_root=str(tmp_path), json=False, watch=False, interval=5,
            )
            rc = run_wf_status(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "current_phase:" in out
        assert "cmux_broker_health:" in out

    def test_watch_enters_run_watch(self, tmp_path: Path):
        """ATC-008 (entry-point coverage): args.watch=True dispatches to run_watch."""
        _init_workflow(tmp_path)
        args = argparse.Namespace(
            repo_root=str(tmp_path), json=False, watch=True, interval=5,
        )
        called = {}

        def fake_run_watch(render_fn, interval, **kwargs):
            called["interval"] = interval
            called["sample"] = render_fn()
            return 0

        with patch("awf.core.watch_loop.run_watch", side_effect=fake_run_watch):
            with patch("awf.core.cmux_health.subprocess.run", side_effect=FileNotFoundError):
                rc = run_wf_status(args)
        assert rc == 0
        assert called["interval"] == 5
        assert "current_phase:" in called["sample"]


class TestDashboardArgparse:
    """D2 ATC-001 — dashboard subparser registration + handler dispatch."""

    def test_dashboard_subparser_parses_options(self):
        from awf.cli import build_parser
        from awf.commands.dashboard import run_dashboard_command

        parser = build_parser()
        args = parser.parse_args(["dashboard", "--repo-root", ".", "--interval", "3"])
        assert args.command == "dashboard"
        assert args.repo_root == "."
        assert args.interval == 3
        assert args.handler is run_dashboard_command

    def test_dashboard_defaults(self):
        from awf.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["dashboard"])
        assert args.command == "dashboard"
        assert args.repo_root is None
        assert args.interval == 5

    def test_dashboard_handler_invokes_run_dashboard(self, tmp_path: Path):
        """handler 가 run_dashboard 를 올바른 인자로 호출하는지."""
        from awf.commands.dashboard import run_dashboard_command

        called = {}

        def fake_run_dashboard(repo_root, interval, **kwargs):
            called["repo_root"] = repo_root
            called["interval"] = interval
            return 0

        with patch("awf.commands.dashboard.run_dashboard", side_effect=fake_run_dashboard):
            args = argparse.Namespace(repo_root=str(tmp_path), interval=7)
            rc = run_dashboard_command(args)
        assert rc == 0
        assert called["repo_root"] == str(tmp_path)
        assert called["interval"] == 7
