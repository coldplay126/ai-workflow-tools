from __future__ import annotations

from awf.core.event_sync_summary import summarize_event_sync


def summarize_analysis_state(state: dict) -> str:
    lines = [
        f"id: {state.get('id', '-')}",
        f"service: {state.get('service', '-')}",
        f"domain: {state.get('domain', '-')}",
        f"mode: {state.get('mode', '-')}",
        f"scale: {state.get('scale', '-')}",
        f"current_layer: {state.get('currentLayer', '-')}",
        f"current_stage: {state.get('currentStage', '-')}",
    ]
    layers = state.get("layers", {}) or {}
    output = layers.get("output", {}) or {}
    lines.append(f"output_status: {output.get('status', '-')}")

    analyze_layers = layers.get("analyze", {}) or {}
    if analyze_layers:
        lines.append("analyze_stages:")
        for stage_name in ("stage1", "stage2", "stage3"):
            info = analyze_layers.get(stage_name, {}) or {}
            status = info.get("status", "-")
            provider = info.get("provider", "")
            if provider:
                lines.append(f"  - {stage_name}: {status} via {provider}")
            else:
                lines.append(f"  - {stage_name}: {status}")

    event_summary = summarize_event_sync(state.get("eventSync"))
    if event_summary:
        lines.append(event_summary)
        sync = state.get("eventSync", {}) or {}
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
        workers = sync.get("workers", {}) or {}
        if workers:
            lines.append("event_workers:")
            for worker_id, info in sorted(workers.items()):
                role = str(info.get("role", "") or "").strip()
                status = str(info.get("status", "-"))
                suffix = f" ({role})" if role else ""
                lines.append(f"  - {worker_id}: {status}{suffix}")
        tasks = sync.get("tasks", {}) or {}
        if tasks:
            lines.append("event_tasks:")
            for task_id, info in sorted(tasks.items()):
                task_type = str(info.get("type", "task"))
                status = str(info.get("status", "-"))
                provider = str(info.get("provider", "-"))
                lines.append(f"  - {task_id}: {task_type} {status} via {provider}")
        artifacts = (sync.get("artifacts", {}) or {}).get("byKind", {}) or {}
        if artifacts:
            details = ", ".join(f"{kind}={count}" for kind, count in sorted(artifacts.items()))
            lines.append(f"event_artifacts: {details}")

    return "\n".join(lines)
