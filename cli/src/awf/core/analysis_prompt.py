from __future__ import annotations

import json
from pathlib import Path

from awf.core.config import AnalysisContext
from awf.core.markdown_frontmatter import read_markdown_body
from awf.tools.file_ops import FileOpsToolset

FANOUT_TARGETS = [
    ("api", "api-spec.json", "API Specification"),
    ("data", "data-model.md", "Data Model"),
    ("domain", "domain-overview.md", "Domain Overview"),
    ("integration", "external-integration.md", "External Integration"),
]

REFERENCE_FILE_ORDER = [
    "domain-overview.md",
    "external-integration.md",
    "api-spec.json",
    "data-model.md",
]
DEFAULT_REFERENCE_POLICY = {
    "max_documents": 5,
    "max_tokens": 8000,
    "require_reason": True,
}


def build_prompt(context: AnalysisContext, execution_mode: str | None = None, native: bool = False) -> str:
    from awf.core.spec_loader import load_prompt, load_prompt_optional

    if native:
        existing_section = _build_native_ai_context_ref(context)
    else:
        existing_section = _build_existing_ai_context_section(context)
    prompt = load_prompt(
        "analysis", "stage2",
        service=context.service,
        unit=context.domain,
        existing_ai_context_section=existing_section,
    )

    # Append format reference and quality checklist as separate sections
    format_ref = load_prompt_optional("analysis", "stage2-format", unit=context.domain) or ""
    quality_ref = load_prompt_optional("analysis", "stage2-quality") or ""
    if format_ref:
        prompt += "\n" + format_ref
    if quality_ref:
        prompt += "\n" + quality_ref

    mode_note = ""
    if execution_mode:
        mode_note = load_prompt_optional("analysis", f"mode-{execution_mode}") or ""

    return prompt + ("\n" + mode_note if mode_note else "")


def _determine_scale(file_count: int) -> str:
    """Determine domain scale per §2.1: small ≤10, standard 11-30, large 31+."""
    if file_count <= 10:
        return "small"
    elif file_count <= 30:
        return "standard"
    return "large"


def build_domain_bundle(
    context: AnalysisContext,
    domain_files: list[dict[str, str]],
    file_analyses: list[dict] | None = None,
    precomputed_context: dict[str, str] | None = None,
    observations: list[dict] | None = None,
) -> str:
    """Build domain bundle with scale-based content policy (§2.2).

    - small (≤10 files): observation + full code
    - standard/large (>10 files): observation only (no code)

    When observations is provided, uses v3 observation-based bundle.
    Otherwise falls back to v2 behavior (always includes code).
    """
    toolset = FileOpsToolset(context.github_root)
    target_paths = {entry.get("path", "") for entry in domain_files}
    scale = _determine_scale(len(domain_files))

    # Index Stage 1 analyses by path for role/summary annotation
    analysis_by_path: dict[str, dict] = {}
    if file_analyses:
        for a in file_analyses:
            analysis_by_path[a.get("path", "")] = a

    # Index observations by path
    obs_by_path: dict[str, dict] = {}
    if observations:
        for o in observations:
            obs_by_path[o.get("path", "")] = o

    # Determine if we use observation-based bundle
    use_obs = bool(observations)
    include_code = not use_obs or scale == "small"

    # Reuse precomputed context from Stage 1 if available, otherwise collect fresh
    context_files: dict[str, str] = {}  # path → signatures
    if precomputed_context is not None:
        context_files = {k: v for k, v in precomputed_context.items() if k not in target_paths}
    else:
        try:
            from awf.core.imports import collect_context_files, extract_signatures
            for entry in domain_files:
                rel = entry.get("path", "")
                if not rel:
                    continue
                read_result = toolset.read(rel)
                if not read_result.ok:
                    continue
                target_path = (context.github_root / rel).resolve()
                language = _guess_language(context.github_root / rel)
                try:
                    ctx_paths = collect_context_files(target_path, context.github_root, language, read_result.output)
                    for ctx_path in ctx_paths:
                        ctx_rel = str(ctx_path.relative_to(context.github_root))
                        if ctx_rel not in target_paths and ctx_rel not in context_files:
                            try:
                                ctx_content = ctx_path.read_text(encoding="utf-8", errors="replace")
                                context_files[ctx_rel] = extract_signatures(ctx_content, language)
                            except Exception:
                                pass
                except Exception:
                    pass
        except Exception:
            pass

    # Build XML
    lines = [f'<review unit="{context.domain}" scale="{scale}">', "  <structure>"]
    for entry in domain_files:
        rel = entry.get("path", "")
        if rel:
            analysis = analysis_by_path.get(rel) or obs_by_path.get(rel)
            role = analysis.get("role", "") if analysis else ""
            role_attr = f' role="{_xml_escape(role)}"' if role else ""
            lines.append(f"    <path{role_attr}>{_xml_escape(rel)}</path>")
    for ctx_rel in sorted(context_files):
        lines.append(f'    <path role="context">{_xml_escape(ctx_rel)}</path>')
    lines.extend(["  </structure>"])

    # Target files
    for entry in domain_files:
        rel = entry.get("path", "")
        if not rel:
            continue
        file_path = context.github_root / rel
        analysis = analysis_by_path.get(rel)
        obs = obs_by_path.get(rel)
        summary_attr = ""
        summary = _derive_summary(analysis) if analysis else ""
        if summary:
            summary_attr = f' summary="{_xml_escape(summary)}"'

        lines.append(f'  <file path="{_xml_escape(rel)}" role="target" language="{_guess_language(file_path)}"{summary_attr}>')

        # Include observation markdown if available
        if obs and "observation" in obs:
            obs_md = obs["observation"].get("markdown", "")
            if obs_md:
                lines.append(f"    <observation>{_xml_escape(obs_md)}</observation>")

        # Include code content based on scale
        if include_code:
            read_result = toolset.read(rel)
            if read_result.ok:
                lines.append(f"    <content encoding=\"xml-escaped\">{_xml_escape(read_result.output)}</content>")

        lines.append("  </file>")

    # Context files (signatures only — imports from outside this unit)
    for ctx_rel in sorted(context_files):
        ctx_lang = _guess_language(context.github_root / ctx_rel)
        signatures = context_files[ctx_rel]
        lines.extend(
            [
                f'  <file path="{_xml_escape(ctx_rel)}" role="context" mode="signatures" language="{ctx_lang}">',
                f"    <content encoding=\"xml-escaped\">{_xml_escape(signatures[:2000])}</content>",
                "  </file>",
            ]
        )

    lines.append("</review>")
    return "\n".join(lines) + "\n"


def build_project_bundle(context: AnalysisContext) -> tuple[str | None, dict[str, object]]:
    policy = _load_reference_policy(context)
    empty_log = {
        "references_used": [],
        "references_dropped": [],
        "total_reference_tokens": 0,
        "policy": policy,
    }

    # Keep v2 behavior unchanged unless related_domains explicitly opt into expansion.
    if context.mode != "deep" or not context.related_domains or policy["max_documents"] <= 0:
        return None, empty_log

    used: list[dict[str, object]] = []
    dropped: list[dict[str, object]] = []
    total_tokens = 0
    for candidate in _reference_candidates(context):
        if len(used) >= int(policy["max_documents"]):
            dropped.append(
                {
                    "document": candidate["document"],
                    "level": candidate["level"],
                    "reason": "문서 수 상한 초과로 제거",
                }
            )
            continue
        if total_tokens + int(candidate["tokens"]) > int(policy["max_tokens"]):
            dropped.append(
                {
                    "document": candidate["document"],
                    "level": candidate["level"],
                    "reason": "토큰 상한 초과로 제거",
                }
            )
            continue
        used.append(candidate)
        total_tokens += int(candidate["tokens"])

    if not used:
        return None, {
            "references_used": [],
            "references_dropped": dropped,
            "total_reference_tokens": 0,
            "policy": policy,
        }

    lines = [
        f'<review scope="project" name="{_xml_escape(context.service)}">',
        (
            "  <reference-policy "
            f'max-documents="{policy["max_documents"]}" '
            f'max-tokens="{policy["max_tokens"]}" '
            f'require-reason="{str(policy["require_reason"]).lower()}" />'
        ),
        "  <references>",
    ]
    for item in used:
        lines.extend(
            [
                (
                    '    <document '
                    f'path="{_xml_escape(str(item["document"]))}" '
                    f'level="{item["level"]}" '
                    f'tokens="{item["tokens"]}" '
                    f'reason="{_xml_escape(str(item["reason"]))}">'
                ),
                f'      <content encoding="xml-escaped">{_xml_escape(str(item["content"]))}</content>',
                "    </document>",
            ]
        )
    lines.extend(["  </references>", "  <reference-log>"])
    for item in used:
        lines.append(
            '    <used '
            f'document="{_xml_escape(str(item["document"]))}" '
            f'level="{item["level"]}" '
            f'tokens="{item["tokens"]}" '
            f'reason="{_xml_escape(str(item["reason"]))}" />'
        )
    for item in dropped:
        lines.append(
            '    <dropped '
            f'document="{_xml_escape(str(item["document"]))}" '
            f'level="{item["level"]}" '
            f'reason="{_xml_escape(str(item["reason"]))}" />'
        )
    lines.extend(
        [
            "  </reference-log>",
            f'  <summary total-reference-tokens="{total_tokens}" />',
            "</review>",
        ]
    )
    return "\n".join(lines) + "\n", {
        "references_used": [
            {
                "document": item["document"],
                "level": item["level"],
                "reason": item["reason"],
                "tokens": item["tokens"],
            }
            for item in used
        ],
        "references_dropped": dropped,
        "total_reference_tokens": total_tokens,
        "policy": policy,
    }


def build_stage1_memo(
    context: AnalysisContext,
    *,
    discovery: dict[str, object] | None = None,
    file_analyses_text: str | None = None,
    skip_file_analyses: bool = False,
) -> str:
    target_dirs = "\n".join(f"- {item}" for item in context.domain_directories) or "- (none)"
    existing_dirs = "\n".join(f"- {item}" for item in (discovery or {}).get("existing_directories", [])) or "- (none)"
    glob_patterns = "\n".join(f"- {item}" for item in (discovery or {}).get("glob_patterns", [])) or "- (none)"
    collected_extensions = "\n".join(f"- {item}" for item in (discovery or {}).get("collected_file_extensions", [])) or "- (none)"
    target_file_count = int((discovery or {}).get("target_file_count", 0) or 0)
    related = "\n".join(f"- {item}" for item in context.related_domains) or "- (none)"
    existing = "\n".join(f"- {item}" for item in context.existing_docs) or "- (none)"
    memo = (
        f"# Stage 1 Memo: {context.service}/{context.domain}\n\n"
        f"- Mode: {context.mode}\n"
        f"- Repo root: {context.repo_root}\n"
        f"- Docs root: {context.docs_root}\n\n"
        "## Target Directories\n"
        f"{target_dirs}\n\n"
        "## Existing Directories\n"
        f"{existing_dirs}\n\n"
        "## Discovery Stats\n"
        f"- Target file count: {target_file_count}\n\n"
        "## Glob Patterns\n"
        f"{glob_patterns}\n\n"
        "## Collected File Extensions\n"
        f"{collected_extensions}\n\n"
        "## Related Domains\n"
        f"{related}\n\n"
        "## Existing Docs\n"
        f"{existing}\n"
    )
    if file_analyses_text and not skip_file_analyses:
        memo += f"\n## File Analyses\n\n{file_analyses_text}\n"
    elif file_analyses_text and skip_file_analyses:
        memo += "\n## File Analyses\n\n(domain-bundle XML의 summary 속성에 포함됨 — 중복 생략)\n"
    return memo


def build_stage3_note(context: AnalysisContext) -> str:
    return (
        f"# Stage 3 Scaffold: {context.service}/{context.domain}\n\n"
        "- status: placeholder cross-service validation summary recorded by awf-cli scaffold\n"
        "- reason: reference expansion was enabled but no live cross-service synthesis was persisted on this path\n"
    )


def build_stage3_prompt(
    context: AnalysisContext,
    provider_name: str,
    project_bundle_text: str | None,
    stage1_memo_text: str,
) -> tuple[str, list[str]]:
    from awf.core.spec_loader import load_prompt_optional
    project_bundle = project_bundle_text or "<review scope=\"project\"></review>\n"

    loaded = load_prompt_optional("analysis", "stage3",
        service=context.service, unit=context.domain,
        stage1_memo=stage1_memo_text, project_bundle=project_bundle)
    if loaded:
        prompt = loaded
    else:
        prompt = (
            f"Perform cross-service validation for `{context.service}/{context.domain}`.\n\n"
            "**모든 출력은 한국어로 작성하세요.** 코드 식별자는 영어 원문 유지.\n\n"
            "## Stage 1 Memo\n"
            f"{stage1_memo_text}\n\n"
            "## Project XML Bundle\n"
            f"{project_bundle}\n\n"
            "Return a concise Markdown summary focused on:\n"
            "- cross-service dependencies\n"
            "- mismatches with the generated Stage 2 outputs\n"
            "- technical debt or follow-up validation\n"
        )
    if provider_name == "claude-sdk":
        prompt += "\nThis provider cannot read local files directly; the prompt is self-contained.\n"
    return _truncate_for_budget(prompt, provider_name)


def build_provider_prompt(
    *,
    context: AnalysisContext,
    provider_name: str,
    base_prompt: str,
    domain_bundle_text: str,
    project_bundle_text: str | None,
    stage1_memo_text: str,
) -> tuple[str, list[str]]:
    # Native providers (claude-code): lightweight prompt with file path references
    if provider_name == "claude-code":
        return _build_native_prompt(
            context=context,
            base_prompt=base_prompt,
            domain_bundle_text=domain_bundle_text,
            stage1_memo_text=stage1_memo_text,
            project_bundle_text=project_bundle_text,
        )

    # Non-native providers: embed everything in prompt (self-contained)
    prompt = (
        f"{base_prompt}\n\n"
        "## Stage 1 Memo\n"
        f"{stage1_memo_text}\n\n"
        "## Domain XML Bundle\n"
        f"{domain_bundle_text}\n"
    )
    if context.mode == "deep" and project_bundle_text:
        prompt += f"\n## Project XML Bundle\n{project_bundle_text}\n"
    if provider_name in {"claude-sdk", "openai"}:
        prompt += (
            "\n## Execution Note\n"
            "This provider runs in non-interactive prompt mode. Treat the prompt content above as the source of truth "
            "and do not depend on additional local file discovery.\n"
        )
    return _truncate_for_budget(prompt, provider_name)


def _build_native_prompt(
    *,
    context: AnalysisContext,
    base_prompt: str,
    domain_bundle_text: str,
    stage1_memo_text: str,
    project_bundle_text: str | None = None,
) -> tuple[str, list[str]]:
    """Build a lightweight prompt for native providers that can read files.

    Embeds: base prompt (already lightweighted via native=True), stage1 memo, domain bundle.
    Adds: source directory references for direct file reading.

    Note: project-context and .ai-context references are already handled by
    _build_native_ai_context_ref() when build_prompt(native=True) is called.
    """
    # Source directory: reference for file reading
    domain_dirs = [d for d in context.domain_directories if Path(d).exists()]
    source_ref = (
        f"\n## 소스 코드 위치\n"
        f"코드를 직접 읽어야 할 때 이 경로를 사용하세요:\n"
        + "\n".join(f"- `{d}`" for d in domain_dirs) + "\n"
    ) if domain_dirs else ""

    prompt = (
        f"{base_prompt}\n\n"
        f"{source_ref}\n"
        "## Stage 1 Memo\n"
        f"{stage1_memo_text}\n\n"
        "## Domain XML Bundle (Stage 1 Observation 결과)\n"
        f"{domain_bundle_text}\n"
    )
    if project_bundle_text:
        prompt += f"\n## Reference XML Bundle\n{project_bundle_text}\n"
    return prompt, []


def estimate_bundle_tokens(bundle_text: str) -> int:
    return max(1, len(bundle_text) // 4)


def build_stage2_fanout_prompts(
    *,
    context: AnalysisContext,
    domain_bundle_text: str,
    stage1_memo_text: str,
) -> dict[str, str]:
    from awf.core.spec_loader import load_prompt_optional
    prompts: dict[str, str] = {}
    synth = load_prompt_optional("analysis", "stage2-synthesizer-fanout",
        service=context.service, domain=context.domain,
        stage1_memo=stage1_memo_text, domain_bundle=domain_bundle_text,
    )
    prompts["synthesizer"] = synth or (
        f"Prepare Stage 2 writer guidance for `{context.service}/{context.domain}`.\n"
    )
    for key, file_name, title in FANOUT_TARGETS:
        writer = load_prompt_optional("analysis", "stage2-writer-fanout",
            service=context.service, domain=context.domain,
            file_name=file_name, title=title,
            stage1_memo=stage1_memo_text, domain_bundle=domain_bundle_text,
        )
        prompts[key] = writer or (
            f"Write only `{file_name}` for `{context.service}/{context.domain}`.\n"
        )
    return prompts


def build_writer_prompts(
    *,
    context: AnalysisContext,
    domain_bundle_text: str,
    stage1_memo_text: str,
    writer_configs: list[dict],
) -> dict[str, str]:
    """Build v2 Writer prompts (Phase 2: claim/evidence/confidence schema).

    Each Writer gets its dedicated prompt template loaded from skills/analysis/prompts/.
    """
    from awf.core.spec_loader import load_prompt

    prompts: dict[str, str] = {}
    for wc in writer_configs:
        writer_id = wc["id"]
        prompt_name = wc["prompt"]
        base = load_prompt(
            "analysis", prompt_name,
            service=context.service,
            unit=context.domain,
        )
        prompt = (
            f"{base}\n\n"
            "## Stage 1 Memo\n"
            f"{stage1_memo_text}\n\n"
            "## Domain XML Bundle (Stage 1 Observation 결과)\n"
            f"{domain_bundle_text}\n"
        )
        prompts[writer_id] = prompt
    return prompts


def build_judge_prompt(
    *,
    context: AnalysisContext,
    writer_input_text: str,
    judge_prompt_name: str = "judge",
) -> str:
    """Build Judge prompt with Writer results injected."""
    from awf.core.spec_loader import load_prompt

    return load_prompt(
        "analysis", judge_prompt_name,
        service=context.service,
        unit=context.domain,
        writer_input=writer_input_text,
    )


def build_fallback_writer_prompt(
    *,
    original_prompt: str,
    fallback_files: list[str],
    context: AnalysisContext,
    max_tokens_per_file: int = 4000,
) -> str:
    """Augment Writer prompt with code content for fallback re-analysis."""
    code_sections: list[str] = []
    toolset = FileOpsToolset(context.github_root)
    for rel_path in fallback_files[:3]:
        read_result = toolset.read(rel_path)
        if not read_result.ok:
            continue
        content = read_result.output
        # Rough token limit: 4 chars per token
        char_limit = max_tokens_per_file * 4
        if len(content) > char_limit:
            content = content[:char_limit] + "\n... (truncated)"
        code_sections.append(f"### {rel_path}\n```\n{content}\n```")

    if not code_sections:
        return original_prompt

    from awf.core.spec_loader import load_prompt_optional
    joined_sections = "\n\n".join(code_sections)
    fallback_section = load_prompt_optional("analysis", "judge-fallback-section",
        code_sections=joined_sections,
    )
    if not fallback_section:
        fallback_section = f"\n\n## 코드 Fallback\n\n{joined_sections}"
    return original_prompt + fallback_section


def _read_existing_docs(context: AnalysisContext) -> str:
    sections: list[str] = []
    for relative_path in context.existing_docs:
        path = context.docs_root / relative_path
        if path.is_file():
            if path.suffix == ".md":
                body = read_markdown_body(path)
            else:
                body = path.read_text(encoding="utf-8", errors="ignore")
            sections.append(f"### {relative_path}\n{body.strip()}")
    for name in ("api-spec.json", "data-model.md", "domain-overview.md", "external-integration.md"):
        path = context.ai_context_dir / name
        if path.exists():
            if path.suffix == ".md":
                body = read_markdown_body(path)
            else:
                body = path.read_text(encoding="utf-8", errors="ignore")
            sections.append(f"### generated:{name}\n{body.strip()}")
    return "\n\n".join(section for section in sections if section.strip())


def _read_project_context(context: AnalysisContext) -> str:
    """Read service-level project-context.md if it exists (K6)."""
    ctx_path = context.docs_root / context.service / "project-context.md"
    if ctx_path.is_file():
        return ctx_path.read_text(encoding="utf-8", errors="ignore").strip()
    return ""


def _load_reference_policy(context: AnalysisContext) -> dict[str, int | bool]:
    policy = dict(DEFAULT_REFERENCE_POLICY)
    try:
        if context.analysis_pipeline_path.exists():
            raw = json.loads(context.analysis_pipeline_path.read_text(encoding="utf-8"))
        else:
            raw = {}
    except (OSError, json.JSONDecodeError):
        raw = {}

    reference_policy = raw.get("reference_policy", {}) or {}
    limits = reference_policy.get("limits", {}) or {}

    raw_max_documents = reference_policy.get(
        "max_documents",
        limits.get("max_reference_documents", policy["max_documents"]),
    )
    try:
        policy["max_documents"] = int(raw_max_documents)
    except (TypeError, ValueError):
        pass

    raw_max_tokens = reference_policy.get(
        "max_tokens",
        limits.get("max_reference_tokens", policy["max_tokens"]),
    )
    try:
        policy["max_tokens"] = int(raw_max_tokens)
    except (TypeError, ValueError):
        pass

    raw_require_reason = reference_policy.get(
        "require_reason",
        limits.get("require_reason", policy["require_reason"]),
    )
    if isinstance(raw_require_reason, bool):
        policy["require_reason"] = raw_require_reason
    elif isinstance(raw_require_reason, str):
        policy["require_reason"] = raw_require_reason.lower() in ("true", "1", "yes")

    return policy


def _reference_candidates(context: AnalysisContext) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []

    for related_domain in context.related_domains:
        ai_context_dir = context.docs_root / context.service / related_domain / ".ai-context"
        if not ai_context_dir.exists():
            continue
        seen: set[str] = set()
        for file_name in REFERENCE_FILE_ORDER:
            path = ai_context_dir / file_name
            if not path.is_file():
                continue
            seen.add(file_name)
            candidate = _make_reference_candidate(
                context=context,
                path=path,
                level=2,
                reason=_related_domain_reason(related_domain, file_name),
            )
            if candidate:
                candidates.append(candidate)
        for path in sorted(ai_context_dir.iterdir()):
            if path.name in seen or path.suffix not in {".md", ".json"} or not path.is_file():
                continue
            candidate = _make_reference_candidate(
                context=context,
                path=path,
                level=2,
                reason=f"related_domains `{related_domain}`의 추가 참조 문서",
            )
            if candidate:
                candidates.append(candidate)

    project_context_path = context.docs_root / context.service / "project-context.md"
    project_context = _make_reference_candidate(
        context=context,
        path=project_context_path,
        level=3,
        reason="서비스 수준 누적 지식(project-context) 참조",
    )
    if project_context:
        candidates.append(project_context)
    return candidates


def _make_reference_candidate(
    *,
    context: AnalysisContext,
    path: Path,
    level: int,
    reason: str,
) -> dict[str, object] | None:
    if not path.is_file():
        return None
    if path.suffix == ".md":
        content = read_markdown_body(path).strip()
    else:
        content = path.read_text(encoding="utf-8", errors="ignore").strip()
    if not content:
        return None
    return {
        "document": str(path.relative_to(context.docs_root)),
        "level": level,
        "reason": reason,
        "tokens": estimate_bundle_tokens(content),
        "content": content,
    }


def _related_domain_reason(domain: str, file_name: str) -> str:
    reason_by_file = {
        "api-spec.json": "API 계약 비교를 위한 참조",
        "data-model.md": "데이터 모델 비교를 위한 참조",
        "domain-overview.md": "도메인 흐름 비교를 위한 참조",
        "external-integration.md": "외부 연동 비교를 위한 참조",
    }
    detail = reason_by_file.get(file_name, "관련 도메인 비교를 위한 참조")
    return f"related_domains `{domain}`의 `{file_name}` 문서 참조: {detail}"


def _build_native_ai_context_ref(context: AnalysisContext) -> str:
    """Native provider용: embed 대신 파일 경로만 제공하여 프롬프트 경량화."""
    lines: list[str] = []
    project_ctx = context.docs_root / context.service / "project-context.md"
    if project_ctx.exists():
        lines.append(f"서비스 도메인 지식이 필요하면 이 파일을 읽으세요: `{project_ctx}`")
    if context.ai_context_dir.exists():
        existing = [f.name for f in context.ai_context_dir.iterdir() if f.suffix in {".json", ".md"} and not f.name.startswith(".")]
        if existing:
            lines.append(f"이전 분석 결과가 필요하면 이 디렉토리를 읽으세요: `{context.ai_context_dir}`")
            lines.append(f"파일: {', '.join(sorted(existing))}")
    return "\n".join(lines) if lines else "(없음)"


def _build_existing_ai_context_section(context: AnalysisContext) -> str:
    """Build the existing .ai-context section for Stage 2 template injection.

    Returns empty string if no previous results exist.
    """
    docs_text = _read_existing_docs(context)
    skip_project_context = context.mode == "deep" and bool(context.related_domains)
    project_ctx = "" if skip_project_context else _read_project_context(context)
    if not docs_text and not project_ctx:
        return ""

    parts: list[str] = []
    if project_ctx:
        parts.append(
            "## 서비스 도메인 지식 (project-context)\n\n"
            "이 서비스의 다른 unit 분석에서 축적된 도메인 지식입니다. 참고하되 현재 unit 분석을 우선하세요.\n\n"
            f"{project_ctx}"
        )
    if not docs_text:
        return "\n\n".join(parts)
    parts.append(
        "## 기존 .ai-context (이전 분석 결과)\n\n"
        "이전 분석 결과가 있습니다. 변경된 부분만 업데이트하고 기존 품질을 유지하세요.\n"
        "변경되지 않은 내용을 삭제하거나 간략화하지 마세요.\n\n"
        f"{docs_text}"
    )
    return "\n\n".join(parts)


def _truncate_for_budget(prompt: str, provider_name: str) -> tuple[str, list[str]]:
    limit = 120_000 if provider_name == "claude-code" else 48_000
    if len(prompt) <= limit:
        return prompt, []
    import sys
    original_len = len(prompt)
    dropped = original_len - limit
    msg = (
        f"warning: prompt for {provider_name} truncated: "
        f"{original_len:,} → {limit:,} chars ({dropped:,} chars dropped, "
        f"~{dropped // 4:,} tokens lost)"
    )
    print(msg, file=sys.stderr)
    return (
        prompt[:limit],
        [msg],
    )


def _guess_language(path: Path) -> str:
    from awf.core.languages import detect_language
    return detect_language(path.suffix)


def _derive_summary(analysis: dict) -> str:
    """Derive summary from v3 observation on-demand (A2: no compat fields in artifact)."""
    # Try v3 observation.json.business_logic first
    obs = analysis.get("observation", {})
    if isinstance(obs, dict):
        json_block = obs.get("json", {})
        if isinstance(json_block, dict):
            logic = json_block.get("business_logic", [])
            if logic:
                methods = [
                    entry.get("method", "?") if isinstance(entry, dict) else str(entry)
                    for entry in logic[:5]
                ]
                return f"Methods: {', '.join(methods)}"
    # Fallback: legacy summary field (backward compat for cached results)
    return str(analysis.get("summary", ""))


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
