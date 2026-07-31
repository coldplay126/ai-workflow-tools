"""Process-level contracts for the local native-OMP Supervisor agent."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

_FIXTURES = Path(__file__).with_name("fixtures")
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))

from fake_supervisor_server import FakeSupervisorServer
from awf.supervisor.credentials import (
    MacOSKeychainCredentialStore,
    RefreshTokenNotFound,
)


pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the local agent process uses the macOS Keychain credential boundary",
)

_AGENT_PREFIX = "local-e2e"
_REFRESH_TOKEN = "fixture-refresh-token"
_ACCESS_TOKEN = "fixture-access-token"
_NATIVE_SESSION_ID = "fixture-native-session"
_NATIVE_TASK_ID = "01SUPERVISOR"
_PROMPT_SECRET = "prompt-secret-must-not-be-uploaded"
_OMP_ECHO_SECRET = "native-omp-echo-must-not-be-uploaded"


@contextmanager
def _keychain_refresh_token(agent_id: str) -> Iterator[None]:
    """Install one isolated real Keychain credential for the child CLI process."""

    store = MacOSKeychainCredentialStore()
    try:
        try:
            store.delete_refresh_token(agent_id)
        except RefreshTokenNotFound:
            pass
        store.save_refresh_token(agent_id, _REFRESH_TOKEN)
        yield
    finally:
        try:
            store.delete_refresh_token(agent_id)
        except RefreshTokenNotFound:
            pass


def _wait_for(predicate: Callable[[], bool], *, description: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting for {}".format(description))

def _wait_for_running_process(
    process: subprocess.Popen[str],
    predicate: Callable[[], bool],
    *,
    description: str,
    diagnostic: Optional[Callable[[], str]] = None,
    timeout: float = 15.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                "agent exited {} before {}:\nstdout={!r}\nstderr={!r}".format(
                    process.returncode, description, stdout, stderr
                )
            )
        time.sleep(0.05)
    detail = "" if diagnostic is None else "\n{}".format(diagnostic())
    process.kill()
    stdout, stderr = process.communicate()
    raise AssertionError(
        "timed out waiting for {}{}\nstdout={!r}\nstderr={!r}".format(
            description, detail, stdout, stderr
        )
    )

def _server_diagnostic(server: FakeSupervisorServer) -> str:
    events = [
        (
            attempt["type"],
            attempt.get("data", {}).get("status_code"),
            attempt["accepted"],
        )
        for attempt in server.event_attempts
    ]
    return "job={!r} polls={} requests={!r} events={!r} protocol_errors={!r}".format(
        server.job_state,
        server.command_poll_count,
        [(request["method"], request["path"]) for request in server.requests],
        events,
        server.protocol_errors,
    )


def _job(*, state: str = "QUEUED") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "job_id": "job-e2e-1",
        "workflow_id": "workflow-e2e-1",
        "state": state,
        "desired_state": "RUNNING",
        "approval_required": True,
        "requested_target": "local",
        "owner_agent_id": None,
        "lease_expires_at": None,
        "generation": 1,
        "attempt": 0,
        "repo_refs": [{"repo": "api", "base": "main"}],
        "required_capabilities": ["git", "omp"],
        "checkpoint": None,
        "created_at": "2026-07-31T12:00:00Z",
        "updated_at": "2026-07-31T12:00:00Z",
    }


def _command() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "command_id": "4a2c1e31-cf45-4fe4-ae47-f40d3eb90837",
        "job_id": "job-e2e-1",
        "generation": 1,
        "type": "EXECUTE",
    }


def _run_git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )


def _make_canonical_repository(tmp_path: Path) -> Path:
    """Create a clean clone whose origin/main can back a real local worktree."""

    remote = tmp_path / "api-origin.git"
    seed = tmp_path / "api-seed"
    repositories = tmp_path / "repositories"
    clone = repositories / "api"
    _run_git("init", "--bare", str(remote))
    _run_git("init", "--initial-branch=main", str(seed))
    _run_git("config", "user.email", "e2e@example.test", cwd=seed)
    _run_git("config", "user.name", "E2E Fixture", cwd=seed)
    (seed / "README.txt").write_text("fixture\n", encoding="utf-8")
    _run_git("add", "README.txt", cwd=seed)
    _run_git("commit", "-m", "fixture", cwd=seed)
    _run_git("remote", "add", "origin", str(remote), cwd=seed)
    _run_git("push", "-u", "origin", "main", cwd=seed)
    repositories.mkdir()
    _run_git("clone", str(remote), str(clone))
    _run_git("checkout", "main", cwd=clone)
    return repositories


def _outbox_sequences(path: Path) -> list[int]:
    if not path.exists():
        return []
    connection = sqlite3.connect(path)
    try:
        return [
            int(row[0])
            for row in connection.execute(
                "SELECT sequence FROM event_outbox ORDER BY sequence"
            )
        ]
    finally:
        connection.close()


def _pid_is_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _agent_id() -> str:
    return "{}-{}".format(_AGENT_PREFIX, uuid.uuid4().hex)


def _launch_agent(
    *,
    tmp_path: Path,
    server: FakeSupervisorServer,
    agent_id: str,
    native_mode: str,
    session_path: Path,
    parent_pid_path: Path | None = None,
    child_pid_path: Path | None = None,
) -> subprocess.Popen[str]:
    cli_root = Path(__file__).parents[1]
    fixture_omp = _FIXTURES / "fake_omp_supervised.py"
    fixture_omp.chmod(0o755)
    launch_root = tmp_path / "launch-root"
    launch_root.mkdir()
    (launch_root / ".awf.toml").write_text("", encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    environment = os.environ.copy()
    environment.update(
        {
            "AWF_SUPERVISOR_API_URL": server.api_url,
            "AWF_SUPERVISOR_REGION": "ap-northeast-2",
            "AWF_OMP_COMMAND": str(fixture_omp),
            "AWF_OMP_NO_SESSION": "0",
            "AWF_OMP_TIMEOUT_SEC": "60",
            "AWF_OMP_TERMINATION_GRACE_SEC": "0.2",
            "AWF_FAKE_OMP_MODE": native_mode,
            "AWF_FAKE_OMP_SESSION_PATH": str(session_path),
            "AWF_FAKE_OMP_SECRET": _OMP_ECHO_SECRET,
            "SSL_CERT_FILE": str(server.certificate_path),
            "HTTPS_PROXY": server.proxy_url,
            "https_proxy": server.proxy_url,
            "NO_PROXY": "",
            "no_proxy": "",
            "PYTHONPATH": os.pathsep.join(
                value for value in (str(cli_root / "src"), environment.get("PYTHONPATH")) if value
            ),
        }
    )
    for variable in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        environment.pop(variable, None)
    if parent_pid_path is not None:
        environment["AWF_FAKE_OMP_PARENT_PID_PATH"] = str(parent_pid_path)
    if child_pid_path is not None:
        environment["AWF_FAKE_OMP_CHILD_PID_PATH"] = str(child_pid_path)

    server.observe_outbox(state_dir / "supervisor.db")
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "awf.cli",
            "supervisor",
            "agent",
            "run",
            "--agent-id",
            agent_id,
            "--environment",
            "local",
            "--transport",
            "http",
            "--state-dir",
            str(state_dir),
            "--active-lease-path",
            str(state_dir / "active-lease.json"),
            "--repo-root",
            str(tmp_path / "repositories"),
        ],
        cwd=launch_root,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _finish_after_idle_signal(process: subprocess.Popen[str]) -> tuple[int, str, str]:
    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=15)
    assert process.returncode is not None
    return process.returncode, stdout, stderr


def _metadata_allowlist() -> set[str]:
    return {
        "schema_version",
        "kind",
        "job_id",
        "generation",
        "terminal_state",
        "returncode",
        "timed_out",
        "termination_reason",
        "result_summary",
        "checkpoint",
        "omp_provenance",
    }


def test_local_agent_process_runs_one_native_job_and_cleans_up_after_idle_sigterm(
    tmp_path: Path,
) -> None:
    repositories = _make_canonical_repository(tmp_path)
    assert repositories == tmp_path / "repositories"
    agent_id = _agent_id()
    session_path = tmp_path / "native-session.txt"
    prompt = "Implement a private request: {}".format(_PROMPT_SECRET)

    with _keychain_refresh_token(agent_id), FakeSupervisorServer(
        command=_command(),
        job=_job(),
        prompt=prompt,
        access_token=_ACCESS_TOKEN,
    ) as server:
        process = _launch_agent(
            tmp_path=tmp_path,
            server=server,
            agent_id=agent_id,
            native_mode="complete",
            session_path=session_path,
        )
        try:
            _wait_for_running_process(
                process,
                lambda: server.job_state == "SUCCEEDED" and not _outbox_sequences(server.outbox_path),
                description="native job completion and durable outbox acknowledgement",
                diagnostic=lambda: _server_diagnostic(server),
            )
            _wait_for_running_process(
                process,
                lambda: server.command_poll_count >= 2,
                description="an idle command poll after the completed job",
                diagnostic=lambda: _server_diagnostic(server),
            )
            polls_before_shutdown = server.command_poll_count
            exit_code, stdout, stderr = _finish_after_idle_signal(process)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    assert exit_code == 0, (stdout, stderr)
    assert session_path.read_text(encoding="utf-8") == _NATIVE_SESSION_ID
    assert server.heartbeat_requests
    assert server.claim_attempts == ["4a2c1e31-cf45-4fe4-ae47-f40d3eb90837"]
    assert server.preclaim_headers == [
        {
            "x-awf-command-id": "4a2c1e31-cf45-4fe4-ae47-f40d3eb90837",
            "x-awf-generation": "1",
        }
    ]
    assert server.command_delivery_count == 1
    assert polls_before_shutdown <= server.command_poll_count <= polls_before_shutdown + 1
    assert all(value == "Bearer {}".format(_ACCESS_TOKEN) for value in server.bearer_headers)
    assert server.bearer_headers
    assert server.protocol_errors == []

    accepted_unique = server.accepted_events_by_sequence()
    assert [event["sequence"] for event in accepted_unique] == [1, 2, 3, 4, 5]
    assert [event["data"].get("status_code") or event["data"].get("terminal_status") for event in accepted_unique] == [
        "PREPARING",
        "RUNNING",
        "WAITING_APPROVAL",
        "RUNNING",
        "SUCCEEDED",
    ]
    assert [event["sequence"] for event in server.event_attempts] == sorted(
        event["sequence"] for event in server.event_attempts
    )
    assert all(server.outbox_observed_before_acceptance)
    assert _outbox_sequences(server.outbox_path) == []

    assert server.prompt_checksums == [hashlib.sha256(prompt.encode("utf-8")).hexdigest()]
    assert {artifact["kind"] for artifact in server.artifacts} == {"provenance", "redacted-result"}
    for artifact in server.artifacts:
        metadata = artifact["json"]
        assert set(metadata) == _metadata_allowlist()
        assert set(metadata["result_summary"]) == {"status", "redacted"}
        assert metadata["result_summary"]["redacted"] is True
        assert set(metadata["checkpoint"]) == {
            "kind",
            "sha256",
            "state",
            "batch_fingerprint",
            "coordinator_session_id",
        }
        assert metadata["checkpoint"]["kind"] == "omp_native_batch"
        assert metadata["checkpoint"]["state"] == "completed"
        assert metadata["checkpoint"]["coordinator_session_id"] == _NATIVE_SESSION_ID
        assert metadata["omp_provenance"]["coordination_surface"] == "native"
        assert metadata["omp_provenance"]["task_id"] == _NATIVE_TASK_ID
        assert metadata["omp_provenance"]["agent_uri"] == "agent://{}".format(_NATIVE_TASK_ID)
        assert metadata["omp_provenance"]["history_uri"] == "history://{}".format(_NATIVE_TASK_ID)
        serialized = json.dumps(metadata, sort_keys=True)
        assert _PROMPT_SECRET not in serialized
        assert _OMP_ECHO_SECRET not in serialized


@pytest.mark.parametrize(
    "accepts_event",
    [pytest.param(True, id="flush-accepted"), pytest.param(False, id="flush-withheld")],
)
def test_local_agent_sigterm_kills_native_process_group_and_preserves_flush_semantics(
    tmp_path: Path,
    accepts_event: bool,
) -> None:
    _make_canonical_repository(tmp_path)
    agent_id = _agent_id()
    session_path = tmp_path / "native-session.txt"
    parent_pid_path = tmp_path / "native-parent.pid"
    child_pid_path = tmp_path / "native-child.pid"

    with _keychain_refresh_token(agent_id), FakeSupervisorServer(
        command=_command(),
        job=_job(),
        prompt="private active request",
        access_token=_ACCESS_TOKEN,
        accept_append_events=accepts_event,
        append_outcomes=None if accepts_event else (True, True, True, True, False),
    ) as server:
        process = _launch_agent(
            tmp_path=tmp_path,
            server=server,
            agent_id=agent_id,
            native_mode="block",
            session_path=session_path,
            parent_pid_path=parent_pid_path,
            child_pid_path=child_pid_path,
        )
        try:
            _wait_for_running_process(
                process,
                lambda: parent_pid_path.exists() and child_pid_path.exists(),
                description="the fake native parent and child processes",
                diagnostic=lambda: _server_diagnostic(server),
            )
            parent_pid = int(parent_pid_path.read_text(encoding="utf-8"))
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            polls_before_signal = server.command_poll_count
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=20)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    assert process.returncode == (0 if accepts_event else 1), (stdout, stderr)
    _wait_for(lambda: _pid_is_gone(parent_pid), description="terminated native parent")
    assert server.bearer_headers
    assert server.protocol_errors == []
    _wait_for(lambda: _pid_is_gone(child_pid), description="terminated native child")
    assert server.command_poll_count == polls_before_signal
    assert all(value == "Bearer {}".format(_ACCESS_TOKEN) for value in server.bearer_headers)
    assert server.event_validation_errors == []
    assert not any(
        event["data"].get("terminal_status") == "FAILED" for event in server.event_attempts
    )

    paused = [
        event
        for event in server.event_attempts
        if event["data"].get("status_code") == "PAUSED"
    ]
    assert paused
    assert paused[-1]["type"] == "ARTIFACT_UPDATED"
    assert server.artifacts_for_kind("checkpoint")
    assert session_path.read_text(encoding="utf-8") == _NATIVE_SESSION_ID
    assert server.job_state == ("PAUSED" if accepts_event else "RUNNING")

    if accepts_event:
        assert all(server.outbox_observed_before_acceptance)
        assert _outbox_sequences(server.outbox_path) == []
        assert server.active_lease_marker_exists is True
    else:
        assert server.outbox_observed_before_acceptance[-1] is True
        assert _outbox_sequences(server.outbox_path) == [5]
        assert server.active_lease_marker_exists is True


