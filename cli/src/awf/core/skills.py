from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Optional

from awf.core.paths import find_repo_root


@dataclass
class SkillInfo:
    name: str
    description: str
    path: Path
    source_dir: Path
    manifest: Optional[SkillManifest] = None


def skill_search_paths(explicit_root: Optional[str] = None) -> list[Path]:
    project_root = find_repo_root(explicit_root)
    paths: list[Path] = []
    env_dir = os.environ.get("AWF_SKILLS_DIR")
    if env_dir:
        paths.append(Path(env_dir).expanduser().resolve())
    paths.extend(
        [
            (Path.home() / ".config" / "awf" / "skills").resolve(),
            (project_root / ".awf" / "skills").resolve(),
            (Path.home() / ".claude" / "skills").resolve(),
            (project_root / "claude" / "skills").resolve(),
            (project_root / ".claude" / "skills").resolve(),
        ]
    )
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def _fallback_roots() -> list[Path]:
    """Minimal search paths when repo root is unavailable."""
    paths: list[Path] = []
    env_dir = os.environ.get("AWF_SKILLS_DIR")
    if env_dir:
        paths.append(Path(env_dir).expanduser().resolve())
    paths.extend([
        Path.home() / ".config" / "awf" / "skills",
        Path.home() / ".claude" / "skills",
        Path.cwd() / "claude" / "skills",
    ])
    return paths


def find_skill_dir(skill_name: str, explicit_root: Optional[str] = None) -> Optional[Path]:
    """Find a skill directory by name across all search paths.

    When explicit_root is provided but invalid, re-raises FileNotFoundError.
    Fallback roots are only used when explicit_root is None and auto-detection fails.
    """
    try:
        roots = skill_search_paths(explicit_root)
    except FileNotFoundError:
        if explicit_root is not None:
            raise
        roots = _fallback_roots()
    for base in roots:
        candidate = base / skill_name
        if candidate.is_dir():
            return candidate
    return None


@dataclass
class SkillResource:
    """A discovered resource within a skill category."""
    name: str
    category: str
    skill: str
    path: Path
    type: str  # json, md, yaml, etc.


@dataclass
class SkillManifest:
    """Parsed skill manifest with resource metadata."""
    skill: str
    version: str
    categories: dict[str, dict]  # {name: {"type": str, "path": str}}
    skill_dir: Path

    def list_resources(self, category: str) -> list[str]:
        """List resource names in a category (without extensions)."""
        cat = self.categories.get(category)
        if not cat:
            return []
        cat_type = cat.get("type", "json")
        ext_map = {
            "json": ".json", "md": ".md", "yaml": ".yaml",
            "yml": ".yml", "txt": ".txt",
        }
        ext = ext_map.get(cat_type, f".{cat_type}")
        cat_dir = self.skill_dir / cat.get("path", category)
        if not cat_dir.is_dir():
            return []
        return sorted(f.stem for f in cat_dir.iterdir() if f.is_file() and f.suffix.lower() == ext)

    def has_category(self, category: str) -> bool:
        return category in self.categories


def load_skill_manifest(skill_name: str, explicit_root: Optional[str] = None) -> Optional[SkillManifest]:
    """Load manifest for a single skill. Returns None if skill not found."""
    skill_dir = find_skill_dir(skill_name, explicit_root)
    if not skill_dir:
        return None
    manifest_path = skill_dir / "manifest.json"
    if manifest_path.is_file():
        import json
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}
    return SkillManifest(
        skill=data.get("skill", skill_name),
        version=data.get("version", "0.0.0"),
        categories=data.get("categories", {}),
        skill_dir=skill_dir,
    )


def discover_skills(explicit_root: Optional[str] = None) -> list[SkillInfo]:
    discovered: list[SkillInfo] = []
    seen_names: set[str] = set()
    for root in skill_search_paths(explicit_root):
        if not root.exists() or not root.is_dir():
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            parsed = _parse_skill_file(skill_md)
            if parsed is None:
                continue
            if parsed.name in seen_names:
                continue
            seen_names.add(parsed.name)
            discovered.append(parsed)
    return discovered


def _parse_skill_file(path: Path) -> SkillInfo | None:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    frontmatter = _parse_frontmatter(text)
    name = str(frontmatter.get("name") or path.parent.name).strip()
    description = str(frontmatter.get("description") or "").strip()
    manifest = load_skill_manifest(name)
    return SkillInfo(
        name=name,
        description=description,
        path=path,
        source_dir=path.parent,
        manifest=manifest,
    )


def _parse_frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        return {}
    parsed: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        parsed[key.strip()] = value.strip().strip("\"'")
    return parsed
