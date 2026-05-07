from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from awf.core.paths import find_repo_root


@dataclass
class AnalysisContext:
    repo_root: Path
    docs_root: Path
    github_root: Path
    analysis_config_path: Path
    analysis_pipeline_path: Path
    service: str
    domain: str
    mode: str
    ai_context_dir: Path
    domain_directories: list[str]
    all_directories: dict[str, list[str]]
    related_domains: list[str]
    existing_docs: list[str]
    include_patterns: list[str] | None = None
    analysis_mode: str = "document"


@dataclass
class AwfConfig:
    raw: dict[str, Any]

    @classmethod
    def defaults(cls) -> "AwfConfig":
        return cls(
            raw={
                "provider": {
                    "default": "claude-code",
                    "fallback": [],
                    "aliases": {},
                    "claude-code": {
                        "command": os.environ.get("AWF_CLAUDE_COMMAND", "claude"),
                        "flags": ["--print", "--permission-mode", "default"],
                    },
                    "claude-sdk": {
                        "api_key_env": "ANTHROPIC_API_KEY",
                        "model": "claude-sonnet-4-6",
                        "max_tokens": 8192,
                    },
                    "openai": {
                        "api_key_env": "OPENAI_API_KEY",
                        "model": "gpt-5-mini",
                        "max_output_tokens": 8192,
                    },
                    "codex": {
                        "command": os.environ.get("AWF_CODEX_COMMAND", "codex"),
                        "flags": ["exec", "--sandbox", "workspace-write"],
                    },
                    "fixture": {
                        "result_file": "",
                    },
                },
                "paths": {},
                "analysis": {
                    "default_mode": "standard",
                    "fanout_large_file_threshold": 80,
                    "stage2_token_threshold": 12000,
                    "stage2_line_threshold": 400,
                },
                "mcp_defaults": {},
                "permissions": {
                    "allowed_tools": [
                        "provider:claude-code",
                        "provider:claude-sdk",
                        "provider:openai",
                        "provider:codex",
                        "provider:fixture",
                        "tool:file.read",
                        "tool:file.glob",
                        "tool:file.grep",
                        "tool:git.diff",
                        "tool:git.log",
                        "tool:mcp.invoke",
                        "tool:mcp.read",
                    ],
                    "disabled_tools": [],
                    "yolo": False,
                },
            }
        )

    def merge(self, other: dict[str, Any]) -> "AwfConfig":
        return AwfConfig(raw=_deep_merge(self.raw, other))

    def provider_name(self) -> str:
        return str(self.raw.get("provider", {}).get("default", "claude-code"))

    def provider_settings(self, name: str) -> dict[str, Any]:
        return dict(self.raw.get("provider", {}).get(name, {}))

    def path_override(self, key: str) -> str | None:
        value = self.raw.get("paths", {}).get(key)
        return str(value) if value is not None else None


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_awf_config(explicit_root: Optional[str] = None) -> AwfConfig:
    root = find_repo_root(explicit_root)
    user_config = _load_toml(Path.home() / ".config" / "awf" / "config.toml")
    project_config = _load_toml(root / ".awf.toml")
    return AwfConfig.defaults().merge(user_config).merge(project_config)


def resolve_repo_root(explicit_root: Optional[str] = None) -> Path:
    return find_repo_root(explicit_root)


def _resolve_docs_root(repo_root: Path, explicit_docs_root: Optional[str]) -> Path:
    if explicit_docs_root:
        return Path(explicit_docs_root).expanduser().resolve()

    env_root = os.environ.get("AWF_DOCS_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    sibling = repo_root.parent / "analysis-docs"
    if sibling.exists():
        return sibling.resolve()

    fallback = Path.home() / "Documents" / "GitHub" / "analysis-docs"
    return fallback.resolve()


def _resolve_github_root(repo_root: Path, explicit_github_root: Optional[str]) -> Path:
    if explicit_github_root:
        return Path(explicit_github_root).expanduser().resolve()

    env_root = os.environ.get("AWF_GITHUB_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    return repo_root.parent.resolve()


def _expand_placeholders(value: str, github_root: Path) -> str:
    return value.replace("${AWF_GITHUB_ROOT}", str(github_root))


def resolve_runtime_paths(explicit_root: Optional[str] = None) -> dict[str, str]:
    root = find_repo_root(explicit_root)
    awf_config = load_awf_config(str(root))
    docs_override = awf_config.path_override("analysis_docs")
    github_override = awf_config.path_override("awf_github")
    docs = _resolve_docs_root(root, docs_override)
    github = _resolve_github_root(root, github_override)
    return {
        "repo_root": str(root),
        "analysis_docs": str(docs),
        "awf_github": str(github),
        "project_config": str(root / ".awf.toml"),
        "user_config": str(Path.home() / ".config" / "awf" / "config.toml"),
    }


def _discover_domain_directories(repo_root: Path, domain: str) -> list[str]:
    """Discover domain directories by searching the actual filesystem.

    Instead of hardcoding patterns per language/framework, walks the repo
    to find directories matching the domain name.
    """
    from awf.core.scanner import normalize_unit_name
    target = normalize_unit_name(domain)
    matches: list[str] = []

    # Walk up to 4 levels deep looking for a directory matching the domain name
    for depth_limit in range(1, 5):
        for path in _walk_dirs(repo_root, depth_limit):
            if normalize_unit_name(path.name) == target:
                try:
                    rel = str(path.relative_to(repo_root))
                    if rel not in matches:
                        matches.append(rel)
                except ValueError:
                    pass
        if matches:
            break

    return matches


def _walk_dirs(root: Path, max_depth: int, _depth: int = 0) -> list[Path]:
    """Walk directories up to max_depth, skipping common non-source dirs."""
    skip = {"node_modules", "vendor", "dist", "build", ".git", "__pycache__", ".next", "storage", "coverage", ".idea"}
    result: list[Path] = []
    if _depth >= max_depth:
        return result
    try:
        for entry in root.iterdir():
            if entry.is_dir() and entry.name not in skip and not entry.name.startswith("."):
                result.append(entry)
                result.extend(_walk_dirs(entry, max_depth, _depth + 1))
    except PermissionError:
        pass
    return result


def resolve_analysis_context(
    service: str,
    domain: str,
    deep: bool,
    repo_root: Optional[str] = None,
    docs_root: Optional[str] = None,
    github_root: Optional[str] = None,
) -> AnalysisContext:
    root = find_repo_root(repo_root)
    awf_config = load_awf_config(str(root))
    docs_override = docs_root or awf_config.path_override("analysis_docs")
    github_override = github_root or awf_config.path_override("awf_github")
    docs = _resolve_docs_root(root, docs_override)
    gh_root = _resolve_github_root(root, github_override)
    analysis_config_path = docs / "_templates" / "analysis-config.json"
    analysis_pipeline_path = docs / "_templates" / "analysis-pipeline.json"

    if not analysis_config_path.exists():
        import sys
        print(f"warning: analysis config not found at {analysis_config_path}, using auto-discovery", file=sys.stderr)
    if not analysis_pipeline_path.exists():
        import sys
        print(f"warning: analysis pipeline not found at {analysis_pipeline_path}, using defaults", file=sys.stderr)

    analysis_config = json.loads(analysis_config_path.read_text(encoding="utf-8")) if analysis_config_path.exists() else {}
    service_map = analysis_config.get("service_map", {})

    # Auto-discover service if not in config
    if service not in service_map:
        candidate = gh_root / service
        if candidate.is_dir():
            import sys
            from awf.core.scanner import scan_repo
            scan_result = scan_repo(candidate)
            service_map[service] = str(candidate)
            print(f"auto_discovered: {service} ({scan_result.language}/{scan_result.framework}, {len(scan_result.units)} domains)", file=sys.stderr)
            # Inject scanned domains into domain_definitions
            auto_definitions = analysis_config.get("domain_definitions", {})
            from awf.core.scanner import normalize_unit_name
            for d in scan_result.units:
                # Find existing definition by normalized name
                matched = False
                for existing_name, existing_defn in auto_definitions.items():
                    if normalize_unit_name(existing_name) == d.normalized:
                        dirs = existing_defn.setdefault("directories", {})
                        if service not in dirs:
                            dirs[service] = [d.directory]
                        matched = True
                        break
                if not matched:
                    auto_definitions[d.name] = {
                        "directories": {service: [d.directory]},
                        "related_domains": [],
                        "existing_docs": [],
                    }
            analysis_config["domain_definitions"] = auto_definitions
            # Use scanned include_patterns
            if scan_result.include_patterns:
                analysis_config.setdefault("_auto_include_patterns", {})[service] = scan_result.include_patterns
        else:
            supported = ", ".join(sorted(service_map))
            raise KeyError(f"Unknown service `{service}` and no repo found at {candidate}. Configured: {supported}")

    domain_definitions = analysis_config.get("domain_definitions", {})
    domain_definition = domain_definitions.get(domain)

    # Try normalized match if exact match fails
    if not domain_definition:
        from awf.core.scanner import normalize_unit_name
        target_normalized = normalize_unit_name(domain)
        for def_name, def_value in domain_definitions.items():
            if normalize_unit_name(def_name) == target_normalized:
                domain_definition = def_value
                break

    if domain_definition:
        domain_directories = domain_definition.get("directories", {}).get(service, [])
        all_directories = dict(domain_definition.get("directories", {}))
        related_domains = domain_definition.get("related_domains", [])
        existing_docs = domain_definition.get("existing_docs", [])
    else:
        # No config definition found and auto-scan didn't find this domain.
        # Last resort: try common patterns based on actual repo structure.
        domain_directories = _discover_domain_directories(gh_root / service, domain) if (gh_root / service).is_dir() else []
        if not domain_directories:
            import sys
            print(
                f"warning: domain '{domain}' not found in {service}. "
                f"Run 'awf scan {service}' to see available domains, or define it in analysis-config.json.",
                file=sys.stderr,
            )
        all_directories = {service: list(domain_directories)}
        related_domains = []
        existing_docs = []

    # Resolve include_patterns: domain > service > auto-scanned > global > None (use default)
    service_config = service_map.get(service)
    include_patterns = None
    if isinstance(service_config, dict):
        include_patterns = service_config.get("include_patterns")
    if include_patterns is None and domain_definition:
        include_patterns = domain_definition.get("include_patterns")
    if include_patterns is None:
        auto_patterns = analysis_config.get("_auto_include_patterns", {})
        if service in auto_patterns:
            include_patterns = auto_patterns[service]
    global_include = analysis_config.get("include_patterns")
    if include_patterns is None and global_include is not None:
        include_patterns = global_include

    service_root_value = service_map[service]
    service_root = Path(_expand_placeholders(service_root_value, gh_root)) if "${" in str(service_root_value) else Path(service_root_value)
    resolved_directories = [str((service_root / directory).resolve()) for directory in domain_directories]
    resolved_all_directories: dict[str, list[str]] = {}
    for service_name, directories in all_directories.items():
        service_root_value = service_map.get(service_name)
        if not service_root_value:
            continue
        service_root_path = Path(_expand_placeholders(service_root_value, gh_root)) if "${" in str(service_root_value) else Path(service_root_value)
        resolved_all_directories[service_name] = [str((service_root_path / directory).resolve()) for directory in directories]
    ai_context_dir = docs / service / domain / ".ai-context"

    return AnalysisContext(
        repo_root=root,
        docs_root=docs,
        github_root=gh_root,
        analysis_config_path=analysis_config_path,
        analysis_pipeline_path=analysis_pipeline_path,
        service=service,
        domain=domain,
        mode="deep" if deep else "standard",
        ai_context_dir=ai_context_dir,
        domain_directories=resolved_directories,
        all_directories=resolved_all_directories,
        related_domains=related_domains,
        existing_docs=existing_docs,
        include_patterns=include_patterns,
    )
