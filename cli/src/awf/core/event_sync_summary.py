from __future__ import annotations


def summarize_event_sync(sync: dict | None) -> str | None:
    if not isinstance(sync, dict) or not sync:
        return None
    stage_count = len(sync.get("stages", {}) or {})
    phase_count = len(sync.get("phases", {}) or {})
    worker_count = len(sync.get("workers", {}) or {})
    task_count = len(sync.get("tasks", {}) or {})
    gate_count = len(sync.get("gates", {}) or {})
    escape_count = len(sync.get("escapes", {}) or {})
    decision_count = len(sync.get("decisions", {}) or {})
    team_turns_raw = sync.get("teamTurns", {}) or {}
    team_turn_count = sum(len(v) for v in team_turns_raw.values() if isinstance(v, dict))
    artifacts = sync.get("artifacts", {}) or {}
    created = int(artifacts.get("created", 0) or 0)
    updated = int(artifacts.get("updated", 0) or 0)

    parts: list[str] = []
    if stage_count:
        parts.append(f"stages={stage_count}")
    if phase_count:
        parts.append(f"phases={phase_count}")
    if task_count:
        parts.append(f"tasks={task_count}")
    if worker_count:
        parts.append(f"workers={worker_count}")
    if team_turn_count:
        parts.append(f"team_turns={team_turn_count}")
    if created or updated:
        parts.append(f"artifacts={created}+{updated}u")
    if gate_count:
        parts.append(f"gates={gate_count}")
    if escape_count:
        parts.append(f"escapes={escape_count}")
    if decision_count:
        parts.append(f"decisions={decision_count}")
    if not parts:
        return None
    return "event_summary: " + ", ".join(parts)
