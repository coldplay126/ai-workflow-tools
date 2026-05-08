"""Integration tests for `awf analyze --cycles`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.commands import analyze as analyze_module
from awf.commands.analyze import _run_cycles
from awf.core.import_graph import ImportGraph


def _stub_resolver(monkeypatch, docs_root: Path, gh_root: Path, service: str) -> None:
    monkeypatch.setattr(
        analyze_module,
        "_resolve_service_docs_root",
        lambda _args: (docs_root, gh_root, service),
    )


def _stub_units(monkeypatch, units: dict[str, dict]) -> None:
    monkeypatch.setattr(
        analyze_module,
        "_scan_ai_context_units",
        lambda _docs, _service: units,
    )


def _make_unit(tmp_path: Path, name: str, graph: ImportGraph | None) -> dict:
    ai_context = tmp_path / name / ".ai-context"
    ai_context.mkdir(parents=True, exist_ok=True)
    if graph is not None:
        graph.save(ai_context / ".tmp" / "import-graph.json")
    return {"name": name, "path": str(ai_context), "completed_at": "2026-05-08T00:00:00Z"}


def _args(service: str = "svc") -> argparse.Namespace:
    return argparse.Namespace(service=service)


def test_run_cycles_returns_zero_with_no_units(monkeypatch, capsys, tmp_path: Path):
    _stub_resolver(monkeypatch, tmp_path, tmp_path, "svc")
    _stub_units(monkeypatch, {})

    rc = _run_cycles(_args())
    out = capsys.readouterr().out

    assert rc == 0
    assert "No analyzed units" in out


def test_run_cycles_reports_clean_units(monkeypatch, capsys, tmp_path: Path):
    from awf.core.import_graph import GraphEdge, GraphNode

    g = ImportGraph()
    for p in ("src/a.ts", "src/b.ts"):
        g.upsert_node(
            GraphNode(p, "typescript", "c", "e", ("X",), "2026-05-08T00:00:00Z")
        )
    g.replace_edges_from(
        "src/a.ts",
        [GraphEdge(src="src/a.ts", dst="src/b.ts", symbols=("X",), kind="import")],
    )

    units = {"clean-unit": _make_unit(tmp_path, "clean-unit", g)}
    _stub_resolver(monkeypatch, tmp_path, tmp_path, "svc")
    _stub_units(monkeypatch, units)

    rc = _run_cycles(_args())
    out = capsys.readouterr().out

    assert rc == 0
    assert "clean-unit: no cycles" in out
    assert "no graph: 0" in out


def test_run_cycles_detects_and_reports_cycle(monkeypatch, capsys, tmp_path: Path):
    from awf.core.import_graph import GraphEdge, GraphNode

    g = ImportGraph()
    for p in ("a.ts", "b.ts", "c.ts"):
        g.upsert_node(
            GraphNode(p, "typescript", "c", "e", ("X",), "2026-05-08T00:00:00Z")
        )
    # a → b → a (cycle of size 2)
    g.replace_edges_from("a.ts", [GraphEdge(src="a.ts", dst="b.ts", symbols=(), kind="import")])
    g.replace_edges_from("b.ts", [GraphEdge(src="b.ts", dst="a.ts", symbols=(), kind="import")])

    units = {"loopy": _make_unit(tmp_path, "loopy", g)}
    _stub_resolver(monkeypatch, tmp_path, tmp_path, "svc")
    _stub_units(monkeypatch, units)

    rc = _run_cycles(_args())
    out = capsys.readouterr().out

    assert rc == 1
    assert "loopy: 1 cycle(s)" in out
    assert "a.ts" in out and "b.ts" in out
    assert "units with cycles: 1" in out


def test_run_cycles_skips_units_without_graph(monkeypatch, capsys, tmp_path: Path):
    units = {"no-graph": _make_unit(tmp_path, "no-graph", graph=None)}
    _stub_resolver(monkeypatch, tmp_path, tmp_path, "svc")
    _stub_units(monkeypatch, units)

    rc = _run_cycles(_args())
    out = capsys.readouterr().out

    # No cycles found anywhere → exit 0 even though one unit had no data.
    assert rc == 0
    assert "no import graph saved" in out
    assert "no graph: 1" in out


def test_run_cycles_mixed_clean_and_cyclic(monkeypatch, capsys, tmp_path: Path):
    from awf.core.import_graph import GraphEdge, GraphNode

    clean = ImportGraph()
    for p in ("c1.ts", "c2.ts"):
        clean.upsert_node(
            GraphNode(p, "typescript", "c", "e", ("X",), "2026-05-08T00:00:00Z")
        )
    clean.replace_edges_from(
        "c1.ts", [GraphEdge(src="c1.ts", dst="c2.ts", symbols=(), kind="import")]
    )

    cyclic = ImportGraph()
    for p in ("x.ts", "y.ts"):
        cyclic.upsert_node(
            GraphNode(p, "typescript", "c", "e", ("X",), "2026-05-08T00:00:00Z")
        )
    cyclic.replace_edges_from(
        "x.ts", [GraphEdge(src="x.ts", dst="y.ts", symbols=(), kind="import")]
    )
    cyclic.replace_edges_from(
        "y.ts", [GraphEdge(src="y.ts", dst="x.ts", symbols=(), kind="import")]
    )

    units = {
        "alpha": _make_unit(tmp_path, "alpha", clean),
        "beta": _make_unit(tmp_path, "beta", cyclic),
    }
    _stub_resolver(monkeypatch, tmp_path, tmp_path, "svc")
    _stub_units(monkeypatch, units)

    rc = _run_cycles(_args())
    out = capsys.readouterr().out

    assert rc == 1  # any cycle in the service flips exit code
    assert "alpha: no cycles" in out
    assert "beta: 1 cycle(s)" in out
    assert "clean: 1" in out and "units with cycles: 1" in out
