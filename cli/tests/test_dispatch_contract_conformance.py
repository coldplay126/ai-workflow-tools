from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

from awf.core import _cmux_bridge as cmux_bridge
from awf.core.agent_runner import AgentResult, MultiAgentResult
from awf.core.dispatch import (
    CmuxDispatch,
    backend_capabilities,
    InlineDispatch,
    OmpDispatch,
    OmpDispatchOptions,
    PiDispatch,
    PiDispatchOptions,
    WorkerSpec,
)
from awf.core.dispatch_provenance import write_omp_dispatch_provenance
from awf.core.multi_agent import judge
from awf.providers.base import ProviderResult, TokenUsage
from awf.runners.omp import OmpRunnerConfig, omp_worker_name
from awf.runners.pi import PiRunnerConfig

CORPUS_PATH = Path(__file__).parent / "fixtures" / "dispatch-contract.json"
_REQUIRED_SCENARIOS = {
    "ordered_success", "malformed_strict_output", "timeout_cancel",
    "partial_failure", "provenance_redaction", "deterministic_verdict",
}

# String-valued gaps avoid turning different forms of timeout into fake parity.
LOCAL_SURFACE_CAPABILITIES: dict[str, dict[str, Any]] = {
    "inline": {"evidence": "measured_local", "cancellation": "passive_timeout_only", "isolation": False, "follow_up": False, "strict_schema": False, "partial_failure": "explicit_result", "durable_provenance": False},
    "cmux": {"evidence": "measured_local", "cancellation": "deadline_only", "isolation": False, "follow_up": False, "strict_schema": False, "partial_failure": "timeout_only", "durable_provenance": False},
    "omp_print": {"evidence": "measured_local", "cancellation": "process_timeout", "isolation": False, "follow_up": False, "strict_schema": False, "partial_failure": "explicit_result", "durable_provenance": True},
    "omp_native": {"evidence": "measured_local", "cancellation": "structured_batch_cancel", "isolation": True, "follow_up": True, "strict_schema": True, "partial_failure": "explicit_result", "durable_provenance": True},
    "pi": {"evidence": "measured_local", "cancellation": "process_timeout", "isolation": False, "follow_up": False, "strict_schema": False, "partial_failure": "explicit_result", "durable_provenance": False},
}

# Mappings only: none of these entries has an executable in LOCAL_EXECUTORS.
EXTERNAL_RUNTIME_MAPPINGS: dict[str, dict[str, Any]] = {
    "claude_teams": {"evidence": "mapped_external", "local_adapter": False, "dispatch_mapping": "shared task list and teammate messaging", "result_mapping": "lead collects teammate results"},
    "claude_subagents": {"evidence": "mapped_external", "local_adapter": False, "dispatch_mapping": "parent invokes independent subagents", "result_mapping": "subagent returns to parent"},
    "codex_subagents": {"evidence": "mapped_external", "local_adapter": False, "dispatch_mapping": "parent spawns and steers agent threads", "result_mapping": "parent collects thread results"},
    "gemini_cli_subagents": {"evidence": "mapped_external", "local_adapter": False, "dispatch_mapping": "specialist subagent exposed as a tool", "result_mapping": "tool result returns to parent"},
    "google_adk": {"evidence": "mapped_external", "local_adapter": False, "dispatch_mapping": "application-owned workflow graph", "result_mapping": "application-owned event and session state"},
}


@pytest.fixture(scope="module")
def corpus() -> dict[str, Any]:
    payload = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert set(payload["scenarios"]) == _REQUIRED_SCENARIOS
    return payload["scenarios"]


def _response_text(worker: dict[str, Any]) -> str:
    return str(worker["response_text"]) if "response_text" in worker else json.dumps(worker.get("response", {}), sort_keys=True)


class _CorpusProvider:
    name = "fixture-provider"

    def __init__(self, worker: dict[str, Any]) -> None:
        self.worker = worker

    def complete(self, prompt: str, **_: Any) -> ProviderResult:
        assert prompt == self.worker["prompt_id"]
        if self.worker["outcome"] == "failure":
            return ProviderResult(int(self.worker["returncode"]), _response_text(self.worker), str(self.worker["stderr"]))
        return ProviderResult(
            0, _response_text(self.worker), "",
            TokenUsage(int(self.worker.get("input_tokens", 0)), int(self.worker.get("output_tokens", 0))),
        )


def _worker_specs(workers: list[dict[str, Any]], *, inline_timeout: bool = False) -> list[WorkerSpec]:
    return [
        WorkerSpec(
            role=str(worker["role"]), provider=_CorpusProvider(worker),
            prompt=str(worker["prompt_id"]),
            timeout_sec=-1 if inline_timeout and worker["outcome"] == "timeout" else 1,
            require_json=True,
        )
        for worker in workers
    ]


def _run_inline(workers: list[dict[str, Any]], *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[AgentResult]:
    del monkeypatch
    return InlineDispatch().run(_worker_specs(workers, inline_timeout=True), cwd=str(tmp_path))


def _fake_print_process(workers: list[dict[str, Any]], *, omp: bool) -> Callable[..., subprocess.CompletedProcess[str]]:
    by_prompt = {str(worker["prompt_id"]): worker for worker in workers}

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        worker = by_prompt[str(cmd[-1])]
        if worker["outcome"] == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))
        returncode = int(worker.get("returncode", 0))
        body = _response_text(worker)
        if not omp or returncode:
            stdout = body
        else:
            usage = {"input": int(worker.get("input_tokens", 0)), "output": int(worker.get("output_tokens", 0))}
            usage["totalTokens"] = usage["input"] + usage["output"]
            message = {
                "role": "assistant", "content": [{"type": "text", "text": body}],
                "provider": "fixture-provider", "model": "fixture-model", "usage": usage,
                "responseId": f"response-{worker['role']}", "stopReason": "stop",
            }
            stdout = "\n".join([
                json.dumps({"type": "session", "id": f"session-{worker['role']}"}),
                json.dumps({"type": "message_end", "message": message}),
                json.dumps({"type": "agent_end", "messages": [message]}),
            ])
        return subprocess.CompletedProcess(cmd, returncode, stdout, str(worker.get("stderr", "")))

    return run


def _run_pi(workers: list[dict[str, Any]], *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[AgentResult]:
    import awf.runners.pi as pi_runner
    monkeypatch.setattr(pi_runner.subprocess, "run", _fake_print_process(workers, omp=False))
    options = PiDispatchOptions(config=PiRunnerConfig(command="fixture-pi"))
    return PiDispatch(options).run(_worker_specs(workers), cwd=str(tmp_path))


def _run_omp_print(workers: list[dict[str, Any]], *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[AgentResult]:
    import awf.runners.omp as omp_runner
    monkeypatch.setattr(omp_runner.subprocess, "run", _fake_print_process(workers, omp=True))
    options = OmpDispatchOptions(
        config=OmpRunnerConfig(command="fixture-omp", coordination_surface="print")
    )
    return OmpDispatch(options).run(_worker_specs(workers), cwd=str(tmp_path))


def _run_cmux(workers: list[dict[str, Any]], *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[AgentResult]:
    state = cmux_bridge.CmuxRunState("fixture-run", tmp_path / "fixture.sqlite3", tmp_path / ".agent")
    infos = [cmux_bridge.WorkerInfo(f"worker-{w['role']}", str(w["role"]), None) for w in workers]
    indexed = dict(enumerate(workers))

    def poll_results(_state: Any, *, batch_id: str, deadlines: dict[int, float], poll_interval: float) -> dict[int, dict[str, str]]:
        del _state, batch_id, poll_interval
        return {i: {"message": _response_text(indexed[i])} for i in deadlines if indexed[i]["outcome"] != "timeout"}

    monkeypatch.setattr(cmux_bridge, "find_active_run", lambda cwd: state)
    monkeypatch.setattr(cmux_bridge, "ensure_orchestrator_registered", lambda value: None)
    monkeypatch.setattr(cmux_bridge, "list_workers", lambda value: infos)
    monkeypatch.setattr(cmux_bridge, "write_dispatch_artifact", lambda *args, **kwargs: None)
    monkeypatch.setattr(cmux_bridge, "poll_results", poll_results)
    monkeypatch.setattr(cmux_bridge, "remove_processed_for_batch", lambda *args, **kwargs: 0)
    return CmuxDispatch().run(_worker_specs(workers), cwd=str(tmp_path))


def _native_stream(workers: list[dict[str, Any]]) -> str:
    progress: list[dict[str, Any]] = []
    envelope: list[dict[str, Any]] = []
    partial_timeout = any(worker["outcome"] == "timeout" for worker in workers)
    for index, worker in enumerate(workers):
        status = "timed_out" if worker["outcome"] == "timeout" else (
            "failed" if worker["outcome"] == "failure" else "completed"
        )
        progress.append(
            {
                "taskId": f"task-fixture-{index}",
                "agentName": omp_worker_name(index, str(worker["role"])),
                "agentType": "task",
                "status": status,
                "resolvedModel": "fixture-worker-model",
                "durationMs": 10 + index,
                "tokens": int(worker.get("output_tokens", 0)),
                "cost": 0.001,
                "usage": {
                    "input_tokens": int(worker.get("input_tokens", 0)),
                    "output_tokens": int(worker.get("output_tokens", 0)),
                },
            }
        )
        if partial_timeout and worker["outcome"] == "timeout":
            continue
        item: dict[str, Any] = {
            "name": omp_worker_name(index, str(worker["role"])),
            "status": status,
        }
        if worker["outcome"] == "failure":
            item.update(
                error=str(worker["stderr"]),
                returncode=int(worker["returncode"]),
            )
        else:
            item["result"] = (
                str(worker["response_text"])
                if "response_text" in worker
                else worker.get("response", {})
            )
        envelope.append(item)

    envelope.reverse()
    body = json.dumps(
        {"awf_omp_batch": 1, "workers": envelope},
        sort_keys=True,
    )
    message = {
        "role": "assistant",
        "content": [{"type": "text", "text": body}],
        "provider": "fixture-host",
        "model": "fixture-host-model",
        "usage": {"input": 19, "output": 13, "totalTokens": 32},
        "responseId": "fixture-host-response",
        "stopReason": "stop",
    }
    return "\n".join(
        [
            json.dumps({"type": "session", "id": "fixture-host-session"}),
            json.dumps(
                {
                    "type": "tool_execution_update",
                    "toolCallId": "task-call-fixture",
                    "toolName": "task",
                    "args": {
                        "tasks": [
                            {
                                "name": omp_worker_name(index, str(worker["role"])),
                                "agent": "task",
                                "task": "fixture",
                            }
                            for index, worker in enumerate(workers)
                        ]
                    },
                    "progress": {"agents": progress},
                }
            ),
            json.dumps(
                {
                    "type": "tool_execution_end",
                    "toolCallId": "task-call-fixture",
                    "toolName": "task",
                    "result": {"details": {"agents": progress}},
                }
            ),
            json.dumps({"type": "message_end", "message": message}),
            json.dumps({"type": "agent_end", "messages": [message]}),
        ]
    )


def _execute_omp_native(
    workers: list[dict[str, Any]],
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schemas: dict[str, dict[str, Any]] | None = None,
) -> list[AgentResult]:
    import awf.runners.omp as omp_runner

    stream = _native_stream(workers)
    times_out = any(worker["outcome"] == "timeout" for worker in workers)
    popen_calls: list[list[str]] = []

    class FakeNativeProcess:
        def __init__(self, cmd: list[str], **_: Any) -> None:
            self.args = cmd
            self.pid = 999_999
            self.returncode = 0
            self._first_communicate = True
            popen_calls.append(cmd)

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            if times_out and self._first_communicate:
                self._first_communicate = False
                raise subprocess.TimeoutExpired(self.args, timeout)
            self._first_communicate = False
            return stream, ""

        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    monkeypatch.setattr(omp_runner.subprocess, "Popen", FakeNativeProcess)
    schema_by_role = schemas or {}
    specs = [
        WorkerSpec(
            role=str(worker["role"]),
            provider=_CorpusProvider(worker),
            prompt=str(worker["prompt_id"]),
            timeout_sec=1,
            require_json=False,
            agent_type="task",
            output_schema=schema_by_role.get(str(worker["role"])),
            schema_mode=(
                "strict" if str(worker["role"]) in schema_by_role else "permissive"
            ),
            isolated=True,
        )
        for worker in workers
    ]
    options = OmpDispatchOptions(
        config=OmpRunnerConfig(
            command="fixture-omp",
            coordination_surface="native",
            no_session=False,
            capacity=4,
        )
    )
    results = OmpDispatch(options).run(specs, cwd=str(tmp_path))
    assert len(popen_calls) == 1
    return results


def _run_omp_native(
    workers: list[dict[str, Any]],
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> list[AgentResult]:
    return _execute_omp_native(
        workers,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )


LOCAL_EXECUTORS: dict[str, Callable[..., list[AgentResult]]] = {
    "inline": _run_inline,
    "cmux": _run_cmux,
    "omp_print": _run_omp_print,
    "omp_native": _run_omp_native,
    "pi": _run_pi,
}


def _projection(result: AgentResult) -> tuple[Any, ...]:
    return result.role, result.returncode, result.timed_out, result.parse_error, result.conclusion, result.ok


@pytest.mark.parametrize("surface", sorted(LOCAL_EXECUTORS))
def test_two_worker_success_has_equivalent_ordered_semantics(surface: str, corpus: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = corpus["ordered_success"]
    results = LOCAL_EXECUTORS[surface](scenario["workers"], tmp_path=tmp_path, monkeypatch=monkeypatch)
    projected = [_projection(result) for result in results]
    assert [item[0] for item in projected] == scenario["expected"]["roles"]
    assert [item[1] for item in projected] == scenario["expected"]["returncodes"]
    assert [item[4] for item in projected] == scenario["expected"]["conclusions"]
    assert all(item[5] and not item[3] for item in projected)


@pytest.mark.parametrize("surface", ["inline", "cmux", "omp_print", "pi"])
def test_legacy_json_check_does_not_pretend_to_be_strict_schema(surface: str, corpus: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = corpus["malformed_strict_output"]
    [result] = LOCAL_EXECUTORS[surface]([scenario["worker"]], tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert result.parse_error is scenario["expected"]["parse_error"]
    assert result.parsed is None and result.returncode == 0
    assert LOCAL_SURFACE_CAPABILITIES[surface]["strict_schema"] is False


@pytest.mark.parametrize("surface", sorted(LOCAL_EXECUTORS))
def test_timeout_preserves_completed_partial_result_without_overclaiming_cancel(surface: str, corpus: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = corpus["timeout_cancel"]
    results = LOCAL_EXECUTORS[surface](scenario["workers"], tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert [result.role for result in results] == scenario["expected"]["roles"]
    assert results[0].returncode == 0 and not results[0].timed_out
    assert results[1].timed_out
    if surface == "inline":
        assert results[1].returncode == 0
        assert LOCAL_SURFACE_CAPABILITIES[surface]["cancellation"] == "passive_timeout_only"
    else:
        assert results[1].returncode == scenario["expected"]["timeout_returncode"]
        assert LOCAL_SURFACE_CAPABILITIES[surface]["cancellation"] in {
            "deadline_only",
            "process_timeout",
            "structured_batch_cancel",
        }


@pytest.mark.parametrize("surface", ["inline", "omp_print", "omp_native", "pi"])
def test_explicit_partial_failure_is_preserved_where_supported(surface: str, corpus: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = corpus["partial_failure"]
    results = LOCAL_EXECUTORS[surface](scenario["workers"], tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert [result.role for result in results] == scenario["expected"]["roles"]
    assert [result.returncode for result in results] == scenario["expected"]["returncodes"]
    assert results[0].ok and not results[1].ok
    assert LOCAL_SURFACE_CAPABILITIES[surface]["partial_failure"] == "explicit_result"


def test_native_strict_schema_invalidity_is_fail_closed(
    corpus: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = corpus["malformed_strict_output"]
    companion = corpus["ordered_success"]["workers"][1]
    results = _execute_omp_native(
        [scenario["worker"], companion],
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        schemas={str(scenario["worker"]["role"]): scenario["schema"]},
    )

    malformed, completed = results
    assert malformed.role == scenario["worker"]["role"]
    assert malformed.parse_error is True
    assert malformed.returncode != 0
    assert malformed.metadata["schema_validation"]["valid"] is False
    assert malformed.metadata["schema_validation"]["mode"] == "strict"
    assert completed.ok is True


def test_native_handles_come_from_task_events(
    corpus: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workers = corpus["ordered_success"]["workers"]
    results = _run_omp_native(
        workers,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )

    assert [result.metadata["task_id"] for result in results] == [
        "task-fixture-0",
        "task-fixture-1",
    ]
    assert [result.metadata["agent_uri"] for result in results] == [
        "agent://task-fixture-0",
        "agent://task-fixture-1",
    ]
    assert [result.metadata["history_uri"] for result in results] == [
        "history://task-fixture-0",
        "history://task-fixture-1",
    ]
    assert all(
        result.metadata["coordinator_session_id"] == "fixture-host-session"
        and result.metadata["coordination_surface"] == "native"
        and result.metadata["provider"] == "fixture-host"
        and result.metadata["model"] == "fixture-worker-model"
        for result in results
    )


def test_cmux_failure_channel_is_explicitly_timeout_only() -> None:
    assert LOCAL_SURFACE_CAPABILITIES["cmux"]["partial_failure"] == "timeout_only"


def test_omp_provenance_v2_redacts_prompt_and_response_bodies(corpus: dict[str, Any], tmp_path: Path) -> None:
    scenario = corpus["provenance_redaction"]
    (tmp_path / ".workflow").mkdir()
    hash_fn = getattr(hashlib, scenario["expected"]["hash_algorithm"])
    response_hash = hash_fn(scenario["response"].encode()).hexdigest()
    agent = AgentResult(
        "omp:fixture-provider", "reviewer", scenario["response"], "", 0, 0.25,
        parsed={"conclusion": "PASS", "findings": []},
        metadata={
            "backend": "omp", "coordination_surface": "native", "session_id": "session-fixture",
            "task_id": "task-fixture", "agent_uri": "agent://fixture-agent",
            "history_uri": "history://fixture-agent",
        },
    )
    path = write_omp_dispatch_provenance(tmp_path, strategy="parallel", mode="cross", agents=[agent], elapsed_sec=0.5)
    assert path is not None
    encoded = path.read_text(encoding="utf-8")
    payload = json.loads(encoded)
    assert payload["schema_version"] == scenario["expected"]["schema_version"]
    assert scenario["prompt"] not in encoded and scenario["response"] not in encoded
    record = payload["agents"][0]
    assert record["output_sha256"] == response_hash
    assert len(record["output_sha256"]) == 64
    assert record["metadata"]["agent_uri"] == "agent://fixture-agent"
    assert record["metadata"]["history_uri"] == "history://fixture-agent"


def test_weak_unreproducible_disagreement_is_deterministic_escalation(corpus: dict[str, Any]) -> None:
    scenario = corpus["deterministic_verdict"]
    agents = [
        AgentResult(
            item["provider_name"], item["role"],
            json.dumps({"conclusion": item["conclusion"], "findings": item["findings"]}),
            "", 0, 0.1, parsed={"conclusion": item["conclusion"], "findings": item["findings"]},
        )
        for item in scenario["agents"]
    ]
    verdict, reason = judge(agents)
    result = MultiAgentResult(mode="cross", agents=agents, judge_verdict=verdict, judge_reason=reason)
    assert verdict == scenario["expected"]["verdict"]
    assert scenario["expected"]["reason_contains"] in reason
    assert judge(list(agents)) == (verdict, reason)
    assert judge(list(reversed(agents))) == (verdict, reason)
    assert not result.ok


def test_capability_gaps_are_named_instead_of_mocked_as_support() -> None:
    assert set(LOCAL_SURFACE_CAPABILITIES) == set(LOCAL_EXECUTORS)
    legacy_surfaces = {"inline", "cmux", "omp_print", "pi"}
    for surface in legacy_surfaces:
        capability = LOCAL_SURFACE_CAPABILITIES[surface]
        assert capability["evidence"] == "measured_local"
        assert capability["isolation"] is False
        assert capability["follow_up"] is False
        assert capability["cancellation"] != "structured_batch_cancel"

    native = LOCAL_SURFACE_CAPABILITIES["omp_native"]
    assert native["evidence"] == "measured_local"
    assert native["isolation"] is True
    assert native["follow_up"] is True
    assert native["cancellation"] == "structured_batch_cancel"
    assert native["strict_schema"] is True

    print_options = OmpDispatchOptions(
        config=OmpRunnerConfig(
            command="fixture-omp",
            coordination_surface="print",
        )
    )
    native_options = OmpDispatchOptions(
        config=OmpRunnerConfig(
            command="fixture-omp",
            coordination_surface="native",
            no_session=False,
        )
    )
    for surface in ("inline", "cmux", "pi"):
        declared = backend_capabilities(surface)
        assert not declared.supports(
            {"cancellation", "capacity", "isolation", "strict_schema", "follow_up"}
        )
    declared_print = backend_capabilities("omp", print_options)
    assert not declared_print.supports(
        {"cancellation", "capacity", "isolation", "strict_schema", "follow_up"}
    )
    declared_native = backend_capabilities("omp", native_options)
    assert declared_native.supports(
        {"cancellation", "capacity", "isolation", "strict_schema", "follow_up"}
    )


def test_external_runtimes_are_contract_mappings_not_execution_claims() -> None:
    assert set(EXTERNAL_RUNTIME_MAPPINGS) == {"claude_teams", "claude_subagents", "codex_subagents", "gemini_cli_subagents", "google_adk"}
    assert set(EXTERNAL_RUNTIME_MAPPINGS).isdisjoint(LOCAL_EXECUTORS)
    for mapping in EXTERNAL_RUNTIME_MAPPINGS.values():
        assert mapping["evidence"] == "mapped_external" and mapping["local_adapter"] is False
        assert "dispatch_mapping" in mapping and "result_mapping" in mapping
