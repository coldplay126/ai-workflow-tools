"""WF scope helpers — allowed-files expansion + git-diff-based G5 check.

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


def planned_files_from_payload(payload: dict) -> list[str]:
    """Return canonical planned files, falling back to legacy ``files``."""
    raw = payload.get("planned_files")
    if raw is None:
        raw = payload.get("files")
    if not isinstance(raw, list):
        return []
    return [str(path) for path in raw if path]


# ---------------------------------------------------------------------------
# G5 deterministic scope check — compare git diff against allowed-files.
# ---------------------------------------------------------------------------

DEFAULT_BASE_BRANCH_CANDIDATES = ("main", "master", "staging")

STATUS_PLANNED = "planned"
STATUS_EXPANDED = "expanded"
STATUS_VIOLATION = "violation"

# Repo-level errors that prevent classification (config issues, not violations).
REPO_ERROR_MISSING = "missing_repo"
REPO_ERROR_NOT_GIT = "not_git_repo"
REPO_ERROR_BRANCH_UNKNOWN = "branch_unknown"
REPO_ERROR_DIFF_FAILED = "git_diff_failed"


@dataclass(frozen=True)
class FileClassification:
    path: str
    status: str  # "planned" | "expanded" | "violation"
    reason: str  # explanation that the verify SKILL can quote verbatim


@dataclass(frozen=True)
class SiblingRepo:
    """Sibling repo declared in .workflow/manifest.json.

    See docs/specs/multi-repo-scope.md §3.1.
    """
    name: str
    path: str       # cycle-root-relative; must start with ".."
    branch: str | None  # None → fall back to default branch detection


@dataclass(frozen=True)
class RepoScopeResult:
    """Per-repo slice of a multi-repo scope check."""
    name: str            # "" for cycle root, else sibling.name
    path: str            # cycle-root-relative
    base_branch: str     # resolved base branch (or "" when error blocks resolution)
    changed_files: tuple[str, ...]
    classifications: tuple[FileClassification, ...]
    violations: tuple[FileClassification, ...]
    error: str | None    # one of REPO_ERROR_* or None


@dataclass(frozen=True)
class ScopeCheckResult:
    base_branch: str
    planned_set: tuple[str, ...]
    expanded_set: tuple[str, ...]
    changed_files: tuple[str, ...]
    classifications: tuple[FileClassification, ...]
    violations: tuple[FileClassification, ...]
    # Files in planned that were not actually changed (informational).
    planned_not_changed: tuple[str, ...]
    # Per-repo breakdown. Always contains at least the cycle root (name="").
    per_repo: tuple[RepoScopeResult, ...] = ()
    # Repo-level errors (missing path, not-git, etc.). Surface separately
    # from scope violations so operators can distinguish config mistakes.
    repo_errors: tuple[RepoScopeResult, ...] = ()

    @property
    def violation_count(self) -> int:
        return len(self.violations)

    @property
    def repo_error_count(self) -> int:
        return len(self.repo_errors)

    def to_json(self) -> dict:
        return {
            "base_branch": self.base_branch,
            "planned_count": len(self.planned_set),
            "expanded_count": len(self.expanded_set),
            "changed_count": len(self.changed_files),
            "violation_count": self.violation_count,
            "violations": [
                {"path": v.path, "reason": v.reason} for v in self.violations
            ],
            "classifications": [
                {"path": c.path, "status": c.status, "reason": c.reason}
                for c in self.classifications
            ],
            "planned_not_changed": list(self.planned_not_changed),
            "per_repo": [
                {
                    "name": r.name,
                    "path": r.path,
                    "base_branch": r.base_branch,
                    "changed_files": list(r.changed_files),
                    "classifications": [
                        {"path": c.path, "status": c.status, "reason": c.reason}
                        for c in r.classifications
                    ],
                    "violations": [
                        {"path": v.path, "reason": v.reason} for v in r.violations
                    ],
                    "error": r.error,
                }
                for r in self.per_repo
            ],
            "repo_errors": [
                {"name": r.name, "path": r.path, "error": r.error}
                for r in self.repo_errors
            ],
        }


def _git_diff_changed_files(repo_root: Path, base_branch: str) -> list[str]:
    import subprocess

    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_branch}...HEAD"],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git diff failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _resolve_base_branch(repo_root: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    state_path = repo_root / ".workflow" / "state.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            base = state.get("baseBranch") or state.get("base_branch")
            if isinstance(base, str) and base.strip():
                return base.strip()
        except (json.JSONDecodeError, OSError):
            pass

    import subprocess

    for candidate in DEFAULT_BASE_BRANCH_CANDIDATES:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", candidate],
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return candidate
    raise RuntimeError(
        "could not resolve base branch; pass --base-branch or set state.baseBranch"
    )


_SIBLING_NAME_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def _valid_sibling_name(name: str) -> bool:
    return bool(name) and all(ch in _SIBLING_NAME_OK for ch in name)


def load_sibling_repos(repo_root: Path) -> list[SiblingRepo]:
    """Load and validate `sibling_repos` from `.workflow/manifest.json`.

    Returns an empty list when the manifest is absent, malformed, or has no
    `sibling_repos` field — that's the documented backward-compat path
    (docs/specs/multi-repo-scope.md §3.1).

    Raises ValueError when a declared entry is malformed (bad name, missing
    path, non-relative path, path not starting with ".."). The caller maps
    that to exit-code 2.
    """
    manifest_path = repo_root / ".workflow" / "manifest.json"
    if not manifest_path.is_file():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw = manifest.get("sibling_repos")
    if not isinstance(raw, list) or not raw:
        return []

    result: list[SiblingRepo] = []
    seen_names: set[str] = set()
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"sibling_repos[{idx}]: must be an object")
        name = str(entry.get("name") or "").strip()
        path = str(entry.get("path") or "").strip()
        branch_raw = entry.get("branch")
        branch = str(branch_raw).strip() if isinstance(branch_raw, str) and branch_raw.strip() else None
        if not _valid_sibling_name(name):
            raise ValueError(
                f"sibling_repos[{idx}].name: must be non-empty and match [A-Za-z0-9_-]+ (got {name!r})"
            )
        if name in seen_names:
            raise ValueError(f"sibling_repos[{idx}].name: duplicate {name!r}")
        seen_names.add(name)
        if not path:
            raise ValueError(f"sibling_repos[{idx}].path: required")
        # Must be a relative sibling: ".." prefix prevents sub-paths inside cycle root.
        if Path(path).is_absolute() or not path.startswith(".."):
            raise ValueError(
                f"sibling_repos[{idx}].path: must be a sibling relative path starting with '..' (got {path!r})"
            )
        result.append(SiblingRepo(name=name, path=path, branch=branch))
    return result


_SIBLING_PREFIX = "@"


def _split_sibling_path(path: str) -> tuple[str | None, str]:
    """Split a file path into (sibling_name | None, real_path).

    "@foo/bar.ts" → ("foo", "bar.ts")
    "src/main.ts" → (None, "src/main.ts")

    A `@<name>/` prefix declares that the file belongs to a sibling repo.
    A `@` without a `/` separator is treated as a literal filename (file
    starting with @) for safety.
    """
    if not path.startswith(_SIBLING_PREFIX):
        return None, path
    rest = path[len(_SIBLING_PREFIX):]
    if "/" not in rest:
        return None, path
    name, _, real = rest.partition("/")
    if not _valid_sibling_name(name):
        return None, path
    return name, real


def _build_expansion_reason_index(payload: dict) -> dict[str, str]:
    """Map each expanded path to its `dependent_of:X` / `import_of:X` reason."""
    audit = payload.get("graph_expansion") or {}
    entries = audit.get("entries") or []
    index: dict[str, str] = {}
    for entry in entries:
        path = entry.get("path") if isinstance(entry, dict) else None
        reason = entry.get("reason") if isinstance(entry, dict) else None
        if path and reason and path not in index:
            index[path] = reason
    return index


def _classify_one_repo(
    *,
    repo_path: Path,
    name: str,
    rel_path: str,
    base_branch_override: str | None,
    planned: list[str],
    expanded: list[str],
    expansion_reasons: dict[str, str],
) -> RepoScopeResult:
    """Run scope classification for a single repo (root or sibling).

    Errors (missing path, not-a-git-repo, branch resolution failure,
    git diff failure) are surfaced as `RepoScopeResult.error` rather than
    raised, so the caller can aggregate them next to successful repos.

    The returned classifications/violations use the repo's *real* relative
    paths (no `@<name>/` prefix). The caller adds the prefix when
    flattening to top-level aggregates.
    """
    if not repo_path.is_dir():
        return RepoScopeResult(
            name=name, path=rel_path, base_branch="",
            changed_files=(), classifications=(), violations=(),
            error=REPO_ERROR_MISSING,
        )
    # .git can be a directory (normal clone) or a file (git worktree).
    if not (repo_path / ".git").exists():
        return RepoScopeResult(
            name=name, path=rel_path, base_branch="",
            changed_files=(), classifications=(), violations=(),
            error=REPO_ERROR_NOT_GIT,
        )
    try:
        base = _resolve_base_branch(repo_path, base_branch_override)
    except RuntimeError:
        return RepoScopeResult(
            name=name, path=rel_path, base_branch="",
            changed_files=(), classifications=(), violations=(),
            error=REPO_ERROR_BRANCH_UNKNOWN,
        )
    try:
        raw_changed = _git_diff_changed_files(repo_path, base)
    except RuntimeError:
        return RepoScopeResult(
            name=name, path=rel_path, base_branch=base,
            changed_files=(), classifications=(), violations=(),
            error=REPO_ERROR_DIFF_FAILED,
        )
    # `.workflow/` is WF infrastructure (artifacts, state.json, etc.), not
    # user-authored source. Excluding it here matches operator intuition —
    # nobody puts these paths in `planned_files` or `expanded_files`.
    changed = [p for p in raw_changed if not p.startswith(".workflow/")]

    planned_set = set(planned)
    expanded_set = set(expanded)
    classifications: list[FileClassification] = []
    violations: list[FileClassification] = []
    for path in changed:
        if path in planned_set:
            classifications.append(
                FileClassification(path=path, status=STATUS_PLANNED, reason="in planned_files")
            )
        elif path in expanded_set:
            reason = expansion_reasons.get(path) or "in expanded_files"
            classifications.append(
                FileClassification(path=path, status=STATUS_EXPANDED, reason=reason)
            )
        else:
            c = FileClassification(
                path=path,
                status=STATUS_VIOLATION,
                reason="not in planned_files or expanded_files",
            )
            classifications.append(c)
            violations.append(c)

    return RepoScopeResult(
        name=name,
        path=rel_path,
        base_branch=base,
        changed_files=tuple(changed),
        classifications=tuple(classifications),
        violations=tuple(violations),
        error=None,
    )


def _prefix_path(repo_name: str, path: str) -> str:
    """Render a per-repo path with its sibling prefix for top-level aggregates."""
    if not repo_name:
        return path
    return f"@{repo_name}/{path}"


def check_scope_violations(
    repo_root: Path,
    *,
    base_branch: str | None = None,
    include_expanded: bool = True,
) -> ScopeCheckResult:
    """Run G5 scope check deterministically.

    Compares ``git diff --name-only <base>...HEAD`` against the union of
    ``planned_files`` and (when ``include_expanded`` is True)
    ``expanded_files`` from ``.workflow/artifacts/allowed-files.json``.

    When `.workflow/manifest.json` declares `sibling_repos`, the check walks
    each sibling repo as well and aggregates results. Files in allowed-files
    are routed to the right repo via the `@<sibling-name>/` prefix
    (docs/specs/multi-repo-scope.md §3.2). Unprefixed paths target the
    cycle root.

    Returns per-file classifications plus a focused list of violations.
    """
    payload = load_allowed_files(repo_root)
    planned_raw = [p for p in planned_files_from_payload(payload) if p]
    expanded_raw = (
        [p for p in (payload.get("expanded_files") or []) if p]
        if include_expanded
        else []
    )
    expansion_reasons_raw = _build_expansion_reason_index(payload) if include_expanded else {}

    siblings = load_sibling_repos(repo_root)
    sibling_by_name = {s.name: s for s in siblings}

    # Partition planned/expanded files by which repo they belong to.
    planned_by_repo: dict[str, list[str]] = {"": []}
    expanded_by_repo: dict[str, list[str]] = {"": []}
    expansion_reasons_by_repo: dict[str, dict[str, str]] = {"": {}}
    for s in siblings:
        planned_by_repo[s.name] = []
        expanded_by_repo[s.name] = []
        expansion_reasons_by_repo[s.name] = {}

    # Top-level violations for unknown sibling prefixes — config typos must
    # not silently pass. See docs/specs/multi-repo-scope.md §5.
    extra_violations: list[FileClassification] = []
    seen_unknown: set[str] = set()

    for p in planned_raw:
        sibling, real = _split_sibling_path(p)
        if sibling is None:
            planned_by_repo[""].append(real)
        elif sibling in sibling_by_name:
            planned_by_repo[sibling].append(real)
        else:
            if p not in seen_unknown:
                extra_violations.append(FileClassification(
                    path=p, status=STATUS_VIOLATION,
                    reason=f"unknown sibling in planned_files: {sibling}",
                ))
                seen_unknown.add(p)

    for p in expanded_raw:
        sibling, real = _split_sibling_path(p)
        reason = expansion_reasons_raw.get(p)
        if sibling is None:
            expanded_by_repo[""].append(real)
            if reason:
                expansion_reasons_by_repo[""][real] = reason
        elif sibling in sibling_by_name:
            expanded_by_repo[sibling].append(real)
            if reason:
                expansion_reasons_by_repo[sibling][real] = reason
        else:
            if p not in seen_unknown:
                extra_violations.append(FileClassification(
                    path=p, status=STATUS_VIOLATION,
                    reason=f"unknown sibling in expanded_files: {sibling}",
                ))
                seen_unknown.add(p)

    # Classify each repo independently.
    per_repo: list[RepoScopeResult] = []
    repo_errors: list[RepoScopeResult] = []

    root_result = _classify_one_repo(
        repo_path=repo_root,
        name="",
        rel_path=".",
        base_branch_override=base_branch,
        planned=planned_by_repo[""],
        expanded=expanded_by_repo[""],
        expansion_reasons=expansion_reasons_by_repo[""],
    )
    per_repo.append(root_result)
    if root_result.error:
        repo_errors.append(root_result)

    for s in siblings:
        sib_path = (repo_root / s.path).resolve()
        r = _classify_one_repo(
            repo_path=sib_path,
            name=s.name,
            rel_path=s.path,
            base_branch_override=base_branch or s.branch,
            planned=planned_by_repo[s.name],
            expanded=expanded_by_repo[s.name],
            expansion_reasons=expansion_reasons_by_repo[s.name],
        )
        per_repo.append(r)
        if r.error:
            repo_errors.append(r)

    # Aggregate with sibling prefixes restored for human-readable output.
    agg_classifications: list[FileClassification] = []
    agg_violations: list[FileClassification] = list(extra_violations)
    agg_changed: list[str] = []
    agg_planned_not_changed: list[str] = []

    # Planned-but-not-changed for each repo, then prefix.
    for r in per_repo:
        if r.error:
            continue
        prefix = r.name
        for c in r.classifications:
            agg_classifications.append(FileClassification(
                path=_prefix_path(prefix, c.path),
                status=c.status,
                reason=c.reason,
            ))
        for v in r.violations:
            agg_violations.append(FileClassification(
                path=_prefix_path(prefix, v.path),
                status=v.status,
                reason=v.reason,
            ))
        for f in r.changed_files:
            agg_changed.append(_prefix_path(prefix, f))
        repo_planned = set(planned_by_repo[prefix])
        repo_changed = set(r.changed_files)
        for path in sorted(repo_planned - repo_changed):
            agg_planned_not_changed.append(_prefix_path(prefix, path))

    # Top-level planned/expanded sets keep prefixes so consumers can
    # round-trip them back to allowed-files entries.
    top_planned = sorted({
        _prefix_path(repo_name, p)
        for repo_name, files in planned_by_repo.items()
        for p in files
    })
    top_expanded = sorted({
        _prefix_path(repo_name, p)
        for repo_name, files in expanded_by_repo.items()
        for p in files
    })

    return ScopeCheckResult(
        base_branch=root_result.base_branch,
        planned_set=tuple(top_planned),
        expanded_set=tuple(top_expanded),
        changed_files=tuple(agg_changed),
        classifications=tuple(agg_classifications),
        violations=tuple(agg_violations),
        planned_not_changed=tuple(sorted(agg_planned_not_changed)),
        per_repo=tuple(per_repo),
        repo_errors=tuple(repo_errors),
    )


def apply_expansion_to_payload(payload: dict, result: ExpansionResult) -> dict:
    """Merge an ExpansionResult into an allowed-files payload.

    Adds an ``expanded_files`` array (sorted, deduplicated) and a
    ``graph_expansion`` audit object so downstream verification can tell
    expansions apart from user-authored entries.
    """
    new_payload = dict(payload)
    expanded = sorted(set(result.added) - set(planned_files_from_payload(new_payload)))
    new_payload["expanded_files"] = expanded
    new_payload["graph_expansion"] = {
        "direction": result.direction,
        "depth": result.depth,
        "added_count": len(expanded),
        "entries": [{"path": e.path, "reason": e.reason} for e in result.entries],
        "coverage": dict(result.coverage),
    }
    return new_payload
