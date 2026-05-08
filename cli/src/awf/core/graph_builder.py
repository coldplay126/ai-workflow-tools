"""Build the import graph from analyzed source files.

This is a deterministic post-Stage 1 step: it re-reads the files that were
analyzed and produces an import graph by walking each file's imports and
resolving them via `awf.core.imports.resolve_import_to_file`. No LLM calls.

Phase 0 contract: the graph is written to disk for diagnostics and used by
Stage 1 incremental invalidation on the next run. Graph build failures are
reported as warnings and never break the analysis pipeline.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from awf.core.analysis_files import compute_exports_hash
from awf.core.config import AnalysisContext
from awf.core.import_graph import (
    EDGE_KIND_IMPORT,
    GraphEdge,
    GraphNode,
    ImportGraph,
)
from awf.core.imports import extract_imports_with_kinds, resolve_import_to_file
from awf.core.languages import detect_language
from awf.tools.file_ops import FileOpsToolset


GRAPH_FILE_NAME = "import-graph.json"

DISABLE_ENV_VAR = "AWF_DISABLE_TRANSITIVE_INVALIDATION"
_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})


def transitive_invalidation_status(pipeline_config: dict | None) -> tuple[bool, str]:
    """Return (enabled, reason) for the transitive Stage-1 invalidation gate.

    Disable mechanisms — first match wins:
    1. ``AWF_DISABLE_TRANSITIVE_INVALIDATION`` environment variable set to a
       truthy value (1/true/yes/on, case-insensitive).
    2. ``analysis-pipeline.json`` → ``transitive_invalidation.enabled = false``.

    Default is enabled; the empty string is treated as unset for the env var.
    """
    env_value = os.environ.get(DISABLE_ENV_VAR, "").strip().lower()
    if env_value and env_value in _TRUTHY_ENV:
        return False, f"env:{DISABLE_ENV_VAR}"

    config_section = (pipeline_config or {}).get("transitive_invalidation", {})
    if isinstance(config_section, dict) and config_section.get("enabled") is False:
        return False, "config:transitive_invalidation.enabled=false"

    return True, "default"


@dataclass(frozen=True)
class Stage1GraphInvalidation:
    target_files: list[dict[str, str]]
    unchanged_files: list[dict[str, str]]
    indirect_paths: tuple[str, ...]
    invalidating_paths: tuple[str, ...]
    deleted_paths: tuple[str, ...]


def graph_path_for(context: AnalysisContext) -> Path:
    return context.ai_context_dir / ".tmp" / GRAPH_FILE_NAME


def _current_exports_hash(context: AnalysisContext, entry: dict[str, str]) -> str:
    rel_path = entry.get("path", "")
    if not rel_path:
        return ""
    read_result = FileOpsToolset(context.github_root).read(rel_path)
    if not read_result.ok:
        return ""
    return compute_exports_hash(read_result.output, detect_language(rel_path))


def _public_surface_changed(
    context: AnalysisContext,
    entry: dict[str, str],
    previous_node: GraphNode,
) -> bool:
    current_content_hash = str(entry.get("sha256", "") or "")
    current_exports_hash = _current_exports_hash(context, entry)

    if previous_node.exports_hash and current_exports_hash:
        return previous_node.exports_hash != current_exports_hash

    # Conservative fallback for languages or files where the baseline
    # extractor cannot produce a stable exported-surface hash.
    return previous_node.content_hash != current_content_hash


def expand_stage1_targets_with_import_graph(
    context: AnalysisContext,
    current_entries: list[dict[str, str]],
    changed_files: list[dict[str, str]],
    deleted_files: Iterable[dict[str, str]] = (),
) -> Stage1GraphInvalidation:
    """Expand Stage 1 targets using the previous import graph.

    Directly changed files are always re-analyzed. If a changed file's
    exported surface changed (or the extractor cannot tell, so content hash is
    used as a fallback), any reverse dependents from the previous graph are
    also re-analyzed. Deleted files similarly invalidate their previous
    dependents.
    """
    current_by_path = {entry["path"]: entry for entry in current_entries if "path" in entry}
    direct_paths = {entry["path"] for entry in changed_files if "path" in entry}
    invalidating_paths: set[str] = set()
    indirect_paths: set[str] = set()
    deleted_paths = {entry["path"] for entry in deleted_files if "path" in entry}

    previous_graph = ImportGraph.load(graph_path_for(context))
    if previous_graph is not None:
        for entry in changed_files:
            path = entry.get("path", "")
            previous_node = previous_graph.nodes.get(path)
            if previous_node and _public_surface_changed(context, entry, previous_node):
                invalidating_paths.add(path)

        for path in deleted_paths:
            if path in previous_graph.nodes:
                invalidating_paths.add(path)

        for path in invalidating_paths:
            indirect_paths.update(previous_graph.reverse_dependents(path, runtime_only=False))

    indirect_paths.intersection_update(current_by_path)
    indirect_paths.difference_update(direct_paths)

    target_paths = direct_paths | indirect_paths
    target_files = [entry for entry in current_entries if entry.get("path") in target_paths]
    unchanged_files = [entry for entry in current_entries if entry.get("path") not in target_paths]

    return Stage1GraphInvalidation(
        target_files=target_files,
        unchanged_files=unchanged_files,
        indirect_paths=tuple(sorted(indirect_paths)),
        invalidating_paths=tuple(sorted(invalidating_paths)),
        deleted_paths=tuple(sorted(deleted_paths)),
    )


def build_graph(
    context: AnalysisContext,
    file_entries: Iterable[dict[str, str]],
) -> ImportGraph:
    """Construct an ImportGraph from file entries (path + sha256).

    Reads file contents on demand. Imports that fail to resolve to an
    on-disk file are skipped — the graph only records edges between files
    that actually exist in the analyzed scope.
    """
    toolset = FileOpsToolset(context.github_root)
    repo_root = context.github_root

    # Build a path → existence index so we only emit edges to files that
    # were part of this analysis (or at least exist in the repo).
    entries = list(file_entries)
    known_paths: set[str] = {e["path"] for e in entries if "path" in e}

    graph = ImportGraph()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for entry in entries:
        rel_path = entry.get("path")
        if not rel_path:
            continue
        read_result = toolset.read(rel_path)
        if not read_result.ok:
            continue
        content = read_result.output
        language = detect_language(rel_path)

        node = GraphNode(
            path=rel_path,
            language=language,
            content_hash=str(entry.get("sha256", "")),
            exports_hash=compute_exports_hash(content, language),
            exports=tuple(),  # populated when an extractor returns symbols
            last_analyzed_at=now_iso,
        )
        # Re-extract exports cheaply so the JSON is auditable.
        try:
            from awf.core.lang_adapters import get as _get_extractor

            extractor = _get_extractor("regex_baseline")
            symbols = extractor.extract(content, language)
            node = GraphNode(
                path=node.path,
                language=node.language,
                content_hash=node.content_hash,
                exports_hash=node.exports_hash,
                exports=tuple(s.name for s in symbols),
                last_analyzed_at=node.last_analyzed_at,
            )
        except Exception:
            pass

        graph.upsert_node(node)

        edges: list[GraphEdge] = []
        source_file = (repo_root / rel_path).resolve()
        try:
            for raw_path, kind in extract_imports_with_kinds(content, language):
                resolved = resolve_import_to_file(raw_path, source_file, repo_root, language)
                if resolved is None:
                    continue
                try:
                    dst_rel = str(resolved.relative_to(repo_root))
                except ValueError:
                    continue
                if dst_rel == rel_path:
                    continue
                # Only record edges to files inside the analyzed scope; stray
                # cross-repo references would otherwise pollute the graph.
                if dst_rel not in known_paths:
                    continue
                edges.append(
                    GraphEdge(
                        src=rel_path,
                        dst=dst_rel,
                        symbols=tuple(),
                        kind=kind or EDGE_KIND_IMPORT,
                    )
                )
        except Exception as exc:  # noqa: BLE001 — never break analysis on graph errors
            print(
                f"warning: import-graph edge extraction failed for {rel_path}: {exc}",
                file=sys.stderr,
            )

        if edges:
            graph.replace_edges_from(rel_path, edges)

    return graph


def build_and_save_graph(
    context: AnalysisContext,
    file_entries: Iterable[dict[str, str]],
) -> Path | None:
    """Build the graph and persist it. Returns the saved path or None on error.

    Designed to be called inside a try/except by callers; it also catches
    its own internal errors so a graph failure cannot break Stage 1.
    """
    try:
        graph = build_graph(context, file_entries)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: import-graph build failed: {exc}", file=sys.stderr)
        return None

    out_path = graph_path_for(context)
    try:
        graph.save(out_path, generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    except OSError as exc:
        print(f"warning: import-graph save failed at {out_path}: {exc}", file=sys.stderr)
        return None
    return out_path
