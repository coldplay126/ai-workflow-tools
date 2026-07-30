from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import signal
import selectors
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from datetime import datetime, timedelta, timezone

from .config import ConfigError, WorktreeConfig, load_worktree_config
from .git import GitClient, GitError, GitWorktree
from .github import ExternalServiceError, GhClient, PullRequest
from .locking import repository_lock
from .models import (
    CleanupReservation,
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
    candidate = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".cache/awf/worktrees"
    )
    return Path(os.path.abspath(str(candidate)))


_MAX_IMPORT_COLLISION_ACTIONS = 32
_DEPLOYMENT_STATUS_TIMEOUT_SECONDS = 30.0

_PRODUCTION_VERIFY_TIMEOUT_SECONDS = 300.0

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
        self.cache_dir = Path(
            os.path.abspath(str((cache_dir or cache_root()).expanduser()))
        )
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
        invalid_oid = self._invalid_promotion_oid(source)
        if invalid_oid is not None:
            return self._promotion_blocked(
                "source_pr_invalid_oid",
                f"source pull request has an invalid {invalid_oid}",
            )
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
                    source=source,
                    target_ref=target_ref,
                    expected_branch=expected_branch,
                    github=github,
                    target_branch=target_branch,
                )
            try:
                source_base_sha = self.git.fetch_ref(source.base_sha)
                source_head_sha = self.git.fetch_ref(source.head_sha)
                target_sha = self.git.fetch_ref(target_branch)
                merge_base = self.git.merge_base(source_base_sha, source_head_sha)
                patch = self.git.binary_diff(merge_base, source_head_sha)
                source_paths = self.git.changed_paths(
                    self.git.repository_root(),
                    merge_base,
                    source_head_sha,
                    find_renames=True,
                )
            except GitError as error:
                return self._promotion_blocked("source_delta_unavailable", str(error))
            if source_base_sha != source.base_sha or source_head_sha != source.head_sha:
                return self._promotion_blocked(
                    "source_sha_mismatch",
                    "fetched source pull request refs do not match the reviewed SHAs",
                )
            expected_paths = tuple(sorted(source.changed_paths))
            if not patch:
                return self._promotion_blocked(
                    "source_pr_empty_delta",
                    "source pull request has no changes after its merge base",
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
                self.git.apply_indexed_patch(lease.worktree_path, patch)
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
                    lease.worktree_path,
                    target_sha,
                    promotion_head,
                    find_renames=True,
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

    def finish(self, *, pr_number: int, apply: bool = False) -> CommandResult:
        if (
            not isinstance(pr_number, int)
            or isinstance(pr_number, bool)
            or pr_number <= 0
        ):
            return self._cleanup_blocked(
                "wt.finish",
                "invalid_pr",
                "pull request must be a positive integer",
            )
        leases = self.registry.list_leases_read_only(
            include_removed=False,
            repository_id=self.git.repository_id(),
            target_pr=pr_number,
        )
        if not leases:
            return self._cleanup_blocked(
                "wt.finish",
                "lease_not_found",
                f"no active lease is registered for pull request #{pr_number}",
            )
        if len(leases) != 1:
            return self._cleanup_blocked(
                "wt.finish",
                "ambiguous_lease",
                f"multiple active leases are registered for pull request #{pr_number}",
            )
        return self._finish_lease(leases[0], apply=apply)

    def gc(
        self, *, merged: bool, older_than: str, apply: bool = False
    ) -> CommandResult:
        if not merged:
            return self._cleanup_blocked(
                "wt.gc",
                "merged_filter_required",
                "gc requires --merged to limit cleanup to merged pull requests",
            )
        try:
            threshold = self._parse_age_threshold(older_than)
        except ValueError as error:
            return self._cleanup_blocked(
                "wt.gc", "invalid_age_threshold", str(error)
            )
        now = datetime.now(timezone.utc)
        warnings: list[dict[str, str]] = []
        actions: list[dict[str, object]] = []
        processed: list[Lease] = []
        deferred_blockers: list[dict[str, str]] = []
        saw_preview = False
        saw_removed = False
        leases = self.registry.list_leases_read_only(
            include_removed=False,
            repository_id=self.git.repository_id(),
        )
        for lease in leases:
            if not self._lease_is_older_than(lease, threshold, now):
                continue
            if not lease.managed or lease.owner_kind != "awf":
                warnings.append(
                    {
                        "code": "unmanaged_lease",
                        "message": f"Lease {lease.id} is not an AWF-managed lease.",
                    }
                )
                continue
            if lease.target_pr is None:
                warnings.append(
                    {
                        "code": "lease_without_pr",
                        "message": f"Lease {lease.id} has no pull request to prove merged.",
                    }
                )
                continue
            try:
                pull_request = (self.github or GhClient(lease.repository_root)).view_pr(
                    lease.target_pr
                )
            except (ExternalServiceError, KeyError, OSError, ValueError) as error:
                warnings.append(
                    {
                        "code": "github_refresh_failed",
                        "message": (
                            f"Unable to refresh pull request state for lease {lease.id}: "
                            f"{error}"
                        ),
                    }
                )
                continue
            if (
                pull_request.number != lease.target_pr
                or pull_request.state != "MERGED"
                or not pull_request.merge_commit_sha
            ):
                continue
            result = self._finish_lease(
                lease,
                apply=apply,
                pull_request=pull_request,
            )
            if result.lease is not None:
                processed.append(result.lease)
            actions.extend(result.actions)
            warnings.extend(result.warnings)
            if result.decision == "removed":
                saw_removed = True
            elif result.decision == "preview":
                saw_preview = True
            else:
                deferred_blockers.extend(
                    {
                        "code": blocker["code"],
                        "message": f"Lease {lease.id}: {blocker['message']}",
                    }
                    for blocker in result.blockers
                )

        if apply and saw_removed:
            return CommandResult.ok(
                "wt.gc",
                decision="removed",
                leases=tuple(processed),
                actions=tuple(actions),
                warnings=tuple(warnings + deferred_blockers),
            )
        if not apply and saw_preview:
            return CommandResult.ok(
                "wt.gc",
                decision="preview",
                leases=tuple(processed),
                actions=tuple(actions),
                warnings=tuple(warnings + deferred_blockers),
            )
        if deferred_blockers:
            return CommandResult.blocked(
                "wt.gc",
                blockers=tuple(deferred_blockers),
                leases=tuple(processed),
                actions=tuple(actions),
                warnings=tuple(warnings),
            )
        return CommandResult.ok(
            "wt.gc",
            decision="preview" if not apply else "no_op",
            leases=tuple(processed),
            actions=tuple(actions),
            warnings=tuple(warnings),
        )

    def _finish_lease(
        self,
        lease: Lease,
        *,
        apply: bool,
        pull_request: PullRequest | None = None,
    ) -> CommandResult:
        reservation = self.registry.get_cleanup_reservation(lease.id)
        if reservation is not None and not apply:
            return self._cleanup_blocked(
                "wt.finish",
                "cleanup_reserved",
                f"Lease {lease.id} is already reserved for cleanup.",
                lease=lease,
            )
        if not apply:
            try:
                pull_request = pull_request or (
                    self.github or GhClient(lease.repository_root)
                ).view_pr(lease.target_pr)
            except (ExternalServiceError, KeyError, OSError, ValueError) as error:
                return self._cleanup_blocked(
                    "wt.finish",
                    "github_refresh_failed",
                    f"Unable to refresh pull request state: {error}",
                    lease=lease,
                )
            if pull_request is None:
                return self._cleanup_blocked(
                    "wt.finish",
                    "lease_without_pr",
                    f"Lease {lease.id} has no pull request to prove merged.",
                    lease=lease,
                )
            blockers = self._cleanup_blockers(lease, pull_request)
            if blockers:
                return CommandResult.blocked(
                    "wt.finish", blockers=blockers, lease=lease
                )
            return CommandResult.ok(
                "wt.finish",
                decision="preview",
                lease=lease,
                actions=(self._cleanup_action("remove_worktree", lease),),
            )

        repository_id = self.git.repository_id()
        warnings: list[dict[str, str]] = []
        with repository_lock(self.lock_dir / f"{repository_id}.lock"):
            current = self.registry.get_lease(lease.id)
            if (
                current is None
                or current.state is LeaseState.REMOVED
                or current.repository_id != repository_id
                or current.target_pr != lease.target_pr
            ):
                return self._cleanup_blocked(
                    "wt.finish",
                    "lease_changed",
                    f"Lease {lease.id} changed before cleanup could be applied.",
                    lease=current,
                )
            reservation = self.registry.get_cleanup_reservation(current.id)
            if reservation is not None:
                return self._recover_cleanup_reservation(
                    current, reservation, warnings
                )
            self._refresh_lease(current, warnings)
            current = self.registry.get_lease(current.id)
            if current is None or current.state is LeaseState.REMOVED:
                return self._cleanup_blocked(
                    "wt.finish",
                    "lease_changed",
                    f"Lease {lease.id} changed while refresh was running.",
                    lease=current,
                    warnings=warnings,
                )
            try:
                pull_request = (self.github or GhClient(current.repository_root)).view_pr(
                    current.target_pr
                )
            except (ExternalServiceError, KeyError, OSError, ValueError) as error:
                return self._cleanup_blocked(
                    "wt.finish",
                    "github_refresh_failed",
                    f"Unable to refresh pull request state: {error}",
                    lease=current,
                    warnings=warnings,
                )
            blockers = self._cleanup_blockers(current, pull_request)
            if blockers:
                return CommandResult.blocked(
                    "wt.finish",
                    blockers=blockers,
                    lease=current,
                    warnings=tuple(warnings),
                )
            forced = self._force_cleanup_deployment_probe(
                current, pull_request, warnings
            )
            if forced is None:
                recorded = self.registry.get_lease(current.id) or current
                return self._cleanup_blocked(
                    "wt.finish",
                    "deployment_not_healthy",
                    f"Promotion lease {current.id} has no freshly proven deployment.",
                    lease=recorded,
                    warnings=warnings,
                )
            current = forced
            try:
                pull_request = (self.github or GhClient(current.repository_root)).view_pr(
                    current.target_pr
                )
            except (ExternalServiceError, KeyError, OSError, ValueError) as error:
                return self._cleanup_blocked(
                    "wt.finish",
                    "github_refresh_failed",
                    f"Unable to revalidate pull request state: {error}",
                    lease=current,
                    warnings=warnings,
                )
            blockers = self._cleanup_blockers(current, pull_request)
            if blockers:
                return CommandResult.blocked(
                    "wt.finish",
                    blockers=blockers,
                    lease=current,
                    warnings=tuple(warnings),
                )
            try:
                branch_sha = self.git.resolve_ref(current.branch)
            except GitError as error:
                return self._cleanup_blocked(
                    "wt.finish",
                    "branch_unavailable",
                    f"Unable to validate cleanup branch {current.branch!r}: {error}",
                    lease=current,
                    warnings=warnings,
                )
            if branch_sha != current.head_sha:
                return self._cleanup_blocked(
                    "wt.finish",
                    "branch_head_mismatch",
                    f"Branch {current.branch!r} changed before cleanup.",
                    lease=current,
                    warnings=warnings,
                )
            try:
                reservation = self.registry.reserve_cleanup(
                    current.id,
                    expected_version=current.version,
                    branch_sha=branch_sha,
                )
            except (RuntimeError, sqlite3.Error) as error:
                return self._cleanup_blocked(
                    "wt.finish",
                    "registry_conflict",
                    f"Unable to reserve lease {current.id} for cleanup: {error}",
                    lease=current,
                    warnings=warnings,
                )
            post_lock_blockers: tuple[dict[str, str], ...] = ()
            post_lock_code: str | None = None
            post_lock_message = ""
            removal_error: GitError | OSError | None = None
            try:
                with self.git.hold_worktree_branch_if_at(
                    current.worktree_path, current.branch, reservation.branch_sha
                ):
                    reserved_current = self.registry.get_lease(current.id)
                    if (
                        reserved_current is None
                        or reserved_current.version != reservation.reserved_version
                        or self.registry.get_cleanup_reservation(current.id)
                        != reservation
                    ):
                        post_lock_code = "lease_changed"
                        post_lock_message = (
                            f"Lease {current.id} changed while cleanup was reserved."
                        )
                    else:
                        try:
                            pull_request = (
                                self.github or GhClient(reserved_current.repository_root)
                            ).view_pr(reserved_current.target_pr)
                        except (ExternalServiceError, KeyError, OSError, ValueError) as error:
                            post_lock_code = "github_refresh_failed"
                            post_lock_message = (
                                f"Unable to revalidate pull request state: {error}"
                            )
                        else:
                            post_lock_blockers = self._cleanup_blockers(
                                reserved_current, pull_request
                            )
                            if not post_lock_blockers:
                                try:
                                    self.git.remove_worktree(
                                        reserved_current.worktree_path
                                    )
                                except (GitError, OSError) as error:
                                    removal_error = error
            except GitError as error:
                return self._release_cleanup_reservation(
                    current,
                    reservation,
                    warnings,
                    code="branch_head_mismatch",
                    message=(
                        f"Branch {current.branch!r} changed before cleanup lock: {error}"
                    ),
                )
            if post_lock_blockers:
                return self._release_cleanup_reservation(
                    current,
                    reservation,
                    warnings,
                    blockers=post_lock_blockers,
                )
            if post_lock_code is not None:
                return self._release_cleanup_reservation(
                    current,
                    reservation,
                    warnings,
                    code=post_lock_code,
                    message=post_lock_message,
                )
            if removal_error is not None:
                return self._recover_cleanup_reservation(
                    current,
                    reservation,
                    warnings,
                    removal_error=removal_error,
                )
            try:
                removed = self.registry.complete_cleanup(
                    current.id, expected_version=reservation.reserved_version
                )
            except (RuntimeError, sqlite3.Error) as error:
                return self._cleanup_blocked(
                    "wt.finish",
                    "registry_conflict",
                    (
                        "Worktree was removed but the cleanup reservation could not be "
                        f"completed: {error}"
                    ),
                    lease=current,
                    warnings=warnings,
                )
            actions: list[dict[str, object]] = [
                self._cleanup_action("remove_worktree", removed)
            ]
            self._cleanup_branches(removed, reservation.branch_sha, actions, warnings)
            return CommandResult.ok(
                "wt.finish",
                decision="removed",
                lease=removed,
                actions=tuple(actions),
                warnings=tuple(warnings),
            )

    def _release_cleanup_reservation(
        self,
        lease: Lease,
        reservation: CleanupReservation,
        warnings: list[dict[str, str]],
        *,
        code: str | None = None,
        message: str | None = None,
        blockers: tuple[dict[str, str], ...] = (),
    ) -> CommandResult:
        try:
            released = self.registry.release_cleanup_reservation(
                lease.id, expected_version=reservation.reserved_version
            )
        except (RuntimeError, sqlite3.Error) as error:
            return self._cleanup_blocked(
                "wt.finish",
                "cleanup_reserved",
                f"Lease {lease.id} remains reserved for cleanup: {error}",
                lease=lease,
                warnings=warnings,
            )
        if blockers:
            return CommandResult.blocked(
                "wt.finish",
                blockers=blockers,
                lease=released,
                warnings=tuple(warnings),
            )
        return self._cleanup_blocked(
            "wt.finish",
            code or "cleanup_reserved",
            message or f"Lease {lease.id} cleanup reservation was released.",
            lease=released,
            warnings=warnings,
        )

    def _force_cleanup_deployment_probe(
        self,
        lease: Lease,
        pull_request: PullRequest,
        warnings: list[dict[str, str]],
    ) -> Lease | None:
        if lease.purpose is not Purpose.PROMOTE:
            return lease
        command = self.config.deployment_status_command

        if not command:
            return self._record_cleanup_deployment_probe(
                lease,
                pull_request,
                state=LeaseState.BLOCKED,
                deployment_state=DeploymentState.UNKNOWN,
                summary="Fresh deployment status probe is not configured",
                warnings=warnings,
            )
        try:
            completed = self.deployment_runner(
                list(command),
                cwd=lease.repository_root,
                check=False,
                shell=False,
                capture_output=True,
                text=True,
                timeout=_DEPLOYMENT_STATUS_TIMEOUT_SECONDS,
            )
        except Exception:
            warnings.append(
                {
                    "code": "deployment_probe_failed",
                    "message": (
                        f"Unable to run a fresh deployment status probe for lease {lease.id}."
                    ),
                }
            )
            return self._record_cleanup_deployment_probe(
                lease,
                pull_request,
                state=LeaseState.BLOCKED,
                deployment_state=DeploymentState.UNKNOWN,
                summary="Fresh deployment status probe could not be completed",
                warnings=warnings,
            )
        if completed.returncode != 0:
            return self._record_cleanup_deployment_probe(
                lease,
                pull_request,
                state=LeaseState.BLOCKED,
                deployment_state=DeploymentState.FAILED,
                summary="Fresh deployment status probe reported failure",
                warnings=warnings,
            )
        return self._record_cleanup_deployment_probe(
            lease,
            pull_request,
            state=LeaseState.CLEANABLE,
            deployment_state=DeploymentState.HEALTHY,
            summary="Fresh deployment status probe is healthy",
            warnings=warnings,
        )

    def _record_cleanup_deployment_probe(
        self,
        lease: Lease,
        pull_request: PullRequest,
        *,
        state: LeaseState,
        deployment_state: DeploymentState,
        summary: str,
        warnings: list[dict[str, str]],
    ) -> Lease | None:
        try:
            updated = self.registry.transition(
                lease.id,
                state,
                expected_version=lease.version,
                event_type="cleanup_deployment_probe",
                summary=summary,
                observed_head_sha=pull_request.head_sha,
                pr_number=pull_request.number,
                deployment_state=deployment_state,
            )
        except (RuntimeError, sqlite3.Error):
            warnings.append(
                {
                    "code": "deployment_probe_record_failed",
                    "message": (
                        f"Unable to record a fresh deployment status probe for lease {lease.id}."
                    ),
                }
            )
            return None
        current = self.registry.get_lease(lease.id)
        if (
            current is None
            or current.version != updated.version
            or current.target_pr != pull_request.number
            or current.state is not state
            or current.deployment_state is not deployment_state
        ):
            warnings.append(
                {
                    "code": "deployment_probe_record_failed",
                    "message": (
                        f"Unable to revalidate a fresh deployment status probe for lease "
                        f"{lease.id}."
                    ),
                }
            )
            return None
        return current

    def _recover_cleanup_reservation(
        self,
        lease: Lease,
        reservation: CleanupReservation,
        warnings: list[dict[str, str]],
        *,
        removal_error: Exception | None = None,
    ) -> CommandResult:
        path_blocker = self._cleanup_path_blocker(lease)
        if path_blocker is not None:
            return CommandResult.blocked(
                "wt.finish",
                blockers=(path_blocker,),
                lease=lease,
                warnings=tuple(warnings),
            )
        try:
            worktrees = self.git.list_worktrees()
        except (GitError, OSError) as error:
            return self._cleanup_blocked(
                "wt.finish",
                "worktree_inspection_failed",
                f"Unable to inspect reserved worktree: {error}",
                lease=lease,
                warnings=warnings,
            )
        path_is_absent = (
            not lease.worktree_path.exists()
            and all(worktree.path != lease.worktree_path for worktree in worktrees)
        )
        if path_is_absent:
            try:
                removed = self.registry.complete_cleanup(
                    lease.id, expected_version=reservation.reserved_version
                )
            except (RuntimeError, sqlite3.Error) as error:
                return self._cleanup_blocked(
                    "wt.finish",
                    "registry_conflict",
                    f"Unable to recover reserved cleanup for lease {lease.id}: {error}",
                    lease=lease,
                    warnings=warnings,
                )
            actions: list[dict[str, object]] = [
                self._cleanup_action("remove_worktree", removed)
            ]
            self._cleanup_branches(removed, reservation.branch_sha, actions, warnings)
            return CommandResult.ok(
                "wt.finish",
                decision="removed",
                lease=removed,
                actions=tuple(actions),
                warnings=tuple(warnings),
            )
        try:
            self.registry.release_cleanup_reservation(
                lease.id, expected_version=reservation.reserved_version
            )
        except (RuntimeError, sqlite3.Error) as error:
            return self._cleanup_blocked(
                "wt.finish",
                "cleanup_reserved",
                f"Lease {lease.id} remains reserved for cleanup: {error}",
                lease=lease,
                warnings=warnings,
            )
        if removal_error is not None:
            return self._cleanup_blocked(
                "wt.finish",
                "worktree_remove_failed",
                f"Unable to remove worktree for lease {lease.id}: {removal_error}",
                lease=self.registry.get_lease(lease.id),
                warnings=warnings,
            )
        return self._cleanup_blocked(
            "wt.finish",
            "cleanup_reserved",
            f"Lease {lease.id} cleanup reservation was released because its path remains.",
            lease=self.registry.get_lease(lease.id),
            warnings=warnings,
        )

    def _cleanup_blockers(
        self, lease: Lease, pull_request: PullRequest
    ) -> tuple[dict[str, str], ...]:
        blockers: list[dict[str, str]] = []
        if not lease.managed or lease.owner_kind != "awf":
            blockers.append(
                {
                    "code": "unmanaged_lease",
                    "message": f"Lease {lease.id} is not managed by AWF.",
                }
            )
        if lease.retain:
            blockers.append(
                {
                    "code": "retained_lease",
                    "message": f"Lease {lease.id} is retained and cannot be removed.",
                }
            )
        if pull_request.number != lease.target_pr:
            blockers.append(
                {
                    "code": "pr_mismatch",
                    "message": f"Lease {lease.id} does not match the refreshed pull request.",
                }
            )
        if pull_request.state != "MERGED" or not pull_request.merge_commit_sha:
            blockers.append(
                {
                    "code": "pr_not_merged",
                    "message": f"Pull request #{lease.target_pr} is not merged.",
                }
            )
        if pull_request.head_sha != lease.head_sha:
            blockers.append(
                {
                    "code": "head_mismatch",
                    "message": (
                        f"Pull request #{pull_request.number} no longer matches the "
                        f"recorded head for lease {lease.id}."
                    ),
                }
            )
        if lease.purpose is Purpose.PROMOTE and (
            lease.deployment_state is not DeploymentState.HEALTHY
        ):
            blockers.append(
                {
                    "code": "deployment_not_healthy",
                    "message": f"Promotion lease {lease.id} has no healthy deployment.",
                }
            )
        path_blocker = self._cleanup_path_blocker(lease)
        if path_blocker is not None:
            blockers.append(path_blocker)
            return tuple(blockers)

        try:
            worktrees = self.git.list_worktrees()
        except (GitError, OSError) as error:
            blockers.append(
                {
                    "code": "worktree_inspection_failed",
                    "message": f"Unable to inspect registered worktrees: {error}",
                }
            )
            return tuple(blockers)
        lease_path = lease.worktree_path
        registered = tuple(
            worktree for worktree in worktrees if worktree.path == lease_path
        )
        if len(registered) != 1 or not lease_path.is_dir():
            blockers.append(
                {
                    "code": "unregistered_worktree",
                    "message": f"Lease {lease.id} is not registered at its expected path.",
                }
            )
        else:
            worktree = registered[0]
            if (
                worktree.branch != lease.branch
                or worktree.bare
                or worktree.detached
            ):
                blockers.append(
                    {
                        "code": "branch_mismatch",
                        "message": (
                            f"Lease {lease.id} is not checked out on its registered branch."
                        ),
                    }
                )
            if any(
                item.path != lease_path and item.branch == lease.branch
                for item in worktrees
            ):
                blockers.append(
                    {
                        "code": "branch_in_use",
                        "message": f"Branch {lease.branch!r} is checked out elsewhere.",
                    }
                )
            protected = self._cleanup_protected_branches(
                worktrees, lease.repository_root, pull_request
            )
            if lease.branch in protected:
                blockers.append(
                    {
                        "code": "protected_branch",
                        "message": f"Branch {lease.branch!r} is protected from cleanup.",
                    }
                )
            try:
                if self.git.status_porcelain(lease_path):
                    blockers.append(
                        {
                            "code": "dirty_worktree",
                            "message": f"Lease {lease.id} has uncommitted changes.",
                        }
                    )
            except (GitError, OSError) as error:
                blockers.append(
                    {
                        "code": "worktree_inspection_failed",
                        "message": f"Unable to inspect lease status: {error}",
                    }
                )
            try:
                actual_head = self.git.head_sha(lease_path)
            except (GitError, OSError) as error:
                blockers.append(
                    {
                        "code": "head_unavailable",
                        "message": f"Unable to inspect lease HEAD: {error}",
                    }
                )
            else:
                if (
                    actual_head != lease.head_sha
                    or actual_head != pull_request.head_sha
                ):
                    blockers.append(
                        {
                            "code": "head_mismatch",
                            "message": (
                                f"Lease {lease.id} HEAD does not match the recorded "
                                f"pull request head for #{pull_request.number}."
                            ),
                        }
                    )
        return tuple(blockers)

    def _cleanup_path_blocker(self, lease: Lease) -> dict[str, str] | None:
        expected = self.cache_dir / lease.repository_name / lease.id
        if lease.worktree_path != expected:
            return {
                "code": "unsafe_worktree_path",
                "message": f"Lease {lease.id} does not use its managed cache path.",
            }
        current = Path(expected.anchor)
        for part in expected.parts[1:]:
            current /= part
            try:
                current.lstat()
            except FileNotFoundError:
                break
            except OSError:
                return {
                    "code": "unsafe_worktree_path",
                    "message": f"Lease {lease.id} cache path could not be inspected.",
                }
            if current.is_symlink():
                return {
                    "code": "unsafe_worktree_path",
                    "message": (
                        f"Lease {lease.id} has a symlinked managed worktree path."
                    ),
                }
        return None

    def _cleanup_branches(
        self,
        lease: Lease,
        expected_sha: str,
        actions: list[dict[str, object]],
        warnings: list[dict[str, str]],
    ) -> None:
        if lease.owner_kind != "awf" or not lease.branch.startswith("awf/"):
            return
        try:
            self.git.delete_branch_if_at(lease.branch, expected_sha)
        except (GitError, OSError) as error:
            self._branch_cleanup_warning(
                lease,
                "local_branch_cleanup_failed",
                f"Could not delete local branch {lease.branch!r}: {error}",
                warnings,
            )
        else:
            actions.append(self._cleanup_action("delete_local_branch", lease))
        try:
            self.git.delete_remote_branch_if_at(lease.branch, expected_sha)
        except (GitError, OSError) as error:
            self._branch_cleanup_warning(
                lease,
                "remote_branch_cleanup_failed",
                f"Could not delete remote branch {lease.branch!r}: {error}",
                warnings,
            )
        else:
            actions.append(self._cleanup_action("delete_remote_branch", lease))

    def _branch_cleanup_warning(
        self,
        lease: Lease,
        code: str,
        message: str,
        warnings: list[dict[str, str]],
    ) -> None:
        warnings.append({"code": code, "message": message})
        try:
            self.registry.record_cleanup_event(
                lease.id, event_type=code, summary=message
            )
        except (RuntimeError, sqlite3.Error):
            warnings.append(
                {
                    "code": "cleanup_warning_record_failed",
                    "message": f"Unable to record cleanup warning for lease {lease.id}.",
                }
            )

    @staticmethod
    def _cleanup_action(kind: str, lease: Lease) -> dict[str, object]:
        return {
            "kind": kind,
            "lease_id": lease.id,
            "path": str(lease.worktree_path),
            "branch": lease.branch,
        }

    @staticmethod
    def _parse_age_threshold(value: str) -> timedelta:
        if not isinstance(value, str):
            raise ValueError(
                "older_than must be a positive duration using s, m, h, or d"
            )
        match = re.fullmatch(r"([1-9][0-9]*)([smhd])", value)
        if match is None:
            raise ValueError(
                "older_than must be a positive duration using s, m, h, or d"
            )
        amount = int(match.group(1))
        multiplier = {
            "s": 1,
            "m": 60,
            "h": 60 * 60,
            "d": 24 * 60 * 60,
        }[match.group(2)]
        try:
            return timedelta(seconds=amount * multiplier)
        except OverflowError as error:
            raise ValueError(
                "older_than must be a positive duration using s, m, h, or d"
            ) from error

    @staticmethod
    def _lease_is_older_than(
        lease: Lease, threshold: timedelta, now: datetime
    ) -> bool:
        try:
            timestamp = datetime.fromisoformat(lease.last_used_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if timestamp.tzinfo is None:
            return False
        return now - timestamp.astimezone(timezone.utc) >= threshold

    def _cleanup_protected_branches(
        self,
        worktrees: tuple[GitWorktree, ...],
        repository_root: Path,
        pull_request: PullRequest,
    ) -> set[str]:
        protected = {
            self._local_branch_name(pull_request.base_ref),
            self._local_branch_name(self.config.default_base),
            self._local_branch_name(self.config.production_branch),
        }
        root = repository_root.resolve()
        protected.update(
            item.branch for item in worktrees if item.path == root and item.branch
        )
        protected.discard("")
        return protected

    @staticmethod
    def _local_branch_name(value: str | None) -> str:
        if not value:
            return ""
        for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
            if value.startswith(prefix):
                return value[len(prefix) :]
        return value

    @staticmethod
    def _cleanup_blocked(
        command: str,
        code: str,
        message: str,
        *,
        lease: Lease | None = None,
        warnings: list[dict[str, str]] | None = None,
    ) -> CommandResult:
        return CommandResult.blocked(
            command,
            blockers=({"code": code, "message": message},),
            lease=lease,
            warnings=tuple(warnings or ()),
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
        if self.registry.get_cleanup_reservation(lease_id) is not None:
            raise RuntimeError("lease cleanup is reserved")
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

    @staticmethod
    def _invalid_promotion_oid(source: PullRequest) -> str | None:
        for name, value in (
            ("base SHA", source.base_sha),
            ("head SHA", source.head_sha),
            ("merge SHA", source.merge_commit_sha),
        ):
            if value is not None and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value) is None:
                return name
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
        source: PullRequest,
        target_ref: str,
        expected_branch: str,
        github: GhClient,
        target_branch: str,
    ) -> CommandResult:
        if (
            lease.source_pr != source.number
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
        if events and events[-1].event_type == "promotion_publish_pending":
            return self._resume_promotion_publish(
                lease,
                github=github,
                source_pr=source.number,
                target_branch=target_branch,
            )
        return self._recover_unrecorded_promotion_publish(
            lease,
            github=github,
            source=source,
            target_branch=target_branch,
        )
        
    def _recover_unrecorded_promotion_publish(
        self,
        lease: Lease,
        *,
        github: GhClient,
        source: PullRequest,
        target_branch: str,
    ) -> CommandResult:
        try:
            if self._registered_worktree(lease) is None:
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} was not verified for publication",
                    lease=lease,
                )
            if self.git.status_porcelain(lease.worktree_path):
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} was not verified for publication",
                    lease=lease,
                )
            promotion_head = self.git.head_sha(lease.worktree_path)
            if promotion_head == lease.head_sha:
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} was not verified for publication",
                    lease=lease,
                )
            source_base_sha = self._promotion_source_base_from_message(
                self.git.commit_message(lease.worktree_path),
                source=source,
                target_sha=lease.head_sha,
                lease=lease,
                target_branch=target_branch,
            )
            if source_base_sha is None:
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} does not have exact promotion provenance",
                    lease=lease,
                )
            if self.git.fetch_ref(source_base_sha) != source_base_sha:
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} does not have exact source provenance",
                    lease=lease,
                )
            source_head_sha = self.git.fetch_ref(source.head_sha)
            expected_paths = tuple(sorted(source.changed_paths))
            if (
                source_head_sha != source.head_sha
                or self.git.changed_paths(
                    lease.worktree_path, lease.head_sha, promotion_head
                )
                != expected_paths
                or any(
                    self.git.path_blob(source_head_sha, path)
                    != self.git.path_blob(promotion_head, path)
                    for path in expected_paths
                )
            ):
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} does not have the exact reviewed delta",
                    lease=lease,
                )
            self._verify_promotion(lease.worktree_path)
            lease = self.registry.transition(
                lease.id,
                LeaseState.ACTIVE,
                expected_version=lease.version,
                event_type="promotion_publish_pending",
                summary="recovered verified promotion; publication pending",
                observed_head_sha=promotion_head,
                head_sha=promotion_head,
            )
        except (
            GitError,
            OSError,
            RuntimeError,
            sqlite3.Error,
            subprocess.SubprocessError,
        ) as error:
            return self._promotion_blocked(
                "promotion_recovery_failed", str(error), lease=lease
            )
        return self._resume_promotion_publish(
            lease,
            github=github,
            source_pr=source.number,
            target_branch=target_branch,
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

    @staticmethod
    def _promotion_source_base_from_message(
        message: str,
        *,
        source: PullRequest,
        target_sha: str,
        lease: Lease,
        target_branch: str,
    ) -> str | None:
        lines = message.splitlines()
        source_base_prefix = "AWF-Source-Base: "
        if (
            len(lines) != 7
            or lines[0] != f"Promote PR #{source.number} to {target_branch}"
            or lines[1] != ""
            or lines[2] != f"AWF-Source-PR: {source.number}"
            or not lines[3].startswith(source_base_prefix)
            or not lines[3][len(source_base_prefix) :]
            or lines[4] != f"AWF-Source-Head: {source.head_sha}"
            or lines[5] != f"AWF-Target-Base: {target_sha}"
            or lines[6] != f"AWF-Lease-ID: {lease.id}"
        ):
            return None
        return lines[3][len(source_base_prefix) :]

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
            try:
                process = subprocess.Popen(
                    list(command),
                    cwd=worktree_path,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                raw_stderr = self._drain_verifier_stderr(process)
            except OSError as error:
                raise RuntimeError("production verification failed to launch") from error
            stderr = self._bounded_promotion_stderr(raw_stderr)
            actions.append(
                {
                    "kind": "verify_production",
                    "argv": list(command),
                    "exit_code": process.returncode,
                    "stderr": stderr,
                }
            )
            if process.returncode != 0:
                detail = f": {stderr}" if stderr else ""
                raise RuntimeError(
                    f"production verification failed with exit {process.returncode}{detail}"
                )
        return tuple(actions)

    @staticmethod
    def _drain_verifier_stderr(process: subprocess.Popen[bytes]) -> bytes:
        if process.stderr is None:
            raise RuntimeError("production verification did not expose stderr")
        retained = bytearray()
        deadline = time.monotonic() + _PRODUCTION_VERIFY_TIMEOUT_SECONDS
        with selectors.DefaultSelector() as selector:
            selector.register(process.stderr, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    WorktreeService._terminate_verifier_process_group(process)
                    raise RuntimeError("production verification timed out")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif len(retained) < 4096:
                        retained.extend(chunk[: 4096 - len(retained)])
                if process.poll() is not None and not selector.get_map():
                    break
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            WorktreeService._terminate_verifier_process_group(process)
            raise RuntimeError("production verification timed out")
        return bytes(retained)

    @staticmethod
    def _terminate_verifier_process_group(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()

    @staticmethod
    def _bounded_promotion_stderr(value: str | bytes | None) -> str:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if not value:
            return ""
        redacted = re.sub(
            r"(?i)(?:gh[pousr]_[a-z0-9_]+|github_pat_[a-z0-9_]+|bearer\s+\S+|token\s+\S+)",
            "<redacted>",
            value,
        )
        redacted = re.sub(r"https?://[^/@\s]*@", "https://<redacted>@", redacted)
        redacted = re.sub(r"https?://[^\s]*$", "https://<redacted>", redacted)
        return redacted.strip().encode("utf-8")[:512].decode(
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
