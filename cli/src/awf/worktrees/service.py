from __future__ import annotations

import os
from pathlib import Path

from .git import GitClient
from .models import CommandResult
from .registry import WorktreeRegistry


def state_db_path() -> Path:
    configured = os.environ.get("AWF_WORKTREE_STATE_DB")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else Path.home() / ".local/state/awf/worktrees.sqlite3"
    )


def cache_root() -> Path:
    configured = os.environ.get("AWF_WORKTREE_CACHE_DIR")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else Path.home() / ".cache/awf/worktrees"
    )


class WorktreeService:
    def __init__(self, registry: WorktreeRegistry, git: GitClient) -> None:
        self.registry = registry
        self.git = git

    def status(self, *, initiative: str | None = None) -> CommandResult:
        repository_id = self.git.repository_id()
        leases = tuple(
            self.registry.list_leases_read_only(
                repository_id=repository_id,
                initiative=initiative,
                include_removed=False,
            )
        )
        return CommandResult.ok(
            "wt.status",
            decision="no_op" if not leases else "ready",
            leases=leases,
        )

    def doctor(self) -> CommandResult:
        registered = {
            lease.worktree_path: lease
            for lease in self.registry.list_leases_read_only(
                repository_id=self.git.repository_id(), include_removed=False
            )
        }
        actual = {item.path: item for item in self.git.list_worktrees()}
        actions: list[dict[str, object]] = []
        for path in sorted(actual.keys() - registered.keys()):
            actions.append({"kind": "unregistered_worktree", "path": str(path)})
        for path in sorted(registered.keys() - actual.keys()):
            actions.append(
                {
                    "kind": "orphaned_registration",
                    "path": str(path),
                    "lease_id": registered[path].id,
                }
            )
        return CommandResult.ok(
            "wt.doctor",
            decision="no_op" if not actions else "preview",
            actions=tuple(actions),
        )
