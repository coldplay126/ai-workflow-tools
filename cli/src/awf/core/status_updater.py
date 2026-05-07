"""Auto-update docs/status/ when a workflow completes (Phase 7: done).

Appends a completion entry to the relevant status document based on
the workflow's manifest and artifacts.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path


def update_status_on_done(repo_root: Path) -> str | None:
    """Append workflow completion entry to docs/status/.

    Reads .workflow/manifest.json and artifacts to determine:
    - Which status document to update
    - What was implemented (from tasks.md)
    - Gate results (from state.json)

    Returns the updated file path, or None if nothing to update.
    """
    wf_dir = repo_root / ".workflow"
    if not wf_dir.is_dir():
        return None

    # Read manifest for project context
    manifest_path = wf_dir / "manifest.json"
    manifest: dict = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    # Read state for gate results
    state_path = wf_dir / "state.json"
    state: dict = {}
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    # Determine concept/feature name
    concept = manifest.get("concept", state.get("concept", "unknown"))

    # Read completed tasks from tasks.md
    tasks_path = wf_dir / "artifacts" / "tasks.md"
    completed_tasks = _extract_completed_tasks(tasks_path)

    # Read gate history
    history = state.get("history", [])
    gate_summary = _summarize_gates(history)

    # Determine target status file
    status_dir = repo_root / "docs" / "status"
    status_dir.mkdir(parents=True, exist_ok=True)

    # Use manifest's area or default to "general"
    area = manifest.get("area", "general")
    status_file = status_dir / f"{area}.md"

    # If no specific area file, append to a workflow-log
    if not status_file.is_file():
        status_file = status_dir / "workflow-completions.md"

    entry = _format_entry(concept, completed_tasks, gate_summary)

    # Append to file
    if status_file.is_file():
        existing = status_file.read_text(encoding="utf-8")
    else:
        existing = f"# Status: Workflow Completions\n\nAutomatically updated when workflows complete.\n"

    status_file.write_text(existing.rstrip() + "\n\n" + entry + "\n", encoding="utf-8")
    print(f"status updated: {status_file.relative_to(repo_root)}", file=sys.stderr)
    return str(status_file)


def _extract_completed_tasks(tasks_path: Path) -> list[str]:
    """Extract completed task lines from tasks.md."""
    if not tasks_path.is_file():
        return []
    tasks = []
    for line in tasks_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- [X]") or stripped.startswith("- [x]"):
            # Remove checkbox prefix
            task = stripped[5:].strip()
            tasks.append(task)
    return tasks


def _summarize_gates(history: list[dict]) -> list[str]:
    """Summarize gate results from workflow history."""
    summaries = []
    for entry in history:
        phase = entry.get("phase", "?")
        action = entry.get("action", "?")
        if "gate" in action.lower() or action in ("pass", "fail", "skip"):
            summaries.append(f"{phase}: {action}")
    return summaries


def _format_entry(concept: str, tasks: list[str], gates: list[str]) -> str:
    """Format a status completion entry."""
    today = date.today().isoformat()
    lines = [
        f"## [{today}] {concept}",
        "",
    ]
    if tasks:
        lines.append(f"**Completed tasks** ({len(tasks)}):")
        for t in tasks[:20]:  # cap at 20
            lines.append(f"- {t}")
        if len(tasks) > 20:
            lines.append(f"- ... and {len(tasks) - 20} more")
        lines.append("")
    if gates:
        lines.append("**Gate results**: " + " → ".join(gates))
        lines.append("")
    return "\n".join(lines)
