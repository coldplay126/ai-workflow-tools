"""summarize_workflow_state tests including cmux health integration."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from awf.core.workflow_status import summarize_workflow_state


@pytest.fixture(autouse=True)
def _clear_disable_env(monkeypatch):
    monkeypatch.delenv("AWF_WF_STATUS_NO_CMUX", raising=False)


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["cmux-agent", "doctor"], returncode=returncode, stdout=stdout, stderr=stderr,
    )


def _state():
    return {
        "id": "demo",
        "repo": "demo-repo",
        "branch": "main",
        "currentPhase": "plan",
        "phases": {"plan": {"status": "in_progress", "retries": 0}},
        "gates": {"G1": {"passed": None}},
        "history": [],
    }


class TestSummarizeWorkflowState:
    def test_no_cmux_when_repo_root_absent(self):
        text = summarize_workflow_state(_state())
        assert "cmux_broker_health" not in text
        assert "id: demo" in text

    def test_renders_redacted_omp_evidence_with_separate_usage_sources(
        self,
        tmp_path: Path,
    ):
        evidence = {
            "status": "available",
            "workflow": {
                "workflow_id": "workflow-1",
                "phase": "impl",
                "attempt": 2,
            },
            "diagnostics": [],
            "usage": {
                "phase_primary_estimated": {
                    "status": "estimated",
                    "totals": {"input_tokens": 100, "cost_usd": 0.2},
                },
                "omp_worker_reported": {
                    "status": "reported",
                    "reported_worker_count": 1,
                    "unknown_worker_count": 0,
                    "totals": {"input_tokens": 7, "cost_usd": 0.04},
                },
            },
            "dispatches": [
                {
                    "status": "completed",
                    "dispatch_run_id": "omp-run-1",
                    "correlation": {
                        "workflow_id": "workflow-1",
                        "phase": "impl",
                        "attempt": 2,
                    },
                    "checkpoint": {
                        "state": "completed",
                        "resumable": False,
                        "session_persisted": True,
                    },
                    "lineage": {
                        "parent_run_id": "omp-parent",
                        "parent_task_id": "task-parent",
                    },
                    "cancellation": {
                        "requested": True,
                        "acknowledged": True,
                        "final": True,
                        "partial": False,
                        "unresolved": False,
                    },
                    "partial_result": "not_partial",
                    "provenance": {
                        "path": ".workflow/artifacts/dispatch/omp-run-1.json",
                        "sha256": "a" * 64,
                    },
                    "agents": [
                        {
                            "role": "reviewer",
                            "status": "completed",
                            "schema": {"strict_status": "valid"},
                            "timeout": "not_timed_out",
                            "cancellation": {
                                "requested": True,
                                "acknowledged": True,
                                "final": True,
                                "partial": False,
                                "unresolved": False,
                            },
                            "partial_result": "not_partial",
                            "patch_scope_status": "valid",
                            "model": "model-x",
                            "reported_usage": {
                                "source": "omp_worker_reported",
                                "status": "reported",
                                "values": {"input_tokens": 7, "cost_usd": 0.04},
                            },
                            "lineage": {
                                "parent_task_id": "task-parent",
                                "successor_task_id": None,
                            },
                        }
                    ],
                }
            ],
        }
        with (
            patch(
                "awf.core.workflow_status.summarize_omp_evidence",
                return_value=evidence,
            ),
            patch(
                "awf.core.workflow_status.probe_cmux_broker_health",
                return_value={"status": "skipped"},
            ),
        ):
            text = summarize_workflow_state(_state(), repo_root=tmp_path)

        assert "omp_evidence: available" in text
        assert "usage.phase_primary_estimated: estimated" in text
        assert "usage.omp_worker_reported: reported" in text
        assert "run=omp-run-1" in text
        assert "strict_schema_status=valid" in text
        assert "patch_scope=valid" in text
        assert "provenance=.workflow/artifacts/dispatch/omp-run-1.json" in text
        assert "secret prompt" not in text
        assert "worker response" not in text

    def test_includes_cmux_broker_health(self, tmp_path: Path):
        payload = {
            "health": {
                "broker_daemon": {"status": "alive", "pid": 100, "detail": "pid 100 responding"},
                "events_log": {"status": "fresh"},
                "sqlite_integrity": {"status": "ok"},
            }
        }
        with patch(
            "awf.core.cmux_health.subprocess.run",
            return_value=_completed(stdout=json.dumps(payload)),
        ):
            text = summarize_workflow_state(_state(), repo_root=tmp_path)
        assert "cmux_broker_health: alive" in text

    def test_events_log_stale_detail(self, tmp_path: Path):
        payload = {
            "health": {
                "broker_daemon": {"status": "alive", "pid": 100},
                "events_log": {"status": "stale", "detail": "last 600s ago"},
                "sqlite_integrity": {"status": "ok"},
            }
        }
        with patch(
            "awf.core.cmux_health.subprocess.run",
            return_value=_completed(stdout=json.dumps(payload)),
        ):
            text = summarize_workflow_state(_state(), repo_root=tmp_path)
        assert "cmux_broker_health: alive" in text
        assert "events_log: stale" in text
        assert "last 600s ago" in text

    def test_unavailable_when_cli_missing(self, tmp_path: Path):
        with patch("awf.core.cmux_health.subprocess.run", side_effect=FileNotFoundError):
            text = summarize_workflow_state(_state(), repo_root=tmp_path)
        assert "cmux_broker_health: unavailable" in text

    def test_skipped_via_env(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AWF_WF_STATUS_NO_CMUX", "1")
        with patch("awf.core.cmux_health.subprocess.run") as run_mock:
            text = summarize_workflow_state(_state(), repo_root=tmp_path)
        assert "cmux_broker_health: skipped" in text
        run_mock.assert_not_called()
