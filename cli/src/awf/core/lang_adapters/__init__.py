"""Language adapters for export-symbol extraction.

Each adapter takes raw source text plus a language tag and returns a list
of exported symbols in a normalized, sorted order. Adapters are pluggable
so the 1st-pass regex baseline can be replaced with AST-based extractors
per language when over-invalidation data justifies the cost.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ExportSymbol:
    name: str
    kind: str  # "value" | "type" | "namespace" | "default"


class ExportExtractor(Protocol):
    def extract(self, source: str, language: str) -> list[ExportSymbol]: ...


_REGISTRY: dict[str, ExportExtractor] = {}


def register(name: str, extractor: ExportExtractor) -> None:
    _REGISTRY[name] = extractor


def get(name: str) -> ExportExtractor:
    if name not in _REGISTRY:
        raise KeyError(f"unknown export extractor: {name}")
    return _REGISTRY[name]


def has(name: str) -> bool:
    return name in _REGISTRY


def _bootstrap() -> None:
    if "regex_baseline" in _REGISTRY:
        return
    from awf.core.lang_adapters import regex_baseline
    register("regex_baseline", regex_baseline.RegexBaselineExtractor())


_bootstrap()
