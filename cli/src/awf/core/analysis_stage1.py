"""Stage 1 file-level analysis: per-file analysis with cheap provider.

Supports two modes:
- v2 (legacy): JSON-only output via stage1-file.md
- v3 (observation): Markdown + JSON output via stage1-observation.md (§1 Technical Specs)
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from awf.core.config import AnalysisContext
from awf.tools.file_ops import FileOpsToolset


_STAGE1_PROMPT_CACHE: str | None = None
_OBSERVATION_PROMPT_CACHE: str | None = None

_REQUIRED_OBSERVATION_FIELDS = {"path", "role", "language", "lines", "imports", "business_logic", "signals"}


def _get_stage1_template() -> str:
    global _STAGE1_PROMPT_CACHE
    if _STAGE1_PROMPT_CACHE is None:
        from awf.core.spec_loader import load_prompt_optional
        _STAGE1_PROMPT_CACHE = (
            load_prompt_optional("analysis", "stage1-file")
            or load_prompt_optional("analysis", "stage1-file-fallback")
            or "Analyze this file and return a JSON object.\n{xml_bundle}\n"
        )
    return _STAGE1_PROMPT_CACHE


def _get_observation_template() -> str:
    """Load the v3 observation prompt template (stage1-observation.md)."""
    global _OBSERVATION_PROMPT_CACHE
    if _OBSERVATION_PROMPT_CACHE is None:
        from awf.core.spec_loader import load_prompt_optional
        _OBSERVATION_PROMPT_CACHE = load_prompt_optional("analysis", "stage1-observation") or ""
    return _OBSERVATION_PROMPT_CACHE

from awf.core.languages import detect_language as _detect_language


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_file_xml_bundle(
    path: str,
    content: str,
    context_files: list[tuple[str, str]] | None = None,
) -> str:
    """Build a per-file XML bundle following the original deep-analysis spec.

    context_files: list of (path, signature_content) tuples for imported files.
    """
    language = _detect_language(path)
    lines = [f'<review target="{_xml_escape(path)}">']
    lines.append("  <structure>")
    lines.append(f"    <path>{_xml_escape(path)}</path>")
    for ctx_path, _ in (context_files or []):
        lines.append(f"    <path>{_xml_escape(ctx_path)}</path>")
    lines.append("  </structure>")

    # Target file (full content, truncated)
    lines.append(f'  <file path="{_xml_escape(path)}" role="target" language="{language}">')
    lines.append('    <content encoding="xml-escaped">')
    lines.append(_xml_escape(content[:8000]))
    lines.append("    </content>")
    lines.append("  </file>")

    # Context files (signatures only)
    for ctx_path, ctx_content in (context_files or []):
        ctx_lang = _detect_language(ctx_path)
        lines.append(f'  <file path="{_xml_escape(ctx_path)}" role="context" mode="signatures" language="{ctx_lang}">')
        lines.append('    <content encoding="xml-escaped">')
        lines.append(_xml_escape(ctx_content[:2000]))
        lines.append("    </content>")
        lines.append("  </file>")

    lines.append("</review>")
    return "\n".join(lines)


def build_file_analysis_prompt(
    path: str,
    content: str,
    context_files: list[tuple[str, str]] | None = None,
) -> str:
    xml_bundle = build_file_xml_bundle(path, content, context_files)
    template = _get_stage1_template()
    # Use str.replace() instead of .format() to avoid conflicts with JSON braces
    result = template.replace("{xml_bundle}", xml_bundle).replace("{path}", path)
    # Warn if any template variables remain unsubstituted
    import re as _re
    remaining = _re.findall(r"\{[a-z_]+\}", result)
    if remaining:
        print(
            f"warning: unsubstituted template variables in stage1 prompt: {remaining}",
            file=sys.stderr,
        )
    return result


def _parse_file_analysis(raw: str, path: str) -> dict[str, Any]:
    """Parse provider response into structured file analysis."""
    text = raw.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        lines = [l for l in lines if not l.startswith("```")]
        text = "\n".join(lines).strip()
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            result["path"] = path
            return result
    except json.JSONDecodeError:
        pass
    # Fallback: minimal entry
    return {
        "path": path,
        "role": "unknown",
        "imports": [],
        "exports": [],
        "summary": "",
        "dependencies": [],
        "complexity": "unknown",
        "parse_error": True,
    }


def extract_git_history(file_path: str, repo_root: Path, max_entries: int = 10) -> str:
    """Extract recent git commit history for a file (facts only, §1.4)."""
    abs_path = repo_root / file_path
    if not abs_path.is_file():
        return "(git history 없음)"
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--format=%ad %s", "--date=short", f"-n{max_entries}", "--", str(abs_path)],
            capture_output=True, text=True, cwd=str(repo_root), timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().splitlines()
            return f"최근 변경 {len(lines)}건:\n" + "\n".join(f"- {line}" for line in lines)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "(git history 없음)"


def build_observation_prompt(
    path: str,
    content: str,
    context_files: list[tuple[str, str]] | None = None,
    git_history: str = "",
) -> str:
    """Build a v3 observation prompt for a single file."""
    xml_bundle = build_file_xml_bundle(path, content, context_files)
    template = _get_observation_template()
    if not template:
        # Fallback to v2 if observation template not found
        return build_file_analysis_prompt(path, content, context_files)
    return (
        template
        .replace("{xml_bundle}", xml_bundle)
        .replace("{path}", path)
        .replace("{git_history}", git_history or "(git history 없음)")
    )


def parse_observation(raw: str, path: str) -> dict[str, Any]:
    """Parse observation output: extract JSON block and preserve markdown (§1.2).

    Returns: {
        "path": ..., "role": ..., "language": ..., "lines": ...,
        "imports": [...], "business_logic": [...], "signals": [...],
        "observation": {"json": {...}, "markdown": "..."},
        # v2 compat fields: "exports": [], "summary": "", "dependencies": [], "complexity": ""
    }
    """
    text = raw.strip()

    # Extract ```json ... ``` block
    json_match = re.search(r"```json\s*\n(.*?)```", text, re.DOTALL)
    json_block: dict[str, Any] = {}
    if json_match:
        try:
            json_block = json.loads(json_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Extract markdown (everything before the json block, or all if no block)
    if json_match:
        markdown = text[:json_match.start()].strip()
    else:
        markdown = text

    # Validate required fields — warn if LLM omitted them
    missing_fields = _REQUIRED_OBSERVATION_FIELDS - set(json_block.keys())
    if missing_fields:
        print(
            f"warning: observation for {path} missing fields: {sorted(missing_fields)}",
            file=sys.stderr,
        )
    json_block.setdefault("path", path)
    json_block.setdefault("role", "unknown")
    json_block.setdefault("language", "unknown")
    json_block.setdefault("lines", 0)
    json_block.setdefault("imports", [])
    json_block.setdefault("business_logic", [])
    json_block.setdefault("signals", [])

    # v3 observation-only result (A2: observation/judgment separation)
    result: dict[str, Any] = {
        "path": path,
        "role": json_block.get("role", "unknown"),
        "language": json_block.get("language", "unknown"),
        "lines": json_block.get("lines", 0),
        "observation": {
            "json": json_block,
            "markdown": markdown,
        },
    }

    return result


def _build_summary_from_logic(business_logic: list) -> str:
    """Build a v2-compatible summary string from business_logic entries."""
    if not business_logic:
        return ""
    methods = [entry.get("method", "?") if isinstance(entry, dict) else str(entry) for entry in business_logic[:5]]
    return f"Methods: {', '.join(methods)}"


def _estimate_complexity(json_block: dict) -> str:
    """Estimate v2-compatible complexity from observation data."""
    logic_count = len(json_block.get("business_logic", []))
    signal_count = len(json_block.get("signals", []))
    total_steps = sum(
        len(entry.get("steps", [])) if isinstance(entry, dict) else 0
        for entry in json_block.get("business_logic", [])
    )
    if total_steps > 15 or logic_count > 5 or signal_count > 5:
        return "high"
    elif total_steps > 5 or logic_count > 2:
        return "medium"
    return "low"


def run_stage1_file_analyses(
    context: AnalysisContext,
    domain_files: list[dict[str, str]],
    provider,
    *,
    max_concurrent: int = 5,
    on_progress=None,
    use_observation: bool = False,
) -> list[dict[str, Any]]:
    """Run per-file analysis with the given provider. Returns list of file analysis dicts.

    When use_observation=True, uses the v3 observation prompt (§1) and parser.
    Results include 'observation' key with json/markdown, plus v2 compat fields.
    """
    toolset = FileOpsToolset(context.github_root)
    results: list[dict[str, Any]] = []
    total = len(domain_files)

    if total == 0:
        return results

    # Track observation cache stats
    cache_stats = {"cached": 0, "analyzed": 0}

    def analyze_one(file_entry: dict[str, str]) -> dict[str, Any]:
        path = file_entry["path"]
        content_hash = file_entry.get("sha256", "")
        read_result = toolset.read(path)
        if not read_result.ok:
            return {"path": path, "role": "unknown", "summary": "file read failed", "imports": [], "exports": [], "dependencies": [], "complexity": "unknown", "read_error": True}

        # Observation cache check (§1.6) — requires content_hash for validation
        if use_observation and content_hash:
            cached = load_observation_cache(context, path, content_hash)
            if cached is not None:
                cache_stats["cached"] += 1
                return cached
        elif use_observation and not content_hash:
            # No content hash available — skip cache to avoid stale results
            pass

        # Collect context files via import tracking
        context_files: list[tuple[str, str]] = []
        try:
            from awf.core.imports import collect_context_files, extract_signatures
            target_path = (context.github_root / path).resolve()
            language = _detect_language(path)
            ctx_paths = collect_context_files(target_path, context.github_root, language, read_result.output)
            for ctx_path in ctx_paths:
                try:
                    ctx_content = ctx_path.read_text(encoding="utf-8", errors="replace")
                    signatures = extract_signatures(ctx_content, language)
                    rel_path = str(ctx_path.relative_to(context.github_root))
                    context_files.append((rel_path, signatures))
                except Exception:
                    pass
        except Exception:
            pass

        xml_bundle = build_file_xml_bundle(path, read_result.output, context_files)
        save_stage1_file_xml(context, path, xml_bundle)

        # Choose prompt and parser based on mode
        if use_observation:
            git_history = extract_git_history(path, context.github_root)
            prompt = build_observation_prompt(path, read_result.output, context_files, git_history)
            parser = parse_observation
        else:
            prompt = build_file_analysis_prompt(path, read_result.output, context_files)
            parser = _parse_file_analysis

        try:
            result = provider.complete(prompt, cwd=str(context.repo_root))
            output = (result.stdout or "").strip()
            if not output:
                output = (result.stderr or "").strip()
            parsed = parser(output, path)
            # Preserve context files for reuse by domain bundle
            if context_files:
                parsed["_context_files"] = {rel: sig for rel, sig in context_files}
            # Save observation cache (§1.6)
            if use_observation and content_hash and "observation" in parsed:
                save_observation_cache(context, path, content_hash, parsed)
            cache_stats["analyzed"] += 1
            return parsed
        except Exception as exc:
            return {"path": path, "role": "unknown", "summary": f"provider error: {exc}", "imports": [], "exports": [], "dependencies": [], "complexity": "unknown", "provider_error": True}

    started = time.monotonic()
    if max_concurrent <= 1 or total <= 2:
        for i, entry in enumerate(domain_files):
            results.append(analyze_one(entry))
            if on_progress:
                on_progress(i + 1, total)
    else:
        with ThreadPoolExecutor(max_workers=min(max_concurrent, total)) as pool:
            futures = {pool.submit(analyze_one, entry): entry for entry in domain_files}
            done_count = 0
            for future in as_completed(futures):
                done_count += 1
                results.append(future.result())
                if on_progress:
                    on_progress(done_count, total)

    elapsed = time.monotonic() - started
    if use_observation and (cache_stats["cached"] > 0 or cache_stats["analyzed"] > 0):
        print(
            f"observation_cache: {cache_stats['cached']} cached / {cache_stats['analyzed']} analyzed "
            f"(total={total}, hit_rate={cache_stats['cached']/total:.0%})",
            file=sys.stderr,
        )
    # Sort by original order
    path_order = {entry["path"]: i for i, entry in enumerate(domain_files)}
    results.sort(key=lambda r: path_order.get(r.get("path", ""), 999))
    return results


def save_stage1_file_xml(context: AnalysisContext, file_path: str, xml_bundle: str) -> Path:
    """Save a per-file XML bundle to .tmp/stage1-xml/ for debugging and reproducibility."""
    xml_dir = context.ai_context_dir / ".tmp" / "stage1-xml"
    xml_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file_path).name.replace(" ", "_")
    out_path = xml_dir / f"{safe_name}.xml"
    out_path.write_text(xml_bundle, encoding="utf-8")
    return out_path


def save_stage1_file_analyses(context: AnalysisContext, analyses: list[dict[str, Any]]) -> Path:
    tmp_dir = context.ai_context_dir / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / "stage1-file-analyses.json"
    path.write_text(json.dumps(analyses, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_stage1_file_analyses(context: AnalysisContext) -> list[dict[str, Any]]:
    """Load previously saved Stage 1 file analyses."""
    path = context.ai_context_dir / ".tmp" / "stage1-file-analyses.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def merge_stage1_analyses(
    new_analyses: list[dict[str, Any]],
    previous_analyses: list[dict[str, Any]],
    unchanged_paths: set[str],
) -> list[dict[str, Any]]:
    """Merge new Stage 1 analyses with previous results for unchanged files.

    Returns a complete list: new analyses for changed files + previous analyses for unchanged files.
    """
    # Index previous by path
    prev_by_path = {a["path"]: a for a in previous_analyses if "path" in a}
    # Index new by path
    new_by_path = {a["path"]: a for a in new_analyses if "path" in a}

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Add new analyses first
    for a in new_analyses:
        p = a.get("path", "")
        if p and p not in seen:
            merged.append(a)
            seen.add(p)

    # Add previous analyses for unchanged files
    for path in sorted(unchanged_paths):
        if path not in seen and path in prev_by_path:
            merged.append(prev_by_path[path])
            seen.add(path)

    return merged


def format_file_analyses_for_memo(analyses: list[dict[str, Any]]) -> str:
    """Format file analyses into a human-readable section for the Stage 1 memo.

    Supports both v2 (JSON-only) and v3 (observation) results.
    """
    if not analyses:
        return "- (no file analyses)\n"
    lines: list[str] = []
    for a in analyses:
        path = a.get("path", "?")
        role = a.get("role", "unknown")
        observation = a.get("observation")

        if observation and "markdown" in observation:
            # v3 observation: use markdown directly
            lines.append(observation["markdown"])
            lines.append("")
        else:
            # Fallback: derive from observation.json or minimal fields
            json_block = (observation or {}).get("json", {}) if isinstance(observation, dict) else {}
            logic = json_block.get("business_logic", [])
            imports = [
                entry.get("name", entry) if isinstance(entry, dict) else str(entry)
                for entry in json_block.get("imports", [])
            ]
            summary = ""
            if logic:
                methods = [entry.get("method", "?") if isinstance(entry, dict) else str(entry) for entry in logic[:5]]
                summary = f"Methods: {', '.join(methods)}"
            lines.append(f"### {path}")
            lines.append(f"- Role: {role}")
            if summary:
                lines.append(f"- Summary: {summary}")
            if imports:
                lines.append(f"- Imports: {', '.join(str(i) for i in imports[:15])}")
            lines.append("")
    return "\n".join(lines)


# --- Observation Cache (§1.6) ---

def _observation_cache_path(context: AnalysisContext, file_path: str) -> Path:
    """Generate cache file path using path hash for directory organization.

    Note: The actual cache hit/miss is validated by content_hash in the frontmatter,
    not the filename. This ensures encoding changes and content modifications
    are properly detected even if the file path stays the same.
    """
    # Use both path and a short stable identifier for the filename
    path_hash = hashlib.sha256(file_path.encode()).hexdigest()[:16]
    return context.ai_context_dir / ".tmp" / "observations" / f"{path_hash}.observation.md"


def save_observation_cache(context: AnalysisContext, file_path: str, content_hash: str, observation: dict[str, Any]) -> Path:
    """Save observation to cache file with frontmatter metadata."""
    cache_path = _observation_cache_path(context, file_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    obs = observation.get("observation", {})
    markdown = obs.get("markdown", "")
    json_block = obs.get("json", {})

    content = (
        f"---\n"
        f"file_path: {file_path}\n"
        f"content_hash: {content_hash}\n"
        f"---\n\n"
        f"{markdown}\n\n"
        f"```json\n{json.dumps(json_block, ensure_ascii=False, indent=2)}\n```\n"
    )
    cache_path.write_text(content, encoding="utf-8")
    return cache_path


def load_observation_cache(context: AnalysisContext, file_path: str, content_hash: str) -> dict[str, Any] | None:
    """Load cached observation. Returns None if cache miss or hash mismatch."""
    cache_path = _observation_cache_path(context, file_path)
    if not cache_path.is_file():
        return None

    try:
        text = cache_path.read_text(encoding="utf-8")
    except OSError:
        return None

    # Parse frontmatter
    if not text.startswith("---\n"):
        return None
    try:
        end_idx = text.index("---", 4)
    except ValueError:
        return None
    frontmatter = text[4:end_idx]
    cached_hash = ""
    for line in frontmatter.splitlines():
        if line.startswith("content_hash:"):
            cached_hash = line.split(":", 1)[1].strip()
            break

    if cached_hash != content_hash:
        return None

    # Parse the body as observation
    body = text[end_idx + 3:].strip()
    return parse_observation(body, file_path)
