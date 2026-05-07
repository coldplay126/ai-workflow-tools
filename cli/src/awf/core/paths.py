from __future__ import annotations

from pathlib import Path
from typing import Optional


def _looks_like_repo_root(path: Path) -> bool:
    return any(
        [
            (path / "claude" / "agents").is_dir(),
            (path / ".awf.toml").is_file(),
            (path / ".workflow").is_dir(),
            (path / ".git").is_dir(),
        ]
    )


def find_repo_root(explicit_root: Optional[str] = None) -> Path:
    if explicit_root:
        root = Path(explicit_root).expanduser().resolve()
        if not _looks_like_repo_root(root):
            raise FileNotFoundError(f"Not a ai-workflow-tools repo root: {root}")
        return root

    here = Path.cwd().resolve()
    candidates = [here, *here.parents]
    for candidate in candidates:
        if _looks_like_repo_root(candidate):
            return candidate
    raise FileNotFoundError("Could not locate ai-workflow-tools repo root. Pass --repo-root.")
