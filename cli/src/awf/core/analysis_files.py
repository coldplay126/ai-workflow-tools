from __future__ import annotations

import hashlib
import json
from pathlib import Path

from awf.core.config import AnalysisContext
from awf.tools.file_ops import FileOpsToolset


from awf.core.languages import DEFAULT_SOURCE_GLOBS

DEFAULT_ANALYSIS_GLOBS = DEFAULT_SOURCE_GLOBS


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def collect_domain_files(context: AnalysisContext) -> tuple[list[dict[str, str]], dict[str, object]]:
    toolset = FileOpsToolset(context.github_root)
    collected: list[dict[str, str]] = []
    seen: set[str] = set()
    collected_extensions: set[str] = set()
    existing_directories: list[str] = []
    glob_patterns = context.include_patterns if context.include_patterns else DEFAULT_ANALYSIS_GLOBS

    for directory in context.domain_directories:
        if Path(directory).exists():
            existing_directories.append(directory)
        try:
            relative = str(Path(directory).resolve().relative_to(context.github_root))
        except Exception:
            continue
        for pattern in glob_patterns:
            result = toolset.glob(f"{relative}/{pattern}")
            if not result.ok or not result.output.strip():
                continue
            for line in result.output.splitlines():
                repo_relative = line.strip()
                if not repo_relative or repo_relative in seen:
                    continue
                read_result = toolset.read(repo_relative)
                if not read_result.ok:
                    continue
                seen.add(repo_relative)
                collected_extensions.add(Path(repo_relative).suffix.lower())
                collected.append(
                    {
                        "path": repo_relative,
                        "sha256": _sha256_text(read_result.output),
                    }
                )
    stats: dict[str, object] = {
        "candidate_directories": list(context.domain_directories),
        "existing_directories": existing_directories,
        "glob_patterns": list(glob_patterns),
        "target_file_count": len(collected),
        "collected_file_extensions": sorted(ext for ext in collected_extensions if ext),
    }
    return collected, stats


def save_hashes_file(context: AnalysisContext, entries: list[dict[str, str]]) -> Path:
    tmp_dir = context.ai_context_dir / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / "hashes.json"
    payload = {
        "service": context.service,
        "domain": context.domain,
        "mode": context.mode,
        "files": entries,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_hashes_file(context: AnalysisContext) -> dict:
    path = context.ai_context_dir / ".tmp" / "hashes.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def hashes_changed(context: AnalysisContext, current_entries: list[dict[str, str]]) -> bool:
    saved = load_hashes_file(context)
    previous = saved.get("files", [])
    return previous != current_entries


def compute_changed_files(
    context: AnalysisContext,
    current_entries: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Compare current file entries with saved hashes.

    Returns (changed_entries, unchanged_entries) where each entry is from current_entries.
    New files, modified files, and renamed files are considered 'changed'.
    """
    saved = load_hashes_file(context)
    previous = saved.get("files", [])
    prev_map = {e["path"]: e["sha256"] for e in previous}
    # Build reverse map: hash → paths for rename detection
    prev_hash_to_paths: dict[str, list[str]] = {}
    for e in previous:
        prev_hash_to_paths.setdefault(e["sha256"], []).append(e["path"])

    changed: list[dict[str, str]] = []
    unchanged: list[dict[str, str]] = []
    for entry in current_entries:
        prev_hash = prev_map.get(entry["path"])
        if prev_hash is not None and prev_hash == entry["sha256"]:
            unchanged.append(entry)
        elif prev_hash is None and entry["sha256"] in prev_hash_to_paths:
            # Same content exists at a different path — likely a rename
            import sys
            old_paths = prev_hash_to_paths[entry["sha256"]]
            print(
                f"info: possible rename detected: {old_paths[0]} → {entry['path']}",
                file=sys.stderr,
            )
            changed.append(entry)
        else:
            changed.append(entry)
    return changed, unchanged


def compute_deleted_files(
    context: AnalysisContext,
    current_entries: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Identify files that existed in previous analysis but are now deleted.

    Returns list of entries from previous hashes that no longer exist.
    """
    saved = load_hashes_file(context)
    previous = saved.get("files", [])
    current_paths = {e["path"] for e in current_entries}
    return [e for e in previous if e["path"] not in current_paths]
