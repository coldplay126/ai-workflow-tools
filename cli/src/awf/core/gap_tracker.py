"""Auto-track gaps from agent verification findings.

When spec-verifier or artifact-reviewer report FAIL findings,
this module appends new gap entries to docs/gaps/.
When tests pass, it can mark gaps as fixed.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any


def track_gaps_from_findings(
    findings: list[dict[str, Any]],
    *,
    phase: str,
    area: str = "workflow",
    repo_root: Path,
) -> list[str]:
    """Create gap entries from CRITICAL/HIGH findings.

    Returns list of new gap IDs created.
    """
    # Only track significant findings
    significant = [
        f for f in findings
        if str(f.get("severity", "")).upper() in {"CRITICAL", "HIGH"}
    ]
    if not significant:
        return []

    gaps_dir = repo_root / "docs" / "gaps"
    gaps_dir.mkdir(parents=True, exist_ok=True)

    gap_file = gaps_dir / f"{area}.md"
    existing = _read_existing_gaps(gap_file)
    next_num = _next_gap_number(existing, area)

    new_ids: list[str] = []
    entries: list[str] = []

    for finding in significant:
        gap_id = f"GAP-{area.upper()[:4]}-{next_num:03d}"
        next_num += 1

        # Skip if a very similar gap already exists
        if _is_duplicate(finding, existing):
            continue

        entry = _format_gap_entry(gap_id, finding, phase)
        entries.append(entry)
        new_ids.append(gap_id)

    if not entries:
        return []

    # Append to file
    if gap_file.is_file():
        content = gap_file.read_text(encoding="utf-8")
    else:
        content = (
            f"# {area.title()} — Gap Inventory\n\n"
            f"## Purpose\n\n"
            f"이 문서는 `{area}` 영역의 설계 대비 구현 차이를 추적한다.\n"
            f"자동 생성: agent verification findings에서 CRITICAL/HIGH 항목.\n\n"
            f"## Gap States\n\n"
            f"- `open` → `in_progress` → `fixed` | `accepted_deviation`\n\n"
            f"## Gap Entries\n"
        )

    content = content.rstrip() + "\n\n" + "\n\n".join(entries) + "\n"
    gap_file.write_text(content, encoding="utf-8")
    print(f"gaps tracked: {len(new_ids)} new in {gap_file.relative_to(repo_root)}", file=sys.stderr)
    return new_ids


def mark_gap_fixed(
    gap_id: str,
    *,
    resolution: str,
    test_id: str = "",
    repo_root: Path,
) -> bool:
    """Mark a gap as fixed following docs/templates/gap.md closing rule.

    Closing rule requires: implementation complete + status updated +
    test added/updated + test passing. This function records the resolution
    and links the test_id.

    Searches all docs/gaps/*.md files for the gap ID and updates state.
    Returns True if found and updated.
    """
    gaps_dir = repo_root / "docs" / "gaps"
    if not gaps_dir.is_dir():
        return False

    today = date.today().isoformat()

    for gap_file in gaps_dir.glob("*.md"):
        content = gap_file.read_text(encoding="utf-8")
        if gap_id not in content:
            continue

        # Update State: open → fixed
        updated = content.replace(
            f"- State: open",
            f"- State: fixed",
            1,  # only first occurrence near this gap
        )
        # Update Resolution plan → actual resolution
        updated = updated.replace(
            f"- Resolution plan: (pending)",
            f"- Resolution plan: {resolution}",
        )
        # Update Test id if provided
        if test_id:
            updated = updated.replace(
                f"- Test id: (pending)",
                f"- Test id: {test_id}",
            )
        # Append fixed metadata
        # Find the gap entry and append after last field
        gap_header = f"### {gap_id}"
        idx = updated.find(gap_header)
        if idx >= 0:
            # Find the end of this entry (next ### or end of file)
            next_entry = updated.find("\n### ", idx + len(gap_header))
            insert_pos = next_entry if next_entry > 0 else len(updated)
            fixed_line = f"\n- Fixed: {today}\n- Resolution: {resolution}\n"
            updated = updated[:insert_pos] + fixed_line + updated[insert_pos:]

        if updated != content:
            gap_file.write_text(updated, encoding="utf-8")
            print(f"gap {gap_id} marked fixed", file=sys.stderr)
            return True

    return False


def _read_existing_gaps(gap_file: Path) -> list[dict]:
    """Parse existing gap entries from file (simplified)."""
    if not gap_file.is_file():
        return []

    content = gap_file.read_text(encoding="utf-8")
    gaps = []
    for match in re.finditer(r"### (GAP-\w+-\d+)", content):
        gap_id = match.group(1)
        # Extract summary line after gap header
        start = match.end()
        block = content[start:start + 500]
        summary_match = re.search(r"Summary:\s*(.+)", block)
        summary = summary_match.group(1).strip() if summary_match else ""
        gaps.append({"id": gap_id, "summary": summary})

    return gaps


def _next_gap_number(existing: list[dict], area: str) -> int:
    """Determine next gap number for area prefix."""
    prefix = f"GAP-{area.upper()[:4]}-"
    max_num = 0
    for gap in existing:
        gap_id = gap.get("id", "")
        if gap_id.startswith(prefix):
            try:
                num = int(gap_id[len(prefix):])
                max_num = max(max_num, num)
            except ValueError:
                pass
    return max_num + 1


def _is_duplicate(finding: dict, existing: list[dict]) -> bool:
    """Check if a finding already has a similar gap entry."""
    desc = str(finding.get("description", "")).lower()
    loc = str(finding.get("location", "")).lower()
    for gap in existing:
        summary = gap.get("summary", "").lower()
        # Simple similarity: same location mentioned in summary
        if loc and loc in summary:
            return True
        # Or very similar description (first 40 chars match)
        if desc[:40] and desc[:40] in summary:
            return True
    return False


def _format_gap_entry(gap_id: str, finding: dict, phase: str) -> str:
    """Format a single gap entry following docs/templates/gap.md structure.

    Template fields: Area, Pattern reference, Status reference, Summary,
    Why it matters, Expected behavior, Current behavior, Severity, State,
    Owner, Test id, Resolution plan.
    """
    severity = finding.get("severity", "?").lower()
    category = finding.get("category", "?")
    location = finding.get("location", "?")
    description = finding.get("description", "")
    suggestion = finding.get("suggestion", "")
    today = date.today().isoformat()

    lines = [
        f"### {gap_id} — {description[:80]}",
        "",
        f"- Area: {category.split('_')[0] if '_' in category else phase}",
        f"- Pattern reference: (auto-detected from {phase} phase)",
        f"- Status reference: (pending manual link)",
        f"- Summary: {description}",
        f"- Why it matters: {phase} phase에서 {severity} severity finding으로 탐지됨",
        f"- Expected behavior: {suggestion}" if suggestion else "- Expected behavior: (agent suggestion 없음)",
        f"- Current behavior: `{location}` — {description}",
        f"- Severity: {severity}",
        f"- State: open",
        f"- Owner: unassigned",
        f"- Test id: (pending)",
        f"- Resolution plan: {suggestion}" if suggestion else "- Resolution plan: (pending)",
        f"- Detected: {today}",
        f"- Source: {phase}/{category}",
    ]

    return "\n".join(lines)
