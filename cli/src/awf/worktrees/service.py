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

    def promote(
        self, *, source_pr: int, target_branch: str, apply: bool
    ) -> CommandResult:
        if (
            not isinstance(source_pr, int)
            or isinstance(source_pr, bool)
            or source_pr <= 0
        ):
            return self._promotion_blocked(
                "invalid_source_pr", "source pull request must be a positive integer"
            )
        try:
            target_ref = self._promotion_target_ref(target_branch)
        except ConfigError as error:
            return self._promotion_blocked("invalid_target_branch", str(error))
        try:
            github = self.github or GhClient(self.git.repository_root())
            source = github.view_pr(source_pr)
        except (ExternalServiceError, ValueError) as error:
            return self._promotion_blocked("source_pr_unavailable", str(error))
        source_blocker = self._promotion_source_blocker(source, target_ref)
        if source_blocker is not None:
            return source_blocker
        if not self.config.verify_production:
            return self._promotion_blocked(
                "production_verify_missing",
                "verify.production.commands must configure at least one command",
            )

        if not apply:
            try:
                target_sha = self.git.resolve_ref(target_ref)
            except GitError as error:
                return self._promotion_blocked("target_ref_unavailable", str(error))
            lease = self._new_promotion_lease(source, target_ref, target_sha)
            return CommandResult.ok(
                "wt.promote",
                decision="preview",
                actions=(
                    {
                        "kind": "create_worktree",
                        "path": str(lease.worktree_path),
                        "branch": lease.branch,
                        "source_pr": source.number,
                        "source_base_sha": source.base_sha,
                        "source_head_sha": source.head_sha,
                        "target_branch": target_branch,
                        "target_base_sha": target_sha,
                    },
                    *(
                        {
                            "kind": "verify_production",
                            "argv": list(command),
                        }
                        for command in self.config.verify_production
                    ),
                ),
            )

        source_merge_commit = source.merge_commit_sha
        if source_merge_commit is None:
            return self._promotion_blocked(
                "source_merge_provenance_missing",
                "source pull request does not provide a merge commit",
            )

        repository_id = self.git.repository_id()
        initiative = self._promotion_initiative(source.number, target_branch)
        expected_branch = self._promotion_branch(initiative)
        with repository_lock(self.lock_dir / f"{repository_id}.lock"):
            active = self.registry.find_active(
                repository_id, initiative, Purpose.PROMOTE
            )
            if active is not None:
                return self._reuse_promotion(
                    active,
                    source_pr=source.number,
                    target_ref=target_ref,
                    expected_branch=expected_branch,
                    github=github,
                    target_branch=target_branch,
                )
            try:
                source_base_sha = self.git.fetch_ref(source.base_sha)
                source_head_sha = self.git.fetch_ref(source.head_sha)
                source_merge_sha = self.git.fetch_ref(source_merge_commit)
                target_sha = self.git.fetch_ref(target_branch)
                merge_parents = self.git.commit_parents(source_merge_sha)
                if not merge_parents:
                    return self._promotion_blocked(
                        "source_merge_provenance_invalid",
                        "source pull request merge commit has no parent",
                    )
                merge_base = self.git.merge_base(merge_parents[0], source_head_sha)
                commits = self.git.ordered_commits(merge_base, source_head_sha)
                source_paths = self.git.changed_paths(
                    self.git.repository_root(), merge_base, source_head_sha
                )
            except GitError as error:
                return self._promotion_blocked("source_delta_unavailable", str(error))
            if (
                source_base_sha != source.base_sha
                or source_head_sha != source.head_sha
                or source_merge_sha != source_merge_commit
            ):
                return self._promotion_blocked(
                    "source_sha_mismatch",
                    "fetched source pull request refs do not match the reviewed SHAs",
                )
            expected_paths = tuple(sorted(source.changed_paths))
            if not commits:
                return self._promotion_blocked(
                    "source_pr_empty_delta",
                    "source pull request has no commits after its merge base",
                )
            if source_paths != expected_paths:
                return self._promotion_blocked(
                    "source_delta_mismatch",
                    "source pull request paths do not match its reviewed Git delta",
                )

            lease = self._new_promotion_lease(source, target_ref, target_sha)
            branch_conflict = self._branch_conflict(lease.branch)
            if branch_conflict is not None:
                return self._promotion_blocked(
                    "branch_conflict",
                    f"branch {lease.branch!r} is already checked out at {branch_conflict}",
                    lease=lease,
                )
            try:
                self.git.add_worktree(lease.worktree_path, lease.branch, target_sha)
            except GitError as error:
                return self._promotion_blocked("worktree_conflict", str(error), lease=lease)
            try:
                lease = replace(
                    lease, head_sha=self.git.head_sha(lease.worktree_path)
                )
                lease = self.registry.create_lease(lease)
            except (GitError, OSError, RuntimeError, ValueError, sqlite3.Error) as error:
                result = self._handle_creation_failure(lease, error, target_sha)
                return replace(result, command="wt.promote")

            try:
                self.git.cherry_pick(lease.worktree_path, commits)
                promotion_head = self.git.commit(
                    lease.worktree_path,
                    self._promotion_message(
                        source=source,
                        target_sha=target_sha,
                        lease=lease,
                        target_branch=target_branch,
                    ),
                    allow_empty=True,
                )
                promoted_paths = self.git.changed_paths(
                    lease.worktree_path, target_sha, promotion_head
                )
                if promoted_paths != expected_paths:
                    return self._block_promotion_lease(
                        lease,
                        "promotion_delta_mismatch",
                        "promotion paths do not exactly match the reviewed pull request",
                    )
                if any(
                    self.git.path_blob(source_head_sha, path)
                    != self.git.path_blob(promotion_head, path)
                    for path in expected_paths
                ):
                    return self._block_promotion_lease(
                        lease,
                        "promotion_content_mismatch",
                        "promotion contents do not exactly match the reviewed pull request",
                    )
                verification_actions = self._verify_promotion(lease.worktree_path)
            except (GitError, OSError, RuntimeError, subprocess.SubprocessError) as error:
                return self._block_promotion_lease(
                    lease, "promotion_apply_failed", str(error)
                )
            try:
                lease = self.registry.transition(
                    lease.id,
                    LeaseState.ACTIVE,
                    expected_version=lease.version,
                    event_type="promotion_publish_pending",
                    summary="promotion verified; publication pending",
                    observed_head_sha=promotion_head,
                    head_sha=promotion_head,
                )
            except (RuntimeError, sqlite3.Error) as error:
                return self._promotion_blocked(
                    "registry_conflict", str(error), lease=lease
                )


            try:
                self.git.push_branch(lease.worktree_path, lease.branch)
                target_pull_request = github.find_open_pr(
                    head=lease.branch, base=target_branch
                )
                if target_pull_request is None:
                    target_pull_request = github.create_pr(
                        base=target_branch,
                        head=lease.branch,
                        title=f"Promote PR #{source.number} to {target_branch}",
                        body=self._promotion_body(
                            source=source,
                            target_sha=target_sha,
                            lease=lease,
                        ),
                    )
            except (ExternalServiceError, GitError, ValueError) as error:
                return self._promotion_blocked(
                    "promotion_publish_failed", str(error), lease=lease
                )
            if (
                target_pull_request.state != "OPEN"
                or target_pull_request.base_ref != target_branch
                or target_pull_request.head_ref != lease.branch
            ):
                return self._block_promotion_lease(
                    lease,
                    "target_pr_mismatch",
                    "GitHub did not return the exact open promotion pull request",
                )
            if target_pull_request.head_sha != promotion_head:
                return self._block_promotion_lease(
                    lease,
                    "target_pr_head_mismatch",
                    "GitHub target pull request head does not match the verified promotion",
                )
            try:
                lease = self.registry.transition(
                    lease.id,
                    LeaseState.PR_OPEN,
                    expected_version=lease.version,
                    event_type="promotion_pr_open",
                    summary=f"promotion PR #{target_pull_request.number} opened",
                    observed_head_sha=promotion_head,
                    pr_number=target_pull_request.number,
                    head_sha=promotion_head,
                )
            except (RuntimeError, sqlite3.Error) as error:
                return self._promotion_blocked(
                    "registry_conflict", str(error), lease=lease
                )
            return CommandResult.ok(
                "wt.promote",
                decision="ready",
                lease=lease,
                actions=verification_actions,
            )

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

    def _promotion_target_ref(self, target_branch: str) -> str:
        if not isinstance(target_branch, str) or not target_branch:
            raise ConfigError("target branch must be a non-empty string")
        target_ref = self._remote_base_ref(target_branch)
        if target_ref != f"origin/{target_branch}":
            raise ConfigError("target branch must not include a ref prefix")
        configured = self.config.production_branch
        if configured is not None and target_ref != self._remote_base_ref(configured):
            raise ConfigError(
                f"target branch must match configured production branch {configured!r}"
            )
        if configured is None and target_branch not in {"main", "master"}:
            raise ConfigError("target branch must be main or master")
        return target_ref

    def _promotion_source_blocker(
        self, source: PullRequest, target_ref: str
    ) -> CommandResult | None:
        if source.state != "MERGED":
            return self._promotion_blocked(
                "source_pr_not_merged",
                f"source pull request #{source.number} is {source.state}",
            )
        if source.review_decision != "APPROVED":
            return self._promotion_blocked(
                "source_pr_not_approved",
                f"source pull request #{source.number} is not approved",
            )
        if not source.checks_passed:
            return self._promotion_blocked(
                "source_pr_checks_failed",
                f"source pull request #{source.number} has incomplete or failing checks",
            )
        if self.config.default_base is None:
            return self._promotion_blocked(
                "source_base_unconfigured",
                "worktree.default_base must identify the staging branch",
            )
        try:
            source_base_ref = self._remote_base_ref(source.base_ref)
            configured_base_ref = self._remote_base_ref(self.config.default_base)
        except ConfigError as error:
            return self._promotion_blocked("source_pr_base_invalid", str(error))
        if source_base_ref != configured_base_ref or source_base_ref == target_ref:
            return self._promotion_blocked(
                "source_pr_base_mismatch",
                "source pull request does not target the configured staging branch",
            )
        return None

    def _new_promotion_lease(
        self, source: PullRequest, target_ref: str, target_sha: str
    ) -> Lease:
        target_branch = target_ref[len("origin/") :]
        initiative = self._promotion_initiative(source.number, target_branch)
        lease = Lease.new(
            repository_id=self.git.repository_id(),
            repository_name=self.git.repository_name(),
            repository_root=self.git.repository_root(),
            worktree_path=self.cache_dir / self.git.repository_name(),
            initiative=initiative,
            purpose=Purpose.PROMOTE,
            branch=self._promotion_branch(initiative),
            base_ref=target_ref,
            head_sha=target_sha,
            managed=True,
            owner_kind="awf",
            source_pr=source.number,
        )
        return replace(
            lease,
            worktree_path=self.cache_dir / lease.repository_name / lease.id,
        )

    @staticmethod
    def _promotion_initiative(source_pr: int, target_branch: str) -> str:
        return f"pr-{source_pr}-to-{target_branch}"

    @staticmethod
    def _promotion_branch(initiative: str) -> str:
        return f"awf/{initiative}/promote"

    def _reuse_promotion(
        self,
        lease: Lease,
        *,
        source_pr: int,
        target_ref: str,
        expected_branch: str,
        github: GhClient,
        target_branch: str,
    ) -> CommandResult:
        if (
            lease.source_pr != source_pr
            or lease.base_ref != target_ref
            or lease.branch != expected_branch
        ):
            return self._promotion_blocked(
                "promotion_lease_conflict",
                f"lease {lease.id} does not match the requested promotion",
                lease=lease,
            )
        if lease.state is LeaseState.PR_OPEN and lease.target_pr is not None:
            return CommandResult.ok("wt.promote", decision="reuse", lease=lease)
        if lease.state is LeaseState.BLOCKED:
            return self._promotion_blocked(
                "promotion_incomplete",
                f"lease {lease.id} is blocked without an open target pull request",
                lease=lease,
            )
        if lease.state is not LeaseState.ACTIVE:
            return self._promotion_blocked(
                "promotion_incomplete",
                f"lease {lease.id} is {lease.state.value} without an open target pull request",
                lease=lease,
            )
        try:
            events = self.registry.list_events(lease.id)
        except sqlite3.Error as error:
            return self._promotion_blocked(
                "promotion_reconciliation_failed", str(error), lease=lease
            )
        if not events or events[-1].event_type != "promotion_publish_pending":
            return self._promotion_blocked(
                "promotion_incomplete",
                f"lease {lease.id} was not verified for publication",
                lease=lease,
            )
        return self._resume_promotion_publish(
            lease, github=github, source_pr=source_pr, target_branch=target_branch
        )

    def _resume_promotion_publish(
        self,
        lease: Lease,
        *,
        github: GhClient,
        source_pr: int,
        target_branch: str,
    ) -> CommandResult:
        try:
            if self._registered_worktree(lease) is None:
                return self._block_promotion_lease(
                    lease,
                    "orphaned_lease",
                    f"lease {lease.id} is not registered as a Git worktree",
                )
            if self.git.status_porcelain(lease.worktree_path):
                return self._block_promotion_lease(
                    lease,
                    "dirty_lease",
                    f"lease {lease.id} has uncommitted changes",
                )
            head_sha = self.git.head_sha(lease.worktree_path)
            if head_sha != lease.head_sha:
                return self._block_promotion_lease(
                    lease,
                    "promotion_head_mismatch",
                    "promotion worktree changed after verification",
                )
            target_pull_request = github.find_open_pr(
                head=lease.branch, base=target_branch
            )
            if target_pull_request is None:
                self.git.push_branch(lease.worktree_path, lease.branch)
                target_pull_request = github.find_open_pr(
                    head=lease.branch, base=target_branch
                )
            if target_pull_request is None:
                target_pull_request = github.create_pr(
                    base=target_branch,
                    head=lease.branch,
                    title=f"Promote PR #{source_pr} to {target_branch}",
                    body=self.git.commit_message(lease.worktree_path),
                )
            if (
                target_pull_request.state != "OPEN"
                or target_pull_request.base_ref != target_branch
                or target_pull_request.head_ref != lease.branch
            ):
                return self._block_promotion_lease(
                    lease,
                    "target_pr_mismatch",
                    "GitHub did not return the exact open promotion pull request",
                )
            if target_pull_request.head_sha != head_sha:
                return self._block_promotion_lease(
                    lease,
                    "target_pr_head_mismatch",
                    "GitHub target pull request head does not match the verified promotion",
                )
            lease = self.registry.transition(
                lease.id,
                LeaseState.PR_OPEN,
                expected_version=lease.version,
                event_type="promotion_pr_reconciled",
                summary=f"promotion PR #{target_pull_request.number} reconciled",
                observed_head_sha=head_sha,
                pr_number=target_pull_request.number,
                head_sha=head_sha,
            )
        except (
            ExternalServiceError,
            GitError,
            OSError,
            RuntimeError,
            sqlite3.Error,
        ) as error:
            return self._promotion_blocked(
                "promotion_publish_failed", str(error), lease=lease
            )
        return CommandResult.ok("wt.promote", decision="ready", lease=lease)
    @staticmethod
    def _promotion_trailers(
        *, source: PullRequest, target_sha: str, lease: Lease
    ) -> tuple[str, ...]:
        return (
            f"AWF-Source-PR: {source.number}",
            f"AWF-Source-Base: {source.base_sha}",
            f"AWF-Source-Head: {source.head_sha}",
            f"AWF-Target-Base: {target_sha}",
            f"AWF-Lease-ID: {lease.id}",
        )

    def _promotion_message(
        self,
        *,
        source: PullRequest,
        target_sha: str,
        lease: Lease,
        target_branch: str,
    ) -> str:
        return "\n".join(
            (
                f"Promote PR #{source.number} to {target_branch}",
                "",
                *self._promotion_trailers(
                    source=source, target_sha=target_sha, lease=lease
                ),
            )
        )

    def _promotion_body(
        self, *, source: PullRequest, target_sha: str, lease: Lease
    ) -> str:
        return "\n".join(
            self._promotion_trailers(
                source=source, target_sha=target_sha, lease=lease
            )
        )

    def _verify_promotion(self, worktree_path: Path) -> tuple[dict[str, object], ...]:
        actions: list[dict[str, object]] = []
        for command in self.config.verify_production:
            completed = self.command_runner(
                list(command),
                cwd=worktree_path,
                check=False,
                shell=False,
                capture_output=True,
                text=True,
            )
            stderr = self._bounded_promotion_stderr(completed.stderr)
            actions.append(
                {
                    "kind": "verify_production",
                    "argv": list(command),
                    "exit_code": completed.returncode,
                    "stderr": stderr,
                }
            )
            if completed.returncode != 0:
                detail = f": {stderr}" if stderr else ""
                raise RuntimeError(
                    f"production verification failed with exit {completed.returncode}{detail}"
                )
        return tuple(actions)

    @staticmethod
    def _bounded_promotion_stderr(value: str | bytes | None) -> str:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if not value:
            return ""
        return value.strip().encode("utf-8")[:512].decode(
            "utf-8", errors="ignore"
        )

    def _block_promotion_lease(
        self, lease: Lease, code: str, message: str
    ) -> CommandResult:
        try:
            current = self.registry.get_lease(lease.id)
            if current is not None and current.state is not LeaseState.BLOCKED:
                try:
                    head_sha = self.git.head_sha(current.worktree_path)
                except GitError:
                    head_sha = current.head_sha
                lease = self.registry.transition(
                    current.id,
                    LeaseState.BLOCKED,
                    expected_version=current.version,
                    event_type="promotion_blocked",
                    summary=f"{code}: {message}",
                    head_sha=head_sha,
                )
            elif current is not None:
                lease = current
        except (OSError, RuntimeError, sqlite3.Error):
            pass
        return self._promotion_blocked(code, message, lease=lease)

    @staticmethod
    def _promotion_blocked(
        code: str, message: str, *, lease: Lease | None = None
    ) -> CommandResult:
        return CommandResult.blocked(
            "wt.promote",
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
