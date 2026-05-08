"""Tests for awf.core.graph_builder and the new exports/import-with-kinds APIs."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core.analysis_files import (
    compute_changed_files,
    compute_exports_hash,
    load_hashes_file,
    save_hashes_file,
)
from awf.core.graph_builder import (
    DISABLE_ENV_VAR,
    build_and_save_graph,
    build_graph,
    expand_stage1_targets_with_import_graph,
    graph_path_for,
    transitive_invalidation_status,
)
from awf.core.import_graph import ImportGraph
from awf.core.imports import extract_imports_with_kinds


# --------------------------------------------------------------------------
# compute_exports_hash
# --------------------------------------------------------------------------

def test_exports_hash_changes_with_signature():
    a = compute_exports_hash("export const foo = 1;\nexport const bar = 2;\n", "typescript")
    b = compute_exports_hash("export const foo = 1;\nexport const baz = 2;\n", "typescript")
    assert a and b and a != b


def test_exports_hash_stable_across_cosmetic_changes():
    src1 = "export const foo = 1;\nexport function bar() {}\n"
    src2 = "// renamed comment\nexport const foo = 2;\nexport function bar() { /* impl */ }\n"
    assert compute_exports_hash(src1, "typescript") == compute_exports_hash(src2, "typescript")


def test_exports_hash_empty_for_unsupported_language():
    # No extractor for HCL/Terraform yet — returns empty string and lets
    # callers fall back to content_hash semantics.
    assert compute_exports_hash('resource "x" "y" {}', "terraform") == ""


# --------------------------------------------------------------------------
# extract_imports_with_kinds — TypeScript flavors
# --------------------------------------------------------------------------

def test_imports_with_kinds_typescript_runtime_and_type_only():
    src = """
import { User } from './types';
import type { Internal } from './internal-types';
import { Helper } from './helper';
"""
    edges = dict(extract_imports_with_kinds(src, "typescript"))
    assert edges["./types"] == "import"
    assert edges["./internal-types"] == "type-only"
    assert edges["./helper"] == "import"


def test_imports_with_kinds_typescript_reexport_is_runtime():
    src = "export { foo } from './barrel';\nexport * from './all';\n"
    edges = dict(extract_imports_with_kinds(src, "typescript"))
    assert edges["./barrel"] == "reexport"
    assert edges["./all"] == "reexport"


def test_imports_with_kinds_typescript_dedup_prefers_runtime_over_type_only():
    src = """
import type { A } from './shared';
import { B } from './shared';
"""
    edges = dict(extract_imports_with_kinds(src, "typescript"))
    # When the same module is imported twice with different kinds, runtime wins.
    assert edges["./shared"] == "import"


def test_imports_with_kinds_excludes_scoped_and_node_modules_paths():
    src = """
import x from '@scope/pkg';
import y from 'node_modules/x';
import local from './local';
"""
    edges = dict(extract_imports_with_kinds(src, "typescript"))
    # @scope/* and node_modules/* paths are excluded at the regex layer
    # (consistent with extract_imports policy). Bare package names like
    # "lodash" pass through here but are filtered later by
    # resolve_import_to_file when no on-disk source can be found.
    assert "./local" in edges
    assert "@scope/pkg" not in edges
    assert "node_modules/x" not in edges


def test_imports_with_kinds_python_falls_back_to_runtime_kind():
    src = "from .types import User\nimport os\n"
    edges = dict(extract_imports_with_kinds(src, "python"))
    # Python doesn't have type-only imports without AST-level analysis.
    assert all(kind == "import" for kind in edges.values())


# --------------------------------------------------------------------------
# build_graph — full pipeline against a synthetic repo
# --------------------------------------------------------------------------

def _make_context(repo_root: Path) -> SimpleNamespace:
    """Minimal AnalysisContext stand-in for graph_builder."""
    ai_dir = repo_root / ".ai-context"
    ai_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        github_root=repo_root,
        repo_root=repo_root,
        ai_context_dir=ai_dir,
        service="fixture",
        domain="domain",
        mode="standard",
    )


def _write(p: Path, content: str) -> dict[str, str]:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {
        "path": str(p.relative_to(p.parents[1] if p.parents[1].name else p.parents[0])),
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def _entry(repo_root: Path, rel_path: str) -> dict[str, str]:
    path = repo_root / rel_path
    return {"path": rel_path, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def test_build_graph_records_edges_between_known_files(tmp_path: Path):
    # Synthetic TS package: types.ts ← service.ts ← controller.ts
    types_path = tmp_path / "src" / "types.ts"
    service_path = tmp_path / "src" / "service.ts"
    controller_path = tmp_path / "src" / "controller.ts"

    types_path.parent.mkdir(parents=True, exist_ok=True)
    types_path.write_text("export interface User { id: string }\n", encoding="utf-8")
    service_path.write_text("import { User } from './types';\nexport function svc(): User { throw 0; }\n", encoding="utf-8")
    controller_path.write_text("import { svc } from './service';\nexport function ctl() { svc(); }\n", encoding="utf-8")

    entries = []
    for p in (types_path, service_path, controller_path):
        rel = str(p.relative_to(tmp_path))
        entries.append({"path": rel, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})

    ctx = _make_context(tmp_path)
    graph = build_graph(ctx, entries)

    assert {"src/types.ts", "src/service.ts", "src/controller.ts"} <= set(graph.nodes.keys())
    # service depends on types; controller depends on service.
    assert graph.reverse_dependents("src/types.ts") == {"src/service.ts", "src/controller.ts"}
    assert graph.reverse_dependents("src/service.ts") == {"src/controller.ts"}


def test_build_graph_excludes_type_only_from_runtime_invalidation(tmp_path: Path):
    types_path = tmp_path / "src" / "types.ts"
    consumer_path = tmp_path / "src" / "consumer.ts"
    types_path.parent.mkdir(parents=True, exist_ok=True)
    types_path.write_text("export interface User { id: string }\n", encoding="utf-8")
    consumer_path.write_text(
        "import type { User } from './types';\n"
        "export function ok(u: User) { return u.id; }\n",
        encoding="utf-8",
    )

    entries = []
    for p in (types_path, consumer_path):
        rel = str(p.relative_to(tmp_path))
        entries.append({"path": rel, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})

    ctx = _make_context(tmp_path)
    graph = build_graph(ctx, entries)

    # Runtime closure: empty (only edge is type-only).
    assert graph.reverse_dependents("src/types.ts") == set()
    # Full closure (runtime_only=False) includes the consumer.
    assert graph.reverse_dependents("src/types.ts", runtime_only=False) == {"src/consumer.ts"}


def test_build_graph_skips_imports_outside_known_scope(tmp_path: Path):
    # Only `service.ts` is in scope; its import target `types.ts` is NOT.
    service_path = tmp_path / "src" / "service.ts"
    out_of_scope = tmp_path / "src" / "types.ts"
    service_path.parent.mkdir(parents=True, exist_ok=True)
    out_of_scope.write_text("export interface User { id: string }\n", encoding="utf-8")
    service_path.write_text("import { User } from './types';\n", encoding="utf-8")

    entries = [
        {
            "path": "src/service.ts",
            "sha256": hashlib.sha256(service_path.read_bytes()).hexdigest(),
        }
    ]

    ctx = _make_context(tmp_path)
    graph = build_graph(ctx, entries)
    # No edges because the destination is not in the analyzed scope.
    assert graph.edge_count() == 0


def test_build_and_save_graph_writes_expected_path(tmp_path: Path):
    f = tmp_path / "src" / "a.ts"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("export const a = 1;\n", encoding="utf-8")

    ctx = _make_context(tmp_path)
    entries = [{"path": "src/a.ts", "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}]

    out = build_and_save_graph(ctx, entries)
    assert out is not None
    assert out == graph_path_for(ctx)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["version"] == "1.0"
    assert "src/a.ts" in payload["nodes"]


def test_build_and_save_graph_handles_missing_files_gracefully(tmp_path: Path):
    ctx = _make_context(tmp_path)
    entries = [{"path": "does/not/exist.ts", "sha256": "deadbeef"}]
    out = build_and_save_graph(ctx, entries)
    # Save still succeeds with an empty graph; never raises.
    assert out is not None
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["stats"]["node_count"] == 0


def test_changed_file_comparison_can_use_snapshot_after_hashes_are_saved(tmp_path: Path):
    source_path = tmp_path / "src" / "a.ts"
    stable_path = tmp_path / "src" / "stable.ts"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("export const a = 1;\n", encoding="utf-8")
    stable_path.write_text("export const stable = true;\n", encoding="utf-8")

    ctx = _make_context(tmp_path)
    old_entries = [_entry(tmp_path, "src/a.ts"), _entry(tmp_path, "src/stable.ts")]
    save_hashes_file(ctx, old_entries)
    previous_hashes = load_hashes_file(ctx)

    source_path.write_text("export const a = 2;\n", encoding="utf-8")
    current_entries = [_entry(tmp_path, "src/a.ts"), _entry(tmp_path, "src/stable.ts")]
    save_hashes_file(ctx, current_entries)

    changed, unchanged = compute_changed_files(ctx, current_entries, saved_hashes=previous_hashes)
    assert [entry["path"] for entry in changed] == ["src/a.ts"]
    assert [entry["path"] for entry in unchanged] == ["src/stable.ts"]


def test_graph_invalidation_adds_reverse_dependents_for_export_changes(tmp_path: Path):
    types_path = tmp_path / "src" / "types.ts"
    service_path = tmp_path / "src" / "service.ts"
    controller_path = tmp_path / "src" / "controller.ts"
    unrelated_path = tmp_path / "src" / "unrelated.ts"
    types_path.parent.mkdir(parents=True, exist_ok=True)
    types_path.write_text("export interface User { id: string }\n", encoding="utf-8")
    service_path.write_text(
        "import type { User } from './types';\nexport function svc(u: User) { return u.id; }\n",
        encoding="utf-8",
    )
    controller_path.write_text("import { svc } from './service';\nexport function ctl() { return svc as unknown; }\n", encoding="utf-8")
    unrelated_path.write_text("export const untouched = true;\n", encoding="utf-8")

    ctx = _make_context(tmp_path)
    old_entries = [
        _entry(tmp_path, "src/types.ts"),
        _entry(tmp_path, "src/service.ts"),
        _entry(tmp_path, "src/controller.ts"),
        _entry(tmp_path, "src/unrelated.ts"),
    ]
    build_graph(ctx, old_entries).save(graph_path_for(ctx), generated_at="2026-05-08T00:00:00Z")

    types_path.write_text("export interface Account { id: string }\n", encoding="utf-8")
    current_entries = [
        _entry(tmp_path, "src/types.ts"),
        _entry(tmp_path, "src/service.ts"),
        _entry(tmp_path, "src/controller.ts"),
        _entry(tmp_path, "src/unrelated.ts"),
    ]

    result = expand_stage1_targets_with_import_graph(
        ctx,
        current_entries,
        changed_files=[current_entries[0]],
    )

    assert [entry["path"] for entry in result.target_files] == [
        "src/types.ts",
        "src/service.ts",
        "src/controller.ts",
    ]
    assert set(result.indirect_paths) == {"src/service.ts", "src/controller.ts"}
    assert result.invalidating_paths == ("src/types.ts",)
    assert [entry["path"] for entry in result.unchanged_files] == ["src/unrelated.ts"]


def test_graph_invalidation_keeps_dependents_cached_when_exports_unchanged(tmp_path: Path):
    shared_path = tmp_path / "src" / "shared.ts"
    consumer_path = tmp_path / "src" / "consumer.ts"
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    shared_path.write_text("export const value = 1;\n", encoding="utf-8")
    consumer_path.write_text("import { value } from './shared';\nexport const result = value;\n", encoding="utf-8")

    ctx = _make_context(tmp_path)
    old_entries = [_entry(tmp_path, "src/shared.ts"), _entry(tmp_path, "src/consumer.ts")]
    build_graph(ctx, old_entries).save(graph_path_for(ctx), generated_at="2026-05-08T00:00:00Z")

    shared_path.write_text("// implementation changed\nexport const value = 2;\n", encoding="utf-8")
    current_entries = [_entry(tmp_path, "src/shared.ts"), _entry(tmp_path, "src/consumer.ts")]

    result = expand_stage1_targets_with_import_graph(
        ctx,
        current_entries,
        changed_files=[current_entries[0]],
    )

    assert [entry["path"] for entry in result.target_files] == ["src/shared.ts"]
    assert result.indirect_paths == ()
    assert [entry["path"] for entry in result.unchanged_files] == ["src/consumer.ts"]


def test_graph_invalidation_without_previous_graph_uses_direct_changes_only(tmp_path: Path):
    shared_path = tmp_path / "src" / "shared.ts"
    consumer_path = tmp_path / "src" / "consumer.ts"
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    shared_path.write_text("export interface User { id: string }\n", encoding="utf-8")
    consumer_path.write_text("import { User } from './shared';\nexport const user = {} as User;\n", encoding="utf-8")

    ctx = _make_context(tmp_path)
    current_entries = [_entry(tmp_path, "src/shared.ts"), _entry(tmp_path, "src/consumer.ts")]
    result = expand_stage1_targets_with_import_graph(
        ctx,
        current_entries,
        changed_files=[current_entries[0]],
    )

    assert [entry["path"] for entry in result.target_files] == ["src/shared.ts"]
    assert result.indirect_paths == ()
    assert [entry["path"] for entry in result.unchanged_files] == ["src/consumer.ts"]


# --------------------------------------------------------------------------
# transitive_invalidation_status — escape hatch
# --------------------------------------------------------------------------

def test_transitive_invalidation_default_is_enabled(monkeypatch):
    monkeypatch.delenv(DISABLE_ENV_VAR, raising=False)
    enabled, reason = transitive_invalidation_status({})
    assert enabled is True
    assert reason == "default"


def test_transitive_invalidation_env_disables(monkeypatch):
    for value in ("1", "true", "TRUE", "Yes", "on"):
        monkeypatch.setenv(DISABLE_ENV_VAR, value)
        enabled, reason = transitive_invalidation_status({})
        assert enabled is False, f"value {value!r} should disable"
        assert reason == f"env:{DISABLE_ENV_VAR}"


def test_transitive_invalidation_env_falsy_keeps_default(monkeypatch):
    for value in ("", "0", "false", "no", "off", "anything-else"):
        monkeypatch.setenv(DISABLE_ENV_VAR, value)
        enabled, reason = transitive_invalidation_status({})
        assert enabled is True, f"value {value!r} must not disable"
        assert reason == "default"


def test_transitive_invalidation_config_disables(monkeypatch):
    monkeypatch.delenv(DISABLE_ENV_VAR, raising=False)
    pipeline_config = {"transitive_invalidation": {"enabled": False}}
    enabled, reason = transitive_invalidation_status(pipeline_config)
    assert enabled is False
    assert reason == "config:transitive_invalidation.enabled=false"


def test_transitive_invalidation_env_overrides_config(monkeypatch):
    # Even when config says enabled=true, the env var emergency switch wins.
    monkeypatch.setenv(DISABLE_ENV_VAR, "1")
    pipeline_config = {"transitive_invalidation": {"enabled": True}}
    enabled, reason = transitive_invalidation_status(pipeline_config)
    assert enabled is False
    assert reason == f"env:{DISABLE_ENV_VAR}"


def test_transitive_invalidation_handles_missing_section(monkeypatch):
    monkeypatch.delenv(DISABLE_ENV_VAR, raising=False)
    # No pipeline_config at all — should default to enabled.
    assert transitive_invalidation_status(None) == (True, "default")
    # Section present but malformed (not a dict) — treat as default.
    assert transitive_invalidation_status({"transitive_invalidation": "off"}) == (True, "default")


# --------------------------------------------------------------------------
# compute_transitive_stale_paths — used by --check / --catalog
# --------------------------------------------------------------------------

def _build_simple_graph_with_chain() -> ImportGraph:
    """types.ts ← service.ts ← controller.ts plus an unrelated leaf.ts."""
    from awf.core.import_graph import GraphEdge, GraphNode

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


def test_transitive_stale_includes_chain_dependents():
    from awf.core.graph_builder import compute_transitive_stale_paths

    g = _build_simple_graph_with_chain()
    stale = compute_transitive_stale_paths(g, ["src/types.ts"])
    assert stale == {"src/service.ts", "src/controller.ts"}


def test_transitive_stale_excludes_directly_changed_set():
    from awf.core.graph_builder import compute_transitive_stale_paths

    g = _build_simple_graph_with_chain()
    # service.ts is directly changed AND a dependent of types.ts. It must
    # not appear in the transitive set when types.ts is also direct.
    stale = compute_transitive_stale_paths(g, ["src/types.ts", "src/service.ts"])
    assert stale == {"src/controller.ts"}


def test_transitive_stale_empty_when_graph_missing():
    from awf.core.graph_builder import compute_transitive_stale_paths

    assert compute_transitive_stale_paths(None, ["src/anything.ts"]) == set()


def test_transitive_stale_empty_when_no_direct_changes():
    from awf.core.graph_builder import compute_transitive_stale_paths

    g = _build_simple_graph_with_chain()
    assert compute_transitive_stale_paths(g, []) == set()


def test_transitive_stale_unrelated_paths_have_no_dependents():
    from awf.core.graph_builder import compute_transitive_stale_paths

    g = _build_simple_graph_with_chain()
    # leaf.ts has no reverse dependents.
    assert compute_transitive_stale_paths(g, ["src/leaf.ts"]) == set()


def test_transitive_stale_runtime_only_flag_passes_through():
    """The runtime_only kwarg threads down to ImportGraph.reverse_dependents."""
    from awf.core.graph_builder import compute_transitive_stale_paths
    from awf.core.import_graph import GraphEdge, GraphNode

    g = ImportGraph()
    for path in ("src/types.ts", "src/consumer.ts"):
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
        "src/consumer.ts",
        [GraphEdge(src="src/consumer.ts", dst="src/types.ts", symbols=("X",), kind="type-only")],
    )
    # Default (runtime_only=False) sees the type-only edge.
    assert compute_transitive_stale_paths(g, ["src/types.ts"]) == {"src/consumer.ts"}
    # runtime_only=True ignores type-only edges.
    assert compute_transitive_stale_paths(g, ["src/types.ts"], runtime_only=True) == set()
