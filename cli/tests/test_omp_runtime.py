from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from awf.core.agent_runner import AgentResult
from awf.core.dispatch import (
    ChainedStep,
    OmpDispatch,
    OmpDispatchOptions,
    WorkerSpec,
    omp_dispatch_available,
    resolve_omp_options_from_config,
    select_dispatch,
)
from awf.core.dispatch_provenance import write_omp_dispatch_provenance
from awf.runners.omp import (
    OmpExecutionResult,
    OmpRunnerConfig,
    OmpWorkerTask,
    build_omp_coordinator_prompt,
    build_omp_native_command,
    build_omp_print_command,
    parse_omp_json_stream,
    parse_omp_task_events,
    parse_omp_steering_evidence,
    run_omp_native_batch,
    validate_json_schema,
)


def _write_fake_omp(path: Path, *, response: str) -> Path:
    script = f'''#!/usr/bin/env python3
import json
import sys
if "--version" in sys.argv:
    print("omp/99.0.0")
    raise SystemExit(0)
model = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else "default-model"
message = {{
    "role": "assistant",
    "content": [{{"type": "text", "text": {response!r}}}],
    "provider": "fixture-provider",
    "model": model,
    "usage": {{"input": 11, "output": 7, "totalTokens": 18, "cost": {{"total": 0.01}}}},
    "responseId": "resp-fixture",
    "stopReason": "stop",
}}
print(json.dumps({{"type": "session", "id": "session-fixture"}}))
print(json.dumps({{"type": "message_end", "message": message}}))
print(json.dumps({{"type": "agent_end", "messages": [message]}}))
'''
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def _write_fake_native_omp(
    path: Path,
    *,
    envelope: dict,
    agents: list[dict] | None = None,
    count_path: Path | None = None,
    prompt_capture_path: Path | None = None,
    config_capture_path: Path | None = None,
    child_pid_path: Path | None = None,
    exit_code: int = 0,
) -> Path:
    agent_rows = agents or []
    count_setup = (
        f'''count_path = Path({str(count_path)!r})
count = int(count_path.read_text()) if count_path.exists() else 0
count_path.write_text(str(count + 1))
'''
        if count_path
        else ""
    )
    prompt_capture = (
        f'''prompt_capture = Path({str(prompt_capture_path)!r})
prompt_capture.write_text(Path(prompt_arg[1:]).read_text(encoding="utf-8"), encoding="utf-8")
'''
        if prompt_capture_path
        else ""
    )
    config_capture = (
        f'''config_arg = sys.argv[sys.argv.index("--config") + 1]
Path({str(config_capture_path)!r}).write_text(
    Path(config_arg).read_text(encoding="utf-8"),
    encoding="utf-8",
)
'''
        if config_capture_path
        else ""
    )
    child_setup = (
        f'''child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path({str(child_pid_path)!r}).write_text(str(child.pid))
def stop_child(_signum, _frame):
    try:
        child.terminate()
    except ProcessLookupError:
        pass
    child.wait()
    raise SystemExit(143)
signal.signal(signal.SIGTERM, stop_child)
time.sleep(60)
'''
        if child_pid_path
        else ""
    )
    response = json.dumps(envelope, separators=(",", ":"))
    script = f'''#!/usr/bin/env python3
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

{count_setup}
prompt_arg = sys.argv[sys.argv.index("-p") + 1]
assert prompt_arg.startswith("@")
{prompt_capture}
{config_capture}
{child_setup}
agents = {agent_rows!r}
tasks = [
    {{"name": row["id"], "agent": row.get("agent", "task"), "task": "fixture"}}
    for row in agents
]
message = {{
    "role": "assistant",
    "content": [{{"type": "text", "text": {response!r}}}],
    "provider": "fixture-provider",
    "model": "coordinator-model",
    "usage": {{"input": 31, "output": 17, "totalTokens": 48}},
}}
print(json.dumps({{"type": "session", "id": "coordinator-session"}}))
print(json.dumps({{
    "type": "tool_execution_update",
    "toolCallId": "call-task-fixture",
    "toolName": "task",
    "args": {{"tasks": tasks}},
    "partialResult": {{"details": {{"progress": agents}}}},
}}))
print(json.dumps({{
    "type": "tool_execution_end",
    "toolCallId": "call-task-fixture",
    "toolName": "task",
    "result": {{"details": {{"results": agents}}}},
}}))
print(json.dumps({{"type": "message_end", "message": message}}))
print(json.dumps({{"type": "agent_end", "messages": [message]}}))
raise SystemExit({exit_code})
'''
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def _result_schema() -> dict:
    return {
        "type": "object",
        "required": ["conclusion"],
        "properties": {
            "conclusion": {"type": "string", "enum": ["PASS", "FAIL"]},
        },
    }


def _native_workers() -> list[OmpWorkerTask]:
    return [
        OmpWorkerTask(
            name="Awf000Reviewer",
            role="reviewer",
            prompt="review privately",
            agent_type="code-reviewer",
            output_schema=_result_schema(),
            schema_mode="strict",
            isolated=True,
            require_json=True,
        ),
        OmpWorkerTask(
            name="Awf001Verifier",
            role="verifier",
            prompt="verify privately",
            agent_type="agent-sdk-verifier-py",
            output_schema=_result_schema(),
            schema_mode="permissive",
            isolated=False,
            require_json=True,
        ),
    ]


def _successful_current_host_bridge(calls: list[dict]):
    def bridge(
        *,
        prompt,
        workers,
        cwd,
        config,
        model,
        timeout_sec,
        agent_model_overrides,
    ):
        immutable = False
        try:
            agent_model_overrides["unexpected"] = "mutated"
        except TypeError:
            immutable = True
        calls.append(
            {
                "workers": [worker.name for worker in workers],
                "overrides": dict(agent_model_overrides),
                "immutable": immutable,
            }
        )
        progress = [
            {
                "index": index,
                "task_id": f"01CURRENT{index}",
                "agent_name": worker.agent_type,
                "status": "completed",
                "resolved_model": worker.model,
            }
            for index, worker in enumerate(workers)
        ]
        envelope = {
            "awf_omp_batch": 1,
            "workers": [
                {"name": worker.name, "result": {"conclusion": "PASS"}}
                for worker in workers
            ],
        }
        return OmpExecutionResult(
            returncode=0,
            stdout=json.dumps(envelope),
            stderr="",
            elapsed_sec=0.01,
            metadata={
                "session_id": "current-host-session",
                "task_progress": progress,
            },
        )

    return bridge


def test_build_omp_print_command_uses_json_no_session_and_model():
    config = OmpRunnerConfig(command="omp-bin", model="slow", extra_args=("--no-tools",))
    assert build_omp_print_command("work", config) == [
        "omp-bin",
        "--no-tools",
        "--mode",
        "json",
        "--no-session",
        "--model",
        "slow",
        "-p",
        "work",
    ]


def test_parse_omp_json_stream_extracts_final_message_and_usage():
    message = {
        "role": "assistant",
        "content": [{"type": "text", "text": "done"}],
        "provider": "openai-codex",
        "model": "gpt-current",
        "usage": {"input": 5, "output": 2, "totalTokens": 7},
        "responseId": "resp-1",
    }
    stream = "\n".join(
        [
            json.dumps({"type": "session", "id": "session-1"}),
            json.dumps({"type": "message_end", "message": message}),
            json.dumps({"type": "agent_end", "messages": [message]}),
        ]
    )
    output, metadata, input_tokens, output_tokens = parse_omp_json_stream(
        stream,
        session_persisted=False,
    )
    assert output == "done"
    assert metadata["session_id"] == "session-1"
    assert metadata["model"] == "gpt-current"
    assert metadata["session_persisted"] is False
    assert input_tokens == 5
    assert output_tokens == 2


def test_omp_print_dispatch_runs_worker_and_preserves_provenance(tmp_path: Path):
    response = json.dumps(
        {
            "conclusion": "PASS",
            "findings": [],
            "evidence": ["fixture"],
            "risks": [],
            "action_items": [],
        }
    )
    fake = _write_fake_omp(tmp_path / "omp", response=response)
    options = OmpDispatchOptions(
        config=OmpRunnerConfig(
            command=str(fake),
            coordination_surface="print",
        ),
        role_models={"reviewer": "review-model"},
    )
    result = OmpDispatch(options).run(
        [WorkerSpec(role="reviewer", provider=object(), prompt="review", require_json=True)],
        cwd=str(tmp_path),
    )[0]
    assert result.conclusion == "PASS"
    assert result.provider_name == "omp:fixture-provider"
    assert result.metadata["model"] == "review-model"
    assert result.metadata["session_id"] == "session-fixture"
    assert result.metadata["coordination_surface"] == "print"
    assert result.input_tokens == 11
    assert result.output_tokens == 7


def test_select_dispatch_uses_omp_only_when_explicit_and_available(tmp_path: Path):
    fake = _write_fake_omp(tmp_path / "omp", response="ok")
    options = OmpDispatchOptions(config=OmpRunnerConfig(command=str(fake)))
    assert omp_dispatch_available(options.config) is True
    selected = select_dispatch(
        worker_count=2,
        cwd=tmp_path,
        preference="omp",
        omp_options=options,
    )
    assert isinstance(selected, OmpDispatch)


def test_resolve_omp_options_reads_native_runtime_options():
    options = resolve_omp_options_from_config(
        {
            "dispatch": {
                "omp": {
                    "command": "custom-omp",
                    "model": "default-role",
                    "role_models": {"reviewer": "slow"},
                    "extra_args": ["--no-tools"],
                    "no_session": False,
                    "timeout_sec": 42,
                    "coordination_surface": "print",
                    "execution_mode": "current_host",
                    "capacity": 3,
                    "termination_grace_sec": 0.25,
                }
            }
        }
    )
    assert options.config.command == "custom-omp"
    assert options.config.model == "default-role"
    assert options.config.extra_args == ("--no-tools",)
    assert options.config.no_session is False
    assert options.config.timeout_sec == 42
    assert options.config.coordination_surface == "print"
    assert options.config.execution_mode == "current_host"
    assert options.config.capacity == 3
    assert options.config.termination_grace_sec == 0.25
    assert options.model_for("reviewer") == "slow"
    assert options.model_for("other") == "default-role"


def test_write_omp_dispatch_provenance_redacts_response_body(tmp_path: Path):
    (tmp_path / ".workflow").mkdir()
    agent = AgentResult(
        provider_name="omp:fixture",
        role="reviewer",
        stdout="sensitive response",
        stderr="",
        returncode=0,
        elapsed_sec=1.25,
        parsed={"conclusion": "PASS", "findings": []},
        metadata={
            "status": "ok",
            "backend": "omp",
            "session_id": "session-1",
            "model": "slow",
            "usage": {"totalTokens": 99},
            "worker_usage": {"tokens": 7, "cost": 0.02, "duration_ms": 1250},
            "cost": 0.02,
        },
    )
    path = write_omp_dispatch_provenance(
        tmp_path,
        strategy="parallel",
        mode="cross",
        agents=[agent],
        elapsed_sec=1.5,
    )
    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["backend"] == "omp"
    assert payload["agents"][0]["metadata"]["session_id"] == "session-1"
    assert payload["agents"][0]["conclusion"] == "PASS"
    assert payload["agents"][0]["runtime"]["usage"]["tokens"] == 7
    assert payload["agents"][0]["runtime"]["cost"] == 0.02
    assert payload["agents"][0]["status"] == "completed"
    assert payload["agents"][0]["declared_status_matches_evidence"] is True
    assert payload["agents"][0]["runtime"]["coordinator_usage"]["totalTokens"] == 99
    assert "sensitive response" not in path.read_text(encoding="utf-8")
    assert len(payload["agents"][0]["output_sha256"]) == 64


def test_provenance_rejects_declared_completion_after_local_validation_failure(
    tmp_path: Path,
):
    (tmp_path / ".workflow").mkdir()
    agent = AgentResult(
        provider_name="omp:fixture",
        role="reviewer",
        stdout="{}",
        stderr="schema_validation_failed",
        returncode=2,
        elapsed_sec=0.1,
        parse_error=True,
        metadata={
            "status": "completed",
            "schema_validation": {"mode": "strict", "valid": False},
        },
    )

    path = write_omp_dispatch_provenance(
        tmp_path,
        strategy="parallel",
        mode="cross",
        agents=[agent],
        elapsed_sec=0.1,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["agents"][0]["status"] == "failed"
    assert payload["agents"][0]["declared_status"] == "completed"
    assert payload["agents"][0]["declared_status_matches_evidence"] is False


def test_json_schema_validation_enforces_draft_2020_object_contract():
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"value": {"type": "string", "const": "valid"}},
        "required": ["value"],
        "additionalProperties": False,
    }

    assert validate_json_schema({"value": "valid"}, schema) == []
    errors = validate_json_schema({"value": "wrong", "extra": True}, schema)
    assert any("was expected" in error for error in errors)
    assert any("Additional properties are not allowed" in error for error in errors)


def test_coordinator_prompt_serializes_one_exact_native_task_batch(tmp_path: Path):
    prompt = build_omp_coordinator_prompt(_native_workers(), capacity=4)
    assert "Call `task` exactly once" in prompt
    assert "bounded, event-driven coordination loop" in prompt
    assert "call it at most 4 times total" in prompt
    assert "at most one `hub send` message" in prompt
    assert "Never call `task` again" in prompt
    assert "Never busy-poll `hub jobs`" in prompt
    assert '"steering":' in prompt
    assert '"name":"Awf000Reviewer"' in prompt
    assert '"agent":"code-reviewer"' in prompt
    assert '"schemaMode":"strict"' in prompt
    assert '"isolated":true' in prompt
    assert '"outputSchema":{"type":"object"' in prompt
    command = build_omp_native_command(
        tmp_path / "prompt.txt",
        OmpRunnerConfig(
            command="omp-bin",
            model="coordinator",
            no_session=False,
        ),
    )
    assert command == [
        "omp-bin",
        "--mode",
        "json",
        "--model",
        "coordinator",
        "-p",
        f"@{tmp_path / 'prompt.txt'}",
    ]


def test_parse_native_task_events_uses_authentic_progress_ids():
    agents = [
        {
            "index": 0,
            "id": "01NATIVE-A",
            "agent": "code-reviewer",
            "status": "running",
            "resolvedModel": "model-a",
            "tokens": 10,
        },
        {
            "index": 1,
            "id": "01NATIVE-B",
            "agent": "agent-sdk-verifier-py",
            "status": "completed",
            "resolvedModel": "model-b",
            "durationMs": 1250,
            "tokens": 25,
            "cost": 0.02,
        },
    ]
    completed = [
        {**agents[0], "status": "completed", "durationMs": 900},
        agents[1],
    ]
    stream = "\n".join(
        [
            json.dumps(
                {
                    "type": "tool_execution_update",
                    "toolCallId": "task-call",
                    "toolName": "task",
                    "partialResult": {"details": {"progress": agents}},
                }
            ),
            json.dumps(
                {
                    "type": "tool_execution_end",
                    "toolCallId": "task-call",
                    "toolName": "task",
                    "result": {"details": {"results": completed}},
                }
            ),
            json.dumps(
                {
                    "type": "tool_execution_end",
                    "toolCallId": "hub-call",
                    "toolName": "hub",
                    "result": {
                        "details": {
                            "jobs": [
                                {
                                    "id": "01NATIVE-B",
                                    "status": "completed",
                                    "resolvedModel": "model-b-final",
                                    "durationMs": 1300,
                                    "resultText": (
                                        "Isolation: changes captured at "
                                        "`/tmp/worker-b.patch` (apply=false)."
                                    ),
                                }
                            ]
                        }
                    },
                }
            ),
        ]
    )
    records = parse_omp_task_events(stream)
    assert [record["task_id"] for record in records] == [
        "01NATIVE-A",
        "01NATIVE-B",
    ]
    assert records[0]["status"] == "completed"
    assert records[1]["agent_name"] == "agent-sdk-verifier-py"
    assert records[1]["resolved_model"] == "model-b-final"
    assert records[1]["duration_ms"] == 1300
    assert records[1]["cost"] == 0.02
    assert records[1]["patch_path"] == "/tmp/worker-b.patch"


def test_native_parallel_dispatch_invokes_omp_once_and_preserves_input_order(
    tmp_path: Path,
):
    count_path = tmp_path / "count"
    prompt_path = tmp_path / "prompt"
    config_path = tmp_path / "config"
    agents = [
        {
            "index": 0,
            "id": "01TASK-A",
            "agent": "code-reviewer",
            "status": "completed",
            "resolvedModel": "worker-a",
            "durationMs": 100,
            "tokens": 12,
            "cost": 0.01,
        },
        {
            "index": 1,
            "id": "01TASK-B",
            "agent": "agent-sdk-verifier-py",
            "status": "completed",
            "resolvedModel": "worker-b",
            "durationMs": 200,
            "tokens": 23,
            "cost": 0.02,
        },
    ]
    fake = _write_fake_native_omp(
        tmp_path / "omp",
        envelope={
            "awf_omp_batch": 1,
            "workers": [
                {"name": "Awf001Verifier", "result": {"conclusion": "FAIL"}},
                {"name": "Awf000Reviewer", "result": {"conclusion": "PASS"}},
            ],
        },
        agents=agents,
        count_path=count_path,
        prompt_capture_path=prompt_path,
        config_capture_path=config_path,
    )
    options = OmpDispatchOptions(
        config=OmpRunnerConfig(
            command=str(fake),
            no_session=False,
            coordination_surface="native",
            capacity=4,
        ),
        role_models={
            "reviewer": "openai-codex/gpt-review",
            "verifier": "anthropic/claude-verify",
        },
    )
    results = OmpDispatch(options).run(
        [
            WorkerSpec(
                role="reviewer",
                provider=object(),
                prompt="review privately",
                require_json=True,
                agent_type="code-reviewer",
                output_schema=_result_schema(),
                schema_mode="strict",
                isolated=True,
            ),
            WorkerSpec(
                role="verifier",
                provider=object(),
                prompt="verify privately",
                require_json=True,
                agent_type="agent-sdk-verifier-py",
                output_schema=_result_schema(),
            ),
        ],
        cwd=str(tmp_path),
        strategy="parallel",
    )
    assert count_path.read_text() == "1"
    assert [result.role for result in results] == ["reviewer", "verifier"]
    assert [result.conclusion for result in results] == ["PASS", "FAIL"]
    assert [result.metadata["task_id"] for result in results] == [
        "01TASK-A",
        "01TASK-B",
    ]
    assert results[0].metadata["agent_uri"] == "agent://01TASK-A"
    assert results[0].metadata["history_uri"] == "history://01TASK-A"
    assert results[0].metadata["coordinator_session_id"] == "coordinator-session"
    assert results[0].metadata["model"] == "worker-a"
    assert results[0].metadata["worker_usage"]["tokens"] == 12
    assert '"agent":"code-reviewer"' in prompt_path.read_text(encoding="utf-8")
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "task": {
            "agentModelOverrides": {
                "agent-sdk-verifier-py": "anthropic/claude-verify",
                "code-reviewer": "openai-codex/gpt-review",
            },
            "isolation": {
                "mode": "auto",
                "apply": False,
                "merge": "patch",
            },
        }
    }
    assert results[0].metadata["requested_worker_model"] == "openai-codex/gpt-review"
    assert results[1].metadata["requested_worker_model"] == "anthropic/claude-verify"


def test_native_parallel_rejects_conflicting_models_for_same_agent(
    tmp_path: Path,
) -> None:
    workers = [
        OmpWorkerTask(
            name="Awf000Plan",
            role="plan",
            prompt="plan",
            agent_type="plan-validator",
            model="openai-codex/gpt-review",
        ),
        OmpWorkerTask(
            name="Awf001Verify",
            role="verify",
            prompt="verify",
            agent_type="plan-validator",
            model="anthropic/claude-verify",
        ),
    ]

    results = run_omp_native_batch(
        workers,
        cwd=str(tmp_path),
        config=OmpRunnerConfig(command=str(tmp_path / "missing-omp")),
    )

    assert all(result.returncode != 0 for result in results)
    assert all("omp_worker_model_conflict" in result.stderr for result in results)


def test_native_strict_schema_failure_and_permissive_metadata(tmp_path: Path):
    agents = [
        {"index": 0, "id": "01STRICT", "agent": "task", "status": "completed"},
        {"index": 1, "id": "01PERMISSIVE", "agent": "task", "status": "completed"},
    ]
    fake = _write_fake_native_omp(
        tmp_path / "omp",
        envelope={
            "awf_omp_batch": 1,
            "workers": [
                {"name": "StrictWorker", "result": {"value": 1}},
                {"name": "PermissiveWorker", "result": {"value": 1}},
            ],
        },
        agents=agents,
    )
    schema = {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": "string", "enum": ["valid"]}},
    }
    results = run_omp_native_batch(
        [
            OmpWorkerTask(
                name="StrictWorker",
                role="strict",
                prompt="strict",
                agent_type="task",
                output_schema=schema,
                schema_mode="strict",
            ),
            OmpWorkerTask(
                name="PermissiveWorker",
                role="permissive",
                prompt="permissive",
                agent_type="task",
                output_schema=schema,
                schema_mode="permissive",
            ),
        ],
        cwd=str(tmp_path),
        config=OmpRunnerConfig(command=str(fake)),
    )
    assert results[0].parse_error is True
    assert results[0].returncode != 0
    assert results[0].metadata["schema_validation"]["valid"] is False
    assert results[1].parsed == {"value": 1}
    assert results[1].returncode == 0
    assert results[1].metadata["schema_validation"]["valid"] is False


def test_native_require_json_rejects_non_object_with_permissive_schema(
    tmp_path: Path,
) -> None:
    fake = _write_fake_native_omp(
        tmp_path / "omp",
        envelope={
            "awf_omp_batch": 1,
            "workers": [
                {
                    "name": "ScalarWorker",
                    "result": ["not", "an", "object"],
                }
            ],
        },
        agents=[
            {
                "index": 0,
                "id": "01SCALAR",
                "agent": "task",
                "status": "completed",
            }
        ],
    )

    result = run_omp_native_batch(
        [
            OmpWorkerTask(
                name="ScalarWorker",
                role="reviewer",
                prompt="review",
                agent_type="task",
                require_json=True,
                output_schema=True,
                schema_mode="permissive",
            )
        ],
        cwd=str(tmp_path),
        config=OmpRunnerConfig(command=str(fake)),
    )[0]

    assert result.parsed is None
    assert result.parse_error is True
    assert result.stdout == '["not","an","object"]'
    assert result.metadata["task_id"] == "01SCALAR"
    assert result.metadata["schema_validation"]["valid"] is False


def test_native_partial_envelope_preserves_success_and_marks_missing_worker(
    tmp_path: Path,
):
    fake = _write_fake_native_omp(
        tmp_path / "omp",
        envelope={
            "awf_omp_batch": 1,
            "workers": [
                {"name": "Awf000Reviewer", "result": {"conclusion": "PASS"}},
            ],
        },
        agents=[
            {
                "index": 0,
                "id": "01DONE",
                "agent": "code-reviewer",
                "status": "completed",
            }
        ],
    )
    results = run_omp_native_batch(
        _native_workers(),
        cwd=str(tmp_path),
        config=OmpRunnerConfig(command=str(fake)),
    )
    assert results[0].returncode == 0
    assert results[0].conclusion == "PASS"
    assert results[0].metadata["task_id"] == "01DONE"
    assert results[1].returncode != 0
    assert "missing worker" in results[1].stderr
    assert results[1].metadata["task_id"] is None


def test_native_rejects_success_envelope_without_task_lifecycle_evidence(
    tmp_path: Path,
):
    fake = _write_fake_native_omp(
        tmp_path / "omp",
        envelope={
            "awf_omp_batch": 1,
            "workers": [
                {"name": "Awf000Reviewer", "result": {"conclusion": "PASS"}},
            ],
        },
        agents=[],
    )
    [result] = run_omp_native_batch(
        _native_workers()[:1],
        cwd=str(tmp_path),
        config=OmpRunnerConfig(command=str(fake)),
    )
    assert result.returncode != 0
    assert result.metadata["task_id"] is None
    assert "task lifecycle evidence missing" in result.stderr


def test_native_rejects_failed_task_lifecycle_despite_success_envelope(
    tmp_path: Path,
):
    fake = _write_fake_native_omp(
        tmp_path / "omp",
        envelope={
            "awf_omp_batch": 1,
            "workers": [
                {
                    "name": "Awf000Reviewer",
                    "result": {"conclusion": "PASS"},
                    "returncode": 0,
                },
            ],
        },
        agents=[
            {
                "index": 0,
                "id": "01FAILED",
                "agent": "code-reviewer",
                "status": "failed",
            }
        ],
    )
    [result] = run_omp_native_batch(
        _native_workers()[:1],
        cwd=str(tmp_path),
        config=OmpRunnerConfig(command=str(fake)),
    )
    assert result.returncode != 0
    assert result.metadata["task_id"] == "01FAILED"
    assert "task lifecycle status: failed" in result.stderr


def test_native_rejects_nonzero_coordinator_exit_after_success_events(
    tmp_path: Path,
):
    fake = _write_fake_native_omp(
        tmp_path / "omp",
        envelope={
            "awf_omp_batch": 1,
            "workers": [
                {"name": "Awf000Reviewer", "result": {"conclusion": "PASS"}},
            ],
        },
        agents=[
            {
                "index": 0,
                "id": "01DONE",
                "agent": "code-reviewer",
                "status": "completed",
            }
        ],
        exit_code=7,
    )
    [result] = run_omp_native_batch(
        _native_workers()[:1],
        cwd=str(tmp_path),
        config=OmpRunnerConfig(command=str(fake)),
    )
    assert result.returncode == 7
    assert result.metadata["task_id"] == "01DONE"
    assert "native coordinator exited 7" in result.stderr


def test_native_process_exit_124_is_not_misclassified_as_timeout(
    tmp_path: Path,
):
    fake = _write_fake_native_omp(
        tmp_path / "omp",
        envelope={
            "awf_omp_batch": 1,
            "workers": [
                {"name": "Awf000Reviewer", "result": {"conclusion": "PASS"}},
            ],
        },
        agents=[
            {
                "index": 0,
                "id": "01EXIT124",
                "agent": "code-reviewer",
                "status": "completed",
            }
        ],
        exit_code=124,
    )
    [result] = run_omp_native_batch(
        _native_workers()[:1],
        cwd=str(tmp_path),
        config=OmpRunnerConfig(command=str(fake)),
    )
    assert result.returncode == 124
    assert result.timed_out is False
    assert "native coordinator exited 124" in result.stderr


def test_native_capacity_fails_before_launching_subprocess(tmp_path: Path):
    results = run_omp_native_batch(
        _native_workers(),
        cwd=str(tmp_path),
        config=OmpRunnerConfig(
            command=str(tmp_path / "must-not-run"),
            capacity=1,
        ),
    )
    assert len(results) == 2
    assert all(result.returncode != 0 for result in results)
    assert all("omp_capacity_exceeded" in result.stderr for result in results)


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup is POSIX-specific")
def test_native_timeout_reaps_descendant_processes(tmp_path: Path):
    child_pid_path = tmp_path / "child.pid"
    fake = _write_fake_native_omp(
        tmp_path / "omp",
        envelope={"awf_omp_batch": 1, "workers": []},
        child_pid_path=child_pid_path,
    )
    results = run_omp_native_batch(
        [
            OmpWorkerTask(
                name="TimeoutWorker",
                role="timeout",
                prompt="wait",
                agent_type="task",
            )
        ],
        cwd=str(tmp_path),
        config=OmpRunnerConfig(
            command=str(fake),
            termination_grace_sec=0.05,
        ),
        timeout_sec=1,
    )
    assert results[0].timed_out is True
    child_pid = int(child_pid_path.read_text())
    for _ in range(20):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def _write_recovering_native_omp(
    path: Path,
    *,
    count_path: Path,
    argv_capture_path: Path,
) -> Path:
    script = f'''#!/usr/bin/env python3
import json
import sys
from pathlib import Path

count_path = Path({str(count_path)!r})
count = int(count_path.read_text()) + 1 if count_path.exists() else 1
count_path.write_text(str(count), encoding="utf-8")
with Path({str(argv_capture_path)!r}).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
prompt_arg = sys.argv[sys.argv.index("-p") + 1]
prompt = Path(prompt_arg[1:]).read_text(encoding="utf-8")

def emit(envelope):
    message = {{
        "role": "assistant",
        "content": [{{"type": "text", "text": json.dumps(envelope, separators=(",", ":"))}}],
        "provider": "fixture-provider",
        "model": "coordinator-model",
        "usage": {{"input": 3, "output": 2, "totalTokens": 5}},
    }}
    print(json.dumps({{"type": "message_end", "message": message}}))
    print(json.dumps({{"type": "agent_end", "messages": [message]}}))

if count == 1:
    assert "-r" not in sys.argv
    running = {{
        "index": 0,
        "id": "01RECOVER",
        "agent": "code-reviewer",
        "agentUri": "agent://01RECOVER",
        "historyUri": "history://01RECOVER",
        "status": "running",
    }}
    print(json.dumps({{"type": "session", "id": "coordinator-session"}}))
    print(json.dumps({{
        "type": "tool_execution_update",
        "toolCallId": "initial-task-call",
        "toolName": "task",
        "partialResult": {{"details": {{"progress": [running]}}}},
    }}))
    raise SystemExit(9)

if count == 2:
    resume_index = sys.argv.index("-r")
    assert sys.argv[resume_index + 1] == "coordinator-session"
    assert "Calling `task` is prohibited" in prompt
    completed = {{
        "index": 0,
        "id": "01RECOVER",
        "agent": "code-reviewer",
        "agentUri": "agent://01RECOVER",
        "historyUri": "history://01RECOVER",
        "status": "completed",
    }}
    print(json.dumps({{"type": "session", "id": "coordinator-session"}}))
    print(json.dumps({{
        "type": "tool_execution_end",
        "toolCallId": "recovery-hub-wait",
        "toolName": "hub",
        "result": {{"details": {{"jobs": [completed]}}}},
    }}))
    emit({{
        "awf_omp_batch": 1,
        "workers": [{{"name": "Awf000Reviewer", "status": "completed", "result": {{"conclusion": "PASS", "findings": [], "evidence": [], "risks": [], "action_items": []}}}}],
        "steering": {{
            "wait_calls": 1,
            "inspected_completed": ["Awf000Reviewer"],
            "message": {{
                "target": "Awf000Reviewer",
                "kind": "corrective",
                "content": "SECRET_STEERING",
            }},
            "result_excerpt": "SECRET_RESULT",
        }},
    }})
    raise SystemExit(0)

assert "-r" not in sys.argv
fresh = {{
    "index": 0,
    "id": "01FRESH",
    "agent": "code-reviewer",
    "agentUri": "agent://01FRESH",
    "historyUri": "history://01FRESH",
    "status": "completed",
}}
print(json.dumps({{"type": "session", "id": "fresh-session"}}))
print(json.dumps({{
    "type": "tool_execution_update",
    "toolCallId": "fresh-task-call",
    "toolName": "task",
    "partialResult": {{"details": {{"progress": [fresh]}}}},
}}))
emit({{
    "awf_omp_batch": 1,
    "workers": [{{"name": "Awf000Reviewer", "status": "completed", "result": {{"conclusion": "PASS", "findings": [], "evidence": [], "risks": [], "action_items": []}}}}],
    "steering": {{
        "wait_calls": 0,
        "inspected_completed": ["Awf000Reviewer"],
        "message": None,
    }},
}})
'''
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_native_checkpoint_exists_before_launch_and_contains_only_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    secret_prompt = "SECRET_WORKER_PROMPT"
    fake = _write_fake_native_omp(
        tmp_path / "omp",
        envelope={
            "awf_omp_batch": 1,
            "workers": [
                {
                    "name": "CheckpointWorker",
                    "status": "completed",
                    "result": {"secret_result": "SECRET_WORKER_RESULT"},
                }
            ],
        },
        agents=[
            {
                "index": 0,
                "id": "01CHECKPOINT",
                "agent": "code-reviewer",
                "status": "completed",
            }
        ],
    )
    worker = OmpWorkerTask(
        name="CheckpointWorker",
        role="reviewer",
        prompt=secret_prompt,
        agent_type="code-reviewer",
    )
    import awf.runners.omp as omp_runner

    real_popen = omp_runner.subprocess.Popen
    observed: list[dict] = []

    def observing_popen(*args, **kwargs):
        checkpoint_paths = list(
            (
                tmp_path / ".workflow" / "artifacts" / "dispatch"
            ).glob("omp-native-*.json")
        )
        assert len(checkpoint_paths) == 1
        observed.append(
            json.loads(checkpoint_paths[0].read_text(encoding="utf-8"))
        )
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(omp_runner.subprocess, "Popen", observing_popen)
    [result] = run_omp_native_batch(
        [worker],
        cwd=str(tmp_path),
        config=OmpRunnerConfig(command=str(fake), no_session=False),
    )
    assert result.returncode == 0
    assert observed[0]["state"] == "prepared"
    assert observed[0]["session_persistence_requested"] is True
    assert observed[0]["resumable"] is False
    prepared_text = json.dumps(observed[0], sort_keys=True)
    assert secret_prompt not in prepared_text
    assert "SECRET_WORKER_RESULT" not in prepared_text
    assert len(observed[0]["descriptor_hashes"][0]) == 64
    checkpoint_path = Path(result.metadata["checkpoint_path"])
    finalized_text = checkpoint_path.read_text(encoding="utf-8")
    assert secret_prompt not in finalized_text
    assert "SECRET_WORKER_RESULT" not in finalized_text
    assert json.loads(finalized_text)["state"] == "completed"


def test_interrupted_batch_resumes_same_session_once_and_completed_is_fresh(
    tmp_path: Path,
):
    count_path = tmp_path / "count"
    argv_path = tmp_path / "argv.jsonl"
    fake = _write_recovering_native_omp(
        tmp_path / "omp",
        count_path=count_path,
        argv_capture_path=argv_path,
    )
    workers = _native_workers()[:1]
    config = OmpRunnerConfig(command=str(fake), no_session=False)
    [interrupted] = run_omp_native_batch(
        workers,
        cwd=str(tmp_path),
        config=config,
    )
    assert interrupted.returncode != 0
    checkpoint_path = Path(interrupted.metadata["checkpoint_path"])
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["state"] == "interrupted"
    assert checkpoint["resumable"] is True
    assert checkpoint["coordinator_session_id"] == "coordinator-session"

    [resumed] = run_omp_native_batch(
        workers,
        cwd=str(tmp_path),
        config=config,
    )
    assert resumed.returncode == 0
    assert resumed.metadata["recovery_resumed"] is True
    assert resumed.metadata["task_batch_calls"] == 0
    argv_rows = [
        json.loads(line)
        for line in argv_path.read_text(encoding="utf-8").splitlines()
    ]
    assert "-r" not in argv_rows[0]
    resume_index = argv_rows[1].index("-r")
    assert argv_rows[1][resume_index + 1] == "coordinator-session"
    assert json.loads(checkpoint_path.read_text(encoding="utf-8"))["state"] == "completed"

    [fresh] = run_omp_native_batch(
        workers,
        cwd=str(tmp_path),
        config=config,
    )
    assert fresh.returncode == 0
    assert fresh.metadata["recovery_resumed"] is False
    argv_rows = [
        json.loads(line)
        for line in argv_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(argv_rows) == 3
    assert "-r" not in argv_rows[2]


def test_interrupted_checkpoint_with_missing_handles_fails_before_relaunch(
    tmp_path: Path,
):
    count_path = tmp_path / "count"
    argv_path = tmp_path / "argv.jsonl"
    fake = _write_recovering_native_omp(
        tmp_path / "omp",
        count_path=count_path,
        argv_capture_path=argv_path,
    )
    workers = _native_workers()[:1]
    config = OmpRunnerConfig(command=str(fake), no_session=False)
    [interrupted] = run_omp_native_batch(
        workers,
        cwd=str(tmp_path),
        config=config,
    )
    checkpoint_path = Path(interrupted.metadata["checkpoint_path"])
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["workers"][0]["history_uri"] = None
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    [failed] = run_omp_native_batch(
        workers,
        cwd=str(tmp_path),
        config=config,
    )
    assert failed.returncode != 0
    assert "omp_checkpoint_handles_missing" in failed.stderr
    assert count_path.read_text(encoding="utf-8") == "1"


def test_current_host_capability_mismatch_launches_no_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import awf.runners.omp as omp_runner

    def forbidden_popen(*args, **kwargs):
        raise AssertionError("Popen must not run in current_host mode")

    monkeypatch.setattr(omp_runner.subprocess, "Popen", forbidden_popen)
    [result] = run_omp_native_batch(
        _native_workers()[:1],
        cwd=str(tmp_path),
        config=OmpRunnerConfig(
            command=str(tmp_path / "must-not-run"),
            execution_mode="current_host",
        ),
    )
    assert result.returncode != 0
    assert "omp_same_host_capability_mismatch" in result.stderr


def test_current_host_bridge_receives_immutable_worker_model_overrides(
    tmp_path: Path,
) -> None:
    calls: list[dict] = []
    dispatch = OmpDispatch(
        OmpDispatchOptions(
            config=OmpRunnerConfig(execution_mode="current_host"),
            host_bridge=_successful_current_host_bridge(calls),
            role_models={
                "reviewer": "@default",
                "verifier": "@slow",
            },
        )
    )

    results = dispatch.run(
        [
            WorkerSpec(
                role="reviewer",
                provider=object(),
                prompt="review",
                agent_type="code-reviewer",
            ),
            WorkerSpec(
                role="verifier",
                provider=object(),
                prompt="verify",
                agent_type="quality-validator",
            ),
        ],
        cwd=str(tmp_path),
        strategy="parallel",
    )

    assert [result.returncode for result in results] == [0, 0]
    assert calls == [
        {
            "workers": ["Awf000Reviewer", "Awf001Verifier"],
            "overrides": {
                "code-reviewer": "@default",
                "quality-validator": "@slow",
            },
            "immutable": True,
        }
    ]


def test_current_host_chained_dispatch_forwards_bridge_without_override_leakage(
    tmp_path: Path,
) -> None:
    calls: list[dict] = []
    dispatch = OmpDispatch(
        OmpDispatchOptions(
            config=OmpRunnerConfig(execution_mode="current_host"),
            host_bridge=_successful_current_host_bridge(calls),
            role_models={
                "precision": "@default",
                "quality_validation": "@slow",
            },
        )
    )
    steps = [
        ChainedStep(
            role="precision",
            factory=lambda _prior: WorkerSpec(
                role="precision",
                provider=object(),
                prompt="precision",
                agent_type="plan-validator",
            ),
        ),
        ChainedStep(
            role="quality_validation",
            factory=lambda _prior: WorkerSpec(
                role="quality_validation",
                provider=object(),
                prompt="quality",
                agent_type="quality-validator",
            ),
        ),
    ]

    results = dispatch.run_chained(steps, cwd=str(tmp_path))

    assert [result.returncode for result in results] == [0, 0]
    assert calls == [
        {
            "workers": ["Awf000Precision"],
            "overrides": {"plan-validator": "@default"},
            "immutable": True,
        },
        {
            "workers": ["Awf000QualityValidation"],
            "overrides": {"quality-validator": "@slow"},
            "immutable": True,
        },
    ]


def test_steering_evidence_is_parsed_and_redacted_from_provenance(
    tmp_path: Path,
):
    envelope = json.dumps(
        {
            "awf_omp_batch": 1,
            "workers": [
                {"name": "WorkerA", "status": "completed", "result": {"ok": True}}
            ],
            "steering": {
                "wait_calls": 2,
                "inspected_completed": ["WorkerA", "UnknownWorker"],
                "message": {
                    "target": "WorkerA",
                    "kind": "blocker",
                    "content": "SECRET_MESSAGE",
                },
                "result_excerpt": "SECRET_RESULT",
            },
        }
    )
    evidence = parse_omp_steering_evidence(
        envelope,
        worker_names=["WorkerA"],
    )
    assert evidence == {
        "reported": True,
        "wait_calls": 2,
        "inspected_completed": ["WorkerA"],
        "message_sent": True,
        "message_target": "WorkerA",
        "message_kind": "blocker",
    }
    assert "SECRET_MESSAGE" not in json.dumps(evidence)
    assert "SECRET_RESULT" not in json.dumps(evidence)
    agent = AgentResult(
        provider_name="omp",
        role="reviewer",
        stdout="SECRET_RESULT_BODY",
        stderr="",
        returncode=0,
        elapsed_sec=0.1,
        metadata={"steering_evidence": evidence},
    )
    (tmp_path / ".workflow").mkdir()
    path = write_omp_dispatch_provenance(
        tmp_path,
        strategy="parallel",
        mode="cross",
        agents=[agent],
        elapsed_sec=0.1,
    )
    assert path is not None
    persisted = path.read_text(encoding="utf-8")
    assert "SECRET_MESSAGE" not in persisted
    assert "SECRET_RESULT_BODY" not in persisted
    assert json.loads(persisted)["agents"][0]["steering_evidence"] == evidence
