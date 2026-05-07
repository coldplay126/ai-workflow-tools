"""Merge multiple repo scan results into unified analysis-config.json."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from awf.core.scanner import RepoScanResult, normalize_unit_name


def merge_scan_results(
    results: list[RepoScanResult],
    github_root: str,
) -> dict[str, Any]:
    """Merge multiple repo scans into a unified analysis-config.json structure."""
    service_map: dict[str, str] = {}
    domain_groups: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    domain_original_names: dict[str, str] = {}

    for result in results:
        # service_map: use placeholder for portability
        relative = result.root
        if relative.startswith(github_root):
            relative = "${AWF_GITHUB_ROOT}/" + result.root[len(github_root):].lstrip("/")
        service_map[result.service] = relative

        # group domains by normalized name
        for domain in result.units:
            normalized = domain.normalized
            domain_groups[normalized][result.service].append(domain.directory)
            # prefer kebab-case for the canonical name
            if normalized not in domain_original_names:
                domain_original_names[normalized] = _to_kebab(domain.name)

    # build domain_definitions
    domain_definitions: dict[str, dict[str, Any]] = {}
    for normalized, service_dirs in sorted(domain_groups.items()):
        canonical_name = domain_original_names.get(normalized, normalized)
        domain_definitions[canonical_name] = {
            "directories": dict(service_dirs),
            "related_domains": [],
            "existing_docs": [],
        }

    return {
        "analysis_perspectives": [
            "api-structure",
            "data-model",
            "business-logic",
            "cross-service-dependency",
            "security",
        ],
        "exclude_patterns": [
            "node_modules/", "dist/", "*.spec.ts", "*.test.ts",
            "*.generated.*", "package-lock.json", "*.d.ts", "*.js.map",
        ],
        "include_tests": False,
        "service_map": service_map,
        "domain_definitions": domain_definitions,
    }


def merge_with_existing(auto: dict, existing: dict) -> dict:
    """Merge auto-generated config with existing manual config. Manual config takes priority."""
    merged = dict(existing)

    # service_map: add new services, keep existing
    existing_services = merged.get("service_map", {})
    for service, root in auto.get("service_map", {}).items():
        if service not in existing_services:
            existing_services[service] = root
    merged["service_map"] = existing_services

    # domain_definitions: merge directories, preserve manual fields
    existing_domains = merged.get("domain_definitions", {})
    auto_domains = auto.get("domain_definitions", {})

    for domain_name, auto_defn in auto_domains.items():
        if domain_name in existing_domains:
            # merge directories only — add new services to existing domain
            existing_dirs = existing_domains[domain_name].get("directories", {})
            for service, dirs in auto_defn.get("directories", {}).items():
                if service not in existing_dirs:
                    existing_dirs[service] = dirs
            existing_domains[domain_name]["directories"] = existing_dirs
        else:
            existing_domains[domain_name] = auto_defn

    merged["domain_definitions"] = existing_domains

    # preserve other fields from existing
    for key in ["analysis_perspectives", "exclude_patterns", "include_tests"]:
        if key in existing and key not in merged:
            merged[key] = existing[key]

    return merged


def _to_kebab(name: str) -> str:
    """Convert camelCase/PascalCase to kebab-case."""
    s = _camel_to_parts(name)
    return "-".join(s)


def _camel_to_parts(name: str) -> list[str]:
    """Split camelCase/PascalCase/kebab/snake into word parts."""
    import re
    # Insert separator before uppercase letters following lowercase
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1-\2', name)
    # Split on any separator
    parts = re.split(r'[-_.\s]+', s)
    return [p.lower() for p in parts if p]
