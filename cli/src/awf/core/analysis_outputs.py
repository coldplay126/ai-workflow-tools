from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from awf.core.analysis_state import get_required_output_files
from awf.core.config import AnalysisContext


FILE_MARKER_RE = re.compile(r"^===FILE:\s*(?P<name>[^=]+?)===\s*$", re.MULTILINE)


def parse_stage2_output(raw: str) -> dict[str, str]:
    matches = list(FILE_MARKER_RE.finditer(raw))
    if not matches:
        return {}

    parsed: dict[str, str] = {}
    for index, match in enumerate(matches):
        name = match.group("name").strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        content = raw[start:end].strip()
        parsed[name] = content + ("\n" if content else "")
    return parsed


def write_stage2_outputs(context: AnalysisContext, raw: str) -> list[Path]:
    """Write output files atomically: write all to .tmp first, then move.

    This prevents partial output when interrupted mid-write.
    """
    parsed = parse_stage2_output(raw)
    tmp_dir = context.ai_context_dir / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    required_files = get_required_output_files(context.analysis_mode)

    # Phase 1: write to temp files
    staged: list[tuple[Path, Path]] = []  # (tmp_path, final_path)
    for file_name in required_files:
        content = parsed.get(file_name)
        if content is None:
            continue
        final_path = context.ai_context_dir / file_name
        tmp_path = tmp_dir / f"{file_name}.staging"
        tmp_path.write_text(content, encoding="utf-8")
        staged.append((tmp_path, final_path))

    # Phase 2: atomic move all staged files
    written: list[Path] = []
    for tmp_path, final_path in staged:
        tmp_path.replace(final_path)
        written.append(final_path)

    return written


def generate_analysis_report(context: AnalysisContext, state: dict) -> Path | None:
    """Generate ANALYSIS_REPORT.md from output files + state metadata.

    For document mode: includes endpoint count, tech debt, env vars, circular deps.
    For other modes: generates a minimal report with metadata and file listing.
    """
    ai_dir = context.ai_context_dir
    analysis_mode = context.analysis_mode
    required_files = get_required_output_files(analysis_mode)
    sections: list[str] = []
    sections.append(f"# {context.domain} 도메인 분석 보고서\n")

    # Overview from state
    bundle = state.get("layers", {}).get("bundle", {})
    analyze = state.get("layers", {}).get("analyze", {})
    file_count = bundle.get("fileCount", 0)
    scale = state.get("scale", "unknown")
    stage1_provider = analyze.get("stage1", {}).get("provider", "?")
    stage2_provider = analyze.get("stage2", {}).get("provider", "?")
    sections.append("## 개요\n")
    sections.append("| 항목 | 값 |")
    sections.append("|------|-----|")
    sections.append(f"| 서비스 | {context.service} |")
    sections.append(f"| 도메인 | {context.domain} |")
    sections.append(f"| 분석 모드 | {analysis_mode} |")
    sections.append(f"| 깊이 | {context.mode} |")
    sections.append(f"| 규모 | {scale} |")
    sections.append(f"| 분석 파일 수 | {file_count} |")
    sections.append(f"| Stage 1 프로바이더 | {stage1_provider} |")
    sections.append(f"| Stage 2 프로바이더 | {stage2_provider} |")

    # Document-mode-specific sections
    if analysis_mode == "document":
        api_path = ai_dir / "api-spec.json"
        endpoint_count = 0
        if api_path.exists():
            try:
                import json
                api = json.loads(api_path.read_text(encoding="utf-8"))
                endpoint_count = len(api.get("endpoints", []))
            except Exception:
                pass
        sections.append(f"| API 엔드포인트 수 | {endpoint_count} |")
    sections.append("")

    # Document-mode: extract summaries from generated files
    if analysis_mode == "document":
        overview_path = ai_dir / "domain-overview.md"
        ext_path = ai_dir / "external-integration.md"

        if overview_path.exists():
            overview_text = overview_path.read_text(encoding="utf-8")
            debt_section = _extract_section(overview_text, "알려진 이슈", "기술 부채")
            if debt_section:
                sections.append("## 알려진 이슈 / 기술 부채\n")
                sections.append(debt_section)
                sections.append("")

        if ext_path.exists():
            ext_text = ext_path.read_text(encoding="utf-8")
            env_section = _extract_section(ext_text, "환경변수")
            if env_section:
                sections.append("## 환경변수\n")
                sections.append(env_section)
                sections.append("")
            dep_section = _extract_section(ext_text, "순환 의존성")
            if dep_section:
                sections.append("## 순환 의존성\n")
                sections.append(dep_section)
                sections.append("")

    # Generated files list (all modes)
    sections.append("## 생성된 문서\n")
    for fname in required_files:
        fpath = ai_dir / fname
        if fpath.exists():
            size = fpath.stat().st_size
            sections.append(f"- `{fname}` ({size:,} bytes)")
        else:
            sections.append(f"- `{fname}` (미생성)")
    sections.append("")

    report_text = "\n".join(sections) + "\n"
    report_path = ai_dir / "ANALYSIS_REPORT.md"
    report_path.write_text(report_text, encoding="utf-8")
    return report_path


def _extract_section(text: str, *keywords: str) -> str:
    """Extract a markdown section by heading keyword match."""
    lines = text.splitlines()
    capturing = False
    captured: list[str] = []
    for line in lines:
        if any(kw in line for kw in keywords) and line.strip().startswith("#"):
            capturing = True
            continue
        if capturing and line.strip().startswith("#") and not any(kw in line for kw in keywords):
            break
        if capturing:
            captured.append(line)
    return "\n".join(captured).strip()


def analyze_stage2_output(raw: str, analysis_mode: str = "document") -> dict[str, Any]:
    parsed = parse_stage2_output(raw)
    required = get_required_output_files(analysis_mode)
    written = [file_name for file_name in required if file_name in parsed]
    missing = [file_name for file_name in required if file_name not in parsed]
    extra = sorted([file_name for file_name in parsed if file_name not in required])
    return {
        "parsed": parsed,
        "written_files": written,
        "missing_files": missing,
        "extra_files": extra,
        "has_required_set": not missing,
    }
