"""cmux_health.probe_cmux_broker_health unit tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from awf.core.cmux_health import probe_cmux_broker_health


@pytest.fixture(autouse=True)
def _clear_disable_env(monkeypatch):
    monkeypatch.delenv("AWF_WF_STATUS_NO_CMUX", raising=False)


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["cmux-agent", "doctor"], returncode=returncode, stdout=stdout, stderr=stderr,
    )


class TestProbeCmuxBrokerHealth:
    def test_unavailable_when_cli_missing(self, tmp_path: Path):
        with patch("awf.core.cmux_health.subprocess.run", side_effect=FileNotFoundError):
            result = probe_cmux_broker_health(tmp_path)
        assert result["status"] == "unavailable"
        assert "cmux-agent CLI not found" in result["detail"]

    def test_skipped_via_env(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AWF_WF_STATUS_NO_CMUX", "1")
        with patch("awf.core.cmux_health.subprocess.run") as run_mock:
            result = probe_cmux_broker_health(tmp_path)
        assert result["status"] == "skipped"
        run_mock.assert_not_called()

    def test_timeout(self, tmp_path: Path):
        with patch(
            "awf.core.cmux_health.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["cmux-agent"], timeout=5.0),
        ):
            result = probe_cmux_broker_health(tmp_path, timeout_seconds=5.0)
        assert result["status"] == "timeout"
        assert "5.0s" in result["detail"]

    def test_error_returncode(self, tmp_path: Path):
        with patch(
            "awf.core.cmux_health.subprocess.run",
            return_value=_completed(stderr="boom", returncode=1),
        ):
            result = probe_cmux_broker_health(tmp_path)
        assert result["status"] == "error"
        assert "boom" in result["detail"]

    def test_error_invalid_json(self, tmp_path: Path):
        with patch(
            "awf.core.cmux_health.subprocess.run",
            return_value=_completed(stdout="not-json"),
        ):
            result = probe_cmux_broker_health(tmp_path)
        assert result["status"] == "error"
        assert "invalid JSON" in result["detail"]

    def test_error_missing_health(self, tmp_path: Path):
        with patch(
            "awf.core.cmux_health.subprocess.run",
            return_value=_completed(stdout=json.dumps({"checks": []})),
        ):
            result = probe_cmux_broker_health(tmp_path)
        assert result["status"] == "error"
        assert "health" in result["detail"]

    def test_alive_response(self, tmp_path: Path):
        payload = {
            "health": {
                "broker_daemon": {"status": "alive", "pid": 123, "detail": "pid 123 responding"},
                "events_log": {"status": "fresh"},
                "sqlite_integrity": {"status": "ok"},
            }
        }
        with patch(
            "awf.core.cmux_health.subprocess.run",
            return_value=_completed(stdout=json.dumps(payload)),
        ):
            result = probe_cmux_broker_health(tmp_path)
        assert result["status"] == "alive"
        assert result["broker_daemon"]["pid"] == 123
        assert result["events_log"]["status"] == "fresh"
        assert result["sqlite_integrity"]["status"] == "ok"

    def test_stale_response(self, tmp_path: Path):
        payload = {
            "health": {
                "broker_daemon": {"status": "stale", "pid": 99999},
                "events_log": {"status": "stale", "detail": "last 600s ago"},
                "sqlite_integrity": {"status": "ok"},
            }
        }
        with patch(
            "awf.core.cmux_health.subprocess.run",
            return_value=_completed(stdout=json.dumps(payload)),
        ):
            result = probe_cmux_broker_health(tmp_path)
        assert result["status"] == "stale"
        assert result["events_log"]["status"] == "stale"
