"""Behavioral contracts for the user-facing AWF Supervisor CLI."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

from awf.cli import build_parser, main
from awf.commands import supervisor as supervisor_command
from awf.supervisor.client import (
    RepoRef,
    SupervisorAuthRequired,
    SupervisorConflict,
    SupervisorRemoteError,
)
from awf.supervisor.contracts import (
    AgentEnvironment,
    AgentStatus,
    JobState,
    RequestedTarget,
    SupervisorAgent,
    SupervisorJob,
)


NOW = "2026-07-30T12:00:00Z"
LATER = "2026-07-30T12:01:00Z"
WORKFLOW_ID = "2026-07-30-login-contract"
JOB_ID = "job-1"
HARNESS_KEY = "5a2c1e31-cf45-4fe4-ae47-f40d3eb90837"
UUID1_KEY = "123e4567-e89b-12d3-a456-426614174000"
PROMPT_LIMIT_BYTES = 64 * 1024
ACTIVE_WATCH_STATES = {
    JobState.QUEUED,
    JobState.CLAIMED,
    JobState.PREPARING,
    JobState.RUNNING,
}


def job_fixture(**updates: Any) -> SupervisorJob:
    """Return a validated, immutable public job envelope."""
    payload: Dict[str, Any] = {
        "schema_version": 1,
        "job_id": JOB_ID,
        "workflow_id": WORKFLOW_ID,
        "state": JobState.QUEUED.value,
        "desired_state": "RUNNING",
        "approval_required": True,
        "requested_target": RequestedTarget.AUTO.value,
        "owner_agent_id": None,
        "lease_expires_at": None,
        "generation": 3,
        "attempt": 1,
        "repo_refs": [{"repo": "blip-server", "base": "main"}],
        "required_capabilities": ["git", "omp", "github"],
        "checkpoint": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    payload.update(updates)
    return SupervisorJob.from_dict(payload)


def agent_fixture(**updates: Any) -> SupervisorAgent:
    """Return a validated, immutable public agent envelope."""
    payload: Dict[str, Any] = {
        "schema_version": 1,
        "agent_id": "local-agent-1",
        "environment": AgentEnvironment.LOCAL.value,
        "status": AgentStatus.ONLINE.value,
        "last_heartbeat_at": NOW,
        "max_concurrency": 2,
        "active_jobs": 1,
        "capabilities": ["git", "omp"],
        "repos": ["blip-server"],
        "version": {"awf": "1.0.0", "omp": "0.1.0"},
    }
    payload.update(updates)
    return SupervisorAgent.from_dict(payload)


class RecordingSupervisorClient:
    """Deterministic Supervisor client double recording public CLI calls."""

    def __init__(
        self,
        *,
        jobs: Sequence[Any] = (),
        agents: Sequence[SupervisorAgent] = (),
        response_job: Optional[SupervisorJob] = None,
        errors: Optional[Mapping[str, BaseException]] = None,
    ) -> None:
        self._jobs = list(jobs)
        self._agents = list(agents)
        self._response_job = response_job
        self._errors = dict(errors or {})
        self.submissions: List[Dict[str, Any]] = []
        self.status_job_ids: List[str] = []
        self.cancel_calls: List[Tuple[str, int]] = []
        self.approve_calls: List[Tuple[str, int]] = []
        self.reject_calls: List[Tuple[str, int]] = []
        self.agents_calls = 0

    def _raise_if_configured(self, method: str) -> None:
        error = self._errors.get(method)
        if error is not None:
            raise error

    def _response(self) -> SupervisorJob:
        if self._response_job is None:
            raise AssertionError("unexpected Supervisor client response request")
        return self._response_job

    def submit_job(
        self,
        *,
        workflow_id: str,
        requested_target: RequestedTarget,
        repo_refs: Sequence[RepoRef],
        required_capabilities: Sequence[str],
        prompt: str,
        idempotency_key: Optional[str] = None,
    ) -> SupervisorJob:
        self.submissions.append(
            {
                "workflow_id": workflow_id,
                "requested_target": requested_target,
                "repo_refs": list(repo_refs),
                "required_capabilities": list(required_capabilities),
                "prompt": prompt,
                "idempotency_key": idempotency_key,
            }
        )
        self._raise_if_configured("submit_job")
        return self._response()

    def get_job(self, job_id: str) -> SupervisorJob:
        self.status_job_ids.append(job_id)
        self._raise_if_configured("get_job")
        if not self._jobs:
            raise AssertionError("unexpected get_job call")
        result = self._jobs.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def cancel_job(self, job_id: str, *, generation: int) -> SupervisorJob:
        self.cancel_calls.append((job_id, generation))
        self._raise_if_configured("cancel_job")
        return self._response()

    def approve_job(self, job_id: str, *, generation: int) -> SupervisorJob:
        self.approve_calls.append((job_id, generation))
        self._raise_if_configured("approve_job")
        return self._response()

    def reject_job(self, job_id: str, *, generation: int) -> SupervisorJob:
        self.reject_calls.append((job_id, generation))
        self._raise_if_configured("reject_job")
        return self._response()

    def list_agents(self) -> List[SupervisorAgent]:
        self.agents_calls += 1
        self._raise_if_configured("list_agents")
        return list(self._agents)


def assert_usage_error(parser: Any, argv: Sequence[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        parser.parse_args(list(argv))
    assert raised.value.code == 2


def submit_argv(*extra: str) -> List[str]:
    return [
        "supervisor",
        "submit",
        "--workflow-id",
        WORKFLOW_ID,
        "--repo",
        "blip-server:main",
        "--prompt",
        "Fix the login contract.\n",
        *extra,
    ]


def test_supervisor_parser_exposes_the_complete_v1_command_surface() -> None:
    parser = build_parser()

    submit = parser.parse_args(submit_argv("--target", "local", "--json"))
    assert submit.command == "supervisor"
    assert submit.supervisor_command == "submit"
    assert submit.workflow_id == WORKFLOW_ID
    assert submit.repo == ["blip-server:main"]
    assert submit.prompt == "Fix the login contract.\n"
    assert submit.prompt_file is None
    assert submit.target == "local"
    assert submit.json is True

    status = parser.parse_args(["supervisor", "status", JOB_ID, "--json"])
    assert status.command == "supervisor"
    assert status.supervisor_command == "status"
    assert status.job_id == JOB_ID
    assert status.json is True

    watch = parser.parse_args(["supervisor", "watch", JOB_ID, "--interval", "17", "--json"])
    assert watch.command == "supervisor"
    assert watch.supervisor_command == "watch"
    assert watch.job_id == JOB_ID
    assert watch.interval == 17
    assert watch.json is True

    for command in ("cancel", "approve", "reject"):
        args = parser.parse_args(
            ["supervisor", command, JOB_ID, "--generation", "3", "--json"]
        )
        assert args.command == "supervisor"
        assert args.supervisor_command == command
        assert args.job_id == JOB_ID
        assert args.generation == 3
        assert args.json is True

    agents = parser.parse_args(["supervisor", "agents", "--json"])
    assert agents.command == "supervisor"
    assert agents.supervisor_command == "agents"
    assert agents.json is True


def test_submit_parser_requires_workflow_repo_and_exactly_one_prompt_source() -> None:
    parser = build_parser()

    assert_usage_error(
        parser,
        [
            "supervisor",
            "submit",
            "--repo",
            "blip-server:main",
            "--prompt",
            "Fix it.",
        ],
    )
    assert_usage_error(
        parser,
        [
            "supervisor",
            "submit",
            "--workflow-id",
            WORKFLOW_ID,
            "--prompt",
            "Fix it.",
        ],
    )
    assert_usage_error(
        parser,
        [
            "supervisor",
            "submit",
            "--workflow-id",
            WORKFLOW_ID,
            "--repo",
            "blip-server:main",
        ],
    )
    assert_usage_error(
        parser,
        [
            "supervisor",
            "submit",
            "--workflow-id",
            WORKFLOW_ID,
            "--repo",
            "blip-server:main",
            "--prompt",
            "Fix it.",
            "--prompt-file",
            "task.txt",
        ],
    )


def test_submit_parses_repo_bases_with_slashes_and_preserves_capability_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = job_fixture()
    fake = RecordingSupervisorClient(response_job=job)
    monkeypatch.setattr(supervisor_command, "_client", lambda args: fake)

    rc = main(
        [
            "supervisor",
            "submit",
            "--workflow-id",
            WORKFLOW_ID,
            "--repo",
            "blip-server:release/2026/login",
            "--repo",
            "web-ui:feature/auth/login",
            "--require-capability",
            "github",
            "--require-capability",
            "docker",
            "--target",
            "aws",
            "--prompt",
            "Fix the login contract.\n",
        ]
    )

    assert rc == 0
    assert fake.submissions == [
        {
            "workflow_id": WORKFLOW_ID,
            "requested_target": RequestedTarget.AWS,
            "repo_refs": [
                RepoRef(repo="blip-server", base="release/2026/login"),
                RepoRef(repo="web-ui", base="feature/auth/login"),
            ],
            "required_capabilities": ["git", "omp", "github", "docker"],
            "prompt": "Fix the login contract.\n",
            "idempotency_key": None,
        }
    ]


@pytest.mark.parametrize(
    "capabilities",
    [
        ("git",),
        ("github", "github"),
        ("contains whitespace",),
        ("caf\u00e9",),
    ],
)
def test_submit_rejects_duplicate_or_invalid_capabilities_before_constructing_client(
    monkeypatch: pytest.MonkeyPatch,
    capabilities: Tuple[str, ...],
) -> None:
    def unexpected_client(args: Any) -> RecordingSupervisorClient:
        raise AssertionError("invalid capabilities must not construct a Supervisor client")

    monkeypatch.setattr(supervisor_command, "_client", unexpected_client)
    argv = submit_argv()
    for capability in capabilities:
        argv.extend(["--require-capability", capability])

    assert main(argv) == 5


@pytest.mark.parametrize(
    "workflow_id",
    (
        "contains whitespace",
        "caf\u00e9",
        "x" * 129,
    ),
)
def test_submit_rejects_invalid_workflow_ids_before_constructing_client(
    monkeypatch: pytest.MonkeyPatch,
    workflow_id: str,
) -> None:
    client_calls: List[Any] = []
    fake = RecordingSupervisorClient(response_job=job_fixture())

    def record_client(args: Any) -> RecordingSupervisorClient:
        client_calls.append(args)
        return fake

    monkeypatch.setattr(supervisor_command, "_client", record_client)

    assert (
        main(
            [
                "supervisor",
                "submit",
                "--workflow-id",
                workflow_id,
                "--repo",
                "blip-server:main",
                "--prompt",
                "Fix the login contract.\n",
            ]
        )
        == 5
    )
    assert client_calls == []


def test_submit_rejects_duplicate_repo_names_before_constructing_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_calls: List[Any] = []
    fake = RecordingSupervisorClient(response_job=job_fixture())

    def record_client(args: Any) -> RecordingSupervisorClient:
        client_calls.append(args)
        return fake

    monkeypatch.setattr(supervisor_command, "_client", record_client)

    assert (
        main(
            [
                "supervisor",
                "submit",
                "--workflow-id",
                WORKFLOW_ID,
                "--repo",
                "blip-server:main",
                "--repo",
                "blip-server:release/2026",
                "--prompt",
                "Fix the login contract.\n",
            ]
        )
        == 5
    )
    assert client_calls == []


def test_submit_loads_inline_and_file_prompts_up_to_the_64_kib_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    job = job_fixture()
    inline_client = RecordingSupervisorClient(response_job=job)
    monkeypatch.setattr(supervisor_command, "_client", lambda args: inline_client)

    assert main(submit_argv()) == 0
    assert inline_client.submissions[0]["prompt"] == "Fix the login contract.\n"

    maximum_prompt = "x" * PROMPT_LIMIT_BYTES
    prompt_file = tmp_path / "maximum.txt"
    prompt_file.write_text(maximum_prompt, encoding="utf-8")
    file_client = RecordingSupervisorClient(response_job=job)
    monkeypatch.setattr(supervisor_command, "_client", lambda args: file_client)

    assert (
        main(
            [
                "supervisor",
                "submit",
                "--workflow-id",
                WORKFLOW_ID,
                "--repo",
                "blip-server:main",
                "--prompt-file",
                str(prompt_file),
            ]
        )
        == 0
    )
    assert file_client.submissions[0]["prompt"] == maximum_prompt


def test_submit_rejects_empty_and_over_64_kib_prompts_before_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    empty_prompt_file = tmp_path / "empty.txt"
    empty_prompt_file.write_text("", encoding="utf-8")
    oversized_prompt_file = tmp_path / "oversized.txt"
    oversized_prompt_file.write_text("x" * (PROMPT_LIMIT_BYTES + 1), encoding="utf-8")

    def unexpected_client(args: Any) -> RecordingSupervisorClient:
        raise AssertionError("invalid prompts must not construct a Supervisor client")

    monkeypatch.setattr(supervisor_command, "_client", unexpected_client)

    assert main(submit_argv("--prompt", "")) == 5
    assert (
        main(
            [
                "supervisor",
                "submit",
                "--workflow-id",
                WORKFLOW_ID,
                "--repo",
                "blip-server:main",
                "--prompt-file",
                str(empty_prompt_file),
            ]
        )
        == 5
    )
    assert (
        main(
            [
                "supervisor",
                "submit",
                "--workflow-id",
                WORKFLOW_ID,
                "--repo",
                "blip-server:main",
                "--prompt-file",
                str(oversized_prompt_file),
            ]
        )
        == 5
    )


def test_submit_reads_regular_prompt_files_once_in_bounded_binary_mode_before_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BinaryPromptReader:
        def __init__(self, contents: bytes) -> None:
            self._contents = contents
            self.read_sizes: List[int] = []

        def __enter__(self) -> "BinaryPromptReader":
            return self

        def __exit__(self, *unused: Any) -> None:
            return None

        def read(self, size: int) -> bytes:
            self.read_sizes.append(size)
            return self._contents

    class RegularPromptPath:
        def __init__(self, contents: bytes) -> None:
            self._contents = contents
            self.reader = BinaryPromptReader(contents)
            self.regular_checks = 0
            self.open_modes: List[str] = []
            self.read_text_calls = 0

        def is_file(self) -> bool:
            self.regular_checks += 1
            return True

        def open(self, *args: Any, **kwargs: Any) -> BinaryPromptReader:
            mode = args[0] if args else kwargs["mode"]
            self.open_modes.append(mode)
            return self.reader

        def read_text(self, *unused: Any, **unused_kwargs: Any) -> str:
            self.read_text_calls += 1
            return self._contents.decode("utf-8")

    source = RegularPromptPath(b"x" * (PROMPT_LIMIT_BYTES + 1))

    def unexpected_client(args: Any) -> RecordingSupervisorClient:
        raise AssertionError("oversized prompt must be rejected before client construction")

    monkeypatch.setattr(supervisor_command, "Path", lambda value: source)
    monkeypatch.setattr(supervisor_command, "_client", unexpected_client)

    assert (
        main(
            [
                "supervisor",
                "submit",
                "--workflow-id",
                WORKFLOW_ID,
                "--repo",
                "blip-server:main",
                "--prompt-file",
                "oversized.txt",
            ]
        )
        == 5
    )
    assert source.regular_checks == 1
    assert source.open_modes == ["rb"]
    assert source.reader.read_sizes == [PROMPT_LIMIT_BYTES + 1]
    assert source.read_text_calls == 0


def test_submit_rejects_nonregular_prompt_files_without_reading_or_constructing_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NonregularPromptPath:
        def __init__(self) -> None:
            self.regular_checks = 0
            self.open_calls = 0
            self.read_text_calls = 0

        def is_file(self) -> bool:
            self.regular_checks += 1
            return False

        def open(self, *unused: Any, **unused_kwargs: Any) -> Any:
            self.open_calls += 1
            return None

        def read_text(self, *unused: Any, **unused_kwargs: Any) -> str:
            self.read_text_calls += 1
            return "must not read a nonregular prompt source"

    source = NonregularPromptPath()
    client_calls: List[Any] = []
    fake = RecordingSupervisorClient(response_job=job_fixture())

    def record_client(args: Any) -> RecordingSupervisorClient:
        client_calls.append(args)
        return fake

    monkeypatch.setattr(supervisor_command, "Path", lambda value: source)
    monkeypatch.setattr(supervisor_command, "_client", record_client)

    assert (
        main(
            [
                "supervisor",
                "submit",
                "--workflow-id",
                WORKFLOW_ID,
                "--repo",
                "blip-server:main",
                "--prompt-file",
                "not-a-regular-file",
            ]
        )
        == 5
    )
    assert source.regular_checks == 1
    assert source.open_calls == 0
    assert source.read_text_calls == 0
    assert client_calls == []


def test_submit_uses_complete_deterministic_harness_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompt = tmp_path / "task.txt"
    prompt.write_text("Fix the login contract.\n", encoding="utf-8")
    job = job_fixture()
    fake = RecordingSupervisorClient(response_job=job)
    monkeypatch.setenv("AWF_SUPERVISOR_E2E_HARNESS", "1")
    monkeypatch.setattr(supervisor_command, "_client", lambda args: fake)
    argv = [
        "supervisor",
        "submit",
        "--workflow-id",
        WORKFLOW_ID,
        "--repo",
        "blip-server:main",
        "--require-capability",
        "github",
        "--prompt-file",
        str(prompt),
        "--idempotency-key",
        HARNESS_KEY,
        "--json",
    ]

    assert main(argv) == 0
    assert main(argv) == 0
    assert fake.submissions == [
        {
            "workflow_id": WORKFLOW_ID,
            "requested_target": RequestedTarget.AUTO,
            "repo_refs": [RepoRef(repo="blip-server", base="main")],
            "required_capabilities": ["git", "omp", "github"],
            "prompt": "Fix the login contract.\n",
            "idempotency_key": HARNESS_KEY,
        },
        {
            "workflow_id": WORKFLOW_ID,
            "requested_target": RequestedTarget.AUTO,
            "repo_refs": [RepoRef(repo="blip-server", base="main")],
            "required_capabilities": ["git", "omp", "github"],
            "prompt": "Fix the login contract.\n",
            "idempotency_key": HARNESS_KEY,
        },
    ]
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        job.to_dict(),
        job.to_dict(),
    ]


def test_submit_without_harness_key_leaves_idempotency_generation_to_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = job_fixture()
    fake = RecordingSupervisorClient(response_job=job)
    monkeypatch.delenv("AWF_SUPERVISOR_E2E_HARNESS", raising=False)
    monkeypatch.setattr(supervisor_command, "_client", lambda args: fake)

    assert main(submit_argv()) == 0
    assert fake.submissions[0]["idempotency_key"] is None


@pytest.mark.parametrize(
    "harness_value,idempotency_key",
    [
        (None, HARNESS_KEY),
        ("1", "not-a-uuid"),
        ("1", UUID1_KEY),
        ("1", HARNESS_KEY.upper()),
    ],
)
def test_submit_rejects_disallowed_or_noncanonical_harness_keys_before_prompt_or_client(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    harness_value: Optional[str],
    idempotency_key: str,
) -> None:
    if harness_value is None:
        monkeypatch.delenv("AWF_SUPERVISOR_E2E_HARNESS", raising=False)
    else:
        monkeypatch.setenv("AWF_SUPERVISOR_E2E_HARNESS", harness_value)

    def prompt_must_not_be_read(args: Any) -> str:
        raise AssertionError("harness validation must run before prompt loading")

    def client_must_not_be_created(args: Any) -> RecordingSupervisorClient:
        raise AssertionError("harness validation must run before client construction")

    monkeypatch.setattr(supervisor_command, "_load_prompt", prompt_must_not_be_read)
    monkeypatch.setattr(supervisor_command, "_client", client_must_not_be_created)

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "supervisor",
                "submit",
                "--workflow-id",
                WORKFLOW_ID,
                "--repo",
                "blip-server:main",
                "--prompt-file",
                "must-not-be-read.txt",
                "--idempotency-key",
                idempotency_key,
            ]
        )

    assert raised.value.code == 2
    assert "idempotency" in capsys.readouterr().err.lower()


def test_status_renders_stable_human_text_and_one_json_object_per_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    job = job_fixture()
    text_client = RecordingSupervisorClient(jobs=[job])
    monkeypatch.setattr(supervisor_command, "_client", lambda args: text_client)

    assert main(["supervisor", "status", JOB_ID]) == 0
    assert text_client.status_job_ids == [JOB_ID]
    assert capsys.readouterr().out == (
        "job_id=job-1 workflow_id=2026-07-30-login-contract "
        "state=QUEUED desired_state=RUNNING generation=3 requested_target=auto\n"
    )

    json_client = RecordingSupervisorClient(jobs=[job])
    monkeypatch.setattr(supervisor_command, "_client", lambda args: json_client)

    assert main(["supervisor", "status", JOB_ID, "--json"]) == 0
    assert json_client.status_job_ids == [JOB_ID]
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        job.to_dict()
    ]


def test_agents_renders_stable_human_text_and_one_json_object_per_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent = agent_fixture()
    text_client = RecordingSupervisorClient(agents=[agent])
    monkeypatch.setattr(supervisor_command, "_client", lambda args: text_client)

    assert main(["supervisor", "agents"]) == 0
    assert text_client.agents_calls == 1
    assert capsys.readouterr().out == (
        "agent_id=local-agent-1 environment=local status=ONLINE "
        "active_jobs=1 max_concurrency=2 capabilities=git,omp repos=blip-server\n"
    )

    second_agent = agent_fixture(
        agent_id="aws-agent-2",
        environment=AgentEnvironment.AWS.value,
        status=AgentStatus.DRAINING.value,
        active_jobs=0,
        capabilities=["git", "omp", "docker"],
        repos=["blip-server", "web-ui"],
    )
    json_client = RecordingSupervisorClient(agents=[agent, second_agent])
    monkeypatch.setattr(supervisor_command, "_client", lambda args: json_client)

    assert main(["supervisor", "agents", "--json"]) == 0
    assert json_client.agents_calls == 1
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        agent.to_dict(),
        second_agent.to_dict(),
    ]


@pytest.mark.parametrize("command", ("cancel", "approve", "reject"))
def test_mutation_commands_require_a_strictly_positive_generation(command: str) -> None:
    parser = build_parser()

    assert_usage_error(
        parser,
        ["supervisor", command, JOB_ID, "--generation", "0"],
    )
    assert_usage_error(
        parser,
        ["supervisor", command, JOB_ID, "--generation", "-1"],
    )
    args = parser.parse_args(
        ["supervisor", command, JOB_ID, "--generation", "1"]
    )
    assert args.generation == 1


@pytest.mark.parametrize(
    "command,command_arguments",
    (
        ("status", ()),
        ("watch", ()),
        ("cancel", ("--generation", "1")),
        ("approve", ("--generation", "1")),
        ("reject", ("--generation", "1")),
    ),
)
@pytest.mark.parametrize(
    "job_id",
    (
        "contains whitespace",
        "caf\u00e9",
        "x" * 129,
    ),
)
def test_job_commands_reject_invalid_ids_before_constructing_client(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    command_arguments: Tuple[str, ...],
    job_id: str,
) -> None:
    client_calls: List[Any] = []

    def record_client(args: Any) -> RecordingSupervisorClient:
        client_calls.append(args)
        raise ValueError("invalid identifiers must not construct a Supervisor client")

    monkeypatch.setattr(supervisor_command, "_client", record_client)

    assert main(["supervisor", command, job_id, *command_arguments]) == 5
    assert client_calls == []


def test_mutation_commands_invoke_only_the_fixed_client_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = job_fixture()
    fake = RecordingSupervisorClient(response_job=job)
    monkeypatch.setattr(supervisor_command, "_client", lambda args: fake)

    assert main(["supervisor", "cancel", JOB_ID, "--generation", "3"]) == 0
    assert main(["supervisor", "approve", JOB_ID, "--generation", "3"]) == 0
    assert main(["supervisor", "reject", JOB_ID, "--generation", "3"]) == 0
    assert fake.cancel_calls == [(JOB_ID, 3)]
    assert fake.approve_calls == [(JOB_ID, 3)]
    assert fake.reject_calls == [(JOB_ID, 3)]


@pytest.mark.parametrize("interval", (0, 61))
def test_watch_parser_rejects_intervals_outside_one_to_sixty(interval: int) -> None:
    assert_usage_error(
        build_parser(),
        ["supervisor", "watch", JOB_ID, "--interval", str(interval)],
    )


def test_watch_parser_accepts_both_interval_bounds() -> None:
    parser = build_parser()
    assert parser.parse_args(["supervisor", "watch", JOB_ID, "--interval", "1"]).interval == 1
    assert parser.parse_args(["supervisor", "watch", JOB_ID, "--interval", "60"]).interval == 60


def test_watch_resolves_an_omitted_interval_from_supervisor_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PollingConfig:
        poll_interval_seconds = 13

    queued = job_fixture()
    waiting_approval = job_fixture(
        state=JobState.WAITING_APPROVAL.value,
        updated_at=LATER,
    )
    fake = RecordingSupervisorClient(jobs=[queued, waiting_approval])
    config_calls: List[None] = []
    sleeps: List[int] = []

    def load_config() -> PollingConfig:
        config_calls.append(None)
        return PollingConfig()

    monkeypatch.setattr(supervisor_command, "_client", lambda args: fake)
    monkeypatch.setattr(supervisor_command, "load_supervisor_config", load_config)
    monkeypatch.setattr(supervisor_command.time, "sleep", sleeps.append)

    assert main(["supervisor", "watch", JOB_ID, "--json"]) == 0
    assert sleeps == [13]
    assert config_calls == [None]


def test_watch_explicit_interval_does_not_load_or_use_the_config_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued = job_fixture()
    waiting_approval = job_fixture(
        state=JobState.WAITING_APPROVAL.value,
        updated_at=LATER,
    )
    fake = RecordingSupervisorClient(jobs=[queued, waiting_approval])
    sleeps: List[int] = []

    def config_must_not_be_loaded() -> Any:
        raise AssertionError("explicit --interval must not read the config default")

    monkeypatch.setattr(supervisor_command, "_client", lambda args: fake)
    monkeypatch.setattr(
        supervisor_command,
        "load_supervisor_config",
        config_must_not_be_loaded,
    )
    monkeypatch.setattr(supervisor_command.time, "sleep", sleeps.append)

    assert main(["supervisor", "watch", JOB_ID, "--interval", "7", "--json"]) == 0
    assert sleeps == [7]


def test_watch_prints_only_distinct_state_generation_updated_at_tuples(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    queued = job_fixture()
    running = job_fixture(
        state=JobState.RUNNING.value,
        generation=4,
        updated_at=LATER,
    )
    waiting_approval = job_fixture(
        state=JobState.WAITING_APPROVAL.value,
        generation=4,
        updated_at="2026-07-30T12:02:00Z",
    )
    fake = RecordingSupervisorClient(
        jobs=[queued, queued, running, waiting_approval]
    )
    sleeps: List[int] = []
    monkeypatch.setattr(supervisor_command, "_client", lambda args: fake)
    monkeypatch.setattr(supervisor_command.time, "sleep", sleeps.append)

    assert main(["supervisor", "watch", JOB_ID, "--interval", "7", "--json"]) == 0
    assert fake.status_job_ids == [JOB_ID, JOB_ID, JOB_ID, JOB_ID]
    assert sleeps == [7, 7, 7]
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        queued.to_dict(),
        running.to_dict(),
        waiting_approval.to_dict(),
    ]


@pytest.mark.parametrize(
    "state",
    tuple(state for state in JobState if state not in ACTIVE_WATCH_STATES),
)
def test_watch_stops_after_printing_each_operator_or_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: JobState,
) -> None:
    stopped = job_fixture(state=state.value)
    fake = RecordingSupervisorClient(jobs=[stopped])
    monkeypatch.setattr(supervisor_command, "_client", lambda args: fake)

    assert main(["supervisor", "watch", JOB_ID, "--json"]) == 0
    assert fake.status_job_ids == [JOB_ID]
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        stopped.to_dict()
    ]


def test_watch_returns_130_on_ctrl_c(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = RecordingSupervisorClient(jobs=[KeyboardInterrupt()])
    monkeypatch.setattr(supervisor_command, "_client", lambda args: fake)

    assert main(["supervisor", "watch", JOB_ID, "--json"]) == 130


@pytest.mark.parametrize(
    "error,expected_exit",
    [
        (
            SupervisorAuthRequired(
                "AWS SSO login is required", request_id="request-auth"
            ),
            3,
        ),
        (SupervisorConflict("stale generation", request_id="request-conflict"), 4),
        (
            SupervisorRemoteError(
                "control plane unavailable", request_id="request-remote"
            ),
            5,
        ),
        (OSError("network unavailable"), 5),
        (ValueError("invalid local request"), 5),
    ],
)
def test_status_maps_auth_conflict_remote_and_local_failures_to_stable_exits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: BaseException,
    expected_exit: int,
) -> None:
    fake = RecordingSupervisorClient(jobs=[error])
    monkeypatch.setattr(supervisor_command, "_client", lambda args: fake)

    assert main(["supervisor", "status", JOB_ID]) == expected_exit
    stderr = capsys.readouterr().err
    assert stderr == "error: {}\n".format(error)
    if isinstance(error, SupervisorRemoteError):
        assert "request_id={}".format(error.request_id) in stderr
