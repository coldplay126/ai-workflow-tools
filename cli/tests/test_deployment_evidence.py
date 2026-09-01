from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from awf.worktrees.config import ConfigError, DeploymentAdapter, load_deployment_adapter
from awf.worktrees.evidence import (
    PROTOCOL,
    DeploymentEvidenceExecutor,
    DeploymentEvidenceRequest,
    DeploymentEvidenceResponse,
    EvidenceProtocolError,
)
from awf.worktrees.models import DeploymentState, Lease, LeaseState, Purpose
from awf.worktrees.registry import WorktreeRegistry


_REPOSITORY_ID = "f" * 64
_HEAD_SHA = "a" * 40
_MERGE_SHA = "b" * 40


def _adapter_script(tmp_path: Path, body: str) -> Path:
    path = (
        tmp_path
        / "operator-home"
        / ".config"
        / "awf"
        / "adapters"
        / "deployment-adapter"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _operator_config(
    tmp_path: Path,
    *,
    repository_id: str,
    command: tuple[str, ...] | None,
    environment: tuple[str, ...] = (),
) -> Path:
    home = tmp_path / "operator-home"
    path = home / ".config" / "awf" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    adapter = "[worktree.deployment.adapters." + json.dumps(repository_id) + "]\n"
    if command is not None:
        adapter += "command = " + json.dumps(list(command)) + "\n"
    if environment:
        adapter += "environment = " + json.dumps(list(environment)) + "\n"
    path.write_text(adapter, encoding="utf-8")
    path.chmod(0o600)
    return home


def _request() -> DeploymentEvidenceRequest:
    return DeploymentEvidenceRequest.create(
        repository_id=_REPOSITORY_ID,
        pull_request_number=12,
        source_head_sha=_HEAD_SHA,
        subject_revision=_MERGE_SHA,
    )


def _response(request: DeploymentEvidenceRequest, **overrides: object) -> bytes:
    payload: dict[str, object] = {
        "protocol": PROTOCOL,
        "request_id": request.request_id,
        "repository_id": request.repository_id,
        "subject_revision": request.subject_revision,
        "status": "healthy",
        "observed_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    payload.update(overrides)
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _adapter(path: Path) -> DeploymentAdapter:
    details = path.lstat()
    return DeploymentAdapter(
        command=(str(path),),
        environment=(),
        max_age_seconds=300,
        config_digest="c" * 64,
        adapter_directory=path.parent,
        executable_device=details.st_dev,
        executable_inode=details.st_ino,
    )


def test_operator_mapping_is_exact_and_validates_operator_files(tmp_path: Path) -> None:
    executable = _adapter_script(tmp_path, "import sys; sys.stdin.read()")
    home = _operator_config(
        tmp_path, repository_id=_REPOSITORY_ID, command=(str(executable),)
    )

    adapter = load_deployment_adapter(_REPOSITORY_ID, home_dir=home)
    assert adapter is not None
    assert adapter.command == (str(executable),)
    assert load_deployment_adapter("other-repository", home_dir=home) is None



def test_operator_mapping_requires_command_and_rejects_symlink(tmp_path: Path) -> None:
    home = _operator_config(
        tmp_path, repository_id=_REPOSITORY_ID, command=None
    )
    with pytest.raises(ConfigError, match="non-empty argv"):
        load_deployment_adapter(_REPOSITORY_ID, home_dir=home)

    target = tmp_path / "outside-adapter"
    _operator_config(
        tmp_path, repository_id=_REPOSITORY_ID, command=(str(target),)
    )
    with pytest.raises(ConfigError, match="must be under"):
        load_deployment_adapter(_REPOSITORY_ID, home_dir=home)
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o700)
    link = (
        home / ".config" / "awf" / "adapters" / "linked-deployment-adapter"
    )
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    _operator_config(
        tmp_path, repository_id=_REPOSITORY_ID, command=(str(link),)
    )
    with pytest.raises(ConfigError, match="must not be a symlink"):
        load_deployment_adapter(_REPOSITORY_ID, home_dir=home)


def test_operator_mapping_rejects_relative_or_insecure_executables(tmp_path: Path) -> None:
    home = _operator_config(
        tmp_path, repository_id=_REPOSITORY_ID, command=("./repository-status",)
    )

    with pytest.raises(ConfigError, match="absolute executable"):
        load_deployment_adapter(_REPOSITORY_ID, home_dir=home)

    executable = _adapter_script(tmp_path, "pass")
    executable.chmod(0o722)
    home = _operator_config(
        tmp_path, repository_id=_REPOSITORY_ID, command=(str(executable),)
    )

    with pytest.raises(ConfigError, match="group- or world-writable"):
        load_deployment_adapter(_REPOSITORY_ID, home_dir=home)


def test_adapter_revalidates_executable_identity_before_execution(tmp_path: Path) -> None:
    executable = _adapter_script(tmp_path, "pass")
    home = _operator_config(
        tmp_path, repository_id=_REPOSITORY_ID, command=(str(executable),)
    )
    adapter = load_deployment_adapter(_REPOSITORY_ID, home_dir=home)
    executable.chmod(0o700)
    executable.parent.chmod(0o722)
    with pytest.raises(ConfigError, match="deployment adapter directory must not"):
        load_deployment_adapter(_REPOSITORY_ID, home_dir=home)
    executable.parent.chmod(0o755)
    assert adapter is not None

    replacement = tmp_path / "replacement-adapter"
    replacement.write_text(f"#!{sys.executable}\npass\n", encoding="utf-8")
    replacement.chmod(0o700)
    os.replace(replacement, executable)

    with pytest.raises(ConfigError, match="changed"):
        adapter.validate_executable()


@pytest.mark.parametrize(
    "payload",
    [
        b'{"protocol":"awf.deployment-evidence/v1","protocol":"awf.deployment-evidence/v1"}',
        b"not-json",
        b"[]",
        b"{}{}",
        b"\xff",
    ],
)
def test_response_rejects_non_strict_json(payload: bytes) -> None:
    with pytest.raises(EvidenceProtocolError):
        DeploymentEvidenceResponse.parse(payload, _request(), max_age_seconds=300)


def test_response_rejects_replay_mismatch_unknown_fields_and_staleness() -> None:
    request = _request()
    replay = _request()
    with pytest.raises(EvidenceProtocolError, match="request_id"):
        DeploymentEvidenceResponse.parse(
            _response(request), replay, max_age_seconds=300
        )
    with pytest.raises(EvidenceProtocolError, match="repository_id"):
        DeploymentEvidenceResponse.parse(
            _response(request, repository_id="other"), request, max_age_seconds=300
        )
    with pytest.raises(EvidenceProtocolError, match="subject_revision"):
        DeploymentEvidenceResponse.parse(
            _response(request, subject_revision="c" * 40), request, max_age_seconds=300
        )
    with pytest.raises(EvidenceProtocolError, match="unknown field"):
        DeploymentEvidenceResponse.parse(
            _response(request, extra="provider-specific"), request, max_age_seconds=300
        )
    stale = datetime.now(timezone.utc) - timedelta(seconds=301)
    with pytest.raises(EvidenceProtocolError, match="stale"):
        DeploymentEvidenceResponse.parse(
            _response(
                request,
                observed_at=stale.isoformat(timespec="seconds").replace("+00:00", "Z"),
            ),
            request,
            max_age_seconds=300,
        )


def test_request_and_response_reject_invalid_oids_and_timestamps() -> None:
    with pytest.raises(EvidenceProtocolError, match="source_head_sha"):
        DeploymentEvidenceRequest.create(
            repository_id=_REPOSITORY_ID,
            pull_request_number=12,
            source_head_sha="not-an-oid",
            subject_revision=_MERGE_SHA,
        )

    request = _request()
    with pytest.raises(EvidenceProtocolError, match="RFC3339"):
        DeploymentEvidenceResponse.parse(
            _response(request, observed_at="tomorrow"),
            request,
            max_age_seconds=300,
        )
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    with pytest.raises(EvidenceProtocolError, match="future"):
        DeploymentEvidenceResponse.parse(
            _response(
                request,
                observed_at=future.isoformat(timespec="seconds").replace("+00:00", "Z"),
            ),
            request,
            max_age_seconds=300,
        )


@pytest.mark.parametrize(
    ("body", "limit", "timeout_seconds", "failure_code"),
    [
        ("import sys; sys.stdin.read(); raise SystemExit(7)", 1024, 1.0, "deployment_adapter_nonzero"),
        ("import sys, time; sys.stdin.read(); time.sleep(10)", 1024, 0.2, "deployment_adapter_timeout"),
        ("import sys; sys.stdin.read(); sys.stdout.write('x' * 4096)", 64, 1.0, "deployment_adapter_output_overflow"),
    ],
)
def test_executor_fails_closed_for_process_errors(
    tmp_path: Path, body: str, limit: int, timeout_seconds: float, failure_code: str
) -> None:
    executable = _adapter_script(tmp_path, body)
    result = DeploymentEvidenceExecutor(
        neutral_cwd=tmp_path,
        stdout_limit=limit,
        stderr_limit=limit,
        timeout_seconds=timeout_seconds,
    ).execute(_adapter(executable), _request())
    assert result.response is None
    assert result.failure_code == failure_code



def test_executor_uses_minimal_explicit_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/untrusted/path")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/pythonpath")
    monkeypatch.setenv("AWF_TEST_ALLOWED", "present")
    executable = _adapter_script(
        tmp_path,
        """
import json
import os
import sys
request = json.load(sys.stdin)
status = "healthy" if (
    os.environ.get("AWF_TEST_ALLOWED") == "present"
    and "PATH" not in os.environ
    and "PYTHONPATH" not in os.environ
) else "failed"
print(json.dumps({
    "protocol": request["protocol"],
    "request_id": request["request_id"],
    "repository_id": request["repository_id"],
    "subject_revision": request["subject_revision"],
    "status": status,
    "observed_at": request["requested_at"],
}))
""",
    )
    home = _operator_config(
        tmp_path,
        repository_id=_REPOSITORY_ID,
        command=(str(executable),),
        environment=("AWF_TEST_ALLOWED",),
    )
    adapter = load_deployment_adapter(_REPOSITORY_ID, home_dir=home)
    assert adapter is not None
    assert adapter.environment == ("AWF_TEST_ALLOWED",)

    result = DeploymentEvidenceExecutor(neutral_cwd=tmp_path).execute(
        adapter, _request()
    )

    assert result.status == "healthy"


def test_executor_records_the_response_receipt_time(tmp_path: Path) -> None:
    executable = _adapter_script(
        tmp_path,
        """
import json
import sys
request = json.load(sys.stdin)
print(json.dumps({
    "protocol": request["protocol"],
    "request_id": request["request_id"],
    "repository_id": request["repository_id"],
    "subject_revision": request["subject_revision"],
    "status": "healthy",
    "observed_at": request["requested_at"],
}))
""",
    )
    issued_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    received_at = issued_at + timedelta(milliseconds=500)
    values = iter((issued_at, received_at))
    request = DeploymentEvidenceRequest.create(
        repository_id=_REPOSITORY_ID,
        pull_request_number=12,
        source_head_sha=_HEAD_SHA,
        subject_revision=_MERGE_SHA,
        now=issued_at,
    )

    result = DeploymentEvidenceExecutor(
        neutral_cwd=tmp_path, now=lambda: next(values)
    ).execute(_adapter(executable), request)

    assert result.status == "healthy"
    assert result.received_at == received_at


def test_executor_times_out_when_child_keeps_pipe_open(tmp_path: Path) -> None:
    executable = _adapter_script(
        tmp_path,
        """
import os
import sys
import time
sys.stdin.read()
if os.fork() == 0:
    time.sleep(60)
    os._exit(0)
""",
    )

    started = time.monotonic()
    result = DeploymentEvidenceExecutor(
        neutral_cwd=tmp_path, timeout_seconds=0.2
    ).execute(_adapter(executable), _request())

    assert result.response is None
    assert result.failure_code == "deployment_adapter_timeout"
    assert time.monotonic() - started < 3


def test_registry_round_trips_allowlisted_deployment_evidence(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "state.sqlite3")
    lease = registry.create_lease(
        Lease.new(
            repository_id=_REPOSITORY_ID,
            repository_name="repository",
            repository_root=tmp_path,
            worktree_path=tmp_path,
            initiative="evidence",
            purpose=Purpose.PROMOTE,
            branch="awf/evidence/promote",
            base_ref="main",
            head_sha=_HEAD_SHA,
            managed=True,
            owner_kind="awf",
        )
    )
    observed_at = datetime.now(timezone.utc) - timedelta(seconds=2)
    received_at = observed_at + timedelta(seconds=1)
    evidence = {
        "protocol": PROTOCOL,
        "subject_revision": _MERGE_SHA,
        "status": "healthy",
        "observed_at": observed_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "received_at": received_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "adapter_digest": "c" * 64,
        "response_digest": "d" * 64,
        "evidence_id": "deployment-42",
        "diagnostic_code": "healthy",
    }

    registry.transition(
        lease.id,
        LeaseState.CLEANABLE,
        expected_version=lease.version,
        deployment_state=DeploymentState.HEALTHY,
        evidence=evidence,
    )

    events = registry.list_events(lease.id)
    assert events[-1].evidence == evidence

    with sqlite3.connect(registry.db_path) as connection:
        connection.execute(
            "UPDATE worktree_events SET evidence = ? WHERE id = ?",
            ("{not-json", events[-1].id),
        )
    assert registry.list_events(lease.id)[-1].evidence is None

    forward = dict(evidence)
    forward["received_at"] = (
        datetime.now(timezone.utc) + timedelta(hours=1)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    with sqlite3.connect(registry.db_path) as connection:
        connection.execute(
            "UPDATE worktree_events SET evidence = ? WHERE id = ?",
            (json.dumps(forward), events[-1].id),
        )
    assert registry.list_events(lease.id)[-1].evidence is None
