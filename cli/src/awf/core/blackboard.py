"""Blackboard — file-based shared workspace for agent team communication.

Manages the .workflow/team/{phase}/ directory structure where workers
communicate indirectly through board/ artifacts and discussion/ files.
Python (TeamRunner) controls turn sequence; workers read/write freely
within their assigned scopes.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class TeamFinding:
    """Structured finding extracted from worker discussion JSON."""

    severity: str  # CRITICAL, HIGH, MAJOR, MEDIUM, LOW, INFO
    category: str  # e.g., constitution_violation, edge_case, bug
    location: str  # e.g., spec.md:FR-003
    description: str
    suggestion: str = ""
    source_role: str = ""
    turn: int = 0
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "location": self.location,
            "description": self.description,
            "suggestion": self.suggestion,
            "source_role": self.source_role,
            "turn": self.turn,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source_role: str = "", turn: int = 0) -> TeamFinding:
        return cls(
            severity=str(data.get("severity", "INFO")).upper(),
            category=str(data.get("category", "")),
            location=str(data.get("location", "")),
            description=str(data.get("description", "")),
            suggestion=str(data.get("suggestion", "")),
            source_role=source_role or str(data.get("source_role", "")),
            turn=turn or int(data.get("turn", 0)),
            timestamp=str(data.get("timestamp", "")) or _now_iso(),
        )

    @property
    def is_critical(self) -> bool:
        return self.severity in {"CRITICAL", "HIGH"}

    @property
    def is_major(self) -> bool:
        return self.severity in {"MAJOR", "MEDIUM"}


@dataclass
class Blackboard:
    """File-based shared workspace for agent team workers.

    Directory layout::

        .workflow/team/{phase}/
        ├── mission.md
        ├── meta.json
        ├── board/          ← shared artifacts (spec.md, plan.md, ...)
        └── discussion/     ← per-turn worker files (turn-N-{role}.md|json)
    """

    root: Path
    phase: str
    board_dir: Path
    discussion_dir: Path
    mission_path: Path
    meta_path: Path
    created_at: str = ""
    _write_scopes: dict[str, list[str]] = field(default_factory=dict)

    # --- Factory ---

    @staticmethod
    def create(cwd: str, phase: str, *, team_config: dict | None = None) -> Blackboard:
        """Create workspace directory structure and return a Blackboard instance.

        Args:
            cwd: Project root (contains .workflow/).
            phase: Workflow phase name (plan, impl, test, ...).
            team_config: Optional team configuration from provider-config.json.
                         Used to set write scopes per role.
        """
        workspace = Path(cwd) / ".workflow" / "team" / phase
        board_dir = workspace / "board"
        discussion_dir = workspace / "discussion"

        board_dir.mkdir(parents=True, exist_ok=True)
        discussion_dir.mkdir(parents=True, exist_ok=True)

        now = _now_iso()
        meta_path = workspace / "meta.json"

        # Load existing meta or create new
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            created_at = meta.get("created_at", now)
        else:
            created_at = now
            meta = {
                "phase": phase,
                "created_at": created_at,
                "last_turn": 0,
            }

        # Extract write scopes from team config; preserve existing only when None
        if team_config is not None:
            write_scopes: dict[str, list[str]] = {}
            for role_cfg in team_config.get("roles", []):
                role_id = role_cfg.get("id", "")
                if role_id and role_cfg.get("write_scope"):
                    write_scopes[role_id] = list(role_cfg["write_scope"])
            meta["write_scopes"] = write_scopes
        else:
            write_scopes = meta.get("write_scopes", {})
        _atomic_write_json(meta_path, meta)

        return Blackboard(
            root=workspace,
            phase=phase,
            board_dir=board_dir,
            discussion_dir=discussion_dir,
            mission_path=workspace / "mission.md",
            meta_path=meta_path,
            created_at=created_at,
            _write_scopes=write_scopes,
        )

    @staticmethod
    def load(cwd: str, phase: str) -> Blackboard:
        """Load an existing workspace. Raises FileNotFoundError if not present."""
        workspace = Path(cwd) / ".workflow" / "team" / phase
        meta_path = workspace / "meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"No team workspace for phase '{phase}': {meta_path}")

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return Blackboard(
            root=workspace,
            phase=phase,
            board_dir=workspace / "board",
            discussion_dir=workspace / "discussion",
            mission_path=workspace / "mission.md",
            meta_path=meta_path,
            created_at=meta.get("created_at", ""),
            _write_scopes=meta.get("write_scopes", {}),
        )

    # --- Mission ---

    def write_mission(self, content: str, turn: int = 1) -> Path:
        """Write or update the mission file with leader's task definition."""
        self.mission_path.write_text(content, encoding="utf-8")
        self._update_meta(last_turn=turn)
        return self.mission_path

    def read_mission(self) -> str:
        """Read the current mission content."""
        if self.mission_path.exists():
            return self.mission_path.read_text(encoding="utf-8")
        return ""

    # --- Discussion files ---

    def discussion_path(self, turn: int, role: str, fmt: str = "json") -> Path:
        """Return path for a worker's discussion file.

        Convention: turn-{N}-{role}.{md|json}
        """
        return self.discussion_dir / f"turn-{turn}-{role}.{fmt}"

    def write_discussion(self, turn: int, role: str, content: str, fmt: str = "md") -> Path:
        """Write a worker's discussion markdown file."""
        path = self.discussion_path(turn, role, fmt)
        path.write_text(content, encoding="utf-8")
        self._update_meta(last_turn=max(turn, self.last_turn))
        return path

    def write_findings(self, turn: int, role: str, findings_data: dict[str, Any]) -> Path:
        """Write a worker's structured findings as JSON."""
        path = self.discussion_path(turn, role, "json")
        _atomic_write_json(path, findings_data)
        self._update_meta(last_turn=max(turn, self.last_turn))
        return path

    # --- Findings collection ---

    def collect_findings(self, turn: int) -> list[TeamFinding]:
        """Parse all findings from a specific turn's JSON files."""
        findings: list[TeamFinding] = []
        pattern = f"turn-{turn}-*.json"
        for json_path in sorted(self.discussion_dir.glob(pattern)):
            role = _extract_role_from_filename(json_path.name, turn)
            for f in _safe_extract_findings(json_path):
                findings.append(TeamFinding.from_dict(f, source_role=role, turn=turn))
        return findings

    def collect_all_findings(self) -> list[TeamFinding]:
        """Collect findings across all turns."""
        findings: list[TeamFinding] = []
        for json_path in sorted(self.discussion_dir.glob("turn-*-*.json")):
            turn, role = _parse_discussion_filename(json_path.name)
            if turn < 1:
                continue
            for f in _safe_extract_findings(json_path):
                findings.append(TeamFinding.from_dict(f, source_role=role, turn=turn))
        return findings

    def has_critical_findings(self, turn: int) -> bool:
        """Check if any CRITICAL/HIGH findings exist in a turn."""
        return any(f.is_critical for f in self.collect_findings(turn))

    def major_count(self, turn: int) -> int:
        """Count MAJOR/MEDIUM findings in a turn."""
        return sum(1 for f in self.collect_findings(turn) if f.is_major)

    def evaluate_termination(self, turn: int) -> tuple[bool, str]:
        """Evaluate deterministic termination conditions for a turn.

        Returns (should_stop, reason).
        Uses the same Judge Rules as multi_agent.judge():
        1. Any CRITICAL/HIGH → FAIL (continue)
        2. MAJOR/MEDIUM >= 2 (deduped by category:location) → FAIL (continue)
        3. Otherwise → PASS (can stop)
        """
        findings = self.collect_findings(turn)
        if not findings:
            return True, "no_findings"

        if any(f.is_critical for f in findings):
            return False, "critical_findings"

        # Dedup by (category, location), keep highest severity — matches multi_agent.judge()
        _severity_rank = {"CRITICAL": 4, "HIGH": 3, "MAJOR": 2, "MEDIUM": 1, "LOW": 0, "INFO": 0}
        seen: dict[str, str] = {}
        for f in findings:
            key = f"{f.category}:{f.location}"
            existing = seen.get(key)
            if existing is None or _severity_rank.get(f.severity, 0) > _severity_rank.get(existing, 0):
                seen[key] = f.severity
        major = sum(1 for sev in seen.values() if sev in {"MAJOR", "MEDIUM"})
        if major >= 2:
            return False, f"major_findings({major})"

        return True, "pass"

    # --- Board artifacts ---

    def get_artifact_path(self, name: str) -> Path:
        """Return path for a named artifact in board/."""
        return self.board_dir / name

    def list_board_artifacts(self) -> list[Path]:
        """List all files in board/ sorted by name."""
        if not self.board_dir.exists():
            return []
        return sorted(p for p in self.board_dir.iterdir() if p.is_file())

    def read_artifact(self, name: str) -> str:
        """Read a board artifact. Returns empty string if not found."""
        path = self.board_dir / name
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    # --- Write scope validation ---

    def validate_write_scope(self, role: str, target_path: Path) -> bool:
        """Check if a role is allowed to write to the target path.

        If no write_scopes are configured, all writes are allowed (permissive default).
        Resolves paths before comparison to prevent '..' traversal escape.
        """
        scopes = self._write_scopes.get(role)
        if scopes is None:
            return True  # no restriction configured

        # repo root = .workflow/team/{phase} → up 3 levels
        repo_root = self.root.parent.parent.parent.resolve()
        resolved = target_path.resolve()
        try:
            rel = str(resolved.relative_to(repo_root))
        except ValueError:
            return False  # outside repo root → deny
        return any(fnmatch(rel, scope) for scope in scopes)

    # --- Metadata ---

    @property
    def last_turn(self) -> int:
        """Return the last completed turn number."""
        if self.meta_path.exists():
            try:
                meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
                return int(meta.get("last_turn", 0))
            except (json.JSONDecodeError, OSError):
                pass
        return 0

    def _update_meta(self, **updates: Any) -> None:
        """Update meta.json with given key-value pairs."""
        meta: dict[str, Any] = {}
        if self.meta_path.exists():
            try:
                meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                meta = {}
        meta.update(updates)
        meta["updated_at"] = _now_iso()
        _atomic_write_json(self.meta_path, meta)

    def summary(self) -> dict[str, Any]:
        """Return workspace summary for logging/state tracking."""
        artifacts = [p.name for p in self.list_board_artifacts()]
        all_findings = self.collect_all_findings()
        return {
            "phase": self.phase,
            "workspace": str(self.root),
            "last_turn": self.last_turn,
            "artifacts": artifacts,
            "findings_total": len(all_findings),
            "findings_critical": sum(1 for f in all_findings if f.is_critical),
            "findings_major": sum(1 for f in all_findings if f.is_major),
        }

    # --- Lifecycle ---

    def cleanup(self) -> None:
        """Remove the entire workspace directory."""
        if self.root.exists():
            shutil.rmtree(self.root)


# --- Helpers ---


def _safe_extract_findings(json_path: Path) -> list[dict[str, Any]]:
    """Read a discussion JSON file and return its findings list defensively.

    Handles: bad JSON syntax, non-dict root, non-list findings, non-dict entries.
    """
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("findings")
    if not isinstance(raw, list):
        return []
    return [f for f in raw if isinstance(f, dict)]


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically via tmp-file + rename (matches state.py pattern)."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _extract_role_from_filename(filename: str, turn: int) -> str:
    """Extract role from 'turn-{N}-{role}.json' filename."""
    prefix = f"turn-{turn}-"
    if filename.startswith(prefix):
        return filename[len(prefix):].rsplit(".", 1)[0]
    return ""


def _parse_discussion_filename(filename: str) -> tuple[int, str]:
    """Parse 'turn-{N}-{role}.{ext}' → (turn, role). Returns (0, '') on failure."""
    # turn-1-spec_writer.json → parts = ["turn", "1", "spec_writer.json"]
    parts = filename.split("-", 2)
    if len(parts) < 3 or parts[0] != "turn":
        return 0, ""
    try:
        turn = int(parts[1])
    except ValueError:
        return 0, ""
    role = parts[2].rsplit(".", 1)[0]
    return turn, role
