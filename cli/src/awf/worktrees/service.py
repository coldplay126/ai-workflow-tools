from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from .config import (
    ConfigError,
    WorktreeConfig,
    load_deployment_adapter,
    load_worktree_config,
)
from .evidence import (
    DeploymentEvidenceExecutor,
    DeploymentEvidenceRequest,
    EvidenceProbeResult,
)
from .git import (
    GitClient,
    GitError,
    GitIndexBackup,
    GitPatchConflict,
    GitPathUsage,
    GitRemoteError,
    GitWorktree,
)
from .github import ExternalServiceError, GhClient, PullRequest
from .locking import repository_lock
from .models import (
    CleanupReservation,
    CommandResult,
    DeploymentState,
    Lease,
    LeaseState,
    PromotionMode,
    PromotionSource,
    Purpose,
    ReleaseBridge,
    ReleaseSource,
    ReleaseState,
    ResolutionState,
    now_iso,
    release_source_digest,
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
_PRODUCTION_VERIFY_TIMEOUT_SECONDS = 300.0
_GITHUB_ACTOR_LOGIN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}(?:\[bot\])?"
)
_GIT_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_SYNC_BRANCH = re.compile(r"awf/sync-[0-9a-f]{16}-[0-9a-f]{12}/feature")
_SYNC_UNMERGED_STATUS_CODES = frozenset(
    {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
)



@dataclass(frozen=True)
class _PromotionRecoveryPreflight:
    target_branch: str
    dirty_paths: tuple[str, ...]
    reconciliation_head: str | None = None
    legacy_manual_message: bool = False

@dataclass(frozen=True)
class _PromotionRetryIdentity:
    initiative: str
    branch: str
    worktree_path: Path


@dataclass(frozen=True)
class _WorktreeIdentity:
    device: int
    inode: int



@dataclass(frozen=True)
class _CompactCandidate:
    lease: Lease
    usages: tuple[tuple[str, GitPathUsage], ...]



def _same_github_actor(author: object, merger: object) -> bool:
    return (
        isinstance(author, str)
        and isinstance(merger, str)
        and _GITHUB_ACTOR_LOGIN.fullmatch(author) is not None
        and _GITHUB_ACTOR_LOGIN.fullmatch(merger) is not None
        and author.casefold() == merger.casefold()
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
        git: GitClient | None,
        *,
        config: WorktreeConfig | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        github: GhClient | None = None,
        evidence_executor: DeploymentEvidenceExecutor | None = None,
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
        self.cache_dir = Path(
            os.path.abspath(str((cache_dir or cache_root()).expanduser()))
        )
        self.state_dir = (state_dir or state_db_path().parent).expanduser().resolve()
        self.lock_dir = (lock_dir or self.state_dir / "locks").expanduser().resolve()
        self.git_factory = git_factory
        self.skill_source_dir = (
            skill_source_dir
            or Path(__file__).resolve().parents[1]
            / "resources"
            / "release-worktree-lifecycle"
        ).resolve()
        self.home_dir = (home_dir or Path.home()).expanduser().resolve()
        self.evidence_executor = evidence_executor or DeploymentEvidenceExecutor(
            neutral_cwd=self.home_dir
        )

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
        if not apply:
            active_leases = self.registry.list_leases_read_only(
                include_removed=False,
                repository_id=repository_id,
                initiative=slug,
                purpose=purpose,
            )
            if active_leases:
                return self._reuse(active_leases[0], expected_branch, apply=False)
        with repository_lock(self.lock_dir / f"{repository_id}.lock"):
            active = self.registry.find_active(repository_id, slug, purpose)
            if active is not None:
                return self._reuse(active, expected_branch, apply=apply)

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

    def sync(
        self,
        *,
        source_branch: str,
        target_branch: str,
        apply: bool,
    ) -> CommandResult:
        command = "wt.sync"
        try:
            source_ref, target_ref = self._sync_refs(source_branch, target_branch)
        except ConfigError as error:
            return self._sync_blocked("invalid_sync_branches", str(error))
        if not self.config.verify_production:
            return self._sync_blocked(
                "sync_verify_missing",
                "verify.production.commands must configure at least one command",
            )

        repository_id = self.git.repository_id()
        initiative = self._sync_initiative(source_branch, target_branch)
        if not apply:
            try:
                source_sha = self.git.resolve_ref(source_ref)
                target_sha = self.git.resolve_ref(target_ref)
                merge_base, expected_blobs = self._sync_expected_blobs(
                    source_sha, target_sha
                )
            except GitError as error:
                return self._sync_blocked("sync_ref_unavailable", str(error))
            if not expected_blobs:
                return self._sync_noop(
                    source_branch,
                    target_branch,
                    source_sha,
                    target_sha,
                    merge_base,
                )
            branch = self._sync_branch(initiative, source_sha)
            lease = self._new_sync_lease(
                initiative=initiative,
                branch=branch,
                source_ref=source_ref,
                target_ref=target_ref,
                source_sha=source_sha,
                target_sha=target_sha,
                merge_base=merge_base,
                reviewed_paths=tuple(expected_blobs),
            )
            try:
                active = self.registry.find_active_read_only(
                    repository_id, initiative, Purpose.FEATURE
                )
            except sqlite3.Error as error:
                return self._sync_blocked("registry_conflict", str(error))
            if active is not None:
                return self._reuse_sync(active, expected=lease)
            return CommandResult.ok(
                command,
                decision="preview",
                actions=(
                    {
                        "kind": "create_worktree",
                        "path": str(lease.worktree_path),
                        "branch": lease.branch,
                        "source_branch": source_branch,
                        "source_base_sha": merge_base,
                        "source_head_sha": source_sha,
                        "target_branch": target_branch,
                        "target_base_sha": target_sha,
                        "changed_paths": list(lease.reviewed_paths),
                    },
                    {
                        "kind": "apply_source_delta",
                        "from": source_branch,
                        "to": target_branch,
                        "paths": list(lease.reviewed_paths),
                    },
                    *(
                        {
                            "kind": "verify_production",
                            "argv": list(verification_command),
                        }
                        for verification_command in self.config.verify_production
                    ),
                    {
                        "kind": "open_pull_request",
                        "base": target_branch,
                        "head": lease.branch,
                    },
                ),
            )

        with repository_lock(self.lock_dir / f"{repository_id}.lock"):
            try:
                source_sha = self.git.fetch_ref(source_branch)
                target_sha = self.git.fetch_ref(target_branch)
                merge_base, expected_blobs = self._sync_expected_blobs(
                    source_sha, target_sha
                )
            except GitRemoteError as error:
                return self._external_error(
                    command, "sync_ref_unavailable", str(error)
                )
            except GitError as error:
                return self._sync_blocked("sync_ref_unavailable", str(error))
            if not expected_blobs:
                return self._sync_noop(
                    source_branch,
                    target_branch,
                    source_sha,
                    target_sha,
                    merge_base,
                )

            branch = self._sync_branch(initiative, source_sha)
            lease = self._new_sync_lease(
                initiative=initiative,
                branch=branch,
                source_ref=source_ref,
                target_ref=target_ref,
                source_sha=source_sha,
                target_sha=target_sha,
                merge_base=merge_base,
                reviewed_paths=tuple(expected_blobs),
            )
            github = self.github or GhClient(self.git.repository_root())
            active = self.registry.find_active(
                repository_id, initiative, Purpose.FEATURE
            )
            if active is not None:
                return self._resume_sync_publication(
                    active,
                    expected=lease,
                    github=github,
                    source_branch=source_branch,
                    target_branch=target_branch,
                )
            branch_conflict = self._branch_conflict(branch)
            if branch_conflict is not None:
                return self._sync_blocked(
                    "branch_conflict",
                    f"branch {branch!r} is already checked out at {branch_conflict}",
                    lease=lease,
                )

            sync_head_prefix = f"awf/{initiative}-"
            try:
                existing_pull_requests = github.find_open_prs_by_prefix(
                    base=target_branch,
                    head_prefix=sync_head_prefix,
                )
                remote_branch_sha = self.git.remote_branch_sha(branch)
            except (ExternalServiceError, GitRemoteError) as error:
                return self._external_error(
                    command, "sync_publish_failed", str(error), lease=lease
                )
            if existing_pull_requests:
                pull_request_numbers = ", ".join(
                    f"#{pull_request.number}"
                    for pull_request in existing_pull_requests
                )
                return self._sync_blocked(
                    "sync_pr_open",
                    (
                        f"sync pull request {pull_request_numbers} is already open "
                        f"for {source_branch!r} to {target_branch!r}"
                    ),
                    lease=lease,
                )
            if remote_branch_sha is not None:
                return self._sync_blocked(
                    "sync_branch_exists",
                    (
                        f"remote branch {branch!r} already exists without an "
                        "active managed synchronization lease"
                    ),
                    lease=lease,
                )

            try:
                self.git.add_worktree(
                    lease.worktree_path,
                    lease.branch,
                    target_sha,
                    reuse_exact_branch=True,
                )
            except GitError as error:
                return self._sync_blocked(
                    "worktree_conflict", str(error), lease=lease
                )
            try:
                lease = replace(
                    lease, head_sha=self.git.head_sha(lease.worktree_path)
                )
                lease = self.registry.create_lease(lease)
            except (GitError, OSError, RuntimeError, ValueError, sqlite3.Error) as error:
                result = self._handle_creation_failure(lease, error, target_sha)
                return replace(result, command=command)

            try:
                patch = self.git.binary_diff(
                    merge_base,
                    source_sha,
                    paths=lease.reviewed_paths,
                )
                if not patch:
                    return self._block_sync_lease(
                        lease,
                        "sync_delta_mismatch",
                        "source-only paths produced an empty synchronization delta",
                    )
                self.git.apply_indexed_patch(lease.worktree_path, patch)
                if (
                    self.git.index_tree_sha(lease.worktree_path)
                    == self.git.commit_tree_sha(target_sha, lease.worktree_path)
                ):
                    try:
                        lease = self.registry.transition(
                            lease.id,
                            LeaseState.ACTIVE,
                            expected_version=lease.version,
                            event_type="sync_noop_pending",
                            summary=(
                                "source-only patch left the synchronization index "
                                "equal to the target tree"
                            ),
                            observed_head_sha=target_sha,
                        )
                    except (RuntimeError, sqlite3.Error) as error:
                        return self._sync_blocked(
                            "registry_conflict", str(error), lease=lease
                        )
                    return self._finalize_sync_noop(
                        lease=lease,
                        expected=lease,
                        github=github,
                        source_branch=source_branch,
                        target_branch=target_branch,
                        source_sha=source_sha,
                        target_sha=target_sha,
                        merge_base=merge_base,
                    )
                sync_head = self.git.commit(
                    lease.worktree_path,
                    self._sync_message(
                        source_branch=source_branch,
                        target_branch=target_branch,
                        merge_base=merge_base,
                        source_sha=source_sha,
                        target_sha=target_sha,
                        lease=lease,
                    ),
                )
                synced_paths = tuple(
                    sorted(
                        self.git.changed_paths(
                            lease.worktree_path,
                            target_sha,
                            sync_head,
                            find_renames=False,
                        )
                    )
                )
                if synced_paths != lease.reviewed_paths:
                    return self._block_sync_lease(
                        lease,
                        "sync_delta_mismatch",
                        "synchronization paths do not exactly match the source-only delta",
                    )
                prepare_error = self._prepare(lease, force=True)
                if prepare_error is not None:
                    return self._block_sync_lease(
                        lease, "sync_prepare_failed", prepare_error
                    )
                if self.git.status_porcelain(lease.worktree_path):
                    return self._block_sync_lease(
                        lease,
                        "sync_prepare_dirty",
                        "sync prepare command left uncommitted changes",
                    )
                verification_actions = self._verify_promotion(lease.worktree_path)
            except GitPatchConflict as error:
                return self._block_sync_lease(
                    lease,
                    "sync_target_conflict",
                    "source-only delta conflicts with target paths: "
                    + ", ".join(error.paths),
                    conflicted_paths=tuple(sorted(error.paths)),
                )
            except (GitError, OSError, RuntimeError, subprocess.SubprocessError) as error:
                return self._block_sync_lease(
                    lease, "sync_apply_failed", str(error)
                )

            try:
                live_source_sha = self.git.remote_branch_sha(source_branch)
                live_target_sha = self.git.remote_branch_sha(target_branch)
            except GitRemoteError as error:
                return self._external_error(
                    command, "sync_publish_failed", str(error), lease=lease
                )
            if live_source_sha != source_sha:
                return self._block_sync_lease(
                    lease,
                    "sync_source_changed",
                    f"source branch {source_branch!r} changed after verification",
                )
            if live_target_sha != target_sha:
                return self._block_sync_lease(
                    lease,
                    "sync_target_changed",
                    f"target branch {target_branch!r} changed after verification",
                )
            try:
                lease = self.registry.transition(
                    lease.id,
                    LeaseState.ACTIVE,
                    expected_version=lease.version,
                    event_type="sync_publish_pending",
                    summary="branch synchronization verified; publication pending",
                    observed_head_sha=sync_head,
                    head_sha=sync_head,
                )
            except (RuntimeError, sqlite3.Error) as error:
                return self._sync_blocked(
                    "registry_conflict", str(error), lease=lease
                )
            try:
                self.git.push_branch(lease.worktree_path, lease.branch)
            except GitError as error:
                return self._external_error(
                    command, "sync_publish_failed", str(error), lease=lease
                )
            drift_blocker = self._sync_remote_drift_blocker(
                lease=lease,
                source_branch=source_branch,
                target_branch=target_branch,
                source_sha=source_sha,
                target_sha=target_sha,
            )
            if drift_blocker is not None:
                return drift_blocker
            try:
                target_pull_request = github.find_open_pr(
                    head=lease.branch, base=target_branch
                )
                if target_pull_request is None:
                    target_pull_request = github.create_pr(
                        base=target_branch,
                        head=lease.branch,
                        title=f"Sync {source_branch} to {target_branch}",
                        body=self._sync_body(
                            source_branch=source_branch,
                            target_branch=target_branch,
                            merge_base=merge_base,
                            source_sha=source_sha,
                            target_sha=target_sha,
                            lease=lease,
                        ),
                    )
            except ExternalServiceError as error:
                return self._external_error(
                    command, "sync_publish_failed", str(error), lease=lease
                )
            expected_body = self._sync_body(
                source_branch=source_branch,
                target_branch=target_branch,
                merge_base=merge_base,
                source_sha=source_sha,
                target_sha=target_sha,
                lease=lease,
            )
            if (
                target_pull_request.state != "OPEN"
                or target_pull_request.base_ref != target_branch
                or target_pull_request.head_ref != lease.branch
                or target_pull_request.body != expected_body
                or target_pull_request.base_sha != target_sha
            ):
                return self._block_sync_lease(
                    lease,
                    "sync_pr_mismatch",
                    "GitHub did not return the exact open synchronization pull request",
                )
            if target_pull_request.head_sha != sync_head:
                return self._block_sync_lease(
                    lease,
                    "sync_pr_head_mismatch",
                    "GitHub synchronization pull request head does not match verification",
                )
            drift_blocker = self._sync_remote_drift_blocker(
                lease=lease,
                source_branch=source_branch,
                target_branch=target_branch,
                source_sha=source_sha,
                target_sha=target_sha,
            )
            if drift_blocker is not None:
                return drift_blocker
            try:
                lease = self.registry.transition(
                    lease.id,
                    LeaseState.PR_OPEN,
                    expected_version=lease.version,
                    event_type="sync_pr_open",
                    summary=f"sync PR #{target_pull_request.number} opened",
                    observed_head_sha=sync_head,
                    pr_number=target_pull_request.number,
                    head_sha=sync_head,
                )
            except (RuntimeError, sqlite3.Error) as error:
                return self._sync_blocked(
                    "registry_conflict", str(error), lease=lease
                )
            return CommandResult.ok(
                command,
                decision="ready",
                lease=lease,
                actions=verification_actions,
            )

    def promote(
        self,
        *,
        source_pr: int | Sequence[int],
        exclude_paths: Sequence[str] = (),
        target_branch: str,
        apply: bool,
        out_of_order: bool = False,
    ) -> CommandResult:
        try:
            source_numbers = self._promotion_source_numbers(source_pr)
        except ValueError as error:
            return self._promotion_blocked("invalid_source_pr", str(error))
        promotion_mode = (
            PromotionMode.OUT_OF_ORDER
            if out_of_order
            else PromotionMode.EXACT
        )
        if promotion_mode is PromotionMode.OUT_OF_ORDER and exclude_paths:
            return self._promotion_blocked(
                "invalid_out_of_order_promotion",
                "out-of-order promotion does not allow excluded paths",
            )
        try:
            excluded_paths = self._promotion_excluded_paths(exclude_paths)
        except ValueError as error:
            return self._promotion_blocked("invalid_excluded_path", str(error))
        try:
            target_ref = self._promotion_target_ref(target_branch)
        except ConfigError as error:
            return self._promotion_blocked("invalid_target_branch", str(error))
        try:
            github = self.github or GhClient(self.git.repository_root())
            sources = tuple(github.view_pr(number) for number in source_numbers)
        except ExternalServiceError as error:
            return self._external_error(
                "wt.promote", "source_pr_unavailable", str(error)
            )
        except ValueError as error:
            return self._promotion_blocked("source_pr_unavailable", str(error))
        source_blocker = self._promotion_sources_blocker(sources, target_ref)
        if source_blocker is not None:
            return source_blocker
        reviewed_paths = {
            path for source in sources for path in source.changed_paths
        }
        unknown_exclusions = tuple(
            path for path in excluded_paths if path not in reviewed_paths
        )
        if unknown_exclusions:
            return self._promotion_blocked(
                "invalid_excluded_path",
                "excluded paths must be reviewed paths from the source pull requests: "
                + ", ".join(unknown_exclusions),
            )
        if reviewed_paths and reviewed_paths.issubset(excluded_paths):
            return self._promotion_blocked(
                "invalid_excluded_path",
                "excluded paths must leave at least one reviewed path to promote",
            )
        for source in sources:
            invalid_oid = self._invalid_promotion_oid(
                source,
                require_merge=(
                    promotion_mode is PromotionMode.OUT_OF_ORDER
                    or bool(excluded_paths)
                    or len(sources) > 1
                ),
            )
            if invalid_oid is not None:
                return self._promotion_blocked(
                    "source_pr_invalid_oid",
                    f"source pull request #{source.number} has an invalid {invalid_oid}",
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
            lease = self._new_promotion_lease(
                sources,
                target_ref,
                target_sha,
                excluded_paths,
                promotion_mode=promotion_mode,
            )
            if promotion_mode is PromotionMode.OUT_OF_ORDER:
                try:
                    active = self.registry.find_active_read_only(
                        lease.repository_id, lease.initiative, Purpose.PROMOTE
                    )
                except sqlite3.Error as error:
                    return self._promotion_blocked("registry_conflict", str(error))
                retry_identity = self._stale_precommit_promotion_retry_identity(
                    active,
                    repository_id=lease.repository_id,
                    initiative=lease.initiative,
                    sources=sources,
                    target_ref=target_ref,
                    expected_branch=lease.branch,
                    live_target_sha=target_sha,
                )
                if retry_identity is not None:
                    lease = self._new_promotion_lease(
                        sources,
                        target_ref,
                        target_sha,
                        excluded_paths,
                        promotion_mode=promotion_mode,
                        retry_identity=retry_identity,
                    )
                resolution_preview = self._out_of_order_resolution_preview(
                    active,
                    expected=lease,
                    github=github,
                    sources=sources,
                    target_branch=target_branch,
                )
                if resolution_preview is not None:
                    return resolution_preview
            return CommandResult.ok(
                "wt.promote",
                decision="preview",
                actions=(
                    {
                        "kind": "create_worktree",
                        "path": str(lease.worktree_path),
                        "branch": lease.branch,
                        "source_pr": sources[0].number,
                        "source_prs": [source.number for source in sources],
                        "sources": [
                            {
                                "ordinal": ordinal,
                                "pr": source.number,
                                "base_ref": source.base_ref,
                                "base_sha": source.base_sha,
                                "head_sha": source.head_sha,
                                "merge_sha": source.merge_commit_sha,
                                "reviewed_paths": list(
                                    sorted(source.changed_paths)
                                ),
                            }
                            for ordinal, source in enumerate(sources)
                        ],
                        "source_base_sha": sources[0].base_sha,
                        "source_head_sha": sources[0].head_sha,
                        "excluded_paths": list(excluded_paths),
                        "target_branch": target_branch,
                        "target_base_sha": target_sha,
                        "promotion_mode": promotion_mode.value,
                        "reviewed_paths": list(lease.reviewed_paths),
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
        initiative = self._promotion_initiative(
            source_numbers,
            target_branch,
            excluded_paths,
            promotion_mode=promotion_mode,
        )
        expected_branch = self._promotion_branch(initiative)
        retry_identity: _PromotionRetryIdentity | None = None
        target_sha: str | None = None
        with repository_lock(self.lock_dir / f"{repository_id}.lock"):
            active = self.registry.find_active(
                repository_id, initiative, Purpose.PROMOTE
            )
            if active is not None:
                if (
                    promotion_mode is PromotionMode.OUT_OF_ORDER
                    and active.state is LeaseState.BLOCKED
                    and active.resolution_state is ResolutionState.NONE
                    and active.target_base_sha == active.head_sha
                ):
                    try:
                        events = self.registry.list_events(active.id)
                    except sqlite3.Error:
                        events = []
                    if (
                        events
                        and events[-1].event_type == "promotion_blocked"
                        and events[-1].summary.startswith("promotion_apply_failed:")
                    ):
                        try:
                            target_sha = self.git.fetch_ref(target_branch)
                        except GitRemoteError as error:
                            return self._external_error(
                                "wt.promote", "source_delta_unavailable", str(error)
                            )
                        except GitError as error:
                            return self._promotion_blocked(
                                "source_delta_unavailable", str(error)
                            )
                        retry_identity = (
                            self._stale_precommit_promotion_retry_identity(
                                active,
                                repository_id=repository_id,
                                initiative=initiative,
                                sources=sources,
                                target_ref=target_ref,
                                expected_branch=expected_branch,
                                live_target_sha=target_sha,
                            )
                        )
                if retry_identity is None:
                    return self._reuse_promotion(
                        active,
                        sources=sources,
                        excluded_paths=excluded_paths,
                        promotion_mode=promotion_mode,
                        target_ref=target_ref,
                        expected_branch=expected_branch,
                        github=github,
                        target_branch=target_branch,
                    )
                initiative = retry_identity.initiative
                expected_branch = retry_identity.branch
                active = self.registry.find_active(
                    repository_id, initiative, Purpose.PROMOTE
                )
                if active is not None:
                    return self._reuse_promotion(
                        active,
                        sources=sources,
                        excluded_paths=excluded_paths,
                        promotion_mode=promotion_mode,
                        target_ref=target_ref,
                        expected_branch=expected_branch,
                        github=github,
                        target_branch=target_branch,
                    )
            try:
                if target_sha is None:
                    target_sha = self.git.fetch_ref(target_branch)
                assert target_sha is not None
                staging_sha = (
                    self.git.fetch_ref(sources[0].base_ref)
                    if (
                        excluded_paths
                        or promotion_mode is PromotionMode.OUT_OF_ORDER
                    )
                    else None
                )
                deltas: list[tuple[int, bytes]] = []
                expected_blob_heads: dict[str, str] | None = (
                    {} if promotion_mode is PromotionMode.EXACT else None
                )
                previous_source_merge_sha: str | None = None
                for source_ordinal, source in enumerate(sources):
                    source_base_sha = self.git.fetch_ref(source.base_sha)
                    source_head_sha = self.git.fetch_ref(source.head_sha)
                    source_merge_sha: str | None = None
                    if source.merge_commit_sha is None:
                        if (
                            excluded_paths
                            or promotion_mode is PromotionMode.OUT_OF_ORDER
                        ):
                            return self._promotion_blocked(
                                "source_pr_invalid_oid",
                                (
                                    f"source pull request #{source.number} has"
                                    " an invalid merge SHA"
                                ),
                            )
                    else:
                        source_merge_sha = self.git.fetch_ref(
                            source.merge_commit_sha
                        )
                    merge_base = self.git.merge_base(
                        source_base_sha, source_head_sha
                    )
                    patch = self.git.binary_diff(merge_base, source_head_sha)
                    source_paths = self.git.changed_paths(
                        self.git.repository_root(),
                        merge_base,
                        source_head_sha,
                        find_renames=True,
                    )
                    if (
                        source_base_sha != source.base_sha
                        or source_head_sha != source.head_sha
                        or (
                            source_merge_sha is not None
                            and source_merge_sha != source.merge_commit_sha
                        )
                    ):
                        return self._promotion_blocked(
                            "source_sha_mismatch",
                            (
                                f"fetched source pull request #{source.number} refs"
                                " do not match the reviewed SHAs"
                            ),
                        )
                    if (
                        excluded_paths
                        or promotion_mode is PromotionMode.OUT_OF_ORDER
                    ) and merge_base != source_base_sha:
                        return self._promotion_blocked(
                            "source_base_not_ancestor",
                            (
                                f"source pull request #{source.number} base is not"
                                " an ancestor of its reviewed head"
                            ),
                        )
                    if (
                        (
                            excluded_paths
                            or promotion_mode is PromotionMode.OUT_OF_ORDER
                        )
                        and source_merge_sha is not None
                        and staging_sha is not None
                        and (
                            self.git.merge_base(source_base_sha, source_merge_sha)
                            != source_base_sha
                            or self.git.merge_base(source_merge_sha, staging_sha)
                            != source_merge_sha
                        )
                    ):
                        return self._promotion_blocked(
                            "source_merge_not_in_staging",
                            (
                                f"source pull request #{source.number} merge commit"
                                " is not in the configured staging history"
                            ),
                        )
                    if (
                        previous_source_merge_sha is not None
                        and source_merge_sha is not None
                        and self.git.merge_base(
                            previous_source_merge_sha, source_merge_sha
                        )
                        != previous_source_merge_sha
                    ):
                        return self._promotion_blocked(
                            "source_pr_sequence_order",
                            (
                                f"source pull request #{source.number} was not"
                                " merged after the preceding source pull request"
                            ),
                        )
                    if source_merge_sha is not None:
                        previous_source_merge_sha = source_merge_sha
                    expected_source_paths = tuple(sorted(source.changed_paths))
                    if not patch:
                        return self._promotion_blocked(
                            "source_pr_empty_delta",
                            (
                                f"source pull request #{source.number} has no changes"
                                " after its merge base"
                            ),
                        )
                    if source_paths != expected_source_paths:
                        return self._promotion_blocked(
                            "source_delta_mismatch",
                            (
                                f"source pull request #{source.number} paths do not"
                                " match its reviewed Git delta"
                            ),
                        )
                    if (
                        promotion_mode is PromotionMode.OUT_OF_ORDER
                        and tuple(
                            sorted(
                                self.git.changed_path_endpoints(
                                    self.git.repository_root(),
                                    merge_base,
                                    source_head_sha,
                                )
                            )
                        )
                        != expected_source_paths
                    ):
                        return self._promotion_blocked(
                            "unsupported_out_of_order_rename",
                            "out-of-order promotion does not support renamed paths",
                        )
                    if (
                        (
                            excluded_paths
                            or promotion_mode is PromotionMode.OUT_OF_ORDER
                        )
                        and source_merge_sha is not None
                        and any(
                            self.git.path_blob(source_merge_sha, path)
                            != self.git.path_blob(source_head_sha, path)
                            for path in expected_source_paths
                        )
                    ):
                        return self._promotion_blocked(
                            "source_merge_delta_mismatch",
                            (
                                f"source pull request #{source.number} merged contents"
                                " do not match its reviewed head"
                            ),
                        )
                    included_source_paths = tuple(
                        path
                        for path in expected_source_paths
                        if path not in excluded_paths
                        and (
                            promotion_mode is not PromotionMode.OUT_OF_ORDER
                            or self.git.path_blob(target_sha, path)
                            != self.git.path_blob(source_head_sha, path)
                        )
                    )
                    if included_source_paths:
                        included_patch = (
                            patch
                            if not excluded_paths
                            else self.git.binary_diff(
                                merge_base,
                                source_head_sha,
                                paths=included_source_paths,
                            )
                        )
                        if not included_patch:
                            return self._promotion_blocked(
                                "source_delta_mismatch",
                                (
                                    f"source pull request #{source.number} has no"
                                    " included changes after path exclusions"
                                ),
                            )
                        deltas.append((source_ordinal, included_patch))
                    if expected_blob_heads is not None:
                        for path in included_source_paths:
                            expected_blob_heads[path] = source_head_sha
            except GitRemoteError as error:
                return self._external_error(
                    "wt.promote", "source_delta_unavailable", str(error)
                )
            except GitError as error:
                return self._promotion_blocked("source_delta_unavailable", str(error))

            expected_blobs: dict[str, str | None] | None = None
            expected_paths: tuple[str, ...] = ()
            if expected_blob_heads is not None:
                expected_blobs = self._promotion_net_blobs(
                    expected_blob_heads, target_sha
                )
                expected_paths = tuple(sorted(expected_blobs))
            lease = self._new_promotion_lease(
                sources,
                target_ref,
                target_sha,
                excluded_paths,
                promotion_mode=promotion_mode,
                retry_identity=retry_identity,
            )
            branch_conflict = self._branch_conflict(lease.branch)
            if branch_conflict is not None:
                return self._promotion_blocked(
                    "branch_conflict",
                    f"branch {lease.branch!r} is already checked out at {branch_conflict}",
                    lease=lease,
                )
            try:
                self.git.add_worktree(
                    lease.worktree_path,
                    lease.branch,
                    target_sha,
                    reuse_exact_branch=True,
                )
            except GitError as error:
                return self._promotion_blocked(
                    "worktree_conflict", str(error), lease=lease
                )
            try:
                lease = replace(
                    lease, head_sha=self.git.head_sha(lease.worktree_path)
                )
                lease = (
                    self.registry.create_promotion_lease(
                        lease,
                        self._promotion_source_pins(lease, sources),
                    )
                    if promotion_mode is PromotionMode.OUT_OF_ORDER
                    else self.registry.create_lease(lease)
                )
            except (GitError, OSError, RuntimeError, ValueError, sqlite3.Error) as error:
                result = self._handle_creation_failure(lease, error, target_sha)
                return replace(result, command="wt.promote")

            try:
                for source_ordinal, patch in deltas:
                    self.git.apply_indexed_patch(lease.worktree_path, patch)
                promotion_head = self.git.commit(
                    lease.worktree_path,
                    self._promotion_message(
                        sources=sources,
                        excluded_paths=excluded_paths,
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
                if promotion_mode is PromotionMode.EXACT:
                    assert expected_blobs is not None
                    if promoted_paths != expected_paths:
                        return self._block_promotion_lease(
                            lease,
                            "promotion_delta_mismatch",
                            (
                                "promotion paths do not exactly match the reviewed"
                                " pull requests"
                            ),
                        )
                    mismatched_paths = tuple(
                        path
                        for path in expected_paths
                        if expected_blobs[path]
                        != self.git.path_blob(promotion_head, path)
                    )
                    if mismatched_paths:
                        staging_branch = self.config.default_base or sources[0].base_ref
                        if self.config.production_branch is None:
                            recommendation = (
                                "configure worktree.production_branch, then run "
                                "`awf wt sync` from production to staging"
                            )
                        else:
                            repository_root = json.dumps(
                                str(self.git.repository_root())
                            )
                            recommendation = (
                                "synchronize it with "
                                f"`awf wt sync --from {self.config.production_branch} "
                                f"--to {staging_branch} --repo-root {repository_root} "
                                "--apply --json`"
                            )
                        return self._block_promotion_lease(
                            lease,
                            "staging_missing_main_delta",
                            (
                                "configured staging is missing production-branch "
                                "content on reviewed paths: "
                                + ", ".join(mismatched_paths)
                                + "; "
                                + recommendation
                            ),
                        )
                elif not promoted_paths or not all(
                    path in lease.reviewed_paths for path in promoted_paths
                ):
                    return self._block_promotion_lease(
                        lease,
                        "promotion_delta_mismatch",
                        (
                            "out-of-order promotion paths must be a non-empty subset"
                            " of the reviewed pull request paths"
                        ),
                    )
                prepare_blocker = self._prepare_promotion(lease, force=True)
                if prepare_blocker is not None:
                    return prepare_blocker
                verification_actions = self._verify_promotion(lease.worktree_path)
            except GitPatchConflict as error:
                if promotion_mode is PromotionMode.OUT_OF_ORDER:
                    return self._record_out_of_order_conflict(
                        lease, error, source_ordinal=source_ordinal
                    )
                return self._block_promotion_lease(
                    lease, "promotion_apply_failed", str(error)
                )
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
                    resolution_state=(
                        ResolutionState.AUTOMATIC
                        if promotion_mode is PromotionMode.OUT_OF_ORDER
                        else None
                    ),
                )
            except (RuntimeError, sqlite3.Error) as error:
                return self._promotion_blocked(
                    "registry_conflict", str(error), lease=lease
                )
            if promotion_mode is PromotionMode.OUT_OF_ORDER:
                source_blocker = self._out_of_order_reuse_source_blocker(
                    lease, sources
                )
                if source_blocker is not None:
                    if source_blocker.status == "error":
                        return source_blocker
                    return self._block_promotion_lease(
                        lease,
                        "promotion_provenance_changed",
                        source_blocker.blockers[0]["message"],
                    )
                try:
                    live_target_sha = self.git.remote_branch_sha(target_branch)
                except GitRemoteError as error:
                    return self._external_error(
                        "wt.promote",
                        "promotion_publish_failed",
                        str(error),
                        lease=lease,
                    )
                if live_target_sha is None:
                    return self._block_promotion_lease(
                        lease,
                        "target_ref_unavailable",
                        f"target branch {target_branch!r} is unavailable on origin",
                    )
                if live_target_sha != target_sha:
                    return self._block_promotion_lease(
                        lease,
                        "promotion_provenance_changed",
                        f"target branch {target_branch!r} changed after verification",
                    )

            try:
                self.git.push_branch(lease.worktree_path, lease.branch)
            except GitError as error:
                return self._external_error(
                    "wt.promote",
                    "promotion_publish_failed",
                    str(error),
                    lease=lease,
                )
            try:
                target_pull_request = github.find_open_pr(
                    head=lease.branch, base=target_branch
                )
                if target_pull_request is None:
                    target_pull_request = github.create_pr(
                        base=target_branch,
                        head=lease.branch,
                        title=self._promotion_title(source_numbers, target_branch),
                        body=self._promotion_body(
                            sources=sources,
                            excluded_paths=excluded_paths,
                            target_sha=target_sha,
                            lease=lease,
                        ),
                    )
            except ExternalServiceError as error:
                return self._external_error(
                    "wt.promote",
                    "promotion_publish_failed",
                    str(error),
                    lease=lease,
                )
            except ValueError as error:
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

    def release_open(
        self,
        *,
        release_id: str,
        target_branch: str,
        apply: bool,
    ) -> CommandResult:
        command = "wt.release.open"
        try:
            release_slug = _initiative_slug(release_id)
            target_ref = self._promotion_target_ref(target_branch)
        except (ConfigError, ValueError) as error:
            return self._release_blocked(command, "invalid_release", str(error))
        repository_id = self.git.repository_id()
        if not apply:
            try:
                existing = self.registry.find_release_read_only(
                    repository_id, release_slug
                )
            except sqlite3.Error as error:
                return self._release_blocked(command, "registry_conflict", str(error))
            if existing is not None:
                return self._release_reuse(
                    command,
                    existing,
                    target_branch=target_branch,
                    lease=self.registry.get_lease_read_only(existing.lease_id),
                    apply=False,
                )
            try:
                target_sha = self.git.resolve_ref(target_ref)
            except GitError as error:
                return self._release_blocked(
                    command, "target_ref_unavailable", str(error)
                )
            lease = self._new_release_lease(
                release_slug, target_ref, target_sha
            )
            release = ReleaseBridge.new(
                repository_id=repository_id,
                repository_name=self.git.repository_name(),
                repository_root=self.git.repository_root(),
                release_id=release_slug,
                target_branch=target_branch,
                lease_id=lease.id,
            )
            return CommandResult.ok(
                command,
                decision="preview",
                release=release,
                actions=(
                    {
                        "kind": "create_worktree",
                        "path": str(lease.worktree_path),
                        "branch": lease.branch,
                        "target_branch": target_branch,
                        "target_base_sha": target_sha,
                    },
                ),
            )

        with repository_lock(self.lock_dir / f"{repository_id}.lock"):
            try:
                existing = self.registry.find_release(repository_id, release_slug)
            except sqlite3.Error as error:
                return self._release_blocked(command, "registry_conflict", str(error))
            if existing is not None:
                return self._release_reuse(
                    command,
                    existing,
                    target_branch=target_branch,
                    lease=self.registry.get_lease(existing.lease_id),
                    apply=True,
                )
            try:
                target_sha = self.git.fetch_ref(target_branch)
            except GitRemoteError as error:
                return self._release_external(
                    command, "target_ref_unavailable", str(error)
                )
            except GitError as error:
                return self._release_blocked(
                    command, "target_ref_unavailable", str(error)
                )
            lease = self._new_release_lease(release_slug, target_ref, target_sha)
            active = self.registry.find_active(
                repository_id, lease.initiative, Purpose.PROMOTE
            )
            if active is not None:
                return self._release_blocked(
                    command,
                    "release_lease_conflict",
                    (
                        f"managed promotion lease {active.id} already owns release "
                        f"initiative {lease.initiative!r}"
                    ),
                    lease=active,
                )
            branch_conflict = self._branch_conflict(lease.branch)
            if branch_conflict is not None:
                return self._release_blocked(
                    command,
                    "branch_conflict",
                    f"branch {lease.branch!r} is already checked out at {branch_conflict}",
                    lease=lease,
                )
            release = ReleaseBridge.new(
                repository_id=repository_id,
                repository_name=self.git.repository_name(),
                repository_root=self.git.repository_root(),
                release_id=release_slug,
                target_branch=target_branch,
                lease_id=lease.id,
            )
            try:
                lease, release = self.registry.create_release_with_lease(
                    lease,
                    release,
                )
            except (RuntimeError, ValueError, sqlite3.Error) as error:
                return self._release_blocked(
                    command,
                    "registry_conflict",
                    str(error),
                    lease=lease,
                    release=release,
                )
            try:
                self.git.add_worktree(
                    lease.worktree_path,
                    lease.branch,
                    target_sha,
                    reuse_exact_branch=True,
                )
            except (GitError, OSError) as error:
                return self._release_blocked(
                    command,
                    "release_worktree_create_failed",
                    str(error),
                    lease=lease,
                    release=release,
                )
            return CommandResult.ok(
                command, decision="ready", lease=lease, release=release
            )

    def release_add(
        self,
        *,
        release_id: str,
        source_pr: int,
        apply: bool,
    ) -> CommandResult:
        command = "wt.release.add"
        try:
            release_slug = _initiative_slug(release_id)
            source_number = self._promotion_source_numbers(source_pr)[0]
        except ValueError as error:
            return self._release_blocked(command, "invalid_release_source", str(error))
        repository_id = self.git.repository_id()
        if not apply:
            try:
                release = self.registry.find_release_read_only(
                    repository_id, release_slug
                )
            except sqlite3.Error as error:
                return self._release_blocked(command, "registry_conflict", str(error))
            if release is None:
                return self._release_blocked(
                    command,
                    "unknown_release",
                    f"release {release_slug!r} does not exist",
                )
            lease = self.registry.get_lease_read_only(release.lease_id)
            validation = self._release_add_validation(
                command, release, source_number
            )
            if isinstance(validation, CommandResult):
                return validation
            source = validation
            return CommandResult.ok(
                command,
                decision="preview",
                lease=lease,
                release=release,
                actions=(
                    {
                        "kind": "append_source",
                        "source_pr": source.source_pr,
                        "ordinal": source.ordinal,
                        "base_sha": source.base_sha,
                        "head_sha": source.head_sha,
                        "merge_sha": source.merge_sha,
                        "changed_paths": list(source.changed_paths),
                    },
                    {
                        "kind": "rebuild_release_bridge",
                        "target_branch": release.target_branch,
                        "source_prs": [
                            *[item.source_pr for item in release.sources],
                            source.source_pr,
                        ],
                    },
                ),
            )

        with repository_lock(self.lock_dir / f"{repository_id}.lock"):
            try:
                release = self.registry.find_release(repository_id, release_slug)
            except sqlite3.Error as error:
                return self._release_blocked(command, "registry_conflict", str(error))
            if release is None:
                return self._release_blocked(
                    command,
                    "unknown_release",
                    f"release {release_slug!r} does not exist",
                )
            validation = self._release_add_validation(command, release, source_number)
            if isinstance(validation, CommandResult):
                if validation.status != "ok" or validation.decision != "reuse":
                    return validation
                try:
                    lease = self._release_lease(command, release)
                    rebuilt = self._rebuild_release_bridge(
                        command,
                        release,
                        lease,
                        verify=False,
                    )
                except (RuntimeError, ValueError, sqlite3.Error) as error:
                    return self._release_blocked(
                        command,
                        "registry_conflict",
                        str(error),
                        release=release,
                    )
                if isinstance(rebuilt, CommandResult):
                    return rebuilt
                lease, _target_sha, _actions = rebuilt
                return CommandResult.ok(
                    command,
                    decision="reuse",
                    lease=lease,
                    release=release,
                )
            source = validation
            lease = self._release_lease(command, release)
            try:
                pending_source = self.registry.get_pending_release_source(release.id)
                if pending_source is not None and pending_source != source:
                    return self._release_blocked(
                        command,
                        "release_source_pending",
                        "another release source add is pending recovery",
                        lease=lease,
                        release=release,
                    )
                release = self.registry.stage_release_source(
                    source,
                    expected_version=release.version,
                )
                if (
                    pending_source is not None
                    and self.git.status_porcelain(lease.worktree_path)
                ):
                    return self._release_blocked(
                        command,
                        "release_worktree_dirty",
                        "pending source recovery found a dirty release worktree",
                        lease=lease,
                        release=release,
                    )
            except (GitError, RuntimeError, ValueError, sqlite3.Error) as error:
                return self._release_blocked(
                    command,
                    "registry_conflict",
                    str(error),
                    lease=lease,
                    release=release,
                )
            candidate_sources = (*release.sources, source)
            candidate_release = replace(
                release,
                sources=candidate_sources,
                source_digest=release_source_digest(candidate_sources),
            )
            if (
                pending_source is not None
                and self.git.head_sha(lease.worktree_path) != lease.head_sha
            ):
                recovered = self._recover_pending_release_candidate(
                    command,
                    candidate_release,
                    lease,
                )
                if isinstance(recovered, CommandResult):
                    return recovered
                rebuilt_lease, target_sha = recovered
            else:
                rebuilt = self._rebuild_release_bridge(
                    command,
                    candidate_release,
                    lease,
                    verify=False,
                    record=False,
                )
                if isinstance(rebuilt, CommandResult):
                    try:
                        release = self.registry.clear_pending_release_source(
                            source,
                            expected_version=release.version,
                        )
                    except (RuntimeError, ValueError, sqlite3.Error) as rollback_error:
                        return self._release_blocked(
                            command,
                            "release_rollback_failed",
                            str(rollback_error),
                            lease=lease,
                            release=release,
                        )
                    return replace(rebuilt, release=release)
                rebuilt_lease, target_sha, _actions = rebuilt
            try:
                lease, release = self.registry.accept_release_source(
                    source,
                    expected_release_version=release.version,
                    lease_id=lease.id,
                    expected_lease_version=lease.version,
                    head_sha=rebuilt_lease.head_sha,
                    target_base_sha=target_sha,
                )
            except (RuntimeError, ValueError, sqlite3.Error) as error:
                return self._release_blocked(
                    command,
                    "registry_conflict",
                    str(error),
                    lease=lease,
                    release=release,
                )
            return CommandResult.ok(
                command,
                decision="ready",
                lease=lease,
                release=release,
            )

    def release_seal(self, *, release_id: str, apply: bool) -> CommandResult:
        command = "wt.release.seal"
        try:
            release_slug = _initiative_slug(release_id)
        except ValueError as error:
            return self._release_blocked(command, "invalid_release", str(error))
        repository_id = self.git.repository_id()
        getter = (
            self.registry.find_release
            if apply
            else self.registry.find_release_read_only
        )
        try:
            release = getter(repository_id, release_slug)
        except sqlite3.Error as error:
            return self._release_blocked(command, "registry_conflict", str(error))
        if release is None:
            return self._release_blocked(
                command, "unknown_release", f"release {release_slug!r} does not exist"
            )
        lease = (
            self.registry.get_lease(release.lease_id)
            if apply
            else self.registry.get_lease_read_only(release.lease_id)
        )
        try:
            pending_source = self.registry.get_pending_release_source(release.id)
        except sqlite3.Error as error:
            return self._release_blocked(
                command,
                "registry_conflict",
                str(error),
                lease=lease,
                release=release,
            )
        if pending_source is not None:
            return self._release_blocked(
                command,
                "release_source_pending",
                (
                    f"source pull request #{pending_source.source_pr} add is "
                    "pending recovery"
                ),
                lease=lease,
                release=release,
            )
        if release.state is ReleaseState.SEALED:
            return CommandResult.ok(
                command, decision="reuse", lease=lease, release=release
            )
        if release.state is not ReleaseState.OPEN:
            return self._release_blocked(
                command,
                "release_not_open",
                f"release {release.release_id!r} is {release.state.value}",
                lease=lease,
                release=release,
            )
        if not release.sources:
            return self._release_blocked(
                command,
                "release_empty",
                "release must contain at least one pinned source before sealing",
                lease=lease,
                release=release,
            )
        if not self.config.verify_production:
            return self._release_blocked(
                command,
                "production_verify_missing",
                "verify.production.commands must configure at least one command",
                lease=lease,
                release=release,
            )
        if not apply:
            try:
                target_ref = self._promotion_target_ref(release.target_branch)
            except ConfigError as error:
                return self._release_blocked(
                    command,
                    "invalid_target_branch",
                    str(error),
                    lease=lease,
                    release=release,
                )
            source_validation = self._release_live_sources(
                command,
                release,
                target_ref,
            )
            if isinstance(source_validation, CommandResult):
                return source_validation
            return CommandResult.ok(
                command,
                decision="preview",
                lease=lease,
                release=release,
                actions=(
                    {
                        "kind": "rebuild_release_bridge",
                        "target_branch": release.target_branch,
                        "source_prs": [source.source_pr for source in release.sources],
                    },
                    {"kind": "prepare_release"},
                    *(
                        {
                            "kind": "verify_production",
                            "argv": list(item),
                        }
                        for item in self.config.verify_production
                    ),
                ),
            )
        with repository_lock(self.lock_dir / f"{repository_id}.lock"):
            release = self.registry.find_release(repository_id, release_slug)
            if release is None:
                return self._release_blocked(
                    command,
                    "unknown_release",
                    f"release {release_slug!r} does not exist",
                )
            lease = self._release_lease(command, release)
            if release.state is ReleaseState.SEALED:
                return CommandResult.ok(
                    command, decision="reuse", lease=lease, release=release
                )
            if release.state is not ReleaseState.OPEN:
                return self._release_blocked(
                    command,
                    "release_not_open",
                    f"release {release.release_id!r} is {release.state.value}",
                    lease=lease,
                    release=release,
                )
            verified = self._verify_release_bridge_live(
                command,
                release,
                lease,
            )
            if isinstance(verified, CommandResult):
                return verified
            lease, target_sha, verification_actions = verified
            try:
                release = self.registry.transition_release(
                    release.id,
                    ReleaseState.SEALED,
                    expected_version=release.version,
                    last_verified_target_sha=target_sha,
                )
            except (RuntimeError, ValueError, sqlite3.Error) as error:
                return self._release_blocked(
                    command,
                    "registry_conflict",
                    str(error),
                    lease=lease,
                    release=release,
                )
            return CommandResult.ok(
                command,
                decision="ready",
                lease=lease,
                release=release,
                actions=verification_actions,
            )

    def release_publish(self, *, release_id: str, apply: bool) -> CommandResult:
        command = "wt.release.publish"
        try:
            release_slug = _initiative_slug(release_id)
        except ValueError as error:
            return self._release_blocked(command, "invalid_release", str(error))
        repository_id = self.git.repository_id()
        getter = (
            self.registry.find_release
            if apply
            else self.registry.find_release_read_only
        )
        try:
            release = getter(repository_id, release_slug)
        except sqlite3.Error as error:
            return self._release_blocked(command, "registry_conflict", str(error))
        if release is None:
            return self._release_blocked(
                command, "unknown_release", f"release {release_slug!r} does not exist"
            )
        lease = (
            self.registry.get_lease(release.lease_id)
            if apply
            else self.registry.get_lease_read_only(release.lease_id)
        )
        if release.state is ReleaseState.PUBLISHED:
            return self._release_published_reuse(command, release, lease)
        if release.state is not ReleaseState.SEALED:
            return self._release_blocked(
                command,
                "release_not_sealed",
                f"release {release.release_id!r} is {release.state.value}",
                lease=lease,
                release=release,
            )
        if not apply:
            try:
                target_ref = self._promotion_target_ref(release.target_branch)
                target_sha = self.git.resolve_ref(target_ref)
            except (ConfigError, GitError) as error:
                return self._release_blocked(
                    command,
                    "target_ref_unavailable",
                    str(error),
                    lease=lease,
                    release=release,
                )
            source_validation = self._release_live_sources(
                command,
                release,
                target_ref,
            )
            if isinstance(source_validation, CommandResult):
                return source_validation
            actions: tuple[dict[str, object], ...] = (
                (
                    {
                        "kind": "rebuild_release_bridge",
                        "target_branch": release.target_branch,
                        "target_base_sha": target_sha,
                    },
                )
                if target_sha != release.last_verified_target_sha
                else ()
            )
            return CommandResult.ok(
                command,
                decision="preview",
                lease=lease,
                release=release,
                actions=(
                    *actions,
                    {"kind": "push_branch", "branch": lease.branch if lease else ""},
                    {"kind": "open_pull_request", "target_branch": release.target_branch},
                ),
            )

        with repository_lock(self.lock_dir / f"{repository_id}.lock"):
            release = self.registry.find_release(repository_id, release_slug)
            if release is None:
                return self._release_blocked(
                    command,
                    "unknown_release",
                    f"release {release_slug!r} does not exist",
                )
            lease = self._release_lease(command, release)
            if release.state is ReleaseState.PUBLISHED:
                return self._release_published_reuse(command, release, lease)
            if release.state is not ReleaseState.SEALED:
                return self._release_blocked(
                    command,
                    "release_not_sealed",
                    f"release {release.release_id!r} is {release.state.value}",
                    lease=lease,
                    release=release,
                )
            try:
                remote_release_sha = self.git.remote_branch_sha(lease.branch)
            except GitRemoteError as error:
                return self._release_external(
                    command,
                    "release_publish_failed",
                    str(error),
                    lease=lease,
                    release=release,
                )
            if (
                remote_release_sha is not None
                and remote_release_sha != lease.head_sha
            ):
                return self._release_blocked(
                    command,
                    "release_branch_changed",
                    "remote release branch no longer matches its registered head",
                    lease=lease,
                    release=release,
                )
            try:
                target_ref = self._promotion_target_ref(release.target_branch)
                target_sha = self.git.fetch_ref(release.target_branch)
            except ConfigError as error:
                return self._release_blocked(
                    command,
                    "invalid_target_branch",
                    str(error),
                    lease=lease,
                    release=release,
                )
            except GitRemoteError as error:
                return self._release_external(
                    command,
                    "target_ref_unavailable",
                    str(error),
                    lease=lease,
                    release=release,
                )
            except GitError as error:
                return self._release_blocked(
                    command,
                    "target_ref_unavailable",
                    str(error),
                    lease=lease,
                    release=release,
                )
            source_validation = self._release_live_sources(command, release, target_ref)
            if isinstance(source_validation, CommandResult):
                return source_validation
            try:
                github = self.github or GhClient(self.git.repository_root())
                existing_target_pr = github.find_pr(
                    head=lease.branch,
                    base=release.target_branch,
                )
            except (ExternalServiceError, ValueError) as error:
                return self._release_external(
                    command,
                    "release_pr_unavailable",
                    str(error),
                    lease=lease,
                    release=release,
                )
            if (
                existing_target_pr is not None
                and existing_target_pr.state not in {"OPEN", "MERGED"}
            ):
                return self._release_blocked(
                    command,
                    "release_pr_closed",
                    (
                        f"release PR #{existing_target_pr.number} is "
                        f"{existing_target_pr.state}"
                    ),
                    lease=lease,
                    release=release,
                )
            if (
                existing_target_pr is not None
                and existing_target_pr.state == "MERGED"
            ):
                if (
                    existing_target_pr.base_ref != release.target_branch
                    or existing_target_pr.base_sha
                    != release.last_verified_target_sha
                    or existing_target_pr.head_ref != lease.branch
                    or existing_target_pr.head_sha != lease.head_sha
                    or existing_target_pr.merge_commit_sha is None
                ):
                    return self._release_blocked(
                        command,
                        "target_pr_mismatch",
                        "merged release PR does not match sealed release provenance",
                        lease=lease,
                        release=release,
                    )
                try:
                    merge_sha = self.git.fetch_ref(
                        existing_target_pr.merge_commit_sha
                    )
                except (GitRemoteError, GitError) as error:
                    return self._release_external(
                        command,
                        "release_pr_unavailable",
                        str(error),
                        lease=lease,
                        release=release,
                    )
                if (
                    merge_sha != existing_target_pr.merge_commit_sha
                    or self.git.merge_base(merge_sha, target_sha) != merge_sha
                ):
                    return self._release_blocked(
                        command,
                        "target_pr_mismatch",
                        "merged release PR is not in target branch history",
                        lease=lease,
                        release=release,
                    )
                try:
                    lease, release = self.registry.publish_release(
                        release.id,
                        lease.id,
                        expected_release_version=release.version,
                        expected_lease_version=lease.version,
                        target_pr=existing_target_pr.number,
                        head_sha=lease.head_sha,
                        target_base_sha=existing_target_pr.base_sha,
                    )
                except (RuntimeError, ValueError, sqlite3.Error) as error:
                    return self._release_blocked(
                        command,
                        "registry_conflict",
                        str(error),
                        lease=lease,
                        release=release,
                    )
                return CommandResult.ok(
                    command,
                    decision="ready",
                    lease=lease,
                    release=release,
                )
            verification_actions: tuple[dict[str, object], ...] = ()
            if target_sha != release.last_verified_target_sha:
                verified = self._verify_release_bridge_live(
                    command,
                    release,
                    lease,
                    target_sha=target_sha,
                    sources=source_validation,
                )
                if isinstance(verified, CommandResult):
                    return verified
                lease, target_sha, verification_actions = verified
                try:
                    release = self.registry.transition_release(
                        release.id,
                        ReleaseState.SEALED,
                        expected_version=release.version,
                        last_verified_target_sha=target_sha,
                    )
                except (RuntimeError, ValueError, sqlite3.Error) as error:
                    return self._release_blocked(
                        command,
                        "registry_conflict",
                        str(error),
                        lease=lease,
                        release=release,
                    )
            else:
                try:
                    worktree_matches = (
                        self.git.head_sha(lease.worktree_path) == lease.head_sha
                        and not self.git.status_porcelain(lease.worktree_path)
                    )
                except GitError as error:
                    return self._release_blocked(
                        command,
                        "release_worktree_unavailable",
                        str(error),
                        lease=lease,
                        release=release,
                    )
                if not worktree_matches:
                    return self._release_blocked(
                        command,
                        "release_worktree_mismatch",
                        "sealed release worktree no longer matches its recorded clean head",
                        lease=lease,
                        release=release,
                    )
            try:
                live_target_sha = self.git.remote_branch_sha(release.target_branch)
            except GitRemoteError as error:
                return self._release_external(
                    command,
                    "target_ref_unavailable",
                    str(error),
                    lease=lease,
                    release=release,
                )
            if live_target_sha != target_sha:
                return self._release_blocked(
                    command,
                    "release_target_changed",
                    (
                        f"target branch {release.target_branch!r} changed after "
                        "release verification"
                    ),
                    lease=lease,
                    release=release,
                )
            source_recheck = self._release_live_sources(
                command,
                release,
                target_ref,
            )
            if isinstance(source_recheck, CommandResult):
                return source_recheck
            try:
                if remote_release_sha is None:
                    self.git.push_branch(lease.worktree_path, lease.branch)
                elif remote_release_sha != lease.head_sha:
                    self.git.push_branch_if_at(
                        lease.worktree_path,
                        lease.branch,
                        remote_release_sha,
                    )
                github = self.github or GhClient(self.git.repository_root())
                target_pull_request = github.find_pr(
                    head=lease.branch,
                    base=release.target_branch,
                )
                if target_pull_request is None:
                    target_pull_request = github.create_pr(
                        base=release.target_branch,
                        head=lease.branch,
                        title=self._release_title(release),
                        body=self._release_body(release, target_sha, lease),
                    )
            except (GitRemoteError, ExternalServiceError) as error:
                return self._release_external(
                    command,
                    "release_publish_failed",
                    str(error),
                    lease=lease,
                    release=release,
                )
            except (GitError, ValueError) as error:
                return self._release_blocked(
                    command,
                    "release_publish_failed",
                    str(error),
                    lease=lease,
                    release=release,
                )
            if target_pull_request.state not in {"OPEN", "MERGED"}:
                return self._release_blocked(
                    command,
                    "release_pr_closed",
                    (
                        f"release PR #{target_pull_request.number} is "
                        f"{target_pull_request.state}"
                    ),
                    lease=lease,
                    release=release,
                )
            if (
                target_pull_request.base_ref != release.target_branch
                or target_pull_request.base_sha != target_sha
                or target_pull_request.head_ref != lease.branch
                or target_pull_request.head_sha != lease.head_sha
            ):
                return self._release_blocked(
                    command,
                    "target_pr_mismatch",
                    "GitHub did not return the exact release pull request",
                    lease=lease,
                    release=release,
                )
            try:
                lease, release = self.registry.publish_release(
                    release.id,
                    lease.id,
                    expected_release_version=release.version,
                    expected_lease_version=lease.version,
                    target_pr=target_pull_request.number,
                    head_sha=lease.head_sha,
                    target_base_sha=target_sha,
                )
            except (RuntimeError, ValueError, sqlite3.Error) as error:
                return self._release_blocked(
                    command,
                    "registry_conflict",
                    str(error),
                    lease=lease,
                    release=release,
                )
            return CommandResult.ok(
                command,
                decision="ready",
                lease=lease,
                release=release,
                actions=verification_actions,
            )


    def recover_promotion(
        self, lease_id: str, *, apply: bool = False
    ) -> CommandResult:
        if not isinstance(lease_id, str) or not lease_id:
            return self._recover_promotion_blocked(
                "invalid_lease", "lease must be a non-empty string"
            )
        if not apply:
            try:
                lease = self.registry.get_lease_read_only(lease_id)
            except sqlite3.Error as error:
                return self._recover_promotion_blocked(
                    "registry_conflict", str(error)
                )
            if lease is None:
                return self._recover_promotion_blocked(
                    "unknown_lease", f"lease {lease_id} does not exist"
                )
            preflight = self._recover_promotion_preflight(lease, apply=False)
            if isinstance(preflight, CommandResult):
                return preflight
            if preflight.reconciliation_head is not None:
                actions = (
                    {
                        "kind": "reconcile_promotion_head",
                        "lease_id": lease.id,
                        "head_sha": preflight.reconciliation_head,
                    },
                )
            else:
                actions = (
                    {
                        "kind": "stage_paths",
                        "lease_id": lease.id,
                        "paths": list(lease.conflicted_paths),
                        "dirty_paths": list(preflight.dirty_paths),
                    },
                    {
                        "kind": "amend_manual_resolution",
                        "lease_id": lease.id,
                        "path": str(lease.worktree_path),
                    },
                )
            return CommandResult.ok(
                "wt.recover-promotion",
                decision="preview",
                lease=lease,
                actions=actions,
            )

        lease: Lease | None = None
        try:
            repository_id = self.git.repository_id()
            with repository_lock(self.lock_dir / f"{repository_id}.lock"):
                lease = self.registry.get_lease(lease_id)
                if lease is None:
                    return self._recover_promotion_blocked(
                        "unknown_lease", f"lease {lease_id} does not exist"
                    )
                preflight = self._recover_promotion_preflight(lease, apply=True)
                if isinstance(preflight, CommandResult):
                    return preflight
                if preflight.reconciliation_head is not None:
                    reconciled = self.registry.transition(
                        lease.id,
                        LeaseState.BLOCKED,
                        expected_version=lease.version,
                        event_type="promotion_manual_resolution_amend_reconciled",
                        summary="manually reviewed amended resolution reconciled",
                        observed_head_sha=preflight.reconciliation_head,
                        head_sha=preflight.reconciliation_head,
                        clear_conflict_source_ordinal=True,
                        legacy_source_trailers=preflight.legacy_manual_message,
                        resolution_state=ResolutionState.MANUAL_REVIEWED,
                    )
                    return CommandResult.ok(
                        "wt.recover-promotion",
                        decision="reconciled",
                        lease=reconciled,
                        actions=(
                            {
                                "kind": "reconcile_promotion_head",
                                "lease_id": reconciled.id,
                                "head_sha": reconciled.head_sha,
                            },
                        ),
                    )

                target_branch = preflight.target_branch
                worktree = self._registered_worktree(lease)
                if (
                    worktree is None
                    or worktree.branch != lease.branch
                    or worktree.detached
                    or worktree.bare
                    or self.git.head_sha(lease.worktree_path) != lease.head_sha
                    or self.git.commit_parents(lease.head_sha)
                    != (lease.target_base_sha,)
                    or not self._manual_reviewed_promotion_message_matches(
                        lease,
                        target_branch,
                        self.git.commit_message(lease.worktree_path),
                        read_only=False,
                        allow_legacy_unpinned=preflight.legacy_manual_message,
                    )
                    or self.git.committed_diff_has_conflict_markers(
                        lease.worktree_path,
                        lease.target_base_sha,
                        lease.head_sha,
                    )
                ):
                    return self._recover_promotion_blocked(
                        "promotion_incomplete",
                        f"lease {lease.id} changed after recovery preflight",
                        lease=lease,
                    )
                self.git.stage_paths(lease.worktree_path, lease.conflicted_paths)
                staged_tree_sha = self.git.index_tree_sha(lease.worktree_path)
                if self.git.unmerged_paths(lease.worktree_path):
                    return self._recover_promotion_blocked(
                        "promotion_resolution_unmerged",
                        f"lease {lease.id} still has unmerged paths after staging",
                        lease=lease,
                    )
                if self.git.unstaged_paths(lease.worktree_path):
                    return self._recover_promotion_blocked(
                        "promotion_resolution_scope_mismatch",
                        f"lease {lease.id} has unstaged changes after staging",
                        lease=lease,
                    )
                if self.git.staged_diff_has_conflict_markers(lease.worktree_path):
                    return self._recover_promotion_blocked(
                        "promotion_incomplete",
                        f"lease {lease.id} staged resolution has conflict markers",
                        lease=lease,
                    )
                if not self._protected_index_entries_match(lease):
                    return self._recover_promotion_blocked(
                        "promotion_resolution_scope_mismatch",
                        f"lease {lease.id} protected reviewed paths changed",
                        lease=lease,
                    )
                promoted_paths = self.git.indexed_changed_paths(
                    lease.worktree_path, lease.target_base_sha
                )
                if not promoted_paths or not set(promoted_paths).issubset(
                    lease.reviewed_paths
                ):
                    return self._recover_promotion_blocked(
                        "promotion_resolution_scope_mismatch",
                        (
                            f"lease {lease.id} amended delta is not a non-empty "
                            "reviewed subset"
                        ),
                        lease=lease,
                    )
                promotion_head = self.git.amend_commit_no_edit(lease.worktree_path)
                if (
                    self.git.head_sha(lease.worktree_path) != promotion_head
                    or self.git.commit_tree_sha(
                        promotion_head, lease.worktree_path
                    )
                    != staged_tree_sha
                    or self.git.commit_parents(promotion_head)
                    != (lease.target_base_sha,)
                    or not self._manual_reviewed_promotion_message_matches(
                        lease,
                        target_branch,
                        self.git.commit_message(lease.worktree_path),
                        read_only=False,
                        allow_legacy_unpinned=preflight.legacy_manual_message,
                    )
                ):
                    return self._recover_promotion_blocked(
                        "promotion_incomplete",
                        f"lease {lease.id} amended resolution provenance changed",
                        lease=lease,
                    )
                promoted_paths = self.git.changed_paths(
                    lease.worktree_path,
                    lease.target_base_sha,
                    promotion_head,
                    find_renames=True,
                )
                if not promoted_paths or not set(promoted_paths).issubset(
                    lease.reviewed_paths
                ):
                    return self._recover_promotion_blocked(
                        "promotion_resolution_scope_mismatch",
                        (
                            f"lease {lease.id} amended delta is not a non-empty "
                            "reviewed subset"
                        ),
                        lease=lease,
                    )
                worktree = self._registered_worktree(lease)
                if (
                    worktree is None
                    or worktree.branch != lease.branch
                    or worktree.detached
                    or worktree.bare
                    or self.git.status_porcelain(lease.worktree_path)
                    or self.git.committed_diff_has_conflict_markers(
                        lease.worktree_path,
                        lease.target_base_sha,
                        promotion_head,
                    )
                    or not self._protected_index_entries_match(lease)
                ):
                    return self._recover_promotion_blocked(
                        "promotion_incomplete",
                        f"lease {lease.id} amended resolution did not remain clean",
                        lease=lease,
                    )
                publication_blocker = self._recover_promotion_publication_blocker(
                    lease, target_branch
                )
                if publication_blocker is not None:
                    return publication_blocker
                lease = self.registry.transition(
                    lease.id,
                    LeaseState.BLOCKED,
                    expected_version=lease.version,
                    event_type="promotion_manual_resolution_amended",
                    summary="manually reviewed conflict resolution amended",
                    observed_head_sha=promotion_head,
                    head_sha=promotion_head,
                    resolution_state=ResolutionState.MANUAL_REVIEWED,
                    clear_conflict_source_ordinal=True,
                    legacy_source_trailers=preflight.legacy_manual_message,
                )
        except GitRemoteError as error:
            return self._external_error(
                "wt.recover-promotion",
                "promotion_recovery_failed",
                str(error),
                lease=lease,
            )
        except (GitError, OSError, RuntimeError, sqlite3.Error, ValueError) as error:
            return self._recover_promotion_blocked(
                "promotion_recovery_failed", str(error), lease=lease
            )
        return CommandResult.ok(
            "wt.recover-promotion",
            decision="recovered",
            lease=lease,
            actions=(
                {
                    "kind": "stage_paths",
                    "lease_id": lease.id,
                    "paths": list(lease.conflicted_paths),
                },
                {
                    "kind": "amend_manual_resolution",
                    "lease_id": lease.id,
                    "path": str(lease.worktree_path),
                },
            ),
        )

    def discard_promotion(
        self, lease_id: str, *, apply: bool = False
    ) -> CommandResult:
        command = "wt.discard-promotion"
        if not isinstance(lease_id, str) or not lease_id:
            return self._discard_promotion_blocked(
                "invalid_lease", "lease must be a non-empty string"
            )
        repository_id = self.git.repository_id()
        if not apply:
            try:
                lease = self.registry.get_lease_read_only(lease_id)
            except sqlite3.Error as error:
                return self._discard_promotion_blocked("registry_conflict", str(error))
            if lease is None:
                return self._discard_promotion_blocked(
                    "unknown_lease", f"lease {lease_id} does not exist"
                )
            preflight = self._discard_promotion_preflight(
                lease, repository_id=repository_id
            )
            if preflight is not None:
                return preflight
            return CommandResult.ok(
                command,
                decision="preview",
                lease=lease,
                actions=(
                    self._cleanup_action("remove_worktree", lease),
                    self._cleanup_action("delete_local_branch", lease),
                ),
            )

        with repository_lock(self.lock_dir / f"{repository_id}.lock"):
            lease = self.registry.get_lease(lease_id)
            if lease is None:
                return self._discard_promotion_blocked(
                    "unknown_lease", f"lease {lease_id} does not exist"
                )
            try:
                existing_reservation = self.registry.get_cleanup_reservation(lease.id)
            except sqlite3.Error as error:
                return self._discard_promotion_blocked(
                    "registry_conflict", str(error), lease=lease
                )
            if existing_reservation is not None:
                recovery_preflight = self._discard_promotion_preflight(
                    lease,
                    repository_id=repository_id,
                    reservation=existing_reservation,
                    recovery=True,
                )
                if recovery_preflight is not None:
                    return recovery_preflight
                return self._recover_discard_promotion_reservation(
                    lease,
                    existing_reservation,
                    RuntimeError("recovering an interrupted discard promotion"),
                )
            preflight = self._discard_promotion_preflight(
                lease, repository_id=repository_id
            )
            if preflight is not None:
                return preflight
            try:
                reservation = self.registry.reserve_cleanup(
                    lease.id,
                    expected_version=lease.version,
                    branch_sha=lease.head_sha,
                )
            except (RuntimeError, sqlite3.Error) as error:
                return self._discard_promotion_blocked(
                    "registry_conflict",
                    f"Unable to reserve lease {lease.id} for cleanup: {error}",
                    lease=lease,
                )

            removal_error: GitError | OSError | None = None
            post_lock_result: CommandResult | None = None
            try:
                with self.git.hold_worktree_branch_if_at(
                    lease.worktree_path, lease.branch, reservation.branch_sha
                ):
                    reserved = self.registry.get_lease(lease.id)
                    if reserved is None:
                        post_lock_result = self._discard_promotion_blocked(
                            "lease_changed",
                            f"Lease {lease.id} changed while cleanup was reserved.",
                        )
                    else:
                        post_lock_result = self._discard_promotion_preflight(
                            reserved,
                            repository_id=repository_id,
                            reservation=reservation,
                        )
                    if post_lock_result is None:
                        try:
                            self.git.remove_worktree(lease.worktree_path)
                        except (GitError, OSError) as error:
                            removal_error = error
            except GitError as error:
                return self._release_discard_promotion_reservation(
                    lease,
                    reservation,
                    self._discard_promotion_blocked(
                        "branch_head_mismatch",
                        (
                            f"Branch {lease.branch!r} changed before cleanup lock: "
                            f"{error}"
                        ),
                        lease=lease,
                    ),
                )
            if post_lock_result is not None:
                return self._release_discard_promotion_reservation(
                    lease, reservation, post_lock_result
                )
            if removal_error is not None:
                return self._recover_discard_promotion_reservation(
                    lease, reservation, removal_error
                )
            return self._complete_discard_promotion_cleanup(lease, reservation)

    def discard_sync(self, lease_id: str, *, apply: bool = False) -> CommandResult:
        command = "wt.discard-sync"
        if not isinstance(lease_id, str) or not lease_id:
            return self._discard_sync_blocked(
                "invalid_lease", "lease must be a non-empty string"
            )
        try:
            source_branch = self.config.production_branch
            target_branch = self.config.default_base
            if source_branch is None or target_branch is None:
                raise ConfigError("sync branches are not configured")
            source_ref, target_ref = self._sync_refs(source_branch, target_branch)
        except ConfigError as error:
            return self._discard_sync_blocked("invalid_sync_branches", str(error))
        repository_id = self.git.repository_id()
        github = self.github or GhClient(self.git.repository_root())
        if not apply:
            try:
                lease = self.registry.get_lease_read_only(lease_id)
            except sqlite3.Error as error:
                return self._discard_sync_blocked("registry_conflict", str(error))
            if lease is None:
                return self._discard_sync_blocked(
                    "unknown_lease", f"lease {lease_id} does not exist"
                )
            preflight = self._discard_sync_preflight(
                lease,
                repository_id=repository_id,
                source_branch=source_branch,
                target_branch=target_branch,
                source_ref=source_ref,
                target_ref=target_ref,
                github=github,
            )
            if preflight is not None:
                return preflight
            return CommandResult.ok(
                command,
                decision="preview",
                lease=lease,
                actions=(
                    self._cleanup_action("remove_worktree", lease),
                    self._cleanup_action("delete_local_branch", lease),
                ),
            )

        with repository_lock(self.lock_dir / f"{repository_id}.lock"):
            lease = self.registry.get_lease(lease_id)
            if lease is None:
                return self._discard_sync_blocked(
                    "unknown_lease", f"lease {lease_id} does not exist"
                )
            try:
                existing_reservation = self.registry.get_cleanup_reservation(lease.id)
            except sqlite3.Error as error:
                return self._discard_sync_blocked(
                    "registry_conflict", str(error), lease=lease
                )
            if existing_reservation is not None:
                recovery_preflight = self._discard_sync_preflight(
                    lease,
                    repository_id=repository_id,
                    source_branch=source_branch,
                    target_branch=target_branch,
                    source_ref=source_ref,
                    target_ref=target_ref,
                    github=github,
                    reservation=existing_reservation,
                    recovery=True,
                )
                if recovery_preflight is not None:
                    return recovery_preflight
                return self._recover_discard_sync_reservation(
                    lease,
                    existing_reservation,
                    RuntimeError("recovering an interrupted discard sync"),
                    normalized_for_removal=False,
                )
            preflight = self._discard_sync_preflight(
                lease,
                repository_id=repository_id,
                source_branch=source_branch,
                target_branch=target_branch,
                source_ref=source_ref,
                target_ref=target_ref,
                github=github,
            )
            if preflight is not None:
                return preflight
            try:
                reservation = self.registry.reserve_cleanup(
                    lease.id,
                    expected_version=lease.version,
                    branch_sha=lease.head_sha,
                )
            except (RuntimeError, sqlite3.Error) as error:
                return self._discard_sync_blocked(
                    "registry_conflict",
                    f"Unable to reserve lease {lease.id} for cleanup: {error}",
                    lease=lease,
                )

            removal_error: GitError | OSError | None = None
            normalized_for_removal = False
            post_lock_result: CommandResult | None = None
            removal_lease: Lease | None = None
            try:
                with self.git.hold_worktree_branch_if_at(
                    lease.worktree_path, lease.branch, reservation.branch_sha
                ):
                    reserved = self.registry.get_lease(lease.id)
                    if reserved is None:
                        post_lock_result = self._discard_sync_blocked(
                            "lease_changed",
                            f"Lease {lease.id} changed while cleanup was reserved.",
                        )
                    else:
                        post_lock_result = self._discard_sync_preflight(
                            reserved,
                            repository_id=repository_id,
                            source_branch=source_branch,
                            target_branch=target_branch,
                            source_ref=source_ref,
                            target_ref=target_ref,
                            github=github,
                            reservation=reservation,
                        )
                        removal_lease = reserved
                    if post_lock_result is None and removal_lease is not None:
                        identity, identity_issue = (
                            self._discard_sync_worktree_identity(removal_lease)
                        )
                        if identity_issue is not None:
                            return self._discard_sync_blocked(
                                "cleanup_reserved",
                                (
                                    "Cleanup reservation retained because the "
                                    f"worktree cannot be safely normalized: "
                                    f"{identity_issue[0]}: {identity_issue[1]}"
                                ),
                                lease=removal_lease,
                            )
                        assert identity is not None
                        try:
                            assert removal_lease.target_base_sha is not None
                            self.git.restore_paths_to_ref(
                                removal_lease.worktree_path,
                                removal_lease.target_base_sha,
                                removal_lease.reviewed_paths,
                            )
                            normalized_for_removal = True
                            _, identity_issue = self._discard_sync_worktree_identity(
                                removal_lease, expected=identity
                            )
                            if identity_issue is not None:
                                return self._discard_sync_blocked(
                                    "cleanup_reserved",
                                    (
                                        "Cleanup reservation retained because the "
                                        f"worktree changed before removal: "
                                        f"{identity_issue[0]}: {identity_issue[1]}"
                                    ),
                                    lease=removal_lease,
                                )
                            self.git.remove_worktree(removal_lease.worktree_path)
                        except (GitError, OSError) as error:
                            removal_error = error
            except GitError as error:
                return self._release_discard_sync_reservation(
                    lease,
                    reservation,
                    self._discard_sync_blocked(
                        "branch_head_mismatch",
                        (
                            f"Branch {lease.branch!r} changed before cleanup lock: "
                            f"{error}"
                        ),
                        lease=lease,
                    ),
                )
            if post_lock_result is not None:
                return self._release_discard_sync_reservation(
                    lease, reservation, post_lock_result
                )
            if removal_error is not None:
                return self._recover_discard_sync_reservation(
                    lease,
                    reservation,
                    removal_error,
                    normalized_for_removal=normalized_for_removal,
                )
            return self._complete_discard_sync_cleanup(lease, reservation)

    def recover_sync(self, lease_id: str, *, apply: bool = False) -> CommandResult:
        command = "wt.recover-sync"
        if not isinstance(lease_id, str) or not lease_id:
            return self._recover_sync_blocked(
                "invalid_lease", "lease must be a non-empty string"
            )
        try:
            source_branch = self.config.production_branch
            target_branch = self.config.default_base
            if source_branch is None or target_branch is None:
                raise ConfigError("sync branches are not configured")
            source_ref, target_ref = self._sync_refs(source_branch, target_branch)
        except ConfigError as error:
            return self._recover_sync_blocked("invalid_sync_branches", str(error))

        if not self.config.verify_production:
            return self._recover_sync_blocked(
                "sync_verify_missing",
                "verify.production.commands must configure at least one command",
            )
        repository_id = self.git.repository_id()
        github = self.github or GhClient(self.git.repository_root())

        if not apply:
            try:
                lease = self.registry.get_lease_read_only(lease_id)
                expected = self._current_sync_lease(
                    source_branch=source_branch,
                    target_branch=target_branch,
                    source_ref=source_ref,
                    target_ref=target_ref,
                    fetch=False,
                )
            except GitError as error:
                return self._recover_sync_blocked("sync_ref_unavailable", str(error))
            except sqlite3.Error as error:
                return self._recover_sync_blocked("registry_conflict", str(error))
            if lease is None:
                return self._recover_sync_blocked(
                    "unknown_lease", f"lease {lease_id} does not exist"
                )
            preflight = self._recover_sync_preflight(
                lease,
                expected=expected,
                repository_id=repository_id,
                source_branch=source_branch,
                target_branch=target_branch,
                github=github,
            )
            if preflight is not None:
                return preflight
            return CommandResult.ok(
                command,
                decision="preview",
                lease=lease,
                actions=(
                    {
                        "kind": "resolve_sync_target_conflict",
                        "lease_id": lease.id,
                        "path": str(lease.worktree_path),
                        "paths": list(lease.conflicted_paths),
                    },
                    {
                        "kind": "stage_paths",
                        "lease_id": lease.id,
                        "paths": list(lease.conflicted_paths),
                    },
                    {
                        "kind": "commit_sync_resolution",
                        "lease_id": lease.id,
                        "parents": [
                            lease.target_base_sha,
                            lease.source_head_sha,
                        ],
                        "paths": list(lease.reviewed_paths),
                    },
                    *(
                        {
                            "kind": "verify_production",
                            "argv": list(verification_command),
                        }
                        for verification_command in self.config.verify_production
                    ),
                    {
                        "kind": "push_branch",
                        "lease_id": lease.id,
                        "branch": lease.branch,
                    },
                    {
                        "kind": "open_pull_request",
                        "lease_id": lease.id,
                        "base": target_branch,
                        "head": lease.branch,
                    },
                ),
            )

        with repository_lock(self.lock_dir / f"{repository_id}.lock"):
            try:
                lease = self.registry.get_lease(lease_id)
                expected = self._current_sync_lease(
                    source_branch=source_branch,
                    target_branch=target_branch,
                    source_ref=source_ref,
                    target_ref=target_ref,
                    fetch=True,
                )
            except GitRemoteError as error:
                return self._external_error(
                    command, "sync_ref_unavailable", str(error)
                )
            except (GitError, sqlite3.Error) as error:
                return self._recover_sync_blocked("sync_ref_unavailable", str(error))
            if lease is None:
                return self._recover_sync_blocked(
                    "unknown_lease", f"lease {lease_id} does not exist"
                )
            preflight = self._recover_sync_preflight(
                lease,
                expected=expected,
                repository_id=repository_id,
                source_branch=source_branch,
                target_branch=target_branch,
                github=github,
            )
            if preflight is not None:
                return preflight
            index_backup: GitIndexBackup | None = None
            try:
                index_backup = self.git.backup_index(lease.worktree_path)
                self.git.stage_paths(lease.worktree_path, lease.conflicted_paths)
                final_preflight = self._recover_sync_final_index_preflight(lease)
            except (GitError, OSError) as error:
                result = self._block_recovered_sync_lease(
                    lease, "sync_recovery_failed", str(error)
                )
                return (
                    self._restore_recovery_index(lease, index_backup, result)
                    if index_backup is not None
                    else result
                )
            if final_preflight is not None:
                return self._restore_recovery_index(
                    lease, index_backup, final_preflight
                )
            assert lease.source_base_sha is not None
            assert lease.source_head_sha is not None
            assert lease.target_base_sha is not None
            assert index_backup is not None
            committed = False
            try:
                sync_head = self.git.commit_index_as_merge(
                    lease.worktree_path,
                    self._sync_message(
                        source_branch=source_branch,
                        target_branch=target_branch,
                        merge_base=lease.source_base_sha,
                        source_sha=lease.source_head_sha,
                        target_sha=lease.target_base_sha,
                        lease=lease,
                    ),
                    branch=lease.branch,
                    target_parent=lease.target_base_sha,
                    source_parent=lease.source_head_sha,
                )
                committed = True
                self.git.discard_index_backup(index_backup)
                index_backup = None
                commit_preflight = self._recovered_sync_commit_preflight(
                    lease,
                    sync_head=sync_head,
                    source_branch=source_branch,
                    target_branch=target_branch,
                )
                if commit_preflight is not None:
                    return commit_preflight
                lease = self.registry.transition(
                    lease.id,
                    LeaseState.ACTIVE,
                    expected_version=lease.version,
                    event_type="sync_publish_pending",
                    summary="recovered synchronization commit verified; publication pending",
                    observed_head_sha=sync_head,
                    head_sha=sync_head,
                )
            except (GitError, OSError, RuntimeError, sqlite3.Error) as error:
                result = self._block_recovered_sync_lease(
                    lease, "sync_recovery_failed", str(error)
                )
                return (
                    self._restore_recovery_index(lease, index_backup, result)
                    if not committed and index_backup is not None
                    else result
                )

            prepare_error = self._prepare(lease, force=False)
            if prepare_error is not None:
                return self._block_recovered_sync_lease(
                    lease, "sync_prepare_failed", prepare_error
                )
            if self.git.status_porcelain(lease.worktree_path):
                return self._block_recovered_sync_lease(
                    lease,
                    "sync_prepare_dirty",
                    "sync prepare command left uncommitted changes",
                )
            try:
                verification_actions = self._verify_promotion(lease.worktree_path)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                return self._block_recovered_sync_lease(
                    lease, "sync_apply_failed", str(error)
                )
            drift_blocker = self._sync_remote_drift_blocker(
                lease=lease,
                source_branch=source_branch,
                target_branch=target_branch,
                source_sha=lease.source_head_sha,
                target_sha=lease.target_base_sha,
            )
            if drift_blocker is not None:
                return replace(drift_blocker, command=command)
            commit_preflight = self._recovered_sync_commit_preflight(
                lease,
                sync_head=sync_head,
                source_branch=source_branch,
                target_branch=target_branch,
            )
            if commit_preflight is not None:
                return commit_preflight
            try:
                self.git.push_branch_create_if_absent(
                    lease.worktree_path, lease.branch, sync_head
                )
                target_pull_request = github.find_open_pr(
                    head=lease.branch, base=target_branch
                )
                expected_body = self._sync_body(
                    source_branch=source_branch,
                    target_branch=target_branch,
                    merge_base=lease.source_base_sha,
                    source_sha=lease.source_head_sha,
                    target_sha=lease.target_base_sha,
                    lease=lease,
                )
                if target_pull_request is None:
                    target_pull_request = github.create_pr(
                        base=target_branch,
                        head=lease.branch,
                        title=f"Sync {source_branch} to {target_branch}",
                        body=expected_body,
                    )
            except (ExternalServiceError, GitRemoteError) as error:
                return self._external_error(
                    command, "sync_publish_failed", str(error), lease=lease
                )
            if (
                target_pull_request.state != "OPEN"
                or target_pull_request.base_ref != target_branch
                or target_pull_request.base_sha != lease.target_base_sha
                or target_pull_request.head_ref != lease.branch
                or target_pull_request.head_sha != sync_head
                or target_pull_request.body != expected_body
            ):
                return self._block_recovered_sync_lease(
                    lease,
                    "sync_pr_mismatch",
                    "GitHub did not return the exact open synchronization pull request",
                )
            drift_blocker = self._sync_remote_drift_blocker(
                lease=lease,
                source_branch=source_branch,
                target_branch=target_branch,
                source_sha=lease.source_head_sha,
                target_sha=lease.target_base_sha,
            )
            if drift_blocker is not None:
                return replace(drift_blocker, command=command)
            try:
                lease = self.registry.transition(
                    lease.id,
                    LeaseState.PR_OPEN,
                    expected_version=lease.version,
                    event_type="sync_pr_recovered",
                    summary=f"sync PR #{target_pull_request.number} recovered",
                    observed_head_sha=sync_head,
                    pr_number=target_pull_request.number,
                    head_sha=sync_head,
                )
            except (RuntimeError, sqlite3.Error) as error:
                return self._recover_sync_blocked(
                    "registry_conflict", str(error), lease=lease
                )
            return CommandResult.ok(
                command,
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

    def link_pr(
        self, lease_id: str, *, pr_number: int, apply: bool = False
    ) -> CommandResult:
        lease = self.registry.get_lease_read_only(lease_id)
        if lease is None:
            return self._managed_link_blocked(
                "unknown_lease", f"lease {lease_id} does not exist"
            )
        validated = self._validate_managed_pr_link(lease, pr_number)
        if isinstance(validated, CommandResult):
            return validated
        pull_request, current_head = validated
        if lease.target_pr == pr_number:
            return CommandResult.ok("wt.link-pr", decision="reuse", lease=lease)
        if not apply:
            return CommandResult.ok(
                "wt.link-pr",
                decision="preview",
                lease=lease,
                actions=(
                    {
                        "kind": "link_pr",
                        "lease_id": lease.id,
                        "path": str(lease.worktree_path),
                        "pr_number": pull_request.number,
                        "head_sha": current_head,
                    },
                ),
            )

        with repository_lock(self.lock_dir / f"{lease.repository_id}.lock"):
            current = self.registry.get_lease(lease.id)
            if current is None:
                return self._managed_link_blocked(
                    "unknown_lease", f"lease {lease.id} does not exist"
                )
            validated = self._validate_managed_pr_link(current, pr_number)
            if isinstance(validated, CommandResult):
                return validated
            pull_request, current_head = validated
            if current.target_pr == pr_number:
                return CommandResult.ok(
                    "wt.link-pr", decision="reuse", lease=current
                )
            try:
                linked = self.registry.transition(
                    current.id,
                    LeaseState.CLEANABLE,
                    expected_version=current.version,
                    event_type="managed_lease_pr_linked",
                    summary=(
                        "managed feature lease linked to pull request "
                        f"#{pull_request.number}"
                    ),
                    observed_head_sha=current_head,
                    pr_number=pull_request.number,
                    head_sha=current_head,
                    deployment_state=DeploymentState.NOT_REQUIRED,
                )
            except (RuntimeError, sqlite3.Error) as error:
                return CommandResult.error(
                    "wt.link-pr",
                    code="registry_conflict",
                    message=str(error),
                    exit_code=5,
                    lease=current,
                )
            stored = self.registry.get_lease(current.id)
            if (
                stored is None
                or stored.version != linked.version
                or stored.target_pr != pull_request.number
                or stored.head_sha != current_head
                or stored.state is not LeaseState.CLEANABLE
                or stored.deployment_state is not DeploymentState.NOT_REQUIRED
            ):
                return CommandResult.error(
                    "wt.link-pr",
                    code="registry_conflict",
                    message=f"unable to verify linked lease {current.id}",
                    exit_code=5,
                    lease=stored or linked,
                )
        return CommandResult.ok("wt.link-pr", decision="ready", lease=linked)

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
            except ExternalServiceError as error:
                return CommandResult.external_error(
                    "wt.gc",
                    code="github_refresh_failed",
                    message=(
                        f"Unable to refresh pull request state for lease {lease.id}: "
                        f"{error}"
                    ),
                    leases=tuple(processed),
                    actions=tuple(actions),
                    warnings=tuple(warnings),
                )
            except (KeyError, OSError, ValueError) as error:
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
            if result.status == "error":
                blocker = result.blockers[0]
                return CommandResult.external_error(
                    "wt.gc",
                    code=blocker["code"],
                    message=blocker["message"],
                    leases=tuple(processed),
                    actions=tuple(actions),
                    warnings=tuple(warnings),
                )
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

    def compact(
        self,
        *,
        lease_id: str | None,
        paths: Sequence[str],
        older_than: str,
        apply: bool = False,
    ) -> CommandResult:
        """Remove only proven-safe ignored paths while preserving every lease."""
        command = "wt.compact"
        try:
            normalized_paths = self._compact_paths(paths)
        except ValueError as error:
            return self._compact_blocked(command, "invalid_compact_path", str(error))
        try:
            threshold = self._parse_age_threshold(older_than)
        except ValueError as error:
            return self._compact_blocked(command, "invalid_age_threshold", str(error))
        try:
            repository_id = self.git.repository_id()
            repository_root = self.git.repository_root()
        except GitError as error:
            return self._compact_blocked(
                command, "repository_unavailable", str(error)
            )
        try:
            if lease_id is not None:
                lease = self.registry.get_lease_read_only(lease_id)
                if lease is None:
                    return self._compact_blocked(
                        command,
                        "unknown_lease",
                        f"lease {lease_id} does not exist",
                    )
                leases = (lease,)
            else:
                now = datetime.now(timezone.utc)
                leases = tuple(
                    lease
                    for lease in self.registry.list_leases_read_only(
                        include_removed=False,
                        repository_id=repository_id,
                    )
                    if lease.state
                    in {
                        LeaseState.PR_OPEN,
                        LeaseState.DEPLOYING,
                        LeaseState.DEPLOYED,
                        LeaseState.CLEANABLE,
                    }
                    and lease.managed
                    and lease.owner_kind == "awf"
                    and self._lease_is_older_than(lease, threshold, now)
                )
        except sqlite3.Error as error:
            return self._compact_blocked(command, "registry_conflict", str(error))

        plans, blockers = self._compact_candidates(
            leases,
            paths=normalized_paths,
            threshold=threshold,
            repository_id=repository_id,
            repository_root=repository_root,
        )
        actions = self._compact_actions(plans)
        if blockers:
            return CommandResult.blocked(
                command,
                blockers=tuple(blockers),
                leases=leases,
                actions=actions,
            )
        if not plans:
            return CommandResult.ok(command, decision="no_op")
        if not apply:
            return CommandResult.ok(
                command,
                decision="preview",
                leases=leases,
                actions=actions,
            )

        original_leases = {lease.id: lease for lease in leases}
        try:
            with repository_lock(
                self.lock_dir / f"{repository_id}.lock",
                blocking=False,
            ):
                try:
                    current_repository_id = self.git.repository_id()
                    current_repository_root = self.git.repository_root()
                except GitError as error:
                    return self._compact_blocked(
                        command,
                        "repository_unavailable",
                        str(error),
                        leases=leases,
                        actions=actions,
                    )
                if current_repository_id != repository_id:
                    return self._compact_blocked(
                        command,
                        "repository_changed",
                        "repository identity changed before compact could be applied",
                        leases=leases,
                        actions=actions,
                    )
                try:
                    if lease_id is not None:
                        current = self.registry.get_lease(lease_id)
                        current_leases = (current,) if current is not None else ()
                    else:
                        now = datetime.now(timezone.utc)
                        current_leases = tuple(
                            lease
                            for lease in self.registry.list_leases(
                                include_removed=False,
                                repository_id=repository_id,
                            )
                            if lease.state
                            in {
                                LeaseState.PR_OPEN,
                                LeaseState.DEPLOYING,
                                LeaseState.DEPLOYED,
                                LeaseState.CLEANABLE,
                            }
                            and lease.managed
                            and lease.owner_kind == "awf"
                            and self._lease_is_older_than(lease, threshold, now)
                        )
                except sqlite3.Error as error:
                    return self._compact_blocked(
                        command,
                        "registry_conflict",
                        str(error),
                        leases=leases,
                        actions=actions,
                    )
                if tuple(lease.id for lease in current_leases) != tuple(
                    original_leases
                ):
                    return self._compact_blocked(
                        command,
                        "candidate_set_changed",
                        "eligible compact leases changed before apply",
                        leases=leases,
                        actions=actions,
                    )
                if any(
                    current.version != original_leases[current.id].version
                    for current in current_leases
                ):
                    return self._compact_blocked(
                        command,
                        "lease_changed",
                        "a compact lease changed before apply",
                        leases=current_leases,
                        actions=actions,
                    )
                locked_plans, locked_blockers = self._compact_candidates(
                    current_leases,
                    paths=normalized_paths,
                    threshold=threshold,
                    repository_id=repository_id,
                    repository_root=current_repository_root,
                )
                locked_actions = self._compact_actions(locked_plans)
                if locked_blockers:
                    return CommandResult.blocked(
                        command,
                        blockers=tuple(locked_blockers),
                        leases=current_leases,
                        actions=locked_actions,
                    )
                completed_actions: list[dict[str, object]] = []
                action_lookup = {
                    (action["lease_id"], action["path"]): action
                    for action in locked_actions
                }
                try:
                    for plan in locked_plans:
                        self._prepare_marker_path(plan.lease.id).unlink(
                            missing_ok=True
                        )
                        for path, _usage in plan.usages:
                            self.git.remove_ignored_path(
                                plan.lease.worktree_path,
                                path,
                            )
                            completed_actions.append(
                                action_lookup[(plan.lease.id, path)]
                            )
                except (GitError, OSError) as error:
                    return self._compact_blocked(
                        command,
                        "compact_remove_failed",
                        str(error),
                        leases=current_leases,
                        actions=tuple(completed_actions),
                    )
        except OSError as error:
            return self._compact_blocked(
                command,
                "repository_locked",
                f"repository lock is unavailable: {error}",
                leases=leases,
                actions=actions,
            )
        return CommandResult.ok(
            command,
            decision="removed",
            leases=tuple(plan.lease for plan in locked_plans),
            actions=locked_actions,
        )

    def _compact_candidates(
        self,
        leases: Sequence[Lease],
        *,
        paths: tuple[str, ...],
        threshold: timedelta,
        repository_id: str,
        repository_root: Path,
    ) -> tuple[list[_CompactCandidate], list[dict[str, str]]]:
        try:
            worktrees = self.git.list_worktrees()
        except GitError as error:
            return [], [
                {
                    "code": "worktree_inspection_failed",
                    "message": f"Unable to inspect registered worktrees: {error}",
                }
            ]
        now = datetime.now(timezone.utc)
        plans: list[_CompactCandidate] = []
        blockers: list[dict[str, str]] = []
        for lease in leases:
            candidate, candidate_blockers = self._compact_candidate(
                lease,
                paths=paths,
                threshold=threshold,
                now=now,
                repository_id=repository_id,
                repository_root=repository_root,
                worktrees=worktrees,
            )
            blockers.extend(candidate_blockers)
            if candidate is not None:
                plans.append(candidate)
        return plans, blockers

    def _compact_candidate(
        self,
        lease: Lease,
        *,
        paths: tuple[str, ...],
        threshold: timedelta,
        now: datetime,
        repository_id: str,
        repository_root: Path,
        worktrees: tuple[GitWorktree, ...],
    ) -> tuple[_CompactCandidate | None, list[dict[str, str]]]:
        blockers: list[dict[str, str]] = []
        allowed_states = {
            LeaseState.PR_OPEN,
            LeaseState.DEPLOYING,
            LeaseState.DEPLOYED,
            LeaseState.CLEANABLE,
        }
        if lease.state not in allowed_states:
            blockers.append(
                self._compact_lease_blocker(
                    lease,
                    "ineligible_state",
                    f"state {lease.state.value} is not eligible for compact",
                )
            )
        if not lease.managed or lease.owner_kind != "awf":
            blockers.append(
                self._compact_lease_blocker(
                    lease,
                    "unmanaged_lease",
                    "compact requires an AWF-managed lease",
                )
            )
        if (
            lease.repository_id != repository_id
            or lease.repository_root != repository_root
        ):
            blockers.append(
                self._compact_lease_blocker(
                    lease,
                    "repository_mismatch",
                    "lease does not belong to this exact repository",
                )
            )
        if not self._lease_is_older_than(lease, threshold, now):
            blockers.append(
                self._compact_lease_blocker(
                    lease,
                    "age_threshold_not_met",
                    "lease has not reached the requested compact age threshold",
                )
            )
        try:
            reservation = self.registry.get_cleanup_reservation(lease.id)
        except sqlite3.Error as error:
            blockers.append(
                self._compact_lease_blocker(
                    lease,
                    "registry_conflict",
                    f"unable to inspect cleanup reservation: {error}",
                )
            )
        else:
            if reservation is not None:
                blockers.append(
                    self._compact_lease_blocker(
                        lease,
                        "cleanup_reserved",
                        "lease is reserved for cleanup",
                    )
                )
        registered = tuple(
            worktree
            for worktree in worktrees
            if worktree.path.resolve() == lease.worktree_path
        )
        if len(registered) != 1 or not lease.worktree_path.is_dir():
            blockers.append(
                self._compact_lease_blocker(
                    lease,
                    "unregistered_worktree",
                    "lease worktree is not registered at its expected path",
                )
            )
        elif (
            registered[0].bare
            or registered[0].detached
            or registered[0].branch != lease.branch
        ):
            blockers.append(
                self._compact_lease_blocker(
                    lease,
                    "branch_mismatch",
                    "lease is not checked out on its registered non-detached branch",
                )
            )
        if blockers:
            return None, blockers
        for path in paths:
            location_blocker = self._compact_path_location_blocker(lease, path)
            if location_blocker is not None:
                blockers.append(location_blocker)
        if blockers:
            return None, blockers
        try:
            if self.git.status_porcelain(lease.worktree_path):
                blockers.append(
                    self._compact_lease_blocker(
                        lease,
                        "dirty_worktree",
                        "lease has uncommitted changes",
                    )
                )
            actual_head = self.git.head_sha(lease.worktree_path)
        except GitError as error:
            blockers.append(
                self._compact_lease_blocker(
                    lease,
                    "worktree_inspection_failed",
                    str(error),
                )
            )
            return None, blockers
        if (
            actual_head != lease.head_sha
            or registered[0].head_sha != lease.head_sha
        ):
            blockers.append(
                self._compact_lease_blocker(
                    lease,
                    "head_mismatch",
                    "registered worktree HEAD does not exactly match the lease",
                )
            )
        if blockers:
            return None, blockers

        usages: list[tuple[str, GitPathUsage]] = []
        for path in paths:
            try:
                if not self.git.path_is_ignored(lease.worktree_path, path):
                    blockers.append(
                        self._compact_lease_blocker(
                            lease,
                            "compact_path_not_ignored",
                            f"path {path!r} is not ignored",
                        )
                    )
                    continue
                tracked_paths = self.git.tracked_paths(lease.worktree_path, path)
                if tracked_paths:
                    blockers.append(
                        self._compact_lease_blocker(
                            lease,
                            "tracked_descendants",
                            f"path {path!r} contains tracked descendants",
                        )
                    )
                    continue
                usages.append(
                    (path, self.git.compact_path_usage(lease.worktree_path, path))
                )
            except GitError as error:
                blockers.append(
                    self._compact_lease_blocker(
                        lease,
                        "worktree_inspection_failed",
                        str(error),
                    )
                )
        if blockers:
            return None, blockers
        return _CompactCandidate(lease=lease, usages=tuple(usages)), blockers

    @staticmethod
    def _compact_paths(paths: Sequence[str]) -> tuple[str, ...]:
        error_message = (
            "paths must be a non-empty sequence of unique normalized "
            "repository-relative paths"
        )
        if isinstance(paths, (str, bytes)) or not isinstance(paths, Sequence):
            raise ValueError(error_message)
        values = tuple(paths)
        if not values or any(not isinstance(path, str) for path in values):
            raise ValueError(error_message)
        if len(set(values)) != len(values):
            raise ValueError(error_message)
        parsed_values = tuple(PurePosixPath(path) for path in values)
        for path, parsed in zip(values, parsed_values, strict=True):
            if (
                not path
                or "\0" in path
                or path.splitlines() != [path]
                or parsed.is_absolute()
                or ".." in parsed.parts
                or str(parsed) != path
                or not parsed.parts
                or any(part.casefold() == ".git" for part in parsed.parts)
            ):
                raise ValueError(error_message)
        canonical_parts = tuple(
            tuple(part.casefold() for part in parsed.parts)
            for parsed in parsed_values
        )
        if len(set(canonical_parts)) != len(canonical_parts):
            raise ValueError(
                "compact paths must be unique on case-insensitive filesystems"
            )
        for parent_parts in canonical_parts:
            for child_parts in canonical_parts:
                if (
                    len(parent_parts) < len(child_parts)
                    and child_parts[: len(parent_parts)] == parent_parts
                ):
                    raise ValueError(
                        "compact paths must not overlap or contain one another"
                    )
        return tuple(sorted(values))

    @staticmethod
    def _compact_actions(
        candidates: Sequence[_CompactCandidate],
    ) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "kind": "remove_ignored_path",
                "lease_id": candidate.lease.id,
                "worktree_path": str(candidate.lease.worktree_path),
                "path": path,
                "bytes": usage.allocated_bytes,
                "entry_count": usage.entry_count,
            }
            for candidate in candidates
            for path, usage in candidate.usages
        )

    @staticmethod
    def _compact_lease_blocker(
        lease: Lease,
        code: str,
        message: str,
    ) -> dict[str, str]:
        return {"code": code, "message": f"Lease {lease.id}: {message}"}

    @staticmethod
    def _compact_blocked(
        command: str,
        code: str,
        message: str,
        *,
        leases: Sequence[Lease] = (),
        actions: tuple[dict[str, object], ...] = (),
    ) -> CommandResult:
        return CommandResult.blocked(
            command,
            blockers=({"code": code, "message": message},),
            leases=tuple(leases),
            actions=actions,
        )

    def _compact_path_location_blocker(
        self,
        lease: Lease,
        path: str,
    ) -> dict[str, str] | None:
        root = lease.worktree_path
        current = root
        try:
            root.lstat()
            if root.is_symlink():
                return self._compact_lease_blocker(
                    lease,
                    "unsafe_compact_path",
                    "worktree root is symlinked",
                )
            for part in PurePosixPath(path).parts:
                current /= part
                current.lstat()
                if current.is_symlink():
                    return self._compact_lease_blocker(
                        lease,
                        "unsafe_compact_path",
                        f"path {path!r} has a symlinked ancestor or leaf",
                    )
            current.resolve(strict=True).relative_to(root.resolve(strict=True))
        except FileNotFoundError:
            return self._compact_lease_blocker(
                lease,
                "compact_path_missing",
                f"path {path!r} does not exist",
            )
        except ValueError:
            return self._compact_lease_blocker(
                lease,
                "unsafe_compact_path",
                f"path {path!r} escapes the worktree",
            )
        except OSError as error:
            return self._compact_lease_blocker(
                lease,
                "unsafe_compact_path",
                f"path {path!r} could not be safely inspected: {error}",
            )
        return None

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
            except ExternalServiceError as error:
                return self._external_error(
                    "wt.finish",
                    "github_refresh_failed",
                    f"Unable to refresh pull request state: {error}",
                    lease=lease,
                )
            except (KeyError, OSError, ValueError) as error:
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
            except ExternalServiceError as error:
                return self._external_error(
                    "wt.finish",
                    "github_refresh_failed",
                    f"Unable to refresh pull request state: {error}",
                    lease=current,
                    warnings=warnings,
                )
            except (KeyError, OSError, ValueError) as error:
                return self._cleanup_blocked(
                    "wt.finish",
                    "github_refresh_failed",
                    f"Unable to refresh pull request state: {error}",
                    lease=current,
                    warnings=warnings,
                )
            blockers = self._cleanup_blockers(
                current, pull_request, include_deployment=False
            )
            if blockers:
                return CommandResult.blocked(
                    "wt.finish",
                    blockers=blockers,
                    lease=current,
                    warnings=tuple(warnings),
                )
            forced, forced_probe, subject_revision = self._force_cleanup_deployment_probe(
                current, pull_request, warnings
            )
            if forced is None:
                return CommandResult.blocked(
                    "wt.finish",
                    blockers=(self._deployment_evidence_blocker(current, forced_probe),),
                    lease=self.registry.get_lease(current.id) or current,
                    warnings=tuple(warnings),
                )
            current = forced
            try:
                pull_request = (self.github or GhClient(current.repository_root)).view_pr(
                    current.target_pr
                )
            except ExternalServiceError as error:
                return self._external_error(
                    "wt.finish",
                    "github_refresh_failed",
                    f"Unable to revalidate pull request state: {error}",
                    lease=current,
                    warnings=warnings,
                )
            except (KeyError, OSError, ValueError) as error:
                return self._cleanup_blocked(
                    "wt.finish",
                    "github_refresh_failed",
                    f"Unable to revalidate pull request state: {error}",
                    lease=current,
                    warnings=warnings,
                )
            if not self._matches_deployment_subject(pull_request, subject_revision):
                return self._cleanup_blocked(
                    "wt.finish",
                    "deployment_subject_changed",
                    (
                        f"Pull request #{current.target_pr} merge revision changed after "
                        "fresh deployment evidence."
                    ),
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
            post_lock_external: tuple[str, str] | None = None
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
                        except ExternalServiceError as error:
                            post_lock_external = (
                                "github_refresh_failed",
                                f"Unable to revalidate pull request state: {error}",
                            )
                        except (KeyError, OSError, ValueError) as error:
                            post_lock_code = "github_refresh_failed"
                            post_lock_message = (
                                f"Unable to revalidate pull request state: {error}"
                            )
                        else:
                            if not self._matches_deployment_subject(
                                pull_request, subject_revision
                            ):
                                post_lock_blockers = (
                                    {
                                        "code": "deployment_subject_changed",
                                        "message": (
                                            f"Pull request #{reserved_current.target_pr} "
                                            "merge revision changed after fresh "
                                            "deployment evidence."
                                        ),
                                    },
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
            if post_lock_external is not None:
                return self._release_cleanup_reservation(
                    current,
                    reservation,
                    warnings,
                    external_code=post_lock_external[0],
                    external_message=post_lock_external[1],
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
            remote_error = self._cleanup_branches(
                removed, reservation.branch_sha, actions, warnings
            )
            if remote_error is not None:
                return CommandResult.external_error(
                    "wt.finish",
                    code="remote_branch_cleanup_failed",
                    message=(
                        f"Could not delete remote branch {removed.branch!r}: {remote_error}"
                    ),
                    lease=removed,
                    actions=tuple(actions),
                    warnings=tuple(warnings),
                )
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
        external_code: str | None = None,
        external_message: str | None = None,
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
        if external_code is not None:
            return CommandResult.external_error(
                "wt.finish",
                code=external_code,
                message=external_message or external_code,
                lease=released,
                warnings=tuple(warnings),
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
    ) -> tuple[Lease | None, EvidenceProbeResult, str | None]:
        if lease.purpose is not Purpose.PROMOTE:
            return lease, EvidenceProbeResult(None, datetime.now(timezone.utc)), None
        probe, adapter_digest = self._probe_deployment(lease, pull_request)
        evidence = (
            probe.response.registry_evidence(
                adapter_digest=adapter_digest, received_at=probe.received_at
            )
            if probe.response is not None and adapter_digest is not None
            else None
        )
        if probe.status == "healthy":
            state = LeaseState.CLEANABLE
            deployment_state = DeploymentState.HEALTHY
            summary = "Fresh deployment evidence is healthy"
        elif probe.status == "failed":
            state = LeaseState.BLOCKED
            deployment_state = DeploymentState.FAILED
            summary = "Fresh deployment evidence reports failed"
        elif probe.status == "pending":
            state = LeaseState.DEPLOYING
            deployment_state = DeploymentState.PENDING
            summary = "Fresh deployment evidence reports pending"
        else:
            state = LeaseState.BLOCKED
            deployment_state = DeploymentState.UNKNOWN
            summary = "Fresh deployment evidence is unavailable or inconclusive"
        recorded = self._record_cleanup_deployment_probe(
            lease,
            pull_request,
            state=state,
            deployment_state=deployment_state,
            summary=summary,
            evidence=evidence,
            warnings=warnings,
        )
        if probe.status != "healthy":
            return None, probe, None
        if recorded is None or probe.response is None:
            return (
                None,
                EvidenceProbeResult(
                    None, probe.received_at, "deployment_probe_record_failed"
                ),
                None,
            )
        return recorded, probe, probe.response.subject_revision

    @staticmethod
    def _matches_deployment_subject(
        pull_request: PullRequest, subject_revision: str | None
    ) -> bool:
        return (
            subject_revision is None
            or pull_request.merge_commit_sha == subject_revision
        )

    @staticmethod
    def _deployment_evidence_blocker(
        lease: Lease, probe: EvidenceProbeResult
    ) -> dict[str, str]:
        if probe.status == "failed":
            return {
                "code": "deployment_evidence_failed",
                "message": (
                    f"Fresh deployment evidence reported failed for promotion lease "
                    f"{lease.id}."
                ),
            }
        if probe.status == "pending":
            return {
                "code": "deployment_evidence_pending",
                "message": (
                    f"Fresh deployment evidence is pending for promotion lease "
                    f"{lease.id}."
                ),
            }
        return {
            "code": "deployment_evidence_unknown",
            "message": (
                f"Fresh deployment evidence is unknown or unavailable for promotion "
                f"lease {lease.id}."
            ),
        }

    @staticmethod
    def _deployment_state_blocker(lease: Lease) -> dict[str, str]:
        status = {
            DeploymentState.FAILED: "failed",
            DeploymentState.PENDING: "pending",
        }.get(lease.deployment_state, "unknown")
        return WorktreeService._deployment_evidence_blocker(
            lease, EvidenceProbeResult(None, datetime.now(timezone.utc), status)
        )

    def _probe_deployment(
        self, lease: Lease, pull_request: PullRequest
    ) -> tuple[EvidenceProbeResult, str | None]:
        received_at = datetime.now(timezone.utc)
        subject_revision = pull_request.merge_commit_sha
        if (
            not isinstance(subject_revision, str)
            or _GIT_OBJECT_ID.fullmatch(subject_revision) is None
            or _GIT_OBJECT_ID.fullmatch(pull_request.head_sha) is None
        ):
            return (
                EvidenceProbeResult(
                    None, received_at, "deployment_subject_revision_invalid"
                ),
                None,
            )
        try:
            adapter = load_deployment_adapter(
                lease.repository_id, home_dir=self.home_dir
            )
        except ConfigError:
            return (
                EvidenceProbeResult(
                    None, received_at, "deployment_adapter_configuration_invalid"
                ),
                None,
            )
        if adapter is None:
            return (
                EvidenceProbeResult(
                    None, received_at, "deployment_adapter_not_configured"
                ),
                None,
            )
        try:
            request = DeploymentEvidenceRequest.create(
                repository_id=lease.repository_id,
                pull_request_number=pull_request.number,
                source_head_sha=pull_request.head_sha,
                subject_revision=subject_revision,
            )
        except ValueError:
            return (
                EvidenceProbeResult(None, received_at, "deployment_request_invalid"),
                None,
            )
        result = self.evidence_executor.execute(adapter, request)
        if result.response is None:
            return result, adapter.config_digest
        try:
            refreshed = (
                self.github or GhClient(lease.repository_root)
            ).view_pr(pull_request.number)
        except (ExternalServiceError, KeyError, OSError, ValueError):
            return (
                EvidenceProbeResult(
                    None, result.received_at, "deployment_pr_revalidation_failed"
                ),
                adapter.config_digest,
            )
        if (
            refreshed.number != pull_request.number
            or refreshed.head_sha != pull_request.head_sha
            or refreshed.merge_commit_sha != subject_revision
        ):
            return (
                EvidenceProbeResult(
                    None, result.received_at, "deployment_subject_changed"
                ),
                adapter.config_digest,
            )
        return result, adapter.config_digest

    def _record_cleanup_deployment_probe(
        self,
        lease: Lease,
        pull_request: PullRequest,
        *,
        state: LeaseState,
        deployment_state: DeploymentState,
        summary: str,
        evidence: Mapping[str, str] | None,
        warnings: list[dict[str, str]],
    ) -> Lease | None:
        try:
            updated = self.registry.transition(
                lease.id,
                state,
                expected_version=lease.version,
                event_type="deployment_evidence",
                summary=summary,
                observed_head_sha=pull_request.head_sha,
                pr_number=pull_request.number,
                deployment_state=deployment_state,
                evidence=evidence,
            )
        except (RuntimeError, sqlite3.Error, ValueError):
            warnings.append(
                {
                    "code": "deployment_probe_record_failed",
                    "message": (
                        f"Unable to record fresh deployment evidence for lease {lease.id}."
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
                        f"Unable to revalidate fresh deployment evidence for lease "
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
            remote_error = self._cleanup_branches(
                removed, reservation.branch_sha, actions, warnings
            )
            if remote_error is not None:
                return CommandResult.external_error(
                    "wt.finish",
                    code="remote_branch_cleanup_failed",
                    message=(
                        f"Could not delete remote branch {removed.branch!r}: {remote_error}"
                    ),
                    lease=removed,
                    actions=tuple(actions),
                    warnings=tuple(warnings),
                )
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

    @staticmethod
    def _is_completed_pr(pull_request: PullRequest) -> bool:
        return pull_request.state == "MERGED" or (
            pull_request.state == "CLOSED"
            and pull_request.merge_commit_sha is not None
        )

    @staticmethod
    def _is_pr_adopted_import(lease: Lease) -> bool:
        return (
            lease.managed
            and lease.owner_kind == "imported"
            and lease.target_pr is not None
        )

    def _is_cleanup_managed(self, lease: Lease) -> bool:
        return (
            lease.managed
            and (
                lease.owner_kind == "awf"
                or self._is_pr_adopted_import(lease)
            )
        )

    def _cleanup_blockers(
        self,
        lease: Lease,
        pull_request: PullRequest,
        *,
        include_deployment: bool = True,
    ) -> tuple[dict[str, str], ...]:
        blockers: list[dict[str, str]] = []
        if not self._is_cleanup_managed(lease):
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
        if not self._is_completed_pr(pull_request):
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
        if (
            include_deployment
            and lease.purpose is Purpose.PROMOTE
            and lease.deployment_state is not DeploymentState.HEALTHY
        ):
            blockers.append(self._deployment_state_blocker(lease))
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
        if (
            not self._is_pr_adopted_import(lease)
            and lease.worktree_path != expected
        ):
            return {
                "code": "unsafe_worktree_path",
                "message": f"Lease {lease.id} does not use its managed cache path.",
            }
        worktree_path = lease.worktree_path
        current = Path(worktree_path.anchor)
        for part in worktree_path.parts[1:]:
            current /= part
            try:
                current.lstat()
            except FileNotFoundError:
                break
            except OSError:
                return {
                    "code": "unsafe_worktree_path",
                    "message": f"Lease {lease.id} worktree path could not be inspected.",
                }
            if current.is_symlink():
                return {
                    "code": "unsafe_worktree_path",
                    "message": f"Lease {lease.id} has a symlinked worktree path.",
                }
        return None

    def _discard_sync_worktree_identity(
        self,
        lease: Lease,
        *,
        expected: _WorktreeIdentity | None = None,
    ) -> tuple[_WorktreeIdentity | None, tuple[str, str] | None]:
        path_blocker = self._cleanup_path_blocker(lease)
        if path_blocker is not None:
            return None, (path_blocker["code"], path_blocker["message"])
        try:
            path_status = lease.worktree_path.lstat()
        except OSError as error:
            return None, (
                "worktree_inspection_failed",
                f"Lease {lease.id} worktree root could not be inspected: {error}",
            )
        if stat.S_ISLNK(path_status.st_mode):
            return None, (
                "unsafe_worktree_path",
                f"Lease {lease.id} has a symlinked worktree root.",
            )
        if not stat.S_ISDIR(path_status.st_mode):
            return None, (
                "unregistered_worktree",
                f"Lease {lease.id} worktree root is not a directory.",
            )
        identity = _WorktreeIdentity(
            device=path_status.st_dev,
            inode=path_status.st_ino,
        )
        if expected is not None and identity != expected:
            return None, (
                "worktree_identity_changed",
                f"Lease {lease.id} worktree root changed during cleanup.",
            )
        try:
            worktrees = self.git.list_worktrees()
        except (GitError, OSError) as error:
            return None, ("worktree_inspection_failed", str(error))
        registered = tuple(
            worktree
            for worktree in worktrees
            if worktree.path == lease.worktree_path
        )
        if (
            len(registered) != 1
            or registered[0].branch != lease.branch
            or registered[0].bare
            or registered[0].detached
        ):
            return None, (
                "unregistered_worktree",
                f"Lease {lease.id} is not registered at its expected worktree root.",
            )
        return identity, None

    def _discard_promotion_preflight(
        self,
        lease: Lease,
        *,
        repository_id: str,
        reservation: CleanupReservation | None = None,
        recovery: bool = False,
    ) -> CommandResult | None:
        blockers: list[dict[str, str]] = []
        if lease.repository_id != repository_id:
            blockers.append(
                {
                    "code": "repository_mismatch",
                    "message": f"Lease {lease.id} belongs to a different repository.",
                }
            )
        if not lease.managed or lease.owner_kind != "awf":
            blockers.append(
                {
                    "code": "unmanaged_lease",
                    "message": f"Lease {lease.id} is not managed by AWF.",
                }
            )
        if lease.purpose is not Purpose.PROMOTE:
            blockers.append(
                {
                    "code": "not_promotion_lease",
                    "message": f"Lease {lease.id} is not a promotion lease.",
                }
            )
        if lease.promotion_mode is not PromotionMode.EXACT:
            blockers.append(
                {
                    "code": "not_exact_promotion",
                    "message": f"Lease {lease.id} is not an exact promotion.",
                }
            )
        if lease.state is not LeaseState.BLOCKED:
            blockers.append(
                {
                    "code": "lease_not_blocked",
                    "message": f"Lease {lease.id} is not blocked.",
                }
            )
        if lease.resolution_state is not ResolutionState.NONE:
            blockers.append(
                {
                    "code": "promotion_resolution_present",
                    "message": f"Lease {lease.id} has a promotion resolution state.",
                }
            )
        if lease.target_pr is not None:
            blockers.append(
                {
                    "code": "target_pr_present",
                    "message": f"Lease {lease.id} is linked to a pull request.",
                }
            )
        if lease.conflicted_paths:
            blockers.append(
                {
                    "code": "promotion_conflicts_present",
                    "message": f"Lease {lease.id} has conflicted paths.",
                }
            )
        if lease.protected_index_entries:
            blockers.append(
                {
                    "code": "protected_index_entries_present",
                    "message": f"Lease {lease.id} has protected index entries.",
                }
            )
        if (
            lease.target_base_sha is not None
            and lease.target_base_sha != lease.head_sha
        ):
            blockers.append(
                {
                    "code": "target_base_mismatch",
                    "message": (
                        f"Lease {lease.id} head does not match its recorded target base."
                    ),
                }
            )
        if not lease.branch.startswith("awf/"):
            blockers.append(
                {
                    "code": "unsafe_branch",
                    "message": f"Lease {lease.id} does not use an AWF branch.",
                }
            )
        if blockers:
            return CommandResult.blocked(
                "wt.discard-promotion", blockers=tuple(blockers), lease=lease
            )
        if lease.target_base_sha is None:
            try:
                live_base_sha = self.git.resolve_ref(lease.base_ref)
                legacy_base = self.git.merge_base(lease.head_sha, live_base_sha)
            except GitRemoteError as error:
                return self._external_error(
                    "wt.discard-promotion",
                    "legacy_target_base_unavailable",
                    (
                        "Unable to prove the legacy target base for lease "
                        f"{lease.id}: {error}"
                    ),
                    lease=lease,
                )
            except (GitError, OSError) as error:
                return self._discard_promotion_blocked(
                    "legacy_target_base_unavailable",
                    (
                        "Unable to prove the legacy target base for lease "
                        f"{lease.id}: {error}"
                    ),
                    lease=lease,
                )
            if legacy_base != lease.head_sha:
                return self._discard_promotion_blocked(
                    "legacy_target_base_mismatch",
                    (
                        f"Lease {lease.id} HEAD is not an ancestor of its current "
                        "target base."
                    ),
                    lease=lease,
                )

        try:
            active_reservation = self.registry.get_cleanup_reservation(lease.id)
            events = self.registry.list_events_read_only(lease.id)
        except sqlite3.Error as error:
            return self._discard_promotion_blocked(
                "registry_conflict", str(error), lease=lease
            )
        if reservation is None:
            if active_reservation is not None:
                return self._discard_promotion_blocked(
                    "cleanup_reserved",
                    f"Lease {lease.id} is already reserved for cleanup.",
                    lease=lease,
                )
        elif (
            active_reservation != reservation
            or lease.version != reservation.reserved_version
            or not events
            or events[-1].event_type != "cleanup_reserved"
            or events[-1].observed_head_sha != reservation.branch_sha
        ):
            return self._discard_promotion_blocked(
                "lease_changed",
                f"Lease {lease.id} changed while cleanup was reserved.",
                lease=lease,
            )
        if not self._has_discardable_promotion_failure(events):
            return self._discard_promotion_blocked(
                "promotion_failure_not_recorded",
                (
                    f"Lease {lease.id} was not most recently blocked by an exact "
                    "promotion apply failure."
                ),
                lease=lease,
            )

        path_blocker = self._cleanup_path_blocker(lease)
        if path_blocker is not None:
            return CommandResult.blocked(
                "wt.discard-promotion", blockers=(path_blocker,), lease=lease
            )
        try:
            remote_head = self.git.remote_branch_sha(lease.branch)
        except GitRemoteError as error:
            return self._external_error(
                "wt.discard-promotion",
                "remote_branch_inspection_failed",
                f"Unable to inspect remote branch {lease.branch!r}: {error}",
                lease=lease,
            )
        except (GitError, OSError) as error:
            return self._discard_promotion_blocked(
                "remote_branch_inspection_failed",
                f"Unable to inspect remote branch {lease.branch!r}: {error}",
                lease=lease,
            )
        if remote_head is not None:
            return self._discard_promotion_blocked(
                "remote_branch_present",
                f"Lease {lease.id} still has a remote branch.",
                lease=lease,
            )
        if recovery:
            return None
        try:
            worktrees = self.git.list_worktrees()
        except (GitError, OSError) as error:
            return self._discard_promotion_blocked(
                "worktree_inspection_failed",
                f"Unable to inspect registered worktrees: {error}",
                lease=lease,
            )
        registered = tuple(
            worktree
            for worktree in worktrees
            if worktree.path == lease.worktree_path
        )
        if len(registered) != 1 or not lease.worktree_path.is_dir():
            return self._discard_promotion_blocked(
                "unregistered_worktree",
                f"Lease {lease.id} is not registered at its expected path.",
                lease=lease,
            )
        worktree = registered[0]
        if (
            worktree.branch != lease.branch
            or worktree.bare
            or worktree.detached
        ):
            return self._discard_promotion_blocked(
                "branch_mismatch",
                f"Lease {lease.id} is not checked out on its registered branch.",
                lease=lease,
            )
        if any(
            item.path != lease.worktree_path and item.branch == lease.branch
            for item in worktrees
        ):
            return self._discard_promotion_blocked(
                "branch_in_use",
                f"Branch {lease.branch!r} is checked out elsewhere.",
                lease=lease,
            )
        try:
            if self.git.status_porcelain(lease.worktree_path):
                return self._discard_promotion_blocked(
                    "dirty_worktree",
                    f"Lease {lease.id} has uncommitted changes.",
                    lease=lease,
                )
            worktree_head = self.git.head_sha(lease.worktree_path)
            branch_head = self.git.resolve_ref(lease.branch)
        except (GitError, OSError) as error:
            return self._discard_promotion_blocked(
                "head_unavailable",
                f"Unable to inspect lease HEAD: {error}",
                lease=lease,
            )
        if worktree_head != lease.head_sha:
            return self._discard_promotion_blocked(
                "head_mismatch",
                f"Lease {lease.id} HEAD no longer matches its recorded head.",
                lease=lease,
            )
        if branch_head != lease.head_sha:
            return self._discard_promotion_blocked(
                "branch_head_mismatch",
                f"Branch {lease.branch!r} no longer matches the recorded head.",
                lease=lease,
            )
        return None

    def _complete_discard_promotion_cleanup(
        self, lease: Lease, reservation: CleanupReservation
    ) -> CommandResult:
        try:
            removed = self.registry.complete_cleanup(
                lease.id, expected_version=reservation.reserved_version
            )
        except (RuntimeError, ValueError, sqlite3.Error) as error:
            return self._discard_promotion_blocked(
                "registry_conflict",
                (
                    "Worktree was removed but the cleanup reservation could not be "
                    f"completed: {error}"
                ),
                lease=lease,
            )
        actions: list[dict[str, object]] = [
            self._cleanup_action("remove_worktree", removed)
        ]
        warnings: list[dict[str, str]] = []
        try:
            self.git.delete_branch_if_at(removed.branch, reservation.branch_sha)
        except (GitError, OSError) as error:
            self._branch_cleanup_warning(
                removed,
                "local_branch_cleanup_failed",
                f"Could not delete local branch {removed.branch!r}: {error}",
                warnings,
            )
        else:
            actions.append(self._cleanup_action("delete_local_branch", removed))
        return CommandResult.ok(
            "wt.discard-promotion",
            decision="removed",
            lease=removed,
            actions=tuple(actions),
            warnings=tuple(warnings),
        )

    def _recover_discard_promotion_reservation(
        self,
        lease: Lease,
        reservation: CleanupReservation,
        removal_error: Exception,
    ) -> CommandResult:
        path_blocker = self._cleanup_path_blocker(lease)
        if path_blocker is not None:
            return CommandResult.blocked(
                "wt.discard-promotion", blockers=(path_blocker,), lease=lease
            )
        try:
            worktrees = self.git.list_worktrees()
        except (GitError, OSError) as error:
            return self._discard_promotion_blocked(
                "worktree_inspection_failed",
                f"Unable to inspect reserved worktree: {error}",
                lease=lease,
            )
        path_is_absent = (
            not lease.worktree_path.exists()
            and all(worktree.path != lease.worktree_path for worktree in worktrees)
        )
        if path_is_absent:
            return self._complete_discard_promotion_cleanup(lease, reservation)
        return self._release_discard_promotion_reservation(
            lease,
            reservation,
            self._discard_promotion_blocked(
                "worktree_remove_failed",
                f"Unable to remove worktree for lease {lease.id}: {removal_error}",
                lease=lease,
            ),
        )

    def _release_discard_promotion_reservation(
        self,
        lease: Lease,
        reservation: CleanupReservation,
        result: CommandResult,
    ) -> CommandResult:
        try:
            released = self.registry.release_cleanup_reservation(
                lease.id, expected_version=reservation.reserved_version
            )
        except (RuntimeError, sqlite3.Error) as error:
            return self._discard_promotion_blocked(
                "cleanup_reserved",
                f"Lease {lease.id} remains reserved for cleanup: {error}",
                lease=lease,
            )
        return replace(result, lease=released)

    @staticmethod
    def _has_discardable_promotion_failure(events: Sequence[object]) -> bool:
        for event in reversed(events):
            event_type = getattr(event, "event_type", None)
            if event_type in {"cleanup_reserved", "cleanup_released"}:
                continue
            summary = getattr(event, "summary", "")
            return (
                event_type == "promotion_blocked"
                and isinstance(summary, str)
                and summary.startswith("promotion_apply_failed:")
            )
        return False

    @staticmethod
    def _discard_promotion_blocked(
        code: str, message: str, *, lease: Lease | None = None
    ) -> CommandResult:
        return CommandResult.blocked(
            "wt.discard-promotion",
            blockers=({"code": code, "message": message},),
            lease=lease,
        )

    def _sync_conflict_worktree_issue(
        self,
        lease: Lease,
        *,
        require_recorded_conflicts: bool,
        allow_untracked_outside_reviewed: bool = False,
    ) -> tuple[str, str] | None:
        if lease.target_base_sha is None or lease.source_head_sha is None:
            return "sync_pins_invalid", f"Lease {lease.id} has incomplete sync pins."
        try:
            worktrees = self.git.list_worktrees()
        except (GitError, OSError) as error:
            return "worktree_inspection_failed", str(error)
        registered = tuple(
            worktree
            for worktree in worktrees
            if worktree.path == lease.worktree_path
        )
        if len(registered) != 1 or not lease.worktree_path.is_dir():
            return (
                "unregistered_worktree",
                f"Lease {lease.id} is not registered at its expected path.",
            )
        worktree = registered[0]
        if (
            worktree.branch != lease.branch
            or worktree.bare
            or worktree.detached
        ):
            return (
                "branch_mismatch",
                f"Lease {lease.id} is not checked out on its registered branch.",
            )
        if any(
            item.path != lease.worktree_path and item.branch == lease.branch
            for item in worktrees
        ):
            return (
                "branch_in_use",
                f"Branch {lease.branch!r} is checked out elsewhere.",
            )
        try:
            worktree_head = self.git.head_sha(lease.worktree_path)
            branch_head = self.git.resolve_ref(lease.branch)
            status_entries = self.git.status_porcelain_entries(lease.worktree_path)
            unmerged_paths = self.git.unmerged_paths(lease.worktree_path)
        except (GitError, OSError) as error:
            return "worktree_inspection_failed", str(error)
        if worktree_head != lease.target_base_sha:
            return (
                "head_mismatch",
                f"Lease {lease.id} worktree HEAD does not match its target pin.",
            )
        if branch_head != lease.target_base_sha:
            return (
                "branch_head_mismatch",
                f"Branch {lease.branch!r} does not match the target pin.",
            )
        conflict_paths = (
            lease.conflicted_paths
            if require_recorded_conflicts
            else lease.reviewed_paths
        )
        if require_recorded_conflicts and not conflict_paths:
            return (
                "sync_conflicted_paths_missing",
                f"Lease {lease.id} has no recorded synchronization conflicts.",
            )
        reviewed = set(lease.reviewed_paths)
        allowed_conflicts = set(conflict_paths)
        allowed_unmerged_statuses = (
            frozenset({"UU"})
            if require_recorded_conflicts
            else _SYNC_UNMERGED_STATUS_CODES
        )
        unmerged_statuses = tuple(
            sorted(
                (entry.path, f"{entry.index_status}{entry.worktree_status}")
                for entry in status_entries
                if f"{entry.index_status}{entry.worktree_status}"
                in _SYNC_UNMERGED_STATUS_CODES
            )
        )
        unmerged_entries = tuple(path for path, _ in unmerged_statuses)
        if not unmerged_entries:
            return (
                "sync_conflicts_missing",
                f"Lease {lease.id} has no unresolved synchronization conflicts.",
            )
        if (
            unmerged_entries != unmerged_paths
            or any(
                status not in allowed_unmerged_statuses
                for _, status in unmerged_statuses
            )
        ):
            return (
                "sync_unmerged_state_invalid",
                f"Lease {lease.id} has unsupported unmerged index entries.",
            )
        if not set(unmerged_entries).issubset(allowed_conflicts):
            return (
                "sync_conflict_scope_mismatch",
                f"Lease {lease.id} has conflicts outside its allowed paths.",
            )
        clean_staged_paths: list[str] = []
        for entry in status_entries:
            if entry.path not in reviewed:
                if (
                    allow_untracked_outside_reviewed
                    and entry.index_status == "?"
                    and entry.worktree_status == "?"
                ):
                    continue
                return (
                    "sync_resolution_scope_mismatch",
                    f"Lease {lease.id} has changes outside reviewed paths.",
                )
            if entry.original_path is not None:
                return (
                    "sync_resolution_scope_mismatch",
                    f"Lease {lease.id} has an unsupported rename or copy.",
                )
            status_code = f"{entry.index_status}{entry.worktree_status}"
            if status_code in _SYNC_UNMERGED_STATUS_CODES:
                continue
            if (
                entry.worktree_status != " "
                or entry.index_status not in {"M", "A", "D", "T"}
            ):
                return (
                    "sync_resolution_scope_mismatch",
                    f"Lease {lease.id} has unsupported working-tree changes.",
                )
            clean_staged_paths.append(entry.path)
        try:
            index_entries = dict(
                self.git.index_entry_snapshot(
                    lease.worktree_path, tuple(sorted(clean_staged_paths))
                )
            )
            for path, entry in index_entries.items():
                if entry != self.git.path_entry(lease.source_head_sha, path):
                    return (
                        "sync_resolution_index_mismatch",
                        f"Lease {lease.id} has a staged entry outside its source pin.",
                    )
        except (GitError, OSError) as error:
            return "worktree_inspection_failed", str(error)
        return None

    def _discard_sync_preflight(
        self,
        lease: Lease,
        *,
        repository_id: str,
        source_branch: str,
        target_branch: str,
        source_ref: str,
        target_ref: str,
        github: GhClient,
        reservation: CleanupReservation | None = None,
        recovery: bool = False,
    ) -> CommandResult | None:
        blockers: list[dict[str, str]] = []
        expected_initiative = self._sync_initiative(source_branch, target_branch)
        if lease.repository_id != repository_id:
            blockers.append(
                {
                    "code": "repository_mismatch",
                    "message": f"Lease {lease.id} belongs to a different repository.",
                }
            )
        if not lease.managed or lease.owner_kind != "awf":
            blockers.append(
                {
                    "code": "unmanaged_lease",
                    "message": f"Lease {lease.id} is not managed by AWF.",
                }
            )
        if lease.purpose is not Purpose.FEATURE:
            blockers.append(
                {
                    "code": "not_sync_lease",
                    "message": f"Lease {lease.id} is not a feature synchronization lease.",
                }
            )
        if (
            lease.initiative != expected_initiative
            or lease.base_ref != target_ref
            or not isinstance(lease.source_head_sha, str)
            or _GIT_OBJECT_ID.fullmatch(lease.source_head_sha) is None
            or lease.branch
            != self._sync_branch(expected_initiative, lease.source_head_sha)
        ):
            blockers.append(
                {
                    "code": "sync_provenance_mismatch",
                    "message": f"Lease {lease.id} does not use the configured sync identity.",
                }
            )
        if (
            not isinstance(lease.source_base_sha, str)
            or _GIT_OBJECT_ID.fullmatch(lease.source_base_sha) is None
            or not isinstance(lease.target_base_sha, str)
            or _GIT_OBJECT_ID.fullmatch(lease.target_base_sha) is None
            or lease.head_sha != lease.target_base_sha
        ):
            blockers.append(
                {
                    "code": "sync_pins_invalid",
                    "message": f"Lease {lease.id} has invalid synchronization pins.",
                }
            )
        try:
            reviewed_paths = self._promotion_excluded_paths(lease.reviewed_paths)
        except ValueError:
            reviewed_paths = ()
        if not reviewed_paths or reviewed_paths != lease.reviewed_paths:
            blockers.append(
                {
                    "code": "reviewed_paths_invalid",
                    "message": f"Lease {lease.id} has invalid reviewed synchronization paths.",
                }
            )
        if lease.state is not LeaseState.BLOCKED:
            blockers.append(
                {
                    "code": "lease_not_blocked",
                    "message": f"Lease {lease.id} is not blocked.",
                }
            )
        if lease.resolution_state is not ResolutionState.NONE:
            blockers.append(
                {
                    "code": "sync_resolution_present",
                    "message": f"Lease {lease.id} has a synchronization resolution state.",
                }
            )
        if lease.target_pr is not None:
            blockers.append(
                {
                    "code": "target_pr_present",
                    "message": f"Lease {lease.id} is linked to a pull request.",
                }
            )
        if blockers:
            return CommandResult.blocked(
                "wt.discard-sync", blockers=tuple(blockers), lease=lease
            )
        assert lease.source_base_sha is not None
        assert lease.source_head_sha is not None
        assert lease.target_base_sha is not None
        try:
            if (
                self.git.merge_base(lease.source_head_sha, lease.target_base_sha)
                != lease.source_base_sha
            ):
                return self._discard_sync_blocked(
                    "sync_provenance_mismatch",
                    f"Lease {lease.id} no longer has its recorded source base.",
                    lease=lease,
                )
            active_reservation = self.registry.get_cleanup_reservation(lease.id)
            events = self.registry.list_events_read_only(lease.id)
        except (GitError, OSError, sqlite3.Error) as error:
            return self._discard_sync_blocked(
                "preflight_inspection_failed", str(error), lease=lease
            )
        if reservation is None:
            if active_reservation is not None:
                return self._discard_sync_blocked(
                    "cleanup_reserved",
                    f"Lease {lease.id} is already reserved for cleanup.",
                    lease=lease,
                )
        elif (
            active_reservation != reservation
            or lease.version != reservation.reserved_version
            or not events
            or events[-1].event_type != "cleanup_reserved"
            or events[-1].observed_head_sha != reservation.branch_sha
        ):
            return self._discard_sync_blocked(
                "lease_changed",
                f"Lease {lease.id} changed while cleanup was reserved.",
                lease=lease,
            )
        if not self._has_discardable_sync_conflict(events):
            return self._discard_sync_blocked(
                "sync_conflict_not_recorded",
                f"Lease {lease.id} was not most recently blocked by a sync target conflict.",
                lease=lease,
            )
        path_blocker = self._cleanup_path_blocker(lease)
        if path_blocker is not None:
            return CommandResult.blocked(
                "wt.discard-sync", blockers=(path_blocker,), lease=lease
            )
        try:
            live_source_sha = self.git.remote_branch_sha(source_branch)
            live_target_sha = self.git.remote_branch_sha(target_branch)
            remote_sync_sha = self.git.remote_branch_sha(lease.branch)
            target_pull_request = github.find_pr(
                head=lease.branch, base=target_branch
            )
        except (ExternalServiceError, GitRemoteError) as error:
            return self._external_error(
                "wt.discard-sync",
                "sync_remote_inspection_failed",
                str(error),
                lease=lease,
            )
        except (GitError, OSError) as error:
            return self._discard_sync_blocked(
                "sync_remote_inspection_failed", str(error), lease=lease
            )
        if live_source_sha is None or live_target_sha is None:
            return self._discard_sync_blocked(
                "sync_live_ref_unavailable",
                "Configured synchronization branches must exist on origin.",
                lease=lease,
            )
        if (
            live_source_sha == lease.source_head_sha
            and live_target_sha == lease.target_base_sha
        ):
            return self._discard_sync_blocked(
                "sync_lease_not_stale",
                f"Lease {lease.id} still pins the live source and target.",
                lease=lease,
            )
        if remote_sync_sha is not None:
            return self._discard_sync_blocked(
                "remote_branch_present",
                f"Lease {lease.id} still has a remote branch.",
                lease=lease,
            )
        if target_pull_request is not None:
            return self._discard_sync_blocked(
                "sync_pr_present",
                f"Lease {lease.id} still has a synchronization pull request.",
                lease=lease,
            )
        if recovery:
            return None
        issue = self._sync_conflict_worktree_issue(
            lease, require_recorded_conflicts=False
        )
        if issue is not None:
            return self._discard_sync_blocked(*issue, lease=lease)
        return None

    def _complete_discard_sync_cleanup(
        self, lease: Lease, reservation: CleanupReservation
    ) -> CommandResult:
        try:
            removed = self.registry.complete_cleanup(
                lease.id, expected_version=reservation.reserved_version
            )
        except (RuntimeError, ValueError, sqlite3.Error) as error:
            return self._discard_sync_blocked(
                "registry_conflict",
                (
                    "Worktree was removed but the cleanup reservation could not be "
                    f"completed: {error}"
                ),
                lease=lease,
            )
        actions: list[dict[str, object]] = [
            self._cleanup_action("remove_worktree", removed)
        ]
        warnings: list[dict[str, str]] = []
        try:
            self.git.delete_branch_if_at(removed.branch, reservation.branch_sha)
        except (GitError, OSError) as error:
            self._branch_cleanup_warning(
                removed,
                "local_branch_cleanup_failed",
                f"Could not delete local branch {removed.branch!r}: {error}",
                warnings,
            )
        else:
            actions.append(self._cleanup_action("delete_local_branch", removed))
        return CommandResult.ok(
            "wt.discard-sync",
            decision="removed",
            lease=removed,
            actions=tuple(actions),
            warnings=tuple(warnings),
        )

    def _recover_discard_sync_reservation(
        self,
        lease: Lease,
        reservation: CleanupReservation,
        removal_error: Exception,
        *,
        normalized_for_removal: bool,
    ) -> CommandResult:
        try:
            worktrees = self.git.list_worktrees()
        except (GitError, OSError) as error:
            return self._discard_sync_cleanup_reserved(
                lease, f"Unable to inspect cleanup recovery: {error}"
            )
        path_is_absent = (
            not lease.worktree_path.exists()
            and all(worktree.path != lease.worktree_path for worktree in worktrees)
        )
        if path_is_absent:
            return self._complete_discard_sync_cleanup(lease, reservation)
        identity, identity_issue = self._discard_sync_worktree_identity(lease)
        if identity_issue is not None:
            return self._discard_sync_cleanup_reserved(
                lease,
                (
                    "Worktree cannot be safely restored after failed removal: "
                    f"{identity_issue[0]}: {identity_issue[1]}"
                ),
            )
        assert identity is not None
        if normalized_for_removal:
            rebuild_issue = self._rebuild_discard_sync_conflict(lease, identity)
        else:
            rebuild_issue = self._discard_sync_conflict_state_issue(lease)
        if rebuild_issue is not None:
            return self._discard_sync_cleanup_reserved(
                lease,
                (
                    "Unable to restore the recorded synchronization conflict after "
                    f"failed removal: {rebuild_issue[0]}: {rebuild_issue[1]}"
                ),
            )
        return self._release_discard_sync_reservation(
            lease,
            reservation,
            self._discard_sync_blocked(
                "worktree_remove_failed",
                (
                    f"Unable to remove worktree for lease {lease.id}: "
                    f"{removal_error}. The recorded conflict was restored."
                ),
                lease=lease,
            ),
        )

    def _discard_sync_conflict_state_issue(
        self,
        lease: Lease,
        *,
        allow_untracked_outside_reviewed: bool = False,
    ) -> tuple[str, str] | None:
        issue = self._sync_conflict_worktree_issue(
            lease,
            require_recorded_conflicts=False,
            allow_untracked_outside_reviewed=allow_untracked_outside_reviewed,
        )
        if issue is not None:
            return issue
        try:
            conflict_paths = self.git.unmerged_paths(lease.worktree_path)
        except (GitError, OSError) as error:
            return "worktree_inspection_failed", str(error)
        if conflict_paths != lease.conflicted_paths:
            return (
                "sync_conflict_rebuild_mismatch",
                f"Lease {lease.id} conflict paths do not match its recorded pins.",
            )
        return None

    def _rebuild_discard_sync_conflict(
        self, lease: Lease, identity: _WorktreeIdentity
    ) -> tuple[str, str] | None:
        assert lease.source_base_sha is not None
        assert lease.source_head_sha is not None
        assert lease.target_base_sha is not None
        _, identity_issue = self._discard_sync_worktree_identity(
            lease, expected=identity
        )
        if identity_issue is not None:
            return identity_issue
        try:
            if (
                self.git.head_sha(lease.worktree_path) != lease.target_base_sha
                or self.git.resolve_ref(lease.branch) != lease.target_base_sha
                or self.git.index_tree_sha(lease.worktree_path)
                != self.git.commit_tree_sha(
                    lease.target_base_sha, lease.worktree_path
                )
            ):
                return (
                    "sync_conflict_rebuild_mismatch",
                    f"Lease {lease.id} is no longer at its target pin and index.",
                )
            if self.git.unmerged_paths(lease.worktree_path):
                return self._discard_sync_conflict_state_issue(
                    lease, allow_untracked_outside_reviewed=True
                )
            patch = self.git.binary_diff(
                lease.source_base_sha,
                lease.source_head_sha,
                paths=lease.reviewed_paths,
            )
            if not patch:
                return (
                    "sync_conflict_rebuild_failed",
                    f"Lease {lease.id} source-only patch is empty.",
                )
            try:
                self.git.apply_indexed_patch(lease.worktree_path, patch)
            except GitPatchConflict as error:
                if tuple(sorted(error.paths)) != lease.conflicted_paths:
                    return (
                        "sync_conflict_rebuild_mismatch",
                        f"Lease {lease.id} rebuilt unexpected conflict paths.",
                    )
            else:
                return (
                    "sync_conflict_rebuild_failed",
                    f"Lease {lease.id} source-only patch no longer conflicts.",
                )
        except (GitError, OSError) as error:
            return "sync_conflict_rebuild_failed", str(error)
        return self._discard_sync_conflict_state_issue(
            lease, allow_untracked_outside_reviewed=True
        )

    @staticmethod
    def _discard_sync_cleanup_reserved(
        lease: Lease, message: str
    ) -> CommandResult:
        return WorktreeService._discard_sync_blocked(
            "cleanup_reserved",
            f"Lease {lease.id} remains reserved for cleanup: {message}",
            lease=lease,
        )

    def _release_discard_sync_reservation(
        self,
        lease: Lease,
        reservation: CleanupReservation,
        result: CommandResult,
    ) -> CommandResult:
        try:
            released = self.registry.release_cleanup_reservation(
                lease.id, expected_version=reservation.reserved_version
            )
        except (RuntimeError, sqlite3.Error) as error:
            return self._discard_sync_blocked(
                "cleanup_reserved",
                f"Lease {lease.id} remains reserved for cleanup: {error}",
                lease=lease,
            )
        return replace(result, lease=released)

    @staticmethod
    def _has_discardable_sync_conflict(events: Sequence[object]) -> bool:
        for event in reversed(events):
            event_type = getattr(event, "event_type", None)
            if event_type in {"cleanup_reserved", "cleanup_released"}:
                continue
            summary = getattr(event, "summary", "")
            return (
                event_type == "sync_blocked"
                and isinstance(summary, str)
                and summary.startswith("sync_target_conflict:")
            )
        return False

    @staticmethod
    def _discard_sync_blocked(
        code: str, message: str, *, lease: Lease | None = None
    ) -> CommandResult:
        return CommandResult.blocked(
            "wt.discard-sync",
            blockers=({"code": code, "message": message},),
            lease=lease,
        )

    def _current_sync_lease(
        self,
        *,
        source_branch: str,
        target_branch: str,
        source_ref: str,
        target_ref: str,
        fetch: bool,
    ) -> Lease:
        source_sha = (
            self.git.fetch_ref(source_branch)
            if fetch
            else self.git.resolve_ref(source_ref)
        )
        target_sha = (
            self.git.fetch_ref(target_branch)
            if fetch
            else self.git.resolve_ref(target_ref)
        )
        merge_base, expected_blobs = self._sync_expected_blobs(source_sha, target_sha)
        initiative = self._sync_initiative(source_branch, target_branch)
        return self._new_sync_lease(
            initiative=initiative,
            branch=self._sync_branch(initiative, source_sha),
            source_ref=source_ref,
            target_ref=target_ref,
            source_sha=source_sha,
            target_sha=target_sha,
            merge_base=merge_base,
            reviewed_paths=tuple(expected_blobs),
        )

    def _recover_sync_preflight(
        self,
        lease: Lease,
        *,
        expected: Lease,
        repository_id: str,
        source_branch: str,
        target_branch: str,
        github: GhClient,
    ) -> CommandResult | None:
        blockers: list[dict[str, str]] = []
        if lease.repository_id != repository_id:
            blockers.append(
                {
                    "code": "repository_mismatch",
                    "message": f"Lease {lease.id} belongs to a different repository.",
                }
            )
        if not lease.managed or lease.owner_kind != "awf":
            blockers.append(
                {
                    "code": "unmanaged_lease",
                    "message": f"Lease {lease.id} is not managed by AWF.",
                }
            )
        if (
            lease.purpose is not Purpose.FEATURE
            or lease.initiative != expected.initiative
            or lease.branch != expected.branch
            or lease.base_ref != expected.base_ref
        ):
            blockers.append(
                {
                    "code": "not_current_sync_lease",
                    "message": f"Lease {lease.id} is not the current configured sync lease.",
                }
            )
        if (
            lease.source_base_sha != expected.source_base_sha
            or lease.source_head_sha != expected.source_head_sha
            or lease.target_base_sha != expected.target_base_sha
            or lease.reviewed_paths != expected.reviewed_paths
            or lease.head_sha != lease.target_base_sha
        ):
            blockers.append(
                {
                    "code": "sync_lease_stale",
                    "message": f"Lease {lease.id} does not retain current sync pins.",
                }
            )
        if lease.state is not LeaseState.BLOCKED:
            blockers.append(
                {
                    "code": "lease_not_blocked",
                    "message": f"Lease {lease.id} is not blocked.",
                }
            )
        if lease.resolution_state is not ResolutionState.NONE:
            blockers.append(
                {
                    "code": "sync_resolution_present",
                    "message": f"Lease {lease.id} has a synchronization resolution state.",
                }
            )
        if lease.target_pr is not None:
            blockers.append(
                {
                    "code": "target_pr_present",
                    "message": f"Lease {lease.id} is linked to a pull request.",
                }
            )
        try:
            conflicted_paths = self._promotion_excluded_paths(lease.conflicted_paths)
        except ValueError:
            conflicted_paths = ()
        if (
            not conflicted_paths
            or conflicted_paths != lease.conflicted_paths
            or not set(conflicted_paths).issubset(lease.reviewed_paths)
        ):
            blockers.append(
                {
                    "code": "sync_conflicted_paths_invalid",
                    "message": f"Lease {lease.id} has invalid recorded conflict paths.",
                }
            )
        if blockers:
            return CommandResult.blocked(
                "wt.recover-sync", blockers=tuple(blockers), lease=lease
            )
        try:
            active_reservation = self.registry.get_cleanup_reservation(lease.id)
            events = self.registry.list_events_read_only(lease.id)
        except sqlite3.Error as error:
            return self._recover_sync_blocked(
                "registry_conflict", str(error), lease=lease
            )
        if active_reservation is not None:
            return self._recover_sync_blocked(
                "cleanup_reserved",
                f"Lease {lease.id} is already reserved for cleanup.",
                lease=lease,
            )
        if not self._has_discardable_sync_conflict(events):
            return self._recover_sync_blocked(
                "sync_conflict_not_recorded",
                f"Lease {lease.id} was not most recently blocked by a sync target conflict.",
                lease=lease,
            )
        path_blocker = self._cleanup_path_blocker(lease)
        if path_blocker is not None:
            return CommandResult.blocked(
                "wt.recover-sync", blockers=(path_blocker,), lease=lease
            )
        try:
            live_source_sha = self.git.remote_branch_sha(source_branch)
            live_target_sha = self.git.remote_branch_sha(target_branch)
            remote_sync_sha = self.git.remote_branch_sha(lease.branch)
            target_pull_request = github.find_pr(
                head=lease.branch, base=target_branch
            )
        except (ExternalServiceError, GitRemoteError) as error:
            return self._external_error(
                "wt.recover-sync",
                "sync_remote_inspection_failed",
                str(error),
                lease=lease,
            )
        except (GitError, OSError) as error:
            return self._recover_sync_blocked(
                "sync_remote_inspection_failed", str(error), lease=lease
            )
        if (
            live_source_sha != lease.source_head_sha
            or live_target_sha != lease.target_base_sha
        ):
            return self._recover_sync_blocked(
                "sync_lease_stale",
                f"Lease {lease.id} no longer pins both live synchronization branches.",
                lease=lease,
            )
        if remote_sync_sha is not None:
            return self._recover_sync_blocked(
                "remote_branch_present",
                f"Lease {lease.id} still has a remote branch.",
                lease=lease,
            )
        if target_pull_request is not None:
            return self._recover_sync_blocked(
                "sync_pr_present",
                f"Lease {lease.id} still has a synchronization pull request.",
                lease=lease,
            )
        issue = self._sync_conflict_worktree_issue(
            lease, require_recorded_conflicts=True
        )
        if issue is not None:
            return self._recover_sync_blocked(*issue, lease=lease)
        return None

    def _recover_sync_final_index_preflight(
        self, lease: Lease
    ) -> CommandResult | None:
        if lease.target_base_sha is None:
            return self._recover_sync_blocked(
                "sync_pins_invalid",
                f"Lease {lease.id} has no target synchronization pin.",
                lease=lease,
            )
        try:
            if self.git.unmerged_paths(lease.worktree_path):
                return self._recover_sync_blocked(
                    "sync_resolution_unmerged",
                    f"Lease {lease.id} still has unmerged paths after staging.",
                    lease=lease,
                )
            status_entries = self.git.status_porcelain_entries(lease.worktree_path)
            if self.git.index_has_conflict_markers(
                lease.worktree_path, lease.reviewed_paths
            ):
                return self._recover_sync_blocked(
                    "sync_conflict_markers_present",
                    f"Lease {lease.id} retains conflict markers after staging.",
                    lease=lease,
                )
            for entry in status_entries:
                if (
                    entry.path not in lease.reviewed_paths
                    or entry.original_path is not None
                    or entry.worktree_status != " "
                    or entry.index_status not in {"M", "A", "D", "T"}
                ):
                    return self._recover_sync_blocked(
                        "sync_resolution_scope_mismatch",
                        (
                            f"Lease {lease.id} changed paths outside the reviewed "
                            "sync delta."
                        ),
                        lease=lease,
                    )
            indexed_paths = tuple(
                sorted(
                    self.git.indexed_changed_paths(
                        lease.worktree_path, lease.target_base_sha
                    )
                )
            )
        except (GitError, OSError) as error:
            return self._recover_sync_blocked(
                "sync_recovery_failed", str(error), lease=lease
            )
        if indexed_paths != lease.reviewed_paths:
            return self._recover_sync_blocked(
                "sync_delta_mismatch",
                "resolved synchronization paths do not match the source-only delta.",
                lease=lease,
            )
        return None

    def _restore_recovery_index(
        self,
        lease: Lease,
        backup: GitIndexBackup,
        result: CommandResult,
    ) -> CommandResult:
        try:
            self.git.restore_index(backup)
        except (GitError, OSError) as error:
            return self._block_recovered_sync_lease(
                lease, "sync_index_restore_failed", str(error)
            )
        return result

    def _recovered_sync_commit_preflight(
        self,
        lease: Lease,
        *,
        sync_head: str,
        source_branch: str,
        target_branch: str,
    ) -> CommandResult | None:
        assert lease.source_base_sha is not None
        assert lease.source_head_sha is not None
        assert lease.target_base_sha is not None
        try:
            if (
                self.git.head_sha(lease.worktree_path) != sync_head
                or self.git.resolve_ref(lease.branch) != sync_head
                or self.git.commit_parents(sync_head)
                != (lease.target_base_sha, lease.source_head_sha)
                or self.git.commit_message(lease.worktree_path, sync_head)
                != self._sync_message(
                    source_branch=source_branch,
                    target_branch=target_branch,
                    merge_base=lease.source_base_sha,
                    source_sha=lease.source_head_sha,
                    target_sha=lease.target_base_sha,
                    lease=lease,
                )
                or self.git.commit_tree_sha(sync_head, lease.worktree_path)
                != self.git.index_tree_sha(lease.worktree_path)
                or self.git.status_porcelain(lease.worktree_path)
                or tuple(
                    sorted(
                        self.git.changed_path_endpoints(
                            lease.worktree_path, lease.target_base_sha, sync_head
                        )
                    )
                )
                != lease.reviewed_paths
                or self.git.tree_has_conflict_markers(
                    lease.worktree_path, sync_head, lease.reviewed_paths
                )
            ):
                return self._block_recovered_sync_lease(
                    lease,
                    "sync_recovery_commit_mismatch",
                    "recovered synchronization commit changed before publication",
                )
        except (GitError, OSError) as error:
            return self._block_recovered_sync_lease(
                lease, "sync_recovery_failed", str(error)
            )
        return None

    def _block_recovered_sync_lease(
        self, lease: Lease, code: str, message: str
    ) -> CommandResult:
        try:
            current = self.registry.get_lease(lease.id)
            if current is not None and current.state is not LeaseState.BLOCKED:
                lease = self.registry.transition(
                    current.id,
                    LeaseState.BLOCKED,
                    expected_version=current.version,
                    event_type="sync_blocked",
                    summary=f"{code}: {message}",
                    observed_head_sha=self.git.head_sha(current.worktree_path),
                )
            elif current is not None:
                lease = current
        except (GitError, OSError, RuntimeError, sqlite3.Error):
            pass
        return self._recover_sync_blocked(code, message, lease=lease)

    @staticmethod
    def _recover_sync_blocked(
        code: str, message: str, *, lease: Lease | None = None
    ) -> CommandResult:
        return CommandResult.blocked(
            "wt.recover-sync",
            blockers=({"code": code, "message": message},),
            lease=lease,
        )

    def _cleanup_branches(
        self,
        lease: Lease,
        expected_sha: str,
        actions: list[dict[str, object]],
        warnings: list[dict[str, str]],
    ) -> GitRemoteError | None:
        if (
            not lease.managed
            or lease.owner_kind != "awf"
            or not lease.branch.startswith("awf/")
        ):
            return None
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
        except GitRemoteError as error:
            self._branch_cleanup_warning(
                lease,
                "remote_branch_cleanup_failed",
                f"Could not delete remote branch {lease.branch!r}: {error}",
                warnings,
            )
            return error
        except (GitError, OSError) as error:
            self._branch_cleanup_warning(
                lease,
                "remote_branch_cleanup_failed",
                f"Could not delete remote branch {lease.branch!r}: {error}",
                warnings,
            )
        else:
            actions.append(self._cleanup_action("delete_remote_branch", lease))
        return None

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

    @staticmethod
    def _external_error(
        command: str,
        code: str,
        message: str,
        *,
        lease: Lease | None = None,
        warnings: list[dict[str, str]] | None = None,
    ) -> CommandResult:
        return CommandResult.external_error(
            command,
            code=code,
            message=message,
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
            if pull_request.head_sha != current.head_sha:
                self._block_refresh_head_mismatch(current, pull_request)
                return
            if pull_request.state == "OPEN":
                self._transition_refresh(
                    lease.id,
                    pull_request,
                    LeaseState.PR_OPEN,
                    deployment_state=None,
                )
            elif self._is_completed_pr(pull_request):
                if current.purpose is Purpose.PROMOTE:
                    self._refresh_promotion(lease.id, pull_request, warnings)
                    self.registry.sync_release_state_for_lease(
                        lease.id,
                        ReleaseState.MERGED,
                    )
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
                self.registry.sync_release_state_for_lease(
                    lease.id,
                    ReleaseState.CLOSED_UNMERGED,
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
        probe, adapter_digest = self._probe_deployment(current, pull_request)
        if probe.response is None:
            self._transition_refresh(
                lease_id,
                pull_request,
                LeaseState.DEPLOYING,
                deployment_state=DeploymentState.UNKNOWN,
            )
            warnings.append(
                {
                    "code": probe.failure_code or "deployment_evidence_invalid",
                    "message": f"Unable to refresh deployment evidence for lease {lease_id}.",
                }
            )
            return
        evidence = probe.response.registry_evidence(
            adapter_digest=adapter_digest or "",
            received_at=probe.received_at,
        )
        if probe.status == "healthy":
            self._transition_refresh(
                lease_id,
                pull_request,
                LeaseState.CLEANABLE,
                deployment_state=DeploymentState.HEALTHY,
                event_type="deployment_evidence",
                summary="Fresh deployment evidence is healthy",
                evidence=evidence,
            )
            return
        if probe.status == "failed":
            state = LeaseState.BLOCKED
            deployment_state = DeploymentState.FAILED
            summary = "Deployment evidence reports failed"
        elif probe.status == "pending":
            state = LeaseState.DEPLOYING
            deployment_state = DeploymentState.PENDING
            summary = "Deployment evidence reports pending"
        else:
            state = LeaseState.DEPLOYING
            deployment_state = DeploymentState.UNKNOWN
            summary = "Deployment evidence reports unknown"
        self._transition_refresh(
            lease_id,
            pull_request,
            state,
            deployment_state=deployment_state,
            event_type="deployment_evidence",
            summary=summary,
            evidence=evidence,
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

    def _block_refresh_head_mismatch(
        self, lease: Lease, pull_request: PullRequest
    ) -> Lease:
        if (
            lease.state is LeaseState.BLOCKED
            and lease.deployment_state is DeploymentState.UNKNOWN
        ):
            events = self.registry.list_events(lease.id)
            if (
                events
                and events[-1].event_type == "github_refresh_head_mismatch"
                and events[-1].observed_head_sha == pull_request.head_sha
                and events[-1].pr_number == pull_request.number
            ):
                return lease
        return self.registry.transition(
            lease.id,
            LeaseState.BLOCKED,
            expected_version=lease.version,
            event_type="github_refresh_head_mismatch",
            summary=(
                "GitHub refresh: pull request HEAD does not match recorded lease HEAD"
            ),
            observed_head_sha=pull_request.head_sha,
            pr_number=pull_request.number,
            deployment_state=DeploymentState.UNKNOWN,
        )

    def _transition_refresh(
        self,
        lease_id: str,
        pull_request: PullRequest,
        state: LeaseState,
        *,
        deployment_state: DeploymentState | None,
        event_type: str = "github_refresh",
        summary: str = "GitHub refresh",
        evidence: Mapping[str, str] | None = None,
    ) -> Lease | None:
        current = self._current_refresh_lease(lease_id, pull_request.number)
        if current is None:
            return None
        if (
            evidence is None
            and current.state is state
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
            event_type=event_type,
            summary=summary,
            observed_head_sha=pull_request.head_sha,
            pr_number=pull_request.number,
            deployment_state=deployment_state,
            evidence=evidence,
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

    def adopt(
        self, lease_id: str, *, pr_number: int | None = None, apply: bool
    ) -> CommandResult:
        imported = self.registry.get_lease_read_only(lease_id)
        if imported is None:
            return self._adopt_blocked(
                "unknown_lease",
                f"lease {lease_id} does not exist",
            )
        if pr_number is None:
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
                try:
                    adopted = self.registry.transition(
                        imported.id,
                        imported.state,
                        expected_version=imported.version,
                        managed=True,
                        summary="imported lease adopted",
                    )
                except (RuntimeError, sqlite3.Error) as error:
                    return CommandResult.error(
                        "wt.adopt",
                        code="registry_conflict",
                        message=str(error),
                        exit_code=5,
                        lease=imported,
                    )
            return CommandResult.ok("wt.adopt", decision="ready", lease=adopted)

        pull_request = self._validate_pr_adoption(imported, pr_number)
        if isinstance(pull_request, CommandResult):
            return pull_request
        if not apply:
            if imported.managed:
                return CommandResult.ok(
                    "wt.adopt",
                    decision="reuse",
                    lease=imported,
                )
            return CommandResult.ok(
                "wt.adopt",
                decision="preview",
                lease=imported,
                actions=(
                    {
                        "kind": "link_pr",
                        "lease_id": imported.id,
                        "path": str(imported.worktree_path),
                        "pr_number": pull_request.number,
                        "head_sha": pull_request.head_sha,
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
            pull_request = self._validate_pr_adoption(imported, pr_number)
            if isinstance(pull_request, CommandResult):
                return pull_request
            blocker = self._adoption_git_safety_blocker(imported)
            if blocker is not None:
                return blocker
            if imported.managed:
                return CommandResult.ok(
                    "wt.adopt",
                    decision="reuse",
                    lease=imported,
                )
            try:
                adopted = self.registry.transition(
                    imported.id,
                    imported.state,
                    expected_version=imported.version,
                    event_type="imported_lease_pr_linked",
                    summary=f"imported lease linked to pull request #{pull_request.number}",
                    observed_head_sha=pull_request.head_sha,
                    pr_number=pull_request.number,
                    managed=True,
                )
            except (RuntimeError, sqlite3.Error) as error:
                return CommandResult.error(
                    "wt.adopt",
                    code="registry_conflict",
                    message=str(error),
                    exit_code=5,
                    lease=imported,
                )
        return CommandResult.ok("wt.adopt", decision="ready", lease=adopted)

    def _validate_managed_pr_link(
        self, lease: Lease, pr_number: object
    ) -> tuple[PullRequest, str] | CommandResult:
        if (
            not isinstance(pr_number, int)
            or isinstance(pr_number, bool)
            or pr_number <= 0
        ):
            return self._managed_link_blocked(
                "invalid_pr_number",
                "pull request number must be a positive integer",
                lease=lease,
            )
        blocker = self._managed_link_lease_blocker(lease, pr_number)
        if blocker is not None:
            return blocker
        before_head = self._managed_link_worktree_head(lease)
        if isinstance(before_head, CommandResult):
            return before_head
        try:
            pull_request = (
                self.github or GhClient(lease.repository_root)
            ).view_pr(pr_number)
        except ExternalServiceError as error:
            return self._external_error(
                "wt.link-pr",
                "github_link_failed",
                f"Unable to validate pull request #{pr_number}: {error}",
                lease=lease,
            )
        if pull_request.number != pr_number:
            return self._managed_link_blocked(
                "pr_number_mismatch",
                f"GitHub returned pull request #{pull_request.number} for #{pr_number}",
                lease=lease,
            )
        if not self._is_completed_pr(pull_request):
            return self._managed_link_blocked(
                "pr_not_merged",
                f"pull request #{pr_number} is {pull_request.state}",
                lease=lease,
            )
        if pull_request.head_ref != lease.branch:
            return self._managed_link_blocked(
                "pr_branch_mismatch",
                f"pull request #{pr_number} does not match branch {lease.branch!r}",
                lease=lease,
            )
        after_head = self._managed_link_worktree_head(lease)
        if isinstance(after_head, CommandResult):
            return after_head
        if pull_request.head_sha != after_head:
            return self._managed_link_blocked(
                "pr_head_mismatch",
                f"pull request #{pr_number} does not match the current worktree HEAD",
                lease=lease,
            )
        if before_head != after_head:
            return self._managed_link_blocked(
                "head_mismatch",
                f"lease {lease.id} changed while its pull request was validated",
                lease=lease,
            )
        return pull_request, after_head

    def _managed_link_lease_blocker(
        self, lease: Lease, pr_number: int
    ) -> CommandResult | None:
        if self.git is None or self.git.repository_id() != lease.repository_id:
            return self._managed_link_blocked(
                "repository_mismatch",
                f"lease {lease.id} does not match this repository",
                lease=lease,
            )
        if lease.state is LeaseState.REMOVED:
            return self._managed_link_blocked(
                "removed_lease",
                f"lease {lease.id} has been removed",
                lease=lease,
            )
        if not lease.managed or lease.owner_kind != "awf":
            return self._managed_link_blocked(
                "unmanaged_lease",
                f"lease {lease.id} is not an AWF-managed lease",
                lease=lease,
            )
        if lease.purpose is not Purpose.FEATURE:
            return self._managed_link_blocked(
                "unsupported_purpose",
                f"lease {lease.id} is not a feature lease",
                lease=lease,
            )
        if lease.state is LeaseState.CLOSED_UNMERGED:
            return self._managed_link_blocked(
                "closed_unmerged",
                f"lease {lease.id} was closed without merging",
                lease=lease,
            )
        if self.registry.get_cleanup_reservation(lease.id) is not None:
            return self._managed_link_blocked(
                "cleanup_reserved",
                f"lease {lease.id} is reserved for cleanup",
                lease=lease,
            )
        if lease.target_pr is not None and lease.target_pr != pr_number:
            return self._managed_link_blocked(
                "pr_link_mismatch",
                f"lease {lease.id} is linked to pull request #{lease.target_pr}",
                lease=lease,
            )
        expected_state = (
            LeaseState.ACTIVE
            if lease.target_pr is None
            else LeaseState.CLEANABLE
        )
        if lease.state is not expected_state:
            return self._managed_link_blocked(
                "unsupported_state",
                (
                    f"lease {lease.id} is {lease.state.value}; expected "
                    f"{expected_state.value} for PR linking"
                ),
                lease=lease,
            )
        return None

    def _managed_link_worktree_head(
        self, lease: Lease
    ) -> str | CommandResult:
        registered = self.git.list_worktrees()
        expected_path = lease.worktree_path.resolve()
        worktree = next(
            (
                item
                for item in registered
                if item.path.resolve() == expected_path and expected_path.is_dir()
            ),
            None,
        )
        if worktree is None:
            return self._managed_link_blocked(
                "orphaned_lease",
                f"lease {lease.id} is not registered as a Git worktree",
                lease=lease,
            )
        if worktree.bare or worktree.detached or worktree.branch != lease.branch:
            return self._managed_link_blocked(
                "branch_mismatch",
                f"lease {lease.id} does not match its registered branch",
                lease=lease,
            )
        if any(
            item.path.resolve() != expected_path and item.branch == lease.branch
            for item in registered
        ):
            return self._managed_link_blocked(
                "branch_conflict",
                f"branch {lease.branch!r} is checked out at another worktree",
                lease=lease,
            )
        current_head = self.git.head_sha(lease.worktree_path)
        if worktree.head_sha != current_head:
            return self._managed_link_blocked(
                "head_mismatch",
                f"lease {lease.id} does not match its registered HEAD",
                lease=lease,
            )
        if self.git.status_porcelain(lease.worktree_path):
            return self._managed_link_blocked(
                "dirty_worktree",
                f"lease {lease.id} has uncommitted changes",
                lease=lease,
            )
        verified_head = self.git.head_sha(lease.worktree_path)
        if verified_head != current_head:
            return self._managed_link_blocked(
                "head_mismatch",
                f"lease {lease.id} changed while its worktree was validated",
                lease=lease,
            )
        return verified_head

    def _validate_pr_adoption(
        self, imported: Lease, pr_number: int
    ) -> PullRequest | CommandResult:
        number_blocker = self._adoption_pr_number_blocker(imported, pr_number)
        if number_blocker is not None:
            return number_blocker
        allow_managed = imported.managed
        if allow_managed and imported.target_pr != pr_number:
            return self._adopt_blocked(
                "pr_link_mismatch",
                f"lease {imported.id} is linked to pull request #{imported.target_pr}",
                lease=imported,
            )
        blocker = self._adoption_blocker(
            imported, allow_managed=allow_managed
        )
        if blocker is not None:
            return blocker
        return self._adoption_pr(imported, pr_number)

    def _adoption_pr_number_blocker(
        self, imported: Lease, pr_number: object
    ) -> CommandResult | None:
        if (
            not isinstance(pr_number, int)
            or isinstance(pr_number, bool)
            or pr_number <= 0
        ):
            return self._adopt_blocked(
                "invalid_pr_number",
                "pull request number must be a positive integer",
                lease=imported,
            )
        return None

    def _adoption_pr(
        self, imported: Lease, pr_number: int
    ) -> PullRequest | CommandResult:
        try:
            pull_request = (
                self.github or GhClient(imported.repository_root)
            ).view_pr(pr_number)
        except ExternalServiceError as error:
            return self._external_error(
                "wt.adopt",
                "github_adopt_failed",
                f"Unable to validate pull request #{pr_number}: {error}",
                lease=imported,
            )
        if pull_request.number != pr_number:
            return self._adopt_blocked(
                "pr_number_mismatch",
                f"GitHub returned pull request #{pull_request.number} for #{pr_number}",
                lease=imported,
            )
        if not self._is_completed_pr(pull_request):
            return self._adopt_blocked(
                "pr_not_merged",
                f"pull request #{pr_number} is {pull_request.state}",
                lease=imported,
            )
        if pull_request.head_ref != imported.branch:
            return self._adopt_blocked(
                "pr_branch_mismatch",
                f"pull request #{pr_number} does not match branch {imported.branch!r}",
                lease=imported,
            )
        if pull_request.head_sha != imported.head_sha:
            return self._adopt_blocked(
                "pr_head_mismatch",
                f"pull request #{pr_number} does not match lease HEAD",
                lease=imported,
            )
        return pull_request

    def _adoption_blocker(
        self, imported: Lease, *, allow_managed: bool = False
    ) -> CommandResult | None:
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
        if imported.managed and not allow_managed:
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
        return self._adoption_git_safety_blocker(imported)

    def _adoption_git_safety_blocker(
        self, imported: Lease
    ) -> CommandResult | None:
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
    def _managed_link_blocked(
        code: str, message: str, *, lease: Lease | None = None
    ) -> CommandResult:
        return CommandResult.blocked(
            "wt.link-pr",
            blockers=({"code": code, "message": message},),
            lease=lease,
        )

    @staticmethod
    def _adopt_blocked(
        code: str, message: str, *, lease: Lease | None = None
    ) -> CommandResult:
        return CommandResult.blocked(
            "wt.adopt",
            blockers=({"code": code, "message": message},),
            lease=lease,
        )

    def _new_release_lease(
        self,
        release_id: str,
        target_ref: str,
        target_sha: str,
    ) -> Lease:
        initiative = f"release-{release_id}"
        lease = Lease.new(
            repository_id=self.git.repository_id(),
            repository_name=self.git.repository_name(),
            repository_root=self.git.repository_root(),
            worktree_path=self.cache_dir / self.git.repository_name(),
            initiative=initiative,
            purpose=Purpose.PROMOTE,
            branch=f"awf/{initiative}/promote",
            base_ref=target_ref,
            head_sha=target_sha,
            managed=True,
            owner_kind="awf",
        )
        return replace(
            lease,
            worktree_path=self.cache_dir / lease.repository_name / lease.id,
        )

    def _release_reuse(
        self,
        command: str,
        release: ReleaseBridge,
        *,
        target_branch: str,
        lease: Lease | None,
        apply: bool,
    ) -> CommandResult:
        if release.target_branch != target_branch:
            return self._release_blocked(
                command,
                "release_target_mismatch",
                (
                    f"release {release.release_id!r} targets "
                    f"{release.target_branch!r}, not {target_branch!r}"
                ),
                lease=lease,
                release=release,
            )
        if lease is None:
            return self._release_blocked(
                command,
                "release_lease_missing",
                f"release {release.release_id!r} has no registered promotion lease",
                release=release,
            )
        if release.state in {
            ReleaseState.CLOSED_UNMERGED,
            ReleaseState.CLEANED,
        }:
            return self._release_blocked(
                command,
                "release_terminal",
                f"release {release.release_id!r} is {release.state.value}",
                lease=lease,
                release=release,
            )
        try:
            target_ref = self._promotion_target_ref(release.target_branch)
        except ConfigError as error:
            return self._release_blocked(
                command,
                "release_lease_mismatch",
                str(error),
                lease=lease,
                release=release,
            )
        expected_initiative = f"release-{release.release_id}"
        expected_branch = f"awf/{expected_initiative}/promote"
        if (
            lease.purpose is not Purpose.PROMOTE
            or not lease.managed
            or lease.repository_id != release.repository_id
            or lease.initiative != expected_initiative
            or lease.branch != expected_branch
            or lease.base_ref != target_ref
            or lease.state
            in {
                LeaseState.REMOVED,
                LeaseState.DIRTY,
                LeaseState.ORPHANED,
                LeaseState.CLOSED_UNMERGED,
                LeaseState.BLOCKED,
            }
        ):
            return self._release_blocked(
                command,
                "release_lease_mismatch",
                "release lease no longer matches its registered provenance",
                lease=lease,
                release=release,
            )
        try:
            worktree = self._registered_worktree(lease)
        except GitError as error:
            return self._release_blocked(
                command,
                "release_lease_mismatch",
                str(error),
                lease=lease,
                release=release,
            )
        recovered = False
        if worktree is None:
            if (
                release.state is not ReleaseState.OPEN
                or release.sources
                or lease.state is not LeaseState.ACTIVE
            ):
                return self._release_blocked(
                    command,
                    "release_lease_mismatch",
                    "non-empty release bridge lost its managed worktree",
                    lease=lease,
                    release=release,
                )
            try:
                remote_head = self.git.remote_branch_sha(lease.branch)
            except GitRemoteError as error:
                return self._release_external(
                    command,
                    "release_worktree_create_failed",
                    str(error),
                    lease=lease,
                    release=release,
                )
            if remote_head is not None:
                return self._release_blocked(
                    command,
                    "release_branch_changed",
                    "incomplete release branch was unexpectedly published",
                    lease=lease,
                    release=release,
                )
            if not apply:
                return CommandResult.ok(
                    command,
                    decision="preview",
                    lease=lease,
                    release=release,
                    actions=(
                        {
                            "kind": "create_worktree",
                            "path": str(lease.worktree_path),
                            "branch": lease.branch,
                            "target_branch": release.target_branch,
                            "target_base_sha": lease.head_sha,
                        },
                    ),
                )
            try:
                self.git.add_worktree(
                    lease.worktree_path,
                    lease.branch,
                    lease.head_sha,
                    reuse_exact_branch=True,
                )
                worktree = self._registered_worktree(lease)
                recovered = True
            except GitError as error:
                return self._release_blocked(
                    command,
                    "release_worktree_create_failed",
                    str(error),
                    lease=lease,
                    release=release,
                )
        try:
            actual_head_sha = self.git.head_sha(lease.worktree_path)
            dirty = self.git.status_porcelain(lease.worktree_path)
        except GitError as error:
            return self._release_blocked(
                command,
                "release_lease_mismatch",
                str(error),
                lease=lease,
                release=release,
            )
        if (
            worktree is None
            or worktree.branch != lease.branch
            or worktree.detached
            or worktree.bare
            or actual_head_sha != lease.head_sha
            or dirty
        ):
            return self._release_blocked(
                command,
                "release_lease_mismatch",
                "release worktree no longer matches its registered provenance",
                lease=lease,
                release=release,
            )
        return CommandResult.ok(
            command,
            decision="ready" if recovered else "reuse",
            lease=lease,
            release=release,
        )

    def _release_published_reuse(
        self,
        command: str,
        release: ReleaseBridge,
        lease: Lease | None,
    ) -> CommandResult:
        if (
            lease is None
            or release.target_pr is None
            or lease.target_pr != release.target_pr
            or release.published_head_sha is None
            or lease.head_sha != release.published_head_sha
        ):
            return self._release_blocked(
                command,
                "release_publication_mismatch",
                "published release registry provenance is incomplete or inconsistent",
                lease=lease,
                release=release,
            )
        try:
            target_ref = self._promotion_target_ref(release.target_branch)
        except ConfigError as error:
            return self._release_blocked(
                command,
                "invalid_target_branch",
                str(error),
                lease=lease,
                release=release,
            )
        source_validation = self._release_live_sources(command, release, target_ref)
        if isinstance(source_validation, CommandResult):
            return source_validation
        try:
            github = self.github or GhClient(self.git.repository_root())
            target_pull_request = github.view_pr(release.target_pr)
        except ExternalServiceError as error:
            return self._release_external(
                command,
                "release_pr_unavailable",
                str(error),
                lease=lease,
                release=release,
            )
        if (
            target_pull_request.base_ref != release.target_branch
            or target_pull_request.base_sha
            != release.last_verified_target_sha
            or target_pull_request.head_ref != lease.branch
            or target_pull_request.head_sha != release.published_head_sha
        ):
            return self._release_blocked(
                command,
                "target_pr_mismatch",
                "published release PR no longer matches its exact branch and head",
                lease=lease,
                release=release,
            )
        if target_pull_request.state not in {"OPEN", "MERGED"}:
            return self._release_blocked(
                command,
                "release_pr_closed",
                f"published release PR #{release.target_pr} is {target_pull_request.state}",
                lease=lease,
                release=release,
            )
        if target_pull_request.state == "OPEN":
            try:
                remote_head = self.git.remote_branch_sha(lease.branch)
            except GitRemoteError as error:
                return self._release_external(
                    command,
                    "release_publish_failed",
                    str(error),
                    lease=lease,
                    release=release,
                )
            if remote_head != release.published_head_sha:
                return self._release_blocked(
                    command,
                    "release_branch_changed",
                    "published release branch no longer matches its recorded head",
                    lease=lease,
                    release=release,
                )
        return CommandResult.ok(
            command,
            decision="reuse",
            lease=lease,
            release=release,
        )
    def _release_add_validation(
        self,
        command: str,
        release: ReleaseBridge,
        source_pr: int,
    ) -> ReleaseSource | CommandResult:
        lease = self.registry.get_lease_read_only(release.lease_id)
        if release.state is not ReleaseState.OPEN:
            return self._release_blocked(
                command,
                "release_not_open",
                f"release {release.release_id!r} is {release.state.value}",
                lease=lease,
                release=release,
            )
        try:
            target_ref = self._promotion_target_ref(release.target_branch)
        except ConfigError as error:
            return self._release_blocked(
                command,
                "invalid_target_branch",
                str(error),

                lease=lease,
                release=release,
            )
        live_sources = self._release_live_sources(command, release, target_ref)
        if isinstance(live_sources, CommandResult):
            return live_sources
        if any(source.source_pr == source_pr for source in release.sources):
            return CommandResult.ok(
                command,
                decision="reuse",
                lease=lease,
                release=release,
            )
        try:
            github = self.github or GhClient(self.git.repository_root())
            source = github.view_pr(source_pr)
        except ExternalServiceError as error:
            return self._release_external(
                command,
                "source_pr_unavailable",
                str(error),
                lease=lease,
                release=release,
            )
        return self._release_source_from_pull(
            command,
            release,
            source,
            target_ref,
            ordinal=len(release.sources),
            lease=lease,
        )

    def _release_live_sources(
        self,
        command: str,
        release: ReleaseBridge,
        target_ref: str,
    ) -> tuple[PullRequest, ...] | CommandResult:
        lease = self.registry.get_lease_read_only(release.lease_id)
        try:
            github = self.github or GhClient(self.git.repository_root())
            sources = tuple(
                github.view_pr(source.source_pr) for source in release.sources
            )
        except ExternalServiceError as error:
            return self._release_external(
                command,
                "source_pr_unavailable",
                str(error),
                lease=lease,
                release=release,
            )
        for source, pinned in zip(sources, release.sources, strict=True):
            if (
                source.base_ref != pinned.base_ref
                or source.base_sha != pinned.base_sha
                or source.head_sha != pinned.head_sha
                or source.merge_commit_sha != pinned.merge_sha
                or tuple(sorted(source.changed_paths)) != pinned.changed_paths
            ):
                return self._release_blocked(
                    command,
                    "source_provenance_changed",
                    (
                        f"source pull request #{pinned.source_pr} no longer matches "
                        "its immutable release pin"
                    ),
                    lease=lease,
                    release=release,
                )
            source_gate = self._promotion_source_blocker(source, target_ref)
            if source_gate is not None:
                blocker = source_gate.blockers[0]
                return self._release_blocked(
                    command,
                    blocker["code"],
                    blocker["message"],
                    lease=lease,
                    release=release,
                )
            invalid_oid = self._invalid_promotion_oid(source, require_merge=True)
            if invalid_oid is not None:
                return self._release_blocked(
                    command,
                    "source_pr_invalid_oid",
                    (
                        f"source pull request #{source.number} has an invalid "
                        f"{invalid_oid}"
                    ),
                    lease=lease,
                    release=release,
                )
        return sources

    def _release_source_from_pull(
        self,
        command: str,
        release: ReleaseBridge,
        source: PullRequest,
        target_ref: str,
        *,
        ordinal: int,
        lease: Lease | None,
    ) -> ReleaseSource | CommandResult:
        source_gate = self._promotion_source_blocker(source, target_ref)
        if source_gate is not None:
            blocker = source_gate.blockers[0]
            return self._release_blocked(
                command,
                blocker["code"],
                blocker["message"],
                lease=lease,
                release=release,
            )
        invalid_oid = self._invalid_promotion_oid(source, require_merge=True)
        if invalid_oid is not None:
            return self._release_blocked(
                command,
                "source_pr_invalid_oid",
                f"source pull request #{source.number} has an invalid {invalid_oid}",
                lease=lease,
                release=release,
            )
        if not source.changed_paths:
            return self._release_blocked(
                command,
                "source_pr_empty_delta",
                f"source pull request #{source.number} has no reviewed paths",
                lease=lease,
                release=release,
            )
        assert source.merge_commit_sha is not None
        try:
            return ReleaseSource(
                bridge_id=release.id,
                ordinal=ordinal,
                source_pr=source.number,
                base_ref=source.base_ref,
                base_sha=source.base_sha,
                head_sha=source.head_sha,
                merge_sha=source.merge_commit_sha,
                changed_paths=tuple(sorted(source.changed_paths)),
            )
        except ValueError as error:
            return self._release_blocked(
                command,
                "source_pr_invalid_metadata",
                str(error),
                lease=lease,
                release=release,
            )

    def _release_lease(self, command: str, release: ReleaseBridge) -> Lease:
        lease = self.registry.get_lease(release.lease_id)
        if lease is None:
            raise ValueError(
                f"{command}: release {release.release_id!r} has no registered promotion lease"
            )
        if (
            lease.purpose is not Purpose.PROMOTE
            or not lease.managed
            or lease.repository_id != release.repository_id
        ):
            raise ValueError(
                f"{command}: release {release.release_id!r} lease provenance is invalid"
            )
        return lease


    def _verify_release_bridge_live(
        self,
        command: str,
        release: ReleaseBridge,
        lease: Lease,
        *,
        target_sha: str | None = None,
        sources: tuple[PullRequest, ...] | None = None,
    ) -> tuple[Lease, str, tuple[dict[str, object], ...]] | CommandResult:
        next_target_sha = target_sha
        for attempt in range(2):
            rebuilt = self._rebuild_release_bridge(
                command,
                release,
                lease,
                verify=True,
                target_sha=next_target_sha,
                sources=sources,
            )
            if isinstance(rebuilt, CommandResult):
                return rebuilt
            lease, verified_target_sha, actions = rebuilt
            try:
                live_target_sha = self.git.remote_branch_sha(
                    release.target_branch
                )
            except GitRemoteError as error:
                return self._release_external(
                    command,
                    "target_ref_unavailable",
                    str(error),
                    lease=lease,
                    release=release,
                )
            if live_target_sha is None:
                return self._release_blocked(
                    command,
                    "target_ref_unavailable",
                    f"target branch {release.target_branch!r} is unavailable on origin",
                    lease=lease,
                    release=release,
                )
            if live_target_sha == verified_target_sha:
                return lease, verified_target_sha, actions
            if attempt == 1:
                break
            try:
                next_target_sha = self.git.fetch_ref(release.target_branch)
            except GitRemoteError as error:
                return self._release_external(
                    command,
                    "target_ref_unavailable",
                    str(error),
                    lease=lease,
                    release=release,
                )
            except GitError as error:
                return self._release_blocked(
                    command,
                    "target_ref_unavailable",
                    str(error),
                    lease=lease,
                    release=release,
                )
        return self._release_blocked(
            command,
            "release_target_changed",
            (
                f"target branch {release.target_branch!r} kept changing during "
                "release verification"
            ),
            lease=lease,
            release=release,
        )

    def _recover_pending_release_candidate(
        self,
        command: str,
        release: ReleaseBridge,
        lease: Lease,
    ) -> tuple[Lease, str] | CommandResult:
        try:
            actual_head_sha = self.git.head_sha(lease.worktree_path)
            message = self.git.commit_message(lease.worktree_path)
            lines = message.splitlines()
            target_prefix = "AWF-Target-Base: "
            if (
                len(lines) < 2
                or not lines[-2].startswith(target_prefix)
                or lines[-1] != f"AWF-Lease-ID: {lease.id}"
            ):
                raise ValueError("pending release candidate trailers do not match")
            target_sha = lines[-2][len(target_prefix) :]
            if _GIT_OBJECT_ID.fullmatch(target_sha) is None:
                raise ValueError("pending release candidate target is invalid")
            if message != self._release_message(release, target_sha, lease):
                raise ValueError("pending release candidate message does not match")
            if self.git.commit_parents(actual_head_sha) != (target_sha,):
                raise ValueError("pending release candidate parent does not match")
            expected_blob_heads: dict[str, str] = {}
            for source in release.sources:
                source_head_sha = self.git.fetch_ref(source.head_sha)
                if source_head_sha != source.head_sha:
                    raise ValueError("pending release source head changed")
                for path in source.changed_paths:
                    expected_blob_heads[path] = source_head_sha
            expected_blobs = self._promotion_net_blobs(
                expected_blob_heads,
                target_sha,
            )
            expected_paths = tuple(sorted(expected_blobs))
            if (
                not expected_paths
                or self.git.changed_paths(
                    lease.worktree_path,
                    target_sha,
                    actual_head_sha,
                    find_renames=True,
                )
                != expected_paths
                or any(
                    expected_blobs[path]
                    != self.git.path_blob(actual_head_sha, path)
                    for path in expected_paths
                )
            ):
                raise ValueError("pending release candidate content does not match")
        except (GitError, GitRemoteError, ValueError) as error:
            return self._release_blocked(
                command,
                "release_worktree_mismatch",
                str(error),
                lease=lease,
                release=release,
            )
        return (
            replace(
                lease,
                head_sha=actual_head_sha,
                target_base_sha=target_sha,
            ),
            target_sha,
        )

    def _release_gate_blocked(
        self,
        command: str,
        code: str,
        message: str,
        *,
        lease: Lease,
        release: ReleaseBridge,
    ) -> CommandResult:
        try:
            untracked = self.git.untracked_paths(lease.worktree_path)
            self.git.reset_hard(lease.worktree_path, lease.head_sha)
            self.git.remove_untracked_paths(lease.worktree_path, untracked)
            if self.git.status_porcelain(lease.worktree_path):
                raise GitError("release worktree remained dirty after gate rollback")
        except GitError as error:
            return self._release_blocked(
                command,
                "release_rollback_failed",
                str(error),
                lease=lease,
                release=release,
            )
        return self._release_blocked(
            command,
            code,
            message,
            lease=lease,
            release=release,
        )

    def _rebuild_release_bridge(
        self,
        command: str,
        release: ReleaseBridge,
        lease: Lease,
        *,
        verify: bool,
        target_sha: str | None = None,
        sources: tuple[PullRequest, ...] | None = None,
        record: bool = True,
    ) -> tuple[Lease, str, tuple[dict[str, object], ...]] | CommandResult:
        if not release.sources:
            return self._release_blocked(
                command,
                "release_empty",
                "release must contain at least one pinned source",
                lease=lease,
                release=release,
            )
        if lease.state not in {LeaseState.ACTIVE, LeaseState.PR_OPEN}:
            return self._release_blocked(
                command,
                "release_lease_not_active",
                f"release lease {lease.id} is {lease.state.value}",
                lease=lease,
                release=release,
            )
        try:
            target_ref = self._promotion_target_ref(release.target_branch)
        except ConfigError as error:
            return self._release_blocked(
                command,
                "invalid_target_branch",
                str(error),
                lease=lease,
                release=release,
            )
        try:
            dirty = self.git.status_porcelain(lease.worktree_path)
        except GitError as error:
            return self._release_blocked(
                command,
                "release_worktree_unavailable",
                str(error),
                lease=lease,
                release=release,
            )
        if dirty:
            return self._release_blocked(
                command,
                "release_worktree_dirty",
                "managed release worktree has uncommitted changes",
                lease=lease,
                release=release,
            )
        try:
            actual_head_sha = self.git.head_sha(lease.worktree_path)
        except GitError as error:
            return self._release_blocked(
                command,
                "release_worktree_unavailable",
                str(error),
                lease=lease,
                release=release,
            )
        if actual_head_sha != lease.head_sha:
            return self._release_blocked(
                command,
                "release_worktree_mismatch",
                "managed release worktree head does not match its registered head",
                lease=lease,
                release=release,
            )
        if verify and not self.config.verify_production:
            return self._release_blocked(
                command,
                "production_verify_missing",
                "verify.production.commands must configure at least one command",
                lease=lease,
                release=release,
            )
        if sources is None:
            sources = self._release_live_sources(command, release, target_ref)
            if isinstance(sources, CommandResult):
                return sources
        original_head_sha = lease.head_sha
        mutated = False
        try:
            actual_target_sha = (
                target_sha
                if target_sha is not None
                else self.git.fetch_ref(release.target_branch)
            )
            staging_sha = self.git.fetch_ref(self.config.default_base or "")
            deltas: list[bytes] = []
            expected_blob_heads: dict[str, str] = {}
            previous_merge_sha: str | None = None
            for source, pinned in zip(sources, release.sources, strict=True):
                source_base_sha = self.git.fetch_ref(pinned.base_sha)
                source_head_sha = self.git.fetch_ref(pinned.head_sha)
                source_merge_sha = self.git.fetch_ref(pinned.merge_sha)
                if (
                    source_base_sha != pinned.base_sha
                    or source_head_sha != pinned.head_sha
                    or source_merge_sha != pinned.merge_sha
                ):
                    return self._release_blocked(
                        command,
                        "source_sha_mismatch",
                        (
                            f"fetched source pull request #{pinned.source_pr} refs "
                            "do not match its immutable release pin"
                        ),
                        lease=lease,
                        release=release,
                    )
                merge_base = self.git.merge_base(source_base_sha, source_head_sha)
                patch = self.git.binary_diff(merge_base, source_head_sha)
                source_paths = self.git.changed_paths(
                    self.git.repository_root(),
                    merge_base,
                    source_head_sha,
                    find_renames=True,
                )
                if not patch:
                    return self._release_blocked(
                        command,
                        "source_pr_empty_delta",
                        (
                            f"source pull request #{pinned.source_pr} has no changes "
                            "after its merge base"
                        ),
                        lease=lease,
                        release=release,
                    )
                if source_paths != pinned.changed_paths:
                    return self._release_blocked(
                        command,
                        "source_delta_mismatch",
                        (
                            f"source pull request #{pinned.source_pr} paths do not "
                            "match its immutable reviewed Git delta"
                        ),
                        lease=lease,
                        release=release,
                    )
                if (
                    self.git.merge_base(source_base_sha, source_merge_sha)
                    != source_base_sha
                    or self.git.merge_base(source_merge_sha, staging_sha)
                    != source_merge_sha
                ):
                    return self._release_blocked(
                        command,
                        "source_merge_not_in_staging",
                        (
                            f"source pull request #{pinned.source_pr} merge commit "
                            "is not in configured staging history"
                        ),
                        lease=lease,
                        release=release,
                    )
                if (
                    previous_merge_sha is not None
                    and self.git.merge_base(previous_merge_sha, source_merge_sha)
                    != previous_merge_sha
                ):
                    return self._release_blocked(
                        command,
                        "source_pr_sequence_order",
                        (
                            f"source pull request #{pinned.source_pr} was not merged "
                            "after the preceding release source"
                        ),
                        lease=lease,
                        release=release,
                    )
                previous_merge_sha = source_merge_sha
                deltas.append(patch)
                for path in pinned.changed_paths:
                    expected_blob_heads[path] = source_head_sha
            expected_blobs = self._promotion_net_blobs(
                expected_blob_heads, actual_target_sha
            )
            expected_paths = tuple(sorted(expected_blobs))
            if not expected_paths:
                return self._release_blocked(
                    command,
                    "release_empty_delta",
                    "pinned release sources do not change the latest target",
                    lease=lease,
                    release=release,
                )
            self.git.reset_hard(lease.worktree_path, actual_target_sha)
            mutated = True
            for patch in deltas:
                self.git.apply_indexed_patch(lease.worktree_path, patch)
            promotion_head = self.git.commit(
                lease.worktree_path,
                self._release_message(release, actual_target_sha, lease),
            )
            promoted_paths = self.git.changed_paths(
                lease.worktree_path,
                actual_target_sha,
                promotion_head,
                find_renames=True,
            )
            if promoted_paths != expected_paths or any(
                expected_blobs[path] != self.git.path_blob(promotion_head, path)
                for path in expected_paths
            ):
                try:
                    self.git.reset_hard(lease.worktree_path, original_head_sha)
                except GitError as rollback_error:
                    return self._release_blocked(
                        command,
                        "release_rollback_failed",
                        str(rollback_error),
                        lease=lease,
                        release=release,
                    )
                mutated = False
                return self._release_blocked(
                    command,
                    "release_content_mismatch",
                    "release paths or blobs do not exactly match pinned source deltas",
                    lease=lease,
                    release=release,
                )
            if record:
                lease = self.registry.transition(
                    lease.id,
                    lease.state,
                    expected_version=lease.version,
                    event_type="release_rebuilt",
                    summary=(
                        f"release {release.release_id} reconstructed from "
                        f"{len(release.sources)} pinned sources"
                    ),
                    observed_head_sha=promotion_head,
                    head_sha=promotion_head,
                    target_base_sha=actual_target_sha,
                )
            else:
                lease = replace(
                    lease,
                    head_sha=promotion_head,
                    target_base_sha=actual_target_sha,
                )
            actions: tuple[dict[str, object], ...] = ()
            if verify:
                prepare_error = self._prepare(lease, force=True)
                if prepare_error is not None:
                    return self._release_gate_blocked(
                        command,
                        "release_prepare_failed",
                        prepare_error,
                        lease=lease,
                        release=release,
                    )
                if self.git.status_porcelain(lease.worktree_path):
                    return self._release_gate_blocked(
                        command,
                        "release_prepare_dirty",
                        "release prepare command left uncommitted changes",
                        lease=lease,
                        release=release,
                    )
                try:
                    actions = self._verify_promotion(lease.worktree_path)
                except RuntimeError as error:
                    return self._release_gate_blocked(
                        command,
                        "release_verification_failed",
                        str(error),
                        lease=lease,
                        release=release,
                    )
                if self.git.status_porcelain(lease.worktree_path):
                    return self._release_gate_blocked(
                        command,
                        "release_verification_dirty",
                        "release verification left uncommitted changes",
                        lease=lease,
                        release=release,
                    )
            return lease, actual_target_sha, actions
        except GitRemoteError as error:
            return self._release_external(
                command,
                "source_delta_unavailable",
                str(error),
                lease=lease,
                release=release,
            )
        except GitPatchConflict as error:
            if mutated:
                try:
                    self.git.reset_hard(lease.worktree_path, original_head_sha)
                except GitError as rollback_error:
                    return self._release_blocked(
                        command,
                        "release_rollback_failed",
                        str(rollback_error),
                        lease=lease,
                        release=release,
                    )
            return self._release_blocked(
                command,
                "release_apply_failed",
                str(error),
                lease=lease,
                release=release,
            )
        except (
            GitError,
            OSError,
            RuntimeError,
            sqlite3.Error,
            subprocess.SubprocessError,
        ) as error:
            if mutated:
                try:
                    self.git.reset_hard(lease.worktree_path, original_head_sha)
                except GitError as rollback_error:
                    return self._release_blocked(
                        command,
                        "release_rollback_failed",
                        str(rollback_error),
                        lease=lease,
                        release=release,
                    )
            return self._release_blocked(
                command,
                "release_apply_failed",
                str(error),
                lease=lease,
                release=release,
            )

    @staticmethod
    def _release_title(release: ReleaseBridge) -> str:
        return f"Release {release.release_id} to {release.target_branch}"

    @staticmethod
    def _release_trailers(
        release: ReleaseBridge, target_sha: str, lease: Lease
    ) -> tuple[str, ...]:
        return (
            f"AWF-Release-ID: {release.release_id}",
            *(
                trailer
                for source in release.sources
                for trailer in (
                    f"AWF-Source-PR: {source.source_pr}",
                    f"AWF-Source-Base: {source.base_sha}",
                    f"AWF-Source-Head: {source.head_sha}",
                    f"AWF-Source-Merge: {source.merge_sha}",
                )
            ),
            f"AWF-Target-Base: {target_sha}",
            f"AWF-Lease-ID: {lease.id}",
        )

    def _release_message(
        self, release: ReleaseBridge, target_sha: str, lease: Lease
    ) -> str:
        return "\n".join(
            (
                self._release_title(release),
                "",
                *self._release_trailers(release, target_sha, lease),
            )
        )

    def _release_body(
        self, release: ReleaseBridge, target_sha: str, lease: Lease
    ) -> str:
        return "\n".join(self._release_trailers(release, target_sha, lease))

    @staticmethod
    def _release_blocked(
        command: str,
        code: str,
        message: str,
        *,
        lease: Lease | None = None,
        release: ReleaseBridge | None = None,
    ) -> CommandResult:
        return CommandResult.blocked(
            command,
            blockers=({"code": code, "message": message},),
            lease=lease,
            release=release,
        )

    @staticmethod
    def _release_external(
        command: str,
        code: str,
        message: str,
        *,
        lease: Lease | None = None,
        release: ReleaseBridge | None = None,
    ) -> CommandResult:
        return CommandResult.external_error(
            command,
            code=code,
            message=message,
            lease=lease,
            release=release,
        )

    @staticmethod
    def _promotion_source_numbers(
        source_pr: int | Sequence[int],
    ) -> tuple[int, ...]:
        if isinstance(source_pr, int) and not isinstance(source_pr, bool):
            values: Sequence[int] = (source_pr,)
        elif isinstance(source_pr, Sequence) and not isinstance(
            source_pr, (str, bytes)
        ):
            values = source_pr
        else:
            raise ValueError(
                "source pull request must be a positive integer or a non-empty"
                " sequence of unique positive integers"
            )
        if (
            not values
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                for value in values
            )
            or len(set(values)) != len(values)
        ):
            raise ValueError(
                "source pull request must be a positive integer or a non-empty"
                " sequence of unique positive integers"
            )
        return tuple(values)

    @staticmethod
    def _promotion_excluded_paths(
        exclude_paths: Sequence[str],
    ) -> tuple[str, ...]:
        error_message = (
            "excluded paths must be a sequence of unique "
            "repository-relative paths"
        )
        if isinstance(exclude_paths, (str, bytes)) or not isinstance(
            exclude_paths, Sequence
        ):
            raise ValueError(error_message)
        values = tuple(exclude_paths)
        if any(not isinstance(path, str) for path in values):
            raise ValueError(error_message)
        if len(set(values)) != len(values):
            raise ValueError(error_message)
        for path in values:
            parsed = PurePosixPath(path)
            if (
                not path
                or "\0" in path
                or path.splitlines() != [path]
                or parsed.is_absolute()
                or ".." in parsed.parts
                or str(parsed) != path
            ):
                raise ValueError(error_message)
        return tuple(sorted(values))

    @staticmethod
    def _sync_initiative(source_branch: str, target_branch: str) -> str:
        digest = hashlib.sha256(
            f"{source_branch}\0{target_branch}".encode("utf-8")
        ).hexdigest()[:16]
        return f"sync-{digest}"

    @staticmethod
    def _sync_branch(initiative: str, source_sha: str) -> str:
        if _GIT_OBJECT_ID.fullmatch(source_sha) is None:
            raise ValueError("sync source SHA is invalid")
        return f"awf/{initiative}-{source_sha[:12]}/feature"

    def _sync_remote_drift_blocker(
        self,
        *,
        lease: Lease,
        source_branch: str,
        target_branch: str,
        source_sha: str,
        target_sha: str,
    ) -> CommandResult | None:
        try:
            live_source_sha = self.git.remote_branch_sha(source_branch)
            live_target_sha = self.git.remote_branch_sha(target_branch)
        except GitRemoteError as error:
            return self._external_error(
                "wt.sync", "sync_publish_failed", str(error), lease=lease
            )
        if live_source_sha != source_sha:
            return self._block_sync_lease(
                lease,
                "sync_source_changed",
                f"source branch {source_branch!r} changed after verification",
            )
        if live_target_sha != target_sha:
            return self._block_sync_lease(
                lease,
                "sync_target_changed",
                f"target branch {target_branch!r} changed after verification",
            )
        return None

    def _sync_refs(
        self, source_branch: str, target_branch: str
    ) -> tuple[str, str]:
        if not isinstance(source_branch, str) or not source_branch:
            raise ConfigError("sync source branch must be a non-empty string")
        if not isinstance(target_branch, str) or not target_branch:
            raise ConfigError("sync target branch must be a non-empty string")
        source_ref = self._remote_base_ref(source_branch)
        target_ref = self._remote_base_ref(target_branch)
        if source_ref != f"origin/{source_branch}":
            raise ConfigError("sync source branch must not include a ref prefix")
        if target_ref != f"origin/{target_branch}":
            raise ConfigError("sync target branch must not include a ref prefix")
        if source_ref == target_ref:
            raise ConfigError("sync source and target branches must differ")
        if self.config.production_branch is None:
            raise ConfigError(
                "worktree.production_branch must identify the production branch"
            )
        if self.config.default_base is None:
            raise ConfigError("worktree.default_base must identify the staging branch")
        configured_source = self._remote_base_ref(self.config.production_branch)
        configured_target = self._remote_base_ref(self.config.default_base)
        if source_ref != configured_source:
            raise ConfigError(
                "sync source branch must match configured production branch "
                f"{self.config.production_branch!r}"
            )
        if target_ref != configured_target:
            raise ConfigError(
                "sync target branch must match configured staging branch "
                f"{self.config.default_base!r}"
            )
        return source_ref, target_ref

    def _sync_expected_blobs(
        self, source_sha: str, target_sha: str
    ) -> tuple[str, dict[str, tuple[str, str] | None]]:
        merge_base = self.git.merge_base(source_sha, target_sha)
        source_paths = tuple(
            sorted(
                self.git.changed_path_endpoints(
                    self.git.repository_root(),
                    merge_base,
                    source_sha,
                )
            )
        )
        expected_entries = {
            path: source_entry
            for path in source_paths
            if (source_entry := self.git.path_entry(source_sha, path))
            != self.git.path_entry(target_sha, path)
        }
        return merge_base, expected_entries

    def _new_sync_lease(
        self,
        *,
        initiative: str,
        branch: str,
        source_ref: str,
        target_ref: str,
        source_sha: str,
        target_sha: str,
        merge_base: str,
        reviewed_paths: tuple[str, ...],
    ) -> Lease:
        lease = Lease.new(
            repository_id=self.git.repository_id(),
            repository_name=self.git.repository_name(),
            repository_root=self.git.repository_root(),
            worktree_path=self.cache_dir / self.git.repository_name(),
            initiative=initiative,
            purpose=Purpose.FEATURE,
            branch=branch,
            base_ref=target_ref,
            head_sha=target_sha,
            managed=True,
            owner_kind="awf",
            source_base_sha=merge_base,
            source_head_sha=source_sha,
            target_base_sha=target_sha,
            reviewed_paths=reviewed_paths,
        )
        return replace(
            lease,
            worktree_path=self.cache_dir / lease.repository_name / lease.id,
        )

    def _reuse_sync(self, lease: Lease, *, expected: Lease) -> CommandResult:
        if (
            lease.purpose is not Purpose.FEATURE
            or lease.branch != expected.branch
            or lease.base_ref != expected.base_ref
        ):
            return self._sync_blocked(
                "sync_lease_conflict",
                f"lease {lease.id} does not match the requested branch synchronization",
                lease=lease,
            )
        if (
            lease.source_base_sha != expected.source_base_sha
            or lease.source_head_sha != expected.source_head_sha
            or lease.target_base_sha != expected.target_base_sha
            or lease.reviewed_paths != expected.reviewed_paths
        ):
            return self._sync_blocked(
                "sync_lease_stale",
                (
                    f"lease {lease.id} pins an older source or target; finish its "
                    "pull request before starting another synchronization"
                ),
                lease=lease,
            )
        if lease.state is not LeaseState.PR_OPEN or lease.target_pr is None:
            return self._sync_blocked(
                "sync_lease_incomplete",
                f"lease {lease.id} is {lease.state.value}; preserve it for recovery",
                lease=lease,
            )
        worktree = self._registered_worktree(lease)
        if (
            worktree is None
            or worktree.branch != lease.branch
            or worktree.detached
            or worktree.bare
        ):
            return self._sync_blocked(
                "orphaned_sync_lease",
                f"lease {lease.id} is not registered as its managed Git worktree",
                lease=lease,
            )
        if self.git.status_porcelain(lease.worktree_path):
            return self._sync_blocked(
                "dirty_sync_lease",
                f"lease {lease.id} has uncommitted changes",
                lease=lease,
            )
        if self.git.head_sha(lease.worktree_path) != lease.head_sha:
            return self._sync_blocked(
                "sync_lease_head_mismatch",
                f"lease {lease.id} worktree head changed",
                lease=lease,
            )
        return CommandResult.ok("wt.sync", decision="reuse", lease=lease)

    def _resume_sync_publication(
        self,
        lease: Lease,
        *,
        expected: Lease,
        github: GhClient,
        source_branch: str,
        target_branch: str,
    ) -> CommandResult:
        if lease.state is LeaseState.PR_OPEN:
            return self._reuse_sync(lease, expected=expected)
        if (
            lease.purpose is not Purpose.FEATURE
            or lease.branch != expected.branch
            or lease.base_ref != expected.base_ref
            or lease.source_base_sha != expected.source_base_sha
            or lease.source_head_sha != expected.source_head_sha
            or lease.target_base_sha != expected.target_base_sha
            or lease.reviewed_paths != expected.reviewed_paths
        ):
            return self._sync_blocked(
                "sync_lease_stale",
                (
                    f"lease {lease.id} does not match the current source and target "
                    "provenance"
                ),
                lease=lease,
            )
        if lease.state not in {LeaseState.ACTIVE, LeaseState.BLOCKED}:
            return self._sync_blocked(
                "sync_lease_incomplete",
                f"lease {lease.id} is {lease.state.value}; preserve it for recovery",
                lease=lease,
            )
        assert expected.source_base_sha is not None
        assert expected.source_head_sha is not None
        assert expected.target_base_sha is not None
        recovered = self._recover_sync_noop(
            lease=lease,
            expected=expected,
            github=github,
            source_branch=source_branch,
            target_branch=target_branch,
            source_sha=expected.source_head_sha,
            target_sha=expected.target_base_sha,
            merge_base=expected.source_base_sha,
        )
        if recovered is not None:
            return recovered
        worktree = self._registered_worktree(lease)
        if (
            worktree is None
            or worktree.branch != lease.branch
            or worktree.detached
            or worktree.bare
        ):
            return self._sync_blocked(
                "orphaned_sync_lease",
                f"lease {lease.id} is not registered as its managed Git worktree",
                lease=lease,
            )
        if self.git.status_porcelain(lease.worktree_path):
            return self._sync_blocked(
                "dirty_sync_lease",
                f"lease {lease.id} has uncommitted changes",
                lease=lease,
            )
        actual_head = self.git.head_sha(lease.worktree_path)
        assert lease.source_base_sha is not None
        assert lease.source_head_sha is not None
        assert lease.target_base_sha is not None
        expected_message = self._sync_message(
            source_branch=source_branch,
            target_branch=target_branch,
            merge_base=lease.source_base_sha,
            source_sha=lease.source_head_sha,
            target_sha=lease.target_base_sha,
            lease=lease,
        )
        legacy_expected_message = self._legacy_sync_message(
            source_branch=source_branch,
            target_branch=target_branch,
            merge_base=lease.source_base_sha,
            source_sha=lease.source_head_sha,
            target_sha=lease.target_base_sha,
            lease=lease,
        )
        if (
            actual_head == lease.target_base_sha
            or self.git.commit_parents(actual_head)
            not in {
                (lease.target_base_sha,),
                (lease.target_base_sha, lease.source_head_sha),
            }
            or self.git.commit_message(lease.worktree_path)
            not in {expected_message, legacy_expected_message}
        ):
            return self._sync_blocked(
                "sync_lease_head_mismatch",
                f"lease {lease.id} does not retain its verified synchronization commit",
                lease=lease,
            )
        synced_paths = tuple(
            sorted(
                self.git.changed_path_endpoints(
                    lease.worktree_path,
                    lease.target_base_sha,
                    actual_head,
                )
            )
        )
        if synced_paths != lease.reviewed_paths:
            return self._block_sync_lease(
                lease,
                "sync_delta_mismatch",
                "synchronization commit changed paths outside the source-only delta",
            )
        if actual_head != lease.head_sha:
            try:
                lease = self.registry.transition(
                    lease.id,
                    LeaseState.ACTIVE,
                    expected_version=lease.version,
                    event_type="sync_publish_pending",
                    summary="synchronization commit reconciled; publication pending",
                    observed_head_sha=actual_head,
                    head_sha=actual_head,
                )
            except (RuntimeError, sqlite3.Error) as error:
                return self._sync_blocked(
                    "registry_conflict", str(error), lease=lease
                )
        prepare_error = self._prepare(lease, force=False)
        if prepare_error is not None:
            return self._block_sync_lease(
                lease, "sync_prepare_failed", prepare_error
            )
        if self.git.status_porcelain(lease.worktree_path):
            return self._block_sync_lease(
                lease,
                "sync_prepare_dirty",
                "sync prepare command left uncommitted changes",
            )
        try:
            verification_actions = self._verify_promotion(lease.worktree_path)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            return self._block_sync_lease(
                lease, "sync_apply_failed", str(error)
            )
        drift_blocker = self._sync_remote_drift_blocker(
            lease=lease,
            source_branch=source_branch,
            target_branch=target_branch,
            source_sha=lease.source_head_sha,
            target_sha=lease.target_base_sha,
        )
        if drift_blocker is not None:
            return drift_blocker
        try:
            remote_sync_sha = self.git.remote_branch_sha(lease.branch)
            if remote_sync_sha is None:
                self.git.push_branch(lease.worktree_path, lease.branch)
            elif remote_sync_sha != actual_head:
                return self._block_sync_lease(
                    lease,
                    "sync_branch_changed",
                    f"remote sync branch {lease.branch!r} changed",
                )
            target_pull_request = github.find_open_pr(
                head=lease.branch, base=target_branch
            )
            expected_body = self._sync_body(
                source_branch=source_branch,
                target_branch=target_branch,
                merge_base=lease.source_base_sha,
                source_sha=lease.source_head_sha,
                target_sha=lease.target_base_sha,
                lease=lease,
            )
            if target_pull_request is None:
                target_pull_request = github.create_pr(
                    base=target_branch,
                    head=lease.branch,
                    title=f"Sync {source_branch} to {target_branch}",
                    body=expected_body,
                )
        except (ExternalServiceError, GitRemoteError) as error:
            return self._external_error(
                "wt.sync", "sync_publish_failed", str(error), lease=lease
            )
        if (
            target_pull_request.state != "OPEN"
            or target_pull_request.base_ref != target_branch
            or target_pull_request.base_sha != lease.target_base_sha
            or target_pull_request.head_ref != lease.branch
            or target_pull_request.head_sha != actual_head
            or target_pull_request.body != expected_body
        ):
            return self._block_sync_lease(
                lease,
                "sync_pr_mismatch",
                "GitHub did not return the exact open synchronization pull request",
            )
        drift_blocker = self._sync_remote_drift_blocker(
            lease=lease,
            source_branch=source_branch,
            target_branch=target_branch,
            source_sha=lease.source_head_sha,
            target_sha=lease.target_base_sha,
        )
        if drift_blocker is not None:
            return drift_blocker
        try:
            lease = self.registry.transition(
                lease.id,
                LeaseState.PR_OPEN,
                expected_version=lease.version,
                event_type="sync_pr_reconciled",
                summary=f"sync PR #{target_pull_request.number} reconciled",
                observed_head_sha=actual_head,
                pr_number=target_pull_request.number,
                head_sha=actual_head,
            )
        except (RuntimeError, sqlite3.Error) as error:
            return self._sync_blocked(
                "registry_conflict", str(error), lease=lease
            )
        return CommandResult.ok(
            "wt.sync",
            decision="ready",
            lease=lease,
            actions=verification_actions,
        )

    def _recover_sync_noop(
        self,
        *,
        lease: Lease,
        expected: Lease,
        github: GhClient,
        source_branch: str,
        target_branch: str,
        source_sha: str,
        target_sha: str,
        merge_base: str,
    ) -> CommandResult | None:
        if (
            lease.repository_root != expected.repository_root
            or lease.initiative != expected.initiative
            or not lease.managed
            or lease.owner_kind != "awf"
        ):
            return self._sync_blocked(
                "sync_lease_stale",
                (
                    f"lease {lease.id} does not match the current source and target "
                    "provenance"
                ),
                lease=lease,
            )
        try:
            events = self.registry.list_events(lease.id)
        except (RuntimeError, sqlite3.Error) as error:
            return self._sync_blocked(
                "registry_conflict", str(error), lease=lease
            )
        for event in reversed(events):
            if event.event_type in {"cleanup_reserved", "cleanup_released"}:
                continue
            if event.event_type == "sync_noop_pending":
                break
            if event.event_type == "sync_blocked":
                if (
                    event.to_state is LeaseState.BLOCKED
                    and event.summary.startswith("sync_apply_failed:")
                ):
                    break
            return None
        else:
            return None
        try:
            reservation = self.registry.get_cleanup_reservation(lease.id)
        except sqlite3.Error as error:
            return self._sync_blocked(
                "registry_conflict", str(error), lease=lease
            )
        if reservation is not None:
            if lease.head_sha != target_sha:
                return self._sync_blocked(
                    "sync_lease_head_mismatch",
                    f"lease {lease.id} worktree head changed before noop recovery",
                    lease=lease,
                )
            if reservation.branch_sha != target_sha:
                return self._sync_blocked(
                    "cleanup_reserved",
                    (
                        f"lease {lease.id} cleanup reservation does not match the "
                        "target head"
                    ),
                    lease=lease,
                )
            try:
                remote_sync_sha = self.git.remote_branch_sha(lease.branch)
                target_pull_request = github.find_open_pr(
                    head=lease.branch, base=target_branch
                )
            except (ExternalServiceError, GitRemoteError) as error:
                return self._external_error(
                    "wt.sync", "sync_publish_failed", str(error), lease=lease
                )
            if remote_sync_sha is not None:
                return self._sync_blocked(
                    "sync_branch_exists",
                    (
                        f"remote branch {lease.branch!r} exists while recovering "
                        "the no-op synchronization"
                    ),
                    lease=lease,
                )
            if target_pull_request is not None:
                return self._sync_blocked(
                    "sync_pr_open",
                    (
                        f"sync pull request #{target_pull_request.number} exists "
                        "while recovering the no-op synchronization"
                    ),
                    lease=lease,
                )
            return self._recover_sync_noop_cleanup_reservation(
                lease,
                reservation,
                source_branch=source_branch,
                target_branch=target_branch,
                source_sha=source_sha,
                target_sha=target_sha,
                merge_base=merge_base,
                removal_error=None,
            )
        if lease.head_sha != target_sha:
            return None
        try:
            if self.git.head_sha(lease.worktree_path) != target_sha:
                return None
        except (GitError, OSError) as error:
            return self._sync_blocked(
                "sync_apply_failed", str(error), lease=lease
            )
        return self._finalize_sync_noop(
            lease=lease,
            expected=expected,
            github=github,
            source_branch=source_branch,
            target_branch=target_branch,
            source_sha=source_sha,
            target_sha=target_sha,
            merge_base=merge_base,
        )

    def _finalize_sync_noop(
        self,
        *,
        lease: Lease,
        expected: Lease,
        github: GhClient,
        source_branch: str,
        target_branch: str,
        source_sha: str,
        target_sha: str,
        merge_base: str,
    ) -> CommandResult:
        try:
            current = self.registry.get_lease(lease.id)
        except (RuntimeError, sqlite3.Error) as error:
            return self._sync_blocked(
                "registry_conflict", str(error), lease=lease
            )
        if current is None:
            return self._sync_blocked(
                "sync_lease_incomplete",
                f"lease {lease.id} changed before noop cleanup could begin",
            )
        if (
            current.repository_id != expected.repository_id
            or current.repository_root != expected.repository_root
            or current.initiative != expected.initiative
            or current.purpose is not Purpose.FEATURE
            or not current.managed
            or current.owner_kind != "awf"
            or current.branch != expected.branch
            or current.base_ref != expected.base_ref
            or current.source_base_sha != merge_base
            or current.source_head_sha != source_sha
            or current.target_base_sha != target_sha
            or current.reviewed_paths != expected.reviewed_paths
        ):
            return self._sync_blocked(
                "sync_lease_stale",
                (
                    f"lease {current.id} does not match the current source and target "
                    "provenance"
                ),
                lease=current,
            )
        if current.state not in {LeaseState.ACTIVE, LeaseState.BLOCKED}:
            return self._sync_blocked(
                "sync_lease_incomplete",
                f"lease {current.id} is {current.state.value}; preserve it for recovery",
                lease=current,
            )
        if current.target_pr is not None:
            return self._sync_blocked(
                "sync_pr_open",
                f"lease {current.id} already has a synchronization pull request",
                lease=current,
            )
        path_blocker = self._cleanup_path_blocker(current)
        if path_blocker is not None:
            return self._block_sync_lease(
                current, path_blocker["code"], path_blocker["message"]
            )
        try:
            worktree = self._registered_worktree(current)
            if (
                worktree is None
                or worktree.branch != current.branch
                or worktree.detached
                or worktree.bare
            ):
                return self._sync_blocked(
                    "orphaned_sync_lease",
                    (
                        f"lease {current.id} is not registered as its managed Git "
                        "worktree"
                    ),
                    lease=current,
                )
            if self.git.status_porcelain(current.worktree_path):
                return self._sync_blocked(
                    "dirty_sync_lease",
                    f"lease {current.id} has uncommitted changes",
                    lease=current,
                )
            actual_head = self.git.head_sha(current.worktree_path)
            branch_sha = self.git.resolve_ref(current.branch)
            index_tree = self.git.index_tree_sha(current.worktree_path)
            target_tree = self.git.commit_tree_sha(
                target_sha, current.worktree_path
            )
        except (GitError, OSError) as error:
            return self._block_sync_lease(
                current, "sync_apply_failed", str(error)
            )
        if (
            current.head_sha != target_sha
            or actual_head != target_sha
            or branch_sha != target_sha
        ):
            return self._block_sync_lease(
                current,
                "sync_lease_head_mismatch",
                f"lease {current.id} worktree head changed",
            )
        if index_tree != target_tree:
            return self._block_sync_lease(
                current,
                "sync_delta_mismatch",
                "synchronization index does not match the target tree",
            )
        drift_blocker = self._sync_remote_drift_blocker(
            lease=current,
            source_branch=source_branch,
            target_branch=target_branch,
            source_sha=source_sha,
            target_sha=target_sha,
        )
        if drift_blocker is not None:
            return drift_blocker
        try:
            remote_sync_sha = self.git.remote_branch_sha(current.branch)
            target_pull_request = github.find_open_pr(
                head=current.branch, base=target_branch
            )
        except (ExternalServiceError, GitRemoteError) as error:
            return self._external_error(
                "wt.sync", "sync_publish_failed", str(error), lease=current
            )
        if remote_sync_sha is not None:
            return self._block_sync_lease(
                current,
                "sync_branch_exists",
                (
                    f"remote branch {current.branch!r} exists while recovering "
                    "the no-op synchronization"
                ),
            )
        if target_pull_request is not None:
            return self._block_sync_lease(
                current,
                "sync_pr_open",
                (
                    f"sync pull request #{target_pull_request.number} exists while "
                    "recovering the no-op synchronization"
                ),
            )
        try:
            reservation = self.registry.reserve_cleanup(
                current.id,
                expected_version=current.version,
                branch_sha=target_sha,
            )
        except (RuntimeError, sqlite3.Error) as error:
            return self._sync_blocked(
                "registry_conflict", str(error), lease=current
            )
        post_lock_failure: tuple[str, str] | None = None
        registry_error: RuntimeError | sqlite3.Error | None = None
        removal_error: GitError | OSError | None = None
        hold_error: GitError | OSError | None = None
        worktree_removed = False
        try:
            with self.git.hold_worktree_branch_if_at(
                current.worktree_path, current.branch, reservation.branch_sha
            ):
                try:
                    reserved_current = self.registry.get_lease(current.id)
                    active_reservation = self.registry.get_cleanup_reservation(
                        current.id
                    )
                except (RuntimeError, sqlite3.Error) as error:
                    registry_error = error
                else:
                    if (
                        reserved_current is None
                        or reserved_current.version != reservation.reserved_version
                        or active_reservation != reservation
                    ):
                        post_lock_failure = (
                            "lease_changed",
                            (
                                f"lease {current.id} changed while noop cleanup was "
                                "reserved"
                            ),
                        )
                    elif self.git.status_porcelain(current.worktree_path):
                        post_lock_failure = (
                            "dirty_sync_lease",
                            f"lease {current.id} has uncommitted changes",
                        )
                    elif (
                        self.git.head_sha(current.worktree_path) != target_sha
                        or self.git.index_tree_sha(current.worktree_path)
                        != target_tree
                    ):
                        post_lock_failure = (
                            "sync_lease_head_mismatch",
                            (
                                f"lease {current.id} worktree changed before noop "
                                "cleanup"
                            ),
                        )
                    else:
                        try:
                            self.git.remove_worktree(current.worktree_path)
                        except (GitError, OSError) as error:
                            removal_error = error
                        else:
                            worktree_removed = True
        except (GitError, OSError) as error:
            hold_error = error
        if registry_error is not None:
            return self._release_sync_noop_cleanup_reservation(
                current,
                reservation,
                code="registry_conflict",
                message=(
                    f"unable to revalidate cleanup reservation for lease "
                    f"{current.id}: {registry_error}"
                ),
            )
        if worktree_removed or removal_error is not None:
            return self._recover_sync_noop_cleanup_reservation(
                current,
                reservation,
                source_branch=source_branch,
                target_branch=target_branch,
                source_sha=source_sha,
                target_sha=target_sha,
                merge_base=merge_base,
                removal_error=removal_error or hold_error,
            )
        if hold_error is not None:
            return self._release_sync_noop_cleanup_reservation(
                current,
                reservation,
                code="sync_cleanup_hold_failed",
                message=(
                    f"unable to hold sync branch {current.branch!r} for noop "
                    f"cleanup: {hold_error}"
                ),
            )
        if post_lock_failure is not None:
            return self._release_sync_noop_cleanup_reservation(
                current,
                reservation,
                code=post_lock_failure[0],
                message=post_lock_failure[1],
            )
        return self._complete_sync_noop_cleanup(
            current,
            reservation,
            source_branch=source_branch,
            target_branch=target_branch,
            source_sha=source_sha,
            target_sha=target_sha,
            merge_base=merge_base,
        )

    def _recover_sync_noop_cleanup_reservation(
        self,
        lease: Lease,
        reservation: CleanupReservation,
        *,
        source_branch: str,
        target_branch: str,
        source_sha: str,
        target_sha: str,
        merge_base: str,
        removal_error: Exception | None,
    ) -> CommandResult:
        path_blocker = self._cleanup_path_blocker(lease)
        if path_blocker is not None:
            return self._release_sync_noop_cleanup_reservation(
                lease,
                reservation,
                code=path_blocker["code"],
                message=path_blocker["message"],
            )
        try:
            worktrees = self.git.list_worktrees()
        except (GitError, OSError) as error:
            return self._release_sync_noop_cleanup_reservation(
                lease,
                reservation,
                code="worktree_inspection_failed",
                message=(
                    f"unable to inspect worktree for lease {lease.id}: {error}"
                ),
            )
        path_is_absent = (
            not lease.worktree_path.exists()
            and all(worktree.path != lease.worktree_path for worktree in worktrees)
        )
        if path_is_absent:
            return self._complete_sync_noop_cleanup(
                lease,
                reservation,
                source_branch=source_branch,
                target_branch=target_branch,
                source_sha=source_sha,
                target_sha=target_sha,
                merge_base=merge_base,
            )
        detail = f": {removal_error}" if removal_error is not None else ""
        return self._release_sync_noop_cleanup_reservation(
            lease,
            reservation,
            code="worktree_remove_failed",
            message=f"unable to remove worktree for lease {lease.id}{detail}",
        )

    def _release_sync_noop_cleanup_reservation(
        self,
        lease: Lease,
        reservation: CleanupReservation,
        *,
        code: str,
        message: str,
    ) -> CommandResult:
        try:
            released = self.registry.release_cleanup_reservation(
                lease.id, expected_version=reservation.reserved_version
            )
        except (RuntimeError, sqlite3.Error) as error:
            return self._sync_blocked(
                "cleanup_reserved",
                f"lease {lease.id} remains reserved for cleanup: {error}",
                lease=lease,
            )
        return self._sync_blocked(code, message, lease=released)

    def _complete_sync_noop_cleanup(
        self,
        lease: Lease,
        reservation: CleanupReservation,
        *,
        source_branch: str,
        target_branch: str,
        source_sha: str,
        target_sha: str,
        merge_base: str,
    ) -> CommandResult:
        try:
            removed = self.registry.complete_cleanup(
                lease.id, expected_version=reservation.reserved_version
            )
        except (RuntimeError, sqlite3.Error) as error:
            return self._sync_blocked(
                "registry_conflict",
                (
                    f"worktree for lease {lease.id} was removed but cleanup could not "
                    f"be completed: {error}"
                ),
                lease=lease,
            )
        actions: list[dict[str, object]] = [
            self._cleanup_action("remove_worktree", removed)
        ]
        warnings: list[dict[str, str]] = []
        try:
            self.git.delete_branch_if_at(removed.branch, reservation.branch_sha)
        except (GitError, OSError) as error:
            self._branch_cleanup_warning(
                removed,
                "local_branch_cleanup_failed",
                f"could not delete local branch {removed.branch!r}: {error}",
                warnings,
            )
        else:
            actions.append(self._cleanup_action("delete_local_branch", removed))
        return self._sync_noop(
            source_branch,
            target_branch,
            source_sha,
            target_sha,
            merge_base,
            actions=tuple(actions),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _sync_noop(
        source_branch: str,
        target_branch: str,
        source_sha: str,
        target_sha: str,
        merge_base: str,
        *,
        actions: tuple[dict[str, object], ...] = (),
        warnings: tuple[dict[str, str], ...] = (),
    ) -> CommandResult:
        return CommandResult.ok(
            "wt.sync",
            decision="noop",
            actions=(
                {
                    "kind": "no_sync_required",
                    "source_branch": source_branch,
                    "source_head_sha": source_sha,
                    "target_branch": target_branch,
                    "target_head_sha": target_sha,
                    "merge_base_sha": merge_base,
                },
                *actions,
            ),
            warnings=warnings,
        )

    @staticmethod
    def _sync_trailers(
        *,
        source_branch: str,
        target_branch: str,
        merge_base: str,
        source_sha: str,
        target_sha: str,
        lease: Lease,
    ) -> tuple[str, ...]:
        return (
            f"AWF-Sync-From: {source_branch}",
            f"AWF-Sync-To: {target_branch}",
            f"AWF-Sync-Base: {merge_base}",
            f"AWF-Sync-Head: {source_sha}",
            f"AWF-Sync-Target: {target_sha}",
            f"AWF-Lease: {lease.id}",
            "AWF-No-Promote: true",
        )

    def _sync_message(
        self,
        *,
        source_branch: str,
        target_branch: str,
        merge_base: str,
        source_sha: str,
        target_sha: str,
        lease: Lease,
    ) -> str:
        return "\n".join(
            (
                f"chore(sync): sync {source_branch} to {target_branch}",
                "",
                *self._sync_trailers(
                    source_branch=source_branch,
                    target_branch=target_branch,
                    merge_base=merge_base,
                    source_sha=source_sha,
                    target_sha=target_sha,
                    lease=lease,
                ),
            )
        )

    def _legacy_sync_message(
        self,
        *,
        source_branch: str,
        target_branch: str,
        merge_base: str,
        source_sha: str,
        target_sha: str,
        lease: Lease,
    ) -> str:
        return "\n".join(
            (
                f"Sync {source_branch} to {target_branch}",
                "",
                *self._sync_trailers(
                    source_branch=source_branch,
                    target_branch=target_branch,
                    merge_base=merge_base,
                    source_sha=source_sha,
                    target_sha=target_sha,
                    lease=lease,
                ),
            )
        )

    def _sync_body(
        self,
        *,
        source_branch: str,
        target_branch: str,
        merge_base: str,
        source_sha: str,
        target_sha: str,
        lease: Lease,
    ) -> str:
        return "\n".join(
            self._sync_trailers(
                source_branch=source_branch,
                target_branch=target_branch,
                merge_base=merge_base,
                source_sha=source_sha,
                target_sha=target_sha,
                lease=lease,
            )
        )

    def _block_sync_lease(
        self,
        lease: Lease,
        code: str,
        message: str,
        *,
        conflicted_paths: tuple[str, ...] | None = None,
    ) -> CommandResult:
        try:
            current = self.registry.get_lease(lease.id)
            if current is not None and current.state is not LeaseState.BLOCKED:
                lease = self.registry.transition(
                    current.id,
                    LeaseState.BLOCKED,
                    expected_version=current.version,
                    event_type="sync_blocked",
                    summary=f"{code}: {message}",
                    observed_head_sha=self.git.head_sha(current.worktree_path),
                    conflicted_paths=conflicted_paths,
                )
            elif current is not None:
                lease = current
        except (GitError, OSError, RuntimeError, sqlite3.Error):
            pass
        return self._sync_blocked(code, message, lease=lease)

    @staticmethod
    def _sync_blocked(
        code: str, message: str, *, lease: Lease | None = None
    ) -> CommandResult:
        return CommandResult.blocked(
            "wt.sync",
            blockers=({"code": code, "message": message},),
            lease=lease,
        )

    def _promotion_net_blobs(
        self,
        expected_blob_heads: Mapping[str, str],
        target_sha: str,
    ) -> dict[str, str | None]:
        expected_blobs: dict[str, str | None] = {}
        for path, source_head_sha in expected_blob_heads.items():
            expected_blob = self.git.path_blob(source_head_sha, path)
            if expected_blob != self.git.path_blob(target_sha, path):
                expected_blobs[path] = expected_blob
        return expected_blobs

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

    def _promotion_sources_blocker(
        self, sources: Sequence[PullRequest], target_ref: str
    ) -> CommandResult | None:
        for source in sources:
            blocker = self._promotion_source_blocker(source, target_ref)
            if blocker is not None:
                return blocker
        return None

    def _promotion_source_blocker(
        self, source: PullRequest, target_ref: str
    ) -> CommandResult | None:
        sync_branch = _SYNC_BRANCH.fullmatch(source.head_ref) is not None
        if sync_branch or any(
            line.strip() == "AWF-No-Promote: true"
            for line in source.body.splitlines()
        ):
            return self._promotion_blocked(
                "source_pr_not_promotable",
                (
                    f"source pull request #{source.number} is a branch "
                    "synchronization and must not be promoted"
                ),
            )
        if source.state != "MERGED":
            return self._promotion_blocked(
                "source_pr_not_merged",
                f"source pull request #{source.number} is {source.state}",
            )
        review_accepted = source.review_decision == "APPROVED"
        if self.config.source_review_policy == "approved_or_self_merged":
            review_accepted = review_accepted or _same_github_actor(
                source.author_login, source.merged_by_login
            )
        if not review_accepted:
            if self.config.source_review_policy == "approved_or_self_merged":
                message = (
                    f"source pull request #{source.number} is neither approved"
                    " nor self-merged"
                )
            else:
                message = f"source pull request #{source.number} is not approved"
            return self._promotion_blocked("source_pr_not_approved", message)
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
    def _invalid_promotion_oid(
        source: PullRequest,
        *,
        require_merge: bool = False,
    ) -> str | None:
        for name, value in (
            ("base SHA", source.base_sha),
            ("head SHA", source.head_sha),
            ("merge SHA", source.merge_commit_sha),
        ):
            if value is not None and re.fullmatch(
                r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value
            ) is None:
                return name
        if require_merge and source.merge_commit_sha is None:
            return "merge SHA"
        return None

    def _new_promotion_lease(
        self,
        sources: Sequence[PullRequest],
        target_ref: str,
        target_sha: str,
        excluded_paths: Sequence[str],
        promotion_mode: PromotionMode = PromotionMode.EXACT,
        retry_identity: _PromotionRetryIdentity | None = None,
    ) -> Lease:
        target_branch = target_ref[len("origin/") :]
        initiative = (
            retry_identity.initiative
            if retry_identity is not None
            else self._promotion_initiative(
                tuple(source.number for source in sources),
                target_branch,
                excluded_paths,
                promotion_mode=promotion_mode,
            )
        )
        lease = Lease.new(
            repository_id=self.git.repository_id(),
            repository_name=self.git.repository_name(),
            repository_root=self.git.repository_root(),
            worktree_path=self.cache_dir / self.git.repository_name(),
            initiative=initiative,
            purpose=Purpose.PROMOTE,
            branch=(
                retry_identity.branch
                if retry_identity is not None
                else self._promotion_branch(initiative)
            ),
            base_ref=target_ref,
            head_sha=target_sha,
            managed=True,
            owner_kind="awf",
            source_pr=sources[0].number,
            promotion_mode=promotion_mode,
            source_base_sha=(
                sources[0].base_sha
                if promotion_mode is PromotionMode.OUT_OF_ORDER
                else None
            ),
            source_head_sha=(
                sources[0].head_sha
                if promotion_mode is PromotionMode.OUT_OF_ORDER
                else None
            ),
            target_base_sha=target_sha,
            reviewed_paths=(
                tuple(
                    sorted(
                        {
                            path
                            for source in sources
                            for path in source.changed_paths
                        }
                    )
                )
                if promotion_mode is PromotionMode.OUT_OF_ORDER
                else ()
            ),
        )
        return replace(
            lease,
            worktree_path=(
                retry_identity.worktree_path
                if retry_identity is not None
                else self.cache_dir / lease.repository_name / lease.id
            ),
        )

    @staticmethod
    def _promotion_source_pins(
        lease: Lease, sources: Sequence[PullRequest]
    ) -> tuple[PromotionSource, ...]:
        pins: list[PromotionSource] = []
        for ordinal, source in enumerate(sources):
            if source.merge_commit_sha is None:
                raise ValueError(
                    f"source pull request #{source.number} has an invalid merge SHA"
                )
            pins.append(
                PromotionSource(
                    lease_id=lease.id,
                    ordinal=ordinal,
                    source_pr=source.number,
                    base_ref=source.base_ref,
                    base_sha=source.base_sha,
                    head_sha=source.head_sha,
                    merge_sha=source.merge_commit_sha,
                    changed_paths=tuple(sorted(source.changed_paths)),
                )
            )
        return tuple(pins)

    @staticmethod
    def _legacy_promotion_source_pins_match(
        lease: Lease, sources: Sequence[PullRequest]
    ) -> bool:
        return (
            lease.purpose is Purpose.PROMOTE
            and lease.promotion_mode is PromotionMode.OUT_OF_ORDER
            and len(sources) == 1
            and lease.source_pr == sources[0].number
            and lease.source_base_sha == sources[0].base_sha
            and lease.source_head_sha == sources[0].head_sha
            and lease.reviewed_paths == tuple(sorted(sources[0].changed_paths))
            and sources[0].merge_commit_sha is not None
        )

    def _promotion_source_pins_match(
        self,
        lease: Lease,
        sources: Sequence[PullRequest],
        *,
        read_only: bool = False,
    ) -> bool:
        persisted = (
            self.registry.get_promotion_sources_read_only(lease.id)
            if read_only
            else self.registry.get_promotion_sources(lease.id)
        )
        return (
            persisted == self._promotion_source_pins(lease, sources)
            if persisted
            else self._legacy_promotion_source_pins_match(lease, sources)
        )

    def _backfill_legacy_promotion_source_pins(
        self, lease: Lease, sources: Sequence[PullRequest]
    ) -> bool:
        if not self._legacy_promotion_source_pins_match(lease, sources):
            return False
        pins = self._promotion_source_pins(lease, sources)
        return self.registry.backfill_promotion_sources(lease, pins) == pins

    @staticmethod
    def _promotion_initiative(
        source_prs: Sequence[int],
        target_branch: str,
        excluded_paths: Sequence[str] = (),
        promotion_mode: PromotionMode = PromotionMode.EXACT,
    ) -> str:
        prefix = "pr" if len(source_prs) == 1 else "prs"
        numbers = "-".join(str(source_pr) for source_pr in source_prs)
        initiative = f"{prefix}-{numbers}-to-{target_branch}"
        if excluded_paths:
            digest = hashlib.sha256(
                "\0".join(excluded_paths).encode("utf-8")
            ).hexdigest()[:16]
            initiative = f"{initiative}-except-{digest}"
        if promotion_mode is PromotionMode.OUT_OF_ORDER:
            return f"{initiative}-out-of-order"
        return initiative

    @staticmethod
    def _promotion_branch(initiative: str) -> str:
        return f"awf/{initiative}/promote"

    def _stale_precommit_promotion_retry_identity(
        self,
        lease: Lease | None,
        *,
        repository_id: str,
        initiative: str,
        sources: Sequence[PullRequest],
        target_ref: str,
        expected_branch: str,
        live_target_sha: str,
    ) -> _PromotionRetryIdentity | None:
        if (
            lease is None
            or lease.state is not LeaseState.BLOCKED
            or lease.purpose is not Purpose.PROMOTE
            or lease.promotion_mode is not PromotionMode.OUT_OF_ORDER
            or lease.target_base_sha != lease.head_sha
            or not lease.managed
            or lease.target_pr is not None
            or lease.resolution_state is not ResolutionState.NONE
            or lease.conflicted_paths
            or lease.protected_index_entries
            or lease.repository_id != repository_id
            or lease.initiative != initiative
            or lease.base_ref != target_ref
            or lease.branch != expected_branch
            or _GIT_OBJECT_ID.fullmatch(lease.head_sha) is None
            or _GIT_OBJECT_ID.fullmatch(live_target_sha) is None
        ):
            return None
        try:
            if not self._promotion_source_pins_match(
                lease, sources, read_only=True
            ):
                return None
        except (ValueError, sqlite3.Error):
            return None
        try:
            events = self.registry.list_events_read_only(lease.id)
            worktree = self._registered_worktree(lease)
            if (
                not events
                or events[-1].event_type != "promotion_blocked"
                or not events[-1].summary.startswith("promotion_apply_failed:")
                or worktree is None
                or worktree.bare
                or worktree.branch != lease.branch
                or self.git.status_porcelain(lease.worktree_path)
                or self.git.head_sha(lease.worktree_path) != lease.head_sha
                or self.git.remote_branch_sha(lease.branch) is not None
            ):
                return None
        except (GitError, OSError, RuntimeError, sqlite3.Error):
            return None
        retry_initiative = f"{initiative}-retry-{live_target_sha}"
        worktree_digest = hashlib.sha256(
            retry_initiative.encode("utf-8")
        ).hexdigest()[:16]
        return _PromotionRetryIdentity(
            initiative=retry_initiative,
            branch=self._promotion_branch(retry_initiative),
            worktree_path=(
                self.cache_dir
                / lease.repository_name
                / f"promotion-retry-{live_target_sha}-{worktree_digest}"
            ),
        )


    def _out_of_order_resolution_preview(
        self,
        lease: Lease | None,
        *,
        expected: Lease,
        github: GhClient,
        sources: Sequence[PullRequest],
        target_branch: str,
    ) -> CommandResult | None:
        if (
            lease is None
            or lease.promotion_mode is not PromotionMode.OUT_OF_ORDER
            or lease.state is not LeaseState.BLOCKED
            or lease.resolution_state is not ResolutionState.PENDING
        ):
            return None
        if (
            not lease.managed
            or not lease.conflicted_paths
            or lease.target_pr is not None
            or lease.repository_id != expected.repository_id
            or lease.initiative != expected.initiative
            or lease.purpose is not Purpose.PROMOTE
            or lease.base_ref != expected.base_ref
            or lease.branch != expected.branch
            or lease.target_base_sha is None
            or lease.reviewed_paths != expected.reviewed_paths
            or not set(lease.conflicted_paths).issubset(lease.reviewed_paths)
        ):
            return self._promotion_blocked(
                "promotion_incomplete",
                f"lease {lease.id} does not match pending conflict provenance",
                lease=lease,
            )
        if lease.head_sha != lease.target_base_sha:
            return self._promotion_blocked(
                "promotion_provenance_changed",
                f"lease {lease.id} pending conflict provenance changed",
                lease=lease,
            )
        try:
            source_pins_match = self._promotion_source_pins_match(
                lease, sources, read_only=True
            )
        except (ValueError, sqlite3.Error) as error:
            return self._promotion_blocked(
                "registry_conflict", str(error), lease=lease
            )
        if not source_pins_match:
            return self._promotion_blocked(
                "promotion_provenance_changed",
                f"lease {lease.id} source pull request provenance changed",
                lease=lease,
            )
        try:
            worktree = self._registered_worktree(lease)
            if (
                worktree is None
                or worktree.branch != lease.branch
                or worktree.detached
                or worktree.bare
                or self.git.head_sha(lease.worktree_path) != lease.head_sha
            ):
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} does not match its managed conflict worktree",
                    lease=lease,
                )
            live_target_sha = self.git.remote_branch_sha(target_branch)
            if live_target_sha is None:
                return self._promotion_blocked(
                    "target_ref_unavailable",
                    f"target branch {target_branch!r} is unavailable on origin",
                    lease=lease,
                )
            if live_target_sha != lease.target_base_sha:
                return self._promotion_blocked(
                    "promotion_provenance_changed",
                    f"lease {lease.id} target branch changed after the conflict",
                    lease=lease,
                )
            if self.git.remote_branch_sha(lease.branch) is not None:
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} promotion branch is already published",
                    lease=lease,
                )
            if github.find_open_pr(head=lease.branch, base=target_branch) is not None:
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} already has a target pull request",
                    lease=lease,
                )
            changed_paths = self.git.worktree_changed_paths(lease.worktree_path)
            unmerged_paths = self.git.unmerged_paths(lease.worktree_path)
            unstaged_paths = self.git.unstaged_paths(lease.worktree_path)
            protected_index_entries_match = self._protected_index_entries_match(lease)
        except GitRemoteError as error:
            return self._external_error(
                "wt.promote",
                "promotion_recovery_failed",
                str(error),
                lease=lease,
            )
        except ExternalServiceError as error:
            return self._external_error(
                "wt.promote",
                "promotion_recovery_failed",
                str(error),
                lease=lease,
            )
        except GitError as error:
            return self._promotion_blocked(
                "promotion_incomplete", str(error), lease=lease
            )
        if not (
            set(unstaged_paths).issubset(lease.conflicted_paths)
            and set(unmerged_paths).issubset(lease.conflicted_paths)
        ):
            return self._promotion_blocked(
                "promotion_resolution_scope_mismatch",
                f"lease {lease.id} has pending changes outside conflicted paths",
                lease=lease,
            )
        if not protected_index_entries_match:
            return self._promotion_blocked(
                "promotion_resolution_scope_mismatch",
                f"lease {lease.id} protected reviewed paths changed",
                lease=lease,
            )
        return CommandResult.ok(
            "wt.promote",
            decision="preview",
            lease=lease,
            actions=(
                {
                    "kind": "resolve_out_of_order_conflict",
                    "lease_id": lease.id,
                    "path": str(lease.worktree_path),
                    "conflicted_paths": list(lease.conflicted_paths),
                    "conflict_source_ordinal": (
                        lease.conflict_source_ordinal
                        if lease.conflict_source_ordinal is not None
                        else 0
                    ),
                    "remaining_source_prs": [
                        source.number
                        for source in sources[
                            (lease.conflict_source_ordinal or 0) + 1 :
                        ]
                    ],
                    "reviewed_paths": list(lease.reviewed_paths),
                    "current_changed_paths": list(changed_paths),
                },
                {
                    "kind": "stage_paths",
                    "paths": list(lease.conflicted_paths),
                },
                {
                    "kind": "commit",
                    "resolution_state": "manual-reviewed",
                },
                *(
                    {
                        "kind": "verify_production",
                        "argv": list(command),
                    }
                    for command in self.config.verify_production
                ),
                {
                    "kind": "push_branch",
                    "branch": lease.branch,
                },
                {
                    "kind": "open_pull_request",
                    "head": lease.branch,
                    "base": target_branch,
                },
            ),
        )

    def _recover_promotion_preflight(
        self, lease: Lease, *, apply: bool
    ) -> _PromotionRecoveryPreflight | CommandResult:
        if (
            lease.purpose is not Purpose.PROMOTE
            or not lease.managed
            or lease.owner_kind != "awf"
            or lease.promotion_mode is not PromotionMode.OUT_OF_ORDER
            or lease.state is not LeaseState.BLOCKED
            or lease.resolution_state is not ResolutionState.MANUAL_REVIEWED
            or lease.target_pr is not None
        ):
            return self._recover_promotion_blocked(
                "promotion_incomplete",
                f"lease {lease.id} is not a blocked manually reviewed out-of-order promotion",
                lease=lease,
            )
        if any(
            value is None or _GIT_OBJECT_ID.fullmatch(value) is None
            for value in (lease.target_base_sha, lease.head_sha)
        ):
            return self._recover_promotion_blocked(
                "promotion_incomplete",
                f"lease {lease.id} has incomplete manual resolution provenance",
                lease=lease,
            )
        try:
            reviewed_paths = self._promotion_excluded_paths(lease.reviewed_paths)
            conflicted_paths = self._promotion_excluded_paths(lease.conflicted_paths)
        except ValueError:
            return self._recover_promotion_blocked(
                "promotion_incomplete",
                f"lease {lease.id} has invalid reviewed-path provenance",
                lease=lease,
            )
        if (
            not reviewed_paths
            or not conflicted_paths
            or reviewed_paths != lease.reviewed_paths
            or conflicted_paths != lease.conflicted_paths
            or not set(conflicted_paths).issubset(reviewed_paths)
        ):
            return self._recover_promotion_blocked(
                "promotion_incomplete",
                f"lease {lease.id} has incomplete conflict-path provenance",
                lease=lease,
            )
        try:
            target_ref = self._remote_base_ref(lease.base_ref)
            target_branch = target_ref[len("origin/") :]
            if (
                target_ref != lease.base_ref
                or self._promotion_target_ref(target_branch) != target_ref
                or not self._is_safe_remote_branch(lease.branch)
                or lease.branch != self._promotion_branch(lease.initiative)
                or lease.repository_id != self.git.repository_id()
                or lease.repository_root != self.git.repository_root()
            ):
                return self._recover_promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} does not match its managed promotion provenance",
                    lease=lease,
                )
            legacy_message_candidate = (
                lease.legacy_source_trailers
                or not (
                    self.registry.get_promotion_sources(lease.id)
                    if apply
                    else self.registry.get_promotion_sources_read_only(lease.id)
                )
            )
            source_blocker = self._recover_promotion_source_blocker(
                lease, target_ref, verify_objects=apply
            )
            if source_blocker is not None:
                return source_blocker
            worktree = self._registered_worktree(lease)
            promotion_head = self.git.head_sha(lease.worktree_path)
            promotion_message = self.git.commit_message(lease.worktree_path)
            legacy_manual_message = (
                legacy_message_candidate
                and self._legacy_manual_reviewed_message_matches(
                    lease, target_branch, promotion_message
                )
            )
            if (
                worktree is None
                or worktree.branch != lease.branch
                or worktree.detached
                or worktree.bare
                or self.git.commit_parents(promotion_head)
                != (lease.target_base_sha,)
                or not self._manual_reviewed_promotion_message_matches(
                    lease,
                    target_branch,
                    promotion_message,
                    read_only=not apply,
                    allow_legacy_unpinned=legacy_message_candidate,
                )
                or self.git.committed_diff_has_conflict_markers(
                    lease.worktree_path,
                    lease.target_base_sha,
                    promotion_head,
                )
            ):
                return self._recover_promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} does not match its single manual resolution commit",
                    lease=lease,
                )
            promoted_paths = self.git.changed_paths(
                lease.worktree_path,
                lease.target_base_sha,
                promotion_head,
                find_renames=True,
            )
            if not promoted_paths or not set(promoted_paths).issubset(
                lease.reviewed_paths
            ):
                return self._recover_promotion_blocked(
                    "promotion_resolution_scope_mismatch",
                    f"lease {lease.id} committed delta is not a non-empty reviewed subset",
                    lease=lease,
                )
            if not self._protected_index_entries_match(lease):
                return self._recover_promotion_blocked(
                    "promotion_resolution_scope_mismatch",
                    f"lease {lease.id} protected reviewed paths changed",
                    lease=lease,
                )
            publication_blocker = self._recover_promotion_publication_blocker(
                lease, target_branch
            )
            if publication_blocker is not None:
                return publication_blocker
            unmerged_paths = self.git.unmerged_paths(lease.worktree_path)
            if unmerged_paths:
                return self._recover_promotion_blocked(
                    "promotion_resolution_unmerged",
                    f"lease {lease.id} has unmerged paths",
                    lease=lease,
                )
            if promotion_head != lease.head_sha:
                if self.git.status_porcelain(lease.worktree_path):
                    return self._recover_promotion_blocked(
                        "promotion_incomplete",
                        f"lease {lease.id} amended resolution is not clean",
                        lease=lease,
                    )
                return _PromotionRecoveryPreflight(
                    target_branch=target_branch,
                    dirty_paths=(),
                    reconciliation_head=promotion_head,
                    legacy_manual_message=legacy_manual_message,
                )
            dirty_paths = self.git.worktree_changed_paths(lease.worktree_path)
            if not dirty_paths:
                return self._recover_promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} has no post-commit resolution changes to amend",
                    lease=lease,
                )
            if not set(dirty_paths).issubset(conflicted_paths):
                return self._recover_promotion_blocked(
                    "promotion_resolution_scope_mismatch",
                    f"lease {lease.id} has dirty changes outside conflicted paths",
                    lease=lease,
                )
            if (
                self.git.staged_diff_has_conflict_markers(lease.worktree_path)
                or self.git.worktree_diff_has_conflict_markers(lease.worktree_path)
            ):
                return self._recover_promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} resolution has conflict markers",
                    lease=lease,
                )
        except GitRemoteError as error:
            return self._external_error(
                "wt.recover-promotion",
                "promotion_recovery_failed",
                str(error),
                lease=lease,
            )
        except ExternalServiceError as error:
            return self._external_error(
                "wt.recover-promotion",
                "promotion_recovery_failed",
                str(error),
                lease=lease,
            )
        except (
            AttributeError,
            ConfigError,
            GitError,
            OSError,
            ValueError,
            sqlite3.Error,
        ) as error:
            return self._recover_promotion_blocked(
                "promotion_incomplete", str(error), lease=lease
            )
        return _PromotionRecoveryPreflight(
            target_branch=target_branch,
            dirty_paths=dirty_paths,
            legacy_manual_message=legacy_manual_message,
        )

    def _recover_promotion_source_blocker(
        self, lease: Lease, target_ref: str, *, verify_objects: bool
    ) -> CommandResult | None:
        try:
            pins = (
                self.registry.get_promotion_sources(lease.id)
                if verify_objects
                else self.registry.get_promotion_sources_read_only(lease.id)
            )
        except sqlite3.Error as error:
            return self._recover_promotion_blocked(
                "registry_conflict", str(error), lease=lease
            )
        source_numbers = (
            tuple(pin.source_pr for pin in pins)
            if pins
            else ((lease.source_pr,) if lease.source_pr is not None else ())
        )
        if not source_numbers:
            return self._recover_promotion_blocked(
                "promotion_incomplete",
                f"lease {lease.id} has no immutable source pins",
                lease=lease,
            )
        try:
            github = self.github or GhClient(self.git.repository_root())
            sources = tuple(github.view_pr(number) for number in source_numbers)
        except ExternalServiceError as error:
            return self._external_error(
                "wt.recover-promotion",
                "source_pr_unavailable",
                str(error),
                lease=lease,
            )
        for source in sources:
            source_blocker = self._promotion_source_blocker(source, target_ref)
            if source_blocker is not None:
                return self._recover_promotion_blocked(
                    source_blocker.blockers[0]["code"],
                    source_blocker.blockers[0]["message"],
                    lease=lease,
                )
            invalid_oid = self._invalid_promotion_oid(source, require_merge=True)
            if invalid_oid is not None:
                return self._recover_promotion_blocked(
                    "source_pr_invalid_oid",
                    f"source pull request #{source.number} has an invalid {invalid_oid}",
                    lease=lease,
                )
        try:
            source_pins_match = (
                pins == self._promotion_source_pins(lease, sources)
                if pins
                else self._legacy_promotion_source_pins_match(lease, sources)
            )
            if not source_pins_match:
                return self._recover_promotion_blocked(
                    "promotion_provenance_changed",
                    f"lease {lease.id} source pull request provenance changed",
                    lease=lease,
                )
        except ValueError:
            return self._recover_promotion_blocked(
                "promotion_provenance_changed",
                f"lease {lease.id} source pull request provenance changed",
                lease=lease,
            )
        if not verify_objects:
            return None
        source_blocker = self._out_of_order_reuse_source_blocker(lease, sources)
        if source_blocker is None:
            return None
        code = source_blocker.blockers[0]["code"]
        message = source_blocker.blockers[0]["message"]
        if source_blocker.status == "error":
            return self._external_error(
                "wt.recover-promotion",
                code,
                message,
                lease=lease,
            )
        return self._recover_promotion_blocked(
            "promotion_provenance_changed",
            message,
            lease=lease,
        )

    def _recover_promotion_publication_blocker(
        self, lease: Lease, target_branch: str
    ) -> CommandResult | None:
        try:
            live_target_sha = self.git.remote_branch_sha(target_branch)
            if live_target_sha is None:
                return self._recover_promotion_blocked(
                    "target_ref_unavailable",
                    f"target branch {target_branch!r} is unavailable on origin",
                    lease=lease,
                )
            if live_target_sha != lease.target_base_sha:
                return self._recover_promotion_blocked(
                    "promotion_provenance_changed",
                    f"lease {lease.id} target branch changed after manual review",
                    lease=lease,
                )
            if self.git.remote_branch_sha(lease.branch) is not None:
                return self._recover_promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} promotion branch is already published",
                    lease=lease,
                )
            github = self.github or GhClient(self.git.repository_root())
            if github.find_open_pr(head=lease.branch, base=target_branch) is not None:
                return self._recover_promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} already has a target pull request",
                    lease=lease,
                )
        except GitRemoteError as error:
            return self._external_error(
                "wt.recover-promotion",
                "promotion_recovery_failed",
                str(error),
                lease=lease,
            )
        except ExternalServiceError as error:
            return self._external_error(
                "wt.recover-promotion",
                "promotion_recovery_failed",
                str(error),
                lease=lease,
            )
        return None

    def _manual_reviewed_promotion_message(
        self, lease: Lease, target_branch: str, *, read_only: bool = False
    ) -> str:
        sources = (
            self.registry.get_promotion_sources_read_only(lease.id)
            if read_only
            else self.registry.get_promotion_sources(lease.id)
        )
        if not sources and lease.source_pr is not None:
            github = self.github or GhClient(self.git.repository_root())
            legacy_source = github.view_pr(lease.source_pr)
            if self._legacy_promotion_source_pins_match(lease, (legacy_source,)):
                sources = (legacy_source,)
        return self._promotion_message(
            sources=sources,
            excluded_paths=(),
            target_sha=lease.target_base_sha or "",
            lease=lease,
            target_branch=target_branch,
            resolution_state=ResolutionState.MANUAL_REVIEWED,
        )

    def _manual_reviewed_promotion_message_matches(
        self,
        lease: Lease,
        target_branch: str,
        message: str,
        *,
        read_only: bool,
        allow_legacy_unpinned: bool,
    ) -> bool:
        if message == self._manual_reviewed_promotion_message(
            lease, target_branch, read_only=read_only
        ):
            return True
        return allow_legacy_unpinned and self._legacy_manual_reviewed_message_matches(
            lease, target_branch, message
        )

    def _legacy_manual_reviewed_message_matches(
        self, lease: Lease, target_branch: str, message: str
    ) -> bool:
        if lease.source_pr is None:
            return False
        github = self.github or GhClient(self.git.repository_root())
        source = github.view_pr(lease.source_pr)
        if (
            not self._legacy_promotion_source_pins_match(lease, (source,))
            or source.merge_commit_sha is None
            or _GIT_OBJECT_ID.fullmatch(source.merge_commit_sha) is None
        ):
            return False
        return message == self._promotion_message(
            sources=(source,),
            excluded_paths=(),
            target_sha=lease.target_base_sha or "",
            lease=lease,
            target_branch=target_branch,
            resolution_state=ResolutionState.MANUAL_REVIEWED,
            include_source_merge=False,
        )

    @staticmethod
    def _recover_promotion_blocked(
        code: str, message: str, *, lease: Lease | None = None
    ) -> CommandResult:
        return CommandResult.blocked(
            "wt.recover-promotion",
            blockers=({"code": code, "message": message},),
            lease=lease,
        )

    def _resume_out_of_order_conflict(
        self,
        lease: Lease,
        *,
        github: GhClient,
        sources: Sequence[PullRequest],
        target_branch: str,
        allow_legacy_unpinned: bool = False,
    ) -> CommandResult:
        conflict_source_ordinal = (
            lease.conflict_source_ordinal
            if lease.conflict_source_ordinal is not None
            else 0
        )
        if (
            lease.state is not LeaseState.BLOCKED
            or lease.resolution_state is not ResolutionState.PENDING
            or not lease.managed
            or not lease.conflicted_paths
            or lease.target_pr is not None
            or lease.target_base_sha is None
            or conflict_source_ordinal >= len(sources)
            or not set(lease.conflicted_paths).issubset(lease.reviewed_paths)
        ):
            return self._promotion_blocked(
                "promotion_incomplete",
                f"lease {lease.id} is not a pending out-of-order conflict",
                lease=lease,
            )
        try:
            worktree = self._registered_worktree(lease)
            if (
                worktree is None
                or worktree.branch != lease.branch
                or worktree.detached
                or worktree.bare
                or lease.head_sha != lease.target_base_sha
            ):
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} does not match its managed conflict worktree",
                    lease=lease,
                )
            if self.git.head_sha(lease.worktree_path) != lease.head_sha:
                return self._reconcile_pending_manual_resolution(
                    lease,
                    github=github,
                    sources=sources,
                    target_branch=target_branch,
                    allow_legacy_unpinned=allow_legacy_unpinned,
                )
            if self.git.fetch_ref(target_branch) != lease.target_base_sha:
                return self._promotion_blocked(
                    "promotion_provenance_changed",
                    f"lease {lease.id} target branch changed after the conflict",
                    lease=lease,
                )
            if self.git.remote_branch_sha(lease.branch) is not None:
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} promotion branch is already published",
                    lease=lease,
                )
            if github.find_open_pr(head=lease.branch, base=target_branch) is not None:
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} already has a target pull request",
                    lease=lease,
                )
            unmerged_paths = self.git.unmerged_paths(lease.worktree_path)
            unstaged_paths = self.git.unstaged_paths(lease.worktree_path)
            if not (
                set(unstaged_paths).issubset(lease.conflicted_paths)
                and set(unmerged_paths).issubset(lease.conflicted_paths)
            ):
                return self._promotion_blocked(
                    "promotion_resolution_scope_mismatch",
                    f"lease {lease.id} has pending changes outside conflicted paths",
                    lease=lease,
                )
            if not self._protected_index_entries_match(lease):
                return self._promotion_blocked(
                    "promotion_resolution_scope_mismatch",
                    f"lease {lease.id} protected reviewed paths changed",
                    lease=lease,
                )
            self.git.stage_paths(lease.worktree_path, lease.conflicted_paths)
            if self.git.unmerged_paths(lease.worktree_path):
                return self._promotion_blocked(
                    "promotion_resolution_unmerged",
                    f"lease {lease.id} still has unmerged paths after staging",
                    lease=lease,
                )
            if self.git.unstaged_paths(lease.worktree_path):
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} has unstaged conflicted resolution changes",
                    lease=lease,
                )
            if self.git.staged_diff_has_conflict_markers(lease.worktree_path):
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} staged resolution has conflict markers",
                    lease=lease,
                )
        except GitRemoteError as error:
            return self._external_error(
                "wt.promote", "promotion_recovery_failed", str(error), lease=lease
            )
        except ExternalServiceError as error:
            return self._external_error(
                "wt.promote", "promotion_recovery_failed", str(error), lease=lease
            )
        except (GitError, OSError, RuntimeError, sqlite3.Error) as error:
            return self._promotion_blocked(
                "promotion_incomplete", str(error), lease=lease
            )
        return self._apply_remaining_out_of_order_sources(
            lease,
            github=github,
            sources=sources,
            target_branch=target_branch,
            conflict_source_ordinal=conflict_source_ordinal,
        )

    def _apply_remaining_out_of_order_sources(
        self,
        lease: Lease,
        *,
        github: GhClient,
        sources: Sequence[PullRequest],
        target_branch: str,
        conflict_source_ordinal: int,
    ) -> CommandResult:
        assert lease.target_base_sha is not None
        try:
            for source_ordinal in range(conflict_source_ordinal + 1, len(sources)):
                source = sources[source_ordinal]
                if not any(
                    self.git.path_blob(lease.target_base_sha, path)
                    != self.git.path_blob(source.head_sha, path)
                    for path in source.changed_paths
                ):
                    continue
                patch = self.git.binary_diff(source.base_sha, source.head_sha)
                if not patch:
                    return self._promotion_blocked(
                        "promotion_incomplete",
                        f"source pull request #{source.number} has an empty pending patch",
                        lease=lease,
                    )
                try:
                    self.git.apply_indexed_patch(lease.worktree_path, patch)
                except GitPatchConflict as error:
                    return self._record_out_of_order_conflict(
                        lease, error, source_ordinal=source_ordinal
                    )
            promoted_paths = self.git.indexed_changed_paths(
                lease.worktree_path, lease.target_base_sha
            )
            if not promoted_paths or not set(promoted_paths).issubset(
                lease.reviewed_paths
            ):
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} resolved delta is not a non-empty reviewed subset",
                    lease=lease,
                )
            protected_paths = tuple(
                path
                for path in lease.reviewed_paths
                if path not in lease.conflicted_paths
            )
            protected_index_entries = self.git.index_entry_snapshot(
                lease.worktree_path, protected_paths
            )
            promotion_head = self.git.commit(
                lease.worktree_path,
                self._promotion_message(
                    sources=sources,
                    excluded_paths=(),
                    target_sha=lease.target_base_sha,
                    lease=lease,
                    target_branch=target_branch,
                    resolution_state=ResolutionState.MANUAL_REVIEWED,
                ),
            )
            if self.git.committed_diff_has_conflict_markers(
                lease.worktree_path, lease.target_base_sha, promotion_head
            ):
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} committed resolution has conflict markers",
                    lease=lease,
                )
            lease = self.registry.transition(
                lease.id,
                LeaseState.BLOCKED,
                expected_version=lease.version,
                event_type="promotion_manual_resolution_committed",
                summary="manually reviewed ordered conflict resolution committed",
                observed_head_sha=promotion_head,
                head_sha=promotion_head,
                resolution_state=ResolutionState.MANUAL_REVIEWED,
                clear_conflict_source_ordinal=True,
                legacy_source_trailers=False,
                protected_index_entries=protected_index_entries,
            )
        except GitRemoteError as error:
            return self._external_error(
                "wt.promote", "promotion_recovery_failed", str(error), lease=lease
            )
        except (
            GitError,
            OSError,
            RuntimeError,
            sqlite3.Error,
            subprocess.SubprocessError,
        ) as error:
            return self._promotion_blocked(
                "promotion_incomplete", str(error), lease=lease
            )
        return self._resume_manually_reviewed_promotion(
            lease,
            github=github,
            sources=sources,
            target_branch=target_branch,
        )


    def _reconcile_pending_manual_resolution(
        self,
        lease: Lease,
        *,
        github: GhClient,
        sources: Sequence[PullRequest],
        target_branch: str,
        allow_legacy_unpinned: bool = False,
    ) -> CommandResult:
        if (
            lease.state is not LeaseState.BLOCKED
            or lease.resolution_state is not ResolutionState.PENDING
            or lease.target_base_sha is None
        ):
            return self._promotion_blocked(
                "promotion_incomplete",
                f"lease {lease.id} is not a pending manual reconciliation",
                lease=lease,
            )
        try:
            worktree = self._registered_worktree(lease)
            promotion_head = self.git.head_sha(lease.worktree_path)
            if (
                worktree is None
                or worktree.branch != lease.branch
                or worktree.detached
                or worktree.bare
                or self.git.status_porcelain(lease.worktree_path)
                or self.git.commit_parents(promotion_head)
                != (lease.target_base_sha,)
            ):
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} does not match a committed manual resolution",
                    lease=lease,
                )
            if self.git.fetch_ref(target_branch) != lease.target_base_sha:
                return self._promotion_blocked(
                    "promotion_provenance_changed",
                    f"lease {lease.id} target branch changed after the conflict",
                    lease=lease,
                )
            if self.git.remote_branch_sha(lease.branch) is not None:
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} promotion branch is already published",
                    lease=lease,
                )
            if github.find_open_pr(head=lease.branch, base=target_branch) is not None:
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} already has a target pull request",
                    lease=lease,
                )
            provisional = replace(
                lease,
                head_sha=promotion_head,
                resolution_state=ResolutionState.MANUAL_REVIEWED,
            )
            legacy_manual_message = (
                allow_legacy_unpinned
                and self._legacy_manual_reviewed_message_matches(
                    provisional,
                    target_branch,
                    self.git.commit_message(lease.worktree_path),
                )
            )
            message_matches = self._manual_reviewed_promotion_message_matches(
                provisional,
                target_branch,
                self.git.commit_message(lease.worktree_path),
                read_only=False,
                allow_legacy_unpinned=allow_legacy_unpinned,
            )
            promoted_paths = self.git.changed_paths(
                lease.worktree_path,
                lease.target_base_sha,
                promotion_head,
                find_renames=True,
            )
            if (
                not message_matches
                or not promoted_paths
                or not set(promoted_paths).issubset(lease.reviewed_paths)
                or self.git.committed_diff_has_conflict_markers(
                    lease.worktree_path, lease.target_base_sha, promotion_head
                )
            ):
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} does not have a valid manual resolution commit",
                    lease=lease,
                )
            protected_paths = tuple(
                path
                for path in lease.reviewed_paths
                if path not in lease.conflicted_paths
            )
            protected_index_entries = self.git.index_entry_snapshot(
                lease.worktree_path, protected_paths
            )
            lease = self.registry.transition(
                lease.id,
                LeaseState.BLOCKED,
                expected_version=lease.version,
                event_type="promotion_manual_resolution_reconciled",
                summary="manually reviewed conflict resolution reconciled",
                observed_head_sha=promotion_head,
                head_sha=promotion_head,
                resolution_state=ResolutionState.MANUAL_REVIEWED,
                clear_conflict_source_ordinal=True,
                legacy_source_trailers=legacy_manual_message,
                protected_index_entries=protected_index_entries,
            )
        except GitRemoteError as error:
            return self._external_error(
                "wt.promote",
                "promotion_recovery_failed",
                str(error),
                lease=lease,
            )
        except ExternalServiceError as error:
            return self._external_error(
                "wt.promote",
                "promotion_recovery_failed",
                str(error),
                lease=lease,
            )
        except (GitError, OSError, RuntimeError, sqlite3.Error) as error:
            return self._promotion_blocked(
                "promotion_incomplete", str(error), lease=lease
            )
        return self._resume_manually_reviewed_promotion(
            lease,
            allow_legacy_unpinned=allow_legacy_unpinned,
            github=github,
            sources=sources,
            target_branch=target_branch,
        )


    def _resume_manually_reviewed_promotion(
        self,
        lease: Lease,
        *,
        github: GhClient,
        sources: Sequence[PullRequest],
        target_branch: str,
        allow_legacy_unpinned: bool = False,
    ) -> CommandResult:
        if (
            lease.state is not LeaseState.BLOCKED
            or lease.resolution_state is not ResolutionState.MANUAL_REVIEWED
            or not lease.managed
            or lease.target_pr is not None
            or lease.target_base_sha is None
        ):
            return self._promotion_blocked(
                "promotion_incomplete",
                f"lease {lease.id} is not a manually reviewed promotion",
                lease=lease,
            )
        try:
            worktree = self._registered_worktree(lease)
            promotion_head = self.git.head_sha(lease.worktree_path)
            if (
                worktree is None
                or worktree.branch != lease.branch
                or worktree.detached
                or worktree.bare
                or promotion_head != lease.head_sha
                or self.git.status_porcelain(lease.worktree_path)
                or self.git.commit_parents(promotion_head)
                != (lease.target_base_sha,)
            ):
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} does not match its committed manual resolution",
                    lease=lease,
                )
            if not self._protected_index_entries_match(lease):
                return self._promotion_blocked(
                    "promotion_resolution_scope_mismatch",
                    f"lease {lease.id} protected reviewed paths changed",
                    lease=lease,
                )
            if self.git.fetch_ref(target_branch) != lease.target_base_sha:
                return self._promotion_blocked(
                    "promotion_provenance_changed",
                    f"lease {lease.id} target branch changed after the conflict",
                    lease=lease,
                )
            if self.git.remote_branch_sha(lease.branch) is not None:
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} promotion branch is already published",
                    lease=lease,
                )
            if github.find_open_pr(head=lease.branch, base=target_branch) is not None:
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} already has a target pull request",
                    lease=lease,
                )
            promotion_message = self.git.commit_message(lease.worktree_path)
            legacy_manual_message = (
                allow_legacy_unpinned
                and self._legacy_manual_reviewed_message_matches(
                    lease, target_branch, promotion_message
                )
            )
            source_base_shas = self._promotion_source_bases_from_message(
                promotion_message,
                sources=sources,
                excluded_paths=(),
                target_sha=lease.target_base_sha,
                lease=lease,
                target_branch=target_branch,
                allow_legacy_unpinned=allow_legacy_unpinned,
            )
            promoted_paths = self.git.changed_paths(
                lease.worktree_path,
                lease.target_base_sha,
                promotion_head,
                find_renames=True,
            )
            if (
                source_base_shas
                != tuple(source.base_sha for source in sources)
                or not promoted_paths
                or not set(promoted_paths).issubset(lease.reviewed_paths)
                or self.git.committed_diff_has_conflict_markers(
                    lease.worktree_path, lease.target_base_sha, promotion_head
                )
            ):
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} does not have its reviewed manual resolution",
                    lease=lease,
                )
            prepare_blocker = self._prepare_promotion(lease, force=True)
            if prepare_blocker is not None:
                return prepare_blocker
            verification_actions = self._verify_promotion(lease.worktree_path)
            lease = self.registry.transition(
                lease.id,
                LeaseState.ACTIVE,
                expected_version=lease.version,
                event_type="promotion_publish_pending",
                summary="manually reviewed promotion verified; publication pending",
                observed_head_sha=promotion_head,
                head_sha=promotion_head,
                resolution_state=ResolutionState.MANUAL_REVIEWED,
                legacy_source_trailers=legacy_manual_message,
            )
        except GitRemoteError as error:
            return self._external_error(
                "wt.promote",
                "promotion_recovery_failed",
                str(error),
                lease=lease,
            )
        except ExternalServiceError as error:
            return self._external_error(
                "wt.promote",
                "promotion_recovery_failed",
                str(error),
                lease=lease,
            )
        except (
            GitError,
            OSError,
            RuntimeError,
            sqlite3.Error,
            subprocess.SubprocessError,
        ) as error:
            return self._promotion_blocked(
                "promotion_incomplete", str(error), lease=lease
            )
        resumed = self._resume_promotion_publish(
            lease,
            github=github,
            source_prs=tuple(source.number for source in sources),
            target_branch=target_branch,
        )
        return replace(resumed, actions=verification_actions)


    def _reuse_promotion(
        self,
        lease: Lease,
        *,
        sources: Sequence[PullRequest],
        excluded_paths: Sequence[str],
        promotion_mode: PromotionMode,
        target_ref: str,
        expected_branch: str,
        github: GhClient,
        target_branch: str,
    ) -> CommandResult:
        if (
            lease.base_ref != target_ref
            or lease.promotion_mode is not promotion_mode
            or lease.branch != expected_branch
        ):
            return self._promotion_blocked(
                "promotion_lease_conflict",
                f"lease {lease.id} does not match the requested promotion",
                lease=lease,
            )
        legacy_unpinned = False
        if promotion_mode is PromotionMode.OUT_OF_ORDER:
            try:
                legacy_unpinned = lease.legacy_source_trailers or not (
                    self.registry.get_promotion_sources(lease.id)
                )
            except sqlite3.Error as error:
                return self._promotion_blocked(
                    "registry_conflict", str(error), lease=lease
                )
            source_blocker = self._out_of_order_reuse_source_blocker(
                lease, sources
            )
            if source_blocker is not None:
                if (
                    source_blocker.status == "blocked"
                    and lease.state is LeaseState.BLOCKED
                    and lease.resolution_state
                    in (
                        ResolutionState.PENDING,
                        ResolutionState.MANUAL_REVIEWED,
                    )
                ):
                    return self._promotion_blocked(
                        "promotion_provenance_changed",
                        source_blocker.blockers[0]["message"],
                        lease=lease,
                    )
                return source_blocker
            if (
                lease.state is LeaseState.BLOCKED
                and lease.resolution_state is ResolutionState.PENDING
            ):
                return self._resume_out_of_order_conflict(
                    lease,
                    github=github,
                    sources=sources,
                    target_branch=target_branch,
                    allow_legacy_unpinned=legacy_unpinned,
                )
        try:
            promotion_message = self.git.commit_message(lease.worktree_path)
        except GitError as error:
            return self._promotion_blocked(
                "promotion_lease_conflict", str(error), lease=lease
            )
        message_lines = promotion_message.splitlines()
        excluded_path_prefix = "AWF-Excluded-Path: "
        recorded_excluded_paths = tuple(
            line[len(excluded_path_prefix) :]
            for line in message_lines
            if line.startswith(excluded_path_prefix)
        )
        if recorded_excluded_paths != tuple(excluded_paths):
            return self._promotion_blocked(
                "promotion_lease_conflict",
                f"lease {lease.id} exclusions do not match the requested promotion",
                lease=lease,
            )
        target_base_prefix = "AWF-Target-Base: "
        target_base_line = (
            message_lines[-4]
            if lease.promotion_mode is PromotionMode.OUT_OF_ORDER
            and len(message_lines) >= 4
            else (message_lines[-2] if len(message_lines) >= 2 else "")
        )
        target_base_sha = (
            target_base_line[len(target_base_prefix) :]
            if target_base_line.startswith(target_base_prefix)
            else ""
        )
        recorded_source_base_shas = self._promotion_source_bases_from_message(
            promotion_message,
            sources=sources,
            excluded_paths=excluded_paths,
            target_sha=target_base_sha,
            lease=lease,
            target_branch=target_branch,
            allow_legacy_unpinned=legacy_unpinned,
        )
        if (
            _GIT_OBJECT_ID.fullmatch(target_base_sha) is None
            or recorded_source_base_shas is None
        ):
            return self._promotion_blocked(
                "promotion_incomplete",
                f"lease {lease.id} does not have exact promotion provenance",
                lease=lease,
            )
        if (
            lease.promotion_mode is PromotionMode.OUT_OF_ORDER
            and lease.state is LeaseState.BLOCKED
            and lease.target_base_sha == target_base_sha
        ):
            try:
                retry_events = self.registry.list_events(lease.id)
                live_target_sha = self.git.fetch_ref(target_branch)
            except GitRemoteError as error:
                return self._external_error(
                    "wt.promote", "promotion_recovery_failed", str(error), lease=lease
                )
            except (GitError, sqlite3.Error) as error:
                return self._promotion_blocked(
                    "promotion_recovery_failed", str(error), lease=lease
                )
            latest_retry_event = retry_events[-1] if retry_events else None
            if (
                live_target_sha != target_base_sha
                and latest_retry_event is not None
                and latest_retry_event.event_type == "promotion_blocked"
                and latest_retry_event.summary.startswith(
                    (
                        "promotion_apply_failed: production verification failed",
                        "promotion_recovery_failed: production verification failed",
                        "promotion_verification_failed:",
                    )
                )
            ):
                return self._rebuild_content_mismatch_promotion(
                    lease,
                    github=github,
                    sources=sources,
                    excluded_paths=excluded_paths,
                    recorded_target_sha=target_base_sha,
                    recorded_source_base_shas=recorded_source_base_shas,
                    target_branch=target_branch,
                )
        if lease.promotion_mode is PromotionMode.OUT_OF_ORDER and (
            lease.target_base_sha != target_base_sha
            or recorded_source_base_shas
            != tuple(source.base_sha for source in sources)
        ):
            return self._promotion_blocked(
                "promotion_incomplete",
                f"lease {lease.id} does not have exact promotion provenance",
                lease=lease,
            )
        if (
            lease.state is LeaseState.BLOCKED
            and lease.resolution_state is ResolutionState.MANUAL_REVIEWED
        ):
            return self._resume_manually_reviewed_promotion(
                lease,
                github=github,
                sources=sources,
                target_branch=target_branch,
                allow_legacy_unpinned=legacy_unpinned,
            )
        if lease.state is LeaseState.PR_OPEN and lease.target_pr is not None:
            return CommandResult.ok("wt.promote", decision="reuse", lease=lease)
        try:
            events = self.registry.list_events(lease.id)
        except sqlite3.Error as error:
            return self._promotion_blocked(
                "promotion_reconciliation_failed", str(error), lease=lease
            )
        if lease.state is LeaseState.BLOCKED:
            latest = events[-1] if events else None
            if (
                latest is not None
                and latest.event_type == "promotion_blocked"
                and latest.summary.startswith(
                    ("promotion_content_mismatch:", "staging_missing_main_delta:")
                )
            ):
                return self._rebuild_content_mismatch_promotion(
                    lease,
                    github=github,
                    sources=sources,
                    excluded_paths=excluded_paths,
                    recorded_target_sha=target_base_sha,
                    recorded_source_base_shas=recorded_source_base_shas,
                    target_branch=target_branch,
                )
            retryable_prefixes = (
                "promotion_apply_failed: production verification failed",
                "promotion_prepare_failed:",
                "promotion_verification_failed:",
            )
            if not (
                events
                and events[-1].event_type == "promotion_blocked"
                and events[-1].summary.startswith(retryable_prefixes)
            ):
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} is blocked without an open target pull request",
                    lease=lease,
                )
            return self._recover_unrecorded_promotion_publish(
                lease,
                github=github,
                sources=sources,
                excluded_paths=excluded_paths,
                target_branch=target_branch,
            )
        if lease.state is not LeaseState.ACTIVE:
            return self._promotion_blocked(
                "promotion_incomplete",
                f"lease {lease.id} is {lease.state.value} without an open target pull request",
                lease=lease,
            )
        if events and events[-1].event_type == "promotion_publish_pending":
            return self._resume_promotion_publish(
                lease,
                github=github,
                source_prs=tuple(source.number for source in sources),
                target_branch=target_branch,
            )
        return self._recover_unrecorded_promotion_publish(
            lease,
            github=github,
            sources=sources,
            excluded_paths=excluded_paths,
            target_branch=target_branch,
        )

    def _out_of_order_reuse_source_blocker(
        self, lease: Lease, sources: Sequence[PullRequest]
    ) -> CommandResult | None:
        try:
            if not self._promotion_source_pins_match(lease, sources):
                return self._promotion_blocked(
                    "promotion_provenance_changed",
                    f"lease {lease.id} source pull request provenance changed",
                    lease=lease,
                )
            staging_sha = self.git.fetch_ref(sources[0].base_ref)
            previous_merge_sha: str | None = None
            reviewed_paths: set[str] = set()
            for source in sources:
                if source.merge_commit_sha is None:
                    return self._promotion_blocked(
                        "promotion_provenance_changed",
                        f"lease {lease.id} source pull request provenance changed",
                        lease=lease,
                    )
                source_base_sha = self.git.fetch_ref(source.base_sha)
                source_head_sha = self.git.fetch_ref(source.head_sha)
                source_merge_sha = self.git.fetch_ref(source.merge_commit_sha)
                if (
                    source_base_sha != source.base_sha
                    or source_head_sha != source.head_sha
                    or source_merge_sha != source.merge_commit_sha
                ):
                    return self._promotion_blocked(
                        "source_sha_mismatch",
                        (
                            f"fetched source pull request #{source.number} refs do not"
                            " match the reviewed SHAs"
                        ),
                        lease=lease,
                    )
                merge_base = self.git.merge_base(source_base_sha, source_head_sha)
                if merge_base != source_base_sha:
                    return self._promotion_blocked(
                        "source_base_not_ancestor",
                        (
                            f"source pull request #{source.number} base is not an"
                            " ancestor of its reviewed head"
                        ),
                        lease=lease,
                    )
                if (
                    self.git.merge_base(source_base_sha, source_merge_sha)
                    != source_base_sha
                    or self.git.merge_base(source_merge_sha, staging_sha)
                    != source_merge_sha
                ):
                    return self._promotion_blocked(
                        "source_merge_not_in_staging",
                        (
                            f"source pull request #{source.number} merge commit is"
                            " not in the configured staging history"
                        ),
                        lease=lease,
                    )
                if (
                    previous_merge_sha is not None
                    and self.git.merge_base(previous_merge_sha, source_merge_sha)
                    != previous_merge_sha
                ):
                    return self._promotion_blocked(
                        "source_pr_sequence_order",
                        (
                            f"source pull request #{source.number} was not merged"
                            " after the preceding source pull request"
                        ),
                        lease=lease,
                    )
                previous_merge_sha = source_merge_sha
                collapsed_paths = self.git.changed_paths(
                    self.git.repository_root(),
                    merge_base,
                    source_head_sha,
                    find_renames=True,
                )
                expanded_paths = tuple(
                    sorted(
                        self.git.changed_path_endpoints(
                            self.git.repository_root(),
                            merge_base,
                            source_head_sha,
                        )
                    )
                )
                source_paths = tuple(sorted(source.changed_paths))
                if expanded_paths != collapsed_paths:
                    return self._promotion_blocked(
                        "unsupported_out_of_order_rename",
                        "out-of-order promotion does not support renamed paths",
                        lease=lease,
                    )
                if (
                    collapsed_paths != source_paths
                    or any(
                        self.git.path_blob(source_merge_sha, path)
                        != self.git.path_blob(source_head_sha, path)
                        for path in source_paths
                    )
                ):
                    return self._promotion_blocked(
                        "promotion_provenance_changed",
                        f"lease {lease.id} source pull request provenance changed",
                        lease=lease,
                    )
                reviewed_paths.update(source_paths)
        except GitRemoteError as error:
            return self._external_error(
                "wt.promote",
                "source_delta_unavailable",
                str(error),
                lease=lease,
            )
        except (GitError, ValueError, sqlite3.Error) as error:
            return self._promotion_blocked(
                "source_delta_unavailable", str(error), lease=lease
            )
        if lease.reviewed_paths != tuple(sorted(reviewed_paths)):
            return self._promotion_blocked(
                "promotion_provenance_changed",
                f"lease {lease.id} reviewed-path provenance changed",
                lease=lease,
            )
        try:
            if not self.registry.get_promotion_sources(lease.id) and not (
                self._backfill_legacy_promotion_source_pins(lease, sources)
            ):
                return self._promotion_blocked(
                    "promotion_provenance_changed",
                    f"lease {lease.id} source pull request provenance changed",
                    lease=lease,
                )
        except (ValueError, RuntimeError, sqlite3.Error) as error:
            return self._promotion_blocked(
                "promotion_provenance_changed", str(error), lease=lease
            )
        return None

    def _rebuild_content_mismatch_promotion(
        self,
        lease: Lease,
        *,
        github: GhClient,
        sources: Sequence[PullRequest],
        excluded_paths: Sequence[str],
        recorded_target_sha: str,
        recorded_source_base_shas: Sequence[str],
        target_branch: str,
    ) -> CommandResult:
        try:
            old_head = self.git.head_sha(lease.worktree_path)
            if (
                self._registered_worktree(lease) is None
                or self.git.status_porcelain(lease.worktree_path)
                or old_head != lease.head_sha
                or self.git.commit_parents(old_head) != (recorded_target_sha,)
            ):
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} was not verified for content-mismatch recovery",
                    lease=lease,
                )
            if self.git.remote_branch_sha(lease.branch) is not None:
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} promotion branch is already published",
                    lease=lease,
                )

            current_target_sha = self.git.fetch_ref(target_branch)
            if current_target_sha == recorded_target_sha:
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} target branch has not advanced",
                    lease=lease,
                )
            if recorded_source_base_shas != tuple(
                source.base_sha for source in sources
            ):
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} source provenance changed",
                    lease=lease,
                )

            patches: list[bytes] = []
            for source in sources:
                source_base_sha = self.git.fetch_ref(source.base_sha)
                source_head_sha = self.git.fetch_ref(source.head_sha)
                if (
                    source_base_sha != source.base_sha
                    or source_head_sha != source.head_sha
                ):
                    return self._promotion_blocked(
                        "promotion_incomplete",
                        f"lease {lease.id} source provenance changed",
                        lease=lease,
                    )
                merge_base = self.git.merge_base(source_base_sha, source_head_sha)
                included_paths = tuple(
                    path for path in source.changed_paths if path not in excluded_paths
                )
                if not included_paths:
                    continue
                patch = self.git.binary_diff(
                    merge_base,
                    source_head_sha,
                    paths=included_paths if excluded_paths else None,
                )
                if not patch:
                    return self._promotion_blocked(
                        "promotion_incomplete",
                        f"lease {lease.id} reviewed source patch is empty",
                        lease=lease,
                    )
                patches.append(patch)
        except GitRemoteError as error:
            return self._external_error(
                "wt.promote",
                "promotion_recovery_failed",
                str(error),
                lease=lease,
            )
        except (GitError, OSError, RuntimeError, sqlite3.Error) as error:
            return self._promotion_blocked(
                "promotion_recovery_failed", str(error), lease=lease
            )

        try:
            self.git.reset_hard(lease.worktree_path, current_target_sha)
            for patch in patches:
                self.git.apply_indexed_patch(lease.worktree_path, patch)
            promotion_head = self.git.commit(
                lease.worktree_path,
                self._promotion_message(
                    sources=sources,
                    excluded_paths=excluded_paths,
                    target_sha=current_target_sha,
                    lease=lease,
                    target_branch=target_branch,
                ),
                allow_empty=True,
                no_verify=True,
            )
            lease = self.registry.transition(
                lease.id,
                LeaseState.BLOCKED,
                expected_version=lease.version,
                event_type="promotion_blocked",
                summary=(
                    "promotion_verification_failed: rebuilt content-mismatch "
                    "promotion on advanced target"
                ),
                observed_head_sha=old_head,
                head_sha=promotion_head,
                target_base_sha=current_target_sha,
            )
        except (GitError, OSError, RuntimeError, sqlite3.Error) as error:
            try:
                self.git.reset_hard(lease.worktree_path, old_head)
            except (GitError, OSError, RuntimeError, sqlite3.Error) as restore_error:
                actual_head: str | None = None
                actual_head_error: Exception | None = None
                try:
                    actual_head = self.git.head_sha(lease.worktree_path)
                except (GitError, OSError, RuntimeError, sqlite3.Error) as head_error:
                    actual_head_error = head_error
                summary = (
                    "promotion_recovery_restore_failed: rebuild failed: "
                    f"{error}; restore failed: {restore_error}"
                )
                reconciliation_error: Exception | None = None
                try:
                    lease = self.registry.transition(
                        lease.id,
                        LeaseState.BLOCKED,
                        expected_version=lease.version,
                        event_type="promotion_blocked",
                        summary=summary,
                        observed_head_sha=actual_head,
                        head_sha=actual_head,
                    )
                except (OSError, RuntimeError, sqlite3.Error) as transition_error:
                    reconciliation_error = transition_error
                message = f"rebuild failed: {error}; restore failed: {restore_error}"
                if actual_head_error is not None:
                    message += f"; actual head unavailable: {actual_head_error}"
                if reconciliation_error is not None:
                    message += (
                        f"; registry reconciliation failed: {reconciliation_error}"
                    )
                return self._promotion_blocked(
                    "promotion_recovery_restore_failed", message, lease=lease
                )
            return self._promotion_blocked(
                "promotion_recovery_failed", str(error), lease=lease
            )
        return self._recover_unrecorded_promotion_publish(
            lease,
            github=github,
            sources=sources,
            excluded_paths=excluded_paths,
            target_branch=target_branch,
        )
    def _recover_unrecorded_promotion_publish(
        self,
        lease: Lease,
        *,
        github: GhClient,
        sources: Sequence[PullRequest],
        excluded_paths: Sequence[str],
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
            if (
                lease.resolution_state is ResolutionState.MANUAL_REVIEWED
                and not self._protected_index_entries_match(lease)
            ):
                return self._promotion_blocked(
                    "promotion_resolution_scope_mismatch",
                    f"lease {lease.id} protected reviewed paths changed",
                    lease=lease,
                )
            promotion_head = self.git.head_sha(lease.worktree_path)
            target_sha = (
                self.git.fetch_ref(target_branch)
                if lease.state is LeaseState.BLOCKED
                else lease.head_sha
            )
            if (
                promotion_head == target_sha
                or _GIT_OBJECT_ID.fullmatch(promotion_head) is None
                or _GIT_OBJECT_ID.fullmatch(target_sha) is None
                or any(
                    _GIT_OBJECT_ID.fullmatch(source.head_sha) is None
                    or _GIT_OBJECT_ID.fullmatch(source.base_sha) is None
                    for source in sources
                )
            ):
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} was not verified for publication",
                    lease=lease,
                )
            source_base_shas = self._promotion_source_bases_from_message(
                self.git.commit_message(lease.worktree_path),
                sources=sources,
                excluded_paths=excluded_paths,
                target_sha=target_sha,
                lease=lease,
                target_branch=target_branch,
            )
            if (
                source_base_shas is None
                or self.git.commit_parents(promotion_head) != (target_sha,)
            ):
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} does not have exact promotion provenance",
                    lease=lease,
                )
            if lease.promotion_mode is PromotionMode.EXACT:
                expected_blob_heads: dict[str, str] = {}
            for source, source_base_sha in zip(sources, source_base_shas):
                if (
                    (
                        lease.state is LeaseState.BLOCKED
                        and source_base_sha != source.base_sha
                    )
                    or self.git.fetch_ref(source_base_sha) != source_base_sha
                    or self.git.fetch_ref(source.head_sha) != source.head_sha
                ):
                    return self._promotion_blocked(
                        "promotion_incomplete",
                        f"lease {lease.id} does not have exact source provenance",
                        lease=lease,
                    )
                if lease.promotion_mode is PromotionMode.EXACT:
                    for path in source.changed_paths:
                        if path not in excluded_paths:
                            expected_blob_heads[path] = source.head_sha
            if lease.promotion_mode is PromotionMode.EXACT:
                expected_blobs = self._promotion_net_blobs(
                    expected_blob_heads, target_sha
                )
                expected_paths = tuple(sorted(expected_blobs))
                contents_valid = (
                    self.git.changed_paths(
                        lease.worktree_path,
                        target_sha,
                        promotion_head,
                        find_renames=True,
                    )
                    == expected_paths
                    and not any(
                        expected_blobs[path]
                        != self.git.path_blob(promotion_head, path)
                        for path in expected_paths
                    )
                )
            else:
                promoted_paths = self.git.changed_paths(
                    lease.worktree_path,
                    target_sha,
                    promotion_head,
                    find_renames=True,
                )
                contents_valid = bool(promoted_paths) and all(
                    path in lease.reviewed_paths for path in promoted_paths
                )
            if not contents_valid or (
                lease.resolution_state is ResolutionState.MANUAL_REVIEWED
                and self.git.committed_diff_has_conflict_markers(
                    lease.worktree_path, target_sha, promotion_head
                )
            ):
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} does not have the exact reviewed delta",
                    lease=lease,
                )
            prepare_blocker = self._prepare_promotion(lease, force=True)
            if prepare_blocker is not None:
                return prepare_blocker
            self._verify_promotion(lease.worktree_path)
            lease = self.registry.transition(
                lease.id,
                LeaseState.ACTIVE,
                expected_version=lease.version,
                event_type="promotion_publish_pending",
                summary="recovered verified promotion; publication pending",
                observed_head_sha=promotion_head,
                head_sha=promotion_head,
                resolution_state=(
                    ResolutionState.MANUAL_REVIEWED
                    if lease.resolution_state is ResolutionState.MANUAL_REVIEWED
                    else (
                        ResolutionState.AUTOMATIC
                        if lease.promotion_mode is PromotionMode.OUT_OF_ORDER
                        else None
                    )
                ),
            )
        except GitRemoteError as error:
            return self._external_error(
                "wt.promote",
                "promotion_recovery_failed",
                str(error),
                lease=lease,
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
            source_prs=tuple(source.number for source in sources),
            target_branch=target_branch,
        )

    def _resume_promotion_publish(
        self,
        lease: Lease,
        *,
        github: GhClient,
        source_prs: Sequence[int],
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
            if (
                lease.resolution_state is ResolutionState.MANUAL_REVIEWED
                and not self._protected_index_entries_match(lease)
            ):
                return self._block_promotion_lease(
                    lease,
                    "promotion_resolution_scope_mismatch",
                    f"lease {lease.id} protected reviewed paths changed",
                )
            prepare_blocker = self._prepare_promotion(lease, force=False)
            if prepare_blocker is not None:
                return prepare_blocker
            try:
                self._verify_promotion(lease.worktree_path)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                return self._block_promotion_lease(
                    lease,
                    "promotion_verification_failed",
                    str(error),
                )
            if lease.promotion_mode is PromotionMode.OUT_OF_ORDER:
                if lease.target_base_sha is None:
                    return self._block_promotion_lease(
                        lease,
                        "promotion_incomplete",
                        f"lease {lease.id} has no out-of-order target provenance",
                    )
                if (
                    lease.resolution_state is ResolutionState.MANUAL_REVIEWED
                    and self.git.committed_diff_has_conflict_markers(
                        lease.worktree_path, lease.target_base_sha, head_sha
                    )
                ):
                    return self._block_promotion_lease(
                        lease,
                        "promotion_incomplete",
                        f"lease {lease.id} committed promotion has conflict markers",
                    )
                live_target_sha = self.git.remote_branch_sha(target_branch)
                if live_target_sha is None:
                    return self._block_promotion_lease(
                        lease,
                        "target_ref_unavailable",
                        f"target branch {target_branch!r} is unavailable on origin",
                    )
                if live_target_sha != lease.target_base_sha:
                    return self._block_promotion_lease(
                        lease,
                        "promotion_provenance_changed",
                        f"lease {lease.id} target branch changed after verification",
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
                    title=self._promotion_title(source_prs, target_branch),
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
        except (ExternalServiceError, GitRemoteError) as error:
            return self._external_error(
                "wt.promote",
                "promotion_publish_failed",
                str(error),
                lease=lease,
            )
        except (
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
    def _promotion_title(
        source_prs: Sequence[int], target_branch: str
    ) -> str:
        label = "PR" if len(source_prs) == 1 else "PRs"
        numbers = ", ".join(f"#{source_pr}" for source_pr in source_prs)
        return f"Promote {label} {numbers} to {target_branch}"


    @staticmethod
    def _promotion_source_number(source: PullRequest | PromotionSource) -> int:
        return (
            source.number
            if isinstance(source, PullRequest)
            else source.source_pr
        )

    @staticmethod
    def _promotion_source_merge_sha(
        source: PullRequest | PromotionSource,
    ) -> str | None:
        return (
            source.merge_commit_sha
            if isinstance(source, PullRequest)
            else source.merge_sha
        )


    @staticmethod
    def _promotion_trailers(
        *,
        sources: Sequence[PullRequest | PromotionSource],
        excluded_paths: Sequence[str],
        target_sha: str,
        lease: Lease,
        resolution_state: ResolutionState | None = None,
        include_source_merge: bool = True,
    ) -> tuple[str, ...]:
        source_trailers = tuple(
            trailer
            for source in sources
            for trailer in (
                f"AWF-Source-PR: {WorktreeService._promotion_source_number(source)}",
                f"AWF-Source-Base: {source.base_sha}",
                f"AWF-Source-Head: {source.head_sha}",
                *(
                    (
                        "AWF-Source-Merge: "
                        f"{WorktreeService._promotion_source_merge_sha(source)}",
                    )
                    if include_source_merge
                    and lease.promotion_mode is PromotionMode.OUT_OF_ORDER
                    and WorktreeService._promotion_source_merge_sha(source)
                    is not None
                    else ()
                ),
            )
        )
        excluded_trailers = tuple(
            f"AWF-Excluded-Path: {path}" for path in excluded_paths
        )
        resolution = (
            ResolutionState.MANUAL_REVIEWED
            if resolution_state is ResolutionState.MANUAL_REVIEWED
            or (
                resolution_state is None
                and lease.resolution_state is ResolutionState.MANUAL_REVIEWED
            )
            else ResolutionState.AUTOMATIC
        )
        mode_trailers = (
            (
                "AWF-Promotion-Mode: out-of-order",
                (
                    "AWF-Resolution: manual-reviewed"
                    if resolution is ResolutionState.MANUAL_REVIEWED
                    else "AWF-Resolution: automatic"
                ),
            )
            if lease.promotion_mode is PromotionMode.OUT_OF_ORDER
            else ()
        )
        return (
            *source_trailers,
            *excluded_trailers,
            f"AWF-Target-Base: {target_sha}",
            f"AWF-Lease-ID: {lease.id}",
            *mode_trailers,
        )

    def _promotion_message(
        self,
        *,
        sources: Sequence[PullRequest | PromotionSource],
        excluded_paths: Sequence[str],
        target_sha: str,
        lease: Lease,
        target_branch: str,
        resolution_state: ResolutionState | None = None,
        include_source_merge: bool = True,
    ) -> str:
        return "\n".join(
            (
                self._promotion_title(
                    tuple(
                        self._promotion_source_number(source) for source in sources
                    ),
                    target_branch,
                ),
                "",
                *self._promotion_trailers(
                    include_source_merge=include_source_merge,
                    sources=sources,
                    excluded_paths=excluded_paths,
                    target_sha=target_sha,
                    lease=lease,
                    resolution_state=resolution_state,
                ),
            )
        )

    @staticmethod
    def _promotion_source_bases_from_message(
        message: str,
        *,
        sources: Sequence[PullRequest],
        excluded_paths: Sequence[str],
        target_sha: str,
        lease: Lease,
        target_branch: str,
        allow_legacy_unpinned: bool = False,
    ) -> tuple[str, ...] | None:
        lines = message.splitlines()
        out_of_order = lease.promotion_mode is PromotionMode.OUT_OF_ORDER
        mode_trailer_count = 2 if out_of_order else 0
        standard_source_trailer_count = len(sources) * (
            4 if out_of_order else 3
        )
        legacy_source_trailers = (
            allow_legacy_unpinned
            and out_of_order
            and len(sources) == 1
            and len(lines)
            == 3 + len(excluded_paths) + 4 + mode_trailer_count
        )
        source_trailer_count = (
            3 if legacy_source_trailers else standard_source_trailer_count
        )
        if (
            len(lines)
            != source_trailer_count + len(excluded_paths) + 4 + mode_trailer_count
            or lines[0]
            != WorktreeService._promotion_title(
                tuple(source.number for source in sources),
                target_branch,
            )
            or lines[1] != ""
        ):
            return None
        source_base_shas: list[str] = []
        offset = 2
        for source in sources:
            base_prefix = "AWF-Source-Base: "
            if (
                lines[offset] != f"AWF-Source-PR: {source.number}"
                or not lines[offset + 1].startswith(base_prefix)
                or lines[offset + 2] != f"AWF-Source-Head: {source.head_sha}"
            ):
                return None
            source_base_sha = lines[offset + 1][len(base_prefix) :]
            if (
                _GIT_OBJECT_ID.fullmatch(source_base_sha) is None
                or (out_of_order and source_base_sha != source.base_sha)
            ):
                return None
            source_base_shas.append(source_base_sha)
            offset += 3
            if out_of_order and not legacy_source_trailers:
                if (
                    source.merge_commit_sha is None
                    or lines[offset]
                    != f"AWF-Source-Merge: {source.merge_commit_sha}"
                ):
                    return None
                offset += 1
        for excluded_path in excluded_paths:
            if lines[offset] != f"AWF-Excluded-Path: {excluded_path}":
                return None
            offset += 1
        if (
            lines[offset] != f"AWF-Target-Base: {target_sha}"
            or lines[offset + 1] != f"AWF-Lease-ID: {lease.id}"
        ):
            return None
        if lease.promotion_mode is PromotionMode.OUT_OF_ORDER and (
            lines[offset + 2] != "AWF-Promotion-Mode: out-of-order"
            or lines[offset + 3]
            != (
                "AWF-Resolution: manual-reviewed"
                if lease.resolution_state is ResolutionState.MANUAL_REVIEWED
                else "AWF-Resolution: automatic"
            )
        ):
            return None
        return tuple(source_base_shas)

    def _promotion_body(
        self,
        *,
        sources: Sequence[PullRequest | PromotionSource],
        excluded_paths: Sequence[str],
        target_sha: str,
        lease: Lease,
    ) -> str:
        return "\n".join(
            self._promotion_trailers(
                sources=sources,
                excluded_paths=excluded_paths,
                target_sha=target_sha,
                lease=lease,
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

    def _prepare_promotion(
        self, lease: Lease, *, force: bool
    ) -> CommandResult | None:
        prepare_error = self._prepare(lease, force=force)
        if prepare_error is not None:
            return self._block_promotion_lease(
                lease,
                "promotion_prepare_failed",
                prepare_error,
            )
        if self.git.status_porcelain(lease.worktree_path):
            return self._block_promotion_lease(
                lease,
                "promotion_prepare_dirty",
                "promotion prepare command left uncommitted changes",
            )
        return None


    def _protected_index_entries_match(self, lease: Lease) -> bool:
        protected_paths = tuple(
            path
            for path in lease.reviewed_paths
            if path not in lease.conflicted_paths
        )
        if (
            tuple(path for path, _entry in lease.protected_index_entries)
            != protected_paths
        ):
            return False
        return self.git.index_entry_snapshot(
            lease.worktree_path, protected_paths
        ) == lease.protected_index_entries


    def _record_out_of_order_conflict(
        self,
        lease: Lease,
        error: GitPatchConflict,
        *,
        source_ordinal: int,
    ) -> CommandResult:
        conflicted_paths = tuple(sorted(error.paths))
        protected_paths = tuple(
            path for path in lease.reviewed_paths if path not in conflicted_paths
        )
        try:
            head_sha = self.git.head_sha(lease.worktree_path)
            protected_index_entries = self.git.index_entry_snapshot(
                lease.worktree_path, protected_paths
            )
            lease = self.registry.transition(
                lease.id,
                LeaseState.BLOCKED,
                expected_version=lease.version,
                event_type="promotion_blocked",
                summary=(
                    "out_of_order_conflict: reviewed patch requires managed "
                    f"resolution for source ordinal {source_ordinal}; conflicted paths: "
                    + ", ".join(repr(path) for path in conflicted_paths)
                ),
                observed_head_sha=head_sha,
                head_sha=head_sha,
                resolution_state=ResolutionState.PENDING,
                conflicted_paths=conflicted_paths,
                conflict_source_ordinal=source_ordinal,
                protected_index_entries=protected_index_entries,
            )
        except (GitError, OSError, RuntimeError, sqlite3.Error) as transition_error:
            return self._promotion_blocked(
                "registry_conflict", str(transition_error), lease=lease
            )
        return self._promotion_blocked(
            "out_of_order_conflict",
            (
                f"out-of-order promotion lease {lease.id} source ordinal "
                f"{source_ordinal} has conflicts that require manual resolution; "
                "conflicted paths: "
                + ", ".join(repr(path) for path in conflicted_paths)
            ),
            lease=lease,
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

    def _reuse(
        self, lease: Lease, expected_branch: str, *, apply: bool
    ) -> CommandResult:
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
            head_sha = self.git.head_sha(lease.worktree_path)
            if apply:
                lease = self.registry.touch(
                    lease.id,
                    expected_version=lease.version,
                    head_sha=head_sha,
                )
        except RuntimeError as error:
            return self._blocked("lease_conflict", str(error), lease=lease)

        if apply:
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
