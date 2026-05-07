"""Work History: preserve WF session context across sessions.

Each `awf wf init` creates a timestamped directory under `.work_history/`
with concept, plan, review reports, and decisions.  Phase artifacts are
copied as they complete so future sessions can reference prior work.
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _slugify(text: str, max_len: int = 40) -> str:
    """Convert concept text to a filesystem-safe slug."""
    slug = re.sub(r"[^a-zA-Z0-9가-힣\s-]", "", text)
    slug = re.sub(r"\s+", "-", slug).strip("-").lower()
    return slug[:max_len].rstrip("-") or "session"


def create_work_history_session(
    repo_root: Path,
    concept: str,
    *,
    workflow_id: str = "",
) -> Path:
    """Create a new .work_history session directory with concept.md.

    Returns the session directory path.
    """
    history_dir = repo_root / ".work_history"
    history_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slug = _slugify(concept)
    session_name = f"{date_str}-{slug}"

    # Avoid collision
    session_dir = history_dir / session_name
    counter = 1
    while session_dir.exists():
        session_dir = history_dir / f"{session_name}-{counter}"
        counter += 1

    session_dir.mkdir(parents=True, exist_ok=True)

    # Write concept.md
    concept_md = session_dir / "concept.md"
    concept_md.write_text(
        f"# {concept}\n\n"
        f"- created: {datetime.now(timezone.utc).isoformat()}\n"
        f"- workflow_id: {workflow_id}\n",
        encoding="utf-8",
    )
    return session_dir


def find_current_session(repo_root: Path) -> Optional[Path]:
    """Find the most recent .work_history session directory."""
    history_dir = repo_root / ".work_history"
    if not history_dir.is_dir():
        return None
    sessions = sorted(
        (d for d in history_dir.iterdir() if d.is_dir() and not d.name.startswith(".")),
        key=lambda d: d.name,
        reverse=True,
    )
    return sessions[0] if sessions else None


def copy_phase_artifact(
    session_dir: Path,
    phase: str,
    source_path: Path,
    *,
    target_name: str = "",
) -> Optional[Path]:
    """Copy a phase artifact to the work_history session.

    Returns the destination path, or None if source doesn't exist.
    """
    if not source_path.is_file():
        return None
    dest_name = target_name or f"{phase}-{source_path.name}"
    dest = session_dir / dest_name
    shutil.copy2(source_path, dest)
    return dest


def record_phase_completion(
    repo_root: Path,
    phase: str,
) -> list[Path]:
    """Copy relevant artifacts for a completed phase to work_history.

    Returns list of copied file paths.
    """
    session_dir = find_current_session(repo_root)
    if not session_dir:
        return []

    wf_dir = repo_root / ".workflow"
    copied: list[Path] = []

    # Phase-specific artifact mapping
    artifact_map: dict[str, list[tuple[str, str]]] = {
        "plan": [
            ("artifacts/spec.md", "plan.md"),
            ("artifacts/tasks.md", "tasks.md"),
        ],
        "review": [
            ("tmp/result-review-codex.txt", "review-codex.txt"),
            ("tmp/result-review-claude_code.txt", "review-claude.txt"),
        ],
        "impl": [
            ("artifacts/impl-log.md", "impl-log.md"),
        ],
        "verify": [
            ("tmp/result-verify-codex.txt", "verify-codex.txt"),
            ("tmp/result-verify-claude_code.txt", "verify-claude.txt"),
        ],
        "test": [
            ("artifacts/test-report.md", "test-report.md"),
        ],
        "done": [
            ("artifacts/summary.md", "summary.md"),
        ],
    }

    for source_rel, dest_name in artifact_map.get(phase, []):
        source = wf_dir / source_rel
        result = copy_phase_artifact(session_dir, phase, source, target_name=dest_name)
        if result:
            copied.append(result)

    # Auto-track gaps from verify/review findings
    if phase in ("verify", "review"):
        try:
            from awf.core.gap_tracker import track_gaps_from_findings
            _track_phase_gaps(repo_root, phase)
        except Exception as exc:
            print(f"warning: gap tracking failed: {exc}", file=sys.stderr)

    # Auto-update docs/status/ on workflow completion
    if phase == "done":
        try:
            from awf.core.status_updater import update_status_on_done
            update_status_on_done(repo_root)
        except Exception as exc:
            print(f"warning: status update failed: {exc}", file=sys.stderr)

    # Copy multi-agent results if present
    multi_agent_dir = wf_dir / "tmp" / "multi-agent"
    if multi_agent_dir.is_dir():
        dest_ma_dir = session_dir / "multi-agent"
        dest_ma_dir.mkdir(exist_ok=True)
        for f in multi_agent_dir.iterdir():
            if f.is_file():
                dest = dest_ma_dir / f.name
                if not dest.exists():
                    shutil.copy2(f, dest)
                    copied.append(dest)

    return copied


def _track_phase_gaps(repo_root: Path, phase: str) -> None:
    """Extract findings from phase result files and track as gaps."""
    import json as _json
    from awf.core.gap_tracker import track_gaps_from_findings

    wf_dir = repo_root / ".workflow"
    # Look for result files from multi-agent runs
    result_patterns = [
        wf_dir / "tmp" / f"result-{phase}-codex.txt",
        wf_dir / "tmp" / f"result-{phase}-claude_code.txt",
    ]

    all_findings: list[dict] = []
    for result_file in result_patterns:
        if not result_file.is_file():
            continue
        try:
            text = result_file.read_text(encoding="utf-8").strip()
            # Try to parse as JSON (agent output format)
            data = _json.loads(text)
            findings = data.get("findings", [])
            all_findings.extend(findings)
        except (_json.JSONDecodeError, OSError):
            continue

    if all_findings:
        # Determine area from manifest
        manifest_path = wf_dir / "manifest.json"
        area = "workflow"
        if manifest_path.is_file():
            try:
                manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
                area = manifest.get("area", "workflow")
            except (_json.JSONDecodeError, OSError):
                pass

        track_gaps_from_findings(
            all_findings, phase=phase, area=area, repo_root=repo_root,
        )


def list_sessions(repo_root: Path) -> list[dict]:
    """List all work_history sessions with basic metadata."""
    history_dir = repo_root / ".work_history"
    if not history_dir.is_dir():
        return []

    sessions: list[dict] = []
    for d in sorted(history_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        concept_path = d / "concept.md"
        concept_text = ""
        if concept_path.is_file():
            first_line = concept_path.read_text(encoding="utf-8", errors="ignore").split("\n")[0]
            concept_text = first_line.lstrip("# ").strip()

        artifacts = [f.name for f in d.iterdir() if f.is_file() and f.name != "concept.md"]
        sessions.append({
            "name": d.name,
            "path": str(d),
            "concept": concept_text,
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
        })
    return sessions
