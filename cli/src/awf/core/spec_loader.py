"""Load prompt templates, JSON resources, and skill specs from skills/ directory.

Provides a generic spec-as-truth resource system:
- load_manifest()          — skill manifest with resource categories
- load_skill_resource()    — generic loader for any resource type (json/md)
- list_skill_resources()   — list available resources in a category
- load_prompt()            — prompt template with variable substitution
- load_json_resource()     — JSON resource (delegates to load_skill_resource)
- load_analysis_mode_contract() — analysis-specific convenience wrapper
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from awf.core.skills import find_skill_dir, iter_skill_dirs

_CACHE: dict[str, str] = {}
_JSON_CACHE: dict[str, dict] = {}
_MANIFEST_CACHE: dict[str, dict] = {}

# Supported resource file types
_TYPE_EXTENSIONS: dict[str, str] = {
    "json": ".json",
    "md": ".md",
    "yaml": ".yaml",
    "yml": ".yml",
    "txt": ".txt",
}


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def load_manifest(skill: str) -> dict[str, Any]:
    """Load the highest-priority skill manifest (or discover its categories)."""
    skill_dir = find_skill_dir(skill)
    if skill_dir is None:
        raise FileNotFoundError(f"Skill not found: {skill}")
    return _manifest_for_dir(skill, skill_dir)


def _manifest_for_dir(skill: str, skill_dir: Path) -> dict[str, Any]:
    cache_key = str(skill_dir)
    if cache_key in _MANIFEST_CACHE:
        return _MANIFEST_CACHE[cache_key]
    manifest_path = skill_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid manifest JSON in {manifest_path}: {exc}") from exc
    else:
        manifest = _auto_discover_manifest(skill, skill_dir)
    _MANIFEST_CACHE[cache_key] = manifest
    return manifest


def _auto_discover_manifest(skill: str, skill_dir: Path) -> dict[str, Any]:
    """Build a manifest by scanning skill directory structure."""
    categories: dict[str, dict] = {}
    for child in sorted(skill_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        # Detect dominant file type in directory
        file_type = _detect_category_type(child)
        if file_type:
            categories[child.name] = {"type": file_type, "path": child.name}
    return {
        "skill": skill,
        "version": "0.0.0",
        "categories": categories,
    }


def _detect_category_type(directory: Path) -> str | None:
    """Detect the dominant file type in a category directory."""
    counts: dict[str, int] = {}
    for f in directory.iterdir():
        if f.is_file():
            suffix = f.suffix.lower()
            for type_name, ext in _TYPE_EXTENSIONS.items():
                if suffix == ext:
                    counts[type_name] = counts.get(type_name, 0) + 1
                    break
    if not counts:
        return None
    return max(counts, key=counts.get)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Generic resource loading
# ---------------------------------------------------------------------------

def load_skill_resource(skill: str, category: str, name: str) -> dict[str, Any] | str:
    """Load the highest-priority existing resource for a skill and category."""
    skill_dirs = iter_skill_dirs(skill)
    if not skill_dirs:
        raise FileNotFoundError(f"Skill not found: {skill}")

    available: set[str] = set()
    for skill_dir in skill_dirs:
        manifest = _manifest_for_dir(skill, skill_dir)
        categories = manifest.get("categories", {})
        available.update(categories)
        cat_config = categories.get(category)
        if not cat_config:
            continue
        cat_path = cat_config.get("path", category)
        cat_type = cat_config.get("type", "json")
        ext = _TYPE_EXTENSIONS.get(cat_type, f".{cat_type}")
        resource_path = skill_dir / cat_path / f"{name}{ext}"
        if not resource_path.is_file():
            continue

        cache_key = str(resource_path)
        if cat_type == "json":
            if cache_key not in _JSON_CACHE:
                try:
                    _JSON_CACHE[cache_key] = json.loads(
                        resource_path.read_text(encoding="utf-8")
                    )
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid JSON in resource {resource_path}: {exc}"
                    ) from exc
            return _JSON_CACHE[cache_key]
        if cache_key not in _CACHE:
            _CACHE[cache_key] = resource_path.read_text(encoding="utf-8")
        return _CACHE[cache_key]

    if category not in available:
        raise FileNotFoundError(
            f"Category '{category}' not found in skill '{skill}'. "
            f"Available: {sorted(available)}"
        )
    raise FileNotFoundError(f"Resource not found: {skill}/{category}/{name}")


def list_skill_resources(skill: str, category: str) -> list[str]:
    """List resources available across all skill directories, without duplicates."""
    names: set[str] = set()
    for skill_dir in iter_skill_dirs(skill):
        manifest = _manifest_for_dir(skill, skill_dir)
        cat_config = manifest.get("categories", {}).get(category)
        if not cat_config:
            continue
        cat_path = cat_config.get("path", category)
        cat_type = cat_config.get("type", "json")
        ext = _TYPE_EXTENSIONS.get(cat_type, f".{cat_type}")
        resource_dir = skill_dir / cat_path
        if resource_dir.is_dir():
            names.update(
                f.stem for f in resource_dir.iterdir()
                if f.is_file() and f.suffix.lower() == ext
            )
    return sorted(names)


# ---------------------------------------------------------------------------
# Prompt loading (unchanged API, enhanced internals)
# ---------------------------------------------------------------------------

def load_prompt(skill: str, prompt_name: str, **kwargs) -> str:
    """Load the first available prompt template and substitute its variables."""
    skill_dirs = iter_skill_dirs(skill)
    if not skill_dirs:
        raise FileNotFoundError(f"Skill not found: {skill}")
    prompt_path = None
    for skill_dir in skill_dirs:
        candidate = skill_dir / "prompts" / f"{prompt_name}.md"
        if candidate.is_file():
            prompt_path = candidate
            break
    if prompt_path is None:
        raise FileNotFoundError(f"Prompt not found: {skill}/prompts/{prompt_name}.md")
    cache_key = str(prompt_path)
    if cache_key not in _CACHE:
        _CACHE[cache_key] = prompt_path.read_text(encoding="utf-8")
    template = _CACHE[cache_key]
    if kwargs:
        result = template
        for key, value in kwargs.items():
            result = result.replace("{" + key + "}", str(value))
        return result
    return template


def load_prompt_optional(skill: str, prompt_name: str, **kwargs) -> Optional[str]:
    """Like load_prompt but returns None if file not found."""
    try:
        return load_prompt(skill, prompt_name, **kwargs)
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# JSON resource loading (backward compatible)
# ---------------------------------------------------------------------------

def load_json_resource(skill: str, category: str, name: str) -> dict[str, Any]:
    """Load a JSON resource from skills/{skill}/{category}/{name}.json.

    Used for mode contracts, configuration specs, and other structured data.
    Results are cached for the process lifetime.
    """
    result = load_skill_resource(skill, category, name)
    if not isinstance(result, dict):
        raise ValueError(f"Expected JSON dict from {skill}/{category}/{name}, got {type(result).__name__}")
    return result


def load_analysis_mode_contract(mode_name: str) -> dict[str, Any]:
    """Load an analysis mode contract from skills/analysis/modes/{mode_name}.json.

    The contract defines required_output_files, writers, judge config for the mode.
    Raises FileNotFoundError if the mode contract doesn't exist.
    """
    contract = load_json_resource("analysis", "modes", mode_name)
    required_keys = {"mode", "required_output_files", "writers", "judge"}
    missing = required_keys - set(contract.keys())
    if missing:
        raise ValueError(f"Mode contract '{mode_name}' missing required keys: {missing}")
    judge = contract.get("judge", {})
    if not isinstance(judge, dict) or "prompt" not in judge:
        raise ValueError(f"Mode contract '{mode_name}' missing judge.prompt")
    return contract


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

_AGENT_CACHE: dict[tuple[tuple[Path, ...], str], dict[str, Any]] = {}


def _agent_search_paths() -> list[Path]:
    """Search paths for agent definition files (agents/*.md).

    Order: repo-local first (authoritative source), then user-level (override).
    """
    paths: list[Path] = []
    # 1. Project-level agents (repo checkout = single source of truth)
    try:
        from awf.core.paths import find_repo_root
        root = find_repo_root(None)
        paths.append(root / "claude" / "agents")
        paths.append(root / ".claude" / "agents")
    except (FileNotFoundError, Exception):
        paths.append(Path.cwd() / "claude" / "agents")
    # 2. User-level agents (symlinks from setup.sh, or user overrides)
    paths.append(Path.home() / ".claude" / "agents")
    return paths


def _parse_agent_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML-like frontmatter from agent markdown file.

    Returns (metadata_dict, body_text).
    Handles simple key: value pairs and key: [list] syntax.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text

    meta: dict[str, Any] = {}
    body_start = 1
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            body_start = i + 1
            break
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Handle lists: [a, b, c]
            if value.startswith("[") and value.endswith("]"):
                items = [v.strip().strip('"').strip("'") for v in value[1:-1].split(",")]
                meta[key] = [v for v in items if v]
            elif value.lower() in ("true", "false"):
                meta[key] = value.lower() == "true"
            else:
                meta[key] = value
    body = "\n".join(lines[body_start:]).strip()
    return meta, body


def load_agent_definition(
    agent_name: str, *, repo_root: str | Path | None = None
) -> dict[str, Any]:
    """Load a complete agent definition from agents/{agent_name}.md.

    Returns dict with 'meta' (frontmatter) and 'instructions' (body).
    Searches repo-local sources before the existing installation search paths.
    Cache entries are isolated by search roots, including implicit cwd changes.
    """
    search_paths = _agent_search_paths()
    if repo_root is not None:
        root = Path(repo_root).resolve()
        search_paths = [root / "claude" / "agents", root / ".claude" / "agents", *search_paths]
    cache_key = (tuple(path.resolve() for path in search_paths), agent_name)
    if cache_key in _AGENT_CACHE:
        return _AGENT_CACHE[cache_key]

    for search_dir in search_paths:
        agent_path = search_dir / f"{agent_name}.md"
        if agent_path.is_file():
            text = agent_path.read_text(encoding="utf-8")
            meta, body = _parse_agent_frontmatter(text)
            result = {"meta": meta, "instructions": body, "path": str(agent_path)}
            _AGENT_CACHE[cache_key] = result
            return result

    raise FileNotFoundError(f"Agent not found: {agent_name}")


def load_agent_instructions(agent_name: str) -> str:
    """Load only the body (system prompt) from an agent definition file.

    This is the primary entry point for injecting agent instructions
    into Codex base-instructions or team worker prompts.
    """
    defn = load_agent_definition(agent_name)
    return defn["instructions"]


def resolve_agent_for_role(role: str) -> Optional[str]:
    """Find an agent definition whose 'roles' list includes the given role.

    Returns agent name if found, None otherwise.
    Used to map legacy protocol roles to new agent definitions.
    """
    for search_dir in _agent_search_paths():
        if not search_dir.is_dir():
            continue
        for agent_path in search_dir.glob("*.md"):
            try:
                text = agent_path.read_text(encoding="utf-8")
                meta, _ = _parse_agent_frontmatter(text)
                roles = meta.get("roles", [])
                if isinstance(roles, list) and role in roles:
                    return agent_path.stem
            except (OSError, UnicodeDecodeError):
                continue
    return None


def clear_cache() -> None:
    """Clear all caches (template, resource, manifest, agent)."""
    _CACHE.clear()
    _JSON_CACHE.clear()
    _MANIFEST_CACHE.clear()
    _AGENT_CACHE.clear()
