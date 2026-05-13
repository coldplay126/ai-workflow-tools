"""Tests for awf.core.wf_scope — allowed-files expansion via import graph."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.commands.wf import run_wf_expand_scope
from awf.core.import_graph import GraphEdge, GraphNode, ImportGraph
from awf.core.wf_scope import (
    apply_expansion_to_payload,
    expand_allowed_files,
    load_allowed_files,
    planned_files_from_payload,
    save_allowed_files,
)


def _build_chain_graph() -> ImportGraph:
    """types.ts ← service.ts ← controller.ts plus an unrelated leaf.ts."""
    g = ImportGraph()
    for path in ("src/types.ts", "src/service.ts", "src/controller.ts", "src/leaf.ts"):
        g.upsert_node(
            GraphNode(
                path=path,
                language="typescript",
                content_hash="c",
                exports_hash="e",
                exports=("X",),
                last_analyzed_at="2026-05-08T00:00:00Z",
            )
        )
    g.replace_edges_from(
        "src/service.ts",
        [GraphEdge(src="src/service.ts", dst="src/types.ts", symbols=("X",), kind="import")],
    )
    g.replace_edges_from(
        "src/controller.ts",
        [GraphEdge(src="src/controller.ts", dst="src/service.ts", symbols=("X",), kind="import")],
    )
    return g


def _write_unit_graph(docs_root: Path, service: str, unit: str, graph: ImportGraph) -> Path:
    out = docs_root / service / unit / ".ai-context" / ".tmp" / "import-graph.json"
    graph.save(out)
    return out


# --------------------------------------------------------------------------
# expand_allowed_files
# --------------------------------------------------------------------------

def test_expand_dependents_default_depth_1(tmp_path: Path):
    _write_unit_graph(tmp_path, "svc", "alpha", _build_chain_graph())

    result = expand_allowed_files(
        ["src/service.ts"],
        docs_root=tmp_path,
        direction="dependents",
        depth=1,
    )

    # Direct dependent of service.ts is controller.ts only.
    assert list(result.added) == ["src/controller.ts"]
    assert all(e.reason == "dependent_of:src/service.ts" for e in result.entries)


def test_expand_imports_includes_what_planned_file_depends_on(tmp_path: Path):
    _write_unit_graph(tmp_path, "svc", "alpha", _build_chain_graph())

    result = expand_allowed_files(
        ["src/service.ts"],
        docs_root=tmp_path,
        direction="imports",
        depth=1,
    )

    assert list(result.added) == ["src/types.ts"]
    assert result.entries[0].reason == "import_of:src/service.ts"


def test_expand_both_combines_directions(tmp_path: Path):
    _write_unit_graph(tmp_path, "svc", "alpha", _build_chain_graph())

    result = expand_allowed_files(
        ["src/service.ts"],
        docs_root=tmp_path,
        direction="both",
        depth=1,
    )
    # 1-hop in both directions: controller.ts (dependent) + types.ts (import).
    assert set(result.added) == {"src/controller.ts", "src/types.ts"}


def test_expand_full_closure_when_depth_none(tmp_path: Path):
    _write_unit_graph(tmp_path, "svc", "alpha", _build_chain_graph())

    result = expand_allowed_files(
        ["src/types.ts"],
        docs_root=tmp_path,
        direction="dependents",
        depth=None,
    )
    # Full reverse closure of types.ts: service.ts AND controller.ts.
    assert set(result.added) == {"src/service.ts", "src/controller.ts"}


def test_expand_excludes_paths_already_in_planned(tmp_path: Path):
    _write_unit_graph(tmp_path, "svc", "alpha", _build_chain_graph())

    result = expand_allowed_files(
        ["src/service.ts", "src/controller.ts"],  # controller already planned
        docs_root=tmp_path,
        direction="dependents",
        depth=1,
    )
    # service's dependent (controller) is already in planned → no addition.
    assert result.added == ()


def test_expand_returns_no_graph_when_docs_root_empty(tmp_path: Path):
    result = expand_allowed_files(
        ["src/service.ts"],
        docs_root=tmp_path,
        direction="dependents",
    )
    assert result.added == ()
    assert result.coverage["src/service.ts"] == "no_graph"


def test_expand_marks_unmapped_files(tmp_path: Path):
    _write_unit_graph(tmp_path, "svc", "alpha", _build_chain_graph())

    result = expand_allowed_files(
        ["src/never_analyzed.ts"],
        docs_root=tmp_path,
        direction="dependents",
    )
    assert result.added == ()
    assert result.coverage["src/never_analyzed.ts"] == "not_in_any_graph"


def test_expand_can_filter_to_specific_services(tmp_path: Path):
    g_alpha = _build_chain_graph()
    g_beta = ImportGraph()
    g_beta.upsert_node(
        GraphNode("src/other.ts", "typescript", "c", "e", (), "2026-05-08T00:00:00Z")
    )
    _write_unit_graph(tmp_path, "svc-alpha", "u", g_alpha)
    _write_unit_graph(tmp_path, "svc-beta", "u", g_beta)

    result = expand_allowed_files(
        ["src/service.ts"],
        docs_root=tmp_path,
        services=["svc-beta"],  # restrict — alpha graph is invisible
        direction="dependents",
    )
    # Service.ts not in svc-beta's graph → no expansion.
    assert result.added == ()


def test_expand_rejects_invalid_direction(tmp_path: Path):
    import pytest

    with pytest.raises(ValueError):
        expand_allowed_files(["x"], docs_root=tmp_path, direction="sideways")


def test_expand_handles_runtime_only_flag(tmp_path: Path):
    g = ImportGraph()
    for p in ("src/types.ts", "src/consumer.ts"):
        g.upsert_node(GraphNode(p, "typescript", "c", "e", (), "2026-05-08T00:00:00Z"))
    g.replace_edges_from(
        "src/consumer.ts",
        [GraphEdge(src="src/consumer.ts", dst="src/types.ts", symbols=(), kind="type-only")],
    )
    _write_unit_graph(tmp_path, "svc", "u", g)

    runtime_only = expand_allowed_files(
        ["src/types.ts"], docs_root=tmp_path, runtime_only=True
    )
    assert runtime_only.added == ()  # type-only edge skipped

    full = expand_allowed_files(
        ["src/types.ts"], docs_root=tmp_path, runtime_only=False
    )
    assert full.added == ("src/consumer.ts",)


# --------------------------------------------------------------------------
# Round-trip helpers
# --------------------------------------------------------------------------

def test_apply_expansion_to_payload_adds_audit_block(tmp_path: Path):
    _write_unit_graph(tmp_path, "svc", "alpha", _build_chain_graph())

    payload = {"planned_files": ["src/service.ts"]}
    result = expand_allowed_files(
        ["src/service.ts"], docs_root=tmp_path, direction="dependents"
    )
    new_payload = apply_expansion_to_payload(payload, result)

    assert new_payload["planned_files"] == ["src/service.ts"]  # unchanged
    assert new_payload["expanded_files"] == ["src/controller.ts"]
    audit = new_payload["graph_expansion"]
    assert audit["direction"] == "dependents"
    assert audit["depth"] == 1
    assert audit["added_count"] == 1
    assert audit["entries"] == [
        {"path": "src/controller.ts", "reason": "dependent_of:src/service.ts"}
    ]
    # planned file's coverage should reflect that the graph was found.
    assert audit["coverage"]["src/service.ts"].startswith("found_in:")


def test_load_save_allowed_files_roundtrip(tmp_path: Path):
    repo_root = tmp_path / "repo"
    artifacts = repo_root / ".workflow" / "artifacts"
    artifacts.mkdir(parents=True)
    src_payload = {"planned_files": ["src/a.ts", "src/b.ts"], "extracted_from": "tasks.md"}
    (artifacts / "allowed-files.json").write_text(
        json.dumps(src_payload), encoding="utf-8"
    )

    loaded = load_allowed_files(repo_root)
    assert loaded == src_payload

    loaded["expanded_files"] = ["src/c.ts"]
    save_allowed_files(repo_root, loaded)

    reread = json.loads((artifacts / "allowed-files.json").read_text(encoding="utf-8"))
    assert reread == loaded


def test_planned_files_from_payload_prefers_canonical_key():
    assert planned_files_from_payload({
        "planned_files": ["src/new.ts"],
        "files": ["src/legacy.ts"],
    }) == ["src/new.ts"]


def test_planned_files_from_payload_falls_back_to_legacy_files():
    assert planned_files_from_payload({"files": ["src/legacy.ts"]}) == [
        "src/legacy.ts"
    ]


def test_load_allowed_files_raises_for_missing(tmp_path: Path):
    import pytest

    with pytest.raises(FileNotFoundError):
        load_allowed_files(tmp_path)


# --------------------------------------------------------------------------
# awf wf expand-scope command
# --------------------------------------------------------------------------


def test_expand_scope_command_resolves_docs_root_without_analysis_config(
    tmp_path: Path, capsys
):
    repo_root = tmp_path / "repo"
    docs_root = tmp_path / "analysis-docs"
    artifacts = repo_root / ".workflow" / "artifacts"
    artifacts.mkdir(parents=True)
    (repo_root / ".awf.toml").write_text(
        "\n".join(
            [
                "[paths]",
                f'analysis_docs = "{docs_root}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (artifacts / "allowed-files.json").write_text(
        json.dumps({"planned_files": ["src/service.ts"]}) + "\n",
        encoding="utf-8",
    )
    _write_unit_graph(docs_root, "svc", "alpha", _build_chain_graph())

    rc = run_wf_expand_scope(argparse.Namespace(
        repo_root=str(repo_root),
        direction="dependents",
        service=None,
        depth=1,
        runtime_only=False,
        dry_run=True,
        json=True,
    ))

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0
    assert payload["planned"] == ["src/service.ts"]
    assert payload["added"] == ["src/controller.ts"]


def test_expand_scope_command_uses_legacy_files_fallback(tmp_path: Path, capsys):
    repo_root = tmp_path / "repo"
    docs_root = tmp_path / "analysis-docs"
    artifacts = repo_root / ".workflow" / "artifacts"
    artifacts.mkdir(parents=True)
    (repo_root / ".awf.toml").write_text(
        "\n".join(
            [
                "[paths]",
                f'analysis_docs = "{docs_root}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (artifacts / "allowed-files.json").write_text(
        json.dumps({"files": ["src/service.ts"]}) + "\n",
        encoding="utf-8",
    )
    _write_unit_graph(docs_root, "svc", "alpha", _build_chain_graph())

    rc = run_wf_expand_scope(argparse.Namespace(
        repo_root=str(repo_root),
        direction="dependents",
        service=None,
        depth=1,
        runtime_only=False,
        dry_run=True,
        json=True,
    ))

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0
    assert payload["planned"] == ["src/service.ts"]
    assert payload["added"] == ["src/controller.ts"]


# --------------------------------------------------------------------------
# check_scope_violations — G5 deterministic gate
# --------------------------------------------------------------------------

def _init_repo_with_branch(repo_root: Path, base: str = "main") -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", base], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "t@x"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo_root, check=True)


def _commit_all(repo_root: Path, msg: str) -> None:
    import subprocess

    subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo_root, check=True)


def _setup_repo_with_changes(
    repo_root: Path,
    *,
    planned: list[str],
    expanded: list[str],
    expansion_entries: list[dict] | None = None,
    base_files: list[str] | None = None,
    changed_files: list[str] | None = None,
) -> None:
    """Initialize a git repo, write base files on `main`, then branch and modify."""
    from awf.core.wf_scope import save_allowed_files

    _init_repo_with_branch(repo_root, base="main")

    for path in (base_files or []):
        full = repo_root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("// base\n", encoding="utf-8")
    (repo_root / "README.md").write_text("seed\n", encoding="utf-8")
    _commit_all(repo_root, "base")

    import subprocess

    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=repo_root, check=True)

    payload = {"planned_files": planned}
    if expanded:
        payload["expanded_files"] = expanded
    if expansion_entries is not None:
        payload["graph_expansion"] = {"entries": expansion_entries}
    save_allowed_files(repo_root, payload)

    for path in (changed_files or []):
        full = repo_root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("// modified\n", encoding="utf-8")
    if changed_files:
        _commit_all(repo_root, "feature changes")


def test_scope_check_passes_when_only_planned_files_change(tmp_path: Path):
    from awf.core.wf_scope import check_scope_violations

    _setup_repo_with_changes(
        tmp_path,
        planned=["src/a.ts", "src/b.ts"],
        expanded=[],
        base_files=["src/a.ts", "src/b.ts"],
        changed_files=["src/a.ts", "src/b.ts"],
    )

    result = check_scope_violations(tmp_path, base_branch="main")
    assert result.violation_count == 0
    statuses = {c.path: c.status for c in result.classifications}
    assert statuses["src/a.ts"] == "planned"
    assert statuses["src/b.ts"] == "planned"


def test_scope_check_treats_legacy_files_as_planned(tmp_path: Path):
    from awf.core.wf_scope import check_scope_violations

    _init_repo_with_branch(tmp_path, base="main")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("// base\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    _commit_all(tmp_path, "base")

    import subprocess

    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=tmp_path, check=True)
    save_allowed_files(tmp_path, {"files": ["src/a.ts"]})
    (tmp_path / "src" / "a.ts").write_text("// modified\n", encoding="utf-8")
    _commit_all(tmp_path, "feature changes")

    result = check_scope_violations(tmp_path, base_branch="main")
    assert result.violation_count == 0
    assert result.planned_set == ("src/a.ts",)
    assert result.classifications[0].status == "planned"


def test_scope_check_treats_expanded_files_as_in_scope(tmp_path: Path):
    from awf.core.wf_scope import check_scope_violations

    _setup_repo_with_changes(
        tmp_path,
        planned=["src/a.ts"],
        expanded=["src/b.ts"],
        expansion_entries=[{"path": "src/b.ts", "reason": "dependent_of:src/a.ts"}],
        base_files=["src/a.ts", "src/b.ts"],
        changed_files=["src/a.ts", "src/b.ts"],
    )

    result = check_scope_violations(tmp_path, base_branch="main")
    assert result.violation_count == 0
    statuses = {c.path: c.status for c in result.classifications}
    reasons = {c.path: c.reason for c in result.classifications}
    assert statuses["src/a.ts"] == "planned"
    assert statuses["src/b.ts"] == "expanded"
    # The reason should come from the audit trail when available.
    assert reasons["src/b.ts"] == "dependent_of:src/a.ts"


def test_scope_check_flags_unplanned_unexpanded_change_as_violation(tmp_path: Path):
    from awf.core.wf_scope import check_scope_violations

    _setup_repo_with_changes(
        tmp_path,
        planned=["src/a.ts"],
        expanded=["src/b.ts"],
        expansion_entries=[{"path": "src/b.ts", "reason": "dependent_of:src/a.ts"}],
        base_files=["src/a.ts", "src/b.ts", "src/c.ts"],
        changed_files=["src/a.ts", "src/b.ts", "src/c.ts"],
    )

    result = check_scope_violations(tmp_path, base_branch="main")
    assert result.violation_count == 1
    assert result.violations[0].path == "src/c.ts"
    assert "not in planned_files or expanded_files" in result.violations[0].reason


def test_scope_check_no_expanded_falls_back_to_legacy_behavior(tmp_path: Path):
    from awf.core.wf_scope import check_scope_violations

    _setup_repo_with_changes(
        tmp_path,
        planned=["src/a.ts"],
        expanded=["src/b.ts"],
        expansion_entries=[{"path": "src/b.ts", "reason": "dependent_of:src/a.ts"}],
        base_files=["src/a.ts", "src/b.ts"],
        changed_files=["src/a.ts", "src/b.ts"],
    )

    result = check_scope_violations(tmp_path, base_branch="main", include_expanded=False)
    # With expansion ignored, src/b.ts becomes a violation again.
    assert result.violation_count == 1
    assert result.violations[0].path == "src/b.ts"


def test_scope_check_reports_planned_not_changed(tmp_path: Path):
    from awf.core.wf_scope import check_scope_violations

    _setup_repo_with_changes(
        tmp_path,
        planned=["src/a.ts", "src/b.ts"],
        expanded=[],
        base_files=["src/a.ts", "src/b.ts"],
        changed_files=["src/a.ts"],  # b planned but not modified
    )

    result = check_scope_violations(tmp_path, base_branch="main")
    assert result.violation_count == 0
    assert result.planned_not_changed == ("src/b.ts",)


def test_scope_check_to_json_serializes_expected_keys(tmp_path: Path):
    from awf.core.wf_scope import check_scope_violations

    _setup_repo_with_changes(
        tmp_path,
        planned=["src/a.ts"],
        expanded=[],
        base_files=["src/a.ts", "src/c.ts"],
        changed_files=["src/a.ts", "src/c.ts"],
    )

    payload = check_scope_violations(tmp_path, base_branch="main").to_json()
    for key in (
        "base_branch",
        "planned_count",
        "expanded_count",
        "changed_count",
        "violation_count",
        "violations",
        "classifications",
        "planned_not_changed",
    ):
        assert key in payload
    assert payload["violation_count"] == 1
    assert payload["violations"][0]["path"] == "src/c.ts"


def test_scope_check_raises_when_allowed_files_missing(tmp_path: Path):
    import pytest
    from awf.core.wf_scope import check_scope_violations

    _init_repo_with_branch(tmp_path, base="main")
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    _commit_all(tmp_path, "base")

    with pytest.raises(FileNotFoundError):
        check_scope_violations(tmp_path, base_branch="main")


# --------------------------------------------------------------------------
# Multi-repo scope check (docs/specs/multi-repo-scope.md)
# --------------------------------------------------------------------------

def _write_manifest(repo_root: Path, siblings: list[dict]) -> None:
    wf_dir = repo_root / ".workflow"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "manifest.json").write_text(
        json.dumps({"version": "1.0.0", "sibling_repos": siblings}), encoding="utf-8"
    )


def _setup_sibling_repo(
    repo_root: Path,
    *,
    base_files: list[str],
    changed_files: list[str],
    base: str = "main",
    branch_name: str = "feature",
) -> None:
    """Initialize a sibling git repo with base→branch changes (no allowed-files)."""
    import subprocess

    _init_repo_with_branch(repo_root, base=base)
    for path in base_files:
        full = repo_root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("// base\n", encoding="utf-8")
    (repo_root / "README.md").write_text("seed\n", encoding="utf-8")
    _commit_all(repo_root, "base")
    subprocess.run(["git", "checkout", "-q", "-b", branch_name], cwd=repo_root, check=True)
    for path in changed_files:
        full = repo_root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("// modified\n", encoding="utf-8")
    if changed_files:
        _commit_all(repo_root, "feature changes")


def test_scope_check_no_siblings_backward_compat(tmp_path: Path):
    """No manifest → behaves exactly like before; per_repo holds only root."""
    from awf.core.wf_scope import check_scope_violations

    _setup_repo_with_changes(
        tmp_path,
        planned=["src/a.ts"],
        expanded=[],
        base_files=["src/a.ts"],
        changed_files=["src/a.ts"],
    )
    result = check_scope_violations(tmp_path, base_branch="main")
    assert result.violation_count == 0
    assert len(result.per_repo) == 1
    assert result.per_repo[0].name == ""
    assert result.per_repo[0].error is None
    assert result.repo_errors == ()


def test_scope_check_single_sibling_planned(tmp_path: Path):
    """Sibling repo whose changes are all in planned_files → PASS."""
    from awf.core.wf_scope import check_scope_violations

    root = tmp_path / "root"
    sibling = tmp_path / "sibling-api"
    root.mkdir()
    sibling.mkdir()

    _setup_sibling_repo(
        sibling,
        base_files=["src/handler.ts"],
        changed_files=["src/handler.ts"],
        branch_name="feature",
    )
    _setup_repo_with_changes(
        root,
        planned=["src/main.ts", "@api/src/handler.ts"],
        expanded=[],
        base_files=["src/main.ts"],
        changed_files=["src/main.ts"],
    )
    _write_manifest(root, [
        {"name": "api", "path": "../sibling-api", "branch": "main"}
    ])

    result = check_scope_violations(root, base_branch="main")
    assert result.violation_count == 0, [v.path for v in result.violations]
    assert len(result.per_repo) == 2
    # Top-level changed_files carry the sibling prefix.
    assert "src/main.ts" in result.changed_files
    assert "@api/src/handler.ts" in result.changed_files


def test_scope_check_sibling_violation_aggregates_to_top_level(tmp_path: Path):
    """Sibling changes a file outside allowed-files → violation surfaced with prefix."""
    from awf.core.wf_scope import check_scope_violations

    root = tmp_path / "root"
    sibling = tmp_path / "sibling-api"
    root.mkdir()
    sibling.mkdir()

    _setup_sibling_repo(
        sibling,
        base_files=["src/handler.ts", "src/leak.ts"],
        changed_files=["src/handler.ts", "src/leak.ts"],
        branch_name="feature",
    )
    _setup_repo_with_changes(
        root,
        planned=["@api/src/handler.ts"],
        expanded=[],
        base_files=["src/main.ts"],
        changed_files=[],
    )
    _write_manifest(root, [{"name": "api", "path": "../sibling-api", "branch": "main"}])

    result = check_scope_violations(root, base_branch="main")
    violation_paths = {v.path for v in result.violations}
    assert "@api/src/leak.ts" in violation_paths
    assert result.violation_count == 1


def test_scope_check_aggregates_violations_across_repos(tmp_path: Path):
    """Each repo with 1 violation → top-level shows 2 violations with prefixes."""
    from awf.core.wf_scope import check_scope_violations

    root = tmp_path / "root"
    sibling = tmp_path / "sibling-api"
    root.mkdir()
    sibling.mkdir()

    _setup_sibling_repo(
        sibling,
        base_files=["src/handler.ts", "src/leak.ts"],
        changed_files=["src/leak.ts"],
        branch_name="feature",
    )
    _setup_repo_with_changes(
        root,
        planned=["src/main.ts"],
        expanded=[],
        base_files=["src/main.ts", "src/extra.ts"],
        changed_files=["src/extra.ts"],
    )
    _write_manifest(root, [{"name": "api", "path": "../sibling-api", "branch": "main"}])

    result = check_scope_violations(root, base_branch="main")
    paths = sorted(v.path for v in result.violations)
    assert paths == ["@api/src/leak.ts", "src/extra.ts"]


def test_scope_check_unknown_prefix_in_allowed_files(tmp_path: Path):
    """`@unknown/` in planned_files → violation surfaced (catch typos)."""
    from awf.core.wf_scope import check_scope_violations

    _setup_repo_with_changes(
        tmp_path,
        planned=["src/main.ts", "@typo/foo.ts"],
        expanded=[],
        base_files=["src/main.ts"],
        changed_files=["src/main.ts"],
    )
    # no manifest at all
    result = check_scope_violations(tmp_path, base_branch="main")
    typos = [v for v in result.violations if v.path == "@typo/foo.ts"]
    assert len(typos) == 1
    assert "unknown sibling" in typos[0].reason


def test_scope_check_missing_sibling_path_yields_repo_error(tmp_path: Path):
    """Sibling path missing → RepoScopeResult.error="missing_repo", not a violation."""
    from awf.core.wf_scope import REPO_ERROR_MISSING, check_scope_violations

    _setup_repo_with_changes(
        tmp_path,
        planned=["src/main.ts"],
        expanded=[],
        base_files=["src/main.ts"],
        changed_files=["src/main.ts"],
    )
    _write_manifest(tmp_path, [{"name": "ghost", "path": "../ghost-repo"}])

    result = check_scope_violations(tmp_path, base_branch="main")
    assert result.repo_error_count == 1
    assert result.repo_errors[0].name == "ghost"
    assert result.repo_errors[0].error == REPO_ERROR_MISSING
    # missing-repo is not counted as a scope violation
    assert result.violation_count == 0


def test_scope_check_sibling_not_git_repo(tmp_path: Path):
    """Path exists but no `.git` → error="not_git_repo"."""
    from awf.core.wf_scope import REPO_ERROR_NOT_GIT, check_scope_violations

    root = tmp_path / "root"
    plain_dir = tmp_path / "sibling-plain"
    root.mkdir()
    plain_dir.mkdir()
    (plain_dir / "file.txt").write_text("x", encoding="utf-8")

    _setup_repo_with_changes(
        root,
        planned=["src/main.ts"],
        expanded=[],
        base_files=["src/main.ts"],
        changed_files=["src/main.ts"],
    )
    _write_manifest(root, [{"name": "plain", "path": "../sibling-plain"}])

    result = check_scope_violations(root, base_branch="main")
    assert result.repo_error_count == 1
    assert result.repo_errors[0].error == REPO_ERROR_NOT_GIT


def test_scope_check_sibling_branch_fallback_to_default(tmp_path: Path):
    """Sibling without explicit branch → fallback to default branch detection."""
    from awf.core.wf_scope import check_scope_violations

    root = tmp_path / "root"
    sibling = tmp_path / "sibling-api"
    root.mkdir()
    sibling.mkdir()

    _setup_sibling_repo(
        sibling,
        base_files=["src/handler.ts"],
        changed_files=["src/handler.ts"],
        branch_name="feature",
    )
    _setup_repo_with_changes(
        root,
        planned=["@api/src/handler.ts"],
        expanded=[],
        base_files=["src/main.ts"],
        changed_files=[],
    )
    # branch field omitted on purpose — fallback should resolve to "main".
    _write_manifest(root, [{"name": "api", "path": "../sibling-api"}])

    result = check_scope_violations(root, base_branch="main")
    # Sibling's base branch resolved from default candidates.
    sibling_result = next(r for r in result.per_repo if r.name == "api")
    assert sibling_result.error is None
    assert sibling_result.base_branch == "main"
    assert result.violation_count == 0


def test_scope_check_to_json_includes_per_repo_block(tmp_path: Path):
    """to_json() exposes per_repo + repo_errors for downstream consumers."""
    from awf.core.wf_scope import check_scope_violations

    root = tmp_path / "root"
    sibling = tmp_path / "sibling-api"
    root.mkdir()
    sibling.mkdir()

    _setup_sibling_repo(
        sibling,
        base_files=["src/handler.ts"],
        changed_files=["src/handler.ts"],
        branch_name="feature",
    )
    _setup_repo_with_changes(
        root,
        planned=["@api/src/handler.ts"],
        expanded=[],
        base_files=["src/main.ts"],
        changed_files=[],
    )
    _write_manifest(root, [
        {"name": "api", "path": "../sibling-api", "branch": "main"},
        {"name": "ghost", "path": "../ghost-repo"},
    ])

    payload = check_scope_violations(root, base_branch="main").to_json()
    assert "per_repo" in payload
    assert {p["name"] for p in payload["per_repo"]} == {"", "api", "ghost"}
    # repo_errors mirrors per_repo entries with non-null error.
    assert len(payload["repo_errors"]) == 1
    assert payload["repo_errors"][0]["name"] == "ghost"


def test_load_sibling_repos_rejects_malformed_entries(tmp_path: Path):
    import pytest
    from awf.core.wf_scope import load_sibling_repos

    cases = [
        [{"name": "", "path": "../x"}],                  # empty name
        [{"name": "a/b", "path": "../x"}],               # invalid chars
        [{"name": "a", "path": ""}],                      # empty path
        [{"name": "a", "path": "./sub"}],                 # not sibling
        [{"name": "a", "path": "/abs/path"}],             # absolute
        [{"name": "dup", "path": "../a"}, {"name": "dup", "path": "../b"}],  # duplicate
    ]
    for siblings in cases:
        _write_manifest(tmp_path, siblings)
        with pytest.raises(ValueError):
            load_sibling_repos(tmp_path)


def test_load_sibling_repos_empty_when_field_missing(tmp_path: Path):
    """No sibling_repos field or empty list → []. Backward-compat path."""
    from awf.core.wf_scope import load_sibling_repos

    # No manifest at all
    assert load_sibling_repos(tmp_path) == []

    # Manifest without sibling_repos
    (tmp_path / ".workflow").mkdir()
    (tmp_path / ".workflow" / "manifest.json").write_text(
        json.dumps({"version": "1.0.0"}), encoding="utf-8"
    )
    assert load_sibling_repos(tmp_path) == []

    # Empty list
    _write_manifest(tmp_path, [])
    assert load_sibling_repos(tmp_path) == []
