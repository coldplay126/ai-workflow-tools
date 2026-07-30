from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .config import ConfigError, WorktreeConfig, load_worktree_config
from .git import GitClient, GitError, GitWorktree
from .locking import repository_lock
from .models import CommandResult, Lease, LeaseState, Purpose, now_iso
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


def _initiative_slug(initiative: str) -> str:
    if not initiative or not initiative.isascii():
        raise ValueError("initiative must contain lowercase letters, digits, and single hyphens")
    if initiative[0] == "-" or initiative[-1] == "-" or "--" in initiative:
        raise ValueError("initiative must contain lowercase letters, digits, and single hyphens")
    if not all(character.islower() or character.isdigit() or character == "-" for character in initiative):
        raise ValueError("initiative must contain lowercase letters, digits, and single hyphens")
    return initiative


class WorktreeService:
    def __init__(
        self,
        registry: WorktreeRegistry,
        git: GitClient,
        *,
        config: WorktreeConfig | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        cache_dir: Path | None = None,
        state_dir: Path | None = None,
        lock_dir: Path | None = None,
    ) -> None:
        self.registry = registry
        self.git = git
        self.config = config or load_worktree_config(git.repository_root())
        self.command_runner = command_runner or subprocess.run
        self.cache_dir = (cache_dir or cache_root()).expanduser().resolve()
        self.state_dir = (state_dir or state_db_path().parent).expanduser().resolve()
        self.lock_dir = (lock_dir or self.state_dir / "locks").expanduser().resolve()

    def acquire(
        self,
        *,
        initiative: str,
        purpose: Purpose,
        base: str | None,
        branch: str | None,
        owner_id: str | None,
        apply: bool,
    ) -> CommandResult:
        slug = _initiative_slug(initiative)
        expected_branch = branch or f"awf/{slug}/{purpose.value}"
        repository_id = self.git.repository_id()
        with repository_lock(self.lock_dir / f"{repository_id}.lock"):
            active = self.registry.find_active(repository_id, slug, purpose)
            if active is not None:
                return self._reuse(active, expected_branch)

            base_ref, base_sha = self._resolve_base(base)
            lease = Lease.new(
                repository_id=repository_id,
                repository_name=self.git.repository_name(),
                repository_root=self.git.repository_root(),
                worktree_path=self.cache_dir / self.git.repository_name(),
                initiative=slug,
                purpose=purpose,
                branch=expected_branch,
                base_ref=base_ref,
                head_sha=base_sha,
                managed=True,
                owner_kind="awf",
                owner_id=owner_id,
            )
            lease = replace(
                lease,
                worktree_path=self.cache_dir / lease.repository_name / lease.id,
            )
            if not apply:
                return CommandResult.ok(
                    "wt.acquire",
                    decision="preview",
                    actions=(
                        {
                            "kind": "create_worktree",
                            "path": str(lease.worktree_path),
                            "branch": lease.branch,
                            "base_ref": lease.base_ref,
                            "head_sha": lease.head_sha,
                        },
                    ),
                )

            branch_conflict = self._branch_conflict(expected_branch)
            if branch_conflict is not None:
                return self._blocked(
                    "branch_conflict",
                    f"branch {expected_branch!r} is already checked out at {branch_conflict}",
                )

            try:
                self.git.add_worktree(lease.worktree_path, lease.branch, base_sha)
            except GitError as error:
                return self._blocked("worktree_conflict", str(error), lease=lease)
            try:
                lease = replace(
                    lease,
                    head_sha=self.git.head_sha(lease.worktree_path),
                )
                lease = self.registry.create_lease(lease)
            except (GitError, OSError, RuntimeError, ValueError, sqlite3.Error) as error:
                return self._handle_creation_failure(lease, error)

            prepare_error = self._prepare(lease, force=True)
            if prepare_error is not None:
                return self._block_prepare_failure(lease, prepare_error)
            return CommandResult.ok("wt.acquire", decision="ready", lease=lease)

    def status(self, *, initiative: str | None = None) -> CommandResult:
        filters: dict[str, str] = {"repository_id": self.git.repository_id()}
        if initiative is not None:
            filters["initiative"] = initiative
        leases = tuple(
            self.registry.list_leases_read_only(
                include_removed=False,
                **filters,
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

    def _reuse(self, lease: Lease, expected_branch: str) -> CommandResult:
        if lease.state is not LeaseState.ACTIVE:
            return self._blocked(
                "lease_not_active",
                f"lease {lease.id} is {lease.state.value}",
                lease=lease,
            )
        if lease.branch != expected_branch:
            return self._blocked(
                "lease_conflict",
                f"lease {lease.id} uses branch {lease.branch!r}, not {expected_branch!r}",
                lease=lease,
            )
        worktree = self._registered_worktree(lease)
        if worktree is None:
            return self._blocked(
                "orphaned_lease",
                f"lease {lease.id} is not registered as a Git worktree",
                lease=lease,
            )
        if worktree.branch != lease.branch or worktree.detached or worktree.bare:
            return self._blocked(
                "lease_conflict",
                f"lease {lease.id} does not match its registered branch",
                lease=lease,
            )
        if self.git.status_porcelain(lease.worktree_path):
            return self._blocked(
                "dirty_lease",
                f"lease {lease.id} has uncommitted changes",
                lease=lease,
            )
        try:
            lease = self.registry.touch(
                lease.id,
                expected_version=lease.version,
                head_sha=self.git.head_sha(lease.worktree_path),
            )
        except RuntimeError as error:
            return self._blocked("lease_conflict", str(error), lease=lease)

        prepare_error = self._prepare(lease, force=False)
        if prepare_error is not None:
            return self._block_prepare_failure(lease, prepare_error)
        return CommandResult.ok("wt.acquire", decision="reuse", lease=lease)

    def _resolve_base(self, base: str | None) -> tuple[str, str]:
        candidate = base if base is not None else self.config.default_base
        if candidate is None:
            candidate = self.git.default_remote_branch()
        if not candidate:
            raise ConfigError("base must not be empty")
        base_ref = self._remote_base_ref(candidate)
        fetch_ref = base_ref[len("origin/") :]
        return base_ref, self.git.fetch_ref(fetch_ref)

    @staticmethod
    def _remote_base_ref(base: str) -> str:
        if base.startswith("refs/remotes/origin/"):
            base = base[len("refs/remotes/origin/") :]
        elif base.startswith("refs/heads/"):
            base = base[len("refs/heads/") :]
        elif base.startswith("origin/"):
            base = base[len("origin/") :]
        elif base.startswith("refs/"):
            raise ConfigError(f"invalid base ref: {base!r}")
        if not WorktreeService._is_safe_remote_branch(base):
            raise ConfigError(f"invalid base ref: {base!r}")
        return f"origin/{base}"

    @staticmethod
    def _is_safe_remote_branch(branch: str) -> bool:
        if not branch or branch[0] in ".-/" or branch[-1] in "./":
            return False
        if ".." in branch or "//" in branch:
            return False
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-"
        if any(character not in allowed for character in branch):
            return False
        return all(not component.endswith(".lock") for component in branch.split("/"))

    def _branch_conflict(self, branch: str) -> Path | None:
        for worktree in self.git.list_worktrees():
            if worktree.branch == branch:
                return worktree.path
        return None

    def _registered_worktree(self, lease: Lease) -> GitWorktree | None:
        for worktree in self.git.list_worktrees():
            if worktree.path == lease.worktree_path and lease.worktree_path.is_dir():
                return worktree
        return None

    def _prepare(self, lease: Lease, *, force: bool) -> str | None:
        if not self.config.prepare_command:
            return None
        try:
            key = self._prepare_key(lease.worktree_path)
            marker = self._prepare_marker_path(lease.id)
            if not force and self._marker_key(marker) == key:
                return None
            self._run_command(self.config.prepare_command, lease.worktree_path)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps({"key": key, "completed_at": now_iso()}, sort_keys=True),
                encoding="utf-8",
            )
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
            return str(error)
        return None

    def _prepare_key(self, worktree_path: Path) -> str:
        digest = hashlib.sha256()
        digest.update(sys.version.encode("utf-8"))
        digest.update(b"\0")
        for configured_path in self.config.prepare_inputs:
            input_path = self._prepare_input_path(worktree_path, configured_path)
            digest.update(configured_path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(input_path.read_bytes())
            digest.update(b"\0")
        version = self._run_command(
            (self.config.prepare_command[0], "--version"), worktree_path
        )
        digest.update(version.encode("utf-8"))
        digest.update(b"\0")
        for argument in self.config.prepare_command:
            digest.update(argument.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def _prepare_input_path(worktree_path: Path, configured_path: str) -> Path:
        candidate = (worktree_path / configured_path).resolve()
        try:
            candidate.relative_to(worktree_path.resolve())
        except ValueError as error:
            raise ValueError(
                f"prepare input must stay within the worktree: {configured_path!r}"
            ) from error
        if not candidate.is_file():
            raise ValueError(f"prepare input does not exist: {configured_path!r}")
        return candidate

    def _run_command(self, argv: tuple[str, ...], cwd: Path) -> str:
        completed = self.command_runner(
            list(argv),
            cwd=cwd,
            check=False,
            shell=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"command failed with exit {completed.returncode}: {argv[0]}")
        return f"{completed.stdout or ''}\0{completed.stderr or ''}"

    def _prepare_marker_path(self, lease_id: str) -> Path:
        return self.state_dir / "prepare" / f"{lease_id}.json"

    @staticmethod
    def _marker_key(marker: Path) -> str | None:
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        key = payload.get("key") if isinstance(payload, dict) else None
        return key if isinstance(key, str) else None

    def _handle_creation_failure(self, lease: Lease, error: Exception) -> CommandResult:
        try:
            clean = not self.git.status_porcelain(lease.worktree_path)
        except (GitError, OSError):
            clean = False
        if not clean:
            return self._blocked("registry_conflict", str(error), lease=lease)
        try:
            self.git.remove_worktree(lease.worktree_path)
        except (GitError, OSError) as cleanup_error:
            return self._blocked(
                "registry_recovery_failed",
                f"worktree cleanup failed after registry failure: {cleanup_error}",
                lease=lease,
            )
        try:
            self.git.delete_local_branch(lease.branch, force=True)
        except (GitError, OSError) as cleanup_error:
            return self._blocked(
                "registry_recovery_failed",
                f"branch cleanup failed after registry failure: {cleanup_error}",
                lease=lease,
            )
        return self._blocked("registry_conflict", str(error), lease=lease)

    def _block_prepare_failure(self, lease: Lease, error: str) -> CommandResult:
        try:
            lease = self.registry.transition(
                lease.id,
                LeaseState.BLOCKED,
                expected_version=lease.version,
                summary=f"prepare failed: {error}",
            )
        except RuntimeError:
            pass
        return self._blocked("prepare_failed", error, lease=lease)

    @staticmethod
    def _blocked(
        code: str, message: str, *, lease: Lease | None = None
    ) -> CommandResult:
        return CommandResult.blocked(
            "wt.acquire",
            blockers=({"code": code, "message": message},),
            lease=lease,
        )
