"""Tests for awf.core.wf_scope — allowed-files expansion via import graph."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core.import_graph import GraphEdge, GraphNode, ImportGraph
from awf.core.wf_scope import (
    apply_expansion_to_payload,
    expand_allowed_files,
    load_allowed_files,
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


def test_load_allowed_files_raises_for_missing(tmp_path: Path):
    import pytest

    with pytest.raises(FileNotFoundError):
        load_allowed_files(tmp_path)
