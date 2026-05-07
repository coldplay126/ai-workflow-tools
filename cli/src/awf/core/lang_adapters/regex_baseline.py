"""Regex-based baseline export extractor.

Phase 0 implementation: catches the common cases for TypeScript, JavaScript,
and Python without an AST. Over-invalidation may occur on cosmetic changes;
per-language AST adapters can replace this baseline when measurement
justifies the cost.
"""
from __future__ import annotations

import re

from awf.core.lang_adapters import ExportSymbol


_TS_PATTERNS = [
    (re.compile(r"^\s*export\s+(?:type|interface)\s+([A-Za-z_$][\w$]*)", re.MULTILINE), "type"),
    (re.compile(r"^\s*export\s+default\s+(?:class|function|async\s+function)?\s*([A-Za-z_$][\w$]*)?", re.MULTILINE), "default"),
    (re.compile(r"^\s*export\s*\{([^}]+)\}", re.MULTILINE), "named-block"),
    (re.compile(r"^\s*export\s+(?:async\s+)?(?:const|let|var|function|class|enum)\s+([A-Za-z_$][\w$]*)", re.MULTILINE), "value"),
]

_PY_ALL_PATTERN = re.compile(r"^__all__\s*=\s*\[([^\]]*)\]", re.MULTILINE)
_PY_DEF_PATTERN = re.compile(r"^(?:async\s+)?def\s+([A-Za-z_][\w]*)", re.MULTILINE)
_PY_CLASS_PATTERN = re.compile(r"^class\s+([A-Za-z_][\w]*)", re.MULTILINE)


def _ts_named_block_symbols(block: str) -> list[ExportSymbol]:
    symbols: list[ExportSymbol] = []
    for raw in block.split(","):
        token = raw.strip()
        if not token:
            continue
        kind = "value"
        if token.startswith("type "):
            kind = "type"
            token = token[5:].strip()
        m = re.match(r"([A-Za-z_$][\w$]*)\s+as\s+([A-Za-z_$][\w$]*)", token)
        name = m.group(2) if m else token
        if not re.match(r"^[A-Za-z_$][\w$]*$", name):
            continue
        symbols.append(ExportSymbol(name=name, kind=kind))
    return symbols


class RegexBaselineExtractor:
    def extract(self, source: str, language: str) -> list[ExportSymbol]:
        if language in ("typescript", "javascript"):
            return _extract_ts(source)
        if language == "python":
            return _extract_py(source)
        return []


def _extract_ts(source: str) -> list[ExportSymbol]:
    found: dict[str, ExportSymbol] = {}
    for pattern, kind in _TS_PATTERNS:
        for match in pattern.finditer(source):
            if kind == "named-block":
                for sym in _ts_named_block_symbols(match.group(1)):
                    found.setdefault(sym.name, sym)
                continue
            name = match.group(1) if match.lastindex else None
            if not name:
                if kind == "default":
                    found.setdefault("default", ExportSymbol(name="default", kind="default"))
                continue
            found.setdefault(name, ExportSymbol(name=name, kind=kind))
    return sorted(found.values(), key=lambda s: (s.name, s.kind))


def _extract_py(source: str) -> list[ExportSymbol]:
    all_match = _PY_ALL_PATTERN.search(source)
    if all_match:
        names = {
            ExportSymbol(name=raw, kind="value")
            for raw in re.findall(r"['\"]([A-Za-z_][\w]*)['\"]", all_match.group(1))
        }
        return sorted(names, key=lambda s: s.name)

    found: dict[str, ExportSymbol] = {}
    for pattern in (_PY_DEF_PATTERN, _PY_CLASS_PATTERN):
        for match in pattern.finditer(source):
            name = match.group(1)
            if name.startswith("_"):
                continue
            found.setdefault(name, ExportSymbol(name=name, kind="value"))
    return sorted(found.values(), key=lambda s: s.name)
