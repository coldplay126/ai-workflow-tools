from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .config import ConfigError, WorktreeConfig, load_worktree_config
from .git import GitClient, GitError, GitWorktree
from .github import ExternalServiceError, GhClient, PullRequest
from .locking import repository_lock
from .models import (
    CommandResult,
    DeploymentState,
    Lease,
    LeaseState,
    Purpose,
    now_iso,
)
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


_MAX_IMPORT_COLLISION_ACTIONS = 32
_DEPLOYMENT_STATUS_TIMEOUT_SECONDS = 30.0


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
        git: GitClient | None,
        *,
        config: WorktreeConfig | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        github: GhClient | None = None,
        deployment_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        cache_dir: Path | None = None,
        state_dir: Path | None = None,
        lock_dir: Path | None = None,
        git_factory: Callable[[Path], GitClient] = GitClient,
        skill_source_dir: Path | None = None,
        home_dir: Path | None = None,
    ) -> None:
        self.registry = registry
        self.git = git
        self.config = config or (
            load_worktree_config(git.repository_root()) if git is not None else WorktreeConfig()
        )
        self.command_runner = command_runner or subprocess.run
        self.github = github
        self.deployment_runner = deployment_runner or self.command_runner
        self.cache_dir = (cache_dir or cache_root()).expanduser().resolve()
        self.state_dir = (state_dir or state_db_path().parent).expanduser().resolve()
        self.lock_dir = (lock_dir or self.state_dir / "locks").expanduser().resolve()
        self.git_factory = git_factory
        self.skill_source_dir = (
            skill_source_dir
            or Path(__file__).resolve().parents[4]
            / "claude"
            / "skills"
            / "release-worktree-lifecycle"
        ).resolve()
        self.home_dir = (home_dir or Path.home()).expanduser().resolve()

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

            creation_sha = base_sha
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
                return self._handle_creation_failure(lease, error, creation_sha)

            prepare_error = self._prepare(lease, force=True)
            if prepare_error is not None:
                return self._block_prepare_failure(lease, prepare_error)
            return CommandResult.ok("wt.acquire", decision="ready", lease=lease)

    def status(
        self, *, initiative: str | None = None, refresh: bool = False
    ) -> CommandResult:
        filters: dict[str, str] = {"repository_id": self.git.repository_id()}
        if initiative is not None:
            filters["initiative"] = initiative
        leases = tuple(
            self.registry.list_leases_read_only(
                include_removed=False,
                **filters,
            )
        )
        if not refresh:
            return CommandResult.ok(
                "wt.status",
                decision="no_op" if not leases else "ready",
                leases=leases,
            )

        warnings: list[dict[str, str]] = []
        for lease in leases:
            self._refresh_lease(lease, warnings)
        refreshed_leases = tuple(
            self.registry.list_leases_read_only(
                include_removed=False,
                **filters,
            )
        )
        return CommandResult.ok(
            "wt.status",
            decision="no_op" if not refreshed_leases else "ready",
            leases=refreshed_leases,
            warnings=tuple(warnings),
        )

    def _refresh_lease(self, lease: Lease, warnings: list[dict[str, str]]) -> None:
        if lease.target_pr is None:
            return
        try:
            github = self.github or GhClient(lease.repository_root)
            pull_request = github.view_pr(lease.target_pr)
        except Exception:
            warnings.append(
                {
                    "code": "github_refresh_failed",
                    "message": f"Unable to refresh pull request state for lease {lease.id}.",
                }
            )
            return
        if pull_request.number != lease.target_pr:
            warnings.append(
                {
                    "code": "github_refresh_failed",
                    "message": f"Unable to refresh pull request state for lease {lease.id}.",
                }
            )
            return


        try:
            current = self._current_refresh_lease(lease.id, pull_request.number)
            if current is None:
                return
            if pull_request.state == "OPEN":
                self._transition_refresh(
                    lease.id,
                    pull_request,
                    LeaseState.PR_OPEN,
                    deployment_state=None,
                )
            elif (
                pull_request.state == "MERGED"
                or (
                    pull_request.state == "CLOSED"
                    and pull_request.merge_commit_sha is not None
                )
            ):
                if current.purpose is Purpose.PROMOTE:
                    self._refresh_promotion(lease.id, pull_request, warnings)
                else:
                    self._transition_refresh(
                        lease.id,
                        pull_request,
                        LeaseState.CLEANABLE,
                        deployment_state=DeploymentState.NOT_REQUIRED,
                    )
            elif pull_request.state == "CLOSED":
                self._transition_refresh(
                    lease.id,
                    pull_request,
                    LeaseState.CLOSED_UNMERGED,
                    deployment_state=None,
                )
            else:
                raise ExternalServiceError("unsupported pull request state")
        except ExternalServiceError:
            warnings.append(
                {
                    "code": "github_refresh_failed",
                    "message": f"Unable to refresh pull request state for lease {lease.id}.",
                }
            )
        except Exception:
            warnings.append(
                {
                    "code": "lease_refresh_failed",
                    "message": f"Unable to record refreshed state for lease {lease.id}.",
                }
            )

    def _refresh_promotion(
        self,
        lease_id: str,
        pull_request: PullRequest,
        warnings: list[dict[str, str]],
    ) -> None:
        current = self._current_refresh_lease(lease_id, pull_request.number)
        if current is None:
            return
        if (
            current.state is LeaseState.CLEANABLE
            and current.deployment_state is DeploymentState.HEALTHY
        ):
            return

        command = self.config.deployment_status_command
        self._transition_refresh(
            lease_id,
            pull_request,
            LeaseState.DEPLOYING,
            deployment_state=(
                DeploymentState.PENDING if command else DeploymentState.UNKNOWN
            ),
        )
        if not command:
            return
        current = self._current_refresh_lease(lease_id, pull_request.number)
        if current is None:
            return
        try:
            completed = self.deployment_runner(
                list(command),
                cwd=current.repository_root,
                check=False,
                shell=False,
                capture_output=True,
                text=True,
                timeout=_DEPLOYMENT_STATUS_TIMEOUT_SECONDS,
            )
        except Exception:
            warnings.append(
                {
                    "code": "deployment_refresh_failed",
                    "message": f"Unable to refresh deployment state for lease {lease_id}.",
                }
            )
            return
        if completed.returncode != 0:
            self._transition_refresh(
                lease_id,
                pull_request,
                LeaseState.BLOCKED,
                deployment_state=DeploymentState.FAILED,
            )
            return
        self._transition_refresh(
            lease_id,
            pull_request,
            LeaseState.DEPLOYED,
            deployment_state=DeploymentState.HEALTHY,
        )
        self._transition_refresh(
            lease_id,
            pull_request,
            LeaseState.CLEANABLE,
            deployment_state=DeploymentState.HEALTHY,
        )

    def _current_refresh_lease(
        self, lease_id: str, pull_request_number: int
    ) -> Lease | None:
        current = self.registry.get_lease(lease_id)
        if (
            current is None
            or current.state is LeaseState.REMOVED
            or current.target_pr != pull_request_number
        ):
            return None
        return current

    def _transition_refresh(
        self,
        lease_id: str,
        pull_request: PullRequest,
        state: LeaseState,
        *,
        deployment_state: DeploymentState | None,
    ) -> Lease | None:
        current = self._current_refresh_lease(lease_id, pull_request.number)
        if current is None:
            return None
        if (
            current.state is state
            and (
                deployment_state is None
                or current.deployment_state is deployment_state
            )
        ):
            return current
        return self.registry.transition(
            lease_id,
            state,
            expected_version=current.version,
            event_type="github_refresh",
            summary="GitHub refresh",
            observed_head_sha=pull_request.head_sha,
            pr_number=pull_request.number,
            deployment_state=deployment_state,
        )

    def import_root(self, root: Path, *, apply: bool) -> CommandResult:
        candidates = tuple(
            sorted(
                (
                    path.resolve()
                    for path in root.expanduser().resolve().iterdir()
                    if path.is_dir()
                    and ((path / ".git").is_file() or (path / ".git").is_dir())
                ),
                key=lambda path: str(path),
            )
        )
        discovered: dict[Path, tuple[GitClient, GitWorktree]] = {}
        for candidate in candidates:
            candidate_git = self.git_factory(candidate)
            worktrees = candidate_git.list_worktrees()
            metadata_path = next(
                (
                    item.path
                    for item in worktrees
                    if not item.bare
                    and not item.prunable
                    and item.path.resolve().is_dir()
                ),
                None,
            )
            if metadata_path is None:
                continue
            metadata_git = self.git_factory(metadata_path)
            for worktree in worktrees:
                path = worktree.path.resolve()
                if (
                    worktree.bare
                    or worktree.detached
                    or worktree.prunable
                    or not path.is_dir()
                    or not worktree.branch
                    or not worktree.head_sha
                ):
                    continue
                discovered.setdefault(path, (metadata_git, worktree))

        existing_leases = self.registry.list_leases_read_only(include_removed=True)
        existing_paths = {
            lease.worktree_path.resolve() for lease in existing_leases
        }
        reserved_identities = {
            (lease.repository_id, lease.initiative, lease.purpose)
            for lease in existing_leases
            if lease.state is not LeaseState.REMOVED
        }
        inventory_items: list[Lease] = []
        collision_actions: list[dict[str, object]] = []
        skipped_collisions = 0
        for path, (git, worktree) in sorted(
            discovered.items(),
            key=lambda item: (item[1][0].repository_id(), str(item[0])),
        ):
            if path in existing_paths:
                continue
            lease = self._imported_lease(git, worktree)
            identity = (lease.repository_id, lease.initiative, lease.purpose)
            if identity in reserved_identities:
                skipped_collisions += 1
                if len(collision_actions) < _MAX_IMPORT_COLLISION_ACTIONS:
                    collision_actions.append(
                        {
                            "kind": "skipped_identity_collision",
                            "path": str(path),
                            "repository_id": lease.repository_id,
                            "initiative": lease.initiative,
                            "purpose": lease.purpose.value,
                        }
                    )
                continue
            reserved_identities.add(identity)
            inventory_items.append(lease)
        if skipped_collisions > len(collision_actions):
            collision_actions.append(
                {
                    "kind": "skipped_identity_collision_summary",
                    "count": skipped_collisions,
                    "reported": len(collision_actions),
                }
            )
        inventory = tuple(inventory_items)
        preview_actions = [
            {
                "kind": "import_worktree",
                "path": str(lease.worktree_path),
                "lease_id": lease.id,
            }
            for lease in inventory
        ]
        preview_actions.extend(collision_actions)
        if not apply:
            return CommandResult.ok(
                "wt.import",
                decision="preview",
                leases=inventory,
                actions=tuple(preview_actions),
            )

        actions: list[dict[str, object]] = list(collision_actions)

        imported: list[Lease] = []
        for snapshot in inventory:
            with repository_lock(self.lock_dir / f"{snapshot.repository_id}.lock"):
                lease, skipped_action = self._revalidate_import_lease(snapshot)
                if lease is None:
                    if skipped_action is not None:
                        actions.append(skipped_action)
                    continue
                current = self.registry.list_leases(
                    include_removed=True,
                    worktree_path=lease.worktree_path,
                )
                if current:
                    actions.append(
                        {
                            "kind": "skipped_existing_registration",
                            "path": str(lease.worktree_path),
                        }
                    )
                    continue
                if self.registry.find_active(
                    lease.repository_id, lease.initiative, lease.purpose
                ):
                    actions.append(self._identity_collision_action(lease))
                    continue
                try:
                    created = self.registry.create_lease(lease)
                    imported.append(created)
                    actions.append(
                        {
                            "kind": "import_worktree",
                            "path": str(created.worktree_path),
                            "lease_id": created.id,
                        }
                    )
                except ValueError as error:
                    if str(error) != "active lease already exists":
                        raise
                    actions.append(self._identity_collision_action(lease))
                except sqlite3.IntegrityError:
                    actions.append(self._identity_collision_action(lease))
        return CommandResult.ok(
            "wt.import",
            decision="ready" if imported else "no_op",
            leases=tuple(imported),
            actions=tuple(actions),
        )

    def _revalidate_import_lease(
        self, snapshot: Lease
    ) -> tuple[Lease | None, dict[str, object] | None]:
        try:
            git = self.git_factory(snapshot.repository_root)
            if git.repository_id() != snapshot.repository_id:
                return None, {
                    "kind": "skipped_unavailable_worktree",
                    "path": str(snapshot.worktree_path),
                    "reason": "repository_identity_changed",
                }
            worktree = next(
                (
                    item
                    for item in git.list_worktrees()
                    if item.path.resolve() == snapshot.worktree_path.resolve()
                ),
                None,
            )
            if (
                worktree is None
                or worktree.bare
                or worktree.detached
                or worktree.prunable
                or not worktree.path.resolve().is_dir()
                or not worktree.branch
                or not worktree.head_sha
            ):
                return None, {
                    "kind": "skipped_unavailable_worktree",
                    "path": str(snapshot.worktree_path),
                    "reason": "registration_changed",
                }
            return self._imported_lease(git, worktree), None
        except (GitError, OSError):
            return None, {
                "kind": "skipped_unavailable_worktree",
                "path": str(snapshot.worktree_path),
                "reason": "registration_unavailable",
            }

    @staticmethod
    def _identity_collision_action(lease: Lease) -> dict[str, object]:
        return {
            "kind": "skipped_identity_collision",
            "path": str(lease.worktree_path),
            "repository_id": lease.repository_id,
            "initiative": lease.initiative,
            "purpose": lease.purpose.value,
        }

    def adopt(self, lease_id: str, *, apply: bool) -> CommandResult:
        imported = self.registry.get_lease_read_only(lease_id)
        if imported is None:
            return self._adopt_blocked(
                "unknown_lease",
                f"lease {lease_id} does not exist",
            )
        if not apply:
            blocker = self._adoption_blocker(imported)
            if blocker is not None:
                return blocker
            return CommandResult.ok(
                "wt.adopt",
                decision="preview",
                lease=imported,
                actions=(
                    {
                        "kind": "adopt",
                        "path": str(imported.worktree_path),
                        "lease_id": imported.id,
                    },
                ),
            )

        with repository_lock(self.lock_dir / f"{imported.repository_id}.lock"):
            imported = self.registry.get_lease(lease_id)
            if imported is None:
                return self._adopt_blocked(
                    "unknown_lease",
                    f"lease {lease_id} does not exist",
                )
            blocker = self._adoption_blocker(imported)
            if blocker is not None:
                return blocker
            adopted = self.registry.transition(
                imported.id,
                imported.state,
                expected_version=imported.version,
                managed=True,
                summary="imported lease adopted",
            )
        return CommandResult.ok("wt.adopt", decision="ready", lease=adopted)

    def _adoption_blocker(self, imported: Lease) -> CommandResult | None:
        if imported.state is LeaseState.REMOVED:
            return self._adopt_blocked(
                "removed_lease",
                f"lease {imported.id} has been removed",
                lease=imported,
            )
        if imported.owner_kind != "imported":
            return self._adopt_blocked(
                "lease_not_imported",
                f"lease {imported.id} was not imported",
                lease=imported,
            )
        if imported.managed:
            return self._adopt_blocked(
                "already_adopted",
                f"lease {imported.id} is already managed",
                lease=imported,
            )
        if imported.state is LeaseState.CLOSED_UNMERGED:
            return self._adopt_blocked(
                "closed_unmerged",
                f"lease {imported.id} was closed without merging",
                lease=imported,
            )
        if self.git is None or self.git.repository_id() != imported.repository_id:
            return self._adopt_blocked(
                "repository_mismatch",
                f"lease {imported.id} does not match this repository",
                lease=imported,
            )
        registered = self.git.list_worktrees()
        expected_path = imported.worktree_path.resolve()
        worktree = next(
            (
                item
                for item in registered
                if item.path.resolve() == expected_path and expected_path.is_dir()
            ),
            None,
        )
        if worktree is None:
            return self._adopt_blocked(
                "orphaned_lease",
                f"lease {imported.id} is not registered as a Git worktree",
                lease=imported,
            )
        if worktree.bare or worktree.detached or worktree.branch != imported.branch:
            return self._adopt_blocked(
                "branch_mismatch",
                f"lease {imported.id} does not match its registered branch",
                lease=imported,
            )
        if any(
            item.path.resolve() != expected_path and item.branch == imported.branch
            for item in registered
        ):
            return self._adopt_blocked(
                "branch_conflict",
                f"branch {imported.branch!r} is checked out at another worktree",
                lease=imported,
            )
        if (
            worktree.head_sha != imported.head_sha
            or self.git.head_sha(imported.worktree_path) != imported.head_sha
        ):
            return self._adopt_blocked(
                "head_mismatch",
                f"lease {imported.id} does not match its recorded HEAD",
                lease=imported,
            )
        if self.git.status_porcelain(imported.worktree_path):
            return self._adopt_blocked(
                "dirty_worktree",
                f"lease {imported.id} has uncommitted changes",
                lease=imported,
            )
        if self.git.head_sha(imported.worktree_path) != imported.head_sha:
            return self._adopt_blocked(
                "head_mismatch",
                f"lease {imported.id} changed while being adopted",
                lease=imported,
            )
        return None

    def doctor(self) -> CommandResult:
        if self.git is None:
            raise ValueError("doctor requires a repository")
        repository_id = self.git.repository_id()
        registered = {
            lease.worktree_path.resolve(): lease
            for lease in self.registry.list_leases_read_only(
                repository_id=repository_id, include_removed=False
            )
        }
        actual = {
            item.path.resolve(): item for item in self.git.list_worktrees()
        }
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
        for branch, paths in self._duplicate_registered_branches(
            registered, actual
        ).items():
            actions.append(
                {
                    "kind": "duplicate_branch",
                    "branch": branch,
                    "paths": [str(path) for path in paths],
                }
            )
        for path in sorted(registered.keys() & actual.keys()):
            lease = registered[path]
            worktree = actual[path]
            if worktree.head_sha != lease.head_sha:
                actions.append(
                    {
                        "kind": "head_mismatch",
                        "path": str(path),
                        "lease_id": lease.id,
                        "expected_head_sha": lease.head_sha,
                        "actual_head_sha": worktree.head_sha,
                    }
                )
            if not worktree.bare and path.is_dir() and self.git.status_porcelain(path):
                actions.append(
                    {
                        "kind": "dirty_worktree",
                        "path": str(path),
                        "lease_id": lease.id,
                    }
                )
        cache_repository_dir = self.cache_dir / self.git.repository_name()
        if cache_repository_dir.is_dir():
            for path in sorted(
                (
                    candidate.resolve()
                    for candidate in cache_repository_dir.iterdir()
                    if candidate.is_dir()
                ),
                key=str,
            ):
                if path not in actual and self._is_current_cache_child(
                    path, repository_id, registered
                ):
                    actions.append(
                        {
                            "kind": "unregistered_cache_directory",
                            "path": str(path),
                        }
                    )
        actions.extend(self._skill_link_actions())
        return CommandResult.ok(
            "wt.doctor",
            decision="no_op" if not actions else "preview",
            actions=tuple(actions),
        )

    def _is_current_cache_child(
        self,
        path: Path,
        repository_id: str,
        registered: dict[Path, Lease],
    ) -> bool:
        try:
            return self.git_factory(path).repository_id() == repository_id
        except (GitError, OSError, ValueError):
            lease = registered.get(path)
            return lease is not None and lease.managed

    def _imported_lease(self, git: GitClient, worktree: GitWorktree) -> Lease:
        if not worktree.branch or not worktree.head_sha:
            raise ValueError("imported worktree must have a branch and HEAD")
        lease = Lease.new(
            repository_id=git.repository_id(),
            repository_name=git.repository_name(),
            repository_root=git.repository_root(),
            worktree_path=worktree.path,
            initiative=self._import_initiative(worktree.branch, worktree.head_sha),
            purpose=Purpose.SCRATCH,
            branch=worktree.branch,
            base_ref=worktree.branch,
            head_sha=worktree.head_sha,
            managed=False,
            owner_kind="imported",
        )
        if git.status_porcelain(worktree.path):
            return replace(lease, state=LeaseState.DIRTY)
        return lease

    @staticmethod
    def _import_initiative(branch: str, head_sha: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")
        return f"import-{slug or 'worktree'}-{head_sha[:8].lower()}"

    @staticmethod
    def _duplicate_registered_branches(
        registered: dict[Path, Lease], actual: dict[Path, GitWorktree]
    ) -> dict[str, tuple[Path, ...]]:
        paths_by_branch: dict[str, list[Path]] = {}
        for path in sorted(registered.keys() & actual.keys()):
            branch = actual[path].branch
            if branch is not None:
                paths_by_branch.setdefault(branch, []).append(path)
        return {
            branch: tuple(paths)
            for branch, paths in sorted(paths_by_branch.items())
            if len(paths) > 1
        }

    def _skill_link_actions(self) -> list[dict[str, object]]:
        actions: list[dict[str, object]] = []
        for path in (
            self.home_dir / ".claude" / "skills" / "release-worktree-lifecycle",
            self.home_dir / ".agents" / "skills" / "release-worktree-lifecycle",
        ):
            if path.exists() and path.resolve() == self.skill_source_dir:
                continue
            actions.append(
                {
                    "kind": (
                        "wrong_skill_link"
                        if path.is_symlink() or path.exists()
                        else "missing_skill_link"
                    ),
                    "path": str(path),
                    "target": str(self.skill_source_dir),
                }
            )
        return actions

    @staticmethod
    def _adopt_blocked(
        code: str, message: str, *, lease: Lease | None = None
    ) -> CommandResult:
        return CommandResult.blocked(
            "wt.adopt",
            blockers=({"code": code, "message": message},),
            lease=lease,
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

    def _handle_creation_failure(
        self, lease: Lease, error: Exception, creation_sha: str
    ) -> CommandResult:
        try:
            current_head = self.git.head_sha(lease.worktree_path)
        except (GitError, OSError) as head_error:
            return self._blocked(
                "registry_recovery_failed",
                f"could not confirm worktree head after registry failure: {head_error}",
                lease=lease,
            )
        if current_head != creation_sha:
            return self._blocked(
                "registry_recovery_failed",
                "worktree head changed after creation; preserving worktree and branch",
                lease=lease,
            )
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
            self.git.delete_branch_if_at(lease.branch, creation_sha)
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
