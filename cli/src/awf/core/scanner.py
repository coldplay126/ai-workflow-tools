"""Project structure scanner for auto-generating analysis config."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


EXCLUDED_DIRS = {
    "common", "base", "shared", "utils", "utilities", "helpers",
    "config", "configuration", "settings",
    "middlewares", "middleware", "interceptors", "guards", "pipes", "filters",
    "mappers", "decorators", "validators",
    "types", "interfaces", "constants", "enums",
    "test", "tests", "__tests__", "e2e", "fixtures",
    "scripts", "tools", "bin",
    "migrations", "seeds", "seeders",
    "node_modules", "dist", "build", ".git", ".next", ".nuxt",
    "errorParser", "errors", "exceptions",
    "app", "main",
    # Laravel framework dirs
    "providers", "console", "events", "jobs", "mail",
    "notifications", "policies", "rules", "trait",
    "exports", "imports", "payments",
}

UNIT_DIR_PATTERNS = [
    "src/domains",
    "src/domain",
    "src/modules",
    "src/features",
    "src/apps",
    "src",
    # Laravel / PHP
    "app/Http/Controllers/Api/V2",
    "app/Http/Controllers/Api/V1",
    "app/Http/Controllers/Api",
    "app/Http/Controllers",
    "app/Services",
    "app/Models",
]

from awf.core.languages import LANGUAGE_TO_GLOBS as LANGUAGE_MAP


@dataclass
class UnitInfo:
    name: str
    directory: str
    file_count: int
    normalized: str


@dataclass
class RepoScanResult:
    service: str
    root: str
    language: str
    framework: str
    unit_pattern: str
    include_patterns: list[str]
    units: list[UnitInfo]
    excluded: list[str]


def normalize_unit_name(name: str) -> str:
    """Normalize unit name for cross-service comparison.

    artistUnit → artistunit
    artist-unit → artistunit
    artist_unit → artistunit
    ArtistUnit → artistunit
    """
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', name)
    s = re.sub(r'[-_.\s]+', '', s)
    return s.lower()


def detect_language(repo_root: Path) -> tuple[str, str]:
    """Return (language, framework)."""
    # PHP/Laravel: composer.json takes priority over package.json
    if (repo_root / "composer.json").exists():
        try:
            composer = json.loads((repo_root / "composer.json").read_text(encoding="utf-8"))
            deps = {**composer.get("require", {}), **composer.get("require-dev", {})}
        except Exception:
            deps = {}
        if "laravel/framework" in deps or (repo_root / "artisan").exists():
            return "php", "laravel"
        return "php", "php"

    if (repo_root / "package.json").exists():
        try:
            pkg = json.loads((repo_root / "package.json").read_text(encoding="utf-8"))
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        except Exception:
            deps = {}

        lang = "typescript" if (repo_root / "tsconfig.json").exists() else "javascript"

        if any(k.startswith("@nestjs/") for k in deps):
            return lang, "nestjs"
        if "express" in deps:
            return lang, "express"
        if "next" in deps:
            return lang, "nextjs"
        if "nuxt" in deps:
            return lang, "nuxtjs"
        return lang, "node"

    if (repo_root / "pyproject.toml").exists() or (repo_root / "setup.py").exists():
        try:
            text = (repo_root / "pyproject.toml").read_text(encoding="utf-8") if (repo_root / "pyproject.toml").exists() else ""
        except Exception:
            text = ""
        if "fastapi" in text.lower():
            return "python", "fastapi"
        if "django" in text.lower():
            return "python", "django"
        return "python", "python"

    if (repo_root / "go.mod").exists():
        return "go", "go"
    if (repo_root / "Cargo.toml").exists():
        return "rust", "rust"
    if (repo_root / "build.gradle").exists() or (repo_root / "build.gradle.kts").exists():
        if any((repo_root / "src").rglob("*.kt")):
            return "kotlin", "kotlin"
        return "java", "java"

    return "unknown", "unknown"


def _has_source_files(directory: Path, language: str) -> int:
    """Count source files in a directory."""
    patterns = LANGUAGE_MAP.get(language, ["**/*"])
    count = 0
    for pattern in patterns:
        try:
            count += len(list(directory.glob(pattern)))
        except Exception:
            pass
    return count


def find_unit_directories(repo_root: Path, language: str) -> tuple[str, list[UnitInfo], list[str]]:
    """Find unit pattern and list units."""
    for pattern in UNIT_DIR_PATTERNS:
        candidate = repo_root / pattern
        if not candidate.is_dir():
            continue

        subdirs = sorted([d for d in candidate.iterdir() if d.is_dir()])
        if not subdirs:
            continue

        units: list[UnitInfo] = []
        excluded: list[str] = []
        is_flat = pattern == "src"

        for d in subdirs:
            name = d.name
            if name.startswith(".") or name.startswith("__"):
                excluded.append(name)
                continue
            if name.lower() in EXCLUDED_DIRS:
                excluded.append(name)
                continue

            file_count = _has_source_files(d, language)
            if file_count == 0:
                excluded.append(name)
                continue

            rel_dir = f"{pattern}/{name}"
            units.append(UnitInfo(
                name=name,
                directory=rel_dir,
                file_count=file_count,
                normalized=normalize_unit_name(name),
            ))

        if units:
            unit_pattern = f"{pattern}/{{unit}}"
            return unit_pattern, units, excluded

    return "", [], []


_SKIP_DIRS = {"node_modules", "vendor", "dist", "build", ".git", "__pycache__", ".next", ".nuxt", "storage", ".idea", ".vscode", "public", "resources", "docs", "tests", "test", "specs"}
_DEEP_DIRS = {"app", "src", "routes", "packages", "modules", "lib", "cmd", "internal", "terraform", "kubernetes", "k8s", "helm", "deploy"}


def _build_directory_tree(repo_root: Path, max_depth: int = 3, deep_max_depth: int = 6) -> str:
    """Build a compact directory tree string for AI analysis.

    Core code directories (app/, src/, routes/) are explored deeper.
    Non-code directories are kept shallow.
    """
    lines: list[str] = []

    def _walk(path: Path, prefix: str, depth: int, is_deep: bool) -> None:
        limit = deep_max_depth if is_deep else max_depth
        if depth > limit:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        except PermissionError:
            return
        dirs = [e for e in entries if e.is_dir() and not e.name.startswith(".") and e.name not in _SKIP_DIRS]
        files = [e for e in entries if e.is_file() and not e.name.startswith(".")]

        if files:
            # Show file count, not individual files (saves space for deeper dirs)
            exts = {}
            for f in files:
                ext = f.suffix.lower() or "no-ext"
                exts[ext] = exts.get(ext, 0) + 1
            ext_summary = ", ".join(f"{ext}×{n}" for ext, n in sorted(exts.items(), key=lambda x: -x[1])[:3])
            lines.append(f"{prefix}[{len(files)} files: {ext_summary}]")

        for d in dirs[:20]:
            lines.append(f"{prefix}{d.name}/")
            child_deep = is_deep or d.name.lower() in _DEEP_DIRS
            _walk(d, prefix + "  ", depth + 1, child_deep)
        if len(dirs) > 20:
            lines.append(f"{prefix}... (+{len(dirs) - 20} dirs)")

    lines.append(f"{repo_root.name}/")
    _walk(repo_root, "  ", 1, False)
    return "\n".join(lines[:500])


def _ai_discover_units(repo_root: Path, tree: str, language: str, framework: str) -> tuple[str, list[UnitInfo], list[str]]:
    """Use AI to discover analysis units from directory tree."""
    import sys
    prompt = (
        f"Project: `{repo_root.name}` ({language}/{framework})\n\n"
        f"```\n{tree}\n```\n\n"
        "List all independent units of analysis. "
        "A unit is a group of files that implements one business function or infrastructure component. "
        "Examples: API controller group (include ALL versions V1/V2), Terraform module, K8s component, service package. "
        "Include business logic inside Common/ or shared/ directories. "
        "Exclude only pure utilities (helpers, base classes, middleware).\n\n"
        "Return ONLY valid JSON:\n"
        '{"unit_pattern":"pattern","units":[{"name":"x","directory":"relative/path","description":"one line"}],"excluded":["dir"]}'
    )

    # Try codex first (cheap), then sonnet
    try:
        from awf.providers.registry import ProviderRegistry
        from awf.core.config import load_awf_config
        config = load_awf_config(None)
        registry = ProviderRegistry(config)

        provider = None
        for name in ["codex", "claude:sonnet", "claude-code"]:
            if registry.supports(name):
                try:
                    provider = registry.get(name)
                    break
                except Exception:
                    continue

        if not provider:
            print("warning: no AI provider available for unit discovery", file=sys.stderr)
            return "", [], []

        print(f"  ai_discovery: {repo_root.name} via {getattr(provider, 'name', '?')}", file=sys.stderr)
        result = provider.complete(prompt, cwd=str(repo_root))
        output = (result.stdout or "").strip()

        # Parse JSON
        from awf.core.agent_runner import _try_parse_json
        parsed = _try_parse_json(output)
        units_key = "units" if "units" in parsed else "domains"  # backward compat
        if not parsed or units_key not in parsed:
            print(f"  ai_discovery: parse failed", file=sys.stderr)
            return "", [], []

        unit_pattern = str(parsed.get("unit_pattern", parsed.get("domain_pattern", "")))
        excluded = list(parsed.get("excluded", []))
        units: list[UnitInfo] = []

        for d in parsed[units_key]:
            name = str(d.get("name", ""))
            directory = str(d.get("directory", ""))
            if not name or not directory:
                continue
            # Verify directory exists
            full_path = repo_root / directory
            if full_path.is_dir():
                file_count = _has_source_files(full_path, language)
                units.append(UnitInfo(
                    name=name,
                    directory=directory,
                    file_count=file_count,
                    normalized=normalize_unit_name(name),
                ))

        # Auto-detect actual language from discovered domains if file_count is 0
        if units and all(d.file_count == 0 for d in units):
            actual_exts: dict[str, int] = {}
            for d in units:
                full = repo_root / d.directory
                if full.is_dir():
                    for f in full.rglob("*"):
                        if f.is_file() and f.suffix:
                            actual_exts[f.suffix.lower()] = actual_exts.get(f.suffix.lower(), 0) + 1
            if actual_exts:
                top_ext = max(actual_exts, key=actual_exts.get)
                from awf.core.languages import EXT_TO_LANGUAGE
                ext_to_lang = EXT_TO_LANGUAGE
                if top_ext in ext_to_lang:
                    language = ext_to_lang[top_ext]
                    # Re-count with correct language
                    for d in units:
                        d.file_count = _has_source_files(repo_root / d.directory, language)

        print(f"  ai_discovery: {len(units)} units found", file=sys.stderr)
        return unit_pattern, units, excluded

    except Exception as exc:
        print(f"  ai_discovery error: {exc}", file=sys.stderr)
        return "", [], []


def scan_repo(repo_root: Path, use_ai: bool = True) -> RepoScanResult:
    """Scan a single repo and return service config fragment.

    Tries heuristic first. If no units found and use_ai=True, falls back
    to AI-based directory analysis.
    """
    repo_root = repo_root.resolve()
    service = repo_root.name
    language, framework = detect_language(repo_root)
    include_patterns = LANGUAGE_MAP.get(language, ["**/*"])
    unit_pattern, units, excluded = find_unit_directories(repo_root, language)

    # AI fallback if heuristic found nothing
    if not units and use_ai:
        tree = _build_directory_tree(repo_root)
        unit_pattern, units, excluded = _ai_discover_units(repo_root, tree, language, framework)
        # AI may have detected actual language differs from heuristic
        if units:
            actual_exts: dict[str, int] = {}
            for d in units:
                full = repo_root / d.directory
                if full.is_dir():
                    for f in full.rglob("*"):
                        if f.is_file() and f.suffix:
                            actual_exts[f.suffix.lower()] = actual_exts.get(f.suffix.lower(), 0) + 1
            if actual_exts:
                top_ext = max(actual_exts, key=actual_exts.get)
                detected_lang = {".php": "php", ".py": "python", ".go": "go", ".tf": "terraform", ".yaml": "yaml", ".yml": "yaml"}.get(top_ext)
                if detected_lang and detected_lang != language:
                    language = detected_lang
                    framework = detected_lang
                    include_patterns = LANGUAGE_MAP.get(language, ["**/*"])

    return RepoScanResult(
        service=service,
        root=str(repo_root),
        language=language,
        framework=framework,
        unit_pattern=unit_pattern,
        include_patterns=include_patterns,
        units=units,
        excluded=excluded,
    )


def scan_result_to_dict(result: RepoScanResult) -> dict[str, Any]:
    """Convert scan result to JSON-serializable dict."""
    return {
        "service": result.service,
        "root": result.root,
        "language": result.language,
        "framework": result.framework,
        "unit_pattern": result.unit_pattern,
        "include_patterns": result.include_patterns,
        "units": [
            {
                "name": d.name,
                "directory": d.directory,
                "file_count": d.file_count,
                "normalized": d.normalized,
            }
            for d in result.units
        ],
        "excluded": result.excluded,
    }
