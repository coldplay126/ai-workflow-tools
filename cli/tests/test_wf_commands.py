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
