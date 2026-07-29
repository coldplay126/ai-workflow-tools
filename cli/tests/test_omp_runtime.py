from __future__ import annotations

import json
from pathlib import Path

from awf.core.agent_runner import AgentResult
from awf.core.dispatch import (
    OmpDispatch,
    OmpDispatchOptions,
    WorkerSpec,
    omp_dispatch_available,
    resolve_omp_options_from_config,
    select_dispatch,
)
from awf.core.dispatch_provenance import write_omp_dispatch_provenance
from awf.runners.omp import OmpRunnerConfig, build_omp_print_command, parse_omp_json_stream


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


def test_omp_dispatch_runs_worker_and_preserves_provenance(tmp_path: Path):
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
        config=OmpRunnerConfig(command=str(fake)),
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


def test_resolve_omp_options_reads_role_models_and_runtime_options():
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
                }
            }
        }
    )
    assert options.config.command == "custom-omp"
    assert options.config.model == "default-role"
    assert options.config.extra_args == ("--no-tools",)
    assert options.config.no_session is False
    assert options.config.timeout_sec == 42
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
        metadata={"backend": "omp", "session_id": "session-1", "model": "slow"},
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
    assert "sensitive response" not in path.read_text(encoding="utf-8")
    assert len(payload["agents"][0]["output_sha256"]) == 64
