"""Behavioral contracts for Supervisor agent CLI entrypoints."""

from __future__ import annotations

import json
import sqlite3
import plistlib
from pathlib import Path
from typing import Any
from typing import Callable, Sequence

import pytest

from awf.cli import build_parser, main
from awf.supervisor.contracts import AgentEnvironment
from awf.supervisor.runtime_paths import RuntimePaths
from awf.supervisor.store import SupervisorStore
from awf.supervisor.install import (
    LABEL,
    LaunchdInstallError,
    install_launchd,
    resolve_awf_executable,
    uninstall_launchd,
)


AGENT_ID = "local-mac-01"
REFRESH_TOKEN = "enrollment-secret-must-never-be-printed"


def _paths(tmp_path: Path) -> RuntimePaths:
    root = tmp_path / "state"
    root.mkdir()
    repos = tmp_path / "repos"
    repos.mkdir()
    return RuntimePaths(
        state_root=root,
        store_path=root / "supervisor.db",
        active_lease_path=root / "active-lease.json",
        repo_root=repos,
    )


def _run_argv(*extra: str) -> list[str]:
    return [
        "supervisor",
        "agent",
        "run",
        "--agent-id",
        AGENT_ID,
        "--environment",
        "local",
        "--transport",
        "http",
        *extra,
    ]


def test_agent_parser_exposes_only_the_task7_surface() -> None:
    parser = build_parser()

    agent = parser.parse_args(_run_argv())
    doctor = parser.parse_args(
        [
            "supervisor",
            "agent",
            "doctor",
            "--agent-id",
            AGENT_ID,
            "--environment",
            "aws",
            "--json",
        ]
    )
    idle = parser.parse_args(
        ["supervisor", "agent", "idle-status", "--environment", "aws", "--json"]
    )
    enroll = parser.parse_args(
        ["supervisor", "agent", "enroll", "--agent-id", AGENT_ID, "--json"]
    )
    install = parser.parse_args(
        ["supervisor", "agent", "install-launchd", "--agent-id", AGENT_ID]
    )
    uninstall = parser.parse_args(
        ["supervisor", "agent", "uninstall-launchd", "--agent-id", AGENT_ID]
    )

    assert agent.environment == "local"
    assert agent.transport == "http"
    assert doctor.environment == "aws"
    assert doctor.json is True
    assert idle.environment == "aws"
    assert idle.json is True
    assert enroll.json is True
    assert install.agent_id == AGENT_ID
    assert uninstall.agent_id == AGENT_ID

    with pytest.raises(SystemExit) as rejected:
        parser.parse_args(["supervisor", "agent", "run", "--agent-id", AGENT_ID])
    assert rejected.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        [
            "supervisor",
            "agent",
            "run",
            "--agent-id",
            AGENT_ID,
            "--environment",
            "local",
            "--transport",
            "sqs",
        ],
        [
            "supervisor",
            "agent",
            "run",
            "--agent-id",
            AGENT_ID,
            "--environment",
            "aws",
            "--transport",
            "http",
        ],
        [
            "supervisor",
            "agent",
            "run",
            "--agent-id",
            "bad/id",
            "--environment",
            "local",
            "--transport",
            "http",
        ],
    ],
)
def test_run_rejects_invalid_agent_ids_and_environment_transport_pairs_before_wiring(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    import awf.commands.supervisor_agent as command

    called: list[Any] = []
    monkeypatch.setattr(command, "resolve_runtime_paths", lambda **kwargs: called.append(kwargs))

    assert main(argv) == 2
    assert called == []


def test_run_resolves_paths_once_and_composes_the_real_runtime_from_one_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import awf.commands.supervisor_agent as command

    paths = _paths(tmp_path)
    calls: dict[str, Any] = {}

    class Store:
        def __init__(self, path: Path) -> None:
            calls["store"] = path

    class Workspace:
        def __init__(self, *, github_root: Path, state_root: Path) -> None:
            calls["workspace"] = (github_root, state_root)

    class Executor:
        def __init__(self, **kwargs: Any) -> None:
            calls["executor"] = kwargs

    class Runtime:
        def __init__(self, **kwargs: Any) -> None:
            calls["runtime"] = kwargs

        def run(self) -> int:
            return 17

    class Http:
        def __init__(self, **kwargs: Any) -> None:
            calls["http"] = kwargs

    class Broker:
        def __init__(self, refresh_tokens: Any, transport: Any) -> None:
            calls["broker"] = (refresh_tokens, transport)

    class Bearer:
        def __init__(self, **kwargs: Any) -> None:
            calls["bearer"] = kwargs

    class Source:
        def __init__(self, **kwargs: Any) -> None:
            calls["source"] = kwargs

    class LeaseApi:
        def __init__(self, **kwargs: Any) -> None:
            calls["lease"] = kwargs

    def resolve(**kwargs: Any) -> RuntimePaths:
        calls["paths"] = kwargs
        return paths

    monkeypatch.setattr(command, "resolve_runtime_paths", resolve)
    monkeypatch.setattr(command, "SupervisorStore", Store)
    monkeypatch.setattr(command, "LocalGitWorkspaceAdapter", Workspace)
    monkeypatch.setattr(command, "SupervisorJobExecutor", Executor)
    monkeypatch.setattr(command, "SupervisorAgentRuntime", Runtime)
    monkeypatch.setattr(command, "_UrlHttpTransport", Http)
    monkeypatch.setattr(command, "AccessTokenBroker", Broker)
    monkeypatch.setattr(command, "BrokerBearerTransport", Bearer)
    monkeypatch.setattr(command, "LocalHttpCommandSource", Source)
    monkeypatch.setattr(command, "HttpLeaseApi", LeaseApi)
    monkeypatch.setattr(command, "MacOSKeychainCredentialStore", lambda: "keychain")
    monkeypatch.setattr(command, "load_supervisor_config", lambda: "config")

    assert main(_run_argv("--state-dir", str(paths.state_root), "--repo-root", str(paths.repo_root))) == 17
    assert calls["paths"] == {
        "environment": "local",
        "state_dir": paths.state_root,
        "active_lease_path": None,
        "repo_root": paths.repo_root,
    }
    assert calls["store"] == paths.store_path
    assert calls["workspace"] == (paths.repo_root, paths.state_root)
    assert calls["runtime"]["paths"] is paths
    assert calls["runtime"]["environment"] is AgentEnvironment.LOCAL
    assert calls["runtime"]["agent_id"] == AGENT_ID
    assert calls["runtime"]["store"] is calls["executor"]["store"]
    assert calls["runtime"]["workspace"] is calls["executor"]["workspace"]


def test_run_selects_only_the_aws_sqs_and_agentctl_dependencies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import awf.commands.supervisor_agent as command

    paths = _paths(tmp_path)
    calls: dict[str, Any] = {}

    class Runtime:
        def __init__(self, **kwargs: Any) -> None:
            calls["runtime"] = kwargs

        def run(self) -> int:
            return 19

    class Executor:
        def __init__(self, **kwargs: Any) -> None:
            calls["executor"] = kwargs

    class Workspace:
        def __init__(self, *, repo_root: Path, workspace_root: Path) -> None:
            calls["workspace"] = (repo_root, workspace_root)

    class Store:
        def __init__(self, path: Path) -> None:
            calls["store"] = path

    class Signed:
        def __init__(self, config: Any) -> None:
            calls["signed"] = config

    class LeaseApi:
        def __init__(self, **kwargs: Any) -> None:
            calls["lease"] = kwargs

    monkeypatch.setattr(command, "resolve_runtime_paths", lambda **kwargs: paths)
    monkeypatch.setattr(command, "SupervisorStore", Store)
    monkeypatch.setattr(command, "AgentctlWorkspaceAdapter", Workspace)
    monkeypatch.setattr(command, "SigV4Transport", Signed)
    monkeypatch.setattr(command, "HttpLeaseApi", LeaseApi)
    monkeypatch.setattr(command, "SupervisorJobExecutor", Executor)
    monkeypatch.setattr(command, "SupervisorAgentRuntime", Runtime)
    monkeypatch.setattr(command, "_aws_command_source", lambda config: "sqs-source")
    monkeypatch.setattr(command, "load_supervisor_config", lambda: "config")

    assert main(
        [
            "supervisor",
            "agent",
            "run",
            "--agent-id",
            AGENT_ID,
            "--environment",
            "aws",
            "--transport",
            "sqs",
        ]
    ) == 19
    assert calls["signed"] == "config"
    assert calls["runtime"]["environment"] is AgentEnvironment.AWS
    assert calls["runtime"]["source"] == "sqs-source"
    assert calls["workspace"] == (paths.repo_root, paths.repo_root.parent)
    assert isinstance(calls["runtime"]["workspace"], Workspace)


def test_doctor_uses_resolved_paths_and_emits_a_redacted_schema_versioned_json_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import awf.commands.supervisor_agent as command

    paths = _paths(tmp_path)
    monkeypatch.setattr(command, "resolve_runtime_paths", lambda **kwargs: paths)
    monkeypatch.setattr(command, "_doctor_checks", lambda **kwargs: {"runtime": "ready"})

    assert main(
        [
            "supervisor",
            "agent",
            "doctor",
            "--agent-id",
            AGENT_ID,
            "--environment",
            "local",
            "--json",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "schema_version": 1,
        "agent_id": AGENT_ID,
        "environment": "local",
        "status": "ok",
        "checks": {"runtime": "ready"},
    }
    assert REFRESH_TOKEN not in json.dumps(payload)


def test_doctor_fails_closed_for_unreadable_or_escaping_repository_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import awf.commands.supervisor_agent as command
    from awf.supervisor.runtime_paths import RuntimePathError

    monkeypatch.setattr(
        command,
        "resolve_runtime_paths",
        lambda **kwargs: (_ for _ in ()).throw(RuntimePathError("repository root cannot be resolved")),
    )

    assert main(
        [
            "supervisor",
            "agent",
            "doctor",
            "--agent-id",
            AGENT_ID,
            "--environment",
            "local",
            "--json",
        ]
    ) == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["status"] == "unknown"
    assert "token" not in json.dumps(payload).lower()


def test_enrollment_uses_iam_endpoint_persists_only_in_keychain_and_never_prints_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import awf.commands.supervisor_agent as command
    from awf.supervisor.client import HttpResponse

    class Transport:
        def __init__(self, config: Any) -> None:
            assert config == "config"

        def request(self, method: str, path: str, *, payload: Any) -> HttpResponse:
            assert (method, path, payload) == (
                "POST",
                "/v1/admin/agents/enroll",
                {"agent_id": AGENT_ID},
            )
            return HttpResponse(
                status=201,
                headers={},
                body=json.dumps({"agent_id": AGENT_ID, "refresh_token": REFRESH_TOKEN}).encode(),
            )

    class Keychain:
        saved: list[tuple[str, str]] = []

        def save_refresh_token(self, agent_id: str, token: str) -> None:
            self.saved.append((agent_id, token))

    keychain = Keychain()
    monkeypatch.setattr(command, "load_supervisor_config", lambda: "config")
    monkeypatch.setattr(command, "SigV4Transport", Transport)
    monkeypatch.setattr(command, "MacOSKeychainCredentialStore", lambda: keychain)

    assert main(["supervisor", "agent", "enroll", "--agent-id", AGENT_ID, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert keychain.saved == [(AGENT_ID, REFRESH_TOKEN)]
    assert payload == {"schema_version": 1, "agent_id": AGENT_ID, "status": "enrolled"}
    assert REFRESH_TOKEN not in json.dumps(payload)


@pytest.mark.parametrize(
    ("kind", "expected_exit", "expected_status"),
    [
        ("safe", 0, "safe"),
        ("pending", 3, "busy"),
        ("marker", 3, "busy"),
        ("malformed-marker", 4, "unknown"),
        ("corrupt", 4, "unknown"),
        ("missing", 4, "unknown"),
        ("missing-schema", 4, "unknown"),
        ("locked", 4, "unknown"),
    ],
)
def test_idle_status_uses_the_real_store_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    expected_exit: int,
    expected_status: str,
) -> None:
    import awf.commands.supervisor_agent as command

    paths = _paths(tmp_path)
    if kind == "missing-schema":
        sqlite3.connect(paths.store_path).close()
    elif kind != "missing":
        store = SupervisorStore(paths.store_path)
        if kind == "pending":
            store.enqueue_event(
                __import__("awf.supervisor.contracts", fromlist=["SupervisorEvent"]).SupervisorEvent.from_dict(
                    {
                        "schema_version": 1,
                        "type": "TASK_STARTED",
                        "job_id": "job-1",
                        "generation": 1,
                        "sequence": 1,
                        "timestamp": "2026-07-30T12:00:00Z",
                        "source": "agent",
                        "data": {},
                    }
                )
            )
        elif kind == "marker":
            paths.active_lease_path.write_text(
                json.dumps(
                    {
                        "job_id": "job-1",
                        "generation": 1,
                        "agent_id": AGENT_ID,
                        "acquired_at": "2026-07-30T12:00:00Z",
                        "lease_expires_at": "2026-07-30T12:05:00Z",
                    }
                ),
                encoding="utf-8",
            )
        elif kind == "malformed-marker":
            paths.active_lease_path.write_text("{", encoding="utf-8")
        elif kind == "corrupt":
            paths.store_path.write_text("not a sqlite database", encoding="utf-8")
        elif kind == "locked":
            connection = sqlite3.connect(paths.store_path, timeout=0.0)
            connection.execute("PRAGMA locking_mode=EXCLUSIVE")
            connection.execute("BEGIN EXCLUSIVE")
    monkeypatch.setattr(command, "resolve_runtime_paths", lambda **kwargs: paths)
    try:
        assert main(["supervisor", "agent", "idle-status", "--environment", "local", "--json"]) == expected_exit
    finally:
        if kind == "locked":
            connection.rollback()
            connection.close()
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["status"] == expected_status
    assert set(payload) == {"schema_version", "environment", "status"}


def _external_awf(tmp_path: Path, name: str = "awf") -> Path:
    executable = tmp_path / name
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def _launchctl_runner(
    calls: list[tuple[str, ...]], outcomes: Sequence[int] = ()
) -> Callable[[Sequence[str]], int]:
    result_codes = iter(outcomes)

    def run(arguments: Sequence[str]) -> int:
        calls.append(tuple(arguments))
        return next(result_codes, 0)

    return run


def test_install_requires_an_external_executable_and_rejects_worktree_shims(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    external = _external_awf(tmp_path)
    monkeypatch.setattr("awf.supervisor.install.shutil.which", lambda name: str(external))
    assert resolve_awf_executable(development_root=tmp_path / "other") == external.resolve()

    monkeypatch.setattr("awf.supervisor.install.shutil.which", lambda name: None)
    with pytest.raises(LaunchdInstallError, match="awf executable"):
        resolve_awf_executable(development_root=tmp_path / "other")

    non_executable = tmp_path / "not-executable"
    non_executable.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        "awf.supervisor.install.shutil.which", lambda name: str(non_executable)
    )
    with pytest.raises(LaunchdInstallError, match="executable"):
        resolve_awf_executable(development_root=tmp_path / "other")

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    shim = _external_awf(worktree)
    monkeypatch.setattr("awf.supervisor.install.shutil.which", lambda name: str(shim))
    with pytest.raises(LaunchdInstallError, match="repository"):
        resolve_awf_executable(development_root=worktree)


def test_install_renders_token_free_absolute_argv_and_orders_launchctl_operations(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo_root = tmp_path / "repos"
    repo_root.mkdir()
    executable = _external_awf(tmp_path)
    calls: list[tuple[str, ...]] = []

    plist_path = install_launchd(
        agent_id=AGENT_ID,
        repo_root=repo_root,
        executable=executable,
        home=home,
        uid=501,
        run_command=_launchctl_runner(calls, outcomes=(3, 0, 0)),
        development_root=tmp_path / "external",
    )

    payload = plistlib.loads(plist_path.read_bytes())
    assert payload["Label"] == LABEL
    assert payload["ProgramArguments"] == [
        str(executable.resolve()),
        "supervisor",
        "agent",
        "run",
        "--agent-id",
        AGENT_ID,
        "--environment",
        "local",
        "--transport",
        "http",
        "--repo-root",
        str(repo_root.resolve()),
    ]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["ThrottleInterval"] == 10
    assert all(Path(payload["ProgramArguments"][index]).is_absolute() for index in (0, 11))
    non_path_arguments = [
        value
        for value in payload["ProgramArguments"]
        if value not in (str(executable.resolve()), str(repo_root.resolve()))
    ]
    sensitive = ("token", "credential", "secret")
    assert all(
        not any(term in value.lower() for term in sensitive)
        for value in non_path_arguments
    )
    environment = payload.get("EnvironmentVariables", {})
    assert all(
        not any(term in str(value).lower() for term in sensitive)
        for pair in environment.items()
        for value in pair
    )
    assert calls == [
        ("launchctl", "bootout", "gui/501/" + LABEL),
        ("launchctl", "bootstrap", "gui/501", str(plist_path)),
        ("launchctl", "print", "gui/501/" + LABEL),
    ]


def test_install_is_idempotent_and_rolls_back_a_failed_replacement(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo_root = tmp_path / "repos"
    repo_root.mkdir()
    executable = _external_awf(tmp_path)
    runner = _launchctl_runner([], outcomes=(3, 0, 0, 0, 0, 0))
    first = install_launchd(
        agent_id=AGENT_ID,
        repo_root=repo_root,
        executable=executable,
        home=home,
        uid=501,
        run_command=runner,
        development_root=tmp_path / "external",
    )
    original = first.read_bytes()
    assert install_launchd(
        agent_id=AGENT_ID,
        repo_root=repo_root,
        executable=executable,
        home=home,
        uid=501,
        run_command=runner,
        development_root=tmp_path / "external",
    ) == first
    assert first.read_bytes() == original

    with pytest.raises(LaunchdInstallError, match="bootstrap"):
        install_launchd(
            agent_id=AGENT_ID,
            repo_root=repo_root,
            executable=executable,
            home=home,
            uid=501,
            run_command=_launchctl_runner([], outcomes=(3, 1)),
            development_root=tmp_path / "external",
        )
    assert first.read_bytes() == original

    with pytest.raises(LaunchdInstallError, match="print"):
        install_launchd(
            agent_id=AGENT_ID,
            repo_root=repo_root,
            executable=executable,
            home=home,
            uid=501,
            run_command=_launchctl_runner([], outcomes=(3, 0, 1, 0)),
            development_root=tmp_path / "external",
        )
    assert first.read_bytes() == original


def test_install_refuses_to_replace_a_known_running_process(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo_root = tmp_path / "repos"
    repo_root.mkdir()

    with pytest.raises(LaunchdInstallError, match="running process"):
        install_launchd(
            agent_id=AGENT_ID,
            repo_root=repo_root,
            executable=_external_awf(tmp_path),
            home=home,
            uid=501,
            run_command=_launchctl_runner([]),
            development_root=tmp_path / "external",
            running_process=lambda: True,
        )


def test_uninstall_boots_out_before_removing_the_plist_and_preserves_it_on_failure(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    plist_path = home / "Library" / "LaunchAgents" / (LABEL + ".plist")
    plist_path.parent.mkdir(parents=True)
    plist_path.write_text("plist", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    uninstall_launchd(
        home=home, uid=501, run_command=_launchctl_runner(calls, outcomes=(3,))
    )
    assert not plist_path.exists()
    assert calls == [("launchctl", "bootout", "gui/501/" + LABEL)]

    plist_path.write_text("plist", encoding="utf-8")
    with pytest.raises(LaunchdInstallError, match="bootout"):
        uninstall_launchd(
            home=home, uid=501, run_command=_launchctl_runner([], outcomes=(1,))
        )
    assert plist_path.exists()
