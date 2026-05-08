"""Expand a WF feature's allowed-files.json using saved import graphs.

The WF plan phase emits ``planned_files`` from the LLM-generated tasks.md.
That list captures where the user *plans* to edit, but real implementations
often ripple into surrounding files: a signature change in one file forces
edits in its consumers, a new dependency forces updates in barrel exports.
G5 SCOPE_VIOLATION fires on every such ripple, even when the change was
unavoidable.

This module looks up each planned file in the saved per-unit import graphs
written by ``awf analyze`` and returns a deterministic, auditable expansion
of the scope. Callers decide whether to write the result back to
allowed-files.json or feed it into a diagnostic.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from awf.core.import_graph import ImportGraph


VALID_DIRECTIONS = ("dependents", "imports", "both")
DEFAULT_DEPTH = 1


@dataclass(frozen=True)
class ExpansionEntry:
    path: str
    reason: str  # "dependent_of:X" | "import_of:X"


@dataclass(frozen=True)
class ExpansionResult:
    planned: tuple[str, ...]
    added: tuple[str, ...]
    entries: tuple[ExpansionEntry, ...]
    # path → "found_in:{ai_context_dir}" / "no_graph" / "not_in_any_graph"
    coverage: dict[str, str] = field(default_factory=dict)
    direction: str = "dependents"
    depth: int | None = DEFAULT_DEPTH

    @property
    def all_files(self) -> tuple[str, ...]:
        seen: list[str] = []
        seen_set: set[str] = set()
        for path in list(self.planned) + list(self.added):
            if path not in seen_set:
                seen_set.add(path)
                seen.append(path)
        return tuple(seen)


def _iter_unit_graph_paths(docs_root: Path, services: Iterable[str] | None = None) -> Iterable[Path]:
    """Yield every saved import-graph.json under docs_root.

    Layout assumed: ``docs_root/<service>/<unit>/.ai-context/.tmp/import-graph.json``.
    When ``services`` is provided, only those service directories are scanned.
    """
    if not docs_root.is_dir():
        return
    service_dirs: Iterable[Path]
    if services:
        service_dirs = (docs_root / s for s in services)
    else:
        service_dirs = (p for p in docs_root.iterdir() if p.is_dir())
    for service_dir in service_dirs:
        if not service_dir.is_dir():
            continue
        for unit_dir in service_dir.iterdir():
            if not unit_dir.is_dir():
                continue
            graph_path = unit_dir / ".ai-context" / ".tmp" / "import-graph.json"
            if graph_path.is_file():
                yield graph_path


def _load_graphs(graph_paths: Iterable[Path]) -> list[tuple[Path, ImportGraph]]:
    loaded: list[tuple[Path, ImportGraph]] = []
    for path in graph_paths:
        graph = ImportGraph.load(path)
        if graph is not None:
            loaded.append((path, graph))
    return loaded


def expand_allowed_files(
    planned_files: list[str],
    docs_root: Path,
    *,
    services: list[str] | None = None,
    direction: str = "dependents",
    depth: int | None = DEFAULT_DEPTH,
    runtime_only: bool = False,
) -> ExpansionResult:
    """Expand ``planned_files`` using saved per-unit import graphs.

    Args:
        planned_files: Paths from ``allowed-files.json``'s ``planned_files``.
        docs_root: Analysis docs root that contains ``<service>/<unit>/`` dirs.
        services: Restrict the graph search to these services. ``None`` =
            scan every service directory under ``docs_root``.
        direction: ``"dependents"`` (files that import a planned file —
            usually the source of G5 false positives), ``"imports"`` (files
            a planned file depends on), or ``"both"``.
        depth: ``1`` for direct neighbors (default), ``None`` for full
            transitive closure. Larger depths permit more scope and should
            be opted into deliberately.
        runtime_only: Forwarded to ``ImportGraph.reverse_dependents``. The
            default ``False`` matches the analysis pipeline's invalidation
            policy: even type-only edges are scope-relevant.

    Returns:
        ``ExpansionResult`` with the original planned set, the deduplicated
        additions, per-addition reasons, and per-planned-file coverage info
        useful for diagnostics ("which graphs found this file?").
    """
    if direction not in VALID_DIRECTIONS:
        raise ValueError(
            f"invalid direction {direction!r}; expected one of {VALID_DIRECTIONS}"
        )

    planned_unique: list[str] = []
    seen: set[str] = set()
    for path in planned_files:
        if path and path not in seen:
            seen.add(path)
            planned_unique.append(path)

    graphs = _load_graphs(_iter_unit_graph_paths(docs_root, services))
    coverage: dict[str, str] = {p: "not_in_any_graph" for p in planned_unique}
    if not graphs:
        for p in planned_unique:
            coverage[p] = "no_graph"
        return ExpansionResult(
            planned=tuple(planned_unique),
            added=tuple(),
            entries=tuple(),
            coverage=coverage,
            direction=direction,
            depth=depth,
        )

    entries: list[ExpansionEntry] = []
    added_set: set[str] = set()
    planned_set = set(planned_unique)

    for path in planned_unique:
        for graph_path, graph in graphs:
            if path not in graph.nodes:
                continue
            coverage[path] = f"found_in:{graph_path.parent.parent}"
            if direction in ("dependents", "both"):
                deps = graph.reverse_dependents(
                    path, max_depth=depth, runtime_only=runtime_only
                )
                for dep in sorted(deps):
                    if dep in planned_set or dep in added_set:
                        continue
                    added_set.add(dep)
                    entries.append(ExpansionEntry(path=dep, reason=f"dependent_of:{path}"))
            if direction in ("imports", "both"):
                imps = graph.transitive_imports(
                    path, max_depth=depth, runtime_only=runtime_only
                )
                for imp in sorted(imps):
                    if imp in planned_set or imp in added_set:
                        continue
                    added_set.add(imp)
                    entries.append(ExpansionEntry(path=imp, reason=f"import_of:{path}"))

    return ExpansionResult(
        planned=tuple(planned_unique),
        added=tuple(sorted(added_set)),
        entries=tuple(entries),
        coverage=coverage,
        direction=direction,
        depth=depth,
    )


# ---------------------------------------------------------------------------
# Persistence helpers — round-trip the .workflow/artifacts/allowed-files.json.
# ---------------------------------------------------------------------------

ALLOWED_FILES_RELATIVE = ".workflow/artifacts/allowed-files.json"


def load_allowed_files(repo_root: Path) -> dict:
    path = repo_root / ALLOWED_FILES_RELATIVE
    if not path.is_file():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def save_allowed_files(repo_root: Path, payload: dict) -> Path:
    path = repo_root / ALLOWED_FILES_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def apply_expansion_to_payload(payload: dict, result: ExpansionResult) -> dict:
    """Merge an ExpansionResult into an allowed-files payload.

    Adds an ``expanded_files`` array (sorted, deduplicated) and a
    ``graph_expansion`` audit object so downstream verification can tell
    expansions apart from user-authored entries.
    """
    new_payload = dict(payload)
    expanded = sorted(set(result.added) - set(new_payload.get("planned_files", [])))
    new_payload["expanded_files"] = expanded
    new_payload["graph_expansion"] = {
        "direction": result.direction,
        "depth": result.depth,
        "added_count": len(expanded),
        "entries": [{"path": e.path, "reason": e.reason} for e in result.entries],
        "coverage": dict(result.coverage),
    }
    return new_payload
