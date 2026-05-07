"""Unit tests for awf.core.import_graph and lang_adapters.regex_baseline."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core.import_graph import (
    EDGE_KIND_IMPORT,
    EDGE_KIND_TYPE_ONLY,
    GraphEdge,
    GraphNode,
    ImportGraph,
)
from awf.core.lang_adapters import get as get_extractor


# --------------------------------------------------------------------------
# Graph mechanics
# --------------------------------------------------------------------------

def _node(path: str, exports_hash: str = "exp") -> GraphNode:
    return GraphNode(
        path=path,
        language="typescript",
        content_hash=f"c-{path}",
        exports_hash=exports_hash,
        exports=("X",),
        last_analyzed_at="2026-05-08T00:00:00Z",
    )


def _import_edge(src: str, dst: str, kind: str = EDGE_KIND_IMPORT) -> GraphEdge:
    return GraphEdge(src=src, dst=dst, symbols=("X",), kind=kind)


def test_reverse_dependents_chain():
    g = ImportGraph()
    for p in ("types.ts", "service.ts", "controller.ts"):
        g.upsert_node(_node(p))
    g.replace_edges_from("service.ts", [_import_edge("service.ts", "types.ts")])
    g.replace_edges_from("controller.ts", [_import_edge("controller.ts", "service.ts")])

    deps = g.reverse_dependents("types.ts")
    assert deps == {"service.ts", "controller.ts"}
    assert g.reverse_dependents("controller.ts") == set()


def test_reverse_dependents_excludes_type_only():
    g = ImportGraph()
    for p in ("types.ts", "service.ts"):
        g.upsert_node(_node(p))
    g.replace_edges_from("service.ts", [_import_edge("service.ts", "types.ts", EDGE_KIND_TYPE_ONLY)])

    assert g.reverse_dependents("types.ts") == set()
    assert g.reverse_dependents("types.ts", runtime_only=False) == {"service.ts"}


def test_reverse_dependents_handles_cycle():
    g = ImportGraph()
    for p in ("a.ts", "b.ts", "c.ts"):
        g.upsert_node(_node(p))
    # a -> b -> c -> a (cycle)
    g.replace_edges_from("a.ts", [_import_edge("a.ts", "b.ts")])
    g.replace_edges_from("b.ts", [_import_edge("b.ts", "c.ts")])
    g.replace_edges_from("c.ts", [_import_edge("c.ts", "a.ts")])

    # All three depend on each other; result must terminate.
    assert g.reverse_dependents("a.ts") == {"b.ts", "c.ts"}


def test_reverse_dependents_max_depth():
    g = ImportGraph()
    for p in ("d0.ts", "d1.ts", "d2.ts", "d3.ts"):
        g.upsert_node(_node(p))
    g.replace_edges_from("d1.ts", [_import_edge("d1.ts", "d0.ts")])
    g.replace_edges_from("d2.ts", [_import_edge("d2.ts", "d1.ts")])
    g.replace_edges_from("d3.ts", [_import_edge("d3.ts", "d2.ts")])

    assert g.reverse_dependents("d0.ts", max_depth=1) == {"d1.ts"}
    assert g.reverse_dependents("d0.ts", max_depth=2) == {"d1.ts", "d2.ts"}
    assert g.reverse_dependents("d0.ts") == {"d1.ts", "d2.ts", "d3.ts"}


def test_transitive_imports():
    g = ImportGraph()
    for p in ("types.ts", "service.ts", "controller.ts"):
        g.upsert_node(_node(p))
    g.replace_edges_from("service.ts", [_import_edge("service.ts", "types.ts")])
    g.replace_edges_from("controller.ts", [_import_edge("controller.ts", "service.ts")])

    assert g.transitive_imports("controller.ts") == {"service.ts", "types.ts"}
    assert g.transitive_imports("types.ts") == set()


def test_remove_path_clears_edges_both_directions():
    g = ImportGraph()
    for p in ("a.ts", "b.ts", "c.ts"):
        g.upsert_node(_node(p))
    g.replace_edges_from("a.ts", [_import_edge("a.ts", "b.ts")])
    g.replace_edges_from("b.ts", [_import_edge("b.ts", "c.ts")])

    g.remove_path("b.ts")
    assert "b.ts" not in g.nodes
    assert g.reverse_dependents("c.ts") == set()
    assert g.transitive_imports("a.ts") == set()


def test_replace_edges_from_replaces_not_appends():
    g = ImportGraph()
    for p in ("a.ts", "b.ts", "c.ts"):
        g.upsert_node(_node(p))
    g.replace_edges_from("a.ts", [_import_edge("a.ts", "b.ts")])
    g.replace_edges_from("a.ts", [_import_edge("a.ts", "c.ts")])

    assert {e.dst for e in g.edges_from("a.ts")} == {"c.ts"}
    assert g.reverse_dependents("b.ts") == set()
    assert g.reverse_dependents("c.ts") == {"a.ts"}


def test_detect_cycles():
    g = ImportGraph()
    for p in ("a.ts", "b.ts", "c.ts", "leaf.ts"):
        g.upsert_node(_node(p))
    g.replace_edges_from("a.ts", [_import_edge("a.ts", "b.ts")])
    g.replace_edges_from("b.ts", [_import_edge("b.ts", "a.ts"), _import_edge("b.ts", "leaf.ts")])
    g.replace_edges_from("c.ts", [_import_edge("c.ts", "leaf.ts")])

    cycles = g.detect_cycles()
    assert cycles == [["a.ts", "b.ts"]]


def test_save_and_load_roundtrip(tmp_path: Path):
    g = ImportGraph()
    g.upsert_node(_node("types.ts"))
    g.upsert_node(_node("service.ts"))
    g.replace_edges_from("service.ts", [_import_edge("service.ts", "types.ts")])

    out = tmp_path / "import-graph.json"
    g.save(out, generated_at="2026-05-08T00:00:00Z")

    loaded = ImportGraph.load(out)
    assert loaded is not None
    assert set(loaded.nodes.keys()) == {"types.ts", "service.ts"}
    assert loaded.reverse_dependents("types.ts") == {"service.ts"}

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["version"] == "1.0"
    assert payload["stats"]["node_count"] == 2
    assert payload["stats"]["edge_count"] == 1


def test_load_returns_none_when_missing_or_corrupt(tmp_path: Path):
    assert ImportGraph.load(tmp_path / "nope.json") is None

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert ImportGraph.load(bad) is None

    wrong_version = tmp_path / "v2.json"
    wrong_version.write_text(json.dumps({"version": "2.0", "nodes": {}, "edges": []}), encoding="utf-8")
    assert ImportGraph.load(wrong_version) is None


# --------------------------------------------------------------------------
# Regex baseline export extractor
# --------------------------------------------------------------------------

def test_regex_baseline_typescript():
    extractor = get_extractor("regex_baseline")
    src = """\
export const foo = 1;
export function bar() {}
export class Baz {}
export interface User { id: string }
export type ID = string;
export { qux, quux as renamed, type Inner } from './x';
export default class App {}
"""
    syms = extractor.extract(src, "typescript")
    by_name = {s.name: s.kind for s in syms}
    assert by_name["foo"] == "value"
    assert by_name["bar"] == "value"
    assert by_name["Baz"] == "value"
    assert by_name["User"] == "type"
    assert by_name["ID"] == "type"
    assert by_name["qux"] == "value"
    assert by_name["renamed"] == "value"
    assert by_name["Inner"] == "type"
    assert by_name["App"] == "default"


def test_regex_baseline_python_with_dunder_all():
    extractor = get_extractor("regex_baseline")
    src = """\
__all__ = ["public_fn", "PublicClass"]

def public_fn(): pass
def _private_fn(): pass
class PublicClass: pass
class _Hidden: pass
"""
    syms = extractor.extract(src, "python")
    names = {s.name for s in syms}
    assert names == {"public_fn", "PublicClass"}


def test_regex_baseline_python_without_dunder_all_skips_private():
    extractor = get_extractor("regex_baseline")
    src = """\
def alpha(): pass
def _hidden(): pass
class Beta: pass
class _Hidden: pass
"""
    syms = extractor.extract(src, "python")
    names = {s.name for s in syms}
    assert names == {"alpha", "Beta"}


def test_regex_baseline_unknown_language_returns_empty():
    extractor = get_extractor("regex_baseline")
    assert extractor.extract("data {}", "hcl") == []


def test_regex_baseline_output_is_deterministic():
    extractor = get_extractor("regex_baseline")
    src = "export const z = 1;\nexport const a = 2;\nexport const m = 3;\n"
    a = extractor.extract(src, "typescript")
    b = extractor.extract(src, "typescript")
    assert a == b
    assert [s.name for s in a] == ["a", "m", "z"]
