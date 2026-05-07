"""Import graph for transitive cache invalidation.

Each node is a source file with two hashes — `content_hash` (whole file)
and `exports_hash` (normalized exported symbols only). When `exports_hash`
changes, every reverse dependent in the graph must be re-analyzed even if
its own `content_hash` did not change.

Edges record `kind` so that consumers can ignore type-only edges (e.g.
TypeScript `import type`) when computing runtime invalidation closures.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Iterator


GRAPH_VERSION = "1.0"

# Edge kinds carried by import statements.
EDGE_KIND_IMPORT = "import"
EDGE_KIND_REEXPORT = "reexport"
EDGE_KIND_TYPE_ONLY = "type-only"

_RUNTIME_EDGE_KINDS = frozenset({EDGE_KIND_IMPORT, EDGE_KIND_REEXPORT})


@dataclass(frozen=True)
class GraphNode:
    path: str
    language: str
    content_hash: str
    exports_hash: str
    exports: tuple[str, ...]
    last_analyzed_at: str

    def to_json(self) -> dict:
        return {
            "language": self.language,
            "content_hash": self.content_hash,
            "exports_hash": self.exports_hash,
            "exports": list(self.exports),
            "last_analyzed_at": self.last_analyzed_at,
        }

    @classmethod
    def from_json(cls, path: str, data: dict) -> "GraphNode":
        return cls(
            path=path,
            language=str(data.get("language", "")),
            content_hash=str(data.get("content_hash", "")),
            exports_hash=str(data.get("exports_hash", "")),
            exports=tuple(data.get("exports", [])),
            last_analyzed_at=str(data.get("last_analyzed_at", "")),
        )


@dataclass(frozen=True)
class GraphEdge:
    src: str
    dst: str
    symbols: tuple[str, ...]
    kind: str = EDGE_KIND_IMPORT

    def to_json(self) -> dict:
        return {
            "from": self.src,
            "to": self.dst,
            "symbols": list(self.symbols),
            "kind": self.kind,
        }

    @classmethod
    def from_json(cls, data: dict) -> "GraphEdge":
        return cls(
            src=str(data["from"]),
            dst=str(data["to"]),
            symbols=tuple(data.get("symbols", [])),
            kind=str(data.get("kind", EDGE_KIND_IMPORT)),
        )


@dataclass
class ImportGraph:
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    # Adjacency: src -> list of edges leaving src (one per dst).
    _out: dict[str, list[GraphEdge]] = field(default_factory=dict)
    # Reverse adjacency: dst -> set of src paths.
    _in: dict[str, set[str]] = field(default_factory=dict)

    # ---- mutation ---------------------------------------------------------

    def upsert_node(self, node: GraphNode) -> None:
        self.nodes[node.path] = node

    def remove_path(self, path: str) -> None:
        self.nodes.pop(path, None)
        for edge in self._out.pop(path, []):
            inbound = self._in.get(edge.dst)
            if inbound:
                inbound.discard(path)
                if not inbound:
                    self._in.pop(edge.dst, None)
        for src in list(self._in.pop(path, ())):
            self._out[src] = [e for e in self._out.get(src, []) if e.dst != path]
            if not self._out[src]:
                self._out.pop(src, None)

    def replace_edges_from(self, src: str, edges: Iterable[GraphEdge]) -> None:
        # Drop existing outbound edges for src and rebuild.
        for old in self._out.pop(src, []):
            inbound = self._in.get(old.dst)
            if inbound:
                inbound.discard(src)
                if not inbound:
                    self._in.pop(old.dst, None)
        new_edges = [e for e in edges if e.src == src]
        if new_edges:
            self._out[src] = new_edges
            for edge in new_edges:
                self._in.setdefault(edge.dst, set()).add(src)

    # ---- queries ----------------------------------------------------------

    def edges(self) -> Iterator[GraphEdge]:
        for outbound in self._out.values():
            yield from outbound

    def edges_from(self, src: str) -> list[GraphEdge]:
        return list(self._out.get(src, []))

    def edge_count(self) -> int:
        return sum(len(v) for v in self._out.values())

    def reverse_dependents(
        self,
        path: str,
        *,
        max_depth: int | None = None,
        runtime_only: bool = True,
    ) -> set[str]:
        """All files that (transitively) import `path`. Excludes `path` itself.

        Cycle-safe via visited set. `runtime_only=True` ignores type-only edges,
        which is the right default for cache invalidation since type-only
        imports are erased at compile time.
        """
        visited: set[str] = set()
        result: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(path, 0)])
        while queue:
            current, depth = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            if max_depth is not None and depth >= max_depth:
                continue
            for src in self._in.get(current, ()):
                # Find the edge(s) src -> current and check kind.
                edge_kinds = {
                    e.kind for e in self._out.get(src, []) if e.dst == current
                }
                if runtime_only and edge_kinds and edge_kinds.isdisjoint(_RUNTIME_EDGE_KINDS):
                    continue
                if src not in result:
                    result.add(src)
                    queue.append((src, depth + 1))
        # In a cycle, the seed can reach itself; callers expect self exclusion.
        result.discard(path)
        return result

    def transitive_imports(
        self,
        path: str,
        *,
        max_depth: int | None = None,
        runtime_only: bool = True,
    ) -> set[str]:
        """All files that `path` (transitively) imports. Excludes `path` itself."""
        visited: set[str] = set()
        result: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(path, 0)])
        while queue:
            current, depth = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            if max_depth is not None and depth >= max_depth:
                continue
            for edge in self._out.get(current, ()):
                if runtime_only and edge.kind not in _RUNTIME_EDGE_KINDS:
                    continue
                if edge.dst not in result:
                    result.add(edge.dst)
                    queue.append((edge.dst, depth + 1))
        result.discard(path)
        return result

    def detect_cycles(self) -> list[list[str]]:
        """Tarjan's SCC. Returns components with size > 1 (true cycles)."""
        index_counter = [0]
        stack: list[str] = []
        lowlinks: dict[str, int] = {}
        index: dict[str, int] = {}
        on_stack: dict[str, bool] = {}
        cycles: list[list[str]] = []

        def strongconnect(node: str) -> None:
            index[node] = index_counter[0]
            lowlinks[node] = index_counter[0]
            index_counter[0] += 1
            stack.append(node)
            on_stack[node] = True

            for edge in self._out.get(node, ()):
                successor = edge.dst
                if successor not in index:
                    strongconnect(successor)
                    lowlinks[node] = min(lowlinks[node], lowlinks[successor])
                elif on_stack.get(successor):
                    lowlinks[node] = min(lowlinks[node], index[successor])

            if lowlinks[node] == index[node]:
                component: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    component.append(w)
                    if w == node:
                        break
                if len(component) > 1:
                    cycles.append(sorted(component))

        for node in list(self.nodes.keys()) + list(self._out.keys()):
            if node not in index:
                strongconnect(node)
        return cycles

    # ---- persistence ------------------------------------------------------

    def to_json(self, *, language_mix: list[str] | None = None, generated_at: str = "") -> dict:
        return {
            "version": GRAPH_VERSION,
            "generated_at": generated_at,
            "language_mix": sorted(language_mix or {n.language for n in self.nodes.values()}),
            "nodes": {p: n.to_json() for p, n in sorted(self.nodes.items())},
            "edges": [e.to_json() for e in sorted(self.edges(), key=lambda e: (e.src, e.dst))],
            "stats": {
                "node_count": len(self.nodes),
                "edge_count": self.edge_count(),
            },
        }

    def save(self, path: Path, *, generated_at: str = "") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_json(generated_at=generated_at), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "ImportGraph | None":
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict) or data.get("version") != GRAPH_VERSION:
            return None

        graph = cls()
        for node_path, node_data in (data.get("nodes") or {}).items():
            graph.upsert_node(GraphNode.from_json(str(node_path), node_data))

        # Group edges by src for replace_edges_from to be O(n).
        by_src: dict[str, list[GraphEdge]] = {}
        for edge_data in data.get("edges") or []:
            try:
                edge = GraphEdge.from_json(edge_data)
            except (KeyError, TypeError):
                continue
            by_src.setdefault(edge.src, []).append(edge)
        for src, edges in by_src.items():
            graph.replace_edges_from(src, edges)
        return graph

    # ---- diagnostics ------------------------------------------------------

    def __repr__(self) -> str:
        return f"ImportGraph(nodes={len(self.nodes)}, edges={self.edge_count()})"
