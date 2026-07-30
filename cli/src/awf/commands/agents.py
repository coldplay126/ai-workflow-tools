from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from awf.core.agent_runner import AgentResult, _try_parse_json
from awf.core.dispatch_provenance import (
    lookup_omp_provenance,
    write_omp_dispatch_provenance,
)
from awf.core.omp_agents import sync_omp_agents
from awf.runners.omp import (
    OmpRunnerConfig,
    omp_worker_name,
    _terminate_process_group,
    parse_omp_json_stream,
    parse_omp_task_events,
)


def run_agents_sync_omp(args) -> int:
    try:
        result = sync_omp_agents(
            args.repo_root or ".",
            dry_run=bool(args.dry_run),
            force=bool(args.force),
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"agents_sync_error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"target_dir: {result['target_dir']}")
        for key in ("created", "updated", "unchanged", "removed", "conflicts"):
            values = result[key]
            print(f"{key}: {', '.join(values) if values else '-'}")
        print(f"generated_count: {result['generated_count']}")
    return 1 if result["conflicts"] else 0


def _load_message(args) -> str:
    if getattr(args, "message_file", None):
        message = Path(args.message_file).read_text(encoding="utf-8")
    else:
        message = str(getattr(args, "message", "") or "")
    if not message.strip():
        raise ValueError("follow-up message must not be empty")
    return message


def _provenance_records(repo_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    dispatch_dir = repo_root / ".workflow" / "artifacts" / "dispatch"
    records: list[tuple[Path, dict[str, Any]]] = []
    if not dispatch_dir.is_dir():
        return records
    for path in sorted(dispatch_dir.glob("*.json")):
        try:
            resolved_path, payload = lookup_omp_provenance(repo_root, path)
        except (FileNotFoundError, ValueError):
            continue
        records.append((resolved_path, payload))
    return records


def _record_matches_role(
    payload: Mapping[str, Any],
    record: Mapping[str, Any],
    role: str,
) -> bool:
    if payload.get("source_kind") != "omp_native_batch":
        return record.get("role") == role
    worker_index = record.get("worker_index")
    if (
        not isinstance(worker_index, int)
        or isinstance(worker_index, bool)
        or worker_index < 0
    ):
        return False
    worker_name = omp_worker_name(worker_index, role)
    return (
        record.get("name") == worker_name
        or record.get("task_id") == worker_name
    )


def _find_followup_target(
    repo_root: Path,
    *,
    run_reference: str | None,
    role: str | None,
    task_id: str | None,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    if run_reference:
        if not role:
            raise ValueError("--role is required with --run")
        path, payload = lookup_omp_provenance(repo_root, run_reference)
        matches = [
            record
            for record in payload.get("agents", [])
            if (
                isinstance(record, dict)
                and _record_matches_role(payload, record, role)
            )
        ]
        if not matches:
            raise FileNotFoundError(
                f"role {role!r} not found in OMP provenance {payload.get('run_id')!r}"
            )
        if len(matches) > 1:
            raise ValueError(
                f"role {role!r} is ambiguous in OMP provenance {payload.get('run_id')!r}"
            )
        return path, payload, matches[0]
    if role:
        raise ValueError("--role is only valid with --run")

    if not task_id:
        raise ValueError("one of --run/--role or --task-id is required")
    exact_matches: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
    lineage_matches: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
    for path, payload in _provenance_records(repo_root):
        for record in payload.get("agents", []):
            if not isinstance(record, dict):
                continue
            if record.get("task_id") == task_id:
                exact_matches.append((path, payload, record))
                continue
            lineage = record.get("lineage")
            successor_id = (
                lineage.get("successor_task_id")
                if isinstance(lineage, Mapping)
                else None
            )
            if successor_id == task_id:
                lineage_matches.append((path, payload, record))
    if exact_matches:
        if len(exact_matches) > 1:
            raise ValueError(
                f"OMP task ID is ambiguous across provenance records: {task_id}"
            )
        return exact_matches[0]
    if not lineage_matches:
        raise FileNotFoundError(f"OMP task ID not found in provenance: {task_id}")
    if len(lineage_matches) > 1:
        raise ValueError(
            f"OMP successor task ID is ambiguous across provenance records: {task_id}"
        )
    return lineage_matches[0]


def _require_actionable_target(
    payload: Mapping[str, Any],
    record: Mapping[str, Any],
) -> tuple[str, str, str, str]:
    session_id = str(
        record.get("coordinator_session_id")
        or payload.get("coordinator_session_id")
        or ""
    ).strip()
    if not session_id or not bool(record.get("session_persisted")):
        raise ValueError(
            "OMP follow-up requires a persisted coordinator session; "
            "the selected provenance record is not resumable"
        )
    task_id = str(record.get("task_id") or "").strip()
    agent_uri = str(record.get("agent_uri") or "").strip()
    history_uri = str(record.get("history_uri") or "").strip()
    if not task_id or not (agent_uri or history_uri):
        raise ValueError(
            "OMP follow-up requires an actionable task ID and agent/history handle"
        )
    return session_id, task_id, agent_uri, history_uri


def _build_followup_prompt(
    *,
    parent_run_id: str,
    task_id: str,
    agent_uri: str,
    history_uri: str,
    message: str,
) -> str:
    return f"""Continue the exact AWF OMP native worker identified below.

Parent AWF run: {parent_run_id}
Original task ID: {task_id}
Original agent URI: {agent_uri or '(unavailable)'}
Exact history URI: {history_uri or '(unavailable)'}

Follow-up message:
<followup>
{message}
</followup>

Rules:
1. First call hub send with `to` set to the exact original task ID `{task_id}`.
   This is the steer/revive path. Do not substitute an agent name or another ID.
2. If and only if hub reports that exact registry agent is unavailable and the
   exact history URI is available, read `{history_uri}`, then launch exactly
   one successor with the task tool. Its task must state parent_run_id
   `{parent_run_id}` and parent_task_id `{task_id}` and include the follow-up
   message. If history is unavailable, report failure without creating a task.
3. A successor is a new agent. Never describe or report it as the original agent.
4. Do not edit `.workflow/state.json`, approve/done artifacts, gates, or scope hashes.
5. Finish with one JSON object only:
   {{"delivery":"direct"|"successor","status":"completed"|"failed"}}
"""


def _load_repo_omp_config(repo_root: Path) -> OmpRunnerConfig:
    provider_config_path = repo_root / ".workflow" / "provider-config.json"
    if not provider_config_path.exists():
        return OmpRunnerConfig.from_env()
    try:
        provider_config = json.loads(
            provider_config_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"invalid workflow provider config: {provider_config_path}: {exc}"
        ) from exc
    if not isinstance(provider_config, dict):
        raise ValueError(
            f"workflow provider config is not an object: {provider_config_path}"
        )
    from awf.core.dispatch import resolve_omp_options_from_config

    return resolve_omp_options_from_config(provider_config).config


def _build_omp_resume_command(
    session_id: str,
    prompt_reference: str,
    config: OmpRunnerConfig | None = None,
) -> list[str]:
    cfg = config or OmpRunnerConfig.from_env()
    return [
        cfg.command,
        *cfg.extra_args,
        "--mode",
        "json",
        "-r",
        session_id,
        "-p",
        prompt_reference,
    ]


def _event_dicts(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _followup_tool_evidence(text: str) -> dict[str, list[dict[str, Any]]]:
    """Extract ordered correlation facts without retaining prompts or results."""
    starts: dict[str, tuple[int, str, dict[str, Any]]] = {}
    evidence: dict[str, list[dict[str, Any]]] = {
        "hub": [],
        "read": [],
        "task": [],
    }
    for index, event in enumerate(_event_dicts(text)):
        event_type = str(event.get("type") or "")
        call_id = str(event.get("toolCallId") or event.get("tool_call_id") or "")
        tool_name = str(event.get("toolName") or event.get("tool_name") or "")
        if event_type == "tool_execution_start" and call_id:
            args = event.get("args")
            starts[call_id] = (
                index,
                tool_name,
                args if isinstance(args, dict) else {},
            )
            continue
        if event_type != "tool_execution_end" or not call_id:
            continue
        start_index, started_tool, args = starts.get(
            call_id, (index, tool_name, {})
        )
        tool = started_tool or tool_name
        result = event.get("result")
        result_map = result if isinstance(result, dict) else {}
        details = result_map.get("details")
        details_map = details if isinstance(details, dict) else {}
        is_error = bool(event.get("isError") or result_map.get("isError"))

        if tool == "hub" and str(args.get("op") or "") == "send":
            target = str(args.get("to") or "")
            receipts = details_map.get("receipts")
            matching = next(
                (
                    receipt
                    for receipt in receipts
                    if isinstance(receipt, dict)
                    and str(receipt.get("to") or "") == target
                ),
                None,
            ) if isinstance(receipts, list) else None
            outcome = (
                str(matching.get("outcome") or "")
                if isinstance(matching, dict)
                else "failed"
            )
            error = (
                str(matching.get("error") or "")
                if isinstance(matching, dict)
                else ""
            ).lower()
            reason_code = None
            if outcome != "delivered":
                reason_code = (
                    "registry_unavailable"
                    if "unknown agent" in error
                    or ("registry" in error and "unavailable" in error)
                    else "delivery_failed"
                )
            evidence["hub"].append(
                {
                    "index": start_index,
                    "call_id": call_id,
                    "op": "send",
                    "target_task_id": target,
                    "outcome": outcome,
                    "reason_code": reason_code,
                }
            )
        elif tool == "read":
            evidence["read"].append(
                {
                    "index": start_index,
                    "call_id": call_id,
                    "path": str(args.get("path") or ""),
                    "outcome": "failed" if is_error else "delivered",
                }
            )
        elif tool == "task":
            evidence["task"].append(
                {
                    "index": start_index,
                    "call_id": call_id,
                    "outcome": "failed" if is_error else "delivered",
                }
            )
    return evidence


def _resolve_followup_evidence(
    text: str,
    *,
    parent_task_id: str,
    parent_history_uri: str,
) -> tuple[str, dict[str, Any] | None, dict[str, Any], str | None]:
    evidence = _followup_tool_evidence(text)
    exact_hub = [
        event
        for event in evidence["hub"]
        if event["target_task_id"] == parent_task_id
    ]
    task_records = [
        record
        for record in parse_omp_task_events(text)
        if str(record.get("task_id") or "") != parent_task_id
    ]
    task_ids = {
        str(record.get("task_id") or "")
        for record in task_records
        if str(record.get("task_id") or "")
    }
    successor = (
        next(
            record
            for record in reversed(task_records)
            if str(record.get("task_id") or "") in task_ids
        )
        if len(task_ids) == 1
        else None
    )
    terminal_hub = exact_hub[-1] if exact_hub else None

    if (
        terminal_hub is not None
        and terminal_hub["outcome"] == "delivered"
        and not task_ids
    ):
        return "direct", None, evidence, None

    if (
        terminal_hub is not None
        and terminal_hub["outcome"] == "failed"
        and terminal_hub["reason_code"] == "registry_unavailable"
    ):
        history_read = next(
            (
                event
                for event in evidence["read"]
                if event["index"] > terminal_hub["index"]
                and event["path"] == parent_history_uri
                and event["outcome"] == "delivered"
            ),
            None,
        )
        task_call = next(
            (
                event
                for event in evidence["task"]
                if history_read is not None
                and event["index"] > history_read["index"]
                and event["outcome"] == "delivered"
            ),
            None,
        )
        if history_read is not None and task_call is not None and successor is not None:
            return "successor", successor, evidence, None
        return (
            "failed",
            None,
            evidence,
            "successor fallback requires an exact successful history read, "
            "one successful task launch, and exactly one authentic successor task ID",
        )

    return (
        "failed",
        None,
        evidence,
        "follow-up requires a terminal delivered hub send to the exact parent task "
        "or an explicit registry-unavailable failure",
    )


def _as_timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _run_omp_resume(
    *,
    session_id: str,
    prompt: str,
    repo_root: Path,
    config: OmpRunnerConfig,
) -> tuple[subprocess.CompletedProcess[str], float]:
    """Resume OMP with a bounded argv and reap the full process group on timeout."""
    prompt_path: Path | None = None
    process: subprocess.Popen[str] | None = None
    started = time.monotonic()
    try:
        with tempfile.NamedTemporaryFile(
            prefix="awf-omp-followup-",
            suffix=".txt",
            mode="w",
            encoding="utf-8",
            delete=False,
        ) as handle:
            handle.write(prompt)
            handle.write("\n")
            prompt_path = Path(handle.name)
        os.chmod(prompt_path, 0o600)
        command = _build_omp_resume_command(
            session_id,
            f"@{prompt_path}",
            config,
        )
        env = os.environ.copy()
        env.setdefault("PI_SKIP_VERSION_CHECK", "1")
        env.setdefault("PI_TELEMETRY", "0")
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            cwd=repo_root,
            env=env,
            start_new_session=os.name == "posix",
        )
        try:
            stdout, stderr = process.communicate(timeout=config.timeout_sec)
            returncode = int(process.returncode or 0)
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = _terminate_process_group(
                process,
                config.termination_grace_sec,
            )
            stdout = stdout or _as_timeout_text(exc.stdout)
            captured_stderr = stderr or _as_timeout_text(exc.stderr)
            timeout_message = (
                f"runner_timeout: omp timed out after {config.timeout_sec}s"
            )
            stderr = (
                f"{timeout_message}\n{captured_stderr}"
                if captured_stderr
                else timeout_message
            )
            returncode = 124
        return (
            subprocess.CompletedProcess(
                command,
                returncode,
                stdout=stdout,
                stderr=stderr,
            ),
            time.monotonic() - started,
        )
    except FileNotFoundError as exc:
        raise OSError(f"omp command not found: {config.command}") from exc
    finally:
        if process is not None and process.poll() is None:
            _terminate_process_group(process, config.termination_grace_sec)
        if prompt_path is not None:
            try:
                prompt_path.unlink()
            except FileNotFoundError:
                pass


def _followup_result(
    *,
    completed: subprocess.CompletedProcess[str],
    elapsed_sec: float,
    coordinator_session_id: str,
    parent_run_id: str,
    parent_task_id: str,
    parent_agent_uri: str,
    parent_history_uri: str,
) -> AgentResult:
    output, runtime, input_tokens, output_tokens = parse_omp_json_stream(
        completed.stdout,
        session_persisted=True,
    )
    declared = _try_parse_json(output) if output else None
    delivery, successor, evidence, evidence_error = _resolve_followup_evidence(
        completed.stdout,
        parent_task_id=parent_task_id,
        parent_history_uri=parent_history_uri,
    )
    if successor is not None:
        resolved_task_id = str(successor["task_id"])
        agent_uri = f"agent://{resolved_task_id}"
        history_uri = f"history://{resolved_task_id}"
    else:
        resolved_task_id = parent_task_id
        agent_uri = parent_agent_uri
        history_uri = parent_history_uri

    returncode = completed.returncode
    if returncode == 0 and evidence_error is not None:
        returncode = 1
    normalized = {
        "delivery": delivery,
        "status": "completed" if returncode == 0 else "failed",
    }
    declared_delivery = (
        str(declared.get("delivery") or "") if isinstance(declared, dict) else ""
    )
    declared_status = (
        str(declared.get("status") or "") if isinstance(declared, dict) else ""
    )
    declared_matches = (
        declared_delivery == normalized["delivery"]
        and declared_status == normalized["status"]
    )
    stderr = completed.stderr.strip()
    if evidence_error:
        stderr = f"followup_evidence_failed: {evidence_error}\n{stderr}".strip()
    metadata = {
        **runtime,
        "coordination_surface": "native",
        "coordinator_session_id": coordinator_session_id,
        "session_persisted": True,
        "status": normalized["status"],
        "task_id": resolved_task_id,
        "agent_uri": agent_uri,
        "history_uri": history_uri,
        "parent_run_id": parent_run_id,
        "parent_task_id": parent_task_id,
        "original_task_id": parent_task_id,
        "successor_task_id": (
            resolved_task_id if delivery == "successor" else None
        ),
        "followup_kind": delivery,
        "followup_evidence": evidence,
        "declared_status_matches_evidence": declared_matches,
    }
    return AgentResult(
        provider_name=(
            f"omp:{runtime['provider']}" if runtime.get("provider") else "omp"
        ),
        role="omp_followup",
        stdout=output,
        stderr=stderr,
        returncode=returncode,
        elapsed_sec=elapsed_sec,
        parse_error=evidence_error is not None,
        parsed=normalized,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        metadata=metadata,
    )


def run_agents_followup_omp(args) -> int:
    repo_root = Path(args.repo_root or ".").resolve()
    try:
        message = _load_message(args)
        _, parent, target = _find_followup_target(
            repo_root,
            run_reference=getattr(args, "run", None),
            role=getattr(args, "role", None),
            task_id=getattr(args, "task_id", None),
        )
        session_id, task_id, agent_uri, history_uri = _require_actionable_target(
            parent, target
        )
        parent_run_id = str(parent.get("run_id") or "").strip()
        if not parent_run_id:
            raise ValueError("selected OMP provenance has no run ID")
        prompt = _build_followup_prompt(
            parent_run_id=parent_run_id,
            task_id=task_id,
            agent_uri=agent_uri,
            history_uri=history_uri,
            message=message,
        )
        config = _load_repo_omp_config(repo_root)
        completed, elapsed = _run_omp_resume(
            session_id=session_id,
            prompt=prompt,
            repo_root=repo_root,
            config=config,
        )
        result = _followup_result(
            completed=completed,
            elapsed_sec=elapsed,
            coordinator_session_id=session_id,
            parent_run_id=parent_run_id,
            parent_task_id=task_id,
            parent_agent_uri=agent_uri,
            parent_history_uri=history_uri,
        )
        provenance_path = write_omp_dispatch_provenance(
            repo_root,
            strategy="followup",
            mode="agents:followup-omp",
            agents=[result],
            elapsed_sec=elapsed,
            parent_run_id=parent_run_id,
            parent_task_id=task_id,
            message_sha256=hashlib.sha256(message.encode("utf-8")).hexdigest(),
        )
        if provenance_path is None:
            raise ValueError("repository has no .workflow directory for provenance")
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"agents_followup_error: {exc}", file=sys.stderr)
        return 1

    summary = {
        "status": result.metadata["status"],
        "delivery": result.metadata["followup_kind"],
        "task_id": result.metadata["task_id"],
        "parent_run_id": parent_run_id,
        "parent_task_id": task_id,
        "provenance_path": str(provenance_path),
        "output_sha256": hashlib.sha256(result.stdout.encode("utf-8")).hexdigest(),
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        for key, value in summary.items():
            print(f"{key}: {value}")
    if result.parse_error:
        print(
            "agents_followup_error: resumed OMP host lacked required "
            "event-proven follow-up evidence",
            file=sys.stderr,
        )
    elif result.returncode != 0:
        print(
            f"agents_followup_error: resumed OMP host exited {result.returncode}",
            file=sys.stderr,
        )
    return 0 if result.returncode == 0 and not result.parse_error else 1
