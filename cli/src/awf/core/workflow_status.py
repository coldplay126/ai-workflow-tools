from __future__ import annotations

from pathlib import Path

from awf.core.cmux_health import probe_cmux_broker_health
from awf.core.dispatch_provenance import summarize_omp_evidence
from awf.core.event_sync_summary import summarize_event_sync


def _format_omp_audit(value: object) -> str:
    audit = value if isinstance(value, dict) else {}
    return ",".join(
        f"{state}={audit.get(state) if audit.get(state) is not None else 'unknown'}"
        for state in ("requested", "acknowledged", "final", "partial", "unresolved")
    )


def _format_omp_usage(value: object) -> str:
    usage = value if isinstance(value, dict) else {}
    totals = usage.get("totals")
    if not isinstance(totals, dict):
        return "unknown"
    return " ".join(f"{name}={amount}" for name, amount in sorted(totals.items()))


def _append_omp_evidence_panel(lines: list[str], evidence: dict) -> None:
    status = str(evidence.get("status") or "unknown")
    diagnostics = evidence.get("diagnostics")
    suffix = (
        f" ({', '.join(str(item) for item in diagnostics)})"
        if isinstance(diagnostics, list) and diagnostics
        else ""
    )
    lines.append(f"omp_evidence: {status}{suffix}")

    workflow = evidence.get("workflow")
    if isinstance(workflow, dict):
        lines.append(
            "  workflow: "
            f"id={workflow.get('workflow_id') or 'unknown'} "
            f"phase={workflow.get('phase') or 'unknown'} "
            f"attempt={workflow.get('attempt') or 'unknown'}"
        )

    usage = evidence.get("usage")
    usage_map = usage if isinstance(usage, dict) else {}
    primary_usage = usage_map.get("phase_primary_estimated")
    primary_map = primary_usage if isinstance(primary_usage, dict) else {}
    lines.append(
        "  usage.phase_primary_estimated: "
        f"{primary_map.get('status', 'unknown')} "
        f"{_format_omp_usage(primary_map)}"
    )
    worker_usage = usage_map.get("omp_worker_reported")
    worker_usage_map = worker_usage if isinstance(worker_usage, dict) else {}
    lines.append(
        "  usage.omp_worker_reported: "
        f"{worker_usage_map.get('status', 'unknown')} "
        f"{_format_omp_usage(worker_usage_map)} "
        f"reported_workers={worker_usage_map.get('reported_worker_count', 0)} "
        f"unknown_workers={worker_usage_map.get('unknown_worker_count', 0)}"
    )

    dispatches = evidence.get("dispatches")
    if not isinstance(dispatches, list):
        return
    for dispatch in dispatches:
        if not isinstance(dispatch, dict):
            continue
        provenance = dispatch.get("provenance")
        provenance_map = provenance if isinstance(provenance, dict) else {}
        if dispatch.get("status") == "blocked":
            lines.append(
                "  dispatch: blocked "
                f"provenance={provenance_map.get('path') or 'unknown'} "
                f"sha256={provenance_map.get('sha256') or 'unknown'} "
                f"diagnostic={dispatch.get('diagnostic') or 'unknown'}"
            )
            continue
        correlation = dispatch.get("correlation")
        correlation_map = correlation if isinstance(correlation, dict) else {}
        lines.append(
            "  dispatch: "
            f"run={dispatch.get('dispatch_run_id') or 'unknown'} "
            f"status={dispatch.get('status') or 'unknown'} "
            f"evidence_status={dispatch.get('evidence_status') or 'unknown'} "
            f"workflow={correlation_map.get('workflow_id') or 'unknown'} "
            f"phase={correlation_map.get('phase') or 'unknown'} "
            f"attempt={correlation_map.get('attempt') or 'unknown'} "
            f"provenance={provenance_map.get('path') or 'unknown'} "
            f"sha256={provenance_map.get('sha256') or 'unknown'}"
        )
        checkpoint = dispatch.get("checkpoint")
        checkpoint_map = checkpoint if isinstance(checkpoint, dict) else {}
        lineage = dispatch.get("lineage")
        lineage_map = lineage if isinstance(lineage, dict) else {}
        lines.append(
            "    checkpoint: "
            f"state={checkpoint_map.get('state') or 'unknown'} "
            f"resumable={checkpoint_map.get('resumable') if checkpoint_map.get('resumable') is not None else 'unknown'} "
            f"session_persisted={checkpoint_map.get('session_persisted') if checkpoint_map.get('session_persisted') is not None else 'unknown'} "
            f"parent_run={lineage_map.get('parent_run_id') or 'unknown'} "
            f"parent_task={lineage_map.get('parent_task_id') or 'unknown'}"
        )
        lines.append(
            "    dispatch_audit: "
            f"timeout=unknown cancellation={_format_omp_audit(dispatch.get('cancellation'))} "
            f"partial={dispatch.get('partial_result') or 'unknown'}"
        )
        agents = dispatch.get("agents")
        if not isinstance(agents, list):
            continue
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            schema = agent.get("schema")
            schema_map = schema if isinstance(schema, dict) else {}
            reported = agent.get("reported_usage")
            reported_map = reported if isinstance(reported, dict) else {}
            lineage = agent.get("lineage")
            lineage_map = lineage if isinstance(lineage, dict) else {}
            values = reported_map.get("values")
            usage_detail = (
                " ".join(
                    f"{name}={amount}"
                    for name, amount in sorted(values.items())
                )
                if isinstance(values, dict)
                else "unknown"
            )
            lines.append(
                "    worker: "
                f"role={agent.get('role') or 'unknown'} "
                f"status={agent.get('status') or 'unknown'} "
                f"strict_schema_status={schema_map.get('strict_status') or 'unknown'} "
                f"timeout={agent.get('timeout') or 'unknown'} "
                f"cancellation={_format_omp_audit(agent.get('cancellation'))} "
                f"partial={agent.get('partial_result') or 'unknown'} "
                f"patch_scope={agent.get('patch_scope_status') or 'unknown'} "
                f"model={agent.get('model') or 'unknown'} "
                f"usage_source={reported_map.get('source') or 'omp_worker_reported'} "
                f"usage_status={reported_map.get('status') or 'unknown'} "
                f"usage={usage_detail} "
                f"parent_task={lineage_map.get('parent_task_id') or 'unknown'} "
                f"successor_task={lineage_map.get('successor_task_id') or 'unknown'}"
            )


_DETAIL_HIDE_STATUSES = {"absent", "ok", "fresh", "alive"}


def summarize_workflow_state(state: dict, repo_root: str | Path | None = None) -> str:
    """Workflow state를 사람이 읽는 텍스트로 요약한다.

    ``repo_root`` 가 전달되면 읽기 전용 OMP evidence panel과 cmux-agent broker
    health 한 줄을 추가한다. 미전달(None) 시 두 통합을 생략한다 (회귀 호환).
    """
    lines = [
        f"id: {state.get('id', '-')}",
        f"repo: {state.get('repo', '-')}",
        f"branch: {state.get('branch', '-')}",
        f"current_phase: {state.get('currentPhase', '-')}",
    ]

    phases = state.get("phases", {})
    if phases:
        lines.append("phases:")
        for name, info in phases.items():
            status = info.get("status", "-")
            retries = info.get("retries", 0)
            lines.append(f"  - {name}: {status} (retries={retries})")

    gates = state.get("gates", {})
    if gates:
        gate_summary = ", ".join(
            f"{gate}="
            f"{'PASS' if info.get('passed') is True else 'FAIL' if info.get('passed') is False else 'PENDING'}"
            for gate, info in sorted(gates.items())
        )
        lines.append(f"gates: {gate_summary}")

    history = state.get("history", [])
    if history:
        last_entry = history[-1]
        lines.append(
            "last_event: "
            f"{last_entry.get('timestamp', '-')} "
            f"{last_entry.get('phase', '-')} "
            f"{last_entry.get('action', '-')}"
        )

    sync = state.get("eventSync", {}) or {}
    event_summary = summarize_event_sync(sync)
    if event_summary:
        lines.append(event_summary)
        event_stages = sync.get("stages", {}) or {}
        if event_stages:
            lines.append("event_stages:")
            for name, info in sorted(event_stages.items()):
                status = str(info.get("status", "-"))
                duration = info.get("durationSec")
                if duration is None:
                    lines.append(f"  - {name}: {status}")
                else:
                    lines.append(f"  - {name}: {status} ({float(duration):.1f}s)")
        event_phases = sync.get("phases", {}) or {}
        if event_phases:
            lines.append("event_phases:")
            for name, info in sorted(event_phases.items()):
                status = str(info.get("status", "-"))
                duration = info.get("durationSec")
                if duration is None:
                    lines.append(f"  - {name}: {status}")
                else:
                    lines.append(f"  - {name}: {status} ({float(duration):.1f}s)")
        event_tasks = sync.get("tasks", {}) or {}
        if event_tasks:
            lines.append("event_tasks:")
            for task_id, info in sorted(event_tasks.items()):
                task_type = str(info.get("type", "task"))
                status = str(info.get("status", "-"))
                provider = str(info.get("provider", "-"))
                lines.append(f"  - {task_id}: {task_type} {status} via {provider}")
        event_gates = sync.get("gates", {}) or {}
        if event_gates:
            gate_details = ", ".join(
                f"{gate}={'PASS' if info.get('passed') else 'FAIL'}"
                for gate, info in sorted(event_gates.items())
            )
            lines.append(f"event_gates: {gate_details}")
        artifacts = sync.get("artifacts", {}) or {}
        by_kind = artifacts.get("byKind", {}) or {}
        if by_kind:
            kind_details = ", ".join(f"{kind}={count}" for kind, count in sorted(by_kind.items()))
            lines.append(f"event_artifacts: {kind_details}")
        event_escapes = sync.get("escapes", {}) or {}
        if event_escapes:
            lines.append("event_escapes:")
            for phase_name, info in sorted(event_escapes.items()):
                lines.append(
                    f"  - {phase_name}: {str(info.get('reason', '-') or '-')} "
                    f"(provider={str(info.get('provider', '-') or '-')})"
                )
        event_decisions = sync.get("decisions", {}) or {}
        if event_decisions:
            lines.append("event_decisions:")
            for phase_name, info in sorted(event_decisions.items()):
                decision = str(info.get("decision", "-") or "-")
                replan_target = str(info.get("replanTarget", "") or "")
                if replan_target:
                    lines.append(f"  - {phase_name}: {decision} -> {replan_target}")
                else:
                    lines.append(f"  - {phase_name}: {decision}")

    # Team turns section
    team_turns = sync.get("teamTurns", {}) or {}
    if team_turns:
        lines.append("team_turns:")
        for phase_name, phase_turns in sorted(team_turns.items()):
            if not isinstance(phase_turns, dict):
                continue
            for turn_num, info in sorted(phase_turns.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
                status = str(info.get("status", "-"))
                duration = info.get("durationSec")
                workers = info.get("workerCount")
                parts_t: list[str] = [status]
                if duration is not None:
                    parts_t.append(f"{float(duration):.1f}s")
                if workers is not None:
                    parts_t.append(f"{workers} workers")
                lines.append(f"  - {phase_name}/turn-{turn_num}: {', '.join(parts_t)}")

    # Multi-agent section
    multi_agent = (state.get("eventSync", {}) or {}).get("multiAgent")
    if isinstance(multi_agent, dict) and multi_agent.get("verdict"):
        mode = multi_agent.get("mode", "?")
        verdict = multi_agent.get("verdict", "?")
        reason = multi_agent.get("reason", "")
        verdict_icon = "✓" if verdict == "PASS" else "✗"
        lines.append(f"multi_agent: {mode} mode → {verdict_icon} {verdict}" + (f" ({reason})" if reason else ""))
        agents = multi_agent.get("agents", [])
        for a in agents:
            icon = "✓" if a.get("ok") else "✗"
            elapsed = a.get("elapsed_sec")
            elapsed_str = f" ({elapsed:.0f}s)" if elapsed else ""
            lines.append(f"  {icon} {a.get('provider', '?')}/{a.get('role', '?')}{elapsed_str}")

    # §8.7-P1 telemetry: per-phase token + cost totals if recorded.
    telemetry = state.get("telemetry") or {}
    phase_tele = telemetry.get("phases") or {}
    if isinstance(phase_tele, dict) and phase_tele:
        lines.append("telemetry:")
        lines.append("  source: phase_primary_estimated")
        total_in = 0
        total_out = 0
        total_cost = 0.0
        for name, info in sorted(phase_tele.items()):
            if not isinstance(info, dict):
                continue
            in_tok = int(info.get("input_tokens", 0) or 0)
            out_tok = int(info.get("output_tokens", 0) or 0)
            cost = float(info.get("cost_usd", 0) or 0)
            runs = int(info.get("runs", 0) or 0)
            total_in += in_tok
            total_out += out_tok
            total_cost += cost
            lines.append(
                f"  - {name}: in={in_tok:,} out={out_tok:,} cost=${cost:.4f} runs={runs}"
            )
        lines.append(
            f"  total: in={total_in:,} out={total_out:,} cost=${total_cost:.4f}"
        )

    loop = state.get("loop", {}) or {}
    last_escape = loop.get("lastEscape")
    if isinstance(last_escape, dict) and last_escape:
        lines.append(
            "last_escape: "
            f"{last_escape.get('phase', '-')} "
            f"{last_escape.get('reason', '-')}"
        )
    pending_decision = loop.get("pendingDecision")
    if isinstance(pending_decision, dict) and pending_decision:
        lines.append(
            "pending_decision: "
            f"{pending_decision.get('decision', '-')} "
            f"(phase={pending_decision.get('phase', '-')})"
        )
    replan_count = int(loop.get("replanCount", 0) or 0)
    max_replans = int(loop.get("maxReplans", 0) or 0)
    if max_replans > 0:
        remaining = max(0, max_replans - replan_count)
        lines.append(f"loop_budget: {replan_count}/{max_replans} ({remaining} remaining)")
    loop_history = loop.get("history")
    if isinstance(loop_history, list) and loop_history:
        lines.append("loop_history:")
        for item in loop_history[-3:]:
            from_phase = str(item.get("fromPhase", "-") or "-")
            to_phase = item.get("toPhase")
            decision = str(item.get("decision", "-") or "-")
            reason = str(item.get("reason", "") or "")
            at = str(item.get("at", "-") or "-")
            transition = f"{from_phase} -> {to_phase}" if to_phase else from_phase
            if reason:
                lines.append(f"  - {transition} ({decision}, {reason}, {at})")
            else:
                lines.append(f"  - {transition} ({decision}, {at})")

    if repo_root is not None:
        try:
            _append_omp_evidence_panel(
                lines,
                summarize_omp_evidence(repo_root, state),
            )
        except Exception:
            # Status rendering must not turn unreadable evidence into a pass.
            lines.append("omp_evidence: blocked (summary_unavailable)")


    if repo_root is not None:
        cmux = probe_cmux_broker_health(repo_root)
        cmux_status = cmux.get("status", "error")
        detail = cmux.get("detail")
        suffix = f" ({detail})" if detail else ""
        lines.append(f"cmux_broker_health: {cmux_status}{suffix}")
        for key in ("events_log", "sqlite_integrity"):
            sub = cmux.get(key) or {}
            sub_status = sub.get("status")
            if sub_status and sub_status not in _DETAIL_HIDE_STATUSES:
                sub_detail = sub.get("detail", "")
                sub_suffix = f" ({sub_detail})" if sub_detail else ""
                lines.append(f"  {key}: {sub_status}{sub_suffix}")

    return "\n".join(lines)
