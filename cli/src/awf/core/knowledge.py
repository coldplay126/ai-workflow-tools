"""K6: Domain Knowledge Accumulation.

After analysis completes, extract key findings from .ai-context outputs
and accumulate them into a service-level project-context.md.

This builds a persistent knowledge base that grows with each analysis,
similar to sample-service's `.claude/project-context.md`.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from awf.core.config import AnalysisContext


# Sections in project-context.md
_HEADER_TEMPLATE = """# {service} - Project Context

이 파일은 `awf analyze` 결과에서 자동 추출된 도메인 지식입니다.
분석이 완료될 때마다 해당 unit의 핵심 정보가 갱신됩니다.

마지막 갱신: {updated_at}

"""

_UNIT_SECTION_TEMPLATE = """### {unit_name}
{summary}
- 소스: `{directories}`
- 엔드포인트: {endpoint_count}개
- 분석일: {analyzed_at}
"""


def _extract_unit_summary(context: AnalysisContext) -> Optional[dict]:
    """Extract key knowledge from completed .ai-context outputs."""
    overview_path = context.ai_context_dir / "domain-overview.md"
    api_spec_path = context.ai_context_dir / "api-spec.json"

    if not overview_path.is_file():
        return None

    # Extract summary from domain-overview.md (first paragraph after # heading)
    overview_text = overview_path.read_text(encoding="utf-8", errors="ignore")
    summary_lines: list[str] = []
    in_summary = False
    for line in overview_text.splitlines():
        if line.startswith("## 목적") or line.startswith("## Purpose"):
            in_summary = True
            continue
        if in_summary:
            if line.startswith("## "):
                break
            stripped = line.strip()
            if stripped:
                summary_lines.append(stripped)

    if not summary_lines:
        # Fallback: first non-heading non-empty lines
        for line in overview_text.splitlines():
            if line.startswith("#"):
                continue
            stripped = line.strip()
            if stripped:
                summary_lines.append(stripped)
                if len(summary_lines) >= 3:
                    break

    # Extract endpoint count from api-spec.json
    endpoint_count = 0
    if api_spec_path.is_file():
        try:
            spec = json.loads(api_spec_path.read_text(encoding="utf-8"))
            endpoints = spec.get("endpoints", [])
            endpoint_count = len(endpoints) if isinstance(endpoints, list) else 0
        except (json.JSONDecodeError, OSError):
            pass

    return {
        "unit_name": context.domain,
        "summary": "\n".join(summary_lines[:5]) if summary_lines else "(요약 없음)",
        "directories": ", ".join(context.domain_directories[:3]),
        "endpoint_count": endpoint_count,
        "analyzed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def _parse_existing_context(content: str) -> dict[str, str]:
    """Parse existing project-context.md into unit sections."""
    units: dict[str, str] = {}
    current_unit: str = ""
    current_lines: list[str] = []

    for line in content.splitlines():
        if line.startswith("### "):
            if current_unit and current_lines:
                units[current_unit] = "\n".join(current_lines)
            current_unit = line[4:].strip()
            current_lines = [line]
        elif current_unit:
            current_lines.append(line)

    if current_unit and current_lines:
        units[current_unit] = "\n".join(current_lines)

    return units


def _rebuild_context(
    service: str,
    units: dict[str, str],
) -> str:
    """Rebuild project-context.md from unit sections."""
    header = _HEADER_TEMPLATE.format(
        service=service,
        updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )
    sections = [header, "## 주요 도메인\n"]
    for name in sorted(units):
        sections.append(units[name])
        sections.append("")
    return "\n".join(sections)


def accumulate_knowledge(context: AnalysisContext) -> Optional[Path]:
    """Extract knowledge from completed analysis and update project-context.md.

    Called after successful analysis completion. Updates the service-level
    project-context.md in analysis-docs/{service}/.

    Returns the path to updated project-context.md, or None if extraction failed.
    """
    unit_data = _extract_unit_summary(context)
    if not unit_data:
        return None

    # project-context.md lives at analysis-docs/{service}/project-context.md
    service_dir = context.docs_root / context.service
    service_dir.mkdir(parents=True, exist_ok=True)
    context_path = service_dir / "project-context.md"

    # Load existing or start fresh
    existing_units: dict[str, str] = {}
    if context_path.is_file():
        existing_units = _parse_existing_context(
            context_path.read_text(encoding="utf-8", errors="ignore")
        )

    # Build new unit section
    new_section = _UNIT_SECTION_TEMPLATE.format(**unit_data).strip()
    existing_units[unit_data["unit_name"]] = new_section

    # Write back
    content = _rebuild_context(context.service, existing_units)
    context_path.write_text(content, encoding="utf-8")
    return context_path
